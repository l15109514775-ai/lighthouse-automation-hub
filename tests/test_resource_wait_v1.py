"""Real owned SQLite/Worker leases; local disk observations, no HTTP or video."""
from copy import deepcopy
from types import SimpleNamespace
import hashlib,time
import pytest

from services.hub.errors import HubError,ResourceDeferred,PublishUnknown
from services.hub.resource_wait_v1 import ResourceWaitCoordinator,DEADLINE,MAX_POLLS,INTERVAL,digest
from services.hub.worker import Worker

KIND='youtube_download_v3'

@pytest.fixture
def waiting(runtime,monkeypatch):
    s,db=runtime;clock=SimpleNamespace(value=time.time());disk=SimpleNamespace(free=35_000_000_000)
    monkeypatch.setattr('services.hub.db.time.time',lambda:clock.value)
    coordinator=ResourceWaitCoordinator(s,db,validators={KIND:lambda job:digest(job['payload'])},clock=lambda:clock.value,disk_free=lambda:disk.free)
    ident=db.enqueue('youtube','ownedWait01',KIND,'video',{'owned':'same exact binding'},'owned-v3-policy')
    job=db.claim('video',600,4)
    return SimpleNamespace(s=s,db=db,clock=clock,disk=disk,coordinator=coordinator,job=job,ident=ident)

def defer(f,required=21_939_609_978):
    return f.coordinator.defer(f.job,ResourceDeferred(resource='disk',required_bytes=required,binding_sha256=digest(f.job['payload'])))

def test_waiting_poll_is_persistent_and_never_claims_or_resets_attempt(waiting,monkeypatch):
    f=waiting;record=defer(f);before=f.db.get(f.ident)
    assert before['state']=='retry_scheduled' and before['stage']=='waiting_resource' and before['attempts']==1
    assert before['next_run']==record['deadline']+60 and before['lease_owner'] is None
    monkeypatch.setattr(f.db,'claim',lambda *args:pytest.fail('Resource polling must never claim a job'))
    for _ in range(10):
        f.clock.value+=INTERVAL;result=f.coordinator.tick();assert result['resumed']==[] and result['external_calls']==0
    after=f.db.get(f.ident);assert after['attempts']==1 and after['payload']==before['payload'] and after['state']=='retry_scheduled'
    assert after['result']['resource_wait_v1']['polls_total']==10 and not f.db.query('SELECT * FROM api_calls')

def test_space_release_resumes_same_job_with_new_lease_and_preserved_partial(waiting):
    f=waiting;area=f.s.root/'media'/f.ident;area.mkdir();part=area/'owned.part';part.write_bytes(b'Owned retained partial')
    before=hashlib.sha256(part.read_bytes()).hexdigest();defer(f);f.clock.value+=INTERVAL;f.disk.free=57_000_000_000
    report=f.coordinator.tick();assert report['resumed']==[f.ident] and f.db.get(f.ident)['attempts']==1
    worker=Worker(f.s,f.db,{KIND:lambda job:{'owned_resumed':True,'attempt_seen':job['attempts']}},resource_deferrals=f.coordinator.callbacks())
    assert worker.once('video') is True;job=f.db.get(f.ident)
    assert job['state']=='completed' and job['attempts']==2 and job['result']['attempt_seen']==2
    assert hashlib.sha256(part.read_bytes()).hexdigest()==before and not f.db.query('SELECT * FROM api_calls')
    f.coordinator.tick();assert f.db.query('SELECT state FROM resource_waits_v1')[0]['state']=='completed'
    assert f.db.query("SELECT COUNT(*) n FROM audit WHERE action='resource_wait_resume'")[0]['n']==1

def test_24_hour_timeout_fails_without_another_claim_or_attempt(waiting):
    f=waiting;record=defer(f);f.clock.value=record['deadline'];report=f.coordinator.tick()
    assert report['failed'][0]['error_code']=='RESOURCE_WAIT_TIMEOUT'
    assert f.db.get(f.ident)['state']=='failed' and f.db.get(f.ident)['attempts']==1
    assert not f.db.query('SELECT * FROM api_calls')
    assert f.coordinator.tick()['checked']==0

def test_poll_ceiling_remains_finite_even_if_due_timestamp_is_corrupt(waiting):
    f=waiting;defer(f)
    with f.db.transaction() as db:db.execute('UPDATE resource_waits_v1 SET polls_total=?,next_check=?',(MAX_POLLS,f.clock.value))
    report=f.coordinator.tick();assert report['failed'][0]['error_code']=='RESOURCE_WAIT_TIMEOUT'
    assert f.db.get(f.ident)['attempts']==1

