"""Review detected patterns and manage confirmed commitments."""

from datetime import date, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .core import FREQUENCIES, add_evidence, categorize_description, category_rules, cents, connect, find_groups, next_on_or_after
from .forecast import bill_dates
from .presentation import friendly_date, line_chart
from .statements import parse_date, parse_money

bp = Blueprint('bills', __name__, url_prefix='/bills')


@bp.get('/')
def index():
    today = date.today()
    with connect(g.db) as con:
        bills = con.execute(
            """SELECT b.*, a.name AS account_name FROM bills b JOIN accounts a ON b.account_id=a.id
               ORDER BY b.active DESC,b.due_date,b.name"""
        ).fetchall()
        dismissed = {tuple(row) for row in con.execute('SELECT account_id,merchant_key FROM dismissed_candidates')}
        managed = {(bill['account_id'], bill['merchant_key']) for bill in bills}
        candidates = [candidate for candidate in find_groups(con)
                      if (candidate['account_id'], candidate['merchant']) not in managed]
        pending = [candidate for candidate in candidates if (candidate['account_id'], candidate['merchant']) not in dismissed]
        hidden = [candidate for candidate in candidates if (candidate['account_id'], candidate['merchant']) in dismissed]
        schedule = sorted(
            [(when, bill) for bill in bills if bill['active']
             for when in bill_dates(bill, today, today + timedelta(days=59))],
            key=lambda pair: (pair[0], pair[1]['name'].casefold()),
        )
        history = con.execute(
            """SELECT substr(p.paid_on,1,7) AS month,sum(p.amount_cents) AS total FROM bill_payments p
               GROUP BY month ORDER BY month DESC LIMIT 6"""
        ).fetchall()[::-1]
    trend = line_chart(
        [friendly_date(row['month'] + '-01', '%b %y') for row in history], [[row['total'] for row in history]], start_at_zero=True,
    ) if history else None
    return render_template(
        'bills.html', active='bills', bills=bills, pending=pending, hidden=hidden, schedule=schedule,
        frequencies=FREQUENCIES, trend=trend,
    )


@bp.route('/suggestion/<int:account_id>/<path:merchant>', methods=['GET', 'POST'])
def suggestion(account_id, merchant):
    today = date.today()
    with connect(g.db) as con:
        if request.method == 'POST':
            con.execute('BEGIN IMMEDIATE')
        candidate = next((row for row in find_groups(con, account_id) if row['merchant'] == merchant), None)
        if not candidate:
            abort(404)
        existing = con.execute('SELECT id FROM bills WHERE account_id=? AND merchant_key=?', (account_id, merchant)).fetchone()
        if existing:
            return redirect(url_for('bills.detail', bill_id=existing['id']))
        first = date.fromisoformat(candidate['suggested_next_date'])
        item = {
            'name': merchant.title(), 'account_id': account_id, 'amount': f"{candidate['average_amount']:.2f}",
            'frequency': candidate['frequency'], 'due_date': first.isoformat() if first >= today else '',
            'category': categorize_description(candidate['evidence'][-1]['description'], category_rules(con)),
        }
        if request.method == 'POST':
            action = request.form.get('action', 'save')
            if action in ('dismiss', 'restore'):
                if action == 'dismiss':
                    con.execute('INSERT OR IGNORE INTO dismissed_candidates VALUES (?,?)', (account_id, merchant))
                    flash('Suggestion dismissed.', 'success')
                else:
                    con.execute('DELETE FROM dismissed_candidates WHERE account_id=? AND merchant_key=?', (account_id, merchant))
                    flash('Suggestion restored.', 'success')
                return redirect(url_for('bills.index') + '#suggestions')
            item = {**dict(request.form), 'account_id': account_id}
            try:
                values = bill_values(item)
            except ValueError as exc:
                flash(str(exc), 'error')
            else:
                bill_id = save_bill(con, values, merchant_key=merchant)
                add_evidence(con, bill_id, candidate['evidence'])
                con.execute('DELETE FROM dismissed_candidates WHERE account_id=? AND merchant_key=?', (account_id, merchant))
                flash('Bill confirmed and supporting payments recorded.', 'success')
                return redirect(url_for('bills.detail', bill_id=bill_id))
        accounts = con.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchall()
    return render_template(
        'bill_form.html', active='bills', item=item, accounts=accounts, frequencies=FREQUENCIES,
        title='Review suggestion', candidate=candidate,
    )


