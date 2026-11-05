"""Completed-only native public lookup for bounded source-retention maintenance.

This entry reuses the existing browser/coverage validators. It cannot claim a
publication job, upload a file, submit, approve, delete a remote video, reset an
attempt, or impersonate a Worker heartbeat. Destructive local work still runs
through the separately released retention core.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import argparse
import json
import re
import threading
import time
import uuid

from .db import encode
from .errors import HubError
from .locks import resource_guard
from .youtube_download import CHANNELS, digest
from .replay_video_browser import ReplayVideoBrowser
from .video_source_retention_natural_formats_v3 import NaturalFormatVideoSourceRetentionV3

VERSION = 'completed-video-retention-refresh-v1'
PRINCIPAL = 'maintenance:completed-video-public-lookup-v1'
HEARTBEAT = 'maintenance:video-retention-refresh-v1'
COOLDOWN_SECONDS = 900
PER_JOB_DAILY_LIMIT = 4
GLOBAL_DAILY_LIMIT = 24
LEASE_SECONDS = 90
SCAN_RETRY_SECONDS = 1800


def fail(code):
    raise HubError(code, retryable=True)


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


class CompletedRefreshServiceMixin:
    """A lookup-only principal, admitted only inside this exact result call."""
    def _operator(self, principal):
        guard = getattr(self, '_completed_retention_guard', None)
        if principal == PRINCIPAL and guard is not None:
            guard()
            return
        return super()._operator(principal)

    def record_completed_refresh(self, job_id, observation, *, guard):
        guard()
        job, binding, state = self.snapshot(job_id, write=False)
        previous = state.get('result_observation', {})
        if (job['state'] != 'completed' or state.get('state') != 'completed' or
                observation.get('state') != 'completed' or observation.get('public_accessible') is not True or
                observation.get('external_id') != previous.get('external_id') or
                observation.get('attempt_id') != state.get('attempt_id') or
                observation.get('site_uid') != binding['site_uid'] or
                observation.get('source_url') != binding['source_url'] or
                observation.get('metadata_sha256') != binding['metadata_sha256']):
            fail('VIDEO_RETENTION_REFRESH_RESULT_NOT_COMPLETED_BOUND_LOOKUP')
        if getattr(self, '_completed_retention_guard', None) is not None:
            fail('VIDEO_RETENTION_REFRESH_CONTEXT_ALREADY_BOUND')
        self._completed_retention_guard = guard
        try:
            # The existing validator reads the native JSON and exact file SHA,
            # checks full player duration, source packet coverage and fresh time.
            # No same-submit-attempt claim is added by this maintenance path.
            return super().record_result(PRINCIPAL, job_id, observation, lookup=True)
        finally:
            self._completed_retention_guard = None


class FreshNaturalVideoSourceRetentionV1(NaturalFormatVideoSourceRetentionV3):
    # Independent release for the Natural295 + retention + refresh combination.
    # The old46 release cannot enable this class or stand in for these tests.
    RELEASE_FILENAME = 'video-source-retention-fresh-natural-v1c-release.json'
    TEST_VERSION = 'video-source-retention-fresh-natural-v1c-ubuntu-tests'
    TEST_COUNT = 74
    TEST_GROUPS = {**NaturalFormatVideoSourceRetentionV3.TEST_GROUPS,
        'tests.test_video_retention_refresh_v1': 28}
    NATURAL_PARENT_ARCHIVE_SHA256 = '8fc11a3e27dca0dd2c08c12a34da111e70f37bc38ce74d6cdebb122840578e2a'
    NATURAL_RUNTIME_SOURCES = ('adapters/bilibili_video_guard.mjs', 'adapters/browser_replay_video.mjs', 'adapters/media_tools.py', 'adapters/recovered_submit_gate.mjs', 'adapters/replay_highest_contract_v2.mjs', 'adapters/replay_native_driver.mjs', 'adapters/replay_natural_contract_v3.mjs', 'adapters/safe_https.py', 'adapters/short_video_metadata_chain.mjs', 'adapters/short_video_saved_draft_navigation_control.mjs', 'adapters/youtube.py', 'adapters/youtube_feed.py', 'adapters/youtube_readiness_evidence_v3.py', 'adapters/ytdlp_highest_v2.py', 'adapters/ytdlp_natural_plan_v3.py', 'adapters/ytdlp_natural_readiness_v3.py', 'adapters/ytdlp_natural_v3.py', 'adapters/ytdlp_replay.py', 'scheduler/service.py', 'scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs', 'services/hub/budget.py', 'services/hub/config.py', 'services/hub/cover_normalizer_v4.py', 'services/hub/db.py', 'services/hub/errors.py', 'services/hub/evidence.py', 'services/hub/locks.py', 'services/hub/maintenance.py', 'services/hub/media_processing.py', 'services/hub/outcomes.py', 'services/hub/recovered_submit_ticket.py', 'services/hub/replay_highest_coverage_v2.py', 'services/hub/replay_highest_limits_v2.py', 'services/hub/replay_highest_video_v2.py', 'services/hub/replay_highest_worker_runtime_v2.py', 'services/hub/replay_machine_checks.py', 'services/hub/replay_moderation_common.py', 'services/hub/replay_natural_launch_v3.py', 'services/hub/replay_natural_moderation_v3.py', 'services/hub/replay_natural_video_v3.py', 'services/hub/replay_natural_worker_runtime_v3.py', 'services/hub/replay_packet_cache_v1.py', 'services/hub/replay_video_browser.py', 'services/hub/replay_video_cover.py', 'services/hub/replay_video_web.py', 'services/hub/replay_worker_runtime.py', 'services/hub/resource_wait_v1.py', 'services/hub/short_video_web.py', 'services/hub/worker.py', 'services/hub/youtube_download.py', 'services/hub/youtube_download_v2.py', 'services/hub/youtube_download_v3.py', 'services/hub/youtube_highest_plan_v2.py', 'services/hub/youtube_monitor.py', 'services/hub/youtube_natural_plan_v3.py', 'services/hub/youtube_readiness_v3.py', 'vendor/subtitle-cloud-worker/observer/store.py')
    RELEASE_EXTRA_SOURCES = tuple(dict.fromkeys((*NaturalFormatVideoSourceRetentionV3.RELEASE_EXTRA_SOURCES,
        *NATURAL_RUNTIME_SOURCES, 'services/hub/video_retention_refresh_v1.py')))


class CompletedLookupBrowser(ReplayVideoBrowser):
    @contextmanager
    def session(self, manifest, *, assert_lease, before_launch):
        # Reserve a platform attempt after every local busy lock succeeds,
        # immediately before the existing native child starts.
        locks = self.settings.root/'data/locks'
        with resource_guard(locks/'bilibili-video-profile'):
            with resource_guard(locks/'heavy'):
                assert_lease()
                before_launch()
                session = self.session_factory(self.settings, manifest, assert_lease=assert_lease)
            try:
                yield session
            finally:
                session.close()


def _route(settings, store, job):
    from .replay_highest_video_v2 import HighestReplayVideoWeb, highest_replay_manifest
    from .replay_natural_video_v3 import NaturalReplayVideoWeb, natural_replay_manifest
    if job['job_type'] == HighestReplayVideoWeb.JOB_TYPE:
        class Service(CompletedRefreshServiceMixin, HighestReplayVideoWeb):
            pass
        return Service(settings, store), highest_replay_manifest
    if job['job_type'] == NaturalReplayVideoWeb.JOB_TYPE:
        class Service(CompletedRefreshServiceMixin, NaturalReplayVideoWeb):
            pass
        return Service(settings, store), natural_replay_manifest
    fail('VIDEO_RETENTION_REFRESH_JOB_TYPE_REJECTED')


class _CandidateWindowFull(Exception):
    pass


class _BoundedRetention:
    """Bound expensive candidate attempts without changing the old scheduler.

    Its success-count limit alone does not bound failed scans. Count a job at
    its first plan/apply invocation; the same plan-to-apply job counts once.
    """
    def __init__(self, service, maximum, guard, *, on_scan=lambda ident: None, on_failure=lambda ident, code: None):
        self.service, self.maximum, self.guard = service, maximum, guard
        self.on_scan, self.on_failure = on_scan, on_failure
        self.seen, self.retained = set(), []

    def __getattr__(self, name):
        return getattr(self.service, name)

    def _take(self, ident):
        self.guard()
        if ident not in self.seen:
            if len(self.seen) >= self.maximum:
                raise _CandidateWindowFull()
            self.on_scan(ident)
            self.seen.add(ident)

    def _call(self, ident, action, *args, **kwargs):
        self._take(ident)
        try:
            return action(*args, **kwargs)
        except HubError as exc:
            self.retained.append({'job_id': ident, 'error_code': exc.code})
            self.on_failure(ident, exc.code)
            raise

    def plan(self, ident, **kwargs):
        return self._call(ident, self.service.plan, ident, **kwargs)

    def apply(self, plan):
        return self._call(plan['job_id'], self.service.apply, plan)


class _FairCandidateView:
    def __init__(self, store, select):
        self.store, self.select = store, select

    def __getattr__(self, name):
        return getattr(self.store, name)

    def query(self, sql, args=()):
        rows = self.store.query(sql, args)
        # The fixed old scheduler query is only a candidate list. All other
        # reads and the destructive service keep their genuine Store object.
        normalized = ' '.join(sql.split())
        expected = "SELECT job_id,result FROM jobs WHERE state='completed' AND queue='video' AND job_type IN ('replay_highest_video_publish_v2','replay_natural_video_publish_v3') ORDER BY created_at,job_id"
        return self.select(rows) if normalized == expected else rows


class CompletedVideoMaintenanceV1:
    def __init__(self, settings, store, *, retention=None, browser=None,
                 route_factory=None, clock=time.time, heartbeat_interval=10):
        self.settings, self.store = settings, store
        self.retention = retention or FreshNaturalVideoSourceRetentionV1(settings, store)
        self.browser = browser or CompletedLookupBrowser(settings)
        self.route_factory = route_factory or _route
        self.clock, self.heartbeat_interval = clock, heartbeat_interval
        self._run_id = self._authorization_id = self._authorization_sha = None
        self._lost = None

    def initialize(self):
        with self.store.transaction() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS video_retention_refresh_runs_v1(
                operation_id TEXT PRIMARY KEY, authorization_id TEXT NOT NULL, authorization_sha256 TEXT NOT NULL,
                state TEXT NOT NULL, phase TEXT NOT NULL, started_at REAL NOT NULL, heartbeat_at REAL NOT NULL,
                lease_until REAL NOT NULL, result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS video_retention_refresh_lookups_v1(
                operation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, job_id TEXT NOT NULL,
                binding_sha256 TEXT NOT NULL, external_id TEXT NOT NULL, state TEXT NOT NULL,
                started_at REAL NOT NULL, finished_at REAL, result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS video_retention_scan_windows_v1b(
                job_id TEXT PRIMARY KEY, last_scan REAL NOT NULL, next_due REAL NOT NULL, run_id TEXT NOT NULL, result_code TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS video_retention_refresh_lookup_time_v1
                ON video_retention_refresh_lookups_v1(job_id,started_at);""")

    def _authorization(self, ident):
        from .video_source_retention_v2 import PUBLISHED
        value = self.retention._authorization(ident)
        if (value['scope'] != PUBLISHED or value['target_site_uid'] != str(self.settings.config['target_bilibili_site_uid'])):
            fail('VIDEO_RETENTION_REFRESH_OWNER_INSTRUCTION_REQUIRED')
        return value

    def _assert_run(self):
        if self._run_id is None or self._lost is not None:
            fail('VIDEO_RETENTION_REFRESH_LEASE_LOST')
        rows = self.store.query('SELECT * FROM video_retention_refresh_runs_v1 WHERE operation_id=?', (self._run_id,))
        if (len(rows) != 1 or rows[0]['state'] != 'processing' or rows[0]['lease_until'] <= self.clock() or
                rows[0]['authorization_id'] != self._authorization_id or
                rows[0]['authorization_sha256'] != self._authorization_sha or
                digest(self._authorization(self._authorization_id)) != self._authorization_sha):
            fail('VIDEO_RETENTION_REFRESH_LEASE_LOST')

    def _pulse(self, stop):
        while not stop.wait(self.heartbeat_interval):
            try:
                now = self.clock()
                with self.store.transaction() as db:
                    changed = db.execute("""UPDATE video_retention_refresh_runs_v1 SET heartbeat_at=?,lease_until=?
                        WHERE operation_id=? AND state='processing' AND lease_until>?""",
                        (now, now + LEASE_SECONDS, self._run_id, now)).rowcount
                if changed != 1:
                    self._lost = 'lease_changed'
                    return
                self.store.beat(HEARTBEAT)
            except Exception:
                self._lost = 'heartbeat_unavailable'
                return

    @contextmanager
    def _run(self, authorization_id):
        if self._run_id is not None:
            fail('VIDEO_RETENTION_REFRESH_RUN_ALREADY_BOUND')
        authorization = self._authorization(authorization_id)
        self.initialize()
        ident, now = str(uuid.uuid4()), self.clock()
        with self.store.transaction() as db:
            if db.execute("SELECT 1 FROM video_retention_refresh_runs_v1 WHERE state='processing' AND lease_until>?", (now,)).fetchone():
                fail('VIDEO_RETENTION_REFRESH_RUN_BUSY')
            db.execute("UPDATE video_retention_refresh_runs_v1 SET state='interrupted',phase='reconciliation' WHERE state='processing' AND lease_until<=?", (now,))
            db.execute('INSERT INTO video_retention_refresh_runs_v1 VALUES(?,?,?,?,?,?,?,?,?)',
                (ident, authorization_id, digest(authorization), 'processing', 'local_scan', now, now, now + LEASE_SECONDS, '{}'))
        self._run_id, self._authorization_id, self._authorization_sha = ident, authorization_id, digest(authorization)
        self._lost = None
        stop = threading.Event()
        thread = threading.Thread(target=self._pulse, args=(stop,), daemon=True)
        thread.start()
        outcome = {'state': 'interrupted', 'error_code': 'VIDEO_RETENTION_MAINTENANCE_INTERRUPTED'}
        try:
            self.store.beat(HEARTBEAT)
            self._assert_run()
            yield ident
            self._assert_run()
            outcome = {'state': 'completed'}
        except HubError as exc:
            outcome = {'state': 'failed', 'error_code': exc.code}
            raise
        finally:
            stop.set()
            thread.join(timeout=20)
            try:
                with self.store.transaction() as db:
                    db.execute('UPDATE video_retention_refresh_runs_v1 SET state=?,phase=?,heartbeat_at=?,lease_until=?,result=? WHERE operation_id=?',
                        (outcome['state'], 'finished', self.clock(), self.clock(), encode(outcome), ident))
            finally:
                self._run_id = self._authorization_id = self._authorization_sha = None
                self._lost = None

    def _admit(self, job_id):
        self._assert_run()
        job, binding, state, source, publication = self.retention._context(job_id)
        service, manifest_builder = self.route_factory(self.settings, self.store, job)
        policy = service.policy()
        previous = state.get('result_observation', {})
        raw = source['payload'].get('raw', {})
        if (job['job_type'] != service.JOB_TYPE or job['policy_version'] != service.POLICY or
                job['state'] != 'completed' or state.get('state') != 'completed' or
                not _uuid(state.get('attempt_id')) or previous.get('attempt_id') != state['attempt_id'] or
                previous.get('version') != service.RESULT_OBSERVATION_VERSION or
                previous.get('state') != 'completed' or previous.get('public_accessible') is not True or
                not re.fullmatch('BV[A-Za-z0-9]{10}', previous.get('external_id', '')) or
                publication['external_id'] != previous['external_id'] or
                binding['site_uid'] != str(self.settings.config['target_bilibili_site_uid']) or
                policy['target_site_uid'] != binding['site_uid'] or policy['target_account_name'] != binding['account_name'] or
                policy['_sha256'] != binding['policy_sha256'] or binding.get('human_review') is not False or
                binding['channel_id'] not in CHANNELS or set(policy['allowed_channel_ids']) != CHANNELS or
                source['payload'].get('source_api') != 'youtube-data-v3-official' or
                raw.get('id') != binding['source_id'] or raw.get('snippet', {}).get('channelId') != binding['channel_id'] or
                binding['metadata_sha256'] != digest(service._metadata(binding['metadata'], binding['source_url'])) or
                state.get('owner_authorized_remote_deletion_v1', {}).get('remote_state') == 'owner_deleted'):
            fail('VIDEO_RETENTION_REFRESH_COMPLETED_CAUSAL_BINDING_REQUIRED')
        return service, manifest_builder, job, binding, state, source

    def _reserve_lookup(self, job, binding, state):
        self._assert_run()
        now = self.clock()
        day = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        ident = str(uuid.uuid4())
        with self.store.transaction() as db:
            rows = db.execute('SELECT started_at FROM video_retention_refresh_lookups_v1 WHERE job_id=? ORDER BY started_at DESC', (job['job_id'],)).fetchall()
            if rows and now - rows[0]['started_at'] < COOLDOWN_SECONDS:
                fail('VIDEO_RETENTION_REFRESH_COOLDOWN')
            if sum(row['started_at'] >= day for row in rows) >= PER_JOB_DAILY_LIMIT:
                fail('VIDEO_RETENTION_REFRESH_JOB_DAILY_LIMIT')
            if db.execute('SELECT COUNT(*) FROM video_retention_refresh_lookups_v1 WHERE started_at>=?', (day,)).fetchone()[0] >= GLOBAL_DAILY_LIMIT:
                fail('VIDEO_RETENTION_REFRESH_GLOBAL_DAILY_LIMIT')
            db.execute('INSERT INTO video_retention_refresh_lookups_v1 VALUES(?,?,?,?,?,?,?,?,?)',
                (ident, self._run_id, job['job_id'], digest(binding), state['result_observation']['external_id'],
                 'lookup_reserved', now, None, '{}'))
            db.execute("UPDATE video_retention_refresh_runs_v1 SET phase='native_public_lookup' WHERE operation_id=?", (self._run_id,))
        return ident

    def _scan_started(self, ident):
        self._assert_run()
        now = self.clock()
        with self.store.transaction() as db:
            db.execute("""INSERT INTO video_retention_scan_windows_v1b VALUES(?,?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET last_scan=excluded.last_scan,next_due=excluded.next_due,
                run_id=excluded.run_id,result_code=excluded.result_code""",
                (ident, now, now + SCAN_RETRY_SECONDS, self._run_id, 'scan_reserved'))

    def _scan_failed(self, ident, code):
        with self.store.transaction() as db:
            db.execute('UPDATE video_retention_scan_windows_v1b SET result_code=? WHERE job_id=? AND run_id=?',
                (code, ident, self._run_id))

    def _select_candidates(self, rows):
        self._assert_run()
        now = self.clock()
        day = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        scans = {row['job_id']: row for row in self.store.query('SELECT * FROM video_retention_scan_windows_v1b')}
        lookups = self.store.query('SELECT job_id,started_at FROM video_retention_refresh_lookups_v1')
        global_used = sum(row['started_at'] >= day for row in lookups)
        accepted = []
        for order, row in enumerate(rows):
            ident = row['job_id']
            if json.loads(row['result']).get('local_original_retention_v2'):
                continue
            attempts = [item['started_at'] for item in lookups if item['job_id'] == ident]
            record = scans.get(ident, {})
            code = None
            if global_used >= GLOBAL_DAILY_LIMIT:
                code = 'VIDEO_RETENTION_REFRESH_GLOBAL_DAILY_LIMIT'
            elif sum(at >= day for at in attempts) >= PER_JOB_DAILY_LIMIT:
                code = 'VIDEO_RETENTION_REFRESH_JOB_DAILY_LIMIT'
            elif attempts and now - max(attempts) < COOLDOWN_SECONDS:
                code = 'VIDEO_RETENTION_REFRESH_COOLDOWN'
            elif record.get('next_due', 0) > now:
                code = 'VIDEO_RETENTION_REFRESH_SCAN_COOLDOWN'
            if code:
                self._pre_scan_deferred.append({'job_id': ident, 'error_code': code})
            else:
                accepted.append((record.get('last_scan', 0), order, row))
        return [row for _, _, row in sorted(accepted, key=lambda item: (item[0], item[1]))]

    def _refresh(self, job_id):
        self._assert_run()
        # Never hold this queue lock across local FFmpeg/hash scans. The browser
        # acquires the same profile then heavy locks as the normal worker. Both
        # roles must be idle; a busy lock simply retains the local source.
        locks = self.settings.root / 'data/locks'
        with resource_guard(locks/'video', locks/'moderation'):
            if self.store.query("SELECT 1 FROM jobs WHERE queue='video' AND state='processing' LIMIT 1"):
                fail('VIDEO_RETENTION_REFRESH_WORKER_ACTIVE')
            service, builder, job, binding, state, source = self._admit(job_id)
            ident = None
            fingerprint, source_fingerprint = digest(job), digest(source)
            def guard():
                # Reopen policy/source/publication as well as the parent row.
                # A concurrent policy or immutable-source change cannot inherit
                # an earlier admission while the native lookup is in flight.
                _, _, current, _, _, current_source = self._admit(job_id)
                if digest(current) != fingerprint or digest(current_source) != source_fingerprint:
                    fail('VIDEO_RETENTION_REFRESH_PARENT_CHANGED')
            duration = source['result'][service.DOWNLOAD_STATE]['binding']['duration']
            manifest = builder(self.settings, self.store, job, binding, state, state['attempt_id'], duration_seconds=duration, require_cover=False)
            result = {'state': 'lookup_unknown', 'error_code': 'VIDEO_RETENTION_REFRESH_LOOKUP_UNKNOWN'}
            def before_launch():
                nonlocal ident
                guard()
                ident = self._reserve_lookup(job, binding, state)
            try:
                guard()
                with self.browser.session(manifest, assert_lease=guard, before_launch=before_launch) as session:
                    value = session.operation('lookup', external_id=state['result_observation']['external_id'])
                    guard()
                    observation = value.get('result_observation')
                    if not isinstance(observation, dict):
                        fail('VIDEO_RETENTION_REFRESH_LOOKUP_UNKNOWN')
                    recorded = service.record_completed_refresh(job_id, observation, guard=guard)
                result = {'state': 'refreshed', 'recorded': recorded,
                    'native_lookup_only': True, 'public_submit_performed': False,
                    'file_selection_performed': False, 'parent_attempts_reset': False}
                return result
            except HubError as exc:
                result = {'state': 'lookup_failed', 'error_code': exc.code}
                raise
            finally:
                if ident is not None:
                    with self.store.transaction() as db:
                        db.execute('UPDATE video_retention_refresh_lookups_v1 SET state=?,finished_at=?,result=? WHERE operation_id=?',
                            (result['state'], self.clock(), encode(result), ident))

    def refresh_once(self, job_id, *, authorization_id):
        with resource_guard(self.settings.root/'data/locks/video-retention-maintenance-v1'):
            with self._run(authorization_id):
                return self._refresh(job_id)

    def run_once(self, authorization_id, *, apply=False, max_jobs=1):
        from .video_retention_scheduler_v2 import VideoRetentionCompletedSchedulerV2
        with resource_guard(self.settings.root/'data/locks/video-retention-maintenance-v1'):
            with self._run(authorization_id) as ident:
                if type(apply) is not bool or type(max_jobs) is not int or not 1 <= max_jobs <= 3:
                    fail('VIDEO_RETENTION_SCHEDULER_ARGUMENT_REJECTED')
                self._pre_scan_deferred = []
                bounded = _BoundedRetention(self.retention, max_jobs, self._assert_run,
                    on_scan=self._scan_started, on_failure=self._scan_failed)
                view = _FairCandidateView(self.store, self._select_candidates)
                scheduler = VideoRetentionCompletedSchedulerV2(self.settings, view, retention=bounded)
                accumulated = {}
                try:
                    report = scheduler.run_once(authorization_id, apply=apply, max_jobs=max_jobs,
                        refresh_public_observation=self._refresh, report_accumulator=accumulated)
                except _CandidateWindowFull:
                    # A partial window can contain committed successes followed
                    # by failed candidates. Preserve the old scheduler's exact
                    # entries, including dry plans and resumed pending intents.
                    # Original per-file intents remain the recovery truth.
                    from .archive_source_retention_v2 import _atomic, _path
                    report = {'operation_id': str(uuid.uuid4()), 'created_at': datetime.now(timezone.utc).isoformat(),
                        'authorization_id': authorization_id, 'apply': apply, 'max_jobs': max_jobs,
                        'processed': accumulated['processed'], 'retained': accumulated['retained'], 'state': 'candidate_window_exhausted',
                        'publication_records_removed': False, 'source_id_records_removed': False, 'remote_video_deleted': False}
                    folder = _path(self.settings.root, self.retention.folder/'reports')
                    folder.mkdir(mode=0o700, exist_ok=True)
                    file = folder/(report['operation_id']+'.json')
                    _atomic(file, report)
                    report['report_file'] = str(file)
                return {**report, 'version': VERSION, 'maintenance_operation_id': ident,
                    'candidate_jobs_attempted': len(bounded.seen), 'candidate_window_maximum': max_jobs,
                    'pre_scan_deferred': self._pre_scan_deferred}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--authorization-id', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--max-jobs', type=int, default=1, choices=(1, 2, 3))
    args = parser.parse_args()
    import os
    if os.name != 'posix' or os.getuid() != 10001 or os.getgid() != 10001:
        fail('VIDEO_RETENTION_REFRESH_APP_IDENTITY_REQUIRED')
    from .config import Settings
    from .db import Store
    settings = Settings.load()
    store = Store(settings.root/'data/hub.sqlite3')
    # No Store.init, recover, claim, enqueue or Worker thread is called here.
    report = CompletedVideoMaintenanceV1(settings, store).run_once(args.authorization_id, apply=args.apply, max_jobs=args.max_jobs)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
