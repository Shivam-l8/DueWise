"""Small, dependency-free chart and dashboard view models."""

from datetime import date
from itertools import groupby
from math import ceil, log10

from .core import add_months


def money(value):
    return f"{'-' if value < 0 else ''}${abs(value) / 100:,.2f}"


def friendly_date(value, pattern='%b %d, %Y'):
    if not value:
        return ''
    when = date.fromisoformat(value) if isinstance(value, str) else value
    return when.strftime(pattern).replace(' 0', ' ')


def month_name(value):
    return friendly_date(value + '-01', '%B %Y') if value else ''


def recent_months(month, length=6):
    anchor = date.fromisoformat(month + '-01')
    return [add_months(anchor, -count).strftime('%Y-%m') for count in range(length - 1, -1, -1)]


def line_chart(labels, series, *, start_at_zero=False):
    width, height, left, top, right, bottom = 640, 244, 66, 16, 22, 34
    plot_w, plot_h = width - left - right, height - top - bottom
    amounts = [amount for line in series for amount in line if amount is not None]
    hi, lo = max(amounts), min(amounts)
    hi = max(0, hi)
    lo = min(0, lo) if start_at_zero or lo < 0 else lo
    interval = max(100, (hi - lo) / 4)
    base = 10 ** int(log10(interval))
    step = next(item * base for item in (1, 2, 2.5, 5, 10) if item * base >= interval)
    lower = (lo // step) * step
    upper = max(lower + step, ceil(hi / step) * step)

    def y_for(value):
        return top + (upper - value) / (upper - lower) * plot_h

    def x_for(index):
        return left + index / max(1, len(labels) - 1) * plot_w

    ticks = []
    for index in range(round((upper - lower) / step) + 1):
        value = upper - index * step
        ticks.append({'y': round(y_for(value), 2), 'label': f"{'-' if value < 0 else ''}${abs(value) / 100:,.0f}", 'zero': value == 0})
    label_indices = sorted({round(i * (len(labels) - 1) / min(5, max(1, len(labels) - 1))) for i in range(min(6, len(labels)))})
    lines, points = [], []
    for line in series:
        positions = [(round(x_for(i), 2), round(y_for(value), 2)) if value is not None else None
                     for i, value in enumerate(line)]
        lines.append([
            ' '.join(f'{x},{y}' for x, y in segment)
            for exists, segment in groupby(positions, key=lambda pos: pos is not None) if exists
        ])
        points.append([
            {'x': pos[0], 'y': pos[1], 'text': f'{labels[i]} · {money(line[i])}'}
            for i, pos in enumerate(positions) if pos is not None
        ])
    area = ''
    if len(points[0]) == len(labels) and len(labels) > 1:
        area = f'{left},{height-bottom} {lines[0][0]} {width-right},{height-bottom}'
    return {
        'ticks': ticks, 'labels': [{'x': round(x_for(i), 2), 'label': labels[i]} for i in label_indices],
        'lines': lines, 'points': points, 'area': area,
        'left': left, 'right': width-right, 'bottom': height-bottom, 'width': width, 'height': height,
    }
