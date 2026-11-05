"""Owned SQLite/files, real FFmpeg and protocol observations; no live platform."""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import uuid

import pytest

from test_short_video_web import short_video,create,STATE,BV
from adapters.media_tools import inspect_media,packet_evidence
from services.hub import video_source_retention_v2 as module
from services.hub.video_source_retention_v2 import VideoSourceRetentionV2 as Retention,PRINCIPAL,DISPOSAL,PUBLISHED
from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.youtube_download import digest


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def now():return datetime.now(timezone.utc).isoformat()
def put(file,value):
    file.parent.mkdir(parents=True,exist_ok=True);file.write_text(encode(value),'utf-8')
    return {'file':str(file),'bytes':file.stat().st_size,'sha256':sha(file)}


@pytest.fixture
def owned(short_video):
    settings,store,service,download,*_=short_video;ident=create(short_video);job=store.get(ident);binding=job['payload']
    checkpoint=job['result'][STATE]
    checkpoint.update(state='completed',result_observation={'site_uid':binding['site_uid'],'source_url':binding['source_url'],
        'metadata_sha256':binding['metadata_sha256'],'external_id':BV,'url':'https://www.bilibili.com/video/'+BV,'state':'completed'},
        test_retention_policy={'version':'owned-legacy-test-hold','local_test_cleanup_requires_formal_workflow_launch':True},
        owner_authorized_remote_deletion_v1={'version':'owner-authorized-short-remote-deletion-v1','remote_state':'owner_deleted','external_id':BV})
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='completed',result=?,external_publish_id=? WHERE job_id=?",(encode(job['result']),BV,ident))
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',(checkpoint['key'],ident,'bilibili','youtube',job['source_id'],
            'site_uid:'+binding['site_uid'],'video_website_short_v1','completed',BV,'https://www.bilibili.com/video/'+BV,1))
    retention=Retention(settings,store)
    authorization=retention.authorize(PRINCIPAL,actual_owner_response='Owned synthetic new disposal instruction, not a real owner message',
        owner_response_reference='Owned newer instruction reference',scope=DISPOSAL,job_id=ident)
    return dict(settings=settings,store=store,service=retention,id=ident,source=download,file=Path(binding['file']),auth=authorization,
        old_hold=deepcopy(checkpoint['test_retention_policy']))


def plan(f):return f['service'].plan(f['id'],authorization_id=f['auth']['authorization_id'])


def allow_owned_release(f,monkeypatch):
    # This replaces only the root deployment admission boundary. File/media,
    # publication, source, evidence and one-use execution checks remain real.
    monkeypatch.setattr(f['service'],'_release',lambda p:'owned-root-release-protocol-only')


def root_release_protocol(f,p,monkeypatch):
    """Owned protocol sample, not a real Ubuntu or owner acceptance record."""
    monkeypatch.setattr(module,'_root_protected',lambda path:None)
    root=Path(module.__file__).resolve().parents[2]
    names=['services/hub/video_source_retention_v2.py','services/hub/archive_source_retention_v2.py',
        'services/hub/replay_highest_video_v2.py','services/hub/replay_highest_coverage_v2.py']
    sources={name:sha(root/name) for name in names}
    junit=f['settings'].root/'data/owned-video-release-protocol.xml'
    junit.write_text('<testsuites><testsuite>'+''.join('<testcase classname="tests.test_video_source_retention_v2" name="owned-protocol-'+str(i)+'"/>'
        for i in range(module.TEST_COUNT))+'</testsuite></testsuites>','utf-8')
    evidence={'version':'video-source-retention-v2-ubuntu-tests-v1','source_files':sources,'network':'none',
        'application_data_mounted':False,'production_profile_mounted':False,'exit_code':0,'full_suite_passed':True,
        'oom_killed':False,'no_pid_or_oom_denial':True,'runtime_matches_declared_candidate':True,'mounts_match_isolated_allowlist':True,
        'junit_file':str(junit),'junit_sha256':sha(junit),'tests':module.TEST_COUNT}
    ref=put(f['settings'].root/'data/owned-video-release-protocol.json',evidence)
    put(f['settings'].root/'config/video-source-retention-v2-release.json',{'version':'video-source-retention-release-v2',
        'scope':p['scope'],'authorization_id':p['authorization_id'],'source_files':{name:sources[name] for name in names[:2]},
        'test_attestation':{'file':ref['file'],'sha256':ref['sha256']}})


