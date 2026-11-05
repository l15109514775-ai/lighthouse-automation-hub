"""One fixed, budgeted HEAD; no upload, GET, refund, failure reset or retry.

Default root plan is offline. Explicit hash apply holds all three production
locks and invokes this same source in one fixed-image app-UID container. A
reserved HEAD cannot be automatically replayed after a crash. A 404 is only
evidence for a later separately reviewed reconciliation, never backup success.
"""
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import argparse,hashlib,json,os,re,sqlite3,stat,subprocess,sys,tempfile,time,tomllib,uuid

VERSION='cloudbase-interrupted-head-v1'
OPERATION='19ceebc3-27c8-42e5-862f-3bb6997ec58c'
DAILY='72dc78ad-9eb1-4d09-8b27-8844b0c87425'
ADAPTER='a2d6469b-b7d1-46d8-8ae2-2cd0ba7fff8a'
IMAGE='sha256:6592ebce93025293ac37e8c990f0f09daa639a0ed95354120a22371d2e9b6d55'
ARCHIVE_BYTES=42761614
ARCHIVE_SHA='e9e4aeead02b3b173761e3767059f95b67703b2ac499efd7026b2637bdc5787d'
CONFIG_SHA='ca354eab3b6eeeb7c3e8cdf418cc5d95ff71b4ccd195d5c3aca8cf5bd6bc46c8'
ADAPTER_INTENT_SHA='cbdb54a380cd3f18ea2372ff2c67d8eefaf96b56943983dceceea9148a04ee7e'
DAILY_SHA='f36f1ad1a6879316c0e6ebc3a80a76c5c2045e4d3e6c472402e222a30cb8b9f5'
BASELINE_SHA='7641d89c98fb99500c94b6d4a1486f8c74e9388dbcea80f79699ef1773dee828'
ALLOWANCE=300000
HEAD_REQUEST=str(uuid.uuid5(uuid.UUID(OPERATION),VERSION))
ROOT=Path('/opt/lighthouse-hub')

class Layout:
    def __init__(self,root=ROOT,ops=Path('/var/lib/lighthouse-backup-ops')):
        self.root,self.ops=root,ops
        self.state=root/'data/cloudbase-backup-state';self.ledger=self.state/'ledger.sqlite3'
        self.archive=root/'backups/cloudbase-staging'/OPERATION/'operational-backup.tar.gz'
        self.adapter_intent=root/'backups/cloudbase-receipts'/(ADAPTER+'.intent.json')
        self.config=root/'config/cloudbase-backup.toml'
        self.daily=ops/'retention-v2b/daily-state';self.latest=self.daily/'latest.json'
        self.history=self.daily/'history'/(DAILY+'.json')
        self.proof=ops/'interrupted-head-v1'/OPERATION
        self.app_proof=self.state/'interrupted-head-v1'/OPERATION
        self.locks=((ops/'daily.lock',0),(self.daily/'daily.lock',0),
                    (self.state/'runner.lock/observer.lock',10001))

def need(value,code):
    if not value:raise ValueError(code)

def wire(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
def sha(raw):return hashlib.sha256(raw).hexdigest()

def safe(path):
    path=Path(path)
    need(path.is_absolute() and path.resolve()==path and not any(p.is_symlink() for p in (path,*path.parents)),
         'HEAD_UNSAFE_PATH')
    return path

def owner(info,uid,private=False):
    need(info.st_uid==uid and not info.st_mode&(0o077 if private else 0o022),'HEAD_OWNER_OR_MODE_CHANGED')

def read(path,uid,maximum=262144):
    safe(path)
    with os.fdopen(os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0)),'rb') as stream:
        before=os.fstat(stream.fileno());owner(before,uid)
        need(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and 0<before.st_size<=maximum,'HEAD_DOCUMENT_INVALID')
        raw=stream.read(maximum+1);after=os.fstat(stream.fileno())
    fields=('st_dev','st_ino','st_size','st_mtime_ns')+(('st_ctime_ns',) if os.name=='posix' else ())
    current=path.stat()
    need(len(raw)==before.st_size and all(getattr(before,k)==getattr(after,k)==getattr(current,k) for k in fields),
         'HEAD_DOCUMENT_CHANGED')
    return raw

