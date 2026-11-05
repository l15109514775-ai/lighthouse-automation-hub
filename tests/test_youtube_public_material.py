"""Owned offline fixtures: no YouTube request, media download or live claim.

The monitor is fed hand-written API-shaped data.  Transfer, probe and packet
interfaces are injected with owned bytes; these cases validate orchestration
and refusal boundaries, not real ffprobe decoding or a platform permission.
"""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from adapters.youtube_public_material import YouTubePublicMaterial, validate_url, open_media_request
from services.hub.db import Store, encode
from services.hub.errors import HubError
from services.hub.source_receipts import digest
from services.hub.worker import Worker
from services.hub.youtube_monitor import YouTubeMonitor


VIDEO = 'abcdefghijk'
CHANNEL = 'UC' + 'a' * 22
CDN = 'rr1---sn-owned.googlevideo.com'
OWNED = b'Owned material fixture; not an actual audiovisual file.\n' * 24


def write_grant(f, change=None, acquisition_change=None):
    grant = {'video_id': VIDEO, 'actions': ['download'], 'evidence': '114514',
             'authorized_by': 'Owned test operator; not a real platform grant', 'max_bytes': 16384}
    acquisition = {'version': 'youtube-public-material-grant-v1', 'channel_id': CHANNEL,
                   'metadata_sha256': digest(f.raw),
                   'platform_download_permission_reference': 'owned-fixture://platform-permission/not-live'}
    if change:
        grant.update(change)
    if acquisition_change:
        acquisition.update(acquisition_change)
    lines = ['[[grants]]']
    lines.extend(key + ' = ' + json.dumps(value, ensure_ascii=False) for key, value in grant.items())
    lines += ['', '[grants.youtube_acquisition]']
    lines.extend(key + ' = ' + json.dumps(value, ensure_ascii=False) for key, value in acquisition.items())
    folder = f.settings.root / 'config'
    folder.mkdir(exist_ok=True)
    (folder / 'rights.toml').write_text('\n'.join(lines) + '\n', 'utf-8')


