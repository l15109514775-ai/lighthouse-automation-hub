"""Owned in-memory session tests: no browser profile or credential is read.

All Cookie objects are synthetic. Linux-only filesystem checks use an isolated
temporary profile; the extractor is replaced before it can read that profile.
The readonly-mount boundary alone is supplied by an explicit owned fixture.
"""
from http.cookiejar import Cookie
import json
import os
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
import time

import pytest
from yt_dlp.cookies import YoutubeDLCookieJar

from adapters import ytdlp_replay as replay
from services.hub.errors import HubError


LINUX_ONLY = pytest.mark.skipif(os.name != 'posix', reason='Real Linux UID and private profile mode checks')
WWW = 'https://www.youtube.com/youtubei/v1/player'
CDN = 'https://rr1---owned.googlevideo.com/videoplayback?expire=1'
SDK_AUTH = 'SAPISIDHASH 1_' + '0' * 40


def owned_cookie(name, *, domain='.youtube.com', value='owned-fixture-value', expired=False):
    return Cookie(version=0, name=name, value=value, port=None, port_specified=False,
        domain=domain, domain_specified=True, domain_initial_dot=domain.startswith('.'),
        path='/', path_specified=True, secure=True,
        expires=int(time.time()) + (-3600 if expired else 3600), discard=False,
        comment=None, comment_url=None, rest={})


def owned_jar(*cookies):
    jar = YoutubeDLCookieJar()
    for cookie in cookies:
        jar.set_cookie(cookie)
    return jar


@pytest.fixture
def session_jar():
    return owned_jar(owned_cookie('LOGIN_INFO'), owned_cookie('SAPISID'),
                     owned_cookie('owned-google-cookie', domain='.google.com'),
                     owned_cookie('owned-host-cookie', domain='youtube.com'))


def normalized(headers):
    return {k.lower(): v for k, v in headers.items()}


def test_filter_keeps_only_the_two_exact_youtube_domains_in_a_memory_jar():
    domains = ['.youtube.com', 'youtube.com', 'www.youtube.com', '.google.com',
               '.youtube.com.evil.test', '.evil.youtube.com', 'evil-youtube.com', '.ytimg.com']
    jar = owned_jar(*(owned_cookie('owned-' + str(i), domain=domain) for i, domain in enumerate(domains)))
    filtered = replay.filter_youtube_cookies(jar)
    assert sorted(cookie.domain for cookie in filtered) == ['.youtube.com', 'youtube.com']
    assert len(list(jar)) == len(domains) and filtered is not jar
    assert filtered.filename is None and jar.filename is None


def test_authorized_www_request_uses_filtered_jar_cookie_and_preserves_sdk_authorization(session_jar):
    jar = replay.filter_youtube_cookies(session_jar)
    headers = replay.scoped_headers({'User-Agent': 'Owned-default', 'Accept-Encoding': 'gzip'},
        {'User-Agent': 'Owned-selected', 'Authorization': SDK_AUTH, 'Cookie': 'untrusted_raw=must-not-be-used'},
        WWW, authenticated=True, jar=jar)
    values = normalized(headers)
    assert values['authorization'] == SDK_AUTH
    assert values['cookie'] == jar.get_cookie_header(WWW)
    assert 'untrusted_raw' not in values['cookie'] and 'owned-google-cookie' not in values['cookie']
    assert values['user-agent'] == 'Owned-selected' and values['accept-encoding'] == 'identity'


@pytest.mark.parametrize('url,authenticated', [
    (WWW, False), (CDN, True), ('https://i.ytimg.com/vi/owned/default.jpg', True),
    ('https://s.ytimg.com/player.js', True), ('https://youtube.com/watch?v=ownedReplay1', True),
])
def test_anonymous_or_non_www_request_does_not_take_cookies_from_the_memory_jar(session_jar, url, authenticated):
    values = normalized(replay.scoped_headers({'User-Agent': 'Owned'}, {}, url,
        authenticated=authenticated, jar=replay.filter_youtube_cookies(session_jar)))
    assert 'cookie' not in values and 'authorization' not in values
    assert values['accept-encoding'] == 'identity'


