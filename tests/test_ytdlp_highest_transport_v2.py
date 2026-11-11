"""Real child, yt-dlp Request/Response and persistent budgets; owned peers only."""
from types import SimpleNamespace
from copy import deepcopy
import json,socket

import pytest

from adapters import ytdlp_highest_v2 as hq
from adapters import ytdlp_replay as legacy
from services.hub.config import Settings
from services.hub.budget import Budget
from services.hub.errors import HubError
from test_youtube_highest_v2 import highest,info
from test_ytdlp_replay import OwnedHTTPResponse


@pytest.fixture
def transport(highest,monkeypatch):
    import yt_dlp
    from yt_dlp.cookies import YoutubeDLCookieJar
    f=highest;ident=f.service.enqueue_pilot(f.payload);job=f.db.claim('video',600,4);assert job['job_id']==ident
    binding=f.service._binding(job)[1];folder=hq.checked_folder(f.s,ident);ticket=folder/'ticket.json'
    hq.atomic_json(ticket,{'root':str(f.s.root),'job_id':ident,'lease_owner':job['lease_owner'],'binding':binding})
    f.job=job;f.binding=binding;f.folder=folder;f.ticket=ticket;f.ytdlp=yt_dlp;f.calls=[];f.sockets=[];f.media_calls=[]
    f.result=lambda:json.loads((folder/'download-result.json').read_bytes())
    monkeypatch.setattr(Settings,'load',lambda:f.s)
    class OwnedJar(YoutubeDLCookieJar):
        def extract_cookies(self,*a,**k):pass
    monkeypatch.setattr(legacy,'load_youtube_session',lambda *a:OwnedJar())
    def forbidden(*a,**k):f.sockets.append('forbidden');raise AssertionError('Owned child tests must not open external sockets')
    monkeypatch.setattr(socket,'create_connection',forbidden)
    def no_media(*a,**k):f.media_calls.append('owned-stop');raise HubError('OWNED_MEDIA_STAGE_NOT_EXECUTED',review=True)
    monkeypatch.setattr(yt_dlp.YoutubeDL,'dl',no_media)
    return f


def owned_loop(f,monkeypatch,count,*,url=None,headers=None,status=200,content_range=None):
    from yt_dlp.networking.common import Request
    url=url or 'https://www.youtube.com/watch?v='+f.binding['video_id']
    class Reply(OwnedHTTPResponse):
        def getheaders(self):
            values=super().getheaders()
            if content_range:values.append(('Content-Range',content_range))
            return values
    def open_owned(current,hosts,**kwargs):
        f.calls.append({'method':kwargs['method'],'headers':{k.lower():v for k,v in kwargs['headers'].items()}})
        reply=Reply();reply.status=status
        return SimpleNamespace(close=lambda:None),reply
    def extract(ydl,current,download=True,**kwargs):
        assert download is False and ydl.params['continuedl'] is True and ydl.params['retries']==0
        for _ in range(count):
            with ydl.urlopen(Request(url,headers=headers)) as response:response.read()
        return info(f.binding)
    monkeypatch.setattr(hq,'open_request',open_owned);monkeypatch.setattr(f.ytdlp.YoutubeDL,'extract_info',extract)


@pytest.mark.parametrize('reply_status',[206,200,429])
def test_fifo_range_body_uses_guarded_transport_and_request_ledger(transport,monkeypatch,reply_status):
    import io
    from hashlib import sha256
    f=transport
    ticket=json.loads(f.ticket.read_bytes())
    bound=ticket['binding']
    bound['request_mode']='fifo-v4'
    for fmt in bound['source_plan']['selected_formats']:
        fmt['filesize']=4
    hq.atomic_json(f.ticket,ticket)
    monkeypatch.setattr(f.ytdlp.YoutubeDL,'extract_info',lambda *a,**kw:info(bound))
    class Response(io.BytesIO):
        status=reply_status
        def getheaders(self):
            return [('Content-Range','bytes 0-3/4'),('Content-Length','4'),('Content-Type','application/octet-stream')]
        def getheader(self,key,default=None):
            return dict(self.getheaders()).get(key,default)
    def open_owned(url,hosts,**kwargs):
        assert kwargs['headers']['Range']=='bytes=0-3'
        assert 'cookie' not in {key.lower() for key in kwargs['headers']}
        return SimpleNamespace(close=lambda:None),Response(b'abcd')
    monkeypatch.setattr(hq,'open_request',open_owned)
    code=hq.child_download(f.ticket,service_type=lambda *a:SimpleNamespace(check_binding=lambda *a:None),
        selector=lambda raw,b:raw['formats'],plan_validator=lambda p:None)
    result=f.result()
    assert (result['http']['range_workers'],result['http']['rate_bytes_per_second'])==(2,18_750_000)
    assert result['http']['requests']==f.db.query('SELECT count(*) n FROM api_calls')[0]['n']
    if reply_status==206:
        assert code==0 and result['http']['response_bytes']==8
        assert [t['sha256'] for t in result['tracks']]==[sha256(b'abcd').hexdigest()]*2
    else:
        assert code==1 and result['http']['requests']==1 and result['http']['response_bytes']==0
        assert result['error_code']==('YOUTUBE_DOWNLOAD_RATE_LIMITED' if reply_status==429 else 'YOUTUBE_HIGHEST_RESUME_RANGE_NOT_HONORED')
        assert not list(f.folder.glob('stream-*.mp4'))


