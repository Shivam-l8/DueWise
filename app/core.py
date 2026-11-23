"""Workspace storage, transaction classification, and recurring-payment detection."""

import re
import sqlite3
import unicodedata
from calendar import monthrange
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from statistics import median

DB_PATH = Path(__file__).resolve().parents[1] / 'duewise.db'

KINDS = {
    'purchase': 'Purchase', 'income': 'Income', 'refund': 'Refund',
    'transfer': 'Transfer', 'card_payment': 'Credit card payment', 'other': 'Other',
}
FREQUENCIES = {
    'weekly': 'Weekly', 'biweekly': 'Every two weeks', 'monthly': 'Monthly',
    'quarterly': 'Quarterly', 'yearly': 'Yearly',
}
DEFAULT_RULES = (
    ('internet', 'Utilities'), ('phone plan', 'Utilities'), ('electric', 'Utilities'),
    ('subscription', 'Subscriptions'), ('streaming', 'Subscriptions'),
    ('grocer', 'Groceries'), ('coffee', 'Dining'), ('book store', 'Shopping'),
    ('transit', 'Transport'), ('pharmacy', 'Health'), ('rent', 'Housing'),
)


@contextmanager
def connect(db_path=DB_PATH):
    con = sqlite3.connect(db_path, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys = ON')
    try:
        with con:
            yield con
    finally:
        con.close()


def init_db(db_path=DB_PATH):
    with connect(db_path) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, kind TEXT NOT NULL DEFAULT 'checking'
        );
        CREATE TABLE IF NOT EXISTS category_rules (
          id INTEGER PRIMARY KEY AUTOINCREMENT, keyword TEXT NOT NULL UNIQUE, category TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transactions (
          id INTEGER PRIMARY KEY AUTOINCREMENT, txn_date TEXT NOT NULL, description TEXT NOT NULL,
          amount REAL NOT NULL, source TEXT NOT NULL DEFAULT 'csv',
          account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
          kind TEXT NOT NULL, category_override TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS recurring_candidates (
          id INTEGER PRIMARY KEY AUTOINCREMENT, merchant TEXT NOT NULL UNIQUE, average_amount REAL NOT NULL,
          last_date TEXT NOT NULL, suggested_next_date TEXT NOT NULL, occurrences INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'suggested', autopay INTEGER NOT NULL DEFAULT 0,
          category TEXT NOT NULL DEFAULT 'Uncategorized'
        );
        """)
        if 'kind' not in {r['name'] for r in con.execute('PRAGMA table_info(accounts)')}:
            con.execute("ALTER TABLE accounts ADD COLUMN kind TEXT NOT NULL DEFAULT 'checking'")
        account = con.execute('SELECT id FROM accounts ORDER BY id LIMIT 1').fetchone()
        if account is None:
            account_id = con.execute("INSERT INTO accounts(name) VALUES ('Checking')").lastrowid
        else:
            account_id = account['id']
        if 'account_id' not in {r['name'] for r in con.execute('PRAGMA table_info(transactions)')}:
            old = con.execute('SELECT * FROM transactions ORDER BY id').fetchall()
            con.execute('ALTER TABLE transactions RENAME TO transactions_legacy')
            con.execute("""
                CREATE TABLE transactions (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, txn_date TEXT NOT NULL, description TEXT NOT NULL,
                  amount REAL NOT NULL, source TEXT NOT NULL DEFAULT 'csv',
                  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                  kind TEXT NOT NULL, category_override TEXT NOT NULL DEFAULT ''
                )
            """)
            con.executemany(
                "INSERT INTO transactions VALUES (?,?,?,?,?,?,?, '')",
                [(r['id'], r['txn_date'], r['description'], r['amount'], r['source'], account_id,
                  infer_kind(r['description'], r['amount'])) for r in old],
            )
            con.execute('DROP TABLE transactions_legacy')
        con.executescript("""
        CREATE INDEX IF NOT EXISTS transaction_scope ON transactions (account_id, txn_date, amount);
        CREATE TABLE IF NOT EXISTS import_batches (
          id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS import_rows (
          id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
          account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
          filename TEXT NOT NULL, reference TEXT NOT NULL, source TEXT NOT NULL,
          txn_date TEXT NOT NULL, description TEXT NOT NULL, amount TEXT NOT NULL, kind TEXT NOT NULL,
          category_override TEXT NOT NULL DEFAULT '', excluded INTEGER NOT NULL DEFAULT 0,
          keep_duplicate INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS import_batch_rows ON import_rows(batch_id, id);
        CREATE TABLE IF NOT EXISTS bills (
          id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
          merchant_key TEXT, name TEXT NOT NULL, amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
          due_date TEXT NOT NULL, frequency TEXT NOT NULL, category TEXT NOT NULL DEFAULT '',
          autopay INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
          anchor_date TEXT, UNIQUE(account_id, merchant_key)
        );
        CREATE TABLE IF NOT EXISTS bill_payments (
          id INTEGER PRIMARY KEY AUTOINCREMENT, bill_id INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
          transaction_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
          paid_on TEXT NOT NULL, amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
          note TEXT NOT NULL DEFAULT '', UNIQUE(bill_id, transaction_id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS bill_payment_transaction ON bill_payments(transaction_id)
          WHERE transaction_id IS NOT NULL;
        CREATE TABLE IF NOT EXISTS dismissed_candidates (
          account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
          merchant_key TEXT NOT NULL, PRIMARY KEY (account_id, merchant_key)
        );
        CREATE TABLE IF NOT EXISTS workspace_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS forecasts (
          account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
          assumptions TEXT NOT NULL
        );
        """)
        if 'anchor_date' not in {r['name'] for r in con.execute('PRAGMA table_info(bills)')}:
            con.execute('ALTER TABLE bills ADD COLUMN anchor_date TEXT')
        imported = con.execute("SELECT 1 FROM workspace_meta WHERE key='legacy_bills_migrated'").fetchone()
        if not imported:
            migrate_legacy_bills(con, account_id)
            con.execute("INSERT INTO workspace_meta VALUES ('legacy_bills_migrated', '1')")


def migrate_legacy_bills(con, account_id):
    groups = {r['merchant']: r for r in find_groups(con, account_id)}
    for old in con.execute("SELECT * FROM recurring_candidates WHERE status <> 'suggested'"):
        key = old['merchant']
        if old['status'] == 'dismissed':
            con.execute('INSERT OR IGNORE INTO dismissed_candidates VALUES (?,?)', (account_id, key))
        elif old['status'] == 'confirmed' and old['average_amount'] > 0:
            bill = con.execute(
                """INSERT OR IGNORE INTO bills
                   (account_id, merchant_key, name, amount_cents, due_date, frequency, category, autopay, anchor_date)
                   VALUES (?, ?, ?, ?, ?, 'monthly', ?, ?, ?)""",
                (account_id, key, key.title(), cents(old['average_amount']), old['suggested_next_date'],
                 old['category'], old['autopay'], old['last_date']),
            )
            if bill.rowcount and key in groups:
                add_evidence(con, bill.lastrowid, groups[key]['evidence'])


def cents(amount):
    return round(float(amount) * 100)


def default_account(con):
    return con.execute('SELECT id FROM accounts ORDER BY id LIMIT 1').fetchone()['id']


def infer_kind(description, amount):
    text = description.casefold()
    if re.search(r'\b(?:credit card (?:repayment|payment|pmt)|card repayment|card payment|visa payment|mastercard payment|payment thank you|payment received)\b', text):
        return 'card_payment'
    if re.search(r'\b(?:transfer|e[- ]?transfer|etransfer|move money)\b', text):
        return 'transfer'
    if re.search(r'\b(?:refund|reversal|return credit)\b', text):
        return 'refund'
    return 'purchase' if float(amount) < 0 else 'income' if float(amount) > 0 else 'other'


def category_rules(con):
    return con.execute(
        "SELECT * FROM category_rules WHERE trim(keyword) <> '' ORDER BY length(keyword) DESC, id"
    ).fetchall()


def categorize_description(description, rules):
    description = description.casefold()
    for rule in rules:
        if rule['keyword'].casefold() in description:
            return rule['category']
    return 'Uncategorized'


def transaction_category(row, rules):
    return row['category_override'] or categorize_description(row['description'], rules)


def monthly_spending(con, month, rules, account_id=None):
    totals = defaultdict(int)
    if month:
        expenses = con.execute(
            """SELECT * FROM transactions WHERE txn_date LIKE ? AND amount < 0 AND kind = 'purchase'
               AND (? IS NULL OR account_id = ?)""", (month + '-%', account_id, account_id),
        )
        for row in expenses:
            totals[transaction_category(row, rules)] += -cents(row['amount'])
    return sorted(totals.items(), key=lambda item: (-item[1], item[0]))


def duplicate_key(account_id, txn_date, description, amount):
    description = ' '.join(unicodedata.normalize('NFKC', description).casefold().split())
    return account_id, txn_date, description, cents(amount)


def known_duplicates(con, start=None, end=None):
    rows = con.execute(
        """SELECT * FROM transactions
           WHERE (? IS NULL OR txn_date >= ?) AND (? IS NULL OR txn_date <= ?)""",
        (start, start, end, end),
    )
    return {duplicate_key(r['account_id'], r['txn_date'], r['description'], r['amount']) for r in rows}


def insert_transaction(con, row, account_id, source):
    return con.execute(
        """INSERT INTO transactions (account_id, txn_date, description, amount, kind, category_override, source)
           VALUES (?,?,?,?,?,?,?)""",
        (account_id, row['txn_date'], row['description'], float(row['amount']),
         row.get('kind') or infer_kind(row['description'], row['amount']), row.get('category_override', ''), source),
    ).lastrowid


def import_transactions(rows, db_path=DB_PATH):
    """Import already validated rows with the legacy programmatic entry point."""
    count = 0
    with connect(db_path) as con:
        con.execute('BEGIN IMMEDIATE')
        account_id = default_account(con)
        seen = known_duplicates(con)
        for row in rows:
            key = duplicate_key(account_id, row['txn_date'], row['description'], row['amount'])
            if key not in seen:
                insert_transaction(con, row, account_id, row.get('source', 'csv'))
                seen.add(key)
                count += 1
    return count


def parse_csv(file_data):
    """Compatibility entry point for already validated programmatic CSV imports."""
    from .statements import csv_rows, validate_row
    rows, errors = [], []
    for raw in csv_rows(file_data):
        clean, problem = validate_row(raw)
        if problem:
            errors.append(f"{raw['reference']}: {problem}")
        else:
            rows.append({name: clean[name] for name in ('txn_date', 'description', 'amount')})
    return rows, errors


def normalize_merchant(value):
    value = unicodedata.normalize('NFKC', str(value)).casefold()
    value = re.sub(r'\b(?:ref|reference|auth|terminal|id)\s*[:#-]?\s*(?=[a-z0-9-]*\d)[a-z0-9-]+\b', ' ', value)
    value = re.sub(r'(?:^|\s)#\s*\d+\b', ' ', value)
    value = re.sub(r'\b(?:payment|purchase|debit|pos|www|com|preauthorized|pre-authorized|recurring)\b', ' ', value)
    return ' '.join(re.findall(r'[^\W_]+', value))


def add_months(anchor, months):
    year, month = divmod(anchor.year * 12 + anchor.month - 1 + months, 12)
    return date(year, month + 1, min(anchor.day, monthrange(year, month + 1)[1]))


def advance(anchor, frequency, steps=1):
    if frequency in ('weekly', 'biweekly'):
        return anchor + timedelta(days=(7 if frequency == 'weekly' else 14) * steps)
    return add_months(anchor, {'monthly': 1, 'quarterly': 3, 'yearly': 12}[frequency] * steps)


def next_on_or_after(anchor, frequency, start, reference=None):
    reference = reference or anchor
    if start <= anchor:
        return anchor
    if frequency in ('weekly', 'biweekly'):
        days = 7 if frequency == 'weekly' else 14
        return anchor + timedelta(days=((start - anchor).days + days - 1) // days * days)
    months = {'monthly': 1, 'quarterly': 3, 'yearly': 12}[frequency]
    delta = (start.year - anchor.year) * 12 + start.month - anchor.month
    step = delta // months
    # Compute from the original day even when the first date lands in a short month.
    year, month = divmod(anchor.year * 12 + anchor.month - 1 + months * step, 12)
    due = date(year, month + 1, min(reference.day, monthrange(year, month + 1)[1]))
    if due < start:
        year, month = divmod(year * 12 + month + months, 12)
        due = date(year, month + 1, min(reference.day, monthrange(year, month + 1)[1]))
    return due


CADENCES = {
    'weekly': (5, 9, 3), 'biweekly': (11, 17, 3), 'monthly': (24, 38, 2),
    'quarterly': (80, 100, 3), 'yearly': (350, 380, 2),
}


def strongest_run(records, frequency):
    low, high, minimum = CADENCES[frequency]
    best, run = [], [records[0]]
    for entry in records[1:]:
        gap = (entry[0] - run[-1][0]).days
        if low <= gap <= high:
            run.append(entry)
        else:
            if len(run) >= len(best):
                best = run
            run = [entry]
    if len(run) >= len(best):
        best = run
    if len(best) < minimum:
        return []
    amounts = [r[1] for r in best]
    middle = median(amounts)
    tolerance = max(200, middle * (.35 if len(best) >= 3 else .20))
    return best if max(abs(amount - middle) for amount in amounts) <= tolerance else []


def find_groups(con, account_id=None):
    records = con.execute(
        """SELECT t.*, a.name AS account_name FROM transactions t JOIN accounts a ON t.account_id=a.id
           WHERE t.amount < 0 AND t.kind = 'purchase' AND (? IS NULL OR a.id = ?)
           ORDER BY t.txn_date,t.id""", (account_id, account_id),
    )
    groups = defaultdict(list)
    for row in records:
        merchant = normalize_merchant(row['description'])
        if merchant:
            groups[row['account_id'], merchant].append((date.fromisoformat(row['txn_date']), -cents(row['amount']), row))
    candidates = []
    for (account, merchant), entries in groups.items():
        runs = [(f, strongest_run(entries, f)) for f in CADENCES]
        frequency, matched = max(runs, key=lambda pair: (len(pair[1]), pair[1][-1][0] if pair[1] else date.min))
        if not matched or len(matched) < len(entries) * .6:
            continue
        gaps = [(cur[0] - prev[0]).days for prev, cur in zip(matched, matched[1:])]
        amounts = [amount for _, amount, _ in matched]
        last = matched[-1][0]
        candidate_date = advance(last, frequency)
        if frequency == 'monthly':
            target_day = round(median(d.day for d, _, _ in matched))
            end = monthrange(candidate_date.year, candidate_date.month)[1]
            if all(d.day == monthrange(d.year, d.month)[1] for d, _, _ in matched):
                target_day = end
            candidate_date = candidate_date.replace(day=min(target_day, end))
        candidates.append({
            'account_id': account, 'account_name': matched[-1][2]['account_name'],
            'merchant': merchant, 'frequency': frequency, 'evidence': [row for _, _, row in matched],
            'average_amount': round(sum(amounts) / len(amounts)) / 100, 'last_date': last.isoformat(),
            'suggested_next_date': candidate_date.isoformat(), 'occurrences': len(matched),
            'min_gap': min(gaps), 'max_gap': max(gaps), 'min_amount': min(amounts), 'max_amount': max(amounts),
        })
    return sorted(candidates, key=lambda row: (-row['occurrences'], row['merchant'], row['account_id']))


def find_candidates(db_path=DB_PATH):
    with connect(db_path) as con:
        return find_groups(con)


def refresh_candidates(db_path=DB_PATH):
    # Keep the legacy CLI/programmatic cache available to existing consumers.
    candidates = find_candidates(db_path)
    with connect(db_path) as con:
        for row in candidates:
            con.execute("""
                INSERT INTO recurring_candidates (merchant,average_amount,last_date,suggested_next_date,occurrences)
                VALUES (:merchant,:average_amount,:last_date,:suggested_next_date,:occurrences)
                ON CONFLICT(merchant) DO UPDATE SET average_amount=excluded.average_amount,
                last_date=excluded.last_date,suggested_next_date=excluded.suggested_next_date,
                occurrences=excluded.occurrences
            """, row)
    return len(candidates)


def add_evidence(con, bill_id, evidence):
    con.executemany(
        """INSERT OR IGNORE INTO bill_payments (bill_id,transaction_id,paid_on,amount_cents,note)
           VALUES (?,?,?,?,?)""",
        [(bill_id, r['id'], r['txn_date'], -cents(r['amount']), r['description']) for r in evidence],
    )
