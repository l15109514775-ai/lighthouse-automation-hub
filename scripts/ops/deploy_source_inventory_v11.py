"""One whole-inventory video cutover with the fixed quiescent deployment gates.

Default remains plan-only. Actual pins must come from the v11 Ubuntu evidence.
The backup enabled/active fingerprint is preserved; no backup unit is changed.
"""
from pathlib import Path
import argparse
import hashlib
import sys
import types

PARENT_HELPER_SHA = '1e7e9dfd7c289349a5949f2568d3a996e5c68823722ad6eadeed12e1a59d7d20'
CURRENT = '1474aad9e99f77f6c5371c4f226252f99253ef4f6b5c894dfbfe43718e6bee5c'
OLD_IMAGE = 'sha256:99529b522af2ef562638e9da47523c9ae3700d98f6224269b6e0b9b2df306d20'
OLD_PROOF = Path('/var/lib/lighthouse-backup-ops/replay-parts-retention-v5-tests/58be4830-7cd9-426d-99e8-f661dd10d444')
OLD_PROOF_SHA = '0be036cab4d7f359fb3144f36384b417f2de6e613a2d0d6028c9598c3f42de0b'
PARENT45_SHA = '3329dd0eeff8c105ad53fa47df792796088c73ef7a348569ddfa9e14a840e960'
IMAGE = 'sha256:1eca8c777e864de54bdd4708959117219408ed0c87e44b265ac762fa167a059d'
QUALIFIED = Path('/var/lib/lighthouse-backup-ops/source-inventory-v11-tests/f0d3f96b-cab3-4111-a042-a22d1597d26a/test-result.json')
QUALIFIED_SHA = 'e9c9108b3998c5aa40ef310a71f99cfc9ccdbe712630a40970ef6c0cad3feeb7'
PROOF = Path('/var/lib/lighthouse-backup-ops/replay-parts-retention-v5-tests/e5a072f7-d9bf-497b-b09a-2855211abe66/test-attestation.json')
PROOF_SHA = 'eed9f509659316b7ca02cf5a7e23fc4c9a6d86ed79b032103a21263864b0f994'
SOURCE_COMMIT = '4930e5af27c8f569286395b1b32344a3bda768de'
ARCHIVE_SHA = 'f48a1f1dcd3a5df7df1cf3d7876873759c42ae84779c49843bf171348fe7eef2'
WHOLE_SHA = 'adbae07574435407448e3a4483c2c593e8d1cb18b1aee5bcc3c367a5953114e9'
GROUPS = {'tests.ops.test_qualify_source_inventory_v11': 12, 'tests.test_whole_inventory_comparison_v1': 31, 'tests.test_youtube_download_v4': 1}
SOURCE_COUNT = 917

REPLACEMENTS = (
    ("len(SOURCES)==3", "len(SOURCES)==1"),
    ("len(original['RootFS']['Layers'])==8 and len(candidate['RootFS']['Layers'])==9", "len(original['RootFS']['Layers'])==10 and len(candidate['RootFS']['Layers'])==11"),
    ("delta['version']=='download-access-ubuntu-v9'", "delta['version']=='source-inventory-ubuntu-v11'"),
    ("proof['qualification_lineage']['download_access_v9_sha256']", "proof['qualification_lineage']['source_inventory_v11_sha256']"),
    ("'version':'download-access-cutover-v9'", "'version':'source-inventory-cutover-v11'"),
    ("report['phase']='preflight'", "backup_guard();report['phase']='preflight'"),
    ("report['phase']='before_stop'", "backup_guard();report['phase']='before_stop'"),
    ("report.update(state='download_access_v9_active_paused'", "backup_guard();report.update(state='download_access_v9_active_paused'"),
    ("state='download_access_v9_active_paused'", "state='source_inventory_v11_active_paused'"),
)


def derive(raw):
    if hashlib.sha256(raw).hexdigest() != PARENT_HELPER_SHA:
        raise ValueError('V11_DEPLOYER_PARENT_BYTES_CHANGED')
    text = raw.decode('utf-8')
    for old, new in REPLACEMENTS:
        if text.count(old) != 1:
            raise ValueError('V11_EXACT_ONE_REPLACEMENT_REQUIRED')
        text = text.replace(old, new, 1)
    return text


def operator(parent_file=None):
    file = Path(parent_file) if parent_file else Path(__file__).with_name('deploy_download_access_v9.py')
    op = types.ModuleType('source_inventory_v11_frozen_deployment')
    op.__file__ = str(file)
    exec(compile(derive(file.read_bytes()), str(file) + ':editor-v11', 'exec'), op.__dict__)
    op.STATE = Path('/var/lib/lighthouse-source-inventory-v11')
    for name in ('CURRENT', 'OLD_IMAGE', 'IMAGE', 'OLD_PROOF', 'OLD_PROOF_SHA',
                 'QUALIFIED', 'QUALIFIED_SHA', 'PROOF', 'PROOF_SHA',
                 'SOURCE_COMMIT', 'ARCHIVE_SHA', 'GROUPS', 'SOURCE_COUNT'):
        setattr(op, name, globals()[name])
    op.PARENT141_SHA = PARENT45_SHA
    op.SOURCES = {'adapters/ytdlp_whole_v4.py': WHOLE_SHA}
    def missing_backup_baseline(): raise ValueError('BACKUP_FINGERPRINT_BASELINE_REQUIRED')
    op.backup_guard = missing_backup_baseline
    return op


def backup_fingerprint(run=None):
    import subprocess
    run = subprocess.run if run is None else run
    result = {}
    for unit in ('lighthouse-cloudbase-backup.timer', 'lighthouse-cloudbase-backup.service'):
        fields = ('UnitFileState', 'ActiveState', 'SubState')
        if unit.endswith('.service'):
            fields += ('MainPID', 'ExecMainStartTimestampMonotonic', 'ExecMainExitTimestampMonotonic')
        args = ['/usr/bin/systemctl', 'show', unit, *('--property='+k for k in fields)]
        child = run(args, capture_output=True, text=True, timeout=15)
        if child.returncode != 0: raise ValueError('BACKUP_STATUS_UNAVAILABLE')
        pairs = [line.split('=', 1) for line in child.stdout.splitlines()]
        value = dict(pairs)
        if len(value) != len(pairs) or set(value) != set(fields): raise ValueError('BACKUP_STATUS_SHAPE_CHANGED')
        if unit.endswith('.service') and (value['ActiveState'] not in ('inactive', 'failed') or value['MainPID'] != '0'):
            raise ValueError('BACKUP_SERVICE_RUNNING')
        result[unit] = value
    return result


def preserve_backup(before, run=None):
    if backup_fingerprint(run) != before: raise ValueError('BACKUP_FINGERPRINT_CHANGED')


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--parent-helper', type=Path)
    args, remaining = parser.parse_known_args()
    op = operator(args.parent_helper)
    before = backup_fingerprint()
    op.backup_guard = lambda: preserve_backup(before)
    sys.argv = [sys.argv[0], *remaining]
    try:
        op.main()
    finally:
        preserve_backup(before)


if __name__ == '__main__':
    main()
