"""Owned offline downloader tests; no YouTube request or account access.

The child boundary is replaced with a plainly identified local file copier.
FFmpeg merge, ffprobe, packet scans, hashes and SQLite are real. These tests
do not establish platform permission or successful remote media retrieval.
"""
from copy import deepcopy
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import socket
from types import SimpleNamespace
import uuid

import pytest

from adapters import ytdlp_replay as replay
from services.hub.errors import HubError


VIDEO = 'ownedReplay1'
CHANNEL = 'UC' + 'x' * 22
CDN = 'rr1---owned.googlevideo.com'


@pytest.fixture
def binding():
    return {'video_id': VIDEO, 'channel_id': CHANNEL, 'duration': 1,
            'max_bytes': 1_000_000, 'max_height': 720, 'timeout_seconds': 30}


def fmt(ident='v720', *, video=True, audio=False, height=720, size=1000, **change):
    value = {'format_id': ident, 'protocol': 'https', 'ext': 'mp4' if video else 'm4a',
             'vcodec': 'avc1.64001f' if video else 'none',
             'acodec': 'mp4a.40.2' if audio else 'none', 'height': height if video else None,
             'width': 1280 if video else None, 'filesize': size,
             'url': f'https://{CDN}/videoplayback?expire=owned&sig=private', 'tbr': 10}
    value.update(change)
    return value


def extracted(formats=None, **change):
    value = {'id': VIDEO, 'channel_id': CHANNEL, 'duration': 1, 'is_live': False,
             'live_status': 'was_live', 'availability': 'public', 'age_limit': 0,
             'formats': formats if formats is not None else [fmt(), fmt('a128', video=False, audio=True)]}
    value.update(change)
    return value


@pytest.mark.parametrize('url,track,host', [
    ('https://www.youtube.com/watch?v=' + VIDEO, False, 'www.youtube.com'),
    ('https://s.ytimg.com/player.js', False, 's.ytimg.com'),
    (f'https://{CDN}/videoplayback?expire=1', True, CDN),
])
def test_only_explicit_https_metadata_or_track_destinations(url, track, host):
    assert replay.destination(url, track=track) == host


@pytest.mark.parametrize('url,track', [
    ('http://www.youtube.com/watch?v=' + VIDEO, False),
    ('https://youtube.com.evil.test/watch?v=' + VIDEO, False),
    ('https://googlevideo.com/videoplayback', True),
    ('https://evilgooglevideo.com/videoplayback', True),
    ('https://www.youtube.com/videoplayback', True),
    (f'https://{CDN}/not-playback', True),
    (f'https://{CDN}:444/videoplayback', True),
    (f'https://user:password@{CDN}/videoplayback', True),
    (f'https://{CDN}/videoplayback#fragment', True),
    (f'https://{CDN}/video playback', True),
    ('https://127.0.0.1/videoplayback', True),
    ('file:///etc/passwd', False),
    (None, False),
])
def test_rejects_unapproved_origins_credentials_protocols_and_paths(url, track):
    with pytest.raises(HubError, match='YOUTUBE_DOWNLOAD_DESTINATION_REJECTED'):
        replay.destination(url, track=track)


@pytest.mark.parametrize('key', ['pot', 'po_token'])
def test_platform_tokens_cannot_be_supplied_as_ordinary_media_urls(key):
    with pytest.raises(HubError, match='YOUTUBE_DOWNLOAD_PLATFORM_TOKEN_REQUIRED'):
        replay.destination(f'https://{CDN}/videoplayback?{key}=owned', track=True)


def test_selects_complete_separate_tracks_within_the_configured_height(binding):
    formats = [fmt('progressive', audio=True, height=360), fmt(),
               fmt('too-tall', height=1080), fmt('a128', video=False, audio=True)]
    selected = replay.select_tracks(extracted(formats), binding)
    assert [f['format_id'] for f in selected] == ['v720', 'a128']
    assert all(f['height'] is None or f['height'] <= binding['max_height'] for f in selected)


def test_total_track_size_is_bounded_before_selection(binding):
    binding['max_bytes'] = 500
    formats = [fmt('progressive', audio=True, size=400), fmt(size=350),
               fmt('a128', video=False, audio=True, size=200)]
    selected = replay.select_tracks(extracted(formats), binding)
    assert [f['format_id'] for f in selected] == ['progressive']


