"""Natural ended replays: persisted metadata planning before bounded download.

Absent/off switches create no jobs and make no request. Existing download,
publication and partial records across all earlier versions remain untouched.
"""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
from fractions import Fraction
import hashlib,json,math,os,re,time,tomllib,uuid
from pathlib import Path
from zoneinfo import ZoneInfo

from .db import encode
from .errors import HubError,ResourceDeferred,YouTubeReadinessDeferred
from .evidence import sha256_file
from .maintenance import Maintenance
from .youtube_download import CHANNELS,digest,timestamp,duration_seconds
from .youtube_natural_plan_v3 import natural_source_plan,validate_natural_plan,stamp
from .youtube_highest_plan_v2 import MAX_BYTES,OPERATING_NOT_BEFORE,require_operating_date

VERSION='youtube-replay-highest-natural-download-v3'
POLICY_VERSION='youtube-highest-natural-download-policy-v3'
POLICY_ID='youtube-highest-natural-download-v3'
JOB_TYPE='youtube_download_v3'
PREPARE_JOB_TYPE='youtube_highest_prepare_v3'
STATE='youtube_download_v3'
PREPARE_STATE='youtube_highest_prepare_v3'
POLICY_FILE='youtube-highest-natural-download-policy.toml'
QUALITY='highest-height-fps-h264-aac-stream-copy-v1'
PROVISIONAL_PLAN={'version':'youtube-natural-metadata-only-plan-v3','source_bytes':8388608,
    'required_working_bytes':32000000,'max_http_requests':64}


def fail(code):raise HubError(code,review=True)


def official_identity(raw):
    """Stable source facts, without mutable title/description/viewer counters."""
    try:
        ident=raw['id'];channel=raw['snippet']['channelId'];live=raw['liveStreamingDetails']
        start=require_operating_date(live.get('actualStartTime'));ended=stamp(live.get('actualEndTime'))
        if (not isinstance(ident,str) or not re.fullmatch('[A-Za-z0-9_-]{11}',ident) or channel not in CHANNELS or
                raw['status']['privacyStatus']!='public' or ended<stamp(start) or ended.timestamp()>time.time()+30):raise ValueError()
        return {'version':'youtube-official-stable-identity-v3','video_id':ident,'channel_id':channel,'privacy_status':'public',
            'actual_start':start,'actual_end':ended.isoformat().replace('+00:00','Z'),
            'duration':duration_seconds(raw['contentDetails']['duration'])}
    except HubError:raise
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):fail('YOUTUBE_NATURAL_OFFICIAL_BINDING_REQUIRED')


def validate_quality(video,audio,binding):
    selected=binding['source_plan']['selected_formats'][0]
    try:
        if (video['codec_name']!='h264' or audio['codec_name']!='aac' or video['width']!=selected['width'] or
                video['height']!=selected['height'] or any(
                    abs(float(Fraction(video[name]))-selected['fps'])>max(.01,selected['fps']*.0011)
                    for name in ('avg_frame_rate','r_frame_rate'))):raise ValueError()
    except (ValueError,KeyError,TypeError,ZeroDivisionError):fail('YOUTUBE_NATURAL_QUALITY_CHANGED')


