"""Cash-flow assumptions, scenario comparison, and export."""

import csv
import io
import json
from datetime import date, timedelta

from flask import Blueprint, Response, flash, g, redirect, render_template, request, url_for

from .core import cents, connect
from .forecast import compare
from .presentation import friendly_date, line_chart
from .statements import parse_date, parse_money

bp = Blueprint('planning', __name__, url_prefix='/cash-flow')


@bp.route('/', methods=['GET', 'POST'])
def index():
    today = date.today()
    with connect(g.db) as con:
        accounts = con.execute("SELECT * FROM accounts WHERE kind <> 'credit' ORDER BY id").fetchall()
        ids = [a['id'] for a in accounts]
        requested = (request.form if request.method == 'POST' else request.args).get('account', type=int)
        account_id = requested if requested in ids else next(iter(ids), None)
        bills = con.execute(
            'SELECT * FROM bills WHERE account_id=? AND active=1 ORDER BY name', (account_id,),
        ).fetchall()
        saved = con.execute('SELECT assumptions FROM forecasts WHERE account_id=?', (account_id,)).fetchone()
        assumptions = json.loads(saved['assumptions']) if saved else initial(today, g.user['demo'])
        if assumptions['change_bill'] not in {bill['id'] for bill in bills}:
            assumptions.update(change_bill=None, bill_action='', increase=0)
        if request.method == 'POST':
            if requested not in ids:
                flash('Choose a cash account for the projection.', 'error')
                return redirect(url_for('planning.index'))
            try:
                assumptions = form_assumptions(request.form, bills)
            except ValueError as exc:
                flash(str(exc), 'error')
                return render_template(
                    'forecast.html', active='forecast', accounts=accounts, account_id=account_id, bills=bills,
                    assumptions=form_values(request.form), baseline=None, alternative=None, chart=None,
                ), 400
            con.execute(
                """INSERT INTO forecasts VALUES (?,?) ON CONFLICT(account_id) DO UPDATE SET assumptions=excluded.assumptions""",
                (account_id, json.dumps(assumptions)),
            )
            flash('Projection updated.', 'success')
            return redirect(url_for('planning.index', account=account_id) + '#projection')
    baseline = alternative = chart = None
    if account_id and (saved or g.user['demo']):
        baseline, alternative = compare(assumptions, bills)
        if request.args.get('download') == 'csv':
            return export_projection(baseline, alternative)
        chart = line_chart(
            [friendly_date(day, '%b %d') for day, _ in baseline['points']],
            [[balance for _, balance in result['points']] for result in (baseline, alternative)], start_at_zero=True,
        )
    page_form = {
        **assumptions, 'opening': f"{assumptions['opening'] / 100:.2f}",
        'increase': f"{assumptions['increase'] / 100:.2f}" if assumptions['increase'] else '',
        'delay_event': assumptions['events'][assumptions['delay_event']]['key'] if assumptions['delay_event'] is not None else '',
        'events': [dict(row, amount=f"{row['cents'] / 100:.2f}") for row in assumptions['events']],
    }
    return render_template(
        'forecast.html', active='forecast', accounts=accounts, account_id=account_id, bills=bills,
        assumptions=page_form, baseline=baseline, alternative=alternative, chart=chart,
    )


def initial(today, demo):
    defaults = {
        'start': today.isoformat(), 'days': 30, 'opening': 0, 'events': [],
        'delay_event': None, 'delay': 0, 'change_bill': None, 'bill_action': '', 'increase': 0,
    }
    if demo:
        defaults.update(
            opening=64000, events=[
                {'name': 'Paycheque', 'cents': 155000, 'date': (today + timedelta(days=2)).isoformat(), 'key': '0'},
                {'name': 'Groceries', 'cents': -12500, 'date': (today + timedelta(days=8)).isoformat(), 'key': '1'},
                {'name': 'Paycheque', 'cents': 155000, 'date': (today + timedelta(days=16)).isoformat(), 'key': '2'},
                {'name': 'Transport', 'cents': -8800, 'date': (today + timedelta(days=21)).isoformat(), 'key': '3'},
            ], delay_event=0, delay=7,
        )
    return defaults