def test_new_owner_instruction_overrides_local_hold_without_fake_launch_or_deleting_history(owned,monkeypatch):
    f=owned;p=plan(f);before=f['store'].query('SELECT * FROM publications');old=f['store'].get(f['id'])['result'][STATE]
    assert p['proof']['basis']['remote_recoverability_claimed'] is False and p['local_files_deleted']==0
    with pytest.raises(HubError,match='VIDEO_RETENTION_RELEASE_REQUIRED'):f['service'].apply(p)
    root_release_protocol(f,p,monkeypatch);assert len(f['service']._release(p))==64
    result=f['service'].apply(p)
    assert result['local_files_deleted']==1 and not f['file'].exists()
    assert f['store'].query('SELECT * FROM publications')==before
    assert f['store'].get(f['id'])['result'][STATE]==old
    assert f['store'].get(f['source'])['result']['local_original_retention_v2']==result
    assert result['formal_workflow_launch_claimed'] is False and result['evidence_files_deleted'] is False
    assert f['service'].apply(p)==result
    assert f['file'].with_name('verified.json').is_file()


@pytest.mark.parametrize('change',['wrong_owner','different_job','wrong_uid','pending_publication','active_source','changed_original','new_source_user','shared_completed_ref','wrong_filename'])
def test_unapproved_or_changed_source_never_becomes_disposable(owned,change):
    f=owned
    if change=='wrong_owner':
        with pytest.raises(HubError):f['service'].authorize('worker:owned',actual_owner_response='owned',owner_response_reference='owned',scope=DISPOSAL,job_id=f['id'])
        return
    if change=='different_job':
        with pytest.raises(HubError):f['service'].plan(str(uuid.uuid4()),authorization_id=f['auth']['authorization_id'])
        return
    if change=='changed_original':f['file'].write_bytes(b'owned changed original')
    elif change=='new_source_user':f['store'].enqueue('youtube','abcdefghijk','another-upload','video',{},'owned')
    elif change=='shared_completed_ref':
        other=f['store'].enqueue('maintenance','owned-related','owned-reader','system',{'file':str(f['file'])},'owned')
        with f['store'].transaction() as db:db.execute("UPDATE jobs SET state='completed' WHERE job_id=?",(other,))
    else:
        with f['store'].transaction() as db:
            if change=='wrong_uid':db.execute("UPDATE publications SET account_id='site_uid:999'")
            elif change=='pending_publication':db.execute("UPDATE publications SET state='awaiting_moderation'")
            elif change=='active_source':db.execute("UPDATE jobs SET state='processing',lease_owner='owned-live' WHERE job_id=?",(f['source'],))
            elif change=='wrong_filename':
                job=f['store'].get(f['id']);job['payload']['file']=str(f['file'].with_name('not-original.mp4'))
                db.execute('UPDATE jobs SET payload=? WHERE job_id=?',(encode(job['payload']),f['id']))
    with pytest.raises(HubError):plan(f)
    assert f['file'].exists()


def test_manual_pass_boolean_cannot_enable_production_file_disposal(owned,monkeypatch):
    f=owned;monkeypatch.setattr(module,'_root_protected',lambda path:None)
    put(f['settings'].root/'config/video-source-retention-v2-release.json',{'passed':True,'cleanup_authorized':True})
    with pytest.raises(HubError,match='VIDEO_RETENTION_RELEASE_REQUIRED'):f['service'].apply(plan(f))
    assert f['file'].exists()