@bp.route('/new', methods=['GET', 'POST'])
def new():
    with connect(g.db) as con:
        accounts = con.execute('SELECT * FROM accounts ORDER BY id').fetchall()
        item = {'account_id': accounts[0]['id'], 'frequency': 'monthly'}
        if request.method == 'POST':
            item = {**dict(request.form), 'account_id': request.form.get('account_id', type=int)}
            try:
                if item['account_id'] not in {a['id'] for a in accounts}:
                    raise ValueError('Choose a payment account.')
                values = bill_values(item)
            except ValueError as exc:
                flash(str(exc), 'error')
            else:
                bill_id = save_bill(con, values)
                flash('Bill added.', 'success')
                return redirect(url_for('bills.detail', bill_id=bill_id))
    return render_template('bill_form.html', active='bills', item=item, accounts=accounts, frequencies=FREQUENCIES, title='Add a bill')


@bp.route('/<int:bill_id>', methods=['GET', 'POST'])
def detail(bill_id):
    with connect(g.db) as con:
        bill = get_bill(con, bill_id)
        accounts = con.execute('SELECT * FROM accounts WHERE id=?', (bill['account_id'],)).fetchall()
        item = dict(bill, amount=f"{bill['amount_cents'] / 100:.2f}")
        if request.method == 'POST':
            action = request.form.get('action', 'save')
            if action in ('archive', 'activate'):
                con.execute('UPDATE bills SET active=? WHERE id=?', (int(action == 'activate'), bill_id))
                flash('Bill archived.' if action == 'archive' else 'Bill reactivated.', 'success')
                return redirect(url_for('bills.detail', bill_id=bill_id))
            item = {**dict(request.form), 'account_id': bill['account_id']}
            try:
                values = bill_values(item)
            except ValueError as exc:
                flash(str(exc), 'error')
            else:
                if values['due_date'] == bill['due_date'] and values['frequency'] == bill['frequency']:
                    values['anchor_date'] = bill['anchor_date']
                save_bill(con, values, bill_id=bill_id)
                flash('Bill details updated.', 'success')
                return redirect(url_for('bills.detail', bill_id=bill_id))
        history = con.execute('SELECT * FROM bill_payments WHERE bill_id=? ORDER BY paid_on DESC,id DESC', (bill_id,)).fetchall()
        eligible = con.execute(
            """SELECT t.* FROM transactions t WHERE t.account_id=? AND t.amount<0 AND t.kind='purchase'
               AND NOT EXISTS (SELECT 1 FROM bill_payments p WHERE p.transaction_id=t.id)
               ORDER BY txn_date DESC,id DESC LIMIT 100""", (bill['account_id'],),
        ).fetchall()
    next_date = next(bill_dates(bill, date.today(), date.today() + timedelta(days=366)), None)
    return render_template(
        'bill_form.html', active='bills', item=item, accounts=accounts, frequencies=FREQUENCIES,
        title=bill['name'], bill=bill, history=history, eligible=eligible, today=date.today(), next_date=next_date,
    )


