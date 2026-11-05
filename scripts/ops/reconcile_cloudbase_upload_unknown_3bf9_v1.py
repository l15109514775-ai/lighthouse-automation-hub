"""Resolve the frozen 3bf9c747 CloudBase upload-unknown operation.

The default mode is offline. Apply reserves one bounded HEAD, sends only a
signed HEAD for the exact archive key, and transitions the operation only when
COS proves that key absent. No PUT, retry, refund, deletion or backup-success
claim is made. The failed daily record is retained and its active pointer is
moved only after the ledger transition is durable.
"""
from contextlib import contextmanager
from pathlib import Path
import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid

VERSION = 'cloudbase-upload-unknown-reconcile-3bf9-v1'
OPERATION = '3bf9c747-58c9-41d5-a510-eaf71efcc6ed'
ADAPTER = '5ec1b296-cec8-4f04-a420-d304e826be53'
DAILY = '1bb1724b-78c2-481c-ba72-7f307010fd36'
IMAGE = 'sha256:6592ebce93025293ac37e8c990f0f09daa639a0ed95354120a22371d2e9b6d55'
CONFIG_SHA = '5de7be2b0899208541780eaf6de82aae57538f1331c5505ea524769c33b1685d'
INTENT_SHA = '477053c091158c75b303a5d506e8a5405bc1e35abc9ac9d70d9acb2bd5f2dbfc'
DAILY_SHA = '39d046798d1ed78ff611838592e55aa7121f8208e60fa395f91d0872b9e1770c'
ARCHIVE_SHA = '21bfb0082ca6f35318e3528fc5c504780e9d5fecd8063790a98d33db1ffc4a72'
ARCHIVE_BYTES = 56_130_797
OPERATION_SHA = 'fa9fa2ee225ad0c32b492bd6ef1e0a47dd248715137e2faf59d37aac48a7cfea'
PLAN_SHA = 'e5f3da6c4213353c86fdb5f6f1c02199cf9c642e1fc0d210539f31f7db9430f8'
REQUESTS_SHA = '914b0824833448166f76eed2af86259c9115989a9268bd92b541e0bdc1f5bb68'
TERMINAL = 'resolved_remote_absent'
ERROR = 'CLOUDBASE_BACKUP_UPLOAD_CONFIRMED_ABSENT'
HEAD_REQUEST = str(uuid.uuid5(uuid.UUID(OPERATION), VERSION))
REQUEST_ALLOWANCE = 300_000
RUNTIME = Path('/usr/local/lib/lighthouse-backup-ops/retention-v2c/runtime')


class Layout:
    def __init__(self, root=Path('/opt/lighthouse-hub'), ops=Path('/var/lib/lighthouse-backup-ops')):
        self.root, self.ops = root, ops
        self.state = root / 'data/cloudbase-backup-state'
        self.ledger = self.state / 'ledger.sqlite3'
        self.daily = ops / 'retention-v2b/daily-state'
        self.latest = self.daily / 'latest.json'
        self.history = self.daily / 'history' / (DAILY + '.json')
        self.archive = root / 'backups/cloudbase-staging' / OPERATION / 'operational-backup.tar.gz'
        self.intent = root / 'backups/cloudbase-receipts' / (ADAPTER + '.intent.json')
        self.config = root / 'config/cloudbase-backup.toml'
        self.proof = ops / 'upload-unknown-reconciliation-v1' / OPERATION
        self.failed_latest = self.proof / 'failed-latest.json'
        self.locks = (
            (ops / 'daily.lock', 0),
            (self.daily / 'daily.lock', 0),
            (self.state / 'runner.lock/observer.lock', 10001),
        )


def need(value, code):
    if not value:
        raise ValueError(code)


def wire(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def safe(path):
    path = Path(path)
    need(
        path.is_absolute()
        and path.resolve() == path
        and not any(parent.is_symlink() for parent in (path, *path.parents)),
        'RECONCILE_UNSAFE_PATH',
    )
    return path


def owner(info, uid, private=False):
    need(
        info.st_uid == uid and not info.st_mode & (0o077 if private else 0o022),
        'RECONCILE_OWNER_OR_MODE_CHANGED',
    )


def read(path, uid=0, maximum=1_048_576):
    safe(path)
    with os.fdopen(
        os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)),
        'rb',
    ) as stream:
        before = os.fstat(stream.fileno())
        owner(before, uid)
        need(
            stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and 0 < before.st_size <= maximum,
            'RECONCILE_DOCUMENT_INVALID',
        )
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns') + (('st_ctime_ns',) if os.name == 'posix' else ())
    current = path.stat()
    need(
        len(raw) == before.st_size
        and all(getattr(before, key) == getattr(after, key) == getattr(current, key) for key in fields),
        'RECONCILE_DOCUMENT_CHANGED',
    )
    return raw


