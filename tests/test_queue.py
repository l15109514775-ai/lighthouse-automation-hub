from concurrent.futures import ThreadPoolExecutor
import sqlite3
import time
import pytest
from conftest import enqueue
from services.hub.errors import HubError
from services.hub.db import Store
from services.hub.locks import exclusive
from services.hub.worker import Worker


def test_concurrent_duplicate_event_creates_one_job(runtime):
    _, store = runtime
    with ThreadPoolExecutor(8) as pool:
        ids = list(pool.map(lambda _: enqueue(store), range(30)))
    assert len(set(ids)) == 1
    assert store.query('SELECT COUNT(*) n FROM jobs')[0]['n'] == 1
    assert store.query('PRAGMA journal_mode')[0]['journal_mode'] == 'wal'


def test_exclusive_claim_and_fencing(runtime):
    _, store = runtime
    ident = enqueue(store)
    with ThreadPoolExecutor(8) as pool:
        claimed = list(pool.map(lambda _: store.claim('tweet', 60, 4), range(8)))
    assert sum(x is not None for x in claimed) == 1
    job = next(x for x in claimed if x)
    fake = {**job, 'lease_owner': 'old-owner'}
    with pytest.raises(RuntimeError, match='LEASE_LOST'):
        store.finish(fake, 'completed')
    store.finish(job, 'completed')
    assert store.get(ident)['state'] == 'completed'


def test_restart_recovery_preserves_unknown_publish(runtime):
    _, store = runtime
    ident = enqueue(store)
    job = store.claim('tweet', 60, 4)
    with store.transaction() as db:
        db.execute('''INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                   ('fixed-key', ident, 'bilibili', 'x', '123', 'test-only', 'image', 'dispatching', None, None, time.time()))
    with exclusive(runtime[0].root / 'data/locks/tweet'):
        assert store.recover('tweet', 4, all_processing=True) == 1
    assert store.get(ident)['state'] == 'publish_unknown'
    assert store.claim('tweet', 60, 4) is None


def test_finite_retry_then_stop(runtime):
    settings, store = runtime
    ident = enqueue(store)
    def fail(job):
        raise HubError('NETWORK_ERROR', retryable=True)
    worker = Worker(settings, store, {'test': fail})
    for attempt in range(4):
        assert worker.once('tweet')
        with store.transaction() as db:
            db.execute('UPDATE jobs SET next_run=0 WHERE job_id=?', (ident,))
    assert store.get(ident)['state'] == 'failed'
    assert store.get(ident)['attempts'] == 4
    assert not worker.once('tweet')


def test_machine_check_failure_blocks_work_and_separate_queues(runtime):
    settings, store = runtime
    ident = enqueue(store)
    enqueue(store, queue='video', source_id='456')
    def block(job):
        raise HubError('TRANSLATION_CHECK_FAILED', review=True)
    worker = Worker(settings, store, {'test': block})
    assert worker.once('tweet')
    assert store.get(ident)['state'] == 'awaiting_review'
    assert store.query("SELECT state FROM jobs WHERE queue='video'")[0]['state'] == 'pending'


def test_os_lock_prevents_recovery_by_competing_worker(runtime):
    settings, store = runtime
    ident = enqueue(store)
    worker = Worker(settings, store, {'test': lambda job: {}})
    with exclusive(settings.root / 'data/locks/tweet'):
        assert not worker.once('tweet')
        assert store.get(ident)['attempts'] == 0


def test_real_process_exit_then_recovery(runtime):
    import subprocess
    import sys
    settings, store = runtime
    ident = enqueue(store)
    code = '''import os,sys
from pathlib import Path
from services.hub.config import Settings
from services.hub.db import Store
from services.hub.worker import Worker
s=Settings(Path(sys.argv[1]),{'lease_seconds':30,'max_attempts':4})
def crash(job): os._exit(17)
Worker(s,Store(s.db_path),{'test':crash}).once('tweet')
'''
    process = subprocess.run([sys.executable, '-c', code, str(settings.root)], timeout=15)
    assert process.returncode == 17
    assert store.get(ident)['state'] == 'processing'
    Worker(settings, Store(settings.db_path), {'test': lambda job: {'recovered': True}}).once('tweet')
    assert store.get(ident)['state'] == 'completed'
    assert store.get(ident)['attempts'] == 2
