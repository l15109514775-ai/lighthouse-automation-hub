"""Actual offline Node validation plus transactional admission fault injection."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import time
import pytest
from services.hub.db import Store,encode
from services.hub.errors import HubError
from services.hub.extended_web_source import ExtendedWebSources,VERSION
from services.hub.worker import Worker


@pytest.fixture
def extended(runtime,tmp_path):
    settings,store=runtime;root=Path(__file__).resolve().parents[1]
    ident=store.enqueue('scheduler','owned-extended-discovery','x_discovery','tweet',{},'machine-tweet-v2')
    parent=store.claim('tweet',600,4)
    output=settings.root/'media'/ident/'extended-browser'
    result=subprocess.run(['node',str(root/'tests/extended_source_fixture.mjs'),str(output)],cwd=root,
        env={**os.environ,'PLAYWRIGHT_BROWSERS_PATH':str(root/'compositor/runtime/browsers'),'TEMP':str(tmp_path),'TMP':str(tmp_path),'TMPDIR':str(tmp_path)},
        capture_output=True,timeout=35)
    assert result.returncode==0,result.stderr.decode('utf-8','replace')
    summary=json.loads(result.stdout);record=json.loads(Path(summary['record_file']).read_text('utf-8'))
    return settings,store,parent,record,ExtendedWebSources(settings,store),summary


def test_real_capture_and_atomic_binding_yield_stable_proof_and_exact_model_source(extended):
    settings,store,parent,record,service,summary=extended
    assert summary['external_calls']==0 and summary['raw_text']=='配信 youtube.com/watch…\n20:00'
    assert summary['normalized_text']=='配信 https://www.youtube.com/watch?v=owned_fixture\n20:00'
    assert record['version']==VERSION and record['payload']['capture']['protocol']==VERSION
    assert record['payload']['source_api']=='x-web-dom-v1' and record['payload']['capture']['human_review'] is False
    inserted=service.ingest(parent,record,'machine-tweet-v2')
    job=store.get(inserted['job_id']);first=service.assess(job);second=ExtendedWebSources(settings,Store(settings.db_path)).assess(job)
    assert first==second and first['proof']['capture_integrity_passed']
    assert first['nodes'][0]['source']==job['payload'] and first['nodes'][0]['media']==[]
    assert first['nodes'][0]['provenance'] and first['proof']['automatic_publication_eligible'] is False
    repeated=service.ingest(parent,record,'machine-tweet-v2')
    assert repeated['job_id']==inserted['job_id'] and not repeated['created']
    assert len(store.query("SELECT * FROM jobs WHERE job_type='tweet_process'"))==1
    assert not store.query('SELECT * FROM api_calls') and not store.query('SELECT * FROM publications')


def test_link_source_or_actual_anchor_proof_cannot_be_replaced_by_model_style_pass_flags(extended):
    settings,store,parent,record,service,summary=extended
    record['machine_extended_source']['nodes'][0]['link_normalization']['observation']['anchors'][0]['actual_href']='https://evil.example.test'
    record['machine_extended_source']['nodes'][0]['passed']=True
    with pytest.raises(HubError):service.ingest(parent,record,'machine-tweet-v2')
    assert not store.query('SELECT * FROM extended_web_source_bindings')
    assert not store.query("SELECT * FROM jobs WHERE job_type='tweet_process'")


def test_capture_file_failure_does_not_expose_a_processing_child(extended,monkeypatch):
    from services.hub.web_source import WebSources
    settings,store,parent,record,service,summary=extended
    original=WebSources._write_durable
    def fail(*a,**kw):raise OSError('owned disk failure')
    monkeypatch.setattr(WebSources,'_write_durable',staticmethod(fail))
    with pytest.raises(HubError,match='EXTENDED_SOURCE_FILES_NOT_DURABLE'):service.ingest(parent,record,'machine-tweet-v2')
    assert not store.query('SELECT * FROM extended_web_source_bindings') and not store.query("SELECT * FROM jobs WHERE job_type='tweet_process'")
    monkeypatch.setattr(WebSources,'_write_durable',staticmethod(original))
    assert service.ingest(parent,record,'machine-tweet-v2')['created']


def test_database_binding_interruption_rolls_back_child_and_recovery_retries_cleanly(extended,monkeypatch):
    settings,store,parent,record,service,summary=extended
    original=store.connect
    class Interrupted(BaseException):pass
    class DB:
        def __init__(self,inner):self.inner=inner
        def __getattr__(self,key):return getattr(self.inner,key)
        def execute(self,sql,args=()):
            if sql.startswith('INSERT INTO extended_web_source_bindings'):raise Interrupted()
            return self.inner.execute(sql,args)
    monkeypatch.setattr(store,'connect',lambda:DB(original()))
    with pytest.raises(Interrupted):service.ingest(parent,record,'machine-tweet-v2')
    assert not store.query('SELECT * FROM extended_web_source_bindings') and not store.query("SELECT * FROM jobs WHERE job_type='tweet_process'")
    monkeypatch.setattr(store,'connect',original)
    assert service.ingest(parent,record,'machine-tweet-v2')['created']


def test_cleared_sources_are_not_resurrected_in_extended_policy(extended):
    settings,store,parent,record,service,summary=extended
    with store.transaction() as db:db.execute('INSERT INTO source_tombstones VALUES(?,?,?,?,?,?)',
        ('x',record['payload']['post']['id'],'OWNER_BACKLOG_CLEARED',time.time(),parent['job_id'],'owner_clear_all'))
    with pytest.raises(HubError,match='OWNER_BACKLOG_CLEARED'):service.ingest(parent,record,'machine-tweet-v2')
    assert not store.query('SELECT * FROM extended_web_source_bindings') and not store.query("SELECT * FROM jobs WHERE job_type='tweet_process'")


def test_changed_screenshot_or_database_payload_invalidates_bound_source(extended):
    settings,store,parent,record,service,summary=extended
    ident=service.ingest(parent,record,'machine-tweet-v2')['job_id'];job=store.get(ident)
    Path(record['screenshot_file']).write_bytes(b'changed source screenshot')
    with pytest.raises(HubError):service.assess(job)
    changed=deepcopy(job);changed['payload']['post']['text']='unbound replacement'
    with pytest.raises(HubError,match='EXTENDED_SOURCE_JOB_CHANGED'):service.assess(changed)


def test_operator_cannot_claim_scheduler_orchestration_in_derived_source(extended):
    settings,store,parent,record,service,summary=extended
    with store.transaction() as db:db.execute("UPDATE jobs SET source_platform='server_acceptance' WHERE job_id=?",(parent['job_id'],))
    record['machine_extended_source']['collector_trigger']='durable_scheduler'
    for source in (record['payload'],record['machine_extended_source']['nodes'][0]['source']):
        source['capture']['orchestration_trigger']='durable_scheduler';source['capture']['scheduled_server_collection']=True
    with pytest.raises(HubError,match='EXTENDED_SOURCE_SCHEDULER_ORIGIN_INVALID'):
        service.ingest(parent,record,'machine-tweet-v2')
    assert not store.query('SELECT * FROM extended_web_source_bindings')


def test_raw_helper_trigger_is_preserved_separately_from_orchestration(extended):
    settings,store,parent,record,service,summary=extended
    node=record['machine_extended_source']['nodes'][0]
    assert node['raw_source']['capture']['collector_trigger']=='extended_public_dom'
    assert node['raw_source']['capture']['scheduled_server_collection'] is False
    assert node['source']['capture']['collector_trigger']=='extended_public_dom'
    assert node['source']['capture']['orchestration_trigger']=='durable_job'
    assert node['source']['capture']['scheduled_server_collection'] is False


def test_current_dom_inventory_must_match_the_raw_capture_hash_not_just_pass_flags(extended):
    settings,store,parent,record,service,summary=extended
    record['machine_extended_source']['nodes'][0]['dom_facts']['ui_classification_version']='not_the_captured_rule'
    with pytest.raises(HubError):service.ingest(parent,record,'machine-tweet-v2')
    assert not store.query('SELECT * FROM extended_web_source_bindings')