@pytest.fixture
def material(runtime, monkeypatch):
    settings, store = runtime
    settings.config.update(youtube_public_material_enabled=True)
    settings.config['sources'] = [{'youtube_channel_id': CHANNEL, 'youtube_handle': '@owned_fixture'}]
    raw = {'id': VIDEO, 'snippet': {'channelId': CHANNEL, 'title': 'Owned ended replay fixture',
                                  'publishedAt': '2026-10-30T00:00:00Z', 'liveBroadcastContent': 'none'},
           'status': {'privacyStatus': 'public'}, 'contentDetails': {'duration': 'PT1M'},
           'liveStreamingDetails': {'actualStartTime': '2026-10-30T00:00:00Z',
                                    'actualEndTime': '2026-10-30T00:01:00Z'}}
    assert YouTubeMonitor(settings, store, None).observe_video(raw, CHANNEL) == 'ended'
    job = store.claim('video', 600, settings.config['max_attempts'])
    assert job and job['state'] == 'processing'
    f = SimpleNamespace(settings=settings, store=store, raw=raw, job=job,
                        extracts=[], transfers=[], inspections=[], proofs=[], downloaded=None)
    f.extracted = {'id': VIDEO, 'channel_id': CHANNEL, 'duration': 60, 'is_live': False,
                   'availability': 'public', 'formats': [{'format_id': 'owned-progressive', 'protocol': 'https',
                       'url': 'https://' + CDN + '/videoplayback?owned=fixture', 'ext': 'mp4',
                       'vcodec': 'avc1.64001f', 'acodec': 'mp4a.40.2', 'filesize': len(OWNED)}]}
    f.free = 100_000_000_000
    monkeypatch.setattr('services.hub.maintenance.shutil.disk_usage', lambda p: SimpleNamespace(free=f.free))

    def extractor(current, binding, attempt_dir):
        f.extracts.append((current['job_id'], deepcopy(binding), str(attempt_dir)))
        return deepcopy(f.extracted)

    def fetch(url, destination, *, allowed_hosts, max_bytes, check_disk, expected_sha256=None):
        f.transfers.append((url, str(destination), tuple(allowed_hosts), max_bytes))
        assert url == f.extracted['formats'][0]['url'] and CDN in allowed_hosts
        check_disk(max_bytes)
        assert len(OWNED) <= max_bytes
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(OWNED)
        f.downloaded = path
        actual = hashlib.sha256(OWNED).hexdigest()
        if expected_sha256 and expected_sha256 != actual:
            raise HubError('ASSET_HASH_MISMATCH', review=True)
        check_disk(0)
        return {'file': str(path), 'bytes': len(OWNED), 'sha256': actual}

    def inspector(file, *, check=lambda: None):
        check()
        f.inspections.append(str(file))
        return {'duration': 60, 'bytes': Path(file).stat().st_size, 'stream_types': {0: 'video', 1: 'audio'},
                'stream_config': {'video': {'codec_name': 'h264', 'width': 1280, 'height': 720},
                                  'audio': {'codec_name': 'aac', 'sample_rate': '48000', 'channels': 2}},
                'format': 'mov,mp4,m4a,3gp,3g2,mj2'}

    def packets(file, streams, *, timeout=7200, check=lambda: None):
        check()
        f.proofs.append(str(file))
        assert {int(k): v for k, v in streams.items()} == {0: 'video', 1: 'audio'}
        return {kind: {'packets': 1, 'chain': hashlib.sha256((kind + ':owned-packet').encode()).hexdigest()}
                for kind in ('video', 'audio')}

    f.extractor, f.fetch, f.inspector, f.packets = extractor, fetch, inspector, packets
    f.service = lambda store=None, **kw: YouTubePublicMaterial(settings, store or f.store,
        extractor=kw.get('extractor', f.extractor), fetch=kw.get('fetch', f.fetch),
        inspector=kw.get('inspector', f.inspector), packets=kw.get('packets', f.packets))
    write_grant(f)
    return f


def unpublished(f):
    row = f.store.get(f.job['job_id'])
    assert 'source_asset' not in row['result'] and 'processed' not in row['result']
    assert not f.store.query('SELECT * FROM publications')
    assert not f.store.query('SELECT * FROM publication_reviews')
    # Injected transfers still receive honest prototype request-accounting rows;
    # they must never consume model/platform API calls or claim a real fetch.
    assert not f.store.query("SELECT * FROM api_calls WHERE provider<>'youtube_public_material'")
    return row


@pytest.mark.parametrize('switch', ['missing', False])
def test_prototype_defaults_disabled_before_any_extraction_or_transfer(material, switch):
    f = material
    if switch == 'missing':
        f.settings.config.pop('youtube_public_material_enabled')
    else:
        f.settings.config['youtube_public_material_enabled'] = switch
    with pytest.raises(HubError):
        f.service().acquire(f.job)
    assert not f.extracts and not f.transfers
    unpublished(f)


@pytest.mark.parametrize('change', ['missing_file', 'no_download', 'missing_reference', 'wrong_version',
                                   'wrong_grant_video', 'wrong_grant_channel', 'wrong_metadata_hash',
                                   'missing_evidence', 'missing_operator', 'zero_bound', 'above_8gb', 'above_config'])
