"""Independent persistence/transport checks; no human, model or platform send."""
from datetime import datetime,timezone
import io
import json
import time
from pathlib import Path
import pytest
from scheduler.service import DurableScheduler
from services.hub.db import encode
from services.hub.errors import PublishUnknown
from services.hub.machine_tweets_v2 import MachineTweetsV2,KIND,VERSION
from services.hub.web_publish import _run_website
from services.hub.readiness import automation_readiness


def unresolved(runtime,kind=KIND):
    settings,store=runtime
    settings.config.update(machine_tweet_enabled=True,policy_version=VERSION)
    payload={'post':{'id':'991','text':'Owned transport fixture','created_at':datetime.fromtimestamp(time.time()-10800,timezone.utc).isoformat()},
        'author':{'username':'example_handle_a','name':'Owned fixture'},'url':'https://x.com/example_handle_a/status/991'}
    ident=store.enqueue('x','991','tweet_process','tweet',payload,VERSION)
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='publish_unknown' WHERE job_id=?",(ident,))
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('owned-v2-unknown',ident,'bilibili','x','991','site_uid:'+settings.config['target_bilibili_site_uid'],kind,'unknown',None,None,time.time()))
    return settings,store,ident


@pytest.mark.parametrize('kind',['dynamic_website_machine_v1',KIND])
def test_persisted_unknown_in_either_policy_triggers_readonly_reconciliation(runtime,kind):
    settings,store,ident=unresolved(runtime,kind)
    scheduler=DurableScheduler(settings,store);scheduler.tick(now=100);scheduler.tick(now=1000)
    jobs=store.query("SELECT * FROM jobs WHERE job_type='tweet_machine_reconcile'")
    assert len(jobs)==1
    assert store.get(ident)['state']=='publish_unknown'
    assert not store.query('SELECT * FROM api_calls')


def test_expired_cleared_v2_unknown_transport_is_lookup_only(runtime,monkeypatch):
    settings,store,ident=unresolved(runtime)
    job=store.get(ident);folder=settings.root/'media'/ident;folder.mkdir()
    with store.transaction() as db:
        db.execute("INSERT INTO source_tombstones VALUES('x','991','OWNER_BACKLOG_CLEARED',?,?,?)",(time.time(),ident,'owner_clear_all'))
    service=MachineTweetsV2(settings,store)
    manifest={'source_url':job['payload']['url'],'target_site_uid':settings.config['target_bilibili_site_uid'],
        'target_name':'Owned fixture','render':{'png':str(folder/'owned.png'),'sha256':'owned-hash'},
        'caption':'Owned fixture '+job['payload']['url'],'caption_sha256':'owned-caption'}
    # Source/model authenticity belongs to the upstream snapshot verifier. This
    # transport test isolates the already persisted uncertain-send boundary.
    service._snapshot=lambda _:(job,manifest)
    service.policy=lambda:{'max_post_age_seconds':7200}
    service.reserve=lambda *a,**kw:pytest.fail('Unknown send must never reserve or send again')
    observed=[]
    class Input(io.BytesIO):
        def close(self):
            spec=json.loads(self.getvalue());observed.append(spec)
            attempt=folder/'website-attempts'/spec['attempt_id']
            (attempt/'result.json').write_text(encode({'state':'not_found_result_unknown','sent':False}),encoding='utf-8')
            super().close()
    class Process:
        def __init__(self,*a,**kw):self.stdin=Input()
        def poll(self):return 0
    monkeypatch.setattr('services.hub.web_publish.subprocess.Popen',Process)
    with pytest.raises(PublishUnknown):_run_website(settings,store,ident,service,KIND,'machine-policy')
    assert len(observed)==1 and observed[0]['mode']=='lookup' and observed[0]['machine_policy']==VERSION
    assert observed[0]['dispatch_expires_at']<time.time()
    assert store.query('SELECT state FROM publications')[0]['state']=='unknown'
    assert store.query("SELECT COUNT(*) n FROM audit WHERE action='reserve_machine_website'")[0]['n']==0
    assert not store.query('SELECT * FROM api_calls')


def test_historical_v1_publication_does_not_accept_active_v2_policy(runtime,monkeypatch):
    settings,store,ident=unresolved(runtime,'dynamic_website_machine_v1')
    with store.transaction() as db:
        db.execute("UPDATE publications SET state='completed',external_id='123',external_url='https://t.bilibili.com/123'")
        db.execute("UPDATE jobs SET state='completed',external_publish_id='123' WHERE job_id=?",(ident,))
    # Isolate the acceptance ledger from policy-file setup: the independently
    # authorized v2 scope is assumed valid, not yet proven by any v2 send.
    monkeypatch.setattr(MachineTweetsV2,'policy',lambda self:{
        'supported_content':['visible_plain_text','public_static_photos_full_dimensions','bounded_reply_parent_reread'],
        'max_post_age_seconds':7200,'activation_at':time.time()-60})
    status=automation_readiness(settings,store)
    assert status['machine_tweet_policy_valid'] is True
    assert status['machine_website_publications_completed']==1
    assert status['active_machine_policy_publications_completed']==0
    assert status['active_machine_policy']==VERSION
    assert status['machine_transport_historical_publication_accepted'] is True
    assert status['machine_tweet_publish_accepted'] is False and status['machine_tweet_ready'] is False
    assert any(b['component']=='dynamic_publish' and b['code']=='MACHINE_PUBLISH_ACCEPTANCE_REQUIRED' for b in status['blockers'])
    assert not store.query("SELECT 1 FROM publications WHERE kind=?",(KIND,))
    assert not store.query('SELECT * FROM api_calls')
