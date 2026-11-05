"""Owned operator boundaries; no live proof, account, network, or media deletion."""
from copy import deepcopy
from contextlib import contextmanager
import hashlib,importlib.util,json
from pathlib import Path
from types import SimpleNamespace
import pytest

FILE=Path(__file__).resolve().parents[2]/'scripts/ops/cleanup_published_pilot_once_v2.py'
spec=importlib.util.spec_from_file_location('owned_cleanup_pilot_operator',FILE);op=importlib.util.module_from_spec(spec);spec.loader.exec_module(op)

def owned_snapshot():
    state={'test_retention_policy':{'owned_old_hold':True},'human_review':False,'public_submission_approved':False,
        'result_observation':{'external_id':op.BV},'cleanup_coverage':{'owned':'before'}}
    return {'job':{'job_id':op.JOB,'attempts':3,'state':'completed','error_code':None,'payload':{'owned':True},
        'updated_at':1,'stage':'owned_before','result':{'replay_highest_video_web_v2':state}},
        'binding':{'policy_sha256':'owned-policy'},'state':state,'source':{'job_id':op.DOWNLOAD,'owned':True},
        'publication':{'state':'completed','external_id':op.BV,'updated_at':1},'intents':{'owned-intent':'same'},'unknown_result':{'owned':'unchanged'}}

def refreshed(before):
    after=deepcopy(before);after['job'].update(stage='owned_refresh',updated_at=2)
    after['state']['result_observation']={'external_id':op.BV,'owned_fresh':True};after['state']['cleanup_coverage']={'owned':'fresh'}
    after['publication']['updated_at']=2;return after

def test_only_fresh_observation_and_timestamp_may_change_not_original_authorization_or_attempts():
    before=owned_snapshot();op.assert_refresh_preserved(before,refreshed(before))
    for change in ('attempt','old_hold','intent','publication','source'):
        after=refreshed(before)
        if change=='attempt':after['job']['attempts']=0
        elif change=='old_hold':after['state']['test_retention_policy']={}
        elif change=='intent':after['intents']={}
        elif change=='publication':after['publication']['external_id']='BV1unrelated'
        else:after['source']['owned']=False
        with pytest.raises(op.HubError):op.assert_refresh_preserved(before,after)

def test_explicit_apply_needs_saved_plan_and_hash_and_plan_cannot_silently_ignore_apply_inputs():
    op.validate_mode(False,None,None);op.validate_mode(True,'owned-plan','owned-sha')
    for args in ((False,'owned-plan',None),(False,None,'owned-sha'),(True,None,None),(True,'owned-plan',None)):
        with pytest.raises(op.HubError,match='EXACT_PLAN'):op.validate_mode(*args)

def test_fresh_callback_dispatches_only_known_bv_lookup_and_records_without_fake_lease(tmp_path,monkeypatch):
    before=owned_snapshot();current=[deepcopy(before)];calls=[]
    before['source']['result']={'youtube_download_v2':{'binding':{'duration':25715}}};current[0]=deepcopy(before)
    before['state']['attempt_id']=op.ATTEMPT;current[0]['state']['attempt_id']=op.ATTEMPT
    monkeypatch.setattr(op,'snapshot',lambda _:deepcopy(current[0]))
    class Service:
        DOWNLOAD_STATE='youtube_download_v2'
        def __init__(self,*args):pass
        def policy(self):return {'_sha256':'owned-policy'}
        def record_result(self,principal,job,observation,*,lookup):
            calls.append(('record',principal,job,lookup));assert lookup is True
            current[0]=refreshed(current[0])
    monkeypatch.setattr(op,'HighestReplayVideoWeb',Service)
    monkeypatch.setattr(op,'highest_replay_manifest',lambda *a,**k:{'owned_manifest':True,'require_cover':k['require_cover']})
    class Session:
        def operation(self,name,**kwargs):
            calls.append((name,kwargs));assert name=='lookup' and kwargs=={'external_id':op.BV}
            return {'result_observation':{'state':'completed','external_id':op.BV,'public_accessible':True}}
    class Browser:
        @contextmanager
        def session(self,manifest,*,assert_lease):
            assert manifest['require_cover'] is False;assert_lease();yield Session();assert_lease()
    callback=op.fresh_callback(SimpleNamespace(),object(),object(),tmp_path,browser=Browser());callback()
    assert calls[0]==('lookup',{'external_id':op.BV}) and calls[1][0]=='record' and len(calls)==2
    assert (tmp_path/'fresh-lookup.json').is_file()

