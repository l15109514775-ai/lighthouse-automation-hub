"""Run on the Linux Docker host. Isolated, synthetic, network-disabled acceptance.

No production mounts, cookies or keys; no downloads or publication requests.
Requires an already-built test image containing tests and the locked runtime.
Artifacts are retained beneath a fresh /var/tmp/lighthouse-acquisition-* folder.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


def check_concurrent_workers(image, root, overlay_mounts=()):
    """Two real containers, one SQLite DB and one physical video lock."""
    setup = '''
import sys,time,tomllib
from pathlib import Path
from services.hub.config import Settings
from services.hub.db import Store
from services.hub.worker import Worker
root=Path('/fixture');settings=Settings(root,tomllib.loads((root/'config/hub.toml').read_text()))
store=Store(settings.db_path)
'''
    left = setup + '''
ident=store.enqueue('youtube','synthetic-left','synthetic_left','video',{},'synthetic')
def handler(job):
 (root/'lock-held').write_text('held')
 until=time.monotonic()+45
 while not (root/'release-lock').exists() and time.monotonic()<until:time.sleep(.05)
 assert (root/'release-lock').exists()
 return {'synthetic':True}
worker=Worker(settings,store,{'synthetic_left':handler});worked=False;until=time.monotonic()+10
while not worked and time.monotonic()<until:
 worked=worker.once('video')
 if not worked:time.sleep(.1)
assert worked
assert store.get(ident)['state']=='completed'
'''
    right = setup + '''
ident=store.enqueue('youtube','synthetic-right','synthetic_right','video',{},'synthetic')
worker=Worker(settings,store,{'synthetic_right':lambda job:{'synthetic':True}})
worked=worker.once('video')
if sys.argv[1]!='blocked':
 until=time.monotonic()+10
 while not worked and time.monotonic()<until:
  time.sleep(.1);worked=worker.once('video')
row=store.get(ident)
if sys.argv[1]=='blocked':assert not worked and row['state']=='pending' and row['attempts']==0
else:assert worked and row['state']=='completed' and row['attempts']==1
'''
    base = ['docker','run','--rm','--network','none','--read-only','--user','10001:10001',
        '--cap-drop','ALL','--security-opt','no-new-privileges','--memory','192m',
        '--cpus','0.25','--tmpfs','/tmp:size=32m','-v',str(root)+':/fixture',
        *overlay_mounts, '--entrypoint','python',image,'-c']
    process = subprocess.Popen([*base,left])
    try:
        until = time.monotonic() + 30
        while not (root/'lock-held').exists() and process.poll() is None and time.monotonic()<until:
            time.sleep(.1)
        assert (root/'lock-held').exists()
        subprocess.run([*base,right,'blocked'],check=True,timeout=30)
        (root/'release-lock').write_text('release')
        assert process.wait(timeout=30) == 0
        subprocess.run([*base,right,'complete'],check=True,timeout=30)
    finally:
        (root/'release-lock').write_text('release')
        if process.poll() is None:process.wait(timeout=50)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', required=True)
    parser.add_argument('--compose', type=Path, required=True)
    parser.add_argument('--overlay-manifest', type=Path)
    parser.add_argument('--overlay-archive', type=Path)
    args = parser.parse_args()
    if bool(args.overlay_manifest) != bool(args.overlay_archive):
        parser.error('Supply both overlay manifest and archive, or neither')
    overlay_blobs = None
    if args.overlay_manifest:
        if __package__:
            from .test_acquisition_overlay_bundle import read_bundle, safe_path
        else:
            from test_acquisition_overlay_bundle import read_bundle, safe_path
        manifest = json.loads(safe_path(args.overlay_manifest).read_text())
        overlay_blobs = read_bundle(manifest, safe_path(args.overlay_archive))
        if args.image != manifest['base_image']:
            raise ValueError('ACQUISITION_TEST_OVERLAY_BASE_MISMATCH')
    root = Path(tempfile.mkdtemp(prefix='lighthouse-acquisition-', dir='/var/tmp'))
    os.chown(root, 10001, 10001)
    project = root.name
    (root / '.env').write_text('')
    shutil.copyfile(args.compose, root / 'compose.yaml')
    # The production image need not ship example configuration. Seed only from
    # the public sample beside the explicitly supplied source Compose file.
    shutil.copyfile(args.compose.resolve().parent / 'config/hub.example.toml', root / 'test-example.toml')
    (root / 'test-example.toml').chmod(0o444)
    override = {'network_mode': 'none'}
    overlay_mounts = []
    if overlay_blobs:
        # Test the current executable modules against synthetic fixture policy.
        # Real source-control settings are qualified by the separate restore test;
        # do not import owner identities into this generic lock/recovery fixture.
        override['volumes'] = []
        for name in sorted(n for n in overlay_blobs if n.endswith('.py')):
            path = root / 'runtime-overlays' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(overlay_blobs[name]); path.chmod(0o444)
            target = '/opt/lighthouse-hub/' + name
            override['volumes'].append({'type': 'bind', 'source': str(path),
                                         'target': target, 'read_only': True})
            overlay_mounts += ['--mount', 'type=bind,src=' + str(path) + ',dst=' + target + ',readonly']
    (root / 'test.yaml').write_text(json.dumps({'services': {'replay-acquisition-worker': override}}))
    env = {**os.environ, 'LIGHTHOUSE_ACQUISITION_IMAGE': args.image}
    compose = ['docker', 'compose', '-p', project, '-f', str(root / 'compose.yaml'),
               '-f', str(root / 'test.yaml'), '--profile', 'youtube-acquisition']
    seed = '''
import sys,os,json,tomllib
from pathlib import Path
sys.path.insert(0,'/opt/lighthouse-hub/tests')
from services.hub.config import Settings
from services.hub.db import Store
from services.hub.youtube_download import CHANNELS
from test_replay_fifo_v4 import fifo
from test_youtube_download_v4 import configure_v4
root=Path('/fixture');raw=(root/'test-example.toml').read_text()
cfg=tomllib.loads(raw);s=Settings(root,cfg);s.prepare();store=Store(s.db_path);store.init()
f=configure_v4(fifo.__wrapped__((s,store)))
raw=raw.replace('youtube_whole_replay_download_enabled = false','youtube_whole_replay_download_enabled = true')
raw=raw.replace('target_bilibili_site_uid = "1000000000000000"','target_bilibili_site_uid = '+json.dumps(s.config['target_bilibili_site_uid']))
for source,channel in zip(cfg['sources'],sorted(CHANNELS)):
 raw=raw.replace(source['youtube_channel_id'],channel)
(root/'config/hub.toml').write_text(raw)
from services.hub.replay_multipart_worker_runtime_v4 import build_worker
build_worker(Settings(root,tomllib.loads(raw)),store,mode='acquisition_only')
(root/'data/browser-profiles/youtube').mkdir(parents=True)
(root/'secrets').mkdir();key=root/'secrets/youtube-api-key'
key.write_text('synthetic-test-only-key-00000000');key.chmod(0o400)
ident=store.enqueue('youtube','synthetic-legacy','unsupported_legacy','video',{},'synthetic')
store.claim('video',3600,4,job_types=('unsupported_legacy',))
(root/'before.json').write_text(json.dumps(store.get(ident)))
'''
    subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--read-only',
        '--user', '10001:10001', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
        '--memory', '256m', '--tmpfs', '/tmp:size=32m', '-v', str(root) + ':/fixture',
        *overlay_mounts, '--entrypoint', 'python', args.image, '-c', seed], check=True)
    report = {'synthetic': True, 'real_youtube_download': False, 'published': False,
              'root': str(root), 'image': args.image, 'checks': {},
              'executable_overlays_verified': bool(overlay_blobs),
              'source_control_policy': 'synthetic_fixture_no_owner_pause',
              'overlay_archive_sha256': manifest['overlay_archive_sha256'] if overlay_blobs else None}
    def call(*parts):
        return subprocess.check_output([*compose, *parts], env=env, text=True)
    def wait_healthy(ident, *, allow_restart=False):
        until = time.monotonic() + 110
        while time.monotonic() < until:
            info = json.loads(subprocess.check_output(['docker', 'inspect', ident]))[0]
            if info['State']['Status'] == 'exited' or info['RestartCount'] and not allow_restart:
                raise RuntimeError('ACQUISITION_CONTAINER_EXITED')
            if info['State'].get('Health', {}).get('Status') == 'healthy':
                return info
            time.sleep(2)
        raise RuntimeError('ACQUISITION_HEALTH_TIMEOUT')
    try:
        config = json.loads(call('config', '--format', 'json'))
        service = config['services']['replay-acquisition-worker']
        assert service['network_mode'] == 'none' and not service.get('ports')
        assert not service.get('env_file')
        report['checks']['compose_isolation'] = True
        call('up', '-d', '--no-build', '--no-deps', 'replay-acquisition-worker')
        ident = call('ps', '-q', 'replay-acquisition-worker').strip()
        info = wait_healthy(ident)
        host = info['HostConfig']
        assert info['Config']['User'] == '10001:10001'
        assert host['ReadonlyRootfs'] and 'ALL' in host['CapDrop']
        assert host['Memory'] == 1400 * 1024**2 and host['NanoCpus'] == 1500000000
        assert host['PidsLimit'] == 160 and host['LogConfig']['Type'] == 'local'
        assert any(x.startswith('no-new-privileges') for x in host['SecurityOpt'])
        mounts = {m['Destination']: m for m in info['Mounts']}
        assert not mounts['/run/secrets/youtube-api-key']['RW']
        assert not mounts['/opt/lighthouse-hub/data/browser-profiles/youtube']['RW']
        report['checks']['runtime_hardening_and_limits'] = True
        probe = '''
import os,json,sqlite3
from pathlib import Path
from services.hub.db import Store
assert os.getuid()==10001
assert len(Path('/run/secrets/youtube-api-key').read_text())>20
try:Path('/opt/lighthouse-hub/forbidden-write').write_text('x')
except OSError:pass
else:raise AssertionError('root writable')
store=Store(Path('/opt/lighthouse-hub/data/hub.sqlite3'))
assert store.query('pragma journal_mode')[0]['journal_mode']=='wal'
assert not store.query('select * from publications')
assert not store.query('select * from api_calls')
print('permissions-secret-wal-no-external-work: PASS')
'''
        subprocess.run(['docker', 'exec', ident, 'python', '-c', probe], check=True)
        report['checks']['secret_permissions_wal'] = True
        check_concurrent_workers(args.image, root, overlay_mounts)
        report['checks']['two_containers_shared_database_and_video_lock'] = True
        epoch_cmd = ['docker', 'exec', ident, 'python', '-c',
            "from pathlib import Path;print(Path('/tmp/replay-acquisition-epoch.json').read_text())"]
        boot_before = json.loads(subprocess.check_output(epoch_cmd))['instance']
        subprocess.run(['docker', 'kill', '--signal', 'KILL', ident], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(['docker', 'start', ident], check=True, stdout=subprocess.DEVNULL)
        wait_healthy(ident, allow_restart=True)
        assert json.loads(subprocess.check_output(epoch_cmd))['instance'] != boot_before
        report['checks']['boot_identity_changed_on_restart'] = True
        import sqlite3
        with sqlite3.connect('file:' + str(root / 'data/hub.sqlite3') + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            prior = json.loads((root / 'before.json').read_text())
            current = dict(db.execute('SELECT * FROM jobs WHERE job_id=?', (prior['job_id'],)).fetchone())
            for key in ('payload', 'result'):current[key] = json.loads(current[key])
            assert current == prior
            assert db.execute('pragma integrity_check').fetchone()[0] == 'ok'
        report['checks']['sigkill_restart_and_foreign_job_preserved'] = True
        rollback = Path(__file__).with_name('rollback_acquisition.py')
        rollback_args = [sys.executable, str(rollback), '--container', ident,
            '--expected-image-id', info['Image'], '--expected-data-root', str(root / 'data')]
        subprocess.run(rollback_args, check=True)
        assert json.loads(subprocess.check_output(['docker','inspect',ident]))[0]['State']['Running']
        subprocess.run([*rollback_args, '--apply'], check=True)
        assert not json.loads(subprocess.check_output(['docker','inspect',ident]))[0]['State']['Running']
        with sqlite3.connect('file:' + str(root / 'data/hub.sqlite3') + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            current = dict(db.execute('SELECT * FROM jobs WHERE job_id=?', (prior['job_id'],)).fetchone())
            for key in ('payload', 'result'):current[key] = json.loads(current[key])
            assert current == prior
        report['checks']['rollback_stops_only_acquisition_and_preserves_job'] = True
        report['passed'] = True
    finally:
        call('stop', 'replay-acquisition-worker')
        (root / 'report.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report))


if __name__ == '__main__':
    main()