@pytest.mark.parametrize('change,code', [
    ({'id': 'otherVideo1'}, 'YOUTUBE_DOWNLOAD_IDENTITY_CHANGED'),
    ({'channel_id': 'UC' + 'z' * 22}, 'YOUTUBE_DOWNLOAD_IDENTITY_CHANGED'),
    ({'_type': 'playlist'}, 'YOUTUBE_DOWNLOAD_IDENTITY_CHANGED'),
    ({'entries': []}, 'YOUTUBE_DOWNLOAD_IDENTITY_CHANGED'),
    ({'is_live': True}, 'YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED'),
    ({'live_status': 'post_live'}, 'YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED'),
    ({'availability': 'private'}, 'YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED'),
    ({'age_limit': 18}, 'YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED'),
    ({'has_drm': True}, 'YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED'),
    ({'duration': 60}, 'YOUTUBE_DOWNLOAD_DURATION_CHANGED'),
    ({'duration': float('nan')}, 'YOUTUBE_DOWNLOAD_DURATION_CHANGED'),
    ({'duration': True}, 'YOUTUBE_DOWNLOAD_DURATION_CHANGED'),
])
def test_identity_state_and_duration_are_independent_requirements(binding, change, code):
    with pytest.raises(HubError, match=code):
        replay.select_tracks(extracted(**change), binding)


@pytest.mark.parametrize('change', [
    {'protocol': 'm3u8_native'}, {'protocol': 'http_dash_segments'},
    {'fragments': [{'url': 'https://example.test/fragment'}]},
    {'manifest_url': 'https://example.test/manifest'}, {'has_drm': True},
    {'vcodec': 'vp9'}, {'ext': 'webm'}, {'format_id': '../escape'},
    {'height': 1080}, {'height': True}, {'filesize': -1},
])
def test_unsupported_or_unbounded_formats_are_not_downloadable(binding, change):
    with pytest.raises(HubError, match='YOUTUBE_DOWNLOAD_PUBLIC_FORMAT_UNAVAILABLE'):
        replay.select_tracks(extracted([fmt(audio=True, **change)]), binding)


@pytest.mark.parametrize('header', ['Cookie', 'authorization', 'Proxy-Authorization'])
def test_extracted_format_cannot_smuggle_authentication_headers(binding, header):
    with pytest.raises(HubError, match='YOUTUBE_DOWNLOAD_AUTHENTICATION_FORBIDDEN'):
        replay.select_tracks(extracted([fmt(audio=True, http_headers={header: 'owned-fixture'})]), binding)


def test_durable_track_identity_excludes_signed_urls_and_headers():
    item = fmt(audio=True, http_headers={'User-Agent': 'owned'})
    original = deepcopy(item)
    identity = replay.track_identity([item])
    assert identity[0]['format_id'] == 'v720'
    assert 'url' not in identity[0] and 'http_headers' not in identity[0]
    assert 'private' not in json.dumps(identity) and item == original


def test_private_child_environment_does_not_inherit_accounts_proxies_or_python_injection(monkeypatch, tmp_path):
    monkeypatch.setenv('PATH', os.environ.get('PATH', ''))
    for key in ('YOUTUBE_API_KEY', 'MODEL_API_KEY', 'BILIBILI_ACCESS_TOKEN', 'HTTPS_PROXY',
                'HTTP_PROXY', 'ALL_PROXY', 'PYTHONPATH', 'PYTHONSTARTUP', 'NODE_OPTIONS'):
        monkeypatch.setenv(key, 'owned-secret-that-must-not-leak')
    result = replay.private_environment(tmp_path)
    assert result['HUB_ROOT'] == str(tmp_path)
    assert result['PYTHONNOUSERSITE'] == '1' and result['YTDLP_NO_PLUGINS'] == '1'
    assert result['HOME'] == '/tmp/lighthouse-ytdlp'
    assert 'owned-secret-that-must-not-leak' not in json.dumps(result)


@pytest.mark.parametrize('ident', ['../other', 'not-a-uuid', None, 'AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA'])
def test_workspace_requires_canonical_job_uuid(runtime, ident):
    settings, _ = runtime
    with pytest.raises(HubError, match='YOUTUBE_DOWNLOAD_WORKSPACE_INVALID'):
        replay.checked_folder(settings, ident)


def test_workspace_is_scoped_to_one_uuid_and_total_regular_file_bytes(runtime):
    settings, _ = runtime
    ident = str(uuid.uuid4())
    folder = replay.checked_folder(settings, ident)
    assert folder == settings.root / 'media' / ident / 'youtube-download-v1'
    (folder / 'stream.part').write_bytes(b'a' * 100)
    (folder / 'ticket.json').write_bytes(b'{}')
    assert replay.bounded_files(folder, 102) == 102
    with pytest.raises(HubError, match='YOUTUBE_DOWNLOAD_SIZE_LIMIT'):
        replay.bounded_files(folder, 101)
    (folder / 'unowned-subdirectory').mkdir()
    with pytest.raises(HubError, match='YOUTUBE_DOWNLOAD_WORKSPACE_INVALID'):
        replay.bounded_files(folder, 1000)


