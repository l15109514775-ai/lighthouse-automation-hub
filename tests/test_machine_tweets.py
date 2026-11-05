"""Owned synthetic fixtures only. No human approval, real model or platform send."""
from copy import deepcopy
from datetime import datetime,timezone
import io
import json
import time
import pytest
from adapters.machine_compositor import machine_input
from services.hub.db import encode,Store
from services.hub.errors import HubError,PublishUnknown
from services.hub.evidence import sha256_file
from services.hub.machine_tweets import MachineTweets,VERSION,KIND,accepted_job_policy,source_policy_generation
from services.hub.source_receipts import digest
from services.hub.web_source import WebSources
from services.hub.worker import Worker
from services.hub.pipelines import TweetPipeline


def iso(value):return datetime.fromtimestamp(value,timezone.utc).isoformat()


@pytest.fixture
def machine(runtime):
    settings,store=runtime;settings.config['machine_tweet_enabled']=True
    settings.config['policy_version']=VERSION;settings.config['x_web_enabled']=True
    (settings.root/'config').mkdir()
    policy={'version':VERSION,'owner_confirmed_machine_mode':True,'human_review_required':False,
        'target_site_uid':settings.config['target_bilibili_site_uid'],'x_handles':['example_handle_a','example-channel-b'],
        'rights':{'owner_confirmed':True,'actions':['publish_translation','publish_image'],'reference':'owned synthetic authorization fixture'},
        'owner_response':'Synthetic policy fixture only; never a human review of a post',
        'max_post_age_seconds':7200,'activation_at':time.time()-60,
        'supported_content':['visible_plain_text_without_media_or_context']}
    (settings.root/'config/machine-tweet-policy.json').write_text(encode(policy),'utf-8')
    return settings,store,MachineTweets(settings,store)


def source_record(settings,store,*,post_id='1000000000000000001',age=60,facts_changes=None):
    now=time.time();handle='example_handle_a'
    facts={'version':'x-visible-plain-text-v1','human_review':False,'official_api':False,
        'observed_text':'Owned synthetic text 20:00','status_ids':[post_id],'body_count':1,'images':[],
        'foreign_image_count':0,'unsupported_elements':0,'extra_links':[],'body_links':[],
        'preceding_or_reply':False,'show_more':False,'clipped':False,'supported':True,'failures':[],
        'scope':'rendered_public_plain_text_without_media_cards_or_context'}
    facts.update(facts_changes or {})
    payload={'post':{'id':post_id,'text':facts['observed_text'],'created_at':iso(now-age),'referenced_tweets':[]},
        'author':{'username':handle,'name':'Owned synthetic source'},'url':f'https://x.com/{handle}/status/{post_id}',
        'source_api':'x-web-dom-v1','source_media_count':None,'references_observed':False,
        'capture':{'protocol':'x-web-dom-observation-v2','human_review':False,'source_screenshot_reviewed':False,
            'official_api_receipt':False,'visibility_passed':True,'machine_facts_sha256':digest(facts),'captured_at':iso(now)}}
    image_bytes=b'owned synthetic source screenshot bytes, not an external image'
    import hashlib
    payload['capture']['screenshot_sha256']=hashlib.sha256(image_bytes).hexdigest()
    ident=store.enqueue('x',post_id,'tweet_process','tweet',payload,VERSION)
    folder=settings.root/'media'/ident;folder.mkdir()
    screenshot=folder/'browser-source.png';screenshot.write_bytes(image_bytes)
    record={'version':'x-web-dom-observation-v2','payload':payload,'machine_source_facts':facts,
        'screenshot_file':str(screenshot),'human_review':False}
    (folder/'web-observation.json').write_text(encode(record),'utf-8')
    return store.get(ident),record


def bound_source(settings,store,**kwargs):
    parent_id=store.enqueue('scheduler','owned-discovery','x_discovery','tweet',{},VERSION)
    parent=store.claim('tweet',180,4)
    job,record=source_record(settings,store,**kwargs)
    WebSources(settings,store).bind(parent,job['job_id'],record)
    store.finish(parent,'completed',result={'synthetic_only':True})
    return job,record,parent


