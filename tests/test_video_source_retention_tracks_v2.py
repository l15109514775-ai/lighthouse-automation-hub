"""Owned exact track disposal, actual FFmpeg stream copy and packet chains."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import uuid

import pytest

from test_video_source_retention_v2 import owned,public_fixture,put,sha,now,root_release_protocol
from test_short_video_web import short_video
from services.hub.video_source_retention_v2 import VideoSourceTracksRetentionV2,PRINCIPAL,PUBLISHED
from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.youtube_download import digest


@pytest.fixture
def tracks(owned):
    f=public_fixture(owned);file=f['file'];folder=file.parent
    video=folder/'stream-299.mp4';audio=folder/'stream-140.m4a'
    for target,selector in ((video,'0:v:0'),(audio,'0:a:0')):
        subprocess.run(['ffmpeg','-v','error','-i',str(file),'-map',selector,'-c','copy','-y',str(target)],
            check=True,capture_output=True,timeout=20)
    source=f['store'].get(f['source']);cp=source['result']['youtube_download_v2'];cp['version']='youtube-replay-highest-download-v2'
    selected=[{'format_id':'299','ext':'mp4','vcodec':'h264','acodec':'none','width':96,'height':64,'fps':6.0,'filesize':video.stat().st_size},
        {'format_id':'140','ext':'m4a','vcodec':'none','acodec':'aac','width':None,'height':None,'fps':None,'filesize':audio.stat().st_size}]
    plan={'version':'owned-track-retention-protocol-only','selected_formats':selected}
    cp['binding'].update(job_id=f['source'],video_id=source['source_id'],channel_id=source['payload']['channel_id'],source_plan=plan)
    cp['source_asset'].update(selected_formats=selected,source_plan=plan)
    cp['receipt'].update(selected_formats=selected,source_plan=plan,
        tracks=[{**value,'bytes':path.stat().st_size,'sha256':sha(path)} for value,path in zip(selected,(video,audio))])
    with f['store'].transaction() as db:db.execute('UPDATE jobs SET job_type=?,policy_version=?,result=? WHERE job_id=?',
        ('youtube_download_v2','youtube-highest-download-v2',encode(source['result']),f['source']))
    f.update(video=video,audio=audio);bind_receipt_and_native(f)
    f['service']=VideoSourceTracksRetentionV2(f['settings'],f['store'])
    return f


def bind_receipt_and_native(f):
    """Owned protocol fixtures are rebuilt consistently; never real acceptance."""
    source=f['store'].get(f['source']);state_key='youtube_download_v3' if 'youtube_download_v3' in source['result'] else 'youtube_download_v2'
    cp=source['result'][state_key];ref=put(f['file'].parent/'verified.json',{'version':cp['version'],
        'binding_sha256':digest(cp['binding']),'source_asset':cp['source_asset'],'receipt':cp['receipt']})
    job=f['store'].get(f['id']);binding=job['payload'];binding['download_receipt_sha256']=ref['sha256']
    state=job['result'][f['highest_state']]
    upload_file=Path(state['upload_coverage']['evidence']['file']);upload=json.loads(upload_file.read_bytes());upload['binding_sha256']=digest(binding)
    upref=put(upload_file,upload);state['upload_coverage']={**upload,'evidence':upref}
    pubfile=Path(state['cleanup_coverage']['evidence']['file']);public=json.loads(pubfile.read_bytes())
    public.update(binding_sha256=digest(binding),upload_coverage_ref=upref);pubref=put(pubfile,public)
    state['cleanup_coverage']={**public,'evidence':pubref};job['result'][f['highest_state']]=state
    with f['store'].transaction() as db:db.execute('UPDATE jobs SET payload=?,result=? WHERE job_id=?',(encode(binding),encode(job['result']),f['id']))
    f['public']=pubfile


def planned(f):return f['service'].plan(f['id'],authorization_id=f['auth']['authorization_id'])
def allow(f,monkeypatch):monkeypatch.setattr(f['service'],'_release',lambda p:'owned-root-release-only')


def test_exact_two_tracks_and_original_are_removed_and_all_evidence_kept(tracks,monkeypatch):
    f=tracks;allow(f,monkeypatch);p=planned(f)
    assert [Path(item['file']).name for item in p['candidates']]==['stream-299.mp4','stream-140.m4a','original.mp4']
    assert p['source_track_cleanup']['cleanup_recomputed_source_packet_coverage'] is True
    before=f['store'].query('SELECT * FROM publications');result=f['service'].apply(p)
    assert result['local_files_deleted']==3 and result['source_tracks_deleted']==2
    assert result['total_logical_bytes_deleted']==sum(item['bytes'] for item in p['candidates'])
    assert all(not Path(item['file']).exists() for item in p['candidates'])
    assert f['public'].exists() and (f['file'].parent/'verified.json').exists()
    assert f['store'].query('SELECT * FROM publications')==before and f['service'].apply(p)==result


@pytest.mark.parametrize('change',['raw_hash','packet_rewritten_receipt','foreign_reference','missing_track','unknown_publication'])
def test_bad_unknown_missing_or_shared_track_is_retained(tracks,change):
    f=tracks
    if change=='raw_hash':f['video'].write_bytes(f['video'].read_bytes()+b'changed')
    elif change=='packet_rewritten_receipt':
        # A full, valid but different H264 track, with its size/hash honestly
        # updated, still cannot stand in for the published merged packet chain.
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=red:s=96x64:r=6','-t','2','-an','-c:v','libx264','-threads','1',
            '-y',str(f['video'])],check=True,capture_output=True,timeout=20)
        src=f['store'].get(f['source']);cp=src['result']['youtube_download_v2'];fmt=cp['binding']['source_plan']['selected_formats'][0]
        fmt['filesize']=f['video'].stat().st_size;cp['receipt']['tracks'][0].update(filesize=fmt['filesize'],bytes=fmt['filesize'],sha256=sha(f['video']))
        cp['receipt']['selected_formats'][0]['filesize']=fmt['filesize'];cp['source_asset']['selected_formats'][0]['filesize']=fmt['filesize']
        cp['receipt']['source_plan']=deepcopy(cp['binding']['source_plan']);cp['source_asset']['source_plan']=deepcopy(cp['binding']['source_plan'])
        with f['store'].transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(src['result']),f['source']))
        bind_receipt_and_native(f)
    elif change=='foreign_reference':f['store'].enqueue('maintenance','reader','reader','system',{'file':str(f['video'])},'owned')
    elif change=='missing_track':f['audio'].unlink()
    else:
        with f['store'].transaction() as db:db.execute("UPDATE publications SET state='awaiting_moderation'")
    with pytest.raises(HubError):planned(f)
    assert f['file'].exists() and (f['file'].parent/'verified.json').exists()


def test_caller_cannot_substitute_configuration_path_for_raw_track(tracks,monkeypatch):
    f=tracks;allow(f,monkeypatch);p=planned(f);target=f['settings'].root/'config/owned-safe.toml';target.write_text('owned=true','utf-8')
    from services.hub.archive_source_retention_v2 import _inspect
    p['candidates'][0]=_inspect(f['settings'].root,target);p['plan_sha256']=digest({key:value for key,value in p.items() if key!='plan_sha256'})
    with pytest.raises(HubError):f['service'].apply(p)
    assert target.exists() and f['video'].exists() and f['file'].exists()


def test_raw_track_unlink_crash_resumes_without_rescanning_or_deleting_twice(tracks,monkeypatch):
    f=tracks;allow(f,monkeypatch);p=planned(f);original=f['service']._stat_unlink;calls=[]
    def crash(item,ctime):calls.append(item['file']);original(item,ctime);raise OSError('owned exit after first raw unlink')
    monkeypatch.setattr(f['service'],'_stat_unlink',crash)
    with pytest.raises(OSError):f['service'].apply(p)
    assert not f['video'].exists() and f['audio'].exists() and f['file'].exists()
    monkeypatch.setattr(f['service'],'_stat_unlink',original)
    assert f['service'].apply(p)['local_files_deleted']==3 and calls==[str(f['video'])]


def test_fresh_public_refresh_callback_runs_after_heavy_scan_lock_released(tracks):
    f=tracks;called=[]
    from services.hub.locks import resource_guard
    def refresh():
        with resource_guard(f['settings'].root/'data/locks/heavy'):called.append('existing lookup permitted here')
    p=f['service'].plan(f['id'],authorization_id=f['auth']['authorization_id'],refresh_public_observation=refresh)
    assert called and p['local_files_deleted']==0 and f['video'].exists()


def test_old_failed_other_job_partial_is_not_part_of_cleanup(tracks,monkeypatch):
    f=tracks;other=str(uuid.uuid4());folder=f['settings'].root/'media'/other/'youtube-download-v1';folder.mkdir(parents=True)
    partial=folder/'stream-0.mp4.part';partial.write_bytes(b'owned retired but not authorized for this cleanup')
    allow(f,monkeypatch);f['service'].apply(planned(f))
    assert partial.read_bytes()==b'owned retired but not authorized for this cleanup'


def natural_tracks(f):
    """A route protocol fixture, not a claim of natural-workflow acceptance."""
    from services.hub.replay_natural_video_v3 import NaturalReplayVideoWeb as Natural
    job=f['store'].get(f['id']);source=f['store'].get(f['source']);old=f['file'].parent
    new=old.with_name('youtube-highest-v3');old.rename(new)
    native=Path(job['result'][f['highest_state']]['upload_coverage']['evidence']['file']).parent
    next_native=native.parent.with_name('replay-browser-natural-v3')/native.name
    next_native.parent.mkdir();native.rename(next_native)
    replacements={str(old):str(new),str(native):str(next_native),'stream-299.mp4':'stream-137.mp4','stream-140.m4a':'stream-141.m4a'}
    (new/'stream-299.mp4').rename(new/'stream-137.mp4');(new/'stream-140.m4a').rename(new/'stream-141.m4a')
    def update(value):
        if isinstance(value,dict):return {k:update(v) for k,v in value.items()}
        if isinstance(value,list):return [update(v) for v in value]
        if isinstance(value,str):
            for old_text,new_text in replacements.items():value=value.replace(old_text,new_text)
        return value
    job=update(job);source=update(source);checkpoint=source['result'].pop('youtube_download_v2')
    checkpoint['version']='youtube-replay-highest-natural-download-v3'
    for formats in (checkpoint['binding']['source_plan']['selected_formats'],checkpoint['source_asset']['selected_formats'],
            checkpoint['source_asset']['source_plan']['selected_formats'],checkpoint['receipt']['selected_formats'],
            checkpoint['receipt']['source_plan']['selected_formats'],checkpoint['receipt']['tracks']):
        for fmt,ident in zip(formats,('137','141')):fmt['format_id']=ident
    source['result']['youtube_download_v3']=checkpoint
    state=job['result'].pop(f['highest_state']);state.update(version=Natural.VERSION,
        key=digest(['bilibili','youtube',job['source_id'],'site_uid:'+job['payload']['site_uid'],Natural.KIND]))
    job['result'][Natural.STATE]=state
    for name in ('upload-coverage.json','public-coverage.json'):
        file=next_native/name;put(file,update(json.loads(file.read_bytes())))
    with f['store'].transaction() as db:
        db.execute('UPDATE jobs SET job_type=?,policy_version=?,payload=?,result=? WHERE job_id=?',
            (Natural.JOB_TYPE,Natural.POLICY,encode(job['payload']),encode(job['result']),f['id']))
        db.execute('UPDATE jobs SET job_type=?,policy_version=?,result=? WHERE job_id=?',
            ('youtube_download_v3','youtube-highest-natural-download-v3',encode(source['result']),f['source']))
        db.execute('UPDATE publications SET kind=?,idempotency_key=? WHERE job_id=?',(Natural.KIND,state['key'],f['id']))
    f.update(file=new/'original.mp4',video=new/'stream-137.mp4',audio=new/'stream-141.m4a',highest_state=Natural.STATE)
    bind_receipt_and_native(f);return f


def test_natural_route_uses_dynamic_format_ids_and_actual_raw_coverage_verifier(tracks,monkeypatch):
    f=natural_tracks(tracks);allow(f,monkeypatch);p=planned(f)
    assert [Path(v['file']).name for v in p['candidates']]==['stream-137.mp4','stream-141.m4a','original.mp4']
    assert p['proof']['pipeline_state']=='replay_natural_video_web_v3'
    assert f['service'].apply(p)['source_tracks_deleted']==2 and f['public'].exists()


def test_wrong_source_job_route_cannot_relabel_an_old_v2_directory(tracks):
    f=tracks;job=f['store'].get(f['source'])
    with f['store'].transaction() as db:db.execute('UPDATE jobs SET job_type=? WHERE job_id=?',('youtube_download_v3',f['source']))
    with pytest.raises(HubError,match='VIDEO_RETENTION_TRACK_ROUTE_REJECTED'):planned(f)
    assert f['file'].exists() and f['video'].exists()


def test_old_merged_only_nineteen_test_release_does_not_enable_three_file_disposal(tracks,monkeypatch):
    f=tracks;p=planned(f);root_release_protocol(f,p,monkeypatch)
    old=f['settings'].root/'config/video-source-retention-v2-release.json'
    old.rename(f['settings'].root/'config'/f['service'].RELEASE_FILENAME)
    with pytest.raises(HubError,match='VIDEO_RETENTION_RELEASE_REQUIRED'):f['service'].apply(p)
    assert all(Path(item['file']).exists() for item in p['candidates'])