def test_workspace_rejects_a_symlink_instead_of_following_it(runtime, tmp_path):
    settings, _ = runtime
    ident = str(uuid.uuid4()); target = tmp_path / 'outside'; target.mkdir()
    link = settings.root / 'media' / ident
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f'OS cannot create a directory symlink: {type(exc).__name__}')
    with pytest.raises(HubError, match='YOUTUBE_DOWNLOAD_WORKSPACE_INVALID'):
        replay.checked_folder(settings, ident)
    assert list(target.iterdir()) == []


@pytest.fixture
def owned_tracks(tmp_path):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('Real FFmpeg and ffprobe are required for local media validation')
    folder = tmp_path / 'owned-inputs'; folder.mkdir()
    video, audio = folder / 'owned-video.mp4', folder / 'owned-audio.m4a'
    subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', '-f', 'lavfi', '-i',
        'color=c=blue:s=32x32:r=10', '-t', '1', '-an', '-c:v', 'libx264',
        '-threads', '1', '-pix_fmt', 'yuv420p', str(video)], check=True, timeout=15)
    subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', '-f', 'lavfi', '-i',
        'sine=frequency=440:sample_rate=48000', '-t', '1', '-vn', '-c:a', 'aac',
        '-threads', '1', str(audio)], check=True, timeout=15)
    return video, audio


@pytest.fixture
def acquisition(runtime, binding, monkeypatch):
    settings, store = runtime
    job_id = store.enqueue('youtube', VIDEO, 'youtube_download', 'video', {}, 'youtube-download-v1')
    job = store.claim('video', 300, 4)
    assert job['job_id'] == job_id
    monkeypatch.setattr('services.hub.maintenance.shutil.disk_usage',
                        lambda _: SimpleNamespace(free=100_000_000_000))
    value = SimpleNamespace(settings=settings, store=store, job=job, binding=binding,
                            checks=[], helper_calls=[], local_commands=[], next_result=None)
    value.check = lambda: value.checks.append('checked')
    real_popen = subprocess.Popen

    class OwnedCompleted:
        def __init__(self, code): self.returncode = code
        def poll(self): return self.returncode
        def wait(self, timeout=None): return self.returncode

    def popen(command, *args, **kwargs):
        if command[:3] == [sys.executable, '-m', 'adapters.ytdlp_replay']:
            value.helper_calls.append(command)
            assert kwargs['stdin'] == subprocess.DEVNULL
            assert kwargs['stdout'] == kwargs['stderr'] == subprocess.DEVNULL
            assert not any(k in kwargs['env'] for k in ('MODEL_API_KEY', 'YOUTUBE_API_KEY', 'HTTPS_PROXY'))
            ticket = Path(command[-1]); request = json.loads(ticket.read_text('utf-8'))
            assert request['binding'] == value.binding and request['job_id'] == value.job['job_id']
            return OwnedCompleted(value.next_result(ticket.parent))
        assert Path(command[0]).stem in ('ffmpeg', 'ffprobe')
        assert not any(str(arg).startswith(('http:', 'https:', 'rtmp:')) for arg in command)
        if '-f' in command and command[command.index('-f') + 1] == 'lavfi':
            inputs = [command[i + 1] for i, arg in enumerate(command) if arg == '-i']
            assert len(inputs) == 1 and inputs[0].startswith(('color=', 'sine='))
            return real_popen(command, *args, **kwargs)
        assert command[command.index('-protocol_whitelist') + 1] == 'file,pipe'
        value.local_commands.append(command)
        return real_popen(command, *args, **kwargs)

    monkeypatch.setattr(replay.subprocess, 'Popen', popen)
    value.service = lambda: replay.ReplayDownloader(settings)
    return value


def owned_result(folder, sources, binding):
    tracks = []
    for index, source in enumerate(sources):
        video = index == 0; ident = 'owned-video' if video else 'owned-audio'
        file = folder / f"stream-{ident}.{'mp4' if video else 'm4a'}"
        shutil.copyfile(source, file)
        identity = replay.track_identity([fmt(ident, video=video, audio=not video,
                                            height=32, size=file.stat().st_size)])[0]
        tracks.append({**identity, 'bytes': file.stat().st_size,
                       'sha256': hashlib.sha256(file.read_bytes()).hexdigest()})
    (folder / 'download-result.json').write_text(json.dumps({'tracks': tracks, 'binding_sha256': replay.digest(binding),
        'http': {'requests': 0, 'response_bytes': 0}, 'transport': 'owned_local_file_copier'}), 'utf-8')
    return 0


