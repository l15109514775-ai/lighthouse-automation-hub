"""Owned local plan fixtures only. Never call apply_plan or contact storage."""
from datetime import datetime,timezone
import copy,hashlib,importlib.util,io,json,os,sqlite3,tarfile,uuid
from pathlib import Path
import pytest
P=Path(__file__).resolve().parents[2]/'scripts/ops/prune_cloudbase_verified_clones_v1.py';spec=importlib.util.spec_from_file_location('pruner',P);p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
def put(path,data):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data if isinstance(data,bytes) else p.encoded(data));path.chmod(0o600);return path
@pytest.fixture
def owned(tmp_path):
    root=tmp_path/'hub';root.mkdir();op=str(uuid.uuid4());accept=str(uuid.uuid4());remoteop=str(uuid.uuid4());now=datetime.now(timezone.utc).isoformat()
    stage=root/'backups/cloudbase-staging'/op;area=root/'backups/cloudbase-restore-check'/accept;stage.mkdir(parents=True);area.mkdir(parents=True)
    database=stage/'credential-excluded-operational.sqlite3'
    with sqlite3.connect(database) as db:db.executescript('CREATE TABLE jobs(id);CREATE TABLE credentials(id);')
    database.chmod(0o600);dbdata=database.read_bytes()
    contents={'database/credential-excluded-operational.sqlite3':dbdata,'tweets/completed-jobs.jsonl':b'{"owned":true}\n'}
    manifest={'version':'cloudbase-operational-backup-v1','operation_id':op,'snapshot':{'excluded_tables':{'credentials':0}},'source_files_deleted':False,'production_database_modified':False,
        'files':{n:{'bytes':len(d),'sha256':p.sha(d)} for n,d in contents.items()}}
    raw=p.encoded(manifest);put(stage/'archive-manifest.json',raw);contents['MANIFEST.json']=raw
    archive=stage/'operational-backup.tar.gz'
    with tarfile.open(archive,'w:gz') as tar:
        for name,data in contents.items():m=tarfile.TarInfo(name);m.size=len(data);m.mode=0o600;tar.addfile(m,io.BytesIO(data))
    archive.chmod(0o600);data=archive.read_bytes();archivehash=p.sha(data);key=p.PREFIX+'/'+remoteop+'/'+archivehash+'.archive';mkey=p.PREFIX+'/'+remoteop+'/manifest.json'
    receipt={'version':'cloudbase-backup-v1','operation_id':remoteop,'env_id':p.ENV,'kind':'database_snapshot','local_archive':archive.relative_to(root).as_posix(),'archive_key':key,
        'bytes':len(data),'sha256':archivehash,'created_at':now,'remote_verified':True,'local_deletion_performed':False,'archive_file_id':'cos://'+p.BUCKET+'/'+key,
        'archive_readback':{'bytes':len(data),'sha256':archivehash,'method':'full_remote_get'}}
    wire=p.encoded(receipt);receipt.update({'manifest_key':mkey,'manifest_file_id':'cos://'+p.BUCKET+'/'+mkey,'manifest_sha256':p.sha(wire),
        'manifest_readback':{'bytes':len(wire),'sha256':p.sha(wire),'method':'full_remote_get'},'verified_at':now,'verification_method':'archive_and_manifest_full_remote_get'})
    receiptfile=put(root/'backups/cloudbase-receipts'/(remoteop+'.verified.json'),receipt)
    canaryfile=root/'data/cloudbase-backup-canary-checks'/(op+'.json')
    check={'version':'cloudbase-runtime-private-canary-v1','operation_id':op,'checked_at':now,'private_storage_verified':True,'canary':{'bytes':106,'sha256':'1'*64},
        'signed_full_get':{'bytes':106,'sha256':'1'*64,'method':'full_remote_get'},'anonymous_observations':[{'host':h,'method':'GET','signed':False,'status':403} for h in (p.BUCKET+'.cos.ap-shanghai.myqcloud.com',p.BUCKET+'.tcb.qcloud.la')]}
    put(canaryfile,check);fresh={'file':str(canaryfile),'sha256':p.sha(canaryfile.read_bytes()),'private_storage_verified':True}
    remote={**receipt,'receipt_file':str(receiptfile)};runtime={'operation_id':op,'remote':remote,'bundle':{'archive':str(archive),'bytes':len(data),'sha256':archivehash,'manifest':manifest},'fresh_private_canary':fresh}
    put(area/'runtime-result.json',runtime);put(area/'remote-archive.tar.gz',data)
    restored=[]
    for name,b in contents.items():f=put(area/'restored'/name,b);restored.append({'file':str(f),'bytes':len(b),'sha256':p.sha(b),'remote_member':name})
    stagec=[{'file':str(f),'bytes':len(b),'sha256':p.sha(b),'scope':'this_exact_operation_stage_only'} for f,b in ((archive,data),(database,dbdata),(stage/'archive-manifest.json',raw))]
    reportfile=area/'restoration-report.json';report={'version':'cloudbase-actual-remote-restore-acceptance-v1','acceptance_id':accept,'operation_id':op,'verified_at':now,
        'actual_archive_get_captured':True,'all_entries_verified':True,'credential_exclusions_verified':True,'additional_remote_gets':0,'local_deletions':0,'sqlite_integrity':'ok','sqlite_foreign_keys':'ok',
        'remote_archive_reference':receipt['archive_file_id'],'remote_manifest_reference':receipt['manifest_file_id'],'archive':{'file':str(area/'remote-archive.tar.gz'),'bytes':len(data),'sha256':archivehash},
        'stage_prune_candidates':stagec,'restore_test_prune_candidates':[{'file':str(area/'remote-archive.tar.gz'),'bytes':len(data),'sha256':archivehash},*restored],
        'entries':len(contents),'keep_files':[str(area/'runtime-result.json'),str(reportfile),str(receiptfile)],'fresh_private_canary':fresh}
    put(reportfile,report)
    ledger={'operation':{'state':'verified','archive':str(archive),'receipt':str(receiptfile),'error_code':None},'requests':[{'method':m,'state':'done','error_code':None} for m in ('GET','GET','GET','PUT','GET','PUT','GET')]}
    return root,reportfile,report,runtime,ledger

