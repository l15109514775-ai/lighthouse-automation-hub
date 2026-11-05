"""The second evidence rule uses measured connectors and keeps day/full times separate."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import time
import tomllib
import uuid
import pytest
from services.hub.config import Settings
from services.hub.db import Store,encode
from services.hub.errors import HubError
from services.hub.evidence import sha256_file
from services.hub.reply_evidence import ReplyEvidence
from services.hub.reply_evidence_v2 import ReplyEvidenceV2,SOURCE_VERSION


@pytest.fixture(scope='module')
def collected(tmp_path_factory):
    root=Path(__file__).resolve().parents[1];base=tmp_path_factory.mktemp('reply-v2-browser')
    settings=Settings(base,tomllib.loads((root/'config/hub.toml').read_text('utf-8')));settings.prepare()
    store=Store(settings.db_path);store.init()
    ident=store.enqueue('scheduler','owned-reply-v2-browser','x_discovery','tweet',{},'reply-v2-prototype')
    job=store.claim('tweet',600,4)
    output=settings.root/'media'/ident/'reply-machine-v2'
    proc=subprocess.run(['node',str(root/'tests/reply_machine_v2_browser_fixture.mjs'),str(output)],cwd=root,
        env={**os.environ,'PLAYWRIGHT_BROWSERS_PATH':str(root/'compositor/runtime/browsers'),'TEMP':str(base),'TMP':str(base),'TMPDIR':str(base)},
        capture_output=True,timeout=100)
    assert proc.returncode==0,proc.stderr.decode('utf-8','replace')
    summary=json.loads(proc.stdout);packet=json.loads(Path(summary['packet_file']).read_text('utf-8'))
    service=ReplyEvidenceV2(settings,store);service.initialize()
    return settings,store,job,packet,service,summary


def negative_observation(packet,key,mutate):
    old=Path(packet[key]['file']);value=json.loads(old.read_text('utf-8'));mutate(value)
    file=old.with_name('negative-'+str(uuid.uuid4())+'.json');file.write_text(encode(value),'utf-8')
    packet[key]={'file':str(file),'sha256':sha256_file(file)}


def test_real_browser_fixture_and_service_accept_source_only_rule_with_precise_time_attribution(collected):
    settings,store,job,packet,service,summary=collected
    assert len(summary['passed'])==10 and summary['external_calls']==0
    checked=service.validate_packet(job['job_id'],packet)
    assert checked['version']==SOURCE_VERSION and checked['machine_relationship_supported']
    assert checked['thread_parent_source']['post']['timestamp_precision']=='day'
    assert 'created_at' not in checked['thread_parent_source']['post']
    assert checked['canonical_parent_time']=={'value':'2026-10-31T12:00:00+08:00','source':'independent_parent_permalink',
        'thread_time_precision':'day','thread_visible_day':{'year':None,'month':10,'day':31,'precision':'day','visible_text':'10月31日'},'same_day_verified':True}
    assert checked['thread_parent_observation_sha256']!=checked['independent_parent_observation_sha256']
    assert not checked['human_review'] and not checked['official_api'] and not checked['automatic_publication_eligible']
    saved=service.record(job,packet)
    assert ReplyEvidenceV2(settings,Store(settings.db_path)).verify(saved['receipt_id'])['validated_source']==checked
    inputs=service.model_inputs(saved['receipt_id'])
    assert inputs['parent']['post']['created_at']=='2026-10-31T12:00:00+08:00'
    assert inputs['reply']['reply_context']['parent_time_basis']['source']=='independent_parent_permalink'
    assert not store.query('SELECT * FROM api_calls') and not store.query('SELECT * FROM publications')


def test_v1_label_rule_is_not_relaxed_by_v2_without_label(collected):
    settings,store,job,packet,service,summary=collected
    with pytest.raises(HubError):ReplyEvidence(settings,store).validate_packet(job['job_id'],packet)
    assert packet['thread']['reply_to_label'] is None


@pytest.mark.parametrize('change',[
    lambda p:p['thread']['topology'].update(connector_candidate_count=0),
    lambda p:p['thread']['topology'].update(connector_candidate_count=2),
    lambda p:p['thread']['topology']['connector'].update(background_color='rgba(255, 255, 255, 1)'),
    lambda p:p['thread']['topology']['connector'].update(background_color='rgb(255, 0, 0)'),
    lambda p:p['thread']['topology']['connector']['rect'].update(x=999),
    lambda p:p['thread']['topology']['connector']['rect'].update(height=10),
    lambda p:p['thread']['topology']['connector'].update(inside_body=True),
    lambda p:p['thread']['topology']['connector']['hit_points'][0].update(visible=False),
    lambda p:p['thread']['topology'].update(items_directly_adjacent=False),
    lambda p:p['thread']['topology']['list'].update(article_ids=['extra','1000000000000000001','1000000000000000002']),
    lambda p:p['thread']['topology']['parent_item'].update(article_ids=['1000000000000000001','extra']),
    lambda p:p['thread'].update(collapsed_context_detected=True),
    lambda p:p['thread'].update(reply_to_label={'target_handles':['other'],'visible':True}),
    lambda p:p.update(direct_parent_independently_verified=True),
])
def test_wrong_line_multiple_parents_and_obscured_or_contradictory_relation_fail(collected,change):
    settings,store,job,packet,service,summary=collected
    mutated=deepcopy(packet);change(mutated)
    with pytest.raises(HubError):service.validate_packet(job['job_id'],mutated)


def test_full_parent_clock_cannot_be_written_back_into_thread_day_observation(collected):
    settings,store,job,packet,service,summary=collected;mutated=deepcopy(packet)
    negative_observation(mutated,'parent_in_thread_day',lambda v:v['payload']['post'].update(created_at='2026-10-31T12:00:00+08:00'))
    with pytest.raises(HubError,match='REPLY_V2_DAY_PARENT_OBSERVATION_INVALID'):service.validate_packet(job['job_id'],mutated)


def test_wrong_parent_day_is_checked_against_independent_permalink_full_time(collected):
    settings,store,job,packet,service,summary=collected;mutated=deepcopy(packet)
    def change(v):
        day={'year':None,'month':10,'day':30,'precision':'day','visible_text':'10月30日'}
        v['payload']['post']['published_day']=day;v['facts']['observed_published_day']=deepcopy(day);v['facts']['visible_timestamp']='10月30日'
    negative_observation(mutated,'parent_in_thread_day',change)
    with pytest.raises(HubError,match='REPLY_V2_PARENT_VISIBLE_DAY_MISMATCH'):service.validate_packet(job['job_id'],mutated)


def test_changed_full_parent_text_or_timestamp_does_not_match_thread_source(collected):
    settings,store,job,packet,service,summary=collected;mutated=deepcopy(packet)
    def change(v):v['payload']['post']['text']='Changed parent';v['facts']['observed_text']='Changed parent'
    negative_observation(mutated,'parent_reread',change)
    with pytest.raises(HubError):service.validate_packet(job['job_id'],mutated)


def test_extended_reply_record_binds_two_exact_model_sources_with_raw_day_provenance(collected):
    from services.hub.extended_web_source import ExtendedWebSources
    settings,store,parent,packet,service,summary=collected
    record=json.loads(Path(summary['extended_record_file']).read_text('utf-8'))
    extended=ExtendedWebSources(settings,store)
    queued=extended.ingest(parent,record,'machine-tweet-v2');job=store.get(queued['job_id'])
    proof=extended.assess(job);nodes=proof['nodes']
    assert [n['role'] for n in nodes]==['post','parent']
    assert nodes[0]['source']==job['payload']
    assert nodes[0]['source']['reply_context']['parent_source']==nodes[1]['source']
    assert nodes[0]['source']['capture']['scheduled_server_collection'] is True
    assert nodes[0]['source']['capture']['orchestration_trigger']=='durable_scheduler'
    assert nodes[0]['source']['capture']['collector_trigger']=='machine_reply_v2_prototype'
    assert proof['proof']['reply_proof']['thread_parent_source']['post']['timestamp_precision']=='day'
    assert 'created_at' not in proof['proof']['reply_proof']['thread_parent_source']['post']
    assert not store.query('SELECT * FROM api_calls') and not store.query('SELECT * FROM publications')
