"""Daily same-budget acceptance, registry, authorized source retention and clone prune."""
import argparse,fcntl,hashlib,json,os,sys
from pathlib import Path
ROOT=Path('/opt/lighthouse-hub');OPS=Path('/usr/local/lib/lighthouse-backup-ops');BASE=OPS/'retention-v2c'
RUNTIME=BASE/'runtime'
IMAGE='sha256:6592ebce93025293ac37e8c990f0f09daa639a0ed95354120a22371d2e9b6d55'
LAYER={'services/hub/cloudbase_backup_runtime.py': '399baa357f6a31457a3e2b89ba3e884f9b7fd4d5fcd30a21a6bcf235c97ffd04', 'services/hub/archive_source_retention_v2.py': 'cb81506a9fc0b65a85f25c3f4417496a50fb0bd22e216b4003826d11f2bb2a5e', 'scripts/ops/prune_cloudbase_verified_clones_v1.py': '58960bfbbeab64776bdfb23f69c83cfb4a80b5b384a5a5d12bcdf6fe91cbcafe', 'scripts/archive-source-retention-v2.py': 'b5280004058bdd730e8a9debfab11e66bc2a8b9e80eaa4c82944480ce91a065e', 'services/hub/archive_source_autorun_v2.py': 'f55b84a24ccb04364c451ef4a829d85ffa46372138d9d5ee12d2d52ff261aade'}
RETENTION_LAYER={**LAYER, 'adapters/safe_https.py': '9f421d83b3aa44a19430896b8b80d1baa6b8916799d71ca967c7d6d46921d391', 'adapters/cloudbase_cos_backup.py': '2955ce521e67291889890d49aaadef2e5789f7a6b71516bfdc855242daad667c'}
PROOF_TARGET=Path('/var/lib/lighthouse-backup-ops/archive-retention-v2-tests/0b452387-bbc9-49d8-9667-85fe969b0dae')
PROOF_MIRROR=Path('/var/lib/lighthouse-backup-ops/retention-v2c/readiness')

def protected(path):
    assert path.is_absolute() and path.resolve()==path and not any(p.is_symlink() for p in (path,*path.parents))
    for p in (path,*path.parents):assert p.stat().st_uid==0 and p.stat().st_mode&0o022==0

def verify_layer():
    for name,digest in LAYER.items():
        file=RUNTIME/name;protected(file);assert file.is_file() and hashlib.sha256(file.read_bytes()).hexdigest()==digest
PINS={'run_cloudbase_backup_sidecar_v1e.py': '0ce1bb9a362a5429ad71631d551b16ab043462295acd54fceb8998107ba4483e', 'accept_cloudbase_backup_capture_v1.py': 'f3a9a0543a236c5d2e94a209d6e2074c7b3828fce6641ce4fa7d780ac5f17efb', 'prune_cloudbase_verified_clones_v1.py': '58960bfbbeab64776bdfb23f69c83cfb4a80b5b384a5a5d12bcdf6fe91cbcafe', 'run_cloudbase_backup_retention_v2c.py': '83b3f6731ac8d3adb63b0a0d9c563647ddbe9c039bb1d8a5380d014c1526dd38', 'run_archive_source_registry_v2c.py': '79f49bd54e4369a89204891f4ba19b1aac8d1e532681a44a515470754f67dd91'}
HOST_FILES={'scripts/ops/cloudbase_backup_daily_v1.py': '16ca26f3613098408ffe68c045c1df3270154e93661bf38a0d82fcae90fece1c', 'scripts/ops/cloudbase_archive_registration_hook_v2.py': 'b85eaf439e738335ae7c2ff04e8beac8c5d7f4a0d4fadc04c2be24b98cb3bf5c'}
LEGACY_PINS={'run_cloudbase_backup_sidecar_v1e.py': '0ce1bb9a362a5429ad71631d551b16ab043462295acd54fceb8998107ba4483e', 'accept_cloudbase_backup_capture_v1.py': 'f3a9a0543a236c5d2e94a209d6e2074c7b3828fce6641ce4fa7d780ac5f17efb', 'prune_cloudbase_verified_clones_v1.py': '6af8e57a2b50d9a47c69c1a7debc957e45f89f140860368e6c761e1c5cf8a9ce', 'run_cloudbase_backup_retention_v2b.py': '79d9715734e080460dcb716d9a39f2450a0a9528940a545cfda989498913c877', 'run_archive_source_registry_v2b.py': '9bad59e2490572abb091050820e0fef098a310e47d5d5ab5c9a9e80f5a3b42f2'}

RECOVERY_DAILY='6cfa6b1d-46db-4ed9-888e-f6564f07d956'
RECOVERY_OPERATION='7505562a-3b39-4e9c-897b-a704a05aa6a9'

PRE_PRUNE_CAP_PINS={'run_cloudbase_backup_sidecar_v1e.py': '22e936ffd5cb03b34d76ec0dc9173a733a8184ab9e26aeb6d6bc90b26dd6a454', 'accept_cloudbase_backup_capture_v1.py': '1508b1a70777806b30ec7b8eeb1c3ced9a2bcd8053199c11a36941979ec74563', 'prune_cloudbase_verified_clones_v1.py': '6af8e57a2b50d9a47c69c1a7debc957e45f89f140860368e6c761e1c5cf8a9ce', 'run_cloudbase_backup_retention_v2c.py': 'b3a4263887e371bd9e5b2e720e71d9c934c6758572a591a0b90b48078247e6a0', 'run_archive_source_registry_v2c.py': 'd8c1fc97e20b3c05519240e14eab9e51026989cef23835c7c6d2d3be6e2ae7f3'}

