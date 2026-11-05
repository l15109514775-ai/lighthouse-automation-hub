"""Use the unchanged v2c daily workflow after one evidence-bound 404 recovery.

No timeout, budget, helper PINS, runtime, queue, schedule or cleanup changes.
--verify-recovery exercises the same missing-latest gate without starting backup.
"""
from pathlib import Path
from types import SimpleNamespace
import hashlib,importlib.util,json,os,sys

OPS=Path('/usr/local/lib/lighthouse-backup-ops')
DAILY_STATE=Path('/var/lib/lighthouse-backup-ops/retention-v2b/daily-state')
SOURCES={'cloudbase_backup_daily_retention_v2c.py':'91fc68526997ebb6c336e4bf11849640208288162a9cae63a208b3d937c7db95',
         'recover_cloudbase_remote_absent_v1.py':'ce0524dddda6c516c4ba09a517faf73553e51d9f65fb3cd3273a3b6822dac830'}

def protected(file):
    assert file.is_absolute() and file.resolve()==file and not any(p.is_symlink() for p in (file,*file.parents))
    for path in (file,*file.parents):assert path.stat().st_uid==0 and not path.stat().st_mode&0o022
    assert file.is_file() and 0<file.stat().st_size<=262144

def load_module(filename):
    file=OPS/filename;protected(file)
    assert hashlib.sha256(file.read_bytes()).hexdigest()==SOURCES[filename]
    name='lighthouse_pinned_'+file.stem
    spec=importlib.util.spec_from_file_location(name,file);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

def bind_recovery_gap(daily,recovery):
    def verify_recovered_gap(context):
        assert context.STATE==DAILY_STATE
        return recovery.verify_completed(recovery.Layout())
    daily.verify_recovered_gap=verify_recovered_gap
    return verify_recovered_gap

def main():
    assert os.name=='posix' and os.geteuid()==0;os.umask(0o077)
    daily=load_module('cloudbase_backup_daily_retention_v2c.py');recovery=load_module('recover_cloudbase_remote_absent_v1.py')
    gap=bind_recovery_gap(daily,recovery)
    if sys.argv[1:]==['--verify-recovery']:
        # Unlike the original --verify-only branch, this checks the full gap,
        # including app-UID read-only live ledger evidence, before returning.
        value=gap(SimpleNamespace(STATE=DAILY_STATE))
        print(json.dumps({'state':'verified_gap','version':'cloudbase-daily-recovery-entry-v2d',
              'daily_id':value['daily_id'],'operation_id':value['operation_id'],'external_requests':0,
              'backup_started':False,'failure_history_preserved':True,'budget_refunded':False}));return 0
    return daily.main()

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'state':'stopped','error_code':'DAILY_RECOVERY_ENTRY_REFUSED','backup_started':False}));raise SystemExit(2) from None
