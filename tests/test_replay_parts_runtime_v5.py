from pathlib import Path as _RecoveryPath
from services.hub.locks import exclusive as _recovery_lock
"""Positive type scoping and real FIFO group projection; no browser/network."""
from copy import deepcopy
import json,time
import threading,uuid
from contextlib import contextmanager
import pytest

from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.replay_parts_worker_runtime_v5 import QueueView,build_worker
from services.hub.replay_parts_v5 import PART_JOB_TYPES,POLICY,STATE,GROUP_JOB,ReplayPartVideoWeb
from services.hub.replay_fifo_v4 import PLAN_JOB,DOWNLOAD_JOB,REFRESH_JOB,VERSION as FIFO_POLICY
from services.hub.youtube_readiness_v4 import JOB_TYPE as READINESS_JOB
from test_replay_fifo_v4 import fifo
from test_ytdlp_highest_v2 import tracks
from test_replay_delivery_v5 import delivery
from test_replay_parts_v5 import publisher,create
from test_replay_parts_v5 import owned_upload,owned_preparation,cover,public_evidence,write,ref
from test_replay_part_moderation_v5 import lookup


def test_default_registration_does_not_need_send_policy_or_claim_even_with_v5_jobs(fifo):
    f=fifo;old=f.db.enqueue('youtube','Owned-one',PART_JOB_TYPES[0],'video',{},POLICY)
    worker,coordinator=build_worker(f.s,f.db)
    assert coordinator.mode=='register_only' and not worker.store.publish_enabled
    assert set(worker.handlers)=={PLAN_JOB,DOWNLOAD_JOB,REFRESH_JOB,READINESS_JOB}
    before=f.db.get(old);result=coordinator.tick()
    assert result['external_requests']==0 and result['publisher_registered'] is False
    assert not worker.once('video') and f.db.get(old)==before


@pytest.mark.parametrize('wrong',['policy','type','platform'])
def test_v5_claim_never_eats_unhandled_or_legacy_jobs(lookup,wrong):
    # This is the explicit historical registry seam, not a media/QA claim.
    f=lookup;values={'policy':POLICY,'type':PART_JOB_TYPES[0],'platform':'youtube'}
    values[wrong]={'policy':'old-version','type':'replay_highest_video_publish_v2','platform':'bilibili'}[wrong]
    rejected=f.db.enqueue(values['platform'],'ownedOld',values['type'],'video',{},values['policy'])
    wanted=f.parent
    with f.db.transaction() as db:
        db.execute('DELETE FROM publications WHERE job_id=?',(wanted,))
        result=f.db.get(wanted)['result'];result[STATE]['state']='prepared'
        db.execute("UPDATE jobs SET state='pending',attempts=0,result=? WHERE job_id=?",(encode(result),wanted))
        db.execute("UPDATE replay_part_children_v5 SET state='prepared' WHERE child_job_id=?",(wanted,))
    before=f.db.get(rejected);q=QueueView(f.db,publish_enabled=True,acquisition_enabled=True)
    assert q.claim('video',30,4)['job_id']==wanted and f.db.get(rejected)==before
    assert q.claim('video',30,4) is None


def test_fixed_32_handlers_and_wait_callbacks_preserve_old_acquisition_scope(publisher):
    f=publisher;worker,coordinator=build_worker(f.s,f.db,mode='publish')
    assert set(worker.handlers)=={PLAN_JOB,DOWNLOAD_JOB,REFRESH_JOB,READINESS_JOB,*PART_JOB_TYPES}
    assert set(worker.resource_deferrals)=={PLAN_JOB,DOWNLOAD_JOB} and set(worker.readiness_deferrals)=={PLAN_JOB}
    assert GROUP_JOB not in worker.handlers
    create(f);result=coordinator.tick();candidate=f.db.query('SELECT * FROM replay_fifo_v4')[0]
    assert candidate['publication_job_id']==f.parent and candidate['state']=='publication_queued'
    assert result['external_requests']==0 and not f.db.query('SELECT * FROM publications')


def test_registered_group_is_not_reinterpreted_by_legacy_sourcewide_single_pub_rule(publisher):
    f=publisher;create(f);job,binding,state=f.publish.snapshot(f.child)
    # The true protected primitive is tested here independently of the native
    # boundary. No send/model/QA acceptance is claimed by this direct call.
    assert f.publish._reserve_submit_once('owned-test',job,binding,state,site_uid=binding['site_uid'])['can_click_submit']
    with f.db.transaction() as db:
        db.execute("UPDATE publications SET state='unknown' WHERE job_id=?",(f.child,))
        result=f.db.get(f.child)['result'];result[STATE]['state']='submit_unknown'
        db.execute("UPDATE jobs SET state='publish_unknown',result=? WHERE job_id=?",(encode(result),f.child))
    old=f.db.get(f.child)
    r=f.q.sync_states(publication_projection=f.groups.fifo_projection)
    candidate=f.db.query('SELECT * FROM replay_fifo_v4')[0]
    assert candidate['publication_job_id']==f.parent and candidate['state']=='unknown'
    assert f.db.get(f.child)==old and r['job_rows_changed']==r['publication_rows_changed']==0
    # Same group projects correctly after switching back to register-only.
    worker,coordinator=build_worker(f.s,f.db);coordinator.tick()
    assert f.db.query('SELECT state FROM replay_fifo_v4')[0]['state']=='unknown'
    assert f.db.get(f.child)==old