def test_content_permission_does_not_substitute_for_bound_platform_acquisition_grant(material, change):
    f = material
    if change == 'missing_file':
        (f.settings.root / 'config/rights.toml').unlink()
    elif change == 'no_download': write_grant(f, {'actions': ['process']})
    elif change == 'missing_reference': write_grant(f, acquisition_change={'platform_download_permission_reference': ''})
    elif change == 'wrong_version': write_grant(f, acquisition_change={'version': 'invented-permission'})
    elif change == 'wrong_grant_video': write_grant(f, {'video_id': 'wrongvideo1'})
    elif change == 'wrong_grant_channel': write_grant(f, acquisition_change={'channel_id': 'UC' + 'b' * 22})
    elif change == 'wrong_metadata_hash': write_grant(f, acquisition_change={'metadata_sha256': '0' * 64})
    elif change == 'missing_evidence': write_grant(f, {'evidence': ''})
    elif change == 'missing_operator': write_grant(f, {'authorized_by': ''})
    elif change == 'zero_bound': write_grant(f, {'max_bytes': 0})
    elif change == 'above_8gb': write_grant(f, {'max_bytes': 8_000_000_001})
    else:
        f.settings.config['max_media_bytes'] = 8192
        write_grant(f, {'max_bytes': 16384})
    with pytest.raises(HubError):
        f.service().acquire(f.job)
    assert not f.extracts and not f.transfers
    unpublished(f)


@pytest.mark.parametrize('change', ['video', 'channel', 'metadata', 'state', 'source_api', 'replay_job', 'not_configured'])
def test_only_the_same_official_monitor_ended_replay_can_acquire(material, change):
    f = material
    payload = deepcopy(f.job['payload'])
    if change in ('video', 'channel'):
        payload['video_id' if change == 'video' else 'channel_id'] = 'wrongvideo1' if change == 'video' else 'UC' + 'b' * 22
        with f.store.transaction() as db:
            db.execute('UPDATE jobs SET payload=? WHERE job_id=?', (encode(payload), f.job['job_id']))
        f.job = f.store.get(f.job['job_id'])
    elif change == 'metadata':
        changed = deepcopy(f.raw); changed['snippet']['title'] = 'Unbound replacement metadata'
        with f.store.transaction() as db: db.execute('UPDATE youtube_videos SET metadata=?', (encode(changed),))
    elif change == 'state':
        with f.store.transaction() as db: db.execute("UPDATE youtube_videos SET state='live'")
    elif change == 'source_api':
        with f.store.transaction() as db: db.execute("UPDATE youtube_videos SET source_api='youtube-atom-public'")
    elif change == 'replay_job':
        other = f.store.enqueue('synthetic', 'other', 'video_process', 'video', {}, 'owned')
        with f.store.transaction() as db: db.execute('UPDATE youtube_videos SET replay_job_id=?', (other,))
    else:
        f.settings.config['sources'] = [{'youtube_channel_id': 'UC' + 'b' * 22}]
    with pytest.raises(HubError):
        f.service().acquire(f.job)
    assert not f.extracts and not f.transfers
    unpublished(f)


@pytest.mark.parametrize('change', ['wrong_id', 'wrong_channel', 'live', 'private', 'playlist', 'drm',
                                   'http', 'foreign_host', 'private_ip', 'userinfo', 'foreign_suffix',
                                   'dash', 'audio_only', 'video_only', 'unknown_size', 'too_big'])
def test_extractor_metadata_cannot_expand_identity_access_or_supported_format(material, change):
    f = material; value = f.extracted; fmt = value['formats'][0]
    if change == 'wrong_id': value['id'] = 'wrongvideo1'
    elif change == 'wrong_channel': value['channel_id'] = 'UC' + 'b' * 22
    elif change == 'live': value['is_live'] = True
    elif change == 'private': value['availability'] = 'private'
    elif change == 'playlist': value.update(_type='playlist', entries=[deepcopy(value)])
    elif change == 'drm': fmt['has_drm'] = True
    elif change == 'http': fmt['url'] = 'http://' + CDN + '/videoplayback'
    elif change == 'foreign_host': fmt['url'] = 'https://untrusted.example.invalid/videoplayback'
    elif change == 'private_ip': fmt['url'] = 'https://127.0.0.1/videoplayback'
    elif change == 'userinfo': fmt['url'] = 'https://user:secret@' + CDN + '/videoplayback'
    elif change == 'foreign_suffix': fmt['url'] = 'https://googlevideo.com.evil.invalid/videoplayback'
    elif change == 'dash': fmt['protocol'] = 'http_dash_segments'
    elif change == 'audio_only': fmt['vcodec'] = 'none'
    elif change == 'video_only': fmt['acodec'] = 'none'
    elif change == 'unknown_size': fmt.pop('filesize')
    else: fmt['filesize'] = 16385
    with pytest.raises(HubError):
        f.service().acquire(f.job)
    assert len(f.extracts) == 1 and not f.transfers
    unpublished(f)