def document(path, uid=0, digest=None):
    raw = read(path, uid)
    if digest is not None:
        need(sha(raw) == digest, 'RECONCILE_BOUND_DOCUMENT_CHANGED')
    return json.loads(raw), {'file': str(path), 'sha256': sha(raw), 'bytes': len(raw)}


def connect(layout, readonly=False):
    safe(layout.ledger)
    owner(layout.ledger.stat(), 10001, True)
    db = sqlite3.connect(
        layout.ledger.as_uri() + ('?mode=ro' if readonly else '?mode=rw'),
        uri=True,
        timeout=5,
        isolation_level=None,
    )
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=5000')
    db.execute('PRAGMA query_only=ON' if readonly else 'PRAGMA synchronous=FULL')
    return db


def rows(db, sql, args=()):
    return [dict(row) for row in db.execute(sql, args)]


def original_rows(db):
    operation = rows(db, 'SELECT * FROM operations WHERE operation_id=?', (OPERATION,))
    plans = rows(db, 'SELECT * FROM plans WHERE operation_id=?', (OPERATION,))
    requests = rows(
        db,
        'SELECT * FROM requests WHERE operation_id=? AND request_id<>? ORDER BY created_at,request_id',
        (OPERATION, HEAD_REQUEST),
    )
    need(len(operation) == len(plans) == 1, 'RECONCILE_ORIGINAL_MISSING')
    need(sha(wire(operation[0])) == OPERATION_SHA, 'RECONCILE_ORIGINAL_OPERATION_CHANGED')
    need(sha(wire(plans[0])) == PLAN_SHA, 'RECONCILE_ORIGINAL_PLAN_CHANGED')
    need(sha(wire(requests)) == REQUESTS_SHA, 'RECONCILE_ORIGINAL_REQUESTS_CHANGED')
    puts = [item for item in requests if item['method'] == 'PUT']
    need(
        operation[0]['state'] == 'upload_unknown'
        and operation[0]['error_code'] == 'CLOUDBASE_BACKUP_UPLOAD_UNKNOWN'
        and len(requests) == 4
        and len(puts) == 1
        and puts[0]['state'] == 'unknown'
        and puts[0]['error_code'] == 'CLOUDBASE_BACKUP_UPLOAD_UNKNOWN'
        and puts[0]['reserved_bytes'] == ARCHIVE_BYTES + REQUEST_ALLOWANCE,
        'RECONCILE_ORIGINAL_STATE_CHANGED',
    )
    need(
        not rows(
            db,
            'SELECT operation_id FROM operations WHERE operation_id<>? '
            "AND state IN ('preparing','uploading','upload_unknown')",
            (OPERATION,),
        ),
        'RECONCILE_ANOTHER_UNRESOLVED_OPERATION',
    )
    return {'operation': operation[0], 'plan': plans[0], 'requests': requests}


def inspect_ledger(layout):
    db = connect(layout, True)
    try:
        db.execute('BEGIN')
        value = original_rows(db)
        need(
            not rows(db, 'SELECT * FROM requests WHERE request_id=?', (HEAD_REQUEST,)),
            'RECONCILE_HEAD_ALREADY_RESERVED',
        )
        return value
    finally:
        db.close()


def local_snapshot(layout, *, terminal=False):
    db = connect(layout, True)
    try:
        db.execute('BEGIN')
        operation = rows(db, 'SELECT * FROM operations WHERE operation_id=?', (OPERATION,))
        plans = rows(db, 'SELECT * FROM plans WHERE operation_id=?', (OPERATION,))
        requests = rows(
            db,
            'SELECT * FROM requests WHERE operation_id=? AND request_id<>? ORDER BY created_at,request_id',
            (OPERATION, HEAD_REQUEST),
        )
        head = rows(db, 'SELECT * FROM requests WHERE request_id=?', (HEAD_REQUEST,))
        need(len(operation) == len(plans) == 1, 'RECONCILE_LEDGER_MISSING')
        need(sha(wire(plans[0])) == PLAN_SHA, 'RECONCILE_PLAN_CHANGED')
        need(sha(wire(requests)) == REQUESTS_SHA, 'RECONCILE_REQUESTS_CHANGED')
        current = operation[0]
        if terminal:
            need(
                current['state'] == TERMINAL and current['error_code'] == ERROR,
                'RECONCILE_TERMINAL_STATE_CHANGED',
            )
        else:
            need(sha(wire(current)) == OPERATION_SHA, 'RECONCILE_OPERATION_CHANGED')
        if head:
            need(
                len(head) == 1
                and head[0]['operation_id'] == OPERATION
                and head[0]['method'] == 'HEAD'
                and head[0]['reserved_bytes'] == REQUEST_ALLOWANCE
                and head[0]['state'] in ('reserved', 'done', 'error'),
                'RECONCILE_HEAD_CHANGED',
            )
        need(
            not rows(
                db,
                'SELECT operation_id FROM operations WHERE operation_id<>? '
                "AND state IN ('preparing','uploading','upload_unknown')",
                (OPERATION,),
            ),
            'RECONCILE_ANOTHER_UNRESOLVED_OPERATION',
        )
        return {'operation': current, 'plan': plans[0], 'requests': requests, 'head': head}
    finally:
        db.close()


