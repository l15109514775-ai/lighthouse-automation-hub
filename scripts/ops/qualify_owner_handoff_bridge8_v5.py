"""Run only the eight offline owner-bridge tests in the existing qualified image.

No build, production mount, browser profile, grant, bridge/tunnel main or live relay.
Optional --install copies only the qualified fixed bridge and records its root metadata.
The three committed bridge/tunnel/test files are read-only overlays; all fixtures use /owned.
"""
from pathlib import Path, PurePosixPath
from collections import Counter
import argparse, hashlib, json, os, re, stat, subprocess, tarfile, time, uuid
import xml.etree.ElementTree as ET

ROOT = Path('/opt/lighthouse-hub')
IMAGE = 'sha256:06ed6b8e6adff8fd2a43e5345c8d1d1d1f62c463fd79d362d2df4dfdac1705e4'
COMMIT = 'bf33dbc53889ee725fb6be7356fb12f7824e5c03'
PARENT = 'b98b7b3b5bbe8325444ec63fabf7c0ea782e7fb2a83d902da8601c08836290e8'
FORMAL = Path('/var/lib/lighthouse-backup-ops/replay-parts-formal16-v5-tests/dd8beff0-6b34-402e-987a-1bc7a026f556/test-result.json')
SECCOMP_SHA = '6fbfe3f512f65445b8bbf468034a3bc0fee95dd1734916e0e3dcb1a75b101e3a'
FILES = {
    'scripts/ops/owner_handoff_bridge_v5.py': 'afb80deb5f4651802d955ad2ad85685e467dc499f287707c5af82faf4b732ae8',
    'scripts/ops/owner_handoff_tunnel_v5.py': '3be63e0fed74f471367ab66cf69e6a13a139fd6c2202616da61d5c884de0f24c',
    'tests/test_owner_handoff_bridge_v5.py': '0fd385a994c102e77d2446b018fcbe53960a204d51b7c7d43551423adad50108',
}
GROUP = 'tests.test_owner_handoff_bridge_v5'
INSTALL = Path('/usr/local/lib/lighthouse-owner-handoff-v5/owner_handoff_bridge_v5.py')



def sha(raw): return hashlib.sha256(raw).hexdigest()
def run(args, timeout=60): return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout)
def inspect(cid): return json.loads(run(['docker', 'inspect', cid]).stdout)[0]
def identity(info):
    return {'id': info['Id'], 'image': info['Image'], 'pid': info['State']['Pid'],
            'started_at': info['State']['StartedAt'], 'running': info['State']['Running']}


def peers():
    out = {}
    for service in ('api','scheduler','tweet-worker','system-worker'):
        cid = run(['docker','compose','--project-directory',str(ROOT),'-f',str(ROOT/'compose.yaml'),'ps','-q',service]).stdout.strip()
        assert re.fullmatch('[a-f0-9]{64}',cid)
        out[service] = identity(inspect(cid))
        assert out[service]['running']
    return out


def root_path(path):
    assert path.is_absolute() and path.resolve() == path and not any(p.is_symlink() for p in (path,*path.parents))
    for item in (path,*path.parents):
        if item.exists():
            s=item.lstat();assert s.st_uid == 0 and not s.st_mode & 0o022


