"""DueWise's local website entry point and dashboard routes."""

import csv
import io
import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from flask import Flask, Response, abort, flash, g, redirect, render_template, request, url_for

from . import bills, imports, planning
from .core import KINDS, category_rules, cents, connect, monthly_spending, transaction_category
from .forecast import bill_dates
from .presentation import friendly_date, line_chart, money, month_name, recent_months
from .statements import validate_row
from .workspace import setup_storage

ACCOUNT_KINDS = {'checking': 'Chequing', 'savings': 'Savings', 'cash': 'Cash', 'credit': 'Credit card'}
ROOT = Path(__file__).resolve().parents[1]


def create_app(data_dir=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get('DUEWISE_SECRET_KEY'),
        MAX_CONTENT_LENGTH=20 * 1024 * 1024,
        MAX_FORM_PARTS=50,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        TRUSTED_HOSTS=['localhost', '127.0.0.1', '[::1]'],
    )
    setup_storage(app, Path(data_dir or os.environ.get('DUEWISE_DATA_DIR') or ROOT / 'instance'))
    app.register_blueprint(imports.bp)
    app.register_blueprint(bills.bp)
    app.register_blueprint(planning.bp)
    app.jinja_env.filters.update(money=money, day=friendly_date, month_name=month_name, cents=cents)

    @app.context_processor
    def shared():
        return {'kinds': KINDS, 'account_kinds': ACCOUNT_KINDS}

    @app.after_request
    def response_headers(response):
        if request.endpoint != 'static':
            response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
        )
        return response

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def handle_error(error):
        messages = {
            404: 'This page or record is unavailable in your workspace.',
            413: 'The upload is too large. Use a combined size below 20 MB and up to 4 MB per file.',
        }
        return render_template('error.html', message=messages.get(error.code, error.description)), error.code

    @app.get('/')
    def home():
        today = date.today()
        with connect(g.db) as con:
            accounts = list_accounts(con)
            account_id = selected_account(accounts)
            months = available_months(con, account_id)
            month = request.args.get('month')
            if month not in months:
                month = next(iter(months), None)
            rules = category_rules(con)
            spending = monthly_spending(con, month, rules, account_id)
            chart = None
            prior = None
            chart_values = []
            if month:
                periods = recent_months(month)
                chart_values = [
                    sum(amount for _, amount in monthly_spending(con, item, rules, account_id)) if item in months else None
                    for item in periods
                ]
                prior = chart_values[-2]
                chart = line_chart([month_name(item).split()[0][:3] for item in periods], [chart_values],
                                   start_at_zero=True)
            transactions = con.execute(
                """SELECT * FROM transactions WHERE (? IS NULL OR account_id=?)
                   ORDER BY txn_date DESC,id DESC LIMIT 5""", (account_id, account_id),
            ).fetchall()
            count = con.execute(
                """SELECT count(*) AS n FROM transactions WHERE txn_date LIKE ?
                   AND (? IS NULL OR account_id=?)""", ((month or '') + '-%', account_id, account_id),
            ).fetchone()['n']
            tracked = con.execute(
                """SELECT b.*, a.name AS account_name FROM bills b JOIN accounts a ON b.account_id=a.id
                   WHERE active=1 AND (? IS NULL OR account_id=?)""", (account_id, account_id),
            ).fetchall()
            upcoming = sorted(
                [(when, item) for item in tracked for when in bill_dates(item, today, today + timedelta(days=29))],
                key=lambda pair: (pair[0], pair[1]['name'].casefold()),
            )
        return render_template(
            'index.html', active='home', accounts=accounts, account_id=account_id, month=month, months=months,
            spending=spending, total_spending=sum(amount for _, amount in spending), chart=chart, prior=prior,
            chart_values=chart_values, transactions=tag_transactions(transactions, rules), tracked=tracked,
            upcoming=upcoming, commitments=sum(item['amount_cents'] for _, item in upcoming), count=count, today=today,
        )

    @app.get('/transactions')
    def transactions():
        with connect(g.db) as con:
            accounts = list_accounts(con)
            account_id = selected_account(accounts)
            months = available_months(con, account_id)
            page = max(1, request.args.get('page', 1, type=int))
            month = request.args.get('month')
            q = request.args.get('q', '').strip()[:160]
            kind = request.args.get('kind', '')
            if month not in months:
                month = ''
            if kind not in KINDS:
                kind = ''
            where = """FROM transactions t JOIN accounts a ON t.account_id=a.id
                       WHERE (? IS NULL OR account_id=?) AND (?='' OR t.kind=?)
                         AND (?='' OR txn_date LIKE ?) AND (?='' OR instr(lower(description),lower(?)) > 0)"""
            args = (account_id, account_id, kind, kind, month, month + '-%', q, q)
            count = con.execute('SELECT count(*) ' + where, args).fetchone()[0]
            pages = max(1, (count + 24) // 25)
            page = min(page, pages)
            rows = con.execute(
                'SELECT t.*,a.name AS account_name ' + where + ' ORDER BY txn_date DESC,t.id DESC LIMIT 25 OFFSET ?',
                (*args, (page - 1) * 25),
            ).fetchall()
            rules = category_rules(con)
        return render_template(
            'transactions.html', active='transactions', accounts=accounts, account_id=account_id, months=months,
            month=month, transactions=tag_transactions(rows, rules), count=count, q=q, kind=kind, page=page, pages=pages,
        )

    @app.route('/transactions/<int:item_id>', methods=['GET', 'POST'])
    def edit_transaction(item_id):
        with connect(g.db) as con:
            item = con.execute('SELECT * FROM transactions WHERE id=?', (item_id,)).fetchone()
            if not item:
                abort(404)
            accounts = list_accounts(con)
            if request.method == 'POST':
                if request.form.get('action') == 'delete':
                    con.execute('DELETE FROM transactions WHERE id=?', (item_id,))
                    flash('Transaction deleted.', 'success')
                    return redirect(url_for('transactions'))
                form = {**dict(request.form), 'category_override': request.form.get('category_override', '').strip()[:60]}
                row, error = validate_row({
                    'txn_date': form.get('txn_date', ''), 'description': form.get('description', ''),
                    'amount': form.get('amount', ''), 'kind': form.get('kind', ''),
                })
                account_id = request.form.get('account_id', type=int)
                if not error and account_id in {a['id'] for a in accounts}:
                    con.execute(
                        """UPDATE transactions SET txn_date=?, description=?, amount=?, kind=?,
                           category_override=?, account_id=? WHERE id=?""",
                        (row['txn_date'], row['description'], row['amount'], row['kind'],
                         form['category_override'], account_id, item_id),
                    )
                    flash('Transaction updated.', 'success')
                    return redirect(url_for('transactions', account=account_id))
                flash(error or 'Choose an account.', 'error')
                item = {**dict(item), **form, 'account_id': account_id}
        return render_template('edit_transaction.html', active='transactions', item=item, accounts=accounts)

    @app.get('/export')
    def export():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['date', 'description', 'amount', 'account', 'type', 'category', 'source'])
        with connect(g.db) as con:
            account_id = selected_account(list_accounts(con))
            rules = category_rules(con)
            for row in con.execute(
                """SELECT t.*,a.name AS account_name FROM transactions t JOIN accounts a ON t.account_id=a.id
                   WHERE (? IS NULL OR account_id=?) ORDER BY txn_date,t.id""", (account_id, account_id),
            ):
                writer.writerow([
                    row['txn_date'], csv_text(row['description']), f"{row['amount']:.2f}",
                    csv_text(row['account_name']), row['kind'], csv_text(transaction_category(row, rules)), row['source'],
                ])
        return Response(output.getvalue(), mimetype='text/csv', headers={
            'Content-Disposition': 'attachment; filename=duewise_transactions.csv',
        })

    @app.get('/settings')
    def settings():
        with connect(g.db) as con:
            accounts, rules = list_accounts(con), category_rules(con)
        return render_template('settings.html', active='settings', accounts=accounts, rules=rules)

    @app.post('/accounts')
    def add_account():
        name = request.form.get('name', '').strip()
        kind = request.form.get('kind', '')
        if not 1 <= len(name) <= 50 or kind not in ACCOUNT_KINDS:
            flash('Enter an account name and choose its type.', 'error')
        else:
            try:
                with connect(g.db) as con:
                    con.execute('INSERT INTO accounts(name,kind) VALUES (?,?)', (name, kind))
                flash('Account added.', 'success')
            except sqlite3.IntegrityError:
                flash('An account already uses that name.', 'error')
        return redirect(url_for('settings'))

    @app.post('/accounts/<int:account_id>/delete')
    def remove_account(account_id):
        with connect(g.db) as con:
            con.execute('BEGIN IMMEDIATE')
            if len(list_accounts(con)) <= 1:
                flash('Keep at least one account in this workspace.', 'error')
            elif request.form.get('confirm', '').strip() != 'DELETE':
                flash('Type DELETE to remove the account and its records.', 'error')
            elif con.execute('DELETE FROM accounts WHERE id=?', (account_id,)).rowcount:
                con.execute('DELETE FROM import_batches WHERE NOT EXISTS (SELECT 1 FROM import_rows WHERE batch_id=import_batches.id)')
                flash('Account and its records deleted.', 'success')
            else:
                abort(404)
        return redirect(url_for('settings'))

    @app.post('/category-rule')
    def save_category_rule():
        keyword = request.form.get('keyword', '').strip().casefold()
        category = request.form.get('category', '').strip()
        if not keyword or not category or len(keyword) > 80 or len(category) > 60:
            flash('Enter a keyword up to 80 characters and a category up to 60 characters.', 'error')
        else:
            with connect(g.db) as con:
                con.execute(
                    """INSERT INTO category_rules (keyword,category) VALUES (?,?)
                       ON CONFLICT(keyword) DO UPDATE SET category=excluded.category""", (keyword, category),
                )
            flash('Category rule saved.', 'success')
        if request.args.get('month'):
            return redirect(url_for('home', month=request.args['month']))
        return redirect(url_for('settings') + '#category-rules')

    @app.post('/category-rule/<int:rule_id>/delete')
    def remove_category_rule(rule_id):
        with connect(g.db) as con:
            con.execute('DELETE FROM category_rules WHERE id=?', (rule_id,))
        flash('Category rule removed.', 'success')
        return redirect(url_for('settings') + '#category-rules')

    return app


def list_accounts(con):
    return con.execute('SELECT * FROM accounts ORDER BY id').fetchall()


def selected_account(accounts):
    account_id = request.args.get('account', type=int)
    return account_id if account_id in {a['id'] for a in accounts} else None


def available_months(con, account_id):
    return [
        row['month'] for row in con.execute(
            """SELECT DISTINCT substr(txn_date,1,7) AS month FROM transactions
               WHERE (? IS NULL OR account_id=?) ORDER BY month DESC""", (account_id, account_id),
        )
    ]


def tag_transactions(rows, rules):
    return [dict(row, category=transaction_category(row, rules)) for row in rows]


def csv_text(value):
    # Prevent user-entered cells from being evaluated as spreadsheet formulas.
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r')) else value


if __name__ == '__main__':
    create_app().run(host='127.0.0.1', port=int(os.environ.get('DUEWISE_PORT', '5077')))
