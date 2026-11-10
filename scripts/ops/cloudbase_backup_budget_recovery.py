"""Resume explicitly attested preupload failures or reconciled absent uploads.

The host's original daily locks and runtime runner lock remain authoritative.
No ledger mutation, refunds, remote requests, receipt rewriting or backup cleanup
occur here. A root-owned proof must bind the failed daily and operation records.
The failed history is retained when the normal runner starts a successor.
"""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import tomllib
import uuid
from zoneinfo import ZoneInfo


VERSION = 'cloudbase-preupload-budget-proof-v1'
ERROR = 'CLOUDBASE_BACKUP_BUDGET_EXCEEDED'
ABSENT_VERSION = 'cloudbase-remote-absent-proof-v1'


def absent_proof_path(state, previous):
    if not previous or previous.get('state') != 'accept_started':
        return None
    value = previous.get('daily_id')
    try:
        if str(uuid.UUID(value)) != value:
            return None
    except (ValueError, TypeError, AttributeError):
        return None
    return state / 'remote-absent-recovery' / (value + '.json')


class BudgetDeferred(Exception):
    def __init__(self, report):
        self.report = report
        super().__init__('CLOUDBASE_BACKUP_BUDGET_DEFERRED')


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def need(value, code):
    if not value:
        raise ValueError(code)


def verify_failure(previous, proof, operation, *, latest_sha, history_sha,
                   request_count, plan_count, unresolved_count):
    """Pure invariant check; never infer permission from an error string alone."""
    need(proof.get('version') == VERSION, 'BUDGET_RECOVERY_PROOF_VERSION')
    need(previous.get('state') == 'accept_failed' and
         previous.get('error_code') == 'ACCEPT_FAILED_OR_UNVERIFIED' and
         'acceptance' not in previous, 'BUDGET_RECOVERY_DAILY_NOT_PREUPLOAD')
    need(proof.get('daily_id') == previous['daily_id'] and
         proof.get('failed_daily_sha256') == latest_sha == history_sha,
         'BUDGET_RECOVERY_DAILY_CHANGED')
    need(proof.get('operation_id') == operation['operation_id'] and
         proof.get('operation_sha256') == hashlib.sha256(encoded(operation)).hexdigest(),
         'BUDGET_RECOVERY_OPERATION_CHANGED')
    # The runtime packs locally before budget preflight, so a local archive may
    # exist. It is bound by the operation hash and never opened/deleted here.
    need(operation['state'] == 'failed' and operation['error_code'] == ERROR and
         (operation['archive'] is None or isinstance(operation['archive'], str)) and
         operation['receipt'] is None,
         'BUDGET_RECOVERY_NOT_PREUPLOAD_FAILURE')
    start = datetime.fromisoformat(previous['created_at'])
    end = datetime.fromisoformat(previous['updated_at'])
    need(start.tzinfo is not None and end.tzinfo is not None and
         start.timestamp() <= operation['created_at'] <= operation['updated_at'] <= end.timestamp(),
         'BUDGET_RECOVERY_OPERATION_OUTSIDE_DAILY')
    need(request_count == plan_count == unresolved_count == 0,
         'BUDGET_RECOVERY_REMOTE_INTENT_OR_ACTIVE_OPERATION')
    need(proof.get('external_requests') == 0 and
         proof.get('budget_refunded') is False and
         proof.get('backup_success_claimed') is False,
         'BUDGET_RECOVERY_INVALID_ATTESTATION')


def headroom(limits, periods):
    # Same seven-request plan as the pinned runtime, using maximum archive and
    # canary sizes. This is a conservative early gate, not provider billing.
    keys = ('daily_network_bytes', 'monthly_network_bytes', 'daily_requests',
            'monthly_requests', 'max_archive_bytes')
    need(all(type(limits.get(k)) is int and limits[k] > 0 for k in keys),
         'BUDGET_RECOVERY_LIMITS_INVALID')
    required_bytes = 2 * limits['max_archive_bytes'] + 2 * 262144 + 3 * 8192 + 7 * 300000
    enough = all(periods[p]['bytes'] + required_bytes <= limits[p + '_network_bytes'] and
                 periods[p]['requests'] + 7 <= limits[p + '_requests']
                 for p in ('daily', 'monthly'))
    return {'state': 'budget_available' if enough else 'budget_deferred',
            'required_network_bytes_upper': required_bytes, 'required_requests': 7,
            'periods': periods, 'limits': {k: limits[k] for k in keys},
            'external_requests': 0, 'budget_refunded': False,
            'backup_started': False, 'backup_success_claimed': False}


