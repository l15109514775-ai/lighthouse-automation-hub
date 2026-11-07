"""Incremental archive primitives for the CloudBase operational backup.

This module is deliberately independent of upload/acceptance side effects.  A
base archive is a normal full `cloudbase-operational-backup-v1` archive.  A
delta archive keeps the full database snapshot, includes only new/changed
files, and records every unchanged file as a reference to an earlier archive.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath

VERSION = 'cloudbase-incremental-v1'
BASE = 'base'
DELTA = 'delta'


class IncrementalError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _sha(data): return hashlib.sha256(data).hexdigest()


def _safe_member(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or not path.parts or path.name == '':
        raise IncrementalError('INCREMENTAL_MEMBER_UNSAFE')
    return name


def _read_manifest(tar):
    member = tar.getmember('MANIFEST.json')
    if member.size > 5_000_000:
        raise IncrementalError('INCREMENTAL_MANIFEST_LIMIT')
    raw = tar.extractfile(member).read()
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get('version') != 'cloudbase-operational-backup-v1':
        raise IncrementalError('INCREMENTAL_MANIFEST_INVALID')
    return value, raw


def _logical(full_manifest, operation_id, archive_key):
    """Return path -> source for a full archive without an explicit index."""
    result = {}
    for name, item in full_manifest['files'].items():
        result[name] = {'bytes': item['bytes'], 'sha256': item['sha256'],
                        'source': {'operation_id': operation_id, 'archive_key': archive_key,
                                   'member': name}}
    return result


class FileIndex:
    """Durable mapping from verified archive members to remote archive objects."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute('''CREATE TABLE IF NOT EXISTS archives(
            operation_id TEXT PRIMARY KEY, archive_key TEXT NOT NULL,
            archive_sha256 TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
            kind TEXT NOT NULL, base_operation_id TEXT, verified_at REAL NOT NULL,
            manifest_json TEXT NOT NULL DEFAULT '{}', archive_bytes INTEGER NOT NULL DEFAULT 0)''')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(archives)')}
        if 'manifest_json' not in columns:
            self.db.execute("ALTER TABLE archives ADD COLUMN manifest_json TEXT NOT NULL DEFAULT '{}'")
        if 'archive_bytes' not in columns:
            self.db.execute('ALTER TABLE archives ADD COLUMN archive_bytes INTEGER NOT NULL DEFAULT 0')
        self.db.execute('''CREATE TABLE IF NOT EXISTS files(
            path TEXT NOT NULL, sha256 TEXT NOT NULL, bytes INTEGER NOT NULL,
            operation_id TEXT NOT NULL, archive_key TEXT NOT NULL, member TEXT NOT NULL,
            PRIMARY KEY(path, sha256))''')
        self.db.commit()

    def close(self): self.db.close()

    def latest(self):
        row = self.db.execute('''SELECT operation_id,archive_key,archive_sha256,manifest_sha256,
            kind,base_operation_id,verified_at FROM archives ORDER BY verified_at DESC, rowid DESC LIMIT 1''').fetchone()
        if not row: return None
        return {'operation_id': row[0], 'archive_key': row[1], 'archive_sha256': row[2],
                'manifest_sha256': row[3], 'kind': row[4],
                'base_operation_id': row[5], 'verified_at': row[6]}

    def chain(self, operation_id):
        """Return verified archives from base to `operation_id`, oldest first."""
        result = []
        seen = set()
        current = operation_id
        while current:
            if current in seen: raise IncrementalError('INCREMENTAL_CHAIN_CYCLE')
            seen.add(current)
            row = self.db.execute('''SELECT operation_id,archive_key,archive_sha256,manifest_sha256,
                kind,base_operation_id,manifest_json FROM archives WHERE operation_id=?''',
                (current,)).fetchone()
            if row is None: raise IncrementalError('INCREMENTAL_CHAIN_ARCHIVE_MISSING')
            result.append({'operation_id': row[0], 'archive_key': row[1],
                           'archive_sha256': row[2], 'manifest_sha256': row[3],
                           'kind': row[4], 'base_operation_id': row[5],
                           'manifest': json.loads(row[6])})
            current = row[5]
        result.reverse()
        if not result or result[0]['kind'] != BASE:
            raise IncrementalError('INCREMENTAL_CHAIN_BASE_MISSING')
        return result

    def chain_refs(self, operation_id):
        refs = []
        for item in self.chain(operation_id):
            row = self.db.execute('SELECT archive_bytes FROM archives WHERE operation_id=?',
                                  (item['operation_id'],)).fetchone()
            refs.append({'operation_id': item['operation_id'],
                         'archive_key': item['archive_key'],
                         'bytes': row[0], 'sha256': item['archive_sha256'],
                         'manifest': item['manifest']})
        return refs

    def plan(self, *, now, base_days=30):
        """Return base or delta intent; a full base is required after the cadence."""
        if type(base_days) is not int or not 1 <= base_days <= 90:
            raise IncrementalError('INCREMENTAL_BASE_DAYS_INVALID')
        latest = self.latest()
        if latest is None:
            return {'kind': BASE, 'base': None, 'reason': 'no_verified_archive'}
        base = self.db.execute('''SELECT operation_id,archive_key,archive_sha256,manifest_sha256,
            verified_at FROM archives WHERE kind=? ORDER BY verified_at DESC, rowid DESC LIMIT 1''',
            (BASE,)).fetchone()
        if base is None:
            return {'kind': BASE, 'base': None, 'reason': 'no_base_archive'}
        age = now - base[4]
        if age >= base_days * 86400:
            return {'kind': BASE, 'base': None, 'reason': 'base_cadence_expired'}
        return {'kind': DELTA,
                'base': {'operation_id': latest['operation_id'], 'archive_key': latest['archive_key'],
                         'archive_sha256': latest['archive_sha256'],
                         'manifest_sha256': latest['manifest_sha256'],
                         'chain_depth': 0},
                'reason': 'incremental'}

    def lookup(self, path, sha256):
        row = self.db.execute('SELECT bytes,operation_id,archive_key,member FROM files WHERE path=? AND sha256=?',
                              (path, sha256)).fetchone()
        if not row: return None
        return {'bytes': row[0], 'operation_id': row[1], 'archive_key': row[2], 'member': row[3]}

    def record(self, manifest, *, operation_id, archive_key, archive_sha256,
               manifest_sha256, kind, base_operation_id=None, archive_bytes=0):
        if kind not in (BASE, DELTA): raise IncrementalError('INCREMENTAL_KIND_INVALID')
        if type(archive_bytes) is not int or archive_bytes < 0:
            raise IncrementalError('INCREMENTAL_ARCHIVE_BYTES_INVALID')
        with self.db:
            self.db.execute('INSERT INTO archives VALUES(?,?,?,?,?,?,?,?,?)',
                            (operation_id, archive_key, archive_sha256, manifest_sha256,
                             kind, base_operation_id, time.time(),
                             json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                             archive_bytes))
            for name, item in manifest['files'].items():
                self.db.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?)',
                                (name, item['sha256'], item['bytes'], operation_id,
                                 archive_key, name))


def build_delta(full_archive, full_manifest, index, *, operation_id, base, output):
    """Turn a freshly built full archive into a delta, without uploading it."""
    if base.get('archive_sha256') is None or base.get('archive_key') is None:
        raise IncrementalError('INCREMENTAL_BASE_REQUIRED')
    files = full_manifest.get('files')
    if not isinstance(files, dict) or not files:
        raise IncrementalError('INCREMENTAL_FILES_INVALID')
    included = {}
    logical = {}
    with tarfile.open(full_archive, 'r:gz') as source:
        members = {m.name: m for m in source if m.isfile()}
        if set(members) != set(files) | {'MANIFEST.json'}:
            raise IncrementalError('INCREMENTAL_FULL_MEMBERS_CHANGED')
        for name, item in files.items():
            # The database is the authoritative current state and is always
            # included. Everything else is either new/changed bytes or a
            # reference to the exact earlier archived member.
            if name == 'database/credential-excluded-operational.sqlite3':
                included[name] = source.extractfile(members[name]).read()
                logical[name] = {'bytes': item['bytes'], 'sha256': item['sha256'],
                                 'source': {'operation_id': operation_id, 'archive_key': base['archive_key'],
                                            'member': name}}
                continue
            prior = index.lookup(name, item['sha256'])
            if prior:
                logical[name] = {'bytes': prior['bytes'], 'sha256': item['sha256'],
                                 'source': {'operation_id': prior['operation_id'],
                                            'archive_key': prior['archive_key'],
                                            'member': prior['member']}}
            else:
                included[name] = source.extractfile(members[name]).read()
                logical[name] = {'bytes': item['bytes'], 'sha256': item['sha256'],
                                 'source': {'operation_id': operation_id, 'archive_key': base['archive_key'],
                                            'member': name}}
    manifest = {
        'version': 'cloudbase-operational-backup-v1',
        'archive_kind': DELTA,
        'operation_id': operation_id,
        'created_at': full_manifest.get('created_at'),
        'snapshot': full_manifest['snapshot'],
        'tweet_jobs': full_manifest.get('tweet_jobs'),
        'files': {name: {'bytes': len(data), 'sha256': _sha(data)} for name, data in included.items()},
        'logical_files': logical,
        'base': dict(base),
        'excluded': full_manifest.get('excluded', []),
        'source_files_deleted': False,
        'production_database_modified': False,
        'raw_docker_logs_captured': False,
        'directory_json_reports_captured': False,
    }
    output = Path(output)
    with tarfile.open(output, 'w:gz') as target:
        for name, data in included.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o600
            target.addfile(info, __import__('io').BytesIO(data))
        raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
        info = tarfile.TarInfo('MANIFEST.json')
        info.size = len(raw)
        info.mode = 0o600
        target.addfile(info, __import__('io').BytesIO(raw))
    return {'archive': str(output), 'bytes': output.stat().st_size,
            'sha256': _sha(output.read_bytes()), 'manifest': manifest}


def _manifest_for(item):
    return item['manifest']


def _manifest_from_archive(path):
    with tarfile.open(path, 'r:gz') as tar:
        return _read_manifest(tar)[0]


def chain_budget(refs):
    """Conservative preflight reservation for a chain restore."""
    if not refs: raise IncrementalError('INCREMENTAL_CHAIN_EMPTY')
    total = 0
    for ref in refs:
        size = ref.get('bytes')
        if type(size) is not int or size <= 0:
            raise IncrementalError('INCREMENTAL_CHAIN_BYTES_INVALID')
        total += size
    return {'archive_bytes': total, 'requests': len(refs)}


def restore_from_remote(refs, fetch, destination):
    """Fetch bounded archive bytes, verify them, then restore the chain.

    `fetch(ref)` must return bytes (or a path) and is the only network seam.
    Every fetched archive is hash-checked before its members are inspected.
    """
    if not refs: raise IncrementalError('INCREMENTAL_CHAIN_EMPTY')
    destination = Path(destination)
    temp = Path(tempfile.mkdtemp(prefix='cloudbase-incremental-chain-'))
    try:
        chain = []
        for index, ref in enumerate(refs):
            raw = fetch(ref)
            data = Path(raw).read_bytes() if isinstance(raw, (str, os.PathLike)) else raw
            if len(data) != ref['bytes'] or _sha(data) != ref['sha256']:
                raise IncrementalError('INCREMENTAL_REMOTE_ARCHIVE_CHANGED')
            path = temp / ('archive-%d.tar.gz' % index)
            path.write_bytes(data)
            manifest = ref.get('manifest') or _manifest_from_archive(path)
            chain.append({'archive': str(path), 'sha256': ref['sha256'], 'manifest': manifest})
        return restore_chain(chain, destination)
    finally:
        import shutil
        shutil.rmtree(temp, ignore_errors=True)


def restore_chain(chain, destination):
    """Restore base -> delta chain and verify every final logical file."""
    if not chain: raise IncrementalError('INCREMENTAL_CHAIN_EMPTY')
    destination = Path(destination)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    actual = {}
    sources = {}
    previous = None
    for item in chain:
        manifest = _manifest_for(item)
        archive = Path(item['archive'])
        if _sha(archive.read_bytes()) != item['sha256']:
            raise IncrementalError('INCREMENTAL_ARCHIVE_CHANGED')
        kind = manifest.get('archive_kind', BASE)
        if previous is None:
            if kind != BASE: raise IncrementalError('INCREMENTAL_BASE_KIND_REQUIRED')
        else:
            base = manifest.get('base') or {}
            if base.get('operation_id') != previous['manifest']['operation_id']:
                raise IncrementalError('INCREMENTAL_CHAIN_ORDER_INVALID')
            if base.get('archive_sha256') != previous['sha256']:
                raise IncrementalError('INCREMENTAL_CHAIN_BASE_CHANGED')
        with tarfile.open(archive, 'r:gz') as tar:
            members = {m.name: m for m in tar if m.isfile()}
            if set(members) != set(manifest['files']) | {'MANIFEST.json'}:
                raise IncrementalError('INCREMENTAL_DELTA_MEMBERS_CHANGED')
            for name, expected in manifest['files'].items():
                data = tar.extractfile(members[name]).read()
                if len(data) != expected['bytes'] or _sha(data) != expected['sha256']:
                    raise IncrementalError('INCREMENTAL_MEMBER_CHANGED')
                path = destination / name
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                path.write_bytes(data)
                path.chmod(0o600)
                actual[name] = data
                sources[(manifest['operation_id'], name)] = data
        previous = item
    head = chain[-1]['manifest']
    logical = head.get('logical_files') or _logical(head, head['operation_id'],
                                                    chain[-1].get('archive_key', ''))
    for name, expected in logical.items():
        if name in actual:
            data = actual[name]
        else:
            source = expected.get('source') or {}
            data = sources.get((source.get('operation_id'), source.get('member')))
            if data is None: raise IncrementalError('INCREMENTAL_REFERENCE_MISSING')
            path = destination / name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_bytes(data); path.chmod(0o600)
        if len(data) != expected['bytes'] or _sha(data) != expected['sha256']:
            raise IncrementalError('INCREMENTAL_REFERENCE_CHANGED')
    return {'state': 'chain_restored', 'entries': len(logical),
            'chain': [item['manifest']['operation_id'] for item in chain]}
