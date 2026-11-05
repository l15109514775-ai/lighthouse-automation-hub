"""Independent highest-v2 pilot known-BV lookups, never a new submit.

Preserves deployed handlers/policies. No natural-v3 grant or activation.
"""
import argparse,logging,os,threading,time
from .errors import HubError
from .replay_highest_video_v2 import HighestReplayVideoWeb,highest_replay_manifest
from .replay_moderation_common import schedule_tick,QueueView,ModerationServiceMixin,ModerationLookupHandler

VERSION='replay-highest-moderation-v2b'
LOOKUP_JOB_TYPE='replay_highest_moderation_lookup_v2b'
LOOKUP_PRINCIPAL='worker:replay-highest-moderation-v2b'

class ModerationService(ModerationServiceMixin,HighestReplayVideoWeb):
    LOOKUP_JOB_TYPE=LOOKUP_JOB_TYPE
    LOOKUP_PRINCIPAL=LOOKUP_PRINCIPAL
    MODERATION_VERSION=VERSION
    MODERATION_AUDIT_PREFIX='replay_highest_v2b'
    LOOKUP_PAYLOAD_VERSION='replay-highest-moderation-lookup-v2b'

def build_worker(settings,store):
    from .replay_highest_worker_runtime_v2 import build_worker as previous
    from .worker import Worker
    worker,_=previous(settings,store)
    worker.store=QueueView(store,lookups=False,lookup_job_type=LOOKUP_JOB_TYPE,heartbeat_suffix='v2b')
    service=ModerationService(settings,store);policy=service.policy()
    if policy['source_scope']!='pilot_single':raise HubError('REPLAY_HIGHEST_MODERATION_PILOT_SCOPE_REQUIRED',review=True)
    handler=ModerationLookupHandler(settings,store,service=service,manifest_builder=highest_replay_manifest)
    worker.moderation_worker=Worker(settings,QueueView(store,lookups=True,lookup_job_type=LOOKUP_JOB_TYPE,heartbeat_suffix='v2b'),
        {LOOKUP_JOB_TYPE:handler.process})
    return worker,service

def side_loop(worker,*,sleep=time.sleep,fatal=os._exit):
    try:
        while True:
            if not worker.once('moderation'):sleep(1)
    except BaseException:
        logging.getLogger(__name__).critical('HIGHEST_MODERATION_WORKER_STOPPED');fatal(70)

def run_loop(settings,store,worker,service,*,once=False,clock=time.monotonic,sleep=time.sleep):
    if not once:threading.Thread(target=side_loop,args=(worker.moderation_worker,),name='highest-moderation',daemon=True).start()
    due=0
    while True:
        if clock()>=due:
            try:schedule_tick(settings,store,service)
            except HubError as error:
                if error.code!='HEAVY_RESOURCE_BUSY':raise
            due=clock()+60
        worked=worker.once('video')
        if once:
            worker.moderation_worker.once('moderation');return worked
        if not worked:sleep(1)

def main():
    from .config import Settings
    from .db import Store
    from .replay_worker_runtime import load_youtube_key
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--once',action='store_true');args=parser.parse_args()
    if os.name!='posix' or os.getuid()!=10001:raise HubError('REPLAY_HIGHEST_RUNTIME_APP_UID_REQUIRED',review=True)
    if os.environ.get('YOUTUBE_API_KEY_FILE')!='/run/secrets/youtube-api-key':raise HubError('REPLAY_HIGHEST_RUNTIME_KEY_FILE_REQUIRED',review=True)
    logging.basicConfig(level=logging.WARNING,format='%(levelname)s %(name)s')
    for name in ('httpx','httpcore'):logging.getLogger(name).setLevel(logging.CRITICAL)
    settings=Settings.load();settings.prepare();store=Store(settings.db_path);store.init();load_youtube_key('/run/secrets/youtube-api-key')
    worker,service=build_worker(settings,store);run_loop(settings,store,worker,service,once=args.once)

if __name__=='__main__':main()
