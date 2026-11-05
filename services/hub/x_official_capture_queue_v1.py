"""Durable official reads and an independent, zero-X-HTTP media stage.

The original HTTP receipt owner remains the discovery job. A fixed ingest
child supplies its own real lease; it never impersonates that finished parent.
"""
from copy import deepcopy
import hashlib,json,math,os,re,time,uuid
from pathlib import Path
from urllib.parse import urlsplit

from .db import encode
from .errors import HubError
from .source_receipts import SourceReceipts,digest,read_body
from .web_source import WebSources

VERSION='x-official-capture-queue-v1'
JOB_TYPE='x_official_media_ingest_v1'
TABLE='x_official_capture_queue_v1'


def fail(code='X_CAPTURE_DESCRIPTOR_INVALID'):raise HubError(code,review=True)


def _safe(root,file):
    file=Path(file)
    if not file.is_absolute() or file.resolve()!=file or not file.is_relative_to(root/'media') or any(p.is_symlink() for p in (file,*file.parents)):fail()
    return file


def _read(root,reference):
    if set(reference)!={'file','bytes','sha256'} or type(reference['bytes']) is not int or not 0<reference['bytes']<=5_500_000:fail()
    file=_safe(root,reference['file']);fd=os.open(file,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(fd,'rb') as stream:
        before=os.fstat(stream.fileno())
        if before.st_size!=reference['bytes']:fail()
        raw=stream.read(reference['bytes']+1);after=os.fstat(stream.fileno())
    if (len(raw)!=reference['bytes'] or hashlib.sha256(raw).hexdigest()!=reference['sha256'] or
            any(getattr(before,k)!=getattr(after,k) or getattr(before,k)!=getattr(file.stat(),k) for k in ('st_dev','st_ino','st_size','st_mtime_ns'))):fail()
    return read_body(raw)


def _write(root,file,value):
    file=_safe(root,file);file.parent.mkdir(mode=0o700,parents=True,exist_ok=True);raw=encode(value).encode()
    if not 0<len(raw)<=5_500_000:fail()
    reference={'file':str(file),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    if file.exists():
        if _read(root,reference)!=value:fail()
        return reference
    temporary=file.with_name('.'+file.name+'.'+str(uuid.uuid4())+'.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        try:os.link(temporary,file)
        except FileExistsError:
            if _read(root,reference)!=value:fail()
        WebSources._sync_dir(file.parent)
    finally:
        if temporary.exists():temporary.unlink();WebSources._sync_dir(file.parent)
    if _read(root,reference)!=value:fail()
    return reference


def _load(root,file):
    file=_safe(root,file)
    fd=os.open(file,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(fd,'rb') as stream:
        if not 0<os.fstat(stream.fileno()).st_size<=5_500_000:fail()
        raw=stream.read(5_500_001)
    return _read(root,{'file':str(file),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})


class CapturedReadClient:
    """Same-discovery replay of real raw receipts, never a synthetic HTTP call."""
    def __init__(self,settings,store,client,job,receipts):
        self.settings,self.store,self.client,self.job,self.receipts=settings,store,client,job,receipts
        self.last_captured_at=None;self.fresh_calls=0

    def request_recorded(self,provider,method,url,*,job_id,headers,params=None,**kwargs):
        self.receipts._parent(self.job)
        if provider!='x' or method!='GET' or job_id!=self.job['job_id']:fail()
        parsed=urlsplit(url)
        from adapters.http import x_resource_bounds
        x_resource_bounds(method,url,params)
        # A retry still filters posts against the current two-hour window.
        # Its stored wire start_time remains the original capture query; it
        # does not claim that old bytes were fetched again at the new time.
        key_params={k:v for k,v in (params or {}).items() if k!='start_time'}
        key=digest({'url':url,'params':key_params})
        folder=self.settings.root/'media'/job_id/'official-capture-v1/reads'
        file=folder/(key+'.json');intent=folder/(key+'-intent.json')
        descriptor=None
        if file.exists():
            descriptor=_load(self.settings.root,file)
        else:
            if intent.exists():
                pending=_load(self.settings.root,intent)
                if (set(pending)!={'version','job_id','key','url','params','created_at','prior_call_ids','human_review'} or pending['version']!=VERSION or pending['human_review'] is not False or
                        type(pending['created_at']) not in (int,float) or not math.isfinite(pending['created_at']) or not 0<pending['created_at']<=time.time() or
                        not isinstance(pending['prior_call_ids'],list) or len(pending['prior_call_ids'])>10000 or
                        len(set(pending['prior_call_ids']))!=len(pending['prior_call_ids']) or any(str(uuid.UUID(v))!=v for v in pending['prior_call_ids']) or
                        pending.get('job_id')!=job_id or pending.get('key')!=key or pending.get('url')!=url or {k:v for k,v in pending['params'].items() if k!='start_time'}!=key_params):fail()
            else:
                before=[r['call_id'] for r in self.store.query("SELECT call_id FROM api_calls WHERE provider='x' AND job_id=? ORDER BY call_id",(job_id,))]
                if len(before)>10000:fail()
                pending={'version':VERSION,'job_id':job_id,'key':key,'url':url,'params':deepcopy(params or {}),'created_at':time.time(),'prior_call_ids':before,'human_review':False}
                _write(self.settings.root,intent,pending)
            rows=self.store.query('SELECT * FROM source_http_receipts WHERE discovery_job_id=? AND path=? AND params=? ORDER BY captured_at',
                (job_id,parsed.path,encode(pending['params'])))
            if rows:
                if len(rows)!=1:fail('X_CAPTURE_MULTIPLE_READS_REQUIRE_RECONCILIATION')
                row=self.receipts._receipt(rows[0]['call_id']);self.receipts._body(row)
                descriptor=self._descriptor(key,url,pending['params'],row)
            else:
                # An interrupted/error request is not silently repeated. A
                # pre-reservation budget refusal has no api_calls row and may
                # safely resume later without erasing any budget reservation.
                calls=[r for r in self.store.query("SELECT call_id FROM api_calls WHERE provider='x' AND job_id=?",(job_id,)) if r['call_id'] not in pending['prior_call_ids']]
                if calls:fail('X_CAPTURE_READ_RECONCILIATION_REQUIRED')
                value,item=self.client.request_recorded(provider,method,url,job_id=job_id,headers=headers,params=pending['params'],**kwargs)
                self.fresh_calls+=1
                self.receipts.record(self.job,item)  # raw bytes fsynced before descriptor / any next read
                row=self.receipts._receipt(item['call_id']);descriptor=self._descriptor(key,url,pending['params'],row)
            _write(self.settings.root,file,descriptor)
        if (descriptor.get('version')!=VERSION or descriptor.get('job_id')!=job_id or descriptor.get('key')!=key or descriptor.get('url')!=url or
                {k:v for k,v in descriptor['params'].items() if k!='start_time'}!=key_params):fail()
        row=self.receipts._receipt(descriptor['call_id']);value=self.receipts._body(row)
        source=self.receipts._file(row);fd=os.open(source,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        with os.fdopen(fd,'rb') as stream:
            if not 0<os.fstat(stream.fileno()).st_size<=5*1024*1024:fail()
            raw=stream.read(5*1024*1024+1)
        if len(raw)>5*1024*1024 or hashlib.sha256(raw).hexdigest()!=row['raw_sha256']:fail()
        if (row['discovery_job_id']!=job_id or row['path']!=parsed.path or json.loads(row['params'])!=descriptor['params'] or
                descriptor!=self._descriptor(key,url,descriptor['params'],row)):fail()
        self.receipts._parent(self.job)
        self.last_captured_at=max(self.last_captured_at or 0,row['captured_at'])
        return value,{'call_id':row['call_id'],'raw_body':raw,'captured_at':row['captured_at'],'transport_mode':row['transport_mode']}

    def _descriptor(self,key,url,params,row):
        return {'version':VERSION,'job_id':self.job['job_id'],'key':key,'url':url,'params':params,'call_id':row['call_id'],
            'raw_sha256':row['raw_sha256'],'canonical_sha256':row['canonical_sha256'],'captured_at':row['captured_at'],
            'transport_mode':row['transport_mode'],'human_review':False}


class OfficialCaptureQueue:
    def __init__(self,settings,store,*,downloader=None):self.settings,self.store,self.downloader=settings,store,downloader

    def initialize(self):
        with self.store.transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS x_official_capture_queue_v1(
                source_id TEXT NOT NULL,target_uid TEXT NOT NULL,discovery_job_id TEXT NOT NULL REFERENCES jobs(job_id),
                ingest_job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id),policy_version TEXT NOT NULL,
                descriptor TEXT NOT NULL,record_sha256 TEXT NOT NULL,state TEXT NOT NULL,created_at REAL NOT NULL,
                updated_at REAL NOT NULL,PRIMARY KEY(source_id,target_uid))''')

    def known_ids(self):
        self.initialize()
        return [r['source_id'] for r in self.store.query('SELECT source_id FROM '+TABLE+' WHERE target_uid=?',(str(self.settings.config['target_bilibili_site_uid']),))]

    def register(self,discovery,record,policy):
        self.initialize();receipts=SourceReceipts(self.settings,self.store);receipts._parent(discovery)
        rebuilt,rows,owner=receipts._official_record(record['payload'],discovery_id=discovery['job_id'])
        if rebuilt!=record:fail()
        source=record['payload']['post']['id'];target=str(self.settings.config['target_bilibili_site_uid'])
        with self.store.transaction() as db:
            receipts._official_parent_row(db,discovery)
            old=db.execute('SELECT ingest_job_id FROM '+TABLE+' WHERE source_id=? AND target_uid=?',(source,target)).fetchone()
            if old:return {'job_id':old['ingest_job_id'],'created':False,'captured_pending':True}
            existing=receipts._official_gate(discovery,record['payload'],policy,db)
            if existing:return existing
            raw_refs=[{'file':str(receipts._file(row)),'bytes':receipts._file(row).stat().st_size,'sha256':row['raw_sha256']} for _,row in sorted(rows.items())]
            checksum=digest(record);descriptor={'version':VERSION,'discovery_job_id':owner,'source_id':source,'target_uid':target,'policy_version':policy,
                'record_sha256':checksum,'record':record,'source_receipts':raw_refs,'human_review':False,'publication_authorized':False}
            file=self.settings.root/'media'/owner/'official-capture-v1/posts'/(source+'-'+checksum+'.json')
            reference=_write(self.settings.root,file,descriptor);payload={'version':VERSION,'source_id':source,'target_uid':target,
                'original_discovery_job_id':owner,'record_sha256':checksum,'descriptor':reference,'source_receipts':raw_refs}
            ident=str(uuid.uuid4());now=time.time()
            db.execute("INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,policy_version,payload) VALUES(?,'x',?,?,'tweet','pending',?,?,?,?)",
                (ident,source,JOB_TYPE,now,now,policy,encode(payload)))
            db.execute('INSERT INTO '+TABLE+' VALUES(?,?,?,?,?,?,?,?,?,?)',(source,target,owner,ident,policy,encode(reference),checksum,'queued',now,now))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',(now,'system:x-official-capture-v1','x_source_captured_before_media',ident,
                encode({'descriptor':reference,'record_sha256':checksum,'original_discovery_job_id':owner,'human_review':False,'publication_authorized':False})))
            return {'job_id':ident,'created':True,'captured_pending':True,'binding_present':False}

    def context(self,job,*,db=None):
        rows=(db.execute('SELECT * FROM jobs WHERE job_id=?',(job['job_id'],)).fetchall() if db is not None else self.store.query('SELECT * FROM jobs WHERE job_id=?',(job['job_id'],)))
        if not rows:fail('X_CAPTURE_MEDIA_LEASE_REQUIRED')
        current=dict(rows[0]);payload=json.loads(current['payload'])
        if (current['job_type']!=JOB_TYPE or current['source_platform']!='x' or current['queue']!='tweet' or current['state']!='processing' or
                not job.get('lease_owner') or current['lease_owner']!=job['lease_owner'] or (current['lease_until'] or 0)<=time.time() or payload!=job['payload'] or
                payload['version']!=VERSION or payload['source_id']!=current['source_id'] or payload['target_uid']!=str(self.settings.config['target_bilibili_site_uid'])):fail('X_CAPTURE_MEDIA_LEASE_REQUIRED')
        records=(db.execute('SELECT * FROM '+TABLE+' WHERE ingest_job_id=?',(job['job_id'],)).fetchall() if db is not None else self.store.query('SELECT * FROM '+TABLE+' WHERE ingest_job_id=?',(job['job_id'],)))
        if len(records)!=1:fail()
        entry=dict(records[0])
        expected=self.settings.root/'media'/entry['discovery_job_id']/'official-capture-v1/posts'/(entry['source_id']+'-'+entry['record_sha256']+'.json')
        if payload['descriptor']['file']!=str(expected):fail()
        descriptor=_read(self.settings.root,payload['descriptor'])
        if (entry['source_id']!=current['source_id'] or entry['target_uid']!=payload['target_uid'] or entry['discovery_job_id']!=payload['original_discovery_job_id'] or
                entry['policy_version']!=current['policy_version'] or json.loads(entry['descriptor'])!=payload['descriptor'] or entry['record_sha256']!=payload['record_sha256'] or
                descriptor['version']!=VERSION or descriptor['discovery_job_id']!=entry['discovery_job_id'] or descriptor['source_id']!=entry['source_id'] or
                descriptor['target_uid']!=entry['target_uid'] or descriptor['policy_version']!=entry['policy_version'] or descriptor['record_sha256']!=digest(descriptor['record']) or
                descriptor['record_sha256']!=entry['record_sha256'] or descriptor['record']['payload']['post']['id']!=entry['source_id'] or payload['source_receipts']!=descriptor['source_receipts'] or
                descriptor['human_review'] is not False or descriptor['publication_authorized'] is not False):fail()
        parent=self.store.get(entry['discovery_job_id'])
        if not parent or parent['job_type']!='x_discovery' or parent['queue']!='tweet':fail()
        base=current['policy_version'].split(':',1)[0]
        generation=self.settings.config.get('machine_source_generation',1)
        if (base!=self.settings.config['policy_version'] or base not in ('machine-tweet-v3-api','machine-tweet-v4-api-relations') or
                type(generation) is not int or not 1<=generation<=1000 or current['policy_version']!=(base if generation==1 else base+':source-'+str(generation)) or
                self.settings.config.get('machine_tweet_enabled') is not True or self.settings.config.get('x_api_enabled') is not True or self.settings.config.get('x_web_enabled') is not False):fail('MACHINE_SOURCE_GENERATION_NOT_ACTIVE')
        return descriptor

    def process(self,job):
        from .x_api_discovery import PERMANENT_ASSET_ERRORS,SKIPPABLE_SOURCE_ERRORS
        self.initialize()
        descriptor=self.context(job);receipts=CapturedMediaReceipts(self.settings,self.store,self,job)
        try:value=receipts.ingest_official(job,descriptor['record'],job['policy_version'],downloader=self.downloader)
        except HubError as exc:
            if exc.code not in PERMANENT_ASSET_ERRORS|SKIPPABLE_SOURCE_ERRORS:raise
            value={'created':False,'disposition':'expired_or_cleared' if exc.code in SKIPPABLE_SOURCE_ERRORS else 'rejected_asset','error_code':exc.code}
            self.store.event('task.failed',{'job_id':job['job_id'],'queue':'tweet','source_id':job['source_id'],'code':exc.code,'media_ingest_only':True},dedup_key=VERSION+':'+job['job_id']+':'+exc.code)
        with self.store.transaction() as db:
            self.context(job,db=db)
            db.execute("UPDATE "+TABLE+" SET state=?,updated_at=? WHERE ingest_job_id=?",('ingested' if value.get('binding_present') else value.get('disposition','existing'),time.time(),job['job_id']))
        return {'version':VERSION,'source_id':job['source_id'],'original_discovery_job_id':descriptor['discovery_job_id'],
            'ingestion':value,'paid_x_api_calls':0,'human_review':False,'publication_authorized':False}


class CapturedMediaReceipts(SourceReceipts):
    def __init__(self,settings,store,queue,job):
        super().__init__(settings,store);self.queue,self.job=queue,job

    def _parent(self,job):self.queue.context(job)

    def _official_parent_row(self,db,job):self.queue.context(job,db=db)

    def _official_capture_owner(self,job):return self.queue.context(job)['discovery_job_id']
