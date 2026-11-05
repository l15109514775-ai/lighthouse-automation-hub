"""Owned source inventories, SQLite and real local tools; never live approval."""
from copy import deepcopy
from datetime import datetime,timezone
import json,os,subprocess,uuid
from pathlib import Path
import pytest

from test_short_video_web import short_video,PRINCIPAL
from test_replay_highest_video_v2 import highest,put,sha,UID
from test_replay_video_web import write_policy,CHANNELS
from test_youtube_natural_plan_v3 import fixture as inventory_fixture
from test_cover_normalizer_v4 import make_jpeg
from adapters.media_tools import inspect_media,packet_evidence
from services.hub.cover_normalizer_v4 import normalize_cover
from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.youtube_download import digest
from services.hub.youtube_natural_plan_v3 import natural_source_plan
from services.hub.youtube_download_v3 import official_identity
from services.hub.youtube_monitor import YouTubeMonitor
from services.hub.replay_highest_video_v2 import HighestReplayVideoWeb
from services.hub.replay_natural_video_v3 import (NaturalReplayVideoWeb,NaturalReplayMachineChecks,natural_quality_check,
    natural_metadata,natural_replay_manifest,STATE,POLICY,JOB_TYPE,DOWNLOAD_VERSION,DOWNLOAD_POLICY,DOWNLOAD_STATE,QUALITY)


