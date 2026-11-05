"""Persistent normal moderation polls, separate from publication attempts.

One lookup job owns one ordinary renewable lease and at most four failure
retries. A normal '审核中' result completes that lookup, never resubmits the
parent or resets the parent's upload-attempt counter.
"""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib,json,re,time,uuid
from .db import encode
from .errors import HubError
from .locks import resource_guard

from .youtube_download import digest




def poll_policy(policy):
    result={key:policy.get(key,default) for key,default in {'moderation_fast_seconds':300,'moderation_slow_seconds':1800,
        'moderation_max_hours':48,'moderation_daily_polls':96,'moderation_daily_attempts':384}.items()}
    limits={'moderation_fast_seconds':(60,600),'moderation_slow_seconds':(300,3600),'moderation_max_hours':(24,168),
        'moderation_daily_polls':(1,1000),'moderation_daily_attempts':(1,4000)}
    if any(type(result[k]) is not int or not low<=result[k]<=high for k,(low,high) in limits.items()):
        raise HubError('REPLAY_MODERATION_MODERATION_POLICY_REQUIRED',review=True)
    return result


def day_bounds(now):
    beginning=datetime.fromtimestamp(now,timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
    return beginning,beginning+86400


def schedule_tick(settings,store,service,*,now=None):
    now=time.time() if now is None else now;policy=service.policy();service._require_workflow_launch(policy);limits=poll_policy(policy)
    LOOKUP_JOB_TYPE,VERSION,JOB_TYPE,STATE,POLICY=service.LOOKUP_JOB_TYPE,service.MODERATION_VERSION,service.JOB_TYPE,service.STATE,service.POLICY
    scheduled=[];stalled=[];day_start,next_day=day_bounds(now)
    with resource_guard(settings.root/('data/locks/'+VERSION)):
        with store.transaction() as db:
            daily=db.execute("SELECT COUNT(*) FROM audit WHERE action=? AND at>=? AND at<?",(service.MODERATION_AUDIT_PREFIX+'_moderation_poll_scheduled',day_start,next_day)).fetchone()[0]
            rows=db.execute("""SELECT * FROM jobs WHERE source_platform='youtube' AND job_type=? AND policy_version=?
                AND state IN ('awaiting_moderation','publish_unknown') ORDER BY updated_at LIMIT 50""",(JOB_TYPE,POLICY)).fetchall()
            for row in rows:
                binding=json.loads(row['payload']);result=json.loads(row['result']);state=result.get(STATE,{})
                observed=state.get('result_observation',{});bv=row['external_publish_id'];attempt=state.get('attempt_id')
                if (state.get('state')!='awaiting_moderation' or state.get('public_submission_approved') is not False or
                        not isinstance(bv,str) or not re.fullmatch('BV[A-Za-z0-9]{10}',bv) or observed.get('external_id')!=bv or
                        observed.get('version')!=service.RESULT_OBSERVATION_VERSION or observed.get('state')!='awaiting_moderation' or
                        observed.get('site_uid')!=binding.get('site_uid') or observed.get('source_url')!=binding.get('source_url') or
                        observed.get('metadata_sha256')!=binding.get('metadata_sha256') or not isinstance(attempt,str)):
                    continue
                try:
                    if str(uuid.UUID(attempt))!=attempt:continue
                except ValueError:continue
                publication=db.execute('SELECT * FROM publications WHERE job_id=? AND idempotency_key=?',(row['job_id'],state.get('key'))).fetchall()
                if (len(publication)!=1 or publication[0]['kind']!=service.KIND or publication[0]['state']!='awaiting_moderation' or
                        publication[0]['external_id']!=bv or publication[0]['source_id']!=row['source_id'] or
                        publication[0]['account_id']!='site_uid:'+binding['site_uid']):continue
                m=deepcopy(state.get('moderation',{'version':VERSION,'first_observed_at':row['updated_at'],
                    'polls_total':0,'next_check_at':row['updated_at']+limits['moderation_fast_seconds'],'active_lookup_job_id':None}))
                if m.get('version')!=VERSION or not isinstance(m.get('polls_total'),int) or m['polls_total']<0:continue
                if m.get('active_lookup_job_id'):
                    child=db.execute('SELECT state FROM jobs WHERE job_id=?',(m['active_lookup_job_id'],)).fetchone()
                    if child and child['state'] in ('pending','processing','retry_scheduled'):continue
                    if child and child['state'] in ('auth_expired','awaiting_review'):
                        m.update(status='blocked',next_check_at=None);state={**state,'moderation':m}
                        db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode({**result,STATE:state}),row['job_id']));continue
                    m['active_lookup_job_id']=None
                if m.get('status') in ('blocked','stalled'):continue
                if now-m['first_observed_at']>=limits['moderation_max_hours']*3600:
                    m.update(status='stalled',next_check_at=None)
                    db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode({**result,STATE:{**state,'moderation':m}}),row['job_id']))
                    store._event(db,'pipeline.stalled',{'job_id':row['job_id'],'code':'BILIBILI_MODERATION_TIME_LIMIT'})
                    stalled.append(row['job_id']);continue
                if m.get('next_check_at',now+1) is None or m.get('next_check_at',now+1)>now:continue
                if daily>=limits['moderation_daily_polls']:
                    m['next_check_at']=next_day
                    db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode({**result,STATE:{**state,'moderation':m}}),row['job_id']));continue
                ident=str(uuid.uuid4());number=m['polls_total']+1
                payload={'version':service.LOOKUP_PAYLOAD_VERSION,'parent_job_id':row['job_id'],'parent_binding_sha256':digest(binding),
                    'attempt_id':attempt,'site_uid':binding['site_uid'],'source_id':row['source_id'],'source_url':binding['source_url'],
                    'metadata_sha256':binding['metadata_sha256'],'external_id':bv,'poll_number':number,'human_review':False,'publication_authorized':False}
                db.execute("""INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,policy_version,payload)
                    VALUES(?,'bilibili',?,?,'video','pending',?,?,?,?)""",(ident,row['job_id']+':'+str(number),LOOKUP_JOB_TYPE,now,now,POLICY,encode(payload)))
                interval=limits['moderation_fast_seconds'] if now-m['first_observed_at']<3600 else limits['moderation_slow_seconds']
                m.update(polls_total=number,active_lookup_job_id=ident,next_check_at=now+interval,status='awaiting_moderation')
                db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode({**result,STATE:{**state,'moderation':m}}),row['job_id']))
                db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                    (now,service.LOOKUP_PRINCIPAL,service.MODERATION_AUDIT_PREFIX+'_moderation_poll_scheduled',row['job_id'],'independent_lookup_no_parent_attempt_reset'))
                daily+=1;scheduled.append(ident);break
    return {'version':VERSION,'scheduled_lookup_jobs':scheduled,'stalled_parent_jobs':stalled,'parent_attempts_reset':False,'submissions':0}


