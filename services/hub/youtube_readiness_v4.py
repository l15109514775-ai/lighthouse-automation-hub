"""Independent finite V4 metadata observations; never spend the parent's retries."""
from copy import deepcopy
from datetime import datetime
import hashlib,json,os,re,time,uuid
from pathlib import Path

from .db import encode
from .errors import HubError,YouTubeReadinessDeferred,PublishUnknown
from .locks import resource_guard
from .replay_multipart_v4 import digest,plan_whole_replay,validate_whole_replay_plan
from .replay_fifo_v4 import PLAN_JOB,VERSION as FIFO_VERSION
from .youtube_download_v4 import YouTubeWholeReplayPrepare,VERSION

JOB_TYPE='youtube_replay_readiness_v4'
WAIT='youtube_readiness_v4'
WAIT_VERSION='youtube-whole-bounded-readiness-v4'
TABLE='youtube_readiness_waits_v4'
INTERVAL=900
REASONS=frozenset(('YOUTUBE_NATURAL_POST_LIVE_NOT_READY','YOUTUBE_NATURAL_EXACT_SIZE_NOT_READY'))


def fail(code):raise HubError(code)
def stamp(value):return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
class WholeReadinessDeferredV4(YouTubeReadinessDeferred):pass


def read_observation(settings,store,job,binding,reference,*,reason=None,receipt=None,require_fresh=True):
    from adapters.youtube_readiness_evidence_v3 import validate_runtime_snapshot
    try:
        if not isinstance(reference,dict) or set(reference)!={'file','sha256','bytes','version'} or reference['version']!='youtube-extractor-observation-v4':raise ValueError()
        file=Path(reference['file']);area=settings.root/'media'/job['job_id']/'youtube-whole-v4-plan'
        if (file.parent!=area or not re.fullmatch(r'extractor-observation-[a-f0-9-]{36}\.json',file.name) or
                file.resolve()!=file or any(p.is_symlink() for p in (file,*file.parents)) or not file.is_file() or
                type(reference['bytes']) is not int or not 0<reference['bytes']<=2_097_152 or os.name=='posix' and file.stat().st_mode&0o077):raise ValueError()
        before=file.stat()
        if before.st_size!=reference['bytes']:raise ValueError()
        fd=os.open(file,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        with os.fdopen(fd,'rb') as stream:
            opened=os.fstat(stream.fileno())
            if any(getattr(before,k)!=getattr(opened,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns')):raise ValueError()
            raw=stream.read(reference['bytes']+1);closed=os.fstat(stream.fileno())
        after=file.stat()
        if (len(raw)!=reference['bytes'] or hashlib.sha256(raw).hexdigest()!=reference['sha256'] or
                any(getattr(opened,k)!=getattr(closed,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')) or
                any(getattr(before,k)!=getattr(after,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns'))):raise ValueError()
        body=json.loads(raw);validate_runtime_snapshot(body['extractor_info'],body.get('normalization'))
        if (body['version']!=reference['version'] or body['job_id']!=job['job_id'] or body['binding_sha256']!=digest(binding) or
                any(body.get(k) is not False for k in ('media_downloaded','human_review','publication_authorized')) or
                type(body['http']['requests']) is not int or not 1<=body['http']['requests']<=64 or
                stamp(body['recorded_at'])>time.time()+30 or require_fresh and not 0<=time.time()-stamp(body['recorded_at'])<=300):raise ValueError()
        used=store.query("SELECT COUNT(*) AS n FROM api_calls WHERE provider='youtube_download_http' AND job_id=?",(job['job_id'],))[0]['n']
        if used<body['http']['requests']:fail('YOUTUBE_WHOLE_READINESS_HTTP_EVIDENCE_MISSING')
        observed=receipt['source_plan']['source_plan_basis']['observed_at'] if receipt is not None else body['recorded_at']
        if not 0<=stamp(observed)-stamp(body['recorded_at'])<=30:raise ValueError()
        try:actual=plan_whole_replay(binding['official_raw'],body['extractor_info'],observed_at=observed,
            policy=binding['planning_policy'],now=stamp(observed))
        except HubError as exc:
            if reason in REASONS and exc.code==reason:return body
            fail('YOUTUBE_WHOLE_READINESS_REASON_NOT_PROVEN')
        if reason is not None or receipt is None or actual!=receipt['source_plan']:raise ValueError()
        return body
    except HubError:raise
    except (OSError,ValueError,TypeError,KeyError,AttributeError,OverflowError):fail('YOUTUBE_WHOLE_READINESS_EVIDENCE_INVALID')


def read_failed_observation(settings,store,job,binding,reason):
    from adapters.ytdlp_whole_v4 import planning_folder
    file=planning_folder(settings,job['job_id'])/'download-result.json'
    try:
        if file.is_symlink() or not file.is_file() or file.stat().st_size>262144:raise ValueError()
        value=json.loads(file.read_bytes())
        if value.get('error_code')!=reason or reason not in REASONS:raise ValueError()
        ref=value['metadata_observation'];read_observation(settings,store,job,binding,ref,reason=reason);return ref
    except HubError:raise
    except (OSError,ValueError,KeyError,TypeError):fail('YOUTUBE_WHOLE_READINESS_EVIDENCE_REQUIRED')


class WholeReadinessCoordinatorV4:
    def __init__(self,settings,store,*,clock=time.time):
        self.settings,self.store,self.clock=settings,store,clock
        hours=settings.config.get('youtube_whole_readiness_max_hours',24)
        if type(hours) is not int or not 6<=hours<=24:raise ValueError('YOUTUBE_WHOLE_READINESS_HOURS_INVALID')
        self.duration=hours*3600;self.maximum=hours*4
    def initialize(self):
        with self.store.transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS youtube_readiness_waits_v4(parent_job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
                wait_id TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('waiting_readiness','ready','hold')),
                created_at REAL NOT NULL,deadline REAL NOT NULL,next_check REAL NOT NULL,observations INTEGER NOT NULL,
                max_observations INTEGER NOT NULL,active_child_id TEXT,binding_sha256 TEXT NOT NULL,payload_sha256 TEXT NOT NULL,
                policy_version TEXT NOT NULL,record TEXT NOT NULL)''')
    def callbacks(self):return {PLAN_JOB:self.defer}
    def _binding(self,parent,expected=None):
        binding=YouTubeWholeReplayPrepare(self.settings,self.store).resource_binding(parent)
        if expected is not None and digest(binding)!=expected:fail('YOUTUBE_WHOLE_READINESS_BINDING_CHANGED')
        return binding
    @staticmethod
    def _publication(db,parent):
        if db.execute("SELECT 1 FROM publications WHERE source_platform='youtube' AND source_id=?",(parent['source_id'],)).fetchone():raise PublishUnknown()
    def defer(self,job,error):
        current,binding=YouTubeWholeReplayPrepare(self.settings,self.store)._binding(job)
        read_observation(self.settings,self.store,current,binding,error.observation,reason=error.reason)
        self.initialize();now=self.clock();wait_id=str(uuid.uuid4());deadline=now+self.duration
        with self.store.transaction() as db:
            row=db.execute('SELECT * FROM jobs WHERE job_id=?',(job['job_id'],)).fetchone()
            if (not row or row['state']!='processing' or row['lease_owner']!=job['lease_owner'] or row['lease_until']<=time.time() or
                    json.loads(row['payload'])!=current['payload']):fail('YOUTUBE_WHOLE_READINESS_LEASE_CHANGED')
            self._publication(db,current)
            if db.execute('SELECT 1 FROM youtube_readiness_waits_v4 WHERE parent_job_id=?',(job['job_id'],)).fetchone():fail('YOUTUBE_WHOLE_READINESS_ALREADY_REGISTERED')
            record={'version':WAIT_VERSION,'state':'waiting_readiness','wait_id':wait_id,'created_at':now,'deadline':deadline,
                'next_check':now+INTERVAL,'observations':1,'max_observations':self.maximum,'reason':error.reason,
                'parent_attempts':row['attempts'],'attempts_reset':False,'human_review':False,'publication_authorized':False,
                'candidate_id':current['payload']['fifo_v4']['candidate_id'],
                'evidence':[{'sequence':1,'job_id':job['job_id'],'reason':error.reason,'observation':error.observation}]}
            result=json.loads(row['result']);result[WAIT]=record
            db.execute('INSERT INTO youtube_readiness_waits_v4 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (job['job_id'],wait_id,'waiting_readiness',now,deadline,now+INTERVAL,1,self.maximum,None,digest(binding),digest(current['payload']),FIFO_VERSION,encode(record)))
            db.execute("UPDATE jobs SET state='retry_scheduled',stage='waiting_readiness_v4',error_code='WAITING_YOUTUBE_READINESS',result=?,next_run=?,updated_at=?,lease_owner=NULL,lease_until=NULL WHERE job_id=?",
                (encode(result),deadline+60,now,job['job_id']))
            db.execute("UPDATE replay_fifo_v4 SET state='waiting_readiness',updated_at=? WHERE candidate_id=?",(now,current['payload']['fifo_v4']['candidate_id']))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                (now,'system:youtube-readiness-v4','readiness_begin',job['job_id'],encode({'maximum':self.maximum,'deadline':deadline,'parent_attempts_preserved':True})))
    def _hold(self,db,parent,waiting,code,now):
        record=json.loads(waiting['record']);record.update(state='hold',error_code=code,finished_at=now)
        result=json.loads(parent['result']);result[WAIT]=record
        db.execute("UPDATE youtube_readiness_waits_v4 SET state='hold',record=? WHERE parent_job_id=?",(encode(record),parent['job_id']))
        db.execute("UPDATE jobs SET state='upload_pending',stage='readiness_hold_v4',error_code=?,result=?,updated_at=? WHERE job_id=?",(code,encode(result),now,parent['job_id']))
        db.execute("UPDATE replay_fifo_v4 SET state='hold',reason=?,updated_at=? WHERE candidate_id=?",(code,now,record['candidate_id']))
        self.store._event(db,'task.failed',{'job_id':parent['job_id'],'code':code,'queue':'video','technical_hold':True})
    def tick(self):
        from adapters.ytdlp_highest_v2 import preflight_budget
        from adapters.ytdlp_whole_v4 import budget_limits,WholeMaintenance
        self.initialize();now=self.clock();out={'version':WAIT_VERSION,'enqueued':[],'held':[],'external_calls':0,'parent_attempts_changed':False}
        with resource_guard(self.settings.root/'data/locks/youtube-readiness-v4'):
            for wait in self.store.query("SELECT * FROM youtube_readiness_waits_v4 WHERE state='waiting_readiness' AND next_check<=? ORDER BY created_at LIMIT 16",(now,)):
                parent=self.store.get(wait['parent_job_id']);code=None;permitted=True;binding=None;blocker=None
                try:
                    if (not parent or parent['state']!='retry_scheduled' or parent['stage']!='waiting_readiness_v4' or parent['error_code']!='WAITING_YOUTUBE_READINESS' or
                            parent['lease_owner'] is not None or parent['lease_until'] is not None or digest(parent['payload'])!=wait['payload_sha256'] or parent['policy_version']!=wait['policy_version']):fail('YOUTUBE_WHOLE_READINESS_PARENT_CHANGED')
                    binding=self._binding(parent,wait['binding_sha256'])
                except HubError as exc:code=exc.code
                if now>=wait['deadline']:code='YOUTUBE_WHOLE_READINESS_TIMEOUT'
                child=self.store.get(wait['active_child_id']) if wait['active_child_id'] else None
                if child and child['state'] in ('failed','auth_expired','awaiting_review','publish_unknown'):
                    code=child.get('error_code') or 'YOUTUBE_WHOLE_READINESS_CHILD_STOPPED'
                if not child and wait['observations']>=wait['max_observations']:code='YOUTUBE_WHOLE_READINESS_OBSERVATION_LIMIT'
                if code is None and child is None:
                    try:
                        WholeMaintenance(self.settings,self.store).check_disk(32_000_000)
                        preflight_budget(self.store,binding,64,limits_function=budget_limits,request_cap=20000)
                    except HubError as exc:
                        if exc.code in ('DISK_LOW','API_BUDGET_EXCEEDED'):permitted=False;blocker=exc.code
                        else:code=exc.code
                with self.store.transaction() as db:
                    row=db.execute('SELECT * FROM youtube_readiness_waits_v4 WHERE parent_job_id=?',(wait['parent_job_id'],)).fetchone()
                    actual=db.execute('SELECT * FROM jobs WHERE job_id=?',(wait['parent_job_id'],)).fetchone()
                    if not row or dict(row)!=wait or not actual:continue
                    if actual['state']!='retry_scheduled' or actual['lease_owner'] is not None or actual['lease_until'] is not None:
                        db.execute("UPDATE youtube_readiness_waits_v4 SET state='hold' WHERE parent_job_id=?",(actual['job_id'],));continue
                    try:self._publication(db,parent or actual)
                    except PublishUnknown:code='RECONCILE_BEFORE_READINESS'
                    if code:self._hold(db,actual,row,code,now);out['held'].append({'job_id':actual['job_id'],'code':code});continue
                    if child:continue
                    if not permitted:
                        next_check=min(now+INTERVAL,wait['deadline']);record=json.loads(row['record']);record.update(next_check=next_check,scheduling_blocker=blocker)
                        result=json.loads(actual['result']);result[WAIT]=record
                        db.execute('UPDATE youtube_readiness_waits_v4 SET next_check=?,record=? WHERE parent_job_id=?',(next_check,encode(record),actual['job_id']))
                        db.execute('UPDATE jobs SET result=?,updated_at=? WHERE job_id=?',(encode(result),now,actual['job_id']));continue
                    if db.execute("SELECT paused FROM pipelines WHERE name='video'").fetchone()['paused']:continue
                    if db.execute("SELECT 1 FROM jobs WHERE queue='video' AND state='processing' LIMIT 1").fetchone():continue
                    ident=str(uuid.uuid4());sequence=row['observations']+1
                    payload={**deepcopy(parent['payload']),'_whole_readiness_parent_job_id':parent['job_id'],
                        '_whole_readiness_wait_id':row['wait_id'],'_whole_readiness_sequence':sequence}
                    db.execute("INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,policy_version,payload) VALUES(?,'youtube',?,?,'video','pending',?,?,?,?)",
                        (ident,parent['source_id']+':readiness:'+str(sequence),JOB_TYPE,now,now,FIFO_VERSION,encode(payload)))
                    db.execute('UPDATE youtube_readiness_waits_v4 SET active_child_id=?,observations=? WHERE parent_job_id=?',(ident,sequence,parent['job_id']))
                    out['enqueued'].append(ident)
        return out

    def observed(self,job,checkpoint):
        service=WholeReadinessObservationV4(self.settings,self.store,coordinator=self);current,binding=service._binding(job)
        read_observation(self.settings,self.store,current,binding,checkpoint['observation'],reason=checkpoint.get('reason'),receipt=checkpoint.get('receipt'))
        parent_id=current['payload']['_whole_readiness_parent_job_id'];now=self.clock()
        with self.store.transaction() as db:
            wait=db.execute('SELECT * FROM youtube_readiness_waits_v4 WHERE parent_job_id=?',(parent_id,)).fetchone();parent=db.execute('SELECT * FROM jobs WHERE job_id=?',(parent_id,)).fetchone()
            if (not wait or wait['state']!='waiting_readiness' or wait['active_child_id']!=job['job_id'] or now>=wait['deadline'] or
                    not parent or parent['state']!='retry_scheduled' or parent['stage']!='waiting_readiness_v4' or parent['lease_owner'] is not None or parent['lease_until'] is not None or
                    digest(json.loads(parent['payload']))!=wait['payload_sha256']):fail('YOUTUBE_WHOLE_READINESS_PARENT_CHANGED')
            self._publication(db,parent)
            record=json.loads(wait['record']);result=json.loads(parent['result'])
            if record['evidence'][-1]['job_id']==job['job_id']:return
            record['evidence'].append({'sequence':current['payload']['_whole_readiness_sequence'],'job_id':job['job_id'],
                'reason':checkpoint.get('reason'),'observation':checkpoint['observation']});record['observations']=wait['observations']
            if checkpoint['state']=='not_ready':
                record.update(next_check=min(now+INTERVAL,wait['deadline']),reason=checkpoint['reason']);result[WAIT]=record
                db.execute('UPDATE youtube_readiness_waits_v4 SET active_child_id=NULL,next_check=?,record=? WHERE parent_job_id=?',(record['next_check'],encode(record),parent_id))
                db.execute('UPDATE jobs SET result=?,updated_at=? WHERE job_id=?',(encode(result),now,parent_id));return
            plan=validate_whole_replay_plan(checkpoint['receipt']['source_plan'],binding['planning_policy'],official_raw=binding['official_raw'])
            actual_parent=self.store.get(parent_id);self._binding(actual_parent,wait['binding_sha256'])
            c=service.fifo.candidate(actual_parent['payload']['fifo_v4']['candidate_id']);service.fifo._fresh(c,service.fifo.policy())
            if service.fifo._existing(db,parent['source_id'],except_jobs=(parent_id,)):fail('YOUTUBE_WHOLE_READINESS_SOURCE_CONFLICT')
            record.update(state='ready',ready_at=now);result[WAIT]=record
            result['replay_fifo_plan_v4']={'version':FIFO_VERSION,'candidate_id':c['candidate_id'],'state':'planned','plan':plan,
                'plan_sha256':digest(plan),'planning_policy_sha256':digest(binding['planning_policy']),'human_review':False,'publication_authorized':False}
            db.execute("UPDATE youtube_readiness_waits_v4 SET state='ready',record=? WHERE parent_job_id=?",(encode(record),parent_id))
            db.execute("UPDATE jobs SET state='completed',stage='youtube_whole_ready_plan',error_code=NULL,result=?,updated_at=? WHERE job_id=?",(encode(result),now,parent_id))
            db.execute("UPDATE replay_fifo_v4 SET state='plan_queued',reason=NULL,updated_at=? WHERE candidate_id=?",(now,c['candidate_id']))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                (now,'system:youtube-readiness-v4','readiness_ready',parent_id,encode({'observation_job_id':job['job_id'],'download_jobs_created':0,'parent_attempts_reset':False})))


class WholeReadinessObservationV4(YouTubeWholeReplayPrepare):
    JOB_TYPE=JOB_TYPE
    def __init__(self,*args,coordinator=None,**kwargs):
        super().__init__(*args,**kwargs);self.coordinator=coordinator or WholeReadinessCoordinatorV4(self.settings,self.store)
    def resource_binding(self,current):
        p=current['payload'];seq=p.get('_whole_readiness_sequence');parent=self.store.get(p.get('_whole_readiness_parent_job_id'))
        rows=self.store.query('SELECT * FROM youtube_readiness_waits_v4 WHERE parent_job_id=?',(p.get('_whole_readiness_parent_job_id'),))
        if (type(seq) is not int or not 2<=seq<=96 or not parent or len(rows)!=1 or rows[0]['state']!='waiting_readiness' or
                rows[0]['active_child_id']!=current['job_id'] or rows[0]['wait_id']!=p.get('_whole_readiness_wait_id') or rows[0]['observations']!=seq or
                current['source_id']!=parent['source_id']+':readiness:'+str(seq) or parent['state']!='retry_scheduled' or parent['stage']!='waiting_readiness_v4' or
                self.coordinator.clock()>=rows[0]['deadline']):fail('YOUTUBE_WHOLE_READINESS_PARENT_CHANGED')
        original={k:v for k,v in p.items() if k not in ('_whole_readiness_parent_job_id','_whole_readiness_wait_id','_whole_readiness_sequence')}
        if original!=parent['payload'] or digest(original)!=rows[0]['payload_sha256']:fail('YOUTUBE_WHOLE_READINESS_BINDING_CHANGED')
        b=YouTubeWholeReplayPrepare(self.settings,self.store).resource_binding(parent)
        if digest(b)!=rows[0]['binding_sha256']:fail('YOUTUBE_WHOLE_READINESS_BINDING_CHANGED')
        return {**b,'job_id':current['job_id'],'source_payload_sha256':digest(p),'readiness_parent_job_id':parent['job_id'],
            'readiness_wait_id':rows[0]['wait_id'],'readiness_sequence':seq}
    def _binding(self,job):
        current=self.store.get(job['job_id'])
        if (not current or current['job_type']!=JOB_TYPE or current['source_platform']!='youtube' or current['queue']!='video' or
                current['policy_version']!=FIFO_VERSION or current['state']!='processing' or not job.get('lease_owner') or
                current['lease_owner']!=job['lease_owner'] or (current['lease_until'] or 0)<=time.time() or current['payload']!=job['payload'] or
                not 1<=current['attempts']<=self.settings.config['max_attempts']):fail('YOUTUBE_WHOLE_READINESS_CHILD_LEASE_CHANGED')
        return current,self.resource_binding(current)
    def process(self,job):
        current=self.store.get(job['job_id']);checkpoint=current['result'].get(WAIT) if current else None
        if (checkpoint and current['state']=='processing' and current['lease_owner']==job.get('lease_owner') and current['lease_until']>time.time() and
                current['job_type']==JOB_TYPE and current['policy_version']==FIFO_VERSION and current['payload']==job['payload']):
            rows=self.store.query('SELECT * FROM youtube_readiness_waits_v4 WHERE parent_job_id=?',(current['payload'].get('_whole_readiness_parent_job_id'),))
            parent=self.store.get(current['payload'].get('_whole_readiness_parent_job_id'))
            if (rows and parent and digest(parent['payload'])==rows[0]['payload_sha256'] and
                    json.loads(rows[0]['record'])['evidence'][-1]['job_id']==job['job_id']):
                if checkpoint.get('binding_sha256')!=digest(checkpoint.get('binding')):fail('YOUTUBE_WHOLE_READINESS_BINDING_CHANGED')
                read_observation(self.settings,self.store,current,checkpoint['binding'],checkpoint['observation'],reason=checkpoint.get('reason'),receipt=checkpoint.get('receipt'),require_fresh=False);return current['result']
        current,binding=self._binding(job)
        if not checkpoint:
            from adapters.ytdlp_whole_readiness_v4 import WholeReadinessPlannerV4
            self.fresh_observation(job,binding)
            try:
                record=(self.downloader_factory or WholeReadinessPlannerV4)(self.settings).acquire(job,binding,check=lambda:self.check_binding(job,binding))
                if (record.get('version')!=VERSION or record.get('binding_sha256')!=digest(binding) or
                        any(record.get(k) is not False for k in ('media_downloaded','human_review','publication_authorized'))):fail('YOUTUBE_WHOLE_PLAN_RECEIPT_INVALID')
                read_observation(self.settings,self.store,current,binding,record['metadata_observation'],receipt=record)
                self.fresh_observation(job,binding)
                checkpoint={'state':'ready','receipt':record,'observation':record['metadata_observation']}
            except HubError as exc:
                if exc.code not in REASONS:raise
                checkpoint={'state':'not_ready','reason':exc.code,'observation':read_failed_observation(self.settings,self.store,current,binding,exc.code)}
            checkpoint.update(binding=binding,binding_sha256=digest(binding));self.store.checkpoint(job,'youtube_whole_readiness_observed',{**current['result'],WAIT:checkpoint})
        self.coordinator.observed(job,checkpoint);return self.store.get(job['job_id'])['result']
