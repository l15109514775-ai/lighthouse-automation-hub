"""Real SQLite/media/coverage record path with an owned lookup-only browser."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import uuid

import pytest

from test_short_video_web import short_video, BV
from test_video_source_retention_v2 import owned, put, now
from test_video_source_retention_tracks_v2 import tracks
from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.locks import resource_guard
from services.hub.replay_highest_video_v2 import HighestReplayVideoWeb
from services.hub.youtube_download import CHANNELS, digest
from services.hub import video_retention_refresh_v1 as module
from services.hub.video_retention_refresh_v1 import CompletedVideoMaintenanceV1, CompletedRefreshServiceMixin, PRINCIPAL


class OwnedCompletedService(CompletedRefreshServiceMixin, HighestReplayVideoWeb):
    def policy(self):
        # Only the root policy loading boundary is an owned fixture. The
        # actual record_result, coverage raw/hash, CAS and publication are real.
        job = self.store.query('SELECT payload FROM jobs WHERE job_type=?', (self.JOB_TYPE,))[0]
        binding = json.loads(job['payload'])
        return {'_sha256': getattr(self, 'policy_override', binding['policy_sha256']), 'target_site_uid': binding['site_uid'],
                'target_account_name': binding['account_name'], 'allowed_channel_ids': list(CHANNELS)}


class OwnedBrowser:
    def __init__(self, f, *, change=None):
        self.f, self.change, self.calls, self.manifests = f, change, [], []

    @contextmanager
    def session(self, manifest, *, assert_lease, before_launch):
        self.manifests.append(manifest)
        locks = self.f['settings'].root/'data/locks'
        # Match the production profile -> heavy ordering. A callback invoked
        # under the scan's heavy lock would fail this real OS lock acquisition.
        with resource_guard(locks/'bilibili-video-profile', locks/'heavy'):
            assert_lease()
            before_launch()
            browser = self
            class Session:
                def operation(self, action, **arguments):
                    assert_lease()
                    browser.calls.append((action, arguments))
                    assert action == 'lookup' and arguments == {'external_id': BV}
                    if browser.change == 'network':
                        raise HubError('REPLAY_BROWSER_RESPONSE_UNKNOWN', review=True)
                    f = browser.f
                    job = f['store'].get(f['id']);binding = job['payload'];state = job['result'][f['highest_state']]
                    if browser.change == 'source_changed':
                        source = f['store'].get(f['source']);source['payload']['title'] = 'Changed owned immutable source'
                        with f['store'].transaction() as db:db.execute('UPDATE jobs SET payload=? WHERE job_id=?', (encode(source['payload']), f['source']))
                    if browser.change == 'policy_changed':
                        OwnedCompletedService.policy_override = '0'*64
                    if browser.change == 'parent_changed':
                        with f['store'].transaction() as db:
                            db.execute('UPDATE jobs SET attempts=attempts+1 WHERE job_id=?', (f['id'],))
                    raw = json.loads(f['public'].read_bytes());raw['public_player']['observed_at'] = now()
                    if browser.change == 'wrong_duration': raw['public_player']['duration_seconds'] += 60
                    ref = put(f['public'], raw)
                    observation = {'version': HighestReplayVideoWeb.RESULT_OBSERVATION_VERSION,
                        'attempt_id': state['attempt_id'], 'site_uid': binding['site_uid'], 'source_url': binding['source_url'],
                        'metadata_sha256': binding['metadata_sha256'], 'external_id': BV,
                        'url': 'https://www.bilibili.com/video/'+BV, 'state': 'completed',
                        'public_accessible': True, 'public_lookup_observed_at': now(), 'cleanup_coverage': ref}
                    if browser.change == 'wrong_uid': observation['site_uid'] = 'wrong-owned-uid'
                    return {'result_observation': observation}
            yield Session()


@pytest.fixture
def refresh(tracks):
    f = tracks
    source = f['store'].get(f['source'])
    source['payload'].update(source_api='youtube-data-v3-official', raw={'id': source['source_id'],
        'snippet': {'channelId': source['payload']['channel_id']}})
    job = f['store'].get(f['id']);state = job['result'][f['highest_state']]
    state['result_observation'].update(version=HighestReplayVideoWeb.RESULT_OBSERVATION_VERSION,
        attempt_id=state['attempt_id'], public_accessible=True,
        public_lookup_observed_at=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat())
    with f['store'].transaction() as db:
        db.execute('UPDATE jobs SET payload=? WHERE job_id=?', (encode(source['payload']), f['source']))
        db.execute('UPDATE jobs SET result=? WHERE job_id=?', (encode(job['result']), f['id']))
    def manifest(settings, store, job, binding, state, attempt, *, duration_seconds, require_cover):
        assert require_cover is False and duration_seconds > 1
        return {'job_id': job['job_id'], 'attempt_id': attempt, 'target_site_uid': binding['site_uid'], 'owned_lookup_only': True}
    f['route'] = lambda settings, store, job: (OwnedCompletedService(settings, store), manifest)
    f['browser'] = OwnedBrowser(f)
    return f


def maintenance(f, *, browser=None, **kwargs):
    return CompletedVideoMaintenanceV1(f['settings'], f['store'], retention=f['service'],
        route_factory=f['route'], browser=browser or f['browser'], **kwargs)


def test_completed_native_lookup_refreshes_actual_coverage_and_preserves_attempts_and_authority(refresh):
    f = refresh;before = f['store'].get(f['id']);pubs = f['store'].query('SELECT * FROM publications')
    result = maintenance(f).refresh_once(f['id'], authorization_id=f['auth']['authorization_id'])
    after = f['store'].get(f['id']);state = after['result'][f['highest_state']]
    assert result['state'] == 'refreshed' and f['browser'].calls == [('lookup', {'external_id': BV})]
    for key in ('attempts', 'lease_owner', 'lease_until', 'payload', 'external_publish_id'):
        assert after[key] == before[key]
    assert state['attempt_id'] == before['result'][f['highest_state']]['attempt_id']
    assert state['public_submission_approved'] is False and state['human_review'] is False
    assert 'same_submit_attempt_observed' not in state['result_observation']
    assert state['cleanup_coverage']['public_player']['observed_at'] != before['result'][f['highest_state']]['cleanup_coverage']['public_player']['observed_at']
    assert f['store'].query('SELECT * FROM publications')[0]['external_id'] == pubs[0]['external_id']
    assert f['file'].exists() and f['video'].exists() and f['audio'].exists()
    assert f['store'].query('SELECT state FROM video_retention_refresh_lookups_v1') == [{'state': 'refreshed'}]
    assert f['store'].query('SELECT state FROM video_retention_refresh_runs_v1') == [{'state': 'completed'}]


def test_authorized_scheduler_real_scan_refresh_then_exact_three_file_cleanup(refresh, monkeypatch):
    f = refresh;monkeypatch.setattr(f['service'], '_release', lambda plan: 'owned-root-admission-only')
    before = f['store'].get(f['id'])
    report = maintenance(f).run_once(f['auth']['authorization_id'], apply=True)
    assert report['processed'][0]['result']['local_files_deleted'] == 3
    assert report['candidate_jobs_attempted'] == report['candidate_window_maximum'] == 1
    assert f['browser'].calls == [('lookup', {'external_id': BV})]
    assert not any(file.exists() for file in (f['file'], f['video'], f['audio']))
    assert f['public'].is_file() and f['file'].with_name('verified.json').is_file()
    after = f['store'].get(f['id']);assert after['attempts'] == before['attempts'] and after['payload'] == before['payload']
    assert maintenance(f).run_once(f['auth']['authorization_id'], apply=True)['processed'] == []
    assert f['store'].query('SELECT COUNT(*) n FROM publications')[0]['n'] == 1


@pytest.mark.parametrize('change', ['wrong_uid', 'wrong_duration', 'network', 'parent_changed', 'source_changed', 'policy_changed'])
def test_unknown_or_wrong_native_result_retains_all_media_without_another_side_effect(refresh, change):
    f = refresh;browser = OwnedBrowser(f, change=change);before = f['store'].get(f['id'])
    with pytest.raises(HubError): maintenance(f, browser=browser).refresh_once(f['id'], authorization_id=f['auth']['authorization_id'])
    assert browser.calls == [('lookup', {'external_id': BV})]
    assert all(file.exists() for file in (f['file'], f['video'], f['audio']))
    assert f['store'].get(f['id'])['result'] == before['result']
    with pytest.raises(HubError):
        maintenance(f, browser=browser).refresh_once(f['id'], authorization_id=f['auth']['authorization_id'])
    if hasattr(OwnedCompletedService, 'policy_override'):del OwnedCompletedService.policy_override
    assert len(browser.calls) == 1
    assert f['store'].query('SELECT COUNT(*) n FROM video_retention_refresh_lookups_v1')[0]['n'] == 1


@pytest.mark.parametrize('change', ['processing', 'unknown', 'wrong_channel', 'wrong_uid', 'owner_deleted', 'different_bv', 'missing_attempt'])
def test_only_completed_causally_bound_authorized_job_can_start_browser(refresh, change):
    f = refresh;job = f['store'].get(f['id'])
    if change in ('processing', 'unknown'):
        with f['store'].transaction() as db:db.execute('UPDATE jobs SET state=? WHERE job_id=?', ('processing' if change == 'processing' else 'publish_unknown', f['id']))
    else:
        if change == 'wrong_channel':job['payload']['channel_id'] = 'unrelated-channel'
        elif change == 'wrong_uid':job['payload']['site_uid'] = '00000'
        elif change == 'different_bv':job['result'][f['highest_state']]['result_observation']['external_id'] = 'BV1111111111'
        elif change == 'missing_attempt':job['result'][f['highest_state']]['result_observation'].pop('attempt_id')
        else:job['result'][f['highest_state']]['owner_authorized_remote_deletion_v1'] = {'remote_state': 'owner_deleted'}
        with f['store'].transaction() as db:db.execute('UPDATE jobs SET payload=?,result=? WHERE job_id=?', (encode(job['payload']), encode(job['result']), f['id']))
    with pytest.raises(HubError):maintenance(f).refresh_once(f['id'], authorization_id=f['auth']['authorization_id'])
    assert f['browser'].calls == [] and f['file'].exists()


def test_video_or_profile_lock_busy_is_a_safe_deferral_and_not_a_new_attempt(refresh):
    f = refresh;before = f['store'].get(f['id'])
    for name in ('video', 'moderation', 'bilibili-video-profile', 'heavy'):
        with resource_guard(f['settings'].root/'data/locks'/name):
            with pytest.raises(HubError, match='HEAVY_RESOURCE_BUSY|COOLDOWN'):
                maintenance(f).refresh_once(f['id'], authorization_id=f['auth']['authorization_id'])
    assert f['browser'].calls == [] and f['store'].get(f['id']) == before
    assert f['store'].query('SELECT COUNT(*) n FROM video_retention_refresh_lookups_v1')[0]['n'] == 0


def test_refresh_principal_cannot_invoke_unbound_write_or_become_worker(refresh):
    f = refresh;service = OwnedCompletedService(f['settings'], f['store'])
    for action in (lambda:service._operator(PRINCIPAL), lambda:service.start_upload(PRINCIPAL, f['id'], attempt_id=str(uuid.uuid4()), site_uid='123456'),
                   lambda:service.reserve_submit(PRINCIPAL, f['id'], attempt_id=str(uuid.uuid4()), site_uid='123456', upload_reference_sha256='a'*64)):
        with pytest.raises(HubError, match='ACCESS_DENIED'):action()
    assert not f['store'].query("SELECT * FROM audit WHERE action='short_video_reserve_submit'")


def test_daily_lookup_reservations_are_persistent_even_for_unknown_network_results(refresh):
    f = refresh;browser = OwnedBrowser(f, change='network')
    day = datetime(2026, 1, 15, tzinfo=timezone.utc)
    clock = [(day + timedelta(hours=12)).timestamp()]
    for _ in range(module.PER_JOB_DAILY_LIMIT):
        with pytest.raises(HubError, match='REPLAY_BROWSER_RESPONSE_UNKNOWN'):
            maintenance(f, browser=browser, clock=lambda:clock[0]).refresh_once(f['id'], authorization_id=f['auth']['authorization_id'])
        clock[0] += module.COOLDOWN_SECONDS + 1
    with pytest.raises(HubError, match='JOB_DAILY_LIMIT'):
        maintenance(f, browser=browser, clock=lambda:clock[0]).refresh_once(f['id'], authorization_id=f['auth']['authorization_id'])
    assert len(browser.calls) == module.PER_JOB_DAILY_LIMIT
    assert f['store'].query('SELECT COUNT(*) n FROM video_retention_refresh_lookups_v1')[0]['n'] == module.PER_JOB_DAILY_LIMIT
    clock[0] = (day + timedelta(days=1)).timestamp()
    with pytest.raises(HubError, match='REPLAY_BROWSER_RESPONSE_UNKNOWN'):
        maintenance(f, browser=browser, clock=lambda:clock[0]).refresh_once(f['id'], authorization_id=f['auth']['authorization_id'])
    assert len(browser.calls) == module.PER_JOB_DAILY_LIMIT + 1
    assert f['store'].query('SELECT COUNT(*) n FROM video_retention_refresh_lookups_v1')[0]['n'] == len(browser.calls)


def test_background_heartbeat_is_dedicated_and_parent_rows_stay_unchanged(refresh):
    f = refresh;before = f['store'].get(f['id']);m = maintenance(f, heartbeat_interval=.01)
    with resource_guard(f['settings'].root/'data/locks/video-retention-maintenance-v1'):
        with m._run(f['auth']['authorization_id']):
            first = f['store'].query('SELECT heartbeat_at FROM video_retention_refresh_runs_v1')[0]['heartbeat_at']
            deadline = time.monotonic() + 2
            while f['store'].query('SELECT heartbeat_at FROM video_retention_refresh_runs_v1')[0]['heartbeat_at'] <= first:
                assert time.monotonic() < deadline
                time.sleep(.01)
            m._assert_run()
    assert f['store'].get(f['id']) == before
    names = {row['name'] for row in f['store'].query('SELECT name FROM heartbeats')}
    assert module.HEARTBEAT in names and 'worker:video' not in names


def test_lost_maintenance_lease_blocks_browser_without_claiming_or_recovering_jobs(refresh):
    f = refresh;m = maintenance(f)
    with m._run(f['auth']['authorization_id']):
        with f['store'].transaction() as db:db.execute('UPDATE video_retention_refresh_runs_v1 SET lease_until=0')
        with pytest.raises(HubError, match='LEASE_LOST'):m._refresh(f['id'])
        # Preserve the deliberate lost lease as a real failed run at exit.
        m._lost = None
        with f['store'].transaction() as db:db.execute('UPDATE video_retention_refresh_runs_v1 SET lease_until=?', (time.time()+90,))
    assert f['browser'].calls == [] and f['store'].get(f['id'])['state'] == 'completed'


def test_actual_transport_hook_reserves_after_locks_and_counts_constructor_failure(refresh):
    f = refresh;seen = []
    def unavailable(settings, manifest, *, assert_lease):
        assert_lease()
        rows = f['store'].query('SELECT state FROM video_retention_refresh_lookups_v1')
        assert rows == [{'state': 'lookup_reserved'}]
        seen.append('actual_session_constructor')
        raise HubError('REPLAY_BROWSER_TRANSPORT_UNAVAILABLE', review=True)
    browser = module.CompletedLookupBrowser(f['settings'], session_factory=unavailable)
    with resource_guard(f['settings'].root/'data/locks/bilibili-video-profile'):
        with pytest.raises(HubError, match='HEAVY_RESOURCE_BUSY'):
            maintenance(f, browser=browser).refresh_once(f['id'], authorization_id=f['auth']['authorization_id'])
    assert f['store'].query('SELECT COUNT(*) n FROM video_retention_refresh_lookups_v1')[0]['n'] == 0 and seen == []
    with pytest.raises(HubError, match='TRANSPORT_UNAVAILABLE'):
        maintenance(f, browser=browser).refresh_once(f['id'], authorization_id=f['auth']['authorization_id'])
    assert seen == ['actual_session_constructor']
    assert f['store'].query('SELECT state FROM video_retention_refresh_lookups_v1') == [{'state': 'lookup_failed'}]


def test_global_platform_attempt_limit_is_shared_across_process_instances(refresh):
    f = refresh;m = maintenance(f);m.initialize();instant = time.time()
    with f['store'].transaction() as db:
        for _ in range(module.GLOBAL_DAILY_LIMIT):
            db.execute('INSERT INTO video_retention_refresh_lookups_v1 VALUES(?,?,?,?,?,?,?,?,?)',
                (str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()), 'a'*64, BV, 'lookup_unknown', instant, None, '{}'))
    with pytest.raises(HubError, match='GLOBAL_DAILY_LIMIT'):
        maintenance(f).refresh_once(f['id'], authorization_id=f['auth']['authorization_id'])
    assert f['browser'].calls == [] and f['store'].query('SELECT COUNT(*) n FROM video_retention_refresh_lookups_v1')[0]['n'] == module.GLOBAL_DAILY_LIMIT
    report = maintenance(f).run_once(f['auth']['authorization_id'], apply=True)
    assert report['candidate_jobs_attempted'] == 0 and report['processed'] == []
    assert report['pre_scan_deferred'][0]['error_code'] == 'VIDEO_RETENTION_REFRESH_GLOBAL_DAILY_LIMIT'


def test_unexpired_interrupted_run_defers_then_expires_without_resetting_lookup_history(refresh):
    f = refresh;m = maintenance(f);m.initialize();instant = time.time();old = str(uuid.uuid4())
    with f['store'].transaction() as db:
        db.execute('INSERT INTO video_retention_refresh_runs_v1 VALUES(?,?,?,?,?,?,?,?,?)',
            (old, f['auth']['authorization_id'], digest(f['auth']), 'processing', 'native_public_lookup', instant, instant, instant+90, '{}'))
        db.execute('INSERT INTO video_retention_refresh_lookups_v1 VALUES(?,?,?,?,?,?,?,?,?)',
            (str(uuid.uuid4()), old, f['id'], digest(f['store'].get(f['id'])['payload']), BV, 'lookup_reserved', instant, None, '{}'))
    with pytest.raises(HubError, match='RUN_BUSY'):
        maintenance(f).refresh_once(f['id'], authorization_id=f['auth']['authorization_id'])
    with f['store'].transaction() as db:db.execute('UPDATE video_retention_refresh_runs_v1 SET lease_until=0 WHERE operation_id=?', (old,))
    with pytest.raises(HubError, match='COOLDOWN'):
        maintenance(f).refresh_once(f['id'], authorization_id=f['auth']['authorization_id'])
    assert f['store'].query('SELECT state FROM video_retention_refresh_runs_v1 WHERE operation_id=?', (old,)) == [{'state': 'interrupted'}]
    assert f['store'].query('SELECT COUNT(*) n FROM video_retention_refresh_lookups_v1')[0]['n'] == 1 and f['browser'].calls == []


def test_fresh_release_requires_new_natural_composition_and_cannot_use_old_46_gate(refresh):
    f = refresh;cls = module.FreshNaturalVideoSourceRetentionV1
    assert cls.TEST_COUNT == 74 and sum(cls.TEST_GROUPS.values()) == 74
    assert len(cls.NATURAL_RUNTIME_SOURCES) == 57
    assert {'scheduler/service.py', 'vendor/subtitle-cloud-worker/observer/store.py',
        'services/hub/replay_packet_cache_v1.py', 'services/hub/replay_machine_checks.py'} <= set(cls.NATURAL_RUNTIME_SOURCES)
    assert cls.RELEASE_FILENAME != f['service'].RELEASE_FILENAME
    assert 'services/hub/video_retention_refresh_v1.py' in cls.RELEASE_EXTRA_SOURCES
    original = f['service'].plan(f['id'], authorization_id=f['auth']['authorization_id'])
    # No new protected release exists. In particular old19/38/46 acceptance
    # cannot authorize this runtime merely because its source paths overlap.
    with pytest.raises(HubError, match='VIDEO_RETENTION_RELEASE_REQUIRED'):
        cls(f['settings'], f['store'])._release(original)


def test_failed_candidate_cannot_cause_unbounded_following_full_scans(refresh, monkeypatch):
    f = refresh;calls = []
    other = f['store'].enqueue('youtube', 'zzzzzzzzzzz', 'replay_highest_video_publish_v2', 'video', {}, 'owned-not-a-real-publish')
    with f['store'].transaction() as db:db.execute("UPDATE jobs SET state='completed' WHERE job_id=?", (other,))
    def blocked(ident, **kwargs):
        calls.append(ident)
        raise HubError('OWNED_SIMULATED_FAILED_SCAN', review=True)
    monkeypatch.setattr(f['service'], 'plan', blocked)
    report = maintenance(f).run_once(f['auth']['authorization_id'], apply=True, max_jobs=1)
    assert calls == [f['id']] and f['browser'].calls == []
    assert report['candidate_jobs_attempted'] == 1 and report['state'] == 'candidate_window_exhausted'
    assert report['processed'] == [] and report['retained'] == [{'job_id': f['id'], 'error_code': 'OWNED_SIMULATED_FAILED_SCAN'}]
    assert Path(report['report_file']).is_file() and f['file'].exists()
    # Persisted next_due cheaply skips the rejected first job. A new process
    # can give the second completed candidate its own bounded window.
    second = maintenance(f).run_once(f['auth']['authorization_id'], apply=True, max_jobs=1)
    assert calls == [f['id'], other] and second['candidate_jobs_attempted'] == 1
    assert second['pre_scan_deferred'] == [{'job_id': f['id'], 'error_code': 'VIDEO_RETENTION_REFRESH_SCAN_COOLDOWN'}]
    scans = f['store'].query('SELECT * FROM video_retention_scan_windows_v1b ORDER BY last_scan')
    assert len(scans) == 2 and all(row['next_due'] > row['last_scan'] for row in scans)


def test_same_candidate_plan_apply_counts_once_then_next_candidate_stops_before_scan():
    calls = []
    service = SimpleNamespace(plan=lambda ident, **kwargs:(calls.append(('plan', ident)) or {'job_id': ident}),
        apply=lambda plan:(calls.append(('apply', plan['job_id'])) or {'local_files_deleted': 0}))
    proxy = module._BoundedRetention(service, 1, lambda: None)
    plan = proxy.plan('owned-job-one');proxy.apply(plan)
    with pytest.raises(module._CandidateWindowFull):proxy.plan('owned-job-two')
    assert proxy.seen == {'owned-job-one'} and calls == [('plan', 'owned-job-one'), ('apply', 'owned-job-one')]


@pytest.mark.parametrize('maximum', [2, 3])
def test_multi_candidate_window_keeps_success_before_later_failures(refresh, monkeypatch, maximum):
    f = refresh;calls = [];original_plan = f['service'].plan
    before = f['store'].get(f['id']);publications = f['store'].query('SELECT * FROM publications')
    # max2 exercises a real three-file apply success. max3 exercises an
    # already-durable pending dry-run success, which never calls proxy.apply.
    apply = maximum == 2
    if not apply:
        pending = original_plan(f['id'], authorization_id=f['auth']['authorization_id'])
        f['service']._persist(pending, {'version': 'video-source-track-cleanup-intent-v1', 'plan': pending,
            'release_sha256': 'owned-root-release-boundary-only',
            'progress': {item['file']: 'pending' for item in pending['candidates']}}, 'deleting')
    others = []
    for index in range(maximum + 1):
        ident = f['store'].enqueue('youtube', 'owned-extra-'+str(index), 'replay_highest_video_publish_v2', 'video', {}, 'owned-scan-rejection-only')
        with f['store'].transaction() as db:db.execute("UPDATE jobs SET state='completed' WHERE job_id=?", (ident,))
        others.append(ident)
    def planned(ident, **kwargs):
        calls.append(ident)
        if ident == f['id']:
            return original_plan(ident, **kwargs)
        raise HubError('OWNED_SIMULATED_LATER_SCAN_REJECTION', review=True)
    monkeypatch.setattr(f['service'], 'plan', planned)
    monkeypatch.setattr(f['service'], '_release', lambda plan: 'owned-root-release-boundary-only')
    report = maintenance(f).run_once(f['auth']['authorization_id'], apply=apply, max_jobs=maximum)
    assert report['state'] == 'candidate_window_exhausted' and report['candidate_jobs_attempted'] == maximum
    assert len(report['processed']) == 1 and report['processed'][0]['job_id'] == f['id']
    assert report['processed'][0]['resumed'] is (not apply)
    if apply:
        assert report['processed'][0]['result']['local_files_deleted'] == 3
        assert not any(file.exists() for file in (f['file'], f['video'], f['audio']))
        assert calls == [f['id'], others[0]] and len(report['retained']) == 1
        assert f['browser'].calls == [('lookup', {'external_id': BV})]
    else:
        assert report['processed'][0]['result'] == {'state': 'durable_cleanup_pending', 'plan_id': pending['plan_id'], 'local_files_deleted': 0}
        assert all(file.exists() for file in (f['file'], f['video'], f['audio']))
        assert calls == others[:maximum] and len(report['retained']) == maximum
        assert f['browser'].calls == []
    assert all(row['error_code'] == 'OWNED_SIMULATED_LATER_SCAN_REJECTION' for row in report['retained'])
    assert json.loads(Path(report['report_file']).read_bytes())['processed'] == report['processed']
    assert f['store'].get(f['id'])['attempts'] == before['attempts']
    assert f['store'].get(f['id'])['payload'] == before['payload']
    assert len(f['store'].query('SELECT * FROM publications')) == len(publications) == 1