@pytest.mark.parametrize('change', ['stale_owner', 'expired', 'not_processing'])
def test_lease_is_checked_before_extraction_even_with_valid_permission(material, change):
    f = material
    if change == 'stale_owner': f.job['lease_owner'] = 'unowned-caller-lease'
    else:
        with f.store.transaction() as db:
            if change == 'expired': db.execute('UPDATE jobs SET lease_until=? WHERE job_id=?', (time.time() - 1, f.job['job_id']))
            else: db.execute("UPDATE jobs SET state='pending' WHERE job_id=?", (f.job['job_id'],))
    with pytest.raises((HubError, RuntimeError)):
        f.service().acquire(f.job)
    assert not f.extracts and not f.transfers
    unpublished(f)


def test_disk_floor_prevents_new_download_and_records_low_space(material):
    f = material; f.free = 14_999_999_999
    with pytest.raises(HubError, match='DISK_LOW'):
        f.service().acquire(f.job)
    assert not f.transfers and not f.inspections
    assert f.store.query("SELECT 1 FROM events WHERE name='disk.low'")
    unpublished(f)


def test_optional_known_hash_is_checked_before_material_receipt(material):
    f = material
    write_grant(f, {'source_sha256': '0' * 64})
    with pytest.raises(HubError, match='HASH|INTEGRITY'):
        f.service().acquire(f.job)
    assert not f.inspections
    unpublished(f)


def test_acquired_material_is_not_a_processed_or_publishable_video(material):
    f = material
    result = f.service().acquire(f.job)
    row = unpublished(f)
    assert row['state'] == 'processing'
    assert len(f.extracts) == len(f.transfers) == len(f.inspections) == len(f.proofs) == 1
    entry = row['result']['youtube_public_material_v1']
    assert entry and isinstance(result, dict)
    assert entry['human_review'] is False and entry['publication_authorized'] is False


def test_network_outage_has_finite_worker_attempts_and_never_marks_material_complete(material):
    f = material
    calls = []
    def offline(*args, **kwargs):
        calls.append('owned network fault')
        raise HubError('ASSET_NETWORK_ERROR', retryable=True)
    with f.store.transaction() as db:
        db.execute("UPDATE jobs SET state='pending',attempts=0,lease_owner=NULL,lease_until=NULL WHERE job_id=?", (f.job['job_id'],))
    service = f.service(fetch=offline)
    worker = Worker(f.settings, f.store, {'video_process': service.acquire})
    for _ in range(f.settings.config['max_attempts']):
        with f.store.transaction() as db: db.execute('UPDATE jobs SET next_run=0 WHERE job_id=?', (f.job['job_id'],))
        assert worker.once('video')
        assert f.store.get(f.job['job_id'])['state'] != 'completed'
    row = unpublished(f)
    assert row['state'] == 'failed' and row['attempts'] == f.settings.config['max_attempts']
    assert len(calls) == f.settings.config['max_attempts']
    assert row['result']['youtube_public_material_v1']['state'] == 'failed'
    assert not worker.once('video')


def test_attempts_above_the_fixed_four_attempt_bound_cannot_extract(material):
    f = material
    with f.store.transaction() as db: db.execute('UPDATE jobs SET attempts=5 WHERE job_id=?', (f.job['job_id'],))
    with pytest.raises(HubError, match='RETRY_EXHAUSTED'):
        f.service().acquire(f.store.get(f.job['job_id']))
    assert not f.extracts and not f.transfers
    unpublished(f)


