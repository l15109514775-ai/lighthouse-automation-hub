"""FIFO-v4 boundary over the existing authenticated TLS/range/stream-copy engine."""
from datetime import datetime,timezone
import hashlib,json,math,os,re,shutil,stat,sys,uuid
from pathlib import Path

from adapters import ytdlp_highest_v2 as engine
from adapters import ytdlp_replay as legacy
from adapters.youtube_readiness_evidence_v3 import runtime_snapshot
from services.hub.errors import HubError
from services.hub.maintenance import Maintenance
from services.hub.media_processing import atomic_json
from services.hub.replay_multipart_v4 import plan_whole_replay,digest
from services.hub.youtube_download_v3 import validate_quality
from services.hub.youtube_download_v4 import (VERSION,PROVISIONAL,YouTubeWholeReplayDownload,YouTubeWholeReplayPrepare,validate_transfer_plan)


def folder(settings,ident,*,planning=False):
    try:
        if str(uuid.UUID(ident))!=ident:raise ValueError()
        path=settings.root/'media'/ident/('youtube-whole-v4-plan' if planning else 'youtube-whole-v4')
        if not path.is_absolute() or path.resolve()!=path or any(p.is_symlink() for p in (path,*path.parents)):raise ValueError()
        path.mkdir(mode=0o700,parents=True,exist_ok=True)
        if os.name=='posix' and path.stat().st_mode&0o077:raise ValueError()
        return path
    except (OSError,ValueError,TypeError,AttributeError):raise HubError('YOUTUBE_WHOLE_WORKSPACE_INVALID',review=True) from None
def planning_folder(settings,ident):return folder(settings,ident,planning=True)


def budget_limits(binding):
    b=binding.get('transport_budget',{})
    if (set(b)!={'provider','daily_requests','monthly_requests','history_reset'} or b['provider']!=legacy.PROVIDER or b['history_reset'] is not False or
            type(b['daily_requests']) is not int or not 1<=b['daily_requests']<=20000 or
            type(b['monthly_requests']) is not int or not 1<=b['monthly_requests']<=300000):raise HubError('YOUTUBE_WHOLE_BUDGET_INVALID',review=True)
    return {legacy.PROVIDER:dict(daily_usd=0,monthly_usd=0,daily_calls=b['daily_requests'],monthly_calls=b['monthly_requests'],max_call_usd=0,paid_authorized=False)}


def _metadata_temporary_limit(name):
    if name in ('progress.partial.json','selection.partial.json','download-result.partial.json'):return 262144
    prefix='extractor-observation-';suffix='.partial.json'
    if name.startswith(prefix) and name.endswith(suffix):
        ident=name[len(prefix):-len(suffix)]
        try:
            if str(uuid.UUID(ident))==ident:return 2_097_152
        except ValueError:pass
    return None


def needed_disk(area,binding):
    budget=binding['source_plan']['required_working_bytes'];used=0;count=0;counted=set()
    for file in area.rglob('*'):
        count+=1
        if count>256 or file.resolve()!=file:raise HubError('YOUTUBE_WHOLE_WORKSPACE_INVALID',review=True)
        relative=file.relative_to(area)
        try:info=file.lstat()
        except FileNotFoundError:
            # Only the actual child atomic_json writers may vanish during
            # enumeration. Require their bounded regular destination, then
            # credit zero for the vanished temporary (a larger reservation).
            limit=_metadata_temporary_limit(relative.name) if len(relative.parts)==1 else None
            if limit is not None:
                target=file.with_name(file.name[:-len('.partial.json')]+'.json')
                try:
                    if target.resolve()!=target:raise ValueError()
                    completed=target.lstat()
                    if not stat.S_ISREG(completed.st_mode) or not 0<completed.st_size<=limit:raise ValueError()
                except (OSError,ValueError):raise HubError('YOUTUBE_WHOLE_WORKSPACE_INVALID',review=True) from None
                continue
            matches=[f for f in binding['source_plan'].get('selected_formats',[]) if
                len(relative.parts)==1 and relative.name=='stream-'+f['format_id']+'.'+f['ext']+'.part']
            if len(matches)!=1:raise HubError('YOUTUBE_WHOLE_WORKSPACE_INVALID',review=True) from None
            # yt-dlp atomically renames only this bound track at completion.
            # A missing partial requires its exact-size regular destination;
            # absence of an arbitrary media/master file is still an error.
            target=file.with_name(file.name[:-5])
            try:
                if target.resolve()!=target:raise ValueError()
                info=target.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_size!=matches[0]['filesize']:raise ValueError()
            except (OSError,ValueError):raise HubError('YOUTUBE_WHOLE_WORKSPACE_INVALID',review=True) from None
        if stat.S_ISLNK(info.st_mode):raise HubError('YOUTUBE_WHOLE_WORKSPACE_INVALID',review=True)
        if stat.S_ISDIR(info.st_mode):
            permitted={('parts-v4',)}
            if binding.get('planning_policy',{}).get('raw_track_release_after_verified_merge') is True:permitted.add(('raw-release-lock-v4',))
            if binding.get('planning_policy',{}).get('prefer_single_whole_passthrough_v5') is True:permitted.add(('delivery-v5',))
            if relative.parts not in permitted:raise HubError('YOUTUBE_WHOLE_WORKSPACE_INVALID',review=True)
        elif stat.S_ISREG(info.st_mode) and len(relative.parts)<=2:
            # One instantaneous, no-follow allocation estimate. This is not
            # an immutability or content-hash claim about the growing media.
            identity=(info.st_dev,info.st_ino)
            if identity not in counted:used+=info.st_size;counted.add(identity)
        else:raise HubError('YOUTUBE_WHOLE_WORKSPACE_INVALID',review=True)
    if used>budget+8_388_608:raise HubError('YOUTUBE_WHOLE_WORKSPACE_SIZE_LIMIT',review=True)
    return max(0,budget-used)