@bp.post('/<int:bill_id>/payment')
def payment(bill_id):
    with connect(g.db) as con:
        con.execute('BEGIN IMMEDIATE')
        bill = get_bill(con, bill_id)
        link = request.form.get('transaction_id', type=int)
        try:
            if link is not None:
                txn = con.execute(
                    "SELECT * FROM transactions WHERE id=? AND account_id=? AND amount<0 AND kind='purchase'",
                    (link, bill['account_id']),
                ).fetchone()
                if not txn:
                    raise ValueError('Choose a purchase from this payment account.')
                paid_on, amount, note = txn['txn_date'], -cents(txn['amount']), txn['description']
            else:
                paid_on = parse_date(request.form.get('paid_on', '')).isoformat()
                amount = cents(parse_money(request.form.get('amount', '')))
                if amount <= 0:
                    raise ValueError('Enter a payment amount greater than zero.')
                note = request.form.get('note', '').strip()[:160]
        except ValueError as exc:
            flash(str(exc), 'error')
        else:
            saved = con.execute(
                """INSERT OR IGNORE INTO bill_payments (bill_id,transaction_id,paid_on,amount_cents,note)
                   VALUES (?,?,?,?,?)""", (bill_id, link, paid_on, amount, note),
            )
            if not saved.rowcount:
                flash('This transaction is already linked to a bill.', 'info')
            else:
                if request.form.get('advance_due'):
                    due = date.fromisoformat(bill['due_date'])
                    paid = date.fromisoformat(paid_on)
                    anchor = date.fromisoformat(bill['anchor_date']) if bill['anchor_date'] else due
                    scheduled = next_on_or_after(due, bill['frequency'], max(paid, due) + timedelta(days=1), anchor)
                    con.execute('UPDATE bills SET due_date=?,anchor_date=? WHERE id=?', (scheduled.isoformat(), anchor.isoformat(), bill_id))
                flash('Payment recorded.', 'success')
    return redirect(url_for('bills.detail', bill_id=bill_id) + '#payment-history')


@bp.post('/<int:bill_id>/payment/<int:payment_id>/delete')
def remove_payment(bill_id, payment_id):
    with connect(g.db) as con:
        get_bill(con, bill_id)
        if not con.execute('DELETE FROM bill_payments WHERE id=? AND bill_id=?', (payment_id, bill_id)).rowcount:
            abort(404)
    flash('Payment removed from history.', 'success')
    return redirect(url_for('bills.detail', bill_id=bill_id) + '#payment-history')


def get_bill(con, bill_id):
    bill = con.execute(
        'SELECT b.*,a.name AS account_name FROM bills b JOIN accounts a ON b.account_id=a.id WHERE b.id=?',
        (bill_id,),
    ).fetchone()
    if not bill:
        abort(404)
    return bill


def bill_values(item):
    name = item.get('name', '').strip()
    category = item.get('category', '').strip()
    frequency = item.get('frequency')
    if not 1 <= len(name) <= 80 or len(category) > 60 or frequency not in FREQUENCIES:
        raise ValueError('Enter a bill name and select a frequency.')
    date_text = item.get('due_date', '')
    if not date_text:
        raise ValueError('Choose the first or next expected payment date.')
    due = parse_date(date_text).isoformat()
    amount = cents(parse_money(item.get('amount', '')))
    if amount <= 0:
        raise ValueError('Enter a bill amount greater than zero.')
    return {
        'name': name, 'account_id': item['account_id'], 'category': category, 'frequency': frequency,
        'due_date': due, 'anchor_date': due, 'amount_cents': amount, 'autopay': int(bool(item.get('autopay'))),
    }


def save_bill(con, values, merchant_key=None, bill_id=None):
    if bill_id is None:
        return con.execute(
            """INSERT INTO bills (account_id,merchant_key,name,amount_cents,due_date,frequency,category,autopay,anchor_date)
               VALUES (:account_id,:merchant_key,:name,:amount_cents,:due_date,:frequency,:category,:autopay,:anchor_date)""",
            dict(values, merchant_key=merchant_key),
        ).lastrowid
    con.execute(
        """UPDATE bills SET name=:name,amount_cents=:amount_cents,due_date=:due_date,frequency=:frequency,
           category=:category,autopay=:autopay,anchor_date=:anchor_date WHERE id=:id""", dict(values, id=bill_id),
    )
    return bill_id