def test_crash_after_unlink_before_job_commit_reconciles_without_second_unlink(owned,monkeypatch):
    f=owned;allow_owned_release(f,monkeypatch);p=plan(f);original=f['service']._unlink_exact;calls=[]
    def crash(item):calls.append(item['file']);original(item);raise OSError('owned crash after unlink')
    monkeypatch.setattr(f['service'],'_unlink_exact',crash)
    with pytest.raises(OSError):f['service'].apply(p)
    assert not f['file'].exists()
    result=f['service'].apply(p)
    assert result['local_files_deleted']==1 and calls==[str(f['file'])]


def test_crash_after_job_commit_before_index_commit_uses_saved_receipt(owned,monkeypatch):
    f=owned;allow_owned_release(f,monkeypatch);p=plan(f);result=f['service'].apply(p)
    with sqlite3.connect(f['service'].path) as db:
        db.execute("UPDATE operations SET state='deleting',record=? WHERE id=?",(encode({'plan':p}),p['plan_id']))
    assert f['service'].apply(p)==result


def test_original_larger_than_archive_limit_still_uses_video_specific_bound(owned,monkeypatch):
    f=owned
    # Sparse owned data, explicitly discarded as a test artifact; not a real
    # uploaded movie and not proof of media/publication quality.
    with f['file'].open('wb') as stream:stream.truncate(50_000_001)
    job=f['store'].get(f['id']);job['payload'].update(file_bytes=f['file'].stat().st_size,artifact_sha256=sha(f['file']))
    with f['store'].transaction() as db:db.execute('UPDATE jobs SET payload=? WHERE job_id=?',(encode(job['payload']),f['id']))
    f['auth']=f['service'].authorize(PRINCIPAL,actual_owner_response='Owned exact large-test disposal',
        owner_response_reference='Owned fixture only',scope=DISPOSAL,job_id=f['id'])
    allow_owned_release(f,monkeypatch)
    assert f['service'].apply(plan(f))['local_files_deleted']==1 and not f['file'].exists()


