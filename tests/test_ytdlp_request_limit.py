"""Real child/yt-dlp Request adapter with owned HTTP responses and no sockets."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from adapters import ytdlp_replay as replay
from services.hub.errors import HubError
from test_ytdlp_replay import binding,child_transport,OwnedHTTPResponse,extracted,VIDEO


@pytest.mark.parametrize('value',[60,64,512])
def test_request_allowance_is_an_explicit_finite_integer(value):
    assert replay.request_limit(SimpleNamespace(config={'youtube_download_max_http_requests':value}))==value


def test_omitted_allowance_preserves_the_original_sixty_request_default():
    assert replay.request_limit(SimpleNamespace(config={}))==60


@pytest.mark.parametrize('value',[None,True,False,59,513,0,-1,512.0,'512',float('inf')])
def test_invalid_or_unbounded_request_config_is_rejected(value):
    with pytest.raises(HubError,match='YOUTUBE_DOWNLOAD_REQUEST_LIMIT_INVALID'):
        replay.request_limit(SimpleNamespace(config={'youtube_download_max_http_requests':value}))


def install_loop(f,monkeypatch,count):
    from yt_dlp.networking.common import Request
    def open_owned(url,allowed_hosts,**kwargs):
        assert url=='https://www.youtube.com/watch?v='+VIDEO and allowed_hosts==['www.youtube.com']
        assert not {'cookie','authorization','proxy-authorization'}&{k.lower() for k in kwargs['headers']}
        f.transport_calls.append('owned-local-response')
        return SimpleNamespace(close=lambda:None),OwnedHTTPResponse()
    def extract(ydl,url,download=True,**kwargs):
        assert download is False and ydl.params['continuedl'] is True
        assert ydl.params['retries']==ydl.params['fragment_retries']==ydl.params['extractor_retries']==0
        assert not ydl.params.get('proxy') and 'http_chunk_size' not in ydl.params
        for _ in range(count):
            with ydl.urlopen(Request(url)) as response:assert response.read()==b'owned local metadata response'
        return extracted(formats=[])
    monkeypatch.setattr(replay,'open_request',open_owned)
    monkeypatch.setattr(f.ytdlp.YoutubeDL,'extract_info',extract)


def test_default_stops_before_sixty_first_dispatch_and_preserves_partial_and_attempt(child_transport,monkeypatch):
    f=child_transport;install_loop(f,monkeypatch,61)
    part=f.folder/'stream-owned.mp4.part';part.write_bytes(b'Owned partial bytes, not a downloaded stream')
    before=part.read_bytes();job=f.store.get(f.job['job_id']);ticket=f.ticket.read_bytes();bound=deepcopy(f.binding)
    assert replay.child_download(f.ticket)==1
    result=f.result()
    assert result['error_code']=='YOUTUBE_DOWNLOAD_REQUEST_LIMIT' and result['http']['requests']==60
    assert result['http']['request_limit']==60 and len(f.transport_calls)==60
    assert f.store.query('SELECT COUNT(*) n FROM api_calls')[0]['n']==60
    assert part.read_bytes()==before and f.ticket.read_bytes()==ticket and f.binding==bound
    assert f.store.get(job['job_id'])==job and not f.external_socket_attempts


def test_configured_allowance_passes_sixty_without_changing_range_resume_or_rights(child_transport,monkeypatch):
    f=child_transport;f.settings.config['youtube_download_max_http_requests']=512;install_loop(f,monkeypatch,63)
    ticket=f.ticket.read_bytes();bound=deepcopy(f.binding)
    assert replay.child_download(f.ticket)==1
    result=f.result()
    # The owned extractor deliberately returns no media formats. Reaching that
    # separate guard proves all 63 requests ran, not a fabricated download.
    assert result['error_code']=='YOUTUBE_DOWNLOAD_PUBLIC_FORMAT_UNAVAILABLE'
    assert result['http']['requests']==63 and result['http']['request_limit']==512
    assert len(f.transport_calls)==63 and f.ticket.read_bytes()==ticket and f.binding==bound
    assert not f.external_socket_attempts and not (f.folder/'original.mp4').exists()


def test_configured_finite_limit_stops_exactly_at_limit(child_transport,monkeypatch):
    f=child_transport;f.settings.config['youtube_download_max_http_requests']=64;install_loop(f,monkeypatch,65)
    assert replay.child_download(f.ticket)==1
    assert f.result()['error_code']=='YOUTUBE_DOWNLOAD_REQUEST_LIMIT'
    assert f.result()['http']['requests']==64 and len(f.transport_calls)==64
    assert f.store.query('SELECT COUNT(*) n FROM api_calls')[0]['n']==64 and not f.external_socket_attempts


def test_invalid_configuration_fails_before_any_request(child_transport,monkeypatch):
    f=child_transport;f.settings.config['youtube_download_max_http_requests']=True;install_loop(f,monkeypatch,1)
    assert replay.child_download(f.ticket)==1 and f.result()['error_code']=='YOUTUBE_DOWNLOAD_REQUEST_LIMIT_INVALID'
    assert f.result()['http']['requests']==0 and not f.transport_calls
    assert not f.store.query('SELECT * FROM api_calls') and not f.external_socket_attempts


def test_shared_daily_budget_is_still_persisted_and_not_reset_by_larger_child_limit(child_transport,monkeypatch):
    f=child_transport;f.settings.config['youtube_download_max_http_requests']=512;install_loop(f,monkeypatch,3)
    limits=deepcopy(replay.LIMITS);limits[replay.PROVIDER]['daily_calls']=1
    monkeypatch.setattr(replay,'LIMITS',limits)
    assert replay.child_download(f.ticket)==1 and f.result()['error_code']=='API_BUDGET_EXCEEDED'
    assert len(f.transport_calls)==1 and f.store.query('SELECT COUNT(*) n FROM api_calls')[0]['n']==1
    assert replay.child_download(f.ticket)==1 and f.result()['error_code']=='API_BUDGET_EXCEEDED'
    assert len(f.transport_calls)==1 and f.store.query('SELECT COUNT(*) n FROM api_calls')[0]['n']==1
    assert not f.external_socket_attempts and not f.store.query('SELECT * FROM publications')