@pytest.mark.timeout(180)
def test_real_child_exceeds_old_512_and_1000_without_resetting_existing_history(transport,monkeypatch):
    f=transport;call=Budget(f.db,legacy.LIMITS).reserve(legacy.PROVIDER,units=60);Budget(f.db,legacy.LIMITS).finish(call,'done')
    old=f.db.query('SELECT * FROM api_calls')[0];owned_loop(f,monkeypatch,1100)
    assert hq.child_download(f.ticket)==1
    result=f.result();assert result['error_code']=='OWNED_MEDIA_STAGE_NOT_EXECUTED'
    assert result['http']['requests']==1100 and result['http']['request_limit']==1436
    assert len(f.calls)==1100 and sum(x['units'] for x in f.db.query('SELECT * FROM api_calls'))==1160
    assert f.db.query('SELECT * FROM api_calls WHERE call_id=?',(old['call_id'],))[0]==old
    assert f.media_calls==['owned-stop'] and not f.sockets and not (f.folder/'original.mp4').exists()
    assert legacy.MAX_REQUEST_LIMIT==512 and legacy.LIMITS[legacy.PROVIDER]['daily_calls']==1000


@pytest.mark.timeout(180)
def test_size_derived_limit_stops_before_one_more_http_dispatch(transport,monkeypatch):
    f=transport;owned_loop(f,monkeypatch,1437)
    before=f.db.get(f.job['job_id']);part=f.folder/'stream-299.mp4.part';part.write_bytes(b'Owned retained partial')
    assert hq.child_download(f.ticket)==1
    assert f.result()['error_code']=='YOUTUBE_HIGHEST_REQUEST_LIMIT' and f.result()['http']['requests']==1436
    assert len(f.calls)==1436 and part.read_bytes()==b'Owned retained partial' and not f.media_calls and not f.sockets
    assert f.db.get(f.job['job_id'])==before


def test_whole_media_preflight_rejects_insufficient_shared_daily_budget(transport,monkeypatch):
    f=transport;budget=Budget(f.db,hq.budget_limits(f.binding));call=budget.reserve(legacy.PROVIDER,units=2250);budget.finish(call,'done')
    owned_loop(f,monkeypatch,1)
    assert hq.child_download(f.ticket)==1 and f.result()['error_code']=='API_BUDGET_EXCEEDED'
    assert len(f.calls)==1 and not f.media_calls and not (f.folder/'selection.json').exists()
    assert sum(x['units'] for x in f.db.query('SELECT * FROM api_calls'))==2251


@pytest.mark.parametrize('code,error',[(403,'YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED'),(429,'YOUTUBE_DOWNLOAD_RATE_LIMITED')])
def test_platform_protection_stops_without_retry_or_media(transport,monkeypatch,code,error):
    f=transport;owned_loop(f,monkeypatch,3,status=code)
    assert hq.child_download(f.ticket)==1 and f.result()['error_code']==error
    assert len(f.calls)==1 and not f.media_calls and not f.sockets and f.result()['retryable'] is False


@pytest.mark.parametrize('status,content_range',[(200,None),(206,'bytes 0-100/10505641986'),(206,'bytes 100-200/999')])
def test_resume_cannot_truncate_part_when_server_ignores_or_changes_range(transport,monkeypatch,status,content_range):
    f=transport;part=f.folder/'stream-299.mp4.part';part.write_bytes(b'Owned original partial')
    owned_loop(f,monkeypatch,1,url='https://rr1---owned.googlevideo.com/videoplayback?owned=1',
        headers={'Range':'bytes=100-200'},status=status,content_range=content_range)
    assert hq.child_download(f.ticket)==1 and f.result()['error_code']=='YOUTUBE_HIGHEST_RESUME_RANGE_NOT_HONORED'
    assert part.read_bytes()==b'Owned original partial' and not f.media_calls


@pytest.mark.parametrize('header',['Cookie','Authorization','Proxy-Authorization'])
def test_secrets_cannot_leave_authorized_youtube_origin(transport,monkeypatch,header):
    f=transport;owned_loop(f,monkeypatch,1,url='https://rr1---owned.googlevideo.com/videoplayback?owned=1',headers={header:'Owned fake auth'})
    assert hq.child_download(f.ticket)==1 and f.result()['error_code']=='YOUTUBE_DOWNLOAD_AUTHENTICATION_FORBIDDEN'
    assert not f.calls and not f.sockets and not f.media_calls


def test_fresh_child_refuses_old_date_even_if_publication_payload_is_new(transport,monkeypatch):
    f=transport;ticket=json.loads(f.ticket.read_bytes());ticket['binding']['source_plan']['source_actual_start_time']='2026-07-01T00:00:00Z'
    hq.atomic_json(f.ticket,ticket);owned_loop(f,monkeypatch,1)
    assert hq.child_download(f.ticket)==1 and f.result()['error_code']=='YOUTUBE_HIGHEST_BEFORE_OPERATING_START'
    assert not f.calls and not f.sockets and not f.media_calls
