"""Separate highest-quality replay preparation; v1 grants never enable v2.

No admission, deployment, network call or platform write occurs on import.
The source plan is revalidated independently of its download receipt's flags.
"""
from copy import deepcopy
from datetime import datetime,timezone
from fractions import Fraction
import math
from pathlib import Path
import re
import tomllib

from adapters.media_tools import inspect_media
from .errors import HubError
from .replay_machine_checks import ReplayMachineChecks
from .replay_video_web import ReplayVideoWeb
from .youtube_download import digest
from .db import encode
from .replay_highest_limits_v2 import native_limit_evidence

VERSION='replay-highest-video-website-v2'
POLICY='replay-highest-video-website-policy-v2'
KIND='video_website_replay_highest_v2'
JOB_TYPE='replay_highest_video_publish_v2'
STATE='replay_highest_video_web_v2'
DOWNLOAD_VERSION='youtube-replay-highest-download-v2'
DOWNLOAD_POLICY='youtube-highest-download-v2'
DOWNLOAD_CONFIG_VERSION='youtube-highest-download-policy-v2'
DOWNLOAD_STATE='youtube_download_v2'
DOWNLOAD_JOB_TYPE='youtube_download_v2'
DOWNLOAD_DIRECTORY='youtube-highest-v2'
QUALITY='h264-1080p60-itag299-aac140-v1'
MAX_BYTES=14_000_000_000
MAX_SECONDS=36000
PILOT_SOURCE='9T5tn1WGlJg'
OPERATING_NOT_BEFORE='2026-10-31T16:00:00Z'


def fail():
    raise HubError('REPLAY_HIGHEST_SOURCE_QUALITY_REQUIRED',review=True)


def actual_start(source,binding):
    """Only official actualStartTime can satisfy the operating-date gate."""
    try:
        raw=source['payload']['raw'];value=raw['liveStreamingDetails']['actualStartTime']
        if source['payload']['source_api']!='youtube-data-v3-official' or raw['id']!=source['source_id'] or not isinstance(value,str):raise ValueError()
        started=datetime.fromisoformat(value.replace('Z','+00:00'))
        cutoff=datetime.fromisoformat(OPERATING_NOT_BEFORE.replace('Z','+00:00'))
        if started.tzinfo is None or started.timestamp()>datetime.now(timezone.utc).timestamp()+30:raise ValueError()
        normalized=started.astimezone(timezone.utc).isoformat().replace('+00:00','Z')
        plan=binding['source_plan']
        if (binding.get('operating_not_before')!=OPERATING_NOT_BEFORE or plan.get('operating_not_before')!=OPERATING_NOT_BEFORE or
                binding.get('source_actual_start_time')!=normalized or plan.get('source_actual_start_time')!=normalized):raise ValueError()
        if started<cutoff:raise HubError('REPLAY_HIGHEST_BEFORE_OPERATING_START',review=True)
        return normalized
    except HubError:raise
    except (KeyError,TypeError,ValueError,AttributeError,OverflowError):
        raise HubError('REPLAY_HIGHEST_ACTUAL_START_REQUIRED',review=True) from None


def stream_config_equal(left,right):
    """Compare real stream properties, with rational video rates normalized."""
    a,b=deepcopy(left),deepcopy(right)
    if not isinstance(a,dict) or not isinstance(b,dict):return False
    for item in (a,b):
        video=item.get('video')
        if not isinstance(video,dict):return False
        for key in ('avg_frame_rate','r_frame_rate'):
            if not isinstance(video.get(key),str):return False
            video[key]=Fraction(video[key])
    return a==b