def publication(store,parent,*,state='unknown',kind=KIND,key='owned-synthetic-key'):
    with store.transaction() as db:
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (key,parent['job_id'],'bilibili','x',parent['source_id'],'site_uid:1000000000000000',kind,state,
             '1234567890123456789' if state=='completed' else None,
             'https://t.bilibili.com/1234567890123456789' if state=='completed' else None,time.time()))


def test_only_machine_flags_are_written_and_original_input_stays_unreviewed(machine):
    settings,store,service=machine;job,record=source_record(settings,store)
    value=machine_input(job['payload'],'Owned synthetic text 20:00')['post']
    assert value['review']=={'sourceConfirmed':False,'translationConfirmed':False}
    assert value['sourceScreenshot']['reviewed'] is False
    assert value['sourceMediaCount']==0 and value['sourceQuote']=='none' and not value['notes']
    assert record['payload']['source_media_count'] is None
    assert record['payload']['capture']['human_review'] is False


def test_source_binding_requires_current_discovery_lease_and_detects_tampering(machine):
    settings,store,service=machine;job,record,parent=bound_source(settings,store)
    receipt=WebSources(settings,store).assess(job)
    assert receipt['capture_integrity_passed'] and receipt['visible_plain_text_verified']
    assert receipt['human_review'] is False and receipt['official_api'] is False
    assert receipt['hidden_or_unrendered_content_verified'] is False
    with pytest.raises(HubError,match='WEB_SOURCE_CAPTURE_LEASE_REQUIRED'):
        WebSources(settings,store).bind(parent,job['job_id'],record)
    (settings.root/'media'/job['job_id']/'browser-source.png').write_bytes(b'changed')
    with pytest.raises(HubError,match='MACHINE_WEB_SOURCE_INTEGRITY_INVALID'):
        WebSources(settings,store).assess(job)
    assert not store.query('SELECT * FROM publications')


def test_forged_model_friendly_source_object_does_not_replace_db_payload(machine):
    settings,store,service=machine;job,record,parent=bound_source(settings,store)
    forged=deepcopy(job);forged['payload']['post']['text']='Changed input'
    with pytest.raises(HubError,match='MACHINE_WEB_SOURCE_JOB_CHANGED'):
        WebSources(settings,store).assess(forged)


@pytest.mark.parametrize('measurement',[{'preceding_or_reply':True},{'foreign_image_count':1},
    {'extra_links':['https://example.test/card']},{'clipped':True},{'body_count':2},
    {'body_links':[{'visible':'display…','href':'https://example.test'}]}])
def test_supported_self_report_cannot_override_incompatible_dom_measurement(machine,measurement):
    settings,store,service=machine
    job,record,parent=bound_source(settings,store,facts_changes=measurement)
    assert record['machine_source_facts']['supported'] is True
    with pytest.raises(HubError,match='MACHINE_SOURCE_CONTENT_UNSUPPORTED'):
        WebSources(settings,store).assess(job)


def test_legacy_source_claim_without_binding_cannot_start_machine_pipeline(machine):
    settings,store,service=machine;job,record=source_record(settings,store)
    pipeline=TweetPipeline(settings,store,None)
    pipeline.model.invoke=lambda *a,**kw:pytest.fail('Unbound source must not call a paid model')
    assert Worker(settings,store,{'tweet_process':pipeline.process}).once('tweet')
    assert store.get(job['job_id'])['state']=='failed'
    assert store.get(job['job_id'])['error_code']=='MACHINE_WEB_SOURCE_BINDING_MISSING'
    assert not store.query('SELECT * FROM api_calls') and not store.query('SELECT * FROM publications')


def test_expired_post_is_stopped_before_model_or_browser(machine):
    settings,store,service=machine;job,record,parent=bound_source(settings,store,age=7201)
    pipeline=TweetPipeline(settings,store,None)
    pipeline.model.invoke=lambda *a,**kw:pytest.fail('Expired source must not call a paid model')
    assert Worker(settings,store,{'tweet_process':pipeline.process}).once('tweet')
    saved=store.get(job['job_id'])
    assert saved['state']=='failed' and saved['error_code']=='OWNER_TWO_HOUR_WINDOW_EXPIRED'
    assert not store.query('SELECT * FROM api_calls') and not store.query('SELECT * FROM publications')


