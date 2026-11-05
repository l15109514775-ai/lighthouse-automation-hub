"""Real SQLite/Worker, owned raw metadata and charged fake HTTP, no sockets."""
from copy import deepcopy
from datetime import datetime,timezone
from types import SimpleNamespace
import json,time
import pytest

from services.hub.budget import Budget
from services.hub.errors import HubError,YouTubeReadinessDeferred
from services.hub.worker import Worker
from services.hub.youtube_download_v3 import (YouTubeNaturalPrepare,YouTubeNaturalDownload,
    PREPARE_JOB_TYPE,PREPARE_STATE,JOB_TYPE as DOWNLOAD,VERSION,digest)
from services.hub.youtube_natural_plan_v3 import natural_source_plan
from services.hub.youtube_readiness_v3 import (YouTubeReadinessCoordinator,ReadinessObservation,
    JOB_TYPE,WAIT,INTERVAL,verify_observation)
from adapters.ytdlp_natural_v3 import planning_folder,natural_budget_limits
from adapters.youtube_readiness_evidence_v3 import record,read
from services.hub.media_processing import atomic_json
from test_youtube_download_v3 import natural
from test_ytdlp_natural_v3 import transport,owned_extract


def factory(f):
    class OwnedMetadata:
        def __init__(self,s):pass
        def acquire(self,job,binding,*,check):
            check();f.calls.append(job['job_id']);info=deepcopy(f.info)
            budget=Budget(f.db,natural_budget_limits(binding));key=budget.reserve('youtube_download_http',job['job_id']);budget.finish(key,'done')
            stats={'requests':1,'response_bytes':len(json.dumps(info)),'provider':'youtube_download_http'}
            area=planning_folder(f.s,job['job_id']);reference=record(info,binding,area,stats)
            observed=datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
            try:plan=natural_source_plan(binding['official_raw'],info,activated_at=binding['activated_at'],observed_at=observed)
            except HubError as exc:
                atomic_json(area/'download-result.json',{'error_code':exc.code,'metadata_observation':reference,'http':stats});raise
            return {'version':VERSION,'binding_sha256':digest(binding),'source_plan':plan,'metadata_observation':reference,
                'http':stats,'media_downloaded':False,'human_review':False,'publication_authorized':False}
    return OwnedMetadata


@pytest.fixture
def ready(natural):
    f=natural;f.clock=SimpleNamespace(value=time.time());f.calls=[];f.info['live_status']='post_live'
    f.ident=f.service.enqueue_if_eligible(f.payload)
    f.prepare=YouTubeNaturalPrepare(f.s,f.db,downloader_factory=factory(f))
    f.observer=ReadinessObservation(f.s,f.db,downloader_factory=factory(f))
    f.coordinator=YouTubeReadinessCoordinator(f.s,f.db,clock=lambda:f.clock.value)
    f.worker=Worker(f.s,f.db,{PREPARE_JOB_TYPE:f.prepare.process,JOB_TYPE:f.observer.process},readiness_deferrals=f.coordinator.callbacks())
    return f


def begin(f):
    assert f.worker.once('video') is True
    parent=f.db.get(f.ident)
    assert parent['state']=='retry_scheduled' and parent['error_code']=='WAITING_YOUTUBE_READINESS' and parent['attempts']==1
    return parent


def next_observation(f):
    f.clock.value+=INTERVAL
    result=f.coordinator.tick();assert len(result['enqueued'])==1 and result['external_calls']==0
    return result['enqueued'][0]


def test_actual_not_ready_metadata_is_preserved_and_poller_never_claims_or_calls_http(ready,monkeypatch):
    f=ready;parent=begin(f);saved=deepcopy(parent['result']);ledger=f.db.query('SELECT * FROM api_calls')
    item=saved[WAIT]['observations_evidence'][0];file=item['observation']['file'];raw=open(file,'rb').read()
    assert json.loads(raw)['extractor_info']==f.info and len(ledger)==1
    monkeypatch.setattr(f.db,'claim',lambda *a:pytest.fail('Readiness poller must only enqueue, never claim'))
    f.clock.value+=INTERVAL-1;assert f.coordinator.tick()['enqueued']==[]
    assert f.db.get(f.ident)['attempts']==1 and f.db.get(f.ident)['payload']==parent['payload']
    assert f.db.query('SELECT * FROM api_calls')==ledger and open(file,'rb').read()==raw