def connect(layout,readonly=False):
    safe(layout.ledger);owner(layout.ledger.stat(),10001,True)
    db=sqlite3.connect(layout.ledger.as_uri()+('?mode=ro' if readonly else '?mode=rw'),uri=True,timeout=5,isolation_level=None)
    db.row_factory=sqlite3.Row;db.execute('PRAGMA busy_timeout=5000')
    if readonly:db.execute('PRAGMA query_only=ON')
    else:db.execute('PRAGMA synchronous=FULL')
    return db

def rows(db,sql,args=()):return [dict(row) for row in db.execute(sql,args)]

def original(db):
    op=rows(db,'SELECT * FROM operations WHERE operation_id=?',(OPERATION,))
    plans=rows(db,'SELECT * FROM plans WHERE operation_id=?',(OPERATION,))
    requests=rows(db,'SELECT * FROM requests WHERE operation_id=? AND request_id<>? ORDER BY created_at,request_id',(OPERATION,HEAD_REQUEST))
    need(len(op)==len(plans)==1,'HEAD_ORIGINAL_OPERATION_MISSING')
    value={'operation':op[0],'plan':plans[0],'requests':requests}
    need(sha(wire(value))==BASELINE_SHA,'HEAD_ORIGINAL_BINDING_CHANGED')
    need(op[0]['state'] in ('uploading','upload_unknown') and len(requests)==4 and
         sum(x['method']=='PUT' and x['state'] in ('reserved','unknown') and
             x['reserved_bytes']==ARCHIVE_BYTES+ALLOWANCE for x in requests)==1,'HEAD_ORIGINAL_PUT_CHANGED')
    need(not rows(db,"SELECT operation_id FROM operations WHERE operation_id<>? AND state IN ('preparing','uploading','upload_unknown')",(OPERATION,)),
         'HEAD_ANOTHER_ACTIVE_OPERATION')
    return value

def immutable(layout):
    cfg=read(layout.config,0,65536);need(sha(cfg)==CONFIG_SHA,'HEAD_CONFIG_CHANGED')
    value=tomllib.loads(cfg.decode('utf-8-sig'));limits=value['limits']
    need(limits['daily_network_bytes']==128000000 and limits['monthly_network_bytes']==800000000 and
         limits['daily_requests']==50 and limits['monthly_requests']==250,'HEAD_LIMITS_CHANGED')
    archive=read(layout.archive,10001,50000000)
    need(len(archive)==ARCHIVE_BYTES and sha(archive)==ARCHIVE_SHA,'HEAD_ARCHIVE_CHANGED')
    intent_raw=read(layout.adapter_intent,10001);need(sha(intent_raw)==ADAPTER_INTENT_SHA,'HEAD_ADAPTER_INTENT_CHANGED')
    intent=json.loads(intent_raw)
    need(intent['version']=='cloudbase-backup-v1' and intent['operation_id']==ADAPTER and
         intent['local_archive']==layout.archive.relative_to(layout.root).as_posix() and intent['bytes']==ARCHIVE_BYTES and
         intent['sha256']==ARCHIVE_SHA and intent['remote_verified'] is intent['local_deletion_performed'] is False and
         intent['archive_key']==value['cloudbase']['object_prefix']+'/'+ADAPTER+'/'+ARCHIVE_SHA+'.archive' and
         not layout.adapter_intent.with_name(ADAPTER+'.verified.json').exists(),'HEAD_ADAPTER_BINDING_CHANGED')
    # Prove no second intent claims this local frozen archive; never guess its
    # key from the unrelated runtime operation UUID.
    candidates=list(layout.adapter_intent.parent.glob('*.intent.json'));need(len(candidates)<=1000,'HEAD_INTENT_LIMIT')
    matches=[]
    for file in candidates:
        candidate=json.loads(read(file,10001))
        if candidate.get('local_archive')==intent['local_archive']:matches.append(file)
    need(matches==[layout.adapter_intent],'HEAD_ADAPTER_INTENT_NOT_UNIQUE')
    return {'config_sha256':sha(cfg),'archive_sha256':ARCHIVE_SHA,'archive_bytes':ARCHIVE_BYTES,
            'adapter_intent':{'file':str(layout.adapter_intent),'sha256':sha(intent_raw),'bytes':len(intent_raw)},
            'adapter_operation_id':ADAPTER,'archive_key':intent['archive_key'],'limits':limits}