class QueueView:
    """Type-scoped claim/recovery without altering the jobs queue CHECK.

    Both streams persist in the existing video queue. The side worker owns a
    distinct OS lock and can recover only its own lookup rows; the main worker
    excludes them. It cannot recover a concurrently running long download.
    """
    def __init__(self,store,*,lookups,lookup_job_type,heartbeat_suffix='v3'):
        self.store,self.lookups,self.lookup_job_type,self.heartbeat_suffix=store,lookups,lookup_job_type,heartbeat_suffix
    def __getattr__(self,name):return getattr(self.store,name)
    def beat(self,name):
        if self.lookups:self.store.beat('worker:video-moderation-'+self.heartbeat_suffix)
        else:
            self.store.beat(name)
            self.store.beat('worker:video-main-'+self.heartbeat_suffix)
    def _queue(self,queue):
        if queue!=('moderation' if self.lookups else 'video'):raise ValueError('REPLAY_QUEUE_VIEW_SCOPE')
    def claim(self,queue,lease_seconds,max_attempts):
        self._queue(queue);now=time.time();owner=str(uuid.uuid4());operator='=' if self.lookups else '<>'
        with self.store.transaction() as db:
            if db.execute("SELECT paused FROM pipelines WHERE name='video'").fetchone()['paused']:return None
            row=db.execute(f"""SELECT job_id FROM jobs WHERE queue='video' AND job_type{operator}?
                AND state IN ('pending','retry_scheduled') AND next_run<=? AND attempts<? ORDER BY created_at LIMIT 1""",
                (self.lookup_job_type,now,max_attempts)).fetchone()
            if not row:return None
            db.execute("UPDATE jobs SET state='processing',attempts=attempts+1,lease_owner=?,lease_until=?,updated_at=? WHERE job_id=?",
                (owner,now+lease_seconds,now,row['job_id']))
        return self.store.get(row['job_id'])
    def recover(self,queue,max_attempts,*,all_processing=False):
        self._queue(queue);now=time.time();operator='=' if self.lookups else '<>'
        with self.store.transaction() as db:
            rows=db.execute(f"SELECT * FROM jobs WHERE queue='video' AND job_type{operator}? AND state='processing' AND (? OR lease_until<?)",
                (self.lookup_job_type,all_processing,now)).fetchall()
            for row in rows:
                publication=db.execute('SELECT * FROM publications WHERE job_id=?',(row['job_id'],)).fetchone()
                if publication and publication['state']!='rejected':state,code='publish_unknown','RECONCILE_BEFORE_RETRY'
                elif row['attempts']>=max_attempts:state,code='failed','RETRY_EXHAUSTED_AFTER_RESTART'
                else:state,code='retry_scheduled','PROCESS_INTERRUPTED'
                db.execute('UPDATE jobs SET state=?,error_code=?,lease_owner=NULL,lease_until=NULL,next_run=?,updated_at=? WHERE job_id=?',
                    (state,code,now,now,row['job_id']))
                if state=='failed':self.store._event(db,'task.failed',{'job_id':row['job_id'],'code':code,'queue':queue})
        return len(rows)


