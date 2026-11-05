"""One normal Worker, bounded natural producers and known-BV reconciliation.

The existing monitor can continue in its own core service. This worker reads
its official observations, never asks a chat client to advance the pipeline.
"""
import argparse,json,logging,os,threading,time
from .db import Store
from .errors import HubError
from .locks import resource_guard
from .replay_worker_runtime import load_youtube_key
from .replay_natural_video_v3 import (NaturalReplayVideoWeb,natural_replay_manifest,JOB_TYPE,STATE,POLICY,
    DOWNLOAD_JOB_TYPE,DOWNLOAD_POLICY,DOWNLOAD_STATE)
from .youtube_download import CHANNELS

VERSION='replay-natural-worker-runtime-v3'


def require_natural_policy(service):
    policy=service.policy();service._require_workflow_launch(policy)
    return policy


def produce_download_tick(settings,store,downloader,*,now=None):
    """At most one new metadata preparation from fresh official ended rows."""
    from .youtube_download_v3 import PREPARE_JOB_TYPE
    now=time.time() if now is None else now;policy=downloader.policy()
    created=[];stopped=[]
    with resource_guard(settings.root/'data/locks/natural-download-producer-v3'):
        rows=store.query("""SELECT * FROM youtube_videos WHERE source_api='youtube-data-v3-official'
            AND state='ended' AND last_api_check>=? AND last_api_check<=? ORDER BY last_api_check DESC LIMIT 20""",(now-300,now+30))
        for row in rows:
            if row['channel_id'] not in CHANNELS:continue
            if store.query("SELECT 1 FROM jobs WHERE source_platform='youtube' AND source_id=? AND job_type IN (?,?)",
                    (row['video_id'],PREPARE_JOB_TYPE,DOWNLOAD_JOB_TYPE)):continue
            if store.query("SELECT 1 FROM publications WHERE source_platform='youtube' AND source_id=?",(row['video_id'],)):continue
            try:
                raw=json.loads(row['metadata']);payload={'video_id':row['video_id'],'channel_id':row['channel_id'],
                    'title':raw['snippet']['title'],'published_at':raw['snippet']['publishedAt'],'duration':raw['contentDetails']['duration'],
                    'url':'https://www.youtube.com/watch?v='+row['video_id'],'source_api':'youtube-data-v3-official','raw':raw}
                ident=downloader.enqueue_if_eligible(payload)
                if ident:created.append(ident);break
            except (KeyError,TypeError,ValueError):
                code='REPLAY_NATURAL_OFFICIAL_OBSERVATION_INVALID';stopped.append(code)
                store.event('review.required',{'source_id':row['video_id'],'code':code},dedup_key='natural-producer:'+row['video_id']+':'+code)
            except HubError as exc:
                if exc.code in ('YOUTUBE_NATURAL_HISTORICAL_SOURCE_REJECTED','YOUTUBE_HIGHEST_BEFORE_OPERATING_START',
                    'YOUTUBE_NATURAL_SOURCE_ALREADY_PROCESSING_OR_PROCESSED','YOUTUBE_NATURAL_PUBLICATION_RECONCILIATION_REQUIRED'):continue
                stopped.append(exc.code);store.event('review.required',{'source_id':row['video_id'],'code':exc.code},
                    dedup_key='natural-producer:'+row['video_id']+':'+exc.code)
    return {'version':VERSION,'created_metadata_prepare_jobs':created,'stop_codes':stopped,'media_downloads_started':0,'publications':0}


def produce_publication_tick(settings,store,service):
    """At most one complete, version-bound download enters the public queue."""
    require_natural_policy(service);created=[];stopped=[]
    rows=store.query("""SELECT * FROM jobs WHERE source_platform='youtube' AND job_type=? AND policy_version=?
        AND state='completed' ORDER BY updated_at LIMIT 20""",(DOWNLOAD_JOB_TYPE,DOWNLOAD_POLICY))
    for row in rows:
        if store.query("SELECT 1 FROM jobs WHERE source_platform='youtube' AND source_id=? AND job_type=?",(row['source_id'],JOB_TYPE)):continue
        if store.query("SELECT 1 FROM publications WHERE source_platform='youtube' AND source_id=?",(row['source_id'],)):continue
        try:
            created.append(service.queue_verified_download(row['job_id']));break
        except HubError as exc:
            if exc.code=='HEAVY_RESOURCE_BUSY':return {'version':VERSION,'queued_publication_jobs':[],'deferred':'HEAVY_RESOURCE_BUSY','stop_codes':[]}
            stopped.append(exc.code);store.event('review.required',{'job_id':row['job_id'],'source_id':row['source_id'],'code':exc.code},
                dedup_key='natural-publication:'+row['job_id']+':'+exc.code)
    return {'version':VERSION,'queued_publication_jobs':created,'stop_codes':stopped,'actual_publications':0}


