"""Root-supplied Docker identity boundary; no production host or heartbeat."""
from pathlib import Path
from types import SimpleNamespace
import pytest
from services.hub.errors import HubError
from scripts.ops import oneoff_reference_backfill_v2 as m


def test_explicit_compose_hostname_matches_env_and_hostname_file(monkeypatch):
    scope = SimpleNamespace(worker_context_verified=False)
    monkeypatch.setenv('HOSTNAME', 'lighthouse-tweet-worker')
    original = Path.read_text
    monkeypatch.setattr(Path, 'read_text', lambda self, *a, **k: 'lighthouse-tweet-worker\n' if str(self) in ('/etc/hostname', '\\etc\\hostname') else original(self, *a, **k))
    m.validate_worker_context(scope, 'a'*64, 'lighthouse-tweet-worker')
    assert scope.worker_context_verified is True


def test_hostname_or_container_id_mismatch_never_qualifies_worker_heartbeat(monkeypatch):
    scope = SimpleNamespace(worker_context_verified=False)
    monkeypatch.setenv('HOSTNAME', 'lighthouse-tweet-worker')
    monkeypatch.setattr(Path, 'read_text', lambda *a, **k: 'owned-other-container\n')
    with pytest.raises(HubError):m.validate_worker_context(scope, 'a'*64, 'lighthouse-tweet-worker')
    with pytest.raises(HubError):m.validate_worker_context(scope, 'not-an-actual-container-id', 'lighthouse-tweet-worker')
    assert scope.worker_context_verified is False
