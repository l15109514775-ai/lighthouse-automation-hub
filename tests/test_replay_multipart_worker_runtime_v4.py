from pathlib import Path as _RecoveryPath
from services.hub.locks import exclusive as _recovery_lock
"""Owned SQLite/Worker/FFmpeg integration; no platform or paid API calls."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import threading
import time

import pytest

from services.hub import replay_multipart_worker_runtime_v4 as rt
from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.replay_fifo_v4 import PLAN_JOB, DOWNLOAD_JOB, REFRESH_JOB, PUBLISH_JOB, VERSION
from services.hub.youtube_readiness_v4 import JOB_TYPE as READINESS_JOB, WholeReadinessObservationV4
from test_replay_fifo_v4 import fifo, seed
from test_youtube_download_v4 import configure_v4, whole
from test_youtube_readiness_v4 import readiness, advance
from test_ytdlp_highest_v2 import tracks


@pytest.fixture
def configured(fifo):
    return configure_v4(fifo)


def enqueue(f, kind, *, platform='youtube', policy=VERSION, source=None):
    return f.db.enqueue(platform, source or 'Owned-' + kind, kind, 'video', {}, policy)


def test_only_four_v4_handlers_and_explicit_wait_callbacks_are_registered(configured):
    f = configured
    worker, coordinator = rt.build_worker(f.s, f.db, mode='acquisition_only')
    assert set(worker.handlers) == {PLAN_JOB, DOWNLOAD_JOB, REFRESH_JOB, READINESS_JOB}
    assert set(worker.resource_deferrals) == {PLAN_JOB, DOWNLOAD_JOB}
    assert set(worker.readiness_deferrals) == {PLAN_JOB}
    assert set(coordinator.validators) == {PLAN_JOB, DOWNLOAD_JOB}
    assert rt.PUBLICATION_ENABLED is False and PUBLISH_JOB not in worker.handlers
    assert not f.db.query("SELECT name FROM sqlite_master WHERE name='youtube_readiness_waits_v4'")
    assert not f.db.query('SELECT * FROM jobs')


def test_runtime_binds_both_heartbeat_paths_to_its_own_boot(configured):
    from services.hub.acquisition_health import instance_names
    f = configured
    worker, coordinator = rt.build_worker(f.s, f.db, mode='acquisition_only')
    coordinator.bind_health('a' * 32)
    coordinator.tick()
    coordinator.idle_heartbeat()
    names = instance_names('a' * 32)
    rows = dict((r['name'], r['at']) for r in f.db.query('SELECT * FROM heartbeats'))
    assert all(name in rows for name in names)
    before = rows[names[0]]
    worker.store.beat('worker:video')
    assert f.db.query('SELECT at FROM heartbeats WHERE name=?', (names[0],))[0]['at'] >= before
    with pytest.raises(ValueError, match='ALREADY_BOUND'):
        coordinator.bind_health('b' * 32)
    assert not f.db.query('SELECT * FROM publications')


@pytest.mark.parametrize('kind', [PLAN_JOB, DOWNLOAD_JOB, READINESS_JOB, REFRESH_JOB])
def test_claim_accepts_real_v4_platform_policy_only_and_does_not_consume_legacy(configured, kind):
    f = configured
    old = enqueue(f, 'replay_highest_video_publish_v2', policy='Owned legacy')
    wrong_policy = enqueue(f, kind, policy='Owned wrong policy', source='wrong-policy')
    wrong_platform = enqueue(f, kind, platform='bilibili', source='wrong-platform')
    publisher = enqueue(f, PUBLISH_JOB)
    wanted = enqueue(f, kind, platform='maintenance' if kind == REFRESH_JOB else 'youtube', source='wanted')
    before = {i: f.db.get(i) for i in (old, wrong_policy, wrong_platform, publisher)}
    view = rt.QueueView(f.db, acquisition_enabled=True)
    claimed = view.claim('video', 60, 4)
    assert claimed['job_id'] == wanted and claimed['attempts'] == 1
    assert claimed['state'] == 'processing' and claimed['lease_until'] > time.time()
    assert all(f.db.get(i) == row for i, row in before.items())
    assert view.claim('video', 60, 4) is None


def test_recovery_never_touches_old_processing_unknown_or_unhandled_publish_rows(configured):
    f = configured
    old = enqueue(f, 'youtube_download_v3', policy='Owned legacy')
    publish = enqueue(f, PUBLISH_JOB)
    own = enqueue(f, PLAN_JOB)
    with f.db.transaction() as db:
        for ident in (old, publish, own):
            db.execute("UPDATE jobs SET state='processing',attempts=1,lease_owner='Owned crashed',lease_until=0 WHERE job_id=?", (ident,))
    before = {i: f.db.get(i) for i in (old, publish)}
    view = rt.QueueView(f.db, acquisition_enabled=True)
    with _recovery_lock(_RecoveryPath(view.path).parent / "locks" / 'video'):
        assert view.recover('video', 4, all_processing=True) == 1
    recovered = f.db.get(own)
    assert recovered['state'] == 'retry_scheduled' and recovered['attempts'] == 1
    assert recovered['error_code'] == 'PROCESS_INTERRUPTED' and recovered['lease_owner'] is None
    assert all(f.db.get(i) == row for i, row in before.items())


def test_recovery_of_reserved_v4_source_is_unknown_without_retransmission(configured):
    f = configured
    ident = enqueue(f, DOWNLOAD_JOB)
    with f.db.transaction() as db:
        db.execute("UPDATE jobs SET state='processing',attempts=1,lease_owner='Owned',lease_until=0 WHERE job_id=?", (ident,))
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('Owned-key', ident, 'bilibili', 'youtube', 'Owned-source', 'site_uid:Owned', 'Owned-kind', 'unknown', None, None, time.time()))
    publications = f.db.query('SELECT * FROM publications')
    view = rt.QueueView(f.db, acquisition_enabled=True)
    with _recovery_lock(_RecoveryPath(view.path).parent / "locks" / 'video'):
        assert view.recover('video', 4, all_processing=True) == 1
    assert f.db.get(ident)['state'] == 'publish_unknown'
    assert view.claim('video', 60, 4) is None
    assert f.db.query('SELECT * FROM publications') == publications


def test_wrong_queue_and_heartbeat_names_are_rejected_without_database_changes(configured):
    f = configured
    view = rt.QueueView(f.db, acquisition_enabled=True)
    before = f.db.query('SELECT * FROM heartbeats')
    for operation in (lambda: view.claim('tweet', 60, 4), lambda: view.recover('moderation', 4), lambda: view.beat('worker:tweet')):
        with pytest.raises(ValueError):
            operation()
    assert f.db.query('SELECT * FROM heartbeats') == before


def test_paused_pipeline_still_registers_all_backlog_without_claim_or_dispatch(configured):
    f = configured
    for index in range(26):
        seed(f, index)
    with f.db.transaction() as db:
        db.execute("UPDATE pipelines SET paused=1 WHERE name='video'")
    worker, coordinator = rt.build_worker(f.s, f.db, mode='acquisition_only')
    result = coordinator.tick()
    assert len(result['steps']['register']['registered']) == 26 and result['dispatch_state'] == 'paused'
    assert not f.db.query('SELECT * FROM jobs') and worker.once('video') is False
    assert not f.db.query('SELECT * FROM api_calls') and not f.db.query('SELECT * FROM publications')


def test_producer_is_local_registers_plan_and_does_not_dispatch_publication(configured):
    f = configured
    raw, info = seed(f, 1)
    worker, coordinator = rt.build_worker(f.s, f.db, mode='acquisition_only')
    result = coordinator.tick()
    assert result['external_requests'] == result['publications'] == 0
    assert len(result['steps']['plan']['created']) == 1
    job = f.db.get(result['steps']['plan']['created'][0])
    assert job['source_id'] == raw['id'] and job['attempts'] == 0
    assert not f.db.query('SELECT * FROM api_calls') and not f.db.query('SELECT * FROM publications')
    assert PUBLISH_JOB not in worker.handlers


def test_changed_runtime_policy_stops_before_any_new_registration_or_dispatch(configured):
    f = configured
    _, coordinator = rt.build_worker(f.s, f.db, mode='acquisition_only')
    seed(f, 1)
    file = f.s.root / 'config/replay-fifo-policy-v4.json'
    policy = json.loads(file.read_bytes())
    policy['owner_queue_reference'] += ' changed'
    file.write_text(encode(policy), encoding='utf-8')
    with pytest.raises(HubError, match='RUNTIME_POLICY_CHANGED'):
        coordinator.tick()
    assert not f.db.query('SELECT * FROM jobs')


def test_real_worker_owned_merge_and_parts_complete_but_publisher_stays_closed(whole):
    f = whole
    worker, coordinator = rt.build_worker(f.s, f.db, mode='acquisition_only')
    # The fixture owns a genuine pre-crash lease. Recovery preserves that
    # attempt before the ordinary Worker claims its next real lease.
    before = f.db.get(f.job['job_id'])
    worker.handlers[DOWNLOAD_JOB] = f.service.process  # owned transport copies owned tracks, then real FFmpeg
    assert worker.once('video') is True
    completed = f.db.get(f.job['job_id'])
    assert completed['state'] == 'completed' and completed['attempts'] == before['attempts'] + 1
    assert completed['result']['youtube_download_v4']['state'] == 'parts_verified'
    assert completed['result']['youtube_download_v4']['publishable'] is False
    assert f.calls == ['owned_track_copy_not_youtube']
    coordinator.tick()
    assert not f.db.query('SELECT * FROM jobs WHERE job_type=?', (PUBLISH_JOB,))
    assert not f.db.query('SELECT * FROM publications')


def test_real_readiness_callback_and_numbered_child_complete_parent_without_download_bypass(readiness, monkeypatch):
    f = readiness
    worker, coordinator = rt.build_worker(f.s, f.db, mode='acquisition_only')
    coordinator.readiness = f.coordinator
    worker.readiness_deferrals = f.coordinator.callbacks()
    worker.handlers[PLAN_JOB] = f.parent_service.process  # explicit owned extractor; real same ledger/raw files
    child = WholeReadinessObservationV4(f.s, f.db, coordinator=f.coordinator, downloader_factory=f.factory)
    worker.handlers[READINESS_JOB] = child.process
    assert worker.once('video') is True
    parent = f.db.get(f.parent_id)
    assert parent['state'] == 'retry_scheduled' and parent['attempts'] == 1
    advance(f, monkeypatch)
    f.ready = True
    result = coordinator.tick()
    assert len(result['steps']['readiness']['enqueued']) == 1
    assert worker.once('video') is True
    parent = f.db.get(f.parent_id)
    assert parent['state'] == 'completed' and parent['attempts'] == 1
    assert not f.db.query('SELECT * FROM replay_fifo_dispatches_v4')
    assert not f.db.query('SELECT * FROM publications')


def test_producer_keeps_registering_while_actual_worker_lease_and_heartbeat_are_live(configured, monkeypatch):
    f = configured
    f.s.config['lease_seconds'] = 1
    raw, info = seed(f, 1)
    worker, coordinator = rt.build_worker(f.s, f.db, mode='acquisition_only')
    started, release, stop = threading.Event(), threading.Event(), threading.Event()
    leases = []
    real_heartbeat = f.db.heartbeat
    def heartbeat(job, seconds):
        ok = real_heartbeat(job, seconds)
        if ok:
            leases.append(f.db.get(job['job_id']))
        return ok
    monkeypatch.setattr(f.db, 'heartbeat', heartbeat)
    monkeypatch.setattr(rt, 'PRODUCER_INTERVAL', 0.03)
    def owned_blocked_phase(job):
        started.set()
        assert release.wait(5)
        from services.hub.replay_multipart_v4 import plan_whole_replay
        plan = plan_whole_replay(raw, info, observed_at=datetime.now(timezone.utc).isoformat(), policy=f.whole)
        result = coordinator.fifo.record_plan(job, plan, f.whole)
        stop.set()
        return result
    worker.handlers[PLAN_JOB] = owned_blocked_phase
    errors = []
    real_producer = rt.producer_loop
    monkeypatch.setattr(rt, 'producer_loop', lambda coordinator, stopping:
        real_producer(coordinator, stopping, fatal=lambda code: errors.append(('producer_exit', code))))
    real_tick = coordinator.tick
    def tick():
        try:
            return real_tick()
        except BaseException as exc:
            errors.append(('producer_error', type(exc).__name__, getattr(exc, 'code', None)))
            raise
    monkeypatch.setattr(coordinator, 'tick', tick)
    def run():
        try:
            rt.run_loop(worker, coordinator, stop=stop)
        except BaseException as exc:
            errors.append(type(exc).__name__)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert started.wait(3)
        newer, _ = seed(f, 2)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            rows = f.db.query('SELECT source_id FROM replay_fifo_v4 WHERE source_id=?', (newer['id'],))
            if rows and leases:
                break
            time.sleep(0.02)
        assert rows and leases
        assert all(row['state'] == 'processing' and row['attempts'] == 1 and row['lease_owner'] for row in leases)
        assert {row['name'] for row in f.db.query('SELECT * FROM heartbeats')} >= {
            'worker:video', 'worker:video-main-v4', 'worker:video-producer-v4'}
    finally:
        release.set()
        stop.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert not f.db.query('SELECT * FROM publications')


def test_producer_failure_requests_restart_instead_of_hidden_stale_health():
    stop = threading.Event()
    class Broken:
        def tick(self):
            raise HubError('Owned_FAILURE')
    class ImmediateStop:
        def wait(self, seconds):
            return False
        def set(self):
            stop.set()
    exits = []
    rt.producer_loop(Broken(), ImmediateStop(), fatal=exits.append)
    assert exits == [70] and stop.is_set()


def test_default_register_only_needs_no_acquisition_policy_and_registers_without_jobs_or_http(fifo):
    f = fifo
    for index in range(25):
        seed(f, index)
    # No whole-download policy/config enable or credentials are installed.
    worker, coordinator = rt.build_worker(f.s, f.db)
    result = coordinator.tick()
    assert result['mode'] == result['dispatch_state'] == 'register_only'
    assert set(result['steps']) == {'sync', 'register'}
    assert len(result['steps']['register']['registered']) == 25
    assert not f.db.query('SELECT * FROM jobs') and not f.db.query('SELECT * FROM api_calls')
    assert not f.db.query('SELECT * FROM publications')
    assert worker.store.acquisition_enabled is False


def test_register_only_cannot_claim_recover_or_run_existing_v4_work(fifo, monkeypatch):
    f = fifo
    pending = enqueue(f, PLAN_JOB)
    processing = enqueue(f, DOWNLOAD_JOB)
    with f.db.transaction() as db:
        db.execute("UPDATE jobs SET state='processing',attempts=1,lease_owner='Owned other consumer',lease_until=0 WHERE job_id=?", (processing,))
    before = {i: f.db.get(i) for i in (pending, processing)}
    worker, coordinator = rt.build_worker(f.s, f.db)
    monkeypatch.setattr(worker, 'once', lambda *args: pytest.fail('register-only called Worker.once'))
    assert worker.store.claim('video', 60, 4) is None
    with _recovery_lock(_RecoveryPath(worker.store.path).parent / "locks" / 'video'):
        assert worker.store.recover('video', 4, all_processing=True) == 0
    assert rt.run_loop(worker, coordinator, once=True) is False
    assert all(f.db.get(i) == value for i, value in before.items())
    assert not f.db.query('SELECT * FROM api_calls')


def test_register_only_continues_later_observations_and_rejects_implicit_publish_mode(fifo):
    f = fifo
    _, coordinator = rt.build_worker(f.s, f.db)
    one, _ = seed(f, 1)
    assert len(coordinator.tick()['steps']['register']['registered']) == 1
    two, _ = seed(f, 2)
    assert len(coordinator.tick()['steps']['register']['registered']) == 1
    assert {row['source_id'] for row in f.db.query('SELECT * FROM replay_fifo_v4')} == {one['id'], two['id']}
    with pytest.raises(ValueError, match='MODE_INVALID'):
        rt.build_worker(f.s, f.db, mode='publish')
    assert not f.db.query('SELECT * FROM jobs')
