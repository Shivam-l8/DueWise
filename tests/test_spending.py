from app.core import import_transactions


def test_month_picker_and_spending_breakdown(workspace):
    client, db_path = workspace
    import_transactions([
        {'txn_date': '2026-08-05', 'description': 'Internet Payment', 'amount': -79},
        {'txn_date': '2026-08-08', 'description': 'Grocery Store', 'amount': -20},
        {'txn_date': '2026-08-09', 'description': 'Refund', 'amount': 10},
        {'txn_date': '2026-08-11', 'description': 'Transfer to savings', 'amount': -900},
        {'txn_date': '2026-08-12', 'description': 'Credit Card Payment', 'amount': -400},
        {'txn_date': '2026-09-05', 'description': 'Internet Payment', 'amount': -85},
        {'txn_date': '2026-10-01', 'description': 'Deposit', 'amount': 500},
    ], db_path)

    latest = client.get('/')
    assert latest.status_code == 200
    assert b'2026-10" selected' in latest.data
    assert b'No expenses for this month.' in latest.data

    with client.session_transaction() as session:
        token = session['_csrf']
    august = client.post('/category-rule?month=2026-08', data={
        '_csrf': token, 'keyword': 'internet', 'category': 'Internet and utilities',
    }, follow_redirects=True)
    assert b'2026-08" selected' in august.data
    assert b'<div class="metric-number">$99.00</div>' in august.data
    assert b'Internet and utilities' in august.data
    assert b'Purchase totals over six months' in august.data

    september = client.get('/?month=2026-09')
    assert b'<div class="metric-number">$85.00</div>' in september.data
    assert b'<div class="metric-number">$99.00</div>' not in september.data

    invalid_month = client.get('/?month=unknown')
    assert b'2026-10" selected' in invalid_month.data


def test_spending_without_transactions(workspace):
    client, _ = workspace
    response = client.get('/')
    assert response.status_code == 200
    assert b'Import transactions to see monthly spending.' in response.data
    assert b'id="spending-month"' not in response.data
