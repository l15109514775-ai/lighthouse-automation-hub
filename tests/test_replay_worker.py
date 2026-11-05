"""Real Worker/SQLite/FFmpeg; mocked owned transport, no external account."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid

import pytest

from test_short_video_web import short_video, PRINCIPAL, result as result_observation
from test_replay_video_web import replay, native_prepare, write_policy, owned_proof, uploaded, authorize, reserve
from test_replay_machine_checks import owned
from scheduler.service import DurableScheduler
from services.hub.db import encode
from services.hub.errors import HubError, PublishUnknown
from services.hub.outcomes import JobOutcome
from services.hub.replay_video_web import WORKER_PRINCIPAL, STATE, JOB_TYPE
from services.hub.worker import Worker
from services.hub.youtube_monitor import YouTubeMonitor
from services.hub.youtube_download import digest


def queue(f):
    return f['service'].queue_pilot(PRINCIPAL,f['source'],f['fixture'][4],
        actual_owner_workflow_response='Owned workflow pilot request; not a real user statement',
        owner_response_reference='Owned pilot workflow fixture')


def upload_observation(binding,attempt):
    return {'version':'short-video-browser-upload-v1','attempt_id':attempt,'site_uid':binding['site_uid'],
        'artifact_sha256':binding['artifact_sha256'],'file_bytes':binding['file_bytes'],
        'upload_reference_sha256':'b'*64,'same_attempt_observed':True,'server_file_hash_verified':False}


def test_blocked_upload_renews_lease_next_day_discovery_deduplicates_and_unknown_is_preserved(owned):
    f=owned;s,db,service=f['s'],f['db'],f['service'];s.config['lease_seconds']=0.9
    source=db.get(f['source']);raw=source['payload']['raw'];monitor=YouTubeMonitor(s,db,None)
    monitor.observe_video(raw,source['payload']['channel_id'])
    metadata_job=db.claim('video',180,4);assert metadata_job['job_type']=='video_process'
    db.finish(metadata_job,'completed',result={'owned_metadata_only':True})
    job_id=queue(f);assert job_id==f['job_id'];attempt=f['original'].parent.name
    entered=threading.Event();release=threading.Event();done=[];counts={'file_selections':0,'public_submit':0}
    def handler(job):
        with service.worker_context(job):
            assert service.verify_for_publish(job_id)['checks']['media']['stream_config']['video']['codec_name']=='h264'
            service.start_upload(WORKER_PRINCIPAL,job_id,attempt_id=attempt,site_uid='123456')
            counts['file_selections']+=1  # Owned transport invocation, not a real chooser.
            entered.set();assert release.wait(8)
            service.assert_worker_lease()
            current,binding,state=service.snapshot(job_id)
            assert current['state']=='processing' and current['lease_owner']==job['lease_owner'] and state['state']=='uploading'
            service.record_upload(WORKER_PRINCIPAL,job_id,upload_observation(binding,attempt))
            native_prepare(f['fixture'],job_id,attempt)
            permit=service.reserve_submit(WORKER_PRINCIPAL,job_id,attempt_id=attempt,site_uid='123456',upload_reference_sha256='b'*64)
            assert permit['can_click_submit'] is True
            assert db.get(job_id)['state']=='processing' and job['result'][STATE]['state']=='submitting'
            counts['public_submit']+=1
            service.mark_unknown(WORKER_PRINCIPAL,job_id)
            raise PublishUnknown()
    worker=Worker(s,db,{JOB_TYPE:handler})
    thread=threading.Thread(target=lambda:done.append(worker.once('video')));thread.start()
    try:
        assert entered.wait(8)
        original=db.get(job_id);initial_expiry=original['lease_until'];original_owner=original['lease_owner']
        baseline_downloads=db.query("SELECT COUNT(*) n FROM jobs WHERE job_type='youtube_download'")[0]['n']
        assert queue(f)==job_id
        monitor.observe_video(raw,source['payload']['channel_id'])
        next_day=datetime.now(timezone.utc)+timedelta(days=1)
        scheduled=next_day.replace(hour=1,minute=0,second=0,microsecond=0).timestamp()
        with db.transaction() as connection:
            connection.execute('INSERT OR REPLACE INTO schedules VALUES(?,?)',('youtube_discovery',scheduled))
        DurableScheduler(s,db).tick(scheduled)
        assert not worker.once('video')  # The active worker's actual queue lock fences recovery.
        deadline=time.monotonic()+4
        while time.time()<=initial_expiry+0.15 and time.monotonic()<deadline:time.sleep(0.04)
        current=db.get(job_id)
        assert time.time()>initial_expiry and current['lease_until']>time.time()
        assert current['lease_owner']==original_owner and current['state']=='processing' and current['attempts']==1
        assert db.query("SELECT COUNT(*) n FROM jobs WHERE job_type='youtube_download'")[0]['n']==baseline_downloads==1
        assert len(db.query('SELECT * FROM jobs WHERE job_type=?',(JOB_TYPE,)))==1
        assert counts=={'file_selections':1,'public_submit':0} and not db.query('SELECT * FROM publications')
        assert db.query("SELECT at FROM heartbeats WHERE name='worker:video'")[0]['at']>original['updated_at']
    finally:
        release.set();thread.join(15)
    assert not thread.is_alive() and done==[True]
    final=db.get(job_id)
    assert final['state']=='publish_unknown' and final['result'][STATE]['state']=='submit_unknown'
    assert final['lease_owner'] is None and final['attempts']==1
    assert final['result'][STATE]['native_preparation']['private_draft_saved'] is True
    assert final['result'][STATE]['machine_acceptance']['checks']['cover']['compared_rgb_bytes']==2_764_800
    assert counts=={'file_selections':1,'public_submit':1}
    assert len(db.query('SELECT * FROM publications'))==1 and db.query('SELECT state FROM publications')[0]['state']=='unknown'
    assert queue(f)==job_id and counts=={'file_selections':1,'public_submit':1}


def test_worker_exception_keeps_private_upload_unknown_checkpoint_and_does_not_requeue(owned):
    f=owned;job_id=queue(f);attempt=f['original'].parent.name;calls=[]
    def handler(job):
        with f['service'].worker_context(job):
            f['service'].start_upload(WORKER_PRINCIPAL,job_id,attempt_id=attempt,site_uid='123456');calls.append('owned_upload')
            f['service'].mark_unknown(WORKER_PRINCIPAL,job_id)
            raise HubError('OWNED_UPLOAD_RESULT_UNKNOWN',review=True)
    worker=Worker(f['s'],f['db'],{JOB_TYPE:handler})
    assert worker.once('video')
    job=f['db'].get(job_id)
    assert job['state']=='awaiting_review' and job['result'][STATE]['state']=='upload_unknown'
    assert queue(f)==job_id and not worker.once('video') and calls==['owned_upload'] and job['attempts']==1


def test_handler_outcome_finishes_processing_only_after_return(owned):
    f=owned;job_id=queue(f);attempt=f['original'].parent.name
    def handler(job):
        with f['service'].worker_context(job):
            f['service'].start_upload(WORKER_PRINCIPAL,job_id,attempt_id=attempt,site_uid='123456')
            binding=job['payload'];f['service'].record_upload(WORKER_PRINCIPAL,job_id,upload_observation(binding,attempt))
            assert job['state']==f['db'].get(job_id)['state']=='processing' and job['result'][STATE]['state']=='uploaded'
            return JobOutcome('upload_pending',deepcopy(job['result']))
    assert Worker(f['s'],f['db'],{JOB_TYPE:handler}).once('video')
    job=f['db'].get(job_id)
    assert job['state']=='upload_pending' and job['result'][STATE]['state']=='uploaded' and job['lease_owner'] is None


def test_cover_network_retry_reuses_bound_attempt_without_counting_a_video_selection(owned):
    f=owned;job_id=queue(f);attempt=f['original'].parent.name;calls=[]
    def handler(job):
        with f['service'].worker_context(job):
            answer=f['service'].bind_browser_attempt(WORKER_PRINCIPAL,job_id,attempt_id=attempt)
            assert answer['new_file_selection'] is False and job['result'][STATE]['attempt_id']==attempt
            calls.append('cover_attempt')
            if len(calls)==1:raise HubError('OWNED_COVER_NETWORK_INTERRUPTED',retryable=True)
            with pytest.raises(HubError,match='REPLAY_BROWSER_ATTEMPT_RECONCILE_REQUIRED'):
                f['service'].bind_browser_attempt(WORKER_PRINCIPAL,job_id,attempt_id=str(uuid.uuid4()))
            f['service'].start_upload(WORKER_PRINCIPAL,job_id,attempt_id=attempt,site_uid='123456');calls.append('owned_upload')
            f['service'].mark_unknown(WORKER_PRINCIPAL,job_id)
            raise HubError('OWNED_UPLOAD_UNKNOWN',review=True)
    worker=Worker(f['s'],f['db'],{JOB_TYPE:handler});assert worker.once('video')
    first=f['db'].get(job_id)
    assert first['state']=='retry_scheduled' and first['result'][STATE]['state']=='prepared'
    assert first['result'][STATE]['attempt_id']==attempt and first['result'][STATE].get('private_upload_selections',0)==0
    with f['db'].transaction() as connection:connection.execute('UPDATE jobs SET next_run=0 WHERE job_id=?',(job_id,))
    assert worker.once('video');final=f['db'].get(job_id)
    assert final['attempts']==2 and final['result'][STATE]['private_upload_selections']==1
    assert final['result'][STATE]['attempt_id']==attempt and calls==['cover_attempt','cover_attempt','owned_upload']
    assert not worker.once('video') and not f['db'].query('SELECT * FROM publications')


@pytest.mark.parametrize('change',['owner','expired','attempt','payload'])
def test_worker_context_rejects_stale_or_changed_lease_before_any_transport(owned,change):
    f=owned;job_id=queue(f);job=f['db'].claim('video',180,4)
    with f['db'].transaction() as connection:
        if change=='owner':connection.execute('UPDATE jobs SET lease_owner=? WHERE job_id=?',(str(uuid.uuid4()),job_id))
        elif change=='expired':connection.execute('UPDATE jobs SET lease_until=0 WHERE job_id=?',(job_id,))
        elif change=='attempt':connection.execute('UPDATE jobs SET attempts=attempts+1 WHERE job_id=?',(job_id,))
        else:
            payload=deepcopy(job['payload']);payload['metadata']['title']='Owned unbound change'
            connection.execute('UPDATE jobs SET payload=? WHERE job_id=?',(encode(payload),job_id))
    with pytest.raises(HubError,match='REPLAY_WORKER_LEASE_LOST'):
        with f['service'].worker_context(job):pytest.fail('Invalid lease entered')
    assert not f['db'].query('SELECT * FROM publications')


def test_heartbeat_is_allowed_but_concurrent_checkpoint_changes_are_not_lost(owned):
    f=owned;job_id=queue(f);job=f['db'].claim('video',180,4);service=f['service']
    with service.worker_context(job):
        current,_,state=service.snapshot(job_id);assert f['db'].heartbeat(job,180)
        service._update(WORKER_PRINCIPAL,current,state,job_state='upload_pending',stage='owned_heartbeat_checkpoint')
        assert job['state']=='processing' and job['stage']=='owned_heartbeat_checkpoint'
        stale,_,state=service.snapshot(job_id)
        changed=deepcopy(stale['result']);changed['owned_concurrent_change']=True
        with f['db'].transaction() as connection:connection.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(changed),job_id))
        with pytest.raises(HubError,match='SHORT_VIDEO_JOB_CHANGED'):
            service._update(WORKER_PRINCIPAL,stale,state,job_state='upload_pending',stage='must_not_overwrite')
        assert f['db'].get(job_id)['result']['owned_concurrent_change'] is True


def test_all_new_automatic_mode_stays_closed_even_with_enabled_policy_and_local_pass(replay,monkeypatch):
    replay[6].update(source_scope='automatic_new_only',acceptance_video_ids=[],automation_enabled=True);write_policy(replay[5],replay[6])
    job,attempt,_=uploaded(replay);authorize(replay,job);owned_proof(replay,job,monkeypatch)
    with pytest.raises(HubError,match='REPLAY_WORKFLOW_LAUNCH_ACCEPTANCE_REQUIRED'):reserve(replay,job,attempt)
    assert not replay[1].query('SELECT * FROM publications')


def test_global_launch_grant_binds_completed_pilot_and_cannot_be_replaced_by_passed_flag(replay,monkeypatch):
    s,db,service,*_=replay;job,attempt,binding=uploaded(replay);authorize(replay,job);owned_proof(replay,job,monkeypatch)
    native_prepare(replay,job,attempt);reserve(replay,job,attempt)
    service.record_result(PRINCIPAL,job,result_observation(attempt,binding))
    service.record_result(PRINCIPAL,job,{**result_observation(attempt,binding,'completed'),'public_accessible':True,
        'public_lookup_observed_at':datetime.now(timezone.utc).isoformat()},lookup=True)
    previous=db.get(job)['result'][STATE]
    replay[6].update(source_scope='automatic_new_only',acceptance_video_ids=[],automation_enabled=True);write_policy(replay[5],replay[6])
    policy=service.policy();file=s.root/'owned-root-workflow-launch.json';service.WORKFLOW_LAUNCH_RECEIPT=file
    grant={'version':'replay-workflow-launch-acceptance-v1','policy_sha256':policy['_sha256'],
        'workflow_authorization_reference':policy['workflow_authorization_reference'],'target_site_uid':policy['target_site_uid'],
        'allowed_channel_ids':policy['allowed_channel_ids'],'acceptance_job_id':job,
        'pilot_machine_evidence_sha256':previous['machine_acceptance']['evidence_sha256'],
        'pilot_public_result_sha256':digest(previous['result_observation']),
        'actual_owner_launch_response':'Owned formal workflow request, not a real user statement',
        'owner_response_reference':'Owned root-only grant fixture','accepted_at':datetime.now(timezone.utc).isoformat()}
    def read_owned(path,*,maximum):
        assert path==file and path.is_file() and path.stat().st_size<=maximum;return path.read_bytes()
    monkeypatch.setattr(service,'_root_readonly_file',read_owned)
    file.write_text(encode(grant),'utf-8');assert service._require_workflow_launch(policy)==grant
    grant['pilot_public_result_sha256']='0'*64;file.write_text(encode(grant),'utf-8')
    with pytest.raises(HubError,match='REPLAY_WORKFLOW_LAUNCH_ACCEPTANCE_REQUIRED'):service._require_workflow_launch(policy)
    file.write_text('{"passed":true}','utf-8')
    with pytest.raises(HubError,match='REPLAY_WORKFLOW_LAUNCH_ACCEPTANCE_REQUIRED'):service._require_workflow_launch(policy)


@pytest.mark.parametrize('tamper',['description','cover_hash','inner_file','screenshot_file','stale','fake_human_review'])
def test_native_preparation_requires_actual_bound_readback_files_and_fresh_observation(replay,monkeypatch,tamper):
    job,attempt,_=uploaded(replay);authorize(replay,job);owned_proof(replay,job,monkeypatch)
    observation=native_prepare(replay,job,attempt)
    if tamper=='description':observation['description']+='Owned unrelated content'
    elif tamper=='cover_hash':observation['cover_sha256']='0'*64
    elif tamper=='inner_file':Path(observation['native_observation_file']['file']).write_bytes(b'{"passed":true}')
    elif tamper=='screenshot_file':Path(observation['pre_submit_screenshot']['file']).write_bytes(b'Owned bad PNG')
    elif tamper=='stale':observation['observed_at']='2026-01-01T00:00:00Z'
    else:observation['human_review']=True
    before=replay[1].get(job)
    with pytest.raises(HubError,match='REPLAY_NATIVE_PREPARATION_REQUIRED'):
        replay[2].record_native_prepare(PRINCIPAL,job,observation)
    assert replay[1].get(job)==before and not replay[1].query('SELECT * FROM publications')