@pytest.mark.parametrize('url,authenticated', [(WWW, False), (CDN, True), ('https://i.ytimg.com/x', True)])
@pytest.mark.parametrize('header', ['Cookie', 'Authorization', 'X-Goog-AuthUser'])
def test_unapproved_origin_cannot_supply_authentication_or_account_context(url, authenticated, header):
    with pytest.raises(HubError, match='YOUTUBE_DOWNLOAD_AUTHENTICATION_FORBIDDEN'):
        replay.scoped_headers({}, {header: 'owned-credential-context'}, url, authenticated=authenticated)


@pytest.mark.parametrize('target,source', [
    (CDN, 'www.youtube.com'), ('https://i.ytimg.com/x', 'www.youtube.com'),
    (WWW, 'rr1---owned.googlevideo.com'),
])
def test_cross_host_redirect_strips_auth_and_does_not_reinject_jar_cookie(session_jar, target, source):
    supplied = {'Cookie': 'owned=first-hop', 'Authorization': SDK_AUTH,
        'X-Goog-AuthUser': '0', 'X-Goog-PageId': 'owned-page',
        'X-Youtube-Identity-Token': 'owned-identity', 'X-Origin': 'https://www.youtube.com',
        'X-Youtube-Bootstrap-Logged-In': 'true', 'User-Agent': 'Owned'}
    values = normalized(replay.scoped_headers({}, supplied, target, authenticated=True,
        jar=replay.filter_youtube_cookies(session_jar), original_host=source))
    for name in supplied:
        if name != 'User-Agent':
            assert name.lower() not in values
    assert values['user-agent'] == 'Owned' and values['accept-encoding'] == 'identity'


def test_same_host_redirect_recomputes_cookie_instead_of_forwarding_raw_header(session_jar):
    jar = replay.filter_youtube_cookies(session_jar)
    target = 'https://www.youtube.com/s/player/owned/base.js'
    values = normalized(replay.scoped_headers({}, {'Cookie': 'owned-old=first-hop'}, target,
        authenticated=True, jar=jar, original_host='www.youtube.com'))
    assert values['cookie'] == jar.get_cookie_header(target) and 'owned-old' not in values['cookie']


@pytest.mark.parametrize('header', ['Proxy-Authorization', 'Forwarded', 'X-Forwarded-For', 'X-Real-IP', 'Host'])
@pytest.mark.parametrize('redirect', [False, True])
def test_proxy_credentials_and_forwarded_origin_headers_are_always_rejected(header, redirect):
    with pytest.raises(HubError, match='YOUTUBE_DOWNLOAD_AUTHENTICATION_FORBIDDEN'):
        replay.scoped_headers({}, {header: 'owned-forbidden'}, CDN if redirect else WWW,
            authenticated=True, original_host='www.youtube.com' if redirect else None)


def test_cookie_logger_accepts_library_keyword_arguments_without_output(capsys):
    logger = replay.SilentCookieLog()
    for name in ('info', 'debug', 'warning', 'error'):
        getattr(logger, name)('owned value must stay silent', only_once=True, once=True, arbitrary='owned')
    captured = capsys.readouterr()
    assert not captured.out and not captured.err


