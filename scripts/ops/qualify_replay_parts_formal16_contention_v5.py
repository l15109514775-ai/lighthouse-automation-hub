"""Run only the 16 owned formal-launch tests in the existing qualified image.

No build, production mount, browser profile, grant or host operator execution.
The two committed files are read-only overlays; all fixtures use /owned.
"""
from pathlib import Path, PurePosixPath
from collections import Counter
import argparse, hashlib, json, os, re, stat, subprocess, tarfile, time, uuid
import xml.etree.ElementTree as ET

ROOT = Path('/opt/lighthouse-hub')
IMAGE = 'sha256:24b0198ba425f527655173c2ef1432445947ebec5fed8660c6bb5527eaa15dde'
COMMIT = 'e8d399c7a52227594fff16964cee2f782833028a'
PARENT = 'e3c646987e7660a232b9e6cddb5e6a449795bb6b05e0204ced50235983684a5f'
RETENTION = Path('/var/lib/lighthouse-backup-ops/replay-parts-retention-v5-tests/529d0c40-7ec6-46ca-b31c-c76ed6a34db4/test-attestation.json')
RETENTION_SHA = 'ab8158d642c05a401f63c482c1994b458d82b8e35451016f015ff779e909b855'
SECCOMP_SHA = '6fbfe3f512f65445b8bbf468034a3bc0fee95dd1734916e0e3dcb1a75b101e3a'
FILES = {
    'scripts/ops/launch_replay_parts_workflow_v5.py': '7cb27cffb8634a946a220344b86723e989519ce1a6b24e41d933149a22fdb003',
    'tests/test_launch_replay_parts_workflow_v5.py': '07b6138efa7660a9b1cd32e0515803aad5d9bd6d4aed3c4f3ffbb506183aec6d',
}
GROUP = 'tests.test_launch_replay_parts_workflow_v5'


def sha(raw): return hashlib.sha256(raw).hexdigest()
def run(args, timeout=60): return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout)
def inspect(cid): return json.loads(run(['docker', 'inspect', cid]).stdout)[0]
def identity(info):
    return {'id': info['Id'], 'image': info['Image'], 'pid': info['State']['Pid'],
            'started_at': info['State']['StartedAt'], 'running': info['State']['Running']}


BOOT = r'''
from pathlib import Path
import hashlib,json,os
root=Path('/opt/lighthouse-hub');m=json.load(open('/run/formal16-manifest.json'))
assert os.getuid()==os.getgid()==10001 and set(os.getgroups())<={10001}
for key in ('HOME','XDG_CONFIG_HOME','XDG_CACHE_HOME'):Path(os.environ[key]).mkdir(parents=True,exist_ok=True)
expected={**m['baseline_source_sha256'],**m['delta_sha256']}
def snapshot():
 return {'sources':{n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in expected},
 'resources':{n:{k:int(v) for k,v in (s.split() for s in (Path('/sys/fs/cgroup')/n).read_text().splitlines())} for n in ('pids.events','memory.events')}}
before=snapshot();assert before['sources']==expected
Path('/owned/before.json').write_text(json.dumps(before))
import pytest
status=pytest.main(['-q','-p','no:cacheprovider','tests/test_launch_replay_parts_workflow_v5.py','--basetemp=/owned/pytest','--junitxml=/owned/results.xml'])
Path('/owned/after.json').write_text(json.dumps(snapshot()));raise SystemExit(status)
'''