@pytest.mark.parametrize('text',['2026-10-31T12:00:00','not a time'])
def test_naive_or_invalid_post_time_never_enters_machine_publishing(machine,text):
    settings,store,service=machine;job,record=source_record(settings,store)
    job['payload']['post']['created_at']=text
    with pytest.raises(HubError,match='MACHINE_SOURCE_TIMESTAMP_INVALID'):service.fresh(job)


@pytest.mark.parametrize('code,retryable,state',[('MODEL_CREDENTIAL_OR_SELECTION_REQUIRED',False,'failed'),
    ('MODEL_PERMISSION_DENIED',False,'failed'),('NETWORK_TIMEOUT',True,'retry_scheduled'),('AUTH_EXPIRED',False,'auth_expired')])
def test_model_errors_keep_specific_codes_without_demanding_human_review(machine,code,retryable,state):
    settings,store,service=machine;job,record,parent=bound_source(settings,store)
    pipeline=TweetPipeline(settings,store,None)
    def fail(*a,**kw):raise HubError(code,review=True,retryable=retryable)
    pipeline.model.invoke=fail
    assert Worker(settings,store,{'tweet_process':pipeline.process}).once('tweet')
    saved=store.get(job['job_id'])
    assert saved['state']==state and saved['error_code']==code
    assert not store.query('SELECT * FROM publications')


def test_translation_quality_failure_does_not_queue_or_publish(machine):
    settings,store,service=machine;job,record,parent=bound_source(settings,store)
    pipeline=TweetPipeline(settings,store,None)
    pipeline.model.invoke=lambda job,phase,source,translation=None:({'translation':'Owned translation 20:00'} if phase=='translation' else
        {'passed':False,'issues':['owned synthetic error']})
    assert Worker(settings,store,{'tweet_process':pipeline.process}).once('tweet')
    saved=store.get(job['job_id'])
    assert saved['state']=='failed' and saved['error_code']=='TRANSLATION_CHECK_FAILED'
    assert not saved['result']['evidence']['machine_checks_passed']
    assert saved['result']['evidence']['human_review'] is False
    assert not store.query("SELECT * FROM jobs WHERE job_type='tweet_machine_publish'")
    assert not store.query('SELECT * FROM publications')


def test_cross_policy_completed_publication_is_reused_without_charging_or_resending(machine):
    settings,store,service=machine;job,record=source_record(settings,store)
    publication(store,job,state='completed',kind='dynamic_website_supervised')
    pipeline=TweetPipeline(settings,store,None)
    pipeline.model.invoke=lambda *a,**kw:pytest.fail('Already published source must not be translated again')
    assert Worker(settings,store,{'tweet_process':pipeline.process}).once('tweet')
    saved=store.get(job['job_id'])
    assert saved['state']=='completed' and saved['result']['already_published'] is True
    assert saved['external_publish_id']=='1234567890123456789'
    assert len(store.query('SELECT * FROM publications'))==1 and not store.query('SELECT * FROM api_calls')


def test_owner_queue_clearing_preserves_history_and_unresolved_publications(machine):
    settings,store,service=machine
    old,_=source_record(settings,store,post_id='1000000000000000001',age=7201)
    fresh,_=source_record(settings,store,post_id='1000000000000000002',age=10)
    unknown,_=source_record(settings,store,post_id='1000000000000000003',age=9000)
    publication(store,unknown)
    child=store.enqueue('machine_web',old['job_id'],'tweet_machine_publish','tweet',{'synthetic':True},VERSION)
    with store.transaction() as db:db.execute("UPDATE jobs SET state='publish_unknown' WHERE job_id=?",(unknown['job_id'],))
    result=service.clear_backlog()
    assert result['job_ids']==[old['job_id']]
    assert store.get(old['job_id'])['state']=='failed' and store.get(child)['state']=='failed'
    assert store.get(old['job_id'])['payload']==old['payload']
    assert store.get(fresh['job_id'])['state']=='pending'
    assert store.get(unknown['job_id'])['state']=='publish_unknown'
    assert store.query('SELECT * FROM audit')[-1]['action']=='clear_tweet_backlog'
    assert result['unresolved_publications_retained'] and result['history_retained']