def validate_previous(previous):
    assert previous['version']=='cloudbase-daily-accept-prune-v1' and previous['image']==IMAGE
    assert previous['helper_pins'] in (PINS, PRE_PRUNE_CAP_PINS) or (previous['state']=='completed' and previous['helper_pins']==LEGACY_PINS)

def verify_recovered_gap(d):
    # Only the exact root-authorized failed latest was relocated; history stays.
    folder=Path('/var/lib/lighthouse-backup-ops/preupload-recovery-v1')/RECOVERY_DAILY
    def load(file):
        protected(file);assert file.is_file() and 0<file.stat().st_size<=262144
        raw=file.read_bytes();return json.loads(raw),hashlib.sha256(raw).hexdigest()
    receipt,_=load(folder/'receipt.json');intent,_=load(folder/'intent.json')
    assert receipt['version']==intent['version']=='cloudbase-preupload-daily-recovery-v1'
    assert receipt['state']=='preupload_failure_reconciled' and intent['state']=='relocation_intent_saved'
    plan=intent['plan'];digest=hashlib.sha256(json.dumps(plan,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    assert digest==intent['plan_sha256']==receipt['plan_sha256']
    assert plan['daily_id']==receipt['daily_id']==RECOVERY_DAILY and plan['operation_id']==receipt['operation_id']==RECOVERY_OPERATION
    assert receipt['active_latest_absent'] is receipt['history_preserved'] is True
    assert receipt['ledger_modified'] is receipt['backup_success_claimed'] is receipt['core_services_restarted'] is False
    assert receipt['external_requests']==0 and plan['external_requests']==0 and plan['ledger_modified'] is False
    failed,failed_sha=load(folder/'failed-latest.json');history,history_sha=load(d.STATE/'history'/(RECOVERY_DAILY+'.json'))
    legacy,legacy_sha=load(Path('/var/lib/lighthouse-backup-ops/latest.json'))
    assert failed_sha==plan['source_hashes']['latest'] and history_sha==plan['source_hashes']['history']
    assert legacy_sha==plan['source_hashes']['legacy'] and legacy['state']=='completed'
    assert failed['daily_id']==history['daily_id']==RECOVERY_DAILY and failed['state']==history['state']=='accept_failed'
    assert failed['helper_pins']==history['helper_pins']==LEGACY_PINS
    assert not (d.STATE/'latest.json').exists()


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
            validate_previous(previous)
        else:
            verify_recovered_gap(d)
        if previous is None and (oldstate/'latest.json').exists():
            legacy,_=d.read_record(oldstate/'latest.json',oldstate,root_owned=True);assert legacy['state']=='completed'
        def runner(script,args,timeout):
            if script=='run_cloudbase_backup_sidecar_v1e.py':
                value=d.call('run_cloudbase_backup_retention_v2c.py',args,timeout)
                assert value.get('retention_runtime_sources')==RETENTION_LAYER
                return value
            return d.call(script,args,timeout)
        cleanups={}
        def registrar(reference,*,verify_only):
            mode='--verify-registration' if verify_only else '--register-before-prune'
            value=d.call('run_archive_source_registry_v2c.py',[mode,'--report',reference['report_file'],
                '--expected-report-sha256',reference['report_sha256']],600)
            assert value['version']=='archive-source-registry-host-v2' and value['uid']==10001 and value['network']=='none'
            assert value['image']==IMAGE and value['credentials_mounted'] is value['profile_mounted'] is False and value['runtime_sources']==LAYER
            try:
                cleanup=d.call('run_archive_source_registry_v2c.py',['--autorun','--report',reference['report_file'],
                    '--expected-report-sha256',reference['report_sha256']],600)
                assert cleanup['network']=='none' and cleanup['credentials_mounted'] is False and cleanup['runtime_sources']==LAYER
                summary=cleanup['result']
            except RuntimeError:
                # Destructive source cleanup is gated on a resolver release that
                # is stale after the prune size-cap fix. Skip it, keep every
                # local source, and let verified clone prune continue.
                summary={'state':'deferred','error_code':'SOURCE_CLEANUP_RESOLVER_NOT_READY',
                    'source_files_deleted':0,'network':'none','credentials_mounted':False,'remote_requests':0}
            cleanups[reference['operation_id']]=summary
            d.atomic(d.STATE/'last-source-cleanup.json',summary)
            return value['result']
        result=hook.execute(previous,registrar=registrar,runner=runner,saver=d.save,validator=d.validate_report)
        print(json.dumps({'version':'cloudbase-daily-retention-v2','state':result['state'],
            'operation_id':result['acceptance']['operation_id'],'registration':result['archive_source_registration_v2'],'source_cleanup':cleanups,
            'clones_pruned':result['pruned_files'],'additional_remote_gets':0,'original_source_apply_enabled':True}));return 0
    finally:
        for fd in locks:os.close(fd)
if __name__=='__main__':raise SystemExit(main())
