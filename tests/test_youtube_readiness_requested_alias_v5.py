"""Real LazyList identity aliases, no expansion/network/quality downgrade.

The native diagnostic observed requested[0] -> formats[12], requested[1] ->
formats[0], identical fragment objects and all 15 identity scalars. Values here
are owned fixtures; the exact native alias structure and type are reproduced.
"""
from copy import deepcopy
import json

import pytest
from yt_dlp.utils import LazyList

from adapters.youtube_readiness_evidence_v3 import (runtime_snapshot,validate_runtime_snapshot,guard,
    FORMAT_IDENTITY_FIELDS,FRAGMENT_MARKER)
from adapters.ytdlp_whole_v4 import observation_record
from services.hub.errors import HubError
from services.hub.youtube_readiness_v4 import WAIT,read_observation
from test_replay_fifo_v4 import fifo
from test_youtube_readiness_v4 import readiness,begin
from test_youtube_readiness_runtime_values_v4 import (lazy_fragments,add_runtime_format,use_actual_v4_planning_boundary)


def aliased_info():
    formats=[]
    for index in range(13):
        formats.append({'format_id':str(index),'ext':'mp4','protocol':'http_dash_segments',
            'vcodec':'avc1.64002a','acodec':'none','width':1920,'height':1080,'fps':60,
            'filesize':12345,'filesize_approx':None,'tbr':5000,'abr':None,'asr':None,
            'audio_channels':None,'dynamic_range':'SDR','fragments':lazy_fragments()})
    return {'formats':formats,'requested_formats':[dict(formats[12]),dict(formats[0])]}


def test_actual_two_alias_positions_get_explicit_v2_provenance_and_reopen_without_expansion(tmp_path):
    info=aliased_info();ref=observation_record(info,{'job_id':'owned'},tmp_path,{'requests':5})
    body=json.loads((tmp_path/ref['file']).read_bytes());normal=body['normalization']
    assert normal['version']=='youtube-readiness-runtime-normalization-v2'
    aliases=[v for v in normal['values'] if v['action']=='opaque-fragment-alias']
    assert [v['source_path'] for v in aliases]==[['formats',12,'fragments'],['formats',0,'fragments']]
    assert [v['path'] for v in aliases]==[['requested_formats',0,'fragments'],['requested_formats',1,'fragments']]
    assert all(v['expanded'] is False for v in normal['values'])
    validate_runtime_snapshot(body['extractor_info'],normal)
    assert all(f['fragments']==FRAGMENT_MARKER for f in body['extractor_info']['formats'])
    assert info['requested_formats'][0]['fragments'] is info['formats'][12]['fragments']


def test_no_boolean_check_iteration_or_source_mutation():
    info=aliased_info();fragment=info['formats'][12]['fragments'];before=set(info['requested_formats'][0])
    snapshot,normal=runtime_snapshot(info)
    assert info['formats'][12]['fragments'] is fragment and info['requested_formats'][0]['fragments'] is fragment
    assert set(info['requested_formats'][0])==before and type(fragment) is LazyList
    assert snapshot['requested_formats'][0]['fragments']==FRAGMENT_MARKER
    assert normal['values'][-2]['format_identity_sha256']


def test_format_only_v1_evidence_stays_byte_shape_compatible():
    info=aliased_info();info.pop('requested_formats');snapshot,normal=runtime_snapshot(info)
    assert normal['version']=='youtube-readiness-runtime-normalization-v1'
    assert all(set(v)=={'path','python_type','action','expanded'} for v in normal['values'])
    validate_runtime_snapshot(json.loads(json.dumps(snapshot)),json.loads(json.dumps(normal)))


@pytest.mark.parametrize('change',['separate_lazy','wrong_format_id','quality_scalar','duplicate_id','top_level','foreign_object'])
def test_only_proven_same_native_format_alias_is_allowed(tmp_path,change):
    info=aliased_info();item=info['requested_formats'][0]
    if change=='separate_lazy':item['fragments']=lazy_fragments()
    elif change=='wrong_format_id':item['format_id']='foreign'
    elif change=='quality_scalar':item['tbr']+=1
    elif change=='duplicate_id':info['formats'].append(dict(info['formats'][12]))
    elif change=='top_level':info['fragments']=info['formats'][12]['fragments']
    else:item['fragments']=object()
    with pytest.raises(HubError,match='YOUTUBE_READINESS_RAW_NOT_JSON'):
        observation_record(info,{'job_id':'owned'},tmp_path,{'requests':1})
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('change',['old_version','source_index','identity_sha','scalar','presence','missing_alias','duplicate_alias','marker'])
def test_reopened_v2_requires_complete_matching_alias_and_source_provenance(change):
    snapshot,normal=runtime_snapshot(aliased_info());alias=normal['values'][-2]
    if change=='old_version':normal['version']='youtube-readiness-runtime-normalization-v1'
    elif change=='source_index':alias['source_path'][1]=11
    elif change=='identity_sha':alias['format_identity_sha256']='0'*64
    elif change=='scalar':snapshot['requested_formats'][0]['fps']=30
    elif change=='presence':snapshot['requested_formats'][0].pop('filesize_approx')
    elif change=='missing_alias':normal['values'].pop(-2)
    elif change=='duplicate_alias':normal['values'].append(deepcopy(alias))
    else:snapshot['requested_formats'][0]['fragments']=[]
    with pytest.raises(HubError,match='YOUTUBE_READINESS_NORMALIZATION_INVALID'):
        validate_runtime_snapshot(snapshot,normal)


def test_same_alias_enters_real_sqlite_readiness_with_original_parent_attempts(readiness):
    f=readiness;add_runtime_format(f);target=f.info['formats'][-1]
    f.info['requested_formats']=[dict(target)]
    parent=begin(f);assert parent['state']=='retry_scheduled' and parent['attempts']==1
    ref=parent['result'][WAIT]['evidence'][0]['observation']
    body=read_observation(f.s,f.db,parent,f.parent_service.resource_binding(parent),ref,reason='YOUTUBE_NATURAL_POST_LIVE_NOT_READY')
    assert body['normalization']['version']=='youtube-readiness-runtime-normalization-v2'
    assert body['normalization']['values'][-1]['action']=='opaque-fragment-alias'
    assert not f.db.query('SELECT * FROM publications') and f.count==1


def test_fragmented_highest_alias_still_cannot_become_a_direct_or_lower_quality_track(readiness,monkeypatch):
    use_actual_v4_planning_boundary(monkeypatch);f=readiness;f.ready=True;add_runtime_format(f,highest=True)
    target=max((v for v in f.info['formats'] if v.get('vcodec')!='none'),key=lambda v:(v.get('height',0),v.get('fps',0),v.get('tbr',0)))
    f.info['requested_formats']=[dict(target)];parent=begin(f)
    assert parent['state']!='completed' and parent['error_code']=='YOUTUBE_NATURAL_EXACT_TRACK_REQUIRED'
    assert not f.db.query('SELECT * FROM publications') and not f.db.query('SELECT * FROM replay_fifo_dispatches_v4')


def test_original_v3_guard_does_not_gain_the_v4_alias_exception():
    with pytest.raises(HubError,match='YOUTUBE_READINESS_RAW_NOT_JSON'):guard(aliased_info())
