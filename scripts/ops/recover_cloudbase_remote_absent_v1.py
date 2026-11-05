"""Resolve one proven absent interrupted PUT; default offline plan.

Keeps original requests, reservations, plans and failed daily history. No network,
backup, deletion, budget change or service restart. Root journals the transition;
the exact SQLite UPDATE runs as the existing app UID to preserve WAL ownership.
"""
from contextlib import contextmanager
from pathlib import Path
import argparse,hashlib,json,math,os,re,sqlite3,stat,subprocess,sys,tempfile,time,uuid

VERSION='cloudbase-remote-absent-recovery-v1'
HEAD_VERSION='cloudbase-interrupted-head-v1'
HEAD_PLAN_SHA='d0e90e5f2e834615520151b1e429ff4329a6bebcf5376be6dc490aea4e6e91fd'
OPERATION='19ceebc3-27c8-42e5-862f-3bb6997ec58c'
DAILY='72dc78ad-9eb1-4d09-8b27-8844b0c87425'
ADAPTER='a2d6469b-b7d1-46d8-8ae2-2cd0ba7fff8a'
IMAGE='sha256:6592ebce93025293ac37e8c990f0f09daa639a0ed95354120a22371d2e9b6d55'
BASELINE_SHA='7641d89c98fb99500c94b6d4a1486f8c74e9388dbcea80f79699ef1773dee828'
DAILY_SHA='f36f1ad1a6879316c0e6ebc3a80a76c5c2045e4d3e6c472402e222a30cb8b9f5'
ADAPTER_INTENT_SHA='cbdb54a380cd3f18ea2372ff2c67d8eefaf96b56943983dceceea9148a04ee7e'
ARCHIVE_SHA='e9e4aeead02b3b173761e3767059f95b67703b2ac499efd7026b2637bdc5787d'
ARCHIVE_BYTES=42761614
HEAD_REQUEST=str(uuid.uuid5(uuid.UUID(OPERATION),HEAD_VERSION))
TERMINAL='resolved_remote_absent'
ERROR='CLOUDBASE_BACKUP_INTERRUPTED_PUT_CONFIRMED_ABSENT'

class Layout:
    def __init__(self,root=Path('/opt/lighthouse-hub'),ops=Path('/var/lib/lighthouse-backup-ops')):
        self.root,self.ops=root,ops
        self.state=root/'data/cloudbase-backup-state';self.ledger=self.state/'ledger.sqlite3'
        self.daily=ops/'retention-v2b/daily-state';self.latest=self.daily/'latest.json'
        self.history=self.daily/'history'/(DAILY+'.json')
        self.head=ops/'interrupted-head-v1'/OPERATION
        self.app_head=self.state/'interrupted-head-v1'/OPERATION
        self.adapter=root/'backups/cloudbase-receipts'/(ADAPTER+'.intent.json')
        self.proof=ops/'remote-absent-recovery-v1'/OPERATION
        self.archive=self.proof/'failed-latest.json'
        self.locks=((ops/'daily.lock',0),(self.daily/'daily.lock',0),(self.state/'runner.lock/observer.lock',10001))

def need(value,code):
    if not value:raise ValueError(code)
def wire(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
def sha(raw):return hashlib.sha256(raw).hexdigest()
def safe(path):
    path=Path(path)
    need(path.is_absolute() and path.resolve()==path and not any(p.is_symlink() for p in (path,*path.parents)),'ABSENT_UNSAFE_PATH')
    return path
def owner(info,uid,private=False):
    need(info.st_uid==uid and not info.st_mode&(0o077 if private else 0o022),'ABSENT_OWNER_OR_MODE_CHANGED')
def read(path,uid=0,maximum=1_048_576):
    safe(path)
    with os.fdopen(os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0)),'rb') as stream:
        before=os.fstat(stream.fileno());owner(before,uid)
        need(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and 0<before.st_size<=maximum,'ABSENT_DOCUMENT_INVALID')
        raw=stream.read(maximum+1);after=os.fstat(stream.fileno())
    fields=('st_dev','st_ino','st_size','st_mtime_ns')+(('st_ctime_ns',) if os.name=='posix' else ())
    current=path.stat();need(len(raw)==before.st_size and all(getattr(before,k)==getattr(after,k)==getattr(current,k) for k in fields),'ABSENT_DOCUMENT_CHANGED')
    return raw
