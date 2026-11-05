"""Owned real FFmpeg delivery and SQLite; no real accounts or large downloads.

The segmented small fixture explicitly substitutes only the size-selection
boundary so the actual existing splitter/packet-chain is exercised cheaply.
Separate pure tests cover the fixed real 14GB / 10h selection boundary.
"""
from copy import deepcopy
import hashlib,json,time
from pathlib import Path

import pytest

from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.evidence import sha256_file
from services.hub.replay_delivery_v5 import (ReplayDeliveryV5,actual_processing_model,SINGLE,SEGMENTED,TABLE)
from services.hub.replay_fifo_v4 import POLICY_FILE as FIFO_FILE
from services.hub.replay_multipart_v4 import DEFAULT_POLICY,plan_whole_replay,digest,validate_planning_policy,validate_whole_replay_plan
from services.hub.youtube_download_v4 import STATE,transfer_plan,validate_transfer_plan
from test_replay_multipart_v4 import evidence
from test_youtube_download_v4 import whole as original_whole
from test_replay_fifo_v4 import fifo
from test_ytdlp_highest_v2 import tracks


@pytest.fixture
def delivery(fifo,tracks,monkeypatch):
    f=fifo;f.whole.update(prefer_single_whole_passthrough_v5=True,raw_track_release_after_verified_merge=True)
    f.p['planning_policy_sha256']=digest(f.whole);(f.s.root/'config'/FIFO_FILE).write_text(encode(f.p),'utf-8')
    return original_whole.__wrapped__(f,tracks,monkeypatch)


def test_real_single_references_complete_canonical_without_copy_or_legacy_split(delivery,monkeypatch):
    f=delivery
    import services.hub.media_processing as base
    monkeypatch.setattr(base,'split_media',lambda *a,**kw:pytest.fail('Fitting whole must not split'))
    result=f.service.process(f.job);state=result[STATE];value=state['delivery_v5'];file=Path(state['source_asset']['file'])
    assert state['state']=='single_output_verified_v5' and value['processing_model']==SINGLE
    assert len(value['outputs'])==1 and value['outputs'][0]['file']==str(file)
    assert value['outputs'][0]['bytes']==file.stat().st_size and value['outputs'][0]['sha256']==sha256_file(file)
    assert not (file.parent/'parts-v4').exists() and 'parts' not in state
    assert state['raw_track_custody_v4']['state']=='released' and file.is_file()
    assert value['packet_coverage_verified'] is True and value['publishable'] is False
    body=json.loads(Path(value['manifest']).read_bytes())
    assert body['source']['packet_evidence']==state['source_asset']['packet_evidence']
    assert body['output_packet_coverage'][0]['packet_after']==body['source']['packet_evidence']
    assert value['manifest_reference']['sha256']==sha256_file(value['manifest'])
    assert not f.db.query('SELECT * FROM publications')


def test_retry_reopens_unchanged_manifest_bytes_mtime_and_output_key_without_download(delivery):
    f=delivery;first=f.service.process(f.job);v=first[STATE]['delivery_v5'];file=Path(v['manifest']);before=(file.read_bytes(),file.stat().st_mtime_ns)
    again=f.service.process(f.job)
    assert again==first and (file.read_bytes(),file.stat().st_mtime_ns)==before
    assert f.calls==['owned_track_copy_not_youtube']
    row=f.db.query('SELECT * FROM '+TABLE)[0]
    assert row['manifest_sha256']==v['manifest_reference']['sha256'] and row['idempotency_key']==v['outputs'][0]['part_idempotency_key']


@pytest.mark.parametrize('changed',['master','manifest','proof','ledger'])
def test_tampering_or_external_state_never_rewrites_evidence_or_reacquires(delivery,changed):
    f=delivery;value=f.service.process(f.job)[STATE]['delivery_v5'];manifest=Path(value['manifest']);saved=manifest.read_bytes()
    if changed=='ledger':
        with f.db.transaction() as db:db.execute("UPDATE "+TABLE+" SET state='publish_unknown'")
    else:
        file=Path(value['outputs'][0]['file']) if changed=='master' else manifest if changed=='manifest' else manifest.parent/'whole-proof.json'
        if changed=='master':
            raw=file.read_bytes();file.write_bytes(bytes([raw[0]^1])+raw[1:])
        else:
            body=json.loads(file.read_bytes());body['unauthorized_change']=True;file.write_text(encode(body),'utf-8')
    before=manifest.read_bytes()
    with pytest.raises(HubError):f.service.process(f.job)
    assert manifest.read_bytes()==before and f.calls==['owned_track_copy_not_youtube']
    if changed!='manifest':assert manifest.read_bytes()==saved


