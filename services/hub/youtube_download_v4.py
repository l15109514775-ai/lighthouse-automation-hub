"""Explicit FIFO-v4 acquisition only. Old V2/V3 jobs and policy stay separate."""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
import hashlib,json,math,os,re,time,tomllib,uuid
from pathlib import Path

from adapters.media_tools import inspect_media
from .db import encode
from .errors import HubError,ResourceDeferred
from .evidence import sha256_file
from .media_processing import atomic_json
from .replay_multipart_v4 import (MultipartMediaProcessingV4,validate_planning_policy,
    validate_whole_replay_plan,digest)
from .replay_fifo_v4 import ReplayFIFOV4,PLAN_JOB,DOWNLOAD_JOB,VERSION as FIFO_VERSION
from .youtube_download import CHANNELS
from .youtube_download_v3 import validate_quality

VERSION='youtube-whole-replay-download-v4'
STATE='youtube_download_v4'
POLICY_VERSION='youtube-whole-replay-download-policy-v4'
POLICY_FILE='youtube-whole-replay-download-policy-v4.toml'
PLAN_POLICY_FILE='whole-replay-planning-policy-v4.toml'
PROVISIONAL={'version':'youtube-whole-metadata-only-plan-v4','source_bytes':8388608,
    'required_working_bytes':32000000,'max_http_requests':64}


def fail(code):raise HubError(code,review=True)
class WholeResourceDeferredV4(ResourceDeferred):
    """Opt-in Worker callback, without changing the old 64GB exception contract."""
    def __init__(self,*,required_free_bytes,binding_sha256):
        if type(required_free_bytes) is not int or not 15*1024**3<=required_free_bytes<2**63 or not re.fullmatch('[a-f0-9]{64}',binding_sha256):raise ValueError('INVALID_WHOLE_RESOURCE_DEFERRAL')
        HubError.__init__(self,'WAITING_RESOURCE');self.resource='disk';self.required_bytes=required_free_bytes;self.binding_sha256=binding_sha256


def transfer_plan(whole):
    whole=validate_whole_replay_plan(whole,whole['planning_policy']);value=deepcopy(whole['source_plan_basis'])
    value.update(version='youtube-whole-transfer-plan-v4',planning_policy=whole['planning_policy'],whole_budget=whole['whole_budget'],
        whole_plan_sha256=digest(whole),required_working_bytes=whole['whole_budget']['required_working_bytes'])
    return value
def validate_transfer_plan(value):
    from .replay_multipart_v4 import _assemble
    try:
        policy=validate_planning_policy(value['planning_policy']);basis={k:deepcopy(v) for k,v in value.items() if k not in ('planning_policy','whole_budget','whole_plan_sha256')}
        basis.update(version='youtube-whole-replay-source-basis-v4',required_working_bytes=basis['source_bytes']+basis['planned_output_max_bytes']+32_000_000)
        whole=_assemble(basis,policy);validate_whole_replay_plan(whole,policy)
        if value!=transfer_plan(whole):raise ValueError()
        return deepcopy(value)
    except HubError:raise
    except (KeyError,TypeError,ValueError,OverflowError):fail('YOUTUBE_WHOLE_TRANSFER_PLAN_CHANGED')


