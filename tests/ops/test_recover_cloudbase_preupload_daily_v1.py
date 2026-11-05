"""Owned pre-upload failure fixtures; never access production, Docker or network."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

spec=importlib.util.spec_from_file_location('recover_preupload',
    Path(__file__).parents[2]/'scripts/ops/recover_cloudbase_preupload_daily_v1.py')
recovery=importlib.util.module_from_spec(spec);spec.loader.exec_module(recovery)
real_owner=recovery.owner


def put(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(recovery.wire(value));path.chmod(0o600)


@pytest.fixture
def owned(tmp_path,monkeypatch):
    layout=recovery.Layout(tmp_path/'app',tmp_path/'ops')
    # Host uid/mode boundaries are separately tested with exact stat fixtures;
    # this owned fixture also runs on Windows without chown or root privileges.
    monkeypatch.setattr(recovery,'owner',lambda info,uid,private=True:None)
    daily={'version':'cloudbase-daily-accept-prune-v1','daily_id':recovery.DAILY_ID,
        'created_at':recovery.START,'updated_at':recovery.END,'state':'accept_failed',
        'error_code':'ACCEPT_FAILED_OR_UNVERIFIED','image':recovery.IMAGE,'helper_pins':{'owned':'fixed'}}
    documents={'latest':daily,'history':dict(daily),'legacy':{'state':'completed','daily_id':'owned-old'},
        'host_status':{'version':'cloudbase-backup-host-service-v1e','state':'failed','service_result':'exit-code',
            'at':'2026-11-02T19:30:07.648996+00:00'},
        'failure':{'version':'cloudbase-restore-acceptance-failure-v1','error_code':recovery.ERROR,
            'additional_remote_gets':0,'local_deletions':0,'captured_partial_files_retained':True,
            'at':'2026-11-02T19:30:06.059051+00:00'}}
    for name,path in {'latest':layout.latest,**layout.sources}.items():put(path,documents[name])
    monkeypatch.setattr(recovery,'PINS',{key:recovery.sha(recovery.wire(value)) for key,value in documents.items()})
    with sqlite3.connect(layout.ledger) as db:
        db.executescript('''CREATE TABLE operations(operation_id TEXT PRIMARY KEY,created_at REAL NOT NULL,
            updated_at REAL NOT NULL,state TEXT NOT NULL,archive TEXT,receipt TEXT,error_code TEXT,policy_sha256 TEXT NOT NULL);
            CREATE TABLE requests(request_id TEXT PRIMARY KEY,operation_id TEXT NOT NULL,created_at REAL NOT NULL,
            day TEXT NOT NULL,month TEXT NOT NULL,method TEXT NOT NULL,reserved_bytes INTEGER NOT NULL,state TEXT NOT NULL,error_code TEXT);
            CREATE TABLE plans(operation_id TEXT PRIMARY KEY,created_at REAL NOT NULL,maximum_network_bytes INTEGER NOT NULL,maximum_requests INTEGER NOT NULL);''')
        db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?)',tuple(recovery.EXPECTED_OPERATION.values()))
        db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?)',('old',100.,110.,'completed','owned-archive','owned-receipt',None,'owned-policy'))
        db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?)',('old-read','old',105.,'2026-11-01','2026-11','GET',1234,'done',None))
        db.execute('INSERT INTO plans VALUES(?,?,?,?)',('old',100.,1234,1))
    layout.ledger.chmod(0o600)
    return layout


def plan_hash(layout):return recovery.sha(recovery.wire(recovery.plan(layout)))


def execute(layout,digest=None,**kwargs):
    return recovery.apply(layout,digest or plan_hash(layout),host_guard=lambda:None,**kwargs)


def repin(layout,name,change):
    path=layout.latest if name=='latest' else layout.sources[name]
    data=json.loads(path.read_bytes());change(data);put(path,data)
    recovery.PINS[name]=recovery.sha(path.read_bytes())


def test_default_plan_does_not_change_files_or_claim_backup_success(owned):
    before={str(p):p.read_bytes() for p in owned.app.parent.rglob('*') if p.is_file()}
    value=recovery.plan(owned)
    assert value['external_requests']==0 and value['ledger_modified'] is False
    assert value['backup_success_claimed'] is False and value['failure_history_retained'] is True
    assert value['ledger']['requests']==value['ledger']['plans']==0
    assert value['ledger']['ledger_tables']['requests']['rows']==1
    assert before=={str(p):p.read_bytes() for p in owned.app.parent.rglob('*') if p.is_file()}
    assert not owned.recovery.exists()


def test_apply_archives_failure_preserves_history_ledger_and_is_idempotent(owned):
    failed=owned.latest.read_bytes();history=owned.sources['history'].read_bytes()
    ledger=owned.ledger.read_bytes();digest=plan_hash(owned)
    result=execute(owned,digest)
    assert result['state']=='preupload_failure_reconciled' and result['active_latest_absent'] is True
    assert not owned.latest.exists() and owned.archive.read_bytes()==failed
    assert owned.sources['history'].read_bytes()==history and owned.ledger.read_bytes()==ledger
    assert json.loads(owned.archive.read_bytes())['state']=='accept_failed'
    intent=json.loads((owned.recovery/'intent.json').read_bytes())
    assert recovery.sha(recovery.wire(intent['plan']))==digest==intent['plan_sha256']==result['plan_sha256']
    assert intent['plan']['source_hashes']['latest']==recovery.sha(failed)
    assert execute(owned,digest)==result
    assert json.loads((owned.recovery/'receipt.json').read_bytes())==result


@pytest.mark.parametrize('stage',('after_intent','after_relocation'))
def test_crash_after_each_durable_boundary_resumes_without_false_completion(owned,stage):
    digest=plan_hash(owned)
    def crash(current):
        if current==stage:raise RuntimeError('owned crash')
    with pytest.raises(RuntimeError,match='owned crash'):execute(owned,digest,checkpoint=crash)
    assert (owned.recovery/'intent.json').exists() and not (owned.recovery/'receipt.json').exists()
    assert owned.latest.exists()==(stage=='after_intent')
    assert json.loads(owned.sources['history'].read_bytes())['state']=='accept_failed'
    result=execute(owned,digest)
    assert result['backup_success_claimed'] is False and not owned.latest.exists()


@pytest.mark.parametrize('method,state',(('PUT','done'),('PUT','unknown'),('PUT','reserved'),('GET','done'),('HEAD','done')))
def test_any_request_even_zero_bytes_blocks_preupload_recovery(owned,method,state):
    with sqlite3.connect(owned.ledger) as db:
        db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?)',('new',recovery.OPERATION_ID,
            recovery.EXPECTED_OPERATION['created_at'],'2026-11-03','2026-11',method,0,state,None))
    with pytest.raises(ValueError,match='REMOTE_REQUEST_OR_PLAN_EXISTS'):execute(owned,'0'*64)
    assert owned.latest.exists() and not owned.recovery.exists()


def test_even_unused_reserved_plan_blocks_recovery(owned):
    with sqlite3.connect(owned.ledger) as db:db.execute('INSERT INTO plans VALUES(?,?,?,?)',(recovery.OPERATION_ID,1.,100,1))
    with pytest.raises(ValueError,match='REMOTE_REQUEST_OR_PLAN_EXISTS'):recovery.plan(owned)


@pytest.mark.parametrize('mutation,code',(
    ('overlap','OPERATION_NOT_UNIQUE'),('unknown','UNRESOLVED_OPERATION_EXISTS'),
    ('later_request','LATER_REMOTE_ACTIVITY_EXISTS'),('archive','OPERATION_CHANGED'),('receipt','OPERATION_CHANGED')))
def test_nonunique_unknown_and_nonpreupload_operations_fail_closed(owned,mutation,code):
    with sqlite3.connect(owned.ledger) as db:
        if mutation=='overlap':
            db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?)',('other',recovery.stamp(recovery.START),
                recovery.stamp(recovery.END),'failed',None,None,recovery.ERROR,'owned'))
        elif mutation=='unknown':
            db.execute("UPDATE operations SET state='upload_unknown' WHERE operation_id='old'")
        elif mutation=='later_request':
            db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?)',('later','old',recovery.stamp(recovery.END)+1,
                '2026-11-03','2026-11','GET',0,'done',None))
        else:db.execute('UPDATE operations SET '+mutation+'=? WHERE operation_id=?',('owned',recovery.OPERATION_ID))
    with pytest.raises(ValueError,match=code):recovery.plan(owned)
    assert not owned.recovery.exists()


def test_fixed_evidence_pin_rejects_even_harmless_document_change(owned):
    owned.latest.write_bytes(owned.latest.read_bytes()+b'\n')
    with pytest.raises(ValueError,match='PIN_CHANGED'):recovery.plan(owned)


@pytest.mark.parametrize('target,field,value,code',(
    ('latest','state','completed','DAILY_BINDING_CHANGED'),
    ('latest','acceptance',{},'DAILY_BINDING_CHANGED'),
    ('legacy','state','accept_failed','LEGACY_NOT_COMPLETED'),
    ('host_status','state','completed','HOST_FAILURE_CHANGED'),
    ('failure','additional_remote_gets',1,'ACCEPTANCE_FAILURE_CHANGED'),
    ('failure','captured_partial_files_retained',False,'ACCEPTANCE_FAILURE_CHANGED')))
def test_semantic_bindings_fail_even_if_owned_fixture_pins_are_updated(owned,target,field,value,code):
    repin(owned,target,lambda data:data.update({field:value}))
    with pytest.raises(ValueError,match=code):recovery.plan(owned)


def test_exact_plan_hash_and_second_read_stop_races_before_relocation(owned):
    with pytest.raises(ValueError,match='PLAN_CHANGED'):execute(owned,'0'*64)
    assert not owned.recovery.exists()
    digest=plan_hash(owned)
    def change(stage):
        if stage=='after_intent':owned.latest.write_bytes(owned.latest.read_bytes()+b'\n')
    with pytest.raises(ValueError,match='PIN_CHANGED'):execute(owned,digest,checkpoint=change)
    assert owned.latest.exists() and not owned.archive.exists() and not (owned.recovery/'receipt.json').exists()


def test_ledger_budget_change_requires_a_new_plan_without_clearing_consumption(owned):
    digest=plan_hash(owned)
    with sqlite3.connect(owned.ledger) as db:db.execute("UPDATE requests SET reserved_bytes=9999 WHERE request_id='old-read'")
    with pytest.raises(ValueError,match='PLAN_CHANGED'):execute(owned,digest)
    assert owned.latest.exists()
    result=execute(owned)
    assert result['ledger_modified'] is False
    with sqlite3.connect(owned.ledger) as db:assert db.execute("SELECT reserved_bytes FROM requests WHERE request_id='old-read'").fetchone()[0]==9999


def test_record_publication_failure_never_leaves_partial_intent(owned,monkeypatch):
    digest=plan_hash(owned);link=recovery.os.link
    def failure(*args,**kwargs):raise OSError('owned link failure')
    monkeypatch.setattr(recovery.os,'link',failure)
    with pytest.raises(OSError,match='owned link failure'):execute(owned,digest)
    assert owned.latest.exists() and not (owned.recovery/'intent.json').exists()
    assert list(owned.recovery.iterdir())==[]
    monkeypatch.setattr(recovery.os,'link',link)
    assert execute(owned,digest)['active_latest_absent'] is True


def test_archived_latest_without_prior_intent_cannot_invent_recovery(owned):
    digest=plan_hash(owned);owned.recovery.mkdir(parents=True)
    owned.latest.rename(owned.archive)
    with pytest.raises(FileNotFoundError):execute(owned,digest)
    assert not (owned.recovery/'receipt.json').exists()


def test_conflicting_existing_intent_is_preserved_and_blocks_apply(owned):
    digest=plan_hash(owned);intent=owned.recovery/'intent.json';put(intent,{'owned':'different'})
    with pytest.raises(ValueError,match='RECORD_CONFLICT'):execute(owned,digest)
    assert json.loads(intent.read_bytes())=={'owned':'different'} and owned.latest.exists()


def test_two_latest_copies_are_never_overwritten(owned):
    put(owned.archive,json.loads(owned.latest.read_bytes()))
    with pytest.raises(ValueError,match='TWO_LATEST_COPIES'):recovery.plan(owned)
    assert owned.latest.exists() and owned.archive.exists()


@pytest.mark.parametrize('uid,mode,private,ok',((0,0o100600,True,True),(10001,0o100600,True,False),
    (0,0o100640,True,False),(0,0o100644,False,True),(0,0o100622,False,False)))
def test_exact_owner_and_modes(uid,mode,private,ok):
    item=SimpleNamespace(st_uid=uid,st_mode=mode)
    if ok:real_owner(item,0,private)
    else:
        with pytest.raises(ValueError,match='OWNER_OR_MODE_CHANGED'):real_owner(item,0,private)


def test_host_guard_queries_only_fixed_service_and_sidecar_inventory(monkeypatch):
    calls=[]
    def run(args,**kwargs):
        calls.append(args)
        assert not kwargs.get('shell')
        return SimpleNamespace(returncode=0,stdout='failed\n' if len(calls)==1 else '')
    monkeypatch.setattr(recovery.subprocess,'run',run)
    recovery.guard()
    assert calls==[['/usr/bin/systemctl','show','lighthouse-cloudbase-backup.service','--property=ActiveState','--value'],
        ['/usr/bin/docker','ps','-q','--filter','label=org.lighthouse.backup.sidecar'],
        ['/usr/bin/docker','ps','-q','--filter','name=^/lighthouse-archive-registry-']]


@pytest.mark.parametrize('response',('active','activating'))
def test_active_service_cannot_be_recovered(monkeypatch,response):
    monkeypatch.setattr(recovery.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(returncode=0,stdout=response))
    with pytest.raises(ValueError,match='BACKUP_SERVICE_ACTIVE'):recovery.guard()


def test_readonly_sqlite_connect_and_query_only_are_enforced(owned,monkeypatch):
    actual=recovery.sqlite3.connect;connections=[];queries=[]
    def connect(database,**kwargs):
        assert database.endswith('?mode=ro') and kwargs['uri'] is True
        connection=actual(database,**kwargs);connection.set_trace_callback(queries.append)
        connections.append(connection);return connection
    monkeypatch.setattr(recovery.sqlite3,'connect',connect)
    assert recovery.ledger_proof(owned)['requests']==0
    assert len(connections)==1
    assert queries[0]=='PRAGMA query_only=ON' and queries[1]=='BEGIN'
    assert all(q.startswith(('PRAGMA query_only=ON','BEGIN','SELECT ')) for q in queries)


@pytest.mark.parametrize('conflict',(False,True))
def test_three_existing_locks_are_exclusive_nonblocking_and_released(owned,monkeypatch,conflict):
    for path,uid in owned.locks:path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'')
    seen=[];fds=[];original_open=os.open
    def opening(path,flags,*args,**kwargs):
        assert not flags&(os.O_CREAT|os.O_WRONLY|os.O_RDWR)
        seen.append(Path(path));fd=original_open(path,flags,*args,**kwargs);fds.append(fd);return fd
    def flock(fd,flags):
        assert flags==6
        if conflict and len(fds)==2:raise BlockingIOError('owned lock contention')
    monkeypatch.setitem(sys.modules,'fcntl',SimpleNamespace(LOCK_EX=2,LOCK_NB=4,flock=flock))
    monkeypatch.setattr(recovery.os,'O_NOFOLLOW',getattr(os,'O_NOFOLLOW',0),raising=False)
    monkeypatch.setattr(recovery.os,'O_NONBLOCK',getattr(os,'O_NONBLOCK',0),raising=False)
    monkeypatch.setattr(recovery.os,'open',opening)
    if conflict:
        with pytest.raises(BlockingIOError):
            with recovery.locked(owned):pytest.fail('Contended lock cannot reach apply')
    else:
        with recovery.locked(owned):assert len(fds)==3
    assert seen==[row[0] for row in owned.locks][:2 if conflict else 3]
    for fd in fds:
        with pytest.raises(OSError):os.fstat(fd)


def test_a_symlinked_ancestor_is_rejected_before_evidence_read(owned,monkeypatch):
    original=Path.is_symlink
    monkeypatch.setattr(Path,'is_symlink',lambda path:path==owned.daily or original(path))
    with pytest.raises(ValueError,match='UNSAFE_PATH'):recovery.plan(owned)
    assert not owned.recovery.exists()


def test_real_upstream_resource_guard_and_recovery_share_the_lock_inode(owned):
    # This imports the actual production adapter and vendored exclusive(), not
    # a duplicate implementation or a lock path derived solely from Layout.
    from services.hub.locks import resource_guard
    folder=owned.app/'data/cloudbase-backup-state/runner.lock'
    for path,_ in owned.locks[:2]:path.write_bytes(b'')
    assert not folder.exists()
    with resource_guard(folder):
        actual=folder/'observer.lock'
        assert actual.is_file() and owned.locks[2][0]==actual
        if os.name=='posix':
            with pytest.raises(BlockingIOError):
                with recovery.locked(owned):pytest.fail('Live backup lock must exclude recovery')
        else:
            # Windows cannot execute the Linux-only operator. Check its exact
            # target against the real Windows upstream lock without fake fcntl.
            import msvcrt
            with actual.open('r+b') as competing:
                with pytest.raises(OSError):msvcrt.locking(competing.fileno(),msvcrt.LK_NBLCK,1)
    if os.name=='posix':
        with recovery.locked(owned):assert actual.exists()
