"""Explicitly scoped acquisition and independent ordinary part publication.

Register-only is still the default. Publishing mode registers exactly the
known acquisition types and 32 positive ordinal types, never old video jobs.
Lookup children use the separate moderation lock and their own heartbeat.
"""
import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import uuid

from .errors import HubError
from .job_scope import job_type_filter
from .replay_fifo_v4 import PLAN_JOB,DOWNLOAD_JOB,REFRESH_JOB,VERSION as FIFO_POLICY
from .replay_parts_v5 import ReplayPartVideoWeb,ReplayPartGroupsV5,PART_JOB_TYPES,POLICY
from .replay_multipart_worker_runtime_v4 import Coordinator as AcquisitionCoordinator,QueueView as AcquisitionQueue,run_loop
from .replay_part_publish_worker_v5 import PartReplayWorkerHandler
from .worker import Worker

VERSION='replay-parts-worker-runtime-v5'


def fatal_diagnostic(role,error):
    """Small stderr record independent of the application's logging format."""
    try:
        kind=type(error).__name__
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}',kind):kind='Exception'
        code=error.code if isinstance(error,HubError) else None
        if code is not None and (not isinstance(code,str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}',code)):
            code='HUB_ERROR_CODE_REDACTED'
        value={'event':'replay_part_background_fatal_v5','role':role if role in ('moderation','retention') else 'unknown',
               'exception_type':kind,'code':code}
        print(json.dumps(value,separators=(',',':')),file=sys.stderr,flush=True)
    except BaseException:
        # A failed diagnostic must never prevent the original fatal exit.
        pass


class QueueView(AcquisitionQueue):
    def __init__(self,store,*,publish_enabled=False,acquisition_enabled=False,publication_only=False):
        super().__init__(store,acquisition_enabled=acquisition_enabled)
        if type(publish_enabled) is not bool:raise ValueError('REPLAY_PART_QUEUE_MODE_INVALID')
        self.publish_enabled=publish_enabled
        if type(publication_only) is not bool or publication_only and not publish_enabled:
            raise ValueError('REPLAY_PART_QUEUE_MODE_INVALID')
        self.publication_only=publication_only

    def _scope(self):
        original,args=super()._scope()
        if not self.publish_enabled:return original,args
        marks=','.join('?' for _ in PART_JOB_TYPES)
        if self.publication_only:
            return "source_platform='youtube' AND policy_version=? AND job_type IN ("+marks+')',(POLICY,*PART_JOB_TYPES)
        return '('+original+" OR (source_platform='youtube' AND policy_version=? AND job_type IN ("+marks+')))',(*args,POLICY,*PART_JOB_TYPES)

    def claim(self,queue,lease_seconds,max_attempts,*,job_types=None):
        self._queue(queue)
        if not self.acquisition_enabled:return None
        scope,args=self._scope();now=time.time();owner=str(uuid.uuid4())
        types_sql,types=job_type_filter(job_types)
        scope,args=f'({scope}) AND {types_sql}',(*args,*types)
        with self.store.transaction() as db:
            if db.execute("SELECT paused FROM pipelines WHERE name='video'").fetchone()['paused']:return None
            marks=','.join('?' for _ in PART_JOB_TYPES)
            tables=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='replay_part_children_v5'").fetchone()
            ordinal_guard=(" AND (policy_version<>? OR job_type NOT IN ("+marks+") OR EXISTS(SELECT 1 FROM replay_part_children_v5 c "
                "JOIN replay_part_groups_v5 g ON g.parent_job_id=c.parent_job_id WHERE c.child_job_id=jobs.job_id AND g.source_id=jobs.source_id "
                "AND c.part_index=json_extract(jobs.payload,'$.part_index') AND c.idempotency_key=json_extract(jobs.payload,'$.part_idempotency_key') "
                "AND g.target_uid=json_extract(jobs.payload,'$.site_uid') AND NOT EXISTS(SELECT 1 FROM replay_part_children_v5 previous "
                "WHERE previous.parent_job_id=c.parent_job_id AND previous.part_index<c.part_index AND (previous.state<>'completed' OR previous.external_id IS NULL "
                "OR NOT EXISTS(SELECT 1 FROM publications p JOIN jobs j ON j.job_id=p.job_id WHERE p.job_id=previous.child_job_id "
                "AND p.idempotency_key=previous.idempotency_key AND p.state='completed' AND j.state='completed' AND p.external_id=previous.external_id "
                "AND p.kind='video_website_replay_part_v5_p'||printf('%02d',previous.part_index+1))))))") if tables else " AND (policy_version<>? OR job_type NOT IN ("+marks+"))"
            row=db.execute("SELECT job_id FROM jobs WHERE queue='video' AND "+scope+" AND state IN ('pending','retry_scheduled') AND next_run<=? AND attempts<? "+ordinal_guard+
                "ORDER BY CASE WHEN policy_version=? AND job_type IN ("+marks+") THEN 0 ELSE 1 END,created_at,job_type,job_id LIMIT 1",
                (*args,now,max_attempts,POLICY,*PART_JOB_TYPES,POLICY,*PART_JOB_TYPES)).fetchone()
            if not row:return None
            db.execute("UPDATE jobs SET state='processing',attempts=attempts+1,lease_owner=?,lease_until=?,updated_at=? WHERE job_id=?",
                (owner,now+lease_seconds,now,row['job_id']))
        return self.store.get(row['job_id'])

    def beat(self,name):
        super().beat(name);self.store.beat('worker:video-main-v5')


class Coordinator:
    def __init__(self,settings,store,*,mode='register_only'):
        if mode not in ('register_only','acquisition_only','publish','publication_only'):raise ValueError('REPLAY_PART_RUNTIME_MODE_INVALID')
        self.settings,self.store,self.mode=settings,store,mode
        self.base=AcquisitionCoordinator(settings,store,mode='register_only' if mode=='register_only' else 'acquisition_only')
        self.fifo=self.base.fifo;self.groups=ReplayPartGroupsV5(settings,store);self._publish_policy_sha=None
        handlers={} if mode=='publication_only' else dict(self.base.worker.handlers)
        if mode in ('publish','publication_only'):
            self.groups.initialize();self.require_publish_policy()
            for index,name in enumerate(PART_JOB_TYPES):handlers[name]=PartReplayWorkerHandler(settings,store,part_index=index).process
        self.worker=Worker(settings,QueueView(store,publish_enabled=mode in ('publish','publication_only'),acquisition_enabled=mode!='register_only',publication_only=mode=='publication_only'),handlers,
            resource_deferrals=self.base.worker.resource_deferrals,readiness_deferrals=self.base.worker.readiness_deferrals)

    def require_publish_policy(self):
        service=ReplayPartVideoWeb(self.settings,self.store);policy=service.policy()
        self.groups.require_launch(policy)
        if self._publish_policy_sha is not None and self._publish_policy_sha!=policy['_sha256']:raise HubError('REPLAY_PART_RUNTIME_POLICY_CHANGED',review=True)
        self._publish_policy_sha=policy['_sha256']
        planning=self.base.require_policy()
        if planning.get('prefer_single_whole_passthrough_v5') is not True:raise HubError('REPLAY_PART_DELIVERY_POLICY_REQUIRED',review=True)
        return policy

    def tick(self):
        from .locks import resource_guard
        planning=self.base.require_policy();self.groups.initialize()
        if self.mode in ('publish','publication_only'):self.require_publish_policy()
        with resource_guard(self.settings.root/'data/locks/replay-multipart-producer-v4'):
            steps={'groups':self.groups.sync_completed_groups(),'sync':self.fifo.sync_states(publication_projection=self.groups.fifo_projection),
                'register':self.fifo.register_all_observed()}
            paused=self.store.query("SELECT paused FROM pipelines WHERE name='video'")[0]['paused']
            if not paused and self.mode!='register_only':
                if self.mode in ('publish','publication_only'):
                    from .replay_part_moderation_v5 import schedule_tick
                    steps['lookups']=schedule_tick(self.settings,self.store)
                    steps['publication']=self.groups.publication_tick(self.fifo)
                if self.mode!='publication_only':
                    steps['readiness']=self.base.readiness.tick()
                    steps['resources']=self.fifo.resource_tick(self.base.validators)
                    steps['plan']=self.fifo.enqueue_plan_tick(self.worker.handlers)
                    steps['download']=self.fifo.enqueue_download_tick(self.worker.handlers,planning)
            self.store.beat('worker:video-producer-v4');self.store.beat('worker:video-producer-v5')
            if self.mode=='register_only':self.worker.store.beat('worker:video')
            return {'version':VERSION,'mode':self.mode,'publisher_registered':self.mode in ('publish','publication_only'),'steps':steps,'external_requests':0}

    def retention_tick(self):
        # This runs outside the producer thread: a current whole-file SHA may
        # take minutes and must not stop registration or its real heartbeat.
        if self.mode not in ('publish','publication_only'):return {'state':'disabled','files_deleted':0,'external_requests':0}
        if self.store.query("SELECT paused FROM pipelines WHERE name='video'")[0]['paused']:
            return {'state':'paused','files_deleted':0,'external_requests':0}
        from .replay_parts_retention_v5 import retention_tick
        value=retention_tick(self.settings,self.store)
        self.store.beat('worker:video-retention-v5')
        return value


def build_worker(settings,store,*,mode='register_only'):
    coordinator=Coordinator(settings,store,mode=mode)
    return coordinator.worker,coordinator


def moderation_loop(settings,store,stop,*,once=False,fatal=None):
    from .replay_part_moderation_v5 import build_lookup_worker,schedule_tick
    try:
        worker=build_lookup_worker(settings,store)
        while not stop.is_set():
            try:
                try:schedule_tick(settings,store)
                except HubError as exc:
                    # The producer also schedules lookups. Its acquired schedule
                    # lock is ordinary contention, before any lookup is claimed.
                    if exc.code!='HEAVY_RESOURCE_BUSY' or not exc.retryable or exc.review:raise
                    if once:return False
                    stop.wait(1)
                    continue
                worked=worker.once('moderation')
            except sqlite3.OperationalError:
                # A transient database lock is contention, not a reason to kill
                # the acquisition process that shares this database.
                logging.getLogger(__name__).warning('REPLAY_PART_MODERATION_DB_BUSY')
                if once:return False
                stop.wait(5)
                continue
            if once:return worked
            if not worked:stop.wait(1)
    except BaseException as error:
        if fatal is None:raise
        fatal_diagnostic('moderation',error)
        logging.getLogger(__name__).critical('REPLAY_PART_MODERATION_STOPPED');stop.set();fatal(70)


def retention_loop(coordinator,stop,*,once=False,fatal=None):
    """One release-gated, local-only group per round; no browser callback."""
    try:
        while not stop.is_set():
            try:
                try:value=coordinator.retention_tick()
                except HubError as exc:
                    # Invalid/changed releases and busy locks retain every file.
                    # They do not prevent independent source registration.
                    coordinator.store.event('review.required',{'code':exc.code,'cleanup_only':True},dedup_key=VERSION+':retention:'+exc.code)
                    value={'state':'held','code':exc.code,'files_deleted':0,'external_requests':0}
            except sqlite3.OperationalError:
                logging.getLogger(__name__).warning('REPLAY_PART_RETENTION_DB_BUSY')
                value={'state':'held','code':'DB_BUSY','files_deleted':0,'external_requests':0}
            if once:return value
            stop.wait(30)
    except BaseException as error:
        if fatal is None:raise
        fatal_diagnostic('retention',error)
        logging.getLogger(__name__).critical('REPLAY_PART_RETENTION_STOPPED');stop.set();fatal(70)


def main():
    from .config import Settings
    from .db import Store
    parser=argparse.ArgumentParser();parser.add_argument('--once',action='store_true');parser.add_argument('--check',action='store_true')
    parser.add_argument('--role',choices=('main','moderation'),default='main')
    modes=parser.add_mutually_exclusive_group();modes.add_argument('--register-only',action='store_true');modes.add_argument('--acquisition-only',action='store_true');modes.add_argument('--publish',action='store_true')
    modes.add_argument('--publication-only',action='store_true')
    args=parser.parse_args()
    if os.name!='posix' or os.getuid()!=10001:raise HubError('REPLAY_PART_APP_UID_REQUIRED',review=True)
    logging.basicConfig(level=logging.WARNING,format='%(levelname)s %(name)s')
    s=Settings.load();s.prepare();store=Store(s.db_path)
    mode='publication_only' if args.publication_only else 'publish' if args.publish else 'acquisition_only' if args.acquisition_only else 'register_only'
    if args.role=='moderation':
        # Historical lookup neither requires nor grants current sending
        # permission. It needs no acquisition policy or YouTube API key.
        if args.check:
            from .replay_part_moderation_v5 import LOOKUP_JOB_TYPE
            print(json.dumps({'version':VERSION,'role':'moderation','handlers':[LOOKUP_JOB_TYPE],'sending_authorized':False}));return
        moderation_loop(s,store,threading.Event(),once=args.once)
        return
    worker,coordinator=build_worker(s,store,mode=mode)
    if args.check:
        print(json.dumps({'version':VERSION,'mode':mode,'role':args.role,'handlers':sorted(worker.handlers),'external_requests':0}));return
    if mode not in ('register_only','publication_only'):
        from .youtube_credentials import load_youtube_key
        if os.environ.get('YOUTUBE_API_KEY_FILE')!='/run/secrets/youtube-api-key':raise HubError('REPLAY_PART_KEY_FILE_REQUIRED',review=True)
        load_youtube_key('/run/secrets/youtube-api-key')
    stop=threading.Event();moderator=None;retention=None
    if mode in ('publish','publication_only') and not args.once:
        moderator=threading.Thread(target=moderation_loop,args=(s,store,stop),kwargs={'fatal':os._exit},name='replay-part-moderation-v5',daemon=True)
        moderator.start()
        retention=threading.Thread(target=retention_loop,args=(coordinator,stop),kwargs={'fatal':os._exit},name='replay-part-retention-v5',daemon=True)
        retention.start()
    try:
        run_loop(worker,coordinator,once=args.once,stop=stop)
        if args.once and mode in ('publish','publication_only'):retention_loop(coordinator,stop,once=True)
    finally:
        stop.set()
        if moderator is not None:moderator.join(timeout=5)
        if retention is not None:retention.join(timeout=5)


if __name__=='__main__':main()