class YouTubeNaturalDownload:
    JOB_TYPE=JOB_TYPE
    STATE=STATE
    METADATA_ONLY=False

    def __init__(self,settings,store,*,downloader_factory=None):
        self.settings,self.store,self.downloader_factory=settings,store,downloader_factory

    def policy(self):
        flag=self.settings.config.get('youtube_natural_download_enabled',False)
        if type(flag) is not bool:fail('YOUTUBE_NATURAL_POLICY_INVALID')
        if not flag:fail('YOUTUBE_NATURAL_DISABLED')
        file=self.settings.root/'config'/POLICY_FILE
        try:
            if file.is_symlink() or not file.is_file() or file.stat().st_size>16384 or os.name=='posix' and file.stat().st_mode&0o022:raise ValueError()
            raw=file.read_bytes();p=tomllib.loads(raw.decode('utf-8-sig'))
            if p.get('enabled') is False:fail('YOUTUBE_NATURAL_DISABLED')
            if (p.get('enabled') is not True or p.get('version')!=POLICY_VERSION or p.get('policy_id')!=POLICY_ID or
                    p.get('source_scope')!='natural_ended_after_activation' or p.get('quality_profile')!=QUALITY or
                    p.get('operating_not_before')!=OPERATING_NOT_BEFORE or p.get('session_profile')!='youtube' or
                    p.get('acquisition_mode')!='owner_accepted_platform_terms_risk' or
                    p.get('allowed_channel_ids')!=sorted(CHANNELS) or
                    {s['youtube_channel_id'] for s in self.settings.config['sources']}!=CHANNELS or
                    any(type(p.get(name)) is not int or p[name]!=value for name,value in
                        (('max_source_bytes',MAX_BYTES),('max_output_bytes',MAX_BYTES),('max_http_requests',2048))) or
                    type(p.get('daily_http_requests')) is not int or not 1<=p['daily_http_requests']<=6000 or
                    type(p.get('monthly_http_requests')) is not int or not 1<=p['monthly_http_requests']<=150000 or
                    type(p.get('timeout_seconds')) is not int or not 30<=p['timeout_seconds']<=21600 or
                    type(p.get('max_new_replays_per_day')) is not int or not 1<=p['max_new_replays_per_day']<=4):raise ValueError()
            for name in ('owner_permission_reference','acquisition_authorization_reference','quality_authorization_reference',
                    'owner_risk_acceptance_reference','session_authorization_reference','activation_reference'):
                if not isinstance(p.get(name),str) or not p[name].strip() or len(p[name])>2048 or p[name].startswith(('REPLACE','UNCONFIGURED')):raise ValueError()
            for name in ('activated_at','session_authorized_at','owner_risk_accepted_at'):
                if timestamp(p.get(name))>time.time():raise ValueError()
            return {**p,'_sha256':hashlib.sha256(raw).hexdigest(),'_activation_epoch':timestamp(p['activated_at'])}
        except HubError:raise
        except (OSError,ValueError,KeyError,TypeError,AttributeError,OverflowError):fail('YOUTUBE_NATURAL_POLICY_INVALID')

    def metadata(self,payload,policy):
        try:
            raw=payload['raw'];identity=official_identity(raw);ident=identity['video_id']
            if (payload.get('video_id')!=ident or payload.get('channel_id')!=identity['channel_id'] or
                    payload['channel_id'] not in policy['allowed_channel_ids'] or payload.get('source_api')!='youtube-data-v3-official' or
                    payload.get('url')!='https://www.youtube.com/watch?v='+ident or raw['snippet']['title']!=payload.get('title') or
                    raw['snippet']['publishedAt']!=payload.get('published_at') or raw['contentDetails']['duration']!=payload.get('duration')):
                raise ValueError()
            rows=self.store.query('SELECT * FROM youtube_videos WHERE video_id=?',(ident,))
            if len(rows)!=1:raise ValueError()
            row=rows[0];latest=json.loads(row['metadata'])
            if (row['source_api']!='youtube-data-v3-official' or row['state']!='ended' or not row['last_api_check'] or
                    row['channel_id']!=identity['channel_id'] or official_identity(latest)!=identity or
                    duration_seconds(row['duration'])!=identity['duration'] or
                    require_operating_date(row['actual_start'])!=identity['actual_start'] or
                    stamp(row['actual_end'])!=stamp(identity['actual_end']) or
                    row['title']!=latest['snippet']['title'] or row['published_at']!=latest['snippet']['publishedAt']):raise ValueError()
            ended=stamp(identity['actual_end']).timestamp()
            if ended<policy['_activation_epoch']:fail('YOUTUBE_NATURAL_HISTORICAL_SOURCE_REJECTED')
            if identity['duration']>36000:fail('YOUTUBE_NATURAL_DURATION_UNSUPPORTED')
            return {'video_id':ident,'channel_id':identity['channel_id'],'duration':identity['duration'],
                'metadata_sha256':digest(raw),'actual_end_epoch':ended,'source_actual_start_time':identity['actual_start'],
                'official_identity':identity,'official_identity_sha256':digest(identity),
                'latest_official_metadata_sha256':digest(latest),'latest_official_observed_at':row['last_api_check']}
        except HubError:raise
        except (ValueError,KeyError,TypeError,AttributeError,OverflowError):fail('YOUTUBE_NATURAL_OFFICIAL_BINDING_REQUIRED')

    def _other_source(self,db,video_id,*,except_prepare=None):
        if db.execute("SELECT 1 FROM publications WHERE source_platform='youtube' AND source_id=?",(video_id,)).fetchone():
            fail('YOUTUBE_NATURAL_PUBLICATION_RECONCILIATION_REQUIRED')
        rows=db.execute("SELECT job_id,job_type,state,result FROM jobs WHERE source_platform='youtube' AND source_id=?",(video_id,)).fetchall()
        for row in rows:
            if row['job_id']==except_prepare:continue
            result=json.loads(row['result'])
            if row['job_type'] not in ('youtube_download','youtube_download_v2',JOB_TYPE,PREPARE_JOB_TYPE):continue
            if row['state'] in ('pending','processing','retry_scheduled','completed','publish_unknown','awaiting_review','awaiting_moderation') or any(
                    isinstance(result.get(name),dict) and result[name].get('state')=='verified'
                    for name in ('youtube_download_v1','youtube_download_v2',STATE)):
                fail('YOUTUBE_NATURAL_SOURCE_ALREADY_PROCESSING_OR_PROCESSED')

    def enqueue_if_eligible(self,payload):
        if self.settings.config.get('youtube_natural_download_enabled',False) is False:return None
        try:policy=self.policy()
        except HubError as exc:
            if exc.code=='YOUTUBE_NATURAL_DISABLED':return None
            raise
        try:metadata=self.metadata(payload,policy)
        except HubError as exc:
            if exc.code in ('YOUTUBE_NATURAL_HISTORICAL_SOURCE_REJECTED','YOUTUBE_HIGHEST_BEFORE_OPERATING_START'):return None
            raise
        ident=metadata['video_id']
        row=self.store.query('SELECT last_api_check FROM youtube_videos WHERE video_id=?',(ident,))[0]
        if not row['last_api_check'] or not 0<=time.time()-row['last_api_check']<=300:fail('YOUTUBE_NATURAL_FRESH_OBSERVATION_REQUIRED')
        requested={**deepcopy(payload),'_natural_policy_sha256':policy['_sha256'],'_natural_request_mode':'natural'}
        with self.store.transaction() as db:
            old=db.execute("SELECT job_id,payload FROM jobs WHERE source_platform='youtube' AND source_id=? AND job_type=? AND policy_version=?",
                (ident,PREPARE_JOB_TYPE,POLICY_ID)).fetchone()
            if old:
                previous=json.loads(old['payload'])
                if (previous.get('_natural_policy_sha256')!=policy['_sha256'] or previous.get('_natural_request_mode')!='natural' or
                        official_identity(previous.get('raw'))!=metadata['official_identity']):
                    fail('YOUTUBE_NATURAL_EXISTING_BINDING_CHANGED')
                return old['job_id']
            self._other_source(db,ident)
            day=datetime.now(ZoneInfo('Asia/Shanghai')).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
            count=db.execute('SELECT COUNT(*) FROM jobs WHERE job_type=? AND created_at>=?',(PREPARE_JOB_TYPE,day)).fetchone()[0]
            if count>=policy['max_new_replays_per_day']:return None
            new=str(uuid.uuid4());now=time.time()
            db.execute("""INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,policy_version,payload)
                VALUES(?,'youtube',?,?,'video','pending',?,?,?,?)""",(new,ident,PREPARE_JOB_TYPE,now,now,POLICY_ID,encode(requested)))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                (now,'system:youtube-natural-v3','youtube_natural_prepare_enqueue',new,'fresh_official_natural_only_no_media_or_publication'))
            return new

    def _binding(self,job):
        current=self.store.get(job['job_id'])
        if (not current or current['job_type']!=self.JOB_TYPE or current['source_platform']!='youtube' or
                current['policy_version']!=POLICY_ID or current['queue']!='video' or current['state']!='processing' or
                not job.get('lease_owner') or current['lease_owner']!=job['lease_owner'] or
                (current.get('lease_until') or 0)<=time.time() or current['payload']!=job['payload'] or
                not 1<=current['attempts']<=min(4,self.settings.config['max_attempts'])):fail('YOUTUBE_NATURAL_LEASE_OR_INPUT_CHANGED')
        return current,self.resource_binding(current)

    def resource_binding(self,current):
        p=self.policy()
        if (current['job_type']!=self.JOB_TYPE or current['source_platform']!='youtube' or current['policy_version']!=POLICY_ID or
                current['queue']!='video'):fail('YOUTUBE_NATURAL_LEASE_OR_INPUT_CHANGED')
        payload=current['payload'];m=self.metadata(payload,p)
        if current['source_id']!=m['video_id'] or payload.get('_natural_policy_sha256')!=p['_sha256'] or payload.get('_natural_request_mode')!='natural':
            fail('YOUTUBE_NATURAL_POLICY_CHANGED')
        if self.METADATA_ONLY:plan=deepcopy(PROVISIONAL_PLAN)
        else:
            plan=validate_natural_plan(payload.get('_natural_source_plan'),official_raw=payload['raw'])
            if plan['activated_at']!=stamp(p['activated_at']).isoformat().replace('+00:00','Z'):fail('YOUTUBE_NATURAL_POLICY_CHANGED')
            prior=self.store.get(payload.get('_natural_prepare_job_id'))
            if (not prior or prior['job_type']!=PREPARE_JOB_TYPE or prior['state']!='completed' or
                    prior['source_id']!=m['video_id'] or prior['result'].get(PREPARE_STATE,{}).get('source_plan')!=plan):
                fail('YOUTUBE_NATURAL_PREPARATION_REQUIRED')
        return {'version':VERSION,'job_id':current['job_id'],'video_id':m['video_id'],'channel_id':m['channel_id'],
            'metadata_sha256':m['metadata_sha256'],'source_payload_sha256':digest(payload),'duration':m['duration'],
            'official_identity':m['official_identity'],'official_identity_sha256':m['official_identity_sha256'],
            'max_bytes':MAX_BYTES,'max_source_bytes':MAX_BYTES,'max_output_bytes':MAX_BYTES,'max_height':1080,
            'timeout_seconds':min(900,p['timeout_seconds']) if self.METADATA_ONLY else p['timeout_seconds'],
            'policy_sha256':p['_sha256'],'request_mode':'natural','authenticated_session':True,'session_profile':'youtube',
            'session_authorization_sha256':p['_sha256'],'quality_profile':QUALITY,'source_plan':plan,
            'operating_not_before':OPERATING_NOT_BEFORE,'source_actual_start_time':m['source_actual_start_time'],
            'activated_at':p['activated_at'],'official_raw':payload['raw'],
            'transport_budget':{'provider':'youtube_download_http','daily_requests':p['daily_http_requests'],
                'monthly_requests':p['monthly_http_requests'],'history_reset':False}}

    def resource_binding_sha(self,job):return digest(self.resource_binding(job))

    @staticmethod
    def resource_resume_guard(current):
        waiting=current['result'].get('resource_wait_v1',{})
        if waiting.get('state')=='waiting_resource':
            fail('RESOURCE_WAIT_TIMEOUT' if time.time()>=waiting.get('deadline',0) else 'RESOURCE_WAIT_COORDINATOR_REQUIRED')

    def check_disk_or_wait(self,job,binding,required):
        try:return Maintenance(self.settings,self.store).check_disk(required)
        except HubError as exc:
            if exc.code!='DISK_LOW':raise
            raise ResourceDeferred(resource='disk',required_bytes=required,binding_sha256=digest(binding)) from None

    def check_binding(self,job,binding):
        if self._binding(job)[1]!=binding:fail('YOUTUBE_NATURAL_POLICY_CHANGED')

    def latest_observation(self,payload):
        record=self.metadata(payload,self.policy())
        return {'metadata_sha256':record['latest_official_metadata_sha256'],
            'observed_at':record['latest_official_observed_at'],'official_identity_sha256':record['official_identity_sha256'],
            'original_metadata_preserved':True,'source_identity_unchanged':True}

    def acquire(self,factory,job,binding,check):
        try:return factory(self.settings).acquire(job,binding,check=check)
        except HubError as exc:
            if self.JOB_TYPE==PREPARE_JOB_TYPE and exc.code in ('YOUTUBE_NATURAL_POST_LIVE_NOT_READY','YOUTUBE_NATURAL_EXACT_SIZE_NOT_READY'):
                from .youtube_readiness_v3 import read_failed_observation
                raise YouTubeReadinessDeferred(exc.code,read_failed_observation(self.settings,job,binding,exc.code)) from None
            if exc.code=='DISK_LOW':
                required=binding['source_plan']['required_working_bytes']
                if self.JOB_TYPE==JOB_TYPE:
                    from adapters.ytdlp_natural_v3 import folder
                    from adapters.ytdlp_highest_v2 import needed_disk
                    required=needed_disk(folder(self.settings,job['job_id']),binding)
                raise ResourceDeferred(resource='disk',required_bytes=required,binding_sha256=digest(binding)) from None
            if exc.code!='API_BUDGET_EXCEEDED':raise
            # Resume through the ordinary finite-attempt Worker after the real
            # quota boundary. No reservation, history or attempt is refunded.
            now=datetime.now(ZoneInfo('Asia/Shanghai'));month=now.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
            used=self.store.query('SELECT COALESCE(SUM(units),0) n FROM api_calls WHERE provider=? AND started_at>=?',
                ('youtube_download_http',month.timestamp()))[0]['n']
            required=binding['source_plan']['max_http_requests']
            due=((month.replace(year=month.year+1,month=1) if month.month==12 else month.replace(month=month.month+1))
                if used+required>binding['transport_budget']['monthly_requests'] else now.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1))
            raise HubError(exc.code,retryable=True,retry_after=max(1,due.timestamp()-time.time()+5)) from None

    def asset(self,asset,binding):
        try:
            path=Path(asset['file']);expected=self.settings.root/'media'/binding['job_id']/'youtube-highest-v3/original.mp4'
            if (path!=expected or not path.is_absolute() or path.resolve()!=path or any(p.is_symlink() for p in (path,*path.parents)) or
                    not path.is_file() or type(asset['bytes']) is not int or not 0<asset['bytes']<=binding['source_plan']['planned_output_max_bytes'] or
                    path.stat().st_size!=asset['bytes'] or sha256_file(path)!=asset['sha256'] or
                    type(asset['duration']) not in (int,float) or not math.isfinite(asset['duration']) or abs(asset['duration']-binding['duration'])>2 or
                    sorted(asset['stream_types'].values())!=['audio','video'] or asset['source_plan']!=binding['source_plan'] or
                    asset['selected_formats']!=binding['source_plan']['selected_formats']):raise ValueError()
            validate_quality(asset['stream_config']['video'],asset['stream_config']['audio'],binding)
            packets=asset['packet_evidence']
            if set(packets)!={'audio','video'} or any(type(v['packets']) is not int or v['packets']<1 or not re.fullmatch('[a-f0-9]{64}',v['chain']) for v in packets.values()):raise ValueError()
            return asset
        except (OSError,ValueError,KeyError,TypeError,AttributeError):fail('YOUTUBE_NATURAL_ASSET_CHANGED')

    def process(self,job):
        current,binding=self._binding(job);old=current['result'].get(STATE)
        self.resource_resume_guard(current)
        if old and old.get('binding')!=binding:fail('YOUTUBE_NATURAL_POLICY_CHANGED')
        def check():self.check_binding(job,binding)
        if old and old.get('state')=='verified':self.asset(old['source_asset'],binding);check();return current['result']
        state={'version':VERSION,'state':'acquiring','binding':binding,'human_review':False,'publication_authorized':False,
            'latest_official_observation':self.latest_observation(current['payload'])}
        self.store.checkpoint(job,'youtube_natural_acquiring',{**current['result'],STATE:state})
        factory=self.downloader_factory
        if factory is None:
            from adapters.ytdlp_natural_v3 import NaturalReplayDownloader
            factory=NaturalReplayDownloader
        acquired=self.acquire(factory,job,binding,check);check()
        asset=self.asset(acquired['source_asset'],binding);receipt=acquired['receipt']
        if (acquired.get('version')!=VERSION or acquired.get('binding_sha256')!=digest(binding) or
                any(acquired.get(k) is not False for k in ('human_review','publication_authorized','creator_original_bytes_verified')) or
                not isinstance(receipt,dict) or len(encode(receipt).encode())>262144 or receipt.get('source_plan')!=binding['source_plan'] or
                receipt.get('selected_formats')!=binding['source_plan']['selected_formats'] or receipt.get('source_stream_config')!=asset['stream_config'] or
                receipt.get('source_to_merged_packet_coverage_verified') is not True):fail('YOUTUBE_NATURAL_RECEIPT_INVALID')
        state.update(state='verified',source_asset=asset,receipt=receipt,verified_at=datetime.now(timezone.utc).isoformat(),
            latest_official_observation=self.latest_observation(current['payload']))
        result={**current['result'],STATE:state,'disposition':'natural_highest_download_verified_v3','download_performed':True,
            'publish_performed':False,'publication_authorized':False,'human_review':False}
        self.store.checkpoint(job,'youtube_natural_verified',result);return result


