"""Bounded canonical thumbnail requests; mock HTTP, actual FFmpeg output."""
from pathlib import Path
from types import SimpleNamespace
import uuid

import httpx
import pytest

from test_cover_normalizer_v4 import make_jpeg
from services.hub.errors import HubError
from services.hub.replay_video_cover import ensure_replay_cover


@pytest.fixture
def cover_input(tmp_path):
    root=tmp_path/'hub';root.mkdir()
    original=tmp_path/'owned.jpg';make_jpeg(original)
    return SimpleNamespace(root=root),{'job_id':str(uuid.uuid4())},{'source_id':'abcdefghijk'},str(uuid.uuid4()),original.read_bytes()


def client(data,status=200,calls=None):
    def factory(**kwargs):
        assert kwargs=={'timeout':30,'follow_redirects':False,'trust_env':False}
        def request(req):
            assert str(req.url)=='https://i.ytimg.com/vi/abcdefghijk/maxresdefault.jpg'
            assert 'authorization' not in req.headers and 'cookie' not in req.headers
            if calls is not None:calls.append(str(req.url))
            return httpx.Response(status,content=data,headers={'content-type':'image/jpeg','location':'https://owned.invalid/rejected'})
        return httpx.Client(transport=httpx.MockTransport(request),**kwargs)
    return factory


def test_canonical_download_normalizes_real_image_and_reuses_verified_cache(cover_input):
    settings,job,binding,attempt,data=cover_input;calls=[]
    first=ensure_replay_cover(settings,job,binding,attempt,client_factory=client(data,calls=calls))
    second=ensure_replay_cover(settings,job,binding,attempt,client_factory=client(data,calls=calls))
    assert second['reused'] is True and first['output_file']==second['output_file'] and len(calls)==1
    assert Path(first['output_file']).read_bytes()[:8]==b'\x89PNG\r\n\x1a\n'
    source=settings.root/'media'/job['job_id']/'short-video-attempts'/attempt/'cover-source-official-maxres.jpg'
    assert source.read_bytes()==data and not list(source.parent.glob('*.partial'))


@pytest.mark.parametrize('status',[302,403,429])
def test_redirect_login_denial_and_rate_limit_do_not_create_source(cover_input,status):
    settings,job,binding,attempt,data=cover_input
    with pytest.raises(HubError) as error:
        ensure_replay_cover(settings,job,binding,attempt,client_factory=client(data,status))
    assert error.value.retryable is (status==429)
    assert not list((settings.root/'media').rglob('*.jpg'))


def test_oversized_source_stops_before_publishing_any_original(cover_input):
    settings,job,binding,attempt,_=cover_input
    with pytest.raises(HubError,match='REPLAY_OFFICIAL_COVER_TOO_LARGE'):
        ensure_replay_cover(settings,job,binding,attempt,client_factory=client(b'x'*1_000_001))
    assert not list((settings.root/'media').rglob('*.jpg'))


def test_cache_cannot_silently_change_the_persistent_attempt(cover_input):
    settings,job,binding,attempt,data=cover_input
    ensure_replay_cover(settings,job,binding,attempt,client_factory=client(data))
    with pytest.raises(HubError,match='REPLAY_COVER_EXISTING_PROVENANCE_INVALID'):
        ensure_replay_cover(settings,job,binding,str(uuid.uuid4()),client_factory=client(data))


def test_invalid_download_does_not_produce_a_derived_cover(cover_input):
    settings,job,binding,attempt,_=cover_input
    with pytest.raises(HubError):
        ensure_replay_cover(settings,job,binding,attempt,client_factory=client(b'not-a-JPEG'))
    assert not (settings.root/'media'/job['job_id']/'cover-assets-v4/provenance.json').exists()
