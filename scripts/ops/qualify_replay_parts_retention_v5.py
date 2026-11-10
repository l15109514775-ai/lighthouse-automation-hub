"""Isolate the real 26-test retention suite; install only its root release.

Default is a read-only plan. This never builds an image, deploys code, restarts
a service, opens a browser/account or deletes application media.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET

ROOT = Path('/opt/lighthouse-hub')
BASE = Path('/var/lib/lighthouse-backup-ops/replay-parts-retention-v5-tests')
CONTAINER_PROOF = Path('/run/qualifications/retention-v5')
PARENT_ARCHIVE = '7cce4b26f1f32ef5642a475eebfb80d6e60c735e3902578954b3105c2b26fb87'
# Canonical wire(manifest) hash, not the pretty-printed tar member's raw SHA.
PARENT_MANIFEST = '46998201ff72f5e55b5374ebff551cdfaf38cfc7db64b1e46e9f27ccf639fe87'
PARENT_COMMIT = 'd63b795e78f7bee01d81f4ee36138042ec292068'
HANDOFF_SOURCE = 'adapters/replay_part_owner_handoff_v5.mjs'
HANDOFF_SHA = 'f02b004427ac78bd0a1304aa7283cca241a79672fbde85cc3b27890a49aea570'
CAPTURE_GROUP = 'tests.test_replay_part_handoff_capture_v5'
GUI_GROUP = 'tests.test_replay_part_handoff_gui_v5'
GUI_BASE = 'sha256:eefd58cacf8c545348d6496f0dd8976adf98acd55e97e872ccf51cab49128cd1'
VERSION = 'replay-parts-retention-v5-ubuntu-tests'
GROUP = 'tests.test_replay_parts_retention_v5'
SELECTION = 'tests/test_replay_parts_retention_v5.py'
AUTHORIZATION = '1fb26fc9-0324-4f13-9c30-64a767fbb012'
UID = '3747574504754572'
RELEASE = 'replay-parts-retention-v5-release.json'
NODE_SOURCES = (
    'adapters/bilibili_video_guard.mjs', 'adapters/browser_replay_part_v5.mjs',
    'adapters/replay_highest_contract_v2.mjs', 'adapters/replay_later_result_lookup_v2.mjs',
    'adapters/replay_native_driver.mjs', 'adapters/replay_natural_contract_v3.mjs',
    'adapters/replay_part_contract_v5.mjs', 'adapters/replay_part_owner_handoff_v5.mjs',
    'adapters/short_video_saved_draft_navigation_control.mjs',
    'scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs',
    'vendor/subtitle-cloud-worker/observer/store.py')


def require(condition, code):
    if not condition:
        raise ValueError(code)


def wire(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read(file, maximum=2_000_000, *, protected=False):
    file = Path(file)
    require(file.is_absolute() and file.resolve() == file and not any(p.is_symlink() for p in (file, *file.parents)), 'UNSAFE_PATH')
    if protected:
        for p in (file, *file.parents):
            info = p.stat()
            require(info.st_uid == 0 and not info.st_mode & 0o022, 'ROOT_PROTECTION_REQUIRED')
    fd = os.open(file, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and 0 < before.st_size <= maximum, 'FILE_BOUND')
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno()); current = file.stat()
    keys = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    require(len(raw) == before.st_size and all(getattr(before, k) == getattr(after, k) == getattr(current, k) for k in keys), 'FILE_CHANGED')
    return raw


def junit(raw, count, *, group=None):
    require(len(raw) < 2_000_000 and b'<!DOCTYPE' not in raw.upper() and b'<!ENTITY' not in raw.upper(), 'JUNIT_BOUND')
    cases = list(ET.fromstring(raw).iter('testcase'))
    groups = {}
    for case in cases:
        name = case.get('classname'); groups[name] = groups.get(name, 0) + 1
    require(len(cases) == count and all(c.find(k) is None for c in cases for k in ('failure', 'error', 'skipped')), 'ACTUAL_COMPLETE_JUNIT_REQUIRED')
    if group is not None:
        require(groups == {group: count}, 'WRONG_TEST_GROUP')
    return groups


def source_files(manifest):
    entries = manifest['files']
    selected = {n for n in entries if n.startswith(('services/hub/', 'adapters/')) and n.endswith('.py')} | set(NODE_SOURCES)
    require(len(selected) == 141 and selected <= set(entries), 'EXACT_SOURCE_SCOPE_REQUIRED')
    require(entries[HANDOFF_SOURCE]['sha256'] == HANDOFF_SHA, 'QUALIFIED_HANDOFF_SOURCE_REQUIRED')
    for name in selected:
        p = PurePosixPath(name)
        require(str(p) == name and '..' not in p.parts and not p.is_absolute() and re.fullmatch('[a-f0-9]{64}', entries[name]['sha256']), 'BAD_SOURCE')
    return {n: entries[n]['sha256'] for n in sorted(selected)}


def validate_parent(value, xml):
    require(value['version'] == 'tracked-source-candidate-ubuntu-result-v1' and value['archive_sha256'] == PARENT_ARCHIVE and value['source_commit'] == PARENT_COMMIT, 'WRONG_276_PARENT')
    manifest = value['source_manifest']
    require(sha(wire(manifest)) == PARENT_MANIFEST and manifest['source_commit'] == PARENT_COMMIT and SELECTION in manifest['test_selections'], 'PARENT_MANIFEST_CHANGED')
    require(value['tests'] == 276 and value['exit_code'] == value['failure'] == value['error'] == value['skipped'] == 0 and value['passed'] is True, '276_NOT_PASSED')
    require(value['network'] == 'none' and value['derived_gui_runtime_base_image'] == GUI_BASE, 'PARENT_ISOLATION_REQUIRED')
    require(all(value[k] is False for k in ('production_data_mounted', 'secrets_injected', 'timed_out', 'oom_killed')), 'PARENT_PRODUCTION_OR_FAILURE')
    require(all(value[k] is True for k in ('no_pid_or_oom_denial', 'mounted_sources_unchanged', 'runtime_matches_declared_candidate', 'empty_media_tmpfs')), 'PARENT_RUNTIME_REQUIRED')
    require(re.fullmatch('sha256:[a-f0-9]{64}', value['image']) and sha(xml) == value['junit_sha256'], 'PARENT_IMAGE_OR_JUNIT_CHANGED')
    groups = junit(xml, 276)
    require(groups.get(GROUP) == 26, 'PARENT_RETENTION_GROUP_REQUIRED')
    require(groups.get(CAPTURE_GROUP) == 3 and groups.get(GUI_GROUP) == 1, 'PARENT_CAPTURE_AND_GUI_GROUPS_REQUIRED')
    source_files(manifest)
    return value


def owner(value):
    require(value['version'] == 'video-source-retention-v2' and value['authorization_id'] == AUTHORIZATION and value['scope'] == 'confirmed_full_duration_public_video' and value['target_site_uid'] == UID, 'EXISTING_OWNER_AUTHORIZATION_REQUIRED')
    require(value['artifact'] is None and all(value[k] is False for k in ('formal_workflow_launch_claimed', 'publication_authorized', 'remote_video_deletion_authorized')), 'OWNER_SCOPE_CHANGED')
    require(value['supersedes_legacy_local_test_hold'] is True and isinstance(value['owner_response_reference'], str) and value['owner_response_reference'].strip(), 'OWNER_REFERENCE_REQUIRED')
    require(isinstance(value['actual_owner_response'], str) and all(s in value['actual_owner_response'] for s in ('录播', 'B站', '保留')), 'OWNER_RESPONSE_REQUIRED')
    return sha(wire(value))


def run(arguments, timeout=30):
    return subprocess.run(arguments, check=True, capture_output=True, text=True, timeout=timeout)


def inspect_image(image):
    info = json.loads(run(['docker', 'image', 'inspect', image]).stdout)[0]
    require(info['Id'] == image, 'IMAGE_CHANGED')
    return info


def validate_isolation(info, *, image, owned, seccomp, seccomp_profile):
    host = info['HostConfig']; config = info['Config']; mounts = info['Mounts']
    require(info['Image'] == image and config['User'] == '10001:10001', 'WRONG_IMAGE_OR_UID')
    require(host['NetworkMode'] == 'none' and host['ReadonlyRootfs'] is True and host['Init'] is True, 'NETWORK_OR_ROOTFS_CHANGED')
    options = host['SecurityOpt']
    require(host['CapDrop'] == ['ALL'] and len(options) == 2 and options.count('no-new-privileges:true') == 1, 'SECURITY_OPTIONS_CHANGED')
    profiles = [s.removeprefix('seccomp=') for s in options if s.startswith('seccomp=')]
    require(len(profiles) == 1, 'BROWSER_SECCOMP_REQUIRED')
    # Docker CLI normally expands the protected profile into its JSON value.
    require(profiles[0] == str(seccomp) or json.loads(profiles[0]) == seccomp_profile, 'BROWSER_SECCOMP_CHANGED')
    require(host['Memory'] == 1400 * 1024**2 and host['NanoCpus'] == 1_000_000_000 and host['PidsLimit'] == 256 and host['ShmSize'] == 256 * 1024**2, 'RESOURCE_LIMITS_CHANGED')
    binds = [m for m in mounts if m['Type'] == 'bind']
    require(len(binds) == 1 and binds[0]['Source'] == str(owned) and binds[0]['Destination'] == '/owned' and binds[0]['RW'] is True, 'UNEXPECTED_BIND')
    require(all(m['Type'] in ('bind', 'tmpfs') for m in mounts), 'UNEXPECTED_MOUNT')
    require(host['Tmpfs'] == {'/tmp': 'rw,size=256m,mode=1777', str(ROOT/'media'): 'rw,size=32m,mode=0700,uid=10001,gid=10001'}, 'TMPFS_CHANGED')
    names = [s.split('=', 1)[0] for s in config['Env']]
    require(len(names) == len(set(names)) and not any(re.search(r'(^|_)(KEY|TOKEN|PASSWORD|SECRET|COOKIE)(_|$)', n) for n in names), 'SECRET_ENVIRONMENT_REJECTED')
    return True


BOOTSTRAP = r'''import hashlib,json,os,sys
from pathlib import Path
for key in ('HOME','XDG_CONFIG_HOME','XDG_CACHE_HOME'):Path(os.environ[key]).mkdir(parents=True,exist_ok=True)
root=Path('/opt/lighthouse-hub');manifest=json.loads((root/'candidate-manifest.json').read_bytes())
def sources():
 return {n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n,v in manifest['files'].items() if (root/n).is_file() and not (root/n).is_symlink() and (root/n).stat().st_size==v['bytes']}
def counters():
 return {n:{k:int(v) for k,v in (x.split() for x in (Path('/sys/fs/cgroup')/n).read_text().splitlines())} for n in ('pids.events','memory.events')}
expected={n:v['sha256'] for n,v in manifest['files'].items()}
before=sources();assert before==expected
Path('/owned/source-before.json').write_text(json.dumps(before,sort_keys=True));Path('/owned/resource-before.json').write_text(json.dumps(counters()))
import pytest
status=pytest.main(['-q','-p','no:cacheprovider','tests/test_replay_parts_retention_v5.py','--basetemp=/owned/pytest','--junitxml=/owned/results.xml'])
Path('/owned/source-after.json').write_text(json.dumps(sources(),sort_keys=True));Path('/owned/resource-after.json').write_text(json.dumps(counters()))
raise SystemExit(status)
'''


def root_directory(path, mode=0o750):
    path = Path(path)
    if not path.exists():
        root_directory(path.parent, 0o755)
        os.mkdir(path, mode); os.chown(path, 0, 10001); os.chmod(path, mode)
    require(path.resolve() == path and not path.is_symlink() and path.is_dir(), 'UNSAFE_DIRECTORY')
    for p in (path, *path.parents):
        st = p.stat(); require(st.st_uid == 0 and not st.st_mode & 0o022, 'ROOT_DIRECTORY_REQUIRED')
    return path


def save(file, raw, *, replace=False):
    file = Path(file); root_directory(file.parent)
    temporary = file.parent / ('.' + file.name + '.' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o640)
    with os.fdopen(fd, 'wb') as stream:
        os.fchown(stream.fileno(), 0, 10001); os.fchmod(stream.fileno(), 0o640)
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    if replace:
        if file.exists():read(file, protected=True)
        os.replace(temporary, file)
    else:
        os.link(temporary, file); temporary.unlink()
    dfd = os.open(file.parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0))
    try:os.fsync(dfd)
    finally:os.close(dfd)


def release_template(authorization, proof, proof_raw):
    return {'version': 'replay-parts-retention-release-v5', 'enabled': True, 'target_site_uid': UID,
        'scope': 'all_parts_verified_public_exact_local_media_only', 'authorization_id': AUTHORIZATION,
        'authorization_sha256': owner(authorization), 'source_files': proof['source_files'],
        'test_attestation': {'file': str(CONTAINER_PROOF/'test-attestation.json'), 'sha256': sha(proof_raw)}}


def qualify(parent, parent_raw, parent_xml, authorization, seccomp):
    seccomp_raw = read(seccomp, protected=True)
    require(sha(seccomp_raw) == parent['source_manifest']['files']['config/browser-seccomp.json']['sha256'], 'QUALIFIED_SECCCOMP_REQUIRED')
    profile = json.loads(seccomp_raw)
    image = parent['image']; inspect_image(image)
    folder = root_directory(BASE/str(uuid.uuid4())); owned = folder/'owned'
    os.mkdir(owned, 0o700); os.chown(owned, 10001, 10001); os.chmod(owned, 0o700)
    save(folder/'parent-qualified-run.json', parent_raw); save(folder/'parent-qualified-results.xml', parent_xml)
    command = ['docker', 'create', '--pull=never', '--init', '--name', 'retention-v5-' + folder.name,
        '--network=none', '--read-only', '--user', '10001:10001', '--cap-drop=ALL',
        '--security-opt=no-new-privileges:true', '--security-opt=seccomp=' + str(seccomp),
        '--memory=1400m', '--cpus=1', '--pids-limit=256', '--shm-size=256m',
        '--tmpfs=/tmp:rw,size=256m,mode=1777', '--tmpfs=' + str(ROOT/'media') + ':rw,size=32m,mode=0700,uid=10001,gid=10001',
        '--env=HOME=/tmp/home', '--env=XDG_CONFIG_HOME=/tmp/config', '--env=XDG_CACHE_HOME=/tmp/cache', '--env=PYTHONDONTWRITEBYTECODE=1',
        '--mount=type=bind,source=' + str(owned) + ',target=/owned', image, 'python', '-c', BOOTSTRAP]
    cid = run(command).stdout.strip(); info = json.loads(run(['docker', 'inspect', cid]).stdout)[0]
    validate_isolation(info, image=image, owned=owned, seccomp=seccomp, seccomp_profile=profile)
    started = time.time(); run(['docker', 'start', cid]); timed_out = False
    try:status = int(run(['docker', 'wait', cid], timeout=900).stdout.strip())
    except subprocess.TimeoutExpired:
        timed_out = True; run(['docker', 'stop', '-t', '15', cid]); status = 124
    logs = run(['docker', 'logs', cid]); save(folder/'tests.log', (logs.stdout + '\n' + logs.stderr).encode())
    final = json.loads(run(['docker', 'inspect', cid]).stdout)[0]
    validate_isolation(final, image=image, owned=owned, seccomp=seccomp, seccomp_profile=profile)
    xml = read(owned/'results.xml'); groups = junit(xml, 26, group=GROUP)
    expected = {n: v['sha256'] for n, v in parent['source_manifest']['files'].items()}
    before = json.loads(read(owned/'source-before.json')); after = json.loads(read(owned/'source-after.json'))
    rb = json.loads(read(owned/'resource-before.json')); ra = json.loads(read(owned/'resource-after.json'))
    for name in ('source-before.json', 'source-after.json', 'resource-before.json', 'resource-after.json'):
        save(folder/name, read(owned/name))
    no_denial = all(c['pids.events'].get('max', -1) == c['memory.events'].get('oom', -1) == c['memory.events'].get('oom_kill', -1) == 0 for c in (rb, ra))
    unchanged = before == after == expected
    proof = {'version': VERSION, 'tests': 26, 'test_groups': groups, 'exit_code': status, 'failures': 0, 'errors': 0, 'skipped': 0,
        'network': 'none', 'application_data_mounted': False, 'production_profile_mounted': False, 'secrets_environment_injected': False,
        'oom_killed': final['State']['OOMKilled'], 'timed_out': timed_out, 'full_suite_passed': status == 0 and not timed_out and not final['State']['OOMKilled'],
        'no_pid_or_oom_denial': no_denial, 'runtime_matches_declared_candidate': unchanged,
        'mounts_match_isolated_allowlist': True, 'mounted_sources_unchanged': unchanged,
        'source_files': source_files(parent['source_manifest']), 'image': image, 'container_id': cid,
        'qualified_parent_sha256': sha(parent_raw), 'source_archive_sha256': PARENT_ARCHIVE, 'source_commit': PARENT_COMMIT,
        'junit_file': str(CONTAINER_PROOF/'results.xml'), 'junit_host_file': str(folder/'results.xml'), 'junit_sha256': sha(xml),
        'resource_before': rb, 'resource_after': ra, 'duration_seconds': time.time() - started,
        'host_file': str(folder/'test-attestation.json'), 'source_manifest_sha256': PARENT_MANIFEST,
        'qualification_helper_sha256': sha(read(Path(__file__).resolve()))}
    save(folder/'results.xml', xml)
    raw = wire(proof) + b'\n'; save(folder/'test-attestation.json', raw)
    require(proof['full_suite_passed'] and no_denial and unchanged, 'RETENTION_26_FAILED_CLOSED')
    save(folder/'release-template.json', wire(release_template(authorization, proof, raw)) + b'\n')
    return {'state': 'qualified_not_installed', 'test_attestation': proof['host_file'], 'sha256': sha(raw),
        'release_template': str(folder/'release-template.json'), 'readonly_mount': str(folder) + ':' + str(CONTAINER_PROOF) + ':ro', 'tests': 26}


def validate_qualification(value, xml, parent, *, parent_sha256):
    require(value['version'] == VERSION and value['tests'] == 26 and value['test_groups'] == {GROUP: 26}, 'WRONG_RETENTION_QUALIFICATION')
    require(value['image'] == parent['image'] and value['source_files'] == source_files(parent['source_manifest']) and value['source_manifest_sha256'] == PARENT_MANIFEST, 'QUALIFICATION_SOURCE_CHANGED')
    require(value['qualified_parent_sha256'] == parent_sha256 and value['source_archive_sha256'] == PARENT_ARCHIVE and value['source_commit'] == PARENT_COMMIT, 'QUALIFICATION_PARENT_CHANGED')
    require(value['junit_file'] == str(CONTAINER_PROOF/'results.xml') and sha(xml) == value['junit_sha256'], 'QUALIFICATION_JUNIT_CHANGED')
    junit(xml, 26, group=GROUP)
    require(value['network'] == 'none' and value['exit_code'] == value['failures'] == value['errors'] == value['skipped'] == 0, 'QUALIFICATION_FAILED')
    require(all(value[k] is True for k in ('full_suite_passed', 'no_pid_or_oom_denial', 'runtime_matches_declared_candidate', 'mounts_match_isolated_allowlist', 'mounted_sources_unchanged')), 'QUALIFICATION_RUNTIME_REQUIRED')
    require(all(value[k] is False for k in ('application_data_mounted', 'production_profile_mounted', 'secrets_environment_injected', 'oom_killed', 'timed_out')), 'QUALIFICATION_PRODUCTION_OR_FAILURE')
    return value


def apply_release(parent, proof, raw, authorization, publisher):
    require(re.fullmatch('[a-f0-9]{64}', publisher or ''), 'EXACT_LIVE_PUBLISHER_REQUIRED')
    info = json.loads(run(['docker', 'inspect', publisher]).stdout)[0]
    require(info['Id'] == publisher and info['State']['Running'] is True and info['Image'] == parent['image'] and info['Config']['User'] == '10001:10001' and info['HostConfig']['ReadonlyRootfs'] is True, 'PUBLISHER_RUNTIME_CHANGED')
    command = "import hashlib,json,sys;from pathlib import Path;p=json.load(sys.stdin);r=Path('/opt/lighthouse-hub');assert all((r/n).is_file() and not (r/n).is_symlink() and hashlib.sha256((r/n).read_bytes()).hexdigest()==s for n,s in p.items());print('source_sha256_verified')"
    checked = subprocess.run(['docker', 'exec', '--user', '10001:10001', '-i', publisher, 'python', '-c', command], input=wire(proof['source_files']).decode(), capture_output=True, text=True, timeout=60, check=True)
    require(checked.stdout.strip() == 'source_sha256_verified', 'LIVE_PUBLISHER_SOURCES_CHANGED')
    mounts = info['Mounts']
    require(any(m['Type'] == 'bind' and m['Source'] == str(Path(proof['host_file']).parent) and m['Destination'] == str(CONTAINER_PROOF) and m['RW'] is False for m in mounts), 'PUBLISHER_READONLY_PROOF_MOUNT_REQUIRED')
    template = release_template(authorization, proof, raw); target = ROOT/'config'/RELEASE
    if target.exists():
        previous = read(target, protected=True)
        if json.loads(previous) == template:
            return {'state': 'release_already_installed', 'release_file': str(target), 'sha256': sha(previous), 'services_restarted': 0, 'source_files_deployed': 0, 'media_deleted': 0}
        save(Path(proof['host_file']).parent/('release-before-' + str(uuid.uuid4()) + '.json'), previous)
    save(target, wire(template) + b'\n', replace=True)
    require(json.loads(read(target, protected=True)) == template, 'RELEASE_READBACK_FAILED')
    return {'state': 'release_installed', 'release_file': str(target), 'sha256': sha(read(target, protected=True)), 'publisher_container': publisher, 'services_restarted': 0, 'source_files_deployed': 0, 'media_deleted': 0}


def main():
    p = argparse.ArgumentParser(); p.add_argument('--qualified-run', type=Path, required=True); p.add_argument('--qualified-run-sha256', required=True)
    p.add_argument('--authorization-json', type=Path, required=True); p.add_argument('--qualification', type=Path); p.add_argument('--qualification-sha256')
    p.add_argument('--publisher-container'); p.add_argument('--seccomp', type=Path, default=ROOT/'config/browser-seccomp.json')
    mode = p.add_mutually_exclusive_group(); mode.add_argument('--qualify', action='store_true'); mode.add_argument('--apply', action='store_true')
    a = p.parse_args(); require(os.name == 'posix' and os.geteuid() == 0, 'ROOT_HOST_REQUIRED'); os.umask(0o077)
    raw = read(a.qualified_run); require(sha(raw) == a.qualified_run_sha256, 'QUALIFIED_RUN_SHA_REQUIRED'); parent = json.loads(raw)
    parent_xml = read(Path(parent['junit_file'])); validate_parent(parent, parent_xml)
    authorization = json.loads(read(a.authorization_json, protected=True)); owner_sha = owner(authorization)
    inspect_image(parent['image'])
    if a.qualify:
        result = qualify(parent, raw, parent_xml, authorization, a.seccomp)
    elif a.qualification:
        praw = read(a.qualification, protected=True); require(sha(praw) == a.qualification_sha256, 'QUALIFICATION_SHA_REQUIRED'); proof = json.loads(praw)
        require(Path(proof['host_file']) == a.qualification and a.qualification.parent.parent == BASE and str(uuid.UUID(a.qualification.parent.name)) == a.qualification.parent.name, 'CANONICAL_PROOF_REQUIRED')
        xml = read(a.qualification.parent/'results.xml', protected=True); validate_qualification(proof, xml, parent, parent_sha256=sha(raw))
        if a.apply:result = apply_release(parent, proof, praw, authorization, a.publisher_container)
        else:result = {'state': 'release_plan_only', 'release_template': release_template(authorization, proof, praw), 'services_restarted': 0, 'media_deleted': 0}
    else:
        require(not a.apply, 'QUALIFICATION_REQUIRED_BEFORE_APPLY')
        result = {'state': 'qualification_plan_only', 'image': parent['image'], 'tests_to_run': 26, 'source_files': len(source_files(parent['source_manifest'])), 'authorization_id': AUTHORIZATION, 'authorization_sha256': owner_sha, 'services_restarted': 0, 'media_deleted': 0}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