class YouTubeNaturalPrepare(YouTubeNaturalDownload):
    JOB_TYPE=PREPARE_JOB_TYPE
    STATE=PREPARE_STATE
    METADATA_ONLY=True

    def process(self,job):
        current,binding=self._binding(job)
        self.resource_resume_guard(current)
        readiness=current['result'].get('youtube_readiness_v3',{})
        if readiness.get('state')=='waiting_readiness':
            raise HubError('YOUTUBE_READINESS_TIMEOUT' if time.time()>=readiness.get('deadline',0) else 'YOUTUBE_READINESS_COORDINATOR_REQUIRED')
        def check():self.check_binding(job,binding)
        prior=current['result'].get(PREPARE_STATE)
        if prior:
            if prior.get('binding')!=binding:fail('YOUTUBE_NATURAL_POLICY_CHANGED')
            if prior.get('state')=='planned':
                plan=validate_natural_plan(prior.get('source_plan'),official_raw=binding['official_raw'])
                child=self.store.get(prior.get('download_job_id'))
                if not child or child['job_type']!=JOB_TYPE or child['payload'].get('_natural_source_plan')!=plan or child['payload'].get('_natural_prepare_job_id')!=job['job_id']:
                    fail('YOUTUBE_NATURAL_PREPARATION_CHANGED')
                check();return current['result']
        cached=bool(prior and prior.get('state')=='metadata_verified')
        if cached:
            acquired=prior['receipt']
            if prior.get('receipt_sha256')!=digest(acquired):fail('YOUTUBE_NATURAL_PLAN_RECEIPT_INVALID')
        else:
            factory=self.downloader_factory
            if factory is None:
                from adapters.ytdlp_natural_v3 import NaturalReplayPlanner
                factory=NaturalReplayPlanner
            acquired=self.acquire(factory,job,binding,check);check()
        if (acquired.get('version')!=VERSION or acquired.get('binding_sha256')!=digest(binding) or
                any(acquired.get(k) is not False for k in ('media_downloaded','human_review','publication_authorized'))):
            fail('YOUTUBE_NATURAL_PLAN_RECEIPT_INVALID')
        plan=validate_natural_plan(acquired.get('source_plan'),official_raw=binding['official_raw'])
        if not cached and not 0<=time.time()-stamp(plan['observed_at']).timestamp()<=300:fail('YOUTUBE_NATURAL_FRESH_OBSERVATION_REQUIRED')
        if plan['max_http_requests']>min(binding['transport_budget']['daily_requests'],binding['transport_budget']['monthly_requests']):
            fail('YOUTUBE_NATURAL_PLAN_EXCEEDS_REQUEST_BUDGET')
        if not cached:
            checkpoint={**current['result'],PREPARE_STATE:{'version':VERSION,'state':'metadata_verified','binding':binding,
                'source_plan':plan,'receipt':acquired,'receipt_sha256':digest(acquired),'metadata_verified_at':time.time(),
                'human_review':False,'publication_authorized':False}}
            self.store.checkpoint(job,'youtube_natural_metadata_verified',checkpoint)
            current=self.store.get(job['job_id'])
        self.check_disk_or_wait(job,binding,plan['required_working_bytes']);check()
        payload={**deepcopy(current['payload']),'_natural_prepare_job_id':job['job_id'],'_natural_source_plan':plan}
        with self.store.transaction() as db:
            # This prepare keeps the video OS lock and its ordinary Worker lease.
            row=db.execute('SELECT * FROM jobs WHERE job_id=?',(job['job_id'],)).fetchone()
            if row['state']!='processing' or row['lease_owner']!=job['lease_owner'] or row['lease_until']<=time.time():
                fail('YOUTUBE_NATURAL_LEASE_OR_INPUT_CHANGED')
            old=db.execute("SELECT job_id,payload FROM jobs WHERE source_platform='youtube' AND source_id=? AND job_type=? AND policy_version=?",
                (current['source_id'],JOB_TYPE,POLICY_ID)).fetchone()
            if old:
                if json.loads(old['payload'])!=payload:fail('YOUTUBE_NATURAL_EXISTING_BINDING_CHANGED')
                child=old['job_id']
            else:
                self._other_source(db,current['source_id'],except_prepare=job['job_id']);child=str(uuid.uuid4());now=time.time()
                db.execute("""INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,policy_version,payload)
                    VALUES(?,'youtube',?,?,'video','pending',?,?,?,?)""",(child,current['source_id'],JOB_TYPE,now,now,POLICY_ID,encode(payload)))
                db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                    (now,'system:youtube-natural-v3','youtube_natural_download_enqueue',child,'real_metadata_inventory_exact_plan_no_media_yet'))
            result={**current['result'],PREPARE_STATE:{'version':VERSION,'state':'planned','binding':binding,'source_plan':plan,
                'receipt':acquired,'download_job_id':child,'human_review':False,'publication_authorized':False},
                'download_performed':False,'publish_performed':False,'human_review':False,'publication_authorized':False}
            db.execute('UPDATE jobs SET result=?,stage=?,updated_at=? WHERE job_id=?',(encode(result),'youtube_natural_planned',time.time(),job['job_id']))
        return result


def worker_handlers(settings,store):
    from .youtube_readiness_v3 import ReadinessObservation,JOB_TYPE as READINESS_JOB_TYPE
    return {PREPARE_JOB_TYPE:YouTubeNaturalPrepare(settings,store).process,JOB_TYPE:YouTubeNaturalDownload(settings,store).process,
        READINESS_JOB_TYPE:ReadinessObservation(settings,store).process}


def resource_coordinator(settings,store):
    from .resource_wait_v1 import ResourceWaitCoordinator
    return ResourceWaitCoordinator(settings,store,validators=resource_wait_validators(settings,store))


def resource_wait_validators(settings,store):
    services=(YouTubeNaturalPrepare(settings,store),YouTubeNaturalDownload(settings,store))
    return {service.JOB_TYPE:service.resource_binding_sha for service in services}
