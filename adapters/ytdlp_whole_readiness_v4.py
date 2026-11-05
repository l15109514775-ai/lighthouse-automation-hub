"""Private V4 readiness inventory child; no media allowed by the shared engine."""
from pathlib import Path
import json,sys
from . import ytdlp_highest_v2 as engine
from .ytdlp_whole_v4 import (WholeReplayPlanner,planning_folder,provisional,plan_from_info,budget_limits,
    observation_record,needed_disk,WholeMaintenance)
from services.hub.youtube_download_v4 import VERSION
from services.hub.youtube_readiness_v4 import WholeReadinessObservationV4

class WholeReadinessPlannerV4(WholeReplayPlanner):
    CHILD_MODULE='adapters.ytdlp_whole_readiness_v4'

def child(ticket):
    data=json.loads(ticket.read_bytes())
    return engine.child_download(ticket,service_type=WholeReadinessObservationV4,folder_fn=planning_folder,
        plan_validator=provisional,selector=plan_from_info,version_label=VERSION,video_id=data['binding']['video_id'],
        metadata_only=True,limits_function=budget_limits,observation_recorder=observation_record,
        disk_needed=needed_disk,maintenance_factory=WholeMaintenance,request_cap=20000)
if __name__=='__main__':
    if len(sys.argv)!=3 or sys.argv[1]!='--download':raise SystemExit('INTERNAL_WHOLE_REPLAY_READINESS_HELPER_ONLY')
    raise SystemExit(child(Path(sys.argv[2])))
