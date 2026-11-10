"""Real SQLite recovery boundaries; no network, profile, authorization or media fixtures."""
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import pytest
from services.hub.db import Store,encode

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('recovery',ROOT/'scripts/ops/recover_private_upload.py')
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
JOB='00000000-0000-4000-8000-000000000001';ATTEMPT='00000000-0000-4000-8000-000000000002'

def fixture(root):
    root.mkdir(exist_ok=True);store=Store(root/'hub.db')
    with store.transaction() as db:
        db.executescript('CREATE TABLE jobs(job_id TEXT PRIMARY KEY,state TEXT,stage TEXT,error_code TEXT,attempts INTEGER,lease_owner TEXT,lease_until REAL,external_publish_id TEXT,payload TEXT,result TEXT,source_id TEXT,updated_at REAL); CREATE TABLE publications(source_platform TEXT,source_id TEXT); CREATE TABLE audit(at REAL,principal TEXT,action TEXT,target TEXT,outcome TEXT);')
        state={'state':'upload_unknown','attempt_id':ATTEMPT,'private_upload_selections':1,'human_review':False,'public_submission_approved':False,'key':'same-key'}
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(JOB,'awaiting_review','short_video_reconcile_required','REPLAY_BROWSER_NATIVE_OPERATION_FAILED',1,None,None,None,encode({'immutable':'payload'}),encode({r.STATE:state,'publish_performed':False}),'owned-source',10))
        db.execute('INSERT INTO audit VALUES(?,?,?,?,?)',(1,'worker','short_video_checkpoint',JOB,'uploading'))
    area=root/'media'/JOB;intents=area/'replay-native-intents';intents.mkdir(parents=True)
    (intents/'video-selection.json').write_text(encode({'kind':'video-selection','job_id':JOB,'attempt_id':ATTEMPT}))
    native=area/'replay-browser-part-v5'/ATTEMPT;native.mkdir(parents=True)
    (native/'last-error.json').write_text(encode({'action':'upload','error_code':'REPLAY_BROWSER_NATIVE_OPERATION_FAILED','publication_result_unknown':False,'video_selection_attempted_in_session':True}))
    def snapshot(job_id,**kwargs):
        job=store.get(job_id);return job,job['payload'],job['result'][r.STATE]
    service=SimpleNamespace(settings=SimpleNamespace(root=root),store=store,snapshot=snapshot,
        _operator=lambda principal:None,_validate_snapshot_binding=lambda *a,**kw:None,
        _require_workflow_launch=lambda policy:None,policy=lambda:{},
        _native_base=lambda job,binding,state:{'job_id':job['job_id'],'attempt_id':state['attempt_id']})
    return service,intents

def plan(service):return r.recover(service,JOB,'Test fixture: confirmed pre-submit process failure')
def apply(service,checksum):return r.recover(service,JOB,'Test fixture: confirmed pre-submit process failure',apply_plan_sha256=checksum)

def test_plan_then_one_retry_preserves_attempts_payload_key_and_evidence(tmp_path):
    service,intents=fixture(tmp_path);before=service.store.get(JOB);original=(intents/'video-selection.json').read_bytes()
    p=plan(service);assert service.store.get(JOB)==before and (intents/'video-selection.json').read_bytes()==original
    result=apply(service,p['plan_sha256']);after=service.store.get(JOB);state=after['result'][r.STATE]
    assert after['state']=='awaiting_review' and after['stage']=='replay_part_private_retry_prepared' and after['attempts']==before['attempts']==1 and after['payload']==before['payload']
    assert state['attempt_id']!=ATTEMPT and state['key']=='same-key' and state[r.MARKER]['previous_checkpoint']==before['result'][r.STATE]
    assert state[r.MARKER]['old_private_blob_unknown'] and state['human_review'] is False
    assert (Path(result['journal'])/'consumed-selection.json').read_bytes()==original and not any(intents.iterdir())
    assert not service.store.query('SELECT * FROM publications')
    with pytest.raises(ValueError):apply(service,p['plan_sha256'])
    with pytest.raises(ValueError,match='ROLLBACK_ROW_CHANGED'):r.restore_interrupted_journal(service,JOB,p['plan_sha256'])