@pytest.mark.parametrize('change',['payload','policy','validator'])
def test_binding_changes_stop_resource_resume_without_external_call(waiting,change):
    from services.hub.db import encode
    f=waiting;defer(f)
    if change=='payload':
        with f.db.transaction() as db:db.execute('UPDATE jobs SET payload=? WHERE job_id=?',(encode({'changed':True}),f.ident))
    if change=='policy':
        with f.db.transaction() as db:db.execute('UPDATE jobs SET policy_version=? WHERE job_id=?',('other-policy',f.ident))
    if change=='validator':f.coordinator.validators[KIND]=lambda job:'f'*64
    f.clock.value+=INTERVAL;f.disk.free=60_000_000_000;report=f.coordinator.tick()
    assert report['failed'] and f.db.get(f.ident)['state']=='failed' and f.db.get(f.ident)['attempts']==1
    assert not f.db.query('SELECT * FROM api_calls')

def test_busy_video_queue_does_not_resume_another_large_allocation(waiting):
    f=waiting;defer(f);f.disk.free=60_000_000_000
    f.db.enqueue('youtube','ownedOther1','video_process','video',{'metadata_only':True},'owned')
    f.clock.value+=INTERVAL;report=f.coordinator.tick();assert report['resumed']==[]
    assert f.db.get(f.ident)['result']['resource_wait_v1']['last_reason']=='video_queue_busy'

def test_paused_pipeline_is_never_unpaused_by_resource_monitor(waiting):
    f=waiting;defer(f);f.disk.free=60_000_000_000
    with f.db.transaction() as db:db.execute("UPDATE pipelines SET paused=1 WHERE name='video'")
    f.clock.value+=INTERVAL;assert f.coordinator.tick()['resumed']==[]
    assert f.db.query("SELECT paused FROM pipelines WHERE name='video'")[0]['paused']==1

def test_repeated_low_space_preserves_first_deadline_polls_and_attempt_history(waiting):
    f=waiting;first=defer(f);f.clock.value+=INTERVAL;f.disk.free=60_000_000_000;f.coordinator.tick()
    f.job=f.db.claim('video',600,4);assert f.job['attempts']==2;f.disk.free=20_000_000_000
    second=defer(f);assert second['wait_id']==first['wait_id'] and second['deadline']==first['deadline']
    assert second['polls_total']==1 and second['resumptions']==1 and f.db.get(f.ident)['attempts']==2

def test_worker_opt_in_deferral_does_not_burn_attempts_on_five_minute_polls(waiting):
    f=waiting
    # Return the initially claimed owned fixture to its initial pending state;
    # this simulates fresh Worker ownership and does not invoke any live retry API.
    with f.db.transaction() as db:db.execute("UPDATE jobs SET state='pending',attempts=0,lease_owner=NULL,lease_until=NULL WHERE job_id=?",(f.ident,))
    def handler(job):raise ResourceDeferred(resource='disk',required_bytes=22_000_000_000,binding_sha256=digest(job['payload']))
    worker=Worker(f.s,f.db,{KIND:handler},resource_deferrals=f.coordinator.callbacks())
    assert worker.once('video') is True and f.db.get(f.ident)['attempts']==1
    for _ in range(3):
        f.clock.value+=INTERVAL;f.coordinator.tick();assert worker.once('video') is False
    assert f.db.get(f.ident)['attempts']==1 and f.db.get(f.ident)['state']=='retry_scheduled'

def test_unregistered_routes_keep_original_worker_failure_behavior(waiting):
    f=waiting
    with f.db.transaction() as db:db.execute("UPDATE jobs SET state='pending',attempts=0,lease_owner=NULL,lease_until=NULL WHERE job_id=?",(f.ident,))
    def handler(job):raise ResourceDeferred(resource='disk',required_bytes=1,binding_sha256=digest(job['payload']))
    assert Worker(f.s,f.db,{KIND:handler}).once('video') is True
    assert f.db.get(f.ident)['state']=='failed' and not f.db.query("SELECT name FROM sqlite_master WHERE name='resource_waits_v1'")

@pytest.mark.parametrize('kind',['youtube_download','youtube_download_v2','tweet_process'])
def test_old_types_cannot_register_resource_deferral(waiting,kind):
    with pytest.raises(ValueError):ResourceWaitCoordinator(waiting.s,waiting.db,validators={kind:lambda job:digest(job['payload'])})


def publication(f,state,external=None):
    with f.db.transaction() as db:
        db.execute('''INSERT INTO publications(idempotency_key,job_id,platform,source_platform,source_id,account_id,kind,state,external_id,updated_at)
            VALUES(?,?,'bilibili','youtube','ownedWait01','site_uid:owned','owned_v3',?,?,?)''',
            ('owned-key',f.ident,state,external,f.clock.value))