def highest_quality_check(source,binding,asset,receipt,details):
    """No fallback: explicit 299 H2641080p60 +140 AAC and exact merged config."""
    try:
        started=actual_start(source,binding)
        plan=binding['source_plan'];formats=plan['selected_formats']
        fields={'version','quality_profile','selected_formats','source_bytes','planned_output_max_bytes',
                'required_working_bytes','workspace_overhead_bytes','minimum_range_bytes','media_requests_plan','metadata_request_allowance',
                'redirect_resume_request_allowance','max_http_requests','operating_not_before','source_actual_start_time'}
        item_fields={'format_id','ext','vcodec','acodec','width','height','fps','filesize'}
        if (source['job_type']!=DOWNLOAD_JOB_TYPE or source['policy_version']!=DOWNLOAD_POLICY or
                source['source_id']!=PILOT_SOURCE or binding.get('quality_profile')!=QUALITY or
                not re.fullmatch('[a-f0-9]{64}',binding.get('policy_sha256','')) or
                set(plan)!=fields or plan['version']!='youtube-highest-source-plan-v2' or plan['quality_profile']!=QUALITY or
                not isinstance(formats,list) or len(formats)!=2 or any(set(item)!=item_fields for item in formats)):
            fail()
        video,audio=formats
        expected_video={'format_id':'299','ext':'mp4','vcodec':'h264','acodec':'none','width':1920,'height':1080,'fps':60.0}
        expected_audio={'format_id':'140','ext':'m4a','vcodec':'none','acodec':'aac','width':None,'height':None,'fps':None}
        if (any(video[k]!=v for k,v in expected_video.items()) or any(audio[k]!=v for k,v in expected_audio.items()) or
                type(video['fps']) not in (int,float) or
                any(type(item['filesize']) is not int or not 0<item['filesize']<=MAX_BYTES for item in formats)):
            fail()
        total=sum(item['filesize'] for item in formats);minimum=9961472
        media=sum(math.ceil(item['filesize']/minimum) for item in formats)
        expected={'source_bytes':total,'planned_output_max_bytes':total+64_000_000,
            'required_working_bytes':2*total+96_000_000,'workspace_overhead_bytes':32_000_000,'minimum_range_bytes':minimum,'media_requests_plan':media,
            'metadata_request_allowance':64,'redirect_resume_request_allowance':max(64,math.ceil(media/4)),
            'max_http_requests':media+64+max(64,math.ceil(media/4))}
        if (any(type(plan[k]) is not int or plan[k]!=v for k,v in expected.items()) or
                plan['planned_output_max_bytes']>MAX_BYTES or plan['max_http_requests']>2048 or
                type(asset['bytes']) is not int or not 0<asset['bytes']<=plan['planned_output_max_bytes'] or
                asset.get('source_plan')!=plan or asset.get('selected_formats')!=formats or
                receipt.get('source_plan')!=plan or receipt.get('selected_formats')!=formats or
                receipt.get('source_to_merged_packet_coverage_verified') is not True or
                not stream_config_equal(asset.get('stream_config'),details['stream_config']) or
                not stream_config_equal(receipt.get('source_stream_config'),details['stream_config'])):
            fail()
        v=details['stream_config']['video'];a=details['stream_config']['audio']
        rates={Fraction(60,1),Fraction(60000,1001)}
        if (v.get('codec_name')!='h264' or a.get('codec_name')!='aac' or v.get('width')!=1920 or v.get('height')!=1080 or
                any(not isinstance(v.get(key),str) or Fraction(v[key]) not in rates for key in ('avg_frame_rate','r_frame_rate'))):
            fail()
        return {'version':'highest-replay-quality-contract-v2','quality_profile':QUALITY,'source_plan_sha256':digest(plan),
            'download_policy_sha256':binding['policy_sha256'],'selected_formats':deepcopy(formats),
            'operating_not_before':OPERATING_NOT_BEFORE,'source_actual_start_time':started,
            'source_bytes':total,'output_bytes':asset['bytes'],'planned_output_max_bytes':plan['planned_output_max_bytes'],
            'working_bytes_excluding_disk_floor':plan['required_working_bytes'],
            'width':v['width'],'height':v['height'],'avg_frame_rate':v['avg_frame_rate'],'r_frame_rate':v['r_frame_rate'],
            'source_and_merged_stream_configuration_equal':True,'silent_quality_fallback_allowed':False,
            'server_file_hash_verified':False,'human_review':False}
    except HubError:
        raise
    except (KeyError,TypeError,ValueError,ZeroDivisionError,OverflowError,AttributeError):
        fail()


