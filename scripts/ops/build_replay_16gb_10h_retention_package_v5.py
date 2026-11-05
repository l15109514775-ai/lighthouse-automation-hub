"""Build the exact runtime/test overlay used by the 16GB/10h retention requalification.

The archive contains every current retention runtime source, the complete test
tree, and the two changed configuration files. It does not contain production
data, browser profiles, credentials, downloaded media, or the unrelated debug
folders in the working tree.
"""
from pathlib import Path, PurePosixPath
import argparse
import hashlib
import json
import subprocess
import tarfile

VERSION = 'replay-16gb-10h-retention-candidate-v5'
BASE_IMAGE = 'sha256:1eca8c777e864de54bdd4708959117219408ed0c87e44b265ac762fa167a059d'
BASE_TAG = 'lighthouse-hub:source-inventory-v11-f48a1f1dcd3a'
RUNTIME_COUNT = 141
NODE_SOURCES = {
    'adapters/bilibili_video_guard.mjs',
    'adapters/browser_replay_part_v5.mjs',
    'adapters/replay_highest_contract_v2.mjs',
    'adapters/replay_later_result_lookup_v2.mjs',
    'adapters/replay_native_driver.mjs',
    'adapters/replay_natural_contract_v3.mjs',
    'adapters/replay_part_contract_v5.mjs',
    'adapters/replay_part_owner_handoff_v5.mjs',
    'adapters/short_video_saved_draft_navigation_control.mjs',
    'scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs',
    'vendor/subtitle-cloud-worker/observer/store.py',
}
CONFIG_SOURCES = {
    'config/browser-seccomp.json',
    'config/hub.toml',
    'config/whole-replay-planning-policy-v4.example.toml',
}
SUPPORT_SOURCES = {
    'scripts/ops/launch_replay_parts_workflow_v5.py',
}
TEST_GROUPS = {
    'tests.test_launch_replay_parts_workflow_v5': 16,
    'tests.test_media_processing': 20,
    'tests.test_metadata_v5_async': 11,
    'tests.test_replay_multipart_v4': 16,
    'tests.test_replay_multipart_worker_runtime_v4': 18,
    'tests.test_replay_part_draft_persistence_v5': 4,
    'tests.test_replay_part_native_v5': 13,
    'tests.test_replay_parts_runtime_v5': 9,
    'tests.test_replay_saved_draft_redirect_v10': 9,
    'tests.test_whole_inventory_comparison_v1': 34,
}
RETENTION_GROUPS = {'tests.test_replay_parts_retention_v5': 33}
UPLOAD_SELECTIONS = (
    'tests/test_media_processing.py',
    'tests/test_replay_multipart_v4.py',
    'tests/test_replay_multipart_worker_runtime_v4.py',
    'tests/test_metadata_v5_async.py',
    'tests/test_replay_saved_draft_redirect_v10.py',
    'tests/test_replay_part_draft_persistence_v5.py',
    'tests/test_replay_part_native_v5.py',
    'tests/test_replay_parts_runtime_v5.py',
    'tests/test_launch_replay_parts_workflow_v5.py',
    'tests/test_whole_inventory_comparison_v1.py',
)
RETENTION_SELECTIONS = ('tests/test_replay_parts_retention_v5.py',)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def wire(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def git(root, *arguments):
    return subprocess.run(
        ['git', *arguments], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def regular_files(root, directory):
    base = root / directory
    found = []
    for path in sorted(base.rglob('*')):
        if not path.is_file() or '__pycache__' in path.parts or path.suffix == '.pyc':
            continue
        if path.is_symlink():
            raise ValueError(f'UNSAFE_SYMLINK:{path}')
        found.append(path.relative_to(root).as_posix())
    return found


def runtime_files(root, release_reference):
    release = json.loads(release_reference.read_bytes())
    selected = set(release['source_files'])
    if len(selected) != RUNTIME_COUNT:
        raise ValueError(f'RUNTIME_SOURCE_COUNT:{len(selected)}')
    if not NODE_SOURCES <= selected:
        raise ValueError('RUNTIME_NODE_SCOPE_CHANGED')
    if any(not (root / name).is_file() for name in selected):
        raise ValueError('RUNTIME_SOURCE_MISSING')
    return sorted(selected)


def archive_info(name, data, mode=0o644):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.uid = info.gid = 0
    info.uname = info.gname = 'root'
    info.mtime = 0
    return info


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--root',
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help='lighthouse-hub checkout',
    )
    parser.add_argument(
        '--output-dir',
        type=Path,
        default=Path(
            r'/opt/lighthouse-hub\Chats\2026-10-31\codex-ubuntu-24-04-lts-cpu'
            r'\outputs\replay-16gb-10h-retention-v5'
        ),
    )
    parser.add_argument(
        '--release-reference',
        type=Path,
        default=Path(
            r'/opt/lighthouse-hub\Chats\2026-10-31\codex-ubuntu-24-04-lts-cpu'
            r'\outputs\replay-16gb-10h-retention-v5\source-release-before.json'
        ),
    )
    parser.add_argument(
        '--runtime-tar',
        type=Path,
        default=Path(
            r'/opt/lighthouse-hub\Chats\2026-10-31\codex-ubuntu-24-04-lts-cpu'
            r'\outputs\replay-16gb-10h-retention-v5\effective-runtime-141.tar'
        ),
    )
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)

    runtime_names = set(runtime_files(root, args.release_reference.resolve()))
    runtime_blobs = {}
    with tarfile.open(args.runtime_tar.resolve(), 'r:') as tar:
        entries = tar.getmembers()
        if len(entries) != RUNTIME_COUNT or {entry.name for entry in entries} != runtime_names:
            raise ValueError('EFFECTIVE_RUNTIME_FILE_SET_CHANGED')
        for entry in entries:
            path = PurePosixPath(entry.name)
            if (
                not entry.isfile()
                or entry.issym()
                or entry.islnk()
                or path.is_absolute()
                or '..' in path.parts
                or entry.size <= 0
            ):
                raise ValueError(f'UNSAFE_EFFECTIVE_RUNTIME:{entry.name}')
            runtime_blobs[entry.name] = tar.extractfile(entry).read()

    selected = (
        runtime_names
        | set(regular_files(root, 'tests'))
        | CONFIG_SOURCES
        | SUPPORT_SOURCES
    )
    selected = {name for name in selected if not name.startswith('%SystemDrive%/')}
    files = {}
    blobs = {}
    for name in sorted(selected):
        if name in runtime_blobs:
            raw = runtime_blobs[name]
        else:
            path = root / PurePosixPath(name)
            if path.is_symlink() or not path.is_file():
                raise ValueError(f'UNSAFE_SOURCE:{name}')
            raw = path.read_bytes()
        if not raw:
            raise ValueError(f'EMPTY_SOURCE:{name}')
        files[name] = {'bytes': len(raw), 'sha256': sha(raw)}
        blobs[name] = raw

    if sum(TEST_GROUPS.values()) != 150:
        raise ValueError('UPLOAD_TEST_COUNT_CHANGED')
    selected_tests = set(UPLOAD_SELECTIONS) | set(RETENTION_SELECTIONS)
    if not selected_tests <= {name for name in files if name.startswith('tests/')}:
        raise ValueError('TEST_SELECTION_MISSING')

    source_manifest = {
        'version': 'replay-16gb-10h-retention-source-manifest-v5',
        'source_commit': git(root, 'rev-parse', 'HEAD'),
        'working_tree_dirty': bool(git(root, 'status', '--porcelain', '--untracked-files=no')),
        'files': files,
        'test_selections': list(UPLOAD_SELECTIONS) + list(RETENTION_SELECTIONS),
        'expected_test_count': sum(TEST_GROUPS.values()) + sum(RETENTION_GROUPS.values()),
    }
    manifest = {
        'version': VERSION,
        'base_image': BASE_IMAGE,
        'base_tag': BASE_TAG,
        'source_commit': source_manifest['source_commit'],
        'working_tree_dirty': source_manifest['working_tree_dirty'],
        'source_manifest': source_manifest,
        'delta_sha256': {name: value['sha256'] for name, value in files.items()},
        'upload_test_selections': list(UPLOAD_SELECTIONS),
        'upload_test_groups': TEST_GROUPS,
        'upload_expected_test_count': sum(TEST_GROUPS.values()),
        'retention_test_selections': list(RETENTION_SELECTIONS),
        'retention_test_groups': RETENTION_GROUPS,
        'retention_expected_test_count': sum(RETENTION_GROUPS.values()),
        'runtime_source_count': RUNTIME_COUNT,
    }
    manifest_raw = wire(manifest) + b'\n'
    archive = output / 'replay-16gb-10h-retention-v5.tar.gz'
    with tarfile.open(archive, 'w:gz', compresslevel=9) as tar:
        tar.addfile(archive_info('manifest.json', manifest_raw), __import__('io').BytesIO(manifest_raw))
        for name in sorted(blobs):
            tar.addfile(archive_info(name, blobs[name]), __import__('io').BytesIO(blobs[name]))
    archive_raw = archive.read_bytes()
    digest = sha(archive_raw)
    (output / 'manifest.json').write_bytes(manifest_raw)
    (output / 'package-sha256.txt').write_text(digest + '\n', encoding='ascii')
    result = {
        'version': VERSION,
        'archive': str(archive),
        'archive_bytes': len(archive_raw),
        'archive_sha256': digest,
        'runtime_sources': RUNTIME_COUNT,
        'manifest_files': len(files),
        'upload_tests': sum(TEST_GROUPS.values()),
        'retention_tests': sum(RETENTION_GROUPS.values()),
        'source_commit': source_manifest['source_commit'],
        'working_tree_dirty': source_manifest['working_tree_dirty'],
    }
    (output / 'build-result.json').write_bytes(wire(result) + b'\n')
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
