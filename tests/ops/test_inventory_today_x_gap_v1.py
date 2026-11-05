"""Owned recorded HTTP/SQLite only; inventory has no import or send path."""
from datetime import datetime,timezone
import importlib.util,json,time,uuid
from pathlib import Path
from types import SimpleNamespace
import httpx,pytest
from adapters.http import ProviderHTTP
from services.hub.budget import Budget
from services.hub.errors import HubError
from services.hub.db import encode

FILE=Path(__file__).resolve().parents[2]/'scripts/ops/inventory_today_x_gap_v1.py'
spec=importlib.util.spec_from_file_location('owned_today_inventory',FILE);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

@pytest.fixture
def inventory(runtime,monkeypatch):
    s,db=runtime;s.config['sources']=[{'x_handle':h} for h in m.HANDLES]
    s.config['budgets']['x'].update(daily_usd=.5,monthly_usd=5,daily_calls=100,monthly_calls=100,max_call_usd=.05,paid_authorized=True)
    monkeypatch.setenv('X_BEARER_TOKEN','owned-only-inventory-token')
    calls=[];state=SimpleNamespace(more=False)
    def response(req):
        calls.append((req.url.path,dict(req.url.params)))
        if '/by/username/' in req.url.path:
            h=req.url.path.rsplit('/',1)[1];author='1' if h==m.HANDLES[0] else '2'
            body={'data':{'id':author,'username':h,'name':'Owned '+h}}
        else:
            author=req.url.path.split('/')[3];post='900' if author=='1' else '901'
            body={'data':[{'id':post,'author_id':author,'text':'Owned full text','created_at':'2026-11-02T02:00:00Z','referenced_tweets':[]}]}
            if state.more:body['meta']={'next_token':'second' if not req.url.params.get('pagination_token') else 'third'}
        return httpx.Response(200,json=body)
    client=ProviderHTTP(Budget(db,s.config['budgets']),httpx.MockTransport(response))
    return SimpleNamespace(s=s,db=db,calls=calls,client=client,state=state,run=str(uuid.uuid4()))


def test_default_plan_has_no_api_no_new_jobs_and_never_claims_full_day_coverage(inventory):
    f=inventory;value=m.plan(f.s,f.db)
    assert value['mode']=='plan_only' and not value['complete'] and value['paid_requests']==0
    assert not f.calls and not f.db.query('SELECT * FROM jobs') and not f.db.query('SELECT * FROM api_calls')


def test_two_handle_complete_raw_inventory_no_details_no_cursor_or_publication_changes(inventory):
    f=inventory
    with f.db.transaction() as db:db.execute('INSERT INTO source_cursors VALUES(?,?)',('x-api:example-channel-b','800'))
    before=m.cursors(f.db);value=m.apply(f.s,f.db,run_id=f.run,http=f.client)
    assert value['complete'] and value['missing_full_raw_ids']==['900','901'] and len(f.calls)==4
    assert all('/2/tweets/' not in path for path,p in f.calls) and m.cursors(f.db)==before
    saved=m._read(f.s.root,value['descriptor'])
    assert len(saved['posts'])==2 and saved['new_receipt_transport_modes']==['custom_transport']
    assert all(p['original_post']['created_at']=='2026-11-02T02:00:00Z' for p in saved['posts'])
    assert not f.db.query('SELECT * FROM publications') and not f.db.query("SELECT * FROM jobs WHERE job_type='tweet_process'")
    assert m.apply(f.s,f.db,run_id=f.run,http=f.client)==value and len(f.calls)==4


def test_two_pages_each_cap_stays_partial_and_preserves_real_continuation(inventory):
    f=inventory;f.state.more=True;value=m.apply(f.s,f.db,run_id=f.run,http=f.client)
    assert not value['complete'] and value['state']=='partial' and len(f.calls)==6
    assert all(len(v['page_call_ids'])==2 and v['next_token']=='third' for v in value['progress']['handles'].values())
    assert all(p['max_results']=='5' for path,p in f.calls if path.endswith('/tweets'))


def test_budget_reservation_stops_before_timeline_and_partial_does_not_claim_missing_scan_complete(inventory):
    f=inventory;f.s.config['budgets']['x']['daily_usd']=.01
    value=m.apply(f.s,f.db,run_id=f.run,http=f.client)
    assert value['state']=='partial' and not value['complete'] and len(f.calls)==1
    assert value['progress']['handles'][m.HANDLES[0]]['stop_reason']=='API_BUDGET_EXCEEDED'
    assert f.db.query('SELECT cost_usd FROM api_calls')==[{'cost_usd':.01}]


def test_active_inventory_lease_cannot_be_stolen_or_make_another_paid_read(inventory):
    f=inventory;payload={'version':m.VERSION,'run_id':f.run,'scope':m.scope(f.s)}
    ident=f.db.enqueue('maintenance',f.run,m.JOB_TYPE,'tweet',payload,m.VERSION)
    claimed=f.db.claim('tweet',180,4)
    with pytest.raises(HubError,match='TODAY_INVENTORY_ACTIVE_LEASE'):m.apply(f.s,f.db,run_id=f.run,http=f.client)
    assert f.db.get(ident)==claimed and not f.calls


def test_existing_jobs_and_publications_are_classified_without_new_import_or_send(inventory):
    f=inventory;ident=f.db.enqueue('x','900','tweet_process','tweet',{},'owned')
    with f.db.transaction() as db:
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',('owned',ident,'bilibili','x','900','site_uid:'+m.UID,'owned','completed','123','https://t.bilibili.com/123',time.time()))
    value=m.apply(f.s,f.db,run_id=f.run,http=f.client);saved=m._read(f.s.root,value['descriptor'])
    assert saved['statuses']['900']=='published' and value['missing_full_raw_ids']==['901']
    assert len(f.db.query("SELECT * FROM jobs WHERE job_type='tweet_process'"))==1 and len(f.db.query('SELECT * FROM publications'))==1