@pytest.fixture
def natural(highest,monkeypatch):
    f=highest;db=f['db'];raw,info=inventory_fixture();raw['snippet']['title']='Owned natural highest title'
    raw['contentDetails']['duration']='PT1S';raw['liveStreamingDetails']['actualEndTime']='2026-11-01T02:00:01Z'
    info['duration']=1;info['formats']=[info['formats'][0],info['formats'][-1]]
    info['formats'][0]['filesize']=100000;info['formats'][1]['filesize']=10000
    activation='2026-11-01T01:00:00Z'
    plan=natural_source_plan(raw,info,activated_at=activation,observed_at=datetime.now(timezone.utc).isoformat())
    source=db.get(f['source']);old=source['result'].pop('youtube_download_v2')
    directory=f['s'].root/'media'/source['job_id']/'youtube-highest-v3';directory.mkdir(parents=True)
    file=directory/'original.mp4';f['file'].replace(file)
    payload={'video_id':raw['id'],'channel_id':raw['snippet']['channelId'],'title':raw['snippet']['title'],
        'published_at':raw['snippet']['publishedAt'],'duration':'PT1S','url':'https://www.youtube.com/watch?v='+raw['id'],
        'source_api':'youtube-data-v3-official','raw':raw,'_natural_source_plan':plan,'_natural_prepare_job_id':str(uuid.uuid4())}
    binding=deepcopy(old['binding']);binding.update(version=DOWNLOAD_VERSION,video_id=raw['id'],channel_id=payload['channel_id'],
        source_payload_sha256=digest(payload),quality_profile=QUALITY,source_plan=plan,activated_at=activation,
        source_actual_start_time=plan['source_actual_start_time'])
    binding.update(official_identity=official_identity(raw),official_identity_sha256=digest(official_identity(raw)))
    asset=deepcopy(old['source_asset']);asset.update(file=str(file),source_plan=plan,selected_formats=plan['selected_formats'])
    asset['stream_config']['video'].update(width=1280,height=720,avg_frame_rate='30/1',r_frame_rate='30/1')
    receipt=deepcopy(old['receipt']);receipt.update(source_plan=plan,selected_formats=plan['selected_formats'],source_stream_config=deepcopy(asset['stream_config']))
    source['result'][DOWNLOAD_STATE]={'version':DOWNLOAD_VERSION,'state':'verified','binding':binding,'source_asset':asset,'receipt':receipt,
        'human_review':False,'publication_authorized':False}
    with db.transaction() as con:con.execute('UPDATE jobs SET source_id=?,job_type=?,policy_version=?,payload=?,result=? WHERE job_id=?',
        (raw['id'],'youtube_download_v3',DOWNLOAD_POLICY,encode(payload),encode(source['result']),source['job_id']))
    YouTubeMonitor(f['s'],db,None).observe_video(raw,payload['channel_id'])
    policy={**f['policy'],'version':POLICY,'public_submission_mode':NaturalReplayVideoWeb.PUBLIC_MODE,'source_scope':'automatic_new_only',
        'acceptance_video_ids':[],'activated_at':activation,'automation_enabled':True,'metadata_template':'source-title-and-link-v1',
        'operating_not_before':'2026-10-31T16:00:00Z'}
    file_policy=f['s'].root/'config/replay-natural-video-web-policy.toml';write_policy(file_policy,policy)
    service=NaturalReplayVideoWeb(f['s'],db)
    f.update(service=service,file=file,policy=policy,policy_file=file_policy,raw=raw,info=info,plan=plan)
    persist(f)
    # Synthetic prior acceptance is only for the root-grant validator test.
    # Production still requires the actual root-owned grant and actual pilot.
    accepted=datetime.now(timezone.utc).isoformat()
    pilot_binding={'site_uid':UID,'source_url':'https://www.youtube.com/watch?v=9T5tn1WGlJg','metadata_sha256':'a'*64,'artifact_sha256':'e'*64}
    pilot=db.enqueue('youtube','9T5tn1WGlJg',HighestReplayVideoWeb.JOB_TYPE,'video',pilot_binding,HighestReplayVideoWeb.POLICY)
    public={'version':HighestReplayVideoWeb.RESULT_OBSERVATION_VERSION,'site_uid':UID,'source_url':pilot_binding['source_url'],
        'metadata_sha256':'a'*64,'state':'completed','public_accessible':True,'public_lookup_observed_at':accepted,
        'external_id':'BV0000000000','url':'https://www.bilibili.com/video/BV0000000000'}
    coverage={'public_player':{'observed_at':accepted},'external_id':public['external_id'],'artifact_sha256':'e'*64,'metadata_sha256':'a'*64,
        'source_to_merged_packet_coverage_verified':True,'owned_only':True}
    machine={'version':HighestReplayVideoWeb.MACHINE_PROOF_VERSION,'job_id':pilot,'binding_sha256':digest(pilot_binding),
        'human_review':False,'per_artifact_owner_acceptance':False,'publication_authorized':False,'evidence_sha256':'b'*64}
    pilot_key=digest(['bilibili','youtube','9T5tn1WGlJg','site_uid:'+UID,HighestReplayVideoWeb.KIND])
    checkpoint={'version':HighestReplayVideoWeb.VERSION,'state':'completed','result_observation':public,'cleanup_coverage':coverage,'key':pilot_key,
        'machine_acceptance':machine}
    with db.transaction() as con:
        con.execute("UPDATE jobs SET state='completed',result=? WHERE job_id=?",(encode({HighestReplayVideoWeb.STATE:checkpoint}),pilot))
        con.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',(pilot_key,pilot,'bilibili','youtube','9T5tn1WGlJg',
            'site_uid:'+UID,HighestReplayVideoWeb.KIND,'completed',public['external_id'],public['url'],0))
    publication=db.query('SELECT * FROM publications WHERE job_id=?',(pilot,))[0]
    snapshot={'version':'replay-natural-pilot-acceptance-snapshot-v3','job_id':pilot,'binding':pilot_binding,'publication':publication,
        'machine_acceptance':machine,'public_result':public,'cleanup_coverage':coverage,'accepted_at':accepted}
    grant={'version':service.WORKFLOW_LAUNCH_VERSION,'policy_sha256':service.policy()['_sha256'],
        'workflow_authorization_reference':policy['workflow_authorization_reference'],'target_site_uid':UID,'allowed_channel_ids':CHANNELS,
        'acceptance_job_id':pilot,'pilot_machine_evidence_sha256':'b'*64,'pilot_public_result_sha256':digest(public),
        'pilot_cleanup_coverage_sha256':digest(coverage),'actual_owner_launch_response':'Owned synthetic workflow grant, not a real approval',
        'owner_response_reference':'Owned local fixture only','accepted_at':accepted,'pilot_acceptance_snapshot_sha256':digest(snapshot)}
    def root_reader(file,**kwargs):return encode(snapshot if Path(file).name=='pilot.json' else grant).encode()
    monkeypatch.setattr(service,'_root_readonly_file',root_reader)
    f.update(grant=grant,launch_snapshot=snapshot,root_reader=root_reader)
    return f


