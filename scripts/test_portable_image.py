"""Qualify a captured image in a fresh, network-isolated Docker image store.

Linux/root operator tool. Never restarts the production daemon or mounts live
application data. Each invocation requires new private work/runtime directories.
See Docker's documented networkless bootstrap daemon configuration:
https://docs.docker.com/reference/cli/dockerd/#run-multiple-daemons
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


PROBE = r'''
import hashlib,importlib.metadata,json,pathlib,shutil,subprocess,sys
root=pathlib.Path('/opt/lighthouse-hub')
files=['services/hub/worker.py','services/hub/db.py','services/hub/replay_fifo_v4.py',
 'services/hub/replay_multipart_worker_runtime_v4.py','services/hub/youtube_download_v4.py',
 'services/hub/youtube_readiness_v4.py','services/hub/acquisition_health.py',
 'adapters/ytdlp_replay.py','adapters/ytdlp_highest_v2.py','adapters/ytdlp_whole_v4.py']
def sha(path):
 h=hashlib.sha256()
 with path.open('rb') as f:
  while chunk:=f.read(1024*1024):h.update(chunk)
 return h.hexdigest()
versions={}
for command in ['node','ffmpeg','ffprobe']:
 argument='--version' if command=='node' else '-version'
 p=subprocess.run([command,argument],capture_output=True,text=True,timeout=15,check=True)
 path=pathlib.Path(shutil.which(command)).resolve()
 versions[command]={'version_line':p.stdout.splitlines()[0],'binary_sha256':sha(path)}
python_packages=sorted([{'name':d.metadata['Name'],'version':d.version}
 for d in importlib.metadata.distributions()],key=lambda d:(d['name'].lower(),d['version']))
packages=subprocess.check_output(['dpkg-query','-W','-f=${binary:Package}\t${Version}\t${Architecture}\n'],text=True)
debian_packages=[dict(zip(['name','version','architecture'],line.split('\t')))
 for line in packages.splitlines()]
print(json.dumps({'python':sys.version.split()[0], 'python_packages':python_packages,
 'debian_packages':debian_packages,'tools':versions,
 'runtime_source_sha256':{name:sha(root/name) for name in files},
 'uid':__import__('os').getuid(),'network_interfaces':sorted(p.name for p in pathlib.Path('/sys/class/net').iterdir())}))
'''


def safe_path(path):
    if not path.is_absolute() or path.resolve() != path or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('PORTABLE_TEST_UNSAFE_PATH')
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--runtime-dir', type=Path, required=True)
    parser.add_argument('--acquisition-overlay', type=Path,
                        help='Directory containing manifest.json, overlays.tar and synthetic fixtures/')
    args = parser.parse_args()
    if os.name != 'posix' or os.geteuid() != 0:
        raise SystemExit('PORTABLE_TEST_REQUIRES_LINUX_ROOT')
    for path in (args.manifest, args.work_dir, args.runtime_dir): safe_path(path)
    if args.work_dir.exists() or args.runtime_dir.exists():
        raise SystemExit('PORTABLE_TEST_EXISTING_WORKSPACE_INSPECT_SAME_RUN')
    if not args.runtime_dir.is_relative_to(Path('/run')) or len(str(args.runtime_dir / 'docker.sock')) >= 90:
        raise SystemExit('PORTABLE_TEST_RUNTIME_PATH_INVALID')
    if shutil.disk_usage(args.work_dir.parent).free < 25 * 1024**3:
        raise SystemExit('PORTABLE_TEST_DISK_FLOOR')
    manifest = json.loads(args.manifest.read_text())
    image = manifest['image']; archive = safe_path(Path(manifest['archive']))
    if not archive.is_relative_to(args.manifest.parent) or manifest['state'] != 'archive_verified':
        raise SystemExit('PORTABLE_TEST_ARCHIVE_BINDING_INVALID')
    digest = hashlib.sha256()
    with archive.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024**2), b''): digest.update(chunk)
    if digest.hexdigest() != manifest['archive_sha256'] or archive.stat().st_size != manifest['archive_bytes']:
        raise SystemExit('PORTABLE_TEST_ARCHIVE_CHANGED')
    if args.acquisition_overlay:
        bundle = safe_path(args.acquisition_overlay)
        release_path = safe_path(bundle / 'manifest.json')
        release = json.loads(release_path.read_text())
        if release['base_image'] != image or release['base_archive_sha256'] != manifest['archive_sha256']:
            raise SystemExit('OVERLAY_BASE_ARCHIVE_CHANGED')
    os.umask(0o077)
    args.work_dir.mkdir(mode=0o700); args.runtime_dir.mkdir(mode=0o700)
    work, runtime = args.work_dir, args.runtime_dir
    config = {'hosts': ['unix://' + str(runtime / 'docker.sock')], 'group': 'root',
              'containerd': str(runtime / 'containerd.sock'),
              'data-root': str(work / 'data'), 'exec-root': str(runtime / 'exec'),
              'pidfile': str(runtime / 'docker.pid'), 'bridge': 'none',
              'iptables': False, 'ip6tables': False, 'ip-forward': False,
              'ip-masq': False, 'userland-proxy': False,
              'containerd-namespace': 'lighthouse-image-test',
              'containerd-plugins-namespace': 'lighthouse-image-test-plugins',
              'features': {'containerd-snapshotter': True}, 'shutdown-timeout': 15,
              'log-driver': 'local', 'log-opts': {'max-size': '5m', 'max-file': '2'}}
    (work / 'daemon.json').write_text(json.dumps(config))
    # Docker can auto-connect to the host containerd even with another data-root.
    # Give both daemons private state AND the same isolated mount namespace.
    containerd_config = '\n'.join([
        'version = 4', 'root = ' + json.dumps(str(work / 'containerd-root')),
        'state = ' + json.dumps(str(runtime / 'containerd-state')),
        'disabled_plugins = ["io.containerd.cri.v1.images", "io.containerd.cri.v1.runtime", "io.containerd.grpc.v1.cri"]',
        '[plugins."io.containerd.server.v1.grpc"]',
        'address = ' + json.dumps(str(runtime / 'containerd.sock')),
        '[plugins."io.containerd.server.v1.ttrpc"]',
        'address = ' + json.dumps(str(runtime / 'containerd.ttrpc')), ''])
    (work / 'containerd.toml').write_text(containerd_config)
    env = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
           'HOME': str(work), 'DOCKER_CONFIG': str(work / 'client'), 'LANG': 'C.UTF-8'}
    client = ['docker', '--host', config['hosts'][0]]
    def production():
        return subprocess.check_output(['docker', '--host', 'unix:///var/run/docker.sock',
                                        'ps', '--format', '{{.ID}} {{.Names}}'], env=env, text=True).splitlines()
    before = production()
    report = {'state': 'starting', 'image': image, 'archive_sha256': manifest['archive_sha256'],
              'started_at': time.time(), 'work_dir': str(work), 'runtime_dir': str(runtime)}
    def save(): (work / 'report.json').write_text(json.dumps(report, indent=2))
    with (work / 'daemon.private.log').open('w') as log, (work / 'containerd.private.log').open('w') as cl:
        cdaemon = subprocess.Popen(['unshare', '--mount', '--net', '--propagation', 'private',
            'containerd', '--config', str(work / 'containerd.toml')],
            stdin=subprocess.DEVNULL, stdout=cl, stderr=cl, env=env, umask=0o022)
        daemon = None
        report['containerd_pid'] = cdaemon.pid; save()
        try:
            deadline = time.monotonic() + 40
            while True:
                if cdaemon.poll() is not None: raise RuntimeError('PORTABLE_TEST_CONTAINERD_EXITED')
                result = subprocess.run(['ctr', '--address', config['containerd'], 'version'],
                                        env=env, capture_output=True, text=True, timeout=5)
                if result.returncode == 0: break
                if time.monotonic() >= deadline: raise RuntimeError('PORTABLE_TEST_CONTAINERD_NOT_READY')
                time.sleep(.5)
            assert not subprocess.check_output(['ctr', '--address', config['containerd'],
                'content', 'list', '--quiet'], env=env, text=True).strip()
            report['fresh_containerd'] = {'address': config['containerd'], 'content_blobs': 0,
                                        'root': str(work / 'containerd-root')}
            daemon = subprocess.Popen(['nsenter', '--target', str(cdaemon.pid), '--mount', '--net',
                                       'dockerd', '--config-file', str(work / 'daemon.json')],
                                      stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, umask=0o022)
            report['daemon_pid'] = daemon.pid; save()
            deadline = time.monotonic() + 40
            while True:
                if daemon.poll() is not None: raise RuntimeError('PORTABLE_TEST_DAEMON_EXITED')
                result = subprocess.run(client + ['info', '--format', '{{json .}}'], env=env,
                                        capture_output=True, text=True, timeout=5)
                if result.returncode == 0: break
                if time.monotonic() >= deadline: raise RuntimeError('PORTABLE_TEST_DAEMON_NOT_READY')
                time.sleep(.5)
            info = json.loads(result.stdout)
            assert info['Images'] == info['Containers'] == 0 and info['DockerRootDir'] == str(work / 'data')
            report['fresh_store'] = {'id': info['ID'], 'images': info['Images'],
                                     'containers': info['Containers'], 'root': info['DockerRootDir']}
            report['separate_network_namespace'] = os.readlink('/proc/self/ns/net') != os.readlink(f'/proc/{daemon.pid}/ns/net')
            report['separate_mount_namespace'] = os.readlink('/proc/self/ns/mnt') != os.readlink(f'/proc/{daemon.pid}/ns/mnt')
            assert report['separate_network_namespace'] and report['separate_mount_namespace']
            for kind in ('mnt', 'net'):
                assert os.readlink(f'/proc/{daemon.pid}/ns/{kind}') == os.readlink(f'/proc/{cdaemon.pid}/ns/{kind}')
            report['state'] = 'loading_verified_archive'; save()
            with (work / 'load.private.log').open('w') as load_log:
                subprocess.run(client + ['image', 'load', '--input', str(archive)], env=env,
                               stdout=load_log, stderr=load_log, timeout=600, check=True)
            loaded = json.loads(subprocess.check_output(client + ['image', 'inspect', image], env=env))[0]
            assert loaded['Id'] == image and loaded['RootFS']['Layers'] == [x['diff_id'] for x in manifest['layers']]
            probe = subprocess.run(client + ['run', '--rm', '--pull=never', '--network', 'none',
                '--read-only', '--user', '10001:10001', '--cap-drop', 'ALL', '--security-opt',
                'no-new-privileges:true', '--memory', '512m', '--cpus', '1', '--pids-limit', '64',
                '--tmpfs', '/tmp:rw,size=64m,mode=1777', '--entrypoint', 'python', image, '-c', PROBE],
                env=env, capture_output=True, text=True, timeout=60)
            (work / 'probe.private.log').write_text(probe.stdout + probe.stderr)
            assert probe.returncode == 0, 'PORTABLE_TEST_CONTAINER_FAILED'
            inventory = json.loads(probe.stdout)
            assert inventory['uid'] == 10001 and inventory['network_interfaces'] == ['lo']
            assert inventory['debian_packages'] and all(
                set(row) == {'name', 'version', 'architecture'} and all(row.values())
                for row in inventory['debian_packages'])
            assert all(len(x) == 64 for x in inventory['runtime_source_sha256'].values())
            (work / 'runtime-inventory.json').write_text(json.dumps(inventory, indent=2))
            if args.acquisition_overlay:
                overlay = subprocess.run([
                    sys.executable, str(Path(__file__).with_name('test_acquisition_overlay_bundle.py')),
                    '--manifest', str(release_path), '--archive', str(bundle / 'overlays.tar'),
                    '--test-fixtures', str(bundle / 'fixtures'), '--output', str(work / 'overlay-check')
                ], env={**env, 'DOCKER_HOST': config['hosts'][0]}, capture_output=True, text=True,
                    timeout=180)
                (work / 'overlay.private.log').write_text(overlay.stdout + overlay.stderr)
                overlay.check_returncode()
                report['overlay'] = json.loads((work / 'overlay-check/report.json').read_text())
            assert not subprocess.check_output(client + ['ps', '-aq'], env=env, text=True).strip()
            report.update(state='clean_import_and_offline_container_passed',
                          loaded_image=loaded['Id'], layers_verified=len(loaded['RootFS']['Layers']),
                          network_interfaces=inventory['network_interfaces'], probe_uid=inventory['uid'],
                          python_packages=len(inventory['python_packages']), debian_packages=len(inventory['debian_packages']),
                          provider_requests=0, live_mounts=False)
        except BaseException as exc:
            report.update(state='failed', error_class=type(exc).__name__)
            raise
        finally:
            try:
                if daemon is not None:
                    if daemon.poll() is None: daemon.terminate()
                    daemon.wait(timeout=40)
                report['daemon_stopped'] = True
            except subprocess.TimeoutExpired:
                report['daemon_stopped'] = False
                raise RuntimeError('PORTABLE_TEST_DAEMON_STOP_UNCONFIRMED') from None
            finally:
                if cdaemon.poll() is None: cdaemon.terminate()
                try:
                    cdaemon.wait(timeout=40); report['containerd_stopped'] = True
                except subprocess.TimeoutExpired:
                    report['containerd_stopped'] = False
                    raise RuntimeError('PORTABLE_TEST_CONTAINERD_STOP_UNCONFIRMED') from None
                finally:
                    report['production_container_ids_unchanged'] = sorted(before) == sorted(production())
                    report['finished_at'] = time.time(); save()
    assert report['production_container_ids_unchanged']
    print(json.dumps(report))


if __name__ == '__main__': main()