def test_restart_reuses_verified_material_without_a_second_extraction_or_transfer(material):
    f = material
    f.service().acquire(f.job)
    original = deepcopy(f.store.get(f.job['job_id'])['result']['youtube_public_material_v1'])
    with f.store.transaction() as db: db.execute('UPDATE jobs SET lease_until=? WHERE job_id=?', (time.time() - 1, f.job['job_id']))
    reopened = Store(f.settings.db_path)
    reopened.recover('video', f.settings.config['max_attempts'])
    claimed = reopened.claim('video', 600, f.settings.config['max_attempts'])
    assert claimed and claimed['lease_owner'] != f.job['lease_owner']
    def never(*args, **kwargs): pytest.fail('Restart must not reacquire an already verified material')
    f.service(store=reopened, extractor=never, fetch=never).acquire(claimed)
    assert reopened.get(f.job['job_id'])['result']['youtube_public_material_v1'] == original
    assert len(f.extracts) == len(f.transfers) == 1
    unpublished(f)


@pytest.mark.parametrize('change', ['bytes', 'grant', 'metadata'])
def test_cached_material_cannot_survive_hash_rights_or_metadata_change(material, change):
    f = material
    f.service().acquire(f.job)
    if change == 'bytes': f.downloaded.write_bytes(b'Changed owned bytes')
    elif change == 'grant': write_grant(f, acquisition_change={'platform_download_permission_reference': 'owned-fixture://different-grant'})
    else:
        raw = deepcopy(f.raw); raw['snippet']['title'] = 'Changed metadata'
        with f.store.transaction() as db: db.execute('UPDATE youtube_videos SET metadata=?', (encode(raw),))
    before = (len(f.extracts), len(f.transfers))
    with pytest.raises(HubError):
        f.service().acquire(f.store.get(f.job['job_id']))
    assert (len(f.extracts), len(f.transfers)) == before
    unpublished(f)


@pytest.mark.parametrize('change', ['short_duration', 'nonfinite_duration', 'wrong_bytes', 'audio_only'])
def test_probe_must_confirm_complete_duration_bytes_and_both_streams(material, change):
    f = material
    def bad(file, *, check=lambda: None):
        details = f.inspector(file, check=check)
        if change == 'short_duration': details['duration'] = 15
        elif change == 'nonfinite_duration': details['duration'] = float('nan')
        elif change == 'wrong_bytes': details['bytes'] += 1
        else: details['stream_types'] = {0: 'audio'}
        return details
    with pytest.raises(HubError, match='MEDIA_INCOMPLETE'):
        f.service(inspector=bad).acquire(f.job)
    assert not f.proofs
    assert unpublished(f)['result']['youtube_public_material_v1']['state'] == 'failed'


@pytest.mark.parametrize('change', ['missing_audio', 'no_audio_packets', 'noninteger_packets', 'invalid_chain'])
def test_packet_evidence_cannot_claim_completeness_without_both_stream_chains(material, change):
    f = material
    def bad(file, streams, *, timeout=7200, check=lambda: None):
        proof = f.packets(file, streams, timeout=timeout, check=check)
        if change == 'missing_audio': proof.pop('audio')
        elif change == 'no_audio_packets': proof['audio']['packets'] = 0
        elif change == 'noninteger_packets': proof['audio']['packets'] = True
        else: proof['audio']['chain'] = 'not-a-sha256'
        return proof
    with pytest.raises(HubError, match='MEDIA_INCOMPLETE'):
        f.service(packets=bad).acquire(f.job)
    assert unpublished(f)['result']['youtube_public_material_v1']['state'] == 'failed'


@pytest.mark.parametrize('change', ['lease', 'disk'])
def test_running_transfer_cannot_commit_after_lease_loss_or_disk_floor_crossing(material, change):
    f = material
    def interrupted(url, destination, **kwargs):
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.with_suffix('.partial').write_bytes(b'Owned partial bytes retained on failure')
        if change == 'lease':
            with f.store.transaction() as db:
                db.execute('UPDATE jobs SET lease_until=? WHERE job_id=?', (time.time() - 1, f.job['job_id']))
        else:
            f.free = 14_999_999_999
        kwargs['check_disk'](0)
        pytest.fail('Guard should stop this partial transfer before a material can be returned')
    with pytest.raises(HubError):
        f.service(fetch=interrupted).acquire(f.job)
    assert not f.inspections and not f.proofs
    row = unpublished(f)
    assert row['result']['youtube_public_material_v1']['state'] != 'verified'
    assert list((f.settings.root / 'media' / f.job['job_id']).rglob('*.partial'))


