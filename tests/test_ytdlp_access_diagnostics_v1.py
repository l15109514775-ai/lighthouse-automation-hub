"""Diagnostic-only changes: real child/extractor hooks, owned peers, zero network."""
import json
from copy import deepcopy

import pytest

from adapters import ytdlp_highest_v2 as hq
from adapters import ytdlp_replay as legacy
from services.hub.errors import HubError
from test_youtube_highest_v2 import highest,info
from test_ytdlp_highest_transport_v2 import transport,owned_loop


def test_whitelist_never_retains_reason_urls_headers_cookies_or_unknown_client():
    secret='OwnedSecretNeverPersist'
    response={'videoDetails':{'videoId':'foreign0001','title':secret},'playabilityStatus':{
        'status':'LOGIN_REQUIRED','reason':'sign in; confirm your age; captcha; not available in your country; DRM '+secret+' https://owned.invalid/?token='+secret},
        'streamingData':{'licenseInfos':[{'url':'https://owned.invalid/'+secret}],'formats':[{'url':secret}]},
        'headers':{'Authorization':secret,'Cookie':secret}}
    value=hq.access_response_diagnostic('client','9T5tn1WGlJg',response,client='web?token='+secret,request_index=12)
    assert value['video_id_relation']=='different' and value['returned_video_id']=='foreign0001' and value['client'] is None
    assert value['restriction_categories']==['login','age','challenge','region','drm']
    assert set(value)=={'version','phase','request_index','http_status','target_video_id','returned_video_id','video_id_relation','client','playability_status','restriction_categories'}
    assert secret not in json.dumps(value) and 'http' not in json.dumps(value['restriction_categories'])
    with pytest.raises(HubError,match='YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED'):legacy.player_access_guard(response)


def test_opaque_or_static_details_remain_unknown_without_stringification_or_truth_tests():
    class Opaque:
        def __str__(self):raise AssertionError('Do not stringify opaque response details')
        def __bool__(self):raise AssertionError('Do not inspect opaque truthiness')
    value=hq.access_response_diagnostic('client','bad',{'videoDetails':{'videoId':Opaque()},'playabilityStatus':{'status':Opaque(),'reason':Opaque()},
        'streamingData':{'licenseInfos':Opaque()}},client=Opaque(),request_index=True,http_status='403')
    assert value['target_video_id'] is value['returned_video_id'] is value['client'] is value['request_index'] is value['http_status'] is None
    assert value['video_id_relation']=='unknown' and value['playability_status']=='unknown' and value['restriction_categories']==[]


@pytest.mark.parametrize('phase',['initial','client','age'])
def test_original_player_guard_denial_adds_only_bounded_phase_evidence(transport,monkeypatch,phase):
    from yt_dlp.extractor.youtube._video import YoutubeIE
    f=transport;defaults=YoutubeIE._DEFAULT_AUTHED_CLIENTS
    response={'videoDetails':{'videoId':f.binding['video_id']},'playabilityStatus':{'status':'LOGIN_REQUIRED','reason':'Please sign in OwnedPrivateText'}}
    if phase=='age':response['playabilityStatus']={'status':'UNPLAYABLE','reason':'inappropriate OwnedPrivateText'}
    monkeypatch.setattr(YoutubeIE,'_extract_player_response',lambda *a,**k:deepcopy(response))
    def extract(ydl,url,download=True,**kwargs):
        assert download is False and tuple(ydl.params['extractor_args']['youtube']['player_client'])==defaults
        ie=ydl._ies['Youtube']
        if phase=='initial':
            monkeypatch.setattr(ie,'_search_json',lambda *a,**k:deepcopy(response))
            ie._extract_player_responses(list(defaults),f.binding['video_id'],'owned webpage','web',{},False)
        elif phase=='client':ie._extract_player_response('web',f.binding['video_id'],webpage_ytcfg={},player_ytcfg={},player_url=None,initial_pr=None,visitor_data=None,data_sync_id=None,po_token=None)
        else:ie._is_agegated(response)
        raise AssertionError('Access guard must still reject')
    monkeypatch.setattr(f.ytdlp.YoutubeDL,'extract_info',extract)
    assert hq.child_download(f.ticket)==1
    result=f.result();diagnostic=result['access_diagnostic']
    assert result['error_code']=='YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED' and result['retryable'] is False and result['review'] is False
    assert diagnostic['phase']==phase and diagnostic['video_id_relation']=='matched' and diagnostic['request_index']==0
    assert diagnostic['client']==(None if phase=='age' else 'web')
    assert 'OwnedPrivateText' not in json.dumps(result) and not f.calls and not f.media_calls and not f.sockets
    assert YoutubeIE._DEFAULT_AUTHED_CLIENTS==defaults


def test_foreign_video_id_is_diagnosed_but_never_releases_the_existing_guard(transport,monkeypatch):
    from yt_dlp.extractor.youtube._video import YoutubeIE
    f=transport
    response={'videoDetails':{'videoId':'foreign0001'},'playabilityStatus':{'status':'LOGIN_REQUIRED'}}
    monkeypatch.setattr(YoutubeIE,'_extract_player_response',lambda *a,**k:response)
    def extract(ydl,url,**kwargs):return ydl._ies['Youtube']._extract_player_response('tv_samsung',f.binding['video_id'])
    monkeypatch.setattr(f.ytdlp.YoutubeDL,'extract_info',extract)
    assert hq.child_download(f.ticket)==1 and f.result()['error_code']=='YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED'
    assert f.result()['access_diagnostic']['video_id_relation']=='different' and f.result()['access_diagnostic']['client']=='tv_samsung'
    assert not f.calls and not f.media_calls and not f.sockets


@pytest.mark.parametrize('status',[401,403])
def test_http_denial_keeps_ledger_and_code_without_persisting_request_url(transport,monkeypatch,status):
    f=transport;owned_loop(f,monkeypatch,1,url='https://www.youtube.com/watch?v='+f.binding['video_id']+'&private=OwnedQuerySecret',status=status)
    assert hq.child_download(f.ticket)==1
    result=f.result();diagnostic=result['access_diagnostic']
    assert result['error_code']=='YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED' and result['retryable'] is False
    assert diagnostic['phase']=='http' and diagnostic['http_status']==status and diagnostic['request_index']==1
    assert diagnostic['client'] is None and diagnostic['video_id_relation']=='unknown' and diagnostic['restriction_categories']==['http_authorization']
    assert 'OwnedQuerySecret' not in json.dumps(result) and not f.media_calls and not f.sockets
    calls=f.db.query('SELECT state,error_code FROM api_calls')
    assert calls==[{'state':'error','error_code':'YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED'}]


def test_successful_player_hook_is_unchanged_and_has_no_false_access_diagnostic(transport,monkeypatch):
    from yt_dlp.extractor.youtube._video import YoutubeIE
    f=transport;response={'videoDetails':{'videoId':f.binding['video_id']},'playabilityStatus':{'status':'OK'},'streamingData':{}}
    monkeypatch.setattr(YoutubeIE,'_extract_player_response',lambda *a,**k:response)
    def extract(ydl,url,**kwargs):
        assert ydl._ies['Youtube']._extract_player_response('web',f.binding['video_id']) is response
        return info(f.binding)
    monkeypatch.setattr(f.ytdlp.YoutubeDL,'extract_info',extract)
    assert hq.child_download(f.ticket)==1 and f.result()['error_code']=='OWNED_MEDIA_STAGE_NOT_EXECUTED'
    assert 'access_diagnostic' not in f.result() and f.media_calls==['owned-stop'] and not f.sockets