def persist(f):
    source=f['db'].get(f['source']);cp=source['result'][DOWNLOAD_STATE]
    put(f['file'].parent/'verified.json',{'version':DOWNLOAD_VERSION,'binding_sha256':digest(cp['binding']),
        'source_asset':cp['source_asset'],'receipt':cp['receipt'],'human_review':False,'publication_authorized':False,
        'creator_original_bytes_verified':False})


def quality(f):
    source=f['db'].get(f['source']);cp=source['result'][DOWNLOAD_STATE]
    return natural_quality_check(source,cp['binding'],cp['source_asset'],cp['receipt'],{'stream_config':cp['source_asset']['stream_config']})


def test_natural_highest_uses_per_video_720p30_ids_and_full_original_metadata(natural):
    f=natural;result=quality(f)
    assert result['height']==720 and result['avg_frame_rate']=='30/1'
    assert [x['format_id'] for x in result['selected_formats']]==['owned720','ownedAudio']
    meta=natural_metadata(f['s'],f['db'].get(f['source']))
    assert f['raw']['snippet']['title'] in meta['description'] and len(meta['title'])<=80
    assert meta['title']=='【示例主播A】2026-11-01「Owned natural highest title」直播回放'
    assert meta['description']=='示例主播A 2026年11月1日直播回放，日语原声。\n原题：Owned natural highest title\n原视频：https://www.youtube.com/watch?v=ownedNew001\n原频道：https://www.youtube.com/@example-channel-a'
    assert meta['tags']==['示例主播A','ぶいすぽっ','虚拟UP主','直播回放']
    assert f['raw']['snippet']['publishedAt'].startswith('2026-07')  # Not an operating-date substitute.
    assert '转载许可编号' not in meta['description']
    assert result['silent_quality_fallback_allowed'] is result['human_review'] is False


def test_automatic_queue_is_durable_deduplicated_and_never_individual_approval(natural):
    f=natural;service=f['service'];ident=service.queue_verified_download(f['source'])
    assert service.queue_verified_download(f['source'])==ident
    job=f['db'].get(ident);state=job['result'][STATE]
    assert job['state']=='pending' and job['job_type']==JOB_TYPE and state['state']=='prepared'
    assert state['public_submission_approved'] is False and state['human_review'] is False and 'owner_acceptance' not in state
    authorization=state['automation_authorization']
    assert authorization['version']==service.WORKFLOW_AUTHORIZATION_VERSION
    assert authorization['actual_owner_workflow_response']==f['grant']['actual_owner_launch_response']
    assert authorization['human_review'] is authorization['per_artifact_owner_acceptance'] is False
    assert len(f['db'].query('SELECT * FROM publications'))==1  # Only the synthetic pilot row.


@pytest.mark.parametrize('change',['v1_grant','wrong_result','wrong_coverage','future_grant','wrong_uid'])
def test_root_launch_evidence_cannot_be_swapped_or_replaced_by_an_old_grant(natural,change):
    f=natural;grant=f['grant']
    if change=='v1_grant':grant['version']='replay-workflow-launch-acceptance-v1'
    elif change=='wrong_result':grant['pilot_public_result_sha256']='0'*64
    elif change=='wrong_coverage':grant['pilot_cleanup_coverage_sha256']='0'*64
    elif change=='future_grant':grant['accepted_at']='2099-01-01T00:00:00Z'
    else:grant['target_site_uid']='9999'
    with pytest.raises(HubError,match='LAUNCH_ACCEPTANCE'):f['service'].queue_verified_download(f['source'])
    assert not f['db'].query('SELECT * FROM jobs WHERE job_type=?',(JOB_TYPE,))


