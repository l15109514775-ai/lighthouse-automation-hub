"""Sanitized real failed observations, replayed without HTTP/media/DB changes."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pytest

from services.hub.errors import HubError
from services.hub.replay_multipart_v4 import plan_whole_replay,validate_whole_replay_plan
from services.hub.youtube_download_v4 import transfer_plan,validate_transfer_plan
from services.hub.youtube_natural_plan_v3 import natural_source_plan,validate_natural_plan


def actual(ident='YsSXSIYtCoE'):
    value=json.loads((Path(__file__).parent/'fixtures/youtube_whole_observations_v5'/(ident+'.json')).read_text('utf-8'))
    return value,value['official_raw'],value['extractor_info']


def plan(value,raw,info):
    return plan_whole_replay(raw,info,observed_at=value['observed_at'],policy=value['planning_policy'],
        now=datetime.fromisoformat(value['observed_at']).timestamp())


@pytest.mark.parametrize('ident',['6wHqfDmNF0U','YsSXSIYtCoE'])
def test_real_inventory_keeps_storyboards_but_selects_full_1080p60_and_standard_aac(ident):
    value,raw,info=actual(ident);before=deepcopy((raw,info));whole=plan(value,raw,info);basis=whole['source_plan_basis']
    assert [f['format_id'] for f in basis['selected_formats']]==['299','140']
    assert basis['highest_tier']=={'height':1080,'fps':60.0}
    assert basis['source_bytes']==sum(f['filesize'] for f in info['formats'] if f['format_id'] in ('299','140'))
    assert len(basis['source_inventory'])==len(info['formats'])
    boards=[f for f in basis['source_inventory'] if f['format_id'].startswith('sb')]
    assert len(boards)==4 and all(f['vcodec']==f['acodec']=='none' and f['bitrate_kbps'] is None for f in boards)
    assert whole['publishable'] is whole['download_authorized_by_plan'] is False
    assert whole['whole_budget']['guaranteed_single_by_plan'] is True
    assert validate_whole_replay_plan(whole,value['planning_policy'],official_raw=raw)==whole
    assert validate_transfer_plan(transfer_plan(whole))==transfer_plan(whole)
    natural=natural_source_plan(raw,info,activated_at=value['planning_policy']['source_not_before'],
        observed_at=value['observed_at'],now=datetime.fromisoformat(value['observed_at']).timestamp())
    assert validate_natural_plan(natural,official_raw=raw)==natural
    assert (raw,info)==before


@pytest.mark.parametrize('change',['unrelated_pair','different_codec','different_container','third_audio','video_tie'])
def test_equal_rate_exception_does_not_choose_arbitrarily(change):
    value,raw,info=actual();drc=next(f for f in info['formats'] if f['format_id']=='140-drc')
    if change=='unrelated_pair':drc['format_id']='141'
    elif change=='different_codec':drc['acodec']='mp4a.40.5'
    elif change=='different_container':drc['ext']='webm'
    elif change=='third_audio':info['formats'].append({**drc,'format_id':'141'})
    else:info['formats'].append({**next(f for f in info['formats'] if f['format_id']=='299'),'format_id':'399'})
    with pytest.raises(HubError,match='YOUTUBE_NATURAL_QUALITY_ORDER_AMBIGUOUS'):plan(value,raw,info)


@pytest.mark.parametrize('change',['zero_audio_rate','zero_video_rate','unrecognized_nonmedia'])
def test_storyboard_normalization_does_not_relax_media_bitrate_evidence(change):
    value,raw,info=actual()
    if change=='zero_audio_rate':
        for f in info['formats']:
            if f['format_id'] in ('140','140-drc'):f['abr']=0
    elif change=='zero_video_rate':next(f for f in info['formats'] if f['format_id']=='299')['tbr']=0
    else:info['formats'][0]['format_id']='unrecognized-board'
    with pytest.raises(HubError,match='YOUTUBE_NATURAL_SOURCE_PLAN_INVALID'):plan(value,raw,info)


def test_rebuild_refuses_changing_the_selected_standard_track_to_drc():
    value,raw,info=actual();whole=plan(value,raw,info)
    drc=next(f for f in whole['source_plan_basis']['source_inventory'] if f['format_id']=='140-drc')
    whole['source_plan_basis']['selected_formats'][1]={k:drc[k] for k in ('format_id','ext','vcodec','acodec','width','height','fps','filesize')}
    with pytest.raises(HubError):validate_whole_replay_plan(whole,value['planning_policy'],official_raw=raw)