@pytest.mark.parametrize('state,external',[('dispatching',None),('unknown',None),('completed','BV0000000001'),('rejected','BV0000000001')])
def test_defer_never_turns_prior_send_or_external_id_into_a_resource_retry(waiting,state,external):
    f=waiting;publication(f,state,external)
    with pytest.raises(PublishUnknown):defer(f)
    assert f.db.get(f.ident)['state']=='processing' and not f.db.query('SELECT * FROM resource_waits_v1')
    assert f.db.query('SELECT state,external_id FROM publications')[0]=={'state':state,'external_id':external}

def test_new_dispatch_while_waiting_becomes_lookup_only_without_claim(waiting):
    f=waiting;defer(f);publication(f,'unknown');f.disk.free=60_000_000_000;f.clock.value+=INTERVAL
    report=f.coordinator.tick()
    assert report['resumed']==[] and report['failed'][0]['error_code']=='RECONCILE_BEFORE_RESOURCE_RESUME'
    assert f.db.get(f.ident)['state']=='publish_unknown' and f.db.get(f.ident)['attempts']==1

def test_worker_deferral_failure_preserves_a_fresh_checkpoint(waiting):
    f=waiting
    with f.db.transaction() as db:db.execute("UPDATE jobs SET state='pending',attempts=0,lease_owner=NULL,lease_until=NULL WHERE job_id=?",(f.ident,))
    def handler(job):
        with f.db.transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',('{"owned_checkpoint":true}',job['job_id']))
        raise ResourceDeferred(resource='disk',required_bytes=1,binding_sha256=digest(job['payload']))
    def reject(job,error):raise HubError('RESOURCE_WAIT_BINDING_CHANGED')
    assert Worker(f.s,f.db,{KIND:handler},resource_deferrals={KIND:reject}).once('video') is True
    assert f.db.get(f.ident)['result']=={'owned_checkpoint':True}

def test_configured_72h_wait_keeps_finite_total_and_daily_poll_boundaries(waiting):
    f=waiting;f.s.config['resource_wait_max_hours']=72
    f.coordinator=ResourceWaitCoordinator(f.s,f.db,validators={KIND:lambda job:digest(job['payload'])},clock=lambda:f.clock.value,disk_free=lambda:f.disk.free)
    start=f.clock.value;record=defer(f);assert record['deadline']==start+72*3600
    f.clock.value+=25*3600
    assert f.coordinator.tick()['failed']==[] and f.db.get(f.ident)['attempts']==1
    assert f.coordinator.max_polls==864
    f.clock.value=record['deadline'];assert f.coordinator.tick()['failed'][0]['error_code']=='RESOURCE_WAIT_TIMEOUT'

@pytest.mark.parametrize('value',[0,23,73,True,24.0,'72'])
def test_unbounded_or_noninteger_wait_configuration_is_rejected(waiting,value):
    f=waiting;f.s.config['resource_wait_max_hours']=value
    with pytest.raises(ValueError):ResourceWaitCoordinator(f.s,f.db,validators={KIND:lambda job:digest(job['payload'])})

def test_temporary_disk_stat_error_stays_bounded_without_claiming_or_killing_worker(waiting):
    f=waiting;defer(f);f.clock.value+=INTERVAL
    def unavailable():raise OSError('Owned transient stat failure')
    f.coordinator.disk_free=unavailable
    result=f.coordinator.tick();job=f.db.get(f.ident)
    assert result['failed']==[] and job['attempts']==1 and job['result']['resource_wait_v1']['last_reason']=='disk_observation_unavailable'
    assert job['result']['resource_wait_v1']['polls_total']==1 and not f.db.query('SELECT * FROM api_calls')

def test_daily_counter_does_not_bypass_total_72h_limit_across_midnight(waiting):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    f=waiting;f.s.config['resource_wait_max_hours']=72
    f.coordinator=ResourceWaitCoordinator(f.s,f.db,validators={KIND:lambda job:digest(job['payload'])},clock=lambda:f.clock.value,disk_free=lambda:f.disk.free)
    record=defer(f);f.clock.value+=25*3600
    with f.db.transaction() as db:db.execute('UPDATE resource_waits_v1 SET polls_total=288,polls_today=288,next_check=?',(f.clock.value,))
    assert f.coordinator.tick()['failed']==[]
    saved=f.db.query('SELECT * FROM resource_waits_v1')[0]
    assert saved['polls_total']==289 and saved['polls_today']==1 and saved['deadline']==record['deadline']
    assert saved['poll_day']==datetime.fromtimestamp(f.clock.value,ZoneInfo('Asia/Shanghai')).date().isoformat()