def test_owned_separate_av_really_merges_scans_and_reuses_verified_file_without_new_child(acquisition, owned_tracks):
    f = acquisition
    f.next_result = lambda folder: owned_result(folder, owned_tracks, f.binding)
    receipt = f.service().acquire(f.job, f.binding, check=f.check)
    source = receipt['source_asset']; file = Path(source['file'])
    assert file.is_file() and source['sha256'] == hashlib.sha256(file.read_bytes()).hexdigest()
    assert source['bytes'] == file.stat().st_size and 0 < source['bytes'] <= f.binding['max_bytes']
    assert set(source['stream_types'].values()) == {'audio', 'video'}
    assert all(source['packet_evidence'][kind]['packets'] > 0 for kind in ('audio', 'video'))
    assert abs(source['duration'] - f.binding['duration']) <= 2
    assert receipt['human_review'] is False and receipt['publication_authorized'] is False
    assert receipt['creator_original_bytes_verified'] is False
    assert receipt['receipt']['expected_source_hash_verified'] is False
    assert receipt['receipt']['http'] == {'requests': 0, 'response_bytes': 0}
    assert receipt['receipt']['source_to_merged_packet_coverage_verified'] is True
    assert len(f.helper_calls) == 1 and f.local_commands and f.checks
    local_count = len(f.local_commands)
    assert f.service().acquire(f.job, f.binding, check=f.check) == json.loads(json.dumps(receipt))
    assert len(f.helper_calls) == 1 and len(f.local_commands) == local_count
    assert not f.store.query('SELECT * FROM publications')
    assert not f.store.query('SELECT * FROM publication_reviews')
    assert not f.store.query('SELECT * FROM api_calls')


def test_changed_completed_file_cannot_be_reused_or_redownloaded(acquisition, owned_tracks):
    f = acquisition; f.next_result = lambda folder: owned_result(folder, owned_tracks, f.binding)
    receipt = f.service().acquire(f.job, f.binding, check=f.check)
    Path(receipt['source_asset']['file']).write_bytes(b'owned-tampered-final')
    with pytest.raises(HubError, match='YOUTUBE_DOWNLOADED_FILE_CHANGED'):
        f.service().acquire(f.job, f.binding, check=f.check)
    assert len(f.helper_calls) == 1


def test_download_binding_cannot_change_when_a_partial_ticket_exists(acquisition):
    f = acquisition; folder = replay.checked_folder(f.settings, f.job['job_id'])
    (folder / 'ticket.json').write_text(json.dumps({'binding': f.binding}), 'utf-8')
    (folder / 'stream-owned-video.mp4.part').write_bytes(b'owned-partial')
    changed = {**f.binding, 'max_height': 360}
    with pytest.raises(HubError, match='YOUTUBE_DOWNLOAD_RESUME_BINDING_CHANGED'):
        f.service().acquire(f.job, changed, check=f.check)
    assert not f.helper_calls
    assert (folder / 'stream-owned-video.mp4.part').read_bytes() == b'owned-partial'


def test_helper_interruption_preserves_partials_and_never_counts_as_downloaded(acquisition):
    f = acquisition
    def interrupted(folder):
        (folder / 'stream-owned-video.mp4.part').write_bytes(b'owned-partial')
        return 1
    f.next_result = interrupted
    with pytest.raises(HubError, match='YOUTUBE_DOWNLOAD_HELPER_INTERRUPTED') as exc:
        f.service().acquire(f.job, f.binding, check=f.check)
    assert exc.value.retryable
    folder = replay.checked_folder(f.settings, f.job['job_id'])
    assert (folder / 'stream-owned-video.mp4.part').read_bytes() == b'owned-partial'
    assert not (folder / 'verified.json').exists() and not (folder / 'original.mp4').exists()
    assert not f.local_commands


def test_explicit_access_error_is_preserved_without_local_merge_or_retry_flag(acquisition):
    f = acquisition
    def restricted(folder):
        (folder / 'download-result.json').write_text(json.dumps({
            'error_code': 'YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED', 'retryable': False}), 'utf-8')
        return 1
    f.next_result = restricted
    with pytest.raises(HubError, match='YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED') as exc:
        f.service().acquire(f.job, f.binding, check=f.check)
    assert exc.value.retryable is False and not f.local_commands