class HighestReplayMachineChecks(ReplayMachineChecks):
    VERSION='verified-replay-highest-machine-checks-v2'
    REPLAY_VERSION='replay-highest-video-website-v2'
    REPLAY_POLICY,REPLAY_STATE,JOB_TYPE=POLICY,STATE,JOB_TYPE
    DOWNLOAD_STATE,DOWNLOAD_VERSION=DOWNLOAD_STATE,DOWNLOAD_VERSION
    DOWNLOAD_JOB_TYPE,DOWNLOAD_DIRECTORY=DOWNLOAD_JOB_TYPE,DOWNLOAD_DIRECTORY
    POLICY_FILE='replay-highest-video-web-policy.toml'
    PUBLIC_MODE='workflow_authorization_and_highest_machine_acceptance_v2'
    MAX_BYTES,MAX_SECONDS=MAX_BYTES,MAX_SECONDS
    EVIDENCE_DIRECTORY='replay-highest-machine-checks-v2'

    def verify(self,*args,**kwargs):
        from .maintenance import Maintenance
        if self.settings.config.get('disk_floor_gb',0)<15:
            raise HubError('REPLAY_HIGHEST_DISK_FLOOR_REQUIRED',review=True)
        Maintenance(self.settings,self.store).check_disk()
        return super().verify(*args,**kwargs)

    def inspect_source_media(self,file,*,check):
        return inspect_media(file,check=check,include_frame_rates=True)

    def quality_check(self,source,download_binding,asset,receipt,details):
        quality=highest_quality_check(source,download_binding,asset,receipt,details)
        policy=tomllib.loads((self.settings.root/'config'/self.POLICY_FILE).read_text('utf-8-sig'))
        return {**quality,'version':'verified-highest-replay-quality-v2',
                'native_upload_limit_evidence':native_limit_evidence(self.settings,policy)}


class HighestReplayVideoWeb(ReplayVideoWeb):
    VERSION,POLICY,KIND,JOB_TYPE,STATE=VERSION,POLICY,KIND,JOB_TYPE,STATE
    MAX_BYTES,MAX_SECONDS=MAX_BYTES,MAX_SECONDS
    DOWNLOAD_STATE,DOWNLOAD_VERSION=DOWNLOAD_STATE,DOWNLOAD_VERSION
    DOWNLOAD_JOB_TYPE,DOWNLOAD_DIRECTORY=DOWNLOAD_JOB_TYPE,DOWNLOAD_DIRECTORY
    RESULT_OBSERVATION_VERSION='replay-highest-browser-result-v2'
    POLICY_FILE='replay-highest-video-web-policy.toml'
    PUBLIC_MODE='workflow_authorization_and_highest_machine_acceptance_v2'
    WORKFLOW_LAUNCH_RECEIPT=Path('/etc/lighthouse-hub/replay-highest-workflow-launch-v2.json')
    WORKFLOW_LAUNCH_VERSION='replay-highest-workflow-launch-acceptance-v2'
    WORKFLOW_AUTHORIZATION_VERSION='replay-highest-workflow-authorization-v2'
    MACHINE_PROOF_VERSION='verified-replay-highest-machine-checks-v2'
    MACHINE_PROOF_DIRECTORY='replay-highest-machine-checks-v2'
    NATIVE_PREPARATION_VERSION='replay-highest-native-preparation-v2'
    NATIVE_FORM_VERSION='replay-highest-native-form-observation-v2'
    NATIVE_DIRECTORY='replay-browser-highest-v2'
    NATIVE_RESERVATION_VERSION='replay-highest-native-reservation-v2'

    def __init__(self,settings,store,*,machine_acceptance_verifier=None):
        super().__init__(settings,store,machine_acceptance_verifier=machine_acceptance_verifier or HighestReplayMachineChecks(settings,store))

    def policy(self):
        policy=super().policy()
        if policy.get('source_scope')!='pilot_single' or policy.get('acceptance_video_ids')!=[PILOT_SOURCE] or policy.get('automation_enabled') is not False:
            fail()
        policy['_native_upload_limits']=native_limit_evidence(self.settings,policy)
        return policy

    def _source_scope(self,source,policy):
        super()._source_scope(source,policy)
        if policy.get('source_scope')!='pilot_single' or policy.get('acceptance_video_ids')!=[PILOT_SOURCE]:
            fail()
        state=source['result'][self.DOWNLOAD_STATE]
        highest_quality_check(source,state['binding'],state['source_asset'],state['receipt'],
                              {'stream_config':state['source_asset'].get('stream_config')})

    def record_upload(self,principal,job_id,observation):
        from .replay_highest_coverage_v2 import upload_coverage
        self._operator(principal);job,binding,state=self.snapshot(job_id)
        fields={'version','attempt_id','site_uid','artifact_sha256','file_bytes','upload_reference_sha256',
            'same_attempt_observed','server_file_hash_verified','upload_coverage'}
        if (state['state']!='uploading' or not isinstance(observation,dict) or set(observation)!=fields or
                observation.get('version')!='short-video-browser-upload-v1' or observation.get('attempt_id')!=state['attempt_id'] or
                any(observation.get(k)!=binding[k] for k in ('site_uid','artifact_sha256','file_bytes')) or
                observation.get('same_attempt_observed') is not True or observation.get('server_file_hash_verified') is not False or
                not re.fullmatch('[a-f0-9]{64}',str(observation.get('upload_reference_sha256','')))):
            raise HubError('REPLAY_HIGHEST_UPLOAD_OBSERVATION_INVALID',review=True)
        coverage=upload_coverage(self.settings,job,binding,state,observation['upload_coverage'],fresh=True,directory=self.NATIVE_DIRECTORY)
        return self._update(principal,job,{**state,'state':'uploaded','upload_observation':deepcopy(observation),'upload_coverage':coverage},
            job_state='upload_pending',stage='replay_highest_uploaded_with_coverage')

    def _result_extra_state(self,job,binding,state,observation,*,lookup):
        if observation.get('state')!='completed':return {}
        if not lookup or observation.get('public_accessible') is not True:
            raise HubError('REPLAY_HIGHEST_PUBLIC_PLAYER_COVERAGE_REQUIRED',review=True)
        from .replay_highest_coverage_v2 import public_coverage
        coverage=public_coverage(self.settings,self.store,job,binding,state,observation.get('cleanup_coverage'),observation,directory=self.NATIVE_DIRECTORY,download_state=self.DOWNLOAD_STATE)
        return {'cleanup_coverage':coverage}

    def cleanup_coverage_summary(self,job_id):
        """Read-only evidence, not deletion authority or a release of a pilot hold."""
        from .replay_highest_coverage_v2 import public_coverage
        job,binding,state=self.snapshot(job_id,write=False)
        recorded=state.get('cleanup_coverage',{})
        coverage=public_coverage(self.settings,self.store,job,binding,state,recorded.get('evidence'),state.get('result_observation',{}),directory=self.NATIVE_DIRECTORY,download_state=self.DOWNLOAD_STATE)
        if coverage!=recorded:
            raise HubError('REPLAY_HIGHEST_COVERAGE_EVIDENCE_REQUIRED',review=True)
        return {'job':job,'binding':binding,'coverage':coverage}


