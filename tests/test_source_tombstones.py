"""Owned synthetic data only; no model, account, human review or actual send."""
from copy import deepcopy
from datetime import datetime,timezone
import io
import json
import sqlite3
import time
import pytest
from services.hub.db import Store,encode
from services.hub.errors import HubError,PublishUnknown
from services.hub.machine_tweets import MachineTweets,VERSION,KIND,accepted_job_policy
from services.hub.pipelines import TweetPipeline
from services.hub.worker import Worker
from services.hub.x_web_discovery import XWebDiscovery
from services.hub.evidence import sha256_file


@pytest.fixture
def machine(runtime):
    settings,store=runtime
    settings.config.update(machine_tweet_enabled=True,x_web_enabled=True,policy_version=VERSION)
    (settings.root/'config').mkdir()
    policy={'version':VERSION,'owner_confirmed_machine_mode':True,'human_review_required':False,
        'target_site_uid':settings.config['target_bilibili_site_uid'],'x_handles':['example_handle_a','example-channel-b'],
        'rights':{'owner_confirmed':True,'actions':['publish_translation','publish_image'],
                  'reference':'Owned synthetic machine-policy fixture only'},
        'owner_response':'Owned synthetic policy fixture, never a post human-review claim',
        'max_post_age_seconds':7200,'activation_at':time.time()-60,
        'supported_content':['visible_plain_text_without_media_or_context']}
    (settings.root/'config/machine-tweet-policy.json').write_text(encode(policy),'utf-8')
    return settings,store,MachineTweets(settings,store)


def post_job(settings,store,*,source_id='1000000000000000001',policy=None,age=60):
    payload={'post':{'id':source_id,'text':'Owned synthetic text 20:00',
                    'created_at':datetime.fromtimestamp(time.time()-age,timezone.utc).isoformat(),
                    'referenced_tweets':[]},
             'author':{'username':'example_handle_a','name':'Owned synthetic author'},
             'url':f'https://x.com/example_handle_a/status/{source_id}','capture':{},
             'source_api':'owned_fixture_only'}
    ident=store.enqueue('x',source_id,'tweet_process','tweet',payload,policy or accepted_job_policy(settings))
    return store.get(ident)


def publication(store,job,state='unknown'):
    with store.transaction() as db:
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('owned-synthetic-publication',job['job_id'],'bilibili','x',job['source_id'],
             'site_uid:1000000000000000',KIND,state,'123' if state=='completed' else None,
             'https://t.bilibili.com/123' if state=='completed' else None,time.time()))


def mark(store,job):
    with store.transaction() as db:
        db.execute('INSERT INTO source_tombstones VALUES(?,?,?,?,?,?)',
            ('x',job['source_id'],'OWNER_BACKLOG_CLEARED',time.time(),job['job_id'],'owner_clear_all'))


def test_migration_seeds_only_actual_legacy_clear_evidence_and_is_idempotent(machine):
    settings,store,service=machine
    old=post_job(settings,store,policy='human-review-v1')
    other=post_job(settings,store,source_id='1000000000000000002')
    observed_at=1234567890.25
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='failed',error_code='OWNER_BACKLOG_CLEARED',updated_at=? WHERE job_id=?",
            (observed_at,old['job_id']))
    Store(settings.db_path).init()
    rows=store.query('SELECT * FROM source_tombstones')
    assert len(rows)==1 and rows[0]=={'source_platform':'x','source_id':old['source_id'],
        'reason':'OWNER_BACKLOG_CLEARED','marked_at':observed_at,'evidence_job_id':old['job_id'],
        'origin':'legacy_job_error_observed'}
    assert store.get(old['job_id'])['payload']==old['payload']
    assert store.get(other['job_id'])==other
    Store(settings.db_path).init()
    assert store.query('SELECT * FROM source_tombstones')==rows