def test_repeated_waits_use_finite_distinct_children_and_keep_parent_attempt_and_raws(ready):
    f=ready;parent=begin(f);ids=[]
    for _ in range(3):
        ident=next_observation(f);ids.append(ident);assert f.worker.once('video') is True
        assert f.db.get(ident)['state']=='completed' and f.db.get(ident)['attempts']==1
        assert f.db.get(f.ident)['attempts']==1
    current=f.db.get(f.ident);history=current['result'][WAIT]['observations_evidence']
    assert len(history)==4 and len({v['observation']['file'] for v in history})==4 and len(set(ids))==3
    assert len(f.db.query('SELECT * FROM api_calls'))==4 and current['payload']==parent['payload']
    assert not f.db.query('SELECT * FROM jobs WHERE job_type=?',(DOWNLOAD,)) and not f.db.query('SELECT * FROM publications')


def test_ready_real_raw_plan_finishes_same_parent_and_creates_one_bound_download(ready):
    f=ready;parent=begin(f);ident=next_observation(f);f.info.pop('live_status')
    assert f.worker.once('video') is True
    current=f.db.get(f.ident);child=f.db.get(ident);prepared=current['result'][PREPARE_STATE]
    assert current['state']=='completed' and current['attempts']==1 and current['payload']==parent['payload']
    assert child['state']=='completed' and child['result'][WAIT]['state']=='ready'
    jobs=f.db.query('SELECT * FROM jobs WHERE job_type=?',(DOWNLOAD,));assert len(jobs)==1
    download=f.db.get(jobs[0]['job_id']);assert download['payload']['_natural_prepare_job_id']==f.ident
    assert download['payload']['_natural_source_plan']==prepared['source_plan'] and prepared['metadata_observation_job_id']==ident
    leased=f.db.claim('video',600,4);binding=YouTubeNaturalDownload(f.s,f.db)._binding(leased)[1]
    assert binding['source_plan']==prepared['source_plan'] and not f.db.query('SELECT * FROM publications')
    assert f.coordinator.tick()['enqueued']==[] and len(f.calls)==2


def test_observation_parent_transition_is_idempotent_after_exit_before_worker_finish(ready):
    f=ready;begin(f);ident=next_observation(f);f.info.pop('live_status');job=f.db.claim('video',600,4)
    result=f.observer.process(job);calls=f.db.query('SELECT * FROM api_calls')
    assert f.observer.process(job)==result and f.db.query('SELECT * FROM api_calls')==calls
    assert len(f.db.query('SELECT * FROM jobs WHERE job_type=?',(DOWNLOAD,)))==1


def test_exact_size_temporarily_missing_can_wait_without_selecting_a_lower_tier(ready):
    f=ready;f.info.pop('live_status');f.info['formats'][1].pop('filesize')
    parent=begin(f);assert parent['result'][WAIT]['reason']=='YOUTUBE_NATURAL_EXACT_SIZE_NOT_READY'
    assert not f.db.query('SELECT * FROM jobs WHERE job_type=?',(DOWNLOAD,))


@pytest.mark.parametrize('change',['drm','format_drm','private','wrong_id','wrong_channel','age','live','geo'])
def test_access_or_identity_failures_cannot_be_misreported_as_transcoding(ready,change):
    f=ready
    if change=='drm':f.info['has_drm']=True
    if change=='format_drm':f.info['formats'][0]['has_drm']=True
    if change=='private':f.info['availability']='private'
    if change=='wrong_id':f.info['id']='wrongID0001'
    if change=='wrong_channel':f.info['channel_id']='wrong'
    if change=='age':f.info['age_limit']=18
    if change=='live':f.info['is_live']=True
    if change=='geo':f.info['availability']='needs_auth'
    assert f.worker.once('video') is True
    assert f.db.get(f.ident)['state']=='awaiting_review' and not f.db.query("SELECT name FROM sqlite_master WHERE name='youtube_readiness_waits_v3'")
    assert not f.db.query('SELECT * FROM jobs WHERE job_type=?',(JOB_TYPE,))


