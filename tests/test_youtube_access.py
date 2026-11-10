"""Synthetic jobs, real SQLite, owned HTTP responses; no external access."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import format_datetime
import json
import sqlite3
import subprocess
import sys
import time

import pytest

from services.hub.db import Store
from services.hub.errors import HubError, LeaseLost
from services.hub.youtube_access import AcquisitionAccess, retry_after_seconds
from services.hub.replay_multipart_worker_runtime_v4 import QueueView
from services.hub.replay_fifo_v4 import PLAN_JOB, DOWNLOAD_JOB, VERSION
from services.hub.worker import Worker


@pytest.fixture
def access(runtime):
    settings, store = runtime
    clock = [time.time()]
    gate = AcquisitionAccess(settings, store, clock=lambda: clock[0], sleep=lambda n: clock.__setitem__(0,clock[0]+n))
    return settings,store,clock,gate


def enqueue(store, ident='one', kind=PLAN_JOB):
    return store.enqueue('youtube', ident, kind, 'video', {'synthetic':True}, VERSION)


@pytest.mark.parametrize('status,code', [(401,'YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED'),(403,'YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED'),(429,'YOUTUBE_DOWNLOAD_RATE_LIMITED')])
def test_rejection_is_durable_and_blocks_other_jobs_without_attempts(access,status,code):
    s,db,clock,gate=access
    first=enqueue(db);second=enqueue(db,'two')
    view=QueueView(db,acquisition_enabled=True,access_gate=gate)
    worker=Worker(s,view,{PLAN_JOB:lambda job:gate.run(job,lambda j:gate.response(status,'7200',j['job_id']))})
    assert worker.once('video')
    assert db.get(first)['state']=='awaiting_review' and db.get(first)['error_code']==code
    restarted=AcquisitionAccess(s,Store(db.path))
    new_view=QueueView(Store(db.path),acquisition_enabled=True,access_gate=restarted)
    assert new_view.claim('video',180,4) is None
    assert db.get(second)['attempts']==0 and db.get(second)['state']=='pending'
    db.init()  # idempotent migration must never erase a trip
    assert restarted.status()['manual_resume_required']


def test_retry_after_date_and_seconds_survive_restart_and_cannot_resume_early(access):
    s,db,clock,gate=access
    stamp=format_datetime(datetime.fromtimestamp(clock[0]+7200,timezone.utc),usegmt=True)
    assert retry_after_seconds(stamp,clock[0]) >=7199
    gate.trip('YOUTUBE_DOWNLOAD_RATE_LIMITED','owned',retry_after='7200')
    before=gate.status();clock[0]+=100
    gate.trip('YOUTUBE_DOWNLOAD_RATE_LIMITED','owned')
    assert gate.status()=={**before,'claim_allowed':False}
    with pytest.raises(HubError,match='COOLDOWN_ACTIVE'):gate.resume('maintenance',before['revision'])
    clock[0]+=7201
    assert gate.status()['manual_resume_required']  # cooldown expiry is not approval
    with pytest.raises(HubError,match='ACCESS_DENIED'):gate.resume('reader',before['revision'])
    with pytest.raises(HubError,match='STATE_CHANGED'):gate.resume('maintenance',before['revision']-1)
    assert gate.resume('maintenance',before['revision'])['claim_allowed']
    assert db.query("SELECT * FROM audit WHERE action='resume_acquisition'")


@pytest.mark.parametrize('header',[None,'-1','garbage','NaN','inf','9'*129])
def test_invalid_retry_after_uses_conservative_fallback(access,header):
    _,_,clock,gate=access
    gate.trip('YOUTUBE_DOWNLOAD_RATE_LIMITED','owned',retry_after=header)
    assert gate.status()['not_before']==clock[0]+3600


def test_request_spacing_is_shared_and_waits_outside_transaction(access):
    s,db,clock,gate=access
    gate.before_request(media=False,check=lambda:None)
    first=clock[0]
    def sleep(n):
        with db.transaction() as conn:conn.execute('SELECT 1')
        clock[0]+=n
    other=AcquisitionAccess(s,Store(db.path),clock=lambda:clock[0],sleep=sleep)
    other.before_request(media=False,check=lambda:None)
    assert clock[0]>=first+1
    gate.trip('YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED','owned')
    with pytest.raises(HubError,match='ACCESS_RESTRICTED'):other.before_request(media=True,check=lambda:None)


def test_cancellation_is_checked_during_pacing(access):
    _,_,_,gate=access
    gate.before_request(media=False,check=lambda:None)
    def lost():raise LeaseLost()
    with pytest.raises(LeaseLost):gate.before_request(media=False,check=lost)


def test_media_request_cannot_shorten_metadata_interval(access):
    _,_,clock,gate=access
    start=clock[0]
    gate.before_request(media=False,check=lambda:None)
    gate.before_request(media=True,check=lambda:None)
    assert clock[0]>=start+0.25
    gate.before_request(media=False,check=lambda:None)
    assert clock[0]>=start+1


def test_real_sqlite_serializes_two_pacing_clients(runtime):
    s,db=runtime;s.config['youtube_acquisition_access']={'metadata_interval_seconds':0.5}
    def request(_):
        AcquisitionAccess(s,Store(db.path)).before_request(media=False,check=lambda:None)
        return time.time()
    with ThreadPoolExecutor(max_workers=2) as pool:times=sorted(pool.map(request,range(2)))
    assert times[1]-times[0]>=0.45


def test_network_attempts_and_between_download_delay_persist(access):
    s,db,clock,gate=access
    ident=enqueue(db,kind=DOWNLOAD_JOB)
    def network(job):raise HubError('YOUTUBE_DOWNLOAD_NETWORK_ERROR',retryable=True)
    for attempt in (1,2,3):
        job={'job_id':ident,'job_type':DOWNLOAD_JOB,'attempts':attempt}
        with pytest.raises(HubError) as err:gate.run(job,network)
        assert err.value.retryable is (attempt<3)
        assert err.value.retry_after==60*2**(attempt-1)
        assert gate.status()['next_job_at']==clock[0]+err.value.retry_after
    assert not AcquisitionAccess(s,Store(db.path)).status()['claim_allowed']
    assert db.get(ident)['attempts']==0  # no direct task mutation by the gate


def test_successful_download_spaces_next_job_and_does_not_touch_publications(access):
    s,db,clock,gate=access
    job={'job_id':enqueue(db,kind=DOWNLOAD_JOB),'job_type':DOWNLOAD_JOB,'attempts':1}
    assert gate.run(job,lambda _: {'verified':True})=={'verified':True}
    assert gate.status()['next_job_at']==clock[0]+60
    assert not gate.status()['claim_allowed'] and not db.query('SELECT * FROM publications')


def test_database_backup_restores_pause_and_pending_jobs(access,tmp_path):
    s,db,_,gate=access
    ident=enqueue(db);gate.trip('YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED',ident)
    restored=tmp_path/'restored.sqlite3'
    src=db.connect();dst=sqlite3.connect(restored)
    try:src.backup(dst)
    finally:src.close();dst.close()
    copied=Store(restored);copied.init()
    assert AcquisitionAccess(s,copied).status()['manual_resume_required']
    assert copied.get(ident)==db.get(ident)


def test_child_abrupt_exit_keeps_pause_before_result_file_can_be_written(runtime):
    s,db=runtime;ident=enqueue(db)
    code="""import os,sys