def assert_lookup_lease(store,job,*,job_type,policy):
    current=store.get(job['job_id'])
    if (not current or current['job_type']!=job_type or current['policy_version']!=policy or current['state']!='processing' or
            current.get('lease_owner')!=job.get('lease_owner') or not current.get('lease_owner') or
            (current.get('lease_until') or 0)<=time.time() or current['attempts']!=job['attempts'] or current['payload']!=job['payload']):
        raise HubError('REPLAY_MODERATION_LOOKUP_LEASE_LOST',review=True)


class ModerationServiceMixin:
    def __init__(self,*args,**kwargs):super().__init__(*args,**kwargs);self._lookup_job=None
    def _operator(self,principal):
        if principal==self.LOOKUP_PRINCIPAL and self._lookup_job is not None:
            assert_lookup_lease(self.store,self._lookup_job,job_type=self.LOOKUP_JOB_TYPE,policy=self.POLICY);return
        return super()._operator(principal)
    def _result_extra_state(self,job,binding,state,observation,*,lookup):
        result=super()._result_extra_state(job,binding,state,observation,lookup=lookup)
        if self._lookup_job is not None:
            now=time.time();limits=poll_policy(self.policy());m=deepcopy(state['moderation'])
            interval=limits['moderation_fast_seconds'] if now-m['first_observed_at']<3600 else limits['moderation_slow_seconds']
            m.update(active_lookup_job_id=None,last_checked_at=now,status=observation['state'],
                next_check_at=now+interval if observation['state']=='awaiting_moderation' else None)
            result['moderation']=m
        return result
    def record_lookup(self,child,observation):
        assert_lookup_lease(self.store,child,job_type=self.LOOKUP_JOB_TYPE,policy=self.POLICY);p=child['payload'];parent,binding,state=self.snapshot(p['parent_job_id'],write=False)
        expected={'version':self.LOOKUP_PAYLOAD_VERSION,'parent_job_id':parent['job_id'],'parent_binding_sha256':digest(binding),
            'attempt_id':state['attempt_id'],'site_uid':binding['site_uid'],'source_id':parent['source_id'],'source_url':binding['source_url'],
            'metadata_sha256':binding['metadata_sha256'],'external_id':state['result_observation']['external_id'],
            'poll_number':state['moderation']['polls_total'],'human_review':False,'publication_authorized':False}
        if (p!=expected or state['moderation']['active_lookup_job_id']!=child['job_id'] or state['state']!='awaiting_moderation'):
            raise HubError('REPLAY_MODERATION_LOOKUP_PARENT_CHANGED',review=True)
        self._lookup_job=child
        try:return self.record_result(self.LOOKUP_PRINCIPAL,parent['job_id'],observation,lookup=True)
        finally:self._lookup_job=None