def plan(layout):
    value=immutable(layout);daily={}
    for name,path in (('latest',layout.latest),('history',layout.history)):
        raw=read(path,0);need(sha(raw)==DAILY_SHA,'HEAD_DAILY_CHANGED');record=json.loads(raw)
        need(record['daily_id']==DAILY and record['state']=='accept_failed' and record['image']==IMAGE and
             'acceptance' not in record,'HEAD_DAILY_NOT_FAILED')
        daily[name]={'file':str(path),'sha256':sha(raw),'bytes':len(raw)}
    db=connect(layout,True)
    try:
        db.execute('BEGIN');baseline=original(db)
        need(not rows(db,'SELECT * FROM requests WHERE request_id=?',(HEAD_REQUEST,)),'HEAD_ALREADY_RESERVED')
    finally:db.close()
    return {'version':VERSION,'operation_id':OPERATION,'daily_id':DAILY,'image':IMAGE,**value,
        'baseline':baseline,'daily':daily,'original_requests_sha256':sha(wire(baseline['requests'])),
        'original_put_requests_sha256':sha(wire([x for x in baseline['requests'] if x['method']=='PUT'])),
        'external_requests':1,'method':'HEAD','puts':0,'gets':0,'deletions':0,'refund_bytes':0,'retry_performed':False}

def sync(folder):
    if os.name=='posix':
        fd=os.open(folder,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)

def directory(folder,uid):
    safe(folder)
    if not folder.exists():
        directory(folder.parent,uid) if not folder.parent.exists() else None
        folder.mkdir(mode=0o700);sync(folder.parent)
    need(folder.is_dir(),'HEAD_DIRECTORY_INVALID');owner(folder.stat(),uid,True)

def record_once(file,value,uid):
    if file.exists():need(json.loads(read(file,uid))==value,'HEAD_RECORD_CONFLICT');return
    safe(file);directory(file.parent,uid);fd,name=tempfile.mkstemp(prefix='.head-',suffix='.partial',dir=file.parent)
    temporary=Path(name)
    try:
        with os.fdopen(fd,'wb') as output:output.write(wire(value));output.flush();os.fsync(output.fileno())
        os.link(temporary,file);sync(file.parent)
    finally:temporary.unlink(missing_ok=True);sync(file.parent)

def reserve(layout,observed,*,clock=time.time):
    need(observed['version']==VERSION and observed['operation_id']==OPERATION and
         all(observed[k]==v for k,v in immutable(layout).items()),'HEAD_PLAN_IMMUTABLE_CHANGED')
    db=connect(layout);now=clock();moment=datetime.fromtimestamp(now,ZoneInfo('Asia/Shanghai'))
    day,month=moment.strftime('%Y-%m-%d'),moment.strftime('%Y-%m')
    try:
        db.execute('BEGIN IMMEDIATE');need(original(db)==observed['baseline'],'HEAD_PLAN_LEDGER_CHANGED')
        need(not rows(db,'SELECT * FROM requests WHERE request_id=?',(HEAD_REQUEST,)),'HEAD_ALREADY_RESERVED')
        planned=observed['baseline']['plan'];used=rows(db,
            'SELECT COUNT(*) calls,COALESCE(SUM(reserved_bytes),0) bytes FROM requests WHERE operation_id=?',(OPERATION,))[0]
        need(used['calls']+1<=planned['maximum_requests'] and used['bytes']+ALLOWANCE<=planned['maximum_network_bytes'],
             'HEAD_OPERATION_PLAN_EXCEEDED')
        limits=observed['limits']
        for field,period,byte_limit,request_limit in (('day',day,limits['daily_network_bytes'],limits['daily_requests']),
                ('month',month,limits['monthly_network_bytes'],limits['monthly_requests'])):
            used=rows(db,'SELECT COUNT(*) calls,COALESCE(SUM(reserved_bytes),0) bytes FROM requests WHERE '+field+'=?',(period,))[0]
            need(used['calls']+1<=request_limit and used['bytes']+ALLOWANCE<=byte_limit,'HEAD_BUDGET_EXCEEDED')
        # This exceptional reservation does not call start(), toggle operation
        # state, overwrite a plan, refund a PUT, or expand an original limit.
        db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,NULL)',
            (HEAD_REQUEST,OPERATION,now,day,month,'HEAD',ALLOWANCE,'reserved'))
        db.commit()
    except BaseException:db.rollback();raise
    finally:db.close()

