from io import BytesIO
from pathlib import Path

from werkzeug.datastructures import MultiDict

from app.core import connect, import_transactions
from app.statements import pdf_rows, validate_row
from conftest import csrf, register

SAMPLES = Path(__file__).resolve().parents[1] / 'sample_data'


def post(client, path, data=None, **options):
    return client.post(path, data=MultiDict([('_csrf', csrf(client)), *data.items(multi=True)])
                       if isinstance(data, MultiDict) else {'_csrf': csrf(client), **(data or {})}, **options)


def state(database, table):
    with connect(database) as con:
        return con.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()


def test_text_pdf_examples_have_expected_transactions():
    for filename, amounts in [
        ('sample_statement.pdf', [-79, -81.5, -54.2, 12.4, -200]),
        ('sample_debit_credit.pdf', [-43.95, 1640, -150, -8.5]),
    ]:
        rows = pdf_rows((SAMPLES / filename).read_bytes())
        assert [validate_row(row)[0]['amount'] for row in rows] == amounts
        assert all(row['reference'].startswith('Page 1') for row in rows)


def test_multi_file_import_requires_review_and_can_be_retried(workspace):
    client, db = workspace
    csv = b'date,description,amount\n2026-09-05,Internet Payment,-79\nbad,Test Cash,-9\n'
    reply = post(client, '/imports/', {
        'account_id': 1, 'files': [(BytesIO(csv), 'sample.csv'),
                                  (BytesIO((SAMPLES / 'sample_statement.pdf').read_bytes()), 'signed.pdf'),
                                  (BytesIO(b'oops'), 'broken.pdf')],
    }, follow_redirects=True)
    assert b'7 extracted rows' in reply.data
    assert b'1 to correct' in reply.data
    assert b'1 duplicates to skip' in reply.data
    assert b'broken.pdf' in reply.data
    assert not state(db, 'transactions')
    batch = state(db, 'import_batches')[0]['id']
    invalid = next(row for row in state(db, 'import_rows') if row['txn_date'] == 'bad')['id']
    assert post(client, f'/imports/{batch}/commit').location.endswith('tab=errors')
    assert not state(db, 'transactions')
    post(client, f'/imports/{batch}/row/{invalid}', {
        'account_id': 1, 'txn_date': '2026-10-21', 'description': 'Corrected meal', 'amount': '-9',
        'kind': 'purchase', 'category_override': 'Dining',
    })
    confirmed = post(client, f'/imports/{batch}/commit', follow_redirects=True)
    assert b'Imported 6 transactions' in confirmed.data
    assert b'Corrected meal' in confirmed.data
    assert len(state(db, 'transactions')) == 6
    new = post(client, '/imports/', {
        'account_id': 1, 'files': (BytesIO(b'date,description,amount\n2026-10-01,Test,-9\n'), 'new.csv'),
    })
    assert new.location != f'/imports/{batch}'
    post(client, f'/imports/{batch}/commit')
    assert len(state(db, 'transactions')) == 6
    assert len(state(db, 'import_batches')) == 1


def test_duplicate_can_be_kept_and_is_account_scoped(workspace):
    client, db = workspace
    csv = b'Posted Date,Merchant,Debit,Credit\n2026-11-19,Harbour Cafe,7.50,\n'
    post(client, '/accounts', {'name': 'Savings', 'kind': 'savings'})
    for account in (1, 2, 2):
        response = post(client, '/imports/', {'account_id': account, 'files': (BytesIO(csv), 'test.csv')})
        batch = response.location.rsplit('/', 1)[1]
        review = client.get(response.location)
        if account == 2 and len(state(db, 'transactions')) == 2:
            assert b'1 duplicates to skip' in review.data
            row = state(db, 'import_rows')[0]['id']
            post(client, f'/imports/{batch}/row/{row}', {
                'account_id': 2, 'txn_date': '2026-11-19', 'description': 'Harbour Cafe',
                'amount': '-7.50', 'keep_duplicate': 'on',
            })
        else:
            assert b'0 duplicates to skip' in review.data
        post(client, f'/imports/{batch}/commit')
    assert [row['account_id'] for row in state(db, 'transactions')] == [1, 2, 2]