def test_low_disk_stops_before_starting_download_helper(acquisition, monkeypatch):
    f = acquisition
    monkeypatch.setattr('services.hub.maintenance.shutil.disk_usage',
                        lambda _: SimpleNamespace(free=15_000_000_001))
    with pytest.raises(HubError, match='DISK_LOW'):
        f.service().acquire(f.job, f.binding, check=f.check)
    assert not f.helper_calls and not f.local_commands
    assert f.store.query("SELECT name FROM events WHERE name='disk.low'")


def test_child_track_bytes_must_match_the_file_before_merge(acquisition):
    f = acquisition
    def wrong_hash(folder):
        file = folder / 'stream-owned.mp4'; file.write_bytes(b'owned-content')
        (folder / 'download-result.json').write_text(json.dumps({'tracks': [{
            'format_id': 'owned', 'ext': 'mp4', 'bytes': file.stat().st_size,
            'sha256': '0' * 64}], 'http': {'requests': 0}, 'binding_sha256': replay.digest(f.binding)}), 'utf-8')
        return 0
    f.next_result = wrong_hash
    with pytest.raises(HubError, match='YOUTUBE_DOWNLOAD_TRACK_CHANGED'):
        f.service().acquire(f.job, f.binding, check=f.check)
    assert not f.local_commands


def test_child_cannot_return_a_path_traversal_track_name(acquisition):
    f = acquisition
    def escaped(folder):
        (folder / 'download-result.json').write_text(json.dumps({'tracks': [{
            'format_id': '../../outside', 'ext': 'mp4', 'bytes': 1, 'sha256': '0' * 64}],
            'http': {'requests': 0}, 'binding_sha256': replay.digest(f.binding)}), 'utf-8')
        return 0
    f.next_result = escaped
    with pytest.raises(HubError, match='YOUTUBE_DOWNLOAD_RESULT_INVALID'):
        f.service().acquire(f.job, f.binding, check=f.check)
    assert not f.local_commands


@pytest.mark.parametrize('field,value', [
    ('version', 'invented-version'), ('human_review', True),
    ('publication_authorized', True), ('creator_original_bytes_verified', True),
    ('binding_sha256', '0' * 64),
])
def test_cache_cannot_claim_review_authority_or_a_different_binding(acquisition, field, value):
    f = acquisition; folder = replay.checked_folder(f.settings, f.job['job_id'])
    file = folder / 'original.mp4'; file.write_bytes(b'owned-cache-boundary-fixture')
    receipt = {'version': replay.VERSION, 'binding_sha256': replay.digest(f.binding),
        'source_asset': {'file': str(file), 'bytes': file.stat().st_size,
                         'sha256': hashlib.sha256(file.read_bytes()).hexdigest()},
        'human_review': False, 'publication_authorized': False,
        'creator_original_bytes_verified': False}
    receipt[field] = value
    (folder / 'verified.json').write_text(json.dumps(receipt), 'utf-8')
    with pytest.raises(HubError, match='YOUTUBE_DOWNLOADED_FILE_CHANGED'):
        f.service().acquire(f.job, f.binding, check=f.check)
    assert not f.helper_calls and not f.local_commands


def test_interrupted_local_merge_reuses_retained_tracks_without_another_download(acquisition, owned_tracks, monkeypatch):
    f = acquisition; f.next_result = lambda folder: owned_result(folder, owned_tracks, f.binding)
    actual_lines = replay.process_lines
    fail_once = [True]
    def stop_first_merge(command, folder, timeout, check):
        if command[0] == 'ffmpeg' and fail_once[0]:
            fail_once[0] = False
            (folder / 'merged.partial.mp4').write_bytes(b'owned-interrupted-derived-merge')
            raise HubError('OWNED_TEST_MERGE_INTERRUPTED', retryable=True)
        yield from actual_lines(command, folder, timeout, check)
    monkeypatch.setattr(replay, 'process_lines', stop_first_merge)
    with pytest.raises(HubError, match='OWNED_TEST_MERGE_INTERRUPTED'):
        f.service().acquire(f.job, f.binding, check=f.check)
    folder = replay.checked_folder(f.settings, f.job['job_id'])
    assert (folder / 'download-result.json').is_file()
    assert (folder / 'stream-owned-video.mp4').is_file()
    assert (folder / 'stream-owned-audio.m4a').is_file()
    assert not (folder / 'verified.json').exists()
    receipt = f.service().acquire(f.job, f.binding, check=f.check)
    assert receipt['receipt']['source_to_merged_packet_coverage_verified'] is True
    assert Path(receipt['source_asset']['file']).is_file()
    assert len(f.helper_calls) == 1