def public_fixture(f):
    """Real local media/packets and actual raw-observation validators, no website."""
    from services.hub.replay_highest_video_v2 import STATE as HS,VERSION,KIND,JOB_TYPE,POLICY,HighestReplayVideoWeb
    from services.hub.replay_highest_coverage_v2 import expected_binding,upload_coverage,public_coverage
    file=f['file'];directory=file.parent.parent/'youtube-highest-v2';directory.mkdir();new=directory/'original.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=96x64:r=6','-f','lavfi','-i',
        'sine=frequency=440:sample_rate=48000','-t','2','-c:v','libx264','-threads','1','-c:a','aac','-y',str(new)],
        check=True,capture_output=True,timeout=30)
    details=inspect_media(new,include_frame_rates=True);packets=packet_evidence(new,details['stream_types']);job=f['store'].get(f['id'])
    binding=job['payload'];binding.update(file=str(new),file_bytes=new.stat().st_size,artifact_sha256=sha(new))
    state=job['result'].pop(STATE);state.pop('owner_authorized_remote_deletion_v1');state.update(version=VERSION,
        key=digest(['bilibili','youtube',job['source_id'],'site_uid:'+binding['site_uid'],KIND]),attempt_id=str(uuid.uuid4()))
    job['result'][HS]=state;source=f['store'].get(f['source'])
    source['result']['youtube_download_v2']={'state':'verified','binding':{'duration':details['duration']},
        'source_asset':{'file':str(new),'bytes':new.stat().st_size,'sha256':sha(new),'packet_evidence':packets},
        'receipt':{'source_to_merged_packet_coverage_verified':True}}
    with f['store'].transaction() as db:
        db.execute('UPDATE jobs SET job_type=?,policy_version=?,payload=?,result=? WHERE job_id=?',
            (JOB_TYPE,POLICY,encode(binding),encode(job['result']),f['id']))
        db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(source['result']),f['source']))
        db.execute('UPDATE publications SET idempotency_key=?,kind=? WHERE job_id=?',(state['key'],KIND,f['id']))
    job=f['store'].get(f['id']);folder=f['settings'].root/'media'/f['id']/'replay-browser-highest-v2'/state['attempt_id']
    expected=expected_binding(job,binding,state)
    upload={**expected,'version':'replay-highest-native-upload-coverage-v2','file':str(new),'selected_filename':'original.mp4',
        'native_filename':{'text':'original.mp4','kind':'file_input','observed':True},'native_upload_complete':True,
        'whole_file_streamed':True,'streamed_bytes':new.stat().st_size,'streamed_sha256':sha(new),
        'observed_at':now(),'server_file_hash_verified':False,'human_review':False}
    uref=put(folder/'upload-coverage.json',upload);state['upload_coverage']=upload_coverage(f['settings'],job,binding,state,uref)
    public={**expected,'version':'replay-highest-public-coverage-v2','external_id':BV,'url':'https://www.bilibili.com/video/'+BV,
        'upload_coverage_ref':uref,'source_duration_seconds':details['duration'],'source_to_merged_packet_coverage_verified':True,
        'public_player':{'duration_seconds':details['duration'],'current_time':1,'ready_state':4,
            'seekable_ranges':[{'start':0,'end':details['duration']}],'observed_at':now(),'error_code':None,
            'near_end_playability_observed':True,'near_end_seek_performed':False},'server_file_hash_verified':False,'human_review':False}
    pref=put(folder/'public-coverage.json',public)
    state['cleanup_coverage']=public_coverage(f['settings'],f['store'],job,binding,state,pref,state['result_observation'])
    job['result'][HS]=state
    with f['store'].transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(job['result']),f['id']))
    f['file']=new;f['public']=folder/'public-coverage.json';f['highest_state']=HS
    f['auth']=f['service'].authorize(PRINCIPAL,actual_owner_response='Owned remove originals after verified publication, not a real user quote',
        owner_response_reference='Owned workflow retention fixture',scope=PUBLISHED)
    return f


def test_real_media_packets_and_native_public_raw_proofs_allow_exact_original_cleanup(owned,monkeypatch):
    f=public_fixture(owned);p=plan(f);allow_owned_release(f,monkeypatch)
    assert p['proof']['basis']['coverage']['public_player']['duration_seconds']>1
    assert p['proof']['basis']['server_file_hash_verified'] is False
    assert f['service'].apply(p)['local_files_deleted']==1 and not f['file'].exists()
    assert f['public'].exists() and f['store'].query('SELECT * FROM publications')[0]['external_id']==BV


@pytest.mark.parametrize('change',['wrong_duration','stale_player','packet_mismatch','fake_whole_file'])
def test_full_file_or_actual_public_player_mismatch_is_not_self_certified(owned,change):
    f=public_fixture(owned);job=f['store'].get(f['id']);state=job['result'][f['highest_state']]
    if change=='packet_mismatch':
        source=f['store'].get(f['source']);source['result']['youtube_download_v2']['source_asset']['packet_evidence']['video']['chain']='0'*64
        with f['store'].transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(source['result']),f['source']))
    elif change=='fake_whole_file':
        file=Path(state['upload_coverage']['evidence']['file']);value=json.loads(file.read_bytes());value['streamed_bytes']-=1;put(file,value)
    else:
        value=json.loads(f['public'].read_bytes())
        if change=='wrong_duration':value['public_player']['duration_seconds']=20
        else:value['public_player']['observed_at']='2020-01-01T00:00:00Z'
        ref=put(f['public'],value);state['cleanup_coverage']['evidence']=ref
        with f['store'].transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(job['result']),f['id']))
    with pytest.raises(HubError):plan(f)
    assert f['file'].exists()