@pytest.mark.parametrize('change',['old_start','old_end','missing_start','lowered_actual','changed_source_config'])
def test_natural_date_highest_and_actual_stream_config_are_rechecked(natural,change):
    f=natural;source=f['db'].get(f['source']);cp=source['result'][DOWNLOAD_STATE]
    if change=='old_start':source['payload']['raw']['liveStreamingDetails']['actualStartTime']='2026-10-31T15:59:59Z'
    elif change=='old_end':cp['binding']['source_plan']['activated_at']='2026-11-01T03:00:00Z'
    elif change=='missing_start':source['payload']['raw']['liveStreamingDetails'].pop('actualStartTime')
    elif change=='lowered_actual':cp['source_asset']['stream_config']['video']['height']=360
    else:cp['receipt']['source_stream_config']['video']['width']=640
    with pytest.raises(HubError):natural_quality_check(source,cp['binding'],cp['source_asset'],cp['receipt'],{'stream_config':cp['source_asset']['stream_config']})


def test_cross_version_deleted_publication_remains_a_reservation(natural):
    f=natural;source=f['db'].get(f['source'])
    with f['db'].transaction() as con:
        con.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',('d'*64,f['source'],'bilibili','youtube',source['source_id'],
            'site_uid:'+UID,'video_website_short_v1','owner_deleted','BV1111111111','https://www.bilibili.com/video/BV1111111111',0))
    with pytest.raises(HubError,match='EXISTING_PUBLICATION'):f['service'].queue_verified_download(f['source'])
    assert not f['db'].query('SELECT * FROM jobs WHERE job_type=?',(JOB_TYPE,))


def test_real_generic_720p30_packet_cover_qa_and_independent_v3_proof(natural):
    f=natural;file=f['file']
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=1280x720:r=30','-f','lavfi','-i',
        'sine=frequency=440:sample_rate=48000','-t','1','-c:v','libx264','-preset','ultrafast','-threads','1','-pix_fmt','yuv420p',
        '-c:a','aac','-y',str(file)],check=True,capture_output=True,timeout=30)
    details=inspect_media(file,include_frame_rates=True);source=f['db'].get(f['source']);cp=source['result'][DOWNLOAD_STATE]
    cp['source_asset'].update(bytes=file.stat().st_size,sha256=sha(file),duration=details['duration'],stream_config=details['stream_config'],
        stream_types=details['stream_types'],packet_evidence=packet_evidence(file,details['stream_types']))
    cp['receipt']['source_stream_config']=deepcopy(details['stream_config'])
    with f['db'].transaction() as con:con.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(source['result']),f['source']))
    persist(f);job=f['service'].queue_verified_download(f['source']);attempt=str(uuid.uuid4())
    f['service'].bind_browser_attempt(PRINCIPAL,job,attempt_id=attempt)
    original=f['s'].root/'media'/job/'short-video-attempts'/attempt/'cover-source-official-maxres.jpg';original.parent.mkdir(parents=True);make_jpeg(original)
    derived=normalize_cover(root=f['s'].root,job_id=job,source_video_id=source['source_id'],source_file=original,source_sha256=sha(original),
        source_url='https://i.ytimg.com/vi/'+source['source_id']+'/maxresdefault.jpg')
    proof=f['service'].verify_for_publish(job,cover_provenance_file=derived['provenance_file'])
    assert proof['version']=='verified-replay-natural-machine-checks-v3' and proof['checks']['quality']['height']==720
    assert proof['checks']['quality']['metadata_template']=='source-title-and-link-v1'
    assert proof['publication_authorized'] is proof['human_review'] is False
    # Current official cosmetic edits do not replace the immutable original
    # snapshot or invalidate a stable source; source-identity changes still stop.
    changed=deepcopy(f['raw']);changed['snippet']['title']='Owned later edited title';changed['snippet']['description']='Owned later edit'
    with f['db'].transaction() as con:con.execute('UPDATE youtube_videos SET metadata=? WHERE video_id=?',(encode(changed),source['source_id']))
    assert f['service'].verify_for_publish(job)['checks']['quality']['metadata_template']=='source-title-and-link-v1'
    changed['contentDetails']['duration']='PT2S'
    with f['db'].transaction() as con:con.execute('UPDATE youtube_videos SET metadata=? WHERE video_id=?',(encode(changed),source['source_id']))
    with pytest.raises(HubError,match='OFFICIAL_STATE_CHANGED'):f['service'].verify_for_publish(job)
    with f['db'].transaction() as con:con.execute('UPDATE youtube_videos SET metadata=? WHERE video_id=?',(encode(f['raw']),source['source_id']))
    current,binding,state=f['service'].snapshot(job)
    manifest=natural_replay_manifest(f['s'],f['db'],current,binding,state,attempt,duration_seconds=1)
    assert manifest['version']=='replay-natural-native-browser-manifest-v3'
    assert '30.0' in manifest['highest_contract']['source_inventory_canonical']


