"""Real owned 1080p60 tracks, FFmpeg copy, ffprobe and packet verification.

The download helper is an identified local copier, never a YouTube retrieval.
"""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json,shutil,subprocess

import pytest

from adapters import ytdlp_highest_v2 as hq
from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.youtube_download_v2 import YouTubeHighestDownload,STATE
from services.hub.youtube_monitor import YouTubeMonitor
from test_youtube_highest_v2 import highest,write_policy,START,CHANNEL


def make_video(file,*,codec='libx264',fps='60',size='1920x1080'):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'),'Real FFmpeg/ffprobe are required'
    cmd=['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-f','lavfi','-i','color=red:s='+size+':r='+fps,
        '-t','1','-an','-c:v',codec,'-threads','1','-pix_fmt','yuv420p']
    if codec=='libx264':cmd+=['-preset','ultrafast']
    subprocess.run(cmd+['-y',str(file)],check=True,capture_output=True,timeout=30)


@pytest.fixture(scope='module')
def tracks(tmp_path_factory):
    folder=tmp_path_factory.mktemp('owned-highest-originals');video=folder/'video.mp4';audio=folder/'audio.m4a'
    make_video(video)
    subprocess.run(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-f','lavfi','-i','sine=frequency=440:sample_rate=48000',
        '-t','1','-vn','-c:a','aac','-b:a','128k','-threads','1','-y',str(audio)],check=True,capture_output=True,timeout=20)
    return video,audio


@pytest.fixture
def real(highest,tracks,monkeypatch):
    f=highest;raw=deepcopy(f.raw);raw['contentDetails']['duration']='PT1S'
    raw['liveStreamingDetails']['actualEndTime']='2026-10-31T16:00:37Z'
    YouTubeMonitor(f.s,f.db,None).observe_video(raw,CHANNEL)
    f.raw=raw;f.payload.update(raw=raw,duration='PT1S')
    f.p.update(video_bytes=tracks[0].stat().st_size,audio_bytes=tracks[1].stat().st_size)
    write_policy(f.s,f.p)
    ident=f.service.enqueue_pilot(f.payload);job=f.db.claim('video',180,4);assert job['job_id']==ident
    downloader=hq.HighestReplayDownloader(f.s);calls=[]
    def owned_helper(current,binding,folder,ticket,output,check):
        check();records=[]
        for input_file,identity in zip(f.inputs,binding['source_plan']['selected_formats']):
            file=folder/('stream-'+identity['format_id']+'.'+identity['ext']);shutil.copyfile(input_file,file)
            records.append({**identity,'bytes':file.stat().st_size,'sha256':hq.sha256_file(file)})
        result={'binding_sha256':hq.digest(binding),'tracks':records,'source_plan':binding['source_plan'],
            'selected_formats':binding['source_plan']['selected_formats'],
            'http':{'requests':0,'response_bytes':0,'request_limit':binding['source_plan']['max_http_requests'],
                    'owned_copier_only':True,'actual_youtube_download':False}}
        hq.atomic_json(output,result);calls.append('owned_helper_no_network');return result
    monkeypatch.setattr(downloader,'_helper',owned_helper)
    f.inputs=tracks;f.job=job;f.downloader=downloader;f.calls=calls
    f.service=YouTubeHighestDownload(f.s,f.db,downloader_factory=lambda settings:downloader)
    return f


def test_actual_1080p60_stream_copy_preserves_both_packet_chains_and_receipt(real):
    f=real;before=[p.read_bytes() for p in f.inputs]
    result=f.service.process(f.job);state=result[STATE];asset=state['source_asset'];receipt=state['receipt']
    assert state['state']=='verified' and asset['stream_config']['video']['width']==1920
    assert asset['stream_config']['video']['height']==1080 and asset['stream_config']['video']['avg_frame_rate']=='60/1'
    assert receipt['source_stream_config']==asset['stream_config'] and receipt['source_to_merged_packet_coverage_verified'] is True
    assert receipt['selected_formats']==asset['source_plan']['selected_formats']==asset['selected_formats']
    assert all(x['packets']>1 for x in asset['packet_evidence'].values())
    file=Path(asset['file']);assert file.name=='original.mp4' and file.parent.name=='youtube-highest-v2'
    stored=json.loads((file.parent/'verified.json').read_bytes())
    assert stored['human_review'] is stored['publication_authorized'] is stored['creator_original_bytes_verified'] is False
    assert result['publish_performed'] is False and result['human_review'] is False
    assert [p.read_bytes() for p in f.inputs]==before and not f.db.query('SELECT * FROM publications')
    assert f.calls==['owned_helper_no_network'] and not f.db.query('SELECT * FROM api_calls')
    assert f.service.process(f.job)==result and f.calls==['owned_helper_no_network']


