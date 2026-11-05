"""One-off reconciliation of the interrupted 2026-11-04 14:44 backup PUT.

The operator stopped the run 15 minutes after the first PUT was dispatched. COS
proves the exact archive key absent, so the operation is closed as
`resolved_remote_absent`: no retry, refund, deletion or success claim. The stale
`accept_started` daily pointer that refuses every later 03:30 run is moved, not
rewritten, into the same root-owned proof directory.

Read-only `--verify` mode performs zero network requests and changes nothing.
`--apply` reserves exactly one bounded HEAD, and only a 404 permits the
transition. The original operation, plan, request reservations, local archive,
failed daily record and its history are preserved.
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, '/opt/lighthouse-hub')
from adapters.cloudbase_cos_backup import CloudBaseCOSConfig, CloudBaseCOSStorage
from services.hub.errors import HubError

VERSION = 'cloudbase-upload-unknown-reconcile-fe876155-v2'
OPERATION = 'fe876155-f773-405e-bdf5-e46539ba22fb'
ADAPTER = '8cde3a68-6a58-49a3-8cb6-e56c881299a1'
DAILY = '2f9e5c31-67f0-4b74-a6d3-968b7c5237a9'
ARCHIVE_BYTES = 56_179_697
ARCHIVE_SHA = 'bb62dd804a81351b88fcad8a04998cc820e31ed86578786dec8aa6aecd05556e'
TERMINAL = 'resolved_remote_absent'
ERROR = 'CLOUDBASE_BACKUP_UPLOAD_CONFIRMED_ABSENT'
REQUEST_ALLOWANCE = 300_000
HEAD_REQUEST = str(uuid.uuid5(uuid.UUID(OPERATION), VERSION))

ROOT = Path('/opt/lighthouse-hub')
OPS = Path('/var/lib/lighthouse-backup-ops')
STATE = ROOT / 'data/cloudbase-backup-state'
LEDGER = STATE / 'ledger.sqlite3'
DAILY_STATE = OPS / 'retention-v2b/daily-state'
LATEST = DAILY_STATE / 'latest.json'
ARCHIVE = ROOT / 'backups/cloudbase-staging' / OPERATION / 'operational-backup.tar.gz'
INTENT = ROOT / 'backups/cloudbase-receipts' / (ADAPTER + '.intent.json')
CONFIG = ROOT / 'config/cloudbase-backup.toml'
SECRET = ROOT / 'secrets/cloudbase-backup.json'
PROOF = OPS / 'upload-unknown-reconciliation-v2' / OPERATION


def need(value, code):
    if not value:
        raise ValueError(code)


def wire(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def sha(raw):
    if isinstance(raw, str):
        raw = raw.encode()
    return hashlib.sha256(raw).hexdigest()


def safe(path):
    path = Path(path)
    need(path.is_absolute() and path.resolve() == path and
         not any(p.is_symlink() for p in (path, *path.parents)), 'RECONCILE_UNSAFE_PATH')
    return path


def read(path, maximum=1_048_576):
    path = safe(path)
    with open(path, 'rb') as stream:
        raw = stream.read(maximum + 1)
    need(0 < len(raw) <= maximum, 'RECONCILE_DOCUMENT_INVALID')
    need(path.read_bytes() == raw, 'RECONCILE_DOCUMENT_CHANGED')
    return raw


def document(path, maximum=1_048_576):
    raw = read(path, maximum)
    return json.loads(raw), {'file': str(path), 'sha256': sha(raw), 'bytes': len(raw)}


def ledger(readonly):
    need(LEDGER.stat().st_uid == 10001 and LEDGER.stat().st_mode & 0o077 == 0, 'RECONCILE_LEDGER_OWNER_CHANGED')
    db = sqlite3.connect(LEDGER.as_uri() + ('?mode=ro' if readonly else '?mode=rw'), uri=True,
                         timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=15000')
    db.execute('PRAGMA query_only=ON' if readonly else 'PRAGMA synchronous=FULL')
    return db


def rows(db, sql, args=()):
    return [dict(row) for row in db.execute(sql, args)]


def baseline(db):
    operation = rows(db, 'SELECT * FROM operations WHERE operation_id=?', (OPERATION,))
    plans = rows(db, 'SELECT * FROM plans WHERE operation_id=?', (OPERATION,))
    requests = rows(db, 'SELECT * FROM requests WHERE operation_id=? AND request_id<>? '
                         'ORDER BY created_at,request_id', (OPERATION, HEAD_REQUEST))
    head = rows(db, 'SELECT * FROM requests WHERE request_id=?', (HEAD_REQUEST,))
    need(len(operation) == len(plans) == 1, 'RECONCILE_ORIGINAL_MISSING')
    current = operation[0]
    need(current['state'] == 'uploading' and current['error_code'] is None, 'RECONCILE_STATE_CHANGED')
    need(Path(current['archive']) == ARCHIVE, 'RECONCILE_ARCHIVE_PATH_CHANGED')
    need(len(requests) == 4 and plans[0]['maximum_requests'] == 7, 'RECONCILE_REQUEST_SET_CHANGED')
    gets = [item for item in requests if item['method'] == 'GET']
    puts = [item for item in requests if item['method'] == 'PUT']
    need(len(gets) == 3 and all(item['state'] == 'done' for item in gets), 'RECONCILE_READ_SET_CHANGED')
    need(len(puts) == 1 and puts[0]['state'] == 'reserved' and puts[0]['error_code'] is None and
         puts[0]['reserved_bytes'] == ARCHIVE_BYTES + REQUEST_ALLOWANCE, 'RECONCILE_PUT_CHANGED')
    need(not rows(db, 'SELECT operation_id FROM operations WHERE operation_id<>? '
                      "AND state IN ('preparing','uploading','upload_unknown')", (OPERATION,)),
         'RECONCILE_ANOTHER_UNRESOLVED_OPERATION')
    need(len(head) == 0, 'RECONCILE_HEAD_ALREADY_PRESENT')
    return {'operation': current, 'plan': plans[0], 'requests': requests}


def verify_original():
    """Read-only, zero network: the exact interrupted shape must still be intact."""
    db = ledger(True)
    try:
        db.execute('BEGIN')
        value = baseline(db)
    finally:
        db.close()
    daily, daily_ref = document(LATEST)
    need(daily.get('daily_id') == DAILY and daily.get('state') == 'accept_started', 'RECONCILE_DAILY_CHANGED')
    need(daily_ref['sha256'] == '484eff02df789609215cf53383fada0786a07e5674c95114d808617bd91179ac',
         'RECONCILE_DAILY_HASH_CHANGED')
    need(ARCHIVE.stat().st_size == ARCHIVE_BYTES and sha(ARCHIVE.read_bytes()) == ARCHIVE_SHA,
         'RECONCILE_ARCHIVE_CHANGED')
    return value, daily_ref


def config():
    import tomllib
    value = tomllib.loads(read(CONFIG, 65536).decode('utf-8-sig'))
    options = dict(value['cloudbase'])
    for key in ('private_canary_reference', 'private_canary_sha256', 'credential_file'):
        options.pop(key, None)
    options['storage_hosts'] = tuple(options.get('storage_hosts', []))
    return CloudBaseCOSConfig(**options)


def head_absent():
    cfg = config()
    credentials = json.loads(read(SECRET, 16384))
    storage = CloudBaseCOSStorage(cfg, lambda: credentials, request_timeout=300)
    intent, _ = document(INTENT)
    key = intent['archive_key']
    need(key == 'lighthouse-hub/backups/v1/' + ADAPTER + '/' + ARCHIVE_SHA + '.archive', 'RECONCILE_KEY_CHANGED')
    need(intent['bytes'] == ARCHIVE_BYTES and intent['sha256'] == ARCHIVE_SHA, 'RECONCILE_INTENT_CHANGED')
    try:
        observed = storage.head('cos://' + cfg.bucket + '/' + key, key)
    except HubError as exc:
        need(exc.code == 'CLOUDBASE_BACKUP_REMOTE_NOT_FOUND', 'RECONCILE_HEAD_NOT_ABSENT')
        return {'key': key, 'request': 'HEAD', 'http_status': 404, 'state': 'absent',
                'reserved_bytes': REQUEST_ALLOWANCE, 'object_bytes': None}
    need(False, 'RECONCILE_OBJECT_PRESENT:' + str(observed.get('bytes')))


def apply():
    need(os.geteuid() == 0, 'RECONCILE_NOT_ROOT')
    os.umask(0o077)
    value, daily_ref = verify_original()
    PROOF.mkdir(mode=0o700, parents=True, exist_ok=True)
    need(PROOF.stat().st_uid == 0 and PROOF.stat().st_mode & 0o077 == 0, 'RECONCILE_PROOF_DIR_INVALID')
    (PROOF / 'ledger-before.sqlite3').unlink(missing_ok=True)
    source = sqlite3.connect(LEDGER.as_uri() + '?mode=ro', uri=True)
    target = sqlite3.connect(str(PROOF / 'ledger-before.sqlite3'))
    try:
        source.backup(target)
    finally:
        source.close()
        target.close()
    (PROOF / 'ledger-before.sqlite3').chmod(0o600)
    ledger_raw = (PROOF / 'ledger-before.sqlite3').read_bytes()
    (PROOF / 'daily-latest-before.json').write_bytes(read(LATEST))
    (PROOF / 'daily-latest-before.json').chmod(0o600)
    plan = {
        'version': VERSION, 'operation_id': OPERATION, 'adapter_receipt': ADAPTER, 'daily_id': DAILY,
        'archive': str(ARCHIVE), 'archive_sha256': ARCHIVE_SHA, 'archive_bytes': ARCHIVE_BYTES,
        'object_key': 'lighthouse-hub/backups/v1/' + ADAPTER + '/' + ARCHIVE_SHA + '.archive',
        'ledger_sha256': sha(ledger_raw), 'ledger_state': value['operation']['state'],
        'ledger_error_code': value['operation']['error_code'],
        'requests': [{'request_id': item['request_id'], 'method': item['method'], 'state': item['state'],
                      'reserved_bytes': item['reserved_bytes']} for item in value['requests']],
        'daily_latest_sha256': daily_ref['sha256'], 'daily_state': 'accept_started',
        'planned_external_requests': 1, 'planned_method': 'HEAD', 'refund_bytes': 0,
        'retry_performed': False, 'backup_success_claimed': False,
    }
    (PROOF / 'intent.json').write_bytes(wire(plan).encode())
    existing = PROOF / 'head-receipt.json'
    if existing.exists():
        # A previous attempt proved absence and stopped before the transaction.
        receipt = json.loads(read(existing))
        need(receipt.get('state') == 'absent' and receipt.get('http_status') == 404, 'RECONCILE_HEAD_CHANGED')
        reused = True
    else:
        receipt = head_absent()
        (PROOF / 'head-receipt.json').write_bytes(wire(receipt).encode())
        reused = False
    now = time.time()
    db = ledger(False)
    try:
        db.execute('BEGIN IMMEDIATE')
        current = rows(db, 'SELECT * FROM operations WHERE operation_id=?', (OPERATION,))
        need(len(current) == 1 and sha(wire(current[0])) == sha(wire(value['operation'])),
             'RECONCILE_OPERATION_CHANGED')
        need(not rows(db, 'SELECT 1 FROM requests WHERE request_id=?', (HEAD_REQUEST,)),
             'RECONCILE_HEAD_ALREADY_PRESENT')
        db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?)',
                   (HEAD_REQUEST, OPERATION, now, time.strftime('%Y-%m-%d', time.localtime(now)),
                    time.strftime('%Y-%m', time.localtime(now)), 'HEAD', REQUEST_ALLOWANCE, 'done', None))
        updated = db.execute('UPDATE operations SET state=?,error_code=?,updated_at=? WHERE operation_id=?',
                             (TERMINAL, ERROR, now, OPERATION))
        need(updated.rowcount == 1, 'RECONCILE_OPERATION_MISSING')
        db.execute('COMMIT')
        db.execute('PRAGMA wal_checkpoint(FULL)')
    except BaseException:
        db.execute('ROLLBACK')
        raise
    finally:
        db.close()
    need(sha(read(LATEST)) == daily_ref['sha256'], 'RECONCILE_DAILY_CHANGED_DURING_APPLY')
    LATEST.unlink()
    directory = os.open(DAILY_STATE, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    (PROOF / 'ledger-after.sqlite3').unlink(missing_ok=True)
    source = sqlite3.connect(LEDGER.as_uri() + '?mode=ro', uri=True)
    target = sqlite3.connect(str(PROOF / 'ledger-after.sqlite3'))
    try:
        source.backup(target)
    finally:
        source.close()
        target.close()
    (PROOF / 'ledger-after.sqlite3').chmod(0o600)
    after = {'version': VERSION, 'state': 'remote_absent_reconciled', 'operation_id': OPERATION, 'daily_id': DAILY,
             'head_receipt': receipt, 'head_receipt_sha256': sha((PROOF / 'head-receipt.json').read_bytes()),
             'intent_sha256': sha((PROOF / 'intent.json').read_bytes()),
             'ledger_before_sha256': plan['ledger_sha256'],
             'ledger_after_sha256': hashlib.sha256((PROOF / 'ledger-after.sqlite3').read_bytes()).hexdigest(),
             'daily_latest_sha256': daily_ref['sha256'], 'active_latest_absent': not LATEST.exists(),
             'history_preserved': (DAILY_STATE / 'history' / (DAILY + '.json')).exists(),
             'original_operation_preserved': True, 'original_requests_preserved': True,
             'local_archive_preserved': ARCHIVE.exists(), 'no_refund': True, 'puts': 0, 'gets': 0,
             'deletions': 0, 'external_requests': 0 if reused else 1, 'head_reused': reused,
             'retry_performed': False,
             'backup_success_claimed': False, 'restore_verified': False,
             'last_verified_backup': '9e9c7ed9-faac-4faf-b753-956d517ddef5'}
    (PROOF / 'receipt.json').write_bytes(wire(after).encode())
    return after


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--verify', action='store_true')
    mode.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.verify:
        value, daily_ref = verify_original()
        print(json.dumps({'version': VERSION, 'state': 'verified_interrupted_shape',
                          'operation_state': value['operation']['state'],
                          'daily_latest_sha256': daily_ref['sha256'],
                          'external_requests': 0, 'changed': False}))
        return 0
    print(json.dumps(apply(), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except HubError as exc:
        print(json.dumps({'state': 'stopped', 'error_code': exc.code}))
        raise SystemExit(2) from None
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({'state': 'stopped', 'error_code': str(exc)[:200]}))
        raise SystemExit(2) from None
