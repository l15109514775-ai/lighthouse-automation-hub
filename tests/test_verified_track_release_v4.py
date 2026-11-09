from pathlib import Path as _RecoveryPath
from services.hub.locks import exclusive as _recovery_lock
"""Owned real tracks/merge/packets/SQLite, never YouTube or Bilibili calls."""
from copy import deepcopy
import json,time
from pathlib import Path

import pytest

from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.evidence import sha256_file
from services.hub.replay_fifo_v4 import POLICY_FILE as FIFO_POLICY_FILE
from services.hub.replay_multipart_v4 import (DEFAULT_POLICY,plan_whole_replay,evaluate_whole_resources,
    validate_planning_policy,digest)
from services.hub.verified_track_release_v4 import VerifiedTrackReleaseV4,VERSION,TABLE
from services.hub.youtube_download_v4 import STATE,transfer_plan,validate_transfer_plan
from adapters.ytdlp_whole_v4 import folder,needed_disk
from test_youtube_download_v4 import whole as original_whole
from test_replay_fifo_v4 import fifo
from test_ytdlp_highest_v2 import tracks
from test_replay_multipart_v4 import evidence


@pytest.fixture
def releasing(fifo,tracks,monkeypatch):
    f=fifo;f.whole['raw_track_release_after_verified_merge']=True
    f.p['planning_policy_sha256']=digest(f.whole)
    (f.s.root/'config'/FIFO_POLICY_FILE).write_text(encode(f.p),'utf-8')
    return original_whole.__wrapped__(f,tracks,monkeypatch)


class OwnedPause(Exception):pass
def durable_merge(f,monkeypatch):
    original=VerifiedTrackReleaseV4.run
    def stop(*a,**k):raise OwnedPause()
    monkeypatch.setattr(VerifiedTrackReleaseV4,'run',stop)
    with pytest.raises(OwnedPause):f.service.process(f.job)
    monkeypatch.setattr(VerifiedTrackReleaseV4,'run',original)
    current=f.db.get(f.job['job_id']);assert current['result'][STATE]['state']=='verified'
    binding=current['result'][STATE]['binding'];area=folder(f.s,f.job['job_id'])
    return binding,area


def raw_paths(binding,area):return [area/('stream-'+p['format_id']+'.'+p['ext']) for p in binding['source_plan']['selected_formats']]
def guard(f,binding):return lambda:f.service.check_binding(f.job,binding)


def test_two_phase_resource_math_is_opt_in_and_admits_only_the_real_lower_peak():
    raw,info,when,now=evidence(seconds=28000,video_bytes=17_000_000_000,audio_bytes=1_000_000_000)
    old=plan_whole_replay(raw,info,observed_at=when,now=now,policy=deepcopy(DEFAULT_POLICY))
    policy={**deepcopy(DEFAULT_POLICY),'raw_track_release_after_verified_merge':True}
    new=plan_whole_replay(raw,info,observed_at=when,now=now,policy=policy)
    b=new['whole_budget'];s=b['source_tracks_bytes'];m=b['merged_max_bytes'];parts=b['segment_output_max_bytes'];o=b['workspace_overhead_bytes']
    assert b['required_working_bytes']==max(s+m,m+parts)+o and b['source_tracks_must_be_released_before_split'] is True
    assert old['whole_budget']['required_working_bytes']==s+m+parts+o and 'resource_model' not in old['whole_budget']
    assert evaluate_whole_resources(new,55_000_000_000)['state']=='ready'
    assert evaluate_whole_resources(old,55_000_000_000)['state']=='waiting_resource'
    assert validate_transfer_plan(transfer_plan(new))['required_working_bytes']==b['required_working_bytes']
    for value in (1,'true',None):
        with pytest.raises(HubError):validate_planning_policy({**deepcopy(DEFAULT_POLICY),'raw_track_release_after_verified_merge':value})