def signed_head(layout,observed):
    """Credentials and auth headers remain solely inside the fixed child."""
    sys.path.insert(0,str(layout.root))
    from adapters.safe_https import open_request
    from adapters.cloudbase_cos_backup import CloudBaseCOSConfig,CloudBaseCOSStorage
    from services.hub.errors import HubError
    value=tomllib.loads(read(layout.config,0,65536).decode('utf-8-sig'));options=dict(value['cloudbase'])
    options.pop('private_canary_reference');options.pop('private_canary_sha256');options.pop('credential_file')
    options['storage_hosts']=tuple(options['storage_hosts']);cfg=CloudBaseCOSConfig(**options);cfg.validate()
    secret=Path('/run/secrets/cloudbase-backup.json')
    def credentials():
        raw=read(secret,0,16384);result=json.loads(raw)
        need(type(result) is dict and not set(result)-{'SecretId','SecretKey','Token','ExpirationEpoch'},'HEAD_CREDENTIAL_SCHEMA')
        return result
    status={};key=observed['archive_key'];expected='https://'+cfg.host+'/'+key
    def request(url,hosts,*,method='GET',headers=None,body=None,timeout=20):
        need(method=='HEAD' and body is None and url==expected and list(hosts)==[cfg.host],'HEAD_METHOD_OR_DESTINATION_CHANGED')
        connection,response=open_request(url,hosts,method=method,headers=headers,body=None,timeout=timeout)
        status['http_status']=response.status
        code=response.getheader('x-cos-error-code','')
        if isinstance(code,str) and re.fullmatch('[A-Za-z_]{1,80}',code):status['provider_code']=code
        return connection,response
    storage=CloudBaseCOSStorage(cfg,credentials,request=request);result=None;error=None
    try:result=storage.head('cos://'+cfg.bucket+'/'+key,key)
    except HubError as exc:error=exc.code
    except Exception:error='HEAD_REQUEST_FAILED'
    return {**status,'head_result':result,'error_code':error}

def execute_head(layout,observed,*,probe=signed_head,clock=time.time,checkpoint=lambda stage:None):
    digest=sha(wire(observed));intent={'version':VERSION,'state':'head_intent_saved','plan_sha256':digest,'plan':observed}
    intent_file=layout.app_proof/'intent.json';receipt_file=layout.app_proof/'head-receipt.json'
    record_once(intent_file,intent,10001)
    if receipt_file.exists():
        receipt=json.loads(read(receipt_file,10001));need(receipt['plan_sha256']==digest,'HEAD_RECEIPT_CHANGED')
        finish_head(layout,observed,receipt)
        return receipt  # Durable completed HEAD is never issued a second time.
    reserve(layout,observed,clock=clock);checkpoint('after_reserve')
    observation=probe(layout,observed)
    need(type(observation) is dict and set(observation)<= {'http_status','provider_code','head_result','error_code'},'HEAD_UNSAFE_OBSERVATION')
    need(observation.get('http_status') is None or type(observation['http_status']) is int,'HEAD_UNSAFE_OBSERVATION')
    if observation.get('http_status') is not None:need(100<=observation['http_status']<=599,'HEAD_UNSAFE_OBSERVATION')
    for key in ('provider_code','error_code'):
        need(observation.get(key) is None or re.fullmatch('[A-Za-z_]{1,100}',observation[key]),'HEAD_UNSAFE_OBSERVATION')
    result=observation.get('head_result')
    need(result is None or (type(result) is dict and set(result)=={'bytes','method','remote_verified'} and
         type(result['bytes']) is int and 0<=result['bytes']<=50000000 and result['method']=='head_only' and
         result['remote_verified'] is False),'HEAD_UNSAFE_OBSERVATION')
    missing=observation.get('http_status')==404 and observation.get('error_code')=='CLOUDBASE_BACKUP_REMOTE_NOT_FOUND'
    receipt={'version':VERSION,'state':'head_observed','operation_id':OPERATION,'adapter_operation_id':ADAPTER,
        'archive_key':observed['archive_key'],'archive_sha256':ARCHIVE_SHA,'archive_bytes':ARCHIVE_BYTES,
        'request_id':HEAD_REQUEST,'plan_sha256':digest,'observation':observation,'remote_missing_at_check':missing,
        'remote_verified':False,'requests':1,'puts':0,'gets':0,'deletions':0,'refund_bytes':0,'retry_performed':False,
        'original_requests_sha256':observed['original_requests_sha256'],
        'original_put_requests_sha256':observed['original_put_requests_sha256'],'original_operation_modified':False,
        'checked_at':clock()}
    record_once(receipt_file,receipt,10001);checkpoint('after_receipt')
    finish_head(layout,observed,receipt)
    return receipt

