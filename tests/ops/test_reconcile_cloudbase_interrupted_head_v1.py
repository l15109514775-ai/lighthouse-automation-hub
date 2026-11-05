"""Offline owned SQLite/file fixtures; no Docker, credentials or provider calls."""
from pathlib import Path
from types import SimpleNamespace
import json,os,sqlite3,stat
import pytest
from scripts.ops import reconcile_cloudbase_interrupted_head_v1 as m

NOW=1793704310
REAL_OWNER=m.owner

def put(file,value):
    file.parent.mkdir(parents=True,exist_ok=True)
    file.write_bytes(value if isinstance(value,bytes) else m.wire(value));file.chmod(0o600)

@pytest.fixture
def owned(tmp_path,monkeypatch):
    layout=m.Layout(tmp_path/'app',tmp_path/'ops')
    # Explicit host-ownership seam for Windows fixtures; the real predicate has
    # independent cases below. All path/hash/schema/SQLite/tar-free logic runs.
    monkeypatch.setattr(m,'owner',lambda info,uid,private=False:None)
    archive=b'Owned frozen archive; not an actual production upload'
    monkeypatch.setattr(m,'ARCHIVE_BYTES',len(archive));monkeypatch.setattr(m,'ARCHIVE_SHA',m.sha(archive))
    put(layout.archive,archive)
    config=b'''[cloudbase]
object_prefix="lighthouse-hub/backups/v1"
[limits]
daily_network_bytes=128000000
monthly_network_bytes=800000000
daily_requests=50
monthly_requests=250
'''
    put(layout.config,config);monkeypatch.setattr(m,'CONFIG_SHA',m.sha(config))
    intent={'version':'cloudbase-backup-v1','operation_id':m.ADAPTER,'bytes':len(archive),'sha256':m.ARCHIVE_SHA,
        'local_archive':layout.archive.relative_to(layout.root).as_posix(),'remote_verified':False,
        'local_deletion_performed':False,'archive_key':'lighthouse-hub/backups/v1/'+m.ADAPTER+'/'+m.ARCHIVE_SHA+'.archive'}
    put(layout.adapter_intent,intent);monkeypatch.setattr(m,'ADAPTER_INTENT_SHA',m.sha(layout.adapter_intent.read_bytes()))
    daily={'daily_id':m.DAILY,'state':'accept_failed','image':m.IMAGE}
    put(layout.latest,daily);put(layout.history,daily);monkeypatch.setattr(m,'DAILY_SHA',m.sha(m.wire(daily)))
    layout.state.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(layout.ledger) as db:
        db.row_factory=sqlite3.Row
        db.executescript('''CREATE TABLE operations(operation_id TEXT PRIMARY KEY,created_at REAL NOT NULL,
            updated_at REAL NOT NULL,state TEXT NOT NULL,archive TEXT,receipt TEXT,error_code TEXT,policy_sha256 TEXT NOT NULL);
            CREATE TABLE requests(request_id TEXT PRIMARY KEY,operation_id TEXT NOT NULL,created_at REAL NOT NULL,
            day TEXT NOT NULL,month TEXT NOT NULL,method TEXT NOT NULL,reserved_bytes INTEGER NOT NULL,state TEXT NOT NULL,error_code TEXT);
            CREATE TABLE plans(operation_id TEXT PRIMARY KEY,created_at REAL NOT NULL,maximum_network_bytes INTEGER NOT NULL,maximum_requests INTEGER NOT NULL);''')
        db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?)',(m.OPERATION,NOW-60,NOW-55,'uploading',str(layout.archive),None,None,m.CONFIG_SHA))
        db.execute('INSERT INTO plans VALUES(?,?,?,?)',(m.OPERATION,NOW-55,2*len(archive)+3000000,7))
        for n in range(4):
            db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?)',(str(n),m.OPERATION,NOW-54+n,
                '2026-11-03','2026-11','GET' if n<3 else 'PUT',m.ALLOWANCE+(0 if n<3 else len(archive)),
                'done' if n<3 else 'reserved',None))
        db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?)',('old','historical',NOW-999,'2026-11-03','2026-11','GET',765432,'done',None))
        baseline={'operation':m.rows(db,'SELECT * FROM operations')[0],'plan':m.rows(db,'SELECT * FROM plans')[0],
            'requests':m.rows(db,'SELECT * FROM requests WHERE operation_id=? ORDER BY created_at,request_id',(m.OPERATION,))}
    layout.ledger.chmod(0o600);monkeypatch.setattr(m,'BASELINE_SHA',m.sha(m.wire(baseline)))
    return SimpleNamespace(layout=layout,baseline=baseline,observed=m.plan(layout),calls=[])

