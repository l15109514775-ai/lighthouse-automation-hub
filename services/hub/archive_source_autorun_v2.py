"""Same-GET daily source retention using the existing destructive core gate.

Every candidate originates in an actual core plan. Rejections remain separate;
the complete plan and applying state are durable before any unlink. No network,
new owner authorization, arbitrary paths or changes to the tested core.
"""
from datetime import datetime,timezone
import hashlib,json,os,uuid
from pathlib import Path

from . import archive_source_retention_v2 as core
from .db import encode
from .errors import HubError
from .locks import resource_guard

VERSION='archive-source-daily-autorun-v2'
MAX_FILES_PER_RUN=128
MAX_PLAN_BYTES=8_000_000


def _cache_files(connection,root):
    tables={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result=set()
    if not {'source_cursors','source_http_receipts'}<=tables:return result
    for row in connection.execute("SELECT value FROM source_cursors WHERE key LIKE 'x-official-author:%'"):
        try:
            value=json.loads(row[0]);call=value['user_call_id']
            if not core._uuid(call):continue
            receipt=connection.execute('SELECT discovery_job_id FROM source_http_receipts WHERE call_id=?',(call,)).fetchone()
            if receipt and core._uuid(receipt[0]):result.add(str(root/'media'/receipt[0]/'source-receipts'/(call+'.json')))
        except (ValueError,TypeError,KeyError):continue
    return result


class _DailyRetention(core.ArchiveSourceRetentionV2):
    pinned_plan=None

    def _business(self,connection,location,authorization):
        if str(self.root/location['relative_file']) in _cache_files(connection,self.root):
            core.fail('ARCHIVE_RETENTION_ACTIVE_SOURCE_CACHE')
        return super()._business(connection,location,authorization)

    def lookup(self,file,expected_sha256,*,deleted_only=False):
        # A later backup may register the same still-present bytes under a new
        # archive. Resume the exact previously verified archive, not a newly
        # generated plan, during an interrupted source-cleanup transaction.
        matching=[item for item in (self.pinned_plan or {}).get('candidates',[]) if item['file']==str(file) and item['sha256']==expected_sha256]
        if not matching:return super().lookup(file,expected_sha256,deleted_only=deleted_only)
        wanted=matching[0]['reference']['location'];core._path(self.root,file)
        with self._db() as db:
            row=db.execute('''SELECT l.record,l.state,l.intent,a.record archive FROM locations l JOIN archives a
                ON a.archive_sha256=l.archive_sha256 WHERE l.relative_file=? AND l.file_sha256=? AND l.archive_sha256=?''',
                (wanted['relative_file'],expected_sha256,wanted['archive_sha256'])).fetchone()
        if not row or deleted_only and row['state'] not in ('deleting','deleted'):return None
        location,archive=json.loads(row['record']),json.loads(row['archive']);self.validate_location(location,archive)
        if row['state'] in ('deleting','deleted') and not row['intent']:core.fail('ARCHIVE_RETENTION_LOCATION_CHANGED')
        return {'location':location,'archive':archive,'state':row['state']}


class ArchiveSourceDailyAutorunV2:
    def __init__(self,settings,store):
        self.settings,self.store=settings,store;self.root=Path(settings.root)
        self.retention=_DailyRetention(settings,store);self.folder=self.retention.folder/'autorun'

    def _authorization(self):
        file=self.root/core.RELEASE_RELATIVE;core._root_protected(file)
        value=json.loads(file.read_bytes());ident=value['authorization_id']
        authorization=self.retention.authorization(ident)
        self.retention._release(ident)
        return authorization

    def _save(self,path,value):core._atomic(path,value)

    def _load(self,path):
        core._path(self.root,path)
        if path.stat().st_size>20_000_000:core.fail('ARCHIVE_RETENTION_AUTORUN_RECORD_LIMIT')
        return json.loads(path.read_bytes())

    def _plan(self,authorization):
        with self.retention._db() as db:
            rows=db.execute("SELECT DISTINCT relative_file FROM locations WHERE state='registered' ORDER BY relative_file").fetchall()
        if len(rows)>5000:core.fail('ARCHIVE_RETENTION_EXPLICIT_FILES_REQUIRED')
        candidates=[];rejected=[];candidate_bytes=0
        for row in rows:
            file=str(self.root/core._relative(row[0]))
            try:
                actual=self.retention.plan([file],authorization_id=authorization['authorization_id'])
                rejected.extend(actual['rejected'])
                for item in actual['candidates']:
                    encoded_bytes=len(encode(item).encode())
                    if len(candidates)>=MAX_FILES_PER_RUN or candidate_bytes+encoded_bytes>MAX_PLAN_BYTES:
                        rejected.append({'file':file,'error_code':'ARCHIVE_RETENTION_BATCH_LIMIT_DEFERRED'})
                    else:candidates.append(item);candidate_bytes+=encoded_bytes
            except OSError:
                rejected.append({'file':file,'error_code':'ARCHIVE_RETENTION_FILE_UNAVAILABLE'})
        plan={'version':core.VERSION,'plan_id':str(uuid.uuid4()),'created_at':datetime.now(timezone.utc).isoformat(),
            'authorization_id':authorization['authorization_id'],'authorization_sha256':core.digest(authorization),
            'candidates':candidates,'rejected':rejected,'state':'prepared','source_files_deleted':0,'remote_requests':0,
            'protected_active_database_configuration_credentials_profiles_unchanged':True}
        # Composition adds no passing claim: the core revalidates all actual
        # snapshots and business bindings again before its first deletion.
        return {**plan,'plan_sha256':core.digest(plan)}

    def _apply(self,journal,path):
        plan_file=core._path(self.root,journal['plan_file']);raw=plan_file.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=journal['plan_file_sha256']:core.fail('ARCHIVE_RETENTION_PLAN_CHANGED')
        plan=json.loads(raw);self.retention.pinned_plan=plan
        journal['state']='applying';self._save(path,journal)
        try:
            result=self.retention.apply(plan)
        except (HubError,OSError) as error:
            missing=[item['file'] for item in plan['candidates'] if not Path(item['file']).exists()]
            journal['state']='applying' if missing else 'retained_after_revalidation_change'
            journal['cleanup_error_code']=error.code if isinstance(error,HubError) else 'ARCHIVE_RETENTION_LOCAL_IO_ERROR'
            journal['missing_candidates_reconciliation_required']=missing
            self._save(path,journal)
            return journal
        finally:self.retention.pinned_plan=None
        journal.update(state='completed',result=result,completed_at=datetime.now(timezone.utc).isoformat())
        self._save(path,journal);return journal

    def run_once(self,report_file,expected_report_sha256,*,apply=False):
        if type(apply) is not bool:core.fail('ARCHIVE_RETENTION_APPLY_PARAMETER_REQUIRED')
        registered=self.retention.verify_registration(report_file,expected_report_sha256)
        authorization=self._authorization();self.folder.mkdir(mode=0o700,exist_ok=True)
        core._path(self.root,self.folder)
        with resource_guard(self.folder/'lock'):
            # Finish an already-started exact plan before starting another day;
            # no retries of an unknown network operation occur here.
            for previous in sorted(self.folder.glob('*.journal.json')):
                value=self._load(previous)
                if value['state']=='applying':
                    if not apply:return self.summary(value,previous)
                    resumed=self._apply(value,previous)
                    if resumed['state']=='applying':return self.summary(resumed,previous)
            operation=registered['operation_id'];path=self.folder/(operation+'.journal.json')
            if path.exists():
                journal=self._load(path)
                if journal['registration']!=registered:core.fail('ARCHIVE_RETENTION_LOCATION_CHANGED')
                if journal['state']=='completed' or not apply:return self.summary(journal,path)
            else:
                plan=self._plan(authorization);plan_file=self.folder/(plan['plan_id']+'.plan.json')
                self._save(plan_file,plan)
                journal={'version':VERSION,'operation_id':operation,'registration':registered,'state':'planned',
                    'authorization_id':authorization['authorization_id'],'plan_file':str(plan_file),
                    'plan_file_sha256':hashlib.sha256(plan_file.read_bytes()).hexdigest(),
                    'candidate_files':len(plan['candidates']),'candidate_bytes':sum(item['bytes'] for item in plan['candidates']),
                    'rejected':plan['rejected'],'external_requests':0,'owner_reconfirmation_required':False}
                self._save(path,journal)
            if not apply:return self.summary(journal,path)
            if journal['candidate_files']==0:
                journal.update(state='completed',result={'source_files_deleted':0,'deleted_files':[],'remote_requests':0})
                self._save(path,journal)
            else:journal=self._apply(journal,path)
            return self.summary(journal,path)

    def summary(self,journal,path):
        return {'version':VERSION,'operation_id':journal['operation_id'],'state':journal['state'],
            'candidate_files':journal['candidate_files'],'candidate_bytes':journal['candidate_bytes'],
            'source_files_deleted':journal.get('result',{}).get('source_files_deleted'),
            'rejected':journal['rejected'][:100],'rejected_count':len(journal['rejected']),
            'full_rejections_in_journal':True,'journal_file':str(path),'external_requests':0,
            'cleanup_error_code':journal.get('cleanup_error_code'),'owner_reconfirmation_required':False}