def finish_head(layout,observed,receipt):
    need(receipt['version']==VERSION and receipt['operation_id']==OPERATION and receipt['request_id']==HEAD_REQUEST and
         receipt['plan_sha256']==sha(wire(observed)) and receipt['original_requests_sha256']==observed['original_requests_sha256'] and
         receipt['original_put_requests_sha256']==observed['original_put_requests_sha256'] and
         receipt['original_operation_modified'] is False,'HEAD_RECEIPT_CHANGED')
    observation=receipt['observation'];state='done' if observation.get('http_status') in (200,404) else 'error'
    db=connect(layout)
    try:
        db.execute('BEGIN IMMEDIATE');need(original(db)==observed['baseline'],'HEAD_ORIGINAL_CHANGED_DURING_PROBE')
        found=rows(db,'SELECT * FROM requests WHERE request_id=?',(HEAD_REQUEST,))
        need(len(found)==1 and found[0]['operation_id']==OPERATION and found[0]['method']=='HEAD' and
             found[0]['reserved_bytes']==ALLOWANCE,'HEAD_REQUEST_RESULT_CONFLICT')
        if found[0]['state']=='reserved':
            need(db.execute('UPDATE requests SET state=?,error_code=? WHERE request_id=? AND state=?',
                (state,observation.get('error_code'),HEAD_REQUEST,'reserved')).rowcount==1,'HEAD_REQUEST_RESULT_CONFLICT')
        else:need(found[0]['state']==state and found[0]['error_code']==observation.get('error_code'),'HEAD_REQUEST_RESULT_CONFLICT')
        db.commit()
    except BaseException:db.rollback();raise
    finally:db.close()

def command(args,*,timeout=15):
    value=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    need(value.returncode==0,'HEAD_HOST_CHECK_FAILED');return value.stdout.strip()

def guard():
    state=command(['/usr/bin/systemctl','show','lighthouse-cloudbase-backup.service','--property=ActiveState','--value'])
    need(state in ('inactive','failed'),'HEAD_BACKUP_SERVICE_ACTIVE')
    for condition in ('label=org.lighthouse.backup.sidecar','name=^/lighthouse-archive-registry-',
                      'name=^/lighthouse-interrupted-head-'):
        need(not command(['/usr/bin/docker','ps','-q','--filter',condition]),'HEAD_BACKUP_CONTAINER_ACTIVE')

@contextmanager
def locked(layout):
    import fcntl
    opened=[]
    try:
        for file,uid in layout.locks:
            safe(file);fd=os.open(file,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK);opened.append(fd)
            info=os.fstat(fd);owner(info,uid);need(stat.S_ISREG(info.st_mode),'HEAD_LOCK_INVALID')
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally:
        for fd in reversed(opened):os.close(fd)

