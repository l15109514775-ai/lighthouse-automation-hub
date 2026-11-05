"""Real OS lock contention must not spend the network retry budget."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from conftest import enqueue
from services.hub.db import Store, encode
from services.hub.errors import HubError
from services.hub.locks import exclusive, resource_guard
from services.hub.worker import Worker


def due(store, ident):
    with store.transaction() as db:
        db.execute('UPDATE jobs SET next_run=0 WHERE job_id=?', (ident,))


def publication(store, ident, *, state='dispatching', external_id=None):
    with store.transaction() as db:
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('owned-' + ident, ident, 'bilibili', 'x', '123', 'owned-test-only', 'image', state,
             external_id, None, time.time()))


def test_long_video_heavy_lock_defers_tweet_more_than_four_times_and_then_completes(runtime):
    settings, store = runtime
    ident = enqueue(store)
    acquired = []

    def render(job):
        with resource_guard(settings.root / 'data/locks/heavy'):
            acquired.append(job['attempts'])
            return {'owned_render_complete': True}

    with exclusive(settings.root / 'data/locks/heavy'):
        for _ in range(7):
            reopened = Store(settings.db_path)
            before = time.time()
            assert Worker(settings, reopened, {'test': render}).once('tweet')
            row = reopened.get(ident)
            assert row['state'] == 'retry_scheduled' and row['error_code'] == 'HEAVY_RESOURCE_BUSY'
            assert row['attempts'] == 0 and 14 < row['next_run'] - before < 32
            assert row['lease_owner'] is None and row['lease_until'] is None
            due(reopened, ident)
    assert not acquired
    assert Worker(settings, Store(settings.db_path), {'test': render}).once('tweet')
    assert store.get(ident)['state'] == 'completed' and store.get(ident)['attempts'] == 1
    assert acquired == [1]
    assert not store.query('SELECT * FROM publications')


def test_resource_wait_preserves_partial_checkpoint_and_does_not_renew_lease(runtime):
    settings, store = runtime
    ident = enqueue(store)
    captured = []

    def handler(job):
        store.checkpoint(job, 'owned_existing_checkpoint', {'hash': 'owned-existing-hash'})
        captured.append(job)
        with resource_guard(settings.root / 'data/locks/heavy'):
            pytest.fail('lock is already held')

    with exclusive(settings.root / 'data/locks/heavy'):
        assert Worker(settings, store, {'test': handler}).once('tweet')
    row = store.get(ident)
    assert row['result'] == {'hash': 'owned-existing-hash'} and row['stage'] == 'owned_existing_checkpoint'
    assert captured[0]['attempts'] == row['attempts'] == 0
    assert captured[0]['state'] == row['state'] == 'retry_scheduled'
    assert captured[0]['lease_owner'] is None and captured[0]['lease_until'] is None


def test_four_real_network_errors_still_exhaust_after_any_number_of_resource_waits(runtime):
    settings, store = runtime
    ident = enqueue(store)
    networks = []

    def fail(job):
        with resource_guard(settings.root / 'data/locks/heavy'):
            networks.append(job['attempts'])
            raise HubError('OWNED_NETWORK_ERROR', retryable=True)

    for attempt in range(1, settings.config['max_attempts'] + 1):
        with exclusive(settings.root / 'data/locks/heavy'):
            for _ in range(3):
                assert Worker(settings, Store(settings.db_path), {'test': fail}).once('tweet')
                assert store.get(ident)['attempts'] == attempt - 1
                due(store, ident)
        assert Worker(settings, Store(settings.db_path), {'test': fail}).once('tweet')
        row = store.get(ident)
        assert row['attempts'] == attempt
        assert row['state'] == ('failed' if attempt == settings.config['max_attempts'] else 'retry_scheduled')
        due(store, ident)
    assert networks == [1, 2, 3, 4]
    assert not Worker(settings, store, {'test': fail}).once('tweet')


@pytest.mark.parametrize('change', ['cancelled', 'owner', 'expired'])
def test_resource_defer_cannot_overwrite_cancellation_recovery_or_expired_lease(runtime, change):
    settings, store = runtime
    ident = enqueue(store)
    job = store.claim('tweet', 60, 4)
    with store.transaction() as db:
        if change == 'cancelled':
            db.execute("UPDATE jobs SET state='failed',error_code='OWNER_BACKLOG_CLEARED',lease_owner=NULL,lease_until=NULL WHERE job_id=?", (ident,))
        elif change == 'owner':
            db.execute("UPDATE jobs SET lease_owner='new-owner' WHERE job_id=?", (ident,))
        else:
            db.execute('UPDATE jobs SET lease_until=? WHERE job_id=?', (time.time() - 1, ident))
    before = store.get(ident)
    assert Worker(settings, store, {})._defer_resource_busy(job) is False
    assert store.get(ident) == before


@pytest.mark.parametrize('state,external_id', [('dispatching', None), ('unknown', None),
    ('awaiting_moderation', 'BVowned'), ('completed', 'BVowned'), ('rejected', 'BVowned')])
def test_existing_submission_never_enters_free_resource_retry(runtime, state, external_id):
    settings, store = runtime
    ident = enqueue(store)

    def busy(job):
        publication(store, ident, state=state, external_id=external_id)
        raise HubError('HEAVY_RESOURCE_BUSY', retryable=True)

    assert Worker(settings, store, {'test': busy}).once('tweet')
    row = store.get(ident)
    assert row['state'] == 'publish_unknown' and row['error_code'] == 'RECONCILE_BEFORE_RETRY'
    assert row['attempts'] == 1
    assert not Worker(settings, store, {'test': busy}).once('tweet')
    assert store.query('SELECT state FROM publications')[0]['state'] == state


def test_machine_child_wait_preserves_parent_unknown_and_cannot_resend(runtime):
    settings, store = runtime
    parent = enqueue(store, source_id='parent-owned')
    publication(store, parent, state='unknown')
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='publish_unknown' WHERE job_id=?", (parent,))
    child = store.enqueue('machine_web', parent, 'tweet_machine_publish', 'tweet',
                          {'parent_job_id': parent}, 'owned-test-policy')

    def busy(job):
        raise HubError('HEAVY_RESOURCE_BUSY', retryable=True)

    assert Worker(settings, store, {'tweet_machine_publish': busy}).once('tweet')
    assert store.get(child)['state'] == 'publish_unknown' and store.get(child)['attempts'] == 1
    assert store.get(parent)['state'] == 'publish_unknown'
    assert not Worker(settings, store, {'tweet_machine_publish': busy}).once('tweet')


def test_resource_wait_still_rechecks_handler_expiry_and_does_not_authorize_work(runtime):
    settings, store = runtime
    ident = enqueue(store)
    expired = False

    def handler(job):
        if expired:
            raise HubError('OWNER_TWO_HOUR_WINDOW_EXPIRED')
        with resource_guard(settings.root / 'data/locks/heavy'):
            pytest.fail('expired work must never reach the resource')

    with exclusive(settings.root / 'data/locks/heavy'):
        assert Worker(settings, store, {'test': handler}).once('tweet')
    expired = True
    due(store, ident)
    assert Worker(settings, store, {'test': handler}).once('tweet')
    row = store.get(ident)
    assert row['state'] == 'failed' and row['error_code'] == 'OWNER_TWO_HOUR_WINDOW_EXPIRED'
    assert row['attempts'] == 1


def test_long_handler_keeps_lease_and_service_heartbeat_live_until_it_returns(runtime):
    settings, store = runtime
    # Use a short real lease so this exercises several renewals without a
    # multi-minute test or a substituted clock/heartbeat implementation.
    settings.config['lease_seconds'] = .9
    ident = enqueue(store, queue='video')
    started, release = threading.Event(), threading.Event()

    def long_download(job):
        started.set()
        assert release.wait(8)
        return {'owned_long_handler_finished': True}

    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(Worker(settings, store, {'test': long_download}).once, 'video')
        try:
            assert started.wait(3)
            initial = store.get(ident)
            initial_beat = store.query("SELECT at FROM heartbeats WHERE name='worker:video'")[0]['at']
            # This exceeds the original lease, while the real handler remains
            # blocked and the real worker heartbeat thread continues running.
            time.sleep(1.2)
            current = Store(settings.db_path).get(ident)
            current_beat = store.query("SELECT at FROM heartbeats WHERE name='worker:video'")[0]['at']
            assert not future.done() and current['state'] == 'processing'
            assert initial['lease_until'] < time.time() < current['lease_until']
            assert current['lease_until'] > initial['lease_until'] and current['lease_owner'] == initial['lease_owner']
            assert current_beat > initial_beat and time.time() - current_beat < .9
        finally:
            release.set()
        assert future.result(timeout=3)
    row = store.get(ident)
    assert row['state'] == 'completed' and row['result']['owned_long_handler_finished']
    assert row['lease_owner'] is None and row['lease_until'] is None


@pytest.mark.parametrize('revoked', ['owner_changed', 'cancelled'])
def test_heartbeat_thread_does_not_advertise_worker_after_it_has_lost_the_job(runtime, revoked):
    settings, store = runtime
    settings.config['lease_seconds'] = .3
    ident = enqueue(store, queue='video')
    job = store.claim('video', .3, settings.config['max_attempts'])
    store.beat('worker:video')
    with store.transaction() as db:
        if revoked == 'owner_changed':
            db.execute("UPDATE jobs SET lease_owner='replacement-owner' WHERE job_id=?", (ident,))
        else:
            db.execute("UPDATE jobs SET state='failed',error_code='OWNER_CANCELLED',lease_owner=NULL,lease_until=NULL WHERE job_id=?", (ident,))
    before = store.get(ident)
    previous_beat = store.query("SELECT at FROM heartbeats WHERE name='worker:video'")[0]['at']
    stop = threading.Event()
    thread = threading.Thread(target=Worker(settings, Store(settings.db_path), {})._heartbeat, args=(job, stop))
    thread.start()
    try:
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert store.get(ident) == before
        assert store.query("SELECT at FROM heartbeats WHERE name='worker:video'")[0]['at'] == previous_beat
    finally:
        stop.set()
        thread.join(timeout=2)


@pytest.mark.parametrize('code', ['AUTH_EXPIRED', 'YOUTUBE_SESSION_AUTH_EXPIRED', 'YOUTUBE_SESSION_UNAVAILABLE'])
def test_expired_youtube_session_stops_after_one_attempt_and_emits_safe_auth_event(runtime, code):
    settings, store = runtime
    ident = enqueue(store, queue='video')
    calls = []

    def session_expired(job):
        calls.append(job['job_id'])
        # Even a mistakenly retryable classification must not repeat login or
        # turn missing/expired account authorization into a network retry.
        raise HubError(code, retryable=True)

    worker = Worker(settings, store, {'test': session_expired})
    assert worker.once('video')
    row = store.get(ident)
    assert row['state'] == 'auth_expired' and row['error_code'] == code and row['attempts'] == 1
    assert row['lease_owner'] is None and row['lease_until'] is None
    due(store, ident)
    assert not worker.once('video') and calls == [ident]
    events = store.query("SELECT name,data FROM events WHERE name='auth.expired'")
    assert events == [{'name': 'auth.expired', 'data': encode({'job_id': ident, 'code': code, 'queue': 'video'})}]