def install_bridge(overlay,proof,proof_sha):
    assert proof['passed'] is True and proof['tests'] == 8 and proof['source_commit'] == COMMIT
    source=overlay/'scripts/ops/owner_handoff_bridge_v5.py';raw=source.read_bytes();expected=FILES['scripts/ops/owner_handoff_bridge_v5.py']
    assert sha(raw)==expected
    root_path(INSTALL)
    created_directory=False
    if not INSTALL.parent.exists():
        INSTALL.parent.mkdir(mode=0o750);os.chown(INSTALL.parent,0,0);INSTALL.parent.chmod(0o750);created_directory=True
        fd=os.open(INSTALL.parent.parent,os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(fd)
        finally:os.close(fd)
    d=INSTALL.parent.lstat();assert stat.S_ISDIR(d.st_mode) and d.st_uid==d.st_gid==0 and stat.S_IMODE(d.st_mode)==0o750
    existed=INSTALL.exists()
    if existed:
        s=INSTALL.lstat();assert stat.S_ISREG(s.st_mode) and s.st_uid==s.st_gid==0 and s.st_nlink==1 and stat.S_IMODE(s.st_mode)==0o750
        assert INSTALL.read_bytes()==raw  # Never overwrite a different existing tool.
    else:
        temporary=INSTALL.with_name('.owner_handoff_bridge_v5.'+uuid.uuid4().hex)
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o750)
        try:
            with os.fdopen(fd,'wb') as stream:
                os.fchown(stream.fileno(),0,0);os.fchmod(stream.fileno(),0o750);stream.write(raw);stream.flush();os.fsync(stream.fileno())
            os.link(temporary,INSTALL,follow_symlinks=False)
        finally:
            if temporary.exists():temporary.unlink()
        directory=os.open(INSTALL.parent,os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(directory)
        finally:os.close(directory)
    root_path(INSTALL);s=INSTALL.lstat();assert stat.S_ISREG(s.st_mode) and s.st_nlink==1 and s.st_uid==s.st_gid==0 and stat.S_IMODE(s.st_mode)==0o750 and INSTALL.read_bytes()==raw
    return {'version':'owner-handoff-bridge-installation-v5','state':'installed_not_started','file':str(INSTALL),'sha256':expected,
        'uid':s.st_uid,'gid':s.st_gid,'mode':oct(stat.S_IMODE(s.st_mode)),'nlink':s.st_nlink,
        'directory':{'file':str(INSTALL.parent),'uid':d.st_uid,'gid':d.st_gid,'mode':oct(stat.S_IMODE(d.st_mode))},
        'already_same_bytes':existed,'created_directory':created_directory,'qualification_sha256':proof_sha,
        'image':IMAGE,'tests':8,'bridge_main_called':False,'browser_started':False,'service_started':False,
        'tunnel_started':False,'ports_opened':False,'credentials_read':False,'application_database_written':False}


BOOT = r'''
from pathlib import Path
import hashlib,json,os
root=Path('/opt/lighthouse-hub');m=json.load(open('/run/bridge8-manifest.json'))
assert os.getuid()==os.getgid()==10001 and set(os.getgroups())<={10001}
for key in ('HOME','XDG_CONFIG_HOME','XDG_CACHE_HOME'):Path(os.environ[key]).mkdir(parents=True,exist_ok=True)
expected={**m['baseline_source_sha256'],**m['delta_sha256']}
def snapshot():
 return {'sources':{n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in expected},
 'resources':{n:{k:int(v) for k,v in (s.split() for s in (Path('/sys/fs/cgroup')/n).read_text().splitlines())} for n in ('pids.events','memory.events')}}
before=snapshot();assert before['sources']==expected
Path('/owned/before.json').write_text(json.dumps(before))
import pytest
status=pytest.main(['-q','-p','no:cacheprovider','tests/test_owner_handoff_bridge_v5.py','--basetemp=/owned/pytest','--junitxml=/owned/results.xml'])
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
    expected = {'/owned': (str(owned), True), '/run/bridge8-manifest.json': (str(overlay/'manifest.json'), False),
                **{str(ROOT/n): (str(overlay/n), False) for n in FILES}}
    binds = [v for v in info['Mounts'] if v['Type'] == 'bind']
    assert len(binds) == len(expected) and {v['Destination']: (v['Source'], v['RW']) for v in binds} == expected
    assert all(v['Type'] in ('bind', 'tmpfs') for v in info['Mounts'])
    assert h['Tmpfs'] == {'/tmp': 'rw,size=256m,mode=1777', str(ROOT/'media'): 'rw,size=32m,mode=0700,uid=10001,gid=10001'}
    assert not any(re.search(r'(^|_)(KEY|TOKEN|PASSWORD|SECRET|COOKIE)(_|$)', v.split('=', 1)[0]) for v in c['Env'])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive', type=Path, required=True); p.add_argument('--sha256', required=True)
    p.add_argument('--formal-proof', type=Path, default=FORMAL); p.add_argument('--install', action='store_true')
    a = p.parse_args()
    assert os.geteuid() == 0 and __debug__ and a.archive.parent == Path('/home/lighthouse-ops')
    assert a.archive.resolve() == a.archive and not a.archive.is_symlink() and sha(a.archive.read_bytes()) == a.sha256
    assert json.loads(run(['docker', 'image', 'inspect', IMAGE]).stdout)[0]['Id'] == IMAGE
    proofraw = a.formal_proof.read_bytes(); assert sha(proofraw) == PARENT
    prior = json.loads(proofraw)
    assert prior['image'] == IMAGE and prior['tests'] == 16 and prior['passed'] is True
    assert prior['exit_code'] == prior['failure'] == prior['error'] == prior['skipped'] == 0
    assert prior['source_commit'] == '29a3190e6ab76a92e1f3acf34fdb9cedec3c2b21'
    assert prior['no_pid_or_oom_denial'] is prior['sources_unchanged'] is prior['production_video_unchanged'] is True
    assert len(prior['runtime141_source_sha256']) == 141
    peer_before = peers()
    videoid = run(['docker', 'compose', '--project-directory', str(ROOT), '-f', str(ROOT/'compose.yaml'), 'ps', '-q', 'video-worker']).stdout.strip()
    assert re.fullmatch('[a-f0-9]{64}', videoid)
    production_before = identity(inspect(videoid)); assert production_before['image'] == IMAGE and production_before['running']
    base = Path('/var/lib/lighthouse-backup-ops/owner-handoff-bridge8-v5-tests')
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    assert all(v.stat().st_uid == 0 and not v.stat().st_mode & 0o022 and not v.is_symlink() for v in (base, *base.parents))
    area = base/str(uuid.uuid4()); area.mkdir(mode=0o700)
    overlay = area/'overlay'; overlay.mkdir(mode=0o755); overlay.chmod(0o755)
    with tarfile.open(a.archive, 'r:gz') as tar:
        members = tar.getmembers()
        assert len(members) == 4 and {v.name for v in members} == set(FILES)|{'manifest.json'}
        for v in members:
            name = PurePosixPath(v.name)
            assert v.isfile() and not v.issym() and not v.islnk() and not name.is_absolute() and '..' not in name.parts and 0 < v.size < 400_000
            raw = tar.extractfile(v).read(); assert v.name == 'manifest.json' or sha(raw) == FILES[v.name]
            file = overlay/v.name; file.parent.mkdir(parents=True, exist_ok=True); file.write_bytes(raw); file.chmod(0o644)
    manifest = json.loads((overlay/'manifest.json').read_bytes())
    assert manifest['version'] == 'owner-handoff-bridge8-candidate-v5' and manifest['source_commit'] == COMMIT
    assert manifest['base_image'] == IMAGE and manifest['formal16_sha256'] == PARENT
    assert manifest['delta_sha256'] == FILES and manifest['tests'] == 8 and manifest['test_groups'] == {GROUP: 8}
    baseline = manifest['baseline_source_sha256']; assert len(baseline) == 852
    assert baseline == prior['manifest']['baseline_source_sha256']
    assert all(baseline[n] == v for n, v in prior['runtime141_source_sha256'].items())
    assert all(not PurePosixPath(n).is_absolute() and '..' not in PurePosixPath(n).parts and re.fullmatch('[a-f0-9]{64}', v) for n, v in baseline.items())
    owned = area/'owned'; owned.mkdir(mode=0o700); os.chown(owned, 10001, 10001); owned.chmod(0o700)
    seccompraw = (ROOT/'config/browser-seccomp.json').read_bytes(); assert sha(seccompraw) == SECCOMP_SHA
    seccomp = json.loads(seccompraw); name = 'bridge8-'+uuid.uuid4().hex[:12]
    args = ['docker', 'create', '--name', name, '--label', 'org.lighthouse.bridge8='+name, '--pull=never', '--init', '--network=none', '--read-only', '--user=10001:10001',
            '--cap-drop=ALL', '--security-opt=no-new-privileges:true', '--security-opt=seccomp='+str(ROOT/'config/browser-seccomp.json'),
            '--memory=1400m', '--cpus=1', '--pids-limit=256', '--shm-size=256m', '--tmpfs=/tmp:rw,size=256m,mode=1777',
            '--tmpfs='+str(ROOT/'media')+':rw,size=32m,mode=0700,uid=10001,gid=10001',
            '--env=HOME=/tmp/home', '--env=XDG_CONFIG_HOME=/tmp/config', '--env=XDG_CACHE_HOME=/tmp/cache', '--env=PYTHONDONTWRITEBYTECODE=1',
            '--mount=type=bind,source='+str(owned)+',target=/owned',
            '--mount=type=bind,source='+str(overlay/'manifest.json')+',target=/run/bridge8-manifest.json,readonly']
    for n in FILES: args += ['--mount=type=bind,source='+str(overlay/n)+',target='+str(ROOT/n)+',readonly']
    cid = run([*args, '--entrypoint=python', IMAGE, '-c', BOOT]).stdout.strip()
    validate(inspect(cid), owned, overlay, seccomp); started = time.time(); run(['docker', 'start', cid]); timed_out = False
    try: code = int(run(['docker', 'wait', cid], timeout=240).stdout.strip())
    except subprocess.TimeoutExpired:
        timed_out = True; current = inspect(cid)
        assert current['Config']['Labels']['org.lighthouse.bridge8'] == name and current['Image'] == IMAGE
        run(['docker', 'stop', '--time', '10', cid]); code = 124
    logs = run(['docker', 'logs', cid]); (area/'tests.log').write_text(logs.stdout+'\n'+logs.stderr)
    final = inspect(cid); validate(final, owned, overlay, seccomp)
    xml = (owned/'results.xml').read_bytes(); cases = list(ET.fromstring(xml).iter('testcase'))
    groups = dict(Counter(v.attrib['classname'] for v in cases)); counts = {k: sum(v.find(k) is not None for v in cases) for k in ('failure', 'error', 'skipped')}
    before = json.loads((owned/'before.json').read_bytes()); after = json.loads((owned/'after.json').read_bytes())
    resources = all(v['resources']['pids.events'].get('max', -1) == v['resources']['memory.events'].get('oom', -1) == v['resources']['memory.events'].get('oom_kill', -1) == 0 for v in (before, after))
    unchanged = before['sources'] == after['sources'] == {**baseline, **FILES}
    production_after = identity(inspect(videoid)); production_unchanged = production_after == production_before
    passed = code == 0 and len(cases) == 8 and groups == {GROUP: 8} and not any(counts.values()) and resources and unchanged and production_unchanged and not timed_out and not final['State']['OOMKilled']
    (area/'accepted-results.xml').write_bytes(xml)
    proof = {'version': 'owner-handoff-bridge8-ubuntu-v5', 'passed': passed, 'image': IMAGE, 'source_commit': COMMIT,
             'archive_sha256': a.sha256, 'manifest': manifest, 'tests': len(cases), 'test_groups': groups, **counts, 'exit_code': code,
             'source_before': before, 'source_after': after, 'sources_unchanged': unchanged, 'runtime141_source_sha256': prior['runtime141_source_sha256'],
             'formal16_proof_sha256': PARENT, 'formal16_not_rerun': True, 'no_pid_or_oom_denial': resources,
             'timed_out': timed_out, 'oom_killed': final['State']['OOMKilled'], 'runtime_matches_allowlist': True,
             'network': 'none', 'rootfs_read_only': True, 'app_uid': 10001, 'production_data_mounted': False,
             'production_profile_mounted': False, 'credentials_injected': False, 'operator_main_called': False,
             'production_video_before': production_before, 'production_video_after': production_after, 'production_video_unchanged': production_unchanged, 'other_four_before': peer_before,
             'other_four_after': peers(), 'bridge_main_called': False, 'tunnel_started': False, 'credentials_read': False, 'ports_opened': False,
             'junit_file': str(area/'accepted-results.xml'), 'junit_sha256': sha(xml), 'seconds': time.time()-started}
    assert proof['other_four_before'] == proof['other_four_after']
    out = area/'test-result.json'; out.write_text(json.dumps(proof, sort_keys=True, indent=2))
    summary = {'file':str(out),'sha256':sha(out.read_bytes()),'passed':passed,'tests':len(cases),**counts}
    if passed and a.install:
        result = install_bridge(overlay, proof, sha(out.read_bytes()))
        result['other_four_unchanged'] = peers() == peer_before
        result['production_video_unchanged'] = identity(inspect(videoid)) == production_before
        assert result['other_four_unchanged'] and result['production_video_unchanged']
        receipt = area/'installation.json'; receipt.write_text(json.dumps(result, sort_keys=True, indent=2)); receipt.chmod(0o600)
        summary.update(installation_file=str(receipt),installation_sha256=sha(receipt.read_bytes()),installed=True)
    print(json.dumps(summary),flush=True)
    raise SystemExit(0 if passed else 1)


if __name__ == '__main__': main()
