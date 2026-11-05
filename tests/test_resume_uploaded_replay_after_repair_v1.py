"""Real SQLite/receipts/source scope; owned prior-upload evidence, no browser."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import threading
import uuid

import pytest

from test_short_video_web import short_video, PRINCIPAL
from test_replay_highest_video_v2 import highest, persist_source, ref, put, UID
from services.hub.db import Store, encode
from services.hub.errors import HubError
from services.hub.worker import Worker
from services.hub.replay_moderation_common import QueueView
from services.hub.replay_highest_video_v2 import STATE, DOWNLOAD_STATE
from services.hub.replay_highest_coverage_v2 import expected_binding
from services.hub.youtube_download import digest

SOURCE='d28685f1-03fc-4a12-9d85-5dbf14107f82'
JOB='594aed06-3aa1-45f4-90f0-1db49a602395'
ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture
def repair(highest, monkeypatch):
    f=highest;db=f['db'];service=f['service'];old=f['source'];source=db.get(old)
    file=f['s'].root/'media'/SOURCE/'youtube-highest-v2/original.mp4';file.parent.mkdir(parents=True)
    shutil.copyfile(f['file'],file);cp=source['result'][DOWNLOAD_STATE]
    cp['binding']['job_id']=SOURCE;cp['source_asset']['file']=str(file)
    with db.transaction() as con:con.execute('UPDATE jobs SET job_id=?,result=? WHERE job_id=?',(SOURCE,encode(source['result']),old))
    f.update(source=SOURCE,file=file);persist_source(f)
    created=service.create_candidate(PRINCIPAL,SOURCE,f['metadata'])
    with db.transaction() as con:con.execute('UPDATE jobs SET job_id=? WHERE job_id=?',(JOB,created))
    service.record_automation_authorization(PRINCIPAL,JOB,actual_owner_workflow_response='Owned original workflow authorization, not a real user',
        owner_response_reference='Owned original reference',workflow_authorization_reference=service.policy()['workflow_authorization_reference'])
    attempt=str(uuid.uuid4());service.start_upload(PRINCIPAL,JOB,attempt_id=attempt,site_uid=UID)
    job=db.get(JOB);binding=job['payload'];state=job['result'][STATE];expected=expected_binding(job,binding,state)
    folder=f['s'].root/'media'/JOB/'replay-native-intents';folder.mkdir(parents=True)
    selected=folder/'video-selection.json';intent=put(selected,{'version':'replay-native-one-use-intent-v1',**expected,'kind':'video-selection','created_at':datetime.now(timezone.utc).isoformat()})
    folder=f['s'].root/'media'/JOB/'replay-browser-highest-v2'/attempt;folder.mkdir(parents=True)
    coverage=put(folder/'upload-coverage.json',{'version':'replay-highest-native-upload-coverage-v2',**expected,
        'file':str(file),'selected_filename':'original.mp4','native_filename':{'text':'original.mp4','kind':'file_input','observed':True},
        'native_upload_complete':True,'whole_file_streamed':True,'streamed_bytes':binding['file_bytes'],'streamed_sha256':binding['artifact_sha256'],
        'observed_at':datetime.now(timezone.utc).isoformat(),'server_file_hash_verified':False,'human_review':False})
    observation={'version':'short-video-browser-upload-v1','attempt_id':attempt,'site_uid':UID,'artifact_sha256':binding['artifact_sha256'],
        'file_bytes':binding['file_bytes'],'upload_reference_sha256':digest({**expected,'local_causal_intent':intent,
            'native_upload_complete':True,'upload_coverage':coverage}), 'same_attempt_observed':True,'server_file_hash_verified':False,'upload_coverage':coverage}
    service.record_upload(PRINCIPAL,JOB,observation)
    with db.transaction() as con:con.execute("UPDATE jobs SET state='awaiting_review',attempts=2,error_code='REPLAY_BROWSER_METADATA_PREPARATION_FAILED' WHERE job_id=?",(JOB,))
    # Register original values with pytest undo before executing the exact APP
    # fences; the child production program runs in its own process.
    for cls,names in ((Store,('claim','recover','enqueue','finish')),(Worker,('once','run')),(QueueView,('claim','recover')),(threading.Thread,('start',)),(socket.socket,('connect',))):
        for name in names:monkeypatch.setattr(cls,name,getattr(cls,name))
    spec=importlib.util.spec_from_file_location('owned_resume_module',ROOT/'scripts/ops/resume_uploaded_replay_after_repair_v1.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);namespace={};exec(module.APP,namespace)
    before=db.get(JOB);request={'job_id':JOB,'before_row_sha256':namespace['row_sha'](before),
        'intents_sha256':{'video-selection.json':intent['sha256']},
        'repair_reference':{'deployment_report':str(f['s'].root/'deployment-report.json'),'sha256':'a'*64,
            'repair_attestation':{'file':str(f['s'].root/'test-attestation.json'),'sha256':'b'*64,'tests':68}}}
    f.update(app=namespace['resume_existing'],before=before,request=request,attempt=attempt,coverage=Path(coverage['file']),intent=selected)
    return f


def test_default_plan_writes_no_job_or_audit_and_explicit_resume_preserves_attempt_and_authorization(repair):
    f=repair;db=f['db'];audit=db.query('SELECT * FROM audit');original=f['file'].read_bytes()
    result=f['app'](f['s'],db,f['request'])
    assert result['resumed'] is False and db.get(JOB)==f['before'] and db.query('SELECT * FROM audit')==audit
    result=f['app'](f['s'],db,f['request'],apply=True);after=db.get(JOB)
    assert result['resumed'] is True and after['state']=='pending' and after['attempts']==2
    assert after['result']==f['before']['result'] and after['payload']==f['before']['payload'] and f['file'].read_bytes()==original
    assert after['result'][STATE]['attempt_id']==f['attempt'] and after['result'][STATE]['public_submission_approved'] is False
    events=db.query("SELECT principal,action,outcome FROM audit WHERE action LIKE 'replay_uploaded_repair_%'")
    assert [e['action'] for e in events]==['replay_uploaded_repair_resume_requested','replay_uploaded_repair_resumed']
    assert all(e['principal']==PRINCIPAL for e in events) and json.loads(events[-1]['outcome'])['repair_reference']==f['request']['repair_reference']
    with pytest.raises(AssertionError):f['app'](f['s'],db,f['request'],apply=True)
    assert db.get(JOB)['attempts']==2 and not db.query('SELECT * FROM publications')


@pytest.mark.parametrize('change',['attempt','unknown','lease','authorization','receipt','public_intent','missing_selection','source','policy','publication','repair_reference'])
def test_changed_or_unknown_evidence_never_enqueues_or_resets(repair,change):
    f=repair;db=f['db'];job=db.get(JOB)
    if change in ('attempt','unknown','lease'):
        with db.transaction() as con:
            if change=='attempt':con.execute('UPDATE jobs SET attempts=3 WHERE job_id=?',(JOB,))
            elif change=='lease':con.execute('UPDATE jobs SET lease_until=9999999999 WHERE job_id=?',(JOB,))
            else:con.execute("UPDATE jobs SET state='publish_unknown' WHERE job_id=?",(JOB,))
    elif change=='authorization':
        job['result'][STATE]['automation_authorization']['human_review']=True
        with db.transaction() as con:con.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(job['result']),JOB))
    elif change=='receipt':f['coverage'].write_bytes(f['coverage'].read_bytes()+b' ')
    elif change=='public_intent':f['intent'].with_name('public-submit.json').write_text('{}')
    elif change=='missing_selection':f['intent'].unlink()
    elif change=='source':
        with db.transaction() as con:con.execute("UPDATE jobs SET state='failed' WHERE job_id=?",(SOURCE,))
    elif change=='publication':
        with db.transaction() as con:con.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('a'*64,JOB,'bilibili','youtube',job['source_id'],'site_uid:'+UID,'owned-other-kind','owner_deleted','BV0000000000','https://www.bilibili.com/video/BV0000000000',0))
    elif change=='repair_reference':f['request']['repair_reference']['repair_attestation']['tests']=21
    else:f['policy_file'].write_bytes(f['policy_file'].read_bytes()+b'\n# changed owned policy\n')
    # Bind a fresh maintenance read to changed state; the business guard must
    # still reject, not only the outer optimistic-row hash.
    current=db.get(JOB);f['request']['before_row_sha256']=hashlib.sha256((json.dumps(current,sort_keys=True,separators=(',',':'))+'\n').encode()).hexdigest()
    with pytest.raises((AssertionError,HubError,OSError)):f['app'](f['s'],db,f['request'],apply=True)
    assert db.get(JOB)==current
    assert len(db.query('SELECT * FROM publications'))==(1 if change=='publication' else 0)
    assert not db.query("SELECT * FROM audit WHERE action='replay_uploaded_repair_resumed'")


def test_existing_checkpoint_cas_refuses_concurrent_change_after_audited_resume_request(repair,monkeypatch):
    from services.hub.replay_highest_video_v2 import HighestReplayVideoWeb
    f=repair;actual=HighestReplayVideoWeb._validate_snapshot_binding;calls=[0]
    def change_after_validation(self,*args,**kwargs):
        value=actual(self,*args,**kwargs);calls[0]+=1
        if calls[0]==2:
            with f['db'].transaction() as con:con.execute('UPDATE jobs SET updated_at=updated_at+1 WHERE job_id=?',(JOB,))
        return value
    monkeypatch.setattr(HighestReplayVideoWeb,'_validate_snapshot_binding',change_after_validation)
    with pytest.raises(HubError,match='JOB_CHANGED'):f['app'](f['s'],f['db'],f['request'],apply=True)
    after=f['db'].get(JOB);assert after['state']=='awaiting_review' and after['attempts']==2 and after['result']==f['before']['result']
    assert len(f['db'].query("SELECT * FROM audit WHERE action='replay_uploaded_repair_resume_requested'"))==1
    assert not f['db'].query("SELECT * FROM audit WHERE action='replay_uploaded_repair_resumed'")
