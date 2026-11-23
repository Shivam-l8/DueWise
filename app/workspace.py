"""Local user identities and synthetic demo workspaces."""

import re
import secrets
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .core import DEFAULT_RULES, add_evidence, cents, connect, find_groups, init_db, next_on_or_after, parse_csv, import_transactions

auth = Blueprint('auth', __name__)
SAMPLES = Path(__file__).resolve().parents[1] / 'sample_data'


def setup_storage(app, folder):
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    app.config['DATA_DIR'] = folder
    app.config['IDENTITY_DB'] = folder / 'identities.sqlite3'
    if not app.secret_key:
        key = folder / 'session.key'
        try:
            with key.open('x') as handle:
                handle.write(secrets.token_hex(32))
            key.chmod(0o600)
        except FileExistsError:
            pass
        app.secret_key = key.read_text().strip()
        if len(app.secret_key) < 32:
            raise RuntimeError('The local session key is incomplete. Set DUEWISE_SECRET_KEY or replace instance/session.key.')
    with connect(app.config['IDENTITY_DB']) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
              id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT, demo INTEGER NOT NULL
            )
        """)
    app.register_blueprint(auth)


def workspace_db(user_id):
    return current_app.config['DATA_DIR'] / f'workspace-{user_id}.sqlite3'


def start_session(user_id):
    session.clear()
    session['user'] = user_id
    session['_csrf'] = secrets.token_urlsafe(32)


def create_user(name, password=None):
    user_id = secrets.token_hex(16)
    password_hash = generate_password_hash(password) if password is not None else None
    demo = password_hash is None
    path = workspace_db(user_id)
    with connect(current_app.config['IDENTITY_DB']) as con:
        con.execute('INSERT INTO users VALUES (?, ?, ?, ?)', (user_id, name, password_hash, int(demo)))
        try:
            init_db(path)
            path.chmod(0o600)
            with connect(path) as workspace:
                workspace.executemany(
                    'INSERT INTO category_rules (keyword, category) VALUES (?, ?)', DEFAULT_RULES,
                )
            if demo:
                seed_demo(path)
        except Exception:
            path.unlink(missing_ok=True)
            raise
    return user_id


@auth.before_app_request
def identify():
    user_id = session.get('user')
    g.user, g.db = None, None
    if user_id:
        with connect(current_app.config['IDENTITY_DB']) as con:
            g.user = con.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
        if g.user:
            g.db = workspace_db(g.user['id'])
    if request.method == 'POST':
        expected = session.get('_csrf')
        submitted = request.form.get('_csrf', '')
        if not expected or not secrets.compare_digest(expected, submitted):
            abort(400, 'The form expired. Reload the page and try again.')
    open_endpoints = {None, 'static', 'auth.welcome', 'auth.sign_in', 'auth.register', 'auth.demo'}
    if not g.user and request.endpoint not in open_endpoints:
        return redirect(url_for('auth.welcome'))


@auth.app_context_processor
def form_context():
    def csrf_token():
        if '_csrf' not in session:
            session['_csrf'] = secrets.token_urlsafe(32)
        return session['_csrf']
    return {'csrf_token': csrf_token}


@auth.get('/welcome')
def welcome():
    if g.user:
        return redirect(url_for('home'))
    return render_template('welcome.html')


@auth.post('/sign-in')
def sign_in():
    if g.user:
        return redirect(url_for('home'))
    username = request.form.get('username', '').strip().lower()
    password = request.form.get('password', '')
    with connect(current_app.config['IDENTITY_DB']) as con:
        user = con.execute('SELECT * FROM users WHERE username=? AND demo=0', (username,)).fetchone()
    if not user or len(password) > 128 or not check_password_hash(user['password_hash'], password):
        flash('That username and password combination could not be verified.', 'error')
        return redirect(url_for('auth.welcome', mode='sign-in'))
    start_session(user['id'])
    return redirect(url_for('home'))


@auth.post('/register')
def register():
    if g.user:
        return redirect(url_for('home'))
    username = request.form.get('username', '').strip().lower()
    password = request.form.get('password', '')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{2,31}', username) or not 8 <= len(password) <= 128:
        flash('Use a 3–32 character username and a password with at least 8 characters.', 'error')
        return redirect(url_for('auth.welcome', mode='register'))
    try:
        user_id = create_user(username, password)
    except sqlite3.IntegrityError:
        flash('That username is already in use.', 'error')
        return redirect(url_for('auth.welcome', mode='register'))
    start_session(user_id)
    return redirect(url_for('home'))


@auth.post('/demo')
def demo():
    if not g.user:
        start_session(create_user(f'demo-{secrets.token_hex(8)}'))
    return redirect(url_for('home'))


@auth.post('/sign-out')
def sign_out():
    # A demo workspace is session-bound and can be reconstructed from the bundled sample.
    if g.user['demo']:
        delete_user()
    session.clear()
    return redirect(url_for('auth.welcome'))


@auth.post('/delete-workspace')
def delete_workspace():
    if request.form.get('confirm', '').strip() != 'DELETE':
        flash('Type DELETE to remove this workspace.', 'error')
        return redirect(url_for('settings'))
    delete_user()
    session.clear()
    flash('Workspace deleted.', 'success')
    return redirect(url_for('auth.welcome'))


def delete_user():
    with connect(current_app.config['IDENTITY_DB']) as con:
        con.execute('BEGIN IMMEDIATE')
        g.db.unlink(missing_ok=True)
        con.execute('DELETE FROM users WHERE id=?', (g.user['id'],))


def seed_demo(path):
    rows, errors = parse_csv((SAMPLES / 'sample_transactions.csv').read_bytes())
    if errors:
        raise RuntimeError('The bundled demo CSV has invalid rows.')
    import_transactions(rows, path)
    demo_start = date.today()
    with connect(path) as con:
        con.execute("UPDATE accounts SET name='Everyday checking' WHERE id=1")
        rules = {'internet': 'Utilities', 'music subscription': 'Subscriptions'}
        for candidate in find_groups(con):
            if candidate['merchant'] not in rules:
                continue
            merchant = candidate['merchant']
            anchor = date.fromisoformat(candidate['last_date'])
            due = next_on_or_after(anchor, 'monthly', demo_start + timedelta(days=1))
            bill = con.execute(
                """INSERT INTO bills (account_id,merchant_key,name,amount_cents,due_date,frequency,category,autopay,anchor_date)
                   VALUES (1,?,?,?,?, 'monthly',?,1,?)""",
                (merchant, merchant.title(), cents(candidate['average_amount']), due.isoformat(), rules[merchant],
                 anchor.isoformat()),
            )
            add_evidence(con, bill.lastrowid, candidate['evidence'])
        for name, amount, days, category in [('Rent', 68000, 4, 'Housing'), ('Cloud storage', 899, 16, 'Subscriptions')]:
            con.execute(
                """INSERT INTO bills (account_id,name,amount_cents,due_date,frequency,category)
                   VALUES (1,?,?,?,'monthly',?)""",
                (name, amount, (demo_start + timedelta(days=days)).isoformat(), category),
            )
