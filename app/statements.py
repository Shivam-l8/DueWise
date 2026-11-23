"""Statement extraction and editable import staging."""

import csv
import io
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .core import KINDS, duplicate_key, infer_kind, insert_transaction, known_duplicates

MAX_ROWS = 1500
MAX_PAGES = 35
MAX_FILES = 8
MAX_FILE_SIZE = 4 * 1024 * 1024
AMOUNT_LIMIT = Decimal('99999999.99')
DATE_PREFIX = re.compile(
    r'^((?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{4}'
    r'|[A-Za-z]{3}\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+[A-Za-z]{3}\s+\d{4}))\s+(.*)$'
)
CASH_TOKEN = r'(?:\(?[+\-−]?\$?\d[\d,]*\.\d{2}\)?(?:\s*(?:CR|DR))?)'
PDF_AMOUNT = re.compile(r'\s+(' + CASH_TOKEN + r')\s*$', re.I)
CSV_ALIASES = {
    'date': ('date', 'transaction date', 'posted date', 'posting date', 'txn date'),
    'description': ('description', 'details', 'merchant', 'name', 'transaction description', 'payee'),
    'amount': ('amount', 'transaction amount', 'signed amount'),
    'debit': ('debit', 'debits', 'withdrawal', 'withdrawals', 'money out'),
    'credit': ('credit', 'credits', 'deposit', 'deposits', 'money in'),
}


def parse_date(text):
    value = str(text).strip()
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%m/%d/%Y', '%m-%d-%Y', '%b %d %Y', '%b %d, %Y', '%d %b %Y'):
        try:
            result = datetime.strptime(value, fmt).date()
        except ValueError:
            continue
        if 1900 <= result.year <= 2199:
            return result
    raise ValueError('Enter a date with the year in YYYY-MM-DD format.')


def parse_money(text):
    value = str(text).strip().upper().replace('−', '-')
    suffix = re.search(r'\s*(CR|DR)$', value)
    if suffix:
        value = value[:suffix.start()].strip()
    parenthesized = value.startswith('(') and value.endswith(')')
    if parenthesized:
        value = value[1:-1].strip()
    if not re.fullmatch(r'[+-]?\$?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)', value) or len(value) > 24:
        raise ValueError('Enter a valid signed amount.')
    negative = parenthesized or (suffix is not None and suffix[1] == 'DR')
    if suffix and suffix[1] == 'CR' and (parenthesized or value.startswith('-')):
        raise ValueError('The amount has conflicting credit and negative signs.')
    value = value.replace('$', '').replace(',', '')
    try:
        amount = Decimal(value).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError('Enter a valid signed amount.') from exc
    if abs(amount) > AMOUNT_LIMIT:
        raise ValueError('The amount is outside the supported range.')
    return -abs(amount) if negative else amount


def split_amount(debit, credit):
    debit, credit = (str(v).strip() for v in (debit, credit))
    empty = ('', '-', '—', '–')
    if debit in empty and credit in empty:
        return ''
    try:
        outflow = Decimal(0) if debit in empty else abs(parse_money(debit))
        inflow = Decimal(0) if credit in empty else abs(parse_money(credit))
    except ValueError:
        return ''
    if outflow and inflow:
        return ''
    return str(inflow - outflow)


def raw_row(txn_date='', description='', amount='', reference='', source='csv'):
    # Keep raw values editable while bounding database and HTML size.
    return {
        'txn_date': txn_date.strip()[:48], 'description': description.strip()[:500],
        'amount': amount.strip()[:48], 'kind': '', 'category_override': '',
        'reference': reference, 'source': source,
    }


def validate_row(row):
    row = dict(row)
    problems = []
    try:
        row['txn_date'] = parse_date(row['txn_date']).isoformat()
    except ValueError as exc:
        problems.append(str(exc))
    try:
        row['amount'] = float(parse_money(row['amount']))
    except ValueError as exc:
        problems.append(str(exc))
    row['description'] = row['description'].strip()
    if not row['description'] or len(row['description']) > 160:
        problems.append('Enter a description up to 160 characters.')
    if row.get('kind') and row['kind'] not in KINDS:
        problems.append('Choose a transaction type.')
    if problems:
        return None, ' '.join(problems)
    row['kind'] = row.get('kind') or infer_kind(row['description'], row['amount'])
    return row, None


def csv_rows(file_data):
    try:
        content = file_data.decode('utf-8-sig')
    except UnicodeError as exc:
        raise ValueError('Save the statement as a UTF-8 CSV and try again.') from exc
    first = next((line for line in content.splitlines() if line.strip()), '')
    if not first or '\0' in content:
        raise ValueError('This CSV has no readable header.')
    try:
        dialect = csv.Sniffer().sniff(first, delimiters=',;\t')
    except csv.Error:
        dialect = csv.excel
    try:
        reader = csv.DictReader(io.StringIO(content.lstrip('\r\n'), newline=''), dialect=dialect, strict=True)
        headers = {' '.join(name.strip().lower().replace('_', ' ').split()): name for name in reader.fieldnames or []}
        if len(headers) != len(reader.fieldnames or []):
            raise ValueError('Use unique column names in the CSV header.')
        fields = {
            key: next((headers[alias] for alias in aliases if alias in headers), None)
            for key, aliases in CSV_ALIASES.items()
        }
        if not (fields['date'] and fields['description'] and (fields['amount'] or fields['debit'] or fields['credit'])):
            raise ValueError('CSV needs date, description and either amount or debit/credit columns.')
        rows = []
        for values in reader:
            def value(field):
                return values.get(fields[field]) or ''

            amount = value('amount') if fields['amount'] else split_amount(value('debit'), value('credit'))
            if None in values:
                amount = ''
            rows.append(raw_row(value('date'), value('description'), amount, f'Line {reader.line_num}'))
            if len(rows) > MAX_ROWS:
                raise ValueError(f'Use at most {MAX_ROWS:,} rows in a batch.')
    except csv.Error as exc:
        raise ValueError('This CSV contains malformed or oversized fields.') from exc
    if not rows:
        raise ValueError('This CSV has no transaction rows.')
    return rows


