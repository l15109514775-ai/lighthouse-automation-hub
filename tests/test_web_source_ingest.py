from pathlib import Path as _RecoveryPath
from services.hub.locks import exclusive as _recovery_lock
"""Owned synthetic observations; fault injection never calls external services."""
from copy import deepcopy
from datetime import datetime,timezone
import json
from pathlib import Path
import time
import pytest
from services.hub.db import encode,Store
from services.hub.errors import HubError
from services.hub.evidence import sha256_file
from services.hub.source_receipts import digest
from services.hub.web_source import WebSources
from services.hub.x_web_discovery import XWebDiscovery


@pytest.fixture
def intake(runtime):
    settings,store=runtime
    parent_id=store.enqueue('scheduler','owned-v2-discovery','x_discovery','tweet',{},'machine-tweet-v1')
    parent=store.claim('tweet',180,4)
    folder=settings.root/'media'/parent_id/'x-web/owned-attempt'
    folder.mkdir(parents=True)
    image=folder/'article.png';image.write_bytes(b'Owned synthetic screenshot, never an external post')
    post_id='1000000000000000001';now=datetime.now(timezone.utc).isoformat()
    facts={'version':'x-visible-plain-text-v1','human_review':False,'official_api':False,
        'observed_text':'Owned synthetic source','status_ids':[post_id],'body_count':1,'images':[],
        'foreign_image_count':0,'unsupported_elements':0,'extra_links':[],'body_links':[],
        'preceding_or_reply':False,'show_more':False,'clipped':False,'supported':True,'failures':[],
        'scope':'rendered_public_plain_text_without_media_cards_or_context'}
    source={'post':{'id':post_id,'text':facts['observed_text'],'created_at':now,'referenced_tweets':[]},
        'author':{'username':'example_handle_a','name':'Owned synthetic author'},
        'url':f'https://x.com/example_handle_a/status/{post_id}','source_api':'x-web-dom-v1',
        'source_media_count':None,'references_observed':False,
        'capture':{'protocol':'x-web-dom-observation-v2','human_review':False,'source_screenshot_reviewed':False,
            'official_api_receipt':False,'visibility_passed':True,'machine_facts_sha256':digest(facts),
            'captured_at':now,'screenshot_sha256':sha256_file(image)}}
    record={'version':'x-web-dom-observation-v2','payload':source,'machine_source_facts':facts,
        'screenshot_file':str(image),'human_review':False}
    return settings,store,parent,record


def counts(store):
    return len(store.query("SELECT * FROM jobs WHERE job_type='tweet_process'")),len(store.query('SELECT * FROM web_source_bindings'))


def test_v2_files_are_durable_before_atomic_child_and_binding_commit(intake,monkeypatch):
    settings,store,parent,record=intake;service=WebSources(settings,store)
    syncs=[];original=service._sync_dir
    def sync(folder):
        assert counts(Store(settings.db_path))==(0,0)
        syncs.append(folder);original(folder)
    monkeypatch.setattr(service,'_sync_dir',sync)
    result=service.ingest(parent,record,'machine-tweet-v1')
    assert result['created'] and result['binding_present'] and len(syncs)==2
    assert counts(Store(settings.db_path))==(1,1)
    child=store.get(result['job_id'])
    assert child['state']=='pending' and child['payload']==record['payload']
    assert WebSources(settings,Store(settings.db_path)).assess(child)['capture_integrity_passed']
    assert (settings.root/'media'/child['job_id']/'browser-source.png').read_bytes()==Path(record['screenshot_file']).read_bytes()


def test_file_write_failure_does_not_expose_child_and_new_attempt_can_recover(intake,monkeypatch):
    settings,store,parent,record=intake;service=WebSources(settings,store)
    original=service._write_durable
    def fail_observation(file,data):
        if file.name=='web-observation.json':raise OSError('owned injected disk failure')
        return original(file,data)
    monkeypatch.setattr(service,'_write_durable',fail_observation)
    with pytest.raises(HubError,match='WEB_SOURCE_FILES_NOT_DURABLE') as err:
        service.ingest(parent,record,'machine-tweet-v1')
    assert err.value.retryable and counts(store)==(0,0)
    # A durable orphan screenshot is retained as recovery evidence, never a job.
    orphans=list((settings.root/'media').glob('*/browser-source.png'))
    assert len(orphans)==1
    monkeypatch.setattr(service,'_write_durable',original)
    with _recovery_lock(_RecoveryPath(store.path).parent / "locks" / 'tweet'):
        store.recover('tweet',4,all_processing=True)
        recovered=store.claim('tweet',180,4)
    assert recovered['job_id']==parent['job_id'] and recovered['lease_owner']!=parent['lease_owner']
    value=service.ingest(recovered,record,'machine-tweet-v1')
    assert counts(store)==(1,1) and value['created']
    assert orphans[0].is_file() and value['job_id']!=orphans[0].parent.name