def test_reason_without_real_raw_file_and_charged_http_is_rejected(ready):
    f=ready
    class Forged:
        def __init__(self,s):pass
        def acquire(self,*a,**k):raise HubError('YOUTUBE_NATURAL_POST_LIVE_NOT_READY',review=True)
    f.prepare.downloader_factory=Forged;assert f.worker.once('video')
    assert f.db.get(f.ident)['state']=='failed' and f.db.get(f.ident)['error_code']=='YOUTUBE_READINESS_EVIDENCE_REQUIRED'
    assert not f.db.query('SELECT * FROM api_calls')


@pytest.mark.parametrize('change',['deadline','limit','payload','policy'])
def test_timeout_count_and_binding_limits_stop_without_another_http(ready,change):
    f=ready;begin(f);ledger=f.db.query('SELECT * FROM api_calls')
    if change=='deadline':f.clock.value+=86401
    elif change=='limit':
        with f.db.transaction() as db:db.execute('UPDATE youtube_readiness_waits_v3 SET observations=max_observations')
        f.clock.value+=INTERVAL
    elif change=='payload':
        with f.db.transaction() as db:db.execute("UPDATE jobs SET payload='{}' WHERE job_id=?",(f.ident,))
        f.clock.value+=INTERVAL
    elif change=='policy':
        with f.db.transaction() as db:db.execute("UPDATE jobs SET policy_version='changed' WHERE job_id=?",(f.ident,))
        f.clock.value+=INTERVAL
    result=f.coordinator.tick();assert result['failed'] and f.db.get(f.ident)['state']=='failed'
    assert f.db.get(f.ident)['attempts']==1 and f.db.query('SELECT * FROM api_calls')==ledger


@pytest.mark.parametrize('hours',[5,25,True,6.0,'24',None])
def test_readiness_window_rejects_unbounded_or_wrong_type_config(ready,hours):
    ready.s.config['youtube_readiness_max_hours']=hours
    with pytest.raises(ValueError):YouTubeReadinessCoordinator(ready.s,ready.db)


def test_private_raw_auth_fields_are_not_written_or_logged(ready):
    f=ready;f.info['http_headers']={'Cookie':'owned-secret-never-written'}
    assert f.worker.once('video')
    assert not list((f.s.root/'media'/f.ident).rglob('extractor-observation-*.json'))
    assert f.db.get(f.ident)['error_code']=='YOUTUBE_READINESS_RAW_CONTAINS_CREDENTIAL'


def test_real_extractor_sort_tuple_uses_standard_json_array_without_changing_source_fields(ready):
    # yt-dlp's locked YouTube extractor returns this exact tuple-shaped helper
    # field. json.dumps natively represents it as an array; no object fallback.
    f=ready;fields=('quality','res','fps','hdr:12','source','vcodec','channels','acodec','lang','proto')
    f.info['_format_sort_fields']=fields;parent=begin(f)
    item=parent['result'][WAIT]['observations_evidence'][0];body=json.loads(open(item['observation']['file'],'rb').read())
    assert body['extractor_info']['_format_sort_fields']==list(fields)
    assert body['extractor_info']['formats']==f.info['formats'] and f.info['_format_sort_fields']==fields


def test_dispatch_appearing_during_wait_becomes_lookup_only(ready):
    f=ready;parent=begin(f)
    with f.db.transaction() as db:
        db.execute("INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)",('owned-key',f.ident,'bilibili','youtube',f.raw['id'],'owned','video','unknown',None,None,time.time()))
    f.clock.value+=INTERVAL;assert f.coordinator.tick()['failed']
    assert f.db.get(f.ident)['state']=='publish_unknown' and not f.db.query('SELECT * FROM jobs WHERE job_type=?',(JOB_TYPE,))


def test_restart_same_coordinator_does_not_duplicate_an_active_child(ready):
    f=ready;begin(f);ident=next_observation(f)
    restarted=YouTubeReadinessCoordinator(f.s,f.db,clock=lambda:f.clock.value+INTERVAL)
    assert restarted.tick()['enqueued']==[]
    assert len(f.db.query('SELECT * FROM jobs WHERE job_type=?',(JOB_TYPE,)))==1 and f.db.get(ident)['attempts']==0


