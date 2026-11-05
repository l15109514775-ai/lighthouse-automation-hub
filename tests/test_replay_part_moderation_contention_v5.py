"""Real owned schedule-lock contention; no platform, media or live leases."""
from contextlib import ExitStack
from types import SimpleNamespace
import json
import threading

import pytest

from services.hub.errors import HubError
from services.hub.locks import resource_guard
from services.hub import replay_part_moderation_v5 as moderation
from services.hub import replay_parts_worker_runtime_v5 as runtime_module


def worker_boundary(monkeypatch, once):
    monkeypatch.setattr(moderation, 'build_lookup_worker',
                        lambda settings, store: SimpleNamespace(once=once))


def test_real_busy_schedule_lock_does_not_stop_process_or_mutate_lookup_job(runtime, monkeypatch):
    settings, store = runtime
    store.enqueue('bilibili', 'owned-parent:lookup:1', moderation.LOOKUP_JOB_TYPE,
                  'video', {'owned_test_only': True}, moderation.POLICY)
    before = store.query('SELECT * FROM jobs')
    calls = []; fatals = []; stop = threading.Event()
    worker_boundary(monkeypatch, lambda queue: calls.append(queue))
    with resource_guard(settings.root/'data/locks/part-lookup-schedule-v5'):
        result = runtime_module.moderation_loop(settings, store, stop, once=True, fatal=fatals.append)
    assert store.query('SELECT * FROM jobs') == before
    assert calls == []
    assert fatals == []
    assert result is False and not stop.is_set()


def test_real_lock_release_resumes_same_loop_after_one_second_wait(runtime, monkeypatch):
    settings, store = runtime
    before = store.query('SELECT * FROM jobs')
    calls = []; waits = []; fatals = []; stopped = threading.Event()
    held = ExitStack()
    held.enter_context(resource_guard(settings.root/'data/locks/part-lookup-schedule-v5'))

    class Stop:
        def is_set(self): return stopped.is_set()
        def set(self): stopped.set()
        def wait(self, seconds):
            waits.append(seconds)
            held.close()
            return stopped.is_set()

    def once(queue):
        calls.append(queue); stopped.set(); return True

    worker_boundary(monkeypatch, once)
    try:
        runtime_module.moderation_loop(settings, store, Stop(), fatal=fatals.append)
    finally:
        held.close()
    assert waits == [1] and calls == ['moderation'] and fatals == []
    assert store.query('SELECT * FROM jobs') == before


@pytest.mark.parametrize('error', [
    HubError('REPLAY_PART_LOOKUP_POLICY_INVALID', review=True),
    HubError('HEAVY_RESOURCE_BUSY', retryable=False),
    HubError('HEAVY_RESOURCE_BUSY', retryable=True, review=True),
    RuntimeError('https://owned.invalid/?token=owned-secret-runtime-message'),
])
def test_other_schedule_failures_remain_fatal(runtime, monkeypatch, capsys, error):
    settings, store = runtime; calls = []; fatals = []; stop = threading.Event()
    worker_boundary(monkeypatch, lambda queue: calls.append(queue))
    def fail(*args): raise error
    monkeypatch.setattr(moderation, 'schedule_tick', fail)
    runtime_module.moderation_loop(settings, store, stop, once=True, fatal=fatals.append)
    assert fatals == [70] and stop.is_set() and calls == []
    raw=capsys.readouterr().err
    diagnostic=json.loads(raw)
    assert diagnostic=={'event':'replay_part_background_fatal_v5','role':'moderation',
        'exception_type':type(error).__name__,'code':error.code if isinstance(error,HubError) else None}
    assert len(raw)<512 and 'owned-secret' not in raw and 'https://' not in raw


def test_worker_busy_is_not_swallowed_by_schedule_contention_handler(runtime, monkeypatch):
    settings, store = runtime; fatals = []; stop = threading.Event()
    def once(queue):
        assert queue == 'moderation'
        raise HubError('HEAVY_RESOURCE_BUSY', retryable=True)
    worker_boundary(monkeypatch, once)
    runtime_module.moderation_loop(settings, store, stop, once=True, fatal=fatals.append)
    assert fatals == [70] and stop.is_set()


@pytest.mark.parametrize('error', [
    RuntimeError('https://owned.invalid/?secret=retention-secret'),
    OSError('retention-secret-password'),
])
def test_retention_fatal_records_only_safe_type_and_role(capsys,error):
    stop=threading.Event();fatals=[]
    def fail():raise error
    runtime_module.retention_loop(SimpleNamespace(retention_tick=fail),stop,once=True,fatal=fatals.append)
    raw=capsys.readouterr().err
    assert json.loads(raw)=={'event':'replay_part_background_fatal_v5','role':'retention',
        'exception_type':type(error).__name__,'code':None}
    assert fatals==[70] and stop.is_set() and len(raw)<512
    assert 'retention-secret' not in raw and 'https://' not in raw


def test_untrusted_hub_code_and_exception_arguments_are_not_diagnostics(capsys):
    error=HubError('https://owned.invalid/?token=never-print-this')
    error.args=('password=never-print-this',)
    runtime_module.fatal_diagnostic('moderation',error)
    raw=capsys.readouterr().err
    assert json.loads(raw)['code']=='HUB_ERROR_CODE_REDACTED'
    assert 'never-print-this' not in raw and 'https://' not in raw and len(raw)<512
