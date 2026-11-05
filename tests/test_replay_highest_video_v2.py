"""Owned files/SQLite and real FFmpeg only. No account, API or publication."""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import uuid

import pytest

from test_short_video_web import short_video,PRINCIPAL
from test_replay_video_web import write_policy,CHANNELS
from test_cover_normalizer_v4 import make_jpeg
from adapters.media_tools import inspect_media,packet_evidence
from services.hub.cover_normalizer_v4 import normalize_cover
from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.replay_highest_video_v2 import (HighestReplayVideoWeb,HighestReplayMachineChecks,
    highest_quality_check,actual_start,highest_replay_manifest,STATE,POLICY,DOWNLOAD_STATE,DOWNLOAD_VERSION,
    DOWNLOAD_JOB_TYPE,DOWNLOAD_POLICY,QUALITY,PILOT_SOURCE,OPERATING_NOT_BEFORE,MAX_BYTES)
from services.hub.replay_highest_limits_v2 import native_limit_evidence
from services.hub.youtube_download import digest
from services.hub.youtube_highest_plan_v2 import source_plan

UID='1000000000000000'
def sha(file):return hashlib.sha256(file.read_bytes()).hexdigest()
def ref(file):return {'file':str(file),'bytes':file.stat().st_size,'sha256':sha(file)}
def put(file,value):file.write_text(encode(value),'utf-8');return ref(file)


def limit_fixture(s):
    directory=s.root/'config/replay-highest-native-limits-v2';directory.mkdir(parents=True)
    instant='2026-11-01T00:00:00Z';url='https://member.bilibili.com/platform/upload/video/frame'
    raw=put(directory/'account-raw.json',{'site_uid':UID,'method':'visible_native_avatar_href','same_browser_context':True})
    account=put(directory/'account.json',{'version':'bilibili-native-uid-operator-association-v2','site_uid':UID,
        'method':'existing_native_uid_record','identity_context':'server_bilibili_video','limits_context':'local_iab',
        'same_context_as_limits':False,'operator_reference':'Owned association, no live user or account','raw_identity':raw})
    dom=put(directory/'native.json',{'url':url,'observed_at':instant,'body':'Owned native limits: 视频大小16G以内，时长10小时以内'})
    # Actual decoded image bytes, retained as JPEG irrespective of file suffix.
    image=directory/'native-image.bin';make_jpeg(directory/'owned.jpg');image.write_bytes((directory/'owned.jpg').read_bytes())
    record={'version':'bilibili-highest-native-limit-binding-v2','site_uid':UID,'url':url,'observed_at':instant,
        'account_correlation_reference':'Owned explicit local-IAB/server UID association, not same-context observation',
        'dom':dom,'account_identity':account,'screenshot':ref(image),'max_file_bytes':16_000_000_000,
        'max_duration_seconds':36000,'server_file_hash_verified':False,'human_review':False}
    put(directory/'binding.json',record)
    return sha(directory/'binding.json')


def contract(*,start='2026-10-31T16:00:36Z',rate='60/1',video_bytes=10505641986,audio_bytes=416163003):
    # Use the downloader's public plan producer; QA recomputes its arithmetic.
    plan=source_plan(video_bytes,audio_bytes,max_source_bytes=MAX_BYTES,max_output_bytes=MAX_BYTES,request_cap=2048,actual_start_time=start)
    payload={'source_api':'youtube-data-v3-official','raw':{'id':PILOT_SOURCE,'liveStreamingDetails':{'actualStartTime':start}}}
    source={'job_type':DOWNLOAD_JOB_TYPE,'policy_version':DOWNLOAD_POLICY,'source_id':PILOT_SOURCE,'payload':payload}
    binding={'source_plan':plan,'policy_sha256':'b'*64,'quality_profile':QUALITY,
        'operating_not_before':OPERATING_NOT_BEFORE,'source_actual_start_time':plan['source_actual_start_time']}
    config={'video':{'codec_name':'h264','width':1920,'height':1080,'avg_frame_rate':rate,'r_frame_rate':rate},
        'audio':{'codec_name':'aac','sample_rate':'48000','avg_frame_rate':'0/0','r_frame_rate':'0/0'}}
    asset={'source_plan':plan,'selected_formats':plan['selected_formats'],'bytes':10000,'stream_config':deepcopy(config)}
    receipt={'source_plan':deepcopy(plan),'selected_formats':deepcopy(plan['selected_formats']),
        'source_stream_config':deepcopy(config),'source_to_merged_packet_coverage_verified':True}
    return source,binding,asset,receipt,{'stream_config':config}