def test_exact_plan_checks_remote_entry_bytes_hashes_and_readonly_sqlite_integrity(owned):
    root,file,report,runtime,ledger=owned;plan=p.create_plan(file,root,lambda *_:ledger)
    assert len(plan['candidates'])==7 and plan['deleted_files']==0 and all(x['state']=='pending' for x in plan['candidates'])
    assert file.exists() and all(Path(x['file']).exists() for x in plan['candidates'])
    assert p.revalidate_plan(plan,file,root,lambda *_:ledger)==plan

@pytest.mark.parametrize('change',['state','requests','unknown','fresh','business','symlink','changed','mtime'])
def test_nonverified_unknown_stale_or_changed_sources_do_not_make_plan(owned,change,monkeypatch):
    root,file,report,runtime,ledger=owned
    if change=='state':ledger['operation']['state']='upload_unknown'
    elif change=='requests':ledger['requests'].pop()
    elif change=='unknown':ledger['requests'][3]['state']='unknown'
    elif change=='fresh':report['verified_at']='2026-01-01T00:00:00+00:00';put(file,report)
    elif change=='business':report['stage_prune_candidates'][0]['file']=str(root/'data/hub.sqlite3');put(file,report)
    elif change=='symlink':
        original=p.Path.is_symlink;target=Path(runtime['bundle']['archive'])
        monkeypatch.setattr(p.Path,'is_symlink',lambda value:value==target or original(value))
    elif change=='changed':put(Path(runtime['bundle']['archive']),b'changed bytes')
    elif change=='mtime':
        plan=p.create_plan(file,root,lambda *_:ledger);f=Path(plan['candidates'][0]['file']);s=f.stat();os.utime(f,ns=(s.st_atime_ns,s.st_mtime_ns+1_000_000_000))
        with pytest.raises(AssertionError):p.revalidate_plan(plan,file,root,lambda *_:ledger)
        return
    with pytest.raises(AssertionError):p.create_plan(file,root,lambda *_:ledger)

def test_completed_journal_retains_every_evidence_record_and_only_exact_candidates(owned):
    root,file,report,runtime,ledger=owned;plan=p.create_plan(file,root,lambda *_:ledger)
    plan['candidates'][0]['file']=str(root/'logs/production.log')
    with pytest.raises(AssertionError):p.revalidate_plan(plan,file,root,lambda *_:ledger)


def test_canary_for_a_different_bucket_is_refused_even_when_receipts_match(owned):
    root,file,report,runtime,ledger=owned
    path=Path(report['fresh_private_canary']['file'])
    check=json.loads(path.read_bytes())
    check['anonymous_observations'][1]['host']='different-bucket.tcb.qcloud.la'
    put(path,check)
    report['fresh_private_canary']['sha256']=p.sha(path.read_bytes())
    runtime['fresh_private_canary']=report['fresh_private_canary']
    put(file.parent/'runtime-result.json',runtime)
    put(file,report)
    with pytest.raises(AssertionError):
        p.create_plan(file,root,lambda *_:ledger)


def test_interrupted_unlink_window_accepts_absence_only_after_durable_deleting_state(owned,monkeypatch):
    root,file,report,runtime,ledger=owned;plan=p.create_plan(file,root,lambda *_:ledger)
    target=Path(plan['candidates'][0]['file']);original=p.Path.exists
    monkeypatch.setattr(p.Path,'exists',lambda value:False if value==target else original(value))
    with pytest.raises(AssertionError):p.revalidate_plan(plan,file,root,lambda *_:ledger)
    plan['candidates'][0]['state']='deleting'
    assert p.revalidate_plan(plan,file,root,lambda *_:ledger)==plan
    assert original(target)  # No actual unlink was performed.


def test_receipt_hash_or_keep_file_binding_change_is_rejected(owned):
    root,file,report,runtime,ledger=owned
    report['keep_files'].append(str(root/'data/hub.sqlite3'));put(file,report)
    with pytest.raises(AssertionError):p.create_plan(file,root,lambda *_:ledger)