class ModerationLookupHandler:
    def __init__(self,settings,store,*,service,manifest_builder,browser=None):
        from .replay_video_browser import ReplayVideoBrowser
        self.settings,self.store=settings,store;self.service=service;self.manifest_builder=manifest_builder
        self.browser=browser or ReplayVideoBrowser(settings)
    def process(self,job):
        assert_lookup_lease(self.store,job,job_type=self.service.LOOKUP_JOB_TYPE,policy=self.service.POLICY);p=job['payload'];parent,binding,state=self.service.snapshot(p['parent_job_id'],write=False)
        if (state.get('state')!='awaiting_moderation' or state.get('moderation',{}).get('active_lookup_job_id')!=job['job_id'] or
                p.get('parent_binding_sha256')!=digest(binding) or p.get('external_id')!=state.get('result_observation',{}).get('external_id')):
            raise HubError('REPLAY_MODERATION_LOOKUP_PARENT_CHANGED',review=True)
        now=time.time();limits=poll_policy(self.service.policy());start,end=day_bounds(now)
        with self.store.transaction() as db:
            count=db.execute("SELECT COUNT(*) FROM audit WHERE action=? AND at>=? AND at<?",(self.service.MODERATION_AUDIT_PREFIX+'_moderation_lookup_attempt',start,end)).fetchone()[0]
            if count>=limits['moderation_daily_attempts']:
                raise HubError('REPLAY_MODERATION_MODERATION_DAILY_LIMIT',retryable=True,retry_after=end-now)
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                (now,self.service.LOOKUP_PRINCIPAL,self.service.MODERATION_AUDIT_PREFIX+'_moderation_lookup_attempt',job['job_id'],'native_lookup_reserved_no_submit'))
        source=self.store.get(binding['download_job_id']);duration=source['result'][self.service.DOWNLOAD_STATE]['binding']['duration']
        manifest=self.manifest_builder(self.settings,self.store,parent,binding,state,state['attempt_id'],duration_seconds=duration,require_cover=False)
        try:
            with self.browser.session(manifest,assert_lease=lambda:assert_lookup_lease(self.store,job,job_type=self.service.LOOKUP_JOB_TYPE,policy=self.service.POLICY)) as session:
                value=session.operation('lookup',external_id=p['external_id'])
        except HubError as exc:
            if exc.code in ('REPLAY_BROWSER_RESPONSE_UNKNOWN','REPLAY_BROWSER_TRANSPORT_UNAVAILABLE'):
                raise HubError('REPLAY_MODERATION_MODERATION_LOOKUP_UNRESOLVED',retryable=True,retry_after=limits['moderation_fast_seconds']) from None
            raise
        assert_lookup_lease(self.store,job,job_type=self.service.LOOKUP_JOB_TYPE,policy=self.service.POLICY);observation=value.get('result_observation')
        if not observation:raise HubError('REPLAY_MODERATION_MODERATION_LOOKUP_UNRESOLVED',retryable=True,retry_after=limits['moderation_fast_seconds'])
        self.service.record_lookup(job,observation)
        return {'version':self.service.MODERATION_VERSION,'parent_job_id':parent['job_id'],'lookup_only':True,'observed_state':observation['state'],
            'parent_attempts_reset':False,'public_submit_performed':False}