def test_highest_plan_keeps_full_1092gb_and_exact_finite_budget():
    args=contract();quality=highest_quality_check(*args)
    assert quality['source_bytes']==10921804989
    assert quality['working_bytes_excluding_disk_floor']==21939609978
    assert quality['planned_output_max_bytes']==10985804989
    assert quality['source_actual_start_time']=='2026-10-31T16:00:36Z'
    assert quality['server_file_hash_verified'] is quality['human_review'] is False
    assert quality['silent_quality_fallback_allowed'] is False


def test_small_owned_plan_uses_minimum_64_redirect_allowance():
    args=contract(video_bytes=100000,audio_bytes=10000)
    assert args[1]['source_plan']['redirect_resume_request_allowance']==64
    assert highest_quality_check(*args)['source_bytes']==110000


@pytest.mark.parametrize('rate',['60/1','120/2','60000/1001'])
def test_actual_rational_fps_is_checked_without_relabeling_as_exact60(rate):
    args=contract(rate=rate)
    if rate=='120/2':args[2]['stream_config']['video']['avg_frame_rate']='60/1'
    result=highest_quality_check(*args)
    assert result['avg_frame_rate']==rate


@pytest.mark.parametrize('change',['fps30','bad_codec','scaled720','workspace','requests','output_limit','source_config','missing_start','scheduled_only','before_november','date_binding'])
def test_quality_or_actual_date_mismatch_stops_without_fallback(change):
    source,binding,asset,receipt,details=contract()
    if change=='fps30':details['stream_config']['video']['avg_frame_rate']='30/1'
    elif change=='bad_codec':details['stream_config']['video']['codec_name']='av1'
    elif change=='scaled720':details['stream_config']['video']['height']=720
    elif change=='workspace':binding['source_plan']['workspace_overhead_bytes']=0
    elif change=='requests':binding['source_plan']['max_http_requests']-=1
    elif change=='output_limit':asset['bytes']=14_000_000_001
    elif change=='source_config':receipt['source_stream_config']['video']['width']=1280
    elif change in ('missing_start','scheduled_only'):
        source['payload']['raw']['liveStreamingDetails'].pop('actualStartTime')
        if change=='scheduled_only':source['payload']['raw']['liveStreamingDetails']['scheduledStartTime']='2026-11-01T00:00:00Z'
    elif change=='before_november':
        source['payload']['raw']['liveStreamingDetails']['actualStartTime']='2026-10-31T15:59:59Z'
        binding['source_actual_start_time']=binding['source_plan']['source_actual_start_time']='2026-10-31T15:59:59Z'
    else:binding['source_plan']['operating_not_before']='2026-01-01T00:00:00Z'
    with pytest.raises(HubError):highest_quality_check(source,binding,asset,receipt,details)


