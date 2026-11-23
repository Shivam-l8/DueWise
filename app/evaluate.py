"""Compare recurring detection with an exact-description-and-amount baseline."""

import csv
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from .core import cents, find_candidates, import_transactions, infer_kind, init_db

DATASET = Path(__file__).resolve().parents[1] / 'sample_data' / 'detection_evaluation.csv'


def record_key(row):
    return row['txn_date'], row['description'], cents(row['amount'])


def metrics(predicted, labelled):
    correct = len(predicted & labelled)
    return {
        'true_positive': correct, 'false_positive': len(predicted - labelled),
        'false_negative': len(labelled - predicted),
        'precision': correct / len(predicted) if predicted else 0,
        'recall': correct / len(labelled) if labelled else 0,
    }


def evaluate(dataset=DATASET):
    with dataset.open(newline='', encoding='utf-8') as source:
        rows = [dict(row, amount=float(row['amount'])) for row in csv.DictReader(source)]
    labelled = {row['series'] for row in rows if row['expected_recurring'] == 'yes'}
    indexed = {record_key(row): row['series'] for row in rows}
    with TemporaryDirectory(prefix='duewise-evaluation-') as folder:
        database = Path(folder) / 'evaluation.sqlite3'
        init_db(database)
        imported = import_transactions(rows, database)
        detected = find_candidates(database)

    proposed = set()
    for item in detected:
        series = {indexed[record_key(evidence)] for evidence in item['evidence']}
        proposed.add(next(iter(series)) if len(series) == 1 else f"mixed:{item['merchant']}")

    unique_purchases = {
        record_key(row): row for row in rows
        if row['amount'] < 0 and infer_kind(row['description'], row['amount']) == 'purchase'
    }
    repeats = Counter((description, amount) for _, description, amount in unique_purchases)
    baseline = {
        row['series'] for (_, description, amount), row in unique_purchases.items()
        if repeats[(description, amount)] >= 2
    }
    return rows, imported, labelled, {'Exact match': baseline, 'DueWise': proposed}


def main():
    rows, imported, labelled, predictions = evaluate()
    print(f'Synthetic evaluation: {len(rows)} rows, {imported} unique transactions, {len(labelled)} labelled recurring series')
    for method, predicted in predictions.items():
        result = metrics(predicted, labelled)
        print(f"{method:12} precision {result['precision']:.1%} | recall {result['recall']:.1%} | "
              f"TP {result['true_positive']} FP {result['false_positive']} FN {result['false_negative']}")
        print('  False positives:', ', '.join(sorted(predicted - labelled)) or 'none')
        print('  Missed series:', ', '.join(sorted(labelled - predicted)) or 'none')


if __name__ == '__main__':
    main()
