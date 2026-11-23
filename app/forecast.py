"""Daily cash projections for one selected cash account, in integer cents."""

from collections import defaultdict
from datetime import date, timedelta

from .core import next_on_or_after


def bill_dates(bill, start, end):
    due = date.fromisoformat(bill['due_date'])
    anchor_value = bill['anchor_date']
    anchor = date.fromisoformat(anchor_value) if anchor_value else due
    first = next_on_or_after(due, bill['frequency'], start, anchor)
    while first <= end:
        yield first
        first = next_on_or_after(due, bill['frequency'], first + timedelta(days=1), anchor)


def compare(assumptions, bills):
    start = date.fromisoformat(assumptions['start'])
    end = start + timedelta(days=assumptions['days'] - 1)
    baseline, alternative = defaultdict(list), defaultdict(list)
    for index, event in enumerate(assumptions['events']):
        when = date.fromisoformat(event['date'])
        baseline[when].append((event['name'], event['cents']))
        delay = assumptions['delay'] if index == assumptions['delay_event'] and event['cents'] > 0 else 0
        alternative[when + timedelta(days=delay)].append((event['name'], event['cents']))
    for bill in bills:
        adjusted = bill['id'] == assumptions['change_bill']
        amount = bill['amount_cents']
        for when in bill_dates(bill, start, end):
            baseline[when].append((bill['name'], -amount))
            if adjusted and assumptions['bill_action'] == 'cancel':
                continue
            increase = assumptions['increase'] if adjusted and assumptions['bill_action'] == 'increase' else 0
            alternative[when].append((bill['name'], -(amount + increase)))

    def run(events):
        opening = assumptions['opening']
        balance = opening
        points, journal = [], []
        for offset in range(assumptions['days']):
            day = start + timedelta(days=offset)
            movements = events[day]
            balance += sum(amount for _, amount in movements)
            points.append((day, balance))
            if movements:
                journal.append((day, movements, balance))
        minimum = min([opening] + [amount for _, amount in points])
        first_shortfall = start if opening < 0 else next((day for day, amount in points if amount < 0), None)
        return {
            'points': points, 'journal': journal, 'minimum': minimum, 'ending': balance,
            'shortfall': max(0, -minimum), 'first_shortfall': first_shortfall,
            'days_below': sum(amount < 0 for _, amount in points),
        }

    return run(baseline), run(alternative)