def reconcile_tick(settings,store,service,*,now=None):
    from .replay_natural_moderation_v3 import schedule_tick
    require_natural_policy(service)
    return schedule_tick(settings,store,service,now=now)


def guarded_handler(service,handler):
    def process(job):
        policy=require_natural_policy(service)
        if (job.get('job_type')!=JOB_TYPE or job.get('policy_version')!=POLICY or
                job.get('payload',{}).get('policy_sha256')!=policy['_sha256'] or
                job.get('payload',{}).get('channel_id') not in CHANNELS):
            raise HubError('REPLAY_NATURAL_RUNTIME_SCOPE_REQUIRED',review=True)
        return handler(job)
    return process


def build_worker(settings,store):
    from .cli import runtime
    from .worker import Worker
    from .replay_video_browser import ReplayVideoWorkerHandler
    from .replay_video_web import ReplayVideoWeb
    from .replay_highest_video_v2 import HighestReplayVideoWeb,highest_replay_manifest
    from .replay_worker_runtime import guarded_handler as old_guard
    from .replay_highest_worker_runtime_v2 import guarded_handler as pilot_guard
    from .youtube_download_v2 import YouTubeHighestDownload
    from .youtube_download_v3 import YouTubeNaturalPrepare,worker_handlers
    service=NaturalReplayVideoWeb(settings,store);policy=require_natural_policy(service)
    downloader=YouTubeNaturalPrepare(settings,store);download_policy=downloader.policy()
    from .youtube_natural_plan_v3 import stamp
    if stamp(policy['activated_at'])!=stamp(download_policy['activated_at']):raise HubError('REPLAY_NATURAL_ACTIVATION_CHANGED',review=True)
    handlers=runtime(settings,store);additions=worker_handlers(settings,store)
    assert not set(handlers)&(set(additions)|{JOB_TYPE,'replay_video_publish','replay_highest_video_publish_v2','youtube_download_v2'})
    old=ReplayVideoWeb(settings,store);legacy=ReplayVideoWorkerHandler(settings,store,service=old)
    handlers['replay_video_publish']=old_guard(old,legacy.process)
    pilot=HighestReplayVideoWeb(settings,store)
    def pilot_manifest(s,j,b,state,a,**kwargs):return highest_replay_manifest(s,store,j,b,state,a,**kwargs)
    pilot_handler=ReplayVideoWorkerHandler(settings,store,service=pilot,manifest_builder=pilot_manifest)
    handlers['replay_highest_video_publish_v2']=pilot_guard(pilot,pilot_handler.process)
    handlers['youtube_download_v2']=YouTubeHighestDownload(settings,store).process
    handlers.update(additions)
    def manifest(s,j,b,state,a,**kwargs):return natural_replay_manifest(s,store,j,b,state,a,**kwargs)
    native=ReplayVideoWorkerHandler(settings,store,service=service,manifest_builder=manifest)
    from .errors import ResourceDeferred,PublishUnknown
    from .maintenance import Maintenance
    from .resource_wait_v1 import ResourceWaitCoordinator
    from .youtube_download import digest
    guarded=guarded_handler(service,native.process)
    def replay_binding(job):
        policy=service.policy()
        if job['payload'].get('policy_sha256')!=policy['_sha256']:raise HubError('REPLAY_NATURAL_ACTIVATION_CHANGED',review=True)
        current,binding,state=service.snapshot(job['job_id'],write=False)
        if (binding!=job['payload'] or state.get('state')!='prepared' or state.get('public_submission_approved') is not False or
                any(key in state for key in ('result_observation','upload_observation','native_preparation')) or
                store.query("SELECT 1 FROM publications WHERE job_id=? AND (state<>'rejected' OR external_id IS NOT NULL OR external_url IS NOT NULL)",(job['job_id'],))):
            raise PublishUnknown()
        folder=settings.root/'media'/job['job_id']
        # Even a crash before the DB checkpoint must not turn a prior native
        # selection/dispatch intent into a fresh resource-resumable action.
        for file in (folder/'replay-native-intents',folder/'recovered-submit-intent.json'):
            if file.exists() or file.is_symlink():raise PublishUnknown()
        return digest(job['payload'])
    def resumable(job):
        if job.get('result',{}).get(STATE,{}).get('state')=='prepared':
            try:Maintenance(settings,store).check_disk(32_000_000)
            except HubError as exc:
                if exc.code=='DISK_LOW':
                    raise ResourceDeferred(resource='disk',required_bytes=32_000_000,binding_sha256=replay_binding(job)) from None
                raise
        # A later DISK_LOW is deliberately not translated: a native action may
        # already have happened and its own checkpoint/reconciliation applies.
        return guarded(job)
    handlers[JOB_TYPE]=resumable
    # Download validators are supplied by the download module, never guessed
    # here; source dates/policy/binding checks remain its authority.
    from .youtube_download_v3 import resource_wait_validators
    waits=ResourceWaitCoordinator(settings,store,validators={**resource_wait_validators(settings,store),JOB_TYPE:replay_binding})
    from .replay_natural_moderation_v3 import LOOKUP_JOB_TYPE,ModerationLookupHandler,QueueView
    from .youtube_readiness_v3 import YouTubeReadinessCoordinator
    readiness=YouTubeReadinessCoordinator(settings,store)
    worker=Worker(settings,QueueView(store,lookups=False),handlers,resource_deferrals=waits.callbacks(),readiness_deferrals=readiness.callbacks())
    worker.resource_waits,worker.readiness_waits=waits,readiness
    moderation=ModerationLookupHandler(settings,store)
    worker.moderation_worker=Worker(settings,QueueView(store,lookups=True),{LOOKUP_JOB_TYPE:moderation.process})
    return worker,service,downloader