def test_completed_receipt_still_calls_current_authorization_check(acquisition, owned_tracks):
    f = acquisition; f.next_result = lambda folder: owned_result(folder, owned_tracks, f.binding)
    receipt = f.service().acquire(f.job, f.binding, check=f.check)
    def revoked(): raise HubError('OWNED_POLICY_REVOKED', review=True)
    with pytest.raises(HubError, match='OWNED_POLICY_REVOKED'):
        f.service().acquire(f.job, f.binding, check=revoked)
    assert len(f.helper_calls) == 1 and Path(receipt['source_asset']['file']).is_file()


@pytest.fixture
def child_transport(runtime, binding, monkeypatch):
    """Exercise the real child, Request/Response and installed YoutubeDL.

    Permission binding is an explicit owned no-op: separate service tests cover
    actual policy authorization. The network adapter alone is substituted here.
    No network transport or authorization success is claimed by this fixture.
    """
    import yt_dlp
    from services.hub.config import Settings
    from services.hub.youtube_download import YouTubeDownload
    settings, store = runtime
    job_id = store.enqueue('youtube', VIDEO, 'youtube_download', 'video', {}, 'youtube-download-v1')
    job = store.claim('video', 300, 4)
    assert job['job_id'] == job_id
    folder = replay.checked_folder(settings, job_id)
    ticket = folder / 'ticket.json'
    ticket.write_text(json.dumps({'root': str(settings.root), 'job_id': job_id,
        'lease_owner': job['lease_owner'], 'binding': binding}), 'utf-8')
    f = SimpleNamespace(settings=settings, store=store, job=job, binding=binding, folder=folder,
        ticket=ticket, permission_checks=[], external_socket_attempts=[], transport_calls=[])
    monkeypatch.setattr(Settings, 'load', lambda: settings)
    def owned_permission_boundary(service, current, current_binding):
        assert current['job_id'] == job_id and current_binding == binding
        f.permission_checks.append('owned_noop_not_a_platform_permission')
    monkeypatch.setattr(YouTubeDownload, 'check_binding', owned_permission_boundary)
    monkeypatch.setattr('services.hub.maintenance.shutil.disk_usage',
                        lambda _: SimpleNamespace(free=100_000_000_000))
    monkeypatch.setenv('YTDLP_NO_PLUGINS', '1')
    def forbidden_socket(*args, **kwargs):
        f.external_socket_attempts.append('forbidden')
        raise AssertionError('Owned transport test must never open an external socket')
    monkeypatch.setattr(socket, 'create_connection', forbidden_socket)
    f.ytdlp = yt_dlp
    f.result = lambda: json.loads((folder / 'download-result.json').read_text('utf-8'))
    return f


def install_owned_extract_request(f, monkeypatch, *, headers=None, read=False):
    from yt_dlp.networking.common import Request
    def extract_info(ydl, url, download=True, **kwargs):
        assert url == 'https://www.youtube.com/watch?v=' + VIDEO and download is False
        response = ydl.urlopen(Request(url, headers=headers))
        if read:
            with response:
                assert response.read() == b'owned local metadata response'
        return extracted(formats=[])
    monkeypatch.setattr(f.ytdlp.YoutubeDL, 'extract_info', extract_info)


@pytest.mark.parametrize('failure', [
    lambda: http.client.IncompleteRead(b'owned-partial', 100),
    lambda: http.client.BadStatusLine('owned-invalid-status'),
    lambda: ConnectionResetError('owned-reset'),
])
def test_real_child_classifies_http_protocol_interruption_as_retryable_network_failure(child_transport, monkeypatch, failure):
    f = child_transport
    install_owned_extract_request(f, monkeypatch)
    def fail_open(url, allowed_hosts, **kwargs):
        assert allowed_hosts == ['www.youtube.com']
        f.transport_calls.append('owned_failure')
        raise failure()
    monkeypatch.setattr(replay, 'open_request', fail_open)
    assert replay.child_download(f.ticket) == 1
    result = f.result()
    assert result['error_code'] == 'YOUTUBE_DOWNLOAD_NETWORK_ERROR' and result['retryable'] is True
    assert result['http']['requests'] == 1 and len(f.transport_calls) == 1
    assert f.permission_checks and not f.external_socket_attempts
    assert not f.store.query('SELECT * FROM publications')
    assert not (f.folder / 'original.mp4').exists()


class OwnedHTTPResponse(io.BytesIO):
    status = 200
    def __init__(self, *, failure=None):
        super().__init__(b'owned local metadata response')
        self.failure = failure
    def getheaders(self):
        return [('Content-Type', 'text/plain; charset=utf-8'), ('Content-Encoding', 'identity')]
    def getheader(self, key, default=None):
        return {k.lower(): v for k, v in self.getheaders()}.get(key.lower(), default)
    def read(self, size=-1):
        if self.failure:
            raise self.failure()
        return super().read(size)


