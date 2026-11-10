import ast
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import pytest
from historical_artifacts import historical_member

REPO=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location('owned_absent',REPO/'scripts/ops/recover_cloudbase_remote_absent_v1.py')
r=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(r)

def put(file,value):
    file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(r.wire(value));file.chmod(0o600)
def all_rows(layout):
    db=sqlite3.connect(layout.ledger);db.row_factory=sqlite3.Row
    try:return {name:[dict(x) for x in db.execute('SELECT * FROM '+name+' ORDER BY 1')] for name in ('operations','plans','requests')}
    finally:db.close()

@pytest.fixture
def sample(tmp_path,monkeypatch):
    layout=r.Layout(tmp_path/'app',tmp_path/'ops');layout.state.mkdir(parents=True)
    monkeypatch.setattr(r,'owner',lambda *args,**kwargs:None)
    monkeypatch.setattr(r,'ledger_snapshot',r.local_ledger_view)
    operation={'operation_id':r.OPERATION,'created_at':100.0,'updated_at':110.0,'state':'uploading',
               'archive':str(layout.root/'backups/cloudbase-staging'/r.OPERATION/'operational-backup.tar.gz'),
               'receipt':None,'error_code':None,'policy_sha256':'a'*64}
    plan={'operation_id':r.OPERATION,'created_at':110.0,'maximum_network_bytes':90000000,'maximum_requests':7}
    requests=[{'request_id':'fixture-'+str(n),'operation_id':r.OPERATION,'created_at':111.0+n,'day':'2026-11-03','month':'2026-11',
               'method':'PUT' if n==3 else 'GET','reserved_bytes':r.ARCHIVE_BYTES+300000 if n==3 else 300100,
               'state':'reserved' if n==3 else 'done','error_code':None} for n in range(4)]
    baseline={'operation':operation,'plan':plan,'requests':requests};monkeypatch.setattr(r,'BASELINE_SHA',r.sha(r.wire(baseline)))
    adapter={'version':'cloudbase-backup-v1','operation_id':r.ADAPTER,'archive_key':'lighthouse-hub/backups/v1/'+r.ADAPTER+'/'+r.ARCHIVE_SHA+'.archive',
             'bytes':r.ARCHIVE_BYTES,'sha256':r.ARCHIVE_SHA,'remote_verified':False,'local_deletion_performed':False}
    put(layout.adapter,adapter);monkeypatch.setattr(r,'ADAPTER_INTENT_SHA',r.sha(layout.adapter.read_bytes()))
    daily={'version':'cloudbase-daily-accept-prune-v1','daily_id':r.DAILY,'image':r.IMAGE,'state':'accept_failed',
           'error_code':'ACCEPT_FAILED_OR_UNVERIFIED','helper_pins':{'unchanged':'ownedfixture'}}
    put(layout.latest,daily);put(layout.history,daily);monkeypatch.setattr(r,'DAILY_SHA',r.sha(layout.latest.read_bytes()))
    doc=lambda file:{'file':str(file),'sha256':r.sha(file.read_bytes()),'bytes':file.stat().st_size}
    head_plan={'version':r.HEAD_VERSION,'operation_id':r.OPERATION,'daily_id':r.DAILY,'adapter_operation_id':r.ADAPTER,
               'image':r.IMAGE,'baseline':baseline,'adapter_intent':doc(layout.adapter),'archive_key':adapter['archive_key'],
               'archive_sha256':r.ARCHIVE_SHA,'archive_bytes':r.ARCHIVE_BYTES,
               'original_requests_sha256':r.sha(r.wire(requests)),
               'original_put_requests_sha256':r.sha(r.wire([requests[-1]])),
               'daily':{'latest':doc(layout.latest),'history':doc(layout.history)}}
    digest=r.sha(r.wire(head_plan));monkeypatch.setattr(r,'HEAD_PLAN_SHA',digest)
    head_intent={'version':r.HEAD_VERSION,'state':'head_intent_saved','plan_sha256':digest,'plan':head_plan}
    head={'version':r.HEAD_VERSION,'state':'head_observed','operation_id':r.OPERATION,'adapter_operation_id':r.ADAPTER,
          'archive_key':adapter['archive_key'],'archive_sha256':r.ARCHIVE_SHA,'archive_bytes':r.ARCHIVE_BYTES,
          'request_id':r.HEAD_REQUEST,'plan_sha256':digest,
          'observation':{'http_status':404,'error_code':'CLOUDBASE_BACKUP_REMOTE_NOT_FOUND','head_result':None},
          'remote_missing_at_check':True,'remote_verified':False,'requests':1,'puts':0,'gets':0,'deletions':0,'refund_bytes':0,
          'retry_performed':False,'original_operation_modified':False,'checked_at':200.0,
          'original_requests_sha256':head_plan['original_requests_sha256'],'original_put_requests_sha256':head_plan['original_put_requests_sha256']}
    for file in (layout.head/'intent.json',layout.app_head/'intent.json'):put(file,head_intent)
    for file in (layout.head/'receipt.json',layout.app_head/'head-receipt.json'):put(file,head)
    db=sqlite3.connect(layout.ledger)
    db.executescript('''CREATE TABLE operations(operation_id TEXT PRIMARY KEY,created_at REAL,updated_at REAL,state TEXT,archive TEXT,receipt TEXT,error_code TEXT,policy_sha256 TEXT);
      CREATE TABLE plans(operation_id TEXT PRIMARY KEY,created_at REAL,maximum_network_bytes INTEGER,maximum_requests INTEGER);
      CREATE TABLE requests(request_id TEXT PRIMARY KEY,operation_id TEXT,created_at REAL,day TEXT,month TEXT,method TEXT,reserved_bytes INTEGER,state TEXT,error_code TEXT);''')
    db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?)',tuple(operation.values()))
    db.execute('INSERT INTO plans VALUES(?,?,?,?)',tuple(plan.values()))
    for item in requests:db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?)',tuple(item.values()))
    db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?)',(r.HEAD_REQUEST,r.OPERATION,190,'2026-11-03','2026-11','HEAD',300000,'done','CLOUDBASE_BACKUP_REMOTE_NOT_FOUND'))
    db.commit();db.close();layout.ledger.chmod(0o600)
    return layout,head

