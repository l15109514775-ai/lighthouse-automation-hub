import hashlib, io, json, tarfile
from pathlib import Path

import pytest

from services.hub.cloudbase_incremental_v1 import (
    BASE, DELTA, FileIndex, IncrementalError, build_delta, chain_budget, restore_chain, restore_from_remote,
)


def sha(data): return hashlib.sha256(data).hexdigest()


def write_full(path, files, operation='full-op'):
    manifest = {
        'version': 'cloudbase-operational-backup-v1',
        'operation_id': operation,
        'created_at': '2026-11-07T00:00:00+00:00',
        'snapshot': {'kind': 'credential-excluded operational snapshot', 'excluded_tables': {}},
        'tweet_jobs': 1,
        'excluded': ['secret environment files'],
        'files': {name: {'bytes': len(data), 'sha256': sha(data)} for name, data in files.items()},
    }
    with tarfile.open(path, 'w:gz') as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name); info.size = len(data); info.mode = 0o600
            tar.addfile(info, io.BytesIO(data))
        raw = json.dumps(manifest, sort_keys=True).encode()
        info = tarfile.TarInfo('MANIFEST.json'); info.size = len(raw); info.mode = 0o600
        tar.addfile(info, io.BytesIO(raw))
    return manifest


def seed_index(index_path, full_manifest, archive_key='base-key', operation='full-op'):
    index = FileIndex(index_path)
    raw = json.dumps(full_manifest, sort_keys=True).encode()
    index.record(full_manifest, operation_id=operation, archive_key=archive_key,
                 archive_sha256='a' * 64, manifest_sha256=sha(raw), kind=BASE)
    return index


def test_delta_includes_new_and_references_unchanged(tmp_path):
    db = b'database-v1'; old = b'old-evidence'; new = b'new-evidence'
    full = tmp_path / 'full.tar.gz'
    manifest = write_full(full, {'database/credential-excluded-operational.sqlite3': db,
                                 'tweets/old.png': old})
    index = seed_index(tmp_path / 'index.sqlite3', manifest)
    full2 = tmp_path / 'full2.tar.gz'
    manifest2 = write_full(full2, {'database/credential-excluded-operational.sqlite3': db + b'2',
                                   'tweets/old.png': old, 'tweets/new.png': new})
    base = {'operation_id': 'full-op', 'archive_key': 'base-key', 'archive_sha256': sha(full.read_bytes()),
            'manifest_sha256': sha(json.dumps(manifest, sort_keys=True).encode()), 'chain_depth': 0}
    out = tmp_path / 'delta.tar.gz'
    bundle = build_delta(full2, manifest2, index, operation_id='delta-op', base=base, output=out)
    with tarfile.open(out, 'r:gz') as tar:
        names = {m.name for m in tar if m.isfile()}
    assert 'tweets/old.png' not in names
    assert 'tweets/new.png' in names
    assert 'database/credential-excluded-operational.sqlite3' in names
    assert bundle['manifest']['archive_kind'] == DELTA
    assert bundle['manifest']['logical_files']['tweets/old.png']['source']['operation_id'] == 'full-op'
    restore = tmp_path / 'restore'
    result = restore_chain([
        {'archive': str(full), 'sha256': sha(full.read_bytes()), 'manifest': manifest},
        {'archive': str(out), 'sha256': bundle['sha256'], 'manifest': bundle['manifest']},
    ], restore)
    assert result['entries'] == 3
    assert (restore / 'tweets/old.png').read_bytes() == old
    assert (restore / 'tweets/new.png').read_bytes() == new


def test_changed_file_is_included_not_referenced(tmp_path):
    db = b'db'; old1 = b'old-v1'; old2 = b'old-v2'
    full = tmp_path / 'full.tar.gz'
    manifest = write_full(full, {'database/credential-excluded-operational.sqlite3': db,
                                 'tweets/old.png': old1})
    index = seed_index(tmp_path / 'index.sqlite3', manifest)
    full2 = tmp_path / 'full2.tar.gz'
    manifest2 = write_full(full2, {'database/credential-excluded-operational.sqlite3': db,
                                   'tweets/old.png': old2})
    base = {'operation_id': 'full-op', 'archive_key': 'base-key', 'archive_sha256': sha(full.read_bytes()),
            'manifest_sha256': sha(json.dumps(manifest, sort_keys=True).encode()), 'chain_depth': 0}
    out = tmp_path / 'delta.tar.gz'
    bundle = build_delta(full2, manifest2, index, operation_id='delta-op', base=base, output=out)
    with tarfile.open(out, 'r:gz') as tar:
        names = {m.name for m in tar if m.isfile()}
    assert 'tweets/old.png' in names
    assert bundle['manifest']['logical_files']['tweets/old.png']['source']['operation_id'] == 'delta-op'


