"""Fault boundaries with real SQLite/files; no external API or publication."""
import errno
import hashlib
import json
import sqlite3
import threading
import time

import pytest

from services.hub.db import Store
from services.hub.errors import HubError, LeaseLost, error_category
from services.hub.evidence import sha256_file
from services.hub.worker import Worker
from adapters.ytdlp_highest_v2 import read_download_record


def claim(store):
    store.enqueue('youtube', 'synthetic', 'youtube_replay_download_v4', 'video', {}, 'replay-fifo-v4')
    return store.claim('video', 60, 4, job_types=('youtube_replay_download_v4',))


def test_expired_lease_cannot_be_resurrected_or_commit(runtime):
    _, store = runtime
    job = claim(store)
    with store.transaction() as db:
        db.execute('UPDATE jobs SET lease_until=0 WHERE job_id=?', (job['job_id'],))
    before = store.get(job['job_id'])
    assert store.heartbeat(job, 60) is False
    with pytest.raises(LeaseLost):store.checkpoint(job, 'verified', {'synthetic': True})
    with pytest.raises(LeaseLost):store.finish(job, 'completed')
    assert store.get(job['job_id']) == before


def test_attempt_fencing_even_if_owner_accidentally_reused(runtime):
    _, store = runtime
    job = claim(store)
    with store.transaction() as db:
        db.execute('UPDATE jobs SET attempts=attempts+1 WHERE job_id=?', (job['job_id'],))
    assert store.heartbeat(job, 60) is False
    with pytest.raises(LeaseLost):store.finish(job, 'completed')


def test_failed_heartbeat_never_reports_healthy(runtime, monkeypatch):
    settings, store = runtime
    job = claim(store)
    settings.config['lease_seconds'] = .03
    lost = threading.Event()
    def unavailable(*args):raise sqlite3.OperationalError('synthetic-do-not-log-secret')
    monkeypatch.setattr(store, 'heartbeat', unavailable)
    Worker(settings, store, {})._heartbeat(job, threading.Event(), lost)
    assert lost.is_set()
    assert store.query('SELECT * FROM heartbeats') == []


def test_worker_cannot_complete_after_heartbeat_failure(runtime, monkeypatch):
    settings, store = runtime
    ident = store.enqueue('youtube', 'synthetic', 'youtube_replay_download_v4', 'video', {}, 'replay-fifo-v4')
    settings.config['lease_seconds'] = .03
    renewed = threading.Event()
    def fail(*args):renewed.set();return False
    monkeypatch.setattr(store, 'heartbeat', fail)
    def handler(job):
        assert renewed.wait(2)
        time.sleep(.03)
        return {'synthetic': True}
    assert not Worker(settings, store, {'youtube_replay_download_v4': handler}).once('video')
    assert store.get(ident)['state'] == 'processing'
    assert store.get(ident)['result'] == {}


@pytest.mark.parametrize('number,code', [(errno.ENOSPC,'DISK_LOW'), (errno.EROFS,'FILESYSTEM_NOT_WRITABLE')])
def test_actual_file_fault_is_stable_review_without_deleting_partial(runtime, number, code):
    settings, store = runtime
    ident = store.enqueue('youtube', 'synthetic', 'youtube_replay_download_v4', 'video', {}, 'replay-fifo-v4')
    partial = settings.root / 'media/owned.part'
    partial.write_bytes(b'owned partial')
    def handler(job):raise OSError(number, 'sensitive-url-must-not-persist')
    assert Worker(settings, store, {'youtube_replay_download_v4': handler}).once('video')
    job = store.get(ident)
    assert (job['state'], job['error_code'], job['attempts']) == ('awaiting_review', code, 1)
    assert partial.read_bytes() == b'owned partial'


def test_error_strings_are_not_persisted(runtime):
    _, store = runtime
    job = claim(store)
    store.finish(job, 'failed', code='https://secret.invalid/?token=sensitive')
    assert store.get(job['job_id'])['error_code'] == 'ERROR_CODE_INVALID'
    assert 'sensitive' not in json.dumps(store.query('SELECT * FROM events'))


@pytest.mark.parametrize('raw', [b'{', b'[]', b'{"state":1,"state":2}', b'x' * 262145])
def test_corrupt_verification_record_requires_review(tmp_path, raw):
    file = tmp_path / 'verified.json'
    file.write_bytes(raw)
    with pytest.raises(HubError) as error:read_download_record(file, 'YOUTUBE_HIGHEST_RECEIPT_INVALID')
    assert error.value.review and not error.value.retryable
    assert file.read_bytes() == raw


def test_hash_is_identical_but_cancellable(tmp_path):
    file = tmp_path / 'owned.bin';raw = b'fixture' * 1000000;file.write_bytes(raw)
    calls = []
    assert sha256_file(file, check=lambda:calls.append(1)) == hashlib.sha256(raw).hexdigest()
    assert len(calls) >= 3
    def cancelled():raise LeaseLost()
    with pytest.raises(LeaseLost):sha256_file(file, check=cancelled)
    assert file.exists()


@pytest.mark.parametrize('code,category', [
    ('SQLITE_BUSY','database'),('DISK_LOW','disk_resource'),
    ('YOUTUBE_SESSION_AUTH_EXPIRED','authorization'),
    ('YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED','platform_restriction'),
    ('YOUTUBE_DOWNLOAD_RATE_LIMITED','platform_restriction'),
    ('YOUTUBE_WHOLE_BINDING_CHANGED','identity'),
    ('YOUTUBE_HIGHEST_RECEIPT_INVALID','integrity'),
    ('YOUTUBE_WHOLE_POLICY_INVALID','configuration'),
    ('YOUTUBE_DOWNLOAD_NETWORK_ERROR','network'),('UNEXPECTED_RUNTIME_ERROR','internal')])
def test_stable_taxonomy(code, category):
    assert error_category(code) == category