def test_real_merge_checkpoint_then_exact_raw_release_then_parts_preserves_complete_master(releasing):
    f=releasing;inputs=[p.read_bytes() for p in f.inputs]
    result=f.service.process(f.job);state=result[STATE];area=Path(state['source_asset']['file']).parent
    row=f.db.query('SELECT * FROM '+TABLE)[0];record=json.loads(row['record'])
    assert record['state']=='released' and all(v=='deleted' for v in record['progress'].values())
    assert all(not p.exists() for p in raw_paths(state['binding'],area)) and Path(state['source_asset']['file']).is_file()
    assert sha256_file(state['source_asset']['file'])==state['source_asset']['sha256'] and state['parts']['parts']
    assert state['raw_track_custody_v4']['raw_container_bytes_recoverable'] is False
    assert record['encoded_payload_preserved'] is True and record['cloud_backup_claimed'] is False
    assert (area/'verified.json').exists() and (area/'raw-track-custody-v4.json').exists()
    assert [p.read_bytes() for p in f.inputs]==inputs and f.calls==['owned_track_copy_not_youtube']
    assert encode(f.service.process(f.job))==encode(result) and not f.db.query('SELECT * FROM publications')


@pytest.mark.parametrize('changed',['raw','master'])
def test_same_size_corruption_never_deletes_raw(releasing,monkeypatch,changed):
    f=releasing;b,area=durable_merge(f,monkeypatch);paths=raw_paths(b,area)
    file=paths[0] if changed=='raw' else area/'original.mp4';data=file.read_bytes();file.write_bytes(bytes([data[0]^1])+data[1:])
    with pytest.raises(HubError):VerifiedTrackReleaseV4(f.s,f.db).run(f.job,b,check=guard(f,b))
    assert all(p.exists() for p in paths) and (area/'original.mp4').exists()


def test_packet_mismatch_is_not_accepted_from_receipt_boolean(releasing,monkeypatch):
    f=releasing;b,area=durable_merge(f,monkeypatch)
    import services.hub.verified_track_release_v4 as module
    real=module.source_packet_evidence
    def corrupt(*a,**k):
        value=real(*a,**k);value['audio']['chain']='0'*64;return value
    monkeypatch.setattr(module,'source_packet_evidence',corrupt)
    with pytest.raises(HubError,match='PACKET_MISMATCH'):VerifiedTrackReleaseV4(f.s,f.db).run(f.job,b,check=guard(f,b))
    assert all(p.exists() for p in raw_paths(b,area))


def test_unknown_partial_and_shared_reference_are_not_disposal_candidates(releasing,monkeypatch):
    f=releasing;b,area=durable_merge(f,monkeypatch);partial=area/'stream-owned.mp4.part';partial.write_bytes(b'owned partial unknown')
    with pytest.raises(HubError,match='UNRESOLVED_PARTIAL'):VerifiedTrackReleaseV4(f.s,f.db).run(f.job,b,check=guard(f,b))
    assert partial.read_bytes()==b'owned partial unknown' and all(p.exists() for p in raw_paths(b,area))
    partial.unlink()  # Only this test's own temporary fixture; not runtime code.
    f.db.enqueue('maintenance','owned-reader','owned-reader','system',{'file':str(raw_paths(b,area)[0])},'owned')
    with pytest.raises(HubError,match='SHARED_SOURCE'):VerifiedTrackReleaseV4(f.s,f.db).run(f.job,b,check=guard(f,b))
    assert all(p.exists() for p in raw_paths(b,area))


