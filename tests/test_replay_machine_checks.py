"""Real FFmpeg/ffprobe over owned synthetic files; no network or publication."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import uuid

import pytest

from test_short_video_web import short_video, PRINCIPAL
from test_replay_video_web import replay, update_source, write_policy
from test_cover_normalizer_v4 import make_jpeg
from adapters.media_tools import inspect_media, packet_evidence
from services.hub import cover_normalizer_v4 as cover
from services.hub import replay_machine_checks as module
from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.replay_machine_checks import ReplayMachineChecks
from services.hub.youtube_download import STATE as DOWNLOAD_STATE, digest


def sha(file): return hashlib.sha256(file.read_bytes()).hexdigest()


def write_source(f, *, codec='libx264', claimed_packets=None):
    s, db, service, source, *_ = f
    current=db.get(source);state=current['result'][DOWNLOAD_STATE];file=Path(state['source_asset']['file'])
    subprocess.run(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc2=s=96x64:r=6',
        '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','2','-c:v',codec,'-threads','1',
        '-pix_fmt','yuv420p','-c:a','aac','-y',str(file)],check=True,capture_output=True,timeout=25)
    details=inspect_media(file);packets=packet_evidence(file,details['stream_types'])
    state['binding']['max_bytes']=3_800_000_000
    state['source_asset']={'file':str(file),'bytes':file.stat().st_size,'sha256':sha(file),'duration':details['duration'],
        'stream_types':{str(k):v for k,v in details['stream_types'].items()},'packet_evidence':claimed_packets or packets}
    verified={'version':state['version'],'binding_sha256':digest(state['binding']),'source_asset':state['source_asset'],
        'receipt':state['receipt'],'human_review':False,'publication_authorized':False,'creator_original_bytes_verified':False}
    (file.parent/'verified.json').write_text(encode(verified),'utf-8')
    with db.transaction() as connection:
        connection.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(current['result']),source))
    return file, packets


@pytest.fixture
def owned(replay, short_video):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Real FFmpeg and ffprobe are required'
    update_source(short_video,duration=2,end='2026-10-31T16:00:38Z')
    file, packets=write_source(replay)
    s,db,service,source,metadata,_,_=replay
    ident=service.create_candidate(PRINCIPAL,source,metadata)
    attempt=str(uuid.uuid4());original=s.root/'media'/ident/'short-video-attempts'/attempt/'cover-source-official-maxres.jpg'
    original.parent.mkdir(parents=True)
    make_jpeg(original)
    derived=cover.normalize_cover(root=s.root,job_id=ident,source_video_id='abcdefghijk',source_file=original,
        source_sha256=sha(original),source_url='https://i.ytimg.com/vi/abcdefghijk/maxresdefault.jpg')
    return {'fixture':replay,'s':s,'db':db,'service':service,'job_id':ident,'source':source,'file':file,'packets':packets,
        'original':original,'derived':derived,'checks':ReplayMachineChecks(s,db)}


def verify(f, **kwargs):
    job,binding,state=f['service'].snapshot(f['job_id'])
    return f['checks'].verify(job,binding,state,f['service'].policy(),**kwargs)


def rebind_source_receipt(f):
    db=f['db'];source=db.get(f['source']);checkpoint=source['result'][DOWNLOAD_STATE]
    receipt=Path(checkpoint['source_asset']['file']).parent/'verified.json'
    value={'version':checkpoint['version'],'binding_sha256':digest(checkpoint['binding']),'source_asset':checkpoint['source_asset'],
        'receipt':checkpoint['receipt'],'human_review':False,'publication_authorized':False,'creator_original_bytes_verified':False}
    receipt.write_text(encode(value),'utf-8')
    job=db.get(f['job_id']);binding=job['payload'];binding['download_receipt_sha256']=sha(receipt)
    binding['artifact_sha256']=checkpoint['source_asset']['sha256'];binding['file_bytes']=checkpoint['source_asset']['bytes']
    with db.transaction() as connection:
        connection.execute('UPDATE jobs SET payload=? WHERE job_id=?',(encode(binding),job['job_id']))


def test_actual_codec_packet_hash_rgb_and_persisted_body_are_verified(owned):
    before=owned['file'].read_bytes();original=owned['original'].read_bytes();count=[0]
    proof=verify(owned,check=lambda:count.__setitem__(0,count[0]+1))
    file=Path(proof['evidence_file']);body=file.read_bytes();record=json.loads(body)
    assert {k:v for k,v in proof.items() if k not in ('evidence_file','evidence_sha256','evidence_bytes')}==record
    assert proof['version']=='verified-replay-machine-checks-v1' and sha(file)==proof['evidence_sha256'] and len(body)==proof['evidence_bytes']
    assert record['checks']['media']['stream_config']['video']['codec_name']=='h264'
    assert record['checks']['media']['stream_config']['audio']['codec_name']=='aac'
    assert record['checks']['download']['packet_evidence']==owned['packets']
    rgb=record['checks']['cover'];assert rgb['compared_rgb_bytes']==2_764_800
    assert rgb['source_rgb_sha256']==rgb['foreground_rgb_sha256']
    assert record['human_review'] is record['per_artifact_owner_acceptance'] is record['publication_authorized'] is False
    assert record['external_requests']==0 and count[0]>20
    assert owned['file'].read_bytes()==before and owned['original'].read_bytes()==original
    assert not owned['db'].query('SELECT * FROM publications')


def test_a_second_call_rechecks_actual_files_and_keeps_both_receipts(owned):
    a=verify(owned);b=verify(owned)
    assert a['evidence_file']!=b['evidence_file'] and Path(a['evidence_file']).is_file()
    before=deepcopy(a['checks']);after=deepcopy(b['checks'])
    first=before['download'].pop('packet_verification');second=after['download'].pop('packet_verification')
    assert before==after and first['mode']=='full_scan' and second['mode']=='reused_after_current_full_sha'
    assert second['full_scan_receipt']['sha256']==a['evidence_sha256'] and second['packet_rescan_performed'] is False


@pytest.mark.parametrize('tool,code',[('ffmpeg','REPLAY_MACHINE_FFMPEG_MISSING'),('ffprobe','REPLAY_MACHINE_FFPROBE_MISSING')])
def test_missing_real_tool_is_error_not_a_passing_claim(owned,monkeypatch,tool,code):
    actual=module.shutil.which
    monkeypatch.setattr(module.shutil,'which',lambda name:None if name==tool else actual(name))
    with pytest.raises(HubError,match=code):verify(owned)
    assert not list((owned['s'].root/'media'/owned['job_id']).glob('replay-machine-checks-v1/*/receipt.json'))


def test_video_changed_after_download_does_not_trust_stored_verified_flag(owned):
    with owned['file'].open('ab') as stream:stream.write(b'owned mutation')
    # Call the independent checker directly so the older snapshot guard cannot
    # mask whether this module actually recomputes the physical file hash.
    job=owned['db'].get(owned['job_id'])
    with pytest.raises(HubError,match='REPLAY_MACHINE_ARTIFACT_HASH_MISMATCH'):
        owned['checks'].verify(job,job['payload'],job['result']['replay_video_web_v1'],owned['service'].policy())


def test_real_wrong_codec_rejected_even_with_matching_file_hash_and_packet_receipt(owned):
    write_source(owned['fixture'],codec='mpeg4');rebind_source_receipt(owned)
    with pytest.raises(HubError,match='REPLAY_MACHINE_MEDIA_CHANGED'):verify(owned)


def test_real_packet_scan_cannot_be_replaced_by_valid_looking_hash_claim(owned):
    db=owned['db'];source=db.get(owned['source']);state=source['result'][DOWNLOAD_STATE]
    state['source_asset']['packet_evidence']['video']['chain']='0'*64
    with db.transaction() as connection:connection.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(source['result']),source['job_id']))
    rebind_source_receipt(owned)
    with pytest.raises(HubError,match='REPLAY_MACHINE_PACKET_EVIDENCE_MISMATCH'):verify(owned)


@pytest.mark.parametrize('change',['source_api','channel','end_time','duration'])
def test_official_metadata_fields_are_cross_checked_not_self_reported(owned,change):
    db=owned['db'];source=db.get(owned['source']);payload=source['payload']
    if change=='source_api':payload['source_api']='owned-nonofficial'
    elif change=='channel':payload['raw']['snippet']['channelId']='not-the-channel'
    elif change=='end_time':payload['raw']['liveStreamingDetails'].pop('actualEndTime')
    else:payload['raw']['contentDetails']['duration']='PT20S'
    source['result'][DOWNLOAD_STATE]['binding']['source_payload_sha256']=digest(payload)
    with db.transaction() as connection:
        connection.execute('UPDATE jobs SET payload=?,result=? WHERE job_id=?',(encode(payload),encode(source['result']),source['job_id']))
    rebind_source_receipt(owned)
    job=owned['db'].get(owned['job_id']);state=job['result']['replay_video_web_v1']
    with pytest.raises(HubError,match='REPLAY_MACHINE_VERIFIED_SOURCE_REQUIRED'):
        owned['checks'].verify(job,job['payload'],state,owned['service'].policy())


def test_public_license_number_is_forbidden_while_internal_rights_remain(owned):
    db=owned['db'];job=db.get(owned['job_id']);binding=job['payload']
    binding['metadata']['description']+='\n转载许可编号：114514'
    binding['metadata_sha256']=digest(binding['metadata'])
    with db.transaction() as connection:connection.execute('UPDATE jobs SET payload=? WHERE job_id=?',(encode(binding),job['job_id']))
    job=db.get(job['job_id'])
    with pytest.raises(HubError,match='REPLAY_MACHINE_POLICY_OR_METADATA_CHANGED'):
        owned['checks'].verify(job,job['payload'],job['result']['replay_video_web_v1'],owned['service'].policy())
    assert owned['service'].policy()['owner_permission_reference']=='Owned internal rights fixture'


@pytest.mark.parametrize('change',['output_hash','filter','source_url','foreign_path','wrong_attempt'])
def test_cover_provenance_wrong_job_source_transform_or_bytes_are_rejected(owned,change):
    p=Path(owned['derived']['provenance_file']);v=json.loads(p.read_bytes())
    kwargs={}
    if change=='output_hash':v['output']['sha256']='0'*64
    elif change=='filter':v['transform']['foreground_rect']['y']=119
    elif change=='source_url':v['input']['source_url']='https://i.ytimg.com/vi/xxxxxxxxxxx/maxresdefault.jpg'
    elif change=='foreign_path':kwargs['cover_provenance_file']=owned['original']
    else:
        job=owned['db'].get(owned['job_id']);job['result']['replay_video_web_v1']['attempt_id']=str(uuid.uuid4())
        with owned['db'].transaction() as connection:connection.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(job['result']),job['job_id']))
    p.write_text(json.dumps(v),'utf-8')
    with pytest.raises(HubError,match='REPLAY_MACHINE_COVER_'):verify(owned,**kwargs)


def test_changed_center_pixels_fail_even_after_updating_png_hash_in_provenance(owned):
    p=Path(owned['derived']['provenance_file']);v=json.loads(p.read_bytes());out=Path(v['output']['file'])
    subprocess.run(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-f','lavfi','-i','color=red:s=1280x960',
        '-frames:v','1','-threads','1','-pix_fmt','rgb24','-y',str(out)],check=True,capture_output=True,timeout=20)
    v['output'].update(bytes=out.stat().st_size,sha256=sha(out));p.write_text(json.dumps(v),'utf-8')
    with pytest.raises(HubError,match='REPLAY_MACHINE_COVER_RGB_MISMATCH'):verify(owned)


def test_counterfeit_passed_field_does_not_skip_any_real_check(owned):
    job,binding,state=owned['service'].snapshot(owned['job_id'])
    forged=deepcopy(state);forged['passed']=True
    with pytest.raises(HubError,match='REPLAY_MACHINE_JOB_BINDING_CHANGED'):
        owned['checks'].verify(job,binding,forged,owned['service'].policy())


def test_persisted_passed_true_still_cannot_replace_physical_checks(owned):
    job=owned['db'].get(owned['job_id']);job['result']['replay_video_web_v1']['passed']=True
    with owned['db'].transaction() as connection:
        connection.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(job['result']),job['job_id']))
    owned['file'].write_bytes(b'owned damaged media with a fake passed flag')
    job=owned['db'].get(job['job_id'])
    with pytest.raises(HubError,match='REPLAY_MACHINE_ARTIFACT_HASH_MISMATCH'):
        owned['checks'].verify(job,job['payload'],job['result']['replay_video_web_v1'],owned['service'].policy())


def test_policy_changed_during_real_probe_prevents_receipt(owned,monkeypatch):
    actual=module.inspect_media
    def changed(file,**kwargs):
        value=actual(file,**kwargs)
        policy=owned['s'].root/'config/replay-video-web-policy.toml'
        policy.write_bytes(policy.read_bytes()+b'\n# owned concurrent change\n')
        return value
    monkeypatch.setattr(module,'inspect_media',changed)
    job=owned['db'].get(owned['job_id']);policy=owned['service'].policy()
    with pytest.raises(HubError,match='REPLAY_MACHINE_INPUT_CHANGED_DURING_CHECK'):
        owned['checks'].verify(job,job['payload'],job['result']['replay_video_web_v1'],policy)
    assert not list((owned['s'].root/'media'/owned['job_id']).glob('replay-machine-checks-v1/*/receipt.json'))


def test_lease_loss_is_propagated_and_no_proof_written(owned):
    def expired():raise HubError('OWNED_LEASE_EXPIRED',review=True)
    with pytest.raises(HubError,match='OWNED_LEASE_EXPIRED'):verify(owned,check=expired)
    assert not list((owned['s'].root/'media'/owned['job_id']).glob('replay-machine-checks-v1/*/receipt.json'))