def document(path,uid=0):
    raw=read(path,uid);return json.loads(raw),{'file':str(path),'sha256':sha(raw),'bytes':len(raw)}
def connect(layout,readonly=True):
    safe(layout.ledger);owner(layout.ledger.stat(),10001,True)
    db=sqlite3.connect(layout.ledger.as_uri()+('?mode=ro' if readonly else '?mode=rw'),uri=True,timeout=5,isolation_level=None)
    db.row_factory=sqlite3.Row;db.execute('PRAGMA busy_timeout=5000')
    db.execute('PRAGMA query_only=ON' if readonly else 'PRAGMA synchronous=FULL');return db
def rows(db,sql,args=()):return [dict(r) for r in db.execute(sql,args)]

def evidence(layout):
    root_intent,ri=document(layout.head/'intent.json');app_intent,ai=document(layout.app_head/'intent.json',10001)
    root_head,rh=document(layout.head/'receipt.json');app_head,ah=document(layout.app_head/'head-receipt.json',10001)
    plan=root_intent['plan'];digest=sha(wire(plan));baseline=plan['baseline']
    need(digest==HEAD_PLAN_SHA,'ABSENT_REAL_HEAD_PLAN_CHANGED')
    need(root_intent==app_intent=={'version':HEAD_VERSION,'state':'head_intent_saved','plan_sha256':digest,'plan':plan},'ABSENT_HEAD_INTENT_CHANGED')
    need(plan['operation_id']==OPERATION and plan['daily_id']==DAILY and plan['adapter_operation_id']==ADAPTER and plan['image']==IMAGE,'ABSENT_HEAD_BINDING_CHANGED')
    need(sha(wire(baseline))==BASELINE_SHA and baseline['operation']['operation_id']==OPERATION,'ABSENT_BASELINE_CHANGED')
    need(root_head==app_head and root_head['version']==HEAD_VERSION and root_head['state']=='head_observed' and
         root_head['operation_id']==OPERATION and root_head['adapter_operation_id']==ADAPTER and
         root_head['plan_sha256']==digest and root_head['request_id']==HEAD_REQUEST,'ABSENT_HEAD_RECEIPT_CHANGED')
    observation=root_head['observation']
    need(observation.get('http_status')==404 and observation.get('error_code')=='CLOUDBASE_BACKUP_REMOTE_NOT_FOUND' and
         observation.get('head_result') is None and root_head['remote_missing_at_check'] is True,'ABSENT_ACTUAL_404_REQUIRED')
    need(root_head['remote_verified'] is root_head['retry_performed'] is root_head['original_operation_modified'] is False and
         root_head['requests']==1 and all(root_head[k]==0 for k in ('puts','gets','deletions','refund_bytes')),'ABSENT_HEAD_SCOPE_CHANGED')
    checked=root_head['checked_at'];need(type(checked) in (int,float) and math.isfinite(checked) and
         baseline['operation']['updated_at']<=checked<=time.time()+5,'ABSENT_HEAD_TIME_INVALID')
    original_requests=sha(wire(baseline['requests']));puts=sha(wire([r for r in baseline['requests'] if r['method']=='PUT']))
    need(original_requests==plan['original_requests_sha256']==root_head['original_requests_sha256'] and
         puts==plan['original_put_requests_sha256']==root_head['original_put_requests_sha256'],'ABSENT_ORIGINAL_REQUESTS_CHANGED')
    adapter,ad=document(layout.adapter,10001)
    need(ad['sha256']==ADAPTER_INTENT_SHA and plan['adapter_intent']==ad and adapter['operation_id']==ADAPTER and
         adapter['archive_key']==plan['archive_key']==root_head['archive_key'] and
         adapter['sha256']==plan['archive_sha256']==root_head['archive_sha256']==ARCHIVE_SHA and
         adapter['bytes']==plan['archive_bytes']==root_head['archive_bytes']==ARCHIVE_BYTES and
         adapter['remote_verified'] is adapter['local_deletion_performed'] is False and
         not layout.adapter.with_name(ADAPTER+'.verified.json').exists(),'ABSENT_ADAPTER_CHANGED')
    need(not(layout.latest.exists() and layout.archive.exists()),'ABSENT_TWO_LATEST_COPIES')
    failed,fd=document(layout.latest if layout.latest.exists() else layout.archive);history,hd=document(layout.history)
    need(fd['sha256']==hd['sha256']==DAILY_SHA and failed==history and failed['daily_id']==DAILY and
         failed['state']=='accept_failed' and failed['error_code']=='ACCEPT_FAILED_OR_UNVERIFIED' and failed['image']==IMAGE and
         not any(k in failed for k in ('acceptance','prune_plan','archive_source_registration_v2')),'ABSENT_FAILED_DAILY_CHANGED')
    need(plan['daily']['latest']['sha256']==plan['daily']['history']['sha256']==DAILY_SHA,'ABSENT_HEAD_DAILY_CHANGED')
    return {'head_plan_sha256':digest,'head_root_intent':ri,'head_app_intent':ai,'head_root_receipt':rh,'head_receipt':ah,
            'adapter_intent':ad,'baseline':baseline,'checked_at':checked,'failed_latest_sha256':DAILY_SHA,'history_sha256':DAILY_SHA}