def probe(f,status=404,error='CLOUDBASE_BACKUP_REMOTE_NOT_FOUND',result=None):
    def run(layout,observed):
        f.calls.append((status,error));return {'http_status':status,'error_code':error,'head_result':result}
    return run

def run(f,**kwargs):return m.execute_head(f.layout,f.observed,clock=lambda:NOW,probe=probe(f),**kwargs)

def test_plan_is_offline_and_uses_adapter_uuid_not_runtime_uuid(owned):
    f=owned;before=f.layout.ledger.read_bytes();value=m.plan(f.layout)
    assert value['archive_key'].split('/')[-2]==m.ADAPTER and m.ADAPTER!=m.OPERATION
    assert value['puts']==value['gets']==value['deletions']==value['refund_bytes']==0
    assert f.layout.ledger.read_bytes()==before and not f.layout.proof.exists() and not f.calls

def test_exact_404_records_one_head_without_changing_original_put_operation_or_failed_daily(owned):
    f=owned;daily=f.layout.latest.read_bytes();history=f.layout.history.read_bytes();answer=run(f)
    with m.connect(f.layout,True) as db:
        assert m.original(db)==f.baseline
        head=m.rows(db,'SELECT * FROM requests WHERE request_id=?',(m.HEAD_REQUEST,))[0]
        assert head['method']=='HEAD' and head['state']=='done' and head['reserved_bytes']==m.ALLOWANCE
        assert m.rows(db,'SELECT reserved_bytes FROM requests WHERE request_id=?',('old',))[0]['reserved_bytes']==765432
    assert answer['remote_missing_at_check'] is True and answer['remote_verified'] is False
    assert answer['original_operation_modified'] is False and len(f.calls)==1
    assert f.layout.latest.read_bytes()==daily and f.layout.history.read_bytes()==history
    assert json.loads((f.layout.app_proof/'head-receipt.json').read_bytes())==answer

def test_second_execution_returns_same_durable_result_without_another_head(owned):
    f=owned;first=run(f);assert run(f)==first and len(f.calls)==1
    with m.connect(f.layout,True) as db:assert len(m.rows(db,'SELECT * FROM requests WHERE request_id=?',(m.HEAD_REQUEST,)))==1

@pytest.mark.parametrize('status,error,result',[(200,None,{'bytes':123,'method':'head_only','remote_verified':False}),
    (403,'CLOUDBASE_BACKUP_AUTH_EXPIRED',None),(404,'DIFFERENT_ERROR',None),
    (429,'CLOUDBASE_BACKUP_HTTP_REJECTED',None),(500,'CLOUDBASE_BACKUP_HTTP_REJECTED',None),
    (None,'CLOUDBASE_BACKUP_NETWORK_ERROR',None)])
def test_present_or_ambiguous_head_never_resolves_or_verifies_old_upload(owned,status,error,result):
    f=owned;answer=m.execute_head(f.layout,f.observed,clock=lambda:NOW,probe=probe(f,status,error,result))
    assert answer['remote_missing_at_check'] is False and answer['remote_verified'] is False
    with m.connect(f.layout,True) as db:assert m.original(db)==f.baseline

def test_crash_after_reservation_never_reissues_even_if_no_provider_call_started(owned):
    f=owned
    def crash(stage):
        if stage=='after_reserve':raise RuntimeError('Owned crash')
    with pytest.raises(RuntimeError):run(f,checkpoint=crash)
    with pytest.raises(ValueError,match='ALREADY_RESERVED'):run(f)
    assert not f.calls and not (f.layout.app_proof/'head-receipt.json').exists()

def test_crash_after_receipt_only_settles_reserved_head_and_does_not_issue_again(owned):
    f=owned
    def crash(stage):
        if stage=='after_receipt':raise RuntimeError('Owned crash')
    with pytest.raises(RuntimeError):run(f,checkpoint=crash)
    with m.connect(f.layout,True) as db:assert m.rows(db,'SELECT state FROM requests WHERE request_id=?',(m.HEAD_REQUEST,))[0]['state']=='reserved'
    answer=run(f);assert len(f.calls)==1 and answer['remote_missing_at_check']
    with m.connect(f.layout,True) as db:assert m.rows(db,'SELECT state FROM requests WHERE request_id=?',(m.HEAD_REQUEST,))[0]['state']=='done'