@pytest.mark.parametrize('sid_name', ['SAPISID', '__Secure-3PAPISID'])
def test_installed_sdk_recognizes_the_owned_memory_session_without_saving_a_jar(monkeypatch, sid_name):
    import socket
    import yt_dlp
    from yt_dlp.extractor.youtube._video import YoutubeIE
    monkeypatch.setenv('YTDLP_NO_PLUGINS', '1')
    def forbidden(*args, **kwargs):
        raise AssertionError('Owned in-memory SDK session test must not save or connect')
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(YoutubeDLCookieJar, 'save', forbidden)
    jar = owned_jar(owned_cookie('LOGIN_INFO'), owned_cookie(sid_name))
    with yt_dlp.YoutubeDL({'quiet': True, 'logger': replay.SilentCookieLog(), 'cookiefile': None,
        'cookiesfrombrowser': None, 'cachedir': False, 'js_runtimes': {}, 'allowed_extractors': ['youtube']}) as ydl:
        ydl.cookiejar = jar
        ie = YoutubeIE(ydl)
        assert ie.is_authenticated is True
        headers = ie.generate_api_headers(default_client='web')
        assert headers['Authorization'].startswith('SAPISIDHASH ')
        assert 'owned-fixture-value' not in headers['Authorization']
        assert headers['Origin'] == 'https://www.youtube.com'
        assert ydl.cookiejar.filename is None


@pytest.mark.parametrize('change', [
    {'authenticated_session': False}, {'authenticated_session': 1},
    {'session_profile': 'bilibili'}, {'session_profile': '/arbitrary/profile'},
    {'session_authorization_sha256': '1' * 64},
])
def test_unauthorized_session_binding_is_rejected_before_cookie_extraction(runtime, monkeypatch, change):
    import yt_dlp.cookies
    settings, _ = runtime
    binding = {'authenticated_session': True, 'session_profile': 'youtube',
               'session_authorization_sha256': '0' * 64, 'policy_sha256': '0' * 64, **change}
    extracted = []
    def forbidden(*args, **kwargs):
        extracted.append('forbidden')
        raise AssertionError('Authorization failure must precede cookie extraction')
    monkeypatch.setattr(yt_dlp.cookies, 'extract_cookies_from_browser', forbidden)
    with pytest.raises(HubError, match='YOUTUBE_SESSION_NOT_AUTHORIZED'):
        replay.load_youtube_session(settings, binding)
    assert not extracted


@pytest.fixture
def private_session(runtime, monkeypatch, session_jar):
    if os.name != 'posix':
        pytest.skip('Real Linux UID and private profile mode checks')
    import yt_dlp.cookies
    settings, store = runtime
    profile = settings.root / 'data/browser-profiles/youtube'
    default = profile / 'Default'; default.mkdir(parents=True)
    profile.chmod(0o700)
    cookie_file = default / 'Cookies'
    cookie_file.write_bytes(b'owned-placeholder-not-a-browser-cookie-database')
    cookie_file.chmod(0o600)
    binding = {'authenticated_session': True, 'session_profile': 'youtube',
               'session_authorization_sha256': '0' * 64, 'policy_sha256': '0' * 64}
    f = SimpleNamespace(settings=settings, store=store, profile=profile, default=default,
        cookie_file=cookie_file, binding=binding, jar=session_jar, calls=[], readonly_checks=[])
    def readonly(path):
        assert path == profile
        f.readonly_checks.append(path)
        return True
    monkeypatch.setattr(replay, '_session_profile_readonly', readonly)
    def owned_extract(browser, *, profile, logger, keyring):
        f.calls.append('owned-memory-cookie-extractor')
        assert browser == 'chromium' and profile == str(default) and keyring is None
        assert isinstance(logger, replay.SilentCookieLog)
        return f.jar
    monkeypatch.setattr(yt_dlp.cookies, 'extract_cookies_from_browser', owned_extract)
    return f


@LINUX_ONLY
def test_private_closed_readonly_profile_loads_only_filtered_memory_cookie_jar(private_session):
    f = private_session
    jar = replay.load_youtube_session(f.settings, f.binding)
    assert {c.domain for c in jar} <= {'.youtube.com', 'youtube.com'}
    assert {'LOGIN_INFO', 'SAPISID'} <= {c.name for c in jar}
    assert jar.filename is None and len(f.calls) == 1 and len(f.readonly_checks) == 1
    assert f.cookie_file.read_bytes() == b'owned-placeholder-not-a-browser-cookie-database'
    assert not list(f.profile.rglob('*.txt'))


