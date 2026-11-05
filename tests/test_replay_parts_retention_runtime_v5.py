"""Local retention integration; no browser, HTTP, credentials or live data.

The destructive positive uses actual FFmpeg/delivery/public-result SQLite
fixtures. Deployment root ownership alone is a named upstream test seam.
"""
from pathlib import Path
import threading
import pytest

from services.hub.errors import HubError
from services.hub.locks import resource_guard
from services.hub.replay_parts_worker_runtime_v5 import build_worker,retention_loop
from services.hub import replay_parts_retention_v5 as module
from test_replay_parts_retention_v5 import completed
from test_replay_parts_v5 import reserved,publisher
from test_replay_delivery_v5 import delivery
from test_ytdlp_highest_v2 import tracks
from test_replay_fifo_v4 import fifo


def released(f,monkeypatch):
    file=f.s.root/'config'/module.RELEASE;file.write_text('Owned root release seam','utf-8')
    monkeypatch.setattr(module.ReplayPartsRetentionV5,'_release',lambda self:(self._authorization(f.owner['authorization_id']),'owned-root-release-only'))
    return build_worker(f.s,f.db,mode='publish')[1]


@pytest.mark.parametrize('mode',['register_only','acquisition_only'])
def test_nonpublication_runtime_never_calls_cleanup_or_creates_cleanup_workspace(publisher,monkeypatch,mode):
    f=publisher;_,coordinator=build_worker(f.s,f.db,mode=mode)
    def forbidden(*args):raise AssertionError('Cleanup must not run in this mode')
    monkeypatch.setattr(module,'retention_tick',forbidden)
    assert coordinator.retention_tick()=={'state':'disabled','files_deleted':0,'external_requests':0}
    assert not (f.s.root/'data/replay-parts-retention-v5').exists()


def test_publish_mode_without_technical_release_retains_source_and_never_starts_browser(publisher):
    f=publisher;_,coordinator=build_worker(f.s,f.db,mode='publish')
    value=coordinator.retention_tick()
    assert value['state']=='disabled' and value['files_deleted']==value['external_requests']==0
    assert not (f.s.root/'data/replay-parts-retention-v5').exists()


def test_expensive_local_scan_is_independent_of_real_registration_and_producer_heartbeat(publisher,monkeypatch):
    f=publisher;_,coordinator=build_worker(f.s,f.db,mode='publish');entered=threading.Event();release=threading.Event();values=[]
    def scan(settings,store):
        entered.set();assert release.wait(10)
        return {'state':'idle','files_deleted':0,'external_requests':0}
    monkeypatch.setattr(module,'retention_tick',scan)
    thread=threading.Thread(target=lambda:values.append(retention_loop(coordinator,threading.Event(),once=True)))
    thread.start();assert entered.wait(10)
    try:
        value=coordinator.tick()
        assert value['steps']['register']['registered']==[] and value['external_requests']==0
        assert f.db.query("SELECT * FROM heartbeats WHERE name='worker:video-producer-v5'")
        assert thread.is_alive()
    finally:release.set();thread.join(10)
    assert not thread.is_alive() and values[0]['state']=='idle'
    assert f.db.query("SELECT * FROM heartbeats WHERE name='worker:video-retention-v5'")


def test_completed_tick_reclaims_exact_canonical_once_and_preserves_publications(completed,monkeypatch):
    f=completed;coordinator=released(f,monkeypatch)
    file=Path(f.db.get(f.job['job_id'])['result']['youtube_download_v4']['source_asset']['file']);pubs=f.db.query('SELECT * FROM publications')
    assert file.is_file()
    value=retention_loop(coordinator,threading.Event(),once=True)
    assert value['state']=='completed' and value['files_deleted']==1 and value['receipt']['external_requests']==0
    assert not file.exists() and f.db.query('SELECT * FROM publications')==pubs
    assert coordinator.retention_tick()['state']=='idle'


def test_paused_runtime_never_enters_destructive_tick(completed,monkeypatch):
    f=completed;coordinator=released(f,monkeypatch)
    with f.db.transaction() as db:db.execute("UPDATE pipelines SET paused=1 WHERE name='video'")
    def forbidden(*args):raise AssertionError('Pause must stop maintenance')
    monkeypatch.setattr(module,'retention_tick',forbidden)
    assert coordinator.retention_tick()=={'state':'paused','files_deleted':0,'external_requests':0}


def test_changed_release_is_recorded_as_cleanup_hold_without_stopping_registration(completed,monkeypatch):
    f=completed;coordinator=released(f,monkeypatch)
    file=Path(f.db.get(f.job['job_id'])['result']['youtube_download_v4']['source_asset']['file'])
    def invalid(self):raise HubError('REPLAY_PART_RETENTION_RELEASE_REQUIRED',review=True)
    monkeypatch.setattr(module.ReplayPartsRetentionV5,'_release',invalid)
    value=retention_loop(coordinator,threading.Event(),once=True)
    assert value['state']=='held' and value['files_deleted']==0 and file.is_file()
    assert coordinator.tick()['external_requests']==0


def test_active_video_os_lock_defers_cleanup_without_media_or_publication_changes(completed,monkeypatch):
    f=completed;coordinator=released(f,monkeypatch);pubs=f.db.query('SELECT * FROM publications')
    file=Path(f.db.get(f.job['job_id'])['result']['youtube_download_v4']['source_asset']['file'])
    with resource_guard(f.s.root/'data/locks/video'):
        value=coordinator.retention_tick()
    assert value['state']=='held' and value['code']=='HEAVY_RESOURCE_BUSY' and value['files_deleted']==0
    assert file.is_file() and f.db.query('SELECT * FROM publications')==pubs