@pytest.fixture
def highest(short_video):
    s,db,_,source,metadata,_=short_video
    s.config.update(target_bilibili_site_uid=UID)
    proof_sha=limit_fixture(s)
    policy={'version':POLICY,'enabled':True,'target_site_uid':UID,'target_account_name':s.config['target_bilibili_account_name'],
        'public_submission_mode':HighestReplayVideoWeb.PUBLIC_MODE,'owner_authorization_reference':'Owned workflow only',
        'owner_permission_reference':'Owned source permission, no real permission represented',
        'workflow_authorization_reference':'Owned highest-v2 workflow; not a v1 grant','allowed_channel_ids':CHANNELS,
        'source_scope':'pilot_single','acceptance_video_ids':[PILOT_SOURCE],'activated_at':OPERATING_NOT_BEFORE,
        'automation_enabled':False,'native_upload_limits_sha256':proof_sha}
    policy_file=s.root/'config/replay-highest-video-web-policy.toml';write_policy(policy_file,policy)
    current=db.get(source);old=current['result'].pop('youtube_download_v1')
    directory=s.root/'media'/source/'youtube-highest-v2';directory.mkdir(parents=True)
    file=directory/'original.mp4';Path(old['source_asset']['file']).replace(file)
    src,binding,asset,receipt,_=contract(video_bytes=100000,audio_bytes=10000)
    payload={'video_id':PILOT_SOURCE,'channel_id':CHANNELS[0],'source_api':'youtube-data-v3-official',
        'url':'https://www.youtube.com/watch?v='+PILOT_SOURCE,'title':'Owned highest replay','duration':'PT1S',
        'published_at':'2026-10-31T14:00:12Z','raw':{'id':PILOT_SOURCE,'snippet':{'channelId':CHANNELS[0],'title':'Owned highest replay'},
        'status':{'privacyStatus':'public'},'contentDetails':{'duration':'PT1S'},
        'liveStreamingDetails':{'actualStartTime':'2026-10-31T16:00:36Z','actualEndTime':'2026-10-31T16:00:37Z'}}}
    binding.update(version=DOWNLOAD_VERSION,job_id=source,video_id=PILOT_SOURCE,channel_id=CHANNELS[0],
        duration=1,max_bytes=MAX_BYTES,source_payload_sha256=digest(payload))
    asset.update(file=str(file),bytes=file.stat().st_size,sha256=sha(file),duration=1,stream_types={'0':'video','1':'audio'})
    state={'version':DOWNLOAD_VERSION,'state':'verified','binding':binding,'source_asset':asset,'receipt':receipt,
        'human_review':False,'publication_authorized':False}
    current['result'][DOWNLOAD_STATE]=state
    with db.transaction() as con:
        con.execute('UPDATE jobs SET source_id=?,job_type=?,policy_version=?,payload=?,result=? WHERE job_id=?',
            (PILOT_SOURCE,DOWNLOAD_JOB_TYPE,DOWNLOAD_POLICY,encode(payload),encode(current['result']),source))
    metadata=deepcopy(metadata);metadata.pop('category_id');metadata.update(category_label='娱乐',source=payload['url'],description='Owned fixture\n'+payload['url'])
    fixture={'s':s,'db':db,'service':HighestReplayVideoWeb(s,db),'source':source,'metadata':metadata,'policy':policy,'policy_file':policy_file,'file':file}
    persist_source(fixture)
    return fixture


def persist_source(f):
    current=f['db'].get(f['source']);state=current['result'][DOWNLOAD_STATE]
    put(f['file'].parent/'verified.json',{'version':DOWNLOAD_VERSION,'binding_sha256':digest(state['binding']),
        'source_asset':state['source_asset'],'receipt':state['receipt'],'human_review':False,'publication_authorized':False,
        'creator_original_bytes_verified':False})


def test_new_contract_candidate_and_tombstone_use_global_cross_version_dedup(highest):
    f=highest;service=f['service'];db=f['db'];job=service.create_candidate(PRINCIPAL,f['source'],f['metadata'])
    assert service.create_candidate(PRINCIPAL,f['source'],f['metadata'])==job
    assert db.get(job)['result'][STATE]['public_submission_approved'] is False
    # Existing source publication of any kind/version remains reserved, even
    # after the owner deleted it remotely; no new v2 job/reset is permitted.
    with db.transaction() as con:
        con.execute('DELETE FROM jobs WHERE job_id=?',(job,))
        con.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',('a'*64,f['source'],'bilibili','youtube',PILOT_SOURCE,
            'site_uid:'+UID,'video_website_short_v1','owner_deleted','BV0000000000','https://www.bilibili.com/video/BV0000000000',0))
    with pytest.raises(HubError,match='EXISTING_PUBLICATION'):service.create_candidate(PRINCIPAL,f['source'],f['metadata'])


def test_cutoff_rechecked_before_prepare_and_final_submit_even_after_candidate(highest):
    f=highest;service=f['service'];job=service.create_candidate(PRINCIPAL,f['source'],f['metadata'])
    current=f['db'].get(f['source']);cp=current['result'][DOWNLOAD_STATE]
    current['payload']['raw']['liveStreamingDetails']['actualStartTime']='2026-10-31T15:59:59Z'
    current['payload']['raw']['liveStreamingDetails']['actualEndTime']='2026-10-31T16:00:00Z'
    cp['binding']['source_payload_sha256']=digest(current['payload'])
    cp['binding']['source_actual_start_time']=cp['binding']['source_plan']['source_actual_start_time']='2026-10-31T15:59:59Z'
    cp['source_asset']['source_plan']=deepcopy(cp['binding']['source_plan']);cp['receipt']['source_plan']=deepcopy(cp['binding']['source_plan'])
    with f['db'].transaction() as con:con.execute('UPDATE jobs SET payload=?,result=? WHERE job_id=?',(encode(current['payload']),encode(current['result']),f['source']))
    persist_source(f)
    for action in (lambda:service.start_upload(PRINCIPAL,job,attempt_id=str(uuid.uuid4()),site_uid=UID),
                   lambda:service.reserve_submit(PRINCIPAL,job,attempt_id=str(uuid.uuid4()),site_uid=UID,upload_reference_sha256='b'*64)):
        with pytest.raises(HubError,match='BEFORE_OPERATING_START'):action()
    assert not f['db'].query('SELECT * FROM publications')


