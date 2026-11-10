"""Operator-only, one bounded retry of a failed *pre-submit* V5 upload.

This does not infer that a private remote blob is absent. Public reservations,
draft-save/submit intents, live leases and prior recovery all prohibit recovery.
The normal worker still performs full source/media/authorization checks. A plan
is read-only; applying it archives the consumed selection intent and all old
state, keeps jobs.attempts/idempotency/payload, and opens one new native attempt.
Run with the existing video/profile/resource locks; no network operation occurs.
"""
from contextlib import contextmanager
from copy import deepcopy
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

STATE='replay_part_video_v5'
MARKER='bounded_pre_submit_recovery'

def need(value,code):
    if not value:raise ValueError(code)

def wire(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
def digest(value):return hashlib.sha256(wire(value)).hexdigest()

def safe(path):
    path=Path(path)
    need(path.is_absolute() and path.resolve()==path and not any(p.is_symlink() for p in (path,*path.parents)),'RECOVERY_PATH_INVALID')
    return path

def read(path):
    path=safe(path);st=path.stat();need(path.is_file() and 0<st.st_size<=2_000_000,'RECOVERY_FILE_INVALID')
    raw=path.read_bytes();need(len(raw)==st.st_size and path.stat().st_mtime_ns==st.st_mtime_ns,'RECOVERY_FILE_CHANGED')
    return raw

def sync(folder):
    if os.name=='posix':
        fd=os.open(folder,os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(fd)
        finally:os.close(fd)

def write(path,raw):
    safe(path)
    with path.open('xb') as f:
        os.chmod(path,0o600);f.write(raw);f.flush();os.fsync(f.fileno())
    sync(path.parent)

def inspect(service,job_id,operator_reference):
    from services.hub.short_video_web import PRINCIPAL
    service._operator(PRINCIPAL)
    need(str(uuid.UUID(job_id))==job_id,'RECOVERY_JOB_ID_INVALID')
    need(isinstance(operator_reference,str) and 1<=len(operator_reference.strip())<=2048,'RECOVERY_OPERATOR_REFERENCE_REQUIRED')
    job,binding,state=service.snapshot(job_id,write=False)
    service._validate_snapshot_binding(job,binding,verify_artifact_hash=False)
    service._require_workflow_launch(service.policy())
    need(job['state']=='awaiting_review' and job['stage']=='short_video_reconcile_required' and
         job['error_code']=='REPLAY_BROWSER_NATIVE_OPERATION_FAILED' and job['attempts']>=1 and
         job['lease_owner'] is None and job['lease_until'] is None and job['external_publish_id'] is None,
         'RECOVERY_JOB_NOT_ELIGIBLE')
    need(state['state']=='upload_unknown' and state.get('private_upload_selections')==1 and
         state.get('public_submission_approved') is False and state.get('human_review') is False and
         job['result'].get('publish_performed') is False and MARKER not in state and
         not any(k in state for k in ('private_upload_history','upload_observation','result_observation','reservation','owner_acceptance')),
         'RECOVERY_CHECKPOINT_NOT_ELIGIBLE')
    attempt=state['attempt_id'];need(str(uuid.UUID(attempt))==attempt,'RECOVERY_ATTEMPT_INVALID')
    area=safe(service.settings.root/'media'/job_id);intents=safe(area/'replay-native-intents')
    need(sorted(p.name for p in intents.iterdir())==['video-selection.json'],'RECOVERY_NON_SELECTION_INTENT')
    selection_raw=read(intents/'video-selection.json');selection=json.loads(selection_raw)
    native=service._native_base(job,binding,state)
    need(selection.get('kind')=='video-selection' and all(selection.get(k)==v for k,v in native.items()),'RECOVERY_SELECTION_BINDING_CHANGED')
    error_path=safe(area/'replay-browser-part-v5'/attempt/'last-error.json');error_raw=read(error_path)
    need(json.loads(error_raw)=={'action':'upload','error_code':'REPLAY_BROWSER_NATIVE_OPERATION_FAILED',
         'publication_result_unknown':False,'video_selection_attempted_in_session':True},'RECOVERY_PRE_SUBMIT_ERROR_REQUIRED')
    need(not service.store.query("SELECT 1 FROM publications WHERE source_platform='youtube' AND source_id=?",(job['source_id'],)),
         'RECOVERY_PUBLICATION_EXISTS')
    audit=service.store.query('SELECT action,outcome FROM audit WHERE target=?',(job_id,))
    need(sum(r['action']=='short_video_checkpoint' and r['outcome']=='uploading' for r in audit)==1 and
         not any('reserve_submit' in r['action'] or r['action']=='bounded_pre_submit_upload_recovery' or
                 r['outcome'] in ('submitting','submit_unknown','awaiting_moderation','completed') for r in audit),
         'RECOVERY_SUBMISSION_OR_RETRY_HISTORY')
    plan={'version':'bounded-pre-submit-upload-recovery-v1','job_id':job_id,'old_attempt_id':attempt,
          'before_row_sha256':digest(job),'attempts_preserved':job['attempts'],'operator_reference':operator_reference,
          'selection_sha256':hashlib.sha256(selection_raw).hexdigest(),'error_sha256':hashlib.sha256(error_raw).hexdigest(),
          'old_private_blob_absence_proven':False,'new_authorization':False,'public_submits':0,'fresh_worker_qa_required':True}
    return plan,job,selection_raw,error_raw

def recover(service,job_id,operator_reference,*,apply_plan_sha256=None):
    from services.hub.db import encode
    from services.hub.short_video_web import PRINCIPAL
    plan,job,selection,error=inspect(service,job_id,operator_reference);checksum=digest(plan)
    if apply_plan_sha256 is None:return {'state':'plan_only','plan_sha256':checksum,'plan':plan}
    need(apply_plan_sha256==checksum,'RECOVERY_PLAN_CHANGED')
    area=safe(service.settings.root/'media'/job_id);journal=safe(area/'private-upload-recovery'/checksum)
    need(not journal.exists(),'RECOVERY_JOURNAL_EXISTS_INSPECT_BEFORE_RETRY')
    journal.mkdir(parents=True,mode=0o700)
    for name,raw in [('job-before.json',wire(job)),('selection-copy.json',selection),('error.json',error),('plan.json',wire(plan))]:write(journal/name,raw)
    intent=area/'replay-native-intents/video-selection.json';archived=journal/'consumed-selection.json'
    moved=False
    try:
        with service.store.transaction() as db:
            current=dict(db.execute('SELECT * FROM jobs WHERE job_id=?',(job_id,)).fetchone())
            for key in ('payload','result'):current[key]=json.loads(current[key])
            need(current==job,'RECOVERY_ROW_CHANGED')
            need(not db.execute("SELECT 1 FROM publications WHERE source_platform='youtube' AND source_id=?",(job['source_id'],)).fetchone(),'RECOVERY_PUBLICATION_EXISTS')
            need(read(intent)==selection and sorted(p.name for p in intent.parent.iterdir())==['video-selection.json'],'RECOVERY_INTENT_CHANGED')
            state=deepcopy(job['result'][STATE]);prior=deepcopy(state)
            state.update(state='prepared',attempt_id=str(uuid.uuid4()),private_upload_selections=0)
            state[MARKER]={'version':plan['version'],'previous_checkpoint':prior,'plan_sha256':checksum,
                           'journal':str(journal),'old_private_blob_unknown':True,'total_prior_private_selections':1}
            result={**job['result'],STATE:state};now=time.time()
            os.rename(intent,archived);moved=True;sync(intent.parent);sync(journal)
            db.execute("UPDATE jobs SET state='awaiting_review',stage='replay_part_private_retry_prepared',result=?,error_code=NULL,updated_at=? WHERE job_id=?",
                       (encode(result),now,job_id))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                       (now,PRINCIPAL,'bounded_pre_submit_upload_recovery',job_id,encode(plan)))
    except BaseException:
        # A process kill may leave the durable journal with the job unchanged.
        # The separate rollback command handles that boundary; never requeue it.
        if moved and not intent.exists() and service.store.get(job_id)==job:
            os.rename(archived,intent);sync(intent.parent);sync(journal)
        raise
    return {'state':'one_private_retry_prepared','job_id':job_id,'plan_sha256':checksum,
            'new_attempt_id':state['attempt_id'],'attempts':job['attempts'],'journal':str(journal),'public_submits':0}

def prepare_cover_and_verify(service,job_id,*,release_plan_sha256=None):
    """Keep prior provenance; derive a new cover from the verified cached JPEG.

    This remains a hold until full existing machine QA passes. No HTTP call is
    necessary: the new attempt receives an exact local copy of the known JPEG.
    """
    from services.hub.short_video_web import PRINCIPAL
    from services.hub.replay_video_cover import ensure_replay_cover
    service._operator(PRINCIPAL);job,binding,state=service.snapshot(job_id,write=False)
    need(job['state']=='awaiting_review' and job['lease_owner'] is None and job['lease_until'] is None and
         state['state']=='prepared' and state.get('private_upload_selections')==0,'RECOVERY_PREPARED_HOLD_REQUIRED')
    marker=state[MARKER];prior=marker['previous_checkpoint'];journal=safe(Path(marker['journal']))
    area=safe(service.settings.root/'media'/job_id)
    need(journal.parent==area/'private-upload-recovery' and journal.name==marker['plan_sha256'] and
         digest(json.loads(read(journal/'plan.json')))==marker['plan_sha256'],'RECOVERY_JOURNAL_INVALID')
    need(not any((area/'replay-native-intents').iterdir()) and
         not service.store.query("SELECT 1 FROM publications WHERE source_platform='youtube' AND source_id=?",(job['source_id'],)),
         'RECOVERY_SUBMISSION_OR_SELECTION_EXISTS')
    old=safe(area/'short-video-attempts'/prior['attempt_id']/'cover-source-official-maxres.jpg')
    current=safe(area/'short-video-attempts'/state['attempt_id']/'cover-source-official-maxres.jpg')
    cover=area/'cover-assets-v4';saved=journal/'prior-cover-assets'
    provenance=(saved if saved.exists() else cover)/'provenance.json';body=json.loads(read(provenance))
    source=body['input'];original=read(old)
    need(source['file']==str(old) and source['source_url']=='https://i.ytimg.com/vi/'+binding['source_id']+'/maxresdefault.jpg' and
         source['bytes']==len(original) and source['sha256']==hashlib.sha256(original).hexdigest(),'RECOVERY_CACHED_COVER_CHANGED')
    current.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if current.exists():need(read(current)==original,'RECOVERY_NEW_COVER_SOURCE_CHANGED')
    else:write(current,original)
    if not saved.exists():
        safe(cover)
        need(all(not p.is_symlink() for p in cover.rglob('*')),'RECOVERY_COVER_SYMLINK')
        os.rename(cover,saved);sync(area);sync(journal)
    generated=ensure_replay_cover(service.settings,job,binding,state['attempt_id'])
    # Run the complete production machine verifier before another claim.
    service.verify_for_publish(job_id)
    outcome={'state':'cover_and_machine_qa_verified','job_id':job_id,'attempts_preserved':job['attempts'],
             'original_cover_archived':str(saved),'new_cover':generated['output_file'],'public_submits':0}
    if release_plan_sha256 is not None:
        need(release_plan_sha256==marker['plan_sha256'],'RECOVERY_RELEASE_PLAN_CHANGED')
        need(job['attempts']<service.settings.config['max_attempts'],'RECOVERY_ATTEMPT_BUDGET_EXHAUSTED')
        service._update(PRINCIPAL,job,state,job_state='pending',stage='replay_part_verified_private_retry_ready')
        service.store.audit(PRINCIPAL,'bounded_private_retry_machine_qa_verified',job_id,marker['plan_sha256'])
        outcome['state']='one_verified_private_retry_pending'
    return outcome

def restore_interrupted_journal(service,job_id,checksum):
    """Restore only the consumed intent when SQLite still has the exact old row."""
    from services.hub.short_video_web import PRINCIPAL
    service._operator(PRINCIPAL)
    need(str(uuid.UUID(job_id))==job_id,'RECOVERY_JOB_ID_INVALID')
    need(len(checksum)==64 and all(c in '0123456789abcdef' for c in checksum),'RECOVERY_JOURNAL_INVALID')
    area=safe(service.settings.root/'media'/job_id);journal=safe(area/'private-upload-recovery'/checksum)
    plan=json.loads(read(journal/'plan.json'));before=json.loads(read(journal/'job-before.json'))
    need(digest(plan)==checksum and plan['job_id']==job_id and digest(before)==plan['before_row_sha256'] and
         service.store.get(job_id)==before,'RECOVERY_ROLLBACK_ROW_CHANGED')
    intent=area/'replay-native-intents/video-selection.json';archived=journal/'consumed-selection.json'
    raw=read(archived);need(hashlib.sha256(raw).hexdigest()==plan['selection_sha256'] and not any(intent.parent.iterdir()),'RECOVERY_ROLLBACK_INTENT_CHANGED')
    os.rename(archived,intent);sync(intent.parent);sync(journal)
    return {'state':'original_unknown_checkpoint_preserved','job_id':job_id,'requeued':False}

@contextmanager
def locks(root):
    from services.hub.locks import resource_guard
    # Cover normalization/QA take their own heavy-resource locks; do not take a
    # non-reentrant heavy lock here. The queue lock excludes other video work.
    with resource_guard(*(root/'data/locks'/n for n in ('video','moderation','replay-multipart-producer-v4','replay-fifo-dispatch-v4','bilibili-video-profile'))):yield

def main():
    from services.hub.config import Settings
    from services.hub.db import Store
    from services.hub.replay_parts_v5 import ReplayPartVideoWeb,index_from_job
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--job',required=True);p.add_argument('--operator-reference',required=True)
    p.add_argument('--apply-plan-sha256');p.add_argument('--restore-interrupted-journal')
    p.add_argument('--prepare-cover',action='store_true');p.add_argument('--release-plan-sha256');a=p.parse_args()
    need(os.getuid()==os.getgid()==10001,'RECOVERY_APP_UID_REQUIRED')
    need(str(uuid.UUID(a.job))==a.job,'RECOVERY_JOB_ID_INVALID')
    need(not(a.apply_plan_sha256 and a.restore_interrupted_journal),'RECOVERY_MODE_INVALID')
    need(not(a.prepare_cover and (a.apply_plan_sha256 or a.restore_interrupted_journal)) and
         (not a.release_plan_sha256 or a.prepare_cover),'RECOVERY_MODE_INVALID')
    settings=Settings.load();store=Store(settings.db_path);service=ReplayPartVideoWeb(settings,store,part_index=index_from_job(store.get(a.job)))
    with locks(settings.root):
        if a.prepare_cover:value=prepare_cover_and_verify(service,a.job,release_plan_sha256=a.release_plan_sha256)
        elif a.restore_interrupted_journal:value=restore_interrupted_journal(service,a.job,a.restore_interrupted_journal)
        else:value=recover(service,a.job,a.operator_reference,apply_plan_sha256=a.apply_plan_sha256)
    print(json.dumps(value))

if __name__=='__main__':main()
