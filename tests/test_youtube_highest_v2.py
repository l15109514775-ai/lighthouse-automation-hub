"""Owned official-shaped observations, plans and SQLite only; no network."""
from copy import deepcopy
from types import SimpleNamespace
import json

import pytest

from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.youtube_download import CHANNELS
from services.hub.youtube_download_v2 import YouTubeHighestDownload,VERSION,POLICY_VERSION,POLICY_ID,JOB_TYPE,STATE
from services.hub.youtube_highest_plan_v2 import VIDEO_ID,QUALITY,source_plan,require_operating_date,OPERATING_NOT_BEFORE
from services.hub.youtube_monitor import YouTubeMonitor
from adapters.ytdlp_highest_v2 import select_highest,preflight_budget,budget_limits
from services.hub.budget import Budget
from adapters import ytdlp_replay as legacy

CHANNEL=sorted(CHANNELS)[0]
START='2026-10-31T16:00:36Z'

def plan(**changes):
    args={'video_bytes':10505641986,'audio_bytes':416163003,'max_source_bytes':14_000_000_000,
          'max_output_bytes':14_000_000_000,'request_cap':2048,'actual_start_time':START}
    return source_plan(**{**args,**changes})

def policy_value():
    return {'version':POLICY_VERSION,'policy_id':POLICY_ID,'enabled':True,'video_id':VIDEO_ID,'channel_id':CHANNEL,
        'quality_profile':QUALITY,'video_format_id':'299','audio_format_id':'140','video_bytes':10505641986,'audio_bytes':416163003,
        'max_source_bytes':14_000_000_000,'max_output_bytes':14_000_000_000,'max_http_requests':2048,
        'daily_http_requests':2500,'monthly_http_requests':30000,'timeout_seconds':21600,'activated_at':'2026-11-01T00:00:00Z',
        'owner_permission_reference':'Owned rights fixture only','acquisition_authorization_reference':'Owned ordinary download scope',
        'quality_authorization_reference':'Owned highest quality scope','owner_risk_acceptance_reference':'Owned terms risk fixture',
        'owner_risk_accepted_at':'2026-10-31T00:00:00Z','acquisition_mode':'owner_accepted_platform_terms_risk',
        'session_profile':'youtube','session_authorization_reference':'Owned server session authorization',
        'session_authorized_at':'2026-10-31T00:00:00Z'}

def write_policy(s,value):
    p=s.root/'config/youtube-highest-download-policy.toml';p.parent.mkdir(exist_ok=True)
    p.write_text('\n'.join(k+' = '+json.dumps(v,ensure_ascii=False) for k,v in value.items())+'\n','utf-8');p.chmod(0o644)
    return p

@pytest.fixture
def highest(runtime,monkeypatch):
    s,db=runtime;s.config['youtube_highest_download_enabled']=True
    # Match the candidate's existing channel scope without changing its guards.
    # All observations and transports below remain synthetic and offline.
    assert len(s.config['sources'])==len(CHANNELS)
    for source,channel in zip(s.config['sources'],sorted(CHANNELS)):
        source['youtube_channel_id']=channel
    raw={'id':VIDEO_ID,'snippet':{'channelId':CHANNEL,'title':'Owned highest replay','publishedAt':'2026-11-01T01:44:55Z'},
        'status':{'privacyStatus':'public'},'contentDetails':{'duration':'PT7H8M35S'},
        'liveStreamingDetails':{'actualStartTime':START,'actualEndTime':'2026-10-31T23:09:22Z'}}
    YouTubeMonitor(s,db,None).observe_video(raw,CHANNEL)
    metadata=db.claim('video',180,4);db.finish(metadata,'completed',result={'owned_metadata_only':True})
    payload={'video_id':VIDEO_ID,'channel_id':CHANNEL,'title':raw['snippet']['title'],'published_at':raw['snippet']['publishedAt'],
        'duration':raw['contentDetails']['duration'],'url':'https://www.youtube.com/watch?v='+VIDEO_ID,
        'source_api':'youtube-data-v3-official','raw':raw}
    p=policy_value();write_policy(s,p)
    monkeypatch.setattr('services.hub.maintenance.shutil.disk_usage',lambda _:SimpleNamespace(free=60_000_000_000))
    return SimpleNamespace(s=s,db=db,raw=raw,payload=payload,p=p,service=YouTubeHighestDownload(s,db))

