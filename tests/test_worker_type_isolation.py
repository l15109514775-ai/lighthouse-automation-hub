"""Real SQLite and OS locks: no provider, browser or posting calls."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time

import pytest

from services.hub.db import Store
from services.hub.job_scope import job_type_filter
from services.hub.locks import exclusive
from services.hub.worker import Worker
from services.hub.replay_multipart_worker_runtime_v4 import (
    QueueView, PLAN_JOB, DOWNLOAD_JOB, FIFO_VERSION)


def add(store, kind, *, policy=FIFO_VERSION, source=None):
    return store.enqueue('youtube', source or kind, kind, 'video', {}, policy)


@pytest.mark.parametrize('scope', [(), [], frozenset()])
def test_empty_whitelist_neither_claims_nor_recovers(runtime, scope):
    settings, store = runtime
    ident = add(store, PLAN_JOB)
    before = store.get(ident)
    assert store.claim('video', 60, 4, job_types=scope) is None
    assert store.get(ident) == before
    job = store.claim('video', 60, 4)
    with exclusive(settings.root / 'data/locks/video'):
        assert store.recover('video', 4, all_processing=True, job_types=scope) == 0
    assert store.get(ident) == job


@pytest.mark.parametrize('scope', ['youtube_download', ("a') OR 1=1--",), (None,), ('a',) * 257])
def test_invalid_whitelist_is_rejected_before_sql(runtime, scope):
    _, store = runtime
    with pytest.raises(ValueError, match='JOB_TYPES_INVALID'):
        store.claim('video', 60, 4, job_types=scope)
    with pytest.raises(ValueError, match='JOB_TYPES_INVALID'):
        store.recover('video', 4, job_types=scope)


def test_two_connections_claim_disjoint_types_once(runtime):
    _, store = runtime
    ids = [add(store, kind) for kind in ('legacy', PLAN_JOB)]
    def claim(kind):
        return Store(store.path).claim('video', 60, 4, job_types=(kind,))
    with ThreadPoolExecutor(8) as pool:
        rows = list(pool.map(claim, ['legacy', PLAN_JOB] * 12))
    actual = [r for r in rows if r]
    assert {r['job_id'] for r in actual} == set(ids)
    assert len(actual) == 2
    assert all(r['attempts'] == 1 for r in actual)


def test_forced_recovery_requires_owning_thread_and_exact_queue_lock(runtime):
    settings, store = runtime
    ident = add(store, PLAN_JOB)
    store.claim('video', 60, 4)
    with pytest.raises(RuntimeError, match='QUEUE_LOCK_REQUIRED'):
        store.recover('video', 4, all_processing=True)
    with exclusive(settings.root / 'data/locks/tweet'):
        with pytest.raises(RuntimeError, match='QUEUE_LOCK_REQUIRED'):
            store.recover('video', 4, all_processing=True)
    with exclusive(settings.root / 'data/locks/video'):
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(store.recover, 'video', 4, all_processing=True)
            with pytest.raises(RuntimeError, match='QUEUE_LOCK_REQUIRED'):
                future.result()
        assert store.recover('video', 4, all_processing=True, job_types=(PLAN_JOB,)) == 1
    assert store.get(ident)['attempts'] == 1


def test_v4_scope_is_intersection_not_replacement(runtime):
    settings, store = runtime
    wrong_policy = add(store, PLAN_JOB, policy='legacy')
    legacy = add(store, 'legacy')
    wanted = add(store, PLAN_JOB)
    other = add(store, DOWNLOAD_JOB)
    view = QueueView(store, acquisition_enabled=True)
    assert view.claim('video', 60, 4, job_types=('legacy',)) is None
    row = view.claim('video', 60, 4, job_types=(PLAN_JOB,))
    assert row['job_id'] == wanted
    with exclusive(settings.root / 'data/locks/video'):
        assert view.recover('video', 4, all_processing=True, job_types=(DOWNLOAD_JOB,)) == 0
    assert store.get(wanted) == row
    assert all(store.get(i)['attempts'] == 0 for i in (wrong_policy, legacy, other))


def test_generic_worker_leaves_foreign_pending_and_processing_untouched(runtime):
    settings, store = runtime
    foreign = add(store, PLAN_JOB)
    pending = add(store, DOWNLOAD_JOB)
    store.claim('video', 60, 4, job_types=(PLAN_JOB,))
    own = add(store, 'legacy')
    snapshots = {i: store.get(i) for i in (foreign, pending)}
    assert Worker(settings, store, {'legacy': lambda job: {}}).once('video')
    assert store.get(own)['state'] == 'completed'
    assert all(store.get(i) == r for i, r in snapshots.items())


def test_shared_video_lock_blocks_competing_type_worker(runtime):
    settings, store = runtime
    first = add(store, 'first')
    second = add(store, 'second')
    entered, release = threading.Event(), threading.Event()
    def handler(job):
        entered.set()
        assert release.wait(10)
        return {'synthetic': True}
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(Worker(settings, store, {'first': handler}).once, 'video')
        try:
            assert entered.wait(10)
            before = store.get(first)
            assert Worker(settings, Store(store.path), {'second': handler}).once('video') is False
            assert store.get(first) == before
            assert store.get(second)['attempts'] == 0
        finally:
            release.set()
        assert future.result()


def test_expired_lease_recovery_filters_before_update(runtime):
    _, store = runtime
    ids = [add(store, kind) for kind in ('legacy', PLAN_JOB)]
    for kind in ('legacy', PLAN_JOB):
        store.claim('video', -1, 4, job_types=(kind,))
    before = store.get(ids[0])
    assert store.recover('video', 4, job_types=(PLAN_JOB,)) == 1
    assert store.get(ids[0]) == before
    assert store.get(ids[1])['state'] == 'retry_scheduled'


def test_reserved_publication_remains_unknown(runtime):
    settings, store = runtime
    ident = add(store, PLAN_JOB)
    store.claim('video', 60, 4, job_types=(PLAN_JOB,))
    with store.transaction() as db:
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('key', ident, 'bilibili', 'youtube', 'synthetic', 'test-only', 'video',
             'dispatching', None, None, time.time()))
    with exclusive(settings.root / 'data/locks/video'):
        assert store.recover('video', 4, all_processing=True, job_types=(PLAN_JOB,)) == 1
    assert store.get(ident)['state'] == 'publish_unknown'
    assert store.claim('video', 60, 4, job_types=(PLAN_JOB,)) is None