def form_values(form):
    names = form.getlist('event_name')
    dates = form.getlist('event_date')
    amounts = form.getlist('event_amount')
    keys = form.getlist('event_key')
    result = dict(form)
    result['events'] = [
        {'name': name, 'date': when, 'amount': amount, 'key': key}
        for name, when, amount, key in zip(names, dates, amounts, keys)
    ]
    result['change_bill'] = form.get('change_bill', type=int)
    return result


def form_assumptions(form, bills):
    values = form_values(form)
    start = parse_date(values.get('start', ''))
    days = form.get('days', type=int)
    if days is None or not 2 <= days <= 366:
        raise ValueError('Choose a projection length of 2 to 366 days.')
    opening = cents(parse_money(values.get('opening', '')))
    end = start + timedelta(days=days - 1)
    events = []
    raw_events = values['events']
    field_counts = {len(form.getlist(field)) for field in ('event_name', 'event_date', 'event_amount', 'event_key')}
    if len(field_counts) != 1:
        raise ValueError('Review the planned transactions and submit them again.')
    if len(raw_events) > 12:
        raise ValueError('Add at most 12 planned transactions.')
    for event in raw_events:
        if not any(event.get(field, '').strip() for field in ('name', 'date', 'amount')):
            continue
        name = event['name'].strip()
        if not 1 <= len(name) <= 70:
            raise ValueError('Enter a name for each planned transaction.')
        when = parse_date(event['date'])
        if not start <= when <= end:
            raise ValueError('Each planned transaction must fall within the projection dates.')
        amount = cents(parse_money(event['amount']))
        if not amount:
            raise ValueError('Planned amounts must be greater than zero for income or negative for expenses.')
        events.append({'name': name, 'date': when.isoformat(), 'cents': amount, 'key': event['key'][:24]})
    keys = [row['key'] for row in events]
    if '' in keys or len(set(keys)) != len(keys):
        raise ValueError('Review the planned transactions and submit them again.')
    delay_key = values.get('delay_event', '')
    delay_event = next((index for index, event in enumerate(events) if event['key'] == delay_key and event['cents'] > 0), None)
    delay = 0
    if delay_key:
        delay = form.get('delay', type=int)
        if delay_event is None or delay is None or not 1 <= delay <= 366:
            raise ValueError('Choose an income entry and a delay between 1 and 366 days.')
    change_bill = values.get('change_bill')
    bill_action = values.get('bill_action', '') if change_bill is not None else ''
    if change_bill is not None and (change_bill not in {r['id'] for r in bills} or bill_action not in ('cancel', 'increase')):
        raise ValueError('Choose a confirmed bill and an adjustment.')
    increase = 0
    if bill_action == 'increase':
        increase = cents(parse_money(values.get('increase', '')))
        if increase <= 0:
            raise ValueError('Enter a bill increase greater than zero.')
    return {
        'start': start.isoformat(), 'days': days, 'opening': opening, 'events': events, 'delay': delay,
        'delay_event': delay_event, 'change_bill': change_bill, 'bill_action': bill_action, 'increase': increase,
    }


def export_projection(baseline, alternative):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(['date', 'baseline_balance_cad', 'alternative_balance_cad', 'difference_cad'])
    for (day, base), (_, alt) in zip(baseline['points'], alternative['points']):
        writer.writerow([day.isoformat(), f'{base / 100:.2f}', f'{alt / 100:.2f}', f'{(alt - base) / 100:.2f}'])
    return Response(buffer.getvalue(), mimetype='text/csv', headers={
        'Content-Disposition': 'attachment; filename=duewise_cash_flow.csv',
    })
