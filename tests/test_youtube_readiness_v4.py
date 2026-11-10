"""Synthetic extractor responses, genuine SQLite leases/ledger; no HTTP/media."""
from copy import deepcopy
from datetime import datetime,timezone
import json,time
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.ytdlp_whole_v4 import observation_record,planning_folder
from services.hub.budget import Budget
from services.hub.errors import HubError
from services.hub.media_processing import atomic_json
from services.hub.replay_multipart_v4 import digest,plan_whole_replay
from services.hub.replay_fifo_v4 import PLAN_JOB,DOWNLOAD_JOB
from services.hub.worker import Worker
from services.hub.youtube_download_v4 import YouTubeWholeReplayPrepare,VERSION
from services.hub.youtube_readiness_v4 import (WholeReadinessCoordinatorV4,WholeReadinessObservationV4,WAIT,JOB_TYPE,
    read_observation,read_failed_observation)
from test_replay_fifo_v4 import fifo,seed
from test_youtube_download_v4 import configure_v4


@pytest.fixture
def readiness(fifo):
    f=configure_v4(fifo);raw,info=seed(f,1);f.raw=raw;f.info=info;f.ready=False;f.code=None;f.tamper=False;f.count=0
    f.q.register_all_observed();f.parent_id=f.q.enqueue_plan_tick(f.handlers)['created'][0]
    f.coordinator=WholeReadinessCoordinatorV4(f.s,f.db,clock=lambda:f.clock[0])
    class OwnedMetadata:
        def acquire(self,job,binding,*,check):
            check()
            if f.code:raise HubError(f.code,review=True)
            data=deepcopy(info)
            if not f.ready:data['live_status']='post_live'
            # Charge the owned transport observation in the genuine same ledger.
            # This is explicitly a test double, not an assertion of a live request.
            from adapters.ytdlp_whole_v4 import budget_limits
            budget=Budget(f.db,budget_limits(binding));call=budget.reserve('youtube_download_http',job['job_id']);budget.finish(call,'done')
            area=planning_folder(f.s,job['job_id']);ref=observation_record(data,binding,area,{'requests':1,'owned_transport_fixture':True})
            f.count+=1
            if not f.ready:
                atomic_json(area/'download-result.json',{'error_code':'YOUTUBE_NATURAL_POST_LIVE_NOT_READY','metadata_observation':ref})
                if f.tamper:
                    file=Path(ref['file']);body=json.loads(file.read_bytes());body['extractor_info']['duration']+=1;atomic_json(file,body)
                raise HubError('YOUTUBE_NATURAL_POST_LIVE_NOT_READY',review=True)
            plan=plan_whole_replay(raw,data,observed_at=datetime.now(timezone.utc).isoformat(),policy=f.whole)
            return {'version':VERSION,'binding_sha256':digest(binding),'source_plan':plan,'metadata_observation':ref,
                'media_downloaded':False,'human_review':False,'publication_authorized':False}
    f.factory=lambda _:OwnedMetadata()
    f.parent_service=YouTubeWholeReplayPrepare(f.s,f.db,downloader_factory=f.factory)
    f.parent_worker=Worker(f.s,f.db,{PLAN_JOB:f.parent_service.process},readiness_deferrals=f.coordinator.callbacks())
    return f


def begin(f):
    before=f.db.get(f.parent_id)['payload'];assert f.parent_worker.once('video')
    f.coordinator.initialize();parent=f.db.get(f.parent_id)
    assert parent['payload']==before
    return parent


def advance(f,monkeypatch,seconds=901):
    f.clock[0]+=seconds
    import services.hub.db as database
    monkeypatch.setattr(database,'time',SimpleNamespace(time=lambda:f.clock[0]))
    # QueueView issues leases too; all issuance and fencing must share the same
    # simulated clock. Previously finish() did not reject already-expired leases.
    import services.hub.replay_multipart_worker_runtime_v4 as runtime
    monkeypatch.setattr(runtime,'time',SimpleNamespace(time=lambda:f.clock[0]))


def test_proven_post_live_moves_parent_to_persistent24h_wait_without_burning_parent_attempts(readiness):
    f=readiness;parent=begin(f);wait=f.db.query('SELECT * FROM youtube_readiness_waits_v4')[0]
    assert parent['state']=='retry_scheduled' and parent['attempts']==1 and parent['lease_owner'] is None
    assert parent['error_code']=='WAITING_YOUTUBE_READINESS' and wait['max_observations']==96 and wait['observations']==1
    assert wait['deadline']-wait['created_at']==86400 and wait['next_check']-wait['created_at']==900
    assert f.count==1 and not f.db.query('SELECT * FROM replay_fifo_dispatches_v4') and not f.db.query('SELECT * FROM publications')
    assert f.coordinator.tick()['enqueued']==[] and f.count==1


def test_local_poller_enqueues_only_one_numbered_observation_without_http(readiness,monkeypatch):
    f=readiness;begin(f);advance(f,monkeypatch)
    made=f.coordinator.tick()['enqueued'];assert len(made)==1
    child=f.db.get(made[0]);assert child['job_type']==JOB_TYPE and child['source_id']==f.raw['id']+':readiness:2'
    assert f.coordinator.tick()['enqueued']==[] and f.count==1
    assert f.db.get(f.parent_id)['attempts']==1 and child['attempts']==0


