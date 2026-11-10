"""Actual SQLite producer/prepare/download transitions with owned observations."""
from copy import deepcopy
from datetime import datetime,timezone
from types import SimpleNamespace
import json,re
import pytest

from services.hub.errors import HubError
from services.hub.youtube_download import digest
from services.hub.youtube_download_v3 import (YouTubeNaturalPrepare,YouTubeNaturalDownload,VERSION,POLICY_VERSION,
    POLICY_ID,JOB_TYPE,PREPARE_JOB_TYPE,STATE,PREPARE_STATE,QUALITY,OPERATING_NOT_BEFORE)
from services.hub.youtube_monitor import YouTubeMonitor
from services.hub.youtube_natural_plan_v3 import natural_source_plan,validate_natural_plan
from test_youtube_natural_plan_v3 import fixture as source_fixture


def policy():
    return {'version':POLICY_VERSION,'policy_id':POLICY_ID,'enabled':True,'source_scope':'natural_ended_after_activation',
        'quality_profile':QUALITY,'allowed_channel_ids':sorted(('UCaaaaaaaaaaaaaaaaaaaaaa','UCbbbbbbbbbbbbbbbbbbbbbb')),
        'operating_not_before':OPERATING_NOT_BEFORE,'session_profile':'youtube','max_source_bytes':14_000_000_000,
        'max_output_bytes':14_000_000_000,'max_http_requests':2048,'daily_http_requests':6000,'monthly_http_requests':150000,
        'timeout_seconds':21600,'max_new_replays_per_day':4,'activated_at':'2026-11-01T01:00:00Z','session_authorized_at':'2026-10-31T00:00:00Z',
        'owner_risk_accepted_at':'2026-10-31T00:00:00Z','acquisition_mode':'owner_accepted_platform_terms_risk',
        **{name:'Owned explicit scope fixture, not a real authorization' for name in ('owner_permission_reference',
            'acquisition_authorization_reference','quality_authorization_reference','owner_risk_acceptance_reference',
            'session_authorization_reference','activation_reference')}}


def write_policy(s,p):
    file=s.root/'config/youtube-highest-natural-download-policy.toml'
    file.parent.mkdir(parents=True,exist_ok=True)
    file.write_text('\n'.join(k+' = '+json.dumps(v) for k,v in p.items())+'\n',encoding='utf-8');file.chmod(0o644)


@pytest.fixture
def natural(runtime,monkeypatch):
    s,db=runtime;raw,info=source_fixture();raw['snippet']['title']='Owned source'
    # Feed/official observation before enabling any new source admission.
    YouTubeMonitor(s,db,None).observe_video(raw,raw['snippet']['channelId'])
    old=db.claim('video',600,4);db.finish(old,'completed',result={'metadata_only':True})
    p=policy();write_policy(s,p);s.config['youtube_natural_download_enabled']=True
    payload={'video_id':raw['id'],'channel_id':raw['snippet']['channelId'],'title':raw['snippet']['title'],
        'published_at':raw['snippet']['publishedAt'],'duration':raw['contentDetails']['duration'],
        'url':'https://www.youtube.com/watch?v='+raw['id'],'source_api':'youtube-data-v3-official','raw':raw}
    monkeypatch.setattr('services.hub.maintenance.shutil.disk_usage',lambda _:SimpleNamespace(free=60_000_000_000))
    return SimpleNamespace(s=s,db=db,raw=raw,info=info,p=p,payload=payload,service=YouTubeNaturalPrepare(s,db))


def owned_planner(f):
    class Planner:
        def __init__(self,s):pass
        def acquire(self,job,binding,*,check):
            check();now=datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
            plan=natural_source_plan(binding['official_raw'],f.info,activated_at=binding['activated_at'],observed_at=now)
            return {'version':VERSION,'binding_sha256':digest(binding),'source_plan':plan,'http':{'requests':0,'owned_only':True},
                'media_downloaded':False,'human_review':False,'publication_authorized':False}
    return Planner


def prepare(f):
    ident=f.service.enqueue_if_eligible(f.payload);job=f.db.claim('video',600,4);assert job['job_id']==ident
    service=YouTubeNaturalPrepare(f.s,f.db,downloader_factory=owned_planner(f));result=service.process(job)
    f.db.finish(job,'completed',result=result);download=f.db.claim('video',600,4)
    return job,result,download


def test_natural_producer_deduplicates_both_channels_and_does_not_download_or_publish(natural):
    f=natural;one=f.service.enqueue_if_eligible(f.payload);assert f.service.enqueue_if_eligible(deepcopy(f.payload))==one
    raw=deepcopy(f.raw);raw['id']='ownedNew002';raw['snippet']['channelId']='UCbbbbbbbbbbbbbbbbbbbbbb'
    assert YouTubeMonitor(f.s,f.db,None).observe_video(raw,raw['snippet']['channelId'])=='ended'
    rows=f.db.query('SELECT * FROM jobs WHERE job_type=?',(PREPARE_JOB_TYPE,))
    assert len(rows)==2 and {r['source_id'] for r in rows}=={'ownedNew001','ownedNew002'}
    assert not f.db.query('SELECT * FROM jobs WHERE job_type=?',(JOB_TYPE,))
    assert not f.db.query('SELECT * FROM api_calls') and not f.db.query('SELECT * FROM publications')