def child_action(layout, value):
    source = safe(Path(__file__))
    read(source)

    def as_app():
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)

    result = subprocess.run(
        ['/usr/bin/python3', str(source), '--child-ledger'],
        input=wire(value),
        capture_output=True,
        timeout=30,
        preexec_fn=as_app,
        env={
            'PATH': '/usr/bin:/bin',
            'HOME': '/tmp',
            'PYTHONNOUSERSITE': '1',
            'PYTHONDONTWRITEBYTECODE': '1',
        },
    )
    need(
        result.returncode == 0 and len(result.stdout) <= 1_048_576 and len(result.stderr) <= 65_536,
        'RECONCILE_LEDGER_CHILD_FAILED',
    )
    return json.loads(result.stdout)


def ledger_action(layout, value):
    if os.name == 'posix' and os.geteuid() == 0:
        return child_action(layout, value)
    action = value['action']
    if action == 'snapshot':
        return local_snapshot(layout, terminal=value['terminal'])
    if action == 'reserve':
        return reserve_local(layout, value['original'], value['limits'])
    if action == 'settle':
        return settle_local(layout, value['original'], value['observation'])
    if action == 'transition':
        return transition_local(layout, value['observation'], value['checked_at'])
    raise ValueError('RECONCILE_CHILD_ACTION_INVALID')


