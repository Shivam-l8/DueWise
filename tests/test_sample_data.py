from pathlib import Path
from app.core import parse_csv, init_db, import_transactions, refresh_candidates, connect

ROOT = Path(__file__).resolve().parents[1]

def test_sample_import_and_suggestions(tmp_path):
    rows, errors = parse_csv((ROOT / 'sample_data' / 'sample_transactions.csv').read_bytes())
    assert errors == []
    assert len(rows) == 51
    db = tmp_path / 'test.db'
    init_db(db)
    assert import_transactions(rows, db) == 51
    assert import_transactions(rows, db) == 0
    assert refresh_candidates(db) == 4
    with connect(db) as con:
        assert con.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 51
        assert con.execute('SELECT COUNT(*) FROM recurring_candidates').fetchone()[0] == 4
