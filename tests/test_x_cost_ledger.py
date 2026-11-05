"""Owned SQLite/public response protocol; no X network or invoice assertions."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime,timezone
import hashlib,json,time
from pathlib import Path
import pytest

from services.hub.budget import Budget
from services.hub.db import encode,Store
from services.hub.errors import HubError
from services.hub import x_cost_ledger as m


@pytest.fixture
def ledger(runtime):
    s,db=runtime;limits=deepcopy(s.config['budgets'])
    limits['x'].update(daily_usd=.50,monthly_usd=5,daily_calls=1000,monthly_calls=10000,max_call_usd=.10,paid_authorized=True)
    job=db.enqueue('scheduler','owned-x-cost','x_discovery','tweet',{},'owned')
    return s,db,Budget(db,limits),job


def receipt(f,post='100',user=None,*,at=None,empty=False,save=True):
    s,db,b,job=f;kind='timeline' if empty else 'post_detail'
    bounds={'endpoint_kind':kind,'posts':10 if empty else 1,'users':int(user is not None)}
    cost=bounds['posts']*.005+bounds['users']*.01
    cid=b.reserve('x',job,reserved_cost_usd=cost,x_resource_bounds=bounds)
    body={'data':[]} if empty else {'data':{'id':post}}
    if user:body['includes']={'users':[{'id':user}]}
    raw=encode(body).encode();b.finish(cid,'done',resource_counts={'posts':0 if empty else 1,'users':int(user is not None)})
    stamp=time.time()-5 if at is None else at
    path='/2/users/123/tweets' if empty else '/2/tweets/'+post
    params={'max_results':10} if empty else {'expansions':'author_id'} if user else {}
    with db.transaction() as con:
        con.execute('UPDATE api_calls SET started_at=?,finished_at=? WHERE call_id=?',(stamp,stamp+.1,cid))
        con.execute('INSERT INTO source_http_receipts VALUES(?,?,?,?,?,?,?,?,?,?)',(cid,job,'GET','api.x.com',path,encode(params),hashlib.sha256(raw).hexdigest(),m.digest(body),'default_tls',stamp+.1))
    m.note_receipt(db,cid)
    file=s.root/'media'/job/'source-receipts'/(cid+'.json');file.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if save:file.write_bytes(raw)
    return cid,file


def calibrate(f):
    s,db,b,_=f;plan=m.calibration_plan(s.root,db);m.apply_calibration(s.root,db,plan)
    b.limits['x']['utc_resource_dedup_estimate_enabled']=True
    return plan


def test_audited_100_receipts_plus_five_unattributed_preserve_gross_history_and_estimate_160(ledger):
    f=ledger;s,db,b,job=f
    for i in range(7):receipt(f,post=str(100+i),user=str(900+i))
    for _ in range(13):receipt(f,user='900')
    for _ in range(25):receipt(f)
    for _ in range(55):receipt(f,empty=True)
    for bound in (.015,.015,.015,.005,.005):
        cid=b.reserve('x',job,reserved_cost_usd=bound,x_resource_bounds={'endpoint_kind':'post_detail','posts':1,'users':int(bound>.005)})
        b.finish(cid,'done',resource_counts={'posts':1,'users':int(bound>.005)})
    before=db.query('SELECT * FROM api_calls ORDER BY call_id')
    usage=db.query('SELECT * FROM api_resource_usage ORDER BY call_id');sources=db.query('SELECT * FROM source_http_receipts ORDER BY call_id')
    plan=calibrate(f)
    assert db.query('SELECT * FROM api_calls ORDER BY call_id')==before
    assert db.query('SELECT * FROM api_resource_usage ORDER BY call_id')==usage
    assert db.query('SELECT * FROM source_http_receipts ORDER BY call_id')==sources
    value=plan['periods']['budget_daily_shanghai']
    assert value['http_attempts']==105 and value['verified_receipt_calls']==100 and value['unverified_calls']==5
    assert value['gross_conservative_upper_bound_usd']==pytest.approx(.480)
    assert value['unique_resources_estimate_usd']==pytest.approx(.105)
    assert value['unverified_gross_retained_usd']==pytest.approx(.055)
    assert value['budget_estimate_with_unverified_usd']==pytest.approx(.160)
    assert len(db.query('SELECT * FROM x_cost_unique_resources'))==14
    assert b.limits['x']['daily_usd']==.5 and b.limits['x']['monthly_usd']==5
    again=m.apply_calibration(s.root,db,plan)
    assert again['state']=='already_calibrated_config_not_enabled' and again['history_before']==again['history_after']
    assert again['history_before']['api_calls']['rows']==105
    guard=json.loads(db.query("SELECT record FROM x_cost_events WHERE kind='calibration_history_guard'")[0]['record'])
    assert guard['history_before']==guard['history_after']==again['history_before']
    assert len(db.query('SELECT * FROM x_cost_calibrations'))==1


def test_default_gross_gate_and_explicit_uncalibrated_gate_never_silently_get_cheaper(ledger):
    f=ledger;s,db,b,job=f;receipt(f,user='900');receipt(f,user='900')
    b.limits['x']['daily_usd']=.030
    with pytest.raises(HubError,match='API_BUDGET_EXCEEDED'):b.reserve('x',reserved_cost_usd=.005,x_resource_bounds={'endpoint_kind':'post_detail','posts':1,'users':0})
    b.limits['x']['utc_resource_dedup_estimate_enabled']=True
    with pytest.raises(HubError,match='X_COST_CALIBRATION_REQUIRED'):b.reserve('x',reserved_cost_usd=.005,x_resource_bounds={'endpoint_kind':'post_detail','posts':1,'users':0})
    calibrate(f)
    assert b.reserve('x',reserved_cost_usd=.005,x_resource_bounds={'endpoint_kind':'post_detail','posts':1,'users':0})


def test_pending_missing_saved_raw_and_network_unknown_keep_full_gross(ledger):
    f=ledger;s,db,b,job=f;cid,file=receipt(f,save=False)
    with pytest.raises(HubError,match='X_COST_SAVED_RECEIPT_REQUIRED'):m.confirm_saved_receipt(s.root,db,cid)
    unknown=b.reserve('x',reserved_cost_usd=.05,x_resource_bounds={'endpoint_kind':'timeline','posts':10,'users':0});b.finish(unknown,'unknown','NETWORK_ERROR')
    plan=calibrate(f);v=plan['periods']['budget_daily_shanghai']
    assert v['unique_resources_estimate_usd']==0 and v['unverified_calls']==2
    assert v['budget_estimate_with_unverified_usd']==pytest.approx(.055)
    assert cid in plan['missing_raw_calls']


def test_receipt_hash_tamper_and_changed_observation_block_credit(ledger):
    f=ledger;s,db,b,_=f;cid,file=receipt(f,user='900');raw=file.read_bytes();file.write_bytes(raw+b' ')
    with pytest.raises(HubError,match='X_COST_RECEIPT_NOT_VERIFIED'):m.confirm_saved_receipt(s.root,db,cid)
    file.write_bytes(raw);m.confirm_saved_receipt(s.root,db,cid);calibrate(f)
    with db.transaction() as con:con.execute("UPDATE x_cost_observations SET resources='[]' WHERE call_id=?",(cid,))
    with pytest.raises(HubError,match='X_COST_LEDGER_BINDING_CHANGED'):b.report()


def test_out_of_order_calibration_deduplicates_utc_not_local_midnight_and_resets_at_utc(ledger):
    f=ledger;s,db,b,_=f
    # All fixtures are historical; the same UTC day spans Shanghai midnight.
    a=datetime(2026,10,30,15,59,tzinfo=timezone.utc).timestamp()
    c=datetime(2026,10,31,0,0,1,tzinfo=timezone.utc).timestamp()
    receipt(f,at=c);receipt(f,at=a+120);receipt(f,at=a)
    plan=calibrate(f)
    with db.connect() as con:
        values=m.estimates(m._rows(con),m.load_observations(con),{'all':0,'after_local_midnight':a+60})
    assert values['all']['unique_resources_estimate_usd']==pytest.approx(.010)
    assert values['after_local_midnight']['unique_resources_estimate_usd']==pytest.approx(.005)


def test_response_crossing_utc_boundary_keeps_gross_and_idempotent_confirm_does_not_duplicate(ledger):
    f=ledger;s,db,b,_=f;cid,_=receipt(f,user='900',at=datetime(2026,10,30,23,59,59,tzinfo=timezone.utc).timestamp())
    end=datetime(2026,10,31,0,0,1,tzinfo=timezone.utc).timestamp()
    with db.transaction() as con:
        con.execute('UPDATE api_calls SET finished_at=? WHERE call_id=?',(end,cid));con.execute('UPDATE source_http_receipts SET captured_at=? WHERE call_id=?',(end,cid));con.execute('DELETE FROM x_cost_observations WHERE call_id=?',(cid,))
    m.confirm_saved_receipt(s.root,db,cid);m.confirm_saved_receipt(s.root,db,cid)
    assert db.query('SELECT state FROM x_cost_observations')==[{'state':'cross_utc_upper_bound'}]
    assert len(db.query("SELECT * FROM x_cost_events WHERE kind='cross_utc_upper_bound'"))==1
    calibrate(f)
    with db.connect() as con:value=m.estimates(m._rows(con),m.load_observations(con),{'all':0})['all']
    assert value['unique_resources_estimate_usd']==0 and value['budget_estimate_with_unverified_usd']==pytest.approx(.015)


def test_calibration_plan_changed_sources_or_receipt_refuses_apply_and_never_mutates_api_costs(ledger):
    f=ledger;s,db,b,_=f;cid,file=receipt(f);plan=m.calibration_plan(s.root,db);before=db.query('SELECT * FROM api_calls')
    changed=deepcopy(plan);changed['source_files']['services/hub/budget.py']='0'*64
    with pytest.raises(HubError,match='X_COST_CALIBRATION_PLAN_CHANGED'):m.apply_calibration(s.root,db,changed)
    assert not db.query('SELECT * FROM x_cost_control') and db.query('SELECT * FROM api_calls')==before
    file.write_bytes(b'{}')
    with pytest.raises(HubError):m.apply_calibration(s.root,db,plan)
    assert db.query('SELECT * FROM api_calls')==before


def test_concurrent_opt_in_reservations_still_charge_full_inflight_capacity_and_call_limit(ledger):
    f=ledger;s,db,b,_=f;calibrate(f);b.limits['x']['daily_usd']=.20
    def attempt(_):
        try:return b.reserve('x',reserved_cost_usd=.05,x_resource_bounds={'endpoint_kind':'timeline','posts':10,'users':0})
        except HubError as e:assert e.code=='API_BUDGET_EXCEEDED'
    with ThreadPoolExecutor(8) as pool:results=list(pool.map(attempt,range(20)))
    assert sum(bool(v) for v in results)==4
    assert db.query('SELECT SUM(cost_usd) cost FROM api_calls')[0]['cost']==pytest.approx(.20)
    reservations=db.query("SELECT record FROM x_cost_events WHERE kind='full_gross_reservation'")
    assert len(reservations)==4 and all(json.loads(v['record'])['pre_request_full_gross_bound_usd']==.05 for v in reservations)


@pytest.mark.parametrize('invalid',[None,'true',1])
def test_invalid_accounting_mode_rejected_before_reservation(ledger,invalid):
    _,db,b,_=ledger;b.limits['x']['utc_resource_dedup_estimate_enabled']=invalid
    with pytest.raises(HubError,match='X_COST_ACCOUNTING_MODE_INVALID'):
        b.reserve('x',reserved_cost_usd=.005,x_resource_bounds={'endpoint_kind':'post_detail','posts':1,'users':0})
    assert not db.query('SELECT * FROM api_calls')


def test_migration_17_preserves_preexisting_16_cost_rows_and_defaults_to_gross(ledger,tmp_path):
    _,_,configured,_=ledger;db=Store(tmp_path/'pre17'/'data'/'hub.sqlite3')
    con=db.connect()
    try:
        for file in sorted((Path(__file__).parents[1]/'scheduler/migrations').glob('*.sql')):
            if file.name<'017_':con.executescript(file.read_text('utf-8'))
    finally:con.close()
    budget=Budget(db,deepcopy(configured.limits))
    cid=budget.reserve('x',reserved_cost_usd=.05,x_resource_bounds={'endpoint_kind':'timeline','posts':10,'users':0})
    budget.finish(cid,'unknown','NETWORK_ERROR')
    before=db.query('SELECT * FROM api_calls');usage=db.query('SELECT * FROM api_resource_usage')
    db.init()
    assert db.query('SELECT * FROM api_calls')==before and db.query('SELECT * FROM api_resource_usage')==usage
    assert not db.query('SELECT * FROM x_cost_observations') and not db.query('SELECT * FROM x_cost_control')
    report=budget.report()
    assert report['x_budget_basis']=='gross_conservative_upper_bound'
    assert report['x_utc_resource_estimate']['periods']['daily']['unverified_gross_retained_usd']==pytest.approx(.05)
    assert report['x_utc_resource_estimate']['invoice_charge_verified'] is False
