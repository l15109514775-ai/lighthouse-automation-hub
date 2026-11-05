"""Versioned driver-only qualification over the immutable v9 source manifest.

Reuse the already qualified v9 implementation after checking its exact bytes.
Every semantic source adaptation is an exact-one replacement. No backup API,
production account, service restart or application-data mount is introduced.
"""
from pathlib import Path
import argparse
import hashlib
import sys
import types

PARENT_HELPER_SHA = 'ebe101d849dcae075808ff55c8daef711c560f1b730478a6fc76c684755f8900'
BASE = 'sha256:5c5391b8bf313525f6fc7c43db40ad6832b6c6b431e58a5aeb0bcf04bdc4e981'
CURRENT = 'fc177477ad458de1c461504fb4ab612055e9c8d086f399eb02eef28baa337437'
PARENT = Path('/var/lib/lighthouse-backup-ops/download-access-v9-tests/58a164b2-382b-45a6-ad7c-5f88b8abac49/test-result.json')
PARENT_SHA = '32e70d2ce83fda10709e116053e3afcdf7d64b4bc5bf9ff61d09edd407bdb329'
OLD26 = Path('/var/lib/lighthouse-backup-ops/replay-parts-retention-v5-tests/1268e129-c608-4c42-94c0-205be7e5e56c/test-attestation.json')
OLD26_SHA = 'cb7d2345adbb7173bdbf2503bfdc54d3d923e01a3e5dc98f18b49b28a0f7744b'
DRIVER = 'adapters/replay_native_driver.mjs'
DRIVER_SHA = '2716d0199ebd8777e24abd641d5c17c46618f2302692160f0ef7405f6933a5d1'

REPLACEMENTS = (
    ("parent['version'] == 'download-access-ubuntu-v8'", "parent['version'] == 'download-access-ubuntu-v9'"),
    ("len(parent['source_manifest']['files']) == 904", "len(parent['source_manifest']['files']) == 910"),
    ("parent['tests'] == 141", "parent['tests'] == 200"),
    ("m.junit(parent_xml, 141)", "m.junit(parent_xml, 200)"),
    ("'download-access-candidate-v9'", "'replay-editor-candidate-v10'"),
    ("len(base['RootFS']['Layers']) == 8 and len(candidate['RootFS']['Layers']) == 9", "len(base['RootFS']['Layers']) == 9 and len(candidate['RootFS']['Layers']) == 10"),
    ("Path('/var/lib/lighthouse-backup-ops/download-access-v9-tests')", "Path('/var/lib/lighthouse-backup-ops/replay-editor-v10-tests')"),
    ("'lighthouse-hub:download-access-v9-'", "'lighthouse-hub:replay-editor-v10-'"),
    ("result.update(version='download-access-ubuntu-v9'", "result.update(version='replay-editor-ubuntu-v10'"),
    ("'download_access_v9_sha256':sha(raw)", "'replay_editor_v10_sha256':sha(raw)"),
)


def derive(raw):
    if hashlib.sha256(raw).hexdigest() != PARENT_HELPER_SHA:
        raise ValueError('V10_QUALIFIER_PARENT_BYTES_CHANGED')
    text = raw.decode('utf-8')
    for old, new in REPLACEMENTS:
        if text.count(old) != 1:
            raise ValueError('V10_EXACT_ONE_REPLACEMENT_REQUIRED')
        text = text.replace(old, new, 1)
    return text


def operator(parent_file=None):
    file = Path(parent_file) if parent_file else Path(__file__).with_name('qualify_download_access_v9.py')
    text = derive(file.read_bytes())
    op = types.ModuleType('replay_editor_v10_frozen_qualification')
    op.__file__ = str(file)
    exec(compile(text, str(file) + ':editor-v10', 'exec'), op.__dict__)
    op.BASE, op.CURRENT = BASE, CURRENT
    op.TAG = 'lighthouse-hub:download-access-v9-87a7f1d91910'
    op.PARENT, op.PARENT_SHA = PARENT, PARENT_SHA
    op.OLD26, op.OLD26_SHA = OLD26, OLD26_SHA
    op.PRODUCTION = {DRIVER}
    op.FROZEN_RUNTIME_SHA256 = {DRIVER: DRIVER_SHA}
    op.RUNNER = 'scripts/ops/qualify_replay_editor_v10.py'
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
