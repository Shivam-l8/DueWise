from datetime import date

from app.forecast import bill_dates, compare


def test_month_end_leap_day_and_anchor():
    rent = {'due_date': '2028-01-31', 'anchor_date': None, 'frequency': 'monthly'}
    assert list(bill_dates(rent, date(2028, 1, 1), date(2028, 4, 30))) == [
        date(2028, 1, 31), date(2028, 2, 29), date(2028, 3, 31), date(2028, 4, 30),
    ]
    rent.update(due_date='2028-02-29', anchor_date='2028-01-31')
    assert list(bill_dates(rent, date(2028, 2, 1), date(2028, 3, 31))) == [
        date(2028, 2, 29), date(2028, 3, 31),
    ]


def test_cancel_increase_delay_and_same_day_accounting():
    bills = [{
        'id': 1, 'name': 'Rent', 'amount_cents': 8000, 'due_date': '2027-02-01',
        'anchor_date': None, 'frequency': 'monthly',
    }]
    plan = {
        'start': '2027-01-30', 'days': 5, 'opening': 5000,
        'events': [{'name': 'Pay', 'date': '2027-02-01', 'cents': 7000}],
        'delay_event': 0, 'delay': 2, 'change_bill': 1, 'bill_action': 'increase', 'increase': 1000,
    }
    baseline, higher = compare(plan, bills)
    assert baseline['minimum'] == 4000
    assert baseline['first_shortfall'] is None
    assert higher['minimum'] == -4000
    assert higher['first_shortfall'] == date(2027, 2, 1)
    assert higher['shortfall'] == 4000
    assert higher['days_below'] == 2
    assert higher['ending'] == 3000
    _, cancelled = compare(dict(plan, bill_action='cancel'), bills)
    assert cancelled['minimum'] == 5000
    assert cancelled['ending'] == 12000
