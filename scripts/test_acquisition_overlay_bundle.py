"""Restore an exact, allowlisted acquisition overlay bundle into an offline test.

Never extracts into the application, mounts live data, or changes a running service.
Requires Linux root, an already loaded immutable image, and synthetic test fixtures.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile

FILES = {'services/hub/worker.py', 'services/hub/source_controls.py',
         'config/source-controls.json'}
SOURCE_BINDINGS = {
    'services/hub/replay_fifo_v4.py', 'services/hub/replay_multipart_v4.py',
    'services/hub/replay_multipart_private_v4.py', 'services/hub/youtube_download.py',
    'services/hub/budget.py', 'adapters/http.py', 'adapters/safe_https.py',
}
SOURCE_BINDING_VERSIONS = {
    'acquisition-source-bindings-v1': SOURCE_BINDINGS,
    'acquisition-source-bindings-v2': SOURCE_BINDINGS | {'services/hub/worker.py', 'services/hub/source_controls.py'},
}
SOURCE_BINDING_VERSIONS['acquisition-source-bindings-v3'] = SOURCE_BINDING_VERSIONS['acquisition-source-bindings-v2'] | {
    'adapters/replay_ranges.py', 'adapters/ytdlp_highest_v2.py',
}


def safe_path(path):
    if not path.is_absolute() or path.resolve() != path or any(
            p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('OVERLAY_UNSAFE_PATH')
    return path


def read_bundle(manifest, archive):
    allowed = {'acquisition-effective-release-v1': FILES, **SOURCE_BINDING_VERSIONS}.get(manifest['version'])
    file_info = archive.lstat()
    if (allowed is None or not stat.S_ISREG(file_info.st_mode) or
            not re.fullmatch(r'sha256:[0-9a-f]{64}', manifest['base_image']) or
            set(manifest['files']) != allowed or file_info.st_size > 1_000_000):
        raise ValueError('OVERLAY_MANIFEST_INVALID')
    with archive.open('rb') as stream:
        raw = stream.read(1_000_001)
    if (len(raw) > 1_000_000 or len(raw) != manifest['overlay_archive_bytes'] or
            hashlib.sha256(raw).hexdigest() != manifest['overlay_archive_sha256']):
        raise ValueError('OVERLAY_ARCHIVE_CHANGED')
    result = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as tar:
        entries = tar.getmembers()
        if len(entries) != len(allowed) or {x.name for x in entries} != allowed:
            raise ValueError('OVERLAY_MEMBER_SET_INVALID')
        for entry in entries:
            expected = manifest['files'][entry.name]
            if not entry.isfile() or entry.size != expected['bytes'] or entry.size > 100_000:
                raise ValueError('OVERLAY_MEMBER_INVALID')
            data = tar.extractfile(entry).read()
            if hashlib.sha256(data).hexdigest() != expected['sha256']:
                raise ValueError('OVERLAY_MEMBER_CHANGED')
            result[entry.name] = data
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('manifest', 'archive', 'output', 'test-fixtures'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    if os.name != 'posix' or os.geteuid() != 0:
        raise SystemExit('OVERLAY_TEST_REQUIRES_LINUX_ROOT')
    for value in vars(args).values(): safe_path(value)
    if args.output.exists(): raise SystemExit('OVERLAY_TEST_OUTPUT_EXISTS')
    manifest = json.loads(args.manifest.read_text())
    if manifest['version'] != 'acquisition-effective-release-v1':
        parser.error('Use test_acquisition_container.py for a source-binding release')
    blobs = read_bundle(manifest, args.archive)
    image = manifest['base_image']
    inspect = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image]))[0]
    if inspect['Id'] != image: raise SystemExit('OVERLAY_BASE_IMAGE_CHANGED')
    fixture_names = ('tests/conftest.py', 'tests/test_source_controls.py', 'config/hub.toml')
    fixtures = {name: safe_path(args.test_fixtures / name).read_bytes() for name in fixture_names}
    os.umask(0o077); args.output.mkdir(mode=0o700)
    extracted = args.output / 'restored'; extracted.mkdir(mode=0o755)
    for name, raw in {**blobs, **{'qualification/' + k: v for k, v in fixtures.items()}}.items():
        path = extracted / name
        path.parent.mkdir(parents=True, exist_ok=True)
        # Containers can traverse their bind-mounted fixture directory as UID10001.
        for parent in (path.parent, *path.parent.parents):
            if parent == extracted or parent.is_relative_to(extracted): parent.chmod(0o755)
        path.write_bytes(raw); path.chmod(0o644)
    base = ['docker', 'run', '--rm', '--pull=never', '--network', 'none', '--read-only',
            '--user', '10001:10001', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
            '--memory', '256m', '--cpus', '1', '--pids-limit', '64',
            '--tmpfs', '/tmp:rw,size=64m,mode=1777', '-e', 'PYTHONDONTWRITEBYTECODE=1']
    for name in sorted(FILES):
        base += ['--mount', 'type=bind,src=' + str(extracted / name) +
                 ',dst=/opt/lighthouse-hub/' + name + ',readonly']
    snapshot = lambda: subprocess.check_output(['docker', 'ps', '--format', '{{.ID}} {{.Names}}'], text=True).splitlines()
    before = snapshot()
    def run_test(arguments, label, timeout):
        cidfile = args.output / (label + '.cid')
        try:
            return subprocess.run(base + ['--cidfile', str(cidfile),
                '--label', 'lighthouse.offline-test=acquisition-overlay'] + arguments,
                capture_output=True, text=True, timeout=timeout)
        finally:
            # A timed-out Docker client does not imply its container stopped.
            if cidfile.exists():
                cid = cidfile.read_text().strip()
                if not re.fullmatch(r'[0-9a-f]{64}', cid):
                    raise RuntimeError('OVERLAY_TEST_CONTAINER_ID_INVALID')
                state = subprocess.run(['docker', 'inspect', cid], capture_output=True,
                                       text=True, timeout=15)
                if state.returncode == 0:
                    item = json.loads(state.stdout)[0]
                    assert item['Id'] == cid and item['Image'] == image
                    assert item['Config']['Labels']['lighthouse.offline-test'] == 'acquisition-overlay'
                    subprocess.run(['docker', 'rm', '--force', cid], check=True,
                                   stdout=subprocess.DEVNULL, timeout=30)
    probe = """import json,hashlib,pathlib