def reserve_local(layout, original, limits):
    db = connect(layout, False)
    now = time.time()
    try:
        db.execute('BEGIN IMMEDIATE')
        need(original_rows(db) == original, 'RECONCILE_ORIGINAL_CHANGED')
        need(not rows(db, 'SELECT * FROM requests WHERE request_id=?', (HEAD_REQUEST,)), 'RECONCILE_ALREADY_RESERVED')
        planned = original['plan']
        used = rows(
            db,
            'SELECT COUNT(*) calls,COALESCE(SUM(reserved_bytes),0) bytes FROM requests WHERE operation_id=?',
            (OPERATION,),
        )[0]
        need(
            used['calls'] + 1 <= planned['maximum_requests']
            and used['bytes'] + REQUEST_ALLOWANCE <= planned['maximum_network_bytes'],
            'RECONCILE_OPERATION_BUDGET_EXCEEDED',
        )
        moment = time.localtime(now)
        day, month = time.strftime('%Y-%m-%d', moment), time.strftime('%Y-%m', moment)
        for field, period, byte_limit, request_limit in (
            ('day', day, limits['daily_network_bytes'], limits['daily_requests']),
            ('month', month, limits['monthly_network_bytes'], limits['monthly_requests']),
        ):
            used = rows(
                db,
                'SELECT COUNT(*) calls,COALESCE(SUM(reserved_bytes),0) bytes FROM requests WHERE ' + field + '=?',
                (period,),
            )[0]
            need(
                used['calls'] + 1 <= request_limit and used['bytes'] + REQUEST_ALLOWANCE <= byte_limit,
                'RECONCILE_GLOBAL_BUDGET_EXCEEDED',
            )
        db.execute(
            'INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,NULL)',
            (HEAD_REQUEST, OPERATION, now, day, month, 'HEAD', REQUEST_ALLOWANCE, 'reserved'),
        )
        db.commit()
        return {'request_id': HEAD_REQUEST, 'state': 'reserved', 'reserved_bytes': REQUEST_ALLOWANCE}
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def settle_local(layout, original, observation):
    missing = observation.get('http_status') == 404 and observation.get('error_code') == 'CLOUDBASE_BACKUP_REMOTE_NOT_FOUND'
    state = 'done' if missing else 'error'
    error = observation.get('error_code', 'CLOUDBASE_BACKUP_HEAD_NOT_CONFIRMED_ABSENT')
    db = connect(layout, False)
    try:
        db.execute('BEGIN IMMEDIATE')
        need(original_rows(db) == original, 'RECONCILE_ORIGINAL_CHANGED_DURING_HEAD')
        found = rows(db, 'SELECT * FROM requests WHERE request_id=?', (HEAD_REQUEST,))
        need(len(found) == 1 and found[0]['state'] in ('reserved', state), 'RECONCILE_HEAD_RESULT_CONFLICT')
        if found[0]['state'] == 'reserved':
            need(
                db.execute(
                    'UPDATE requests SET state=?,error_code=? WHERE request_id=? AND state=?',
                    (state, error, HEAD_REQUEST, 'reserved'),
                ).rowcount
                == 1,
                'RECONCILE_HEAD_UPDATE_CONFLICT',
            )
        db.commit()
        return {'request_id': HEAD_REQUEST, 'state': state, 'error_code': error, 'remote_missing': missing}
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def transition_local(layout, observation, checked_at):
    missing = observation.get('http_status') == 404 and observation.get('error_code') == 'CLOUDBASE_BACKUP_REMOTE_NOT_FOUND'
    need(missing, 'RECONCILE_ACTUAL_404_REQUIRED')
    db = connect(layout, False)
    try:
        db.execute('BEGIN IMMEDIATE')
        need(
            sha(wire(rows(db, 'SELECT * FROM operations WHERE operation_id=?', (OPERATION,))[0])) == OPERATION_SHA,
            'RECONCILE_ORIGINAL_OPERATION_CHANGED',
        )
        head = rows(db, 'SELECT * FROM requests WHERE request_id=?', (HEAD_REQUEST,))
        need(
            len(head) == 1
            and head[0]['operation_id'] == OPERATION
            and head[0]['method'] == 'HEAD'
            and head[0]['state'] == 'done'
            and head[0]['error_code'] == 'CLOUDBASE_BACKUP_REMOTE_NOT_FOUND',
            'RECONCILE_COMPLETED_404_REQUIRED',
        )
        need(
            db.execute(
                'UPDATE operations SET state=?,error_code=?,updated_at=? '
                'WHERE operation_id=? AND state=? AND error_code=?',
                (TERMINAL, ERROR, checked_at, OPERATION, 'upload_unknown', 'CLOUDBASE_BACKUP_UPLOAD_UNKNOWN'),
            ).rowcount
            == 1,
            'RECONCILE_OPERATION_UPDATE_CONFLICT',
        )
        current = rows(db, 'SELECT * FROM operations WHERE operation_id=?', (OPERATION,))[0]
        db.commit()
        return {'operation': current, 'state': TERMINAL, 'error_code': ERROR, 'updated_at': checked_at}
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def sync(folder):
    if os.name == 'posix':
        descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def directory(folder, uid=0):
    safe(folder)
    if not folder.exists():
        directory(folder.parent, uid)
        folder.mkdir(mode=0o700)
        if uid != 0:
            os.chown(folder, uid, uid)
        sync(folder.parent)
    owner(folder.stat(), uid, True)
    need(folder.is_dir(), 'RECONCILE_DIRECTORY_INVALID')


