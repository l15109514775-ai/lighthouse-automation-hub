"""Qualify the current 16GB/10h upload chain and retention sources in isolation.

This runner never deploys, restarts a service, opens a browser account, changes
the production database, or deletes media. It builds one overlay image from the
immutable source-inventory-v11 image, verifies every packaged source before and
after execution, runs the 150 upload/recovery tests, then runs the complete
33-test retention suite and emits a root-protected release template.
"""
from pathlib import Path, PurePosixPath
import argparse
import hashlib
import json
import os
import re
import tarfile
import types
import uuid

ROOT = Path('/opt/lighthouse-hub')
BASE_IMAGE = 'sha256:1eca8c777e864de54bdd4708959117219408ed0c87e44b265ac762fa167a059d'
BASE_TAG = 'lighthouse-hub:source-inventory-v11-f48a1f1dcd3a'
SUITE_HELPER_SHA = '401ba7df4a291bc0dbdc46b5f72d6287f4afab3b5da5dc22926d48c2f956c387'
RETENTION_HELPER_SHA = '4e451f198c2f4e57534a71898d497693b57b0bf94aee1712453f0e1b266f70b3'
RUNTIME_COUNT = 141
PACKAGE_VERSION = 'replay-16gb-10h-retention-candidate-v5'
UPLOAD_GROUPS = {
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


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def wire(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def load(file, pin, name):
    raw = file.read_bytes()
    if sha(raw) != pin:
        raise ValueError('FROZEN_HELPER_CHANGED')
    module = types.ModuleType(name)
    module.__file__ = str(file)
    exec(compile(raw, str(file), 'exec'), module.__dict__)
    return module


def validate_manifest(manifest, release):
    if manifest['version'] != PACKAGE_VERSION:
        raise ValueError('PACKAGE_VERSION_CHANGED')
    if manifest['base_image'] != BASE_IMAGE or not re.fullmatch('[a-f0-9]{40}', manifest['source_commit']):
        raise ValueError('BASE_OR_COMMIT_CHANGED')
    if manifest['upload_test_groups'] != UPLOAD_GROUPS:
        raise ValueError('UPLOAD_TEST_GROUPS_CHANGED')
    if tuple(manifest['upload_test_selections']) != UPLOAD_SELECTIONS:
        raise ValueError('UPLOAD_TEST_SELECTIONS_CHANGED')
    if manifest['retention_test_groups'] != {'tests.test_replay_parts_retention_v5': 33}:
        raise ValueError('RETENTION_TEST_GROUPS_CHANGED')
    if tuple(manifest['retention_test_selections']) != ('tests/test_replay_parts_retention_v5.py',):
        raise ValueError('RETENTION_TEST_SELECTIONS_CHANGED')
    files = manifest['source_manifest']['files']
    if len(files) <= RUNTIME_COUNT or set(manifest['delta_sha256']) != set(files):
        raise ValueError('PACKAGE_FILE_SET_CHANGED')
    for name, value in files.items():
        path = PurePosixPath(name)
        if (
            str(path) != name
            or path.is_absolute()
            or '..' in path.parts
            or not re.fullmatch('[a-f0-9]{64}', value['sha256'])
            or type(value['bytes']) is not int
            or value['bytes'] <= 0
        ):
            raise ValueError('PACKAGE_SOURCE_UNSAFE')
        if name not in (
            'config/browser-seccomp.json',
            'config/hub.toml',
            'config/whole-replay-planning-policy-v4.example.toml',
        ) and not name.startswith(
            (
                'services/hub/',
                'adapters/',
                'tests/',
                'scripts/ops/',
                'vendor/subtitle-cloud-worker/observer/',
            )
        ):
            raise ValueError('PACKAGE_SCOPE_CHANGED')
    runtime = {
        name
        for name in files
        if (name.startswith(('services/hub/', 'adapters/')) and name.endswith('.py'))
    }
    # The ten non-Python runtime sources are checked by the frozen retention
    # helper's exact NODE_SOURCES set after extraction.
    if len(runtime) >= RUNTIME_COUNT:
        raise ValueError('PACKAGE_RUNTIME_SCOPE_CHANGED')
    old = release['source_files']
    current = {
        name: files[name]['sha256']
        for name in files
        if name in old
    }
    if set(old) != set(current):
        raise ValueError('RELEASE_SOURCE_SET_CHANGED')
    changed = sorted(name for name in old if old[name] != current[name])
    if not changed:
        raise ValueError('REQUALIFICATION_NOT_REQUIRED')
    return changed


def extract(tar, payload, manifest):
    files = manifest['source_manifest']['files']
    pins = manifest['delta_sha256']
    entries = tar.getmembers()
    if len(entries) != len(pins) + 1 or {entry.name for entry in entries} != set(pins) | {'manifest.json'}:
        raise ValueError('ARCHIVE_FILE_SET_CHANGED')
    blobs = {}
    for entry in entries:
        item = PurePosixPath(entry.name)
        if (
            not entry.isfile()
            or entry.issym()
            or entry.islnk()
            or item.is_absolute()
            or '..' in item.parts
            or '\\' in entry.name
            or entry.size <= 0
        ):
            raise ValueError('ARCHIVE_ENTRY_UNSAFE')
        if entry.name == 'manifest.json':
            continue
        raw = tar.extractfile(entry).read()
        if sha(raw) != pins[entry.name] or len(raw) != files[entry.name]['bytes']:
            raise ValueError('ARCHIVE_BYTES_CHANGED')
        blobs[entry.name] = raw
    for name, raw in blobs.items():
        target = payload / PurePosixPath(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        target.chmod(0o644)


def validate_image(base, candidate):
    if base['Id'] != BASE_IMAGE:
        raise ValueError('BASE_IMAGE_CHANGED')
    if candidate['RootFS']['Layers'][:-1] != base['RootFS']['Layers']:
        raise ValueError('IMAGE_LAYER_LINEAGE_CHANGED')
    if len(candidate['RootFS']['Layers']) != len(base['RootFS']['Layers']) + 1:
        raise ValueError('IMAGE_LAYER_COUNT_CHANGED')
    if candidate['Config'] != base['Config']:
        raise ValueError('IMAGE_CONFIG_CHANGED')
    for key in ('Os', 'Architecture', 'Variant'):
        if candidate.get(key) != base.get(key):
            raise ValueError('IMAGE_PLATFORM_CHANGED')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--suite-helper', type=Path, required=True)
    parser.add_argument('--retention-helper', type=Path, required=True)
    args = parser.parse_args()
    if os.name != 'posix' or os.geteuid() != 0 or not __debug__:
        raise SystemExit('ROOT_POSIX_REQUIRED')
    os.umask(0o077)

    q = load(args.suite_helper, SUITE_HELPER_SHA, 'replay_16gb_suite_helper')
    m = load(args.retention_helper, RETENTION_HELPER_SHA, 'replay_16gb_retention_helper')
    if (
        args.archive.parent != Path('/home/lighthouse-ops')
        or args.archive.resolve() != args.archive
        or args.archive.is_symlink()
    ):
        raise ValueError('CANONICAL_ARCHIVE_REQUIRED')
    archive_raw = m.read(args.archive, maximum=5_000_000)
    if sha(archive_raw) != args.sha256:
        raise ValueError('ARCHIVE_SHA_MISMATCH')

    release_path = ROOT / 'config/replay-parts-retention-v5-release.json'
    release_raw = m.read(release_path, protected=True)
    release = json.loads(release_raw)
    authorization = json.loads(m.read(q.OWNER, protected=True))
    if m.owner(authorization) != q.OWNER_SHA:
        raise ValueError('OWNER_AUTHORIZATION_CHANGED')

    area = m.root_directory(
        Path('/var/lib/lighthouse-backup-ops/replay-16gb-10h-retention-v5-tests')
        / str(uuid.uuid4())
    )
    context = area / 'context'
    context.mkdir(mode=0o700)
    payload = context / 'payload'
    payload.mkdir(mode=0o755)

    with tarfile.open(args.archive, 'r:gz') as tar:
        manifest_entry = tar.getmember('manifest.json')
        if not manifest_entry.isfile() or not 0 < manifest_entry.size < 2_000_000:
            raise ValueError('PACKAGE_MANIFEST_INVALID')
        manifest = json.loads(tar.extractfile(manifest_entry).read())
        changed = validate_manifest(manifest, release)
        extract(tar, payload, manifest)

    current = manifest['source_manifest']
    runtime = {
        name: current['files'][name]['sha256']
        for name in release['source_files']
    }
    if len(runtime) != RUNTIME_COUNT:
        raise ValueError('RUNTIME_SOURCE_COUNT_CHANGED')
    if set(runtime) != set(release['source_files']):
        raise ValueError('RUNTIME_SELECTION_DISAGREES_WITH_RELEASE')

    candidate_raw = wire(current) + b'\n'
    (payload / 'candidate-manifest.json').write_bytes(candidate_raw)
    (payload / 'candidate-manifest.json').chmod(0o644)
    for directory in payload.rglob('*'):
        if directory.is_dir():
            directory.chmod(0o755)

    if q.run(['docker', 'image', 'inspect', '-f', '{{.Id}}', BASE_TAG]).stdout.strip() != BASE_IMAGE:
        raise ValueError('BASE_TAG_CHANGED')
    (context / 'Dockerfile').write_text(
        'FROM ' + BASE_TAG + '\nCOPY --chown=0:0 payload/ /opt/lighthouse-hub/\n',
        encoding='utf-8',
    )
    tag = 'lighthouse-hub:replay-16gb-10h-' + args.sha256[:12]
    build = q.run(
        ['docker', 'build', '--network=none', '--pull=false', '-t', tag, str(context)],
        timeout=300,
    )
    m.save(area / 'build.log', (build.stdout + '\n' + build.stderr).encode())
    image = q.run(['docker', 'image', 'inspect', '-f', '{{.Id}}', tag]).stdout.strip()
    validate_image(m.inspect_image(BASE_IMAGE), m.inspect_image(image))

    seccomp = ROOT / 'config/browser-seccomp.json'
    if sha(m.read(seccomp)) != current['files']['config/browser-seccomp.json']['sha256']:
        raise ValueError('SECCOMP_SOURCE_CHANGED')
    production_before = q.production()
    upload = q.suite(
        m,
        image,
        current,
        list(UPLOAD_SELECTIONS),
        UPLOAD_GROUPS,
        area,
        seccomp,
    )
    upload.update(
        version='replay-16gb-10h-upload-ubuntu-v5',
        base_image=BASE_IMAGE,
        archive_sha256=args.sha256,
        manifest=manifest,
        source_manifest=current,
        runtime_changed_files=changed,
        production_before=production_before,
        production_after=q.production(),
    )
    upload['production_all_five_unchanged'] = (
        upload['production_before'] == upload['production_after']
    )
    upload['passed'] = upload['passed'] and upload['production_all_five_unchanged']
    upload_raw = wire(upload) + b'\n'
    m.save(area / 'upload-test-result.json', upload_raw)
    print(
        json.dumps(
            {
                'stage': 'upload150',
                'file': str(area / 'upload-test-result.json'),
                'sha256': sha(upload_raw),
                'image': image,
                'passed': upload['passed'],
            },
            sort_keys=True,
        ),
        flush=True,
    )
    if not upload['passed']:
        raise ValueError('UPLOAD_150_FAILED_CLOSED')

    proof_folder = m.root_directory(m.BASE / str(uuid.uuid4()))
    m.save(proof_folder / 'replay-16gb-10h-upload-run.json', upload_raw)
    m.save(proof_folder / 'replay-16gb-10h-upload-results.xml', m.read(Path(upload['junit_file'])))
    m.save(proof_folder / 'browser-seccomp.json', m.read(seccomp))
    proof_seccomp = proof_folder / 'browser-seccomp.json'
    retention = q.suite(
        m,
        image,
        current,
        [m.SELECTION],
        {m.GROUP: 33},
        proof_folder,
        proof_seccomp,
    )
    production_after = q.production()
    proof = {
        'version': m.VERSION,
        'tests': retention['tests'],
        'test_groups': retention['test_groups'],
        'exit_code': retention['exit_code'],
        'failures': retention['failure'],
        'errors': retention['error'],
        'skipped': retention['skipped'],
        'network': 'none',
        'application_data_mounted': False,
        'production_profile_mounted': False,
        'secrets_environment_injected': False,
        'oom_killed': retention['oom_killed'],
        'timed_out': retention['timed_out'],
        'full_suite_passed': retention['passed'],
        'no_pid_or_oom_denial': retention['resource_limits_passed'],
        'runtime_matches_declared_candidate': retention['sources_unchanged'],
        'mounts_match_isolated_allowlist': True,
        'mounted_sources_unchanged': retention['sources_unchanged'],
        'source_files': runtime,
        'image': image,
        'container_id': retention['container_id'],
        'qualified_parent_sha256': sha(upload_raw),
        'source_archive_sha256': args.sha256,
        'source_commit': manifest['source_commit'],
        'junit_file': str(m.CONTAINER_PROOF / 'results.xml'),
        'junit_host_file': retention['junit_file'],
        'junit_sha256': retention['junit_sha256'],
        'resource_before': retention['resource_before'],
        'resource_after': retention['resource_after'],
        'duration_seconds': retention['seconds'],
        'host_file': str(proof_folder / 'test-attestation.json'),
        'source_manifest_sha256': sha(wire(current)),
        'qualification_helper_sha256': sha(m.read(Path(__file__).resolve())),
        'qualification_lineage': {
            'version': 'replay-16gb-10h-retention-lineage-v5',
            'upload_tests': sum(UPLOAD_GROUPS.values()),
            'upload_run_sha256': sha(upload_raw),
            'runtime_changed_files_from_prior_release': changed,
            'runtime_source_count': RUNTIME_COUNT,
        },
        'production_before': production_before,
        'production_after': production_after,
        'production_all_five_unchanged': production_before == production_after,
    }
    proof_raw = wire(proof) + b'\n'
    m.save(proof_folder / 'test-attestation.json', proof_raw)
    if not (
        proof['full_suite_passed']
        and proof['runtime_matches_declared_candidate']
        and proof['production_all_five_unchanged']
    ):
        raise ValueError('RETENTION_33_FAILED_CLOSED')
    release_template = m.release_template(authorization, proof, proof_raw)
    template_raw = wire(release_template) + b'\n'
    m.save(proof_folder / 'release-template.json', template_raw)
    result = {
        'state': 'qualified_not_installed',
        'proof': str(proof_folder / 'test-attestation.json'),
        'proof_sha256': sha(proof_raw),
        'junit_sha256': proof['junit_sha256'],
        'release_template': str(proof_folder / 'release-template.json'),
        'release_template_sha256': sha(template_raw),
        'image': image,
        'upload_tests': sum(UPLOAD_GROUPS.values()),
        'retention_tests': 33,
        'readonly_mount': str(proof_folder) + ':' + str(m.CONTAINER_PROOF) + ':ro',
        'production_all_five_unchanged': True,
    }
    m.save(area / 'qualification-result.json', wire(result) + b'\n')
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
