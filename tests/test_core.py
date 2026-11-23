from app.core import init_db,parse_csv,import_transactions,find_candidates,refresh_candidates,connect

def test_csv_validation():
    rows,errors=parse_csv(b'date,description,amount\n2026-01-01,Internet,-75\nwrong,Phone,-50\n')
    assert len(rows)==1 and len(errors)==1

def test_monthly_detection(tmp_path):
    db=tmp_path/'test.db';init_db(db)
    rows=[{'txn_date':d,'description':'Internet payment','amount':-75} for d in ['2026-01-10','2026-02-10','2026-03-10']]
    assert import_transactions(rows,db)==3
    assert import_transactions(rows,db)==0
    result=find_candidates(db)
    assert len(result)==1 and result[0]['occurrences']==3
    assert refresh_candidates(db)==1
    with connect(db) as con:
        assert con.execute('SELECT COUNT(*) FROM recurring_candidates').fetchone()[0]==1

def test_different_amounts_not_detected(tmp_path):
    db=tmp_path/'test.db';init_db(db)
    rows=[{'txn_date':d,'description':'Grocery','amount':a} for d,a in [('2026-01-10',-40),('2026-02-10',-100)]]
    import_transactions(rows,db)
    assert find_candidates(db)==[]