def ledger_view(db,proof,*,allow_terminal=False):
    baseline=proof['baseline'];need(sha(wire(baseline))==BASELINE_SHA,'ABSENT_BASELINE_CHANGED')
    found=rows(db,'SELECT * FROM operations WHERE operation_id=?',(OPERATION,))
    need(len(found)==1,'ABSENT_OPERATION_MISSING');current=found[0];expected=baseline['operation']
    terminal=current['state']==TERMINAL
    if terminal:
        need(allow_terminal and current['error_code']==ERROR and type(current['updated_at']) in (int,float) and
             math.isfinite(current['updated_at']) and proof['checked_at']<=current['updated_at']<=time.time()+5 and
             {k:v for k,v in current.items() if k not in ('state','error_code','updated_at')}==
             {k:v for k,v in expected.items() if k not in ('state','error_code','updated_at')},'ABSENT_TERMINAL_NOT_BOUND')
    else:need(current==expected,'ABSENT_OPERATION_CHANGED')
    request_rows=rows(db,'SELECT * FROM requests ORDER BY request_id');plans=rows(db,'SELECT * FROM plans ORDER BY operation_id')
    original=[r for r in request_rows if r['operation_id']==OPERATION and r['request_id']!=HEAD_REQUEST]
    original.sort(key=lambda r:(r['created_at'],r['request_id']))
    need(original==baseline['requests'] and [r for r in plans if r['operation_id']==OPERATION]==[baseline['plan']],'ABSENT_ORIGINAL_RESERVATIONS_CHANGED')
    head=[r for r in request_rows if r['request_id']==HEAD_REQUEST]
    need(len(head)==1 and head[0]['operation_id']==OPERATION and head[0]['method']=='HEAD' and head[0]['reserved_bytes']==300000 and
         head[0]['state']=='done' and head[0]['error_code']=='CLOUDBASE_BACKUP_REMOTE_NOT_FOUND' and
         expected['updated_at']<=head[0]['created_at']<=proof['checked_at'],'ABSENT_BUDGETED_HEAD_NOT_COMPLETED')
    operations=rows(db,'SELECT * FROM operations ORDER BY operation_id')
    need(not any(r['operation_id']!=OPERATION and r['state'] in ('preparing','uploading','upload_unknown') for r in operations),'ABSENT_ANOTHER_UNRESOLVED_OPERATION')
    normalized=[expected if r['operation_id']==OPERATION else r for r in operations]
    return {'requests':{'rows':len(request_rows),'sha256':sha(wire(request_rows))},
            'plans':{'rows':len(plans),'sha256':sha(wire(plans))},'normalized_operations_sha256':sha(wire(normalized)),
            'operation':current,'terminal':terminal}

def local_ledger_view(layout,proof,allow_terminal):
    db=connect(layout)
    try:db.execute('BEGIN');return ledger_view(db,proof,allow_terminal=allow_terminal)
    finally:db.close()