def test_confirmation_keeps_payment_history_after_bill_edit_and_transaction_deletion(workspace):
    client, db = workspace
    import_transactions([
        {'txn_date': '2026-01-29', 'description': 'Internet Payment', 'amount': -76},
        {'txn_date': '2026-02-27', 'description': 'POS INTERNET REF 951', 'amount': -84},
        {'txn_date': '2026-03-30', 'description': 'Internet Debit', 'amount': -78},
    ], db)
    path = '/bills/suggestion/1/internet'
    evidence = client.get(path)
    assert b'REF 951' in evidence.data
    assert b'29' in evidence.data
    reply = post(client, path, {
        'name': 'Internet', 'amount': '79.50', 'category': 'Utilities', 'frequency': 'monthly', 'due_date': '2030-01-31',
    })
    assert reply.location == '/bills/1'
    assert len(state(db, 'bill_payments')) == 3
    post(client, '/bills/1', {
        'name': 'Internet', 'amount': '89.50', 'frequency': 'monthly', 'due_date': '2030-01-31',
    })
    post(client, '/transactions/2', {'action': 'delete'})
    payments = state(db, 'bill_payments')
    assert [row['amount_cents'] for row in payments] == [7600, 8400, 7800]
    assert payments[1]['transaction_id'] is None
    assert state(db, 'bills')[0]['amount_cents'] == 8950
    post(client, '/bills/1', {'action': 'archive'})
    assert state(db, 'bills')[0]['active'] == 0
    assert len(state(db, 'bill_payments')) == 3


def test_owner_isolation_csrf_login_and_deletion(app, workspace):
    alice, database = workspace
    import_transactions([{'txn_date': '2026-11-01', 'description': 'Private groceries', 'amount': -20}], database)
    anonymous = app.test_client()
    assert anonymous.get('/export').location == '/welcome'
    assert alice.post('/accounts', data={'name': 'Blocked', 'kind': 'cash'}).status_code == 400
    bob = register(app.test_client(), 'separate-user')
    assert b'Private groceries' not in bob.get('/export').data
    assert bob.get('/transactions/1').status_code == 404
    assert b'Private groceries' in alice.get('/export').data
    post(alice, '/sign-out')
    assert alice.get('/transactions/1').location == '/welcome'
    assert post(alice, '/sign-in', {'username': 'test-user', 'password': 'fictional-password'}).location == '/'
    post(alice, '/delete-workspace', {'confirm': 'DELETE'})
    assert not database.exists()
    assert alice.get('/export').location == '/welcome'
    assert bob.get('/export').status_code == 200


def test_saved_forecast_uses_only_selected_cash_account(workspace):
    client, _ = workspace
    post(client, '/accounts', {'name': 'Credit card', 'kind': 'credit'})
    for name, account, amount in [('Rent', 1, '90'), ('Card subscription', 2, '80')]:
        post(client, '/bills/new', {
            'name': name, 'account_id': account, 'amount': amount, 'frequency': 'monthly', 'due_date': '2027-01-31',
        })
    response = post(client, '/cash-flow/', MultiDict([
        ('account', '1'), ('start', '2027-01-29'), ('days', '4'), ('opening', '50'),
        ('event_name', 'Income'), ('event_date', '2027-01-30'), ('event_amount', '100'), ('event_key', '0'),
        ('delay_event', '0'), ('delay', '2'),
    ]), follow_redirects=True)
    assert b'Projected balance' in response.data
    assert b'Card subscription' not in response.data
    assert b'-$40.00' in response.data
    exported = client.get('/cash-flow/?account=1&download=csv')
    assert b'2027-01-31,60.00,-40.00,-100.00' in exported.data
    assert b'2027-02-01,60.00,60.00,0.00' in exported.data
    assert post(client, '/cash-flow/', {'account': 2}).location == '/cash-flow/'