def recover(layout,**kwargs):
    observed=r.plan(layout)
    return r.apply(layout,r.sha(r.wire(observed)),host_guard=lambda:None,
                   ledger_mutator=lambda a,b:r.transition(a,b,clock=lambda:300.0),**kwargs)

def test_offline_plan_does_not_change_ledger_or_failure(sample):
    layout,_=sample;before=all_rows(layout);failed=layout.latest.read_bytes()
    first=r.plan(layout);assert r.plan(layout)==first
    assert first['external_requests']==first['refund_bytes']==0 and not layout.proof.exists()
    assert all_rows(layout)==before and layout.latest.read_bytes()==layout.history.read_bytes()==failed

def test_404_resolution_preserves_failed_history_original_operation_and_every_reservation(sample):
    layout,_=sample;before=all_rows(layout);failed=layout.latest.read_bytes()
    value=recover(layout);after=all_rows(layout)
    assert value['state']=='remote_absent_reconciled' and value['no_refund'] and not value['backup_success_claimed']
    assert not layout.latest.exists() and layout.archive.read_bytes()==layout.history.read_bytes()==failed
    assert before['requests']==after['requests'] and before['plans']==after['plans']
    original=json.loads((layout.proof/'original-operation.json').read_bytes())
    assert original['operation']==before['operations'][0] and original['operation']['error_code'] is None
    current=after['operations'][0]
    assert current['state']==r.TERMINAL and current['error_code']==r.ERROR and current['updated_at']==300
    assert {k:v for k,v in current.items() if k not in ('state','error_code','updated_at')}=={k:v for k,v in before['operations'][0].items() if k not in ('state','error_code','updated_at')}
    assert r.verify_completed(layout)==value
    assert recover(layout)==value and all_rows(layout)==after

@pytest.mark.parametrize('stage',['after_intent','after_ledger','after_relocation'])
def test_restart_resumes_exact_journal_without_refunding_or_losing_history(sample,stage):
    layout,_=sample;before=all_rows(layout);history=layout.history.read_bytes()
    def stop(name):
        if name==stage:raise RuntimeError('owned crash')
    with pytest.raises(RuntimeError,match='owned crash'):recover(layout,checkpoint=stop)
    value=recover(layout)
    assert value['state']=='remote_absent_reconciled' and layout.history.read_bytes()==history
    assert all_rows(layout)['requests']==before['requests'] and all_rows(layout)['plans']==before['plans']