def moderation_loop(worker,*,sleep=time.sleep,fatal=os._exit):
    try:
        while True:
            if not worker.once('moderation'):sleep(1)
    except BaseException:
        # An otherwise silent dead side thread must never be masked by the
        # main role's healthy heartbeat. Restart recovers each scoped lease.
        logging.getLogger(__name__).critical('MODERATION_WORKER_STOPPED')
        fatal(70)


def run_loop(settings,store,worker,service,downloader,*,once=False,clock=time.monotonic,sleep=time.sleep):
    if not once and hasattr(worker,'moderation_worker'):
        threading.Thread(target=moderation_loop,args=(worker.moderation_worker,),name='replay-moderation',daemon=True).start()
    due=0
    while True:
        require_natural_policy(service)
        if clock()>=due:
            for operation in (lambda:produce_download_tick(settings,store,downloader),
                    lambda:produce_publication_tick(settings,store,service),lambda:reconcile_tick(settings,store,service)):
                try:operation()
                except HubError as exc:
                    if exc.code!='HEAVY_RESOURCE_BUSY':raise
            if hasattr(worker,'resource_waits'):worker.resource_waits.tick()
            if hasattr(worker,'readiness_waits'):worker.readiness_waits.tick()
            due=clock()+60
        worked=worker.once('video')
        if once:
            if hasattr(worker,'moderation_worker'):worker.moderation_worker.once('moderation')
            return worked
        if not worked:sleep(1)


def main():
    from .config import Settings
    parser=argparse.ArgumentParser();parser.add_argument('--once',action='store_true');args=parser.parse_args()
    if os.name!='posix' or os.getuid()!=10001:raise HubError('REPLAY_NATURAL_RUNTIME_APP_UID_REQUIRED',review=True)
    logging.basicConfig(level=logging.WARNING,format='%(levelname)s %(name)s')
    for name in ('httpx','httpcore'):logging.getLogger(name).setLevel(logging.CRITICAL)
    if os.environ.get('YOUTUBE_API_KEY_FILE')!='/run/secrets/youtube-api-key':raise HubError('REPLAY_NATURAL_RUNTIME_KEY_FILE_REQUIRED',review=True)
    settings=Settings.load();settings.prepare();store=Store(settings.db_path);store.init();load_youtube_key('/run/secrets/youtube-api-key')
    worker,service,downloader=build_worker(settings,store);run_loop(settings,store,worker,service,downloader,once=args.once)


if __name__=='__main__':main()