@pytest.mark.parametrize('after_binding',[False,True])
def test_interruption_in_binding_transaction_rolls_back_both_rows_and_can_retry(intake,monkeypatch,after_binding):
    settings,store,parent,record=intake;service=WebSources(settings,store)
    class InjectedInterruption(BaseException):pass
    original=service._binding
    def interrupted(*args):
        # Another connection cannot see either uncommitted row.
        assert counts(Store(settings.db_path))==(0,0)
        if after_binding:original(*args)
        raise InjectedInterruption()
    monkeypatch.setattr(service,'_binding',interrupted)
    with pytest.raises(InjectedInterruption):service.ingest(parent,record,'machine-tweet-v1')
    assert counts(store)==(0,0)
    assert len(list((settings.root/'media').glob('*/web-observation.json')))==1
    monkeypatch.setattr(service,'_binding',original)
    service.ingest(parent,record,'machine-tweet-v1')
    assert counts(store)==(1,1)


def test_parent_lease_change_during_file_io_prevents_child_commit(intake,monkeypatch):
    settings,store,parent,record=intake;service=WebSources(settings,store)
    original=service._persist_observation
    def revoke_after_files(*args):
        original(*args)
        with store.transaction() as db:db.execute('UPDATE jobs SET lease_owner=? WHERE job_id=?',('owned-lease-revoked',parent['job_id']))
    monkeypatch.setattr(service,'_persist_observation',revoke_after_files)
    with pytest.raises(HubError,match='WEB_SOURCE_CAPTURE_LEASE_REQUIRED'):
        service.ingest(parent,record,'machine-tweet-v1')
    assert counts(store)==(0,0)


def test_repeated_observation_preserves_first_payload_files_and_binding(intake):
    settings,store,parent,record=intake;service=WebSources(settings,store)
    first=service.ingest(parent,record,'machine-tweet-v1');child=store.get(first['job_id'])
    file=settings.root/'media'/child['job_id']/'web-observation.json';original=file.read_bytes()
    row=store.query('SELECT * FROM web_source_bindings')[0]
    changed=deepcopy(record);changed['payload']['post']['text']='Observed edit must not overwrite input'
    Path(record['screenshot_file']).write_bytes(b'later different screenshot')
    again=service.ingest(parent,changed,'machine-tweet-v1')
    assert again['job_id']==first['job_id'] and not again['created']
    assert counts(store)==(1,1) and store.get(child['job_id'])['payload']==child['payload']
    assert file.read_bytes()==original and store.query('SELECT * FROM web_source_bindings')[0]==row


def test_v2_discovery_uses_atomic_ingest_and_duplicate_read_never_reenqueues(intake):
    settings,store,parent,record=intake
    settings.config['sources']=settings.config['sources'][:1]
    service=XWebDiscovery(settings,store)
    service.browser.latest=lambda *a,**kw:{'state':'observed','result_file':'owned-v2-result','posts':[record]}
    result=service.run(parent)
    assert result['new_jobs']==1 and counts(store)==(1,1)
    assert service.run(parent)['new_jobs']==0 and counts(store)==(1,1)


def test_preexisting_unbound_child_is_preserved_with_explicit_error_not_fake_binding(intake):
    settings,store,parent,record=intake
    settings.config['sources']=settings.config['sources'][:1]
    old=store.enqueue('x',record['payload']['post']['id'],'tweet_process','tweet',record['payload'],settings.config['policy_version'])
    original=store.get(old)
    service=XWebDiscovery(settings,store)
    service.browser.latest=lambda *a,**kw:{'state':'observed','result_file':'owned-v2-result','posts':[record]}
    with pytest.raises(HubError,match='WEB_EXISTING_UNBOUND_SOURCE_PRESERVED'):service.run(parent)
    assert counts(store)==(1,0) and store.get(old)==original
    assert store.query("SELECT * FROM events WHERE name='task.failed'")
    assert store.query('SELECT * FROM source_polls')[0]['error_code']=='WEB_EXISTING_UNBOUND_SOURCE_PRESERVED'


def test_screenshot_outside_leased_discovery_is_rejected_without_copy(intake,tmp_path):
    settings,store,parent,record=intake
    outside=tmp_path/'private-not-a-source.png';outside.write_bytes(b'owned negative fixture')
    record['screenshot_file']=str(outside)
    with pytest.raises(HubError,match='WEB_SOURCE_SCREENSHOT_PATH_INVALID'):
        WebSources(settings,store).ingest(parent,record,'machine-tweet-v1')
    assert counts(store)==(0,0)


