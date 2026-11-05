"""Owned plans and real synthetic FFmpeg parts. No account/download/publication."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from adapters.media_tools import empty_evidence,inspect_media,packet_evidence
from services.hub.errors import HubError
from services.hub.evidence import sha256_file
from services.hub.locks import resource_guard
from services.hub.replay_multipart_v4 import (DEFAULT_POLICY,MultipartMediaProcessingV4,digest,
    evaluate_whole_resources,plan_whole_replay,validate_whole_replay_plan,validate_planning_policy)
from services.hub.youtube_download import duration_seconds
from services.hub.youtube_natural_plan_v3 import natural_source_plan
from test_media_processing import make_video,recover


def evidence(*,seconds=108000,video_bytes=25_000_000_000,audio_bytes=1_000_000_000,width=1920,height=1080,fps=60):
    start=datetime(2026,10,31,16,tzinfo=timezone.utc);end=start+timedelta(seconds=seconds)
    raw={'id':'ownedNew001','snippet':{'channelId':'UCaaaaaaaaaaaaaaaaaaaaaa','publishedAt':'2026-07-01T00:00:00Z'},
        'status':{'privacyStatus':'public'},'contentDetails':{'duration':f'PT{seconds}S'},
        'liveStreamingDetails':{'actualStartTime':start.isoformat(),'actualEndTime':end.isoformat()}}
    formats=[{'format_id':'ownedVideo','ext':'mp4','protocol':'https','vcodec':'avc1.640028','acodec':'none',
            'width':width,'height':height,'fps':fps,'filesize':video_bytes,'tbr':4000},
        {'format_id':'ownedAudio','ext':'m4a','protocol':'https','vcodec':'none','acodec':'mp4a.40.2','filesize':audio_bytes,'abr':128}]
    info={'id':raw['id'],'channel_id':raw['snippet']['channelId'],'duration':seconds,'formats':formats}
    observed=end+timedelta(seconds=10)
    return raw,info,observed.isoformat(),observed.timestamp()


def plan(policy=None,**kw):
    raw,info,observed,now=evidence(**kw)
    return plan_whole_replay(raw,info,observed_at=observed,now=now,policy=policy or deepcopy(DEFAULT_POLICY))


def test_whole_30h_26gb_is_planned_without_relaxing_v3_or_claiming_multipart_connected():
    raw,info,observed,now=evidence();policy=deepcopy(DEFAULT_POLICY)
    value=plan_whole_replay(raw,info,observed_at=observed,policy=policy,now=now)
    assert value['duration']==108000 and value['source_plan_basis']['source_bytes']==26_000_000_000
    assert value['split_policy']['max_part_seconds']==36000 and value['split_policy']['max_part_bytes']==14_000_000_000
    assert value['source_plan_basis']['highest_tier']=={'height':1080,'fps':60.0}
    assert value['publishable'] is value['native_multipart_verified'] is value['download_authorized_by_plan'] is False
    assert policy['enabled'] is False and 'activated_at' not in value['source_plan_basis']
    validate_whole_replay_plan(value,policy,official_raw=raw)
    with pytest.raises(HubError):natural_source_plan(raw,info,activated_at=policy['source_not_before'],observed_at=observed,now=now)
    with pytest.raises(HubError):duration_seconds('PT30H')
    assert duration_seconds('PT30H',maximum=36000*32)==108000


def test_peak_disk_reserves_tracks_merged_all_parts_and_15gib_then_waits_without_changing_plan():
    value=plan();before=deepcopy(value);p=DEFAULT_POLICY;b=value['whole_budget'];basis=value['source_plan_basis']
    assert b['required_working_bytes']==basis['source_bytes']+2*basis['planned_output_max_bytes']+64_000_000*32+32_000_000
    assert b['required_free_bytes']==b['required_working_bytes']+15*1024**3
    assert evaluate_whole_resources(value,b['required_free_bytes']-1)['state']=='waiting_resource'
    assert evaluate_whole_resources(value,b['required_free_bytes'])['state']=='ready' and value==before
    assert b['disk_floor_bytes']==p['disk_floor_bytes']


@pytest.mark.parametrize('change',['missing_start','before_november','unknown_size','higher_unsupported_tier'])
def test_missing_date_or_highest_track_evidence_does_not_downgrade_or_guess(change):
    raw,info,observed,now=evidence()
    if change=='missing_start':raw['liveStreamingDetails'].pop('actualStartTime')
    elif change=='before_november':raw['liveStreamingDetails']['actualStartTime']='2026-10-31T15:59:59Z'
    elif change=='unknown_size':info['formats'][0].pop('filesize')
    else:info['formats'][0]['height']=2160
    with pytest.raises(HubError):plan_whole_replay(raw,info,observed_at=observed,now=now,policy=deepcopy(DEFAULT_POLICY))


@pytest.mark.parametrize('change',['budget','part_limit','publishable'])
def test_changed_plan_is_rebuilt_and_refused(change):
    value=plan()
    if change=='budget':value['whole_budget']['required_free_bytes']-=1
    elif change=='part_limit':value['split_policy']['max_part_seconds']=36001
    else:value['publishable']=True
    with pytest.raises(HubError):validate_whole_replay_plan(value,deepcopy(DEFAULT_POLICY))


def test_unsafe_single_part_policy_or_old_decimal_disk_floor_is_not_accepted():
    for key,value in [('max_part_seconds',36001),('max_part_bytes',16_000_000_000),('disk_floor_bytes',15_000_000_000),('enabled',1)]:
        p=deepcopy(DEFAULT_POLICY);p[key]=value
        with pytest.raises(HubError):validate_planning_policy(p)


@pytest.fixture
def owned(runtime):
    settings,store=runtime;policy=deepcopy(DEFAULT_POLICY);policy['max_part_seconds']=3
    # This job owns a synthetic source, not an asserted YouTube download receipt.
    ident=store.enqueue('youtube','ownedNew001','replay_multipart_process_v4','video',{},'replay-multipart-v4')
    root=settings.root/'media'/ident;root.mkdir();source=root/'original.mp4';make_video(source)
    details=inspect_media(source);size=source.stat().st_size
    raw,info,observed,now=evidence(seconds=6,video_bytes=size-1000,audio_bytes=1000,width=160,height=90,fps=10)
    value=plan_whole_replay(raw,info,observed_at=observed,now=now,policy=policy)
    with store.transaction() as db:
        db.execute('UPDATE jobs SET payload=? WHERE job_id=?',(json.dumps({'whole_replay_plan':value,'whole_replay_plan_sha256':digest(value)}),ident))
    job=store.claim('video',180,4);source_info={'file':str(source),'sha256':sha256_file(source),'bytes':size}
    return settings,store,job,source_info,policy,value,details


def test_real_stream_copy_preserves_all_packets_fps_and_persists_independent_prepared_parts(owned):
    s,db,job,info,p,value,details=owned
    result=MultipartMediaProcessingV4(s,db,policy=p,plan=value).run(job,info)
    assert len(result['parts'])>=2 and result['publishable'] is result['native_multipart_verified'] is False
    combined=empty_evidence()
    for part in result['parts']:
        actual=inspect_media(Path(part['file']),include_frame_rates=True)
        assert part['bytes']<=p['max_part_bytes'] and actual['duration']<=p['max_part_seconds']
        combined=packet_evidence(part['file'],actual['stream_types'],initial=combined)
    assert combined==packet_evidence(info['file'],details['stream_types'])
    assert sha256_file(info['file'])==info['sha256'] and not db.query('SELECT * FROM publications')
    rows=db.query('SELECT * FROM replay_multipart_parts_v4 WHERE job_id=? ORDER BY part_index',(job['job_id'],))
    assert len(rows)==len(result['parts']) and all(r['state']=='prepared' and r['external_publish_id'] is None for r in rows)
    assert len({r['idempotency_key'] for r in rows})==len(rows)
    document=json.loads(Path(result['manifest']).read_bytes())
    assert document['acquisition_authority_verified'] is document['publishable'] is False


def test_restart_reuses_complete_outputs_and_keys_without_split_again(owned,monkeypatch):
    s,db,job,info,p,value,_=owned
    first=MultipartMediaProcessingV4(s,db,policy=p,plan=value).run(job,info)
    refs={x['file']:(x['sha256'],Path(x['file']).stat().st_mtime_ns,x['part_idempotency_key']) for x in first['parts']}
    import services.hub.media_processing as base
    monkeypatch.setattr(base,'split_media',lambda *a,**kw:pytest.fail('Durable complete batch must not split again'))
    again=recover(s,db)
    second=MultipartMediaProcessingV4(s,db,policy=p,plan=value).run(again,info)
    assert {x['file']:(x['sha256'],Path(x['file']).stat().st_mtime_ns,x['part_idempotency_key']) for x in second['parts']}==refs


def test_tampered_derived_file_is_rejected_and_original_is_retained(owned):
    s,db,job,info,p,value,_=owned;service=MultipartMediaProcessingV4(s,db,policy=p,plan=value)
    first=service.run(job,info);file=Path(first['parts'][0]['file']);raw=file.read_bytes();file.write_bytes(bytes([raw[0]^1])+raw[1:])
    with pytest.raises(HubError,match='INTEGRITY_CHANGED'):service.run(job,info)
    assert sha256_file(info['file'])==info['sha256']


def test_insufficient_disk_or_lost_lease_stops_before_new_outputs_without_source_deletion(owned,monkeypatch):
    s,db,job,info,p,value,_=owned
    import services.hub.replay_multipart_v4 as module
    monkeypatch.setattr(module.shutil,'disk_usage',lambda _:SimpleNamespace(free=p['disk_floor_bytes']-1))
    with pytest.raises(HubError,match='DISK_LOW'):MultipartMediaProcessingV4(s,db,policy=p,plan=value).run(job,info)
    assert not list(Path(info['file']).parent.rglob('*.partial.mp4'))
    assert sha256_file(info['file'])==info['sha256']
    with db.transaction() as connection:connection.execute('UPDATE jobs SET lease_until=? WHERE job_id=?',(time.time()-1,job['job_id']))
    with pytest.raises(RuntimeError,match='LEASE_LOST'):MultipartMediaProcessingV4(s,db,policy=p,plan=value).run(job,info)


def test_unknown_external_part_state_is_not_reset_or_reprepared(owned):
    s,db,job,info,p,value,_=owned;service=MultipartMediaProcessingV4(s,db,policy=p,plan=value)
    result=service.run(job,info);before=Path(result['manifest']).read_bytes()
    with db.transaction() as connection:connection.execute("UPDATE replay_multipart_parts_v4 SET state='publish_unknown' WHERE job_id=? AND part_index=0",(job['job_id'],))
    with pytest.raises(HubError,match='EXTERNAL_STATE_RECONCILIATION_REQUIRED'):service.run(job,info)
    assert Path(result['manifest']).read_bytes()==before
    assert db.query('SELECT state FROM replay_multipart_parts_v4 WHERE job_id=? AND part_index=0',(job['job_id'],))[0]['state']=='publish_unknown'


def test_busy_heavy_lock_does_not_start_ffmpeg_or_create_part_files(owned):
    s,db,job,info,p,value,_=owned
    with resource_guard(s.root/'data/locks/heavy'):
        with pytest.raises(HubError,match='HEAVY_RESOURCE_BUSY'):MultipartMediaProcessingV4(s,db,policy=p,plan=value).run(job,info)
    assert sha256_file(info['file'])==info['sha256'] and not (Path(info['file']).parent/'parts-v4').exists()
