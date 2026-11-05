"""Real shared child/FFmpeg paths, owned HTTP peers and local tracks only."""
from copy import deepcopy
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
import json,shutil,socket,subprocess
import pytest

from adapters import ytdlp_natural_v3 as native
from adapters import ytdlp_highest_v2 as engine
from adapters import ytdlp_replay as legacy
from services.hub.config import Settings
from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.youtube_download import digest
from services.hub.youtube_download_v3 import YouTubeNaturalPrepare,YouTubeNaturalDownload,STATE,VERSION
from services.hub.youtube_monitor import YouTubeMonitor
from test_youtube_download_v3 import natural,prepare,owned_planner,write_policy
from test_ytdlp_highest_v2 import make_video,tracks
from test_ytdlp_replay import OwnedHTTPResponse


@pytest.fixture
def transport(natural,monkeypatch):
    import yt_dlp
    from yt_dlp.cookies import YoutubeDLCookieJar
    f=natural;ident=f.service.enqueue_if_eligible(f.payload);job=f.db.claim('video',600,4)
    binding=f.service._binding(job)[1];folder=native.planning_folder(f.s,ident);ticket=folder/'ticket.json'
    engine.atomic_json(ticket,{'root':str(f.s.root),'job_id':ident,'lease_owner':job['lease_owner'],'binding':binding})
    f.job=job;f.binding=binding;f.folder=folder;f.ticket=ticket;f.ytdlp=yt_dlp;f.calls=[];f.media=[]
    monkeypatch.setattr(Settings,'load',lambda:f.s)
    class OwnedJar(YoutubeDLCookieJar):
        def extract_cookies(self,*args):pass
    monkeypatch.setattr(legacy,'load_youtube_session',lambda *args:OwnedJar())
    monkeypatch.setattr(socket,'create_connection',lambda *args,**kwargs:pytest.fail('External sockets forbidden in owned child test'))
    def no_media(*args,**kwargs):f.media.append('unexpected');pytest.fail('Metadata planner must never call a downloader')
    monkeypatch.setattr(yt_dlp.YoutubeDL,'dl',no_media)
    return f


def owned_extract(f,monkeypatch,*,count=1,mime='application/json',status=200):
    from yt_dlp.networking.common import Request
    class Reply(OwnedHTTPResponse):
        def getheaders(self):return [('Content-Type',mime)]
        def getheader(self,key,default=None):return mime if key.lower()=='content-type' else super().getheader(key,default)
    def open_owned(url,hosts,**kwargs):
        f.calls.append(url);reply=Reply();reply.status=status
        return SimpleNamespace(close=lambda:None),reply
    def extract(ydl,url,download=True,**kwargs):
        assert url=='https://www.youtube.com/watch?v='+f.raw['id'] and download is False
        for _ in range(count):
            with ydl.urlopen(Request(url)) as response:response.read()
        return f.info
    monkeypatch.setattr(engine,'open_request',open_owned)
    monkeypatch.setattr(f.ytdlp.YoutubeDL,'extract_info',extract)


def test_real_shared_child_only_plans_generic_source_and_persists_no_signed_urls(transport,monkeypatch):
    f=transport;owned_extract(f,monkeypatch)
    assert native.planning_child(f.ticket)==0
    result=json.loads((f.folder/'download-result.json').read_bytes())
    assert result['version']==VERSION and result['source_plan']['video_id']=='ownedNew001'
    assert result['source_plan']['selected_formats'][0]['format_id']=='owned60'
    assert result['http']['requests']==1 and result['media_downloaded'] is result['publication_authorized'] is False
    assert not f.media and 'secret=' not in json.dumps(result) and not list(f.folder.glob('*.mp4*'))
    assert sum(x['units'] for x in f.db.query('SELECT * FROM api_calls'))==1


def test_metadata_cap_stops_65th_request_without_download(transport,monkeypatch):
    f=transport;owned_extract(f,monkeypatch,count=65)
    assert native.planning_child(f.ticket)==1
    result=json.loads((f.folder/'download-result.json').read_bytes())
    assert result['error_code']=='YOUTUBE_HIGHEST_REQUEST_LIMIT' and len(f.calls)==64
    assert not f.media and not list(f.folder.glob('*.mp4*'))


@pytest.mark.parametrize('status,error',[(200,'YOUTUBE_NATURAL_PLANNING_MEDIA_FORBIDDEN'),(403,'YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED'),
    (429,'YOUTUBE_DOWNLOAD_RATE_LIMITED')])
