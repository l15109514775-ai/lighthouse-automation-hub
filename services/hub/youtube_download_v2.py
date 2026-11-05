"""A distinct highest-quality pilot job. Legacy jobs/parts are never mutated."""
from copy import deepcopy
from datetime import datetime,timezone
from fractions import Fraction
from pathlib import Path
import hashlib,json,math,os,re,stat,time,tomllib,uuid

from .db import encode
from .errors import HubError
from .evidence import sha256_file
from .maintenance import Maintenance
from .youtube_download import CHANNELS,YouTubeDownload,digest,timestamp
from .youtube_highest_plan_v2 import VIDEO_ID,QUALITY,MAX_BYTES,OPERATING_NOT_BEFORE,require_operating_date,source_plan

VERSION='youtube-replay-highest-download-v2'
POLICY_VERSION='youtube-highest-download-policy-v2'
POLICY_ID='youtube-highest-download-v2'
JOB_TYPE='youtube_download_v2'
STATE='youtube_download_v2'
POLICY_FILE='youtube-highest-download-policy.toml'


class YouTubeHighestDownload:
    def __init__(self,settings,store,*,downloader_factory=None):
        self.settings,self.store,self.downloader_factory=settings,store,downloader_factory

    def policy(self):
        enabled=self.settings.config.get('youtube_highest_download_enabled',False)
        if type(enabled) is not bool:raise HubError('YOUTUBE_HIGHEST_POLICY_INVALID',review=True)
        if not enabled:raise HubError('YOUTUBE_HIGHEST_DISABLED',review=True)
        file=self.settings.root/'config'/POLICY_FILE
        try:
            if (file.is_symlink() or not file.is_file() or file.stat().st_size>16384 or
                    os.name=='posix' and file.stat().st_mode&0o022):raise ValueError()
            raw=file.read_bytes();p=tomllib.loads(raw.decode('utf-8-sig'))
            if p.get('enabled') is False:raise HubError('YOUTUBE_HIGHEST_DISABLED',review=True)
            if (p.get('enabled') is not True or p.get('version')!=POLICY_VERSION or p.get('policy_id')!=POLICY_ID or
                    p.get('video_id')!=VIDEO_ID or p.get('channel_id') not in CHANNELS or p.get('quality_profile')!=QUALITY or
                    p.get('video_format_id')!='299' or p.get('audio_format_id')!='140' or
                    p.get('session_profile')!='youtube' or p.get('acquisition_mode')!='owner_accepted_platform_terms_risk' or
                    type(p.get('timeout_seconds')) is not int or not 30<=p['timeout_seconds']<=21600 or
                    type(p.get('daily_http_requests')) is not int or p['daily_http_requests']!=2500 or
                    type(p.get('monthly_http_requests')) is not int or p['monthly_http_requests']!=30000):raise ValueError()
            for name in ('owner_permission_reference','acquisition_authorization_reference','quality_authorization_reference',
                    'owner_risk_acceptance_reference','session_authorization_reference'):
                if (not isinstance(p.get(name),str) or not p[name].strip() or len(p[name])>2048 or
                        p[name].startswith(('REPLACE','UNCONFIGURED'))):raise ValueError()
            for name in ('activated_at','session_authorized_at','owner_risk_accepted_at'):
                if timestamp(p.get(name))>time.time():raise ValueError()
            configured={s['youtube_channel_id'] for s in self.settings.config['sources']}
            if configured!=CHANNELS:raise ValueError()
            official=self.store.query('SELECT * FROM youtube_videos WHERE video_id=?',(VIDEO_ID,))
            if (len(official)!=1 or official[0]['source_api']!='youtube-data-v3-official' or
                    official[0]['state']!='ended' or official[0]['channel_id']!=p['channel_id']):
                raise HubError('YOUTUBE_HIGHEST_OFFICIAL_SOURCE_REQUIRED',review=True)
            start=json.loads(official[0]['metadata'])['liveStreamingDetails'].get('actualStartTime')
            plan=source_plan(p['video_bytes'],p['audio_bytes'],max_source_bytes=p['max_source_bytes'],
                max_output_bytes=p['max_output_bytes'],request_cap=p['max_http_requests'],actual_start_time=start)
            return {**p,'allowed_channel_ids':sorted(CHANNELS),'_sha256':hashlib.sha256(raw).hexdigest(),'source_plan':plan}
        except HubError:raise
        except (OSError,ValueError,KeyError,TypeError,AttributeError,OverflowError):
            raise HubError('YOUTUBE_HIGHEST_POLICY_INVALID',review=True) from None

    def metadata(self,payload,policy):
        # Reuse the original official API/SQLite source validation, unchanged.
        value=YouTubeDownload._metadata(self,payload,policy)
        started=require_operating_date(payload.get('raw',{}).get('liveStreamingDetails',{}).get('actualStartTime'))
        if (value['video_id']!=VIDEO_ID or value['channel_id']!=policy['channel_id'] or value['duration']>36000):
            raise HubError('YOUTUBE_HIGHEST_OFFICIAL_SOURCE_REQUIRED',review=True)
        if started!=policy['source_plan']['source_actual_start_time']:
            raise HubError('YOUTUBE_HIGHEST_ACTUAL_START_REQUIRED',review=True)
        return value

    def enqueue_if_eligible(self,payload):
        return None  # All-source automatic admission is deliberately unimplemented.

    def enqueue_pilot(self,payload):
        policy=self.policy();self.metadata(payload,policy)
        requested={**deepcopy(payload),'_highest_download_policy_sha256':policy['_sha256'],'_highest_download_mode':'pilot'}
        prior=self.store.query("SELECT * FROM jobs WHERE source_platform='youtube' AND source_id=? AND job_type=? AND policy_version=?",
            (VIDEO_ID,JOB_TYPE,POLICY_ID))
        if prior:
            if json.loads(prior[0]['payload'])!=requested:raise HubError('YOUTUBE_HIGHEST_EXISTING_BINDING_CHANGED',review=True)
            return prior[0]['job_id']
        Maintenance(self.settings,self.store).check_disk(policy['source_plan']['required_working_bytes'])
        with self.store.transaction() as db:
            old=db.execute("SELECT * FROM jobs WHERE source_platform='youtube' AND source_id=? AND job_type=? AND policy_version=?",
                (VIDEO_ID,JOB_TYPE,POLICY_ID)).fetchone()
            if old:
                if json.loads(old['payload'])!=requested:raise HubError('YOUTUBE_HIGHEST_EXISTING_BINDING_CHANGED',review=True)
                return old['job_id']
            if db.execute("SELECT 1 FROM publications WHERE source_platform='youtube' AND source_id=?",(VIDEO_ID,)).fetchone():
                raise HubError('YOUTUBE_HIGHEST_PUBLICATION_RECONCILIATION_REQUIRED',review=True)
            other=db.execute("SELECT state,result FROM jobs WHERE source_platform='youtube' AND source_id=? AND job_type IN ('youtube_download','youtube_download_v2')",
                (VIDEO_ID,)).fetchall()
            for row in other:
                recorded=json.loads(row['result'])
                if (row['state'] in ('pending','processing','retry_scheduled','completed','publish_unknown') or
                        any(recorded.get(key,{}).get('state')=='verified' for key in ('youtube_download_v1',STATE))):
                    raise HubError('YOUTUBE_HIGHEST_SOURCE_ALREADY_PROCESSING_OR_PROCESSED',review=True)
            ident=str(uuid.uuid4());now=time.time()
            db.execute("""INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,policy_version,payload)
                VALUES(?,'youtube',?,?,'video','pending',?,?,?,?)""",(ident,VIDEO_ID,JOB_TYPE,now,now,POLICY_ID,encode(requested)))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                (now,'operator:local-highest-pilot','youtube_highest_enqueue',ident,'new_version_original_legacy_job_unchanged'))
        return ident

    def _binding(self,job):
        policy=self.policy();current=self.store.get(job['job_id'])
        if (not current or current['job_type']!=JOB_TYPE or current['source_platform']!='youtube' or current['source_id']!=VIDEO_ID or
                current['policy_version']!=POLICY_ID or current['queue']!='video' or current['state']!='processing' or
                not job.get('lease_owner') or current['lease_owner']!=job['lease_owner'] or
                (current.get('lease_until') or 0)<=time.time() or current['payload']!=job['payload'] or
                not 1<=current['attempts']<=min(4,self.settings.config['max_attempts'])):
            raise HubError('YOUTUBE_HIGHEST_LEASE_OR_INPUT_CHANGED',review=True)
        payload=current['payload'];metadata=self.metadata(payload,policy)
        if payload.get('_highest_download_policy_sha256')!=policy['_sha256'] or payload.get('_highest_download_mode')!='pilot':
            raise HubError('YOUTUBE_HIGHEST_POLICY_CHANGED',review=True)
        binding={'version':VERSION,'job_id':current['job_id'],'video_id':VIDEO_ID,'channel_id':metadata['channel_id'],
            'metadata_sha256':metadata['metadata_sha256'],'source_payload_sha256':digest(payload),'duration':metadata['duration'],
            'max_bytes':policy['max_output_bytes'],'max_source_bytes':policy['max_source_bytes'],'max_output_bytes':policy['max_output_bytes'],
            'max_height':1080,'timeout_seconds':policy['timeout_seconds'],'policy_sha256':policy['_sha256'],'request_mode':'pilot',
            'authenticated_session':True,'session_profile':'youtube','session_authorization_sha256':policy['_sha256'],
            'quality_profile':QUALITY,'source_plan':policy['source_plan'],
            'operating_not_before':OPERATING_NOT_BEFORE,'source_actual_start_time':policy['source_plan']['source_actual_start_time'],
            'transport_budget':{'provider':'youtube_download_http','daily_requests':2500,'monthly_requests':30000,'history_reset':False}}
        return current,binding

    def check_binding(self,job,binding):
        if self._binding(job)[1]!=binding:raise HubError('YOUTUBE_HIGHEST_POLICY_CHANGED',review=True)

    def asset(self,asset,binding):
        try:
            path=Path(asset['file']);expected=self.settings.root/'media'/binding['job_id']/'youtube-highest-v2/original.mp4'
            video=asset['stream_config']['video'];audio=asset['stream_config']['audio']
            if (path!=expected or not path.is_absolute() or path.resolve()!=path or any(p.is_symlink() for p in (path,*path.parents)) or
                    not path.is_file() or type(asset['bytes']) is not int or
                    not 0<asset['bytes']<=min(MAX_BYTES,binding['source_plan']['planned_output_max_bytes']) or
                    path.stat().st_size!=asset['bytes'] or sha256_file(path)!=asset['sha256'] or
                    type(asset['duration']) not in (int,float) or not math.isfinite(asset['duration']) or abs(asset['duration']-binding['duration'])>2 or
                    sorted(asset['stream_types'].values())!=['audio','video'] or asset['source_plan']!=binding['source_plan'] or
                    asset['selected_formats']!=binding['source_plan']['selected_formats'] or video['codec_name']!='h264' or
                    video['width']!=1920 or video['height']!=1080 or audio['codec_name']!='aac' or
                    any(Fraction(video[k]) not in (Fraction(60,1),Fraction(60000,1001)) for k in ('avg_frame_rate','r_frame_rate'))):raise ValueError()
            packets=asset['packet_evidence']
            if (set(packets)!={'audio','video'} or any(type(v['packets']) is not int or v['packets']<1 or
                    not re.fullmatch('[a-f0-9]{64}',v['chain']) for v in packets.values())):raise ValueError()
            return asset
        except (OSError,ValueError,KeyError,TypeError,AttributeError,ZeroDivisionError):
            raise HubError('YOUTUBE_HIGHEST_ASSET_CHANGED',review=True) from None

    def process(self,job):
        current,binding=self._binding(job);old=current['result'].get(STATE)
        if old and old.get('binding')!=binding:raise HubError('YOUTUBE_HIGHEST_POLICY_CHANGED',review=True)
        def check():self.check_binding(job,binding)
        if old and old.get('state')=='verified':self.asset(old['source_asset'],binding);check();return current['result']
        state={'version':VERSION,'state':'acquiring','binding':binding,'human_review':False,'publication_authorized':False}
        self.store.checkpoint(job,'youtube_highest_acquiring',{**current['result'],STATE:state})
        factory=self.downloader_factory
        if factory is None:
            from adapters.ytdlp_highest_v2 import HighestReplayDownloader
            factory=HighestReplayDownloader
        acquired=factory(self.settings).acquire(job,binding,check=check);check()
        asset=self.asset(acquired['source_asset'],binding);receipt=acquired['receipt']
        if (acquired.get('version')!=VERSION or acquired.get('binding_sha256')!=digest(binding) or
                any(acquired.get(k) is not False for k in ('human_review','publication_authorized','creator_original_bytes_verified')) or
                not isinstance(receipt,dict) or len(encode(receipt).encode())>262144 or
                receipt.get('source_plan')!=binding['source_plan'] or receipt.get('selected_formats')!=binding['source_plan']['selected_formats'] or
                receipt.get('source_stream_config')!=asset['stream_config'] or
                receipt.get('source_to_merged_packet_coverage_verified') is not True):
            raise HubError('YOUTUBE_HIGHEST_RECEIPT_INVALID',review=True)
        state.update(state='verified',source_asset=asset,receipt=receipt,verified_at=datetime.now(timezone.utc).isoformat())
        result={**job['result'],STATE:state,'disposition':'replay_highest_download_verified_v2','download_performed':True,
            'publish_performed':False,'publication_authorized':False,'human_review':False}
        self.store.checkpoint(job,'youtube_highest_verified',result);return result