def test_fixed_boundary_uses_actual_bytes_and_duration_not_conservative_plan_bound():
    for size,seconds in [(14_000_000_000,36000),(13_970_000_000,35999.99)]:assert actual_processing_model(size,seconds)==SINGLE
    assert actual_processing_model(14_000_000_001,36000)==SEGMENTED
    assert actual_processing_model(1,36000.01)==SEGMENTED
    for size,seconds in [(True,1),(1,True),(0,1),(1,float('nan')),(1,float('inf'))]:
        with pytest.raises(HubError):actual_processing_model(size,seconds)
    raw,info,when,now=evidence(seconds=28000,video_bytes=13_950_000_000,audio_bytes=20_000_000)
    policy={**deepcopy(DEFAULT_POLICY),'prefer_single_whole_passthrough_v5':True}
    value=plan_whole_replay(raw,info,observed_at=when,now=now,policy=policy)
    assert value['source_plan_basis']['planned_output_max_bytes']>14_000_000_000
    assert value['processing_model']=='whole_or_lossless_segments_v5' and value['whole_budget']['segment_output_max_bytes']>0
    # The conservative pre-download bound reserves possible segments; it does not choose the actual model.
    assert actual_processing_model(13_970_000_000,28000)==SINGLE


def test_single_plan_budget_omits_unneeded_third_copy_and_old_default_is_unchanged():
    raw,info,when,now=evidence(seconds=28000,video_bytes=10_500_000_000,audio_bytes=400_000_000)
    old=plan_whole_replay(raw,info,observed_at=when,now=now,policy=deepcopy(DEFAULT_POLICY))
    policy={**deepcopy(DEFAULT_POLICY),'prefer_single_whole_passthrough_v5':True,'raw_track_release_after_verified_merge':True}
    value=plan_whole_replay(raw,info,observed_at=when,now=now,policy=policy);b=value['whole_budget']
    assert b['guaranteed_single_by_plan'] is True and b['segment_output_max_bytes']==0
    assert b['required_working_bytes']==b['source_tracks_bytes']+b['merged_max_bytes']+32_000_000
    assert b['processing_required_free_bytes']==15*1024**3+32_000_000
    assert 'split_policy' not in value and 'split_policy' in old and 'processing_model' not in old
    assert validate_whole_replay_plan(value,policy,official_raw=raw)==value
    assert validate_transfer_plan(transfer_plan(value))==transfer_plan(value)
    for bad in (1,'true',None):
        with pytest.raises(HubError):validate_planning_policy({**deepcopy(DEFAULT_POLICY),'prefer_single_whole_passthrough_v5':bad})


def test_small_owned_real_segment_branch_keeps_old_packet_chain_and_new_delivery_identity(delivery,monkeypatch):
    f=delivery
    # This is only an inexpensive branch-selection seam; no giant-source acceptance is claimed.
    import services.hub.replay_delivery_v5 as module
    monkeypatch.setattr(module,'actual_processing_model',lambda size,seconds:SEGMENTED)
    result=f.service.process(f.job);state=result[STATE];v=state['delivery_v5'];body=json.loads(Path(v['manifest']).read_bytes())
    assert state['state']=='parts_verified' and v['processing_model']==SEGMENTED
    assert Path(body['processing_evidence']['file']).as_posix().endswith('parts-v4/manifest.json')
    assert body['output_packet_coverage'][-1]['packet_after']==body['source']['packet_evidence']
    assert v['outputs'][0]['file']!=state['source_asset']['file'] and Path(state['source_asset']['file']).is_file()
    before=Path(v['manifest']).read_bytes();assert f.service.process(f.job)==result
    assert Path(v['manifest']).read_bytes()==before and not f.db.query('SELECT * FROM publications')


def test_lost_lease_before_delivery_emits_no_outputs_and_retains_master(delivery,monkeypatch):
    f=delivery
    from services.hub.replay_delivery_v5 import ReplayDeliveryV5
    real=ReplayDeliveryV5.run
    def expire(self,job,asset):
        with f.db.transaction() as db:db.execute('UPDATE jobs SET lease_until=? WHERE job_id=?',(time.time()-1,job['job_id']))
        return real(self,job,asset)
    monkeypatch.setattr(ReplayDeliveryV5,'run',expire)
    with pytest.raises((HubError,RuntimeError)):f.service.process(f.job)
    state=f.db.get(f.job['job_id'])['result'][STATE];assert Path(state['source_asset']['file']).is_file()
    assert not (Path(state['source_asset']['file']).parent/'delivery-v5').exists()