def test_default_off_returns_none_with_zero_new_jobs_or_requests(natural):
    f=natural;f.s.config.pop('youtube_natural_download_enabled');before=f.db.query('SELECT * FROM jobs')
    assert f.service.enqueue_if_eligible(f.payload) is None
    assert f.db.query('SELECT * FROM jobs')==before and not f.db.query('SELECT * FROM api_calls')


@pytest.mark.parametrize('change',['pre_activation','pre_november','missing_start','wrong_channel','non_bool','stale'])
def test_natural_admission_does_not_backfill_or_accept_unverified_source(natural,change):
    f=natural;before=f.db.query('SELECT * FROM jobs');payload=deepcopy(f.payload)
    if change=='pre_activation':f.p['activated_at']='2026-11-01T03:00:01Z';write_policy(f.s,f.p)
    if change=='pre_november':
        payload['raw']['liveStreamingDetails']['actualStartTime']='2026-10-31T15:59:59Z'
        YouTubeMonitor(f.s,f.db,None).observe_video(payload['raw'],payload['channel_id'])
    if change=='missing_start':payload['raw']['liveStreamingDetails'].pop('actualStartTime')
    if change=='wrong_channel':payload['channel_id']='UC'+'x'*22
    if change=='non_bool':f.s.config['youtube_natural_download_enabled']='yes'
    if change=='stale':
        with f.db.transaction() as db:db.execute('UPDATE youtube_videos SET last_api_check=1')
    if change in ('pre_activation','pre_november'):assert f.service.enqueue_if_eligible(payload) is None
    else:
        with pytest.raises(HubError):f.service.enqueue_if_eligible(payload)
    assert len(f.db.query('SELECT * FROM jobs WHERE job_type=?',(PREPARE_JOB_TYPE,)))==0
    assert not f.db.query('SELECT * FROM api_calls') and not f.db.query('SELECT * FROM publications')


def test_real_preparation_persists_highest_plan_then_new_download_and_recovers_without_replanning(natural):
    f=natural;ident=f.service.enqueue_if_eligible(f.payload);job=f.db.claim('video',600,4)
    service=YouTubeNaturalPrepare(f.s,f.db,downloader_factory=owned_planner(f));result=service.process(job)
    state=result[PREPARE_STATE];child=f.db.get(state['download_job_id'])
    assert state['state']=='planned' and state['source_plan']['selected_formats'][0]['format_id']=='owned60'
    assert child['job_type']==JOB_TYPE and child['state']=='pending' and child['attempts']==0
    class NoReplan:
        def __init__(self,s):raise AssertionError('Checkpointed preparation must survive process loss without another request')
    recovered=YouTubeNaturalPrepare(f.s,f.db,downloader_factory=NoReplan)
    assert recovered.process(f.db.get(ident))==result
    f.db.finish(job,'completed',result=result);download=f.db.claim('video',600,4)
    current,binding=YouTubeNaturalDownload(f.s,f.db)._binding(download)
    assert binding['video_id']=='ownedNew001' and binding['source_plan']==state['source_plan']
    assert binding['transport_budget']['history_reset'] is False and not f.db.query('SELECT * FROM publications')


@pytest.mark.parametrize('key',['source_bytes','max_http_requests','highest_tier','selected_formats','official_metadata_sha256'])
def test_plan_numbers_and_evidence_cannot_be_self_asserted(natural,key):
    f=natural;now=datetime.now(timezone.utc).isoformat();p=natural_source_plan(f.raw,f.info,activated_at=f.p['activated_at'],observed_at=now)
    if key=='highest_tier':p[key]={'height':720,'fps':30}
    elif key=='selected_formats':p[key][0]['format_id']='owned720'
    elif key=='official_metadata_sha256':p[key]='0'*64
    else:p[key]+=1
    with pytest.raises(HubError):validate_natural_plan(p,official_raw=f.raw)


def test_completed_earlier_version_source_is_not_stolen(natural):
    f=natural;old=f.db.enqueue('youtube',f.raw['id'],'youtube_download_v2','video',{'owned':'earlier verified source'},'youtube-highest-download-v2')
    claimed=f.db.claim('video',600,4);assert claimed['job_id']==old;f.db.finish(claimed,'completed',result={'owned':True})
    before=f.db.get(old)
    with pytest.raises(HubError,match='ALREADY_PROCESSING_OR_PROCESSED'):f.service.enqueue_if_eligible(f.payload)
    assert f.db.get(old)==before and not f.db.query('SELECT * FROM jobs WHERE job_type=?',(PREPARE_JOB_TYPE,))