def test_crash_after_sql_commit_before_transition_receipt_recovers(sample):
    layout,_=sample;observed=r.plan(layout)
    def stop(a,b):r.transition(a,b,clock=lambda:300.0);raise RuntimeError('after commit')
    with pytest.raises(RuntimeError,match='after commit'):
        r.apply(layout,r.sha(r.wire(observed)),host_guard=lambda:None,ledger_mutator=stop)
    assert not (layout.proof/'ledger-transition.json').exists()
    assert recover(layout)['state']=='remote_absent_reconciled'

@pytest.mark.parametrize('change',['200','403','500','no_status','wrong_error','has_result','remote_verified','refund','retry','wrong_op','wrong_key','future','request_hash','missing_flag'])
def test_non404_or_unbound_head_never_changes_old_upload(sample,change):
    layout,head=sample;before=all_rows(layout);value=deepcopy(head)
    if change in ('200','403','500'):value['observation']['http_status']=int(change)
    elif change=='no_status':value['observation'].pop('http_status')
    elif change=='wrong_error':value['observation']['error_code']='NETWORK_ERROR'
    elif change=='has_result':value['observation']['head_result']={'bytes':r.ARCHIVE_BYTES}
    elif change=='remote_verified':value['remote_verified']=True
    elif change=='refund':value['refund_bytes']=1
    elif change=='retry':value['retry_performed']=True
    elif change=='wrong_op':value['operation_id']='wrong'
    elif change=='wrong_key':value['archive_key']='other'
    elif change=='future':value['checked_at']=r.time.time()+99999
    elif change=='request_hash':value['original_put_requests_sha256']='0'*64
    elif change=='missing_flag':value['remote_missing_at_check']=False
    for file in (layout.head/'receipt.json',layout.app_head/'head-receipt.json'):put(file,value)
    with pytest.raises(ValueError):r.plan(layout)
    assert all_rows(layout)==before and layout.latest.exists() and not layout.proof.exists()

@pytest.mark.parametrize('sql',["UPDATE requests SET reserved_bytes=reserved_bytes-1 WHERE method='PUT'",
    "UPDATE requests SET state='reserved' WHERE method='HEAD'","UPDATE requests SET error_code=NULL WHERE method='HEAD'",
    "DELETE FROM requests WHERE method='HEAD'","UPDATE plans SET maximum_network_bytes=maximum_network_bytes+1",
    "UPDATE operations SET state='completed'","UPDATE operations SET state='resolved_remote_absent'",
    "INSERT INTO operations VALUES('another',1,1,'upload_unknown',NULL,NULL,NULL,'x')"])
def test_live_ledger_changes_refuse_recovery(sample,sql):
    layout,_=sample;db=sqlite3.connect(layout.ledger);db.execute(sql);db.commit();db.close();before=all_rows(layout)
    with pytest.raises(ValueError):r.plan(layout)
    assert all_rows(layout)==before and layout.latest.exists()

@pytest.mark.parametrize('name',['head_root','head_app','adapter','latest','history'])
def test_missing_or_changed_bound_file_is_refused(sample,name):
    layout,_=sample
    file={'head_root':layout.head/'receipt.json','head_app':layout.app_head/'head-receipt.json','adapter':layout.adapter,'latest':layout.latest,'history':layout.history}[name]
    file.unlink()
    with pytest.raises((ValueError,FileNotFoundError)):r.plan(layout)
    assert not layout.proof.exists()

def test_wrong_apply_plan_hash_has_no_side_effects(sample):
    layout,_=sample;before=all_rows(layout)
    with pytest.raises(ValueError,match='ABSENT_PLAN_CHANGED'):
        r.apply(layout,'0'*64,host_guard=lambda:None,ledger_mutator=lambda *_:pytest.fail('no write allowed'))
    assert all_rows(layout)==before and not layout.proof.exists()

def test_root_receipt_cannot_omit_or_fabricate_terminal_closure(sample):
    layout,_=sample;recover(layout);file=layout.proof/'receipt.json';receipt=json.loads(file.read_bytes())
    receipt['no_refund']=False;put(file,receipt)
    with pytest.raises(ValueError,match='ABSENT_FINAL_RECEIPT_CHANGED'):r.verify_completed(layout)

