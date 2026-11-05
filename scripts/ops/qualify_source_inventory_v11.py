"""Whole-inventory-only qualification over the actual immutable v10 manifest.

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
BASE = 'sha256:99529b522af2ef562638e9da47523c9ae3700d98f6224269b6e0b9b2df306d20'
CURRENT = '1474aad9e99f77f6c5371c4f226252f99253ef4f6b5c894dfbfe43718e6bee5c'
PARENT = Path('/var/lib/lighthouse-backup-ops/replay-editor-v10-tests/c622e44f-82b8-4e8e-8e6f-f79b66732937/test-result.json')
PARENT_SHA = '3329dd0eeff8c105ad53fa47df792796088c73ef7a348569ddfa9e14a840e960'
OLD26 = Path('/var/lib/lighthouse-backup-ops/replay-parts-retention-v5-tests/58be4830-7cd9-426d-99e8-f661dd10d444/test-attestation.json')
OLD26_SHA = '0be036cab4d7f359fb3144f36384b417f2de6e613a2d0d6028c9598c3f42de0b'
WHOLE = 'adapters/ytdlp_whole_v4.py'
WHOLE_SHA = 'adbae07574435407448e3a4483c2c593e8d1cb18b1aee5bcc3c367a5953114e9'

REPLACEMENTS = (
    ("parent['version'] == 'download-access-ubuntu-v8'", "parent['version'] == 'replay-editor-ubuntu-v10'"),
    ("len(parent['source_manifest']['files']) == 904", "len(parent['source_manifest']['files']) == 914"),
    ("parent['tests'] == 141", "parent['tests'] == 45"),
    ("m.junit(parent_xml, 141)", "m.junit(parent_xml, 45)"),
    ("'download-access-candidate-v9'", "'source-inventory-candidate-v11'"),
    ("len(base['RootFS']['Layers']) == 8 and len(candidate['RootFS']['Layers']) == 9", "len(base['RootFS']['Layers']) == 10 and len(candidate['RootFS']['Layers']) == 11"),
    ("Path('/var/lib/lighthouse-backup-ops/download-access-v9-tests')", "Path('/var/lib/lighthouse-backup-ops/source-inventory-v11-tests')"),
    ("'lighthouse-hub:download-access-v9-'", "'lighthouse-hub:source-inventory-v11-'"),
    ("result.update(version='download-access-ubuntu-v9'", "result.update(version='source-inventory-ubuntu-v11'"),
    ("'download_access_v9_sha256':sha(raw)", "'source_inventory_v11_sha256':sha(raw)"),
)


def derive(raw):
    if hashlib.sha256(raw).hexdigest() != PARENT_HELPER_SHA:
        raise ValueError('V11_QUALIFIER_PARENT_BYTES_CHANGED')
    text = raw.decode('utf-8')
    for old, new in REPLACEMENTS:
        if text.count(old) != 1:
            raise ValueError('V11_EXACT_ONE_REPLACEMENT_REQUIRED')
        text = text.replace(old, new, 1)
    return text


def operator(parent_file=None):
    file = Path(parent_file) if parent_file else Path(__file__).with_name('qualify_download_access_v9.py')
    text = derive(file.read_bytes())
    op = types.ModuleType('source_inventory_v11_frozen_qualification')
    op.__file__ = str(file)
    exec(compile(text, str(file) + ':editor-v11', 'exec'), op.__dict__)
    op.BASE, op.CURRENT = BASE, CURRENT
    op.TAG = 'lighthouse-hub:replay-editor-v10-7c1aee2caf68'
    op.PARENT, op.PARENT_SHA = PARENT, PARENT_SHA
    op.OLD26, op.OLD26_SHA = OLD26, OLD26_SHA
    op.PRODUCTION = {WHOLE}
    op.FROZEN_RUNTIME_SHA256 = {WHOLE: WHOLE_SHA}
    op.RUNNER = 'scripts/ops/qualify_source_inventory_v11.py'
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
