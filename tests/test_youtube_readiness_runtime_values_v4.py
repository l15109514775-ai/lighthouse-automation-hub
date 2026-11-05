"""Owned extractor objects and real SQLite readiness; no external requests."""
from copy import deepcopy
import hashlib,json

import pytest
from yt_dlp.utils import LazyList

from services.hub.errors import HubError
from adapters.youtube_readiness_evidence_v3 import guard,runtime_snapshot
from adapters.ytdlp_whole_v4 import observation_record,plan_from_info
from services.hub.youtube_readiness_v4 import WAIT,JOB_TYPE,read_observation,WholeReadinessObservationV4
from services.hub.worker import Worker
from test_replay_fifo_v4 import fifo
from test_youtube_readiness_v4 import readiness,begin,advance


def lazy_fragments():
    class NeverExpand:
        def __iter__(self):return self
        def __next__(self):pytest.fail('Runtime fragments must not be iterated, peeked or downloaded')
        # The owned metadata fixture deep-copies its dict; the actual extractor
        # does not. Keep the exact native LazyList through that test-only copy.
        def __deepcopy__(self,memo):return self
    return LazyList(NeverExpand())


def add_runtime_format(f,*,highest=False):
    # Real yt-dlp type at the exact format fragment position. This is an owned
    # extractor boundary, not a claim about the discarded live Python objects.
    if highest:
        target=max((v for v in f.info['formats'] if v.get('vcodec')!='none'),
                   key=lambda v:(v.get('height',0),v.get('fps',0),v.get('tbr',0)))
    else:
        target=deepcopy(next(v for v in f.info['formats'] if v.get('vcodec')!='none'))
        target.update(format_id='ownedDashLow',height=360,width=640,fps=30,tbr=100)
        f.info['formats'].append(target)
    target.update(protocol='http_dash_segments',fragments=lazy_fragments())


def use_actual_v4_planning_boundary(monkeypatch):
    import test_youtube_readiness_v4 as owned
    # The older owned downloader calls the pure planner directly. Route only
    # that fixture seam through the actual V4 adapter used by the native child.
    monkeypatch.setattr(owned,'plan_whole_replay',lambda raw,info,*,observed_at,policy:
        plan_from_info(info,{'official_raw':raw,'planning_policy':policy}))


def test_native_lazy_fragments_enter_real_bounded_readiness_without_expansion(readiness,monkeypatch):
    f=readiness;add_runtime_format(f);parent=begin(f)
    assert parent['state']=='retry_scheduled' and parent['error_code']=='WAITING_YOUTUBE_READINESS'
    assert parent['attempts']==1 and parent['lease_owner'] is None
    wait=f.db.query('SELECT * FROM youtube_readiness_waits_v4')[0]
    assert wait['observations']==1 and wait['deadline']-wait['created_at']==86400
    ref=parent['result'][WAIT]['evidence'][0]['observation']
    body=read_observation(f.s,f.db,parent,f.parent_service.resource_binding(parent),ref,
                          reason='YOUTUBE_NATURAL_POST_LIVE_NOT_READY')
    assert body['normalization']['version']=='youtube-readiness-runtime-normalization-v1'
    assert body['normalization']['values'][0]['python_type']=='yt_dlp.utils.LazyList'
    assert body['normalization']['values'][0]['expanded'] is False
    assert not f.db.query('SELECT * FROM publications') and f.count==1
    advance(f,monkeypatch);child_id=f.coordinator.tick()['enqueued'][0]
    f.ready=True;use_actual_v4_planning_boundary(monkeypatch)
    observer=WholeReadinessObservationV4(f.s,f.db,coordinator=f.coordinator,downloader_factory=f.factory)
    assert Worker(f.s,f.db,{JOB_TYPE:observer.process}).once('video')
    after=f.db.get(f.parent_id);child=f.db.get(child_id)
    assert after['state']==child['state']=='completed' and after['attempts']==child['attempts']==1
    assert after['payload']==parent['payload'] and f.count==2
    assert not f.db.query('SELECT * FROM replay_fifo_dispatches_v4') and not f.db.query('SELECT * FROM publications')