@pytest.mark.parametrize('change',['fps30','height720','mpeg4'])
def test_real_media_cannot_lie_about_selected_quality(real,tmp_path,change):
    f=real;wrong=tmp_path/'wrong.mp4'
    make_video(wrong,codec='mpeg4' if change=='mpeg4' else 'libx264',fps='30' if change=='fps30' else '60',
        size='1280x720' if change=='height720' else '1920x1080')
    # Bind the fixture's real byte size, so the failure is the real probe and
    # not a deliberately stale hash or size field.
    f.p['video_bytes']=wrong.stat().st_size;write_policy(f.s,f.p)
    current=f.db.get(f.job['job_id']);current['payload']['_highest_download_policy_sha256']=f.service.policy()['_sha256']
    with f.db.transaction() as db:db.execute('UPDATE jobs SET payload=? WHERE job_id=?',(encode(current['payload']),current['job_id']))
    f.job=f.db.get(f.job['job_id']);f.inputs=(wrong,f.inputs[1])
    with pytest.raises(HubError,match='YOUTUBE_HIGHEST_QUALITY_CHANGED'):f.service.process(f.job)
    assert f.db.get(f.job['job_id'])['result'][STATE]['state']=='acquiring'
    assert not (f.s.root/'media'/f.job['job_id']/'youtube-highest-v2/verified.json').exists()


def test_packet_mismatch_does_not_write_verified_receipt(real,monkeypatch):
    f=real;actual=hq.packet_evidence
    def bad(*a,**k):
        value=actual(*a,**k);value['video']['chain']='0'*64;return value
    monkeypatch.setattr(hq,'packet_evidence',bad)
    with pytest.raises(HubError,match='YOUTUBE_HIGHEST_PACKET_MISMATCH'):f.service.process(f.job)
    assert not (f.s.root/'media'/f.job['job_id']/'youtube-highest-v2/verified.json').exists()


def test_policy_revoke_during_helper_stops_without_verified_claim(real,monkeypatch):
    f=real;actual=f.downloader._helper
    def revoked(*a,**k):
        value=actual(*a,**k);f.s.config['youtube_highest_download_enabled']=False;return value
    monkeypatch.setattr(f.downloader,'_helper',revoked)
    with pytest.raises(HubError,match='YOUTUBE_HIGHEST_DISABLED'):f.service.process(f.job)
    assert f.db.get(f.job['job_id'])['result'][STATE]['state']=='acquiring'


def test_low_disk_after_claim_keeps_originals_and_no_verified_receipt(real,monkeypatch):
    f=real;before=[p.read_bytes() for p in f.inputs]
    monkeypatch.setattr('services.hub.maintenance.shutil.disk_usage',lambda _:SimpleNamespace(free=15_000_000_000))
    with pytest.raises(HubError,match='DISK_LOW'):f.service.process(f.job)
    assert [p.read_bytes() for p in f.inputs]==before and not f.calls


def test_receipt_survives_parent_restart_without_reinvoking_network(real):
    f=real;first=f.service.process(f.job);binding=first[STATE]['binding']
    replacement=hq.HighestReplayDownloader(f.s)
    def no_network(*a,**k):raise AssertionError('Verified local cache must not launch network helper')
    replacement._helper=no_network
    cached=replacement.acquire(f.job,binding,check=lambda:f.service.check_binding(f.job,binding))
    assert cached['source_asset']['sha256']==first[STATE]['source_asset']['sha256']
    assert len(f.calls)==1
