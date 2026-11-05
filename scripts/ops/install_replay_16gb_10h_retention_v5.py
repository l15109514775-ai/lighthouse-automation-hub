"""Install a qualified retention release and rotate the worker proof mount.

The installer changes exactly one video-worker bind mount, recreates only that
container, verifies all declared runtime sources inside the effective
container, and then atomically installs the root-protected release. It never
launches a publish job, touches browser profiles, or deletes media itself.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import subprocess
import time
import types
import uuid

ROOT = Path('/opt/lighthouse-hub')
COMPOSE = ROOT / 'compose.yaml'
PROOF_TARGET = '/run/qualifications/retention-v5'
CONTAINER = 'lighthouse-hub-video-worker-1'
RETENTION_HELPER_SHA = '4e451f198c2f4e57534a71898d497693b57b0bf94aee1712453f0e1b266f70b3'
RELEASE = ROOT / 'config/replay-parts-retention-v5-release.json'
RUNTIME_COUNT = 141
EXPECTED_PROOF_VERSION = 'replay-parts-retention-v5-ubuntu-tests'
EXPECTED_PROOF_GROUPS = {'tests.test_replay_parts_retention_v5': 33}
CRITICAL_SOURCES = (
    'services/hub/replay_parts_retention_v5.py',
    'services/hub/replay_parts_v5.py',
    'services/hub/replay_part_public_evidence_v5.py',
    'services/hub/replay_delivery_v5.py',
    'services/hub/video_source_retention_v2.py',
    'services/hub/archive_source_retention_v2.py',
    'services/hub/replay_part_publish_worker_v5.py',
    'services/hub/replay_multipart_private_v4.py',
    'services/hub/replay_video_browser.py',
    'services/hub/replay_video_web.py',
    'services/hub/short_video_web.py',
    'services/hub/locks.py',
    'services/hub/db.py',
    'adapters/browser_replay_part_v5.mjs',
    'adapters/replay_part_contract_v5.mjs',
    'adapters/replay_native_driver.mjs',
    'adapters/replay_later_result_lookup_v2.mjs',
    'adapters/bilibili_video_guard.mjs',
    'adapters/short_video_saved_draft_navigation_control.mjs',
    'scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs',
    'vendor/subtitle-cloud-worker/observer/store.py',
)
RUNTIME_MOUNT_ROOT = Path('/usr/local/lib/lighthouse-replay-policy-v5b')
REQUIRED_RUNTIME_MOUNTS = {
    'adapters/replay_native_driver.mjs': RUNTIME_MOUNT_ROOT / 'replay_native_driver.mjs',
    'adapters/replay_part_contract_v5.mjs': RUNTIME_MOUNT_ROOT / 'replay_part_contract_v5.mjs',
    'adapters/ytdlp_highest_v2.py': RUNTIME_MOUNT_ROOT / 'ytdlp_highest_v2.py',
    'adapters/ytdlp_whole_v4.py': RUNTIME_MOUNT_ROOT / 'ytdlp_whole_v4.py',
    'services/hub/config.py': RUNTIME_MOUNT_ROOT / 'config.py',
    'services/hub/media_processing.py': RUNTIME_MOUNT_ROOT / 'media_processing.py',
    'services/hub/replay_multipart_v4.py': RUNTIME_MOUNT_ROOT / 'replay_multipart_v4.py',
    'services/hub/replay_multipart_video_v4.py': RUNTIME_MOUNT_ROOT / 'replay_multipart_video_v4.py',
    'services/hub/replay_parts_retention_v5.py': RUNTIME_MOUNT_ROOT / 'replay_parts_retention_v5.py',
    'services/hub/replay_parts_v5.py': RUNTIME_MOUNT_ROOT / 'replay_parts_v5.py',
    'services/hub/youtube_download.py': RUNTIME_MOUNT_ROOT / 'youtube_download.py',
    'services/hub/youtube_natural_plan_v3.py': RUNTIME_MOUNT_ROOT / 'youtube_natural_plan_v3.py',
    'scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs':
        RUNTIME_MOUNT_ROOT / 'short-video-native-metadata-v5.mjs',
}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def wire(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def run(arguments, *, timeout=120, input=None):
    return subprocess.run(
        arguments,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        input=input,
    )


def inspect(container):
    return json.loads(run(['docker', 'inspect', container]).stdout)[0]


def effective_sources(container, source_files):
    helper = r'''import hashlib,json,sys
from pathlib import Path
files=json.load(sys.stdin)
out={}
for name,expected in files.items():
    path=Path('/opt/lighthouse-hub')/name
    assert path.is_file() and not path.is_symlink()
    actual=hashlib.sha256(path.read_bytes()).hexdigest()
    assert actual==expected,(name,actual,expected)
    out[name]=actual
print(json.dumps(out,sort_keys=True,separators=(',',':')))
'''
    value = run(
        ['docker', 'exec', '-i', container, 'python', '-c', helper],
        timeout=120,
        input=wire(source_files).decode(),
    )
    return json.loads(value.stdout)


def load_helper(file):
    raw = file.read_bytes()
    if sha(raw) != RETENTION_HELPER_SHA:
        raise ValueError('RETENTION_HELPER_CHANGED')
    module = types.ModuleType('retention_install_helper')
    module.__file__ = str(file)
    exec(compile(raw, str(file), 'exec'), module.__dict__)
    return module


def validate(proof, proof_raw, template, template_raw, helper):
    if template['version'] != 'replay-parts-retention-release-v5' or template['enabled'] is not True:
        raise ValueError('RELEASE_SHAPE_CHANGED')
    if template['scope'] != 'all_parts_verified_public_exact_local_media_only':
        raise ValueError('RELEASE_SCOPE_CHANGED')
    if template['target_site_uid'] != helper.UID:
        raise ValueError('RELEASE_TARGET_CHANGED')
    owner = json.loads(helper.read(Path(helper.OWNER if hasattr(helper, 'OWNER') else
        '/var/lib/lighthouse-backup-ops/replay-parts-retention-v5-tests/owner-authorization-1fb26fc9.json'), protected=True))
    if template['authorization_id'] != helper.AUTHORIZATION or template['authorization_sha256'] != helper.owner(owner):
        raise ValueError('RELEASE_AUTHORIZATION_CHANGED')
    if (
        proof['version'] != EXPECTED_PROOF_VERSION
        or proof['tests'] != sum(EXPECTED_PROOF_GROUPS.values())
        or proof['test_groups'] != EXPECTED_PROOF_GROUPS
    ):
        raise ValueError('PROOF_TEST_SHAPE_CHANGED')
    if template['test_attestation'] != {
        'file': PROOF_TARGET + '/test-attestation.json',
        'sha256': sha(proof_raw),
    }:
        raise ValueError('PROOF_NOT_BOUND_TO_RELEASE')
    if any(proof[key] != 0 for key in ('exit_code', 'failures', 'errors', 'skipped')):
        raise ValueError('RETENTION_TEST_RESULT_NOT_CLEAN')
    if proof['network'] != 'none':
        raise ValueError('PROOF_NETWORK_CHANGED')
    for key in (
        'full_suite_passed',
        'no_pid_or_oom_denial',
        'runtime_matches_declared_candidate',
        'mounts_match_isolated_allowlist',
        'mounted_sources_unchanged',
        'production_all_five_unchanged',
    ):
        if proof.get(key) is not True:
            raise ValueError('PROOF_REQUIRED_CHECK_FAILED:' + key)
    for key in (
        'application_data_mounted',
        'production_profile_mounted',
        'secrets_environment_injected',
        'oom_killed',
        'timed_out',
    ):
        if proof.get(key) is not False:
            raise ValueError('PROOF_FORBIDDEN_STATE:' + key)
    sources = template['source_files']
    if sources != proof['source_files'] or len(sources) != RUNTIME_COUNT:
        raise ValueError('RELEASE_SOURCE_MAP_CHANGED')
    if not set(CRITICAL_SOURCES) <= set(sources):
        raise ValueError('RELEASE_CRITICAL_SOURCE_MISSING')
    for name, checksum in sources.items():
        if (
            not re.fullmatch(
                r'(?:services/hub|adapters|scheduler|scripts/ops/native_control_sources|vendor/subtitle-cloud-worker/observer)/[A-Za-z0-9_./-]+\.(?:py|mjs)',
                name,
            )
            or '..' in Path(name).parts
            or not re.fullmatch('[a-f0-9]{64}', checksum)
        ):
            raise ValueError('RELEASE_SOURCE_UNSAFE')
    return template_raw


def locate_proof_mount(compose):
    matches = [
        volume
        for volume in compose['services']['video-worker']['volumes']
        if volume.get('target') == PROOF_TARGET
    ]
    if len(matches) != 1:
        raise ValueError('VIDEO_PROOF_MOUNT_SHAPE_CHANGED')
    return matches[0]


def ensure_runtime_mounts(compose):
    volumes = compose['services']['video-worker']['volumes']
    by_target = {volume['target']: volume for volume in volumes}
    added = []
    for target, source in sorted(REQUIRED_RUNTIME_MOUNTS.items()):
        destination = '/opt/lighthouse-hub/' + target
        expected = {
            'bind': {'create_host_path': False},
            'read_only': True,
            'source': str(source),
            'target': destination,
            'type': 'bind',
        }
        current = by_target.get(destination)
        if current is None:
            volumes.append(expected)
            by_target[destination] = expected
            added.append(destination)
        elif current != expected:
            raise ValueError('RUNTIME_MOUNT_DRIFT:' + destination)
    return added


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proof', type=Path, required=True)
    parser.add_argument('--proof-sha256', required=True)
    parser.add_argument('--release-template', type=Path, required=True)
    parser.add_argument('--release-template-sha256', required=True)
    parser.add_argument('--retention-helper', type=Path, required=True)
    args = parser.parse_args()
    if os.name != 'posix' or os.geteuid() != 0 or not __debug__:
        raise SystemExit('ROOT_POSIX_REQUIRED')
    os.umask(0o077)
    helper = load_helper(args.retention_helper)

    proof_raw = helper.read(args.proof, protected=True)
    template_raw = helper.read(args.release_template, protected=True)
    if sha(proof_raw) != args.proof_sha256:
        raise ValueError('PROOF_SHA_MISMATCH')
    if sha(template_raw) != args.release_template_sha256:
        raise ValueError('TEMPLATE_SHA_MISMATCH')
    proof = json.loads(proof_raw)
    template = json.loads(template_raw)
    validate(proof, proof_raw, template, template_raw, helper)

    if args.proof.parent.parent != helper.BASE:
        raise ValueError('NONCANONICAL_PROOF_PATH')
    if args.release_template != args.proof.parent / 'release-template.json':
        raise ValueError('NONCANONICAL_TEMPLATE_PATH')

    before = inspect(CONTAINER)

    compose_raw = helper.read(COMPOSE, protected=True)
    compose = json.loads(compose_raw)
    runtime_mounts_added = ensure_runtime_mounts(compose)
    mount = locate_proof_mount(compose)
    previous_source = mount['source']
    mount['source'] = str(args.proof.parent)
    mount['read_only'] = True
    if locate_proof_mount(compose)['source'] != str(args.proof.parent):
        raise ValueError('PROOF_MOUNT_UPDATE_FAILED')
    new_compose_raw = (
        json.dumps(compose, ensure_ascii=False, separators=(',', ':')) + '\n'
    ).encode()
    stamp = time.strftime('%Y%m%dT%H%M%S')
    backup = COMPOSE.with_name('compose.yaml.before-retention-v5-' + stamp)
    helper.save(backup, compose_raw)
    helper.save(COMPOSE, new_compose_raw, replace=True)
    if helper.read(COMPOSE, protected=True) != new_compose_raw:
        raise ValueError('COMPOSE_READBACK_FAILED')

    compose_command = [
        'docker',
        'compose',
        '--project-directory',
        str(ROOT),
        '-f',
        str(COMPOSE),
        'up',
        '-d',
        '--no-deps',
        '--force-recreate',
        'video-worker',
    ]
    up = run(compose_command, timeout=180)
    deadline = time.time() + 90
    after = inspect(CONTAINER)
    while not after['State']['Running'] and time.time() < deadline:
        time.sleep(2)
        after = inspect(CONTAINER)
    if after['State']['Running'] is not True:
        raise ValueError('VIDEO_WORKER_RESTART_FAILED')
    if after['Image'] != before['Image'] or after['Config']['Image'] != before['Config']['Image']:
        raise ValueError('VIDEO_IMAGE_CHANGED')
    mounts = [
        item
        for item in after['Mounts']
        if item['Destination'] == PROOF_TARGET
    ]
    if (
        len(mounts) != 1
        or mounts[0]['Source'] != str(args.proof.parent)
        or mounts[0]['RW'] is not False
    ):
        raise ValueError('PROOF_MOUNT_READBACK_FAILED')

    effective = effective_sources(CONTAINER, template['source_files'])
    if effective != template['source_files']:
        raise ValueError('EFFECTIVE_SOURCES_CHANGED_AFTER_RECREATE')
    mounted_proof = run(
        ['docker', 'exec', CONTAINER, 'sha256sum', PROOF_TARGET + '/test-attestation.json']
    ).stdout.split()[0]
    if mounted_proof != sha(proof_raw):
        raise ValueError('CONTAINER_PROOF_SHA_MISMATCH')

    previous_release = helper.read(RELEASE, protected=True) if RELEASE.exists() else None
    helper.save(RELEASE, template_raw, replace=True)
    installed = helper.read(RELEASE, protected=True)
    if installed != template_raw:
        raise ValueError('RELEASE_INSTALL_READBACK_FAILED')

    area = helper.root_directory(
        Path('/var/lib/lighthouse-backup-ops/replay-16gb-10h-retention-install')
        / str(uuid.uuid4())
    )
    if previous_release is not None:
        helper.save(area / 'release-before.json', previous_release)
    helper.save(area / 'release-installed.json', installed)
    helper.save(area / 'proof.json', proof_raw)
    helper.save(area / 'compose-before.json', compose_raw)
    helper.save(area / 'compose-after.json', new_compose_raw)
    result = {
        'version': 'replay-16gb-10h-retention-install-v5',
        'state': 'installed_recreate_verified',
        'container': CONTAINER,
        'image_before': before['Image'],
        'image_after': after['Image'],
        'container_before': before['Id'],
        'container_after': after['Id'],
        'previous_proof_mount': previous_source,
        'proof_mount': str(args.proof.parent),
        'proof_sha256': sha(proof_raw),
        'release_sha256': sha(installed),
        'release_previous_sha256': sha(previous_release) if previous_release is not None else None,
        'release_template_sha256': args.release_template_sha256,
        'effective_source_count': len(effective),
        'effective_sources_match': effective == template['source_files'],
        'runtime_mounts_added': runtime_mounts_added,
        'compose_before_sha256': sha(compose_raw),
        'compose_after_sha256': sha(new_compose_raw),
        'compose_backup': str(backup),
        'compose_up_output': (up.stdout + '\n' + up.stderr).strip(),
        'media_deleted': 0,
        'jobs_modified': 0,
        'provider_requests': 0,
    }
    helper.save(area / 'install-result.json', wire(result) + b'\n')
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
