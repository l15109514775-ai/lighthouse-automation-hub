"""Explicit replacement Worker entry for the separate highest-quality pilot.

Install only after stopping the old video consumer. No automatic admission,
workflow grant, key creation, history reset or host/container changes occur.
"""
import argparse
import json
import logging
import os
import re
import time
import uuid

from .errors import HubError
from .locks import resource_guard
from .replay_highest_video_v2 import (HighestReplayVideoWeb,highest_replay_manifest,actual_start,
    PILOT_SOURCE,POLICY,STATE,KIND,JOB_TYPE,DOWNLOAD_STATE,DOWNLOAD_JOB_TYPE,DOWNLOAD_POLICY)
from .replay_worker_runtime import load_youtube_key

VERSION='replay-highest-pilot-worker-runtime-v2'
PRINCIPAL='system:replay-highest-pilot-reconcile-v2'


def require_pilot_policy(service):
    policy=service.policy()
    if (policy.get('source_scope')!='pilot_single' or policy.get('acceptance_video_ids')!=[PILOT_SOURCE] or
            policy.get('automation_enabled') is not False):
        raise HubError('REPLAY_HIGHEST_RUNTIME_PILOT_SCOPE_REQUIRED',review=True)
    return policy


def guarded_handler(service,handler):
    def process(job):
        policy=require_pilot_policy(service)
        if (job.get('source_id')!=PILOT_SOURCE or job.get('job_type')!=JOB_TYPE or
                job.get('policy_version')!=POLICY or job.get('payload',{}).get('policy_sha256')!=policy['_sha256']):
            raise HubError('REPLAY_HIGHEST_RUNTIME_JOB_OUTSIDE_PILOT',review=True)
        return handler(job)
    return process


def reconcile_tick(settings,store,service,*,now=None):
    """At most one existing, causally bound BV query; never retry an unknown send."""
    return known_bv_tick(settings,store,service,require_pilot_policy(service),now=now)


def known_bv_tick(settings,store,service,policy,*,now=None,version=VERSION,principal=PRINCIPAL):
    """Shared fixed-service-state query; no media access, admission or resend."""
    now=time.time() if now is None else now;scheduled=[]
    with resource_guard(settings.root/'data/locks'/(service.STATE+'-reconcile')):
        with store.transaction() as db:
            rows=db.execute("""SELECT * FROM jobs WHERE source_platform='youtube'
                AND job_type=? AND queue='video' AND policy_version=? AND state='awaiting_moderation'
                AND updated_at<=? ORDER BY updated_at LIMIT 1""",(service.JOB_TYPE,service.POLICY,now-300)).fetchall()
            for row in rows:
                binding=json.loads(row['payload']);result=json.loads(row['result']);state=result.get(service.STATE,{})
                observed=state.get('result_observation',{});external=row['external_publish_id'];attempt=state.get('attempt_id')
                try:valid=isinstance(attempt,str) and str(uuid.UUID(attempt))==attempt
                except ValueError:valid=False
                if (not valid or binding.get('source_id')!=row['source_id'] or
                        policy['source_scope']=='pilot_single' and row['source_id'] not in policy['acceptance_video_ids'] or
                        binding.get('policy_sha256')!=policy['_sha256'] or
                        binding.get('site_uid')!=policy['target_site_uid'] or row['attempts']>=settings.config['max_attempts'] or
                        row['lease_owner'] is not None or row['lease_until'] is not None or state.get('state')!='awaiting_moderation' or
                        not isinstance(external,str) or not re.fullmatch('BV[A-Za-z0-9]{10}',external) or
                        observed.get('version')!=service.RESULT_OBSERVATION_VERSION or observed.get('external_id')!=external or
                        observed.get('state')!='awaiting_moderation' or observed.get('attempt_id')!=attempt or
                        observed.get('site_uid')!=binding['site_uid'] or observed.get('source_url')!=binding.get('source_url') or
                        observed.get('metadata_sha256')!=binding.get('metadata_sha256') or
                        observed.get('url')!='https://www.bilibili.com/video/'+external or state.get('public_submission_approved') is not False):continue
                # No media read here: public reconciliation must remain possible
                # when a later, separately authorized retention task cleaned it.
                source=store.get(binding['download_job_id'])
                try:actual_start(source,source['result'][service.DOWNLOAD_STATE]['binding'])
                except (HubError,KeyError,TypeError):continue
                publication=db.execute("""SELECT * FROM publications WHERE job_id=? AND idempotency_key=?
                    AND platform='bilibili' AND kind=? AND state='awaiting_moderation'""",(row['job_id'],state.get('key'),service.KIND)).fetchall()
                if (len(publication)!=1 or publication[0]['external_id']!=external or publication[0]['source_id']!=row['source_id'] or
                        publication[0]['source_platform']!='youtube' or publication[0]['external_url']!=observed['url'] or
                        publication[0]['account_id']!='site_uid:'+binding['site_uid'] or db.execute(
                            "SELECT COUNT(*) FROM audit WHERE target=? AND action='short_video_reserve_submit' AND outcome='one_artifact_one_click_reserved'",
                            (row['job_id'],)).fetchone()[0]!=1):continue
                changed=db.execute("""UPDATE jobs SET state='retry_scheduled',next_run=?,updated_at=?,
                    stage='replay_highest_known_bv_lookup_scheduled' WHERE job_id=? AND state='awaiting_moderation'
                    AND updated_at=? AND attempts=? AND lease_owner IS NULL AND lease_until IS NULL""",
                    (now,now,row['job_id'],row['updated_at'],row['attempts'])).rowcount
                if changed:
                    db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                        (now,principal,'replay_highest_known_bv_lookup_schedule',row['job_id'],'lookup_only_no_upload_or_approval'))
                    scheduled.append(row['job_id'])
    return {'version':version,'scheduled_known_bv_lookups':scheduled,'new_sources_admitted':0,
        'attempts_reset':False,'publication_authorized':False,'unknown_submissions_changed':False}