def verify_absence(previous, proof, operation, requests, plan, *, latest_sha,
                   history_sha, unresolved_count):
    """A real, budgeted 404 closes one exact interrupted upload, never success."""
    need(proof.get('version') == ABSENT_VERSION and previous['state'] == 'accept_started'
         and 'acceptance' not in previous, 'ABSENT_RECOVERY_DAILY_INVALID')
    need(proof.get('daily_id') == previous['daily_id'] and
         proof.get('failed_daily_sha256') == latest_sha == history_sha,
         'ABSENT_RECOVERY_DAILY_CHANGED')
    need(proof.get('operation_id') == operation['operation_id'] and
         proof.get('operation_sha256') == hashlib.sha256(encoded(operation)).hexdigest() and
         operation['state'] == 'resolved_remote_absent' and
         operation['error_code'] == 'CLOUDBASE_BACKUP_UPLOAD_CONFIRMED_ABSENT' and
         operation['receipt'] is None, 'ABSENT_RECOVERY_OPERATION_CHANGED')
    need(proof.get('requests_sha256') == hashlib.sha256(encoded(requests)).hexdigest() and
         proof.get('plan_sha256') == hashlib.sha256(encoded(plan)).hexdigest(),
         'ABSENT_RECOVERY_LEDGER_CHANGED')
    head = [r for r in requests if r['request_id'] == proof.get('head_request_id')]
    originals = [r for r in requests if r['request_id'] != proof.get('head_request_id')]
    need(len(head) == 1 and head[0]['method'] == 'HEAD' and head[0]['state'] == 'error' and
         head[0]['error_code'] == 'CLOUDBASE_BACKUP_REMOTE_NOT_FOUND' and
         head[0]['reserved_bytes'] == 300000, 'ABSENT_RECOVERY_HEAD_NOT_ABSENT')
    need(len(originals) == 4 and sum(r['method'] == 'GET' and r['state'] == 'done' for r in originals) == 3
         and sum(r['method'] == 'PUT' and r['state'] == 'reserved' for r in originals) == 1
         and proof.get('original_requests_sha256') == hashlib.sha256(encoded(originals)).hexdigest(),
         'ABSENT_RECOVERY_ORIGINAL_REQUESTS_CHANGED')
    need(all(r['operation_id'] == operation['operation_id'] for r in requests) and
         plan['operation_id'] == operation['operation_id'] and unresolved_count == 0,
         'ABSENT_RECOVERY_ACTIVE_OR_FOREIGN_OPERATION')
    observation = proof.get('head_result', {})
    need(observation.get('operation_id') == operation['operation_id'] and
         observation.get('state') == 'absent' and observation.get('request') == 'HEAD' and
         observation.get('error_code') == 'CLOUDBASE_BACKUP_REMOTE_NOT_FOUND' and
         observation.get('remote_verified') is False and observation.get('puts') == observation.get('gets') == 0 and
         proof.get('budget_refunded') is False and proof.get('backup_success_claimed') is False,
         'ABSENT_RECOVERY_INVALID_ATTESTATION')