def ledger_snapshot(layout,proof,allow_terminal):
    # Even a read-only WAL connection may create -shm. Root never opens the
    # live app ledger; use app UID for both inspection and the exact UPDATE.
    if os.name=='posix' and os.geteuid()==0:
        return child_action({'action':'inspect','proof':proof,'allow_terminal':allow_terminal})
    return local_ledger_view(layout,proof,allow_terminal)

def plan(layout):
    proof=evidence(layout);intent_path=layout.proof/'intent.json'
    saved=json.loads(read(intent_path)) if intent_path.exists() else None
    ledger=ledger_snapshot(layout,proof,saved is not None)
    value={'version':VERSION,'operation_id':OPERATION,'daily_id':DAILY,'evidence':proof,
           'ledger_unchanged':{k:ledger[k] for k in ('requests','plans','normalized_operations_sha256')},
           'terminal_state':TERMINAL,'terminal_error_code':ERROR,'old_failure_preserved':True,'refund_bytes':0,
           'external_requests':0,'puts':0,'gets':0,'deletions':0,'retry_performed':False,'backup_success_claimed':False}
    if saved:need(saved=={'version':VERSION,'state':'recovery_intent_saved','plan_sha256':sha(wire(value)),'plan':value},'ABSENT_RECOVERY_INTENT_CHANGED')
    return value

def transition(layout,observed,*,clock=time.time):
    need(observed['version']==VERSION and observed['operation_id']==OPERATION and observed['daily_id']==DAILY and
         sha(wire(observed['evidence']['baseline']))==BASELINE_SHA,'ABSENT_TRANSITION_PLAN_INVALID')
    db=connect(layout,False)
    try:
        db.execute('BEGIN IMMEDIATE');before=ledger_view(db,observed['evidence'],allow_terminal=True)
        need({k:before[k] for k in observed['ledger_unchanged']}==observed['ledger_unchanged'],'ABSENT_LEDGER_CHANGED')
        if not before['terminal']:
            need(db.execute('UPDATE operations SET state=?,error_code=?,updated_at=? WHERE operation_id=? AND state=?',
                 (TERMINAL,ERROR,clock(),OPERATION,before['operation']['state'])).rowcount==1,'ABSENT_OPERATION_UPDATE_CONFLICT')
        after=ledger_view(db,observed['evidence'],allow_terminal=True)
        need(after['terminal'] and {k:after[k] for k in observed['ledger_unchanged']}==observed['ledger_unchanged'],'ABSENT_RESERVATIONS_NOT_PRESERVED')
        db.commit()
        return {'version':VERSION,'operation_id':OPERATION,'state':TERMINAL,'operation':after['operation'],
                'original_operation_sha256':sha(wire(observed['evidence']['baseline']['operation'])),
                'ledger_unchanged':observed['ledger_unchanged'],'refund_bytes':0,'external_requests':0}
    except BaseException:db.rollback();raise
    finally:db.close()

def sync(folder):
    if os.name=='posix':
        fd=os.open(folder,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
def directory(folder):
    safe(folder)
    if not folder.exists():
        owner(folder.parent.stat(),0);folder.mkdir(mode=0o700);sync(folder.parent)
    owner(folder.stat(),0,True);need(folder.is_dir(),'ABSENT_DIRECTORY_INVALID')
def record_once(file,value):
    if file.exists():need(json.loads(read(file))==value,'ABSENT_RECORD_CONFLICT');return
    safe(file);owner(file.parent.stat(),0,True);fd,name=tempfile.mkstemp(prefix='.absent-',suffix='.partial',dir=file.parent)
    temporary=Path(name)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(wire(value));stream.flush();os.fsync(stream.fileno())
        os.link(temporary,file);sync(file.parent)
    finally:temporary.unlink(missing_ok=True);sync(file.parent)
def command(args):
    result=subprocess.run(args,capture_output=True,text=True,timeout=20)
    need(result.returncode==0,'ABSENT_HOST_CHECK_FAILED');return result.stdout.strip()
def guard():
    need(command(['/usr/bin/systemctl','show','lighthouse-cloudbase-backup.service','--property=ActiveState','--value']) in ('inactive','failed'),'ABSENT_SERVICE_ACTIVE')
    for condition in ('label=org.lighthouse.backup.sidecar','name=^/lighthouse-archive-registry-','name=^/lighthouse-interrupted-head-'):
        need(not command(['/usr/bin/docker','ps','-q','--filter',condition]),'ABSENT_CONTAINER_ACTIVE')
@contextmanager
def locked(layout):
    import fcntl
    descriptors=[]
    try:
        for path,uid in layout.locks:
            safe(path);fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK);descriptors.append(fd)
            info=os.fstat(fd);owner(info,uid);need(stat.S_ISREG(info.st_mode),'ABSENT_LOCK_INVALID')
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally:
        for fd in reversed(descriptors):os.close(fd)