def test_ready_child_completes_original_plan_only_and_preserves_daily_dispatch_gate(readiness,monkeypatch):
    f=readiness;begin(f);advance(f,monkeypatch);made=f.coordinator.tick()['enqueued'];f.ready=True
    service=WholeReadinessObservationV4(f.s,f.db,coordinator=f.coordinator,downloader_factory=f.factory)
    assert Worker(f.s,f.db,{JOB_TYPE:service.process}).once('video')
    parent=f.db.get(f.parent_id);child=f.db.get(made[0]);wait=f.db.query('SELECT * FROM youtube_readiness_waits_v4')[0]
    assert parent['state']==child['state']=='completed' and parent['attempts']==child['attempts']==1
    assert wait['state']=='ready' and parent['result']['replay_fifo_plan_v4']['state']=='planned'
    assert parent['result'][WAIT]['observations']==2 and f.count==2
    assert not f.db.query('SELECT * FROM jobs WHERE job_type=?',(DOWNLOAD_JOB,)) and not f.db.query('SELECT * FROM replay_fifo_dispatches_v4')
    assert not f.db.query('SELECT * FROM publications')


def test_not_ready_child_finishes_own_lease_and_schedules_next_observation_parent_unchanged(readiness,monkeypatch):
    f=readiness;begin(f);advance(f,monkeypatch);made=f.coordinator.tick()['enqueued']
    service=WholeReadinessObservationV4(f.s,f.db,coordinator=f.coordinator,downloader_factory=f.factory)
    assert Worker(f.s,f.db,{JOB_TYPE:service.process}).once('video')
    parent=f.db.get(f.parent_id);child=f.db.get(made[0]);wait=f.db.query('SELECT * FROM youtube_readiness_waits_v4')[0]
    assert parent['state']=='retry_scheduled' and parent['attempts']==1 and child['state']=='completed' and child['attempts']==1
    assert wait['active_child_id'] is None and wait['observations']==2 and wait['next_check']==f.clock[0]+900
    assert len(json.loads(wait['record'])['evidence'])==2 and f.count==2


def test_budget_pause_makes_no_child_or_http_and_retains_real_charged_history(readiness,monkeypatch):
    f=readiness;begin(f);advance(f,monkeypatch)
    from adapters.ytdlp_whole_v4 import budget_limits
    parent=f.db.get(f.parent_id);binding=f.parent_service.resource_binding(parent)
    b=Budget(f.db,budget_limits(binding));call=b.reserve('youtube_download_http',parent['job_id'],units=5999);b.finish(call,'done')
    before=f.db.query('SELECT * FROM api_calls');out=f.coordinator.tick();after=f.db.query('SELECT * FROM api_calls')
    assert out['enqueued']==[] and out['held']==[] and before==after and f.count==1
    assert f.db.get(f.parent_id)['attempts']==1 and f.db.query('SELECT observations FROM youtube_readiness_waits_v4')[0]['observations']==1


def test_deadline_becomes_durable_technical_hold_and_event_without_reset_or_new_sourcejob(readiness,monkeypatch):
    f=readiness;begin(f);before=f.db.query('SELECT * FROM api_calls');advance(f,monkeypatch,86401)
    out=f.coordinator.tick();parent=f.db.get(f.parent_id)
    assert len(out['held'])==1 and out['enqueued']==[] and parent['state']=='upload_pending' and parent['attempts']==1
    assert parent['error_code']=='YOUTUBE_WHOLE_READINESS_TIMEOUT' and f.q.candidate(parent['payload']['fifo_v4']['candidate_id'])['state']=='hold'
    assert f.db.query('SELECT * FROM api_calls')==before and f.count==1
    assert f.db.query("SELECT * FROM events WHERE name='task.failed'")
    assert f.coordinator.tick()['held']==[]


@pytest.mark.parametrize('change',['tampered_raw','authentication'])
def test_forged_or_auth_failure_cannot_be_relabelled_as_transcoding(readiness,change):
    f=readiness
    if change=='tampered_raw':f.tamper=True
    else:f.code='YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED'
    assert f.parent_worker.once('video');f.coordinator.initialize();parent=f.db.get(f.parent_id)
    assert parent['state'] in ('failed','awaiting_review') and parent['attempts']==1
    assert not f.db.query('SELECT * FROM youtube_readiness_waits_v4') and not f.db.query('SELECT * FROM jobs WHERE job_type=?',(JOB_TYPE,))


def test_observation_size_mismatch_is_rejected_before_opening_raw_content(readiness,monkeypatch):
    f=readiness;parent=begin(f);reference=parent['result'][WAIT]['evidence'][0]['observation']
    file=Path(reference['file']);file.write_bytes(file.read_bytes()+b' owned mismatch')
    import services.hub.youtube_readiness_v4 as module
    original=module.os.open;opened=[]
    def tracked(path,*args,**kwargs):
        if Path(path)==file:opened.append(path)
        return original(path,*args,**kwargs)
    monkeypatch.setattr(module.os,'open',tracked)
    with pytest.raises(HubError,match='YOUTUBE_WHOLE_READINESS_EVIDENCE_INVALID'):
        read_observation(f.s,f.db,parent,f.parent_service.resource_binding(parent),reference,reason='YOUTUBE_NATURAL_POST_LIVE_NOT_READY')
    assert opened==[]