def test_metadata_only_rejects_media_and_preserves_platform_protection_status(transport,monkeypatch,status,error):
    f=transport;owned_extract(f,monkeypatch,mime='video/mp4',status=status)
    assert native.planning_child(f.ticket)==1
    assert json.loads((f.folder/'download-result.json').read_bytes())['error_code']==error
    assert len(f.calls)==1 and not f.media and not list(f.folder.glob('*.mp4*'))


@pytest.mark.parametrize('change',['higher_tier','size_changed','signed_auth_header'])
def test_fresh_download_selection_cannot_use_stale_plan_or_leak_auth(natural,change):
    f=natural;old,result,job=prepare(f);binding=YouTubeNaturalDownload(f.s,f.db)._binding(job)[1]
    info=deepcopy(f.info)
    for item in info['formats']:item['url']='https://rr1---owned.googlevideo.com/videoplayback?owned=1'
    if change=='higher_tier':info['formats'][1]['height']=2160
    if change=='size_changed':info['formats'][1]['filesize']+=1
    if change=='signed_auth_header':info['formats'][1]['http_headers']={'Cookie':'owned-secret-do-not-send'}
    with pytest.raises(HubError):native.select_from_info(info,binding)
    assert not f.db.query('SELECT * FROM api_calls') and not f.db.query('SELECT * FROM publications')


@pytest.mark.parametrize('bad_quality',[False,True])
def test_real_generic_720p30_stream_copy_and_quality_mismatch(natural,tracks,tmp_path,monkeypatch,bad_quality):
    f=natural;video=tmp_path/'owned-generic.mp4';make_video(video,fps='30',size='1280x720')
    f.raw['contentDetails']['duration']='PT1S';f.raw['liveStreamingDetails']['actualEndTime']='2026-11-01T02:00:01Z'
    f.payload.update(duration='PT1S',raw=f.raw)
    YouTubeMonitor(f.s,f.db,None).observe_video(f.raw,f.payload['channel_id'])
    # The monitor's hook genuinely admitted the updated source; one metadata
    # job was already completed by the fixture and remains unchanged.
    f.info['duration']=1;chosen=f.info['formats'][0];chosen.update(width=1280,height=720,fps=60 if bad_quality else 30,
        format_id='genericH264',filesize=video.stat().st_size,tbr=5000)
    audio=f.info['formats'][-1];audio.update(format_id='genericAAC',filesize=tracks[1].stat().st_size)
    f.info['formats']=[chosen,audio]
    prepare_job,result,job=prepare(f)
    downloader=native.NaturalReplayDownloader(f.s)
    def owned_copy(current,binding,folder,ticket,output,check):
        check();records=[]
        for src,identity in zip((video,tracks[1]),binding['source_plan']['selected_formats']):
            file=folder/('stream-'+identity['format_id']+'.'+identity['ext']);shutil.copyfile(src,file)
            records.append({**identity,'bytes':file.stat().st_size,'sha256':engine.sha256_file(file)})
        return {'binding_sha256':digest(binding),'tracks':records,'source_plan':binding['source_plan'],
            'selected_formats':binding['source_plan']['selected_formats'],'http':{'requests':0,'owned_copier_only':True}}
    monkeypatch.setattr(downloader,'_helper',owned_copy)
    service=YouTubeNaturalDownload(f.s,f.db,downloader_factory=lambda s:downloader)
    before=video.read_bytes()
    if bad_quality:
        with pytest.raises(HubError,match='QUALITY_CHANGED'):service.process(job)
        assert not list((f.s.root/'media'/job['job_id']).rglob('verified.json'))
    else:
        result=service.process(job);asset=result[STATE]['source_asset'];file=Path(asset['file'])
        assert file.parent.name=='youtube-highest-v3' and asset['stream_config']['video']['height']==720
        assert asset['stream_config']['video']['avg_frame_rate']=='30/1' and result[STATE]['state']=='verified'
        assert result[STATE]['receipt']['source_to_merged_packet_coverage_verified'] is True
        assert all(x['packets']>1 for x in asset['packet_evidence'].values())
        assert service.process(job)==result
    assert video.read_bytes()==before and not f.db.query('SELECT * FROM api_calls') and not f.db.query('SELECT * FROM publications')
