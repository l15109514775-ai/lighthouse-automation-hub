"""Real metadata planner/selector with owned inputs; no HTTP, media or accounts."""
from copy import deepcopy
from datetime import datetime,timezone

import pytest

from adapters import ytdlp_whole_v4 as adapter
from services.hub.errors import HubError
from services.hub.replay_multipart_v4 import plan_whole_replay,DEFAULT_POLICY
from services.hub.youtube_download_v4 import transfer_plan
from test_replay_multipart_v4 import evidence


def storyboard(format_id,width,height):
    return {'format_id':format_id,'format_note':'storyboard','ext':'mhtml','protocol':'mhtml',
        'acodec':'none','vcodec':'none','width':width,'height':height,'fps':.1,
        'fragmented':True,'filesize':None,'tbr':None,
        'url':'https://i.ytimg.com/sb/owned/storyboard/M0.jpg?owned=fixture'}


@pytest.fixture
def inventory():
    raw,info,_,_=evidence(seconds=38917,video_bytes=13190218172,audio_bytes=629862280)
    info['formats'][0]['format_id']='299';info['formats'][1]['format_id']='140'
    info['formats'].insert(0,{'format_id':'18','ext':'mp4','protocol':'https','vcodec':'avc1.42001E',
        'acodec':'mp4a.40.2','width':640,'height':360,'fps':30,'filesize':1766668026,'tbr':363.146})
    for item in info['formats']:
        item.update(has_drm=False,url='https://owned.googlevideo.com/videoplayback?owned=fixture')
    policy={**deepcopy(DEFAULT_POLICY),'enabled':True,'raw_track_release_after_verified_merge':True,'prefer_single_whole_passthrough_v5':True}
    plan=plan_whole_replay(raw,info,observed_at=datetime.now(timezone.utc).isoformat(),policy=policy)
    binding={'official_raw':raw,'planning_policy':policy,'whole_replay_plan':plan,'source_plan':transfer_plan(plan)}
    return info,binding


@pytest.mark.parametrize('change',['added','removed'])
def test_nonmedia_mhtml_storyboard_lifecycle_is_not_contractual(inventory,change):
    info,binding=inventory;boards=[storyboard('sb0',320,180),storyboard('sb1',160,90),
        storyboard('sb2',80,45),storyboard('sb3',48,27)]
    if change=='added':
        fresh=deepcopy(info);fresh['formats']=boards+fresh['formats']
    else:
        planned=deepcopy(info);planned['formats']=boards+planned['formats']
        plan=plan_whole_replay(binding['official_raw'],planned,observed_at=datetime.now(timezone.utc).isoformat(),policy=binding['planning_policy'])
        binding={**binding,'whole_replay_plan':plan,'source_plan':transfer_plan(plan)};fresh=deepcopy(info)
    original=deepcopy(binding);selected=adapter.select_from_info(fresh,binding)
    assert [item['format_id'] for item in selected]==['299','140'] and binding==original


def test_mhtml_that_is_not_the_exact_storyboard_family_remains_contractual(inventory):
    info,binding=inventory;fresh=deepcopy(info)
    fresh['formats'].insert(0,{**storyboard('ownedFrame',320,180)})
    with pytest.raises(HubError,match='YOUTUBE_WHOLE_FRESH_INVENTORY_CHANGED'):
        adapter.select_from_info(fresh,binding)


@pytest.mark.parametrize('changes',[{'filesize':1976862571},{'tbr':406.353},{'filesize':1976862571,'tbr':406.353}])
def test_real_lower_encode_size_bitrate_change_keeps_original_selected_streams_and_binding(inventory,changes):
    info,binding=inventory;original=deepcopy(binding);fresh=deepcopy(info);fresh['formats'][0].update(changes)
    selected=adapter.select_from_info(fresh,binding)
    assert [item['format_id'] for item in selected]==['299','140']
    assert selected==info['formats'][1:] and binding==original
    assert fresh['formats'][0]=={**info['formats'][0],**changes}


@pytest.mark.parametrize('index,field,value',[
    (1,'filesize',13190218173),(2,'filesize',629862281),(1,'tbr',3999),(2,'abr',127),
    (0,'width',641),(0,'height',361),(0,'fps',31),(0,'vcodec','vp9'),(0,'acodec','none'),
    (0,'ext','webm'),(0,'protocol','http'),(0,'has_drm',True),(0,'fragments',[{}]),(0,'dynamic_range','HDR'),
    (0,'height',2160),(0,'filesize',None),(0,'tbr',None),(1,'filesize',None),(1,'tbr',None),
])
def test_selected_metadata_quality_transport_and_missing_values_still_refuse(inventory,index,field,value):
    info,binding=inventory;fresh=deepcopy(info);original=deepcopy(binding);fresh['formats'][index][field]=value
    with pytest.raises(HubError):adapter.select_from_info(fresh,binding)
    assert binding==original


@pytest.mark.parametrize('change',['added','removed','reordered','duration','source','formats_missing'])
def test_inventory_identity_shape_order_and_source_remain_strict(inventory,change):
    info,binding=inventory;fresh=deepcopy(info)
    if change=='added':fresh['formats'].append({**fresh['formats'][0],'format_id':'ownedNew'})
    elif change=='removed':fresh['formats'].pop(0)
    elif change=='reordered':fresh['formats'].reverse()
    elif change=='duration':fresh['duration']+=10
    elif change=='source':fresh['id']='ownedNew002'
    else:fresh.pop('formats')
    with pytest.raises(HubError):adapter.select_from_info(fresh,binding)


def test_other_derived_plan_fields_remain_compared(inventory,monkeypatch):
    info,binding=inventory;fresh=deepcopy(info);fresh['formats'][0].update(filesize=1976862571,tbr=406.353)
    real=adapter.plan_from_info
    def altered(value,bound):
        plan=real(value,bound);plan['whole_budget']['required_free_bytes']-=1;return plan
    monkeypatch.setattr(adapter,'plan_from_info',altered)
    with pytest.raises(HubError,match='YOUTUBE_WHOLE_FRESH_INVENTORY_CHANGED'):adapter.select_from_info(fresh,binding)


def test_unused_bitrate_revision_cannot_change_the_highest_selected_track(inventory):
    info,binding=inventory
    info['formats'].append({**info['formats'][1],'format_id':'ownedAlternate','tbr':3900})
    plan=plan_whole_replay(binding['official_raw'],info,observed_at=datetime.now(timezone.utc).isoformat(),policy=binding['planning_policy'])
    binding.update(whole_replay_plan=plan,source_plan=transfer_plan(plan));fresh=deepcopy(info)
    fresh['formats'][-1]['tbr']=4100
    with pytest.raises(HubError,match='YOUTUBE_WHOLE_FRESH_INVENTORY_CHANGED'):adapter.select_from_info(fresh,binding)


def test_untrusted_original_inventory_hash_is_not_hidden_by_normalization(inventory):
    info,binding=inventory;binding['whole_replay_plan']['source_plan_basis']['inventory_sha256']='0'*64
    fresh=deepcopy(info);fresh['formats'][0].update(filesize=1976862571,tbr=406.353)
    with pytest.raises(HubError,match='YOUTUBE_WHOLE_FRESH_INVENTORY_CHANGED'):adapter.select_from_info(fresh,binding)
