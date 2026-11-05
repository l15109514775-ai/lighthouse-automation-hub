"""Natural route bindings for the shared known-BV query coordinator."""
from .replay_natural_video_v3 import NaturalReplayVideoWeb,natural_replay_manifest
from .replay_moderation_common import (poll_policy,day_bounds,schedule_tick as schedule_common,assert_lookup_lease,
    QueueView as BaseQueueView,ModerationServiceMixin,ModerationLookupHandler as BaseHandler)

LOOKUP_JOB_TYPE='replay_natural_moderation_lookup_v3'
LOOKUP_PRINCIPAL='worker:replay-natural-moderation-v3'
VERSION='replay-natural-moderation-v3'

class Route:
    LOOKUP_JOB_TYPE=LOOKUP_JOB_TYPE
    MODERATION_VERSION=VERSION
    MODERATION_AUDIT_PREFIX='replay_natural'
    LOOKUP_PAYLOAD_VERSION='replay-natural-moderation-lookup-v3'
    LOOKUP_PRINCIPAL=LOOKUP_PRINCIPAL
    def __init__(self,service):self.service=service
    def __getattr__(self,name):return getattr(self.service,name)

def schedule_tick(settings,store,service,*,now=None):
    return schedule_common(settings,store,Route(service),now=now)

class QueueView(BaseQueueView):
    def __init__(self,store,*,lookups):super().__init__(store,lookups=lookups,lookup_job_type=LOOKUP_JOB_TYPE)

class ModerationService(ModerationServiceMixin,NaturalReplayVideoWeb):
    LOOKUP_JOB_TYPE=LOOKUP_JOB_TYPE
    LOOKUP_PRINCIPAL=LOOKUP_PRINCIPAL
    MODERATION_VERSION=VERSION
    MODERATION_AUDIT_PREFIX='replay_natural'
    LOOKUP_PAYLOAD_VERSION='replay-natural-moderation-lookup-v3'

class ModerationLookupHandler(BaseHandler):
    def __init__(self,settings,store,*,service=None,browser=None):
        super().__init__(settings,store,service=service or ModerationService(settings,store),
            manifest_builder=natural_replay_manifest,browser=browser)