def child_action(value):
    source=safe(Path(__file__));read(source)
    def as_app():os.setgroups([]);os.setgid(10001);os.setuid(10001)
    result=subprocess.run(['/usr/bin/python3',str(source),'--child-ledger'],input=wire(value),capture_output=True,timeout=20,
                          preexec_fn=as_app,env={'PATH':'/usr/bin:/bin','HOME':'/tmp','PYTHONNOUSERSITE':'1','PYTHONDONTWRITEBYTECODE':'1'})
    need(result.returncode==0 and len(result.stdout)<=1_048_576 and len(result.stderr)<=65536,'ABSENT_LEDGER_CHILD_FAILED')
    return json.loads(result.stdout)
def child_transition(layout,observed):return child_action({'action':'transition','observed':observed})

def verify_completed(layout,*,require_gap=True):
    """Read-only durable closure for the new daily entry; never initializes a DB."""
    intent=json.loads(read(layout.proof/'intent.json'));observed=intent['plan'];digest=sha(wire(observed))
    need(intent=={'version':VERSION,'state':'recovery_intent_saved','plan_sha256':digest,'plan':observed},'ABSENT_RECOVERY_INTENT_CHANGED')
    need(observed['version']==VERSION and observed['operation_id']==OPERATION and observed['daily_id']==DAILY and
         observed['evidence']==evidence(layout),'ABSENT_RECOVERY_EVIDENCE_CHANGED')
    original=json.loads(read(layout.proof/'original-operation.json'))
    need(original==observed['evidence']['baseline'],'ABSENT_ORIGINAL_SNAPSHOT_CHANGED')
    transition_raw=read(layout.proof/'ledger-transition.json');resolved=json.loads(transition_raw)
    need(resolved['version']==VERSION and resolved['operation_id']==OPERATION and resolved['state']==TERMINAL and
         resolved['operation']['state']==TERMINAL and resolved['operation']['error_code']==ERROR and
         resolved['ledger_unchanged']==observed['ledger_unchanged'] and resolved['refund_bytes']==resolved['external_requests']==0,
         'ABSENT_TERMINAL_RECEIPT_CHANGED')
    live=ledger_snapshot(layout,observed['evidence'],True)
    need(live['terminal'] and live['operation']==resolved['operation'] and
         {k:live[k] for k in observed['ledger_unchanged']}==observed['ledger_unchanged'],'ABSENT_TERMINAL_LEDGER_CHANGED')
    receipt=json.loads(read(layout.proof/'receipt.json'))
    expected={'version':VERSION,'state':'remote_absent_reconciled','operation_id':OPERATION,'daily_id':DAILY,'plan_sha256':digest,
              'head_receipt':observed['evidence']['head_receipt'],'ledger_transition_sha256':sha(transition_raw),
              'failed_latest_sha256':DAILY_SHA,'history_sha256':DAILY_SHA,'ledger_terminal_state':TERMINAL,
              'active_latest_absent':True,'history_preserved':True,'original_operation_preserved':True,'no_refund':True,
              'puts':0,'gets':0,'deletions':0,'external_requests':0,'retry_performed':False,'backup_success_claimed':False}
    need(receipt==expected,'ABSENT_FINAL_RECEIPT_CHANGED')
    if require_gap:need(not layout.latest.exists() and sha(read(layout.archive))==DAILY_SHA,'ABSENT_LATEST_NOT_RELOCATED')
    return receipt

