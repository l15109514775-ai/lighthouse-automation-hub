"""V3-only bounded local disk waits. Polling never claims a job or calls HTTP.

No jobs-table migration: the durable resource substate is waiting_resource;
the existing retry_scheduled row is fenced until deadline+60. Only a validated
coordinator resume makes it runnable earlier. Ordinary claim increments the
existing attempt counter; no external attempt is ever reset/refunded.
"""
from datetime import datetime
import hashlib,json,math,re,shutil,time,uuid
from zoneinfo import ZoneInfo

from .db import encode
from .errors import HubError,PublishUnknown,ResourceDeferred
from .locks import resource_guard

VERSION='local-resource-wait-v1'
ALLOWED=frozenset(('youtube_highest_prepare_v3','youtube_download_v3','replay_natural_video_publish_v3'))
INTERVAL=300
DEADLINE=86400
MAX_POLLS=288

def digest(value):return hashlib.sha256(encode(value).encode()).hexdigest()


class ResourceWaitCoordinator:
    def __init__(self,settings,store,*,validators,clock=time.time,disk_free=None):
        if not validators or not set(validators)<=ALLOWED or not all(callable(v) for v in validators.values()):
            raise ValueError('RESOURCE_WAIT_V3_REGISTRATION_REQUIRED')
        self.settings,self.store,self.validators=settings,store,dict(validators)
        self.clock=clock;self.disk_free=disk_free or (lambda:shutil.disk_usage(settings.root).free)
        hours=settings.config.get('resource_wait_max_hours',24)
        if type(hours) is not int or not 24<=hours<=72:raise ValueError('RESOURCE_WAIT_LIMIT_MUST_BE_24_TO_72_HOURS')
        self.max_wait=hours*3600;self.max_polls=hours*12
        floor=settings.config.get('disk_floor_gb')
        if type(floor) not in (int,float) or not math.isfinite(floor) or floor<15:raise ValueError('INVALID_RESOURCE_DISK_FLOOR')

    def initialize(self):
        with self.store.transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS resource_waits_v1(
                job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),wait_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('waiting_resource','ready','completed','failed')),
                created_at REAL NOT NULL,deadline REAL NOT NULL,next_check REAL NOT NULL,
                polls_total INTEGER NOT NULL,poll_day TEXT NOT NULL,polls_today INTEGER NOT NULL,
                required_bytes INTEGER NOT NULL,binding_sha256 TEXT NOT NULL,payload_sha256 TEXT NOT NULL,
                policy_version TEXT NOT NULL,record TEXT NOT NULL)''')

    def callbacks(self):return {name:self.defer for name in self.validators}

    def _validate(self,job,expected=None):
        if job['job_type'] not in self.validators or job['queue']!='video' or job['source_platform']!='youtube':raise HubError('RESOURCE_WAIT_V3_REGISTRATION_REQUIRED')
        value=self.validators[job['job_type']](job)
        if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{64}',value) or expected is not None and value!=expected:
            raise HubError('RESOURCE_WAIT_BINDING_CHANGED')
        return value

    @staticmethod
    def _publication(db,job_id):
        return db.execute("SELECT 1 FROM publications WHERE job_id=? AND (state<>'rejected' OR external_id IS NOT NULL OR external_url IS NOT NULL) LIMIT 1",(job_id,)).fetchone()

    def defer(self,job,error):
        if not isinstance(error,ResourceDeferred):raise HubError('RESOURCE_WAIT_TYPED_EXCEPTION_REQUIRED')
        self._validate(job,error.binding_sha256);self.initialize();now=self.clock()
        day=datetime.fromtimestamp(now,ZoneInfo('Asia/Shanghai')).date().isoformat()
        with self.store.transaction() as db:
            row=db.execute('SELECT * FROM jobs WHERE job_id=?',(job['job_id'],)).fetchone()
            if (not row or row['state']!='processing' or row['lease_owner']!=job.get('lease_owner') or
                    (row['lease_until'] or 0)<=time.time() or row['attempts']!=job['attempts'] or
                    json.loads(row['payload'])!=job['payload']):raise HubError('RESOURCE_WAIT_LEASE_CHANGED')
            if self._publication(db,job['job_id']):raise PublishUnknown()
            old=db.execute('SELECT * FROM resource_waits_v1 WHERE job_id=?',(job['job_id'],)).fetchone()
            if old and (old['binding_sha256']!=error.binding_sha256 or old['payload_sha256']!=digest(job['payload']) or
                    old['policy_version']!=row['policy_version']):raise HubError('RESOURCE_WAIT_BINDING_CHANGED')
            if old and old['state']=='failed':raise HubError('RESOURCE_WAIT_ALREADY_EXPIRED')
            created=old['created_at'] if old else now;deadline=old['deadline'] if old else now+self.max_wait
            if now>=deadline:raise HubError('RESOURCE_WAIT_TIMEOUT')
            if row['attempts']>=min(4,self.settings.config['max_attempts']):raise HubError('RESOURCE_WAIT_RETRY_EXHAUSTED')
            wait_id=old['wait_id'] if old else str(uuid.uuid4());polls=old['polls_total'] if old else 0
            today=old['polls_today'] if old and old['poll_day']==day else 0
            result=json.loads(row['result']);prior=json.loads(old['record']) if old else {}
            record={'version':VERSION,'state':'waiting_resource','wait_id':wait_id,'resource':'disk',
                'required_bytes':error.required_bytes,'binding_sha256':error.binding_sha256,'payload_sha256':digest(job['payload']),
                'created_at':created,'deadline':deadline,'next_check':min(deadline,now+INTERVAL),
                'polls_total':polls,'polls_today':today,'poll_day':day,'attempts_at_wait':row['attempts'],
                'resumptions':prior.get('resumptions',0),'attempts_reset':False,'external_calls_by_poller':0}
            result['resource_wait_v1']=record
            db.execute('''INSERT INTO resource_waits_v1 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET state=excluded.state,next_check=excluded.next_check,
                poll_day=excluded.poll_day,polls_today=excluded.polls_today,required_bytes=excluded.required_bytes,record=excluded.record''',
                (job['job_id'],wait_id,'waiting_resource',created,deadline,record['next_check'],polls,day,today,
                 error.required_bytes,error.binding_sha256,digest(job['payload']),row['policy_version'],encode(record)))
            changed=db.execute('''UPDATE jobs SET state='retry_scheduled',error_code='WAITING_RESOURCE',stage='waiting_resource',
                result=?,next_run=?,updated_at=?,lease_owner=NULL,lease_until=NULL
                WHERE job_id=? AND state='processing' AND lease_owner=?''',
                (encode(result),deadline+60,now,job['job_id'],job['lease_owner'])).rowcount
            if changed!=1:raise HubError('RESOURCE_WAIT_LEASE_CHANGED')
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                (now,'system:resource-wait-v1','resource_wait_begin',job['job_id'],encode({'wait_id':wait_id,'required_bytes':error.required_bytes,'attempts_preserved':row['attempts']})))
            self.store._event(db,'disk.low',{'job_id':job['job_id'],'queue':'video','code':'WAITING_RESOURCE','required_bytes':error.required_bytes})
        job.update(state='retry_scheduled',result=result,error_code='WAITING_RESOURCE',stage='waiting_resource',next_run=deadline+60,lease_owner=None,lease_until=None)
        return record

    def tick(self):
        self.initialize();now=self.clock();summary={'version':VERSION,'checked':0,'waiting':[],'resumed':[],'failed':[],
            'external_calls':0,'attempts_reset':False}
        with resource_guard(self.settings.root/'data/locks/resource-wait-v1'):
            with self.store.transaction() as db:
                settled=db.execute("SELECT w.job_id,w.record,j.state FROM resource_waits_v1 w JOIN jobs j ON j.job_id=w.job_id WHERE w.state='ready' AND j.state IN ('completed','failed','publish_unknown','awaiting_review','auth_expired') LIMIT 16").fetchall()
                for row in settled:
                    record=json.loads(row['record']);state='completed' if row['state']=='completed' else 'failed'
                    record.update(state=state,finished_at=now,job_final_state=row['state'])
                    db.execute('UPDATE resource_waits_v1 SET state=?,record=? WHERE job_id=?',(state,encode(record),row['job_id']))
            rows=self.store.query("SELECT * FROM resource_waits_v1 WHERE state='waiting_resource' AND next_check<=? ORDER BY created_at LIMIT 16",(now,))
            for waiting in rows:
                job=self.store.get(waiting['job_id']);code=None
                try:
                    if not job or job['state']!='retry_scheduled' or job['error_code']!='WAITING_RESOURCE' or job['stage']!='waiting_resource' or job['lease_owner'] is not None or job['lease_until'] is not None:
                        raise HubError('RESOURCE_WAIT_JOB_CHANGED')
                    if digest(job['payload'])!=waiting['payload_sha256'] or job['policy_version']!=waiting['policy_version']:
                        raise HubError('RESOURCE_WAIT_BINDING_CHANGED')
                    self._validate(job,waiting['binding_sha256'])
                except HubError as exc:code=exc.code if re.fullmatch('[A-Z][A-Z0-9_]{0,79}',exc.code) else 'RESOURCE_WAIT_BINDING_CHANGED'
                except Exception:code='RESOURCE_WAIT_BINDING_CHANGED'
                day=datetime.fromtimestamp(now,ZoneInfo('Asia/Shanghai')).date().isoformat()
                today=waiting['polls_today'] if waiting['poll_day']==day else 0
                if now>=waiting['deadline'] or waiting['polls_total']>=self.max_polls or today>=MAX_POLLS:code='RESOURCE_WAIT_TIMEOUT'
                if job and job['attempts']>=min(4,self.settings.config['max_attempts']):code='RESOURCE_WAIT_RETRY_EXHAUSTED'
                unavailable=False
                try:free=self.disk_free() if code is None else None
                except OSError:free=None;unavailable=True
                floor=int(self.settings.config['disk_floor_gb']*1_000_000_000)
                if code is None and not unavailable and (type(free) is not int or free<0 or floor<15_000_000_000):code='RESOURCE_WAIT_DISK_OBSERVATION_INVALID'
                with self.store.transaction() as db:
                    actual=db.execute('SELECT * FROM resource_waits_v1 WHERE job_id=?',(waiting['job_id'],)).fetchone()
                    current=db.execute('SELECT * FROM jobs WHERE job_id=?',(waiting['job_id'],)).fetchone()
                    if not actual or dict(actual)!=waiting or not current:continue
                    if current['state']!='retry_scheduled' or current['error_code']!='WAITING_RESOURCE' or current['lease_owner'] is not None or current['lease_until'] is not None:
                        state='completed' if current['state']=='completed' else 'failed'
                        record=json.loads(actual['record']);record.update(state=state,finished_at=now,error_code='RESOURCE_WAIT_JOB_CHANGED')
                        db.execute('UPDATE resource_waits_v1 SET state=?,record=? WHERE job_id=?',(state,encode(record),waiting['job_id']))
                        continue
                    if digest(json.loads(current['payload']))!=waiting['payload_sha256'] or current['policy_version']!=waiting['policy_version']:code='RESOURCE_WAIT_BINDING_CHANGED'
                    if self._publication(db,waiting['job_id']):code='RECONCILE_BEFORE_RESOURCE_RESUME'
                    result=json.loads(current['result']);record=json.loads(actual['record']);summary['checked']+=1
                    if code:
                        state='publish_unknown' if code in ('RECONCILE_BEFORE_RESOURCE_RESUME','PUBLISH_UNKNOWN') else 'failed'
                        record.update(state='failed',error_code=code,finished_at=now)
                        result['resource_wait_v1']=record
                        db.execute('UPDATE resource_waits_v1 SET state=\'failed\',record=? WHERE job_id=?',(encode(record),waiting['job_id']))
                        db.execute('UPDATE jobs SET state=?,error_code=?,result=?,updated_at=? WHERE job_id=?',
                            (state,code,encode(result),now,waiting['job_id']))
                        self.store._event(db,'task.failed',{'job_id':waiting['job_id'],'code':code,'queue':'video'})
                        summary['failed'].append({'job_id':waiting['job_id'],'error_code':code});continue
                    paused=db.execute("SELECT paused FROM pipelines WHERE name='video'").fetchone()['paused']
                    busy=db.execute("SELECT 1 FROM jobs WHERE queue='video' AND job_id<>? AND (state='processing' OR (state IN ('pending','retry_scheduled') AND next_run<=?)) LIMIT 1",
                        (waiting['job_id'],now)).fetchone()
                    polls=waiting['polls_total']+1;today+=1
                    record.update(polls_total=polls,polls_today=today,poll_day=day,last_checked_at=now,free_bytes=free,floor_bytes=floor,next_check=min(waiting['deadline'],now+INTERVAL))
                    enough=free is not None and not paused and not busy and free-waiting['required_bytes']>=floor
                    if enough:
                        record.update(state='ready',resumed_at=now,resumptions=record['resumptions']+1)
                        result['resource_wait_v1']=record
                        db.execute("UPDATE jobs SET next_run=?,stage='resource_resume_scheduled',error_code=NULL,result=?,updated_at=? WHERE job_id=?",
                            (now,encode(result),now,waiting['job_id']))
                        summary['resumed'].append(waiting['job_id'])
                        db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                            (now,'system:resource-wait-v1','resource_wait_resume',waiting['job_id'],encode({'wait_id':waiting['wait_id'],'attempts_preserved':current['attempts'],'polls':polls})))
                    else:
                        record['last_reason']='disk_observation_unavailable' if unavailable else 'pipeline_paused' if paused else 'video_queue_busy' if busy else 'disk_low'
                        result['resource_wait_v1']=record
                        db.execute('UPDATE jobs SET result=?,updated_at=? WHERE job_id=?',(encode(result),now,waiting['job_id']))
                        summary['waiting'].append(waiting['job_id'])
                    db.execute('UPDATE resource_waits_v1 SET state=?,next_check=?,polls_total=?,poll_day=?,polls_today=?,record=? WHERE job_id=?',
                        ('ready' if enough else 'waiting_resource',record['next_check'],polls,day,today,encode(record),waiting['job_id']))
        return summary
