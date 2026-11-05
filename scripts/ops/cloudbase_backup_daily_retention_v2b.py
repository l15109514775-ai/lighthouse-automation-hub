"""Daily same-budget acceptance, registry, authorized source retention and clone prune."""
import argparse,fcntl,hashlib,json,os,sys
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
PINS={'run_cloudbase_backup_sidecar_v1e.py': '0ce1bb9a362a5429ad71631d551b16ab043462295acd54fceb8998107ba4483e', 'accept_cloudbase_backup_capture_v1.py': 'f3a9a0543a236c5d2e94a209d6e2074c7b3828fce6641ce4fa7d780ac5f17efb', 'prune_cloudbase_verified_clones_v1.py': '6af8e57a2b50d9a47c69c1a7debc957e45f89f140860368e6c761e1c5cf8a9ce', 'run_cloudbase_backup_retention_v2b.py': '79d9715734e080460dcb716d9a39f2450a0a9528940a545cfda989498913c877', 'run_archive_source_registry_v2b.py': '9bad59e2490572abb091050820e0fef098a310e47d5d5ab5c9a9e80f5a3b42f2'}
HOST_FILES={'scripts/ops/cloudbase_backup_daily_v1.py': 'c31d61724d1e6ea2d6c9ab39d55cec9f07e4ae656edbd4965ca647aa3d10f989', 'scripts/ops/cloudbase_archive_registration_hook_v2.py': 'b85eaf439e738335ae7c2ff04e8beac8c5d7f4a0d4fadc04c2be24b98cb3bf5c'}
def main():
    assert os.name=='posix' and os.geteuid()==0;os.umask(0o077);verify_layer()
    host=BASE/'host';protected(host)
    for name,digest in HOST_FILES.items():
        file=host/name;protected(file);assert hashlib.sha256(file.read_bytes()).hexdigest()==digest
    sys.path.insert(0,str(host))
    from scripts.ops import cloudbase_backup_daily_v1 as d
    from scripts.ops import cloudbase_archive_registration_hook_v2 as hook
    oldstate=d.STATE;d.STATE=Path('/var/lib/lighthouse-backup-ops/retention-v2b/daily-state');d.PINS=PINS
    d.verify_helpers();p=argparse.ArgumentParser();p.add_argument('--run-once',action='store_true');p.add_argument('--verify-only',action='store_true');a=p.parse_args()
    if not a.run_once:
        print(json.dumps({'state':'verified_not_run','version':'cloudbase-daily-retention-v2','external_requests':0,'original_source_apply_enabled':True}));return 0
    for folder in (d.STATE,d.STATE/'history'):d.protected(folder)
    locks=[]
    try:
        for file in (oldstate/'daily.lock',d.STATE/'daily.lock'):
            protected(file.parent);fd=os.open(file,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600);locks.append(fd);fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        previous=None
        if (d.STATE/'latest.json').exists():
            previous,_=d.read_record(d.STATE/'latest.json',d.STATE,root_owned=True)
            assert previous['version']=='cloudbase-daily-accept-prune-v1' and previous['helper_pins']==PINS and previous['image']==IMAGE
        elif (oldstate/'latest.json').exists():
            legacy,_=d.read_record(oldstate/'latest.json',oldstate,root_owned=True);assert legacy['state']=='completed'
        def runner(script,args,timeout):
            if script=='run_cloudbase_backup_sidecar_v1e.py':
                value=d.call('run_cloudbase_backup_retention_v2b.py',args,timeout)
                assert value.get('retention_runtime_sources')==LAYER
                return value
            return d.call(script,args,timeout)
        cleanups={}
        def registrar(reference,*,verify_only):
            mode='--verify-registration' if verify_only else '--register-before-prune'
            value=d.call('run_archive_source_registry_v2b.py',[mode,'--report',reference['report_file'],
                '--expected-report-sha256',reference['report_sha256']],600)
            assert value['version']=='archive-source-registry-host-v2' and value['uid']==10001 and value['network']=='none'
            assert value['image']==IMAGE and value['credentials_mounted'] is value['profile_mounted'] is False and value['runtime_sources']==LAYER
            cleanup=d.call('run_archive_source_registry_v2b.py',['--autorun','--report',reference['report_file'],
                '--expected-report-sha256',reference['report_sha256']],600)
            assert cleanup['network']=='none' and cleanup['credentials_mounted'] is False and cleanup['runtime_sources']==LAYER
            cleanups[reference['operation_id']]=cleanup['result']
            d.atomic(d.STATE/'last-source-cleanup.json',cleanup['result'])
            return value['result']
        result=hook.execute(previous,registrar=registrar,runner=runner,saver=d.save,validator=d.validate_report)
        print(json.dumps({'version':'cloudbase-daily-retention-v2','state':result['state'],
            'operation_id':result['acceptance']['operation_id'],'registration':result['archive_source_registration_v2'],'source_cleanup':cleanups,
            'clones_pruned':result['pruned_files'],'additional_remote_gets':0,'original_source_apply_enabled':True}));return 0
    finally:
        for fd in locks:os.close(fd)
if __name__=='__main__':raise SystemExit(main())