def test_limit_evidence_preserves_real_encoding_and_explicit_context_association(highest):
    f=highest;result=native_limit_evidence(f['s'],f['policy'])
    assert result['screenshot']['mime_type']=='image/jpeg'
    assert result['same_context_as_limits'] is False
    assert result['fresh_native_uid_and_limit_check_required'] is True
    assert result['identity_context']=='server_bilibili_video' and result['limits_context']=='local_iab'
    assert result['server_file_hash_verified'] is result['human_review'] is False
    assert 'site_uid' not in json.loads(Path(result['dom']['file']).read_bytes())


def test_changed_native_raw_screenshot_or_identity_fails_closed(highest):
    f=highest;image=f['s'].root/'config/replay-highest-native-limits-v2/native-image.bin';image.write_bytes(b'not the observed JPEG')
    with pytest.raises(HubError,match='NATIVE_LIMIT_EVIDENCE'):f['service'].policy()


def test_disk_floor_is_checked_before_real_media_scan(highest,monkeypatch):
    from services.hub import maintenance
    f=highest;monkeypatch.setattr(maintenance.shutil,'disk_usage',lambda _:SimpleNamespace(free=14_999_999_999))
    with pytest.raises(HubError,match='DISK_LOW'):HighestReplayMachineChecks(f['s'],f['db']).verify()
    f['s'].config['disk_floor_gb']=14
    with pytest.raises(HubError,match='DISK_FLOOR'):HighestReplayMachineChecks(f['s'],f['db']).verify()


def test_real_ffmpeg_1080p60_full_packet_cover_and_v2_proof(highest):
    f=highest;file=f['file']
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=1920x1080:r=60','-f','lavfi','-i',
        'sine=frequency=440:sample_rate=48000','-t','1','-c:v','libx264','-preset','ultrafast','-threads','1','-pix_fmt','yuv420p',
        '-c:a','aac','-y',str(file)],check=True,capture_output=True,timeout=30)
    details=inspect_media(file,include_frame_rates=True);packets=packet_evidence(file,details['stream_types'])
    assert 'avg_frame_rate' not in inspect_media(file)['stream_config']['video']
    assert details['stream_config']['video']['avg_frame_rate']=='60/1'
    current=f['db'].get(f['source']);state=current['result'][DOWNLOAD_STATE]
    asset=state['source_asset'];asset.update(bytes=file.stat().st_size,sha256=sha(file),duration=details['duration'],
        stream_config=details['stream_config'],stream_types=details['stream_types'],packet_evidence=packets)
    state['receipt']['source_stream_config']=deepcopy(details['stream_config'])
    with f['db'].transaction() as con:con.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(current['result']),f['source']))
    persist_source(f);job=f['service'].create_candidate(PRINCIPAL,f['source'],f['metadata']);attempt=str(uuid.uuid4())
    f['service'].bind_browser_attempt(PRINCIPAL,job,attempt_id=attempt)
    original=f['s'].root/'media'/job/'short-video-attempts'/attempt/'cover-source-official-maxres.jpg';original.parent.mkdir(parents=True);make_jpeg(original)
    cover=normalize_cover(root=f['s'].root,job_id=job,source_video_id=PILOT_SOURCE,source_file=original,
        source_sha256=sha(original),source_url='https://i.ytimg.com/vi/'+PILOT_SOURCE+'/maxresdefault.jpg')
    proof=f['service'].verify_for_publish(job,cover_provenance_file=cover['provenance_file'])
    assert proof['version']=='verified-replay-highest-machine-checks-v2'
    assert proof['checks']['quality']['avg_frame_rate']=='60/1'
    assert proof['checks']['download']['packet_evidence']==packets
    assert proof['checks']['cover']['source_rgb_sha256']==proof['checks']['cover']['foreground_rgb_sha256']
    assert proof['checks']['official_metadata']['actual_start_time']=='2026-10-31T16:00:36Z'
    assert proof['publication_authorized'] is proof['human_review'] is False
    current,binding,checkpoint=f['service'].snapshot(job)
    manifest=highest_replay_manifest(f['s'],f['db'],current,binding,checkpoint,attempt,duration_seconds=1)
    assert manifest['version']=='replay-highest-native-browser-manifest-v2'
    assert '60.0' in manifest['highest_contract']['download_binding_canonical']
    assert not f['db'].query('SELECT * FROM publications')


