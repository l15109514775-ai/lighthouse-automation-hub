"""Default plan. Install same-budget 03:30 registry hook and guarded release.

No backup run, GET, original deletion, timer enable/start or core restart.
The existing timer remains enabled; only its inactive service ExecStart changes.
"""
from datetime import datetime,timezone
from pathlib import Path
import argparse,hashlib,json,os,stat,subprocess,uuid
import xml.etree.ElementTree as ET

ROOT=Path('/opt/lighthouse-hub');OPS=Path('/usr/local/lib/lighthouse-backup-ops')
BASE=OPS/'retention-v2b';STATE=Path('/var/lib/lighthouse-backup-ops/retention-v2b')
PROOF=Path('/var/lib/lighthouse-backup-ops/archive-retention-v2-tests/8ef6cce2-53c4-4cf3-b391-61bdbef7012c')
PROOF_SHA='5f95dbb817acf77dd3b004b96335032b3a16c693de3f7b9e29ce8ba4766b3902'
IMAGE='sha256:6592ebce93025293ac37e8c990f0f09daa639a0ed95354120a22371d2e9b6d55'
ARCHIVE_SHA='a218fc8d728340755c076572f2f301cdd04bc4ff57f50aac71d044bc46f98705'
FILES={'run_cloudbase_backup_retention_v2b.py':'79d9715734e080460dcb716d9a39f2450a0a9528940a545cfda989498913c877',
 'run_archive_source_registry_v2b.py':'9bad59e2490572abb091050820e0fef098a310e47d5d5ab5c9a9e80f5a3b42f2',
 'cloudbase_backup_daily_retention_v2b.py':'fe8c9948e0c94633f455d4a7ea12ddd7ab8f22631a7862698a9a210d07e216d9'}
RUNTIME_FILES={'services/hub/cloudbase_backup_runtime.py':'ed47e411c6f96696b2886c8a6d8379b4f15df9c1b0ed3257d439d1cdc4277099',
 'services/hub/archive_source_retention_v2.py':'cb81506a9fc0b65a85f25c3f4417496a50fb0bd22e216b4003826d11f2bb2a5e',
 'scripts/ops/prune_cloudbase_verified_clones_v1.py':'6af8e57a2b50d9a47c69c1a7debc957e45f89f140860368e6c761e1c5cf8a9ce',
 'scripts/archive-source-retention-v2.py':'b5280004058bdd730e8a9debfab11e66bc2a8b9e80eaa4c82944480ce91a065e',
 'services/hub/archive_source_autorun_v2.py':'f55b84a24ccb04364c451ef4a829d85ffa46372138d9d5ee12d2d52ff261aade'}
HOST_FILES={'scripts/ops/cloudbase_backup_daily_v1.py':'c31d61724d1e6ea2d6c9ab39d55cec9f07e4ae656edbd4965ca647aa3d10f989',
 'scripts/ops/cloudbase_archive_registration_hook_v2.py':'b85eaf439e738335ae7c2ff04e8beac8c5d7f4a0d4fadc04c2be24b98cb3bf5c'}
COUNTS={'tests.test_archive_source_retention_v2':31,'tests.ops.test_cloudbase_archive_registration_hook_v2':5,
 'tests.test_cloudbase_backup_runtime':36,'tests.test_cloudbase_backup':44,'tests.test_cloudbase_cos_backup':23}
UNIT=Path('/etc/systemd/system/lighthouse-cloudbase-backup.service')
OLD_EXEC='ExecStart=/usr/bin/python3 /usr/local/lib/lighthouse-backup-ops/cloudbase_backup_daily_v1.py --run-once'
INTERIM_EXEC='ExecStart=/usr/bin/python3 /usr/local/lib/lighthouse-backup-ops/cloudbase_backup_daily_retention_v2.py --run-once'
NEW_EXEC='ExecStart=/usr/bin/python3 /usr/local/lib/lighthouse-backup-ops/cloudbase_backup_daily_retention_v2b.py --run-once'


def sha(raw):return hashlib.sha256(raw).hexdigest()


def safe(path):
    assert path.is_absolute() and path.resolve()==path and not any(p.is_symlink() for p in (path,*path.parents));return path


def protected(path):
    safe(path)
    for p in (path,*path.parents):assert p.stat().st_uid==0 and p.stat().st_mode&0o022==0


