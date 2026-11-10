import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from scripts.ops import cloudbase_backup_budget_recovery as m


def records():
    previous = {'daily_id': 'aed00779-ad1a-421d-8375-b4535a633ff6',
                'state': 'accept_failed', 'error_code': 'ACCEPT_FAILED_OR_UNVERIFIED',
                'created_at': '2026-11-10T13:12:52+00:00',
                'updated_at': '2026-11-10T13:13:28+00:00'}
    operation = {'operation_id': '3284d90b-9844-4a01-85ba-37ac0eb4758f',
                 'created_at': 1794316376.0, 'updated_at': 1794316406.0,
                 'state': 'failed', 'archive': None, 'receipt': None,
                 'error_code': m.ERROR, 'policy_sha256': 'a' * 64}
    digest = hashlib.sha256(m.encoded(previous)).hexdigest()
    proof = {'version': m.VERSION, 'daily_id': previous['daily_id'],
             'failed_daily_sha256': digest, 'operation_id': operation['operation_id'],
             'operation_sha256': hashlib.sha256(m.encoded(operation)).hexdigest(),
             'external_requests': 0, 'budget_refunded': False, 'backup_success_claimed': False}
    return previous, operation, proof, digest


def limits():
    return dict(daily_network_bytes=512000000, monthly_network_bytes=8000000000,
                daily_requests=50, monthly_requests=250, max_archive_bytes=192000000)


def test_budget_retains_source_host_reservations_and_requires_full_plan():
    periods = {'daily': {'bytes': 354548122, 'requests': 7},
               'monthly': {'bytes': 2241894688, 'requests': 102}}
    result = m.headroom(limits(), periods)
    assert result['state'] == 'budget_deferred'
    assert result['required_network_bytes_upper'] == 386648864
    assert periods['daily']['bytes'] == 354548122
    periods['daily'] = {'bytes': 0, 'requests': 0}
    assert m.headroom(limits(), periods)['state'] == 'budget_available'
    periods['monthly']['requests'] = 245
    assert m.headroom(limits(), periods)['state'] == 'budget_deferred'


@pytest.mark.parametrize('kind', ['request', 'plan', 'unresolved', 'changed_record',
                                  'history', 'wrong_error', 'receipt', 'outside_time'])
def test_failure_reconciliation_refuses_ambiguity(kind):
    previous, operation, proof, digest = records()
    counts = dict(request_count=0, plan_count=0, unresolved_count=0)
    history = digest
    if kind in ('request', 'plan', 'unresolved'):
        counts[{'request': 'request_count', 'plan': 'plan_count', 'unresolved': 'unresolved_count'}[kind]] = 1
    if kind == 'changed_record': operation['policy_sha256'] = 'b' * 64
    if kind == 'history': history = 'b' * 64
    if kind == 'wrong_error': operation['error_code'] = 'NETWORK_TIMEOUT'
    if kind == 'receipt': operation['receipt'] = '/some/receipt'
    if kind == 'outside_time': operation['created_at'] -= 100
    if kind in ('wrong_error', 'receipt', 'outside_time'):
        proof['operation_sha256'] = hashlib.sha256(m.encoded(operation)).hexdigest()
    with pytest.raises(ValueError, match='BUDGET_RECOVERY_'):
        m.verify_failure(previous, proof, operation, latest_sha=digest, history_sha=history, **counts)


@pytest.mark.parametrize('archive', [None, '/opt/lighthouse-hub/backups/prepared-only.tar.gz'])
def test_exact_preupload_failure_can_start_successor_without_changing_original(archive):
    previous, operation, proof, digest = records()
    operation['archive'] = archive
    proof['operation_sha256'] = hashlib.sha256(m.encoded(operation)).hexdigest()
    m.verify_failure(previous, proof, operation, latest_sha=digest, history_sha=digest,
                     request_count=0, plan_count=0, unresolved_count=0)
    calls, audits = [], []
    daily = SimpleNamespace(execute=lambda prev, **kw: calls.append(prev) or {'state': 'completed'},
                            atomic=lambda path, value: audits.append(value))
    m.bind(daily, inspector=lambda *args: {'state': 'budget_available'})
    assert daily.execute(previous)['state'] == 'completed'
    assert calls == [None]
    assert previous['state'] == 'accept_failed' and operation['state'] == 'failed'
    assert audits[0]['previous_daily_id'] == previous['daily_id']


def test_deferred_has_no_successor_and_cleanup_and_unknown_keep_original_path():
    previous, _, _, _ = records()
    calls = []
    daily = SimpleNamespace(execute=lambda prev, **kw: calls.append(prev), atomic=lambda *args: None)
    m.bind(daily, inspector=lambda *args: {'state': 'budget_deferred'})
    with pytest.raises(m.BudgetDeferred): daily.execute(previous)
    assert calls == []
    for state in ('accepted_pending_prune', 'prune_started', 'accept_started', 'reconciliation_required'):
        record = {'state': state}
        daily.execute(record)
        assert calls[-1] is record


def test_actual_sqlite_readonly_gate_rejects_request_added_after_attestation(tmp_path):
    previous, operation, proof, _ = records()
    root, state = tmp_path / 'app', tmp_path / 'state'
    for p in (root / 'config', root / 'data/cloudbase-backup-state', state / 'budget-recovery', state / 'history'):
        p.mkdir(parents=True)
    (root / 'config/cloudbase-backup.toml').write_text('[limits]\n' + '\n'.join(f'{k}={v}' for k, v in limits().items()))
    for p, value in [(state / 'latest.json', previous), (state / 'history' / (previous['daily_id'] + '.json'), previous),
                     (state / 'budget-recovery' / (previous['daily_id'] + '.json'), proof)]:
        p.write_bytes(m.encoded(value))
    ledger = root / 'data/cloudbase-backup-state/ledger.sqlite3'
    with sqlite3.connect(ledger) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('CREATE TABLE operations(operation_id,created_at,updated_at,state,archive,receipt,error_code,policy_sha256);'
                         'CREATE TABLE requests(operation_id,day,month,reserved_bytes);CREATE TABLE plans(operation_id);')
        db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?)', tuple(operation.values()))
    def read(path, base, **kwargs):
        raw = path.read_bytes()
        return json.loads(raw), hashlib.sha256(raw).hexdigest()
    # Ownership validation belongs to the host helper; exercise genuine SQLite
    # and proof binding here on Windows too, without pretending to test POSIX ACLs.
    daily = SimpleNamespace(safe=lambda p, base: p, protected=lambda *args, **kw: None,
                            ident=lambda value: value, read_record=read)
    assert m.inspect(daily, root, state, previous)['state'] == 'budget_available'
    with sqlite3.connect(ledger) as db:
        db.execute('INSERT INTO requests VALUES(?,?,?,?)', (operation['operation_id'], '2026-11-10', '2026-11', 300000))
    with pytest.raises(ValueError, match='REMOTE_INTENT'):
        m.inspect(daily, root, state, previous)
    with sqlite3.connect(ledger) as db:
        assert db.execute('SELECT state FROM operations').fetchone()[0] == 'failed'
        assert db.execute('SELECT COUNT(*) FROM requests').fetchone()[0] == 1