def test_missing_base_reference_fails(tmp_path):
    db = b'db'; old = b'old'
    full = tmp_path / 'full.tar.gz'
    manifest = write_full(full, {'database/credential-excluded-operational.sqlite3': db,
                                 'tweets/old.png': old})
    index = seed_index(tmp_path / 'index.sqlite3', manifest)
    full2 = tmp_path / 'full2.tar.gz'
    manifest2 = write_full(full2, {'database/credential-excluded-operational.sqlite3': db,
                                   'tweets/old.png': old, 'tweets/new.png': b'new'})
    base = {'operation_id': 'full-op', 'archive_key': 'base-key', 'archive_sha256': sha(full.read_bytes()),
            'manifest_sha256': sha(json.dumps(manifest, sort_keys=True).encode()), 'chain_depth': 0}
    out = tmp_path / 'delta.tar.gz'
    bundle = build_delta(full2, manifest2, index, operation_id='delta-op', base=base, output=out)
    with pytest.raises(IncrementalError, match='INCREMENTAL_BASE_KIND_REQUIRED'):
        restore_chain([{'archive': str(out), 'sha256': bundle['sha256'],
                        'manifest': bundle['manifest']}], tmp_path / 'restore')


def test_plan_selects_base_then_delta_and_honours_cadence(tmp_path):
    manifest = {'version': 'cloudbase-operational-backup-v1', 'files': {
        'database/credential-excluded-operational.sqlite3': {'bytes': 1, 'sha256': 'a' * 64}}}
    empty = FileIndex(tmp_path / 'empty.sqlite3')
    assert empty.plan(now=1000, base_days=30)['kind'] == BASE
    empty.close()
    index = seed_index(tmp_path / 'index.sqlite3', manifest)
    now = index.latest()['verified_at'] + 1
    plan = index.plan(now=now, base_days=30)
    assert plan['kind'] == DELTA and plan['base']['operation_id'] == 'full-op'
    index.db.execute("UPDATE archives SET verified_at=? WHERE kind=?", (now - 31 * 86400, BASE))
    index.db.commit()
    assert index.plan(now=now, base_days=30)['kind'] == BASE
    with pytest.raises(IncrementalError, match='INCREMENTAL_BASE_DAYS_INVALID'):
        index.plan(now=now, base_days=0)


def test_remote_chain_restore_uses_only_verified_bytes(tmp_path):
    db = b'db'; old = b'old'
    full = tmp_path / 'full.tar.gz'
    manifest = write_full(full, {'database/credential-excluded-operational.sqlite3': db,
                                 'tweets/old.png': old})
    index = seed_index(tmp_path / 'index.sqlite3', manifest)
    full2 = tmp_path / 'full2.tar.gz'
    manifest2 = write_full(full2, {'database/credential-excluded-operational.sqlite3': db,
                                   'tweets/old.png': old, 'tweets/new.png': b'new'})
    base = {'operation_id': 'full-op', 'archive_key': 'base-key', 'archive_sha256': sha(full.read_bytes()),
            'manifest_sha256': sha(json.dumps(manifest, sort_keys=True).encode()), 'chain_depth': 0}
    out = tmp_path / 'delta.tar.gz'
    bundle = build_delta(full2, manifest2, index, operation_id='delta-op', base=base, output=out)
    refs = [{'bytes': full.stat().st_size, 'sha256': sha(full.read_bytes()), 'manifest': manifest,
             'archive_key': 'base-key'},
            {'bytes': out.stat().st_size, 'sha256': bundle['sha256'], 'manifest': bundle['manifest'],
             'archive_key': 'delta-key'}]
    objects = {0: full.read_bytes(), 1: out.read_bytes()}
    result = restore_from_remote(refs, lambda ref: objects[refs.index(ref)], tmp_path / 'remote-restore')
    assert result['state'] == 'chain_restored'
    assert (tmp_path / 'remote-restore' / 'tweets/new.png').read_bytes() == b'new'


def test_chain_budget_reserves_every_archive():
    assert chain_budget([{'bytes': 10}, {'bytes': 20}]) == {'archive_bytes': 30, 'requests': 2}
    with pytest.raises(IncrementalError, match='INCREMENTAL_CHAIN_BYTES_INVALID'):
        chain_budget([{'bytes': 0}])


def test_tampered_reference_fails(tmp_path):
    db = b'db'; old = b'old'
    full = tmp_path / 'full.tar.gz'
    manifest = write_full(full, {'database/credential-excluded-operational.sqlite3': db,
                                 'tweets/old.png': old})
    index = seed_index(tmp_path / 'index.sqlite3', manifest)
    full2 = tmp_path / 'full2.tar.gz'
    manifest2 = write_full(full2, {'database/credential-excluded-operational.sqlite3': db,
                                   'tweets/old.png': old, 'tweets/new.png': b'new'})
    base = {'operation_id': 'full-op', 'archive_key': 'base-key', 'archive_sha256': sha(full.read_bytes()),
            'manifest_sha256': sha(json.dumps(manifest, sort_keys=True).encode()), 'chain_depth': 0}
    out = tmp_path / 'delta.tar.gz'
    bundle = build_delta(full2, manifest2, index, operation_id='delta-op', base=base, output=out)
    bundle['manifest']['logical_files']['tweets/old.png']['sha256'] = '0' * 64
    with pytest.raises(IncrementalError, match='INCREMENTAL_REFERENCE_CHANGED'):
        restore_chain([
            {'archive': str(full), 'sha256': sha(full.read_bytes()), 'manifest': manifest},
            {'archive': str(out), 'sha256': bundle['sha256'], 'manifest': bundle['manifest']},
        ], tmp_path / 'restore')
