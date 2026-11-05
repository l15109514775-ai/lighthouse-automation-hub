"""Generic natural source inventory and acquisition via the bounded v2 engine."""
from datetime import datetime,timezone
from pathlib import Path
import json,os,sys,uuid

from adapters import ytdlp_highest_v2 as engine
from adapters import ytdlp_replay as legacy
from services.hub.errors import HubError
from services.hub.media_processing import atomic_json
from services.hub.youtube_download import digest
from services.hub.youtube_download_v3 import (VERSION,YouTubeNaturalDownload,YouTubeNaturalPrepare,
    PROVISIONAL_PLAN,validate_quality)
from services.hub.youtube_natural_plan_v3 import natural_source_plan,validate_natural_plan,stamp


def folder(settings,ident,*,planning=False):
    try:
        if str(uuid.UUID(ident))!=ident:raise ValueError()
        path=settings.root/'media'/ident/('youtube-highest-v3-plan' if planning else 'youtube-highest-v3')
        if not path.is_absolute() or path.resolve()!=path or any(p.is_symlink() for p in (path,*path.parents)):raise ValueError()
        path.mkdir(mode=0o700,parents=True,exist_ok=True)
        if os.name=='posix' and path.stat().st_mode&0o077:raise ValueError()
        return path
    except (OSError,ValueError,TypeError,AttributeError):raise HubError('YOUTUBE_NATURAL_WORKSPACE_INVALID',review=True) from None


def planning_folder(settings,ident):return folder(settings,ident,planning=True)


def provisional(value):
    if value!=PROVISIONAL_PLAN:raise HubError('YOUTUBE_NATURAL_PROVISIONAL_PLAN_CHANGED',review=True)
    return value


def natural_budget_limits(binding):
    b=binding.get('transport_budget',{})
    if (set(b)!={'provider','daily_requests','monthly_requests','history_reset'} or b['provider']!=legacy.PROVIDER or
            b['history_reset'] is not False or type(b['daily_requests']) is not int or not 1<=b['daily_requests']<=6000 or
            type(b['monthly_requests']) is not int or not 1<=b['monthly_requests']<=150000):
        raise HubError('YOUTUBE_NATURAL_BUDGET_INVALID',review=True)
    return {legacy.PROVIDER:dict(daily_usd=0,monthly_usd=0,daily_calls=b['daily_requests'],
        monthly_calls=b['monthly_requests'],max_call_usd=0,paid_authorized=False)}


def plan_from_info(info,binding):
    now=datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
    return natural_source_plan(binding['official_raw'],info,activated_at=binding['activated_at'],observed_at=now)


def select_from_info(info,binding):
    expected=validate_natural_plan(binding['source_plan'],official_raw=binding['official_raw'])
    fresh=plan_from_info(info,binding)
    if {k:v for k,v in fresh.items() if k!='observed_at'}!={k:v for k,v in expected.items() if k!='observed_at'}:
        raise HubError('YOUTUBE_NATURAL_FRESH_INVENTORY_CHANGED',review=True)
    answer=[]
    for identity in expected['selected_formats']:
        matching=[item for item in info['formats'] if item.get('format_id')==identity['format_id']]
        if len(matching)!=1:raise HubError('YOUTUBE_NATURAL_EXACT_TRACK_REQUIRED',review=True)
        item=matching[0];legacy.destination(item.get('url'),track=True)
        if any(k.lower() in ('cookie','authorization','proxy-authorization') for k in (item.get('http_headers') or {})):
            raise HubError('YOUTUBE_DOWNLOAD_AUTHENTICATION_FORBIDDEN')
        answer.append(item)
    return answer


class NaturalReplayDownloader(engine.HighestReplayDownloader):
    VERSION=VERSION
    CHILD_MODULE='adapters.ytdlp_natural_v3'
    PLAN_VALIDATOR=staticmethod(validate_natural_plan)

    def folder(self,ident):return folder(self.settings,ident)
    def validate_quality(self,video,audio,binding):return validate_quality(video,audio,binding)


class NaturalReplayPlanner(NaturalReplayDownloader):
    CHILD_MODULE='adapters.ytdlp_natural_plan_v3'

    def folder(self,ident):return planning_folder(self.settings,ident)

    def acquire(self,job,binding,*,check):
        area=self.folder(job['job_id']);check();self.maintenance.check_disk(PROVISIONAL_PLAN['required_working_bytes'])
        engine.preflight_budget(self.store,binding,64,limits_function=natural_budget_limits)
        ticket=area/'ticket.json';output=area/'download-result.json'
        atomic_json(ticket,{'root':str(self.settings.root),'job_id':job['job_id'],'lease_owner':job['lease_owner'],'binding':binding})
        ticket.chmod(0o600)
        if output.exists():
            if output.is_symlink() or output.stat().st_size>262144:raise HubError('YOUTUBE_NATURAL_PLAN_RECEIPT_INVALID',review=True)
            record=json.loads(output.read_bytes())
            cache_valid=not record.get('error_code') and record.get('binding_sha256')==digest(binding)
            fresh=cache_valid and 0<=datetime.now(timezone.utc).timestamp()-stamp(record['source_plan']['observed_at']).timestamp()<=300
            if fresh:return record
            name=('expired-plan' if cache_valid else 'failure')+'-before-attempt-'+str(job['attempts'])+'.json'
            atomic_json(area/name,record);output.unlink() # Metadata-only cache; no original or partial media is deleted.
        return self._helper(job,binding,area,ticket,output,check)


def planning_child(ticket):
    # The video ID is read from the normal lease-checked immutable ticket. The
    # shared child revalidates it against the official job before any request.
    data=json.loads(ticket.read_bytes())
    from .youtube_readiness_evidence_v3 import record
    return engine.child_download(ticket,service_type=YouTubeNaturalPrepare,folder_fn=planning_folder,
        plan_validator=provisional,selector=plan_from_info,version_label=VERSION,
        video_id=data['binding']['video_id'],metadata_only=True,limits_function=natural_budget_limits,observation_recorder=record)


def download_child(ticket):
    data=json.loads(ticket.read_bytes())
    return engine.child_download(ticket,service_type=YouTubeNaturalDownload,folder_fn=folder,
        plan_validator=validate_natural_plan,selector=select_from_info,version_label=VERSION,
        video_id=data['binding']['video_id'],limits_function=natural_budget_limits)


if __name__=='__main__':
    if len(sys.argv)!=3 or sys.argv[1]!='--download':raise SystemExit('INTERNAL_NATURAL_DOWNLOAD_HELPER_ONLY')
    raise SystemExit(download_child(Path(sys.argv[2])))