def test_reserve_rechecks_time_after_render_before_actual_click(machine,monkeypatch):
    settings,store,service=machine;job,record=source_record(settings,store,age=7190)
    m={'target_site_uid':settings.config['target_bilibili_site_uid'],'render':{'sha256':'image'},'caption_sha256':'caption'}
    service._snapshot=lambda _:(job,m)
    def expired(*a,**kw):raise HubError('OWNER_TWO_HOUR_WINDOW_EXPIRED')
    service.fresh=expired
    with pytest.raises(HubError,match='OWNER_TWO_HOUR_WINDOW_EXPIRED'):
        service.reserve('machine-policy',job['job_id'],site_uid=m['target_site_uid'],artifact_sha256='image',caption_sha256='caption')
    assert not store.query('SELECT * FROM publications')


def test_reader_cannot_reserve_or_record_machine_result(machine):
    settings,store,service=machine
    with pytest.raises(HubError,match='ACCESS_DENIED'):
        service.reserve('reader','missing',site_uid='x',artifact_sha256='x',caption_sha256='x')
    with pytest.raises(HubError,match='ACCESS_DENIED'):service.record_visible('reader','missing','missing')
    assert len(store.query("SELECT * FROM audit WHERE outcome='denied'"))==2
    assert not store.query('SELECT * FROM publications')


def test_unknown_dispatch_after_restart_runs_lookup_only_even_if_source_expired(machine,monkeypatch):
    from services.hub.web_publish import _run_website
    settings,store,service=machine;job,record=source_record(settings,store,age=9000)
    publication(store,job)
    manifest={'source_url':job['payload']['url'],'target_site_uid':settings.config['target_bilibili_site_uid'],
        'target_name':'Owned synthetic target','render':{'png':'owned-fixture.png','sha256':'image'},'caption':'caption','caption_sha256':'caption-hash'}
    service._snapshot=lambda _:(job,manifest)
    service.reserve=lambda *a,**kw:pytest.fail('Unknown dispatch must never authorize another click')
    observed=[]
    class Input(io.BytesIO):
        def close(self):
            spec=json.loads(self.getvalue());observed.append(spec)
            folder=settings.root/'media'/job['job_id']/'website-attempts'/spec['attempt_id']
            (folder/'result.json').write_text(encode({'state':'blocked','error_code':'AUTH_EXPIRED'}),'utf-8')
            super().close()
    class FakeBrowser:
        def __init__(self,*a,**kw):self.stdin=Input()
        def poll(self):return 0
    monkeypatch.setattr('services.hub.web_publish.subprocess.Popen',FakeBrowser)
    with pytest.raises(PublishUnknown):_run_website(settings,store,job['job_id'],service,KIND,'machine-policy')
    assert observed[0]['mode']=='lookup' and len(observed)==1
    assert len(store.query('SELECT * FROM publications'))==1
    assert store.query("SELECT name FROM events")[-1]['name']=='auth.expired'


def test_restart_resumes_prepared_manifest_without_rerender_or_new_child(machine,monkeypatch):
    settings,store,service=machine;job,record=source_record(settings,store)
    with store.transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',
        (encode({'machine_web':{'manifest_sha256':'owned-stable-manifest','human_review':False}}),job['job_id']))
    service._snapshot=lambda _:(store.get(job['job_id']),{'synthetic':True})
    first=service.resume_prepared(store.get(job['job_id']))
    second=MachineTweets(settings,Store(settings.db_path))
    second._snapshot=service._snapshot
    again=second.resume_prepared(store.get(job['job_id']))
    assert first.state=='upload_pending' and again.result['machine_dispatch_job_id']==first.result['machine_dispatch_job_id']
    assert len(store.query("SELECT * FROM jobs WHERE job_type='tweet_machine_publish'"))==1
    assert store.get(first.result['machine_dispatch_job_id'])['payload']['manifest_sha256']=='owned-stable-manifest'
    assert not store.query('SELECT * FROM api_calls') and not store.query('SELECT * FROM publications')


