"""Actual native handler + default checker; only browser transport is owned."""
from datetime import datetime, timezone
from pathlib import Path

from test_short_video_web import short_video, result as result_observation
from test_replay_video_web import replay, native_prepare
from test_replay_machine_checks import owned
from test_replay_worker import queue, upload_observation
from services.hub.locks import resource_guard
from services.hub.replay_video_browser import ReplayVideoBrowser, ReplayVideoWorkerHandler
from services.hub.replay_video_web import STATE, JOB_TYPE
from services.hub.worker import Worker


def transport(f, *, malformed_upload=False, lookup_unknown=False):
    counts={'upload':0,'prepare':0,'submit':0,'lookup':0,'closed':0}
    class OwnedSession:
        def __init__(self,settings,manifest,*,assert_lease):
            self.settings,self.manifest,self.check=settings,manifest,assert_lease
        def operation(self,operation,**arguments):
            # Match the real session: prepare/submit/lookup are local heavy
            # phases, while upload is network-only and must not take heavy.
            if operation=='upload':
                return self._operation(operation,**arguments)
            with resource_guard(self.settings.root/'data/locks/heavy'):
                return self._operation(operation,**arguments)
        def _operation(self,operation,**arguments):
            self.check();counts[operation]+=1;m=self.manifest
            job,binding,state=f['service'].snapshot(m['job_id'],write=False)
            if operation=='upload':
                return {} if malformed_upload else {'upload_observation':upload_observation(binding,m['attempt_id'])}
            if operation=='prepare':
                proof={'checks':{'cover':{'output':{'sha256':m['cover']['sha256']},
                    'provenance':{'sha256':m['cover_derivation']['provenance_sha256']}}}}
                return {'native_preparation':native_prepare(f['fixture'],m['job_id'],m['attempt_id'],proof=proof,record=False)}
            if operation=='submit':
                ticket=arguments['reservation']
                assert ticket['machine_evidence_sha256']==state['machine_acceptance']['evidence_sha256']
                assert job['state']=='processing' and state['state']=='submitting'
                return {'result_observation':result_observation(m['attempt_id'],binding)}
            if lookup_unknown:return {'candidate_only':True}
            assert m['cover'] is None and m['cover_derivation'] is None
            return {'result_observation':{**result_observation(m['attempt_id'],binding,'completed'),
                'public_accessible':True,'public_lookup_observed_at':datetime.now(timezone.utc).isoformat()}}
        def close(self):counts['closed']+=1
    return ReplayVideoBrowser(f['s'],session_factory=OwnedSession),counts