def directory(path,mode=0o755,gid=0):
    safe(path)
    if not path.exists():
        protected(path.parent);path.mkdir(mode=mode);os.chown(path,0,gid);path.chmod(mode)
    protected(path);assert path.is_dir()


def write(path,raw,mode=0o644,gid=0):
    safe(path);protected(path.parent)
    if path.exists():protected(path);assert path.is_file()
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.partial')
    fd=os.open(temp,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,mode)
    with os.fdopen(fd,'wb') as stream:
        stream.write(raw);stream.flush();os.fchmod(stream.fileno(),mode);os.fchown(stream.fileno(),0,gid);os.fsync(stream.fileno())
    os.replace(temp,path);fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def command(*args):return subprocess.check_output(args,timeout=45,text=True)


def candidate_service(raw):
    value=raw.decode('utf-8')
    if value.count(NEW_EXEC)==1 and OLD_EXEC not in value:return raw
    assert (value.count(OLD_EXEC)==1 or value.count(INTERIM_EXEC)==1) and NEW_EXEC not in value
    assert 'ProtectSystem=strict' in value and 'Restart=no' in value and '/opt/lighthouse-hub/data' in value
    return value.replace(OLD_EXEC,NEW_EXEC).replace(INTERIM_EXEC,NEW_EXEC).encode()


def owner_instruction(raw):
    assert 0<len(raw)<=16384;value=json.loads(raw)
    assert set(value)=={'actual_owner_response','owner_response_reference'}
    assert all(isinstance(value[key],str) and value[key].strip() and len(value[key])<=2048 for key in value)
    return value


def verify_attestation(value,raw_junit):
    assert value['version']=='archive-source-retention-v2-ubuntu-tests-v1' and value['image']==IMAGE
    assert value['archive_sha256']==ARCHIVE_SHA and value['tests']==139 and value['failures']==value['errors']==value['skipped']==0
    assert value['exit_code']==0 and value['oom_killed'] is False and value['network']=='none'
    assert value['application_data_mounted'] is value['production_profile_mounted'] is value['secrets_environment_injected'] is False
    for key in ('full_suite_passed','no_pid_or_oom_denial','runtime_matches_declared_candidate','mounts_match_isolated_allowlist'):
        assert value[key] is True
    assert sha(raw_junit)==value['junit_sha256'];cases=ET.fromstring(raw_junit).findall('.//testcase');groups={}
    for case in cases:
        groups[case.get('classname')]=groups.get(case.get('classname'),0)+1
        assert all(case.find(tag) is None for tag in ('failure','error','skipped'))
    assert groups==COUNTS and len(cases)==139
    expected={name:RUNTIME_FILES[name] for name in ('services/hub/cloudbase_backup_runtime.py','services/hub/archive_source_retention_v2.py','scripts/ops/prune_cloudbase_verified_clones_v1.py')}
    assert value['source_files']==expected
    return expected