def test_current_unknown_child_never_recovered_as_fresh_upload_and_other_parent_is_not_claimed(fifo):
    f=fifo;own=f.db.enqueue('youtube','oldClaim',PART_JOB_TYPES[0],'video',{},POLICY)
    legacy=f.db.enqueue('youtube','oldClaim','replay_highest_video_publish_v2','video',{},'old')
    with f.db.transaction() as db:
        for job in (own,legacy):db.execute("UPDATE jobs SET state='processing',attempts=2,lease_owner='owned lost',lease_until=0 WHERE job_id=?",(job,))
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',('a'*64,own,'bilibili','youtube','oldClaim','site_uid:owned','owned','unknown',None,None,time.time()))
    before=f.db.get(legacy);q=QueueView(f.db,publish_enabled=True,acquisition_enabled=True)
    with _recovery_lock(_RecoveryPath(q.path).parent / "locks" / 'video'):
        assert q.recover('video',4,all_processing=True)==1
    assert f.db.get(own)['state']=='publish_unknown' and f.db.get(own)['attempts']==2 and q.claim('video',30,4) is None
    assert f.db.get(legacy)==before


@pytest.mark.parametrize('pending',[False,True])
def test_actual_worker_source_qa_native_checkpoint_heartbeat_and_single_publish_transport(publisher,pending):
    """Only the named browser boundary is an owned double, not real Bili UI."""
    from datetime import datetime,timezone
    from pathlib import Path
    from services.hub.worker import Worker
    from services.hub.locks import resource_guard
    from services.hub.replay_part_publish_worker_v5 import PartReplayWorkerHandler
    from services.hub.replay_part_moderation_v5 import schedule_tick
    f=publisher;create(f);ready=threading.Event();release=threading.Event();calls=[]
    f.s.config['lease_seconds']=1.5
    class OwnedBrowser:
        @contextmanager
        def session(self,manifest,*,assert_lease,before_launch=None):
            assert manifest['version']=='replay-part-native-browser-manifest-v5' and manifest['part_count']==1
            with resource_guard(f.s.root/'data/locks/bilibili-video-profile'):
                with resource_guard(f.s.root/'data/locks/heavy'):
                    assert_lease()
                    if before_launch:before_launch()
                class Session:
                    def operation(self,operation,**arguments):
                        with resource_guard(f.s.root/'data/locks/heavy'):
                            assert_lease();calls.append(operation)
                            job,binding,state=f.publish.snapshot(f.child,write=False)
                            if operation=='upload':
                                ready.set();assert release.wait(10);assert_lease()
                                return {'upload_observation':owned_upload(f,f.publish,state['attempt_id'])}
                            if operation=='prepare':
                                path=f.s.root/'media'/f.child/'cover-assets-v4/provenance.json';body=json.loads(path.read_bytes())
                                return {'native_preparation':owned_preparation(f,f.publish,{'checks':{'cover':{'output':body['output'],'provenance':ref(path)}}})}
                            assert operation=='submit';f.ticket=arguments['reservation'];f.clicked=datetime.now(timezone.utc)
                            f.intent=write(f.s.root/'media'/f.child/'replay-native-intents/public-submit.json',{'version':'replay-part-one-use-intent-v5',
                                **f.publish._native_base(job,binding,state),'kind':'public-submit','created_at':f.clicked.isoformat(),'reservation':f.ticket})
                            if pending:return {'result_observation':{'state':'awaiting_moderation'}}
                            observation=public_evidence(f);reference=write(f.s.root/'media'/f.child/f.publish.NATIVE_DIRECTORY/state['attempt_id']/'result-history'/str(uuid.uuid4()),observation)
                            return {'result_observation':observation,'result_reference':reference}
                yield Session()
    handler=PartReplayWorkerHandler(f.s,f.db,service=f.publish,browser=OwnedBrowser(),cover_provider=lambda j,b,a:cover(f,a))
    worker=Worker(f.s,QueueView(f.db,publish_enabled=True,acquisition_enabled=True),{PART_JOB_TYPES[0]:handler.process})
    failures=[]
    def run():
        try:worker.once('video')
        except BaseException as exc:failures.append(exc)
    thread=threading.Thread(target=run);thread.start();assert ready.wait(10)
    first=f.db.get(f.child);time.sleep(1.7);current=f.db.get(f.child)
    assert current['state']=='processing' and current['attempts']==1 and current['lease_owner']==first['lease_owner']
    assert current['lease_until']>first['lease_until']
    before=len(f.db.query('SELECT * FROM jobs'));f.q.register_all_observed();f.q.sync_states(publication_projection=f.groups.fifo_projection)
    assert len(f.db.query('SELECT * FROM jobs'))==before
    release.set();thread.join(15);assert not thread.is_alive() and not failures
    assert calls==['upload','prepare','submit'] and f.calls==['owned_track_copy_not_youtube']
    job=f.db.get(f.child);assert job['attempts']==1 and len(f.db.query('SELECT * FROM publications'))==1
    if pending:
        assert job['state']=='publish_unknown' and job['result'][STATE]['state']=='submit_unknown'
        assert schedule_tick(f.s,f.db)['scheduled']
        assert not worker.once('video') and calls==['upload','prepare','submit']
    else:
        assert job['state']=='completed' and job['error_code'] is None and f.db.get(f.parent)['state']=='completed'
