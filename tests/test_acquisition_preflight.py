"""Read-only policy checks against genuine SQLite fixtures; no external access."""
import json
import pytest
from services.hub.acquisition_preflight import inspect
from services.hub.replay_fifo_v4 import POLICY_FILE as FIFO_FILE
from services.hub.youtube_download_v4 import POLICY_FILE
from test_replay_fifo_v4 import fifo, seed
from test_youtube_download_v4 import configure_v4, toml


def test_consistent_config_is_not_release_authorization_and_does_not_write(fifo):
    f = configure_v4(fifo)
    seed(f, 1)
    f.q.register_all_observed()
    before = f.db.query('SELECT * FROM replay_fifo_v4')
    jobs = f.db.query('SELECT * FROM jobs')
    result = inspect(f.s)
    assert result['configuration_consistent'] and not result['production_activation_allowed']
    assert result['network_used'] is False
    assert result['history']['fifo_bindings'] == [
        {'matches_current_policy': True, 'work_started': False, 'state': 'registered', 'count': 1}]
    assert f.db.query('SELECT * FROM replay_fifo_v4') == before
    assert f.db.query('SELECT * FROM jobs') == jobs


def test_disabled_channel_and_fingerprint_conflicts_reported_together(fifo):
    f = configure_v4(fifo)
    f.s.config['youtube_whole_replay_download_enabled'] = False
    path = f.s.root / 'config' / POLICY_FILE
    import tomllib
    p = tomllib.loads(path.read_text('utf-8'))
    p['allowed_channel_ids'] = p['allowed_channel_ids'][:1]
    p['planning_policy_sha256'] = '0' * 64
    path.write_text(toml(p), encoding='utf-8')
    result = inspect(f.s)
    assert 'YOUTUBE_WHOLE_DISABLED' in result['blockers']
    assert 'CHANNEL_SCOPE_MISMATCH:' + POLICY_FILE in result['blockers']
    assert 'PLANNING_BINDING_MISMATCH:' + POLICY_FILE in result['blockers']
    assert p['owner_permission_reference'] not in json.dumps(result)


def test_history_conflict_is_not_rebound_or_reset(fifo):
    f = configure_v4(fifo)
    seed(f, 1)
    f.q.register_all_observed()
    with f.db.transaction() as db:
        db.execute("UPDATE replay_fifo_v4 SET policy_sha256=?", ('0' * 64,))
    before = f.db.query('SELECT * FROM replay_fifo_v4')
    result = inspect(f.s)
    assert 'HISTORICAL_FIFO_BINDINGS_REQUIRE_RECONCILIATION' in result['blockers']
    assert f.db.query('SELECT * FROM replay_fifo_v4') == before


def test_missing_database_is_not_created(fifo):
    f = configure_v4(fifo)
    f.s.root = f.s.root / 'not-created'
    result = inspect(f.s)
    assert 'DATABASE_READONLY_INSPECTION_FAILED' in result['blockers']
    assert not f.s.root.exists()


@pytest.mark.parametrize('state', ['completed', 'excluded', 'existing'])
def test_terminal_history_retains_original_bindings_without_demanding_reset(fifo, state):
    f = configure_v4(fifo)
    seed(f, 1)
    f.q.register_all_observed()
    with f.db.transaction() as db:
        db.execute('UPDATE replay_fifo_v4 SET policy_sha256=?,state=?', ('0' * 64, state))
    before = f.db.query('SELECT * FROM replay_fifo_v4')
    result = inspect(f.s)
    assert result['configuration_consistent']
    assert result['historical_terminal_rows_preserved'] == 1
    assert f.db.query('SELECT * FROM replay_fifo_v4') == before


def test_duplicate_keys_fail_closed_without_exposing_value(fifo):
    f = configure_v4(fifo)
    seed(f, 1)
    f.q.register_all_observed()
    path = f.s.root / 'config' / FIFO_FILE
    path.write_text('{"enabled":true,"enabled":false,"secret":"sensitive-fixture"}')
    result = inspect(f.s)
    assert 'POLICY_UNREADABLE_OR_INVALID:' + FIFO_FILE in result['blockers']
    assert 'HISTORICAL_FIFO_BINDINGS_UNVERIFIED' in result['blockers']
    assert result['history']['fifo_bindings'][0]['matches_current_policy'] is None
    assert 'sensitive-fixture' not in json.dumps(result)


def test_changed_policy_is_reported_without_restoring_it(fifo, monkeypatch):
    f = configure_v4(fifo)
    from services.hub import acquisition_preflight as module
    original = module._history
    path = f.s.root / 'config' / FIFO_FILE
    def changed(*args):
        value = original(*args)
        path.write_text(path.read_text() + '\n')
        return value
    monkeypatch.setattr(module, '_history', changed)
    assert 'POLICY_CHANGED_DURING_READ:' + FIFO_FILE in inspect(f.s)['blockers']