@pytest.mark.parametrize('active',['service','backup','registry','head'])
def test_running_processes_refuse_recovery(monkeypatch,active):
    def command(args):
        if args[0].endswith('systemctl'):return 'active' if active=='service' else 'inactive'
        condition=args[-1]
        return 'running' if (active=='backup' and 'sidecar' in condition or active=='registry' and 'archive-registry' in condition or active=='head' and 'interrupted-head' in condition) else ''
    monkeypatch.setattr(r,'command',command)
    with pytest.raises(ValueError):r.guard()

def test_existing_runner_lock_inode_excludes_root_recovery(tmp_path,monkeypatch):
    from services.hub.locks import resource_guard
    layout=r.Layout(tmp_path/'app',tmp_path/'ops');layout.daily.mkdir(parents=True)
    for file,_ in layout.locks[:2]:file.write_bytes(b'');file.chmod(0o600)
    monkeypatch.setattr(r,'owner',lambda *args,**kwargs:None)
    with resource_guard(layout.state/'runner.lock'):
        assert layout.locks[2][0].name=='observer.lock'
        if os.name=='posix':
            with pytest.raises(BlockingIOError):
                with r.locked(layout):pytest.fail('must not acquire')
        else:
            import msvcrt
            with layout.locks[2][0].open('r+b') as stream:
                with pytest.raises(OSError):msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
    if os.name=='posix':
        with r.locked(layout):pass

def test_recovery_has_no_network_shell_or_budget_initialization():
    text=(REPO/'scripts/ops/recover_cloudbase_remote_absent_v1.py').read_text()
    assert 'CloudBaseBackupRuntime' not in text and 'BackupLedger(' not in text and 'shell=True' not in text
    tree=ast.parse(text);sql=[n.args[0].value for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='execute' and n.args and isinstance(n.args[0],ast.Constant) and isinstance(n.args[0].value,str)]
    updates=[s for s in sql if s.startswith(('UPDATE','INSERT','DELETE','CREATE'))]
    assert updates==['UPDATE operations SET state=?,error_code=?,updated_at=? WHERE operation_id=? AND state=?']

@pytest.mark.parametrize('sql',["UPDATE operations SET state='upload_unknown'", "UPDATE requests SET reserved_bytes=0 WHERE method='PUT'",
                              "UPDATE plans SET maximum_network_bytes=maximum_network_bytes+1"])
def test_first_gap_rechecks_live_terminal_and_unmodified_reservations(sample,sql):
    layout,_=sample;recover(layout);db=sqlite3.connect(layout.ledger);db.execute(sql);db.commit();db.close()
    with pytest.raises(ValueError):r.verify_completed(layout)

def test_root_ledger_inspection_delegates_to_app_uid_without_opening_root_connection(monkeypatch):
    calls=[]
    monkeypatch.setattr(r,'os',SimpleNamespace(name='posix',geteuid=lambda:0))
    monkeypatch.setattr(r,'child_action',lambda value:calls.append(value) or {'owned':'answer'})
    monkeypatch.setattr(r,'connect',lambda *_:pytest.fail('root must not connect'))
    assert r.ledger_snapshot(None,{'owned':'proof'},False)=={'owned':'answer'}
    assert calls==[{'action':'inspect','proof':{'owned':'proof'},'allow_terminal':False}]

def entry():
    spec=importlib.util.spec_from_file_location('owned_v2d',REPO/'scripts/ops/cloudbase_backup_daily_retention_v2d.py')
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value

def test_new_entry_pins_old_workflow_and_final_recovery_source():
    e=entry()
    assert e.SOURCES['cloudbase_backup_daily_retention_v2c.py']=='91fc68526997ebb6c336e4bf11849640208288162a9cae63a208b3d937c7db95'
    for name,digest in e.SOURCES.items():
        original=historical_member('historical-deployment-source-inputs.tar','scripts/ops/'+name,digest)
        assert r.sha(original)==digest
    text=(REPO/'scripts/ops/cloudbase_backup_daily_retention_v2d.py').read_text()
    assert '7380' not in text and '7200' not in text and 'PINS=' not in text

