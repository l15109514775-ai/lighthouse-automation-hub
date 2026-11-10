"""Exact verified-clone cleanup. Default dry-run; no network or recursive deletion."""
from contextlib import contextmanager
from datetime import datetime,timezone
import argparse,hashlib,json,os,re,sqlite3,stat,subprocess,sys,tarfile,time,uuid
from pathlib import Path,PurePosixPath
ROOT=Path('/opt/lighthouse-hub')
ENV='subtitledev-exampleenv000000'
BUCKET='examplebucket-1250000000'
PREFIX='lighthouse-hub/backups/v1'
FIELDS=('version','operation_id','env_id','kind','local_archive','archive_key','bytes','sha256','created_at','remote_verified','local_deletion_performed','archive_file_id','archive_readback')
def encoded(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
def sha(data):return hashlib.sha256(data).hexdigest()
def ident(value):assert str(uuid.UUID(value))==value;return value
def fresh(value):
    at=datetime.fromisoformat(value);assert at.tzinfo is not None and 0<=time.time()-at.timestamp()<=86400

def safe(path,root):
    path=Path(path);assert path.is_absolute() and path.resolve()==path and path.is_relative_to(root)
    assert not path.is_symlink() and not any(p.is_symlink() for p in path.parents)
    return path

def inspect_file(path,root,maximum=128_000_000):
    path=safe(path,root);flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_BINARY',0);fd=os.open(path,flags)
    try:
        before=os.fstat(fd);assert stat.S_ISREG(before.st_mode) and before.st_size<=maximum
        if os.name=='posix':assert before.st_uid in (0,10001) and before.st_mode&0o077==0
        digest=hashlib.sha256()
        while True:
            data=os.read(fd,1048576)
            if not data:break
            digest.update(data)
        after=os.fstat(fd);assert (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)==(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)
    finally:os.close(fd)
    parent=path.parent.stat()
    return {'file':str(path),'bytes':before.st_size,'sha256':digest.hexdigest(),'device':before.st_dev,'inode':before.st_ino,
        'mtime_ns':before.st_mtime_ns,'parent_device':parent.st_dev,'parent_inode':parent.st_ino}

def read_json(path,root,maximum=5_000_000):
    item=inspect_file(path,root,maximum);data=Path(path).read_bytes();assert len(data)==item['bytes'] and sha(data)==item['sha256']
    return json.loads(data),item

def ledger_rows(root,operation):
    # Read as the app uid, so any SQLite readonly WAL shared-memory file remains
    # app-owned. Never use immutable against this live WAL ledger.
    code="""from pathlib import Path
import sqlite3,sys,json,os
assert os.getuid()==10001
db=sqlite3.connect(Path(sys.argv[1]).as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
db.execute('PRAGMA query_only=ON')
r=db.execute('SELECT * FROM operations WHERE operation_id=?',(sys.argv[2],)).fetchone()
q=[dict(x) for x in db.execute('SELECT method,state,error_code FROM requests WHERE operation_id=? ORDER BY created_at,request_id',(sys.argv[2],))]
print(json.dumps({'operation':dict(r) if r else None,'requests':q}));db.close()
"""
    def as_app():os.setgroups([]);os.setgid(10001);os.setuid(10001)
    result=subprocess.check_output([sys.executable,'-c',code,str(root/'data/cloudbase-backup-state/ledger.sqlite3'),operation],
        timeout=25,preexec_fn=as_app,env={'PATH':'/usr/bin:/bin','HOME':'/tmp','PYTHONNOUSERSITE':'1'})
    return json.loads(result)

def bindings(report_file,root,reader=ledger_rows):
    report,ri=read_json(report_file,root);acceptance=ident(report['acceptance_id']);operation=ident(report['operation_id'])
    area=root/'backups/cloudbase-restore-check'/acceptance;assert Path(report_file)==area/'restoration-report.json'
    assert report['version']=='cloudbase-actual-remote-restore-acceptance-v1'
    assert all(report.get(k) is True for k in ('actual_archive_get_captured','all_entries_verified','credential_exclusions_verified'))
    assert report['additional_remote_gets']==report['local_deletions']==0 and report['sqlite_integrity']==report['sqlite_foreign_keys']=='ok'
    fresh(report['verified_at'])
    runtime,ui=read_json(area/'runtime-result.json',root);assert runtime['operation_id']==operation
    remote=runtime['remote'];receipt_file=Path(remote['receipt_file']);receipt,vi=read_json(receipt_file,root,262144)
    remoteop=ident(receipt['operation_id']);assert receipt_file==root/'backups/cloudbase-receipts'/(remoteop+'.verified.json')
    assert {k:v for k,v in remote.items() if k!='receipt_file'}==receipt
    assert receipt['version']=='cloudbase-backup-v1' and receipt['env_id']==ENV and receipt['kind']=='database_snapshot'
    assert receipt['remote_verified'] is True and receipt['local_deletion_performed'] is False
    assert receipt['verification_method']=='archive_and_manifest_full_remote_get';fresh(receipt['verified_at'])
    stage=root/'backups/cloudbase-staging'/operation;archive=stage/'operational-backup.tar.gz'
    assert runtime['bundle']['archive']==str(archive) and receipt['local_archive']==archive.relative_to(root).as_posix()
    assert receipt['bytes']==runtime['bundle']['bytes'] and receipt['sha256']==runtime['bundle']['sha256'] and re.fullmatch('[a-f0-9]{64}',receipt['sha256'])
    key=PREFIX+'/'+remoteop+'/'+receipt['sha256']+'.archive';manifest_key=PREFIX+'/'+remoteop+'/manifest.json'
    assert receipt['archive_key']==key and receipt['manifest_key']==manifest_key
    assert receipt['archive_file_id']=='cos://'+BUCKET+'/'+key and receipt['manifest_file_id']=='cos://'+BUCKET+'/'+manifest_key
    assert report['remote_archive_reference']==receipt['archive_file_id'] and report['remote_manifest_reference']==receipt['manifest_file_id']
    wire=encoded({k:receipt[k] for k in FIELDS});assert sha(wire)==receipt['manifest_sha256']
    assert receipt['archive_readback']=={'bytes':receipt['bytes'],'sha256':receipt['sha256'],'method':'full_remote_get'}
    assert receipt['manifest_readback']=={'bytes':len(wire),'sha256':sha(wire),'method':'full_remote_get'}
    ledger=reader(root,operation);row=ledger['operation'];assert row['state']=='verified' and row['archive']==str(archive) and row['receipt']==str(receipt_file) and row['error_code'] is None
    calls=ledger['requests'];assert len(calls)==7 and [x['method'] for x in calls].count('PUT')==2 and [x['method'] for x in calls].count('GET')==5
    assert all(x['state']=='done' and x['error_code'] is None for x in calls)
    freshref=runtime['fresh_private_canary'];assert report['fresh_private_canary']==freshref and freshref['private_storage_verified'] is True
    checkfile=root/'data/cloudbase-backup-canary-checks'/(operation+'.json');assert freshref['file']==str(checkfile)
    check,ci=read_json(checkfile,root,65536);assert ci['sha256']==freshref['sha256']
    assert check['version']=='cloudbase-runtime-private-canary-v1' and check['operation_id']==operation and check['private_storage_verified'] is True;fresh(check['checked_at'])
    assert check['signed_full_get']=={'bytes':check['canary']['bytes'],'sha256':check['canary']['sha256'],'method':'full_remote_get'}
    obs=check['anonymous_observations'];assert len(obs)==2 and {x['host'] for x in obs}=={BUCKET+'.cos.ap-shanghai.myqcloud.com',BUCKET+'.tcb.qcloud.la'}
    assert all(x['method']=='GET' and x['signed'] is False and x['status'] in (401,403) for x in obs)
    assert set(report['keep_files'])=={str(area/'runtime-result.json'),str(report_file),str(receipt_file)}
    proof={'report':ri,'runtime':ui,'receipt':vi,'fresh_canary':ci,'ledger_operation':row}
    return report,runtime,proof,area,stage

def candidate_specs(report,runtime,area,stage):
    remote=runtime['remote'];manifest=runtime['bundle']['manifest'];operation=report['operation_id']
    assert manifest['version']=='cloudbase-operational-backup-v1' and manifest['operation_id']==operation
    raw=encoded(manifest);db='database/credential-excluded-operational.sqlite3'
    assert db in manifest['files'] and manifest['source_files_deleted'] is False and manifest['production_database_modified'] is False
    expected=[{'file':str(stage/'operational-backup.tar.gz'),'bytes':remote['bytes'],'sha256':remote['sha256']},
      {'file':str(stage/'credential-excluded-operational.sqlite3'),**manifest['files'][db]},
      {'file':str(stage/'archive-manifest.json'),'bytes':len(raw),'sha256':sha(raw)}]
    assert len(report['stage_prune_candidates'])==3
    for got,want in zip(report['stage_prune_candidates'],expected):assert {k:got[k] for k in ('file','bytes','sha256')}==want and got['scope']=='this_exact_operation_stage_only'
    restored=[{'file':str(area/'remote-archive.tar.gz'),'bytes':remote['bytes'],'sha256':remote['sha256']}]
    assert report['archive']==restored[0]
    members={**manifest['files'],'MANIFEST.json':{'bytes':len(raw),'sha256':sha(raw)}}
    for name,item in members.items():
        p=PurePosixPath(name);assert not p.is_absolute() and '..' not in p.parts and all(':' not in x for x in p.parts)
        assert set(item)=={'bytes','sha256'} and type(item['bytes']) is int and item['bytes']>=0 and re.fullmatch('[a-f0-9]{64}',item['sha256'])
        restored.append({'file':str(area/'restored'/name),**item})
    assert report['entries']==len(members)
    actual=[{k:x[k] for k in ('file','bytes','sha256')} for x in report['restore_test_prune_candidates']]
    assert len(actual)==len(restored) and sorted(actual,key=lambda x:x['file'])==sorted(restored,key=lambda x:x['file'])
    candidates=expected+restored;assert len({x['file'] for x in candidates})==len(candidates)
    return candidates,members

def verify_tar_and_database(report,runtime,area,members):
    with tarfile.open(area/'remote-archive.tar.gz','r:gz') as tar:
        items=tar.getmembers();assert len(items)==len(members) and len(items)<6000
        assert {x.name for x in items}==set(members) and len({x.name for x in items})==len(items)
        for item in items:
            assert item.isfile() and item.size==members[item.name]['bytes']
            with tar.extractfile(item) as stream:assert hashlib.file_digest(stream,'sha256').hexdigest()==members[item.name]['sha256']
    db=area/'restored/database/credential-excluded-operational.sqlite3'
    with sqlite3.connect(db.as_uri()+'?mode=ro&immutable=1',uri=True) as connection:
        assert connection.execute('PRAGMA integrity_check').fetchone()[0]=='ok' and connection.execute('PRAGMA foreign_key_check').fetchall()==[]
        for table in runtime['bundle']['manifest']['snapshot']['excluded_tables']:
            assert re.fullmatch('[A-Za-z0-9_]+',table) and connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0

def atomic(path,value):
    assert not path.is_symlink();temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.partial')
    fd=os.open(temp,os.O_CREAT|os.O_EXCL|os.O_WRONLY|getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(fd,'wb') as stream:stream.write(encoded(value));stream.flush();os.fsync(stream.fileno())
    os.replace(temp,path)
    if os.name=='posix':
        fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)

def create_plan(report_file,root,reader=ledger_rows):
    report,runtime,proof,area,stage=bindings(report_file,root,reader);specs,members=candidate_specs(report,runtime,area,stage)
    items=[]
    for spec in specs:
        actual=inspect_file(Path(spec['file']),root);assert {k:actual[k] for k in ('file','bytes','sha256')}==spec, (spec,actual)
        assert actual['mtime_ns']<=proof['report']['mtime_ns']+1_000_000_000
        items.append({**actual,'state':'pending'})
    verify_tar_and_database(report,runtime,area,members)
    return {'version':'cloudbase-exact-verified-clone-prune-v1','acceptance_id':report['acceptance_id'],'operation_id':report['operation_id'],
        'created_at':datetime.now(timezone.utc).isoformat(),'proof':proof,'candidates':items,'state':'prepared','deleted_files':0,
        'historical_raw_backups_touched':False,'business_sources_touched':False,'remote_requests':0}

def revalidate_plan(plan,report_file,root,reader=ledger_rows):
    report,runtime,proof,area,stage=bindings(report_file,root,reader);specs,_=candidate_specs(report,runtime,area,stage)
    assert plan['version']=='cloudbase-exact-verified-clone-prune-v1' and plan['acceptance_id']==report['acceptance_id'] and plan['operation_id']==report['operation_id'] and plan['proof']==proof
    assert [{k:i[k] for k in ('file','bytes','sha256')} for i in plan['candidates']]==specs
    for item in plan['candidates']:
        path=safe(item['file'],root)
        if path.exists():
            assert item['state']!='deleted';actual=inspect_file(path,root)
            assert all(actual[k]==item[k] for k in actual)
        else:assert item['state'] in ('deleting','deleted')
    return plan

def apply_plan(plan,journal,root):
    # Each unlink uses an O_NOFOLLOW directory chain and an immediately verified
    # inode/mtime/size/SHA. Never remove directories or use rm/rmtree.
    for item in plan['candidates']:
        if item['state']=='deleted':continue
        path=Path(item['file'])
        if not path.exists():
            assert item['state']=='deleting';item['state']='deleted';item['recovered_after_unlink']=True
        else:
            actual=inspect_file(path,root);assert all(actual[k]==item[k] for k in actual)
            item['state']='deleting';atomic(journal,plan)
            fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:
                for part in path.parent.relative_to(root).parts:
                    child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=child
                parent=os.fstat(fd);assert (parent.st_dev,parent.st_ino)==(item['parent_device'],item['parent_inode'])
                current=os.stat(path.name,dir_fd=fd,follow_symlinks=False)
                assert stat.S_ISREG(current.st_mode) and (current.st_dev,current.st_ino,current.st_size,current.st_mtime_ns)==(item['device'],item['inode'],item['bytes'],item['mtime_ns'])
                os.unlink(path.name,dir_fd=fd);os.fsync(fd)
            finally:os.close(fd)
            item['state']='deleted';item['deleted_at']=datetime.now(timezone.utc).isoformat()
        plan['deleted_files']=sum(i['state']=='deleted' for i in plan['candidates']);atomic(journal,plan)
    plan['state']='completed';plan['completed_at']=datetime.now(timezone.utc).isoformat();atomic(journal,plan)

def main():
    assert os.name=='posix' and os.geteuid()==0;os.umask(0o077)
    parser=argparse.ArgumentParser();parser.add_argument('--report',required=True);mode=parser.add_mutually_exclusive_group();mode.add_argument('--apply',action='store_true');mode.add_argument('--dry-run',action='store_true');args=parser.parse_args()
    root=safe(ROOT,ROOT);report_file=safe(Path(args.report),root)
    lockdir=safe(root/'data/cloudbase-backup-state/runner.lock',root)
    assert lockdir.is_dir() and lockdir.stat().st_uid==10001 and lockdir.stat().st_mode&0o022==0
    lock=safe(lockdir/'observer.lock',root);fd=os.open(lock,os.O_RDWR|os.O_NOFOLLOW)
    import fcntl
    try:
        assert os.fstat(fd).st_uid==10001 and os.fstat(fd).st_mode&0o022==0;fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        report,_=read_json(report_file,root);acceptance=ident(report['acceptance_id']);folder=root/'backups/cloudbase-prune'
        safe(folder,root)
        if not folder.exists():folder.mkdir(mode=0o700)
        assert folder.stat().st_uid==0 and folder.stat().st_mode&0o077==0
        journal=folder/(acceptance+'.intent.json')
        if journal.exists():
            plan,_=read_json(journal,root);plan=revalidate_plan(plan,report_file,root)
        else:plan=create_plan(report_file,root)
        if args.apply:
            if not journal.exists():atomic(journal,plan)
            apply_plan(plan,journal,root)
        print(json.dumps({'state':plan['state'] if args.apply else 'dry_run_verified','apply':args.apply,'candidate_files':len(plan['candidates']),
            'candidate_bytes':sum(x['bytes'] for x in plan['candidates']),'deleted_files':plan['deleted_files'] if args.apply else 0,
            'journal_file':str(journal),'remote_requests':0,'historical_raw_backups_touched':False,'business_sources_touched':False}))
        return 0
    finally:os.close(fd)
if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:print(json.dumps({'state':'stopped_safely','error_code':type(exc).__name__,'remote_requests':0}));raise SystemExit(2) from None
