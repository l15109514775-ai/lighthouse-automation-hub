"""Reconcile one proven pre-upload failure; default plan, explicit hash apply.

No backup/upload/deletion, ledger mutation, core restart, or arbitrary path CLI.
The failed latest pointer is archived, never relabelled as a successful backup.
"""
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import tempfile


VERSION='cloudbase-preupload-daily-recovery-v1'
DAILY_ID='6cfa6b1d-46db-4ed9-888e-f6564f07d956'
OPERATION_ID='7505562a-3b39-4e9c-897b-a704a05aa6a9'
ACCEPTANCE_ID='a39e7b99-6660-4fba-9f6d-8b466faa2958'
ERROR='CLOUDBASE_BACKUP_EVIDENCE_PATH_REJECTED'
START='2026-11-02T19:30:02.522147+00:00'
END='2026-11-02T19:30:07.393482+00:00'
IMAGE='sha256:6592ebce93025293ac37e8c990f0f09daa639a0ed95354120a22371d2e9b6d55'
PINS={'latest':'166898672baa06cbef9bd9215f1edac7f90fe81c6cc0ed04896b77bb4ef4bed6',
      'history':'166898672baa06cbef9bd9215f1edac7f90fe81c6cc0ed04896b77bb4ef4bed6',
      'legacy':'12b5fa9a4dc5f8244d97f92b6730434ec6400692236d537eca1e3dcdc1e78356',
      'host_status':'0b27fdb9275e957433e53cab4b508a13bda8d693a3231d3b3131a66a822f3ea1',
      'failure':'dcf01e73be04dc67405e6c5425c0e00544bfd18312bdf20154d6432bd9205e93'}
EXPECTED_OPERATION={'operation_id':OPERATION_ID,'created_at':1793647804.5745535,'updated_at':1793647806.045616,
    'state':'failed','archive':None,'receipt':None,'error_code':ERROR,
    'policy_sha256':'8161c4764ea01290b001ce236df512defcfdb3fcc0f59fbeb69533a90a28e63b'}


class Layout:
    def __init__(self, app=Path('/opt/lighthouse-hub'), ops=Path('/var/lib/lighthouse-backup-ops')):
        self.app,self.ops=app,ops
        self.daily=ops/'retention-v2b/daily-state'
        self.recovery=ops/'preupload-recovery-v1'/DAILY_ID
        self.latest=self.daily/'latest.json'
        self.archive=self.recovery/'failed-latest.json'
        self.sources={'history':self.daily/'history'/(DAILY_ID+'.json'),'legacy':ops/'latest.json',
            'host_status':app/'data/cloudbase-backup-state/host-service-status.json',
            'failure':app/'backups/cloudbase-restore-check'/ACCEPTANCE_ID/'acceptance-failure.json'}
        self.ledger=app/'data/cloudbase-backup-state/ledger.sqlite3'
        # resource_guard/exclusive receives a directory and locks observer.lock
        # inside it. Lock the upstream inode, not the runner.lock directory.
        self.locks=((ops/'daily.lock',0),(self.daily/'daily.lock',0),
                    (app/'data/cloudbase-backup-state/runner.lock/observer.lock',10001))


def need(value,code):
    if not value: raise ValueError(code)


def wire(value): return json.dumps(value,sort_keys=True,separators=(',',':')).encode()
def sha(raw): return hashlib.sha256(raw).hexdigest()
def stamp(value): return datetime.fromisoformat(value).timestamp()


def safe(path):
    path=Path(path)
    need(path.is_absolute() and path.resolve()==path and not any(p.is_symlink() for p in (path,*path.parents)),
         'RECOVERY_UNSAFE_PATH')
    return path


def owner(info,uid,private=True):
    need(info.st_uid==uid and not info.st_mode&(0o077 if private else 0o022),'RECOVERY_OWNER_OR_MODE_CHANGED')


def read(path,uid=0):
    path=safe(path)
    flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0)
    with os.fdopen(os.open(path,flags),'rb') as stream:
        before=os.fstat(stream.fileno());owner(before,uid)
        need(stat.S_ISREG(before.st_mode) and 0<before.st_size<=65536,'RECOVERY_DOCUMENT_INVALID')
        raw=stream.read(65537);after=os.fstat(stream.fileno())
    current=path.stat()
    # Windows stat/fstat expose different ctime semantics; the production CLI
    # is Linux-only, where ctime is checked along with identity and mtime.
    fields=('st_dev','st_ino','st_size','st_mtime_ns')+(('st_ctime_ns',) if os.name=='posix' else ())
    need(len(raw)==before.st_size and all(getattr(before,k)==getattr(after,k)==getattr(current,k)
        for k in fields),'RECOVERY_DOCUMENT_CHANGED')
    return json.loads(raw),raw