def test_historical_unbound_machine_row_is_not_silently_skipped_by_seen_ids(intake,monkeypatch):
    from services.hub.machine_tweets import MachineTweets
    settings,store,parent,record=intake
    settings.config.update(machine_tweet_enabled=True,policy_version='machine-tweet-v1')
    settings.config['sources']=settings.config['sources'][:1]
    old=store.enqueue('x',record['payload']['post']['id'],'tweet_process','tweet',record['payload'],'machine-tweet-v1')
    monkeypatch.setattr(MachineTweets,'policy',lambda _:{'activation_at':time.time(),'max_post_age_seconds':7200})
    monkeypatch.setattr(MachineTweets,'clear_backlog',lambda _:{'synthetic_fixture':True})
    seen=[]
    def read(handle,**kwargs):
        seen.extend(kwargs['seen_ids'])
        return {'state':'observed','result_file':'owned-v2-result','posts':[record]}
    service=XWebDiscovery(settings,store);service.browser.latest=read
    with pytest.raises(HubError,match='WEB_EXISTING_UNBOUND_SOURCE_PRESERVED'):service.run(parent)
    assert record['payload']['post']['id'] not in seen
    assert store.get(old)['payload']==record['payload'] and counts(store)==(1,0)


def test_discovery_file_failure_records_truthful_error_and_retry_remains_possible(intake,monkeypatch):
    settings,store,parent,record=intake
    settings.config['sources']=settings.config['sources'][:1]
    original=WebSources._write_durable
    def no_disk(file,data):raise OSError('owned injected failure')
    monkeypatch.setattr(WebSources,'_write_durable',staticmethod(no_disk))
    service=XWebDiscovery(settings,store)
    service.browser.latest=lambda *a,**kw:{'state':'observed','result_file':'owned-v2-result','posts':[record]}
    with pytest.raises(HubError,match='WEB_SOURCE_FILES_NOT_DURABLE') as err:service.run(parent)
    assert err.value.retryable and counts(store)==(0,0)
    poll=store.query('SELECT * FROM source_polls')[0]
    assert poll['error_code']=='WEB_SOURCE_FILES_NOT_DURABLE' and poll['last_success_at'] is None
    assert store.get(parent['job_id'])['result']['new_jobs']==0
    monkeypatch.setattr(WebSources,'_write_durable',staticmethod(original))
    assert service.run(parent)['new_jobs']==1 and counts(store)==(1,1)


def test_generation_two_discovery_recaptures_old_failure_and_deduplicates_current_generation(intake,monkeypatch):
    from services.hub.machine_tweets import MachineTweets,VERSION
    settings,store,parent,record=intake
    settings.config.update(machine_tweet_enabled=True,policy_version=VERSION,machine_source_generation=2)
    settings.config['sources']=settings.config['sources'][:1]
    # The previous capture remains immutable, including its real rejection.
    old=WebSources(settings,store).ingest(parent,record,VERSION)['job_id']
    with store.transaction() as db:db.execute("UPDATE jobs SET state='failed',error_code='MACHINE_SOURCE_CONTENT_UNSUPPORTED' WHERE job_id=?",(old,))
    before=store.get(old);binding_before=store.query('SELECT * FROM web_source_bindings WHERE job_id=?',(old,))[0]
    monkeypatch.setattr(MachineTweets,'policy',lambda _:{'activation_at':time.time(),'max_post_age_seconds':7200})
    monkeypatch.setattr(MachineTweets,'clear_backlog',lambda _:{'synthetic_fixture':True})
    seen=[]
    def read(handle,**kwargs):
        seen.append(kwargs['seen_ids'])
        return {'state':'observed','result_file':'owned-v2-result','posts':[record]}
    service=XWebDiscovery(settings,store);service.browser.latest=read
    assert service.run(parent)['new_jobs']==1
    current=store.query("SELECT job_id FROM jobs WHERE job_type='tweet_process' AND policy_version=?",(VERSION+':source-2',))[0]['job_id']
    assert current!=old and store.get(current)['payload']==record['payload']
    assert service.run(parent)['new_jobs']==0 and counts(store)==(2,2)
    assert record['payload']['post']['id'] not in seen[0] and record['payload']['post']['id'] in seen[1]
    assert store.get(old)==before and store.query('SELECT * FROM web_source_bindings WHERE job_id=?',(old,))[0]==binding_before
    assert WebSources(settings,store).assess(store.get(current))['capture_integrity_passed']