@pytest.mark.parametrize('change',['archive','config','adapter_intent','daily','history','original_request','operation','plan'])
def test_every_frozen_input_rejects_changed_bytes_or_budget_before_dispatch(owned,change):
    f=owned
    file={'archive':f.layout.archive,'config':f.layout.config,'adapter_intent':f.layout.adapter_intent,
        'daily':f.layout.latest,'history':f.layout.history}.get(change)
    if file:file.write_bytes(file.read_bytes()+b' ')
    else:
        with m.connect(f.layout) as db:
            if change=='original_request':db.execute("UPDATE requests SET reserved_bytes=reserved_bytes+1 WHERE request_id='3'")
            if change=='operation':db.execute("UPDATE operations SET state='failed'")
            if change=='plan':db.execute('UPDATE plans SET maximum_requests=8')
    with pytest.raises(ValueError):m.plan(f.layout)
    assert not f.calls

@pytest.mark.parametrize('dimension',['daily_bytes','monthly_bytes','daily_calls','monthly_calls'])
def test_existing_day_month_consumption_is_never_reset_or_refunded(owned,dimension):
    f=owned
    with m.connect(f.layout) as db:
        byte_limit=128000000 if dimension=='daily_bytes' else 800000000
        if dimension.endswith('bytes'):db.execute("UPDATE requests SET reserved_bytes=? WHERE request_id='old'",(byte_limit,))
        else:
            n=50 if dimension=='daily_calls' else 250
            for i in range(n):db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?)',
                ('historical-'+str(i),'old',NOW,'2026-11-03','2026-11','GET',1,'done',None))
    with pytest.raises(ValueError,match='BUDGET_EXCEEDED'):run(f)
    assert not f.calls
    with m.connect(f.layout,True) as db:assert not m.rows(db,'SELECT * FROM requests WHERE request_id=?',(m.HEAD_REQUEST,))

@pytest.mark.parametrize('field,value',[('maximum_requests',4),('maximum_network_bytes',1200000)])
def test_original_operation_plan_cannot_be_expanded_for_head(owned,monkeypatch,field,value):
    f=owned
    with m.connect(f.layout) as db:
        db.execute('UPDATE plans SET '+field+'=?',(value,));baseline={**f.baseline,'plan':m.rows(db,'SELECT * FROM plans')[0]}
    monkeypatch.setattr(m,'BASELINE_SHA',m.sha(m.wire(baseline)));f.observed=m.plan(f.layout)
    with pytest.raises(ValueError,match='OPERATION_PLAN_EXCEEDED'):run(f)
    assert not f.calls

@pytest.mark.parametrize('value',[{'http_status':404,'error_code':'Cookie:secret','head_result':None},
    {'http_status':404,'error_code':None,'head_result':None,'headers':{'Authorization':'secret'}},
    {'http_status':200,'error_code':None,'head_result':{'bytes':1,'method':'head_only','remote_verified':True}}])
def test_raw_headers_or_unbounded_error_details_cannot_enter_receipts(owned,value):
    f=owned
    with pytest.raises(ValueError,match='UNSAFE_OBSERVATION'):m.execute_head(f.layout,f.observed,clock=lambda:NOW,probe=lambda *_:value)
    assert not (f.layout.app_proof/'head-receipt.json').exists()

@pytest.mark.parametrize('uid,mode,expected_uid,private,ok',[(0,0o100640,0,False,True),
    (10001,0o100644,10001,False,True),(0,0o100644,10001,False,False),
    (0,0o100660,0,False,False),(10001,0o100600,10001,True,True),(10001,0o100640,10001,True,False)])
def test_real_owner_mode_predicate_covers_root_secret_and_existing_app_lock(uid,mode,expected_uid,private,ok):
    info=SimpleNamespace(st_uid=uid,st_mode=mode)
    if ok:REAL_OWNER(info,expected_uid,private)
    else:
        with pytest.raises(ValueError,match='OWNER_OR_MODE'):REAL_OWNER(info,expected_uid,private)

def test_host_guard_checks_service_and_fixed_sidecars_without_touching_timer(monkeypatch):
    calls=[]
    def command(args,**kwargs):calls.append(args);return 'failed' if len(calls)==1 else ''
    monkeypatch.setattr(m,'command',command);m.guard()
    assert len(calls)==4 and not any('timer' in str(x) or 'stop' in x for x in calls)

