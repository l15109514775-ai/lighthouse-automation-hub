"""Driver-only video cutover, reusing the fixed v9 quiescent deployment gates.

Default remains plan-only. Actual pins must come from the v10 Ubuntu evidence.
The backup pause is outside this operator's scope; no backup unit is touched.
"""
from pathlib import Path
import argparse
import hashlib
import sys
import types

PARENT_HELPER_SHA = '1e7e9dfd7c289349a5949f2568d3a996e5c68823722ad6eadeed12e1a59d7d20'
CURRENT = 'fc177477ad458de1c461504fb4ab612055e9c8d086f399eb02eef28baa337437'
OLD_IMAGE = 'sha256:5c5391b8bf313525f6fc7c43db40ad6832b6c6b431e58a5aeb0bcf04bdc4e981'
OLD_PROOF = Path('/var/lib/lighthouse-backup-ops/replay-parts-retention-v5-tests/1268e129-c608-4c42-94c0-205be7e5e56c')
OLD_PROOF_SHA = 'cb7d2345adbb7173bdbf2503bfdc54d3d923e01a3e5dc98f18b49b28a0f7744b'
PARENT200_SHA = '32e70d2ce83fda10709e116053e3afcdf7d64b4bc5bf9ff61d09edd407bdb329'
IMAGE = 'sha256:99529b522af2ef562638e9da47523c9ae3700d98f6224269b6e0b9b2df306d20'
QUALIFIED = Path('/var/lib/lighthouse-backup-ops/replay-editor-v10-tests/c622e44f-82b8-4e8e-8e6f-f79b66732937/test-result.json')
QUALIFIED_SHA = '3329dd0eeff8c105ad53fa47df792796088c73ef7a348569ddfa9e14a840e960'
PROOF = Path('/var/lib/lighthouse-backup-ops/replay-parts-retention-v5-tests/58be4830-7cd9-426d-99e8-f661dd10d444/test-attestation.json')
PROOF_SHA = '0be036cab4d7f359fb3144f36384b417f2de6e613a2d0d6028c9598c3f42de0b'
SOURCE_COMMIT = '2d6368390def04fa20053f1c3c74cefd822091d3'
ARCHIVE_SHA = '7c1aee2caf684fa6aece37d7567574abe989bbc13d3d03af766fc5a4c97a70cc'
DRIVER_SHA = '2716d0199ebd8777e24abd641d5c17c46618f2302692160f0ef7405f6933a5d1'
GROUPS = {'tests.ops.test_qualify_replay_editor_v10': 12, 'tests.test_replay_native_driver': 11, 'tests.test_replay_part_native_v5': 13, 'tests.test_replay_saved_draft_redirect_v10': 9}
SOURCE_COUNT = 914

REPLACEMENTS = (
    ("len(SOURCES)==3", "len(SOURCES)==1"),
    ("len(original['RootFS']['Layers'])==8 and len(candidate['RootFS']['Layers'])==9", "len(original['RootFS']['Layers'])==9 and len(candidate['RootFS']['Layers'])==10"),
    ("delta['version']=='download-access-ubuntu-v9'", "delta['version']=='replay-editor-ubuntu-v10'"),
    ("proof['qualification_lineage']['download_access_v9_sha256']", "proof['qualification_lineage']['replay_editor_v10_sha256']"),
    ("'version':'download-access-cutover-v9'", "'version':'replay-editor-cutover-v10'"),
    ("state='download_access_v9_active_paused'", "state='replay_editor_v10_active_paused'"),
)


def derive(raw):
    if hashlib.sha256(raw).hexdigest() != PARENT_HELPER_SHA:
        raise ValueError('V10_DEPLOYER_PARENT_BYTES_CHANGED')
    text = raw.decode('utf-8')
    for old, new in REPLACEMENTS:
        if text.count(old) != 1:
            raise ValueError('V10_EXACT_ONE_REPLACEMENT_REQUIRED')
        text = text.replace(old, new, 1)
    return text


def operator(parent_file=None):
    file = Path(parent_file) if parent_file else Path(__file__).with_name('deploy_download_access_v9.py')
    op = types.ModuleType('replay_editor_v10_frozen_deployment')
    op.__file__ = str(file)
    exec(compile(derive(file.read_bytes()), str(file) + ':editor-v10', 'exec'), op.__dict__)
    op.STATE = Path('/var/lib/lighthouse-replay-editor-v10')
    for name in ('CURRENT', 'OLD_IMAGE', 'IMAGE', 'OLD_PROOF', 'OLD_PROOF_SHA',
                 'QUALIFIED', 'QUALIFIED_SHA', 'PROOF', 'PROOF_SHA',
                 'SOURCE_COMMIT', 'ARCHIVE_SHA', 'GROUPS', 'SOURCE_COUNT'):
        setattr(op, name, globals()[name])
    op.PARENT141_SHA = PARENT200_SHA
    op.SOURCES = {'adapters/replay_native_driver.mjs': DRIVER_SHA}
    return op


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--parent-helper', type=Path)
    args, remaining = parser.parse_known_args()
    op = operator(args.parent_helper)
    sys.argv = [sys.argv[0], *remaining]
    op.main()


if __name__ == '__main__':
    main()