def validate(info, owned, overlay, seccomp):
    h = info['HostConfig']; c = info['Config']
    assert info['Image'] == IMAGE and c['User'] == '10001:10001'
    assert h['NetworkMode'] == 'none' and h['ReadonlyRootfs'] is True and h['Init'] is True
    assert h['CapDrop'] == ['ALL'] and h['Memory'] == 1400 * 1024**2
    assert h['NanoCpus'] == 1_000_000_000 and h['PidsLimit'] == 256 and h['ShmSize'] == 256 * 1024**2
    assert len(h['SecurityOpt']) == 2 and 'no-new-privileges:true' in h['SecurityOpt']
    value = next(v.removeprefix('seccomp=') for v in h['SecurityOpt'] if v.startswith('seccomp='))
    assert value == str(ROOT/'config/browser-seccomp.json') or json.loads(value) == seccomp
    expected = {'/owned': (str(owned), True), '/run/formal16-manifest.json': (str(overlay/'manifest.json'), False),
                **{str(ROOT/n): (str(overlay/n), False) for n in FILES}}
    binds = [v for v in info['Mounts'] if v['Type'] == 'bind']
    assert len(binds) == len(expected) and {v['Destination']: (v['Source'], v['RW']) for v in binds} == expected
    assert all(v['Type'] in ('bind', 'tmpfs') for v in info['Mounts'])
    assert h['Tmpfs'] == {'/tmp': 'rw,size=256m,mode=1777', str(ROOT/'media'): 'rw,size=32m,mode=0700,uid=10001,gid=10001'}
    assert not any(re.search(r'(^|_)(KEY|TOKEN|PASSWORD|SECRET|COOKIE)(_|$)', v.split('=', 1)[0]) for v in c['Env'])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive', type=Path, required=True); p.add_argument('--sha256', required=True)
    p.add_argument('--retention-proof', type=Path, default=RETENTION)
    a = p.parse_args()
    assert os.geteuid() == 0 and __debug__ and a.archive.parent == Path('/home/lighthouse-ops')
    assert a.archive.resolve() == a.archive and not a.archive.is_symlink() and sha(a.archive.read_bytes()) == a.sha256
    assert json.loads(run(['docker', 'image', 'inspect', IMAGE]).stdout)[0]['Id'] == IMAGE
    proofraw = a.retention_proof.read_bytes(); assert sha(proofraw) == RETENTION_SHA
    prior = json.loads(proofraw)
    assert prior['image'] == IMAGE and prior['tests'] == 26 and prior['full_suite_passed'] is True
    assert prior['exit_code'] == prior['failures'] == prior['errors'] == prior['skipped'] == 0
    assert len(prior['source_files']) == 141
    videoid = run(['docker', 'compose', '--project-directory', str(ROOT), '-f', str(ROOT/'compose.yaml'), 'ps', '-q', 'video-worker']).stdout.strip()
    assert re.fullmatch('[a-f0-9]{64}', videoid)
    production_before = identity(inspect(videoid)); assert production_before['image'] == IMAGE and production_before['running']
    base = Path('/var/lib/lighthouse-backup-ops/replay-parts-formal16-contention-v5-tests')
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    assert all(v.stat().st_uid == 0 and not v.stat().st_mode & 0o022 and not v.is_symlink() for v in (base, *base.parents))
    area = base/str(uuid.uuid4()); area.mkdir(mode=0o700)
    overlay = area/'overlay'; overlay.mkdir(mode=0o755); overlay.chmod(0o755)
    with tarfile.open(a.archive, 'r:gz') as tar:
        members = tar.getmembers()
        assert len(members) == 3 and {v.name for v in members} == set(FILES)|{'manifest.json'}
        for v in members:
            name = PurePosixPath(v.name)
            assert v.isfile() and not v.issym() and not v.islnk() and not name.is_absolute() and '..' not in name.parts and 0 < v.size < 400_000
            raw = tar.extractfile(v).read(); assert v.name == 'manifest.json' or sha(raw) == FILES[v.name]
            file = overlay/v.name; file.parent.mkdir(parents=True, exist_ok=True); file.write_bytes(raw); file.chmod(0o644)
    manifest = json.loads((overlay/'manifest.json').read_bytes())
    assert manifest['version'] == 'replay-parts-formal16-contention-candidate-v5' and manifest['source_commit'] == COMMIT
    assert manifest['base_image'] == IMAGE and manifest['parent42_sha256'] == PARENT
    assert manifest['delta_sha256'] == FILES and manifest['tests'] == 16 and manifest['test_groups'] == {GROUP: 16}
    baseline = manifest['baseline_source_sha256']; assert len(baseline) == 853
    assert all(baseline[n] == v for n, v in prior['source_files'].items())
    assert all(not PurePosixPath(n).is_absolute() and '..' not in PurePosixPath(n).parts and re.fullmatch('[a-f0-9]{64}', v) for n, v in baseline.items())
    owned = area/'owned'; owned.mkdir(mode=0o700); os.chown(owned, 10001, 10001); owned.chmod(0o700)
    seccompraw = (ROOT/'config/browser-seccomp.json').read_bytes(); assert sha(seccompraw) == SECCOMP_SHA
    seccomp = json.loads(seccompraw); name = 'formal16-'+uuid.uuid4().hex[:12]
    args = ['docker', 'create', '--name', name, '--label', 'org.lighthouse.formal16='+name, '--pull=never', '--init', '--network=none', '--read-only', '--user=10001:10001',
            '--cap-drop=ALL', '--security-opt=no-new-privileges:true', '--security-opt=seccomp='+str(ROOT/'config/browser-seccomp.json'),
            '--memory=1400m', '--cpus=1', '--pids-limit=256', '--shm-size=256m', '--tmpfs=/tmp:rw,size=256m,mode=1777',
            '--tmpfs='+str(ROOT/'media')+':rw,size=32m,mode=0700,uid=10001,gid=10001',
            '--env=HOME=/tmp/home', '--env=XDG_CONFIG_HOME=/tmp/config', '--env=XDG_CACHE_HOME=/tmp/cache', '--env=PYTHONDONTWRITEBYTECODE=1',
            '--mount=type=bind,source='+str(owned)+',target=/owned',
            '--mount=type=bind,source='+str(overlay/'manifest.json')+',target=/run/formal16-manifest.json,readonly']
    for n in FILES: args += ['--mount=type=bind,source='+str(overlay/n)+',target='+str(ROOT/n)+',readonly']
    cid = run([*args, '--entrypoint=python', IMAGE, '-c', BOOT]).stdout.strip()
    validate(inspect(cid), owned, overlay, seccomp); started = time.time(); run(['docker', 'start', cid]); timed_out = False
    try: code = int(run(['docker', 'wait', cid], timeout=240).stdout.strip())
    except subprocess.TimeoutExpired:
        timed_out = True; current = inspect(cid)
        assert current['Config']['Labels']['org.lighthouse.formal16'] == name and current['Image'] == IMAGE
        run(['docker', 'stop', '--time', '10', cid]); code = 124
    logs = run(['docker', 'logs', cid]); (area/'tests.log').write_text(logs.stdout+'\n'+logs.stderr)
    final = inspect(cid); validate(final, owned, overlay, seccomp)
    xml = (owned/'results.xml').read_bytes(); cases = list(ET.fromstring(xml).iter('testcase'))
    groups = dict(Counter(v.attrib['classname'] for v in cases)); counts = {k: sum(v.find(k) is not None for v in cases) for k in ('failure', 'error', 'skipped')}
    before = json.loads((owned/'before.json').read_bytes()); after = json.loads((owned/'after.json').read_bytes())
    resources = all(v['resources']['pids.events'].get('max', -1) == v['resources']['memory.events'].get('oom', -1) == v['resources']['memory.events'].get('oom_kill', -1) == 0 for v in (before, after))
    unchanged = before['sources'] == after['sources'] == {**baseline, **FILES}
    production_after = identity(inspect(videoid)); production_unchanged = production_after == production_before
    passed = code == 0 and len(cases) == 16 and groups == {GROUP: 16} and not any(counts.values()) and resources and unchanged and production_unchanged and not timed_out and not final['State']['OOMKilled']
    (area/'accepted-results.xml').write_bytes(xml)
    proof = {'version': 'replay-parts-formal16-contention-ubuntu-v5', 'passed': passed, 'image': IMAGE, 'source_commit': COMMIT,
             'archive_sha256': a.sha256, 'manifest': manifest, 'tests': len(cases), 'test_groups': groups, **counts, 'exit_code': code,
             'source_before': before, 'source_after': after, 'sources_unchanged': unchanged, 'runtime141_source_sha256': prior['source_files'],
             'retention26_proof_sha256': RETENTION_SHA, 'retention26_not_rerun': True, 'no_pid_or_oom_denial': resources,
             'timed_out': timed_out, 'oom_killed': final['State']['OOMKilled'], 'runtime_matches_allowlist': True,
             'network': 'none', 'rootfs_read_only': True, 'app_uid': 10001, 'production_data_mounted': False,
             'production_profile_mounted': False, 'credentials_injected': False, 'operator_main_called': False,
             'production_video_before': production_before, 'production_video_after': production_after, 'production_video_unchanged': production_unchanged,
             'junit_file': str(area/'accepted-results.xml'), 'junit_sha256': sha(xml), 'seconds': time.time()-started}
    out = area/'test-result.json'; out.write_text(json.dumps(proof, sort_keys=True, indent=2))
    print(json.dumps({'file': str(out), 'sha256': sha(out.read_bytes()), 'passed': passed, 'tests': len(cases), **counts}), flush=True)
    raise SystemExit(0 if passed else 1)


if __name__ == '__main__': main()