def test_initial_dispatch_hold_is_durable_and_expired_hold_does_not_delay_future_jobs(machine):
    settings,store,service=machine;job,record=source_record(settings,store)
    file=settings.root/'config/machine-tweet-policy.json';policy=json.loads(file.read_text('utf-8'))
    hold=policy['activation_at']+300;policy['dispatch_not_before']=hold;file.write_text(encode(policy),'utf-8')
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='upload_pending',result=? WHERE job_id=?",
            (encode({'machine_web':{'manifest_sha256':'owned-stable-manifest','human_review':False}}),job['job_id']))
    service._snapshot=lambda _:(store.get(job['job_id']),{'synthetic':True})
    result=service.resume_prepared(store.get(job['job_id']))
    child=store.get(result.result['machine_dispatch_job_id'])
    assert child['next_run']==hold and store.claim('tweet',180,4) is None
    reopened=Store(settings.db_path)
    assert reopened.get(child['job_id'])['next_run']==hold
    # Simulate the hold elapsing via a one-second-old policy for a fresh new job.
    policy['dispatch_not_before']=time.time()-1;file.write_text(encode(policy),'utf-8')
    other,_=source_record(settings,store,post_id='1000000000000000002')
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='upload_pending',result=? WHERE job_id=?",
            (encode({'machine_web':{'manifest_sha256':'owned-other-manifest','human_review':False}}),other['job_id']))
    later=service.resume_prepared(store.get(other['job_id']))
    claimed=store.claim('tweet',180,4)
    assert claimed['job_id']==later.result['machine_dispatch_job_id']


def test_machine_reconciliation_is_bounded_and_completes_related_child(machine,monkeypatch):
    import services.hub.web_publish
    settings,store,service=machine;job,record=source_record(settings,store)
    publication(store,job)
    child=store.enqueue('machine_web',job['job_id'],'tweet_machine_publish','tweet',{'synthetic':True},VERSION)
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='publish_unknown' WHERE job_id IN (?,?)",(job['job_id'],child))
    calls=[]
    def unknown(*a,**kw):calls.append(True);raise PublishUnknown()
    monkeypatch.setattr(services.hub.web_publish,'run_machine_tweet',unknown)
    for _ in range(5):service.reconcile({})
    assert len(calls)==settings.config['max_attempts']
    assert store.get(job['job_id'])['result']['machine_reconcile']['attempts']==settings.config['max_attempts']
    assert store.get(child)['state']=='publish_unknown'
    with store.transaction() as db:db.execute("UPDATE jobs SET result='{}' WHERE job_id=?",(job['job_id'],))
    monkeypatch.setattr(services.hub.web_publish,'run_machine_tweet',lambda *a,**kw:{'state':'completed','external_id':'123','url':'https://t.bilibili.com/123'})
    service.reconcile({})
    assert store.get(child)['state']=='completed' and store.get(child)['external_publish_id']=='123'
    assert store.get(child)['result']['previous_dispatch_state']=='publish_unknown'


@pytest.mark.parametrize('generation,expected',[(1,VERSION),(2,VERSION+':source-2'),(1000,VERSION+':source-1000')])
def test_source_generation_has_canonical_job_policy_without_changing_publish_policy(machine,generation,expected):
    settings,store,service=machine;original=service.policy()
    settings.config['machine_source_generation']=generation
    assert accepted_job_policy(settings)==expected and source_policy_generation(expected)==generation
    assert service.policy()==original and service.policy()['version']==VERSION
    settings.config.pop('machine_source_generation')
    assert accepted_job_policy(settings)==VERSION


@pytest.mark.parametrize('generation',[True,False,0,-1,1001,1.0,'2',None])
def test_invalid_source_generation_cannot_be_accepted(machine,generation):
    settings,store,service=machine;settings.config['machine_source_generation']=generation
    with pytest.raises(HubError,match='MACHINE_SOURCE_GENERATION_INVALID'):accepted_job_policy(settings)


@pytest.mark.parametrize('policy',[VERSION+':source-1',VERSION+':source-02',VERSION+':source-0',VERSION+':source-1001',VERSION+':unknown','human-review-v1'])
def test_noncanonical_job_policy_does_not_masquerade_as_machine_source_generation(policy):
    with pytest.raises(HubError,match='MACHINE_SOURCE_GENERATION_INVALID'):source_policy_generation(policy)


def test_prior_generation_pending_job_is_not_routed_back_to_human_pipeline(machine):
    settings,store,service=machine;job,record=source_record(settings,store)
    settings.config['machine_source_generation']=2
    pipeline=TweetPipeline(settings,store,None)
    pipeline.model.invoke=lambda *a,**kw:pytest.fail('Inactive generation must not call models or request human review')
    assert Worker(settings,store,{'tweet_process':pipeline.process}).once('tweet')
    saved=store.get(job['job_id'])
    assert saved['state']=='failed' and saved['error_code']=='MACHINE_SOURCE_GENERATION_NOT_ACTIVE'
    assert saved['payload']==job['payload'] and not store.query('SELECT * FROM api_calls')