@pytest.mark.parametrize('state',['active','activating','deactivating'])
def test_running_service_stops_before_any_container_launch(monkeypatch,state):
    monkeypatch.setattr(m,'command',lambda *_:state)
    with pytest.raises(ValueError,match='SERVICE_ACTIVE'):m.guard()

def test_fixed_child_container_is_nonroot_head_only_and_credentials_readonly(owned,monkeypatch,tmp_path):
    f=owned;source=tmp_path/'operator.py';put(source,b'owned source');secret=f.layout.root/'secrets/cloudbase-backup.json';put(secret,b'owned secret placeholder')
    monkeypatch.setattr(m,'__file__',str(source));calls=[]
    def command(args,**kwargs):
        calls.append(args);return m.IMAGE if 'image' in args else ''
    def launch(args,**kwargs):
        calls.append(args);assert kwargs['input']==m.wire(f.observed)
        return SimpleNamespace(returncode=0,stdout=b'{"owned":"response"}',stderr=b'')
    monkeypatch.setattr(m,'command',command);monkeypatch.setattr(m.subprocess,'run',launch)
    assert m.container_head(f.layout,f.observed)=={'owned':'response'}
    args=next(x for x in calls if 'run' in x)
    assert args[-4:]==[m.IMAGE,'python','/run/interrupted-head.py','--child']
    assert '--user' in args and args[args.index('--user')+1]=='10001:10001'
    assert '--privileged' not in args and '-p' not in args and '--env-file' not in args
    secret_mount=next(x for x in args if '/run/secrets/cloudbase-backup.json' in x.replace('\\','/'))
    assert secret_mount.endswith(',readonly') and 'owned secret placeholder' not in str(args)
    assert not any('docker.sock' in x for x in args)

@pytest.mark.skipif(os.name!='posix',reason='fcntl advisory lock requires Linux')
def test_all_three_actual_os_locks_refuse_a_competing_holder(owned):
    f=owned
    for file,_ in f.layout.locks:put(file,b'')
    with m.locked(f.layout):
        with pytest.raises(BlockingIOError):
            with m.locked(f.layout):raise AssertionError('competing holder must not enter')
    with m.locked(f.layout):pass

def test_signed_transport_only_sends_head_and_does_not_leak_auth_into_observation(owned,monkeypatch):
    from adapters import safe_https,cloudbase_cos_backup
    f=owned;prefix='lighthouse-hub/backups/v1';host='example-1250000000.cos.ap-shanghai.myqcloud.com'
    raw_config=('''[cloudbase]
object_prefix="lighthouse-hub/backups/v1"
private_canary_reference="unused"
private_canary_sha256="unused"
credential_file="/run/secrets/cloudbase-backup.json"
storage_hosts=["'''+host+'''"]
''').encode()
    real_read=m.read;reads=[];calls=[]
    def read(file,uid,maximum=262144):
        reads.append((str(file),uid,maximum))
        if file==f.layout.config:return raw_config
        if file==Path('/run/secrets/cloudbase-backup.json'):
            return m.wire({'SecretId':'AKIDMOCKEXAMPLE','SecretKey':'owned-mock-key'})
        return real_read(file,uid,maximum)
    monkeypatch.setattr(m,'read',read)
    cfg=SimpleNamespace(host=host,bucket='example-1250000000',object_prefix=prefix,reference_scheme='cos',validate=lambda:None)
    monkeypatch.setattr(cloudbase_cos_backup,'CloudBaseCOSConfig',lambda **_:cfg)
    class Response:
        status=404
        def getheader(self,key,default=None):return 'NoSuchKey' if key=='x-cos-error-code' else default
        def read(self,n):assert n<=65537;return b''
    def request(url,hosts,*,method,headers,body,timeout):
        calls.append((url,method,body))
        assert method=='HEAD' and body is None and list(hosts)==[host]
        assert timeout==900
        assert headers['Authorization'].startswith('q-sign-algorithm=sha1')
        return SimpleNamespace(close=lambda:None),Response()
    monkeypatch.setattr(safe_https,'open_request',request)
    value=m.signed_head(f.layout,f.observed)
    assert len(calls)==1 and value=={'http_status':404,'provider_code':'NoSuchKey','head_result':None,
        'error_code':'CLOUDBASE_BACKUP_REMOTE_NOT_FOUND'}
    assert reads[-1]==(str(Path('/run/secrets/cloudbase-backup.json')),0,16384)
    assert 'AKIDMOCKEXAMPLE' not in m.wire(value).decode() and 'owned-mock-key' not in m.wire(value).decode()