@LINUX_ONLY
def test_source_profile_requires_an_exact_readonly_mount(private_session, monkeypatch):
    f = private_session
    monkeypatch.setattr(replay, '_session_profile_readonly', lambda _: False)
    with pytest.raises(HubError, match='YOUTUBE_SESSION_READONLY_MOUNT_REQUIRED'):
        replay.load_youtube_session(f.settings, f.binding)
    assert not f.calls


@LINUX_ONLY
def test_source_profile_cannot_be_group_or_world_accessible(private_session):
    f = private_session; f.profile.chmod(0o755)
    with pytest.raises(HubError, match='YOUTUBE_SESSION_PROFILE_NOT_PRIVATE'):
        replay.load_youtube_session(f.settings, f.binding)
    assert not f.calls and not f.readonly_checks


@LINUX_ONLY
@pytest.mark.parametrize('dangling', [False, True])
def test_live_or_dangling_singleton_lock_stops_before_extraction(private_session, dangling):
    f = private_session; lock = f.profile / 'SingletonLock'
    if dangling:
        lock.symlink_to(f.profile / 'missing-owned-lock-target')
    else:
        lock.write_bytes(b'owned-lock')
    with pytest.raises(HubError, match='YOUTUBE_SESSION_BROWSER_STILL_OPEN'):
        replay.load_youtube_session(f.settings, f.binding)
    assert not f.calls


@LINUX_ONLY
@pytest.mark.parametrize('missing', ['login', 'sapisid', 'empty', 'expired'])
def test_missing_or_expired_authentication_cookies_are_not_a_live_session(private_session, missing):
    f = private_session
    f.jar = owned_jar(*(cookie for cookie in (
        owned_cookie('LOGIN_INFO', expired=missing == 'expired'),
        owned_cookie('SAPISID', value='' if missing == 'empty' else 'owned-sid'),
    ) if cookie.name != {'login': 'LOGIN_INFO', 'sapisid': 'SAPISID'}.get(missing)))
    with pytest.raises(HubError, match='YOUTUBE_SESSION_AUTH_EXPIRED'):
        replay.load_youtube_session(f.settings, f.binding)
    assert len(f.calls) == 1


@LINUX_ONLY
def test_three_party_sapisid_is_a_supported_sdk_session_alternative(private_session):
    f = private_session
    f.jar = owned_jar(owned_cookie('LOGIN_INFO'), owned_cookie('__Secure-3PAPISID'))
    assert '__Secure-3PAPISID' in {cookie.name for cookie in replay.load_youtube_session(f.settings, f.binding)}


@LINUX_ONLY
def test_network_cookie_directory_cannot_escape_through_a_symlink(private_session):
    f = private_session; f.cookie_file.unlink()
    outside = f.settings.root / 'owned-unrelated-cookie-directory'; outside.mkdir()
    (outside / 'Cookies').write_bytes(b'owned-unrelated-placeholder')
    (f.default / 'Network').symlink_to(outside, target_is_directory=True)
    with pytest.raises(HubError, match='YOUTUBE_SESSION_UNAVAILABLE'):
        replay.load_youtube_session(f.settings, f.binding)
    assert not f.calls


def test_readonly_mount_check_requires_the_exact_profile_mount_not_an_ancestor(monkeypatch):
    profile = PurePosixPath('/owned-youtube-profile')
    original = Path.read_text
    content = ['1 1 0:1 / / ro - tmpfs tmpfs ro\n']
    def mountinfo(path, *args, **kwargs):
        if path.as_posix() == '/proc/self/mountinfo':
            return content[0]
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', mountinfo)
    assert replay._session_profile_readonly(profile) is False
    content[0] = '1 1 0:1 / /owned-youtube-profile rw - tmpfs tmpfs rw\n'
    assert replay._session_profile_readonly(profile) is False
    content[0] = '1 1 0:1 / /owned-youtube-profile ro,nosuid - tmpfs tmpfs ro\n'
    assert replay._session_profile_readonly(profile) is True