def test_real_observed_track_sizes_create_finite_plan_without_allocating_large_files():
    p=plan()
    assert p['source_bytes']==10921804989 and p['planned_output_max_bytes']==10985804989
    assert p['required_working_bytes']==21939609978 and p['workspace_overhead_bytes']==32_000_000
    assert p['media_requests_plan']==1097 and p['max_http_requests']==1436<2048
    assert [x['format_id'] for x in p['selected_formats']]==['299','140']
    assert p['selected_formats'][0]['height']==1080 and p['selected_formats'][0]['fps']==60.0
    assert p['operating_not_before']==OPERATING_NOT_BEFORE and p['source_actual_start_time']==START

@pytest.mark.parametrize('changes',[{'video_bytes':0},{'audio_bytes':None},{'video_bytes':True},{'video_bytes':14_000_000_001},
    {'request_cap':2049},{'request_cap':512},{'max_source_bytes':3_800_000_000},{'max_output_bytes':10_000_000_000}])
def test_unknown_oversized_or_under_budget_plan_fails_closed(changes):
    with pytest.raises(HubError):plan(**changes)

@pytest.mark.parametrize('date',[None,'','2026-07-01T00:00:00Z','2026-10-31T15:59:59Z','2026-11-01T00:00:00','invalid'])
def test_pre_november_or_unreliable_actual_start_cannot_be_replaced_by_publication(date):
    with pytest.raises(HubError):plan(actual_start_time=date)

def test_actual_start_uses_beijing_midnight_and_normalizes_timezone():
    assert require_operating_date('2026-11-01T00:00:00+08:00')==OPERATING_NOT_BEFORE
    assert require_operating_date('2026-11-01T01:00:00+09:00')==OPERATING_NOT_BEFORE

def test_new_job_deduplicates_and_preserves_legacy_failed_job_partial_and_attempt(highest):
    f=highest;old=f.db.enqueue('youtube',VIDEO_ID,'youtube_download','video',{'owned_legacy':True},'youtube-download-auth-v1')
    with f.db.transaction() as db:db.execute("UPDATE jobs SET state='failed',attempts=1,error_code='YOUTUBE_DOWNLOAD_REQUEST_LIMIT' WHERE job_id=?",(old,))
    part=f.s.root/'media'/old/'youtube-download-v1/stream-owned.mp4.part';part.parent.mkdir(parents=True);part.write_bytes(b'Owned legacy partial')
    before=f.db.get(old);data=part.read_bytes()
    ident=f.service.enqueue_pilot(f.payload)
    assert ident!=old and f.service.enqueue_pilot(deepcopy(f.payload))==ident
    job=f.db.get(ident)
    assert job['job_type']==JOB_TYPE and job['policy_version']==POLICY_ID and job['attempts']==0 and job['state']=='pending'
    assert f.db.get(old)==before and part.read_bytes()==data
    assert len(f.db.query('SELECT * FROM jobs WHERE job_type=?',(JOB_TYPE,)))==1 and not f.db.query('SELECT * FROM api_calls')
    assert f.service.enqueue_if_eligible(f.payload) is None

def test_binding_is_new_version_exact_quality_plan_and_policy_not_old_fields(highest):
    f=highest;ident=f.service.enqueue_pilot(f.payload);job=f.db.claim('video',180,4)
    assert job['job_id']==ident;current,binding=f.service._binding(job)
    assert binding['version']==VERSION and binding['quality_profile']==QUALITY and binding['source_plan']==plan()
    assert binding['source_actual_start_time']==START and binding['max_bytes']==14_000_000_000
    assert binding['transport_budget']['daily_requests']==2500 and binding['transport_budget']['history_reset'] is False
    assert current['attempts']==1 and current['lease_owner']==job['lease_owner']

@pytest.mark.parametrize('key,value',[('enabled',False),('video_id','abcdefghijk'),('video_format_id','18'),('audio_format_id','251'),
    ('daily_http_requests',0),('monthly_http_requests',0),('quality_profile','lower-quality'),('video_bytes',0)])
def test_disabled_unknown_or_reduced_quality_config_never_enqueues(highest,key,value):
    f=highest;f.p[key]=value;write_policy(f.s,f.p)
    with pytest.raises(HubError):f.service.enqueue_pilot(f.payload)
    assert not f.db.query('SELECT * FROM jobs WHERE job_type=?',(JOB_TYPE,)) and not f.db.query('SELECT * FROM api_calls')

def test_missing_hub_switch_is_off_even_if_policy_exists(highest):
    f=highest;f.s.config.pop('youtube_highest_download_enabled')
    with pytest.raises(HubError,match='YOUTUBE_HIGHEST_DISABLED'):f.service.enqueue_pilot(f.payload)