def record_once(file, value, uid=0):
    if file.exists():
        need(json.loads(read(file, uid)) == value, 'RECONCILE_RECORD_CONFLICT')
        return
    safe(file)
    owner(file.parent.stat(), uid, True)
    descriptor, name = tempfile.mkstemp(prefix='.reconcile-', suffix='.partial', dir=file.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(wire(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, file)
        sync(file.parent)
    finally:
        temporary.unlink(missing_ok=True)
        sync(file.parent)


def documents(layout):
    config_raw = read(layout.config)
    need(sha(config_raw) == CONFIG_SHA, 'RECONCILE_BOUND_DOCUMENT_CHANGED')
    config = tomllib.loads(config_raw.decode('utf-8-sig'))
    config_doc = {'file': str(layout.config), 'sha256': sha(config_raw), 'bytes': len(config_raw)}
    intent, intent_doc = document(layout.intent, 10001, INTENT_SHA)
    need(
        intent['version'] == 'cloudbase-backup-v1'
        and intent['operation_id'] == ADAPTER
        and intent['local_archive'] == str(layout.archive.relative_to(layout.root))
        and intent['bytes'] == ARCHIVE_BYTES
        and intent['sha256'] == ARCHIVE_SHA
        and intent['archive_key']
        == 'lighthouse-hub/backups/v1/' + ADAPTER + '/' + ARCHIVE_SHA + '.archive'
        and intent['remote_verified'] is intent['local_deletion_performed'] is False,
        'RECONCILE_INTENT_CHANGED',
    )
    need(
        layout.archive.is_file()
        and layout.archive.stat().st_size == ARCHIVE_BYTES
        and sha(layout.archive.read_bytes()) == ARCHIVE_SHA,
        'RECONCILE_ARCHIVE_CHANGED',
    )
    daily, daily_doc = document(layout.latest, digest=DAILY_SHA)
    history, history_doc = document(layout.history, digest=DAILY_SHA)
    need(
        daily == history
        and daily['daily_id'] == DAILY
        and daily['state'] == 'accept_failed'
        and daily['error_code'] == 'ACCEPT_FAILED_OR_UNVERIFIED'
        and daily['image'] == IMAGE,
        'RECONCILE_FAILED_DAILY_CHANGED',
    )
    return {
        'config': config,
        'config_document': config_doc,
        'intent': intent,
        'intent_document': intent_doc,
        'archive': {'file': str(layout.archive), 'bytes': ARCHIVE_BYTES, 'sha256': ARCHIVE_SHA},
        'latest_document': daily_doc,
        'history_document': history_doc,
    }


def build_plan(layout):
    bound = documents(layout)
    ledger = ledger_action(layout, {'action': 'snapshot', 'terminal': False})
    original = {key: ledger[key] for key in ('operation', 'plan', 'requests')}
    return {
        'version': VERSION,
        'operation_id': OPERATION,
        'adapter_operation_id': ADAPTER,
        'daily_id': DAILY,
        'image': IMAGE,
        'archive_key': bound['intent']['archive_key'],
        'archive_bytes': ARCHIVE_BYTES,
        'archive_sha256': ARCHIVE_SHA,
        'documents': {key: value for key, value in bound.items() if key.endswith('_document')},
        'limits': bound['config']['limits'],
        'archive': bound['archive'],
        'original': original,
        'head_request_id': HEAD_REQUEST,
        'head_allowance': REQUEST_ALLOWANCE,
        'terminal_state': TERMINAL,
        'terminal_error_code': ERROR,
        'puts': 0,
        'gets': 0,
        'deletions': 0,
        'refund_bytes': 0,
        'external_requests': 1,
        'retry_performed': False,
        'backup_success_claimed': False,
        'failed_daily_retained': True,
    }


def signed_head(layout, observed):
    sys.path.insert(0, str(layout.root))
    from adapters.safe_https import open_request
    from adapters.cloudbase_cos_backup import CloudBaseCOSConfig, CloudBaseCOSStorage
    from services.hub.errors import HubError

    config = tomllib.loads(read(layout.config).decode('utf-8-sig'))
    options = dict(config['cloudbase'])
    options.pop('private_canary_reference', None)
    options.pop('private_canary_sha256', None)
    options.pop('credential_file', None)
    options['storage_hosts'] = tuple(options['storage_hosts'])
    cfg = CloudBaseCOSConfig(**options)
    cfg.validate()

    def credentials():
        value = json.loads(read(Path('/run/secrets/cloudbase-backup.json'), 0, 16_384))
        need(
            type(value) is dict and not set(value) - {'SecretId', 'SecretKey', 'Token', 'ExpirationEpoch'},
            'RECONCILE_CREDENTIAL_SCHEMA',
        )
        return value

    status = {}
    expected = 'https://' + cfg.host + '/' + observed['archive_key']

    def request(url, hosts, *, method='GET', headers=None, body=None, timeout=20):
        need(
            method == 'HEAD' and body is None and url == expected and list(hosts) == [cfg.host],
            'RECONCILE_HEAD_TRANSPORT_CHANGED',
        )
        connection, response = open_request(
            url,
            hosts,
            method=method,
            headers=headers,
            body=None,
            timeout=timeout,
        )
        status['http_status'] = response.status
        provider_code = response.getheader('x-cos-error-code', '')
        if isinstance(provider_code, str) and re.fullmatch('[A-Za-z_]{1,80}', provider_code):
            status['provider_code'] = provider_code
        return connection, response

    storage = CloudBaseCOSStorage(cfg, credentials, request=request)
    result, error = None, None
    try:
        result = storage.head('cos://' + cfg.bucket + '/' + observed['archive_key'], observed['archive_key'])
    except HubError as exc:
        error = exc.code
    return {**status, 'head_result': result, 'error_code': error}


def ensure_proof_directory(layout):
    directory(layout.proof.parent)
    directory(layout.proof)


def container_head(layout, observed):
    need(
        subprocess.check_output(
            ['/usr/bin/docker', 'image', 'inspect', '-f', '{{.Id}}', IMAGE],
            text=True,
            timeout=30,
        ).strip()
        == IMAGE,
        'RECONCILE_IMAGE_CHANGED',
    )
    source = safe(Path(__file__))
    name = 'lighthouse-upload-unknown-head-' + str(uuid.uuid4())
    args = [
        '/usr/bin/docker',
        'run',
        '--pull=never',
        '--rm',
        '--init',
        '-i',
        '--name',
        name,
        '--user',
        '10001:10001',
        '--read-only',
        '--cap-drop',
        'ALL',
        '--security-opt',
        'no-new-privileges:true',
        '--memory',
        '256m',
        '--memory-swap',
        '256m',
        '--cpus',
        '0.5',
        '--pids-limit',
        '64',
        '--tmpfs',
        '/tmp:rw,size=16m,mode=1777',
    ]
    mounts = (
        (layout.config, layout.config, True),
        (layout.root / 'secrets/cloudbase-backup.json', Path('/run/secrets/cloudbase-backup.json'), True),
        (source, Path('/run/reconcile.py'), True),
        (RUNTIME / 'adapters/safe_https.py', layout.root / 'adapters/safe_https.py', True),
        (RUNTIME / 'adapters/cloudbase_cos_backup.py', layout.root / 'adapters/cloudbase_cos_backup.py', True),
    )
    for origin, target, readonly in mounts:
        args += ['--mount', f'type=bind,src={origin},dst={target}' + (',readonly' if readonly else '')]
    args += [IMAGE, 'python', '/run/reconcile.py', '--child-head']
    try:
        result = subprocess.run(args, input=wire(observed), capture_output=True, timeout=90)
        need(
            result.returncode == 0 and len(result.stdout) <= 262_144 and len(result.stderr) <= 65_536,
            'RECONCILE_HEAD_CHILD_FAILED',
        )
        return json.loads(result.stdout)
    finally:
        ids = subprocess.run(
            ['/usr/bin/docker', 'ps', '-q', '--filter', 'name=^/' + name + '$'],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if ids.returncode == 0 and ids.stdout.strip():
            subprocess.run(
                ['/usr/bin/docker', 'stop', '-t', '10', name],
                capture_output=True,
                text=True,
                timeout=20,
            )


def validate_observation(value, expected_key):
    need(type(value) is dict and set(value) <= {'http_status', 'provider_code', 'head_result', 'error_code'}, 'RECONCILE_UNSAFE_OBSERVATION')
    status = value.get('http_status')
    need(
        status is None or type(status) is int and 100 <= status <= 599,
        'RECONCILE_UNSAFE_OBSERVATION',
    )
    for key in ('provider_code', 'error_code'):
        need(
            value.get(key) is None
            or isinstance(value[key], str)
            and re.fullmatch('[A-Za-z_]{1,100}', value[key]),
            'RECONCILE_UNSAFE_OBSERVATION',
        )
    result = value.get('head_result')
    need(
        result is None
        or type(result) is dict
        and set(result) == {'bytes', 'method', 'remote_verified'}
        and type(result['bytes']) is int
        and 0 <= result['bytes'] <= ARCHIVE_BYTES + 1
        and result['method'] == 'head_only'
        and result['remote_verified'] is False,
        'RECONCILE_UNSAFE_OBSERVATION',
    )
    need(expected_key == 'recovery', 'RECONCILE_OBSERVATION_KIND_INVALID')
    return True


def command(args, timeout=20):
    value = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    need(value.returncode == 0, 'RECONCILE_HOST_CHECK_FAILED')
    return value.stdout.strip()


def guard():
    need(
        command(
            [
                '/usr/bin/systemctl',
                'show',
                'lighthouse-cloudbase-backup.service',
                '--property=ActiveState',
                '--value',
            ]
        )
        in ('inactive', 'failed'),
        'RECONCILE_BACKUP_SERVICE_ACTIVE',
    )
    for condition in (
        'label=org.lighthouse.backup.sidecar',
        'name=^/lighthouse-archive-registry-',
        'name=^/lighthouse-upload-unknown-head-',
    ):
        need(
            not command(['/usr/bin/docker', 'ps', '-q', '--filter', condition]),
            'RECONCILE_BACKUP_CONTAINER_ACTIVE',
        )


@contextmanager
def locked(layout):
    import fcntl

    descriptors = []
    try:
        for path, uid in layout.locks:
            safe(path)
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            descriptors.append(descriptor)
            info = os.fstat(descriptor)
            owner(info, uid)
            need(stat.S_ISREG(info.st_mode), 'RECONCILE_LOCK_INVALID')
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def transition(layout, observed, observation, *, ledger_mutator=ledger_action):
    checked_at = time.time()
    head_receipt = {
        'version': VERSION,
        'state': 'head_observed',
        'operation_id': OPERATION,
        'adapter_operation_id': ADAPTER,
        'daily_id': DAILY,
        'archive_key': observed['archive_key'],
        'archive_sha256': ARCHIVE_SHA,
        'archive_bytes': ARCHIVE_BYTES,
        'request_id': HEAD_REQUEST,
        'observation': observation,
        'remote_missing_at_check': observation.get('http_status') == 404
        and observation.get('error_code') == 'CLOUDBASE_BACKUP_REMOTE_NOT_FOUND',
        'remote_verified': False,
        'requests': 1,
        'puts': 0,
        'gets': 0,
        'deletions': 0,
        'refund_bytes': 0,
        'retry_performed': False,
        'backup_success_claimed': False,
        'checked_at': checked_at,
    }
    record_once(layout.proof / 'head-receipt.json', head_receipt)
    settlement = ledger_mutator(
        layout,
        {'action': 'settle', 'original': observed['original'], 'observation': observation},
    )
    need(settlement['remote_missing'], 'RECONCILE_ACTUAL_404_REQUIRED')
    result = ledger_mutator(
        layout,
        {
            'action': 'transition',
            'observation': observation,
            'checked_at': checked_at,
        },
    )
    record_once(layout.proof / 'ledger-transition.json', result)
    if layout.latest.exists():
        need(not layout.failed_latest.exists(), 'RECONCILE_FAILED_LATEST_EXISTS')
        os.rename(layout.latest, layout.failed_latest)
        sync(layout.daily)
        sync(layout.proof)
    receipt = {
        'version': VERSION,
        'state': 'remote_absent_reconciled',
        'operation_id': OPERATION,
        'daily_id': DAILY,
        'head_request_id': HEAD_REQUEST,
        'head_receipt_sha256': sha(read(layout.proof / 'head-receipt.json')),
        'ledger_transition_sha256': sha(read(layout.proof / 'ledger-transition.json')),
        'failed_latest_sha256': DAILY_SHA,
        'history_sha256': DAILY_SHA,
        'ledger_terminal_state': TERMINAL,
        'active_latest_absent': True,
        'history_preserved': True,
        'local_archive_preserved': True,
        'original_operation_preserved': True,
        'no_refund': True,
        'refund_bytes': 0,
        'puts': 0,
        'gets': 0,
        'deletions': 0,
        'external_requests': 1,
        'retry_performed': False,
        'backup_success_claimed': False,
    }
    record_once(layout.proof / 'receipt.json', receipt)
    return verify_completed(layout)


def verify_completed(layout):
    intent = json.loads(read(layout.proof / 'intent.json'))
    observed = intent['plan']
    digest = sha(wire(observed))
    need(
        intent
        == {
            'version': VERSION,
            'state': 'head_intent_saved',
            'plan_sha256': digest,
            'plan': observed,
        },
        'RECONCILE_INTENT_CHANGED',
    )
    need(observed['version'] == VERSION and observed['operation_id'] == OPERATION, 'RECONCILE_PLAN_CHANGED')
    head = json.loads(read(layout.proof / 'head-receipt.json'))
    need(
        head['version'] == VERSION
        and head['operation_id'] == OPERATION
        and head['daily_id'] == DAILY
        and head['archive_key'] == observed['archive_key']
        and head['observation'].get('http_status') == 404
        and head['observation'].get('error_code') == 'CLOUDBASE_BACKUP_REMOTE_NOT_FOUND'
        and head['remote_missing_at_check'] is True
        and head['remote_verified'] is False
        and head['puts'] == head['gets'] == head['deletions'] == head['refund_bytes'] == 0,
        'RECONCILE_HEAD_RECEIPT_CHANGED',
    )
    transition_raw = read(layout.proof / 'ledger-transition.json')
    transition_value = json.loads(transition_raw)
    need(
        transition_value['state'] == TERMINAL
        and transition_value['operation']['state'] == TERMINAL
        and transition_value['operation']['error_code'] == ERROR,
        'RECONCILE_TRANSITION_CHANGED',
    )
    live = ledger_action(layout, {'action': 'snapshot', 'terminal': True})
    need(
        live['operation']['state'] == TERMINAL
        and live['operation']['error_code'] == ERROR
        and live['head'][0]['state'] == 'done'
        and live['head'][0]['error_code'] == 'CLOUDBASE_BACKUP_REMOTE_NOT_FOUND',
        'RECONCILE_LIVE_LEDGER_CHANGED',
    )
    need(not layout.latest.exists() and layout.failed_latest.is_file(), 'RECONCILE_ACTIVE_LATEST_PRESENT')
    need(
        sha(read(layout.failed_latest)) == sha(read(layout.history)) == DAILY_SHA,
        'RECONCILE_FAILED_HISTORY_CHANGED',
    )
    need(
        layout.archive.stat().st_size == ARCHIVE_BYTES and sha(layout.archive.read_bytes()) == ARCHIVE_SHA,
        'RECONCILE_LOCAL_ARCHIVE_CHANGED',
    )
    receipt = json.loads(read(layout.proof / 'receipt.json'))
    expected = {
        'version': VERSION,
        'state': 'remote_absent_reconciled',
        'operation_id': OPERATION,
        'daily_id': DAILY,
        'head_request_id': HEAD_REQUEST,
        'head_receipt_sha256': sha(read(layout.proof / 'head-receipt.json')),
        'ledger_transition_sha256': sha(transition_raw),
        'failed_latest_sha256': DAILY_SHA,
        'history_sha256': DAILY_SHA,
        'ledger_terminal_state': TERMINAL,
        'active_latest_absent': True,
        'history_preserved': True,
        'local_archive_preserved': True,
        'original_operation_preserved': True,
        'no_refund': True,
        'refund_bytes': 0,
        'puts': 0,
        'gets': 0,
        'deletions': 0,
        'external_requests': 1,
        'retry_performed': False,
        'backup_success_claimed': False,
    }
    need(receipt == expected, 'RECONCILE_FINAL_RECEIPT_CHANGED')
    return receipt


def apply(
    layout,
    expected,
    *,
    host_guard=guard,
    probe=container_head,
    ledger_mutator=ledger_action,
):
    need(re.fullmatch('[a-f0-9]{64}', expected or ''), 'RECONCILE_PLAN_HASH_REQUIRED')
    host_guard()
    intent_path = layout.proof / 'intent.json'
    if intent_path.exists():
        return verify_completed(layout)
    observed = build_plan(layout)
    need(sha(wire(observed)) == expected, 'RECONCILE_PLAN_CHANGED')
    ensure_proof_directory(layout)
    record_once(
        intent_path,
        {
            'version': VERSION,
            'state': 'head_intent_saved',
            'plan_sha256': expected,
            'plan': observed,
        },
    )
    host_guard()
    ledger_mutator(layout, {'action': 'reserve', 'original': observed['original'], 'limits': observed['limits']})
    observation = probe(layout, observed)
    need(validate_observation(observation, 'recovery'), 'RECONCILE_UNSAFE_OBSERVATION')
    return transition(layout, observed, observation, ledger_mutator=ledger_mutator)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--plan-sha256')
    parser.add_argument('--self-sha256')
    parser.add_argument('--child-ledger', action='store_true')
    parser.add_argument('--child-head', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    need(os.name == 'posix', 'RECONCILE_LINUX_REQUIRED')
    layout = Layout()
    if args.child_head:
        need(
            os.getuid() == 10001
            and not args.apply
            and args.plan_sha256 is args.self_sha256 is None,
            'RECONCILE_HEAD_CHILD_IDENTITY',
        )
        raw = sys.stdin.buffer.read(262_145)
        need(len(raw) <= 262_144, 'RECONCILE_HEAD_INPUT_LIMIT')
        observed = json.loads(raw)
        need(
            type(observed) is dict
            and observed.get('operation_id') == OPERATION
            and observed.get('archive_key')
            == 'lighthouse-hub/backups/v1/' + ADAPTER + '/' + ARCHIVE_SHA + '.archive'
            and observed.get('archive_bytes') == ARCHIVE_BYTES
            and observed.get('archive_sha256') == ARCHIVE_SHA,
            'RECONCILE_HEAD_INPUT_INVALID',
        )
        print(json.dumps(signed_head(layout, observed)))
        return 0
    if args.child_ledger:
        need(
            os.getuid() == 10001
            and not args.apply
            and args.plan_sha256 is args.self_sha256 is None,
            'RECONCILE_LEDGER_CHILD_IDENTITY',
        )
        raw = sys.stdin.buffer.read(1_048_577)
        need(len(raw) <= 1_048_576, 'RECONCILE_LEDGER_INPUT_LIMIT')
        print(json.dumps(ledger_action(layout, json.loads(raw))))
        return 0
    need(
        os.geteuid() == 0
        and args.apply == bool(args.plan_sha256)
        and sha(read(Path(__file__))) == args.self_sha256,
        'RECONCILE_ROOT_SELF_PIN_REQUIRED',
    )
    with locked(layout):
        guard()
        if args.apply:
            value = apply(layout, args.plan_sha256)
        elif (layout.proof / 'intent.json').exists():
            value = verify_completed(layout)
        else:
            observed = build_plan(layout)
            value = {
                'state': 'planned_no_changes',
                'plan_sha256': sha(wire(observed)),
                'plan': observed,
                'external_requests': 0,
            }
    print(json.dumps(value, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        code = str(exc) if isinstance(exc, ValueError) and re.fullmatch('[A-Z_]{1,100}', str(exc)) else 'RECONCILE_STOPPED'
        print(json.dumps({'state': 'stopped', 'error_code': code, 'retry_performed': False}))
        raise SystemExit(2) from None