def apply(layout,expected,*,host_guard=guard,ledger_mutator=child_transition,checkpoint=lambda stage:None):
    need(re.fullmatch('[a-f0-9]{64}',expected or ''),'ABSENT_PLAN_HASH_REQUIRED');host_guard();observed=plan(layout)
    need(sha(wire(observed))==expected,'ABSENT_PLAN_CHANGED')
    directory(layout.proof.parent);directory(layout.proof)
    record_once(layout.proof/'intent.json',{'version':VERSION,'state':'recovery_intent_saved','plan_sha256':expected,'plan':observed})
    record_once(layout.proof/'original-operation.json',observed['evidence']['baseline']);checkpoint('after_intent')
    host_guard();need(sha(wire(plan(layout)))==expected,'ABSENT_PLAN_CHANGED')
    resolved=ledger_mutator(layout,observed);record_once(layout.proof/'ledger-transition.json',resolved);checkpoint('after_ledger')
    need(sha(wire(plan(layout)))==expected,'ABSENT_PLAN_CHANGED')
    if layout.latest.exists():
        need(not layout.archive.exists() and layout.latest.parent.stat().st_dev==layout.proof.stat().st_dev,'ABSENT_RELOCATION_REFUSED')
        os.rename(layout.latest,layout.archive);sync(layout.daily);sync(layout.proof)
    checkpoint('after_relocation')
    need(not layout.latest.exists() and sha(read(layout.archive))==sha(read(layout.history))==DAILY_SHA,'ABSENT_HISTORY_NOT_PRESERVED')
    value={'version':VERSION,'state':'remote_absent_reconciled','operation_id':OPERATION,'daily_id':DAILY,'plan_sha256':expected,
           'head_receipt':observed['evidence']['head_receipt'],'ledger_transition_sha256':sha(read(layout.proof/'ledger-transition.json')),
           'failed_latest_sha256':DAILY_SHA,'history_sha256':DAILY_SHA,'ledger_terminal_state':TERMINAL,
           'active_latest_absent':True,'history_preserved':True,'original_operation_preserved':True,'no_refund':True,
           'puts':0,'gets':0,'deletions':0,'external_requests':0,'retry_performed':False,'backup_success_claimed':False}
    record_once(layout.proof/'receipt.json',value);return verify_completed(layout)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--apply',action='store_true');p.add_argument('--plan-sha256')
    p.add_argument('--self-sha256');p.add_argument('--child-ledger',action='store_true');a=p.parse_args();os.umask(0o077)
    need(os.name=='posix','ABSENT_LINUX_REQUIRED');layout=Layout()
    if a.child_ledger:
        need(os.getuid()==10001 and not a.apply and a.plan_sha256 is a.self_sha256 is None,'ABSENT_CHILD_IDENTITY')
        raw=sys.stdin.buffer.read(1_048_577);need(len(raw)<=1_048_576,'ABSENT_CHILD_INPUT_LIMIT')
        value=json.loads(raw)
        if value.get('action')=='inspect':
            need(set(value)=={'action','proof','allow_terminal'} and type(value['allow_terminal']) is bool,'ABSENT_CHILD_ACTION_INVALID')
            result=local_ledger_view(layout,value['proof'],value['allow_terminal'])
        else:
            need(set(value)=={'action','observed'} and value['action']=='transition','ABSENT_CHILD_ACTION_INVALID')
            result=transition(layout,value['observed'])
        print(json.dumps(result));return
    need(os.geteuid()==0 and a.apply==bool(a.plan_sha256) and sha(read(Path(__file__)))==a.self_sha256,'ABSENT_ROOT_SELF_PIN_REQUIRED')
    with locked(layout):
        guard()
        if a.apply:value=apply(layout,a.plan_sha256)
        else:
            observed=plan(layout);value={'state':'planned_no_changes','plan_sha256':sha(wire(observed)),'plan':observed,'external_requests':0}
    print(json.dumps(value,separators=(',',':')))
if __name__=='__main__':
    try:main()
    except Exception as exc:
        code=str(exc) if isinstance(exc,ValueError) and re.fullmatch('[A-Z_]{1,100}',str(exc)) else 'ABSENT_RECOVERY_STOPPED'
        print(json.dumps({'state':'stopped','error_code':code,'retry_performed':False,'backup_success_claimed':False}));raise SystemExit(2) from None