def pdf_text_pages(file_data):
    if not file_data[:1024].lstrip().startswith(b'%PDF-'):
        raise ValueError('This file does not have a valid PDF header.')
    try:
        import pdfplumber
    except ImportError:
        # PDFium also works when pdfminer cannot load the platform crypto library.
        import pypdfium2 as pdfium
        with pdfium.PdfDocument(file_data) as document:
            if len(document) > MAX_PAGES:
                raise ValueError(f'Use PDFs with at most {MAX_PAGES} pages.')
            for page in document:
                textpage = page.get_textpage()
                try:
                    yield textpage.get_text_range()
                finally:
                    textpage.close()
                    page.close()
    else:
        with pdfplumber.open(io.BytesIO(file_data)) as document:
            if len(document.pages) > MAX_PAGES:
                raise ValueError(f'Use PDFs with at most {MAX_PAGES} pages.')
            for page in document.pages:
                yield page.extract_text(layout=True) or ''
                page.close()


def parse_pdf_tail(tail, layout):
    if layout == 'signed':
        match = PDF_AMOUNT.search(tail)
        return (tail[:match.start()], match[1]) if match else (tail, '')
    cells = re.split(r'\s{2,}|\s*\|\s*', tail.strip())
    if len(cells) >= 3:
        return ' '.join(cells[:-2]), split_amount(cells[-2], cells[-1])
    match = re.match(r'^(.*?)\s+(' + CASH_TOKEN + r'|[-—–])\s+(' + CASH_TOKEN + r'|[-—–])$', tail, re.I)
    if match:
        return match[1], split_amount(match[2], match[3])
    return tail, ''


def pdf_rows(file_data):
    rows, has_text, layout = [], False, None
    try:
        for page_number, text in enumerate(pdf_text_pages(file_data), 1):
            has_text = has_text or bool(text.strip())
            for line_number, line in enumerate(text.splitlines(), 1):
                line = line.strip()
                header = line.lower()
                if re.match(r'^(?:transaction |posted )?date\b', header) and re.search(r'\b(?:description|details|merchant)\b', header):
                    # A running balance can otherwise be mistaken for the signed amount.
                    layout = None
                    if re.search(r'\b(?:balance|running total)\b', header):
                        continue
                    if re.search(r'\b(?:debits?|withdrawals?)\b.*\b(?:credits?|deposits?)\b', header):
                        layout = 'split'
                    elif re.search(r'\bamount\b', header):
                        layout = 'signed'
                    continue
                match = DATE_PREFIX.match(line)
                if not layout or not match:
                    continue
                tail = match[2].strip(' |')
                if re.match(r'^(?:opening|closing|beginning|ending|previous|new) balance\b', tail, re.I):
                    continue
                description, amount = parse_pdf_tail(tail, layout)
                rows.append(raw_row(match[1], description, amount, f'Page {page_number}, line {line_number}', 'pdf'))
                if len(rows) > MAX_ROWS:
                    raise ValueError(f'Use at most {MAX_ROWS:,} rows in a batch.')
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('This PDF could not be read. Check that it opens and is free of password protection.') from exc
    if not has_text:
        raise ValueError('This PDF has no selectable text. Use a text-based statement or export a CSV.')
    if not rows:
        raise ValueError('PDF layout not supported. Use a Date / Description / Amount or Date / Description / Debit / Credit table.')
    return rows


def review_import(con, batch_id):
    raw = con.execute(
        """SELECT r.*, a.name AS account_name FROM import_rows r JOIN accounts a ON a.id=r.account_id
           WHERE batch_id=? ORDER BY r.id""", (batch_id,),
    ).fetchall()
    reviewed = []
    for row in raw:
        clean, error = validate_row(row)
        reviewed.append({'raw': row, 'clean': clean, 'error': error, 'duplicate': False})
    dates = [row['clean']['txn_date'] for row in reviewed if row['clean']]
    seen = known_duplicates(con, min(dates), max(dates)) if dates else set()
    for row in reviewed:
        if not row['clean']:
            continue
        item = row['clean']
        key = duplicate_key(item['account_id'], item['txn_date'], item['description'], item['amount'])
        row['duplicate'] = key in seen
        if not item['excluded']:
            seen.add(key)
    return reviewed


class ImportNeedsReview(ValueError):
    pass


def commit_import(con, batch_id):
    """Recompute validation and duplicates while holding the write lock."""
    con.execute('BEGIN IMMEDIATE')
    batch = con.execute('SELECT id FROM import_batches WHERE id=?', (batch_id,)).fetchone()
    if not batch:
        return None
    rows = review_import(con, batch_id)
    if any(row['error'] and not row['raw']['excluded'] for row in rows):
        raise ImportNeedsReview('Correct or exclude each incomplete row before importing.')
    added = 0
    for row in rows:
        item = row['raw']
        if item['excluded'] or (row['duplicate'] and not item['keep_duplicate']):
            continue
        insert_transaction(con, row['clean'], item['account_id'], item['source'])
        added += 1
    con.execute('DELETE FROM import_batches WHERE id=?', (batch_id,))
    return added, len(rows) - added