def test_new_generation_and_reset_job_error_cannot_revive_cleared_source_or_charge(machine):
    settings,store,service=machine;old=post_job(settings,store)
    assert service.clear_backlog(all_existing=True)['job_ids']==[old['job_id']]
    marker=store.query('SELECT * FROM source_tombstones')[0]
    assert marker['origin']=='owner_clear_all'
    # A generic maintenance retry or migration must not be an un-clear operation.
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='pending',error_code=NULL WHERE job_id=?",(old['job_id'],))
    Store(settings.db_path).init()
    assert store.query('SELECT * FROM source_tombstones')==[marker]
    settings.config['machine_source_generation']=2
    current=post_job(settings,store)
    assert current['job_id']!=old['job_id']
    pipeline=TweetPipeline(settings,store,None)
    pipeline.model.invoke=lambda *a,**kw:pytest.fail('Cleared source must not invoke a billable model')
    with pytest.raises(HubError,match='OWNER_BACKLOG_CLEARED'):pipeline.process(current)
    with pytest.raises(HubError,match='OWNER_BACKLOG_CLEARED'):service.fresh(current)
    assert store.get(old['job_id'])['payload']==old['payload']
    assert not store.query('SELECT * FROM api_calls') and not store.query('SELECT * FROM publications')


def test_automatic_window_retirement_uses_a_separate_persistent_origin(machine):
    settings,store,service=machine
    old=post_job(settings,store,age=9000);fresh=post_job(settings,store,source_id='1000000000000000002')
    result=service.clear_backlog()
    assert result['job_ids']==[old['job_id']]
    marker=store.query('SELECT * FROM source_tombstones')[0]
    assert marker['origin']=='policy_window_expired' and marker['evidence_job_id']==old['job_id']
    assert store.get(fresh['job_id'])==fresh