@pytest.mark.parametrize('scenario',['upload_once','cutoff_at_submit','public_coverage'])
def test_generic_route_real_native_stages_keep_new_versions_and_complete_player_evidence(natural,scenario):
    f=natural;source=f['db'].get(f['source'])
    if scenario=='public_coverage':
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=1280x720:r=30','-f','lavfi','-i',
            'sine=frequency=440:sample_rate=48000','-t','1','-c:v','libx264','-preset','ultrafast','-threads','1','-pix_fmt','yuv420p',
            '-c:a','aac','-y',str(f['file'])],check=True,capture_output=True,timeout=30)
        details=inspect_media(f['file'],include_frame_rates=True);cp=source['result'][DOWNLOAD_STATE]
        cp['source_asset'].update(bytes=f['file'].stat().st_size,sha256=sha(f['file']),duration=details['duration'],stream_config=details['stream_config'])
        cp['receipt']['source_stream_config']=deepcopy(details['stream_config'])
        with f['db'].transaction() as con:con.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(source['result']),f['source']))
        persist(f)
    job=f['service'].queue_verified_download(f['source']);attempt=str(uuid.uuid4());full=scenario=='public_coverage'
    if full:
        original=f['s'].root/'media'/job/'short-video-attempts'/attempt/'cover-source-official-maxres.jpg';original.parent.mkdir(parents=True);make_jpeg(original)
        normalize_cover(root=f['s'].root,job_id=job,source_video_id=source['source_id'],source_file=original,source_sha256=sha(original),
            source_url='https://i.ytimg.com/vi/'+source['source_id']+'/maxresdefault.jpg')
    current,binding,state=f['service'].snapshot(job)
    manifest=natural_replay_manifest(f['s'],f['db'],current,binding,state,attempt,duration_seconds=1,require_cover=full)
    file=f['s'].root/'owned-natural-manifest.json';put(file,manifest)
    repo=Path(__file__).resolve().parents[1]
    env={k:os.environ[k] for k in ('PATH','SystemRoot','SYSTEMROOT','SystemDrive','SYSTEMDRIVE','ProgramData','PROGRAMDATA','ALLUSERSPROFILE',
        'LOCALAPPDATA','APPDATA','USERPROFILE','TEMP','TMP','HOME','XDG_CONFIG_HOME','XDG_CACHE_HOME','LANG','LC_ALL') if k in os.environ}
    env['PLAYWRIGHT_BROWSERS_PATH']=str(repo/'compositor/runtime/browsers')
    result=subprocess.run(['node',str(repo/'tests/replay_highest_native_v2_fixture.mjs'),str(f['s'].root),str(file),scenario],
        cwd=repo,env=env,capture_output=True,text=True,encoding='utf-8',timeout=45)
    assert result.returncode==0,result.stderr
    response=json.loads(result.stdout.strip().splitlines()[-1]);assert response['submits']==int(full)
    assert response['unowned_http_responses']==0 and response['human_review'] is response['live_tested'] is False
    if full:
        f['service'].start_upload(PRINCIPAL,job,attempt_id=attempt,site_uid=UID)
        output=f['s'].root/'media'/job/'replay-browser-natural-v3'/attempt
        f['service'].record_upload(PRINCIPAL,job,json.loads((output/'upload-observation.json').read_bytes()))
        current,binding,state=f['service'].snapshot(job);public=json.loads((output/'owned-public-result.json').read_bytes())
        extra=f['service']._result_extra_state(current,binding,state,public,lookup=True)
        assert extra['cleanup_coverage']['public_player']['ready_state']>=2
        assert extra['cleanup_coverage']['site_uid']==UID and extra['cleanup_coverage']['source_id']==source['source_id']