def test_actual_handler_pilot_submits_after_real_checks_and_later_lookup_needs_no_local_media(owned,monkeypatch):
    f=owned;job_id=queue(f);attempt=f['original'].parent.name
    actual=f['service'].machine_acceptance_verifier.verify;verified=[]
    def observe_check(job,binding,state,policy,**kwargs):
        result=actual(job,binding,state,policy,**kwargs)
        verified.append((state['state'],'native_preparation' in state,result['evidence_sha256']))
        return result
    monkeypatch.setattr(f['service'].machine_acceptance_verifier,'verify',observe_check)
    f['service'].bind_browser_attempt('operator:ssh-sudo',job_id,attempt_id=attempt)
    browser,counts=transport(f)
    handler=ReplayVideoWorkerHandler(f['s'],f['db'],service=f['service'],browser=browser,
        cover_provider=lambda *args:None)  # Real v4 fixture already generated.
    worker=Worker(f['s'],f['db'],{JOB_TYPE:handler.process})
    assert worker.once('video')
    first=f['db'].get(job_id)
    assert first['state']=='awaiting_moderation' and first['result'][STATE]['state']=='awaiting_moderation'
    assert first['result'][STATE]['public_submission_approved'] is False and first['result'][STATE]['human_review'] is False
    assert f['service'].policy()['automation_enabled'] is False  # Explicit pilot, not all-new replay launch.
    assert counts=={'upload':1,'prepare':1,'submit':1,'lookup':0,'closed':1}
    # Real checks remain before upload, while recording the native form, and
    # at the actual one-use reservation. There is no unused fourth packet scan.
    assert [(state,native) for state,native,_ in verified]==[('prepared',False),('uploaded',False),('uploaded',True)]
    assert len({proof for _,_,proof in verified})==3
    assert first['result'][STATE]['machine_acceptance']['evidence_sha256']==verified[-1][2]
    assert first['result'][STATE]['machine_acceptance']['checks']['media']['stream_config']['audio']['codec_name']=='aac'
    # Owned retention-boundary simulation only; production pilot files remain
    # held. Lookup must not re-open original media or require a deleted cover.
    output=Path(f['derived']['output_file']);assert output.is_relative_to(f['s'].root)
    f['file'].unlink();output.unlink()
    with f['db'].transaction() as connection:connection.execute("UPDATE jobs SET state='pending' WHERE job_id=?",(job_id,))
    assert worker.once('video')
    final=f['db'].get(job_id)
    assert final['state']=='completed' and final['result'][STATE]['result_observation']['public_accessible'] is True
    assert final['attempts']==2 and counts=={'upload':1,'prepare':1,'submit':1,'lookup':1,'closed':2}
    assert len(verified)==3  # Result lookup never demands local packet scans.
    assert len(f['db'].query('SELECT * FROM publications'))==1
    with resource_guard(f['s'].root/'data/locks/heavy',f['s'].root/'data/locks/bilibili-video-profile'):pass


def test_actual_handler_malformed_upload_becomes_private_unknown_without_another_selection(owned):
    f=owned;job_id=queue(f);attempt=f['original'].parent.name
    f['service'].bind_browser_attempt('operator:ssh-sudo',job_id,attempt_id=attempt)
    browser,counts=transport(f,malformed_upload=True)
    handler=ReplayVideoWorkerHandler(f['s'],f['db'],service=f['service'],browser=browser,cover_provider=lambda *args:None)
    assert Worker(f['s'],f['db'],{JOB_TYPE:handler.process}).once('video')
    job=f['db'].get(job_id)
    assert job['state']=='awaiting_review' and job['result'][STATE]['state']=='upload_unknown'
    assert queue(f)==job_id and counts=={'upload':1,'prepare':0,'submit':0,'lookup':0,'closed':1}
    assert not f['db'].query('SELECT * FROM publications')


def test_actual_handler_unknown_without_recorded_bv_only_looks_up_and_never_claims_a_candidate(owned):
    f=owned;job_id=queue(f);attempt=f['original'].parent.name;service=f['service']
    service.start_upload('operator:ssh-sudo',job_id,attempt_id=attempt,site_uid='123456')
    _,binding,_=service.snapshot(job_id)
    service.record_upload('operator:ssh-sudo',job_id,upload_observation(binding,attempt))
    native_prepare(f['fixture'],job_id,attempt)
    assert service.reserve_submit('operator:ssh-sudo',job_id,attempt_id=attempt,site_uid='123456',upload_reference_sha256='b'*64)['can_click_submit'] is True
    service.mark_unknown('operator:ssh-sudo',job_id);f['file'].unlink()
    with f['db'].transaction() as connection:connection.execute("UPDATE jobs SET state='pending' WHERE job_id=?",(job_id,))
    browser,counts=transport(f,lookup_unknown=True)
    handler=ReplayVideoWorkerHandler(f['s'],f['db'],service=service,browser=browser,cover_provider=lambda *args:None)
    assert Worker(f['s'],f['db'],{JOB_TYPE:handler.process}).once('video')
    final=f['db'].get(job_id)
    assert final['state']=='publish_unknown' and final['result'][STATE]['state']=='submit_unknown'
    assert final['external_publish_id'] is None and 'result_observation' not in final['result'][STATE]
    assert counts=={'upload':0,'prepare':0,'submit':0,'lookup':1,'closed':1}
    assert len(f['db'].query('SELECT * FROM publications'))==1