def test_exit_after_first_unlink_recovers_same_job_and_never_downloads_again(releasing,monkeypatch):
    f=releasing;b,area=durable_merge(f,monkeypatch);service=VerifiedTrackReleaseV4(f.s,f.db);original=service._unlink;called=[]
    def exit_after(item,**kwargs):called.append(item['file']);original(item,**kwargs);raise OSError('owned crash after first unlink')
    monkeypatch.setattr(service,'_unlink',exit_after)
    with pytest.raises(OSError):service.run(f.job,b,check=guard(f,b))
    paths=raw_paths(b,area);assert not paths[0].exists() and paths[1].exists() and (area/'original.mp4').exists()
    assert json.loads(f.db.query('SELECT record FROM '+TABLE)[0]['record'])['progress'][str(paths[0])]=='deleting'
    with _recovery_lock(_RecoveryPath(f.db.path).parent / "locks" / 'video'):
        f.db.recover('video',4,all_processing=True)
        f.job=f.db.claim('video',180,4)
    assert f.job['attempts']==2
    result=f.service.process(f.job);assert result[STATE]['state']=='parts_verified' and all(not p.exists() for p in paths)
    assert f.calls==['owned_track_copy_not_youtube'] and len(called)==1


def test_lease_lost_after_intent_persist_stops_before_unlink(releasing,monkeypatch):
    f=releasing;b,area=durable_merge(f,monkeypatch);service=VerifiedTrackReleaseV4(f.s,f.db);original=service._persist
    def expired(job,binding,record,**kw):
        value=original(job,binding,record,**kw)
        if any(v=='deleting' for v in record['progress'].values()):
            with f.db.transaction() as db:db.execute('UPDATE jobs SET lease_until=? WHERE job_id=?',(time.time()-1,job['job_id']))
        return value
    monkeypatch.setattr(service,'_persist',expired)
    with pytest.raises(HubError):service.run(f.job,b,check=guard(f,b))
    assert all(p.exists() for p in raw_paths(b,area))


def test_missing_raw_without_durable_deleting_intent_is_not_assumed_released(releasing,monkeypatch):
    f=releasing;b,area=durable_merge(f,monkeypatch);raw_paths(b,area)[0].unlink()
    with pytest.raises((HubError,OSError)):VerifiedTrackReleaseV4(f.s,f.db).run(f.job,b,check=guard(f,b))
    assert raw_paths(b,area)[1].exists() and (area/'original.mp4').exists()


def test_merged_tamper_after_release_stops_recovery_without_new_network(releasing,monkeypatch):
    f=releasing;result=f.service.process(f.job);file=Path(result[STATE]['source_asset']['file']);data=file.read_bytes()
    file.write_bytes(bytes([data[0]^1])+data[1:])
    with pytest.raises(HubError):f.service.process(f.job)
    assert f.calls==['owned_track_copy_not_youtube'] and not f.db.query('SELECT * FROM api_calls')


def test_long_packet_scan_does_not_fake_source_freshness_or_demand_repeated_api(releasing,monkeypatch):
    f=releasing;b,area=durable_merge(f,monkeypatch);old=time.time()-301
    with f.db.transaction() as db:db.execute('UPDATE youtube_videos SET last_api_check=? WHERE video_id=?',(old,f.job['source_id']))
    result=f.service.process(f.job)
    assert result[STATE]['state']=='parts_verified' and f.calls==['owned_track_copy_not_youtube']
    assert f.db.query('SELECT last_api_check FROM youtube_videos')[0]['last_api_check']==old
    assert not f.db.query('SELECT * FROM api_calls')


def test_low_disk_custody_phase_uses_finite_same_job_resource_wait_without_deleting(releasing,monkeypatch):
    f=releasing;b,area=durable_merge(f,monkeypatch)
    import services.hub.verified_track_release_v4 as module
    from services.hub.youtube_download_v4 import WholeResourceDeferredV4
    from types import SimpleNamespace
    required=b['planning_policy']['disk_floor_bytes']+b['planning_policy']['workspace_overhead_bytes']
    monkeypatch.setattr(module.shutil,'disk_usage',lambda _:SimpleNamespace(free=required-1))
    with pytest.raises(WholeResourceDeferredV4) as caught:f.service.process(f.job)
    assert caught.value.required_bytes==required and all(p.exists() for p in raw_paths(b,area))
    assert f.db.get(f.job['job_id'])['attempts']==1 and f.calls==['owned_track_copy_not_youtube']