def build_worker(settings,store):
    from .cli import runtime
    from .worker import Worker
    from .replay_video_web import ReplayVideoWeb
    from .replay_video_browser import ReplayVideoWorkerHandler
    from .replay_worker_runtime import guarded_handler as old_guard
    from .youtube_download_v2 import YouTubeHighestDownload
    service=HighestReplayVideoWeb(settings,store);require_pilot_policy(service)
    download=YouTubeHighestDownload(settings,store);download.policy()
    handlers=runtime(settings,store)
    if any(key in handlers for key in (JOB_TYPE,DOWNLOAD_JOB_TYPE,'replay_video_publish')):
        raise HubError('REPLAY_HIGHEST_RUNTIME_HANDLER_ALREADY_REGISTERED',review=True)
    # Keep the old bounded replay route for already-admitted v1 jobs. Its own
    # unchanged policy and old pilot guard apply when that job actually runs.
    old=ReplayVideoWeb(settings,store);old_handler=ReplayVideoWorkerHandler(settings,store,service=old)
    handlers['replay_video_publish']=old_guard(old,old_handler.process)
    def manifest(settings,job,binding,state,attempt_id,**kwargs):
        return highest_replay_manifest(settings,store,job,binding,state,attempt_id,**kwargs)
    handler=ReplayVideoWorkerHandler(settings,store,service=service,manifest_builder=manifest)
    handlers[JOB_TYPE]=guarded_handler(service,handler.process)
    handlers[DOWNLOAD_JOB_TYPE]=download.process
    return Worker(settings,store,handlers),service


def run_loop(settings,store,worker,service,*,once=False,clock=time.monotonic,sleep=time.sleep):
    due=0
    while True:
        require_pilot_policy(service)
        if clock()>=due:
            try:reconcile_tick(settings,store,service)
            except HubError as exc:
                if exc.code!='HEAVY_RESOURCE_BUSY':raise
            due=clock()+60
        worked=worker.once('video')
        if once:return worked
        if not worked:sleep(1)


def main():
    from .config import Settings
    from .db import Store
    parser=argparse.ArgumentParser();parser.add_argument('--once',action='store_true');args=parser.parse_args()
    if os.name!='posix' or os.getuid()!=10001:raise HubError('REPLAY_HIGHEST_RUNTIME_APP_UID_REQUIRED',review=True)
    logging.basicConfig(level=logging.WARNING,format='%(levelname)s %(name)s')
    for name in ('httpx','httpcore'):logging.getLogger(name).setLevel(logging.CRITICAL)
    settings=Settings.load();settings.prepare();store=Store(settings.db_path);store.init()
    if os.environ.get('YOUTUBE_API_KEY_FILE')!='/run/secrets/youtube-api-key':
        raise HubError('REPLAY_HIGHEST_RUNTIME_YOUTUBE_KEY_FILE_REQUIRED',review=True)
    load_youtube_key('/run/secrets/youtube-api-key')
    worker,service=build_worker(settings,store);run_loop(settings,store,worker,service,once=args.once)


if __name__=='__main__':main()