def container_head(layout,observed):
    need(command(['/usr/bin/docker','image','inspect','-f','{{.Id}}',IMAGE])==IMAGE,'HEAD_IMAGE_CHANGED')
    source=safe(Path(__file__));read(source,0)
    name='lighthouse-interrupted-head-'+str(uuid.uuid4())
    args=['/usr/bin/docker','run','--pull=never','--rm','--init','-i','--name',name,
        '--user','10001:10001','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true',
        '--memory','256m','--memory-swap','256m','--cpus','0.5','--pids-limit','64','--tmpfs','/tmp:rw,size=16m,mode=1777']
    for origin,target,ro in ((layout.config,layout.config,True),(layout.root/'backups',layout.root/'backups',True),
        (layout.state,layout.state,False),(layout.root/'secrets/cloudbase-backup.json',Path('/run/secrets/cloudbase-backup.json'),True),
        (source,Path('/run/interrupted-head.py'),True)):
        safe(origin);args+=['--mount','type=bind,src='+str(origin)+',dst='+str(target)+(',readonly' if ro else '')]
    args+=[IMAGE,'python','/run/interrupted-head.py','--child']
    try:
        result=subprocess.run(args,input=wire(observed),capture_output=True,timeout=90)
        need(len(result.stdout)<=262144 and len(result.stderr)<=65536 and result.returncode==0,'HEAD_CHILD_FAILED')
        return json.loads(result.stdout)
    finally:
        ids=command(['/usr/bin/docker','ps','-q','--filter','name=^/'+name+'$'])
        if ids:command(['/usr/bin/docker','stop','-t','10',name],timeout=20)

def apply(layout,expected,*,host_guard=guard,child=container_head):
    need(re.fullmatch('[a-f0-9]{64}',expected or ''),'HEAD_PLAN_HASH_REQUIRED');host_guard()
    target=layout.proof/'intent.json'
    if target.exists():
        saved=json.loads(read(target,0));observed=saved['plan']
        need(saved=={'version':VERSION,'state':'head_intent_saved','plan_sha256':expected,'plan':observed},'HEAD_ROOT_INTENT_CHANGED')
    else:observed=plan(layout)
    need(sha(wire(observed))==expected,'HEAD_PLAN_CHANGED')
    record_once(target,{'version':VERSION,'state':'head_intent_saved','plan_sha256':expected,'plan':observed},0)
    host_guard();answer=child(layout,observed)
    raw=read(layout.app_proof/'head-receipt.json',10001)
    need(json.loads(raw)==answer and answer['plan_sha256']==expected and answer['operation_id']==OPERATION,'HEAD_CHILD_RECEIPT_CHANGED')
    db=connect(layout,True)
    try:need(original(db)==observed['baseline'],'HEAD_ORIGINAL_NOT_PRESERVED')
    finally:db.close()
    record_once(layout.proof/'receipt.json',answer,0)
    return {**answer,'root_receipt_file':str(layout.proof/'receipt.json'),'original_failure_retained':True,
        'terminal_resolution_performed':False,'daily_latest_relocated':False,'automatic_retry_authorized':False}

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--apply',action='store_true')
    parser.add_argument('--plan-sha256');parser.add_argument('--child',action='store_true');args=parser.parse_args()
    need(os.name=='posix','HEAD_LINUX_REQUIRED');layout=Layout();os.umask(0o077)
    if args.child:
        need(os.getuid()==10001 and not args.apply and args.plan_sha256 is None,'HEAD_CHILD_IDENTITY')
        raw=sys.stdin.buffer.read(262145);need(len(raw)<=262144,'HEAD_PLAN_TOO_LARGE')
        print(json.dumps(execute_head(layout,json.loads(raw))));return
    need(os.geteuid()==0 and args.apply==bool(args.plan_sha256),'HEAD_ROOT_EXPLICIT_APPLY_REQUIRED')
    with locked(layout):
        guard()
        if args.apply:value=apply(layout,args.plan_sha256)
        else:
            observed=plan(layout);value={'state':'planned_no_network','plan_sha256':sha(wire(observed)),'plan':observed,'external_requests':0}
    print(json.dumps(value,separators=(',',':')))

if __name__=='__main__':
    try:main()
    except Exception as exc:
        code=str(exc) if isinstance(exc,ValueError) and re.fullmatch('[A-Z_]{1,100}',str(exc)) else 'HEAD_STOPPED_SAFELY'
        print(json.dumps({'state':'stopped','error_code':code,'automatic_retry_authorized':False}))
        raise SystemExit(2) from None
