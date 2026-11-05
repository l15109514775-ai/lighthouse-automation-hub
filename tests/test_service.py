from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi.testclient import TestClient
from conftest import enqueue
from scheduler.service import DurableScheduler
from services.hub.api import create_app
from services.hub.db import Store
from services.hub.maintenance import backup, Maintenance
import pytest


def test_scheduler_survives_new_instance_without_dot(runtime):
    settings, store = runtime
    now = datetime(2026,10,31,8,59,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
    DurableScheduler(settings, store).tick(now)
    count = store.query('SELECT COUNT(*) n FROM jobs')[0]['n']
    restarted = DurableScheduler(settings, Store(settings.db_path))
    restarted.tick(now + 1)
    assert store.query('SELECT COUNT(*) n FROM jobs')[0]['n'] == count
    restarted.tick(now + 61)
    assert store.query("SELECT COUNT(*) n FROM jobs WHERE job_type='youtube_discovery'")[0]['n'] == 1


def test_online_backup_restore_with_wal(runtime, tmp_path):
    settings, store = runtime
    ident = enqueue(store)
    destination = tmp_path / 'restored.sqlite3'
    result = backup(store, destination)
    assert result['restore_verified']
    restored = Store(destination)
    assert restored.get(ident)['source_id'] == '123'
    assert restored.query('PRAGMA integrity_check')[0]['integrity_check'] == 'ok'


def test_management_read_and_write_permissions(runtime, monkeypatch):
    settings, store = runtime
    monkeypatch.setenv('HUB_READ_TOKEN', 'reader-test')
    monkeypatch.setenv('HUB_MAINTAIN_TOKEN', 'maintainer-test')
    client = TestClient(create_app(settings))
    assert client.get('/health').status_code == 200
    assert client.get('/status').status_code == 403
    assert client.get('/status', headers={'Authorization':'Bearer reader-test'}).status_code == 200
    assert client.post('/pipelines/tweet', json={'paused':True}, headers={'Authorization':'Bearer reader-test'}).status_code == 403
    assert client.post('/pipelines/tweet', json={'paused':True}, headers={'Authorization':'Bearer maintainer-test'}).status_code == 200
    assert store.query("SELECT paused FROM pipelines WHERE name='tweet'")[0]['paused'] == 1
    assert store.query("SELECT COUNT(*) n FROM audit WHERE outcome='denied'")[0]['n'] >= 2


def test_ready_reports_missing_workers(runtime):
    settings, store = runtime
    client = TestClient(create_app(settings))
    assert client.get('/ready').status_code == 503
    for name in ('scheduler','worker:tweet','worker:video','worker:system'):
        store.beat(name)
    assert client.get('/ready').status_code == 200


def test_low_disk_blocks_before_download(runtime, monkeypatch):
    import shutil
    from services.hub.errors import HubError
    settings, store = runtime
    monkeypatch.setattr(shutil, 'disk_usage', lambda _: type('Disk', (), {'free':14_000_000_000})())
    with pytest.raises(HubError, match='DISK_LOW'):
        Maintenance(settings, store).check_disk()
    assert store.query('SELECT name FROM events')[0]['name'] == 'disk.low'