def test_space_is_reserved_for_actual_tracks_output_and_floor_before_new_job(highest,monkeypatch):
    f=highest;required=plan()['required_working_bytes']+15_000_000_000
    monkeypatch.setattr('services.hub.maintenance.shutil.disk_usage',lambda _:SimpleNamespace(free=required-1))
    with pytest.raises(HubError,match='DISK_LOW'):f.service.enqueue_pilot(f.payload)
    assert not f.db.query('SELECT * FROM jobs WHERE job_type=?',(JOB_TYPE,))
    assert f.db.query("SELECT name FROM events WHERE name='disk.low'")

def test_before_operating_start_is_rejected_despite_new_publication_date(highest):
    f=highest;old=deepcopy(f.raw);old['liveStreamingDetails']['actualStartTime']='2026-10-31T15:59:59Z'
    assert old['snippet']['publishedAt'].startswith('2026-11-01')
    YouTubeMonitor(f.s,f.db,None).observe_video(old,CHANNEL)
    with pytest.raises(HubError,match='YOUTUBE_HIGHEST_BEFORE_OPERATING_START'):f.service.enqueue_pilot(f.payload)
    assert not f.db.query('SELECT * FROM jobs WHERE job_type=?',(JOB_TYPE,))

def test_already_completed_legacy_source_is_not_downloaded_again(highest):
    f=highest;old=f.db.enqueue('youtube',VIDEO_ID,'youtube_download','video',{},'youtube-download-auth-v1')
    with f.db.transaction() as db:db.execute("UPDATE jobs SET state='completed' WHERE job_id=?",(old,))
    with pytest.raises(HubError,match='YOUTUBE_HIGHEST_SOURCE_ALREADY'):f.service.enqueue_pilot(f.payload)

def info(binding):
    formats=[]
    for f in binding['source_plan']['selected_formats']:
        formats.append({**f,'protocol':'https','has_drm':False,'vcodec':'avc1.64002a' if f['vcodec']=='h264' else 'none',
            'acodec':'mp4a.40.2' if f['acodec']=='aac' else 'none',
            'url':'https://rr1---owned.googlevideo.com/videoplayback?owned=1'})
    return {'id':VIDEO_ID,'channel_id':CHANNEL,'duration':25715,'live_status':'was_live','availability':'public','formats':formats}

def selector_binding():
    return {'channel_id':CHANNEL,'duration':25715,'source_plan':plan(),'source_actual_start_time':START,'operating_not_before':OPERATING_NOT_BEFORE}

def test_fresh_selector_requires_exact_highest_tracks_and_keeps_wire_urls_ephemeral():
    binding=selector_binding();raw=info(binding);selected=select_highest(raw,binding)
    assert [x['format_id'] for x in selected]==['299','140'] and 'googlevideo.com' not in json.dumps(binding['source_plan'])

@pytest.mark.parametrize('change',['no299','unknown_size','changed_size','fps30','height720','drm','fragments','more_height','old_start'])
def test_selector_refuses_unknown_or_fallback_quality(change):
    binding=selector_binding();raw=info(binding)
    if change=='no299':raw['formats'][0]['format_id']='18'
    elif change=='unknown_size':raw['formats'][0].pop('filesize')
    elif change=='changed_size':raw['formats'][0]['filesize']+=1
    elif change=='fps30':raw['formats'][0]['fps']=30
    elif change=='height720':raw['formats'][0]['height']=720
    elif change=='drm':raw['formats'][0]['has_drm']=True
    elif change=='fragments':raw['formats'][0]['fragments']=[{'owned':True}]
    elif change=='more_height':raw['formats'].append({'height':2160,'fps':60})
    else:binding['source_actual_start_time']='2026-07-01T00:00:00Z'
    with pytest.raises(HubError):select_highest(raw,binding)

def test_new_budget_preserves_old_provider_usage_and_stops_before_media_if_insufficient(highest):
    f=highest;ident=f.service.enqueue_pilot(f.payload);job=f.db.claim('video',180,4);binding=f.service._binding(job)[1]
    old=Budget(f.db,legacy.LIMITS);call=old.reserve(legacy.PROVIDER,units=60);old.finish(call,'done')
    before=f.db.query('SELECT * FROM api_calls');preflight_budget(f.db,binding,1436)
    assert f.db.query('SELECT * FROM api_calls')==before
    budget=Budget(f.db,budget_limits(binding));call=budget.reserve(legacy.PROVIDER,units=2000);budget.finish(call,'done')
    with pytest.raises(HubError,match='API_BUDGET_EXCEEDED'):preflight_budget(f.db,binding,1436)
    assert sum(x['units'] for x in f.db.query('SELECT * FROM api_calls'))==2060