def test_cache_receipt_cannot_be_changed_independently_of_database_checkpoint(material):
    f = material
    receipt = f.service().acquire(f.job)
    file = Path(receipt['attempt_dir']) / 'acquisition-receipt.json'
    value = json.loads(file.read_text('utf-8'))
    value['binding']['metadata_sha256'] = '0' * 64
    file.write_text(encode(value), 'utf-8')
    with pytest.raises(HubError, match='ACQUIRED_FILE_CHANGED'):
        f.service().acquire(f.store.get(f.job['job_id']))
    assert len(f.extracts) == len(f.transfers) == 1
    unpublished(f)


@pytest.mark.parametrize('url', [
    'http://' + CDN + '/videoplayback',
    'file:///etc/passwd', 'https://127.0.0.1/videoplayback',
    'https://[::1]/videoplayback', 'https://169.254.169.254/videoplayback',
    'https://localhost/videoplayback', 'https://googlevideo.com/videoplayback',
    'https://googlevideo.com.attacker.invalid/videoplayback',
    'https://user:password@' + CDN + '/videoplayback',
    'https://' + CDN + ':8443/videoplayback',
    'https://' + CDN + '/videoplayback#fragment',
    'https://' + CDN + '/different-endpoint',
    'https://' + CDN + '/videoplayback?pot=unapproved',
    'https://' + CDN + '/videoplayback?%70ot=unapproved',
    'https://' + CDN + '/videoplayback?po_token=unapproved',
    'https://' + CDN + '/videoplayback?token=unapproved',
    'https://' + CDN + '/video playback',
])
def test_media_destination_refuses_local_foreign_non_https_or_auth_token_urls(url):
    with pytest.raises(HubError):
        validate_url(url, media=True)


def test_only_validated_first_party_https_destinations_are_accepted_without_network():
    assert validate_url('https://www.youtube.com/watch?v=' + VIDEO) == 'www.youtube.com'
    assert validate_url('https://' + CDN + '/videoplayback?owned=fixture', media=True) == CDN


def test_actual_missing_optional_extractor_dependency_returns_real_error_without_network(material):
    f = material
    if importlib.util.find_spec('yt_dlp') is not None:
        pytest.skip('Missing-dependency probe only runs when the optional dependency is genuinely absent')
    service = YouTubePublicMaterial(f.settings, f.store, fetch=f.fetch, inspector=f.inspector, packets=f.packets)
    with pytest.raises(HubError, match='YOUTUBE_EXTRACTOR_RUNTIME_UNAVAILABLE'):
        service.acquire(f.job)
    assert not f.transfers and not f.inspections
    assert not f.store.query('SELECT * FROM api_calls')
    assert unpublished(f)['result']['youtube_public_material_v1']['state'] == 'failed'