def test_current_generation_can_enter_machine_path_without_mutating_old_failure(machine):
    settings,store,service=machine;old,record=source_record(settings,store)
    with store.transaction() as db:db.execute("UPDATE jobs SET state='failed',error_code='MACHINE_SOURCE_CONTENT_UNSUPPORTED' WHERE job_id=?",(old['job_id'],))
    original=store.get(old['job_id']);settings.config['machine_source_generation']=2
    current=store.enqueue('x',old['source_id'],'tweet_process','tweet',old['payload'],accepted_job_policy(settings))
    pipeline=TweetPipeline(settings,store,None)
    pipeline.model.invoke=lambda *a,**kw:pytest.fail('Missing new source binding must block paid calls')
    assert Worker(settings,store,{'tweet_process':pipeline.process}).once('tweet')
    saved=store.get(current)
    assert saved['policy_version']==VERSION+':source-2' and saved['error_code']=='MACHINE_WEB_SOURCE_BINDING_MISSING'
    assert saved['state']=='failed' and store.get(old['job_id'])==original
    assert not store.query('SELECT * FROM publications')


def test_generation_upgrade_does_not_restart_two_hour_clock(machine):
    settings,store,service=machine;job,record=source_record(settings,store,age=7201)
    policy_before=service.policy();settings.config['machine_source_generation']=2
    current=store.enqueue('x',job['source_id'],'tweet_process','tweet',job['payload'],accepted_job_policy(settings))
    with pytest.raises(HubError,match='OWNER_TWO_HOUR_WINDOW_EXPIRED'):service.fresh(store.get(current))
    assert service.policy()==policy_before


def test_prior_generation_snapshot_is_readable_only_for_a_persisted_publication(machine):
    settings,store,service=machine;job,record=source_record(settings,store)
    settings.config['machine_source_generation']=2
    with pytest.raises(HubError,match='MACHINE_SOURCE_GENERATION_NOT_ACTIVE'):service._require_snapshot_generation(job)
    publication(store,job)
    service._require_snapshot_generation(job)
    with pytest.raises(HubError,match='MACHINE_SOURCE_GENERATION_NOT_ACTIVE'):service.fresh(job)
    with pytest.raises(HubError,match='MACHINE_SOURCE_GENERATION_NOT_ACTIVE'):service.resume_prepared(job)
    assert len(store.query('SELECT * FROM publications'))==1


def test_cross_generation_completed_source_is_reused_without_charge_or_duplicate_publish(machine):
    settings,store,service=machine;old,record=source_record(settings,store)
    publication(store,old,state='completed')
    settings.config['machine_source_generation']=2
    with store.transaction() as db:db.execute("UPDATE jobs SET state='completed' WHERE job_id=?",(old['job_id'],))
    current=store.enqueue('x',old['source_id'],'tweet_process','tweet',old['payload'],accepted_job_policy(settings))
    pipeline=TweetPipeline(settings,store,None)
    pipeline.model.invoke=lambda *a,**kw:pytest.fail('Published sources are idempotent across capture generations')
    assert Worker(settings,store,{'tweet_process':pipeline.process}).once('tweet')
    saved=store.get(current)
    assert saved['state']=='completed' and saved['result']['already_published'] is True
    assert len(store.query('SELECT * FROM publications'))==1 and not store.query('SELECT * FROM api_calls')


@pytest.mark.parametrize('literal',['0','1001','true','false','"2"','2.0'])
def test_settings_load_rejects_invalid_source_generation(runtime,monkeypatch,literal):
    from pathlib import Path
    from services.hub.config import Settings
    settings,store=runtime
    original=(Path(__file__).resolve().parents[1]/'config/hub.toml').read_text('utf-8')
    original=original.replace('machine_source_generation = 1 #',f'machine_source_generation = {literal} #')
    file=settings.root/'owned-invalid-generation.toml';file.write_text(original,'utf-8')
    monkeypatch.setenv('HUB_CONFIG',str(file));monkeypatch.setenv('HUB_ROOT',str(settings.root))
    with pytest.raises(ValueError,match='Invalid machine source generation'):Settings.load()