class YouTubeWholeReplayDownload:
    JOB_TYPE=DOWNLOAD_JOB
    METADATA_ONLY=False
    def __init__(self,settings,store,*,downloader_factory=None,http=None):
        self.settings,self.store=settings,store;self.downloader_factory=downloader_factory;self.http=http
        self.fifo=ReplayFIFOV4(settings,store)

    def policy(self):
        try:
            if self.settings.config.get('youtube_whole_replay_download_enabled',False) is not True:fail('YOUTUBE_WHOLE_DISABLED')
            if self.settings.config.get('youtube_natural_download_enabled',False) is not False:fail('YOUTUBE_WHOLE_LEGACY_AUTOMATION_ACTIVE')
            f=self.settings.root/'config'/POLICY_FILE;g=self.settings.root/'config'/PLAN_POLICY_FILE
            for p in (f,g):
                if p.resolve()!=p or any(x.is_symlink() for x in (p,*p.parents)) or not p.is_file() or not 0<p.stat().st_size<16384 or os.name=='posix' and p.stat().st_mode&0o022:raise ValueError()
            raw=f.read_bytes();p=tomllib.loads(raw.decode('utf-8-sig'));planning=validate_planning_policy(tomllib.loads(g.read_text('utf-8-sig')))
            if (p['version']!=POLICY_VERSION or p['enabled'] is not True or planning['enabled'] is not True or
                    p['source_scope']!='all_eligible_ended_since_operating_cutoff' or p['planning_policy_sha256']!=digest(planning) or
                    p['session_profile']!='youtube' or p['acquisition_mode']!='owner_accepted_platform_terms_risk' or
                    p['allowed_channel_ids']!=sorted(CHANNELS) or p['operating_not_before']!=planning['source_not_before']):raise ValueError()
            for key,minimum,maximum in (('daily_http_requests',1,20000),('monthly_http_requests',1,300000),('timeout_seconds',30,86400)):
                if type(p[key]) is not int or not minimum<=p[key]<=maximum:raise ValueError()
            for key in ('owner_permission_reference','acquisition_authorization_reference','quality_authorization_reference',
                    'owner_risk_acceptance_reference','session_authorization_reference'):
                if not isinstance(p[key],str) or not 0<len(p[key].strip())<=2048 or p[key].startswith(('REPLACE','UNCONFIGURED')):raise ValueError()
            for key in ('owner_risk_accepted_at','session_authorized_at'):
                instant=datetime.fromisoformat(p[key].replace('Z','+00:00'))
                if instant.tzinfo is None or instant.timestamp()>time.time():raise ValueError()
            return {**p,'_sha256':hashlib.sha256(raw).hexdigest(),'_planning':planning}
        except HubError:raise
        except (OSError,ValueError,KeyError,TypeError,AttributeError):fail('YOUTUBE_WHOLE_POLICY_INVALID')

    def resource_binding(self,current):
        p=self.policy();fp=self.fifo.policy();payload=current['payload'];c=self.fifo.candidate(payload.get('fifo_v4',{}).get('candidate_id'))
        if (current['job_type']!=self.JOB_TYPE or current['policy_version']!=FIFO_VERSION or current['queue']!='video' or
                current['source_platform']!='youtube' or current['source_id']!=c['source_id'] or
                c['policy_sha256']!=fp['_sha256'] or payload['fifo_v4']['policy_sha256']!=fp['_sha256'] or
                payload['fifo_v4']['identity_sha256']!=c['identity_sha256'] or
                c['plan_job_id' if self.METADATA_ONLY else 'download_job_id']!=current['job_id']):fail('YOUTUBE_WHOLE_FIFO_BINDING_CHANGED')
        source_payload=self.fifo.source_payload_for(c,current)
        if self.METADATA_ONLY:
            from .replay_fifo_v4 import observed_duration
            plan=deepcopy(PROVISIONAL);duration=observed_duration(source_payload['duration'])
        else:
            whole=validate_whole_replay_plan(payload['whole_replay_plan'],p['_planning'],official_raw=payload['raw'])
            if payload['whole_replay_plan_sha256']!=digest(whole) or payload['fifo_v4']['plan_sha256']!=digest(whole):fail('YOUTUBE_WHOLE_PLAN_CHANGED')
            plan=transfer_plan(whole);duration=whole['duration']
        return {'version':VERSION,'job_id':current['job_id'],'video_id':c['source_id'],'channel_id':c['channel_id'],
            'source_payload_sha256':digest(payload),'metadata_sha256':digest(source_payload['raw']),'duration':duration,
            'source_plan':plan,'planning_policy':p['_planning'],'policy_sha256':p['_sha256'],'fifo_policy_sha256':fp['_sha256'],
            **({} if self.METADATA_ONLY else {'whole_replay_plan':whole}),
            'official_identity_sha256':c['identity_sha256'],'official_raw':source_payload['raw'],
            'max_bytes':plan.get('planned_output_max_bytes',8388608),'max_source_bytes':plan['source_bytes'],
            'max_output_bytes':plan.get('planned_output_max_bytes',8388608),'max_height':1080,'timeout_seconds':min(900,p['timeout_seconds']) if self.METADATA_ONLY else p['timeout_seconds'],
            'request_mode':'fifo-v4','authenticated_session':True,'session_profile':'youtube','session_authorization_sha256':p['_sha256'],
            'transport_budget':{'provider':'youtube_download_http','daily_requests':p['daily_http_requests'],
                'monthly_requests':p['monthly_http_requests'],'history_reset':False}}

    def _binding(self,job):
        self.fifo.assert_job_binding(job,require_fresh=False);current=self.store.get(job['job_id']);return current,self.resource_binding(current)
    def resource_binding_sha(self,job):return digest(self.resource_binding(job))
    def check_binding(self,job,binding):
        if self._binding(job)[1]!=binding:fail('YOUTUBE_WHOLE_BINDING_CHANGED')
    def check_media_start(self,job,binding):
        self.check_binding(job,binding)
        if not 0<=time.time()-self._row(job['source_id'])['last_api_check']<=300:
            raise HubError('YOUTUBE_WHOLE_FRESH_SOURCE_REQUIRED',retryable=True,retry_after=15)
    def _row(self,ident):
        rows=self.store.query('SELECT * FROM youtube_videos WHERE video_id=?',(ident,))
        if len(rows)!=1:fail('YOUTUBE_WHOLE_OFFICIAL_SOURCE_REQUIRED')
        return rows[0]

    def fresh_observation(self,job,binding):
        """At most one real official refresh; never update time without a response."""
        ident=binding['video_id'];fp=self.fifo.policy();row=self._row(ident)
        c=self.fifo.candidate(job['payload']['fifo_v4']['candidate_id'])
        self.fifo.observed_identity(c,row,fp)
        if binding['official_identity_sha256']!=c['identity_sha256']:fail('YOUTUBE_WHOLE_OFFICIAL_SOURCE_CHANGED')
        requested=False
        if not 0<=time.time()-row['last_api_check']<=300:
            key=os.getenv('YOUTUBE_API_KEY')
            if not key:fail('YOUTUBE_CREDENTIAL_REQUIRED')
            http=self.http
            if http is None:
                from adapters.http import ProviderHTTP
                from .budget import Budget
                http=ProviderHTTP(Budget(self.store,self.settings.config['budgets']))
            value=http.request('youtube','GET','https://www.googleapis.com/youtube/v3/videos',
                params={'key':key,'part':'snippet,contentDetails,liveStreamingDetails,status','id':ident},job_id=job['job_id'])
            self.check_binding(job,binding);items=value.get('items') if isinstance(value,dict) else None
            if not isinstance(items,list) or len(items)!=1:fail('YOUTUBE_WHOLE_OFFICIAL_SOURCE_UNAVAILABLE')
            raw=items[0];observed=time.time();new={**row,'metadata':encode(raw),'last_api_check':observed}
            try:new.update(title=raw['snippet']['title'],published_at=raw['snippet']['publishedAt'],duration=raw['contentDetails']['duration'],
                actual_start=raw['liveStreamingDetails']['actualStartTime'],actual_end=raw['liveStreamingDetails']['actualEndTime'])
            except (KeyError,TypeError):fail('YOUTUBE_WHOLE_OFFICIAL_SOURCE_CHANGED')
            self.fifo.observed_identity(c,new,fp)
            with self.store.transaction() as db:
                db.execute('UPDATE youtube_videos SET metadata=?,last_api_check=?,title=?,published_at=?,duration=?,actual_start=?,actual_end=? WHERE video_id=?',
                    (new['metadata'],observed,new['title'],new['published_at'],new['duration'],new['actual_start'],new['actual_end'],ident))
            row=new;requested=True
        if not 0<=time.time()-row['last_api_check']<=300:fail('YOUTUBE_WHOLE_FRESH_SOURCE_REQUIRED')
        from adapters.ytdlp_whole_v4 import folder
        area=folder(self.settings,job['job_id'],planning=self.METADATA_ONLY);file=area/('official-source-'+str(uuid.uuid4())+'.json')
        body={'version':'youtube-official-acquisition-source-v4','job_id':job['job_id'],'source_id':ident,
            'observed_at':row['last_api_check'],'official_raw':json.loads(row['metadata']),'identity_sha256':binding['official_identity_sha256'],
            'new_api_request':requested,'human_review':False,'publication_authorized':False}
        observation=self.fifo.observed_identity(c,row,fp)[2]
        if observation['duration_delta_seconds']!=0:body['duration_identity_observation']=observation
        atomic_json(file,body);file.chmod(0o600);raw=file.read_bytes()
        return {'file':str(file),'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'observed_at':row['last_api_check']}

    def asset(self,asset,binding):
        try:
            file=Path(asset['file']);expected=self.settings.root/'media'/binding['job_id']/'youtube-whole-v4/original.mp4'
            if (file!=expected or file.resolve()!=file or any(p.is_symlink() for p in (file,*file.parents)) or not file.is_file() or
                    type(asset['bytes']) is not int or not 0<asset['bytes']<=binding['source_plan']['planned_output_max_bytes'] or
                    file.stat().st_size!=asset['bytes'] or sha256_file(file)!=asset['sha256'] or asset['source_plan']!=binding['source_plan'] or
                    asset['selected_formats']!=binding['source_plan']['selected_formats']):raise ValueError()
            actual=inspect_media(file,include_frame_rates=True)
            if actual['stream_config']!=asset['stream_config'] or abs(actual['duration']-binding['duration'])>2:raise ValueError()
            validate_quality(actual['stream_config']['video'],actual['stream_config']['audio'],binding)
            packets=asset['packet_evidence']
            if set(packets)!={'audio','video'} or any(type(v['packets']) is not int or not 0<v['packets']<=64_000_000 or not re.fullmatch('[a-f0-9]{64}',v['chain']) for v in packets.values()):raise ValueError()
            return asset
        except (OSError,ValueError,KeyError,TypeError,AttributeError):fail('YOUTUBE_WHOLE_ASSET_CHANGED')

    def process(self,job):
        current,binding=self._binding(job);old=current['result'].get(STATE)
        if old and old.get('binding')!=binding:fail('YOUTUBE_WHOLE_BINDING_CHANGED')
        def check():self.check_binding(job,binding)
        from adapters.ytdlp_whole_v4 import WholeReplayDownloader,folder,needed_disk
        reusable=('verified','parts_verified')+ (('single_output_verified_v5',) if binding['planning_policy'].get('prefer_single_whole_passthrough_v5') is True else ())
        if old and old.get('state') in reusable:
            asset=self.asset(old['source_asset'],binding);state=deepcopy(old)
        else:
            observation=self.fresh_observation(job,binding);check()
            state={'version':VERSION,'state':'acquiring','binding':binding,'official_observation':observation,'human_review':False,'publication_authorized':False}
            self.store.checkpoint(job,'youtube_whole_acquiring',{**job['result'],STATE:state})
            try:acquired=(self.downloader_factory or WholeReplayDownloader)(self.settings).acquire(job,binding,check=check)
            except HubError as exc:
                if exc.code=='DISK_LOW':raise WholeResourceDeferredV4(required_free_bytes=needed_disk(folder(self.settings,job['job_id']),binding)+binding['planning_policy']['disk_floor_bytes'],binding_sha256=digest(binding)) from None
                if exc.code=='API_BUDGET_EXCEEDED':
                    now=datetime.now(timezone.utc);due=(now+timedelta(days=1)).replace(hour=0,minute=0,second=5,microsecond=0)
                    raise HubError(exc.code,retryable=True,retry_after=due.timestamp()-time.time()) from None
                raise
            check();asset=self.asset(acquired['source_asset'],binding);receipt=acquired['receipt']
            if (acquired.get('version')!=VERSION or acquired.get('binding_sha256')!=digest(binding) or
                    any(acquired.get(k) is not False for k in ('human_review','publication_authorized','creator_original_bytes_verified')) or
                    receipt.get('source_plan')!=binding['source_plan'] or receipt.get('source_stream_config')!=asset['stream_config'] or
                    receipt.get('source_to_merged_packet_coverage_verified') is not True):fail('YOUTUBE_WHOLE_RECEIPT_INVALID')
            state.update(state='verified',source_asset=asset,receipt=receipt,verified_at=datetime.now(timezone.utc).isoformat())
            self.store.checkpoint(job,'youtube_whole_verified',{**job['result'],STATE:state,'download_performed':True,'publish_performed':False})
        if binding['planning_policy'].get('raw_track_release_after_verified_merge') is True:
            from .verified_track_release_v4 import VerifiedTrackReleaseV4
            # Mandatory before split for this explicitly smaller resource model.
            # A failed release never falls back to keeping three live copies.
            try:VerifiedTrackReleaseV4(self.settings,self.store).run(job,binding,check=check)
            except HubError as exc:
                if exc.code!='DISK_LOW':raise
                floor=max(binding['planning_policy']['disk_floor_bytes'],int(self.settings.config['disk_floor_gb']*1_000_000_000))
                raise WholeResourceDeferredV4(required_free_bytes=floor+binding['planning_policy']['workspace_overhead_bytes'],binding_sha256=digest(binding)) from None
            refreshed=self.store.get(job['job_id']);job['result']=refreshed['result'];state=deepcopy(refreshed['result'][STATE])
        v5=binding['planning_policy'].get('prefer_single_whole_passthrough_v5') is True
        if v5:
            from .replay_delivery_v5 import ReplayDeliveryV5
            processor=ReplayDeliveryV5(self.settings,self.store,policy=binding['planning_policy'],plan=job['payload']['whole_replay_plan'],check=check)
        else:processor=MultipartMediaProcessingV4(self.settings,self.store,policy=binding['planning_policy'],plan=job['payload']['whole_replay_plan'],check=check)
        try:parts=processor.run(job,asset)
        except HubError as exc:
            if exc.code!='DISK_LOW':raise
            budget=job['payload']['whole_replay_plan']['whole_budget']
            required=budget['processing_required_free_bytes'] if v5 else budget['split_required_free_bytes']
            raise WholeResourceDeferredV4(required_free_bytes=required,binding_sha256=digest(binding)) from None
        if v5:
            state.update(state='single_output_verified_v5' if parts['processing_model']=='single_whole_passthrough_v5' else 'parts_verified',
                delivery_v5=parts,publishable=False,publication_authorized=False)
        else:state.update(state='parts_verified',parts=parts,publishable=False,publication_authorized=False)
        result={**job['result'],STATE:state,'download_performed':True,'publish_performed':False,'publication_authorized':False,'human_review':False}
        if v5:result=json.loads(encode(result))
        self.store.checkpoint(job,'youtube_whole_parts_verified',result);return result


class YouTubeWholeReplayPrepare(YouTubeWholeReplayDownload):
    JOB_TYPE=PLAN_JOB
    METADATA_ONLY=True
    def _pin_planning_source(self,job):
        from .replay_fifo_v4 import PLANNING_SOURCE
        self.fifo.assert_job_binding(job,require_fresh=False);current=self.store.get(job['job_id'])
        c=self.fifo.candidate(current['payload']['fifo_v4']['candidate_id'])
        if PLANNING_SOURCE in current['result']:
            self.fifo.source_payload_for(c,current);return
        row=self.fifo._fresh(c,self.fifo.policy())
        _,_,proof=self.fifo.observed_identity(c,row,self.fifo.policy())
        if proof['duration_delta_seconds']==0:return
        # Only an unplanned metadata job may select its first real planning raw.
        # Keep the original payload and registration hashes, and pin this whole
        # official response rather than inventing an updated duration in it.
        if current['result'].get('replay_fifo_plan_v4') or current['result'].get('youtube_readiness_v4'):
            fail('REPLAY_FIFO_PLANNING_SOURCE_CHANGED')
        self.store.checkpoint(job,current['stage'],{**current['result'],PLANNING_SOURCE:proof})
    def process(self,job):
        current,binding=self._binding(job)
        if current['result'].get('youtube_readiness_v4',{}).get('state')=='waiting_readiness':fail('YOUTUBE_WHOLE_READINESS_COORDINATOR_REQUIRED')
        observation=self.fresh_observation(job,binding)
        self._pin_planning_source(job)
        current,binding=self._binding(job)
        def check():self.check_binding(job,binding)
        from adapters.ytdlp_whole_v4 import WholeReplayPlanner
        try:record=(self.downloader_factory or WholeReplayPlanner)(self.settings).acquire(job,binding,check=check)
        except HubError as exc:
            if exc.code in ('YOUTUBE_NATURAL_POST_LIVE_NOT_READY','YOUTUBE_NATURAL_EXACT_SIZE_NOT_READY'):
                from .youtube_readiness_v4 import read_failed_observation,WholeReadinessDeferredV4
                reference=read_failed_observation(self.settings,self.store,current,binding,exc.code)
                raise WholeReadinessDeferredV4(exc.code,reference) from None
            raise
        check()
        if (record.get('version')!=VERSION or record.get('binding_sha256')!=digest(binding) or
                any(record.get(k) is not False for k in ('media_downloaded','human_review','publication_authorized'))):fail('YOUTUBE_WHOLE_PLAN_RECEIPT_INVALID')
        plan=validate_whole_replay_plan(record['source_plan'],binding['planning_policy'],official_raw=binding['official_raw'])
        observation=self.fresh_observation(job,binding)
        result=self.fifo.record_plan(job,plan,binding['planning_policy'])
        result['youtube_whole_plan_observation_v4']={'official_observation':observation,'receipt':record,'publishable':False}
        self.store.checkpoint(job,'youtube_whole_planned',result);return result


def worker_handlers(settings,store):
    from .youtube_readiness_v4 import WholeReadinessObservationV4,JOB_TYPE as READINESS_JOB
    return {PLAN_JOB:YouTubeWholeReplayPrepare(settings,store).process,DOWNLOAD_JOB:YouTubeWholeReplayDownload(settings,store).process,
        READINESS_JOB:WholeReadinessObservationV4(settings,store).process}
def resource_validators(settings,store):
    services=(YouTubeWholeReplayPrepare(settings,store),YouTubeWholeReplayDownload(settings,store))
    return {s.JOB_TYPE:s.resource_binding_sha for s in services}
def resource_callbacks(settings,store):
    fifo=ReplayFIFOV4(settings,store)
    return {name:lambda job,exc:fifo.resource_defer(job,required_free_bytes=exc.required_bytes,binding_sha256=exc.binding_sha256) for name in (PLAN_JOB,DOWNLOAD_JOB)}
def readiness_callbacks(settings,store):
    from .youtube_readiness_v4 import WholeReadinessCoordinatorV4
    return WholeReadinessCoordinatorV4(settings,store).callbacks()