class WholeMaintenance(Maintenance):
    def check_disk(self,reserve_bytes=0):
        free=shutil.disk_usage(self.settings.root).free
        p=YouTubeWholeReplayDownload(self.settings,self.store).policy()['_planning']
        floor=max(p['disk_floor_bytes'],int(self.settings.config['disk_floor_gb']*1_000_000_000))
        if free-reserve_bytes<floor:
            self.store.event('disk.low',{'free_bytes':free,'floor_bytes':floor,'policy':'whole-replay-v4'},dedup_key='disk.low')
            raise HubError('DISK_LOW',review=True)
        return free


def provisional(value):
    if value!=PROVISIONAL:raise HubError('YOUTUBE_WHOLE_PROVISIONAL_CHANGED',review=True)
    return value
def plan_from_info(info,binding):
    snapshot,_=runtime_snapshot(info)
    return plan_whole_replay(binding['official_raw'],snapshot,observed_at=datetime.now(timezone.utc).isoformat(),policy=binding['planning_policy'])


def optional_storyboard(value):
    """Only YouTube's non-media MHTML storyboard family is non-contractual."""
    return (isinstance(value,dict) and re.fullmatch(r'sb[0-9]{1,3}',str(value.get('format_id'))) is not None and
        value.get('ext')==value.get('protocol')=='mhtml' and value.get('acodec')==value.get('vcodec')=='none' and
        value.get('has_drm') is False)


def select_from_info(info,binding):
    expected=binding['whole_replay_plan'];fresh=plan_from_info(info,binding)
    def stable(value):
        item=json.loads(json.dumps(value));item['source_plan_basis'].pop('observed_at');return item
    previous,current=stable(expected),stable(fresh)
    old_basis,new_basis=previous['source_plan_basis'],current['source_plan_basis']
    old_inventory,new_inventory=old_basis['source_inventory'],new_basis['source_inventory']
    def changed():raise HubError('YOUTUBE_WHOLE_FRESH_INVENTORY_CHANGED',review=True)
    if (old_basis['inventory_sha256']!=digest(old_inventory) or
            new_basis['inventory_sha256']!=digest(new_inventory)):changed()
    selected_ids={f['format_id'] for basis in (old_basis,new_basis) for f in basis['selected_formats']}
    storyboard_ids={f['format_id'] for f in (*old_inventory,*new_inventory) if optional_storyboard(f)}
    if selected_ids&storyboard_ids:changed()
    old_inventory=[f for f in old_inventory if not optional_storyboard(f)]
    new_inventory=[f for f in new_inventory if not optional_storyboard(f)]
    old_basis['source_inventory']=old_inventory;new_basis['source_inventory']=new_inventory
    old_basis['inventory_sha256']=digest(old_inventory);new_basis['inventory_sha256']=digest(new_inventory)
    if len(old_inventory)!=len(new_inventory):changed()
    for old,new in zip(old_inventory,new_inventory):
        if set(old)!=set(new) or old['format_id']!=new['format_id']:changed()
        if old['format_id'] in selected_ids:continue
        # YouTube may revise a lower, unused encode while the selected byte
        # streams remain identical. Keep every other inventory field and all
        # plan/budget/selection checks; missing metadata is not a revision.
        for field in ('filesize','bitrate_kbps'):
            if old[field]==new[field]:continue
            values=(old[field],new[field])
            if field=='filesize':valid=all(type(v) is int and v>0 for v in values)
            else:valid=all(type(v) in (int,float) and math.isfinite(v) and v>0 for v in values)
            if not valid:changed()
            new[field]=old[field]
    new_basis['inventory_sha256']=digest(new_inventory)
    if current!=previous:changed()
    plan=validate_transfer_plan(binding['source_plan']);selected=[]
    for record in plan['selected_formats']:
        matches=[f for f in info['formats'] if f.get('format_id')==record['format_id']]
        if len(matches)!=1:raise HubError('YOUTUBE_WHOLE_EXACT_TRACK_REQUIRED',review=True)
        fmt=matches[0];legacy.destination(fmt.get('url'),track=True)
        if any(k.lower() in ('cookie','authorization','proxy-authorization') for k in (fmt.get('http_headers') or {})):raise HubError('YOUTUBE_DOWNLOAD_AUTHENTICATION_FORBIDDEN')
        selected.append(fmt)
    return selected