@pytest.mark.parametrize('kind',['publication','submit_intent','draft_intent','lease','completed','prior_recovery','audit_submit','mismatched_selection'])
def test_rejects_any_submission_uncertainty_or_repeated_recovery(tmp_path,kind):
    service,intents=fixture(tmp_path)
    with service.store.transaction() as db:
        if kind=='publication':db.execute('INSERT INTO publications VALUES(?,?)',('youtube','owned-source'))
        elif kind=='lease':db.execute("UPDATE jobs SET lease_owner='live',lease_until=9999999999")
        elif kind=='completed':db.execute("UPDATE jobs SET state='completed'")
        elif kind=='audit_submit':db.execute('INSERT INTO audit VALUES(?,?,?,?,?)',(2,'worker','reserve_submit',JOB,'submitting'))
        elif kind=='prior_recovery':
            result=service.store.get(JOB)['result'];result[r.STATE][r.MARKER]={};db.execute('UPDATE jobs SET result=?',(encode(result),))
    if kind in ('submit_intent','draft_intent'):(intents/('public-submit.json' if kind=='submit_intent' else 'private-draft-save.json')).write_text('{}')
    if kind=='mismatched_selection':(intents/'video-selection.json').write_text(encode({'kind':'video-selection','job_id':'foreign','attempt_id':ATTEMPT}))
    before=service.store.get(JOB)
    with pytest.raises(ValueError):plan(service)
    assert service.store.get(JOB)==before

def test_changed_plan_and_symlink_are_rejected(tmp_path):
    service,intents=fixture(tmp_path);p=plan(service)
    with service.store.transaction() as db:db.execute('UPDATE jobs SET updated_at=11')
    with pytest.raises(ValueError,match='PLAN_CHANGED'):apply(service,p['plan_sha256'])
    if os.name!='posix':return
    file=intents/'video-selection.json';raw=file.read_bytes();file.unlink();other=tmp_path/'foreign';other.write_bytes(raw);file.symlink_to(other)
    with pytest.raises(ValueError,match='PATH_INVALID'):plan(service)

@pytest.mark.skipif(os.name!='posix',reason='Actual process-kill/WAL recovery exercised in Linux Docker')
def test_process_killed_after_intent_archive_keeps_job_unknown_and_can_restore(tmp_path):
    import multiprocessing,signal
    service,intents=fixture(tmp_path);before=service.store.get(JOB);p=plan(service)
    def crash():
        rename=r.os.rename
        def kill_after_rename(src,dst):
            rename(src,dst);os.kill(os.getpid(),signal.SIGKILL)
        r.os.rename=kill_after_rename
        apply(service,p['plan_sha256'])
    child=multiprocessing.get_context('fork').Process(target=crash);child.start();child.join(10)
    if child.is_alive():child.kill();child.join();pytest.fail('Recovery child failed to terminate')
    assert child.exitcode==-signal.SIGKILL and service.store.get(JOB)==before and not any(intents.iterdir())
    outcome=r.restore_interrupted_journal(service,JOB,p['plan_sha256'])
    assert outcome['requeued'] is False and service.store.get(JOB)==before and (intents/'video-selection.json').exists()

def test_new_attempt_cover_passes_real_machine_cover_verifier_before_release(tmp_path,monkeypatch):
    import hashlib
    import httpx
    from test_cover_normalizer_v4 import make_jpeg
    from services.hub.cover_normalizer_v4 import normalize_cover
    from services.hub.replay_machine_checks import ReplayMachineChecks
    service,intents=fixture(tmp_path);service.settings.config={'max_attempts':4}
    with service.store.transaction() as db:db.execute('UPDATE jobs SET payload=?',(encode({'source_id':'abcdefghijk'}),))
    old=tmp_path/'media'/JOB/'short-video-attempts'/ATTEMPT/'cover-source-official-maxres.jpg';old.parent.mkdir(parents=True);make_jpeg(old)
    normalize_cover(tmp_path,JOB,'abcdefghijk',str(old),hashlib.sha256(old.read_bytes()).hexdigest(),'https://i.ytimg.com/vi/abcdefghijk/maxresdefault.jpg')
    provenance=tmp_path/'media'/JOB/'cover-assets-v4/provenance.json';original=provenance.read_bytes()
    p=plan(service);result=apply(service,p['plan_sha256']);after=service.store.get(JOB)
    monkeypatch.setattr(httpx,'Client',lambda **kwargs:pytest.fail('Cached cover recovery must not issue HTTP'))
    verified=[]
    def verify(job_id):
        value=ReplayMachineChecks._cover(None,tmp_path,JOB,'abcdefghijk',None,'ffmpeg',lambda:None,attempt_id=result['new_attempt_id']);verified.append(value);return value
    service.verify_for_publish=verify
    report=r.prepare_cover_and_verify(service,JOB)
    assert report['state']=='cover_and_machine_qa_verified' and service.store.get(JOB)==after and len(verified)==1
    assert (Path(result['journal'])/'prior-cover-assets/provenance.json').read_bytes()==original
    assert json.loads(provenance.read_bytes())['input']['file']==str(tmp_path/'media'/JOB/'short-video-attempts'/result['new_attempt_id']/'cover-source-official-maxres.jpg')
    def denied(job_id):raise ValueError('QA_REJECTED')
    service.verify_for_publish=denied
    with pytest.raises(ValueError,match='QA_REJECTED'):r.prepare_cover_and_verify(service,JOB,release_plan_sha256=p['plan_sha256'])
    assert service.store.get(JOB)==after