def test_insufficient_working_space_stops_before_download_job_creation(natural,monkeypatch):
    f=natural;ident=f.service.enqueue_if_eligible(f.payload);job=f.db.claim('video',600,4)
    monkeypatch.setattr('services.hub.maintenance.shutil.disk_usage',lambda _:SimpleNamespace(free=15_100_000_000))
    service=YouTubeNaturalPrepare(f.s,f.db,downloader_factory=owned_planner(f))
    with pytest.raises(HubError,match='WAITING_RESOURCE'):service.process(job)
    assert not f.db.query('SELECT * FROM jobs WHERE job_type=?',(JOB_TYPE,))
    assert f.db.get(job['job_id'])['result'][PREPARE_STATE]['state']=='metadata_verified'
    assert not f.db.query('SELECT * FROM api_calls')


def test_daily_new_source_ceiling_is_persistent_and_never_resets_old_jobs(natural):
    f=natural;f.p['max_new_replays_per_day']=1;write_policy(f.s,f.p)
    first=f.service.enqueue_if_eligible(f.payload);before=f.db.get(first)
    raw=deepcopy(f.raw);raw['id']='ownedNew002'
    assert YouTubeMonitor(f.s,f.db,None).observe_video(raw,raw['snippet']['channelId'])=='ended'
    assert len(f.db.query('SELECT * FROM jobs WHERE job_type=?',(PREPARE_JOB_TYPE,)))==1
    assert f.db.get(first)==before and not f.db.query('SELECT * FROM api_calls')


def test_budget_deferral_keeps_history_and_finite_attempts_without_human_review(natural):
    from services.hub.budget import Budget
    from adapters.ytdlp_natural_v3 import natural_budget_limits
    from services.hub.worker import Worker
    f=natural;ident=f.service.enqueue_if_eligible(f.payload)
    class Exhausted:
        def __init__(self,s):pass
        def acquire(self,job,binding,*,check):
            check();budget=Budget(f.db,natural_budget_limits(binding));call=budget.reserve('youtube_download_http',units=6000);budget.finish(call,'done')
            budget.reserve('youtube_download_http')
    worker=Worker(f.s,f.db,{PREPARE_JOB_TYPE:YouTubeNaturalPrepare(f.s,f.db,downloader_factory=Exhausted).process})
    assert worker.once('video') is True
    job=f.db.get(ident)
    assert job['state']=='retry_scheduled' and job['error_code']=='API_BUDGET_EXCEEDED' and job['attempts']==1
    assert job['next_run']>job['updated_at']+1 and sum(row['units'] for row in f.db.query('SELECT * FROM api_calls'))==6000
    assert not f.db.query('SELECT * FROM publications') and not f.db.query('SELECT * FROM jobs WHERE job_type=?',(JOB_TYPE,))


@pytest.mark.parametrize('field',['media_downloaded','human_review','publication_authorized'])
def test_machine_planner_cannot_self_grant_review_or_publication(natural,field):
    f=natural;f.service.enqueue_if_eligible(f.payload);job=f.db.claim('video',600,4);base=owned_planner(f)
    class FalseClaim(base):
        def acquire(self,*args,**kwargs):
            result=super().acquire(*args,**kwargs);result[field]=True;return result
    service=YouTubeNaturalPrepare(f.s,f.db,downloader_factory=FalseClaim)
    with pytest.raises(HubError,match='PLAN_RECEIPT_INVALID'):service.process(job)
    assert not f.db.query('SELECT * FROM jobs WHERE job_type=?',(JOB_TYPE,)) and not f.db.query('SELECT * FROM publications')


@pytest.mark.parametrize('value',['"true"','1','0','1.0','[]','{}'])
def test_settings_rejects_nonboolean_natural_switch(tmp_path,monkeypatch,value):
    from test_youtube_download_acceptance_only import load_config
    with pytest.raises(ValueError,match='natural YouTube'):
        load_config(tmp_path,monkeypatch,'youtube_natural_download_enabled = '+value+'\n')


def test_settings_default_off_and_rejects_simultaneous_legacy_automatic_download(tmp_path,monkeypatch):
    from test_youtube_download_acceptance_only import load_config,config_without_download_mode_fields
    from services.hub.config import Settings
    from pathlib import Path
    assert load_config(tmp_path,monkeypatch).config['youtube_natural_download_enabled'] is False
    source=config_without_download_mode_fields()
    source=re.sub(r'^youtube_download_enabled\s*=.*$', 'youtube_download_enabled = true',source,flags=re.M)
    target=tmp_path/'natural-config.toml';monkeypatch.setenv('HUB_CONFIG',str(target))
    target.write_text('youtube_natural_download_enabled = true\n'+source,encoding='utf-8')
    with pytest.raises(ValueError,match='conflicting natural'):
        Settings.load()
    target.write_text('youtube_natural_download_enabled = true\nyoutube_download_acceptance_only = true\n'+source,encoding='utf-8')
    assert Settings.load().config['youtube_natural_download_enabled'] is True
