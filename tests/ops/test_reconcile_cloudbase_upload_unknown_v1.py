"""Owned offline fixtures for upload-unknown reconciliation."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json
import sqlite3

import pytest

from scripts.ops import reconcile_cloudbase_upload_unknown_v1 as module


NOW = 1_793_753_034.571024


def write(path, value):
    raw = value if isinstance(value, bytes) else module.wire(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(0o600)
    return raw


def rows(layout):
    db = sqlite3.connect(layout.ledger)
    db.row_factory = sqlite3.Row
    try:
        return {
            'operations': [dict(row) for row in db.execute('SELECT * FROM operations ORDER BY operation_id')],
            'plans': [dict(row) for row in db.execute('SELECT * FROM plans ORDER BY operation_id')],
            'requests': [
                dict(row)
                for row in db.execute('SELECT * FROM requests ORDER BY created_at,request_id')
            ],
        }
    finally:
        db.close()


@pytest.fixture
def owned(tmp_path, monkeypatch):
    layout = module.Layout(tmp_path / 'app', tmp_path / 'ops')
    monkeypatch.setattr(module, 'owner', lambda *args, **kwargs: None)
    archive = b'Owned frozen backup archive; no provider transfer.'
    write(layout.archive, archive)
    config = b'''[cloudbase]
object_prefix="lighthouse-hub/backups/v1"
[limits]
daily_network_bytes=512000000
monthly_network_bytes=8000000000
daily_requests=50
monthly_requests=250
'''
    write(layout.config, config)
    intent = {
        'version': 'cloudbase-backup-v1',
        'operation_id': module.ADAPTER,
        'local_archive': str(layout.archive.relative_to(layout.root)),
        'bytes': len(archive),
        'sha256': module.sha(archive),
        'archive_key': 'lighthouse-hub/backups/v1/'
        + module.ADAPTER
        + '/'
        + module.sha(archive)
        + '.archive',
        'remote_verified': False,
        'local_deletion_performed': False,
    }
    write(layout.intent, intent)
    daily = {
        'daily_id': module.DAILY,
        'state': 'accept_failed',
        'error_code': 'ACCEPT_FAILED_OR_UNVERIFIED',
        'image': module.IMAGE,
    }
    write(layout.latest, daily)
    write(layout.history, daily)

    operation = {
        'operation_id': module.OPERATION,
        'created_at': NOW - 293.471323,
        'updated_at': NOW,
        'state': 'upload_unknown',
        'archive': str(layout.archive),
        'receipt': None,
        'error_code': 'CLOUDBASE_BACKUP_UPLOAD_UNKNOWN',
        'policy_sha256': 'p' * 64,
    }
    plan = {
        'operation_id': module.OPERATION,
        'created_at': NOW - 286.352554,
        'maximum_network_bytes': 2 * len(archive) + 3_000_000,
        'maximum_requests': 7,
    }
    requests = []
    for number in range(3):
        requests.append(
            {
                'request_id': 'fixture-get-' + str(number),
                'operation_id': module.OPERATION,
                'created_at': NOW - 286 + number,
                'day': '2026-11-04',
                'month': '2026-11',
                'method': 'GET',
                'reserved_bytes': 300_100,
                'state': 'done',
                'error_code': None,
            }
        )
    requests.append(
        {
            'request_id': 'fixture-put',
            'operation_id': module.OPERATION,
            'created_at': NOW - 283,
            'day': '2026-11-04',
            'month': '2026-11',
            'method': 'PUT',
            'reserved_bytes': len(archive) + module.REQUEST_ALLOWANCE,
            'state': 'unknown',
            'error_code': 'CLOUDBASE_BACKUP_UPLOAD_UNKNOWN',
        }
    )
    layout.state.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(layout.ledger) as db:
        db.executescript(
            '''CREATE TABLE operations(operation_id TEXT PRIMARY KEY,created_at REAL NOT NULL,
            updated_at REAL NOT NULL,state TEXT NOT NULL,archive TEXT,receipt TEXT,error_code TEXT,policy_sha256 TEXT NOT NULL);
            CREATE TABLE plans(operation_id TEXT PRIMARY KEY,created_at REAL NOT NULL,
            maximum_network_bytes INTEGER NOT NULL,maximum_requests INTEGER NOT NULL);
            CREATE TABLE requests(request_id TEXT PRIMARY KEY,operation_id TEXT NOT NULL,created_at REAL NOT NULL,
            day TEXT NOT NULL,month TEXT NOT NULL,method TEXT NOT NULL,reserved_bytes INTEGER NOT NULL,state TEXT NOT NULL,error_code TEXT);'''
        )
        db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?)', tuple(operation.values()))
        db.execute('INSERT INTO plans VALUES(?,?,?,?)', tuple(plan.values()))
        for request in requests:
            db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?)', tuple(request.values()))
    layout.ledger.chmod(0o600)
    monkeypatch.setattr(module, 'ARCHIVE_BYTES', len(archive))
    monkeypatch.setattr(module, 'ARCHIVE_SHA', module.sha(archive))
    monkeypatch.setattr(module, 'CONFIG_SHA', module.sha(config))
    monkeypatch.setattr(module, 'INTENT_SHA', module.sha(layout.intent.read_bytes()))
    monkeypatch.setattr(module, 'DAILY_SHA', module.sha(layout.latest.read_bytes()))
    monkeypatch.setattr(module, 'OPERATION_SHA', module.sha(module.wire(operation)))
    monkeypatch.setattr(module, 'PLAN_SHA', module.sha(module.wire(plan)))
    monkeypatch.setattr(module, 'REQUESTS_SHA', module.sha(module.wire(requests)))
    return SimpleNamespace(layout=layout, operation=operation, plan=plan, requests=requests)


def observation(status=404, error='CLOUDBASE_BACKUP_REMOTE_NOT_FOUND'):
    result = (
        {'bytes': module.ARCHIVE_BYTES, 'method': 'head_only', 'remote_verified': False}
        if status == 200
        else None
    )
    return {'http_status': status, 'error_code': error, 'head_result': result}


def apply(fixture, answer=None):
    intent = fixture.layout.proof / 'intent.json'
    if intent.exists():
        expected = json.loads(intent.read_bytes())['plan_sha256']
    else:
        observed = module.build_plan(fixture.layout)
        expected = module.sha(module.wire(observed))
    answer = answer or observation()
    return module.apply(
        fixture.layout,
        expected,
        host_guard=lambda: None,
        probe=lambda *_: answer,
    )


def test_default_plan_is_offline_and_preserves_ledger_and_failed_daily(owned):
    before = rows(owned.layout)
    daily = owned.layout.latest.read_bytes()
    value = module.build_plan(owned.layout)
    assert value['external_requests'] == 1
    assert value['puts'] == value['gets'] == value['deletions'] == value['refund_bytes'] == 0
    assert rows(owned.layout) == before
    assert owned.layout.latest.read_bytes() == daily
    assert not owned.layout.proof.exists()


def test_exact_404_closes_only_the_upload_and_preserves_all_reservations(owned):
    before = rows(owned.layout)
    local_archive = owned.layout.archive.read_bytes()
    history = owned.layout.history.read_bytes()
    value = apply(owned)
    after = rows(owned.layout)
    assert value['state'] == 'remote_absent_reconciled'
    assert value['puts'] == value['gets'] == value['deletions'] == value['refund_bytes'] == 0
    assert before['requests'] == after['requests'][:-1]
    assert before['plans'] == after['plans']
    assert after['operations'][0]['state'] == module.TERMINAL
    assert after['operations'][0]['error_code'] == module.ERROR
    assert after['requests'][-1]['method'] == 'HEAD'
    assert after['requests'][-1]['state'] == 'done'
    assert owned.layout.archive.read_bytes() == local_archive
    assert not owned.layout.latest.exists()
    assert owned.layout.history.read_bytes() == history
    assert owned.layout.failed_latest.read_bytes() == history
    assert module.verify_completed(owned.layout) == value
    assert apply(owned) == value


@pytest.mark.parametrize(
    'answer',
    [
        {'http_status': 200, 'error_code': None, 'head_result': None},
        observation(403, 'CLOUDBASE_BACKUP_ACCESS_DENIED'),
        observation(500, 'CLOUDBASE_BACKUP_HTTP_REJECTED'),
        observation(None, 'CLOUDBASE_BACKUP_NETWORK_ERROR'),
    ],
)
def test_non_404_head_never_marks_remote_absent_or_moves_daily(owned, answer):
    before = deepcopy(rows(owned.layout))
    with pytest.raises(ValueError, match='ACTUAL_404_REQUIRED'):
        apply(owned, answer)
    after = rows(owned.layout)
    assert after['operations'][0]['state'] == 'upload_unknown'
    assert before['plans'] == after['plans']
    assert owned.layout.latest.exists()
    assert not owned.layout.failed_latest.exists()
    assert not (owned.layout.proof / 'receipt.json').exists()


@pytest.mark.parametrize(
    'change',
    ['archive', 'intent', 'daily', 'history', 'operation', 'plan', 'request'],
)
def test_every_frozen_input_rejects_changes_before_network(owned, change):
    layout = owned.layout
    file = {
        'archive': layout.archive,
        'intent': layout.intent,
        'daily': layout.latest,
        'history': layout.history,
    }.get(change)
    if file:
        file.write_bytes(file.read_bytes() + b' ')
    else:
        with sqlite3.connect(layout.ledger) as db:
            if change == 'operation':
                db.execute('UPDATE operations SET updated_at=updated_at+1')
            elif change == 'plan':
                db.execute('UPDATE plans SET maximum_requests=8')
            else:
                db.execute("UPDATE requests SET reserved_bytes=reserved_bytes+1 WHERE request_id='fixture-put'")
    with pytest.raises(ValueError):
        module.build_plan(layout)
    assert not layout.proof.exists()


def test_unsafe_or_oversized_head_observation_is_rejected(owned):
    observed = module.build_plan(owned.layout)
    for value in (
        {'http_status': 404, 'error_code': 'Cookie:secret', 'head_result': None},
        {'http_status': 200, 'head_result': {'bytes': 1, 'method': 'head_only', 'remote_verified': True}},
        {'http_status': 404, 'error_code': 'CLOUDBASE_BACKUP_REMOTE_NOT_FOUND', 'head_result': None, 'headers': {}},
    ):
        with pytest.raises(ValueError, match='UNSAFE_OBSERVATION'):
            module.validate_observation(value, 'recovery')
    assert observed['archive_key'].endswith(module.ARCHIVE_SHA + '.archive')


def test_completed_receipt_cannot_be_rewritten_to_claim_backup_success(owned):
    apply(owned)
    receipt = owned.layout.proof / 'receipt.json'
    value = json.loads(receipt.read_bytes())
    value['backup_success_claimed'] = True
    receipt.write_bytes(module.wire(value))
    with pytest.raises(ValueError, match='FINAL_RECEIPT_CHANGED'):
        module.verify_completed(owned.layout)


def test_host_guard_checks_service_and_all_fixed_backup_containers(monkeypatch):
    calls = []

    def command(args, timeout=20):
        calls.append(args)
        return 'failed' if len(calls) == 1 else ''

    monkeypatch.setattr(module, 'command', command)
    module.guard()
    assert len(calls) == 4
    assert not any('timer' in str(value) or 'stop' in value for value in calls)


def test_head_child_transport_contract_is_head_only():
    source = Path(module.__file__).read_text()
    assert "method == 'HEAD'" in source
    assert "method='PUT'" not in source
    assert 'object_puts' not in source
