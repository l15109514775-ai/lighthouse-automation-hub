"""Byte-exact archive locations, bounded restore, and two-phase local cleanup.

No network client, cloud deletion, publication, recursive filesystem operation,
or main-job mutation lives here. Register only between the existing complete
restore acceptance and clone prune. Deletion additionally needs a root-owned
release bound to actual isolated tests and these exact source files.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import tarfile
import time
import uuid
import xml.etree.ElementTree as ET

from .db import encode
from .errors import HubError
from .locks import resource_guard

VERSION='archive-source-retention-v2'
AUTHORIZATION_VERSION='owner-local-retention-instruction-v2'
EXPORT_VERSION='recoverable-source-locations-v2'
INDEX_RELATIVE=Path('data/archive-source-retention-v2/index.sqlite3')
RELEASE_RELATIVE=Path('config/archive-source-retention-v2-release.json')
SOURCE_FILES=('services/hub/archive_source_retention_v2.py','services/hub/cloudbase_backup_runtime.py',
    'scripts/ops/prune_cloudbase_verified_clones_v1.py')
REQUIRED_TEST_GROUPS={'tests.test_archive_source_retention_v2':31,
    'tests.ops.test_cloudbase_archive_registration_hook_v2':5,'tests.test_cloudbase_backup_runtime':36,
    'tests.test_cloudbase_backup':44,'tests.test_cloudbase_cos_backup':23}
SHA=re.compile('[a-f0-9]{64}')
SEAL_FIELDS={'version','at','level','queue','state','error_code','job_id','event'}


def fail(code):
    raise HubError(code,review=True)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def _path(root,path):
    root=Path(root);path=Path(path)
    if (not root.is_absolute() or root.resolve()!=root or not path.is_absolute() or
            path.resolve()!=path or not path.is_relative_to(root) or
            any(p.is_symlink() for p in (path,*path.parents))):
        fail('ARCHIVE_RETENTION_PATH_REJECTED')
    return path


def _relative(value):
    if not isinstance(value,str):fail('ARCHIVE_RETENTION_PATH_REJECTED')
    p=PurePosixPath(value)
    if (p.is_absolute() or not p.parts or str(p)!=value or
            any(part in ('.','..') or ':' in part or '\\' in part for part in p.parts)):
        fail('ARCHIVE_RETENTION_PATH_REJECTED')
    if (p.parts[0]=='media' and len(p.parts)>2 and _uuid(p.parts[1]) or
            p.parts[:2]==('data','model-receipts') and len(p.parts)>3 and _uuid(p.parts[2]) or
            p.parts[:2]==('logs','sealed-v2') and len(p.parts)==3 and p.suffix=='.jsonl' and _uuid(p.stem)):
        if any(x.lower() in ('browser-profiles','secrets','youtube-download-v1','youtube-highest-v2','parts-v1') for x in p.parts):
            fail('ARCHIVE_RETENTION_PROTECTED_PATH')
        if p.suffix.lower() in ('.mp4','.mkv','.m4a','.ts','.webm','.part'):
            fail('ARCHIVE_RETENTION_VIDEO_REQUIRES_SEPARATE_POLICY')
        return p
    fail('ARCHIVE_RETENTION_PROTECTED_PATH')


def _uuid(value):
    try:return str(uuid.UUID(value))==value
    except (TypeError,ValueError,AttributeError):return False


def _inspect(root,path,maximum=50_000_000):
    path=_path(root,path)
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_BINARY',0))
    try:
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 0<before.st_size<=maximum or before.st_nlink!=1:
            fail('ARCHIVE_RETENTION_FILE_REJECTED')
        checksum=hashlib.sha256()
        while block:=os.read(fd,1_048_576):checksum.update(block)
        after=os.fstat(fd)
        if any(getattr(before,key)!=getattr(after,key) for key in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')):
            fail('ARCHIVE_RETENTION_FILE_CHANGED')
    finally:os.close(fd)
    parent=path.parent.stat()
    return {'file':str(path),'bytes':before.st_size,'sha256':checksum.hexdigest(),'device':before.st_dev,
        'inode':before.st_ino,'mtime_ns':before.st_mtime_ns,'parent_device':parent.st_dev,'parent_inode':parent.st_ino}


def _atomic(path,value):
    if path.is_symlink():fail('ARCHIVE_RETENTION_PATH_REJECTED')
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.partial')
    fd=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY|getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(fd,'wb') as stream:
        stream.write(encode(value).encode());stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)
    if os.name=='posix':
        fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|getattr(os,'O_NOFOLLOW',0))
        try:os.fsync(fd)
        finally:os.close(fd)


def _root_protected(path):
    if not path.is_absolute() or path.resolve()!=path or any(p.is_symlink() for p in (path,*path.parents)):
        fail('ARCHIVE_RETENTION_RESOLVER_NOT_READY')
    if os.name=='posix':
        for current in (path,*path.parents):
            info=current.stat()
            if info.st_uid!=0 or info.st_mode&0o022:fail('ARCHIVE_RETENTION_RESOLVER_NOT_READY')


def _app_ledger_reader(root,operation):
    path=_path(root,root/'data/cloudbase-backup-state/ledger.sqlite3')
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        row=db.execute('SELECT * FROM operations WHERE operation_id=?',(operation,)).fetchone()
        calls=[dict(x) for x in db.execute('SELECT method,state,error_code FROM requests WHERE operation_id=? ORDER BY created_at,request_id',(operation,))]
        return {'operation':dict(row) if row else None,'requests':calls}
    finally:db.close()


def _owner_map(connection,root,completed):
    """Map a file owner to actual completed tweet roots, including cached X calls."""
    result={job:{job} for job in completed};tables={r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for row in connection.execute("SELECT job_id,result FROM jobs WHERE source_platform='machine_model'"):
        parent=json.loads(row['result']).get('parent_job_id')
        if parent in completed:result.setdefault(row['job_id'],set()).add(parent)
    for table in ('tweet_source_bindings','web_source_bindings','extended_web_source_bindings'):
        if table in tables:
            for row in connection.execute('SELECT job_id,discovery_job_id FROM '+table):
                if row['job_id'] in completed:result.setdefault(row['discovery_job_id'],set()).add(row['job_id'])
    if 'tweet_source_bindings' in tables and 'source_http_receipts' in tables:
        for row in connection.execute('''SELECT b.job_id,r.discovery_job_id FROM tweet_source_bindings b
            JOIN source_http_receipts r ON r.call_id=b.user_call_id OR r.call_id=b.timeline_call_id'''):
            if row['job_id'] in completed:result.setdefault(row['discovery_job_id'],set()).add(row['job_id'])
    return result


def validate_sealed_log(raw):
    """Only bounded structured fields; arbitrary/raw message text is never sealed."""
    if not raw or len(raw)>5_000_000:fail('ARCHIVE_RETENTION_UNSEALED_LOG')
    for line in raw.splitlines():
        try:item=json.loads(line)
        except (ValueError,UnicodeError):fail('ARCHIVE_RETENTION_UNSEALED_LOG')
        if not isinstance(item,dict) or set(item)-SEAL_FIELDS or item.get('version')!='lighthouse-sealed-log-v2':
            fail('ARCHIVE_RETENTION_UNSEALED_LOG')
        for key,value in item.items():
            if key=='version':continue
            valid=(key=='at' and type(value) in (int,float) and 0<=value<=time.time()+30 or
                key=='level' and value in ('DEBUG','INFO','WARNING','ERROR','CRITICAL') or
                key=='queue' and value in ('tweet','video','system') or
                key=='state' and value in ('pending','processing','completed','failed','publish_unknown','awaiting_review') or
                key=='error_code' and isinstance(value,str) and re.fullmatch('[A-Z][A-Z0-9_]{1,79}',value) and
                    value.startswith(('AUTH_','API_','YOUTUBE_','BILIBILI_','CLOUDBASE_','MEDIA_','REPLAY_','SHORT_',
                        'X_','MODEL_','DISK_','PIPELINE_','UNEXPECTED_','RETRY_')) or
                key=='job_id' and _uuid(value) or
                key=='event' and value in ('task.failed','auth.expired','review.required','disk.low','pipeline.stalled'))
            if not valid:fail('ARCHIVE_RETENTION_UNSEALED_LOG')
    return True


def sealed_log_exports(root,maximum):
    folder=Path(root)/'logs/sealed-v2';exports={};total=0
    if not folder.exists():return exports
    _path(root,folder)
    for file in sorted(folder.iterdir()):
        relative=file.relative_to(root).as_posix();_relative(relative)
        actual=_inspect(root,file,maximum=maximum);raw=file.read_bytes()
        if file.stat().st_mode&0o222:fail('ARCHIVE_RETENTION_UNSEALED_LOG')
        if hashlib.sha256(raw).hexdigest()!=actual['sha256']:fail('ARCHIVE_RETENTION_FILE_CHANGED')
        validate_sealed_log(raw);total+=len(raw)
        if total>maximum:fail('CLOUDBASE_BACKUP_EXPORT_SIZE_LIMIT')
        exports['sealed-logs/'+relative]=raw
    return exports


def seal_structured_logs(root,records):
    """Create a new immutable structured log segment, never mutate a raw log."""
    if not isinstance(records,list) or not 1<=len(records)<=10000:fail('ARCHIVE_RETENTION_UNSEALED_LOG')
    raw=b''.join((encode({'version':'lighthouse-sealed-log-v2',**item})+'\n').encode() for item in records)
    validate_sealed_log(raw)
    folder=Path(root)/'logs/sealed-v2';_path(root,folder);folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    file=folder/(str(uuid.uuid4())+'.jsonl')
    fd=os.open(file,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o400)
    with os.fdopen(fd,'wb') as stream:
        stream.write(raw);stream.flush();os.fsync(stream.fileno());os.fchmod(stream.fileno(),0o400) if hasattr(os,'fchmod') else None
    if os.name=='posix':
        fd=os.open(folder,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    return {'file':str(file),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}


class ArchiveSourceRetentionV2:
    def __init__(self,settings,store):
        self.settings,self.store=settings,store;self.root=Path(settings.root)
        self.folder=self.root/INDEX_RELATIVE.parent;self.path=self.root/INDEX_RELATIVE

    def initialize(self):
        _path(self.root,self.folder);self.folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        if os.name=='posix' and self.folder.stat().st_mode&0o077:fail('ARCHIVE_RETENTION_INDEX_PERMISSIONS')
        _path(self.root,self.path)
        with sqlite3.connect(self.path) as db:
            db.executescript('''PRAGMA journal_mode=WAL;PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS archives(archive_sha256 TEXT PRIMARY KEY,record TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS locations(relative_file TEXT NOT NULL,file_sha256 TEXT NOT NULL,
                archive_sha256 TEXT NOT NULL,record TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'registered',
                intent TEXT,PRIMARY KEY(relative_file,file_sha256,archive_sha256));
            CREATE TABLE IF NOT EXISTS authorizations(id TEXT PRIMARY KEY,record TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,record TEXT NOT NULL);
            ''')
        if os.name=='posix':self.path.chmod(0o600)

    @contextmanager
    def _db(self,write=False):
        _path(self.root,self.path)
        if not self.path.is_file():fail('ARCHIVE_RETENTION_INDEX_NOT_INITIALIZED')
        db=sqlite3.connect(self.path if write else self.path.as_uri()+'?mode=ro',uri=not write,timeout=15)
        db.row_factory=sqlite3.Row;db.execute('PRAGMA synchronous=FULL')
        try:
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            db.commit()
        except BaseException:db.rollback();raise
        finally:db.close()

    def register_verified_archive(self,report_file,*,ledger_reader=None):
        """Same accepted GET only, before prune; no extra API request."""
        from scripts.ops import prune_cloudbase_verified_clones_v1 as pruner
        self.initialize()
        with resource_guard(self.root/'data/cloudbase-backup-state/runner.lock',self.folder/'lock'):
            plan=pruner.create_plan(Path(report_file),self.root,ledger_reader or _app_ledger_reader)
            report,runtime,proof,area,stage=pruner.bindings(Path(report_file),self.root,ledger_reader or _app_ledger_reader)
            manifest=runtime['bundle']['manifest'];remote=runtime['remote'];archive=area/'remote-archive.tar.gz'
            actual=_inspect(self.root,archive,maximum=1_000_000_000)
            if actual['sha256']!=remote['sha256'] or actual['bytes']!=remote['bytes']:fail('ARCHIVE_RETENTION_ARCHIVE_CHANGED')
            db=sqlite3.connect((area/'restored/database/credential-excluded-operational.sqlite3').as_uri()+'?mode=ro&immutable=1',uri=True);db.row_factory=sqlite3.Row
            try:
                completed={r['job_id'] for r in db.execute("SELECT job_id FROM jobs WHERE source_platform='x' AND job_type='tweet_process' AND queue='tweet' AND state='completed'")}
                owners=_owner_map(db,self.root,completed)
            finally:db.close()
            references=[]
            for member,item in manifest['files'].items():
                if member.startswith('tweets/files/'):
                    relative=member[len('tweets/files/'):];p=_relative(relative)
                    owner=p.parts[1] if p.parts[0]=='media' else p.parts[2]
                    covered=sorted(owners.get(owner,set()))
                    if not covered:continue  # Unresolved ownership remains local.
                    kind='completed_tweet_evidence'
                elif member.startswith('sealed-logs/'):
                    relative=member[len('sealed-logs/'):];_relative(relative);covered=[];kind='sealed_structured_log'
                    validate_sealed_log((area/'restored'/member).read_bytes())
                else:continue
                references.append({'version':VERSION,'relative_file':relative,'file_sha256':item['sha256'],'bytes':item['bytes'],
                    'archive_sha256':remote['sha256'],'member':member,'covering_tweet_ids':covered,'kind':kind})
            record={'version':VERSION,'archive_sha256':remote['sha256'],'archive_bytes':remote['bytes'],
                'remote_operation_id':remote['operation_id'],
                'archive_file_id':remote['archive_file_id'],'archive_key':remote['archive_key'],
                'remote_manifest_file_id':remote['manifest_file_id'],'remote_manifest_sha256':remote['manifest_sha256'],
                'operation_id':report['operation_id'],'acceptance_id':report['acceptance_id'],
                'verified_at':report['verified_at'],'archive_manifest':manifest,'acceptance_proof':proof,
                'registration_verification':'same_full_GET_all_members_and_SQLite_integrity_FK_rechecked',
                'external_requests':0,'original_sources_deleted':False}
            with self._db(True) as index:
                previous=index.execute('SELECT record FROM archives WHERE archive_sha256=?',(remote['sha256'],)).fetchone()
                if previous and json.loads(previous['record'])!=record:fail('ARCHIVE_RETENTION_REGISTRY_CONFLICT')
                index.execute('INSERT OR IGNORE INTO archives VALUES(?,?)',(remote['sha256'],encode(record)))
                for ref in references:
                    old=index.execute('SELECT record FROM locations WHERE relative_file=? AND file_sha256=? AND archive_sha256=?',
                        (ref['relative_file'],ref['file_sha256'],ref['archive_sha256'])).fetchone()
                    if old and json.loads(old['record'])!=ref:fail('ARCHIVE_RETENTION_REGISTRY_CONFLICT')
                    index.execute('INSERT OR IGNORE INTO locations(relative_file,file_sha256,archive_sha256,record) VALUES(?,?,?,?)',
                        (ref['relative_file'],ref['file_sha256'],ref['archive_sha256'],encode(ref)))
            return {'version':VERSION,'state':'archive_locations_registered','archive_sha256':remote['sha256'],
                'operation_id':record['operation_id'],'acceptance_id':record['acceptance_id'],
                'report_sha256':proof['report']['sha256'],'mapped_files':len(references),'external_requests':0,'source_files_deleted':0}

    def verify_registration(self,report_file,expected_report_sha256):
        """Resume after clone prune without another GET or a missing-clone claim."""
        path=_path(self.root,report_file);body=path.read_bytes();report=json.loads(body)
        if (not _uuid(report.get('acceptance_id')) or
                path!=self.root/'backups/cloudbase-restore-check'/report['acceptance_id']/'restoration-report.json'):
            fail('ARCHIVE_RETENTION_LOCATION_CHANGED')
        if hashlib.sha256(body).hexdigest()!=expected_report_sha256:fail('ARCHIVE_RETENTION_LOCATION_CHANGED')
        with self._db() as db:
            rows=db.execute('SELECT archive_sha256,record FROM archives').fetchall()
            for row in rows:
                record=json.loads(row['record'])
                if record['operation_id']!=report['operation_id'] or record['acceptance_id']!=report['acceptance_id']:continue
                if record['acceptance_proof']['report']['sha256']!=expected_report_sha256:fail('ARCHIVE_RETENTION_LOCATION_CHANGED')
                locations=[json.loads(x[0]) for x in db.execute('SELECT record FROM locations WHERE archive_sha256=?',(row['archive_sha256'],))]
                for location in locations:self.validate_location(location,record)
                return {'version':VERSION,'state':'archive_locations_registered','archive_sha256':row['archive_sha256'],
                    'operation_id':report['operation_id'],'acceptance_id':report['acceptance_id'],'report_sha256':expected_report_sha256,
                    'mapped_files':len(locations),'external_requests':0,'source_files_deleted':0}
        fail('ARCHIVE_RETENTION_REGISTRATION_REQUIRED_BEFORE_PRUNE')

    def authorize(self,*,actual_owner_response,owner_response_reference,superseded_holds=None):
        if any(not isinstance(x,str) or not x.strip() or len(x)>2048 for x in (actual_owner_response,owner_response_reference)):
            fail('ARCHIVE_RETENTION_OWNER_INSTRUCTION_REQUIRED')
        holds=superseded_holds or {}
        if not isinstance(holds,dict) or any(not _uuid(k) or not SHA.fullmatch(v) for k,v in holds.items()):
            fail('ARCHIVE_RETENTION_OWNER_INSTRUCTION_REQUIRED')
        self.initialize();ident=str(uuid.uuid4())
        record={'version':AUTHORIZATION_VERSION,'authorization_id':ident,'actual_owner_response':actual_owner_response,
            'owner_response_reference':owner_response_reference,'superseded_hold_sha256':holds,
            'recorded_at':datetime.now(timezone.utc).isoformat(),'formal_workflow_launch_claimed':False,
            'remote_deletion_authorized':False,'scope':'byte_exact_verified_tweet_and_sealed_log_originals_only'}
        with self._db(True) as db:db.execute('INSERT INTO authorizations VALUES(?,?)',(ident,encode(record)))
        return record

    def authorization(self,ident):
        if not _uuid(ident):fail('ARCHIVE_RETENTION_OWNER_INSTRUCTION_REQUIRED')
        with self._db() as db:row=db.execute('SELECT record FROM authorizations WHERE id=?',(ident,)).fetchone()
        if not row:fail('ARCHIVE_RETENTION_OWNER_INSTRUCTION_REQUIRED')
        return json.loads(row['record'])

    @staticmethod
    def validate_location(location,archive):
        from scripts.ops.prune_cloudbase_verified_clones_v1 import BUCKET,PREFIX
        relative=_relative(location['relative_file']);item=archive['archive_manifest']['files'].get(location['member'])
        prefix='sealed-logs/' if location['kind']=='sealed_structured_log' else 'tweets/files/'
        archive_sha=archive.get('archive_sha256');remote_operation=archive.get('remote_operation_id')
        if not isinstance(archive_sha,str) or not SHA.fullmatch(archive_sha) or not _uuid(remote_operation):
            fail('ARCHIVE_RETENTION_LOCATION_CHANGED')
        key=PREFIX+'/'+remote_operation+'/'+archive_sha+'.archive';mkey=PREFIX+'/'+remote_operation+'/manifest.json'
        if (location['version']!=VERSION or archive['version']!=VERSION or location['member']!=prefix+relative.as_posix() or
                not SHA.fullmatch(location['file_sha256']) or location['archive_sha256']!=archive['archive_sha256'] or
                type(location['bytes']) is not int or not 0<location['bytes']<=50_000_000 or
                item!={'bytes':location['bytes'],'sha256':location['file_sha256']} or
                archive['archive_key']!=key or archive['archive_file_id']!='cos://'+BUCKET+'/'+key or
                archive['remote_manifest_file_id']!='cos://'+BUCKET+'/'+mkey or
                not SHA.fullmatch(archive['remote_manifest_sha256']) or not _uuid(archive['operation_id']) or
                archive['archive_manifest'].get('operation_id')!=archive['operation_id'] or
                type(archive['archive_bytes']) is not int or not 0<archive['archive_bytes']<=1_000_000_000 or
                sum(item['bytes'] for item in archive['archive_manifest']['files'].values())>1_000_000_000):
            fail('ARCHIVE_RETENTION_LOCATION_CHANGED')
        return location

    def lookup(self,file,expected_sha256,*,deleted_only=False):
        path=_path(self.root,file);relative=path.relative_to(self.root).as_posix();_relative(relative)
        if not self.path.exists():return None
        with self._db() as db:
            rows=db.execute('''SELECT l.record,l.state,l.intent,a.record archive FROM locations l JOIN archives a
                ON a.archive_sha256=l.archive_sha256 WHERE l.relative_file=? AND l.file_sha256=? ORDER BY a.rowid DESC''',
                (relative,expected_sha256)).fetchall()
        for row in rows:
            if deleted_only and row['state'] not in ('deleting','deleted'):continue
            result={'location':json.loads(row['record']),'archive':json.loads(row['archive']),'state':row['state']}
            self.validate_location(result['location'],result['archive'])
            if row['state'] in ('deleting','deleted') and not row['intent']:fail('ARCHIVE_RETENTION_LOCATION_CHANGED')
            return result
        return None

    def backup_export(self,completed_tweets=None):
        """Preserve transitive recovery metadata after originals/clone tar are gone."""
        if not self.path.exists():return None
        archives={};locations=[]
        with self._db() as db:
            rows=db.execute("SELECT l.record,l.state,l.intent,a.record archive FROM locations l JOIN archives a ON a.archive_sha256=l.archive_sha256 WHERE l.state IN ('deleting','deleted')").fetchall()
        for row in rows:
            location=json.loads(row['record']);archive=json.loads(row['archive']);self.validate_location(location,archive)
            path=_path(self.root,self.root/location['relative_file'])
            if path.exists():continue
            if not row['intent']:fail('ARCHIVE_RETENTION_LOCATION_CHANGED')
            if (completed_tweets is not None and location['kind']=='completed_tweet_evidence' and
                    not set(location['covering_tweet_ids'])&set(completed_tweets)):continue
            archives[archive['archive_sha256']]=archive
            locations.append(location)
        if not locations:return None
        return {'version':EXPORT_VERSION,'archives':archives,'locations':locations,
            'old_remote_archives_must_be_retained':True,'requires_transitive_archive_restore':True,
            'credentials_included':False,'source_file_contents_embedded':False}

    @staticmethod
    def restore_bytes(reference,archive_file):
        """Actual bounded local restore from the cryptographically bound GET file.

        A budgeted storage adapter can obtain that file. This function never
        makes an extra GET and never accepts a URL or a passed-boolean receipt.
        """
        location,record=reference['location'],reference['archive']
        ArchiveSourceRetentionV2.validate_location(location,record)
        path=Path(archive_file)
        if path.is_symlink() or not path.is_file() or any(p.is_symlink() for p in path.parents):fail('ARCHIVE_RETENTION_PATH_REJECTED')
        before=path.stat()
        if before.st_size!=record['archive_bytes'] or before.st_size>1_000_000_000:fail('ARCHIVE_RETENTION_ARCHIVE_CHANGED')
        with path.open('rb') as stream:checksum=hashlib.file_digest(stream,'sha256').hexdigest()
        if checksum!=record['archive_sha256']:fail('ARCHIVE_RETENTION_ARCHIVE_CHANGED')
        members={**record['archive_manifest']['files'],'MANIFEST.json':None};answer=None
        with tarfile.open(path,'r:gz') as archive:
            entries=archive.getmembers()
            if len(entries)!=len(members) or {x.name for x in entries}!=set(members) or len(entries)>6000:
                fail('ARCHIVE_RETENTION_ARCHIVE_CHANGED')
            for item in entries:
                if not item.isfile() or item.issym() or item.islnk():fail('ARCHIVE_RETENTION_ARCHIVE_CHANGED')
                if item.name=='MANIFEST.json':
                    if item.size>5_000_000:fail('ARCHIVE_RETENTION_ARCHIVE_CHANGED')
                    if json.load(archive.extractfile(item))!=record['archive_manifest']:fail('ARCHIVE_RETENTION_ARCHIVE_CHANGED')
                    continue
                wanted=members[item.name]
                if item.size!=wanted['bytes'] or item.size>50_000_000:fail('ARCHIVE_RETENTION_ARCHIVE_CHANGED')
                body=archive.extractfile(item).read(item.size+1)
                if len(body)!=item.size or hashlib.sha256(body).hexdigest()!=wanted['sha256']:fail('ARCHIVE_RETENTION_ARCHIVE_CHANGED')
                if item.name==location['member']:answer=body
        after=path.stat()
        if any(getattr(before,key)!=getattr(after,key) for key in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')) or answer is None:
            fail('ARCHIVE_RETENTION_ARCHIVE_CHANGED')
        return answer

    def _business(self,connection,location,authorization):
        if location['kind']=='sealed_structured_log':return {'kind':'sealed_structured_log'}
        jobs={r['job_id']:dict(r) for r in connection.execute('SELECT * FROM jobs')}
        roots=location['covering_tweet_ids']
        if len(roots)!=1:fail('ARCHIVE_RETENTION_SHARED_SOURCE')
        rootjob=jobs.get(roots[0])
        if (not rootjob or rootjob['state']!='completed' or rootjob['source_platform']!='x' or
                rootjob['job_type']!='tweet_process' or rootjob.get('lease_owner') or rootjob.get('lease_until')):
            fail('ARCHIVE_RETENTION_JOB_NOT_COMPLETED')
        owner=PurePosixPath(location['relative_file']).parts
        owner_id=owner[1] if owner[0]=='media' else owner[2]
        if owner_id not in jobs:fail('ARCHIVE_RETENTION_OWNER_UNKNOWN')
        if jobs[owner_id]['state'] not in ('completed','failed') or jobs[owner_id].get('lease_owner') or jobs[owner_id].get('lease_until'):
            fail('ARCHIVE_RETENTION_FILE_IN_USE')
        all_tweets={job_id for job_id,job in jobs.items() if job['source_platform']=='x' and job['job_type']=='tweet_process'}
        dynamic_owners=_owner_map(connection,self.root,all_tweets).get(owner_id,set())
        if dynamic_owners-set(roots):fail('ARCHIVE_RETENTION_SHARED_SOURCE')
        path=str(self.root/location['relative_file']);references=set()
        def visit(value):
            if isinstance(value,dict):return any(visit(x) for x in value.values())
            if isinstance(value,list):return any(visit(x) for x in value)
            return value==path
        for job in jobs.values():
            if visit(json.loads(job['payload'])) or visit(json.loads(job['result'])):
                parent=json.loads(job['result']).get('parent_job_id');logical=parent if parent in jobs else job['job_id']
                references.add(logical)
                if job['state'] not in ('completed','failed') or job.get('lease_owner') or job.get('lease_until'):
                    fail('ARCHIVE_RETENTION_FILE_IN_USE')
        if references-set(roots)-{owner_id}:fail('ARCHIVE_RETENTION_SHARED_SOURCE')
        for job_id in {owner_id,*roots}:
            result=json.loads(jobs[job_id]['result'])
            def holds(value):
                if isinstance(value,dict):
                    for key,item in value.items():
                        if key in ('test_retention_policy','retention_hold','local_retention_hold'):
                            yield item
                        yield from holds(item)
                elif isinstance(value,list):
                    for item in value:yield from holds(item)
            allholds=list(holds(result))
            if allholds and authorization['superseded_hold_sha256'].get(job_id)!=digest(allholds):
                fail('ARCHIVE_RETENTION_HOLD_ACTIVE')
        publications=[dict(row) for row in connection.execute('SELECT * FROM publications WHERE job_id=?',(roots[0],))]
        target=str(self.settings.config['target_bilibili_site_uid'])
        if (not publications or any(row['state']!='completed' or not row['external_id'] or not row['external_url'] or
                row['platform']!='bilibili' or row['source_platform']!='x' or row['source_id']!=rootjob['source_id'] or
                row['account_id']!='site_uid:'+target for row in publications)):
            fail('ARCHIVE_RETENTION_PUBLICATION_UNRESOLVED')
        return {'kind':'completed_tweet_evidence','job_id':roots[0],'job_sha256':digest(rootjob),
            'owner_job_sha256':digest(jobs[owner_id]),'publication_sha256':digest(publications)}

    def plan(self,files,*,authorization_id):
        authorization=self.authorization(authorization_id)
        if not isinstance(files,list) or not 1<=len(files)<=5000 or len(set(map(str,files)))!=len(files):
            fail('ARCHIVE_RETENTION_EXPLICIT_FILES_REQUIRED')
        candidates=[];rejected=[]
        with self.store.transaction() as connection:
            for file in files:
                try:
                    path=_path(self.root,file);_relative(path.relative_to(self.root).as_posix())
                    actual=_inspect(self.root,path);reference=self.lookup(path,actual['sha256'])
                    if not reference or reference['location']['bytes']!=actual['bytes']:fail('ARCHIVE_RETENTION_NOT_BYTE_EXACT_BACKED_UP')
                    business=self._business(connection,reference['location'],authorization)
                    candidates.append({**actual,'reference':reference,'business':business,'state':'pending'})
                except HubError as error:rejected.append({'file':str(file),'error_code':error.code})
        result={'version':VERSION,'plan_id':str(uuid.uuid4()),'created_at':datetime.now(timezone.utc).isoformat(),
            'authorization_id':authorization_id,'authorization_sha256':digest(authorization),
            'candidates':candidates,'rejected':rejected,'state':'prepared','source_files_deleted':0,'remote_requests':0,
            'protected_active_database_configuration_credentials_profiles_unchanged':True}
        return {**result,'plan_sha256':digest(result)}

    def _release(self,authorization_id):
        file=self.root/RELEASE_RELATIVE
        try:
            _root_protected(file);release=json.loads(file.read_bytes())
            if (release['version']!='archive-source-retention-release-v2' or release['authorization_id']!=authorization_id or
                    release['mode']!='verified_archive_members_only' or release['formal_video_launch_claimed'] is not False):
                fail('ARCHIVE_RETENTION_RESOLVER_NOT_READY')
            source_root=Path(__file__).resolve().parents[2]
            actual={name:hashlib.sha256((source_root/name).read_bytes()).hexdigest() for name in SOURCE_FILES}
            if release['source_files']!=actual:fail('ARCHIVE_RETENTION_RESOLVER_NOT_READY')
            attest_file=Path(release['test_attestation']['file']);_root_protected(attest_file)
            raw=attest_file.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=release['test_attestation']['sha256']:fail('ARCHIVE_RETENTION_RESOLVER_NOT_READY')
            attest=json.loads(raw)
            if (attest['version']!='archive-source-retention-v2-ubuntu-tests-v1' or attest['source_files']!=actual or
                    attest['network']!='none' or attest['application_data_mounted'] is not False or
                    attest['production_profile_mounted'] is not False or attest['exit_code']!=0 or
                    attest.get('full_suite_passed') is not True or attest.get('oom_killed') is not False or
                    any(attest.get(key) is not True for key in ('no_pid_or_oom_denial','runtime_matches_declared_candidate',
                        'mounts_match_isolated_allowlist'))):
                fail('ARCHIVE_RETENTION_RESOLVER_NOT_READY')
            junit=Path(attest['junit_file']);_root_protected(junit);raw=junit.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=attest['junit_sha256']:fail('ARCHIVE_RETENTION_RESOLVER_NOT_READY')
            suites=ET.fromstring(raw);cases=suites.findall('.//testcase')
            groups={}
            for case in cases:groups[case.get('classname')]=groups.get(case.get('classname'),0)+1
            if (groups!=REQUIRED_TEST_GROUPS or len(cases)!=attest['tests'] or
                    any(case.find(tag) is not None for case in cases for tag in ('failure','error','skipped'))):
                fail('ARCHIVE_RETENTION_RESOLVER_NOT_READY')
            return {'release_sha256':hashlib.sha256(file.read_bytes()).hexdigest(),'test_attestation_sha256':release['test_attestation']['sha256']}
        except (OSError,ValueError,KeyError,TypeError,ET.ParseError):fail('ARCHIVE_RETENTION_RESOLVER_NOT_READY')

    def apply(self,plan):
        """One file per durable intent; unknown unlink windows reconcile first."""
        original={k:v for k,v in plan.items() if k!='plan_sha256'}
        if plan.get('version')!=VERSION or digest(original)!=plan.get('plan_sha256') or not _uuid(plan.get('plan_id')):
            fail('ARCHIVE_RETENTION_PLAN_CHANGED')
        authorization=self.authorization(plan['authorization_id'])
        if digest(authorization)!=plan['authorization_sha256']:fail('ARCHIVE_RETENTION_PLAN_CHANGED')
        release=self._release(plan['authorization_id'])
        locks=(self.root/'data/cloudbase-backup-state/runner.lock',self.root/'data/locks/tweet',self.root/'data/locks/video',self.folder/'lock')
        with resource_guard(*locks),self.store.transaction() as connection:
            # Revalidate every item before the first unlink. Do not partly
            # process a stale list whose later item has acquired a new user.
            for item in plan['candidates']:
                path=_path(self.root,item['file']);_relative(path.relative_to(self.root).as_posix())
                current=self.lookup(path,item['sha256'])
                if not current or current['location']!=item['reference']['location'] or current['archive']!=item['reference']['archive']:
                    fail('ARCHIVE_RETENTION_PLAN_CHANGED')
                if self._business(connection,current['location'],authorization)!=item['business']:fail('ARCHIVE_RETENTION_PLAN_CHANGED')
                if path.exists():
                    actual=_inspect(self.root,path)
                    if any(actual[key]!=item[key] for key in actual):fail('ARCHIVE_RETENTION_FILE_CHANGED')
                elif current['state'] not in ('deleting','deleted'):fail('ARCHIVE_RETENTION_UNEXPECTED_MISSING_SOURCE')
            deleted=[]
            for item in plan['candidates']:
                path=Path(item['file']);location=item['reference']['location']
                intent={'version':VERSION,'plan_id':plan['plan_id'],'plan_sha256':plan['plan_sha256'],
                    'file':item['file'],'file_sha256':item['sha256'],'bytes':item['bytes'],'release':release,
                    'authorization_id':plan['authorization_id'],'recorded_at':datetime.now(timezone.utc).isoformat(),
                    'snapshot':{key:item[key] for key in ('device','inode','mtime_ns','parent_device','parent_inode')}}
                with self._db(True) as db:
                    row=db.execute('SELECT state,intent FROM locations WHERE relative_file=? AND file_sha256=? AND archive_sha256=?',
                        (location['relative_file'],location['file_sha256'],location['archive_sha256'])).fetchone()
                    if row['state'] in ('deleting','deleted'):
                        previous=json.loads(row['intent'])
                        if previous['plan_sha256']!=plan['plan_sha256']:fail('ARCHIVE_RETENTION_RECONCILIATION_REQUIRED')
                    else:db.execute("UPDATE locations SET state='deleting',intent=? WHERE relative_file=? AND file_sha256=? AND archive_sha256=?",
                        (encode(intent),location['relative_file'],location['file_sha256'],location['archive_sha256']))
                if path.exists():self._unlink_exact(item)
                with self._db(True) as db:
                    db.execute("UPDATE locations SET state='deleted' WHERE relative_file=? AND file_sha256=? AND archive_sha256=?",
                        (location['relative_file'],location['file_sha256'],location['archive_sha256']))
                deleted.append(item['file'])
            result={'version':VERSION,'plan_id':plan['plan_id'],'state':'completed','deleted_files':deleted,
                'source_files_deleted':len(deleted),'remote_requests':0,'main_job_database_modified':False,
                'archive_locations_retained':True,'remote_objects_deleted':False}
            with self._db(True) as db:db.execute('INSERT OR REPLACE INTO operations VALUES(?,?)',(plan['plan_id'],encode(result)))
            return result

    def _unlink_exact(self,item):
        path=_path(self.root,item['file']);actual=_inspect(self.root,path)
        if any(actual[key]!=item[key] for key in actual):fail('ARCHIVE_RETENTION_FILE_CHANGED')
        if os.name=='posix':
            fd=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:
                for part in path.parent.relative_to(self.root).parts:
                    child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=child
                parent=os.fstat(fd);current=os.stat(path.name,dir_fd=fd,follow_symlinks=False)
                if ((parent.st_dev,parent.st_ino)!=(item['parent_device'],item['parent_inode']) or
                        not stat.S_ISREG(current.st_mode) or current.st_nlink!=1 or
                        (current.st_dev,current.st_ino,current.st_size,current.st_mtime_ns)!=(item['device'],item['inode'],item['bytes'],item['mtime_ns'])):
                    fail('ARCHIVE_RETENTION_FILE_CHANGED')
                os.unlink(path.name,dir_fd=fd);os.fsync(fd)
            finally:os.close(fd)
        else:
            # Windows owned tests only. Deployment/apply is Linux and uses
            # directory-fd NOFOLLOW above; never compose a shell delete.
            path.unlink()