def guard():
    def command(args):
        p=subprocess.run(args,capture_output=True,text=True,timeout=15)
        need(p.returncode==0,'RECOVERY_HOST_CHECK_FAILED');return p.stdout.strip()
    state=command(['/usr/bin/systemctl','show','lighthouse-cloudbase-backup.service','--property=ActiveState','--value'])
    need(state in ('inactive','failed'),'RECOVERY_BACKUP_SERVICE_ACTIVE')
    for condition in ('label=org.lighthouse.backup.sidecar','name=^/lighthouse-archive-registry-'):
        need(not command(['/usr/bin/docker','ps','-q','--filter',condition]),'RECOVERY_BACKUP_CONTAINER_ACTIVE')


@contextmanager
def locked(layout):
    import fcntl
    opened=[]
    try:
        for path,uid in layout.locks:
            safe(path);fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK);opened.append(fd)
            info=os.fstat(fd);owner(info,uid,private=False)
            need(stat.S_ISREG(info.st_mode),'RECOVERY_LOCK_INVALID')
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally:
        for fd in reversed(opened): os.close(fd)


def ledger_proof(layout):
    safe(layout.ledger);owner(layout.ledger.stat(),10001)
    db=sqlite3.connect(layout.ledger.as_uri()+'?mode=ro',uri=True,isolation_level=None,timeout=3)
    db.row_factory=sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        row=db.execute('SELECT * FROM operations WHERE operation_id=?',(OPERATION_ID,)).fetchone()
        need(row is not None and dict(row)==EXPECTED_OPERATION,'RECOVERY_OPERATION_CHANGED')
        start,end=stamp(START),stamp(END)
        window=db.execute('SELECT operation_id FROM operations WHERE created_at<=? AND updated_at>=?',(end,start)).fetchall()
        need([r[0] for r in window]==[OPERATION_ID],'RECOVERY_OPERATION_NOT_UNIQUE')
        requests=db.execute('SELECT COUNT(*) FROM requests WHERE operation_id=?',(OPERATION_ID,)).fetchone()[0]
        plans=db.execute('SELECT COUNT(*) FROM plans WHERE operation_id=?',(OPERATION_ID,)).fetchone()[0]
        need(requests==plans==0,'RECOVERY_REMOTE_REQUEST_OR_PLAN_EXISTS')
        need(db.execute("SELECT COUNT(*) FROM operations WHERE state IN ('preparing','uploading','upload_unknown')").fetchone()[0]==0,
             'RECOVERY_UNRESOLVED_OPERATION_EXISTS')
        need(db.execute('SELECT COUNT(*) FROM requests WHERE created_at>=?',(start,)).fetchone()[0]==0,
             'RECOVERY_LATER_REMOTE_ACTIVITY_EXISTS')
        # All historical budget rows are fingerprinted; no UPDATE/DELETE/initializer.
        totals={}
        for table in ('operations','requests','plans'):
            rows=[dict(r) for r in db.execute('SELECT * FROM '+table+' ORDER BY 1')]
            totals[table]={'rows':len(rows),'sha256':sha(wire(rows))}
        return {'operation_id':OPERATION_ID,'operation_state':'failed','error_code':ERROR,'requests':0,'plans':0,
                'archive_present':False,'receipt_present':False,'window_unique':True,'ledger_tables':totals}
    finally: db.close()


def plan(layout):
    safe(layout.latest);safe(layout.archive)
    need(not(layout.latest.exists() and layout.archive.exists()),'RECOVERY_TWO_LATEST_COPIES')
    source=layout.latest if layout.latest.exists() else layout.archive
    documents={};hashes={}
    for name,path in {'latest':source,**layout.sources}.items():
        value,raw=read(path,10001 if name=='failure' else 0)
        need(sha(raw)==PINS[name],'RECOVERY_PIN_CHANGED')
        documents[name]=value;hashes[name]=sha(raw)
    daily=documents['latest'];history=documents['history'];host=documents['host_status'];failure=documents['failure']
    need(daily==history and daily['version']=='cloudbase-daily-accept-prune-v1' and daily['daily_id']==DAILY_ID and
         daily['state']=='accept_failed' and daily['error_code']=='ACCEPT_FAILED_OR_UNVERIFIED' and daily['image']==IMAGE and
         daily['created_at']==START and daily['updated_at']==END and not any(k in daily for k in ('acceptance','prune_plan','archive_source_registration_v2')),
         'RECOVERY_DAILY_BINDING_CHANGED')
    need(documents['legacy']['state']=='completed','RECOVERY_LEGACY_NOT_COMPLETED')
    need(host['version']=='cloudbase-backup-host-service-v1e' and host['state']=='failed' and host['service_result']=='exit-code' and
         stamp(END)<=stamp(host['at'])<=stamp(END)+10,'RECOVERY_HOST_FAILURE_CHANGED')
    need(failure['version']=='cloudbase-restore-acceptance-failure-v1' and failure['error_code']==ERROR and
         failure['additional_remote_gets']==failure['local_deletions']==0 and failure['captured_partial_files_retained'] is True and
         EXPECTED_OPERATION['updated_at']<=stamp(failure['at'])<=stamp(END),'RECOVERY_ACCEPTANCE_FAILURE_CHANGED')
    return {'version':VERSION,'daily_id':DAILY_ID,'operation_id':OPERATION_ID,'source_hashes':hashes,
            'daily_window':{'created_at':START,'updated_at':END},'ledger':ledger_proof(layout),
            'action':'archive_failed_latest_allow_existing_no_previous_branch',
            'failure_history_retained':True,'backup_success_claimed':False,'external_requests':0,'ledger_modified':False}