def test_absent_coordinator_after_deadline_cannot_restart_metadata_on_parent_claim(ready):
    f=ready;parent=begin(f);ledger=f.db.query('SELECT * FROM api_calls')
    with f.db.transaction() as db:
        value=f.db.get(f.ident)['result'];value[WAIT]['deadline']=time.time()-60
        db.execute('UPDATE jobs SET next_run=0,result=? WHERE job_id=?',(json.dumps(value),f.ident))
    assert f.worker.once('video') is True
    actual=f.db.get(f.ident);assert actual['state']=='failed' and actual['error_code']=='YOUTUBE_READINESS_TIMEOUT' and actual['attempts']==2
    assert actual['payload']==parent['payload'] and f.db.query('SELECT * FROM api_calls')==ledger and len(f.calls)==1


def test_auth_failure_in_later_observation_preserves_real_code_and_never_creates_another_child(ready):
    f=ready;begin(f);ident=next_observation(f)
    class Restricted:
        def __init__(self,s):pass
        def acquire(self,*a,**k):raise HubError('YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED',review=True)
    f.observer.downloader_factory=Restricted;assert f.worker.once('video')
    f.clock.value+=INTERVAL;result=f.coordinator.tick()
    assert result['failed'] and f.db.get(f.ident)['error_code']=='YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED'
    assert len(f.db.query('SELECT * FROM jobs WHERE job_type=?',(JOB_TYPE,)))==1 and len(f.calls)==1


def test_transient_network_retry_keeps_child_attempts_and_waits_at_least_fifteen_minutes(ready):
    f=ready;begin(f);ident=next_observation(f)
    class Offline:
        def __init__(self,s):pass
        def acquire(self,*a,**k):raise HubError('YOUTUBE_DOWNLOAD_NETWORK_ERROR',retryable=True)
    f.observer.downloader_factory=Offline;before=time.time();assert f.worker.once('video')
    child=f.db.get(ident);assert child['state']=='retry_scheduled' and child['attempts']==1 and child['next_run']>=before+INTERVAL
    assert f.db.get(f.ident)['attempts']==1 and f.coordinator.tick()['enqueued']==[]


@pytest.mark.parametrize('change',['bytes','ledger'])
def test_raw_evidence_hash_and_actual_charged_ledger_are_required(ready,change):
    f=ready;parent=begin(f);item=parent['result'][WAIT]['observations_evidence'][0]
    binding=f.prepare.resource_binding(parent)
    if change=='bytes':
        from pathlib import Path
        with Path(item['observation']['file']).open('ab') as out:out.write(b'changed')
    else:
        with f.db.transaction() as db:db.execute('DELETE FROM api_calls')
    with pytest.raises(HubError):verify_observation(f.s,f.db,parent,binding,item['observation'],item['reason'])


@pytest.mark.parametrize('condition',['post_live','missing_size','auth403'])
def test_real_guarded_metadata_child_preserves_raw_and_keeps_auth_separate(transport,monkeypatch,condition):
    from adapters.ytdlp_natural_v3 import planning_child
    f=transport
    if condition=='post_live':f.info['live_status']='post_live';f.info.pop('duration')
    if condition=='missing_size':f.info['formats'][1].pop('filesize')
    owned_extract(f,monkeypatch,status=403 if condition=='auth403' else 200)
    assert planning_child(f.ticket)==1
    result=json.loads((f.folder/'download-result.json').read_bytes())
    if condition=='auth403':
        assert result['error_code']=='YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED' and 'metadata_observation' not in result
    else:
        expected='YOUTUBE_NATURAL_POST_LIVE_NOT_READY' if condition=='post_live' else 'YOUTUBE_NATURAL_EXACT_SIZE_NOT_READY'
        assert result['error_code']==expected
        body=verify_observation(f.s,f.db,f.job,f.binding,result['metadata_observation'],expected)
        assert body['extractor_info']==f.info and body['http']['requests']==1
    assert not f.media and sum(row['units'] for row in f.db.query('SELECT * FROM api_calls'))==1
