"""Multi-file import and transaction review routes."""

from pathlib import Path

from flask import Blueprint, abort, flash, g, redirect, render_template, request, send_from_directory, url_for

from .core import KINDS, connect
from .statements import (
    ImportNeedsReview, MAX_FILES, MAX_FILE_SIZE, MAX_ROWS, commit_import, csv_rows, pdf_rows, review_import,
)
from .workspace import SAMPLES

bp = Blueprint('imports', __name__, url_prefix='/imports')


@bp.get('/')
def index():
    with connect(g.db) as con:
        accounts = con.execute('SELECT * FROM accounts ORDER BY id').fetchall()
        batches = con.execute(
            """SELECT b.id,b.created_at,count(*) AS rows,count(DISTINCT r.filename) AS files FROM import_batches b
               JOIN import_rows r ON b.id=r.batch_id GROUP BY b.id ORDER BY b.id DESC"""
        ).fetchall()
    return render_template('imports.html', active='imports', accounts=accounts, batches=batches, max_files=MAX_FILES)


@bp.get('/sample/<kind>')
def sample(kind):
    files = {'csv': 'sample_transactions.csv', 'pdf': 'sample_statement.pdf', 'split-pdf': 'sample_debit_credit.pdf'}
    if kind not in files:
        abort(404)
    return send_from_directory(SAMPLES, files[kind], as_attachment=True)


@bp.post('/')
def upload():
    uploads = [file for file in request.files.getlist('files') if file.filename]
    account_id = request.form.get('account_id', type=int)
    with connect(g.db) as con:
        if not con.execute('SELECT 1 FROM accounts WHERE id=?', (account_id,)).fetchone():
            flash('Choose the account for these statements.', 'error')
            return redirect(url_for('imports.index'))
    if not 1 <= len(uploads) <= MAX_FILES:
        flash(f'Choose between one and {MAX_FILES} statements.', 'error')
        return redirect(url_for('imports.index'))
    rows = []
    for file in uploads:
        filename = Path(file.filename.replace('\\', '/')).name[:120]
        suffix = Path(filename).suffix.lower()
        data = file.read(MAX_FILE_SIZE + 1)
        try:
            if len(data) > MAX_FILE_SIZE:
                raise ValueError('Use files up to 4 MB each.')
            if suffix == '.pdf':
                extracted = pdf_rows(data)
            elif suffix == '.csv':
                extracted = csv_rows(data)
            else:
                raise ValueError('Choose a PDF or CSV statement.')
            if len(rows) + len(extracted) > MAX_ROWS:
                raise ValueError(f'The batch would exceed {MAX_ROWS:,} rows. Upload this file separately.')
        except ValueError as exc:
            flash(f'{filename}: {exc}', 'error')
            continue
        rows.extend(dict(row, filename=filename, account_id=account_id) for row in extracted)
    if not rows:
        return redirect(url_for('imports.index'))
    with connect(g.db) as con:
        con.execute('BEGIN IMMEDIATE')
        if not con.execute('SELECT 1 FROM accounts WHERE id=?', (account_id,)).fetchone():
            flash('That account is no longer available. Choose another account and upload again.', 'error')
            return redirect(url_for('imports.index'))
        batch_id = con.execute('INSERT INTO import_batches DEFAULT VALUES').lastrowid
        con.executemany(
            """INSERT INTO import_rows (batch_id,account_id,filename,reference,source,txn_date,description,amount,kind)
               VALUES (:batch_id,:account_id,:filename,:reference,:source,:txn_date,:description,:amount,:kind)""",
            [dict(row, batch_id=batch_id) for row in rows],
        )
    return redirect(url_for('imports.review', batch_id=batch_id))


@bp.get('/<int:batch_id>')
def review(batch_id):
    with connect(g.db) as con:
        if not con.execute('SELECT 1 FROM import_batches WHERE id=?', (batch_id,)).fetchone():
            abort(404)
        rows = review_import(con, batch_id)
        accounts = con.execute('SELECT * FROM accounts ORDER BY id').fetchall()
    counts = {
        'errors': sum(bool(row['error']) and not row['raw']['excluded'] for row in rows),
        'duplicates': sum(row['duplicate'] and not row['raw']['excluded'] and not row['raw']['keep_duplicate'] for row in rows),
        'excluded': sum(row['raw']['excluded'] for row in rows),
    }
    tab = request.args.get('tab', '')
    visible = [r for r in rows if not r['raw']['excluded'] and (
        (tab == 'errors' and r['error']) or (tab == 'duplicates' and r['duplicate'])
    )] if tab in ('errors', 'duplicates') else rows
    pages = max(1, (len(visible) + 19) // 20)
    page = min(pages, max(1, request.args.get('page', 1, type=int)))
    return render_template(
        'review.html', active='imports', batch_id=batch_id, rows=visible[(page - 1) * 20:page * 20],
        accounts=accounts, counts=counts, total=len(rows), page=page, pages=pages, tab=tab,
    )


@bp.post('/<int:batch_id>/row/<int:row_id>')
def update_row(batch_id, row_id):
    account_id = request.form.get('account_id', type=int)
    kind = request.form.get('kind', '')
    if kind and kind not in KINDS:
        abort(400, 'Choose a transaction type.')
    vals = [request.form.get(field, '').strip()[:limit] for field, limit in (
        ('txn_date', 48), ('description', 500), ('amount', 48), ('category_override', 60),
    )]
    with connect(g.db) as con:
        if not con.execute('SELECT 1 FROM accounts WHERE id=?', (account_id,)).fetchone():
            abort(400, 'Choose an account for the transaction.')
        changed = con.execute(
            """UPDATE import_rows SET txn_date=?,description=?,amount=?,category_override=?,
               account_id=?,kind=?,excluded=?,keep_duplicate=? WHERE id=? AND batch_id=?""",
            (*vals, account_id, kind, int('excluded' in request.form), int('keep_duplicate' in request.form), row_id, batch_id),
        )
        if not changed.rowcount:
            abort(404)
    flash('Review row saved.', 'success')
    return redirect(url_for('imports.review', batch_id=batch_id, page=request.args.get('page'), tab=request.args.get('tab')))


@bp.post('/<int:batch_id>/commit')
def commit(batch_id):
    try:
        with connect(g.db) as con:
            result = commit_import(con, batch_id)
    except ImportNeedsReview as exc:
        flash(str(exc), 'error')
        return redirect(url_for('imports.review', batch_id=batch_id, tab='errors'))
    if result is None:
        flash('That batch has already been closed.', 'info')
        return redirect(url_for('imports.index'))
    added, skipped = result
    flash(f'Imported {added} transaction{"s" if added != 1 else ""}. Skipped {skipped} excluded or duplicate rows.', 'success')
    return redirect(url_for('transactions'))


@bp.post('/<int:batch_id>/discard')
def discard(batch_id):
    with connect(g.db) as con:
        con.execute('DELETE FROM import_batches WHERE id=?', (batch_id,))
    flash('Import discarded.', 'success')
    return redirect(url_for('imports.index'))