def test_real_child_preserves_yt_dlp_default_headers_and_real_response_read_contract(child_transport, monkeypatch):
    f = child_transport
    install_owned_extract_request(f, monkeypatch, read=True)
    connection = SimpleNamespace(closed=False)
    connection.close = lambda: setattr(connection, 'closed', True)
    def owned_open(url, allowed_hosts, *, method, headers, body):
        normalized = {k.lower(): v for k, v in headers.items()}
        assert normalized.get('user-agent')
        assert normalized.get('accept') and normalized.get('accept-language')
        assert normalized['accept-encoding'] == 'identity'
        assert not any(k in normalized for k in ('cookie', 'authorization', 'proxy-authorization'))
        assert method == 'GET' and body is None and allowed_hosts == ['www.youtube.com']
        f.transport_calls.append('owned_local_response')
        return connection, OwnedHTTPResponse()
    monkeypatch.setattr(replay, 'open_request', owned_open)
    assert replay.child_download(f.ticket) == 1
    assert f.result()['error_code'] == 'YOUTUBE_DOWNLOAD_PUBLIC_FORMAT_UNAVAILABLE'
    assert f.result()['http']['requests'] == 1 and connection.closed
    assert not f.external_socket_attempts and len(f.transport_calls) == 1


def test_real_child_body_read_interruption_is_retryable_and_not_a_format_error(child_transport, monkeypatch):
    f = child_transport
    install_owned_extract_request(f, monkeypatch, read=True)
    connection = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(replay, 'open_request', lambda *args, **kwargs:
        (connection, OwnedHTTPResponse(failure=lambda: http.client.IncompleteRead(b'owned-partial', 100))))
    assert replay.child_download(f.ticket) == 1
    assert f.result()['error_code'] == 'YOUTUBE_DOWNLOAD_NETWORK_ERROR'
    assert f.result()['retryable'] is True and not f.external_socket_attempts


@pytest.mark.parametrize('header', ['Cookie', 'Authorization', 'Proxy-Authorization'])
def test_real_child_rejects_sensitive_headers_before_transport(child_transport, monkeypatch, header):
    f = child_transport
    install_owned_extract_request(f, monkeypatch, headers={header: 'owned-authentication-fixture'})
    def no_transport(*args, **kwargs):
        f.transport_calls.append('forbidden')
        raise AssertionError('Sensitive headers must be rejected before transport')
    monkeypatch.setattr(replay, 'open_request', no_transport)
    assert replay.child_download(f.ticket) == 1
    result = f.result()
    assert result['error_code'] == 'YOUTUBE_DOWNLOAD_AUTHENTICATION_FORBIDDEN'
    assert result['retryable'] is False and result['http']['requests'] == 0
    assert not f.transport_calls and not f.external_socket_attempts


@pytest.mark.parametrize('response', [
    {'playabilityStatus': {'status': 'LOGIN_REQUIRED'}},
    {'playabilityStatus': {'status': 'AGE_VERIFICATION_REQUIRED'}},
    {'playabilityStatus': {'status': 'UNPLAYABLE', 'reason': 'Not available in your country'}},
    {'playabilityStatus': {'status': 'UNPLAYABLE', 'reason': 'Please complete the CAPTCHA'}},
    {'playabilityStatus': {'status': 'UNPLAYABLE', 'reason': 'DRM protected content'}},
    {'playabilityStatus': {'status': 'OK'}, 'streamingData': {'licenseInfos': [{'drmFamily': 'owned-drm'}]}},
])
def test_player_access_protection_stops_before_client_fallback(response):
    with pytest.raises(HubError, match='YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED') as exc:
        replay.player_access_guard(response)
    assert exc.value.retryable is False


@pytest.mark.parametrize('response', [
    {'playabilityStatus': {'status': 'OK'}, 'streamingData': {}},
    {'playabilityStatus': {'status': 'OK'}, 'streamingData': None},
])
def test_normal_player_status_does_not_require_streaming_data_to_be_a_dictionary(response):
    assert replay.player_access_guard(response) is None