def highest_replay_manifest(settings,store,job,binding,state,attempt_id,*,duration_seconds,require_cover=True):
    """Bind original Python canonical JSON too: FPS 60.0 must not hash as JS 60."""
    from .replay_video_browser import replay_manifest
    source=store.get(binding['download_job_id'])
    if source is None:
        raise HubError('REPLAY_HIGHEST_MANIFEST_STORE_REQUIRED',review=True)
    checkpoint=source['result'][DOWNLOAD_STATE];download_binding=checkpoint['binding']
    # The result lookup path remains usable after local media has been cleaned.
    # Admission/prepare/final reserve independently re-open and re-verify bytes.
    actual_start(source,download_binding)
    policy=HighestReplayVideoWeb(settings,store).policy()
    manifest=replay_manifest(settings,job,binding,state,attempt_id,duration_seconds=duration_seconds,require_cover=require_cover)
    return {**manifest,'version':'replay-highest-native-browser-manifest-v2','highest_contract':{
        'version':'replay-highest-source-contract-v2','download_policy_version':source['policy_version'],
        'download_binding':deepcopy(download_binding),'download_binding_canonical':encode(download_binding),
        'source_payload':deepcopy(source['payload']),'source_payload_canonical':encode(source['payload']),
        'download_receipt_sha256':binding['download_receipt_sha256'],
        'native_upload_limits':policy['_native_upload_limits']}}


def highest_replay_worker_handlers(settings,store):
    """Explicit v2 registration; this never enqueues or enables a policy."""
    from .replay_video_browser import ReplayVideoWorkerHandler
    def manifest(settings,job,binding,state,attempt_id,**kwargs):
        return highest_replay_manifest(settings,store,job,binding,state,attempt_id,**kwargs)
    handler=ReplayVideoWorkerHandler(settings,store,service=HighestReplayVideoWeb(settings,store),manifest_builder=manifest)
    return {JOB_TYPE:handler.process}
