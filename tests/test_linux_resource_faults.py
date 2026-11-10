"""Run with a dedicated 1 MiB tmpfs at /fault-media in an isolated container."""
import os
from contextlib import closing
from pathlib import Path

import pytest

from services.hub.worker import Worker


@pytest.mark.skipif(os.environ.get('LIGHTHOUSE_FULL_DISK_FIXTURE') != '1',
                    reason='requires explicitly mounted isolated 1 MiB tmpfs')
def test_actual_full_media_filesystem_preserves_partial_and_holds_job(runtime):
    settings, store = runtime
    area = Path('/fault-media')
    assert os.name == 'posix' and os.path.ismount(area)
    assert os.statvfs(area).f_blocks * os.statvfs(area).f_frsize <= 2 * 1024**2
    ident = store.enqueue('youtube','synthetic','youtube_replay_download_v4','video',{},'synthetic')
    file = area / 'owned-incomplete.part'
    def handler(job):
        with file.open('xb') as stream:stream.write(b'x' * (4 * 1024**2))
    assert Worker(settings, store, {'youtube_replay_download_v4':handler}).once('video')
    assert file.exists() and 0 < file.stat().st_size < 4 * 1024**2
    row = store.get(ident)
    assert (row['state'],row['error_code'],row['attempts']) == ('awaiting_review','DISK_LOW',1)
    assert not store.query('SELECT * FROM publications')


@pytest.mark.skipif(os.name != 'posix' or getattr(os, 'geteuid', lambda:0)() == 0,
                    reason='requires unprivileged Linux filesystem semantics')
def test_actual_sqlite_readonly_recovers_without_task_reset(runtime):
    settings, store = runtime
    ident = store.enqueue('youtube','synthetic-readonly','owned_download','video',{},'synthetic')
    # sqlite3.Connection's context manager commits but does not close. Do not
    # accidentally leave a reader open across the permission fault.
    with closing(store.connect()) as db:db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    store.path.chmod(0o400)
    try:
        assert not Worker(settings, store, {'owned_download':lambda _: {}}).once('video')
        row = store.get(ident)
        assert row['state'] == 'pending' and row['attempts'] == 0
    finally:
        # SQLite creates WAL/SHM with the database's temporary mode. Restore
        # the entire *test fixture* permission fault, not just the main file.
        # Production Worker must never chmod database files to bypass denial.
        for suffix in ('', '-wal', '-shm'):
            path = store.path.with_name(store.path.name + suffix)
            if path.exists():path.chmod(0o600)
    assert Worker(settings, store, {'owned_download':lambda _: {}}).once('video')
    assert store.get(ident)['state'] == 'completed'
    assert store.get(ident)['attempts'] == 1