def inspect(daily, root, state, previous):
    """Read a consistent ledger snapshot; retain all existing file guards."""
    policy = daily.safe(root / 'config/cloudbase-backup.toml', root)
    daily.protected(policy, file=True)
    need(policy.stat().st_size <= 65536, 'BUDGET_RECOVERY_POLICY_TOO_LARGE')
    limits = tomllib.loads(policy.read_text(encoding='utf-8-sig'))['limits']
    ledger = daily.safe(root / 'data/cloudbase-backup-state/ledger.sqlite3', root)
    need(ledger.is_file(), 'BUDGET_RECOVERY_LEDGER_MISSING')
    current_time = datetime.now(ZoneInfo('Asia/Shanghai'))
    periods = {}
    db = sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True, timeout=15)
    db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN')
        if previous and previous['state'] == 'accept_failed':
            ident = daily.ident(previous['daily_id'])
            proof, _ = daily.read_record(state / 'budget-recovery' / (ident + '.json'), state, root_owned=True)
            operation_id = daily.ident(proof['operation_id'])
            current, latest_sha = daily.read_record(state / 'latest.json', state, root_owned=True)
            history, history_sha = daily.read_record(state / 'history' / (ident + '.json'), state, root_owned=True)
            need(current == history == previous, 'BUDGET_RECOVERY_POINTER_CHANGED')
            row = db.execute('SELECT * FROM operations WHERE operation_id=?', (operation_id,)).fetchone()
            need(row is not None, 'BUDGET_RECOVERY_OPERATION_MISSING')
            counts = [db.execute('SELECT COUNT(*) FROM ' + table + ' WHERE operation_id=?',
                                 (operation_id,)).fetchone()[0] for table in ('requests', 'plans')]
            unresolved = db.execute("SELECT COUNT(*) FROM operations WHERE state IN ('preparing','uploading','upload_unknown')").fetchone()[0]
            verify_failure(previous, proof, dict(row), latest_sha=latest_sha,
                           history_sha=history_sha, request_count=counts[0],
                           plan_count=counts[1], unresolved_count=unresolved)
        elif previous and previous['state'] == 'accept_started':
            path = absent_proof_path(state, previous)
            need(path is not None, 'ABSENT_RECOVERY_DAILY_INVALID')
            proof, _ = daily.read_record(path, state, root_owned=True)
            ident = daily.ident(previous['daily_id'])
            current, latest_sha = daily.read_record(state / 'latest.json', state, root_owned=True)
            history, history_sha = daily.read_record(state / 'history' / (ident + '.json'), state, root_owned=True)
            need(current == history == previous, 'ABSENT_RECOVERY_POINTER_CHANGED')
            operation_id = daily.ident(proof['operation_id'])
            row = db.execute('SELECT * FROM operations WHERE operation_id=?', (operation_id,)).fetchone()
            plan = db.execute('SELECT * FROM plans WHERE operation_id=?', (operation_id,)).fetchone()
            need(row is not None and plan is not None, 'ABSENT_RECOVERY_OPERATION_MISSING')
            requests = [dict(r) for r in db.execute('SELECT * FROM requests WHERE operation_id=? ORDER BY created_at,request_id', (operation_id,))]
            unresolved = db.execute("SELECT COUNT(*) FROM operations WHERE state IN ('preparing','uploading','upload_unknown')").fetchone()[0]
            verify_absence(previous, proof, dict(row), requests, dict(plan), latest_sha=latest_sha,
                           history_sha=history_sha, unresolved_count=unresolved)
        for label, field, value in (('daily', 'day', current_time.strftime('%Y-%m-%d')),
                                    ('monthly', 'month', current_time.strftime('%Y-%m'))):
            row = db.execute('SELECT COUNT(*),COALESCE(SUM(reserved_bytes),0) FROM requests WHERE ' + field + '=?', (value,)).fetchone()
            periods[label] = {'period': value, 'requests': row[0], 'bytes': row[1]}
    finally:
        db.close()
    return headroom(limits, periods)


def bind(daily, *, root=Path('/opt/lighthouse-hub'),
         state=Path('/var/lib/lighthouse-backup-ops/retention-v2b/daily-state'),
         inspector=inspect):
    """Called once before the original daily main acquires its exclusive locks."""
    original = daily.execute

    def execute(previous=None, **kwargs):
        # Cleanup resumes exact verified work; never let network budget block it.
        # Interrupted/unknown acceptance retains the original reconciliation gate.
        absent = absent_proof_path(state, previous)
        eligible = previous is None or previous['state'] in ('completed', 'accept_failed') or (absent is not None and absent.is_file())
        if not eligible:
            return original(previous, **kwargs)
        report = inspector(daily, root, state, previous)
        report = {**report, 'previous_daily_id': previous['daily_id'] if previous else None,
                  'failure_history_preserved': True}
        daily.atomic(state / 'budget-recovery' / 'last-check.json', report)
        if report['state'] == 'budget_deferred':
            raise BudgetDeferred(report)
        need(report['state'] == 'budget_available', 'BUDGET_RECOVERY_BAD_DECISION')
        # Only inspect()'s exact root-attested failed operation can reach this.
        # The failed record stays in history; original save() creates a new ID.
        if previous and previous['state'] in ('accept_failed', 'accept_started'):
            previous = None
        return original(previous, **kwargs)

    daily.execute = execute
    return execute
