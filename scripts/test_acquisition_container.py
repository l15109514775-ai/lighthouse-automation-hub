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
import tempfile
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', required=True)
    parser.add_argument('--compose', type=Path, required=True)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='lighthouse-acquisition-', dir='/var/tmp'))
    os.chown(root, 10001, 10001)
    project = root.name
    (root / '.env').write_text('')
    shutil.copyfile(args.compose, root / 'compose.yaml')
    (root / 'test.yaml').write_text('services:\n  replay-acquisition-worker:\n    network_mode: none\n')
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
root=Path('/fixture');raw=Path('config/hub.example.toml').read_text()
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
        '--entrypoint', 'python', args.image, '-c', seed], check=True)
    report = {'synthetic': True, 'real_youtube_download': False, 'published': False,
              'root': str(root), 'image': args.image, 'checks': {}}
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
        subprocess.run(['docker', 'kill', '--signal', 'KILL', ident], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(['docker', 'start', ident], check=True, stdout=subprocess.DEVNULL)
        wait_healthy(ident, allow_restart=True)
        import sqlite3
        with sqlite3.connect('file:' + str(root / 'data/hub.sqlite3') + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            prior = json.loads((root / 'before.json').read_text())
            current = dict(db.execute('SELECT * FROM jobs WHERE job_id=?', (prior['job_id'],)).fetchone())
            for key in ('payload', 'result'):current[key] = json.loads(current[key])
            assert current == prior
            assert db.execute('pragma integrity_check').fetchone()[0] == 'ok'
        report['checks']['sigkill_restart_and_foreign_job_preserved'] = True
        report['passed'] = True
    finally:
        call('stop', 'replay-acquisition-worker')
        (root / 'report.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report))


if __name__ == '__main__':
    main()
