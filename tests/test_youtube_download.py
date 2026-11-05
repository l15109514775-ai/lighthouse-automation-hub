"""Download-only orchestration with owned bytes; no live YouTube requests."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from services.hub.db import Store, encode
from services.hub.errors import HubError
from services.hub.worker import Worker
from services.hub.youtube_download import AUTH_POLICY_ID, CHANNELS, POLICY_ID, STATE, YouTubeDownload
from services.hub.youtube_monitor import YouTubeMonitor

VIDEO = 'abcdefghijk'
CHANNEL = 'UCaaaaaaaaaaaaaaaaaaaaaa'
OWNED = b'Owned downloader fixture, not a real audiovisual file.\n' * 20


def write_policy(f, **changes):
    value = {**deepcopy(f.policy), **changes}
    folder = f.settings.root / 'config'
    folder.mkdir(exist_ok=True)
    file = folder / 'youtube-download-policy.toml'
    file.write_text('\n'.join(k + ' = ' + json.dumps(v, ensure_ascii=False) for k, v in value.items()) + '\n', 'utf-8')
    file.chmod(0o644)
    return file


@pytest.fixture
def replay(runtime):
    settings, store = runtime
    ended = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=1)
    started = ended - timedelta(minutes=1)
    raw = {'id': VIDEO, 'snippet': {'channelId': CHANNEL, 'title': 'Owned ended replay',
            'publishedAt': started.isoformat(), 'liveBroadcastContent': 'none'},
        'status': {'privacyStatus': 'public'}, 'contentDetails': {'duration': 'PT1M'},
        'liveStreamingDetails': {'actualStartTime': started.isoformat(), 'actualEndTime': ended.isoformat()}}
    monitor = YouTubeMonitor(settings, store, None)
    assert monitor.observe_video(raw, CHANNEL) == 'ended'
    old = store.query("SELECT job_id FROM jobs WHERE job_type='video_process'")[0]['job_id']
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='completed',result=? WHERE job_id=?",
                   (encode({'disposition': 'metadata_only', 'download_performed': False, 'publish_performed': False}), old))
    settings.config['youtube_download_enabled'] = True
    payload = {'video_id': VIDEO, 'channel_id': CHANNEL, 'title': raw['snippet']['title'],
        'published_at': raw['snippet']['publishedAt'], 'duration': 'PT1M',
        'url': 'https://www.youtube.com/watch?v=' + VIDEO, 'source_api': 'youtube-data-v3-official', 'raw': raw}
    f = SimpleNamespace(settings=settings, store=store, raw=raw, payload=payload, old=old,
                        calls=[], acquired=None, ended=ended)
    f.policy = {'version': 'youtube-download-policy-v1', 'policy_id': POLICY_ID,
        'activation_at': (ended - timedelta(seconds=1)).isoformat(), 'allowed_channel_ids': sorted(CHANNELS),
        'owner_permission_reference': 'Owned test permission fixture; not live rights evidence',
        'acquisition_authorization_reference': 'owned-fixture://operator-test',
        'acquisition_mode': 'owner_accepted_platform_terms_risk',
        'owner_risk_acceptance_reference': 'owned-fixture://explicit-risk-acceptance',
        'owner_risk_accepted_at': (ended - timedelta(seconds=2)).isoformat(),
        'max_bytes': 16384, 'max_height': 720, 'timeout_seconds': 60, 'max_new_replays_per_day': 4}
    write_policy(f)

    class OwnedDownloader:
        def __init__(self, current_settings):
            assert current_settings is settings

        def acquire(self, job, binding, *, check):
            f.calls.append(deepcopy(binding))
            check()
            file = settings.root / 'media' / job['job_id'] / 'owned-fixture.mp4'
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(OWNED)
            f.acquired = file
            return {'source_asset': {'file': str(file), 'bytes': len(OWNED),
                'sha256': hashlib.sha256(OWNED).hexdigest(), 'duration': 60,
                'stream_types': {0: 'video', 1: 'audio'},
                'packet_evidence': {kind: {'packets': 1, 'chain': hashlib.sha256(kind.encode()).hexdigest()}
                                    for kind in ('video', 'audio')}},
                'receipt': {'validation': 'owned_fixture_only', 'real_youtube_download': False}}

    f.factory = OwnedDownloader
    f.service = lambda store=None, factory=None: YouTubeDownload(settings, store or f.store,
                                                               downloader_factory=factory or OwnedDownloader)
    return f


def claim(f):
    ident = f.service().enqueue_if_eligible(f.payload)
    assert ident
    job = f.store.claim('video', 600, f.settings.config['max_attempts'])
    assert job['job_id'] == ident
    return job


def unchanged_old_and_no_publish(f):
    old = f.store.get(f.old)
    assert old['state'] == 'completed' and old['result']['disposition'] == 'metadata_only'
    assert f.store.query('SELECT replay_job_id FROM youtube_videos')[0]['replay_job_id'] == f.old
    assert not f.store.query('SELECT * FROM publications')
    assert not f.store.query('SELECT * FROM publication_reviews')
    assert not f.store.query('SELECT * FROM api_calls')


@pytest.mark.parametrize('switch', [None, False, 'true', 1])
def test_disabled_default_does_not_enqueue_or_create_downloader(replay, switch):
    f = replay
    if switch is None:
        f.settings.config.pop('youtube_download_enabled')
    else:
        f.settings.config['youtube_download_enabled'] = switch
    assert f.service().enqueue_if_eligible(f.payload) is None
    assert not f.calls
    unchanged_old_and_no_publish(f)


@pytest.mark.parametrize('change', [
    {'version': 'invented'}, {'policy_id': 'human-review-v1'}, {'allowed_channel_ids': [CHANNEL]},
    {'allowed_channel_ids': [CHANNEL, CHANNEL]}, {'max_bytes': 0}, {'max_bytes': 3_800_000_001},
    {'max_bytes': True}, {'max_height': 1081}, {'timeout_seconds': 21601},
    {'max_new_replays_per_day': 5}, {'owner_permission_reference': ''},
    {'acquisition_authorization_reference': ''}, {'activation_at': '2026-11-01T00:00:00'},
    {'acceptance_video_ids': [VIDEO, 'mnopqrstuvw']}, {'acceptance_video_ids': ['../bad']},
    {'acquisition_mode': 'ordinary_public'}, {'owner_risk_acceptance_reference': ''},
    {'owner_risk_accepted_at': '2099-01-01T00:00:00Z'},
    {'acquisition_mode': 'platform_authorized', 'platform_download_permission_reference': ''},
])
def test_policy_requires_separate_explicit_acquisition_basis(replay, change):
    f = replay
    write_policy(f, **change)
    with pytest.raises(HubError, match='POLICY_INVALID'):
        f.service().enqueue_if_eligible(f.payload)
    assert not f.calls
    unchanged_old_and_no_publish(f)


def test_missing_policy_does_not_turn_copyright_scope_into_acquisition_authorization(replay):
    f = replay
    (f.settings.root / 'config/youtube-download-policy.toml').unlink()
    f.settings.config['owner_content_permission_statement'] = 'owner_confirmed_scope'
    with pytest.raises(HubError, match='POLICY_INVALID'):
        f.service().enqueue_if_eligible(f.payload)
    assert not f.calls


def test_activation_does_not_backfill_old_replays_and_acceptance_is_explicit(replay):
    f = replay
    write_policy(f, activation_at=(f.ended + timedelta(seconds=1)).isoformat(), acceptance_video_ids=[VIDEO])
    assert f.service().enqueue_if_eligible(f.payload) is None
    YouTubeMonitor(f.settings, f.store, None).observe_video(f.raw, CHANNEL)
    assert not f.store.query("SELECT * FROM jobs WHERE job_type='youtube_download'")
    ident = f.service().enqueue_acceptance(f.payload)
    row = f.store.get(ident)
    assert row['payload']['_download_mode'] == 'acceptance' and row['policy_version'] == POLICY_ID
    unchanged_old_and_no_publish(f)


def test_acceptance_video_must_be_pinned_in_policy(replay):
    f = replay
    with pytest.raises(HubError, match='ACCEPTANCE_NOT_AUTHORIZED'):
        f.service().enqueue_acceptance(f.payload)


def test_repeated_official_observations_queue_one_new_type_without_touching_old_metadata(replay):
    f = replay
    monitor = YouTubeMonitor(f.settings, f.store, None)
    monitor.observe_video(f.raw, CHANNEL)
    before = f.store.query("SELECT * FROM jobs WHERE job_type='youtube_download'")
    monitor.observe_video(deepcopy(f.raw), CHANNEL)
    after = f.store.query("SELECT * FROM jobs WHERE job_type='youtube_download'")
    assert len(after) == 1 and before == after
    assert after[0]['policy_version'] == POLICY_ID
    unchanged_old_and_no_publish(f)


@pytest.mark.parametrize('change', ['channel', 'url', 'source_api', 'privacy', 'live', 'duration', 'db_metadata', 'db_state'])
def test_official_ended_replay_binding_is_required(replay, change):
    f = replay
    payload = deepcopy(f.payload)
    if change == 'channel':
        payload['channel_id'] = 'UC' + 'a' * 22
    elif change == 'url':
        payload['url'] = 'https://owned.invalid/other'
    elif change == 'source_api':
        payload['source_api'] = 'youtube-atom-public'
    elif change == 'privacy':
        payload['raw']['status']['privacyStatus'] = 'private'
    elif change == 'live':
        payload['raw']['liveStreamingDetails'].pop('actualEndTime')
    elif change == 'duration':
        payload['duration'] = 'PT2M'
    else:
        with f.store.transaction() as db:
            if change == 'db_metadata':
                value = deepcopy(f.raw); value['snippet']['title'] = 'Replacement'
                db.execute('UPDATE youtube_videos SET metadata=?', (encode(value),))
            else:
                db.execute("UPDATE youtube_videos SET state='live'")
    with pytest.raises(HubError, match='OFFICIAL_BINDING_REQUIRED'):
        f.service().enqueue_if_eligible(payload)
    assert not f.calls


def test_daily_limit_is_persisted_and_dedup_does_not_consume_another_slot(replay):
    f = replay
    write_policy(f, max_new_replays_per_day=1)
    first = f.service().enqueue_if_eligible(f.payload)
    assert f.service(Store(f.settings.db_path)).enqueue_if_eligible(f.payload) == first
    raw = deepcopy(f.raw); raw['id'] = 'mnopqrstuvw'
    YouTubeMonitor(f.settings, f.store, None).observe_video(raw, CHANNEL)
    assert len(f.store.query("SELECT * FROM jobs WHERE job_type='youtube_download'")) == 1


def test_completed_download_is_unpublished_and_reused_after_database_reopen(replay):
    f = replay
    job = claim(f)
    result = f.service().process(job)
    assert result['disposition'] == 'replay_download_verified'
    assert result[STATE]['state'] == 'verified'
    assert result['download_performed'] and not result['publish_performed']
    assert result['human_review'] is False and result['publication_authorized'] is False
    assert result[STATE]['human_review'] is False and result[STATE]['publication_authorized'] is False
    assert encode(f.service(Store(f.settings.db_path)).process(job)) == encode(result)
    assert len(f.calls) == 1
    unchanged_old_and_no_publish(f)


@pytest.mark.parametrize('change', ['lease', 'policy', 'input', 'disabled', 'source'])
def test_inflight_check_aborts_on_revocation_or_changed_input(replay, change):
    f = replay
    job = claim(f)

    class Revoke:
        def __init__(self, settings):
            pass

        def acquire(self, current, binding, *, check):
            if change == 'lease':
                with f.store.transaction() as db:
                    db.execute("UPDATE jobs SET lease_owner='new-owner' WHERE job_id=?", (job['job_id'],))
            elif change == 'policy':
                write_policy(f, max_height=480)
            elif change == 'input':
                payload = deepcopy(job['payload']); payload['title'] = 'Changed'
                with f.store.transaction() as db:
                    db.execute('UPDATE jobs SET payload=? WHERE job_id=?', (encode(payload), job['job_id']))
            elif change == 'source':
                with f.store.transaction() as db:
                    db.execute("UPDATE youtube_videos SET state='unavailable'")
            else:
                f.settings.config['youtube_download_enabled'] = False
            check()
            pytest.fail('revoked download reached completion')

    with pytest.raises(HubError):
        f.service(factory=Revoke).process(job)
    assert f.store.get(job['job_id'])['result'][STATE]['state'] == 'acquiring'
    unchanged_old_and_no_publish(f)


@pytest.mark.parametrize('change', ['bytes', 'file', 'hash', 'duration', 'streams', 'packets'])
def test_downloaded_asset_cannot_claim_verification_with_invalid_evidence(replay, change):
    f = replay
    job = claim(f)

    class Invalid(f.factory):
        def acquire(self, *args, **kwargs):
            result = super().acquire(*args, **kwargs)
            asset = result['source_asset']
            if change == 'bytes': asset['bytes'] += 1
            elif change == 'file': asset['file'] = str(f.settings.root / 'outside.mp4')
            elif change == 'hash': asset['sha256'] = '0' * 64
            elif change == 'duration': asset['duration'] = 90
            elif change == 'streams': asset['stream_types'] = {0: 'video'}
            else: asset['packet_evidence']['audio']['packets'] = 0
            return result

    with pytest.raises(HubError, match='ASSET_INTEGRITY_CHANGED'):
        f.service(factory=Invalid).process(job)
    assert f.store.get(job['job_id'])['result'][STATE]['state'] == 'acquiring'


def test_verified_material_tampering_is_not_redownloaded_or_republished(replay):
    f = replay
    job = claim(f)
    f.service().process(job)
    f.acquired.write_bytes(b'Tampered owned bytes')
    with pytest.raises(HubError, match='ASSET_INTEGRITY_CHANGED'):
        f.service(Store(f.settings.db_path)).process(job)
    assert len(f.calls) == 1
    unchanged_old_and_no_publish(f)


def test_worker_records_bounded_network_retries_with_persistent_checkpoint(replay):
    f = replay
    ident = f.service().enqueue_if_eligible(f.payload)

    class Offline:
        def __init__(self, settings):
            pass

        def acquire(self, job, binding, *, check):
            check()
            raise HubError('YOUTUBE_DOWNLOAD_NETWORK_ERROR', retryable=True)

    for attempt in range(1, f.settings.config['max_attempts'] + 1):
        restarted = Store(f.settings.db_path)
        handler = YouTubeDownload(f.settings, restarted, downloader_factory=Offline)
        assert Worker(f.settings, restarted, {'youtube_download': handler.process}).once('video')
        row = restarted.get(ident)
        assert row['attempts'] == attempt and row['result'][STATE]['state'] == 'acquiring'
        assert row['state'] == ('failed' if attempt == f.settings.config['max_attempts'] else 'retry_scheduled')
        with restarted.transaction() as db:
            db.execute('UPDATE jobs SET next_run=0 WHERE job_id=?', (ident,))
    unchanged_old_and_no_publish(f)


def test_restart_recovers_same_download_job_and_keeps_original_checkpoint(replay):
    f = replay
    job = claim(f)
    _, binding = f.service()._binding(job)
    checkpoint = {STATE: {'version': 'youtube-replay-download-v1', 'state': 'acquiring', 'binding': binding,
                          'publication_authorized': False, 'human_review': False}}
    f.store.checkpoint(job, 'youtube_download_acquiring', checkpoint)
    restarted = Store(f.settings.db_path)
    assert Worker(f.settings, restarted, {'youtube_download': f.service(restarted).process}).once('video')
    row = restarted.get(job['job_id'])
    assert row['state'] == 'completed' and row['attempts'] == 2 and row['result'][STATE]['state'] == 'verified'
    assert len(f.calls) == 1
    unchanged_old_and_no_publish(f)


def test_child_binding_check_does_not_renew_lease_and_rejects_changed_binding(replay):
    f = replay
    job = claim(f)
    _, binding = f.service()._binding(job)
    before = f.store.get(job['job_id'])
    f.service().check_binding(job, binding)
    assert f.store.get(job['job_id']) == before
    with pytest.raises(HubError, match='POLICY_OR_SOURCE_CHANGED'):
        f.service().check_binding(job, {**binding, 'max_height': 1080})
    with f.store.transaction() as db:
        db.execute('UPDATE jobs SET lease_until=? WHERE job_id=?', (time.time()-1, job['job_id']))
    with pytest.raises(HubError, match='LEASE_OR_INPUT_CHANGED'):
        f.service().check_binding(job, binding)


def write_authenticated_policy(f, *, omit=(), **changes):
    values = {'policy_id': AUTH_POLICY_ID, 'session_profile': 'youtube',
              'session_authorization_reference': 'owned-fixture://manual-server-youtube-login',
              'session_authorized_at': f.ended.isoformat()}
    values.update(changes)
    for key in omit:
        values.pop(key, None)
    return write_policy(f, **values)


@pytest.mark.parametrize('omit', ['session_authorization_reference', 'session_authorized_at', 'session_profile'])
def test_authenticated_policy_requires_all_explicit_session_authorization_fields(replay, omit):
    f = replay
    write_authenticated_policy(f, omit=[omit])
    with pytest.raises(HubError, match='POLICY_INVALID'):
        f.service().enqueue_if_eligible(f.payload)
    assert not f.calls
    unchanged_old_and_no_publish(f)


@pytest.mark.parametrize('change', [
    {'session_authorization_reference': ''}, {'session_authorization_reference': '  '},
    {'session_authorized_at': '2099-01-01T00:00:00Z'}, {'session_authorized_at': '2026-11-01T01:00:00'},
    {'session_profile': '../other'}, {'session_profile': '/root/.config/browser'}, {'session_profile': 'bilibili'},
    {'session_profile_path': '/tmp/other'},
])
def test_authenticated_policy_cannot_select_arbitrary_profiles_or_imply_future_authorization(replay, change):
    f = replay
    write_authenticated_policy(f, **change)
    with pytest.raises(HubError, match='POLICY_INVALID'):
        f.service().enqueue_if_eligible(f.payload)
    assert not f.calls


@pytest.mark.parametrize('field,value', [('session_profile', 'youtube'),
    ('session_authorized_at', '2026-11-01T00:00:00Z'), ('session_authorization_reference', 'owned-fixture://login'),
    ('session_cookie_file', '/tmp/forbidden')])
def test_anonymous_policy_rejects_all_session_fields_instead_of_reading_cookies(replay, field, value):
    f = replay
    write_policy(f, **{field: value})
    with pytest.raises(HubError, match='POLICY_INVALID'):
        f.service().enqueue_if_eligible(f.payload)
    assert not f.calls


def test_anonymous_binding_never_contains_account_or_profile_authority(replay):
    f = replay
    job = claim(f)
    _, binding = f.service()._binding(job)
    assert binding['authenticated_session'] is False
    assert not any(key.startswith('session_') for key in binding)
    assert f.store.get(job['job_id'])['policy_version'] == POLICY_ID


def test_authentication_creates_new_job_without_reviving_failed_anonymous_history(replay):
    f = replay
    anonymous_id = f.service().enqueue_if_eligible(f.payload)
    with f.store.transaction() as db:
        db.execute("UPDATE jobs SET state='failed',error_code='YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED',attempts=1 WHERE job_id=?", (anonymous_id,))
    original_failed = deepcopy(f.store.get(anonymous_id))
    write_authenticated_policy(f)
    authenticated_id = f.service().enqueue_if_eligible(f.payload)
    assert authenticated_id != anonymous_id
    assert f.service(Store(f.settings.db_path)).enqueue_if_eligible(f.payload) == authenticated_id
    assert f.store.get(anonymous_id) == original_failed
    auth = f.store.get(authenticated_id)
    assert auth['policy_version'] == AUTH_POLICY_ID and auth['job_type'] == 'youtube_download'
    assert auth['queue'] == 'video' and auth['state'] == 'pending'
    job = f.store.claim('video', 600, 4)
    assert job['job_id'] == authenticated_id
    result = f.service().process(job)
    binding = result[STATE]['binding']
    assert binding['authenticated_session'] is True and binding['session_profile'] == 'youtube'
    assert binding['session_authorization_sha256'] == binding['policy_sha256'] == f.service().policy()['_sha256']
    assert not any(key in encode(binding).lower() for key in ('cookie', 'account_name', 'username'))
    assert result['publication_authorized'] is False and result['human_review'] is False
    assert f.store.get(anonymous_id) == original_failed
    unchanged_old_and_no_publish(f)


def test_switching_back_to_anonymous_does_not_reuse_authenticated_task(replay):
    f = replay
    write_authenticated_policy(f)
    authenticated_id = f.service().enqueue_if_eligible(f.payload)
    with f.store.transaction() as db:
        db.execute("UPDATE jobs SET state='failed',error_code='OWNED_SESSION_EXPIRED' WHERE job_id=?", (authenticated_id,))
    old_auth = f.store.get(authenticated_id)
    write_policy(f)
    anonymous_id = f.service().enqueue_if_eligible(f.payload)
    assert anonymous_id != authenticated_id and f.store.get(anonymous_id)['policy_version'] == POLICY_ID
    assert f.store.get(authenticated_id) == old_auth


def test_old_anonymous_processing_lease_cannot_borrow_new_authenticated_policy(replay):
    f = replay
    job = claim(f)
    _, anonymous_binding = f.service()._binding(job)
    before = f.store.get(job['job_id'])
    write_authenticated_policy(f)
    with pytest.raises(HubError, match='LEASE_OR_INPUT_CHANGED'):
        f.service().check_binding(job, anonymous_binding)
    assert f.store.get(job['job_id']) == before
    assert not f.calls