def test_new_entry_only_rebinds_gap_and_uses_real_validator(sample):
    layout,_=sample;recover(layout);e=entry();pins={'owned':'unchanged'};old_main=lambda:17
    daily=SimpleNamespace(PINS=pins,main=old_main,verify_recovered_gap=lambda _:pytest.fail('old gap'))
    recovery=SimpleNamespace(Layout=lambda:layout,verify_completed=r.verify_completed)
    gate=e.bind_recovery_gap(daily,recovery)
    assert daily.PINS is pins and daily.main is old_main and daily.verify_recovered_gap is gate
    assert gate(SimpleNamespace(STATE=e.DAILY_STATE))['state']=='remote_absent_reconciled'
    with pytest.raises(AssertionError):gate(SimpleNamespace(STATE=Path('different')))
    (layout.proof/'receipt.json').unlink()
    with pytest.raises(FileNotFoundError):gate(SimpleNamespace(STATE=e.DAILY_STATE))

def test_verify_recovery_uses_same_gate_and_does_not_run_workflow(sample,monkeypatch,capsys):
    layout,_=sample;recover(layout);e=entry();before=all_rows(layout)
    daily=SimpleNamespace(main=lambda:pytest.fail('must not start backup'))
    recovery=SimpleNamespace(Layout=lambda:layout,verify_completed=r.verify_completed)
    monkeypatch.setattr(e,'load_module',lambda name:daily if name.startswith('cloudbase_backup_daily') else recovery)
    monkeypatch.setattr(e,'os',SimpleNamespace(name='posix',geteuid=lambda:0,umask=lambda _:None))
    monkeypatch.setattr(e,'sys',SimpleNamespace(argv=['owned','--verify-recovery']))
    assert e.main()==0
    result=json.loads(capsys.readouterr().out)
    assert result['state']=='verified_gap' and result['external_requests']==0 and result['backup_started'] is False
    assert all_rows(layout)==before

def test_unchanged_previous_completed_pins_allow_multiple_daily_cycles(monkeypatch):
    spec=importlib.util.spec_from_file_location('owned_daily_core',REPO/'scripts/ops/cloudbase_backup_daily_v1.py')
    daily=importlib.util.module_from_spec(spec);spec.loader.exec_module(daily)
    tree=ast.parse((REPO/'scripts/ops/cloudbase_backup_daily_retention_v2c.py').read_text());nodes=[]
    for node in tree.body:
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id in ('IMAGE','PINS','LEGACY_PINS','PRE_PRUNE_CAP_PINS') for t in node.targets):nodes.append(node)
        elif isinstance(node,ast.FunctionDef) and node.name=='validate_previous':nodes.append(node)
    scope={};exec(compile(ast.Module(body=nodes,type_ignores=[]),'<pinned-old-validation>','exec'),scope)
    monkeypatch.setattr(daily,'PINS',scope['PINS']);monkeypatch.setattr(daily,'accept_reference',lambda _: {'report_file':'owned-report'})
    def completed(record,**kwargs):record['state']='completed';return record
    monkeypatch.setattr(daily,'cleanup',completed);calls=[];saved=[]
    def runner(script,args,timeout):calls.append((script,args,timeout));return {}
    previous={'version':'cloudbase-daily-accept-prune-v1','image':r.IMAGE,'helper_pins':scope['PINS'],'state':'completed'}
    ids=[]
    for _ in range(3):
        scope['validate_previous'](previous)
        previous=daily.execute(previous,runner=runner,saver=lambda value:saved.append(deepcopy(value)),validator=lambda _: '1'*64)
        ids.append(previous['daily_id']);assert previous['helper_pins']==scope['PINS']
    assert len(set(ids))==3 and calls==[('run_cloudbase_backup_sidecar_v1e.py',['--accept'],960)]*3


def test_historical_daily_entry_rejects_current_source_before_import(tmp_path,monkeypatch):
    e=entry();name='cloudbase_backup_daily_retention_v2c.py'
    raw=(REPO/'scripts/ops'/name).read_bytes();assert r.sha(raw)!=e.SOURCES[name]
    (tmp_path/name).write_bytes(raw)
    monkeypatch.setattr(e,'OPS',tmp_path)
    def owned_path_only(file):assert file==tmp_path/name
    monkeypatch.setattr(e,'protected',owned_path_only)
    with pytest.raises(AssertionError):e.load_module(name)
