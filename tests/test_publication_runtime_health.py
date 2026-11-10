"""Independent publisher health and ordinary shared-lock contention; no network."""
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from services.hub.acquisition_health import instance_names
from services.hub.errors import HubError
from services.hub.publication_health import NAMES, healthy
from services.hub.replay_parts_worker_runtime_v5 import Coordinator
from services.hub.replay_multipart_worker_runtime_v4 import run_loop


def test_acquisition_heartbeats_cannot_make_publisher_healthy(tmp_path):
    marker=tmp_path/'epoch.json';marker.write_text(json.dumps({'started_at':100,'instance':'a'*32}))
    database=tmp_path/'test.sqlite3'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE heartbeats(name TEXT PRIMARY KEY, at REAL)')
        db.executemany('INSERT INTO heartbeats VALUES(?,?)',[(n,110) for n in instance_names('a'*32)])
    assert not healthy(database,marker,now=120)
    with sqlite3.connect(database) as db:
        db.executemany('INSERT INTO heartbeats VALUES(?,?)',[(n,110) for n in instance_names('a'*32,names=NAMES)])
    assert healthy(database,marker,now=120)
    assert not healthy(database,marker,now=300)


def test_main_waiting_heartbeat_does_not_refresh_producer():
    events=[];c=Coordinator.__new__(Coordinator);c.producer_heartbeat=None
    c.store=SimpleNamespace(beat=events.append)
    c.worker=SimpleNamespace(store=SimpleNamespace(heartbeat_instance=None))
    c.bind_health('b'*32);c.idle_heartbeat()
    assert events==[instance_names('b'*32,names=NAMES)[0]]
    assert c.producer_heartbeat==instance_names('b'*32,names=NAMES)[1]
    with pytest.raises(ValueError):c.bind_health('c'*32)


def test_startup_busy_once_does_not_claim_or_restart():
    def busy():raise HubError('HEAVY_RESOURCE_BUSY',retryable=True)
    def claim(_):pytest.fail('must not claim after incomplete one-shot producer')
    assert run_loop(SimpleNamespace(once=claim),SimpleNamespace(mode='publication_only',tick=busy),once=True) is False


def test_startup_busy_continues_normal_background_loop():
    stop=threading.Event();calls=[]
    def busy():raise HubError('HEAVY_RESOURCE_BUSY',retryable=True)
    def once(queue):calls.append(queue);stop.set();return False
    run_loop(SimpleNamespace(once=once),SimpleNamespace(mode='publication_only',tick=busy),stop=stop)
    assert calls==['video']


def test_startup_non_contention_failure_remains_fatal():
    def fail():raise HubError('REPLAY_PART_POLICY_REQUIRED',review=True)
    with pytest.raises(HubError,match='REPLAY_PART_POLICY_REQUIRED'):
        run_loop(SimpleNamespace(),SimpleNamespace(mode='publication_only',tick=fail),once=True)


def test_publication_resource_wait_keeps_producer_alive_without_admission(tmp_path,monkeypatch):
    import services.hub.replay_part_moderation_v5 as moderation
    monkeypatch.setattr(moderation,'schedule_tick',lambda *a:{'created':[]})
    events=[];c=Coordinator.__new__(Coordinator);c.mode='publication_only'
    c.settings=SimpleNamespace(root=tmp_path);c.store=SimpleNamespace(query=lambda *a:[{'paused':False}],beat=events.append)
    c.base=SimpleNamespace(require_policy=lambda:{})
    c.require_publish_policy=lambda:{};c.producer_heartbeat='owned-producer'
    def busy(_):raise HubError('HEAVY_RESOURCE_BUSY',retryable=True)
    c.groups=SimpleNamespace(initialize=lambda:None,sync_completed_groups=lambda:{},fifo_projection=lambda:None,publication_tick=busy)
    c.fifo=SimpleNamespace(sync_states=lambda **k:{},register_all_observed=lambda:{})
    result=c.tick()
    assert result['steps']['publication']=={'state':'waiting_resource','created':[]}
    assert result['external_requests']==0 and 'owned-producer' in events
