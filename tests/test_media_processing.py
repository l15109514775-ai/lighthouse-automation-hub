"""Actual owned synthetic AV; never download recordings, approve humans or publish."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import pytest
from adapters.media_tools import inspect_media,packet_evidence,process_lines
from services.hub.db import Store,encode
from services.hub.errors import HubError
from services.hub.evidence import sha256_file
from services.hub.locks import exclusive
from services.hub.media_processing import MediaProcessing,balanced_segment_seconds
from services.hub.video_pipeline import VideoPipeline


@pytest.fixture
def owned_video(runtime):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('FFmpeg is mandatory in the Ubuntu deployment; absent on this developer computer')
    settings,store=runtime
    ident=store.enqueue('youtube','abcdefghijk','video_process','video',
        {'video_id':'abcdefghijk','url':'https://www.youtube.com/watch?v=abcdefghijk'},'human-review-v1')
    job=store.claim('video',180,4)
    folder=settings.root/'media'/ident;folder.mkdir()
    source=folder/'original.mp4'
    make_video(source)
    info={'file':str(source),'sha256':sha256_file(source),'bytes':source.stat().st_size}
    store.checkpoint(job,'authorized_media_acquired',{'source_asset':info,'source_sha256':info['sha256'],
                                                  'rights_evidence':'owned-synthetic-test-source'})
    return settings,store,job,info


def make_video(file,gop=10):
    subprocess.run(['ffmpeg','-nostdin','-y','-v','error','-f','lavfi','-i','testsrc2=s=160x90:r=10',
        '-f','lavfi','-i','sine=frequency=440:sample_rate=16000','-t','6','-c:v','libx264','-threads','1',
        '-g',str(gop),'-keyint_min',str(gop),'-sc_threshold','0','-bf','2','-pix_fmt','yuv420p','-c:a','aac',str(file)],
        check=True,timeout=20)


def recover(settings,store):
    with exclusive(settings.root/'data/locks/video'):
        store.recover('video',4,all_processing=True)
    return store.claim('video',180,4)


def assert_private_complete(settings,store,source,result):
    assert result['packet_coverage_verified'] and result['source_packets']['video']['packets']==60
    manifest=json.loads(Path(result['manifest']).read_text('utf-8'))
    assert manifest['human_review'] is False and manifest['publishable'] is False
    assert sha256_file(source)==manifest['source_sha256']
    assert not store.query('SELECT * FROM publications')
    assert not store.query('SELECT * FROM publication_reviews')


def test_ten_hour_standard_balances_real_10h48m_shape_into_two_parts():
    recipe={'max_part_seconds':36000,'max_part_bytes':14_000_000_000}
    count,seconds=balanced_segment_seconds(38919.058866,13850268492,recipe)
    assert count==2
    assert seconds==pytest.approx(19459.529433)


def test_real_segmented_packet_payloads_and_codec_configuration_are_preserved(owned_video):
    settings,store,job,info=owned_video
    settings.config['video_part_max_seconds']=3
    result=MediaProcessing(settings,store).run(job,info)
    assert len(result['outputs'])>=2
    assert all(part['duration']<=3 and part['bytes']<=settings.config['video_part_max_bytes'] for part in result['outputs'])
    assert_private_complete(settings,store,Path(info['file']),result)
    assert job['result']['media_processing']['state']=='completed'


def test_completed_batch_reuses_files_after_new_instance_without_rendering(owned_video,monkeypatch):
    import services.hub.media_processing as module
    settings,store,job,info=owned_video
    settings.config['video_part_max_seconds']=3
    first=MediaProcessing(settings,store).run(job,info)
    original_time=Path(first['outputs'][0]['file']).stat().st_mtime_ns
    monkeypatch.setattr(module,'split_media',lambda *a,**k:pytest.fail('Completed batch must not render again'))
    next_job=recover(settings,Store(settings.db_path))
    restored=MediaProcessing(settings,Store(settings.db_path)).run(next_job,info)
    assert restored['outputs']==first['outputs']
    assert Path(restored['outputs'][0]['file']).stat().st_mtime_ns==original_time


class SimulatedExit(BaseException): pass


def test_process_exit_during_verification_recovers_sqlite_checkpoint(owned_video,monkeypatch):
    import services.hub.media_processing as module
    settings,store,job,info=owned_video
    settings.config['video_part_max_seconds']=3
    service=MediaProcessing(settings,store)
    save=service._save
    def interrupt(claimed,plan):
        save(claimed,plan)
        if plan['state']=='verifying' and len(plan['parts'])==1: raise SimulatedExit()
    monkeypatch.setattr(service,'_save',interrupt)
    with pytest.raises(SimulatedExit): service.run(job,info)
    assert len(store.get(job['job_id'])['result']['media_processing']['parts'])==1
    monkeypatch.setattr(module,'split_media',lambda *a,**k:pytest.fail('Finished muxing must not run again'))
    next_job=recover(settings,Store(settings.db_path))
    result=MediaProcessing(settings,Store(settings.db_path)).run(next_job,info)
    assert next_job['attempts']==2
    assert_private_complete(settings,store,Path(info['file']),result)


def test_process_exit_during_final_rename_reuses_verified_output(owned_video,monkeypatch):
    import services.hub.media_processing as module
    settings,store,job,info=owned_video
    settings.config['video_part_max_seconds']=3
    replace=module.os.replace
    def interrupt(before,after):
        replace(before,after)
        if str(before).endswith('.partial.mp4'): raise SimulatedExit()
    with monkeypatch.context() as context:
        context.setattr(module.os,'replace',interrupt)
        with pytest.raises(SimulatedExit): MediaProcessing(settings,store).run(job,info)
    assert store.get(job['job_id'])['result']['media_processing']['state']=='finalizing'
    monkeypatch.setattr(module,'split_media',lambda *a,**k:pytest.fail('Final rename recovery cannot rerender'))
    result=MediaProcessing(settings,Store(settings.db_path)).run(recover(settings,store),info)
    assert_private_complete(settings,store,Path(info['file']),result)


def test_size_limit_balances_parts_then_preserves_entire_original_packet_sequence(owned_video):
    settings,store,job,info=owned_video
    settings.config['video_part_max_bytes']=60000
    result=MediaProcessing(settings,store).run(job,info)
    assert result['render_attempts']==1 and len(result['outputs'])>=2
    assert all(part['bytes']<=60000 for part in result['outputs'])
    assert_private_complete(settings,store,Path(info['file']),result)


def test_long_keyframe_gap_stops_after_bounded_attempts_and_keeps_source(owned_video):
    settings,store,job,info=owned_video
    source=Path(info['file']);make_video(source,gop=600)
    info.update(sha256=sha256_file(source),bytes=source.stat().st_size)
    store.checkpoint(job,'authorized_media_acquired',{'source_asset':info,'source_sha256':info['sha256']})
    settings.config['video_part_max_seconds']=2
    with pytest.raises(HubError,match='REQUIRES_TRANSCODING_OR_REVIEW'):
        MediaProcessing(settings,store).run(job,info)
    plan=store.get(job['job_id'])['result']['media_processing']
    assert plan['attempts']==3 and len(plan['history'])==3
    assert sha256_file(source)==info['sha256']
    assert not store.query('SELECT * FROM publications')


def test_low_disk_blocks_new_segment_outputs_and_retains_original(owned_video,monkeypatch):
    import services.hub.media_processing as module
    settings,store,job,info=owned_video
    service=MediaProcessing(settings,store)
    monkeypatch.setattr(service.maintenance,'check_disk',lambda *a:(_ for _ in ()).throw(HubError('DISK_LOW',review=True)))
    monkeypatch.setattr(module,'split_media',lambda *a,**k:pytest.fail('Cannot start a new output on low disk'))
    with pytest.raises(HubError,match='DISK_LOW'): service.run(job,info)
    assert sha256_file(info['file'])==info['sha256']
    assert not list(Path(info['file']).parent.rglob('*.partial.mp4'))


def test_child_process_is_actually_terminated_when_disk_check_fails(tmp_path,monkeypatch):
    import adapters.media_tools as module
    children=[];popen=module.subprocess.Popen
    def spawn(*a,**k):
        child=popen(*a,**k);children.append(child);return child
    monkeypatch.setattr(module.subprocess,'Popen',spawn)
    marker=tmp_path/'child-started'
    program='from pathlib import Path;import sys,time;Path(sys.argv[1]).write_text("started");time.sleep(30)'
    def check():
        if marker.exists(): raise HubError('DISK_LOW',review=True)
    started=time.monotonic()
    with pytest.raises(HubError,match='DISK_LOW'):
        list(process_lines([sys.executable,'-c',program,str(marker)],tmp_path,35,check))
    assert children[0].poll() is not None and time.monotonic()-started<8


def test_changed_derived_file_is_rejected_without_replacing_source(owned_video):
    settings,store,job,info=owned_video
    service=MediaProcessing(settings,store)
    result=service.run(job,info)
    Path(result['outputs'][0]['file']).write_bytes(b'changed derived file')
    with pytest.raises(HubError,match='DERIVED_INTEGRITY_CHANGED'):
        MediaProcessing(settings,store).run(store.get(job['job_id']),info)
    assert sha256_file(info['file'])==info['sha256']


def test_packet_hash_rejects_payload_changed_after_muxing(owned_video,monkeypatch):
    import services.hub.media_processing as module
    settings,store,job,info=owned_video
    original=module.split_media
    def changed(*a,**k):
        original(*a,**k)
        file=Path(a[1])/'part-000.partial.mp4'
        data=bytearray(file.read_bytes())
        position=data.index(b'mdat')+100
        data[position]^=1
        file.write_bytes(data)
    monkeypatch.setattr(module,'split_media',changed)
    with pytest.raises(HubError,match='PACKET_COVERAGE_MISMATCH'):
        MediaProcessing(settings,store).run(job,info)
    assert sha256_file(info['file'])==info['sha256']


def grant(info):
    return {'video_id':'abcdefghijk','actions':['download','process'],'evidence':'owned-synthetic-test-source',
        'authorized_by':'owned-synthetic-fixture','source_sha256':info['sha256'],'max_bytes':1000000,
        'source_url':'https://example.invalid/not-contacted-owned-fixture.mp4'}


def test_pipeline_reuses_original_checkpoint_without_downloading(owned_video,monkeypatch):
    import services.hub.video_pipeline as module
    settings,store,job,info=owned_video
    service=VideoPipeline(settings,store,None)
    monkeypatch.setattr(service,'rights',lambda _:grant(info))
    monkeypatch.setattr(module,'download',lambda *a,**k:pytest.fail('Verified original cannot be downloaded again'))
    with pytest.raises(HubError,match='VIDEO_REVIEW_AND_BILIBILI_AUTHORIZATION_REQUIRED'): service.process(job)
    assert store.get(job['job_id'])['stage']=='video_ready_for_review'
    assert not store.query('SELECT * FROM publication_reviews')


def test_batch_is_not_silently_submitted_as_first_part_or_many_bvs(owned_video,monkeypatch):
    import services.hub.video_pipeline as module
    settings,store,job,info=owned_video
    settings.config['video_part_max_seconds']=3
    service=VideoPipeline(settings,store,None)
    monkeypatch.setattr(service,'rights',lambda _:grant(info))
    monkeypatch.setattr(module,'download',lambda *a,**k:pytest.fail('No new download'))
    monkeypatch.setattr(service.bilibili,'publish_reviewed_video',lambda *a:pytest.fail('Multi-part publishing is not authorized'))
    with pytest.raises(HubError,match='VIDEO_BATCH_REVIEW_AND_TRANSPORT_REQUIRED'): service.process(job)
    assert store.get(job['job_id'])['stage']=='video_batch_ready_for_review'
    assert len(store.get(job['job_id'])['result']['processed_parts'])>1
    assert not store.query('SELECT * FROM publications')


def test_revoked_rights_preserve_existing_media_checkpoint(owned_video,monkeypatch):
    settings,store,job,info=owned_video
    service=VideoPipeline(settings,store,None)
    monkeypatch.setattr(service,'rights',lambda _:None)
    with pytest.raises(HubError,match='MEDIA_RIGHTS_REVOKED'): service.process(job)
    assert store.get(job['job_id'])['result']['source_asset']==info
    assert sha256_file(info['file'])==info['sha256']


@pytest.mark.parametrize('key,value',[('video_part_max_seconds',36001),('video_part_max_bytes',14000000001),
    ('video_max_parts',33),('video_processing_timeout_seconds',True)])
def test_processor_enforces_platform_and_resource_caps_even_with_direct_settings(runtime,key,value):
    settings,store=runtime;settings.config[key]=value
    with pytest.raises(HubError,match='LIMITS_INVALID'): MediaProcessing(settings,store).recipe()


def test_additional_audio_track_is_reported_instead_of_silently_dropped(owned_video):
    settings,store,job,info=owned_video
    extra=Path(info['file']).parent/'owned-two-audio-tracks.mp4'
    subprocess.run(['ffmpeg','-nostdin','-y','-v','error','-i',info['file'],'-f','lavfi','-i',
        'sine=frequency=880:sample_rate=16000','-map','0:v:0','-map','0:a:0','-map','1:a:0',
        '-c:v','copy','-c:a','aac','-t','6',str(extra)],check=True,timeout=20)
    with pytest.raises(HubError,match='STREAM_SELECTION_REQUIRES_REVIEW'): inspect_media(extra)
    assert sha256_file(info['file'])==info['sha256']


def test_segment_manifest_path_traversal_cannot_target_original(owned_video):
    settings,store,job,info=owned_video
    service=MediaProcessing(settings,store)
    folder=Path(info['file']).parent/'malicious-list';folder.mkdir()
    (folder/'segments.csv').write_text('../original.mp4,0,6\n','utf-8')
    with pytest.raises(HubError,match='SEGMENT_COVERAGE_INVALID'): service._rows(folder,6,32)
    assert sha256_file(info['file'])==info['sha256']
