"""The same guarded metadata-only extractor under a finite observation lease."""
from pathlib import Path
import json,sys

from . import ytdlp_highest_v2 as engine
from .ytdlp_natural_v3 import NaturalReplayPlanner,planning_folder,provisional,plan_from_info,natural_budget_limits
from .youtube_readiness_evidence_v3 import record


class ReadinessPlanner(NaturalReplayPlanner):
    CHILD_MODULE='adapters.ytdlp_natural_readiness_v3'


def child(ticket):
    from services.hub.youtube_readiness_v3 import ReadinessObservation
    from services.hub.youtube_download_v3 import VERSION
    data=json.loads(ticket.read_bytes())
    return engine.child_download(ticket,service_type=ReadinessObservation,folder_fn=planning_folder,
        plan_validator=provisional,selector=plan_from_info,version_label=VERSION,
        video_id=data['binding']['video_id'],metadata_only=True,limits_function=natural_budget_limits,observation_recorder=record)


if __name__=='__main__':
    if len(sys.argv)!=3 or sys.argv[1]!='--download':raise SystemExit('INTERNAL_YOUTUBE_READINESS_HELPER_ONLY')
    raise SystemExit(child(Path(sys.argv[2])))