from services.hub.source_controls import paused_sources
root=pathlib.Path('/opt/lighthouse-hub')
names=['services/hub/worker.py','services/hub/source_controls.py','config/source-controls.json']
print(json.dumps({'hashes':{n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in names},'paused':paused_sources(root)}))
"""
    result = run_test(['--entrypoint', 'python', image, '-c', probe], 'probe', 45)
    (args.output / 'probe.log').write_text(result.stdout + result.stderr)
    assert result.returncode == 0, 'OVERLAY_RUNTIME_PROBE_FAILED'
    observed = json.loads(result.stdout)
    assert observed['hashes'] == {k: v['sha256'] for k, v in manifest['files'].items()}
    expected = json.loads(blobs['config/source-controls.json'])
    assert observed['paused'] == {'x': [x.lower() for x in expected['paused_x_handles']],
                                  'youtube': expected['paused_youtube_channel_ids']}
    tests = run_test(['--mount', 'type=bind,src=' + str(extracted / 'qualification') +
        ',dst=/qualification,readonly', '-e', 'PYTHONPATH=/opt/lighthouse-hub', '-w', '/qualification',
        '--entrypoint', 'python', image, '-m', 'pytest', '-p', 'no:cacheprovider',
        '--basetemp=/tmp/tests', '-q', 'tests/test_source_controls.py'], 'tests', 60)
    (args.output / 'tests.log').write_text(tests.stdout + tests.stderr)
    assert tests.returncode == 0, 'OVERLAY_RESTORED_TESTS_FAILED'
    assert sorted(before) == sorted(snapshot()), 'OVERLAY_PRODUCTION_CHANGED'
    report = {'state': 'restored_overlay_bundle_offline_tests_passed', 'image': image,
              'archive_sha256': manifest['overlay_archive_sha256'], 'files_verified': len(blobs),
              'fixture_sha256': {k: hashlib.sha256(v).hexdigest() for k, v in fixtures.items()},
              'paused': observed['paused'], 'production_container_ids_unchanged': True,
              'live_data_mounts': False, 'external_requests': 0}
    (args.output / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == '__main__': main()