def test_lookup_guard_rejects_changed_job_before_starting_browser(tmp_path,monkeypatch):
    before=owned_snapshot();current=[before];started=[]
    monkeypatch.setattr(op,'snapshot',lambda _:deepcopy(current[0]))
    monkeypatch.setattr(op,'HighestReplayVideoWeb',lambda *a:SimpleNamespace(policy=lambda:{'_sha256':'owned-policy'}))
    callback=op.fresh_callback(SimpleNamespace(),object(),object(),tmp_path,browser=SimpleNamespace(session=lambda *a,**k:started.append(True)))
    current[0]=deepcopy(before);current[0]['job']['attempts']=4
    with pytest.raises(op.HubError,match='CONTEXT_CHANGED'):callback()
    assert started==[]

def test_original_coverage_is_copied_byte_exact_and_never_rewritten(tmp_path,monkeypatch):
    file=tmp_path/'owned-original.json';raw=b'{"owned_original":true}';file.write_bytes(raw)
    operation=tmp_path/'owned-operation';operation.mkdir();before=owned_snapshot();before['state']['cleanup_coverage']={'evidence':{'file':str(file)}}
    monkeypatch.setattr(op,'ORIGINAL_COVERAGE',op.sha(raw))
    op.preserve_before(tmp_path,before,operation)
    assert file.read_bytes()==raw and (operation/'original-b4c-coverage.json').read_bytes()==raw
    assert json.loads((operation/'state-before.json').read_bytes())==before

def test_kept_sample_hash_mismatch_blocks_cleanup_and_does_not_touch_any_file(tmp_path):
    files=[tmp_path/'owned-part-1.mp4',tmp_path/'owned-part-2.mp4']
    for i,file in enumerate(files):file.write_bytes(b'owned sample hash protocol '+bytes([i]))
    binding={'file':str(tmp_path/'owned-master.mp4'),'file_bytes':100,'artifact_sha256':'a'*64}
    value={'version':'real-source-private-multipart-samples-v4','source_id':op.SOURCE,'source_path':binding['file'],
        'source_bytes':100,'source_sha256':'a'*64,'method':'ffmpeg_stream_copy','private_upload_only':True,
        'public_submission_allowed':False,'formal_splitter_acceptance_claimed':False,
        'parts':[{'path':str(p),'bytes':p.stat().st_size,'sha256':op.sha(p.read_bytes())} for p in files]}
    manifest=tmp_path/'owned-samples.json';raw=json.dumps(value).encode();manifest.write_bytes(raw)
    result=op.retained_samples(tmp_path,binding,file=manifest,checksum=op.sha(raw));assert len(result['parts'])==2
    files[1].write_bytes(files[1].read_bytes()+b'changed')
    with pytest.raises(op.HubError,match='SAMPLES_REQUIRED'):op.retained_samples(tmp_path,binding,file=manifest,checksum=op.sha(raw))
    assert all(p.exists() for p in files) and manifest.read_bytes()==raw

def test_passed_boolean_without_real_frozen_junit_cannot_enable_operator(tmp_path,monkeypatch):
    junit=tmp_path/'owned-empty.xml';junit.write_bytes(b'<testsuites/>')
    proof=tmp_path/'owned-false-attestation.json';raw=json.dumps({'full_suite_passed':True,'junit_file':str(junit)}).encode();proof.write_bytes(raw)
    monkeypatch.setattr(op,'_root_protected',lambda _:None)  # Owned root-FS seam only.
    with pytest.raises(op.HubError,match='QUALIFICATION_REQUIRED'):op.qualify(tmp_path,file=proof,checksum=op.sha(raw))