def test_ready_real_plan_keeps_below_highest_runtime_format_fragmented(readiness,monkeypatch):
    use_actual_v4_planning_boundary(monkeypatch)
    f=readiness;f.ready=True;add_runtime_format(f);parent=begin(f)
    assert parent['state']=='completed' and parent['attempts']==1
    plan=parent['result']['replay_fifo_plan_v4']['plan']['source_plan_basis']
    item=next(v for v in plan['source_inventory'] if v['format_id']=='ownedDashLow')
    assert item['fragmented'] is True
    assert not f.db.query('SELECT * FROM publications') and f.count==1


def test_fragmented_highest_is_not_replaced_with_a_lower_direct_track(readiness,monkeypatch):
    use_actual_v4_planning_boundary(monkeypatch)
    f=readiness;f.ready=True;add_runtime_format(f,highest=True);parent=begin(f)
    assert parent['state']!='completed'
    assert parent['error_code']=='YOUTUBE_NATURAL_EXACT_TRACK_REQUIRED'
    assert not f.db.query('SELECT * FROM publications')


def test_original_v3_guard_still_rejects_runtime_objects():
    with pytest.raises(HubError,match='YOUTUBE_READINESS_RAW_NOT_JSON'):
        guard({'formats':[{'fragments':lazy_fragments()}]})


def test_plain_json_record_remains_original_shape_and_input_is_not_mutated(tmp_path):
    info={'id':'owned000001','formats':[{'fragments':[]}],'nested':{'tuple':(1,2)}}
    reference=observation_record(info,{'job_id':'owned'},tmp_path,{'requests':1})
    body=json.loads((tmp_path/reference['file']).read_bytes())
    assert 'normalization' not in body and body['extractor_info']==json.loads(json.dumps(info))
    assert info['nested']['tuple']==(1,2) and info['formats'][0]['fragments']==[]


class UnknownRuntimeObject:
    def __repr__(self):pytest.fail('Unknown object repr may contain credentials')


@pytest.mark.parametrize('case,code',[
    ('unknown','YOUTUBE_READINESS_RAW_NOT_JSON'),
    ('wrong_lazy_path','YOUTUBE_READINESS_RAW_NOT_JSON'),
    ('html','YOUTUBE_READINESS_RAW_NOT_JSON'),
    ('credential','YOUTUBE_READINESS_RAW_CONTAINS_CREDENTIAL'),
    ('nan','YOUTUBE_READINESS_RAW_NOT_JSON'),
])
def test_non_json_credentials_and_unknown_types_still_write_no_evidence(tmp_path,case,code):
    info={'formats':[{'fragments':lazy_fragments()}]}
    if case=='unknown':info['other']=UnknownRuntimeObject()
    if case=='wrong_lazy_path':info['thumbnails']=lazy_fragments()
    if case=='html':info='<html>sign in/captcha</html>'
    if case=='credential':info['http_headers']={'Authorization':'owned-do-not-write'}
    if case=='nan':info['duration']=float('nan')
    with pytest.raises(HubError,match=code):
        observation_record(info,{'job_id':'owned'},tmp_path,{'requests':1})
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('change',['marker','provenance'])
def test_reopened_normalization_must_match_opaque_fragment_presence(readiness,change):
    f=readiness;add_runtime_format(f);parent=begin(f)
    reference=parent['result'][WAIT]['evidence'][0]['observation']
    from pathlib import Path
    file=Path(reference['file']);body=json.loads(file.read_bytes())
    if change=='marker':body['extractor_info']['formats'][-1]['fragments']=[]
    else:body['normalization']['values'][0]['path'][1]=0
    raw=json.dumps(body,sort_keys=True,separators=(',',':')).encode();file.write_bytes(raw)
    reference={**reference,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    with pytest.raises(HubError,match='YOUTUBE_READINESS_NORMALIZATION_INVALID'):
        read_observation(f.s,f.db,parent,f.parent_service.resource_binding(parent),reference,
                         reason='YOUTUBE_NATURAL_POST_LIVE_NOT_READY')


def test_runtime_snapshot_never_mutates_or_boolean_checks_the_native_lazy_source():
    fragments=lazy_fragments();info={'formats':[{'fragments':fragments}]}
    snapshot,provenance=runtime_snapshot(info)
    assert info['formats'][0]['fragments'] is fragments
    assert snapshot['formats'][0]['fragments']['expanded'] is False
    assert provenance['values'][0]['path']==['formats',0,'fragments']
