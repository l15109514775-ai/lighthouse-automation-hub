"""Owned two-record intake faults and durable scheduler; no platform/model I/O."""
from copy import deepcopy
from types import SimpleNamespace
import json
import time
import pytest
from scheduler.service import DurableScheduler
from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.machine_tweets_api import MachineTweetsAPI
from services.hub.x_api_discovery import XAPIDiscovery,PERMANENT_ASSET_ERRORS


@pytest.fixture
def intake(runtime,monkeypatch):
    settings,store=runtime
    settings.config.update(x_api_enabled=True,x_web_enabled=False,machine_tweet_enabled=True,policy_version='machine-tweet-v3-api',machine_source_generation=1)
    settings.config['sources']=[{'x_handle':'fixture_only'}]
    monkeypatch.setattr(MachineTweetsAPI,'policy',lambda self:{'max_post_age_seconds':7200,'activation_at':time.time()-60})
    monkeypatch.setattr(MachineTweetsAPI,'clear_backlog',lambda *a,**k:{'cleared':0})
    parent_id=store.enqueue('scheduler','owned-intake-isolation','x_discovery','tweet',{},'machine-tweet-v3-api')
    parent=store.claim('tweet',300,4);assert parent['job_id']==parent_id
    service=XAPIDiscovery(settings,store,None)
    records=[{'payload':{'post':{'id':v}}} for v in ('1000000000000000001','1000000000000000002')]
    response={'records':records,'observed_post_ids':[r['payload']['post']['id'] for r in records],
        'skipped_posts':[],'rejected_posts':[],'catchup_complete':True}
    service.x.latest_official=lambda *a,**k:deepcopy(response)
    service.receipts.cached_author=lambda *a,**k:None
    # This fault-routing seam now sits at capture admission. Actual deferred
    # photo failures/leases are covered by test_x_official_capture_queue_v1.
    service.capture_queue=SimpleNamespace(initialize=lambda:None,known_ids=lambda:[],
        register=lambda *a,**k:service.receipts.ingest_official(*a,**k))
    return settings,store,parent,service,records


@pytest.mark.parametrize('code',sorted(PERMANENT_ASSET_ERRORS))
def test_permanent_first_asset_reject_does_not_starve_second_valid_source(intake,code):
    settings,store,parent,service,records=intake;calls=[]
    def ingest(job,record,policy):
        pid=record['payload']['post']['id'];calls.append(pid)
        if pid==records[0]['payload']['post']['id']:raise HubError(code)
        return {'created':True,'job_id':'owned-not-sent','binding_present':True}
    service.receipts.ingest_official=ingest
    result=service.run(parent)
    assert calls==[r['payload']['post']['id'] for r in records] and result['new_jobs']==1
    assert result['sources'][0]['rejected_posts']==[{'post_id':calls[0],'error_code':code}]
    assert store.query("SELECT value FROM source_cursors WHERE key='x-api:fixture_only'")[0]['value']==calls[1]
    event=store.query("SELECT * FROM events WHERE name='task.failed'")[0]
    assert json.loads(event['data'])=={'job_id':parent['job_id'],'queue':'tweet','source_platform':'x','source_id':calls[0],
        'code':code,'policy_version':'machine-tweet-v3-api'}
    assert not store.query('SELECT * FROM publications') and not store.query('SELECT * FROM api_calls')


@pytest.mark.parametrize('code',['ASSET_NETWORK_ERROR','SOURCE_CAPTURE_LEASE_REQUIRED','SOURCE_RESPONSE_INTEGRITY_CHANGED',
    'API_BUDGET_EXCEEDED','AUTH_EXPIRED','INSUFFICIENT_BALANCE','ASSET_PERMISSION_DENIED','X_OFFICIAL_MEDIA_VALIDATOR_FAILED'])
def test_unknown_or_system_failure_stops_source_and_does_not_advance_cursor(intake,code):
    settings,store,parent,service,records=intake;calls=[]
    def ingest(*args):calls.append(args[1]['payload']['post']['id']);raise HubError(code,retryable=code=='ASSET_NETWORK_ERROR')
    service.receipts.ingest_official=ingest
    with pytest.raises(HubError,match=code):service.run(parent)
    assert calls==[records[0]['payload']['post']['id']]
    assert not store.query("SELECT * FROM source_cursors WHERE key='x-api:fixture_only'")
    assert not store.query('SELECT * FROM publications') and not store.query('SELECT * FROM api_calls')


@pytest.mark.parametrize('code',['OWNER_TWO_HOUR_WINDOW_EXPIRED','OWNER_BACKLOG_CLEARED'])
def test_expired_or_owner_cleared_single_source_is_skipped_without_starving_following(intake,code):
    settings,store,parent,service,records=intake;calls=[]
    def ingest(job,record,policy):
        pid=record['payload']['post']['id'];calls.append(pid)
        if len(calls)==1:raise HubError(code)
        return {'created':True,'job_id':'owned-not-sent','binding_present':True}
    service.receipts.ingest_official=ingest
    value=service.run(parent)
    assert len(calls)==2 and value['new_jobs']==1
    assert value['sources'][0]['skipped_posts']==[{'post_id':calls[0],'reason':code}]
    assert not store.query("SELECT * FROM events WHERE name='task.failed'")


def test_durable_scheduler_queues_api_unknown_lookup_once_and_stops_at_attempt_bound(runtime):
    settings,store=runtime
    settings.config.update(machine_tweet_enabled=True,x_api_enabled=False,x_web_enabled=False)
    parent=store.enqueue('x','1000000000000000003','tweet_process','tweet',{},'machine-tweet-v3-api')
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='publish_unknown',result=? WHERE job_id=?",(encode({'machine_reconcile':{'attempts':0}}),parent))
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',('owned-api-unknown',parent,'bilibili','x',
            '1000000000000000003','site_uid:'+settings.config['target_bilibili_site_uid'],'dynamic_website_machine_v3_api','unknown',None,None,time.time()))
    scheduler=DurableScheduler(settings,store);now=time.time();scheduler.tick(now);scheduler.tick(now)
    jobs=store.query("SELECT * FROM jobs WHERE job_type='tweet_machine_reconcile'")
    assert len(jobs)==1 and jobs[0]['state']=='pending'
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='completed' WHERE job_id=?",(jobs[0]['job_id'],))
        db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode({'machine_reconcile':{'attempts':settings.config['max_attempts']}}),parent))
    scheduler.tick(now+settings.config.get('bilibili_reconcile_minutes',5)*60+1)
    assert len(store.query("SELECT * FROM jobs WHERE job_type='tweet_machine_reconcile'"))==1
    assert store.query('SELECT state FROM publications')[0]['state']=='unknown'
    assert not store.query('SELECT * FROM api_calls')
