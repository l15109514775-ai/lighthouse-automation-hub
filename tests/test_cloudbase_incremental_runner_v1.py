import json
from pathlib import Path

from services.hub.cloudbase_incremental_runner_v1 import IncrementalBackup
from services.hub.cloudbase_incremental_v1 import BASE, DELTA, FileIndex
from tests.test_cloudbase_incremental_v1 import write_full, sha


def test_runner_builds_base_then_delta(tmp_path):
    db = b'db'; old = b'old'
    full = tmp_path / 'full.tar.gz'
    manifest = write_full(full, {'database/credential-excluded-operational.sqlite3': db,
                                 'tweets/old.png': old}, operation='op-1')
    index = FileIndex(tmp_path / 'index.sqlite3')
    runner = IncrementalBackup(index, lambda op: {'archive': str(full), 'manifest': manifest})
    base = runner.prepare('op-1', now=1000)
    assert base['kind'] == BASE
    runner.commit(base, archive_key='base-key', archive_sha256=sha(full.read_bytes()),
                  manifest_sha256=sha(json.dumps(manifest, sort_keys=True).encode()))
    full2 = tmp_path / 'full2.tar.gz'
    manifest2 = write_full(full2, {'database/credential-excluded-operational.sqlite3': db,
                                   'tweets/old.png': old, 'tweets/new.png': b'new'}, operation='op-2')
    runner2 = IncrementalBackup(index, lambda op: {'archive': str(full2), 'manifest': manifest2})
    delta = runner2.prepare('op-2', now=index.latest()['verified_at'] + 1)
    assert delta['kind'] == DELTA
    assert delta['manifest']['logical_files']['tweets/old.png']['source']['operation_id'] == 'op-1'
    assert 'tweets/new.png' in delta['manifest']['files']
    runner2.commit(delta, archive_key='delta-key', archive_sha256=sha(Path(delta['archive']).read_bytes()),
                   manifest_sha256=sha(json.dumps(delta['manifest'], sort_keys=True).encode()))
    assert index.latest()['kind'] == DELTA
    chain = index.chain('op-2')
    assert [item['operation_id'] for item in chain] == ['op-1', 'op-2']
    assert chain[0]['kind'] == BASE and chain[1]['kind'] == DELTA
    refs = index.chain_refs('op-2')
    assert len(refs) == 2 and all(item['bytes'] > 0 for item in refs)