def test_real_offline_synthetic_mp4_uses_actual_ffprobe_and_packet_chains(runtime, tmp_path, monkeypatch):
    """One second of owned color/sine; copied locally, never fetched from YouTube."""
    import shutil
    import subprocess
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        pytest.skip('Real offline media integration requires installed ffmpeg and ffprobe')
    source = tmp_path / 'owned-color-sine-one-second.mp4'
    proc = subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
        'color=c=blue:s=160x90:r=10:d=1', '-f', 'lavfi', '-i',
        'sine=frequency=440:sample_rate=48000:duration=1', '-map', '0:v:0', '-map', '1:a:0',
        '-c:v', 'libx264', '-threads', '1', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '32k', '-t', '1', '-shortest', '-movflags', '+faststart', str(source)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    assert proc.returncode == 0, proc.stderr.decode('utf-8', 'replace')
    settings, store = runtime
    settings.config['youtube_public_material_enabled'] = True
    settings.config['sources'] = [{'youtube_channel_id': CHANNEL, 'youtube_handle': '@owned_fixture'}]
    monkeypatch.setattr('services.hub.maintenance.shutil.disk_usage', lambda p: SimpleNamespace(free=100_000_000_000))
    raw = {'id': VIDEO, 'snippet': {'channelId': CHANNEL, 'title': 'Owned one-second synthetic media',
           'publishedAt': '2026-10-30T00:00:00Z', 'liveBroadcastContent': 'none'},
           'status': {'privacyStatus': 'public'}, 'contentDetails': {'duration': 'PT1S'},
           'liveStreamingDetails': {'actualStartTime': '2026-10-30T00:00:00Z',
                                    'actualEndTime': '2026-10-30T00:00:01Z'}}
    assert YouTubeMonitor(settings, store, None).observe_video(raw, CHANNEL) == 'ended'
    job = store.claim('video', 600, settings.config['max_attempts'])
    fixture = SimpleNamespace(settings=settings, store=store, raw=raw, job=job)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    write_grant(fixture, {'source_sha256': source_hash})
    calls = []
    def extractor(current, binding, folder):
        return {'id': VIDEO, 'channel_id': CHANNEL, 'duration': 1, 'is_live': False, 'availability': 'public',
            'formats': [{'format_id': 'owned-real-mp4', 'protocol': 'https', 'ext': 'mp4',
                         'url': 'https://' + CDN + '/videoplayback?owned=local-copy',
                         'vcodec': 'avc1', 'acodec': 'mp4a', 'filesize': source.stat().st_size}]}
    def copy_owned(url, destination, *, allowed_hosts, max_bytes, check_disk, expected_sha256=None):
        calls.append(url)
        assert allowed_hosts == [CDN] and expected_sha256 == source_hash
        assert source.stat().st_size <= max_bytes
        check_disk(max_bytes)
        shutil.copyfile(source, destination)
        return {'file': str(destination), 'bytes': source.stat().st_size, 'sha256': source_hash}
    # Inspector and packet collector deliberately use their REAL defaults.
    receipt = YouTubePublicMaterial(settings, store, extractor=extractor, fetch=copy_owned).acquire(job)
    assert receipt['state'] == 'verified' and receipt['expected_hash_verified'] is True
    assert receipt['material']['sha256'] == source_hash and receipt['material']['bytes'] == source.stat().st_size
    assert abs(receipt['material']['duration'] - 1) <= .05
    assert set(receipt['packet_evidence']) == {'audio', 'video'}
    for proof in receipt['packet_evidence'].values():
        assert type(proof['packets']) is int and proof['packets'] > 0
        assert len(proof['chain']) == 64 and proof['chain'] != '0' * 64
    assert receipt['transport_mode'] == 'custom_owned_test'
    assert receipt['human_review'] is False and receipt['publication_authorized'] is False
    assert len(calls) == 1 and store.get(job['job_id'])['state'] == 'processing'
    unpublished(fixture)


@pytest.mark.parametrize('status,code', [(401, 'YOUTUBE_MATERIAL_ACCESS_RESTRICTED'),
    (403, 'YOUTUBE_MATERIAL_ACCESS_RESTRICTED'), (429, 'YOUTUBE_MATERIAL_RATE_LIMITED'),
    (302, 'YOUTUBE_MATERIAL_REDIRECT_NOT_ALLOWED')])
def test_media_http_access_rate_and_redirect_restrictions_are_closed_and_not_retryable(monkeypatch, status, code):
    state = SimpleNamespace(closed=False)
    connection = SimpleNamespace(close=lambda: setattr(state, 'closed', True))
    response = SimpleNamespace(status=status)
    calls = []
    def owned_open(url, hosts):
        calls.append((url, hosts))
        return connection, response
    monkeypatch.setattr('adapters.youtube_public_material.open_request', owned_open)
    with pytest.raises(HubError) as raised:
        open_media_request('https://' + CDN + '/videoplayback?owned=fixture', [CDN])
    assert raised.value.code == code and raised.value.retryable is False
    assert state.closed is True and len(calls) == 1