def test_real_child_registers_guarded_youtube_extractor_under_the_original_key_and_default_clients(child_transport, monkeypatch):
    from yt_dlp.extractor.youtube._video import YoutubeIE as OriginalYoutubeIE
    f = child_transport
    observed = []
    def extract_info(ydl, url, download=True, **kwargs):
        ie = ydl._ies['Youtube']
        assert isinstance(ie, OriginalYoutubeIE)
        assert type(ie) is not OriginalYoutubeIE and type(ie).__name__ == 'YoutubeIE'
        assert ie._DEFAULT_CLIENTS == OriginalYoutubeIE._DEFAULT_CLIENTS
        assert tuple(ydl.params['extractor_args']['youtube']['player_client']) == OriginalYoutubeIE._DEFAULT_CLIENTS
        with pytest.raises(HubError, match='YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED'):
            ie._is_agegated({'playabilityStatus': {'reason': 'Please confirm your age'}})
        observed.append('guarded_original_key_and_defaults_verified')
        return extracted(formats=[])
    monkeypatch.setattr(f.ytdlp.YoutubeDL, 'extract_info', extract_info)
    monkeypatch.setattr(replay, 'open_request', lambda *args, **kwargs:
        (_ for _ in ()).throw(AssertionError('Extractor registration test must not use transport')))
    assert replay.child_download(f.ticket) == 1
    assert observed == ['guarded_original_key_and_defaults_verified']
    assert f.result()['error_code'] == 'YOUTUBE_DOWNLOAD_PUBLIC_FORMAT_UNAVAILABLE'
    assert f.result()['http']['requests'] == 0 and not f.external_socket_attempts


def owned_cdn_probe(f, monkeypatch, *, over_limit):
    """Real child response adapter; simulated small CDN metadata, no media GET."""
    from yt_dlp.networking.common import Request
    f.binding['max_bytes'] = 20_000_000  # File bound exceeds the independent 8MiB metadata cap.
    ticket = json.loads(f.ticket.read_text('utf-8')); ticket['binding'] = f.binding
    f.ticket.write_text(json.dumps(ticket), 'utf-8')
    url = f'https://{CDN}/api/manifest/hls_variant/owned'
    expected = b'#EXTM3U\n# Owned metadata probe only; no segment retrieval\n'
    read_sizes = []
    connection = SimpleNamespace(closed=False)
    connection.close = lambda: setattr(connection, 'closed', True)
    class OwnedManifestResponse(OwnedHTTPResponse):
        def getheaders(self):
            return [('Content-Type', 'application/vnd.apple.mpegurl'), ('Content-Encoding', 'identity')]
        def read(self, size=-1):
            assert type(size) is int and 0 <= size <= 8_388_609
            read_sizes.append(size)
            # The simulated peer never allocates the hypothetical full media size.
            return b'x' * size if over_limit else expected
    def owned_open(current_url, allowed_hosts, **kwargs):
        assert current_url == url and allowed_hosts == [CDN]
        assert not {'cookie', 'authorization'} & {k.lower() for k in kwargs['headers']}
        f.transport_calls.append('owned-cdn-manifest-probe')
        return connection, OwnedManifestResponse()
    def extract_info(ydl, current_url, download=True, **kwargs):
        assert current_url == 'https://www.youtube.com/watch?v=' + VIDEO and download is False
        with ydl.urlopen(Request(url)) as response:
            assert response.read() == expected
        return extracted(formats=[])
    monkeypatch.setattr(replay, 'open_request', owned_open)
    monkeypatch.setattr(f.ytdlp.YoutubeDL, 'extract_info', extract_info)
    return read_sizes, connection


def test_real_child_allows_a_bounded_small_cdn_manifest_probe_read_without_treating_it_as_media(child_transport, monkeypatch):
    f = child_transport
    read_sizes, connection = owned_cdn_probe(f, monkeypatch, over_limit=False)
    assert replay.child_download(f.ticket) == 1
    result = f.result()
    assert result['error_code'] == 'YOUTUBE_DOWNLOAD_PUBLIC_FORMAT_UNAVAILABLE'
    assert result['http']['requests'] == 1 and result['http']['response_kinds'] == {'manifest': 1}
    assert result['http']['response_bytes'] > 0
    assert read_sizes == [8_388_609] and connection.closed
    assert len(f.transport_calls) == 1 and not f.external_socket_attempts
    assert CDN not in json.dumps(result) and not (f.folder / 'original.mp4').exists()


def test_real_child_rejects_a_cdn_no_argument_read_above_metadata_cap_with_a_bounded_allocation(child_transport, monkeypatch):
    f = child_transport
    read_sizes, connection = owned_cdn_probe(f, monkeypatch, over_limit=True)
    assert replay.child_download(f.ticket) == 1
    result = f.result()
    assert result['error_code'] == 'YOUTUBE_DOWNLOAD_METADATA_RESPONSE_LIMIT'
    assert result['retryable'] is False
    assert result['http']['requests'] == 1 and result['http']['response_kinds'] == {'manifest': 1}
    assert read_sizes == [8_388_609] and connection.closed
    assert len(f.transport_calls) == 1 and not f.external_socket_attempts
    assert not (f.folder / 'original.mp4').exists()