def test_tombstone_job_changes_and_audit_rollback_together(machine):
    settings,store,service=machine;job=post_job(settings,store)
    with store.transaction() as db:
        db.execute("""CREATE TRIGGER owned_audit_failure BEFORE INSERT ON audit
            WHEN NEW.action='clear_tweet_backlog' BEGIN SELECT RAISE(ABORT,'owned failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):service.clear_backlog(all_existing=True)
    assert store.get(job['job_id'])==job
    assert not store.query('SELECT * FROM source_tombstones')
    assert not store.query('SELECT * FROM audit')


def test_source_with_unknown_publication_is_not_cleared_through_another_generation(machine):
    settings,store,service=machine
    unknown=post_job(settings,store,age=9000);publication(store,unknown)
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='publish_unknown' WHERE job_id=?",(unknown['job_id'],))
    settings.config['machine_source_generation']=2
    other=post_job(settings,store,age=9000)
    assert service.clear_backlog(all_existing=True)['cleared']==0
    assert store.get(unknown['job_id'])['state']=='publish_unknown'
    assert store.get(other['job_id'])==other
    assert not store.query('SELECT * FROM source_tombstones')
    assert store.query('SELECT * FROM publications')[0]['state']=='unknown'


def test_discovery_skips_cleared_source_even_if_browser_returns_it_outside_seen_limit(machine):
    settings,store,service=machine;old=post_job(settings,store)
    service.clear_backlog(all_existing=True)
    # The browser accepts at most 1000 IDs. Correctness cannot rely on that list.
    with store.transaction() as db:
        for i in range(1001):
            db.execute('INSERT INTO source_tombstones VALUES(?,?,?,?,?,?)',
                ('x',str(2000000000000000000+i),'OWNER_BACKLOG_CLEARED',time.time()+i,old['job_id'],'owner_clear_all'))
    settings.config['machine_source_generation']=2;settings.config['sources']=settings.config['sources'][:1]
    ident=store.enqueue('scheduler','owned-tombstone-discovery','x_discovery','tweet',{},VERSION)
    discovery=store.claim('tweet',180,4);assert discovery['job_id']==ident
    seen=[]
    def read(*a,**kw):
        seen.extend(kw['seen_ids'])
        return {'state':'observed','result_file':'owned-fixture','posts':[{'payload':old['payload']}]}
    collector=XWebDiscovery(settings,store);collector.browser.latest=read
    result=collector.run(discovery)
    assert len(seen)==1000 and old['source_id'] not in seen
    assert result['new_jobs']==0 and result['sources'][0]['cleared_sources_skipped']==1
    assert len(store.query("SELECT * FROM jobs WHERE job_type='tweet_process'"))==1
    assert not store.query('SELECT * FROM api_calls')


def test_discovery_seen_list_includes_cleared_old_generation_without_a_source_binding(machine):
    settings,store,service=machine;old=post_job(settings,store)
    service.clear_backlog(all_existing=True)
    with store.transaction() as db:db.execute('UPDATE jobs SET error_code=NULL WHERE job_id=?',(old['job_id'],))
    settings.config['machine_source_generation']=2;settings.config['sources']=settings.config['sources'][:1]
    ident=store.enqueue('scheduler','owned-seen-check','x_discovery','tweet',{},VERSION)
    parent=store.claim('tweet',180,4);assert parent['job_id']==ident
    seen=[]
    def read(*a,**kw):
        seen.extend(kw['seen_ids'])
        return {'state':'observed','result_file':'owned-fixture','posts':[]}
    collector=XWebDiscovery(settings,store);collector.browser.latest=read
    assert collector.run(parent)['new_jobs']==0
    assert seen==[old['source_id']] and not store.query('SELECT * FROM web_source_bindings')


def test_billable_quality_request_is_blocked_if_source_is_cleared_during_translation(machine,monkeypatch):
    from services.hub.web_source import WebSources
    settings,store,service=machine;current=post_job(settings,store)
    old=post_job(settings,store,policy='human-review-v1')
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='awaiting_review' WHERE job_id=?",(old['job_id'],))
    monkeypatch.setattr(WebSources,'assess',lambda *a,**kw:{'owned_fixture':True})
    pipeline=TweetPipeline(settings,store,None);calls=[]
    def model(job,phase,source,translation=None):
        calls.append(phase)
        assert phase=='translation'
        service.clear_backlog(all_existing=True)
        return {'translation':'Owned synthetic translation 20:00'}
    pipeline.model.invoke=model
    assert Worker(settings,store,{'tweet_process':pipeline.process}).once('tweet')
    saved=store.get(current['job_id'])
    assert calls==['translation'] and saved['state']=='failed' and saved['error_code']=='OWNER_BACKLOG_CLEARED'
    assert store.query('SELECT * FROM source_tombstones')[0]['evidence_job_id']==old['job_id']
    assert not store.query('SELECT * FROM publications')


def test_reservation_rechecks_tombstone_inside_its_transaction(machine,monkeypatch):
    from services.hub.web_source import WebSources
    settings,store,service=machine;job=post_job(settings,store)
    with store.transaction() as db:db.execute("UPDATE jobs SET state='upload_pending' WHERE job_id=?",(job['job_id'],))
    job=store.get(job['job_id'])
    manifest={'target_site_uid':settings.config['target_bilibili_site_uid'],'render':{'sha256':'owned-image'},'caption_sha256':'owned-caption'}
    service._snapshot=lambda _:(job,manifest)
    def source_check(*a,**kw):
        # This happens after fresh() but before the reservation transaction.
        mark(store,job)
        return {'owned_fixture':True}
    monkeypatch.setattr(WebSources,'assess',source_check)
    with pytest.raises(HubError,match='OWNER_BACKLOG_CLEARED'):
        service.reserve('machine-policy',job['job_id'],site_uid=manifest['target_site_uid'],
            artifact_sha256='owned-image',caption_sha256='owned-caption')
    assert not store.query('SELECT * FROM publications')
    assert store.get(job['job_id'])['state']=='upload_pending'


def test_completed_cross_generation_publication_remains_readable_after_clear(machine):
    settings,store,service=machine;old=post_job(settings,store)
    publication(store,old,state='completed');mark(store,old)
    settings.config['machine_source_generation']=2;current=post_job(settings,store)
    pipeline=TweetPipeline(settings,store,None)
    pipeline.model.invoke=lambda *a,**kw:pytest.fail('Existing publication must not invoke model')
    outcome=pipeline.process(current)
    assert outcome.state=='completed' and outcome.external_id=='123'
    assert outcome.result['already_published'] and not store.query('SELECT * FROM api_calls')


def test_prepared_retry_cannot_reinsert_child_for_a_cleared_source(machine):
    settings,store,service=machine;job=post_job(settings,store)
    service.clear_backlog(all_existing=True)
    service._snapshot=lambda _:pytest.fail('Cleared prepared retries must stop before artifact resume')
    with pytest.raises(HubError,match='OWNER_BACKLOG_CLEARED'):service.resume_prepared(store.get(job['job_id']))
    assert not store.query("SELECT * FROM jobs WHERE source_platform='machine_web'")


def test_existing_unknown_can_still_run_lookup_only_with_source_tombstone(machine,monkeypatch):
    from services.hub.web_publish import _run_website
    settings,store,service=machine;job=post_job(settings,store,age=9000)
    publication(store,job);mark(store,job)
    manifest={'source_url':job['payload']['url'],'target_site_uid':settings.config['target_bilibili_site_uid'],
        'target_name':'Owned fixture target','render':{'png':'owned-fixture.png','sha256':'owned-image'},
        'caption':'owned caption','caption_sha256':'owned-caption'}
    service._snapshot=lambda _:(job,manifest)
    service.reserve=lambda *a,**kw:pytest.fail('Unknown publication cannot authorize a new click')
    observed=[]
    class Input(io.BytesIO):
        def close(self):
            spec=json.loads(self.getvalue());observed.append(spec)
            folder=settings.root/'media'/job['job_id']/'website-attempts'/spec['attempt_id']
            (folder/'result.json').write_text(encode({'state':'not_found_result_unknown','sent':False}),'utf-8')
            super().close()
    class Browser:
        def __init__(self,*a,**kw):self.stdin=Input()
        def poll(self):return 0
    monkeypatch.setattr('services.hub.web_publish.subprocess.Popen',Browser)
    with pytest.raises(PublishUnknown):_run_website(settings,store,job['job_id'],service,KIND,'machine-policy')
    assert len(observed)==1 and observed[0]['mode']=='lookup'
    assert store.query('SELECT * FROM publications')[0]['state']=='unknown'
    assert len(store.query('SELECT * FROM source_tombstones'))==1


def test_lookup_result_can_be_recorded_after_source_is_cleared_without_new_send(machine):
    # This fixture is a mocked UI result in an isolated test database. It does
    # not attest real browser visibility or publish content to an account.
    settings,store,service=machine;job=post_job(settings,store)
    publication(store,job);mark(store,job)
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='publish_unknown',result=? WHERE job_id=?",
            (encode({'machine_web':{'human_review':False,'owned_fixture':True}}),job['job_id']))
    job=store.get(job['job_id']);folder=settings.root/'media'/job['job_id'];folder.mkdir()
    image=folder/'mock-ui-screenshot.png';image.write_bytes(b'Owned synthetic UI-result fixture bytes')
    manifest={'target_site_uid':settings.config['target_bilibili_site_uid'],'caption':'Owned synthetic caption',
        'render':{'sha256':'owned-artifact'}}
    packet={'version':'operator-browser-publication-observation-v1','owned_fixture_only':True,
        'url':'https://t.bilibili.com/123','site_uid':manifest['target_site_uid'],'caption':manifest['caption'],
        'artifact_sha256':'owned-artifact','screenshot_file':str(image),'screenshot_sha256':sha256_file(image),
        'visible_in_owner_browser':True}
    file=folder/'mock-ui-result.json';file.write_text(encode(packet),'utf-8')
    service._snapshot=lambda _:(job,manifest)
    result=service.record_visible('machine-policy',job['job_id'],file)
    assert result['external_id']=='123' and result['human_review'] is False
    assert store.get(job['job_id'])['state']=='completed'
    assert store.query('SELECT * FROM publications')[0]['state']=='completed'
    assert len(store.query('SELECT * FROM source_tombstones'))==1
    assert not store.query("SELECT * FROM audit WHERE action='reserve_machine_website'")