@pytest.mark.parametrize('scenario',['upload_once','native_limit_changed','receipt_changed','cutoff_at_submit','prepare_submit','public_coverage'])
def test_highest_native_route_on_owned_real_chromium(highest,scenario):
    f=highest
    if scenario=='public_coverage':
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=1920x1080:r=60','-f','lavfi','-i',
            'sine=frequency=440:sample_rate=48000','-t','1','-c:v','libx264','-preset','ultrafast','-threads','1','-pix_fmt','yuv420p',
            '-c:a','aac','-y',str(f['file'])],check=True,capture_output=True,timeout=30)
        details=inspect_media(f['file'],include_frame_rates=True);current=f['db'].get(f['source']);cp=current['result'][DOWNLOAD_STATE]
        cp['source_asset'].update(bytes=f['file'].stat().st_size,sha256=sha(f['file']),duration=details['duration'],stream_config=details['stream_config'])
        cp['receipt']['source_stream_config']=deepcopy(details['stream_config'])
        with f['db'].transaction() as con:con.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(current['result']),f['source']))
        persist_source(f)
    job=f['service'].create_candidate(PRINCIPAL,f['source'],f['metadata']);attempt=str(uuid.uuid4())
    current,binding,state=f['service'].snapshot(job)
    full=scenario in ('prepare_submit','public_coverage')
    if full:
        original=f['s'].root/'media'/job/'short-video-attempts'/attempt/'cover-source-official-maxres.jpg';original.parent.mkdir(parents=True);make_jpeg(original)
        normalize_cover(root=f['s'].root,job_id=job,source_video_id=PILOT_SOURCE,source_file=original,
            source_sha256=sha(original),source_url='https://i.ytimg.com/vi/'+PILOT_SOURCE+'/maxresdefault.jpg')
    manifest=highest_replay_manifest(f['s'],f['db'],current,binding,state,attempt,duration_seconds=1,require_cover=full)
    file=f['s'].root/'owned-manifest.json';put(file,manifest)
    repo=Path(__file__).resolve().parents[1]
    env={k:os.environ[k] for k in ('PATH','SystemRoot','SYSTEMROOT','SystemDrive','SYSTEMDRIVE','ProgramData','PROGRAMDATA',
        'ALLUSERSPROFILE','LOCALAPPDATA','APPDATA','USERPROFILE','TEMP','TMP','HOME','XDG_CONFIG_HOME','XDG_CACHE_HOME','LANG','LC_ALL') if k in os.environ}
    env['PLAYWRIGHT_BROWSERS_PATH']=str(repo/'compositor/runtime/browsers')
    result=subprocess.run(['node',str(repo/'tests/replay_highest_native_v2_fixture.mjs'),str(f['s'].root),str(file),scenario],
        cwd=repo,env=env,capture_output=True,text=True,encoding='utf-8',timeout=45)
    assert result.returncode==0,result.stderr
    receipt=json.loads(result.stdout.strip().splitlines()[-1])
    assert receipt['scenario']==scenario and receipt['submits']==(1 if full else 0) and receipt['unowned_http_responses']==0
    assert receipt['live_tested'] is receipt['human_review'] is False
    if scenario=='public_coverage':
        f['service'].start_upload(PRINCIPAL,job,attempt_id=attempt,site_uid=UID)
        output=f['s'].root/'media'/job/'replay-browser-highest-v2'/attempt
        f['service'].record_upload(PRINCIPAL,job,json.loads((output/'upload-observation.json').read_bytes()))
        current,binding,state=f['service'].snapshot(job)
        observed=json.loads((output/'owned-public-result.json').read_bytes())
        extra=f['service']._result_extra_state(current,binding,state,observed,lookup=True)
        assert extra['cleanup_coverage']['public_player']['ready_state']>=2
        assert extra['cleanup_coverage']['upload_coverage_ref']==state['upload_observation']['upload_coverage']
        file=Path(observed['cleanup_coverage']['file']);invalid=json.loads(file.read_bytes());invalid['public_player']['duration_seconds']=30
        put(file,invalid);observed['cleanup_coverage']=ref(file)
        with pytest.raises(HubError,match='COVERAGE'):f['service']._result_extra_state(current,binding,state,observed,lookup=True)