def observation_record(info,binding,area,http):
    snapshot,normalization=runtime_snapshot(info)
    value={'version':'youtube-extractor-observation-v4','job_id':binding['job_id'],'binding_sha256':digest(binding),
        'recorded_at':datetime.now(timezone.utc).isoformat(),'extractor_info':snapshot,'http':dict(http),
        'media_downloaded':False,'human_review':False,'publication_authorized':False}
    if normalization is not None:value['normalization']=normalization
    raw=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    if len(raw)>2_097_152:raise HubError('YOUTUBE_WHOLE_METADATA_SIZE_LIMIT',review=True)
    file=area/('extractor-observation-'+str(uuid.uuid4())+'.json');atomic_json(file,value);file.chmod(0o600)
    return {'file':str(file),'sha256':hashlib.sha256(file.read_bytes()).hexdigest(),'bytes':file.stat().st_size,'version':value['version']}


class WholeReplayDownloader(engine.HighestReplayDownloader):
    VERSION=VERSION
    CHILD_MODULE='adapters.ytdlp_whole_v4'
    PLAN_VALIDATOR=staticmethod(validate_transfer_plan)
    NEEDED_DISK=staticmethod(needed_disk)
    MAX_PACKET_COUNT=64_000_000
    PACKET_TIMEOUT=28800
    def __init__(self,settings):
        super().__init__(settings);self.maintenance=WholeMaintenance(settings,self.store)
    def folder(self,ident):return folder(self.settings,ident)
    def validate_quality(self,video,audio,binding):return validate_quality(video,audio,binding)
    def _finalize(self,job,binding,area,result,check):
        self.MAX_PACKET_COUNT=binding['planning_policy']['max_total_packets'];self.PACKET_TIMEOUT=binding['planning_policy']['max_processing_seconds']
        return super()._finalize(job,binding,area,result,check)


class WholeReplayPlanner(WholeReplayDownloader):
    CHILD_MODULE='adapters.ytdlp_whole_plan_v4'
    def folder(self,ident):return planning_folder(self.settings,ident)
    def acquire(self,job,binding,*,check):
        area=self.folder(job['job_id']);check();self.maintenance.check_disk(PROVISIONAL['required_working_bytes'])
        engine.preflight_budget(self.store,binding,64,limits_function=budget_limits,request_cap=20000)
        ticket=area/'ticket.json';output=area/'download-result.json'
        atomic_json(ticket,{'root':str(self.settings.root),'job_id':job['job_id'],'lease_owner':job['lease_owner'],'binding':binding});ticket.chmod(0o600)
        if output.exists():
            if output.is_symlink() or output.stat().st_size>262144:raise HubError('YOUTUBE_WHOLE_PLAN_RECEIPT_INVALID',review=True)
            value=json.loads(output.read_bytes());valid=not value.get('error_code') and value.get('binding_sha256')==digest(binding)
            if valid and 0<=datetime.now(timezone.utc).timestamp()-datetime.fromisoformat(value['source_plan']['source_plan_basis']['observed_at'].replace('Z','+00:00')).timestamp()<=300:return value
            atomic_json(area/('expired-or-failed-before-attempt-'+str(job['attempts'])+'.json'),value);output.unlink()
        return self._helper(job,binding,area,ticket,output,check)


def child(ticket,*,planning=False):
    data=json.loads(ticket.read_bytes())
    return engine.child_download(ticket,service_type=YouTubeWholeReplayPrepare if planning else YouTubeWholeReplayDownload,
        folder_fn=planning_folder if planning else folder,plan_validator=provisional if planning else validate_transfer_plan,
        selector=plan_from_info if planning else select_from_info,version_label=VERSION,video_id=data['binding']['video_id'],
        metadata_only=planning,limits_function=budget_limits,observation_recorder=observation_record,
        disk_needed=needed_disk,maintenance_factory=WholeMaintenance,request_cap=20000,
        media_start_guard=lambda service,job,binding:service.check_media_start(job,binding))
if __name__=='__main__':
    if len(sys.argv)!=3 or sys.argv[1]!='--download':raise SystemExit('INTERNAL_WHOLE_REPLAY_DOWNLOAD_HELPER_ONLY')
    raise SystemExit(child(Path(sys.argv[2])))