def test_media_http_200_preserves_response_and_leaves_connection_for_bounded_reader(monkeypatch):
    state = SimpleNamespace(closed=False)
    connection = SimpleNamespace(close=lambda: setattr(state, 'closed', True))
    response = SimpleNamespace(status=200)
    monkeypatch.setattr('adapters.youtube_public_material.open_request', lambda url, hosts: (connection, response))
    actual = open_media_request('https://' + CDN + '/videoplayback?owned=fixture', [CDN])
    assert actual == (connection, response) and state.closed is False
    connection.close()
    assert state.closed is True


def test_generic_job_retry_reset_cannot_reset_durable_acquisition_attempt_limit(material):
    f = material
    transfers = []
    def offline(*args, **kwargs):
        transfers.append('owned outage')
        raise HubError('ASSET_NETWORK_ERROR', retryable=True)
    with f.store.transaction() as db:
        db.execute("UPDATE jobs SET state='pending',attempts=0,lease_owner=NULL,lease_until=NULL WHERE job_id=?", (f.job['job_id'],))
    service = f.service(fetch=offline)
    worker = Worker(f.settings, f.store, {'video_process': service.acquire})
    for _ in range(4):
        with f.store.transaction() as db: db.execute('UPDATE jobs SET next_run=0 WHERE job_id=?', (f.job['job_id'],))
        assert worker.once('video')
    assert f.store.get(f.job['job_id'])['state'] == 'failed'
    assert len(transfers) == len(f.extracts) == 4
    with f.store.transaction() as db:
        db.execute("UPDATE jobs SET state='pending',attempts=0,next_run=0,lease_owner=NULL,lease_until=NULL WHERE job_id=?", (f.job['job_id'],))
    retried = f.store.claim('video', 600, f.settings.config['max_attempts'])
    assert retried['attempts'] == 1
    with pytest.raises(HubError, match='YOUTUBE_MATERIAL_RETRY_EXHAUSTED'):
        service.acquire(retried)
    assert len(transfers) == len(f.extracts) == 4
    assert unpublished(f)['result']['youtube_public_material_v1']['state'] != 'verified'


def test_default_helper_drops_api_credentials_and_proxy_environment_before_subprocess(material, monkeypatch):
    f = material
    forbidden = ('MODEL_API_KEY', 'YOUTUBE_API_KEY', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                 'http_proxy', 'https_proxy', 'all_proxy')
    for key in forbidden:
        monkeypatch.setenv(key, 'owned-dummy-secret-or-proxy-not-real')
    seen = []
    def owned_helper(command, **kwargs):
        assert not set(forbidden).intersection(kwargs['env'])
        assert kwargs['env']['YTDLP_NO_PLUGINS'] == '1'
        assert kwargs['env']['PYTHONNOUSERSITE'] == '1'
        assert kwargs['timeout'] == 150
        packet = json.loads(Path(command[-1]).read_text('utf-8'))
        Path(packet['output']).write_text(encode({'error_code': 'YOUTUBE_EXTRACTOR_RUNTIME_UNAVAILABLE', 'retryable': False}), 'utf-8')
        seen.append(command[-2])
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr('adapters.youtube_public_material.subprocess.run', owned_helper)
    service = YouTubePublicMaterial(f.settings, f.store, fetch=f.fetch, inspector=f.inspector, packets=f.packets)
    with pytest.raises(HubError, match='YOUTUBE_EXTRACTOR_RUNTIME_UNAVAILABLE'):
        service.acquire(f.job)
    assert seen == ['--extract'] and not f.transfers
    assert not f.store.query('SELECT * FROM api_calls')
    unpublished(f)
