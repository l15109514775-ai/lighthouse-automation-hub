"""Pure owned inventories; no media, HTTP, job, policy, or publication writes."""
from copy import deepcopy
from datetime import datetime
import json
import pytest

from services.hub.errors import HubError
from services.hub.youtube_natural_plan_v3 import natural_source_plan,VERSION


def fixture():
    raw={'id':'ownedNew001','snippet':{'channelId':'UCaaaaaaaaaaaaaaaaaaaaaa','publishedAt':'2026-07-01T00:00:00Z'},
        'status':{'privacyStatus':'public'},'contentDetails':{'duration':'PT1H'},
        'liveStreamingDetails':{'actualStartTime':'2026-11-01T02:00:00Z','actualEndTime':'2026-11-01T03:00:00Z'}}
    def video(name,height,fps,size,rate,codec='avc1.640028'):
        return {'format_id':name,'vcodec':codec,'acodec':'none','width':int(height*16/9),'height':height,'fps':fps,
            'ext':'mp4','protocol':'https','filesize':size,'tbr':rate,'url':'https://owned.invalid/video?secret=never_saved'}
    formats=[video('owned720',720,30,100000000,2000),video('owned60',1080,60,200000000,4000),
        video('owned60alt',1080,60,180000000,3500),
        {'format_id':'ownedAudio','vcodec':'none','acodec':'mp4a.40.2','ext':'m4a','protocol':'https',
         'filesize':10000000,'abr':128,'url':'https://owned.invalid/audio?secret=never_saved'}]
    info={'id':raw['id'],'channel_id':raw['snippet']['channelId'],'duration':3600,'formats':formats}
    return raw,info


def planned(raw,info,**changes):
    args={'activated_at':'2026-11-01T01:00:00Z','observed_at':'2026-11-01T03:00:10Z',
        'now':datetime.fromisoformat('2026-11-01T03:00:20+00:00').timestamp()}
    return natural_source_plan(raw,info,**{**args,**changes})


def test_per_video_highest_not_hardcoded_to_pilot_and_inventory_never_copies_signed_urls():
    raw,info=fixture();before=deepcopy((raw,info));p=planned(raw,info)
    assert p['version']==VERSION and p['video_id']=='ownedNew001'
    assert [x['format_id'] for x in p['selected_formats']]==['owned60','ownedAudio']
    assert p['highest_tier']=={'height':1080,'fps':60.0} and p['source_bytes']==210000000
    assert p['required_working_bytes']==516000000 and p['max_http_requests']<2048
    assert p['human_review'] is p['publication_authorized'] is p['plan_grants_admission'] is False
    assert 'secret=' not in json.dumps(p) and 'owned.invalid' not in json.dumps(p)
    assert (raw,info)==before


def test_highest_source_can_be_lower_resolution_without_forcing_299_or_1080():
    raw,info=fixture();info['formats']=[info['formats'][0],info['formats'][-1]]
    p=planned(raw,info);assert p['highest_tier']=={'height':720,'fps':30.0}
    assert p['selected_formats'][0]['format_id']=='owned720'


@pytest.mark.parametrize('field,value',[('height',2160),('fps',120),('dynamic_range','HDR10')])
def test_unsupported_highest_never_silently_selects_existing_720_or_other_tier(field,value):
    raw,info=fixture();info['formats'][1][field]=value
    with pytest.raises(HubError,match='YOUTUBE_NATURAL_HIGHEST_TIER_UNSUPPORTED'):planned(raw,info)


@pytest.mark.parametrize('change',['unknown_size','approx_size','fragmented','drm','unsupported_codec','missing_fps','unknown_rate','tie'])
def test_missing_exact_highest_evidence_stops_instead_of_falling_back(change):
    raw,info=fixture();top=info['formats'][1]
    if change=='unknown_size':top['filesize']=None
    if change=='approx_size':top.pop('filesize');top['filesize_approx']=200000000
    if change=='fragmented':top['fragments']=[{'url':'https://owned.invalid'}]
    if change=='drm':top['has_drm']=True
    if change=='unsupported_codec':top['vcodec']='av01.0.12M.08';info['formats'].pop(2)
    if change=='missing_fps':top.pop('fps')
    if change=='unknown_rate':top['tbr']=None
    if change=='tie':info['formats'][2]['tbr']=top['tbr']
    with pytest.raises(HubError):planned(raw,info)


@pytest.mark.parametrize('change',['old_start','missing_start','ended_before_activation','missing_end','future_end',
    'wrong_channel','wrong_source','private','stale_observation','future_activation','duration','zero_size','oversize','unsafe_label'])
def test_date_source_budget_and_serialization_boundaries(change):
    raw,info=fixture();kwargs={};live=raw['liveStreamingDetails']
    if change=='old_start':live['actualStartTime']='2026-10-31T15:59:59Z'
    if change=='missing_start':live.pop('actualStartTime')
    if change=='ended_before_activation':kwargs['activated_at']='2026-11-01T03:00:01Z'
    if change=='missing_end':live.pop('actualEndTime')
    if change=='future_end':live['actualEndTime']='2026-11-01T04:00:00Z'
    if change=='wrong_channel':raw['snippet']['channelId']='UC'+'x'*22
    if change=='wrong_source':info['id']='another0001'
    if change=='private':raw['status']['privacyStatus']='private'
    if change=='stale_observation':kwargs['observed_at']='2026-11-01T02:00:00Z'
    if change=='future_activation':kwargs['activated_at']='2026-11-01T04:00:00Z'
    if change=='duration':raw['contentDetails']['duration']='PT11H'
    if change=='zero_size':info['formats'][1]['filesize']=0
    if change=='oversize':info['formats'][1]['filesize']=14_000_000_000
    if change=='unsafe_label':info['formats'][0]['protocol']='https://secret.invalid?token=owned'
    with pytest.raises(HubError):planned(raw,info,**kwargs)