from pathlib import Path
from services.hub.config import Settings
from services.hub.db import Store
from services.hub.youtube_access import AcquisitionAccess
gate=AcquisitionAccess(Settings(Path(sys.argv[1]),{}),Store(sys.argv[2]))
gate.trip('YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED',sys.argv[3])
os._exit(23)
"""
    result=subprocess.run([sys.executable,'-c',code,str(s.root),str(db.path),ident],capture_output=True,timeout=15)
    assert result.returncode==23
    gate=AcquisitionAccess(s,Store(db.path))
    assert gate.status()['manual_resume_required']
    assert QueueView(db,acquisition_enabled=True,access_gate=gate).claim('video',180,4) is None
    assert db.get(ident)['attempts']==0


@pytest.mark.parametrize('value',[{'network_max_attempts':True},{'metadata_interval_seconds':0},{'media_interval_seconds':float('nan')},{'disable':True}])
def test_invalid_config_cannot_disable_safety(runtime,value):
    s,db=runtime;s.config['youtube_acquisition_access']=value
    with pytest.raises(HubError,match='CONFIG_INVALID'):AcquisitionAccess(s,db)


def test_coordinator_pauses_dispatch_but_keeps_registration_and_health(fifo):
    from test_youtube_download_v4 import configure_v4
    from test_replay_fifo_v4 import seed
    from services.hub.replay_multipart_worker_runtime_v4 import build_worker
    f=configure_v4(fifo);seed(f,1)
    worker,coordinator=build_worker(f.s,f.db,mode='acquisition_only')
    coordinator.access.trip('YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED','owned')
    result=coordinator.tick()
    assert result['dispatch_state']=='access_paused'
    assert len(result['steps']['register']['registered'])==1
    assert not f.db.query('SELECT * FROM jobs') and not worker.once('video')
    assert f.db.query("SELECT * FROM heartbeats WHERE name='worker:replay-acquisition-producer'")


def test_preflight_reports_missing_migration_and_pause_without_writing(fifo):
    from test_youtube_download_v4 import configure_v4
    from services.hub.acquisition_preflight import inspect
    f=configure_v4(fifo)
    gate=AcquisitionAccess(f.s,f.db);gate.trip('YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED','owned')
    before=f.db.query('SELECT * FROM youtube_acquisition_access')
    assert 'YOUTUBE_ACCESS_MANUAL_RESUME_REQUIRED' in inspect(f.s)['blockers']
    assert f.db.query('SELECT * FROM youtube_acquisition_access')==before
    with f.db.transaction() as db:db.execute('DROP TABLE youtube_acquisition_access')
    assert 'YOUTUBE_ACCESS_MIGRATION_REQUIRED' in inspect(f.s)['blockers']


# Import existing owned transport fixtures, with all sockets prohibited.
from test_replay_fifo_v4 import fifo
from test_ytdlp_highest_transport_v2 import transport, owned_loop
from test_youtube_highest_v2 import highest


@pytest.mark.parametrize('status',[401,403,429])
def test_v4_transport_stops_before_second_request_and_after_restart(transport,monkeypatch,status):
    from adapters import ytdlp_highest_v2 as engine
    f=transport
    ticket=json.loads(f.ticket.read_bytes());ticket['binding']['request_mode']='fifo-v4'
    engine.atomic_json(f.ticket,ticket)
    # This test exercises the shared engine's V4 transport branch; source
    # identity validation has separate full V4 tests and is an owned stub here.
    monkeypatch.setattr(engine.YouTubeHighestDownload,'check_binding',lambda *a:None)
    from test_ytdlp_replay import OwnedHTTPResponse
    original=OwnedHTTPResponse.getheader
    monkeypatch.setattr(OwnedHTTPResponse,'getheader',lambda self,name,default=None: '7200' if name.lower()=='retry-after' else original(self,name,default))
    owned_loop(f,monkeypatch,3,status=status)
    assert engine.child_download(f.ticket)==1 and len(f.calls)==1
    assert AcquisitionAccess(f.s,f.db).status()['manual_resume_required']
    if status==429:assert AcquisitionAccess(f.s,f.db).status()['not_before']>=time.time()+7190
    assert engine.child_download(f.ticket)==1 and len(f.calls)==1
    assert not f.media_calls and not f.sockets
