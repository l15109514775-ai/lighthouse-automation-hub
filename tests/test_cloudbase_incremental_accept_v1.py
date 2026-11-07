import json
import sqlite3
from pathlib import Path

import pytest

from services.hub.cloudbase_incremental_accept_v1 import accept
from services.hub.cloudbase_incremental_v1 import IncrementalError, build_delta
from tests.test_cloudbase_incremental_v1 import write_full, seed_index, sha


def make_db(path):
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE jobs(job_id TEXT PRIMARY KEY, state TEXT)')
    db.execute('CREATE TABLE publications(id TEXT PRIMARY KEY)')
    db.execute('CREATE TABLE events(seq INTEGER PRIMARY KEY)')
    db.execute('CREATE TABLE audit(seq INTEGER PRIMARY KEY)')
    db.execute("INSERT INTO jobs VALUES('j1','completed')")
    db.commit(); db.close()
    return path.read_bytes()


def test_accept_restores_and_checks_database(tmp_path):
    db = make_db(tmp_path / 'db.sqlite3')
    full = tmp_path / 'full.tar.gz'
    manifest = write_full(full, {'database/credential-excluded-operational.sqlite3': db,
                                 'tweets/old.png': b'old'})
    index = seed_index(tmp_path / 'index.sqlite3', manifest)
    full2 = tmp_path / 'full2.tar.gz'
    manifest2 = write_full(full2, {'database/credential-excluded-operational.sqlite3': db,
                                   'tweets/old.png': b'old', 'tweets/new.png': b'new'})
    base = {'operation_id': 'full-op', 'archive_key': 'base-key',
            'archive_sha256': sha(full.read_bytes()),
            'manifest_sha256': sha(json.dumps(manifest, sort_keys=True).encode()), 'chain_depth': 0}
    out = tmp_path / 'delta.tar.gz'
    bundle = build_delta(full2, manifest2, index, operation_id='delta-op', base=base, output=out)
    refs = [{'operation_id': 'full-op', 'bytes': full.stat().st_size,
             'sha256': sha(full.read_bytes()), 'manifest': manifest},
            {'operation_id': 'delta-op', 'bytes': out.stat().st_size,
             'sha256': bundle['sha256'], 'manifest': bundle['manifest']}]
    objects = {0: full.read_bytes(), 1: out.read_bytes()}
    report = accept(refs, lambda ref: objects[refs.index(ref)], tmp_path / 'restore')
    assert report['state'] == 'chain_restored_and_verified'
    assert report['entries'] == 3 and report['archive_requests'] == 2
    assert report['restored_counts']['jobs'] == 1


def test_accept_rejects_chain_over_budget(tmp_path):
    with pytest.raises(IncrementalError, match='INCREMENTAL_CHAIN_BUDGET_EXCEEDED'):
        accept([{'bytes': 11}], lambda ref: b'', tmp_path / 'restore', limits=10)
