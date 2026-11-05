"""Run inside the isolated sidecar. Tee its already-budgeted archive GET.

No extra GET, delete/list/ACL call, or source mutation. This helper is an explicit
operator acceptance mount, not an undisclosed change to production verification.
"""
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import re
import sqlite3
import sys
import tarfile
import tomllib
from urllib.parse import urlsplit
import uuid

sys.path.insert(0,'/opt/lighthouse-hub')

from adapters.safe_https import open_request
from adapters.cloudbase_backup import _atomic_receipt,_private_directory,hash_file
from adapters.cloudbase_cos_backup import CloudBaseCOSConfig,CloudBaseCOSStorage
from services.hub.cloudbase_backup_runtime import BackupLimits,CloudBaseBackupRuntime,ListRates
from services.hub.errors import HubError

ROOT=Path('/opt/lighthouse-hub')


def run():
    assert os.name=='posix' and os.getuid()==10001
    policy=ROOT/'config/cloudbase-backup.toml'
    assert policy.is_file() and not policy.is_symlink() and policy.stat().st_size<=65536 and policy.stat().st_mode & 0o022==0
    body=policy.read_bytes();value=tomllib.loads(body.decode('utf-8-sig'));options=dict(value['cloudbase'])
    reference=options.pop('private_canary_reference');canary_sha=options.pop('private_canary_sha256');secret=Path(options.pop('credential_file'))
    options['storage_hosts']=tuple(options['storage_hosts']);cfg=CloudBaseCOSConfig(**options);cfg.validate()
    limits=BackupLimits(**value.get('limits',{}));rates=ListRates(**value.get('list_rates',{}))
    def credentials():
        assert secret.is_absolute() and secret.is_file() and not secret.is_symlink() and secret.stat().st_size<=16384
        assert secret.stat().st_mode & 0o007==0
        result=json.loads(secret.read_bytes());assert isinstance(result,dict)
        assert not set(result)-{'SecretId','SecretKey','Token','ExpirationEpoch'}
        return result
    acceptance=str(uuid.uuid4());area=ROOT/'backups/cloudbase-restore-check'/acceptance;_private_directory(area)
    captured=[];open_files=[]
    class TeeResponse:
        def __init__(self,response):
            self.original=response;self.status=response.status;self.count=0;self.digest=hashlib.sha256()
            self.path=area/('remote-archive-'+str(len(captured))+'.partial')
            self.stream=self.path.open('xb');self.path.chmod(0o600);self.complete=False
            captured.append(self);open_files.append(self.stream)
        def read(self,amount):
            data=self.original.read(amount)
            if data:
                self.count+=len(data)
                if self.count>limits.max_archive_bytes+1:raise HubError('CLOUDBASE_ACCEPTANCE_CAPTURE_LIMIT')
                self.stream.write(data);self.digest.update(data)
            elif not self.complete:
                self.stream.flush();os.fsync(self.stream.fileno());self.stream.close();self.complete=True
            return data
        def getheader(self,*args):return self.original.getheader(*args)
    def tee_request(url,hosts,*,method='GET',headers=None,body=None,timeout=20):
        connection,response=open_request(url,hosts,method=method,headers=headers,body=body,timeout=timeout)
        parsed=urlsplit(url)
        # Precisely the ordinary archive full GET, never the canary, manifest,
        # or an arbitrary URL. Signing headers stay only in memory.
        archive_path=r'/'+re.escape(cfg.object_prefix)+r'/[a-f0-9-]{36}/[a-f0-9]{64}\.archive'
        if method=='GET' and parsed.hostname==cfg.host and headers and headers.get('Authorization') and re.fullmatch(archive_path,parsed.path):
            return connection,TeeResponse(response)
        return connection,response
    storage=CloudBaseCOSStorage(cfg,credentials,request=tee_request)
    runtime=CloudBaseBackupRuntime(ROOT,cfg,limits,private_canary_reference=reference,private_canary_sha256=canary_sha,
        policy_sha256=hashlib.sha256(body).hexdigest(),storage=storage,list_rates=rates)
    try:
        result=runtime.run_once();_atomic_receipt(area/'runtime-result.json',result)
        assert len(captured)==1 and captured[0].complete
        capture=captured[0];remote=result['remote'];bundle=result['bundle'];operation=result['operation_id']
        assert capture.count==remote['bytes']==bundle['bytes'] and capture.digest.hexdigest()==remote['sha256']==bundle['sha256']
        archive=area/'remote-archive.tar.gz';assert not archive.exists();os.replace(capture.path,archive)
        assert hash_file(archive)==remote['sha256']
        restore=area/'restored';_private_directory(restore)
        restored=[]
        with tarfile.open(archive,'r:gz') as tar:
            members=tar.getmembers();assert 1<len(members)<6000
            names=[];total=0
            for member in members:
                path=PurePosixPath(member.name)
                assert member.isfile() and not path.is_absolute() and '..' not in path.parts and all(':' not in p for p in path.parts)
                assert member.name not in names;names.append(member.name);total+=member.size
            assert total<=limits.max_uncompressed_bytes+5_000_000
            meta=tar.getmember('MANIFEST.json');assert meta.size<=5_000_000
            manifest_raw=tar.extractfile(meta).read();manifest=json.loads(manifest_raw)
            assert manifest['version']=='cloudbase-operational-backup-v1' and manifest['operation_id']==operation
            assert set(names)==set(manifest['files'])|{'MANIFEST.json'}
            for member in members:
                expected=({'bytes':len(manifest_raw),'sha256':hashlib.sha256(manifest_raw).hexdigest()} if member.name=='MANIFEST.json' else manifest['files'][member.name])
                assert member.size==expected['bytes']
                target=restore/member.name;assert target.resolve().is_relative_to(restore.resolve())
                assert not target.is_symlink() and not any(p.is_symlink() for p in target.parents)
                target.parent.mkdir(mode=0o700,parents=True,exist_ok=True);digest=hashlib.sha256();count=0
                with tar.extractfile(member) as source,target.open('xb') as output:
                    target.chmod(0o600)
                    while True:
                        chunk=source.read(1048576)
                        if not chunk:break
                        count+=len(chunk);assert count<=member.size;digest.update(chunk);output.write(chunk)
                    output.flush();os.fsync(output.fileno())
                assert count==expected['bytes'] and digest.hexdigest()==expected['sha256']
                restored.append({'file':str(target),'bytes':count,'sha256':digest.hexdigest(),'remote_member':member.name})
        database=restore/'database/credential-excluded-operational.sqlite3'
        db=sqlite3.connect(database.as_uri()+'?mode=ro&immutable=1',uri=True)
        try:
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
            tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in manifest['snapshot']['excluded_tables']:
                assert re.fullmatch('[A-Za-z0-9_]+',table) and table in tables
                assert db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0
            counts={table:db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ('jobs','publications','events','audit') if table in tables}
        finally:db.close()
        stage=ROOT/'backups/cloudbase-staging'/operation
        assert stage.is_dir() and not stage.is_symlink() and stage.stat().st_uid==os.getuid() and stage.stat().st_mode&0o077==0
        candidates=[]
        for path,size,sha,relation in (
            (stage/'operational-backup.tar.gz',remote['bytes'],remote['sha256'],'full_remote_archive_get'),
            (stage/'credential-excluded-operational.sqlite3',manifest['files']['database/credential-excluded-operational.sqlite3']['bytes'],
             manifest['files']['database/credential-excluded-operational.sqlite3']['sha256'],'remote_member'),
            (stage/'archive-manifest.json',len(manifest_raw),hashlib.sha256(manifest_raw).hexdigest(),'remote_inner_manifest')):
            assert path.parent==stage and not path.is_symlink() and not any(p.is_symlink() for p in path.parents)
            assert path.is_file() and path.stat().st_size==size and hash_file(path)==sha
            candidates.append({'file':str(path),'bytes':size,'sha256':sha,'remote_binding':relation,'scope':'this_exact_operation_stage_only'})
        report={'version':'cloudbase-actual-remote-restore-acceptance-v1','acceptance_id':acceptance,'operation_id':operation,
            'verified_at':datetime.now(timezone.utc).isoformat(),'actual_archive_get_captured':True,'additional_remote_gets':0,
            'archive':{'file':str(archive),'bytes':archive.stat().st_size,'sha256':hash_file(archive)},
            'remote_archive_reference':remote['archive_file_id'],'remote_manifest_reference':remote['manifest_file_id'],
            'all_entries_verified':True,'entries':len(restored),'sqlite_integrity':'ok','sqlite_foreign_keys':'ok','restored_counts':counts,
            'credential_exclusions_verified':True,'stage_prune_candidates':candidates,
            'restore_test_prune_candidates':[{'file':str(archive),'bytes':archive.stat().st_size,'sha256':hash_file(archive)},*restored],
            'keep_files':[str(area/'runtime-result.json'),str(area/'restoration-report.json'),remote['receipt_file']],
            'budget':result['budget'],'fresh_private_canary':result['fresh_private_canary'],
            'historical_raw_backups_migrated':False,'local_deletions':0,'core_alerts_integrated':False}
        _atomic_receipt(area/'restoration-report.json',report)
        print(json.dumps({'state':'remote_archive_restored_and_verified','operation_id':operation,'entries':len(restored),
            'sqlite_integrity':'ok','sqlite_foreign_keys':'ok','additional_remote_gets':0,'stage_prune_candidates':len(candidates),
            'report_file':str(area/'restoration-report.json'),'local_deletions':0}))
        return 0
    except BaseException as exc:
        for stream in open_files:
            if not stream.closed:stream.flush();os.fsync(stream.fileno());stream.close()
        _atomic_receipt(area/'acceptance-failure.json',{'version':'cloudbase-restore-acceptance-failure-v1',
            'at':datetime.now(timezone.utc).isoformat(),'error_code':getattr(exc,'code',type(exc).__name__),
            'additional_remote_gets':0,'local_deletions':0,'captured_partial_files_retained':True})
        print(json.dumps({'state':'stopped_safely','error_code':getattr(exc,'code',type(exc).__name__),
            'report_file':str(area/'acceptance-failure.json'),'local_deletions':0}));return 2


if __name__=='__main__':raise SystemExit(run())
