"""Root app-UID registry runner; no network/credentials/browser or remote calls."""
import argparse,hashlib,json,os,subprocess,uuid
from pathlib import Path
ROOT=Path('/opt/lighthouse-hub');OPS=Path('/usr/local/lib/lighthouse-backup-ops');BASE=OPS/'retention-v2b'
RUNTIME=BASE/'runtime'
IMAGE='sha256:6592ebce93025293ac37e8c990f0f09daa639a0ed95354120a22371d2e9b6d55'
LAYER={'services/hub/cloudbase_backup_runtime.py': 'ed47e411c6f96696b2886c8a6d8379b4f15df9c1b0ed3257d439d1cdc4277099', 'services/hub/archive_source_retention_v2.py': 'cb81506a9fc0b65a85f25c3f4417496a50fb0bd22e216b4003826d11f2bb2a5e', 'scripts/ops/prune_cloudbase_verified_clones_v1.py': '6af8e57a2b50d9a47c69c1a7debc957e45f89f140860368e6c761e1c5cf8a9ce', 'scripts/archive-source-retention-v2.py': 'b5280004058bdd730e8a9debfab11e66bc2a8b9e80eaa4c82944480ce91a065e', 'services/hub/archive_source_autorun_v2.py': 'f55b84a24ccb04364c451ef4a829d85ffa46372138d9d5ee12d2d52ff261aade'}
PROOF_TARGET=Path('/var/lib/lighthouse-backup-ops/archive-retention-v2-tests/8ef6cce2-53c4-4cf3-b391-61bdbef7012c')
PROOF_MIRROR=Path('/var/lib/lighthouse-backup-ops/retention-v2b/readiness')

def protected(path):
    assert path.is_absolute() and path.resolve()==path and not any(p.is_symlink() for p in (path,*path.parents))
    for p in (path,*path.parents):assert p.stat().st_uid==0 and p.stat().st_mode&0o022==0

def verify_layer():
    for name,digest in LAYER.items():
        file=RUNTIME/name;protected(file);assert file.is_file() and hashlib.sha256(file.read_bytes()).hexdigest()==digest

def mounts_for_mode(apply):
    assert type(apply) is bool
    return (('data',True),('media',apply),('logs',apply),('backups',False),('config',False))

def main():
    assert os.name=='posix' and os.geteuid()==0;os.umask(0o077);verify_layer()
    p=argparse.ArgumentParser();m=p.add_mutually_exclusive_group(required=True)
    for name in ('register-before-prune','verify-registration','authorize','plan','apply','verify-release','autorun'):m.add_argument('--'+name,action='store_true')
    p.add_argument('--report');p.add_argument('--expected-report-sha256');p.add_argument('--file');p.add_argument('--authorization-id');p.add_argument('--dry-run',action='store_true');a=p.parse_args()
    if a.register_before_prune or a.verify_registration or a.autorun:
        report=Path(a.report);assert report.parent.parent==ROOT/'backups/cloudbase-restore-check' and report.name=='restoration-report.json'
        assert str(uuid.UUID(report.parent.name))==report.parent.name and report.resolve()==report and not report.is_symlink()
        assert hashlib.sha256(report.read_bytes()).hexdigest()==a.expected_report_sha256
        arguments=['python',str(ROOT/'scripts/archive-source-retention-v2.py'),
            '--register-before-prune' if a.register_before_prune else '--verify-registration',str(report),
            '--expected-report-sha256',a.expected_report_sha256]
        if a.autorun:
            code="from pathlib import Path;from services.hub.config import Settings;from services.hub.db import Store;from services.hub.archive_source_autorun_v2 import ArchiveSourceDailyAutorunV2;import json,sys;s=Settings.load();r=ArchiveSourceDailyAutorunV2(s,Store(s.db_path));print(json.dumps(r.run_once(Path(sys.argv[1]),sys.argv[2],apply=sys.argv[3]=='apply')))"
            arguments=['python','-c',code,str(report),a.expected_report_sha256,'dry-run' if a.dry_run else 'apply']
    elif a.authorize:arguments=['python',str(ROOT/'scripts/archive-source-retention-v2.py'),'--authorize-file',str(ROOT/'config/archive-source-retention-owner-v2.json')]
    elif a.verify_release:
        assert str(uuid.UUID(a.authorization_id))==a.authorization_id
        code="from services.hub.config import Settings;from services.hub.db import Store;from services.hub.archive_source_retention_v2 import ArchiveSourceRetentionV2;import json,sys;s=Settings.load();r=ArchiveSourceRetentionV2(s,Store(s.db_path));print(json.dumps(r._release(sys.argv[1])))"
        arguments=['python','-c',code,a.authorization_id]
    else:
        file=Path(a.file);expected=ROOT/'data/archive-source-retention-v2'/('plans' if a.apply else 'requests')
        assert file.parent==expected and file.suffix=='.json' and str(uuid.UUID(file.stem))==file.stem and file.resolve()==file
        arguments=['python',str(ROOT/'scripts/archive-source-retention-v2.py'),'--apply-plan' if a.apply else '--plan-file',str(file)]
    cmd=['docker','run','--pull=never','--rm','--init','--name','lighthouse-archive-registry-'+uuid.uuid4().hex[:10],
        '--network','none','--read-only','--user','10001:10001','--cap-drop','ALL','--security-opt','no-new-privileges:true',
        '--memory','256m','--cpus','1','--pids-limit','64','--tmpfs','/tmp:rw,size=32m,mode=1777',
        '--tmpfs',str(ROOT/'data/browser-profiles')+':rw,size=1024,mode=0700,uid=10001,gid=10001',
        '-e','HUB_ROOT='+str(ROOT),'-e','PYTHONDONTWRITEBYTECODE=1','-e','PYTHONNOUSERSITE=1']
    for name,writable in mounts_for_mode(a.apply or (a.autorun and not a.dry_run)):
        cmd+=['--mount','type=bind,src='+str(ROOT/name)+',dst='+str(ROOT/name)+('' if writable else ',readonly')]
    for name in LAYER:cmd+=['--mount','type=bind,src='+str(RUNTIME/name)+',dst='+str(ROOT/name)+',readonly']
    if PROOF_MIRROR.exists():cmd+=['--mount','type=bind,src='+str(PROOF_MIRROR)+',dst='+str(PROOF_TARGET)+',readonly']
    result=subprocess.run(cmd+[IMAGE,*arguments],stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=600)
    assert len(result.stdout)<2000000 and len(result.stderr)<200000
    try:value=json.loads(result.stdout)
    except ValueError:value={'state':'registry_failed','error_code':'APP_RESULT_NOT_JSON','stderr_bytes':len(result.stderr)}
    output={'version':'archive-source-registry-host-v2','exit_code':result.returncode,'image':IMAGE,'uid':10001,
        'network':'none','credentials_mounted':False,'profile_mounted':False,'runtime_sources':LAYER,'result':value}
    print(json.dumps(output,ensure_ascii=False));return result.returncode
if __name__=='__main__':raise SystemExit(main())
