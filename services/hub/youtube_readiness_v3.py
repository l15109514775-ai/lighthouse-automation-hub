"""Finite, budgeted post-end metadata observations. No media or publication.

The original prepare keeps its payload/attempt history. At most96 uniquely
numbered observation jobs use ordinary leases; the poller itself makes no HTTP.
"""
from copy import deepcopy
import json,os,re,time,uuid
from pathlib import Path

from .db import encode
from .errors import HubError,PublishUnknown
from .locks import resource_guard
from .youtube_download_v3 import (YouTubeNaturalPrepare,YouTubeNaturalDownload,PREPARE_JOB_TYPE,
    PREPARE_STATE,POLICY_ID,JOB_TYPE as DOWNLOAD_JOB_TYPE,VERSION,digest,stamp,validate_natural_plan)
from .youtube_natural_plan_v3 import natural_source_plan

JOB_TYPE='youtube_highest_readiness_observation_v3'
WAIT='youtube_readiness_v3'
WAIT_VERSION='youtube-bounded-readiness-wait-v3'
REASONS=frozenset(('YOUTUBE_NATURAL_POST_LIVE_NOT_READY','YOUTUBE_NATURAL_EXACT_SIZE_NOT_READY'))
INTERVAL=900


def fail(code):raise HubError(code)


def verify_observation(settings,store,job,binding,reference,reason):
    from adapters.youtube_readiness_evidence_v3 import read
    body=read(settings,job,binding,reference)
    calls=store.query("SELECT COUNT(*) n FROM api_calls WHERE provider='youtube_download_http' AND job_id=?",(job['job_id'],))[0]['n']
    if calls<body['http']['requests']:fail('YOUTUBE_READINESS_HTTP_EVIDENCE_MISSING')
    try:
        natural_source_plan(binding['official_raw'],body['extractor_info'],activated_at=binding['activated_at'],
            observed_at=body['recorded_at'],now=stamp(body['recorded_at']).timestamp())
    except HubError as exc:
        if exc.code==reason and reason in REASONS:return body
        raise HubError('YOUTUBE_READINESS_REASON_NOT_PROVEN') from None
    fail('YOUTUBE_READINESS_REASON_NOT_PROVEN')


def read_failed_observation(settings,job,binding,reason):
    from adapters.ytdlp_natural_v3 import planning_folder
    from .db import Store
    file=planning_folder(settings,job['job_id'])/'download-result.json'
    try:
        if file.is_symlink() or not file.is_file() or file.stat().st_size>262144:raise ValueError()
        value=json.loads(file.read_bytes())
        if value.get('error_code')!=reason or reason not in REASONS:raise ValueError()
        reference=value['metadata_observation'];verify_observation(settings,Store(settings.db_path),job,binding,reference,reason)
        return reference
    except (OSError,ValueError,TypeError,KeyError):fail('YOUTUBE_READINESS_EVIDENCE_REQUIRED')


