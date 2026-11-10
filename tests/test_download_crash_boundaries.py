"""Actual owned FFmpeg media; injected crash boundaries, never YouTube content."""
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from adapters import ytdlp_highest_v2 as engine
from services.hub.db import Store
from services.hub.errors import HubError
from services.hub.locks import exclusive
from services.hub.worker import Worker
from test_replay_fifo_v4 import fifo
from test_ytdlp_highest_v2 import tracks
from test_youtube_download_v4 import whole


@pytest.mark.parametrize('boundary', ['download-result.json', 'verified.json'])
def test_completed_file_before_database_commit_recovers_without_second_download(whole, monkeypatch, boundary):
    f = whole
    write = engine.atomic_json
    def crash(file, value):
        write(file, value)
        if file.name == boundary:raise SystemExit(77)
    monkeypatch.setattr(engine, 'atomic_json', crash)
    with pytest.raises(SystemExit):f.service.process(f.job)
    assert f.db.get(f.job['job_id'])['state'] == 'processing'
    assert f.calls == ['owned_track_copy_not_youtube']
    monkeypatch.setattr(engine, 'atomic_json', write)
    with exclusive(f.s.root / 'data/locks/video'):
        f.db.recover('video', 4, all_processing=True, job_types=(f.job['job_type'],))
        f.job = f.db.claim('video', 180, 4, job_types=(f.job['job_type'],))
        result = f.service.process(f.job)
        f.db.finish(f.job, 'completed', result=result)
    assert f.db.get(f.job['job_id'])['state'] == 'completed'
    assert f.db.get(f.job['job_id'])['attempts'] == 2
    assert f.calls == ['owned_track_copy_not_youtube']
    assert not f.db.query('SELECT * FROM publications')


@pytest.mark.skipif(os.name != 'posix', reason='Linux SIGKILL acceptance')
def test_downloader_subprocess_sigkill_is_bounded_and_preserves_partial(runtime, monkeypatch):
    settings, store = runtime
    downloader = engine.HighestReplayDownloader(settings)
    area = settings.root / 'media/owned-helper';area.mkdir()
    partial = area / 'stream.part';partial.write_bytes(b'owned partial')
    popen = subprocess.Popen
    def killed(*args, **kwargs):
        return popen([sys.executable, '-c', 'import os,signal;os.kill(os.getpid(),signal.SIGKILL)'], **kwargs)
    monkeypatch.setattr(engine.subprocess, 'Popen', killed)
    monkeypatch.setattr(downloader, 'NEEDED_DISK', lambda *_:0)
    with pytest.raises(HubError) as error:
        downloader._helper({}, {'timeout_seconds': 5}, area, area/'ticket.json', area/'download-result.json', lambda:None)
    assert error.value.code == 'YOUTUBE_HIGHEST_HELPER_INTERRUPTED' and error.value.retryable
    assert partial.read_bytes() == b'owned partial'


def test_sqlite_write_lock_does_not_claim_or_reset_other_jobs(runtime, monkeypatch):
    settings, store = runtime
    ident = store.enqueue('youtube','owned','owned_download','video',{},'owned-policy')
    original = store.connect
    def quick():
        db = original();db.execute('PRAGMA busy_timeout=10');return db
    monkeypatch.setattr(store, 'connect', quick)
    with Store(store.path).transaction() as writer:
        writer.execute("UPDATE jobs SET stage='owned-checkpoint' WHERE job_id=?", (ident,))
        assert not Worker(settings, store, {'owned_download':lambda _: {}}).once('video')
    row = store.get(ident)
    assert row['attempts'] == 0 and row['state'] == 'pending' and row['stage'] == 'owned-checkpoint'
    assert Worker(settings, store, {'owned_download':lambda _: {}}).once('video')


@pytest.mark.skipif(os.name != 'posix', reason='Linux process/WAL crash acceptance')
def test_sigkill_during_sqlite_transaction_rolls_back_and_online_backup_restores(runtime):
    settings, store = runtime
    ident = store.enqueue('youtube','owned','owned_download','video',{},'owned-policy')
    marker = settings.root / 'transaction-open'
    code = '''
import sqlite3,sys,time
from pathlib import Path
db=sqlite3.connect(sys.argv[1],isolation_level=None);db.execute('PRAGMA journal_mode=WAL')
db.execute('BEGIN IMMEDIATE');db.execute("UPDATE jobs SET attempts=999")
Path(sys.argv[2]).write_text('ready')
time.sleep(30)
'''
    process = subprocess.Popen([sys.executable,'-c',code,str(store.path),str(marker)])
    try:
        until = time.monotonic()+10
        while not marker.exists() and process.poll() is None and time.monotonic()<until:time.sleep(.01)
        assert marker.exists()
        process.kill();process.wait(timeout=5)
        assert process.returncode == -signal.SIGKILL
    finally:
        if process.poll() is None:process.kill();process.wait(timeout=5)
    assert store.get(ident)['attempts'] == 0
    backup = settings.root / 'backups/consistent.sqlite3'
    with store.connect() as source, sqlite3.connect(backup) as target:source.backup(target)
    restored = Store(backup)
    assert restored.query('PRAGMA integrity_check')[0]['integrity_check'] == 'ok'
    assert restored.get(ident) == store.get(ident)