def main():
    assert os.name=='posix' and os.geteuid()==0;os.umask(0o077)
    import fcntl
    p=argparse.ArgumentParser(description=__doc__);m=p.add_mutually_exclusive_group();m.add_argument('--plan',action='store_true');m.add_argument('--install',action='store_true')
    p.add_argument('--source-dir',default=str(Path(__file__).resolve().parent));p.add_argument('--owner-request');p.add_argument('--autorun-proof',default='/var/lib/lighthouse-backup-ops/archive-autorun-v2-tests/d743131c-c3e5-4b70-8339-31348ae48199');p.add_argument('--autorun-proof-sha256',default='186a531f679d3926cecabd074817f5ada8041449b3e3783eece9c62295fe4436');a=p.parse_args()
    protected(PROOF);att=(PROOF/'test-attestation.json').read_bytes();assert sha(att)==PROOF_SHA
    value=json.loads(att);junit=PROOF/'accepted-results.xml';assert value['junit_file']==str(junit)
    sources=verify_attestation(value,junit.read_bytes());bodies={}
    for name,digest in {**{k:v for k,v in RUNTIME_FILES.items() if k!='services/hub/archive_source_autorun_v2.py'},**HOST_FILES}.items():
        file=safe(PROOF/'overlay'/name);raw=file.read_bytes()
        assert sha(raw)==digest and value['manifest']['files'][name]['sha256']==digest
        bodies[name]=raw
    directory_source=safe(Path(a.source_dir));helpers={}
    for name,digest in FILES.items():
        file=safe(directory_source/name);raw=file.read_bytes();assert 0<len(raw)<100000 and sha(raw)==digest;helpers[name]=raw
    protected(UNIT);previous=UNIT.read_bytes();updated=candidate_service(previous)
    assert command('/usr/bin/docker','image','inspect','-f','{{.Id}}',IMAGE).strip()==IMAGE
    active=command('/usr/bin/systemctl','is-active','lighthouse-cloudbase-backup.service').strip() if subprocess.run(
        ['/usr/bin/systemctl','is-active','--quiet','lighthouse-cloudbase-backup.service']).returncode==0 else 'inactive'
    assert active=='inactive'
    assert command('/usr/bin/docker','ps','-q','--filter','label=org.lighthouse.backup.sidecar').strip()==''
    oldstate=Path('/var/lib/lighthouse-backup-ops')
    if (oldstate/'latest.json').exists():
        protected(oldstate/'latest.json');assert json.loads((oldstate/'latest.json').read_bytes())['state']=='completed'
    auto_sources={}
    if a.autorun_proof:
        ap=safe(Path(a.autorun_proof));protected(ap);araw=(ap/'test-attestation.json').read_bytes();assert sha(araw)==a.autorun_proof_sha256
        av=json.loads(araw);assert av['version']=='archive-source-autorun-v2-ubuntu-tests-v1' and av['tests']==9 and av['exit_code']==0 and av['full_suite_passed'] is True
        assert av['failures']==av['errors']==av['skipped']==0 and av['oom_killed'] is False and av['network']=='none'
        assert av['image']==IMAGE and all(av['source_files'].get(k)==v for k,v in sources.items())
        assert av['application_data_mounted'] is av['production_profile_mounted'] is av['secrets_environment_injected'] is False
        assert av['no_pid_or_oom_denial'] is av['runtime_matches_declared_candidate'] is av['mounts_match_isolated_allowlist'] is True
        j=(ap/'accepted-results.xml').read_bytes();assert sha(j)==av['junit_sha256'];cases=ET.fromstring(j).findall('.//testcase')
        assert len(cases)==9 and all(c.get('classname')=='tests.test_archive_source_autorun_v2' and all(c.find(t) is None for t in ('failure','error','skipped')) for c in cases)
        auto_file=ap/'overlay'/'services/hub/archive_source_autorun_v2.py';auto_raw=auto_file.read_bytes();assert sha(auto_raw)==RUNTIME_FILES['services/hub/archive_source_autorun_v2.py'] and av['source_files']['services/hub/archive_source_autorun_v2.py']==sha(auto_raw)
        bodies['services/hub/archive_source_autorun_v2.py']=auto_raw;auto_sources=av['source_files']
    owner_raw=None
    if a.owner_request:owner_raw=safe(Path(a.owner_request)).read_bytes();owner_instruction(owner_raw)
    summary={'version':'archive-retention-install-v2b','state':'planned_not_installed','default_plan':not a.install,
        'image':IMAGE,'tested_139_attestation_sha256':PROOF_SHA,'runtime_files':RUNTIME_FILES,'host_files':HOST_FILES,'helper_files':FILES,
        'unit_changed':previous!=updated,'timer_changed':False,'timer_enabled_or_started_by_this_script':False,
        'network_requests':0,'backup_started':False,'core_containers_restarted':False,'source_files_deleted':0,
        'original_source_apply_scheduled':True,'autorun_tests_verified':bool(auto_sources),'release_install_requested':owner_raw is not None,
        'sqlite_index_auxiliary_directory_rw_only':True,'main_database_mount_readonly_for_backup':True}
    if not a.install:print(json.dumps(summary));return 0
    assert auto_sources,'ACTUAL_AUTORUN_9_TEST_PROOF_REQUIRED'
    assert owner_raw is not None or (ROOT/'config/archive-source-retention-v2-release.json').exists(),'RECORDED_LONG_TERM_OWNER_INSTRUCTION_REQUIRED'
    protected(OPS);directory(BASE);directory(BASE/'runtime');directory(BASE/'host')
    directory(STATE,0o700);directory(STATE/'daily-state',0o700);directory(STATE/'daily-state/history',0o700)
    directory(STATE/'install-history',0o700);history=STATE/'install-history'/str(uuid.uuid4());directory(history,0o700)
    lock=os.open(oldstate/'daily.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert subprocess.run(['/usr/bin/systemctl','is-active','--quiet','lighthouse-cloudbase-backup.service']).returncode!=0
        write(history/'previous.service',previous,0o600)
        for name,body in bodies.items():
            target=(BASE/'runtime' if name in RUNTIME_FILES else BASE/'host')/name
            chain=[];parent=target.parent
            while not parent.exists():chain.append(parent);parent=parent.parent
            for folder in reversed(chain):directory(folder)
            write(target,body)
        for name,body in helpers.items():write(OPS/name,body)
        directory(STATE/'readiness',0o750,10001)
        write(STATE/'readiness/test-attestation.json',att,0o640,10001)
        write(STATE/'readiness/accepted-results.xml',junit.read_bytes(),0o640,10001)
        # The mirror retains the exact accepted bytes and original absolute
        # JUnit reference. The registry container binds it to PROOF read-only.
        authorization=None
        if owner_raw is not None:
            write(ROOT/'config/archive-source-retention-owner-v2.json',owner_raw,0o640,10001)
            result=json.loads(command('/usr/bin/python3',str(OPS/'run_archive_source_registry_v2b.py'),'--authorize'))
            assert result['exit_code']==0 and result['network']=='none' and result['credentials_mounted'] is False
            authorization=result['result'];assert authorization['version']=='owner-local-retention-instruction-v2'
            release={'version':'archive-source-retention-release-v2','authorization_id':authorization['authorization_id'],
                'mode':'verified_archive_members_only','formal_video_launch_claimed':False,'source_files':sources,
                'test_attestation':{'file':str(PROOF/'test-attestation.json'),'sha256':PROOF_SHA}}
            file=ROOT/'config/archive-source-retention-v2-release.json'
            if file.exists():write(history/'previous.release.json',file.read_bytes(),0o600)
            write(file,(json.dumps(release,indent=2)+'\n').encode(),0o640,10001)
            verified=json.loads(command('/usr/bin/python3',str(OPS/'run_archive_source_registry_v2b.py'),'--verify-release',
                '--authorization-id',authorization['authorization_id']))
            assert verified['exit_code']==0 and verified['result']['test_attestation_sha256']==PROOF_SHA
        if authorization is None:
            existing=json.loads((ROOT/'config/archive-source-retention-v2-release.json').read_bytes());authorization={'authorization_id':existing['authorization_id']}
            verified=json.loads(command('/usr/bin/python3',str(OPS/'run_archive_source_registry_v2b.py'),'--verify-release','--authorization-id',authorization['authorization_id']))
            assert verified['exit_code']==0 and verified['result']['test_attestation_sha256']==PROOF_SHA
        checked=json.loads(command('/usr/bin/python3',str(OPS/'cloudbase_backup_daily_retention_v2b.py'),'--verify-only'))
        assert checked['state']=='verified_not_run' and checked['external_requests']==0
        try:
            write(UNIT,updated)
            command('/usr/bin/systemd-analyze','verify',str(UNIT));command('/usr/bin/systemctl','daemon-reload')
        except BaseException:
            write(UNIT,previous);command('/usr/bin/systemctl','daemon-reload');raise
        report={**summary,'state':'installed_next_timer_not_run','default_plan':False,'at':datetime.now(timezone.utc).isoformat(),
            'unit_sha256':sha(updated),'previous_unit_sha256':sha(previous),'authorization_id':authorization['authorization_id'] if authorization else None,
            'release_installed':authorization is not None,'old_helper_files_preserved':True,'historical_ledgers_erased':False,
            'proof_mirror_bytes_unchanged':True,'proof_mirror_owner':'root:10001','proof_mirror_mode':'dir0750/file0640'}
        write(history/'result.json',(json.dumps(report,indent=2)+'\n').encode(),0o600)
        print(json.dumps({**report,'result_file':str(history/'result.json')}));return 0
    finally:os.close(lock)


if __name__=='__main__':raise SystemExit(main())