class YouTubeReadinessCoordinator:
    def __init__(self,settings,store,*,clock=time.time):
        self.settings,self.store,self.clock=settings,store,clock
        hours=settings.config.get('youtube_readiness_max_hours',24)
        if type(hours) is not int or not 6<=hours<=24:raise ValueError('YOUTUBE_READINESS_HOURS_MUST_BE_6_TO_24')
        self.duration=hours*3600;self.maximum=hours*4

    def initialize(self):
        with self.store.transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS youtube_readiness_waits_v3(
                parent_job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),wait_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('waiting_readiness','ready','failed')),
                created_at REAL NOT NULL,deadline REAL NOT NULL,next_check REAL NOT NULL,
                observations INTEGER NOT NULL,max_observations INTEGER NOT NULL,active_child_id TEXT,
                binding_sha256 TEXT NOT NULL,payload_sha256 TEXT NOT NULL,policy_version TEXT NOT NULL,record TEXT NOT NULL)''')

    def callbacks(self):return {PREPARE_JOB_TYPE:self.defer}

    def _binding(self,parent,expected=None):
        binding=YouTubeNaturalPrepare(self.settings,self.store).resource_binding(parent)
        if expected and digest(binding)!=expected:fail('YOUTUBE_READINESS_BINDING_CHANGED')
        return binding

    @staticmethod
    def _publication(db,parent):
        if db.execute("SELECT 1 FROM publications WHERE source_platform='youtube' AND source_id=? LIMIT 1",(parent['source_id'],)).fetchone():raise PublishUnknown()

    def defer(self,job,error):
        current,binding=YouTubeNaturalPrepare(self.settings,self.store)._binding(job)
        verify_observation(self.settings,self.store,current,binding,error.observation,error.reason)
        self.initialize();now=self.clock();deadline=now+self.duration;wait_id=str(uuid.uuid4())
        with self.store.transaction() as db:
            row=db.execute('SELECT * FROM jobs WHERE job_id=?',(job['job_id'],)).fetchone()
            if (row['state']!='processing' or row['lease_owner']!=job['lease_owner'] or (row['lease_until'] or 0)<=time.time() or
                    json.loads(row['payload'])!=current['payload']):fail('YOUTUBE_READINESS_LEASE_CHANGED')
            self._publication(db,current)
            if db.execute('SELECT 1 FROM youtube_readiness_waits_v3 WHERE parent_job_id=?',(job['job_id'],)).fetchone():fail('YOUTUBE_READINESS_ALREADY_REGISTERED')
            record={'version':WAIT_VERSION,'state':'waiting_readiness','wait_id':wait_id,'created_at':now,'deadline':deadline,
                'next_check':now+INTERVAL,'observations':1,'max_observations':self.maximum,'reason':error.reason,
                'observations_evidence':[{'sequence':1,'job_id':job['job_id'],'reason':error.reason,'observation':error.observation}],
                'attempts_reset':False,'parent_attempts':row['attempts'],'human_review':False,'publication_authorized':False}
            result=json.loads(row['result']);result[WAIT]=record
            db.execute('INSERT INTO youtube_readiness_waits_v3 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (job['job_id'],wait_id,'waiting_readiness',now,deadline,now+INTERVAL,1,self.maximum,None,digest(binding),digest(current['payload']),row['policy_version'],encode(record)))
            db.execute("UPDATE jobs SET state='retry_scheduled',stage='waiting_readiness',error_code='WAITING_YOUTUBE_READINESS',result=?,next_run=?,updated_at=?,lease_owner=NULL,lease_until=NULL WHERE job_id=?",
                (encode(result),deadline+60,now,job['job_id']))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                (now,'system:youtube-readiness-v3','youtube_readiness_begin',job['job_id'],encode({'wait_id':wait_id,'reason':error.reason,'maximum':self.maximum})))

    def _stop(self,db,parent,waiting,code,now):
        result=json.loads(parent['result']);record=json.loads(waiting['record']);record.update(state='failed',error_code=code,finished_at=now)
        result[WAIT]=record;state='publish_unknown' if code=='PUBLISH_UNKNOWN' else 'failed'
        db.execute("UPDATE youtube_readiness_waits_v3 SET state='failed',record=? WHERE parent_job_id=?",(encode(record),parent['job_id']))
        db.execute('UPDATE jobs SET state=?,error_code=?,result=?,updated_at=? WHERE job_id=?',
            (state,code,encode(result),now,parent['job_id']))
        self.store._event(db,'task.failed',{'job_id':parent['job_id'],'code':code,'queue':'video'})

    def tick(self):
        self.initialize();now=self.clock();summary={'version':WAIT_VERSION,'enqueued':[],'failed':[],'external_calls':0,'attempts_reset':False}
        with resource_guard(self.settings.root/'data/locks/youtube-readiness-v3'):
            rows=self.store.query("SELECT * FROM youtube_readiness_waits_v3 WHERE state='waiting_readiness' AND next_check<=? ORDER BY created_at LIMIT 16",(now,))
            for waiting in rows:
                parent=self.store.get(waiting['parent_job_id']);code=None
                try:
                    if not parent or parent['state']!='retry_scheduled' or parent['stage']!='waiting_readiness' or parent['error_code']!='WAITING_YOUTUBE_READINESS':fail('YOUTUBE_READINESS_PARENT_CHANGED')
                    self._binding(parent,waiting['binding_sha256'])
                    if digest(parent['payload'])!=waiting['payload_sha256'] or parent['policy_version']!=waiting['policy_version']:fail('YOUTUBE_READINESS_BINDING_CHANGED')
                except HubError as exc:code=exc.code
                if now>=waiting['deadline']:code='YOUTUBE_READINESS_TIMEOUT'
                active=self.store.get(waiting['active_child_id']) if waiting['active_child_id'] else None
                if active and active['state'] in ('failed','auth_expired','awaiting_review','publish_unknown'):
                    code='PUBLISH_UNKNOWN' if active['state']=='publish_unknown' else active.get('error_code')
                    if not isinstance(code,str) or not re.fullmatch('[A-Z][A-Z0-9_]{0,79}',code):code='YOUTUBE_READINESS_CHILD_FAILED'
                if not active and waiting['observations']>=waiting['max_observations']:code='YOUTUBE_READINESS_OBSERVATION_LIMIT'
                disk_ready=True
                if code is None and not active:
                    from .maintenance import Maintenance
                    try:Maintenance(self.settings,self.store).check_disk(32_000_000)
                    except HubError as exc:
                        if exc.code!='DISK_LOW':raise
                        disk_ready=False
                with self.store.transaction() as db:
                    row=db.execute('SELECT * FROM youtube_readiness_waits_v3 WHERE parent_job_id=?',(waiting['parent_job_id'],)).fetchone()
                    actual=db.execute('SELECT * FROM jobs WHERE job_id=?',(waiting['parent_job_id'],)).fetchone()
                    if not row or dict(row)!=waiting or not actual:continue
                    if actual['state']!='retry_scheduled' or actual['stage']!='waiting_readiness' or actual['error_code']!='WAITING_YOUTUBE_READINESS':
                        db.execute("UPDATE youtube_readiness_waits_v3 SET state='failed' WHERE parent_job_id=?",(actual['job_id'],));continue
                    try:self._publication(db,parent or actual)
                    except PublishUnknown:code='PUBLISH_UNKNOWN'
                    if code:
                        self._stop(db,actual,row,code,now);summary['failed'].append({'job_id':actual['job_id'],'code':code});continue
                    if active or not disk_ready:continue
                    if db.execute("SELECT paused FROM pipelines WHERE name='video'").fetchone()['paused']:continue
                    if db.execute("SELECT 1 FROM jobs WHERE queue='video' AND (state='processing' OR state IN ('pending','retry_scheduled') AND next_run<=?) LIMIT 1",(now,)).fetchone():continue
                    ident=str(uuid.uuid4());sequence=row['observations']+1
                    payload={**deepcopy(parent['payload']),'_natural_readiness_parent_job_id':parent['job_id'],
                        '_natural_readiness_wait_id':row['wait_id'],'_natural_readiness_sequence':sequence}
                    db.execute("INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,policy_version,payload) VALUES(?,'youtube',?,?,'video','pending',?,?,?,?)",
                        (ident,parent['source_id']+':readiness:'+str(sequence),JOB_TYPE,now,now,POLICY_ID,encode(payload)))
                    # jobs has a source/kind/policy uniqueness key. Each finite
                    # sequence is a distinct evidence source ID, bound back to
                    # the real video through the immutable payload below.
                    db.execute('UPDATE youtube_readiness_waits_v3 SET active_child_id=?,observations=? WHERE parent_job_id=?',(ident,sequence,parent['job_id']))
                    summary['enqueued'].append(ident)
        return summary

    def observed(self,job,checkpoint):
        current,binding=ReadinessObservation(self.settings,self.store)._binding(job);parent_id=current['payload']['_natural_readiness_parent_job_id'];now=self.clock()
        with self.store.transaction() as db:
            waiting=db.execute('SELECT * FROM youtube_readiness_waits_v3 WHERE parent_job_id=?',(parent_id,)).fetchone()
            parent=db.execute('SELECT * FROM jobs WHERE job_id=?',(parent_id,)).fetchone()
            if not waiting or waiting['state']!='waiting_readiness' or waiting['active_child_id']!=job['job_id'] or now>=waiting['deadline']:fail('YOUTUBE_READINESS_PARENT_CHANGED')
            if (not parent or parent['state']!='retry_scheduled' or parent['stage']!='waiting_readiness' or
                    parent['error_code']!='WAITING_YOUTUBE_READINESS' or parent['lease_owner'] is not None or parent['lease_until'] is not None or
                    digest(json.loads(parent['payload']))!=waiting['payload_sha256'] or parent['policy_version']!=waiting['policy_version']):fail('YOUTUBE_READINESS_PARENT_CHANGED')
            self._publication(db,parent)
            result=json.loads(parent['result']);record=json.loads(waiting['record'])
            item={'sequence':current['payload']['_natural_readiness_sequence'],'job_id':job['job_id'],'reason':checkpoint.get('reason'),
                'observation':checkpoint['observation']}
            if record['observations_evidence'][-1].get('job_id')==job['job_id']:return
            record['observations_evidence'].append(item);record['observations']=waiting['observations']
            if checkpoint['state']=='not_ready':
                record.update(next_check=min(waiting['deadline'],now+INTERVAL),reason=checkpoint['reason'])
                result[WAIT]=record
                db.execute('UPDATE youtube_readiness_waits_v3 SET active_child_id=NULL,next_check=?,record=? WHERE parent_job_id=?',
                    (record['next_check'],encode(record),parent_id))
                db.execute('UPDATE jobs SET result=?,updated_at=? WHERE job_id=?',(encode(result),now,parent_id));return
            plan=validate_natural_plan(checkpoint['receipt']['source_plan'],official_raw=binding['official_raw'])
            parent_payload=json.loads(parent['payload']);parent_binding=YouTubeNaturalPrepare(self.settings,self.store).resource_binding(self.store.get(parent_id))
            if digest(parent_binding)!=waiting['binding_sha256']:fail('YOUTUBE_READINESS_BINDING_CHANGED')
            service=YouTubeNaturalDownload(self.settings,self.store);service._other_source(db,parent['source_id'],except_prepare=parent_id)
            payload={**parent_payload,'_natural_prepare_job_id':parent_id,'_natural_source_plan':plan};ident=str(uuid.uuid4())
            # A ready plan is persisted even if disk is currently insufficient;
            # the normal v3 download handler then uses bounded disk waiting.
            db.execute("INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,policy_version,payload) VALUES(?,'youtube',?,?,'video','pending',?,?,?,?)",
                (ident,parent['source_id'],DOWNLOAD_JOB_TYPE,now,now,POLICY_ID,encode(payload)))
            record.update(state='ready',ready_at=now,metadata_observation_job_id=job['job_id'])
            result[WAIT]=record;result[PREPARE_STATE]={'version':VERSION,'state':'planned','binding':parent_binding,
                'source_plan':plan,'receipt':checkpoint['receipt'],'download_job_id':ident,'metadata_observation_job_id':job['job_id'],
                'human_review':False,'publication_authorized':False}
            db.execute("UPDATE youtube_readiness_waits_v3 SET state='ready',record=? WHERE parent_job_id=?",(encode(record),parent_id))
            db.execute("UPDATE jobs SET state='completed',stage='youtube_natural_ready_plan',error_code=NULL,result=?,updated_at=? WHERE job_id=?",
                (encode(result),now,parent_id))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                (now,'system:youtube-readiness-v3','youtube_readiness_ready',parent_id,encode({'observation_job_id':job['job_id'],'download_job_id':ident,'attempts_reset':False})))


class ReadinessObservation(YouTubeNaturalPrepare):
    JOB_TYPE=JOB_TYPE

    def resource_binding(self,current):
        p=current['payload'];sequence=p.get('_natural_readiness_sequence')
        if type(sequence) is not int or not 2<=sequence<=96 or current['source_id']!=p.get('video_id','')+':readiness:'+str(sequence):fail('YOUTUBE_READINESS_BINDING_CHANGED')
        binding=super().resource_binding({**current,'source_id':p['video_id']});parent=self.store.get(p.get('_natural_readiness_parent_job_id'))
        rows=self.store.query('SELECT * FROM youtube_readiness_waits_v3 WHERE parent_job_id=?',(p.get('_natural_readiness_parent_job_id'),))
        if (not parent or len(rows)!=1 or rows[0]['state']!='waiting_readiness' or rows[0]['active_child_id']!=current['job_id'] or
                rows[0]['wait_id']!=p.get('_natural_readiness_wait_id') or rows[0]['observations']!=p.get('_natural_readiness_sequence') or
                not 2<=p['_natural_readiness_sequence']<=96 or parent['state']!='retry_scheduled' or parent['stage']!='waiting_readiness' or
                parent['error_code']!='WAITING_YOUTUBE_READINESS' or time.time()>=rows[0]['deadline']):fail('YOUTUBE_READINESS_PARENT_CHANGED')
        original={k:v for k,v in p.items() if k not in ('_natural_readiness_parent_job_id','_natural_readiness_wait_id','_natural_readiness_sequence')}
        if original!=parent['payload'] or digest(YouTubeNaturalPrepare(self.settings,self.store).resource_binding(parent))!=rows[0]['binding_sha256']:fail('YOUTUBE_READINESS_BINDING_CHANGED')
        return {**binding,'readiness_parent_job_id':parent['job_id'],'readiness_wait_id':rows[0]['wait_id'],'readiness_sequence':p['_natural_readiness_sequence']}

    def process(self,job):
        current=self.store.get(job['job_id']);checkpoint=current['result'].get(WAIT) if current else None
        # If the process exited after the atomic parent transition, the exact
        # completed observation remains idempotent without another HTTP call.
        if checkpoint and current['state']=='processing' and current['lease_owner']==job.get('lease_owner') and (current['lease_until'] or 0)>time.time():
            rows=self.store.query('SELECT record FROM youtube_readiness_waits_v3 WHERE parent_job_id=?',(current['payload'].get('_natural_readiness_parent_job_id'),))
            if rows:
                record=json.loads(rows[0]['record'])
                if record['observations_evidence'][-1].get('job_id')==job['job_id']:
                    if checkpoint.get('binding_sha256')!=digest(checkpoint.get('binding')):fail('YOUTUBE_READINESS_BINDING_CHANGED')
                    return current['result']
        current,binding=self._binding(job)
        if not checkpoint:
            factory=self.downloader_factory
            if factory is None:
                from adapters.ytdlp_natural_readiness_v3 import ReadinessPlanner
                factory=ReadinessPlanner
            try:
                receipt=self.acquire(factory,job,binding,lambda:self.check_binding(job,binding))
                if (receipt.get('version')!=VERSION or receipt.get('binding_sha256')!=digest(binding) or
                        any(receipt.get(k) is not False for k in ('media_downloaded','human_review','publication_authorized'))):fail('YOUTUBE_NATURAL_PLAN_RECEIPT_INVALID')
                validate_natural_plan(receipt['source_plan'],official_raw=binding['official_raw'])
                from adapters.youtube_readiness_evidence_v3 import read
                body=read(self.settings,current,binding,receipt['metadata_observation'])
                observed=receipt['source_plan']['observed_at']
                actual_plan=natural_source_plan(binding['official_raw'],body['extractor_info'],activated_at=binding['activated_at'],observed_at=observed,now=stamp(observed).timestamp())
                if actual_plan!=receipt['source_plan']:fail('YOUTUBE_READINESS_READY_PLAN_NOT_PROVEN')
                calls=self.store.query("SELECT COUNT(*) n FROM api_calls WHERE provider='youtube_download_http' AND job_id=?",(job['job_id'],))[0]['n']
                if calls<body['http']['requests']:fail('YOUTUBE_READINESS_HTTP_EVIDENCE_MISSING')
                checkpoint={'state':'ready','receipt':receipt,'observation':receipt['metadata_observation']}
            except HubError as exc:
                if exc.code not in REASONS:
                    if exc.retryable:raise HubError(exc.code,retryable=True,review=exc.review,retry_after=max(INTERVAL,exc.retry_after)) from None
                    raise
                observation=read_failed_observation(self.settings,current,binding,exc.code)
                checkpoint={'state':'not_ready','reason':exc.code,'observation':observation}
            checkpoint.update(binding=binding,binding_sha256=digest(binding))
            self.store.checkpoint(job,'youtube_readiness_observed',{**current['result'],WAIT:checkpoint})
        YouTubeReadinessCoordinator(self.settings,self.store).observed(job,checkpoint)
        return self.store.get(job['job_id'])['result']
