"""Pilot-scoped replacement entry for the normal durable video Worker.

No operator approval, queue admission, API-key loading or arbitrary command is
implemented here. Install only after the legacy video dispatcher is stopped.
"""
import argparse
import json
import logging
import os
import re
import time
import uuid

from .db import Store
from .errors import HubError
from .youtube_credentials import load_youtube_key
from .locks import resource_guard

PILOT_SOURCE = '9T5tn1WGlJg'
VERSION = 'replay-pilot-worker-runtime-v1'
PRINCIPAL = 'system:replay-pilot-reconcile-v1'
STATE = 'replay_video_web_v1'
POLICY = 'replay-video-website-policy-v1'
KIND = 'video_website_replay_v1'


def require_pilot_policy(service):
    policy=service.policy()
    if (policy.get('source_scope')!='pilot_single' or policy.get('acceptance_video_ids')!=[PILOT_SOURCE] or
            policy.get('automation_enabled') is not False):
        raise HubError('REPLAY_RUNTIME_PILOT_SCOPE_REQUIRED',review=True)
    return policy


def guarded_handler(service, handler):
    def process(job):
        policy=require_pilot_policy(service)
        if (job.get('source_id')!=PILOT_SOURCE or job.get('job_type')!='replay_video_publish' or
                job.get('policy_version')!=POLICY or job.get('payload',{}).get('policy_sha256')!=policy['_sha256']):
            raise HubError('REPLAY_RUNTIME_JOB_OUTSIDE_PILOT',review=True)
        return handler(job)
    return process


def reconcile_tick(settings,store,service,*,now=None):
    """At most one known-BV moderation lookup, respecting ordinary retry caps.

    Unknown submissions without a causal result remain unknown. This operation
    cannot admit a new source, clear an error, renew a lease or reset attempts.
    """
    policy=require_pilot_policy(service);now=time.time() if now is None else now
    maximum=settings.config['max_attempts'];scheduled=[]
    with resource_guard(settings.root/'data/locks/replay-pilot-reconcile'):
        with store.transaction() as db:
            rows=db.execute("""SELECT * FROM jobs WHERE source_platform='youtube' AND source_id=?
                AND job_type='replay_video_publish' AND queue='video' AND policy_version=?
                AND state='awaiting_moderation' AND updated_at<=? ORDER BY updated_at LIMIT 1""",
                (PILOT_SOURCE,POLICY,now-300)).fetchall()
            for row in rows:
                binding=json.loads(row['payload']);result=json.loads(row['result']);state=result.get(STATE,{})
                observed=state.get('result_observation',{});external=row['external_publish_id']
                attempt=state.get('attempt_id')
                try:valid_attempt=isinstance(attempt,str) and str(uuid.UUID(attempt))==attempt
                except ValueError:valid_attempt=False
                if (binding.get('source_id')!=PILOT_SOURCE or binding.get('policy_sha256')!=policy['_sha256'] or
                        binding.get('site_uid')!=policy['target_site_uid'] or row['attempts']>=maximum or
                        row['lease_owner'] is not None or row['lease_until'] is not None or
                        state.get('state')!='awaiting_moderation' or not isinstance(external,str) or
                        not re.fullmatch(r'BV[A-Za-z0-9]{10}',external) or observed.get('external_id')!=external or
                        observed.get('state')!='awaiting_moderation' or not valid_attempt or
                        observed.get('version')!='short-video-browser-result-v1' or observed.get('attempt_id')!=attempt or
                        observed.get('site_uid')!=binding['site_uid'] or observed.get('source_url')!=binding.get('source_url') or
                        observed.get('metadata_sha256')!=binding.get('metadata_sha256') or
                        observed.get('url')!='https://www.bilibili.com/video/'+external or
                        state.get('public_submission_approved') is not False):
                    continue
                publication=db.execute("""SELECT * FROM publications WHERE job_id=? AND idempotency_key=?
                    AND platform='bilibili' AND kind=? AND state='awaiting_moderation'""",
                    (row['job_id'],state.get('key'),KIND)).fetchall()
                if (len(publication)!=1 or publication[0]['external_id']!=external or
                        publication[0]['source_id']!=PILOT_SOURCE or
                        publication[0]['source_platform']!='youtube' or publication[0]['external_url']!=observed['url'] or
                        publication[0]['account_id']!='site_uid:'+binding['site_uid'] or
                        db.execute("SELECT COUNT(*) FROM audit WHERE target=? AND action='short_video_reserve_submit' AND outcome='one_artifact_one_click_reserved'",
                            (row['job_id'],)).fetchone()[0]!=1):
                    continue
                # Only job scheduling fields change. Original payload, result,
                # attempt count, BV, idempotency reservation and approvals stay.
                changed=db.execute("""UPDATE jobs SET state='retry_scheduled',next_run=?,updated_at=?,
                    stage='replay_known_bv_lookup_scheduled' WHERE job_id=? AND state='awaiting_moderation'
                    AND updated_at=? AND attempts=? AND lease_owner IS NULL AND lease_until IS NULL""",
                    (now,now,row['job_id'],row['updated_at'],row['attempts'])).rowcount
                if changed:
                    db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                        (now,PRINCIPAL,'replay_known_bv_lookup_schedule',row['job_id'],'lookup_only_no_upload_or_approval'))
                    scheduled.append(row['job_id'])
    return {'version':VERSION,'scheduled_known_bv_lookups':scheduled,'new_sources_admitted':0,
            'attempts_reset':False,'publication_authorized':False,'unknown_submissions_changed':False}


def build_worker(settings,store):
    from .cli import runtime
    from .replay_video_web import ReplayVideoWeb
    from .replay_video_browser import ReplayVideoWorkerHandler
    from .worker import Worker
    service=ReplayVideoWeb(settings,store);require_pilot_policy(service)
    handler=ReplayVideoWorkerHandler(settings,store,service=service)
    handlers=runtime(settings,store)
    if 'replay_video_publish' in handlers:
        raise HubError('REPLAY_RUNTIME_HANDLER_ALREADY_REGISTERED',review=True)
    handlers['replay_video_publish']=guarded_handler(service,handler.process)
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
    parser=argparse.ArgumentParser();parser.add_argument('--once',action='store_true');args=parser.parse_args()
    if os.name!='posix' or os.getuid()!=10001:
        raise HubError('REPLAY_RUNTIME_APP_UID_REQUIRED',review=True)
    logging.basicConfig(level=logging.WARNING,format='%(levelname)s %(name)s')
    for name in ('httpx','httpcore'):logging.getLogger(name).setLevel(logging.CRITICAL)
    settings=Settings.load();settings.prepare();store=Store(settings.db_path);store.init()
    file=os.environ.get('YOUTUBE_API_KEY_FILE')
    if file!='/run/secrets/youtube-api-key':raise HubError('REPLAY_RUNTIME_YOUTUBE_KEY_FILE_REQUIRED',review=True)
    load_youtube_key(file)
    worker,service=build_worker(settings,store)
    run_loop(settings,store,worker,service,once=args.once)


if __name__=='__main__':main()