def sync_directory(path):
    if os.name=='posix':
        fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)


def create_directory(path):
    safe(path);owner(path.parent.stat(),0,private=False)
    if not path.exists(): path.mkdir(mode=0o700);sync_directory(path.parent)
    need(path.is_dir(),'RECOVERY_DIRECTORY_INVALID');owner(path.stat(),0)


def record_once(path,value):
    raw=wire(value)
    if path.exists():
        old,_=read(path);need(old==value,'RECOVERY_RECORD_CONFLICT');return
    safe(path);owner(path.parent.stat(),0)
    # A crash before the atomic link can leave only an unreferenced partial;
    # an intent/receipt pathname can never contain a partially written record.
    fd,name=tempfile.mkstemp(prefix='.'+path.name+'.',suffix='.partial',dir=path.parent)
    temporary=Path(name)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        try: os.link(temporary,path)
        except FileExistsError:
            old,_=read(path);need(old==value,'RECOVERY_RECORD_CONFLICT')
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True);sync_directory(path.parent)


def apply(layout,expected_plan_sha256,*,host_guard=guard,checkpoint=lambda stage:None):
    need(re.fullmatch('[a-f0-9]{64}',expected_plan_sha256 or '') is not None,'RECOVERY_EXACT_PLAN_HASH_REQUIRED')
    host_guard();observed=plan(layout)
    need(sha(wire(observed))==expected_plan_sha256,'RECOVERY_PLAN_CHANGED')
    create_directory(layout.recovery.parent);create_directory(layout.recovery)
    intent={'version':VERSION,'state':'relocation_intent_saved','plan_sha256':expected_plan_sha256,'plan':observed}
    intent_path=layout.recovery/'intent.json'
    if not layout.latest.exists():
        prior,_=read(intent_path);need(prior==intent,'RECOVERY_MISSING_RELOCATION_INTENT')
    record_once(intent_path,intent);checkpoint('after_intent')
    host_guard();need(sha(wire(plan(layout)))==expected_plan_sha256,'RECOVERY_PLAN_CHANGED')
    if layout.latest.exists():
        need(not layout.archive.exists(),'RECOVERY_ARCHIVE_ALREADY_EXISTS')
        need(layout.latest.parent.stat().st_dev==layout.recovery.stat().st_dev,'RECOVERY_CROSS_DEVICE_REFUSED')
        os.rename(layout.latest,layout.archive)
        sync_directory(layout.latest.parent);sync_directory(layout.recovery)
    checkpoint('after_relocation')
    need(not layout.latest.exists() and sha(read(layout.archive)[1])==PINS['latest'] and
         sha(read(layout.sources['history'])[1])==PINS['history'],'RECOVERY_RELOCATION_NOT_VERIFIED')
    need(ledger_proof(layout)==observed['ledger'],'RECOVERY_LEDGER_CHANGED')
    receipt={'version':VERSION,'state':'preupload_failure_reconciled','daily_id':DAILY_ID,'operation_id':OPERATION_ID,
             'plan_sha256':expected_plan_sha256,'active_latest_absent':True,'history_preserved':True,
             'ledger_modified':False,'external_requests':0,'core_services_restarted':False,'backup_success_claimed':False}
    record_once(layout.recovery/'receipt.json',receipt)
    return receipt


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true');parser.add_argument('--plan-sha256')
    args=parser.parse_args()
    need(os.name=='posix' and os.geteuid()==0,'RECOVERY_ROOT_LINUX_REQUIRED')
    need(args.apply==bool(args.plan_sha256),'RECOVERY_APPLY_HASH_PAIR_REQUIRED')
    layout=Layout()
    with locked(layout):
        guard()
        if args.apply: result=apply(layout,args.plan_sha256)
        else:
            value=plan(layout);result={'state':'planned_no_changes','plan_sha256':sha(wire(value)),'plan':value}
    print(json.dumps(result,separators=(',',':')))


if __name__=='__main__':
    try: main()
    except Exception as error:
        code=str(error) if isinstance(error,ValueError) and re.fullmatch('[A-Z_]{1,100}',str(error)) else 'RECOVERY_REFUSED'
        print(json.dumps({'state':'stopped','error_code':code,'external_requests':0,'core_services_restarted':False}))
        raise SystemExit(1) from None
