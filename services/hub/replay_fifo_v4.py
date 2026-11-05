"""Durable whole-replay FIFO, independent of V3's after-activation contract.

Registration is lossless and unmetered. Only media dispatch spends a daily
slot. This module never uploads, publishes, deletes, resets attempts or grants
machine acceptance. New V4 handlers must be registered before job dispatch.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import time
import uuid
from zoneinfo import ZoneInfo

from .db import encode
from .errors import HubError
from .locks import resource_guard
from .youtube_download import CHANNELS

VERSION='replay-fifo-v4'
POLICY_VERSION='replay-fifo-policy-v4'
POLICY_FILE='replay-fifo-policy-v4.json'
PLAN_JOB='youtube_replay_plan_v4'
DOWNLOAD_JOB='youtube_replay_download_v4'
PUBLISH_JOB='replay_multipart_publish_v4'
REFRESH_JOB='youtube_replay_refresh_v4'
PLANNING_SOURCE='replay_fifo_planning_source_v1'
MAX_DURATION_REVISION_SECONDS=5
CUTOFF='2026-10-31T16:00:00Z'
UID='1000000000000000'
ASSET_JOBS=('youtube_download','youtube_download_v2','youtube_highest_prepare_v3','youtube_download_v3',
            PLAN_JOB,DOWNLOAD_JOB,PUBLISH_JOB,'replay_video_publish','replay_highest_video_publish_v2',
            'replay_natural_video_publish_v3')
DEFAULT_POLICY={'version':POLICY_VERSION,'enabled':False,'source_scope':'all_eligible_ended_since_operating_cutoff',
    'source_not_before':CUTOFF,'allowed_channel_ids':sorted(CHANNELS),'target_site_uid':UID,
    'activated_at':'REPLACE_WITH_REAL_AUTHORIZATION_TIME','owner_queue_reference':'REPLACE_WITH_REAL_USER_REFERENCE',
    'max_download_dispatches_per_day':4,'dispatch_timezone':'Asia/Shanghai','source_max_age_seconds':300,
    'resource_wait_max_seconds':259200,'planning_policy_sha256':'REPLACE_WITH_REAL_POLICY_SHA256'}


def sha(value):return hashlib.sha256(encode(value).encode()).hexdigest()
def stamp(value):
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None:raise ValueError()
    return parsed.timestamp()
def observed_duration(value):
    # Registration has no hidden per-video media limit. The separately
    # versioned whole planner decides whether a registered replay fits its
    # actual part/space bounds, leaving an explicit hold when it cannot.
    if not isinstance(value,str) or len(value)>128:raise ValueError()
    match=re.fullmatch(r'P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?',value)
    if not match or not any(match.groups()):raise ValueError()
    result=sum(float(v or 0)*scale for v,scale in zip(match.groups(),(86400,3600,60,1)))
    if not math.isfinite(result) or result<=0:raise ValueError()
    return result
def fail(code):raise HubError(code,review=True)
def parsed_job(row):
    result=dict(row)
    for key in ('payload','result'):result[key]=json.loads(result[key])
    return result


class ReplayFIFOV4:
    def __init__(self,settings,store,*,clock=time.time):
        self.settings,self.store,self.clock=settings,store,clock

    def policy(self):
        file=self.settings.root/'config'/POLICY_FILE
        if not file.exists():fail('REPLAY_FIFO_V4_DISABLED')
        try:
            if (file.resolve()!=file or any(p.is_symlink() for p in (file,*file.parents)) or
                    file.stat().st_size>16384 or os.name=='posix' and file.stat().st_mode&0o022):raise ValueError()
            raw=file.read_bytes();p=json.loads(raw)
            if p.get('enabled') is False:fail('REPLAY_FIFO_V4_DISABLED')
            if (set(p)!=set(DEFAULT_POLICY) or p['version']!=POLICY_VERSION or p['enabled'] is not True or
                    p['source_scope']!=DEFAULT_POLICY['source_scope'] or p['source_not_before']!=CUTOFF or
                    p['target_site_uid']!=UID or p['target_site_uid']!=self.settings.config['target_bilibili_site_uid'] or
                    p['allowed_channel_ids']!=sorted(CHANNELS) or
                    {s['youtube_channel_id'] for s in self.settings.config['sources']}!=CHANNELS or
                    p['dispatch_timezone']!='Asia/Shanghai' or p['source_max_age_seconds']!=300 or
                    type(p['max_download_dispatches_per_day']) is not int or not 1<=p['max_download_dispatches_per_day']<=4 or
                    type(p['resource_wait_max_seconds']) is not int or not 3600<=p['resource_wait_max_seconds']<=259200 or
                    not re.fullmatch('[a-f0-9]{64}',p['planning_policy_sha256']) or stamp(p['activated_at'])>self.clock() or
                    not isinstance(p['owner_queue_reference'],str) or not 0<len(p['owner_queue_reference'].strip())<=2048 or
                    p['owner_queue_reference'].startswith(('REPLACE','UNCONFIGURED'))):raise ValueError()
            return {**p,'_sha256':hashlib.sha256(raw).hexdigest()}
        except HubError:raise
        except (OSError,ValueError,KeyError,TypeError,AttributeError):fail('REPLAY_FIFO_V4_POLICY_INVALID')

    def initialize(self):
        # Explicit V4 initialization only; old migrations and V3 jobs are untouched.
        statements=(
            """CREATE TABLE IF NOT EXISTS replay_fifo_v4(
                candidate_id TEXT PRIMARY KEY,source_id TEXT NOT NULL,target_uid TEXT NOT NULL,
                channel_id TEXT NOT NULL,actual_start REAL,actual_end REAL,registered_at REAL NOT NULL,
                updated_at REAL NOT NULL,state TEXT NOT NULL,reason TEXT,policy_sha256 TEXT NOT NULL,
                identity_sha256 TEXT,source_payload TEXT NOT NULL,source_metadata_sha256 TEXT NOT NULL,
                plan_job_id TEXT REFERENCES jobs(job_id),download_job_id TEXT REFERENCES jobs(job_id),
                publication_job_id TEXT REFERENCES jobs(job_id),refresh_job_id TEXT REFERENCES jobs(job_id),
                UNIQUE(source_id,target_uid))""",
            'CREATE INDEX IF NOT EXISTS replay_fifo_due_v4 ON replay_fifo_v4(state,actual_end,source_id)',
            """CREATE TABLE IF NOT EXISTS replay_fifo_dispatches_v4(
                dispatch_id TEXT PRIMARY KEY,candidate_id TEXT NOT NULL REFERENCES replay_fifo_v4(candidate_id),
                job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id),local_day TEXT NOT NULL,
                dispatched_at REAL NOT NULL,policy_sha256 TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS replay_fifo_resource_holds_v4(
                job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),candidate_id TEXT NOT NULL REFERENCES replay_fifo_v4(candidate_id),
                state TEXT NOT NULL,first_wait_at REAL NOT NULL,deadline REAL NOT NULL,next_check REAL NOT NULL,
                required_free_bytes INTEGER NOT NULL,binding_sha256 TEXT NOT NULL,payload_sha256 TEXT NOT NULL,
                polls INTEGER NOT NULL DEFAULT 0,record TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS replay_fifo_capacity_v4(
                candidate_id TEXT PRIMARY KEY REFERENCES replay_fifo_v4(candidate_id),state TEXT NOT NULL,
                first_wait_at REAL NOT NULL,deadline REAL NOT NULL,next_check REAL NOT NULL,
                required_free_bytes INTEGER NOT NULL,plan_sha256 TEXT NOT NULL,polls INTEGER NOT NULL,record TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS replay_fifo_retired_jobs_v4(
                job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),retired_at REAL NOT NULL,
                reason TEXT NOT NULL,reference TEXT NOT NULL)""")
        with self.store.transaction() as db:
            for sql in statements:db.execute(sql)

    def _source(self,row,p):
        try:
            raw=json.loads(row['metadata']);live=raw['liveStreamingDetails'];snippet=raw['snippet']
            start,end=stamp(live['actualStartTime']),stamp(live['actualEndTime'])
            duration=observed_duration(raw['contentDetails']['duration'])
            if (row['source_api']!='youtube-data-v3-official' or row['state']!='ended' or
                    not re.fullmatch('[A-Za-z0-9_-]{11}',row['video_id']) or raw['id']!=row['video_id'] or
                    snippet['channelId']!=row['channel_id'] or row['channel_id'] not in p['allowed_channel_ids'] or
                    raw['status']['privacyStatus']!='public' or start>end or end>self.clock()+30 or
                    stamp(row['actual_start'])!=start or stamp(row['actual_end'])!=end or
                    row['duration']!=raw['contentDetails']['duration'] or row['title']!=snippet['title'] or
                    row['published_at']!=snippet['publishedAt'] or
                    row['url']!='https://www.youtube.com/watch?v='+row['video_id'] or
                    type(row['last_api_check']) not in (int,float) or not math.isfinite(row['last_api_check']) or
                    not 0<row['last_api_check']<=self.clock()+30 or len(encode(raw).encode())>1_000_000):raise ValueError()
            if start<stamp(p['source_not_before']):fail('REPLAY_FIFO_BEFORE_OPERATING_CUTOFF')
            identity={'source_id':row['video_id'],'channel_id':row['channel_id'],'actual_start':start,
                'actual_end':end,'duration':duration,'privacy_status':'public'}
            payload={'video_id':row['video_id'],'channel_id':row['channel_id'],'title':snippet['title'],
                'published_at':snippet['publishedAt'],'duration':raw['contentDetails']['duration'],
                'url':row['url'],'source_api':'youtube-data-v3-official','raw':raw}
            return identity,payload
        except HubError:raise
        except (ValueError,KeyError,TypeError,AttributeError,OverflowError):fail('REPLAY_FIFO_OFFICIAL_OBSERVATION_INVALID')

    @staticmethod
    def _existing(db,source_id,*,except_jobs=()):
        if db.execute("SELECT 1 FROM publications WHERE source_platform='youtube' AND source_id=? LIMIT 1",(source_id,)).fetchone():return 'REPLAY_FIFO_EXISTING_PUBLICATION'
        if db.execute("SELECT 1 FROM source_tombstones WHERE source_platform='youtube' AND source_id=? LIMIT 1",(source_id,)).fetchone():return 'REPLAY_FIFO_SOURCE_TOMBSTONE'
        marks=','.join('?' for _ in ASSET_JOBS)
        # Retired pre-media failures stay as evidence but no longer block a
        # re-planned acquisition of the same source.
        for row in db.execute(f"""SELECT job_id FROM jobs WHERE source_platform='youtube' AND source_id=?
                AND job_type IN ({marks}) AND job_id NOT IN (SELECT job_id FROM replay_fifo_retired_jobs_v4)""",
                (source_id,*ASSET_JOBS)):
            if row['job_id'] not in except_jobs:return 'REPLAY_FIFO_EXISTING_ASSET_TASK'
        return None

    def retire_asset(self,db,job_id,*,expected_state,expected_error,reason,reference,now):
        """Retire one failed, media-less asset job so its source can be re-planned.

        The job row, result, attempts and partial files stay untouched; only the
        FIFO exclusion set changes. Every retirement carries a reason and an
        operator reference in its own row and must match the exact failed state.
        """
        row=db.execute('SELECT state,error_code FROM jobs WHERE job_id=?',(job_id,)).fetchone()
        if not row or row['state']!=expected_state or row['error_code']!=expected_error:
            fail('REPLAY_FIFO_RETIRE_TARGET_CHANGED')
        if db.execute('SELECT 1 FROM replay_fifo_retired_jobs_v4 WHERE job_id=?',(job_id,)).fetchone():
            fail('REPLAY_FIFO_ALREADY_RETIRED')
        db.execute('INSERT INTO replay_fifo_retired_jobs_v4 VALUES(?,?,?,?)',(job_id,now,reason,reference))
        return {'job_id':job_id,'state':expected_state,'error_code':expected_error,'reason':reason}

    def register_all_observed(self,*,page_size=100):
        """Drain all observed rows, with exclusion BEFORE each bounded SQL page.

        Old/stale official observations are registered too. Freshness is an
        execution gate, never a reason to forget backlog or spend a day slot.
        """
        if type(page_size) is not int or not 1<=page_size<=500:raise ValueError('INVALID_FIFO_PAGE_SIZE')
        p=self.policy();self.initialize();created=[];held=[];excluded=[]
        with resource_guard(self.settings.root/'data/locks/replay-fifo-register-v4'):
            while True:
                rows=self.store.query("""SELECT y.* FROM youtube_videos y WHERE y.source_api='youtube-data-v3-official'
                    AND y.state='ended' AND y.channel_id IN (?,?) AND NOT EXISTS(
                    SELECT 1 FROM replay_fifo_v4 f WHERE f.source_id=y.video_id AND f.target_uid=?)
                    ORDER BY y.video_id LIMIT ?""",(*sorted(CHANNELS),p['target_site_uid'],page_size))
                if not rows:break
                for row in rows:
                    ident=str(uuid.uuid4());now=self.clock();state='registered';reason=None;identity=None;payload={}
                    try:identity,payload=self._source(row,p)
                    except HubError as exc:
                        reason=exc.code;state='excluded' if exc.code=='REPLAY_FIFO_BEFORE_OPERATING_CUTOFF' else 'hold'
                    with self.store.transaction() as db:
                        if db.execute('SELECT 1 FROM replay_fifo_v4 WHERE source_id=? AND target_uid=?',(row['video_id'],p['target_site_uid'])).fetchone():continue
                        prior=self._existing(db,row['video_id'])
                        if prior:state='existing';reason=prior
                        db.execute("""INSERT INTO replay_fifo_v4(candidate_id,source_id,target_uid,channel_id,actual_start,actual_end,
                            registered_at,updated_at,state,reason,policy_sha256,identity_sha256,source_payload,source_metadata_sha256)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(ident,row['video_id'],p['target_site_uid'],row['channel_id'],
                            identity['actual_start'] if identity else None,identity['actual_end'] if identity else None,
                            now,now,state,reason,p['_sha256'],sha(identity) if identity else None,encode(payload),
                            hashlib.sha256(row['metadata'].encode()).hexdigest()))
                        db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                            (now,'system:replay-fifo-v4','replay_fifo_register',ident,encode({'state':state,'reason':reason,
                            'source_time_preserved':True,'activation_is_not_source_cutoff':True,'human_review':False})))
                    (created if state=='registered' else excluded if state in ('excluded','existing') else held).append(ident)
        return {'version':VERSION,'registered':created,'held':held,'excluded_or_existing':excluded,
            'daily_slots_spent':0,'media_downloads':0,'publications':0}

    def candidate(self,ident):
        rows=self.store.query('SELECT * FROM replay_fifo_v4 WHERE candidate_id=?',(ident,))
        if len(rows)!=1:fail('REPLAY_FIFO_CANDIDATE_REQUIRED')
        result=rows[0];result['source_payload']=json.loads(result['source_payload']);return result

    def observed_identity(self,c,row,p):
        """Keep the registered identity; admit only a small, non-rolling length correction."""
        identity,payload=self._source(row,p)
        try:
            old=c['source_payload'];raw=old['raw'];live=raw['liveStreamingDetails'];snippet=raw['snippet']
            registered={'source_id':raw['id'],'channel_id':snippet['channelId'],
                'actual_start':stamp(live['actualStartTime']),'actual_end':stamp(live['actualEndTime']),
                'duration':observed_duration(raw['contentDetails']['duration']),'privacy_status':raw['status']['privacyStatus']}
            expected={'video_id':raw['id'],'channel_id':snippet['channelId'],'title':snippet['title'],
                'published_at':snippet['publishedAt'],'duration':raw['contentDetails']['duration'],
                'url':'https://www.youtube.com/watch?v='+raw['id'],'source_api':'youtube-data-v3-official','raw':raw}
            if (old!=expected or sha(raw)!=c['source_metadata_sha256'] or sha(registered)!=c['identity_sha256'] or
                    c['source_id']!=registered['source_id'] or c['channel_id']!=registered['channel_id'] or
                    c['target_uid']!=p['target_site_uid'] or c['actual_start']!=registered['actual_start'] or
                    c['actual_end']!=registered['actual_end'] or registered['privacy_status']!='public' or
                    any(identity[k]!=registered[k] for k in registered if k!='duration') or
                    abs(identity['duration']-registered['duration'])>MAX_DURATION_REVISION_SECONDS):raise ValueError()
            proof={'version':'replay-fifo-official-duration-observation-v1','candidate_id':c['candidate_id'],
                'registered_identity_sha256':c['identity_sha256'],'registered_metadata_sha256':c['source_metadata_sha256'],
                'registered_source_payload_sha256':sha(old),'official_raw':deepcopy(payload['raw']),
                'official_metadata_sha256':sha(payload['raw']),'observed_at':row['last_api_check'],
                'observed_identity_sha256':sha(identity),'registered_duration_seconds':registered['duration'],
                'observed_duration_seconds':identity['duration'],'duration_delta_seconds':identity['duration']-registered['duration'],
                'maximum_revision_seconds':MAX_DURATION_REVISION_SECONDS,'human_review':False,'publication_authorized':False}
            return identity,payload,proof
        except (ValueError,KeyError,TypeError,AttributeError,OverflowError):fail('REPLAY_FIFO_SOURCE_IDENTITY_CHANGED')

    def source_payload_for(self,c,job):
        """A plan's real official snapshot is immutable and follows only its own download."""
        proof=(job['result'].get(PLANNING_SOURCE) if job['job_type']==PLAN_JOB else job['payload'].get(PLANNING_SOURCE))
        if proof is None:return deepcopy(c['source_payload'])
        try:
            raw=proof['official_raw'];snippet=raw['snippet'];live=raw['liveStreamingDetails']
            row={'metadata':encode(raw),'video_id':raw['id'],'channel_id':snippet['channelId'],'title':snippet['title'],
                'published_at':snippet['publishedAt'],'duration':raw['contentDetails']['duration'],
                'actual_start':live['actualStartTime'],'actual_end':live['actualEndTime'],'state':'ended',
                'source_api':'youtube-data-v3-official','url':'https://www.youtube.com/watch?v='+raw['id'],
                'last_api_check':proof['observed_at']}
            _,payload,actual=self.observed_identity(c,row,self.policy())
            if proof!=actual:raise ValueError()
            if job['job_type']==DOWNLOAD_JOB:
                parent=self.store.get(c['plan_job_id'])
                if (not parent or parent['state']!='completed' or parent['job_type']!=PLAN_JOB or
                        parent['source_id']!=c['source_id'] or parent['result'].get(PLANNING_SOURCE)!=proof):raise ValueError()
            return payload
        except (ValueError,KeyError,TypeError,AttributeError):fail('REPLAY_FIFO_PLANNING_SOURCE_CHANGED')

    def rebind_unstarted_candidates(self,*,expected_previous_policy_sha256,maintenance_reference,max_candidates=100):
        """Explicit policy maintenance for registered rows with no work begun.

        This never migrates a plan, lease, hold or source snapshot. Rows which
        have acquired any job/publication/hold are retained under their old
        policy for reconciliation. In particular it is safe to register first
        and later opt an entirely unstarted backlog into a new resource model.
        """
        p=self.policy();self.initialize()
        if (not re.fullmatch('[a-f0-9]{64}',expected_previous_policy_sha256 or '') or
                not isinstance(maintenance_reference,str) or not 0<len(maintenance_reference.strip())<=2048 or
                type(max_candidates) is not int or not 1<=max_candidates<=1000):fail('REPLAY_FIFO_REBIND_SCOPE_INVALID')
        result={'rebound':[],'retained':[],'old_policy_sha256':expected_previous_policy_sha256,
            'new_policy_sha256':p['_sha256'],'job_rows_changed':0,'publication_rows_changed':0,'source_rows_changed':0}
        with resource_guard(self.settings.root/'data/locks/replay-fifo-dispatch-v4'):
            with self.store.transaction() as db:
                if self.policy()['_sha256']!=p['_sha256']:fail('REPLAY_FIFO_POLICY_CHANGED')
                rows=db.execute('SELECT * FROM replay_fifo_v4 WHERE policy_sha256=? ORDER BY actual_end,source_id LIMIT ?',
                    (expected_previous_policy_sha256,max_candidates)).fetchall()
                for row in rows:
                    reason=None
                    if row['state']!='registered' or row['reason'] is not None or any(row[k] is not None for k in ('plan_job_id','download_job_id','publication_job_id','refresh_job_id')):
                        reason='CANDIDATE_ALREADY_STARTED_OR_HELD'
                    elif db.execute("SELECT 1 FROM jobs WHERE source_platform='youtube' AND source_id=? LIMIT 1",(row['source_id'],)).fetchone():
                        reason='SOURCE_ALREADY_HAS_JOB'
                    elif self._existing(db,row['source_id']):reason='SOURCE_ALREADY_RECORDED_OR_TOMBSTONED'
                    elif (db.execute('SELECT 1 FROM replay_fifo_capacity_v4 WHERE candidate_id=?',(row['candidate_id'],)).fetchone() or
                          db.execute('SELECT 1 FROM replay_fifo_resource_holds_v4 WHERE candidate_id=?',(row['candidate_id'],)).fetchone()):
                        reason='SOURCE_HAS_RESOURCE_HISTORY'
                    else:
                        official=db.execute('SELECT * FROM youtube_videos WHERE video_id=?',(row['source_id'],)).fetchone()
                        try:
                            identity,_=self._source(dict(official),p) if official else (None,None)
                            if (identity is None or sha(identity)!=row['identity_sha256'] or row['target_uid']!=p['target_site_uid'] or
                                    row['channel_id'] not in p['allowed_channel_ids']):reason='SOURCE_IDENTITY_CHANGED'
                        except HubError:reason='SOURCE_IDENTITY_CHANGED'
                    if reason:
                        result['retained'].append({'candidate_id':row['candidate_id'],'reason':reason});continue
                    if row['policy_sha256']==p['_sha256']:continue
                    changed=db.execute("""UPDATE replay_fifo_v4 SET policy_sha256=?,updated_at=? WHERE candidate_id=?
                        AND policy_sha256=? AND state='registered' AND plan_job_id IS NULL AND download_job_id IS NULL
                        AND publication_job_id IS NULL AND refresh_job_id IS NULL""",
                        (p['_sha256'],self.clock(),row['candidate_id'],expected_previous_policy_sha256)).rowcount
                    if changed!=1:fail('REPLAY_FIFO_REBIND_CAS_CHANGED')
                    db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                        (self.clock(),'operator:replay-fifo-v4','replay_fifo_unstarted_policy_rebind',row['candidate_id'],
                         encode({'old_policy_sha256':row['policy_sha256'],'new_policy_sha256':p['_sha256'],
                            'maintenance_reference':maintenance_reference,'source_identity_preserved':True,'registered_at_preserved':True,
                            'job_rows_changed':0,'publication_rows_changed':0,'human_review':False})))
                    result['rebound'].append(row['candidate_id'])
        return result

    def _fresh(self,candidate,p,*,require_fresh=True):
        rows=self.store.query('SELECT * FROM youtube_videos WHERE video_id=?',(candidate['source_id'],))
        if len(rows)!=1:fail('REPLAY_FIFO_FRESH_SOURCE_REQUIRED')
        self.observed_identity(candidate,rows[0],p)
        if require_fresh and not 0<=self.clock()-rows[0]['last_api_check']<=p['source_max_age_seconds']:fail('REPLAY_FIFO_FRESH_SOURCE_REQUIRED')
        return rows[0]

    def assert_job_binding(self,job,*,require_fresh=True):
        """Lease/source fence for the real V4 acquisition/processing adapter."""
        p=self.policy();frame=job.get('payload',{}).get('fifo_v4',{});c=self.candidate(frame.get('candidate_id'))
        current=self.store.get(job['job_id'])
        if (not current or current['source_platform']!='youtube' or current['queue']!='video' or
                current['job_type'] not in (PLAN_JOB,DOWNLOAD_JOB) or current['policy_version']!=VERSION or
                current['state']!='processing' or not job.get('lease_owner') or current['lease_owner']!=job['lease_owner'] or
                (current['lease_until'] or 0)<=self.clock() or current['attempts']!=job['attempts'] or
                not 1<=current['attempts']<=self.settings.config['max_attempts'] or current['payload']!=job['payload'] or
                current['source_id']!=c['source_id'] or frame.get('version')!=VERSION or
                frame.get('policy_sha256')!=p['_sha256'] or c['policy_sha256']!=p['_sha256'] or
                frame.get('identity_sha256')!=c['identity_sha256'] or
                job['job_id']!=(c['plan_job_id'] if current['job_type']==PLAN_JOB else c['download_job_id']) or
                any(current['payload'].get(k)!=v for k,v in
                    (c['source_payload'] if current['job_type']==PLAN_JOB else self.source_payload_for(c,current)).items())):
            fail('REPLAY_FIFO_JOB_BINDING_CHANGED')
        if type(require_fresh) is not bool:fail('REPLAY_FIFO_FRESHNESS_MODE_INVALID')
        self._fresh(c,p,require_fresh=require_fresh)
        with self.store.transaction() as db:
            if self._existing(db,c['source_id'],except_jobs=tuple(x for x in (c['plan_job_id'],c['download_job_id']) if x)):
                fail('REPLAY_FIFO_SOURCE_RECONCILIATION_REQUIRED')
        return {'candidate_id':c['candidate_id'],'payload_sha256':sha(current['payload']),
            'policy_sha256':p['_sha256'],'source_identity_sha256':c['identity_sha256']}

    def _insert_job(self,db,candidate,kind,p,payload):
        ident=str(uuid.uuid4());now=self.clock()
        db.execute("""INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,policy_version,payload)
            VALUES(?,'youtube',?,?,'video','pending',?,?,?,?)""",(ident,candidate['source_id'],kind,now,now,VERSION,encode(payload)))
        return ident

    def enqueue_plan_tick(self,registered_handlers):
        p=self.policy();self.initialize()
        for kind in (PLAN_JOB,REFRESH_JOB):
            if not callable(registered_handlers.get(kind)):return {'state':'waiting_handler','job_type':kind,'created':[]}
        with resource_guard(self.settings.root/'data/locks/replay-fifo-dispatch-v4'):
            # Exclude all linked/existing work before pagination; historic jobs
            # can never occupy the first20 slots and starve a new replay.
            marks=','.join('?' for _ in ASSET_JOBS)
            rows=self.store.query(f"""SELECT f.* FROM replay_fifo_v4 f WHERE f.state='registered'
                AND f.plan_job_id IS NULL AND f.policy_sha256=? AND NOT EXISTS(
                SELECT 1 FROM jobs j WHERE j.source_platform='youtube' AND j.source_id=f.source_id AND j.job_type IN ({marks})
                AND j.job_id NOT IN (SELECT job_id FROM replay_fifo_retired_jobs_v4))
                AND NOT EXISTS(SELECT 1 FROM publications p WHERE p.source_platform='youtube' AND p.source_id=f.source_id)
                ORDER BY f.actual_end,f.source_id LIMIT 1""",(p['_sha256'],*ASSET_JOBS))
            if not rows:return {'state':'idle','created':[]}
            c=self.candidate(rows[0]['candidate_id'])
            try:self._fresh(c,p)
            except HubError as exc:
                if exc.code=='REPLAY_FIFO_FRESH_SOURCE_REQUIRED':
                    refresh=self.request_refresh(c['candidate_id'])
                    return {'state':'waiting_source','candidate_id':c['candidate_id'],'reason':exc.code,'created':[],
                        'refresh_job_id':refresh,'daily_slots_spent':0}
                with self.store.transaction() as db:db.execute("UPDATE replay_fifo_v4 SET state='hold',reason=?,updated_at=? WHERE candidate_id=? AND plan_job_id IS NULL",
                    (exc.code,self.clock(),c['candidate_id']))
                return {'state':'hold','candidate_id':c['candidate_id'],'reason':exc.code,'created':[]}
            with self.store.transaction() as db:
                actual=db.execute('SELECT * FROM replay_fifo_v4 WHERE candidate_id=?',(c['candidate_id'],)).fetchone()
                if not actual or actual['plan_job_id'] or self._existing(db,c['source_id']):return {'state':'changed','created':[]}
                payload={**deepcopy(c['source_payload']),'fifo_v4':{'version':VERSION,'candidate_id':c['candidate_id'],
                    'policy_sha256':p['_sha256'],'identity_sha256':c['identity_sha256'],'source_metadata_sha256':c['source_metadata_sha256']}}
                ident=self._insert_job(db,c,PLAN_JOB,p,payload)
                db.execute("UPDATE replay_fifo_v4 SET state='plan_queued',plan_job_id=?,updated_at=?,reason=NULL WHERE candidate_id=?",(ident,self.clock(),c['candidate_id']))
            return {'state':'plan_queued','created':[ident],'daily_slots_spent':0}

    def request_refresh(self,candidate_id):
        """Durable maintenance action, not a new discovery or a source-time edit."""
        p=self.policy();c=self.candidate(candidate_id)
        with self.store.transaction() as db:
            row=db.execute('SELECT * FROM replay_fifo_v4 WHERE candidate_id=?',(candidate_id,)).fetchone()
            if not row or row['policy_sha256']!=p['_sha256']:fail('REPLAY_FIFO_POLICY_CHANGED')
            if row['refresh_job_id']:
                previous=db.execute('SELECT state FROM jobs WHERE job_id=?',(row['refresh_job_id'],)).fetchone()
                if not previous or previous['state']!='completed':return row['refresh_job_id']
            latest=db.execute('SELECT last_api_check FROM youtube_videos WHERE video_id=?',(c['source_id'],)).fetchone()
            if not latest:fail('REPLAY_FIFO_FRESH_SOURCE_REQUIRED')
            generation=sha({'candidate_id':candidate_id,'previous_official_observed_at':latest['last_api_check']})
            source='youtube-refresh:'+c['source_id']+':'+generation
            old=db.execute('SELECT job_id FROM jobs WHERE source_platform=? AND source_id=? AND job_type=? AND policy_version=?',
                ('maintenance',source,REFRESH_JOB,VERSION)).fetchone()
            ident=old['job_id'] if old else str(uuid.uuid4());now=self.clock()
            if not old:
                db.execute("""INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,policy_version,payload)
                    VALUES(?,'maintenance',?,?,'video','pending',?,?,?,?)""",(ident,source,REFRESH_JOB,now,now,VERSION,
                    encode({'candidate_id':candidate_id,'video_id':c['source_id'],'policy_sha256':p['_sha256'],'generation':generation})))
            db.execute("UPDATE replay_fifo_v4 SET refresh_job_id=?,state='waiting_source',reason='REPLAY_FIFO_FRESH_SOURCE_REQUIRED',updated_at=? WHERE candidate_id=?",(ident,now,candidate_id))
            return ident

    def process_refresh(self,job,http=None):
        """One official videos.list through the existing budgeted transport."""
        p=self.policy();c=self.candidate(job.get('payload',{}).get('candidate_id'))
        def check():
            current=self.store.get(job['job_id'])
            if (not current or current['job_type']!=REFRESH_JOB or current['source_platform']!='maintenance' or
                    current['policy_version']!=VERSION or current['state']!='processing' or not job.get('lease_owner') or
                    current['lease_owner']!=job['lease_owner'] or (current['lease_until'] or 0)<=self.clock() or
                    current['payload']!=job['payload'] or c['refresh_job_id']!=job['job_id'] or
                    job['payload']['video_id']!=c['source_id'] or job['payload']['policy_sha256']!=p['_sha256'] or
                    self.policy()['_sha256']!=p['_sha256']):fail('REPLAY_FIFO_REFRESH_LEASE_CHANGED')
            return current
        check();key=os.environ.get('YOUTUBE_API_KEY')
        if not key:fail('YOUTUBE_CREDENTIAL_REQUIRED')
        if http is None:
            from adapters.http import ProviderHTTP
            from .budget import Budget
            http=ProviderHTTP(Budget(self.store,self.settings.config['budgets']))
        value=http.request('youtube','GET','https://www.googleapis.com/youtube/v3/videos',
            params={'key':key,'part':'snippet,contentDetails,liveStreamingDetails,status','id':c['source_id']},job_id=job['job_id'])
        check();items=value.get('items') if isinstance(value,dict) else None
        if not isinstance(items,list) or len(items)>1:fail('REPLAY_FIFO_OFFICIAL_REFRESH_INVALID')
        if not items:
            with self.store.transaction() as db:
                db.execute("UPDATE youtube_videos SET state='unavailable',last_api_check=? WHERE video_id=?",(self.clock(),c['source_id']))
                db.execute("UPDATE replay_fifo_v4 SET state='hold',reason='YOUTUBE_SOURCE_UNAVAILABLE',updated_at=? WHERE candidate_id=?",(self.clock(),c['candidate_id']))
            return {'version':VERSION,'candidate_id':c['candidate_id'],'state':'hold','external_requests':1,'downloads':0}
        raw=items[0];rows=self.store.query('SELECT * FROM youtube_videos WHERE video_id=?',(c['source_id'],))
        if len(rows)!=1:fail('REPLAY_FIFO_FRESH_SOURCE_REQUIRED')
        row={**rows[0],'metadata':encode(raw),'state':'ended','source_api':'youtube-data-v3-official','last_api_check':self.clock()}
        try:
            row.update(title=raw['snippet']['title'],published_at=raw['snippet']['publishedAt'],
                actual_start=raw['liveStreamingDetails']['actualStartTime'],actual_end=raw['liveStreamingDetails']['actualEndTime'],duration=raw['contentDetails']['duration'])
        except (KeyError,TypeError):fail('REPLAY_FIFO_OFFICIAL_REFRESH_INVALID')
        self.observed_identity(c,row,p)
        check()
        with self.store.transaction() as db:
            current=db.execute('SELECT * FROM jobs WHERE job_id=?',(job['job_id'],)).fetchone()
            if not current or current['state']!='processing' or current['lease_owner']!=job['lease_owner'] or (current['lease_until'] or 0)<=self.clock():fail('REPLAY_FIFO_REFRESH_LEASE_CHANGED')
            db.execute("""UPDATE youtube_videos SET title=?,published_at=?,actual_start=?,actual_end=?,duration=?,metadata=?,
                last_api_check=?,last_seen_at=?,state='ended',source_api='youtube-data-v3-official' WHERE video_id=? AND channel_id=?""",
                (row['title'],row['published_at'],row['actual_start'],row['actual_end'],row['duration'],row['metadata'],self.clock(),self.clock(),c['source_id'],c['channel_id']))
            # Retain the original registration payload/title/rawSHA. This is a
            # new observation of the same stable identity, never new source time.
            target='plan_queued' if c['plan_job_id'] else 'registered'
            db.execute('UPDATE replay_fifo_v4 SET state=?,reason=NULL,updated_at=? WHERE candidate_id=?',(target,self.clock(),c['candidate_id']))
        return {'version':VERSION,'candidate_id':c['candidate_id'],'official_metadata_sha256':sha(raw),'state':'observed',
            'external_requests':1,'media_downloads':0,'publication_authorized':False}

    def record_plan(self,job,plan,planning_policy):
        """Called by a genuine metadata/inventory handler under its live lease."""
        from .replay_multipart_v4 import validate_whole_replay_plan
        p=self.policy();cid=job.get('payload',{}).get('fifo_v4',{}).get('candidate_id');c=self.candidate(cid)
        self.assert_job_binding(job)
        if sha(planning_policy)!=p['planning_policy_sha256']:fail('REPLAY_FIFO_PLANNING_POLICY_CHANGED')
        self._fresh(c,p)
        validate_whole_replay_plan(plan,planning_policy,official_raw=self.source_payload_for(c,self.store.get(job['job_id']))['raw'])
        with self.store.transaction() as db:
            row=db.execute('SELECT * FROM jobs WHERE job_id=?',(job['job_id'],)).fetchone()
            if (not row or row['job_type']!=PLAN_JOB or row['policy_version']!=VERSION or row['state']!='processing' or
                    row['lease_owner']!=job.get('lease_owner') or not job.get('lease_owner') or (row['lease_until'] or 0)<=self.clock() or
                    row['queue']!='video' or row['source_platform']!='youtube' or row['source_id']!=c['source_id'] or
                    not 1<=row['attempts']<=self.settings.config['max_attempts'] or row['attempts']!=job['attempts'] or
                    c['plan_job_id']!=job['job_id'] or c['policy_sha256']!=p['_sha256'] or
                    json.loads(row['payload'])!=job['payload'] or self._existing(db,c['source_id'],except_jobs=(job['job_id'],))):
                fail('REPLAY_FIFO_PLAN_LEASE_OR_BINDING_CHANGED')
            result=json.loads(row['result']);result['replay_fifo_plan_v4']={'version':VERSION,'candidate_id':cid,
                'state':'planned','plan':deepcopy(plan),'plan_sha256':sha(plan),'planning_policy_sha256':sha(planning_policy),
                'human_review':False,'publication_authorized':False}
            db.execute('UPDATE jobs SET result=?,stage=?,updated_at=? WHERE job_id=?',
                (encode(result),'replay_fifo_planned_v4',self.clock(),job['job_id']))
        job['result']=result;return result

    def enqueue_download_tick(self,registered_handlers,planning_policy,*,free_bytes=None):
        from .replay_multipart_v4 import validate_whole_replay_plan,evaluate_whole_resources
        p=self.policy();self.initialize()
        if not callable(registered_handlers.get(DOWNLOAD_JOB)):return {'state':'waiting_handler','created':[]}
        if sha(planning_policy)!=p['planning_policy_sha256']:fail('REPLAY_FIFO_PLANNING_POLICY_CHANGED')
        free=shutil.disk_usage(self.settings.root).free if free_bytes is None else free_bytes
        if type(free) is not int or free<0:fail('REPLAY_FIFO_DISK_OBSERVATION_INVALID')
        self.capacity_tick(planning_policy,free_bytes=free)
        with resource_guard(self.settings.root/'data/locks/replay-fifo-dispatch-v4'):
            deferred=[]
            while True:
                marks=','.join('?' for _ in ASSET_JOBS)
                ready=self.store.query(f"""SELECT f.candidate_id,j.result AS plan_result FROM replay_fifo_v4 f JOIN jobs j ON j.job_id=f.plan_job_id
                    WHERE f.state='plan_queued' AND f.download_job_id IS NULL AND f.policy_sha256=? AND j.state='completed'
                    AND j.job_type=? AND j.policy_version=? AND NOT EXISTS(SELECT 1 FROM publications p
                    WHERE p.source_platform='youtube' AND p.source_id=f.source_id) AND NOT EXISTS(SELECT 1 FROM jobs x
                    WHERE x.source_platform='youtube' AND x.source_id=f.source_id AND x.job_type IN ({marks}) AND x.job_id<>f.plan_job_id
                    AND x.job_id NOT IN (SELECT job_id FROM replay_fifo_retired_jobs_v4))
                    ORDER BY f.actual_end,f.source_id LIMIT 1""",(p['_sha256'],PLAN_JOB,VERSION,*ASSET_JOBS))
                if not ready:return {'state':'waiting_backlog' if deferred else 'idle','created':[],'deferred':deferred}
                c=self.candidate(ready[0]['candidate_id']);data=json.loads(ready[0]['plan_result'])['replay_fifo_plan_v4'];plan=data['plan']
                plan_job=self.store.get(c['plan_job_id']);source_payload=self.source_payload_for(c,plan_job)
                validate_whole_replay_plan(plan,planning_policy,official_raw=source_payload['raw'])
                if data['candidate_id']!=c['candidate_id'] or data['plan_sha256']!=sha(plan):fail('REPLAY_FIFO_PLAN_BINDING_CHANGED')
                resource=evaluate_whole_resources(plan,free)
                if resource['state']!='ready':
                    self._capacity_wait(c,plan,resource,p);deferred.append({'candidate_id':c['candidate_id'],'reason':'INSUFFICIENT_PEAK_DISK','required_free_bytes':resource['required_free_bytes']});continue
                try:self._fresh(c,p)
                except HubError as exc:
                    if exc.code=='REPLAY_FIFO_FRESH_SOURCE_REQUIRED':
                        deferred.append({'candidate_id':c['candidate_id'],'reason':exc.code,'refresh_job_id':self.request_refresh(c['candidate_id'])});continue
                    with self.store.transaction() as db:db.execute("UPDATE replay_fifo_v4 SET state='hold',reason=?,updated_at=? WHERE candidate_id=?",(exc.code,self.clock(),c['candidate_id']))
                    deferred.append({'candidate_id':c['candidate_id'],'reason':exc.code});continue
                break
            day=datetime.fromtimestamp(self.clock(),ZoneInfo(p['dispatch_timezone'])).date().isoformat()
            with self.store.transaction() as db:
                # Moderation and unrelated holds/unknown results are not a
                # global queue fence. Normal Worker/profile/heavy locks still
                # serialize heavy work; each source keeps its own one-use gate.
                if db.execute("SELECT 1 FROM jobs WHERE queue='video' AND state='processing' AND job_type IN (?,?) LIMIT 1",(DOWNLOAD_JOB,PUBLISH_JOB)).fetchone():return {'state':'heavy_worker_busy','created':[]}
                used=db.execute('SELECT COUNT(*) FROM replay_fifo_dispatches_v4 WHERE local_day=?',(day,)).fetchone()[0]
                if used>=p['max_download_dispatches_per_day']:return {'state':'daily_dispatch_limit','created':[],'registered_backlog_preserved':True}
                row=db.execute(f"""SELECT f.*,j.result AS plan_result FROM replay_fifo_v4 f JOIN jobs j ON j.job_id=f.plan_job_id
                    WHERE f.state='plan_queued' AND f.download_job_id IS NULL AND f.policy_sha256=? AND j.state='completed'
                    AND j.job_type=? AND j.policy_version=? AND NOT EXISTS(SELECT 1 FROM publications p WHERE p.source_platform='youtube'
                    AND p.source_id=f.source_id) AND NOT EXISTS(SELECT 1 FROM jobs x WHERE x.source_platform='youtube'
                    AND x.source_id=f.source_id AND x.job_type IN ({marks}) AND x.job_id<>f.plan_job_id
                    AND x.job_id NOT IN (SELECT job_id FROM replay_fifo_retired_jobs_v4))
                    ORDER BY f.actual_end,f.source_id LIMIT 1""",(p['_sha256'],PLAN_JOB,VERSION,*ASSET_JOBS)).fetchone()
                if not row:return {'state':'idle','created':[]}
                if row['candidate_id']!=c['candidate_id']:return {'state':'changed','created':[]}
                self._fresh(c,p)
                data=json.loads(row['plan_result'])['replay_fifo_plan_v4'];plan=data['plan']
                if data['candidate_id']!=c['candidate_id'] or data['plan_sha256']!=sha(plan):fail('REPLAY_FIFO_PLAN_BINDING_CHANGED')
                plan_job=self.store.get(c['plan_job_id']);source_payload=self.source_payload_for(c,plan_job)
                validate_whole_replay_plan(plan,planning_policy,official_raw=source_payload['raw'])
                if self._existing(db,c['source_id'],except_jobs=(c['plan_job_id'],)):return {'state':'existing_source_work','created':[]}
                payload={**source_payload,'whole_replay_plan':plan,'whole_replay_plan_sha256':sha(plan),
                    'fifo_v4':{'version':VERSION,'candidate_id':c['candidate_id'],
                    'policy_sha256':p['_sha256'],'identity_sha256':c['identity_sha256'],'plan_job_id':c['plan_job_id'],
                    'plan_sha256':sha(plan)}}
                if PLANNING_SOURCE in plan_job['result']:payload[PLANNING_SOURCE]=deepcopy(plan_job['result'][PLANNING_SOURCE])
                ident=self._insert_job(db,c,DOWNLOAD_JOB,p,payload)
                db.execute('INSERT INTO replay_fifo_dispatches_v4 VALUES(?,?,?,?,?,?)',
                    (str(uuid.uuid4()),c['candidate_id'],ident,day,self.clock(),p['_sha256']))
                db.execute("UPDATE replay_fifo_v4 SET state='download_queued',download_job_id=?,updated_at=? WHERE candidate_id=?",(ident,self.clock(),c['candidate_id']))
            return {'state':'download_queued','created':[ident],'daily_slots_spent':1,'deferred':deferred}

    def _capacity_wait(self,c,plan,resource,p):
        now=self.clock()
        with self.store.transaction() as db:
            old=db.execute('SELECT * FROM replay_fifo_capacity_v4 WHERE candidate_id=?',(c['candidate_id'],)).fetchone()
            if old and old['plan_sha256']!=sha(plan):fail('REPLAY_FIFO_RESOURCE_BINDING_CHANGED')
            deadline=old['deadline'] if old else now+p['resource_wait_max_seconds'];state='hold' if now>=deadline else 'waiting_resource'
            record={'version':VERSION,'candidate_id':c['candidate_id'],'whole_plan_sha256':sha(plan),
                'first_wait_at':old['first_wait_at'] if old else now,'deadline':deadline,'required_free_bytes':resource['required_free_bytes'],
                'state':state,'reason':'CAPACITY_WAIT_TIMEOUT' if state=='hold' else 'INSUFFICIENT_PEAK_DISK','no_job_dispatched':True}
            db.execute("""INSERT INTO replay_fifo_capacity_v4 VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(candidate_id)
                DO UPDATE SET state=excluded.state,next_check=excluded.next_check,record=excluded.record""",
                (c['candidate_id'],state,record['first_wait_at'],deadline,min(now+300,deadline),resource['required_free_bytes'],sha(plan),old['polls'] if old else 0,encode(record)))
            db.execute('UPDATE replay_fifo_v4 SET state=?,reason=?,updated_at=? WHERE candidate_id=?',(state,record['reason'],now,c['candidate_id']))

    def capacity_tick(self,planning_policy,*,free_bytes=None):
        from .replay_multipart_v4 import validate_whole_replay_plan,evaluate_whole_resources
        p=self.policy();self.initialize();now=self.clock()
        if sha(planning_policy)!=p['planning_policy_sha256']:fail('REPLAY_FIFO_PLANNING_POLICY_CHANGED')
        free=shutil.disk_usage(self.settings.root).free if free_bytes is None else free_bytes
        ready=[];held=[]
        for w in self.store.query("SELECT * FROM replay_fifo_capacity_v4 WHERE state='waiting_resource' AND next_check<=? ORDER BY first_wait_at",(now,)):
            c=self.candidate(w['candidate_id']);j=self.store.get(c['plan_job_id']);plan=j['result']['replay_fifo_plan_v4']['plan']
            validate_whole_replay_plan(plan,planning_policy,official_raw=self.source_payload_for(c,j)['raw'])
            if sha(plan)!=w['plan_sha256']:fail('REPLAY_FIFO_RESOURCE_BINDING_CHANGED')
            observation=evaluate_whole_resources(plan,free);state='hold' if now>=w['deadline'] else 'ready' if observation['state']=='ready' else 'waiting_resource'
            with self.store.transaction() as db:
                actual=db.execute('SELECT * FROM replay_fifo_capacity_v4 WHERE candidate_id=?',(c['candidate_id'],)).fetchone()
                if not actual or dict(actual)!=w:continue
                record=json.loads(w['record']);record.update(state=state,polls=w['polls']+1,last_checked_at=now,free_bytes=free)
                db.execute('UPDATE replay_fifo_capacity_v4 SET state=?,next_check=?,polls=?,record=? WHERE candidate_id=?',(state,min(now+300,w['deadline']),w['polls']+1,encode(record),c['candidate_id']))
                if state=='ready':
                    db.execute("UPDATE replay_fifo_v4 SET state='plan_queued',reason=NULL,updated_at=? WHERE candidate_id=? AND state='waiting_resource'",(now,c['candidate_id']));ready.append(c['candidate_id'])
                elif state=='hold':
                    db.execute("UPDATE replay_fifo_v4 SET state='hold',reason='CAPACITY_WAIT_TIMEOUT',updated_at=? WHERE candidate_id=?",(now,c['candidate_id']));held.append(c['candidate_id'])
        return {'ready':ready,'held':held,'external_requests':0,'slots_spent':0}

    def resume_capacity_hold(self,candidate_id,planning_policy,*,maintenance_reference,free_bytes=None):
        from .replay_multipart_v4 import validate_whole_replay_plan,evaluate_whole_resources
        p=self.policy();c=self.candidate(candidate_id)
        if sha(planning_policy)!=p['planning_policy_sha256'] or not isinstance(maintenance_reference,str) or not 0<len(maintenance_reference.strip())<=2048:fail('REPLAY_FIFO_MAINTENANCE_REFERENCE_REQUIRED')
        rows=self.store.query("SELECT * FROM replay_fifo_capacity_v4 WHERE candidate_id=? AND state='hold'",(candidate_id,))
        if len(rows)!=1 or c['state']!='hold' or c['download_job_id']:fail('REPLAY_FIFO_CAPACITY_HOLD_REQUIRED')
        wait=rows[0];job=self.store.get(c['plan_job_id'])
        if not job or job['state']!='completed':fail('REPLAY_FIFO_PLAN_BINDING_CHANGED')
        plan=job['result']['replay_fifo_plan_v4']['plan'];validate_whole_replay_plan(plan,planning_policy,official_raw=self.source_payload_for(c,job)['raw'])
        free=shutil.disk_usage(self.settings.root).free if free_bytes is None else free_bytes
        if sha(plan)!=wait['plan_sha256'] or evaluate_whole_resources(plan,free)['state']!='ready':fail('REPLAY_FIFO_RESOURCE_HOLD_NOT_READY')
        with self.store.transaction() as db:
            actual=db.execute('SELECT * FROM replay_fifo_capacity_v4 WHERE candidate_id=?',(candidate_id,)).fetchone()
            if not actual or dict(actual)!=wait or self._existing(db,c['source_id'],except_jobs=(c['plan_job_id'],)):fail('REPLAY_FIFO_RESOURCE_BINDING_CHANGED')
            record=json.loads(wait['record']);record.update(state='ready',resumed_at=self.clock(),maintenance_reference=maintenance_reference)
            db.execute("UPDATE replay_fifo_capacity_v4 SET state='ready',record=? WHERE candidate_id=?",(encode(record),candidate_id))
            db.execute("UPDATE replay_fifo_v4 SET state='plan_queued',reason=NULL,updated_at=? WHERE candidate_id=?",(self.clock(),candidate_id))
        return {'candidate_id':candidate_id,'state':'plan_queued','slots_spent':0,'source_and_plan_preserved':True}

    def publication_tick(self,service):
        """Call the actual V4 machine coordinator; never manufacture its binding."""
        p=self.policy();self.initialize()
        if getattr(service,'JOB_TYPE',None)!=PUBLISH_JOB:fail('REPLAY_FIFO_MULTIPART_COORDINATOR_REQUIRED')
        with resource_guard(self.settings.root/'data/locks/replay-fifo-publication-v4'):
            rows=self.store.query("""SELECT f.* FROM replay_fifo_v4 f JOIN jobs d ON d.job_id=f.download_job_id
                WHERE f.policy_sha256=? AND f.state IN ('download_queued','download_completed') AND d.job_type=? AND d.policy_version=? AND d.state='completed'
                AND f.publication_job_id IS NULL AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.source_platform='youtube'
                AND j.source_id=f.source_id AND j.job_type=?) AND NOT EXISTS(SELECT 1 FROM publications p
                WHERE p.source_platform='youtube' AND p.source_id=f.source_id)
                ORDER BY f.actual_end,f.source_id LIMIT 1""",(p['_sha256'],DOWNLOAD_JOB,VERSION,PUBLISH_JOB))
            if not rows:return {'created':[]}
            c=self.candidate(rows[0]['candidate_id']);ident=service.queue_verified_download(c['download_job_id'])
            job=self.store.get(ident)
            if (not job or job['source_platform']!='youtube' or job['source_id']!=c['source_id'] or job['job_type']!=PUBLISH_JOB):fail('REPLAY_FIFO_PUBLICATION_BINDING_CHANGED')
            with self.store.transaction() as db:db.execute("UPDATE replay_fifo_v4 SET publication_job_id=?,state='publication_queued',updated_at=? WHERE candidate_id=? AND publication_job_id IS NULL",(ident,self.clock(),c['candidate_id']))
            return {'created':[ident],'actual_publications':0}

    def sync_states(self,*,publication_projection=None):
        """Mirror real jobs/publications, without changing any of their rows.

        This also recovers the narrow crash window after a real coordinator
        creates its idempotent publication job but before FIFO links it.
        """
        self.policy();self.initialize();changed=[]
        with self.store.transaction() as db:
            for row in db.execute("SELECT * FROM replay_fifo_v4 WHERE state NOT IN ('excluded','existing','completed')").fetchall():
                state,reason,publication_id=row['state'],row['reason'],row['publication_job_id']
                if publication_projection is not None:
                    # An explicitly registered successor may have one durable
                    # parent plus independent ordinary publications. The old
                    # default remains exactly the single-publication rule.
                    projection=publication_projection(db,dict(row))
                    if projection is not None:
                        state,reason,publication_id=projection
                        if state not in ('completed','unknown','awaiting_moderation','publication_queued','hold') or not isinstance(publication_id,str):
                            fail('REPLAY_FIFO_PUBLICATION_PROJECTION_INVALID')
                        if (state,reason,publication_id)!=(row['state'],row['reason'],row['publication_job_id']):
                            db.execute('UPDATE replay_fifo_v4 SET state=?,reason=?,publication_job_id=?,updated_at=? WHERE candidate_id=?',
                                (state,reason,publication_id,self.clock(),row['candidate_id']));changed.append(row['candidate_id'])
                        continue
                if not publication_id:
                    found=db.execute("SELECT job_id FROM jobs WHERE source_platform='youtube' AND source_id=? AND job_type=?",(row['source_id'],PUBLISH_JOB)).fetchall()
                    if len(found)==1:publication_id=found[0]['job_id']
                    elif len(found)>1:state,reason='hold','REPLAY_FIFO_MULTIPLE_PUBLICATION_JOBS'
                pubs=db.execute("SELECT * FROM publications WHERE source_platform='youtube' AND source_id=?",(row['source_id'],)).fetchall()
                if pubs:
                    own=[p for p in pubs if p['job_id']==publication_id and p['account_id']=='site_uid:'+row['target_uid']]
                    if len(pubs)!=1 or len(own)!=1:state,reason='unknown','REPLAY_FIFO_PUBLICATION_RECONCILIATION_REQUIRED'
                    else:
                        pub=own[0];parent=db.execute('SELECT state,external_publish_id FROM jobs WHERE job_id=?',(publication_id,)).fetchone()
                        if (pub['state']=='completed' and parent and parent['state']=='completed' and
                                parent['external_publish_id']==pub['external_id'] and re.fullmatch('BV[A-Za-z0-9]{10}',pub['external_id'] or '') and
                                pub['external_url']=='https://www.bilibili.com/video/'+pub['external_id']):state,reason='completed',None
                        elif pub['state']=='awaiting_moderation':state,reason='awaiting_moderation',None
                        elif pub['state'] in ('dispatching','unknown'):state,reason='unknown','RECONCILE_BEFORE_RETRY'
                        else:state,reason='hold','REPLAY_FIFO_PUBLICATION_'+re.sub('[^A-Z_]','',pub['state'].upper())[:50]
                elif state not in ('waiting_resource','hold'):
                    owned=tuple(x for x in (row['plan_job_id'],row['download_job_id'],publication_id) if x)
                    external=self._existing(db,row['source_id'],except_jobs=owned)
                    if external:
                        db.execute("UPDATE replay_fifo_v4 SET state='existing',reason=?,updated_at=? WHERE candidate_id=?",(external,self.clock(),row['candidate_id']))
                        changed.append(row['candidate_id']);continue
                    for key,done in (('publication_job_id','publication_queued'),('download_job_id','download_completed'),('plan_job_id','plan_queued'),('refresh_job_id','registered')):
                        ident=publication_id if key=='publication_job_id' else row[key]
                        if not ident:continue
                        job=db.execute('SELECT state,error_code FROM jobs WHERE job_id=?',(ident,)).fetchone()
                        if not job:state,reason='hold','REPLAY_FIFO_LINKED_JOB_MISSING';break
                        if job['state'] in ('failed','auth_expired','awaiting_review'):
                            state,reason='hold',job['error_code'] or 'REPLAY_FIFO_LINKED_JOB_STOPPED';break
                        if job['state']=='publish_unknown':state,reason='unknown','RECONCILE_BEFORE_RETRY';break
                        if job['state']=='completed':state,reason=done,None
                        break
                if (state,reason,publication_id)!=(row['state'],row['reason'],row['publication_job_id']):
                    db.execute('UPDATE replay_fifo_v4 SET state=?,reason=?,publication_job_id=?,updated_at=? WHERE candidate_id=?',
                        (state,reason,publication_id,self.clock(),row['candidate_id']));changed.append(row['candidate_id'])
        return {'changed':changed,'job_rows_changed':0,'publication_rows_changed':0,'attempts_reset':False}

    def resource_defer(self,job,*,required_free_bytes,binding_sha256):
        """V4 wait accepts whole-replay budgets beyond the old64GB exception cap."""
        p=self.policy();self.initialize();now=self.clock()
        if type(required_free_bytes) is not int or not 16_106_127_360<=required_free_bytes<2**63 or not re.fullmatch('[a-f0-9]{64}',binding_sha256):fail('REPLAY_FIFO_RESOURCE_REQUEST_INVALID')
        cid=job.get('payload',{}).get('fifo_v4',{}).get('candidate_id');c=self.candidate(cid)
        with self.store.transaction() as db:
            row=db.execute('SELECT * FROM jobs WHERE job_id=?',(job['job_id'],)).fetchone()
            if (not row or row['job_type'] not in (PLAN_JOB,DOWNLOAD_JOB) or row['policy_version']!=VERSION or
                    row['state']!='processing' or not job.get('lease_owner') or row['lease_owner']!=job['lease_owner'] or
                    (row['lease_until'] or 0)<=now or sha(json.loads(row['payload']))!=sha(job['payload']) or
                    job['job_id'] not in (c['plan_job_id'],c['download_job_id']) or
                    self._existing(db,c['source_id'],except_jobs=tuple(x for x in (c['plan_job_id'],c['download_job_id']) if x))):fail('REPLAY_FIFO_RESOURCE_BINDING_CHANGED')
            old=db.execute('SELECT * FROM replay_fifo_resource_holds_v4 WHERE job_id=?',(job['job_id'],)).fetchone()
            if old and (old['binding_sha256']!=binding_sha256 or old['payload_sha256']!=sha(job['payload'])):fail('REPLAY_FIFO_RESOURCE_BINDING_CHANGED')
            deadline=old['deadline'] if old else now+p['resource_wait_max_seconds'];hold=deadline<=now
            record={'version':VERSION,'candidate_id':cid,'job_id':job['job_id'],'binding_sha256':binding_sha256,
                'required_free_bytes':required_free_bytes,'first_wait_at':old['first_wait_at'] if old else now,'deadline':deadline,
                'state':'hold' if hold else 'waiting_resource','attempts_preserved':row['attempts'],'human_review':False}
            result=json.loads(row['result']);result['replay_fifo_resource_v4']=record
            db.execute("""INSERT INTO replay_fifo_resource_holds_v4 VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(job_id)
                DO UPDATE SET state=excluded.state,required_free_bytes=excluded.required_free_bytes,record=excluded.record""",
                (job['job_id'],cid,record['state'],record['first_wait_at'],deadline,min(now+300,deadline),required_free_bytes,binding_sha256,sha(job['payload']),old['polls'] if old else 0,encode(record)))
            db.execute("UPDATE jobs SET state=?,error_code=?,stage=?,result=?,next_run=?,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE job_id=?",
                ('upload_pending' if hold else 'retry_scheduled','REPLAY_FIFO_RESOURCE_HOLD' if hold else 'WAITING_RESOURCE',
                'replay_fifo_resource_hold' if hold else 'waiting_resource',encode(result),deadline+60,now,job['job_id']))
            db.execute('UPDATE replay_fifo_v4 SET state=?,reason=?,updated_at=? WHERE candidate_id=?',(record['state'],None if not hold else 'RESOURCE_WAIT_TIMEOUT',now,cid))
        return record

    def resource_callbacks(self):
        """Explicit opt-in for the ordinary Worker's V4-only deferral path."""
        def defer(job,error):
            return self.resource_defer(job,required_free_bytes=error.required_bytes,binding_sha256=error.binding_sha256)
        return {PLAN_JOB:defer,DOWNLOAD_JOB:defer}

    def resource_tick(self,validators,*,free_bytes=None):
        p=self.policy();self.initialize();now=self.clock();free=shutil.disk_usage(self.settings.root).free if free_bytes is None else free_bytes
        if type(free) is not int or free<0:fail('REPLAY_FIFO_DISK_OBSERVATION_INVALID')
        resumed=[];held=[]
        for wait in self.store.query("SELECT * FROM replay_fifo_resource_holds_v4 WHERE state='waiting_resource' AND next_check<=? ORDER BY first_wait_at LIMIT 32",(now,)):
            job=self.store.get(wait['job_id']);reason=None
            try:
                if (not job or job['state']!='retry_scheduled' or job['error_code']!='WAITING_RESOURCE' or job['lease_owner'] is not None or
                        job['lease_until'] is not None or sha(job['payload'])!=wait['payload_sha256'] or job['policy_version']!=VERSION):fail('REPLAY_FIFO_RESOURCE_JOB_CHANGED')
                check=validators.get(job['job_type'])
                if not callable(check) or check(job)!=wait['binding_sha256']:fail('REPLAY_FIFO_RESOURCE_BINDING_CHANGED')
                if now>=wait['deadline']:reason='RESOURCE_WAIT_TIMEOUT'
            except HubError as exc:reason=exc.code
            except Exception:reason='REPLAY_FIFO_RESOURCE_BINDING_CHANGED'
            with self.store.transaction() as db:
                current=db.execute('SELECT * FROM jobs WHERE job_id=?',(wait['job_id'],)).fetchone()
                actual=db.execute('SELECT * FROM replay_fifo_resource_holds_v4 WHERE job_id=?',(wait['job_id'],)).fetchone()
                if not actual or dict(actual)!=wait or not current:continue
                if current['state']!='retry_scheduled' or current['lease_owner'] is not None or current['lease_until'] is not None:continue
                c=self.candidate(wait['candidate_id'])
                if c['policy_sha256']!=p['_sha256']:reason='REPLAY_FIFO_POLICY_CHANGED'
                if self._existing(db,c['source_id'],except_jobs=tuple(x for x in (c['plan_job_id'],c['download_job_id']) if x)):reason='RECONCILE_BEFORE_RESOURCE_RESUME'
                result=json.loads(current['result']);record=json.loads(wait['record']);record.update(last_checked_at=now,free_bytes=free,polls=wait['polls']+1)
                if reason:
                    record.update(state='hold',reason=reason);state='hold';held.append(wait['job_id'])
                    db.execute("UPDATE jobs SET state='upload_pending',error_code='REPLAY_FIFO_RESOURCE_HOLD',stage='replay_fifo_resource_hold',result=?,updated_at=? WHERE job_id=?",(encode({**result,'replay_fifo_resource_v4':record}),now,wait['job_id']))
                    db.execute("UPDATE replay_fifo_v4 SET state='hold',reason=?,updated_at=? WHERE candidate_id=?",(reason,now,wait['candidate_id']))
                elif free>=wait['required_free_bytes'] and current['attempts']<self.settings.config['max_attempts']:
                    record.update(state='ready',resumed_at=now);state='ready';resumed.append(wait['job_id'])
                    db.execute("UPDATE jobs SET error_code=NULL,stage='replay_fifo_resource_ready',next_run=?,result=?,updated_at=? WHERE job_id=?",(now,encode({**result,'replay_fifo_resource_v4':record}),now,wait['job_id']))
                    db.execute('UPDATE replay_fifo_v4 SET state=?,reason=NULL,updated_at=? WHERE candidate_id=?',('download_queued' if current['job_type']==DOWNLOAD_JOB else 'plan_queued',now,wait['candidate_id']))
                else:state='waiting_resource'
                db.execute('UPDATE replay_fifo_resource_holds_v4 SET state=?,next_check=?,polls=?,record=? WHERE job_id=?',(state,min(now+300,wait['deadline']),wait['polls']+1,encode(record),wait['job_id']))
        return {'resumed':resumed,'held':held,'external_requests':0,'attempts_reset':False}

    def resume_resource_hold(self,job_id,validators,*,maintenance_reference,free_bytes=None):
        """Recover the same held job after space is actually available.

        This is a technical maintenance reference, not a new content approval.
        The original first wait/deadline/polls/attempts and partials stay intact.
        """
        p=self.policy();self.initialize()
        if not isinstance(maintenance_reference,str) or not 0<len(maintenance_reference.strip())<=2048:fail('REPLAY_FIFO_MAINTENANCE_REFERENCE_REQUIRED')
        rows=self.store.query("SELECT * FROM replay_fifo_resource_holds_v4 WHERE job_id=? AND state='hold'",(job_id,))
        if len(rows)!=1:fail('REPLAY_FIFO_RESOURCE_HOLD_REQUIRED')
        wait=rows[0];job=self.store.get(job_id);c=self.candidate(wait['candidate_id'])
        free=shutil.disk_usage(self.settings.root).free if free_bytes is None else free_bytes
        check=validators.get(job['job_type']) if job else None
        if (c['policy_sha256']!=p['_sha256'] or not job or job['state']!='upload_pending' or job['error_code']!='REPLAY_FIFO_RESOURCE_HOLD' or
                job['lease_owner'] is not None or job['lease_until'] is not None or job['attempts']>=self.settings.config['max_attempts'] or
                sha(job['payload'])!=wait['payload_sha256'] or not callable(check) or check(job)!=wait['binding_sha256'] or
                type(free) is not int or free<wait['required_free_bytes']):fail('REPLAY_FIFO_RESOURCE_HOLD_NOT_READY')
        with self.store.transaction() as db:
            row=db.execute('SELECT * FROM jobs WHERE job_id=?',(job_id,)).fetchone()
            actual=db.execute('SELECT * FROM replay_fifo_resource_holds_v4 WHERE job_id=?',(job_id,)).fetchone()
            if (not row or not actual or dict(actual)!=wait or parsed_job(row)!=job or self._existing(db,c['source_id'],
                    except_jobs=tuple(x for x in (c['plan_job_id'],c['download_job_id']) if x))):fail('REPLAY_FIFO_RESOURCE_BINDING_CHANGED')
            record=json.loads(wait['record']);record.update(state='ready',resumed_at=self.clock(),maintenance_reference=maintenance_reference)
            result=json.loads(row['result']);result['replay_fifo_resource_v4']=record
            db.execute("UPDATE jobs SET state='retry_scheduled',error_code=NULL,stage='replay_fifo_resource_hold_released',next_run=?,result=?,updated_at=? WHERE job_id=?",(self.clock(),encode(result),self.clock(),job_id))
            db.execute("UPDATE replay_fifo_resource_holds_v4 SET state='ready',record=? WHERE job_id=?",(encode(record),job_id))
            db.execute('UPDATE replay_fifo_v4 SET state=?,reason=NULL,updated_at=? WHERE candidate_id=?',('download_queued' if job['job_type']==DOWNLOAD_JOB else 'plan_queued',self.clock(),c['candidate_id']))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',(self.clock(),'system:replay-fifo-v4',
                'replay_fifo_resource_hold_released',job_id,encode({'maintenance_reference':maintenance_reference,'attempts_preserved':job['attempts']})))
        return {'job_id':job_id,'state':'retry_scheduled','attempts_reset':False,'external_requests':0}
