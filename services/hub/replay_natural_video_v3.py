"""Automatic natural replays under a separate, root-accepted workflow.

The highest track is selected by the genuine versioned inventory contract;
neither format 299, a model's claim, nor a previous pilot grant authorizes it.
"""
from copy import deepcopy
from datetime import datetime,timezone
from fractions import Fraction
from pathlib import Path
import hashlib,json,re,time,tomllib
from zoneinfo import ZoneInfo

from .db import encode
from .errors import HubError
from .locks import resource_guard
from .replay_highest_video_v2 import (HighestReplayVideoWeb,HighestReplayMachineChecks,actual_start,
    stream_config_equal,MAX_BYTES,MAX_SECONDS,OPERATING_NOT_BEFORE)
from .replay_highest_limits_v2 import native_limit_evidence
from .replay_video_web import ReplayVideoWeb
from .youtube_download import CHANNELS,digest
from .youtube_natural_plan_v3 import validate_natural_plan,stamp
from .youtube_download_v3 import official_identity

VERSION='replay-highest-natural-website-v3'
POLICY='replay-highest-natural-website-policy-v3'
KIND='video_website_replay_natural_v3'
JOB_TYPE='replay_natural_video_publish_v3'
STATE='replay_natural_video_web_v3'
DOWNLOAD_VERSION='youtube-replay-highest-natural-download-v3'
DOWNLOAD_POLICY='youtube-highest-natural-download-v3'
DOWNLOAD_JOB_TYPE='youtube_download_v3'
DOWNLOAD_STATE='youtube_download_v3'
DOWNLOAD_DIRECTORY='youtube-highest-v3'
QUALITY='highest-height-fps-h264-aac-stream-copy-v1'
ADMISSION_PRINCIPAL='system:replay-natural-admission-v3'


def fail(code='REPLAY_NATURAL_SOURCE_QUALITY_REQUIRED'):raise HubError(code,review=True)


def natural_quality_check(source,binding,asset,receipt,details):
    try:
        started=actual_start(source,binding);raw=source['payload']['raw']
        plan=validate_natural_plan(binding['source_plan'],official_raw=raw)
        identity=official_identity(raw)
        formats=plan['selected_formats'];video,audio=formats
        if (source['job_type']!=DOWNLOAD_JOB_TYPE or source['policy_version']!=DOWNLOAD_POLICY or binding['version']!=DOWNLOAD_VERSION or
                source['source_id']!=plan['video_id'] or plan['channel_id'] not in CHANNELS or binding['quality_profile']!=QUALITY or
                binding['channel_id']!=plan['channel_id'] or binding['duration']!=plan['duration'] or
                not re.fullmatch('[a-f0-9]{64}',binding['policy_sha256']) or stamp(binding['activated_at'])!=stamp(plan['activated_at']) or
                binding.get('official_identity')!=identity or binding.get('official_identity_sha256')!=digest(identity) or
                asset.get('source_plan')!=plan or asset.get('selected_formats')!=formats or receipt.get('source_plan')!=plan or
                receipt.get('selected_formats')!=formats or receipt.get('source_to_merged_packet_coverage_verified') is not True or
                type(asset['bytes']) is not int or not 0<asset['bytes']<=min(MAX_BYTES,plan['planned_output_max_bytes']) or
                not stream_config_equal(asset.get('stream_config'),details['stream_config']) or
                not stream_config_equal(receipt.get('source_stream_config'),details['stream_config'])):fail()
        actual=details['stream_config']['video'];sound=details['stream_config']['audio']
        if (actual['codec_name']!='h264' or sound['codec_name']!='aac' or actual['width']!=video['width'] or actual['height']!=video['height'] or
                any(abs(float(Fraction(actual[key]))-video['fps'])>max(.01,video['fps']*.0011) for key in ('avg_frame_rate','r_frame_rate'))):fail()
        return {'version':'highest-natural-quality-contract-v3','quality_profile':QUALITY,'source_plan_sha256':digest(plan),
            'inventory_sha256':plan['inventory_sha256'],'selected_formats':deepcopy(formats),'highest_tier':deepcopy(plan['highest_tier']),
            'source_actual_start_time':started,'source_actual_end_time':plan['source_actual_end_time'],'activated_at':plan['activated_at'],
            'operating_not_before':OPERATING_NOT_BEFORE,'width':actual['width'],'height':actual['height'],
            'avg_frame_rate':actual['avg_frame_rate'],'r_frame_rate':actual['r_frame_rate'],'output_bytes':asset['bytes'],
            'silent_quality_fallback_allowed':False,'server_file_hash_verified':False,'human_review':False}
    except HubError:raise
    except (KeyError,TypeError,ValueError,AttributeError,ZeroDivisionError,OverflowError):fail()


def natural_metadata(settings,source):
    """Stable original official title/source, with no model or invented review."""
    try:
        payload=source['payload'];raw=payload['raw'];snippet=raw['snippet'];channel=payload['channel_id']
        configured=[item for item in settings.config['sources'] if item['youtube_channel_id']==channel]
        if len(configured)!=1 or channel not in CHANNELS or snippet['channelId']!=channel or snippet['title']!=payload['title']:raise ValueError()
        name=configured[0]['name'];handle=configured[0]['youtube_handle'];title=snippet['title'];url=payload['url']
        if (not isinstance(name,str) or not 0<len(name)<=20 or not isinstance(title,str) or not 0<len(title)<=200 or
                not isinstance(handle,str) or not re.fullmatch('@[A-Za-z0-9_.-]{1,100}',handle)):raise ValueError()
        start=stamp(raw['liveStreamingDetails']['actualStartTime']).astimezone(ZoneInfo('Asia/Shanghai'))
        units=lambda text:len(text.encode('utf-16-le'))//2
        prefix='【'+name+'】'+start.strftime('%Y-%m-%d')+'「'
        suffix='」直播回放';room=80-units(prefix)-units(suffix)
        if room<2:raise ValueError()
        display=title
        if units(title)>room:
            display=''
            for character in title:
                if units(display+character)>room-1:break
                display+=character
            display+='…'
        description=(name+' '+str(start.year)+'年'+str(start.month)+'月'+str(start.day)+'日直播回放，日语原声。\n原题：'+title+
            '\n原视频：'+url+'\n原频道：https://www.youtube.com/'+handle)
        return {'title':prefix+display+suffix,'description':description,'source':url,'category_label':'娱乐','copyright':2,
            'tags':[name,'ぶいすぽっ','虚拟UP主','直播回放']}
    except (KeyError,ValueError,TypeError,AttributeError,OverflowError):fail('REPLAY_NATURAL_METADATA_SOURCE_REQUIRED')


class NaturalReplayMachineChecks(HighestReplayMachineChecks):
    VERSION='verified-replay-natural-machine-checks-v3'
    REPLAY_VERSION='replay-highest-natural-website-v3'
    REPLAY_POLICY,REPLAY_STATE,JOB_TYPE=POLICY,STATE,JOB_TYPE
    DOWNLOAD_STATE,DOWNLOAD_VERSION=DOWNLOAD_STATE,DOWNLOAD_VERSION
    DOWNLOAD_JOB_TYPE,DOWNLOAD_DIRECTORY=DOWNLOAD_JOB_TYPE,DOWNLOAD_DIRECTORY
    POLICY_FILE='replay-natural-video-web-policy.toml'
    PUBLIC_MODE='workflow_authorization_and_natural_highest_machine_acceptance_v3'
    EVIDENCE_DIRECTORY='replay-natural-machine-checks-v3'

    def quality_check(self,source,download_binding,asset,receipt,details):
        quality=natural_quality_check(source,download_binding,asset,receipt,details)
        latest=self.store.query('SELECT * FROM youtube_videos WHERE video_id=?',(source['source_id'],))
        if (len(latest)!=1 or latest[0]['source_api']!='youtube-data-v3-official' or latest[0]['state']!='ended' or
                latest[0]['channel_id']!=download_binding['channel_id'] or
                official_identity(json.loads(latest[0]['metadata']))!=download_binding['official_identity']):
            fail('REPLAY_NATURAL_OFFICIAL_STATE_CHANGED')
        job=self.store.query('SELECT payload FROM jobs WHERE job_type=? AND source_id=? AND policy_version=?',(JOB_TYPE,source['source_id'],POLICY))
        if len(job)!=1:fail('REPLAY_NATURAL_METADATA_SOURCE_REQUIRED')
        binding=json.loads(job[0]['payload'])
        if binding['metadata']!=natural_metadata(self.settings,source):fail('REPLAY_NATURAL_METADATA_SOURCE_REQUIRED')
        policy=tomllib.loads((self.settings.root/'config'/self.POLICY_FILE).read_text('utf-8-sig'))
        if stamp(policy['activated_at'])!=stamp(download_binding['source_plan']['activated_at']):fail('REPLAY_NATURAL_ACTIVATION_CHANGED')
        return {**quality,'version':'verified-natural-highest-quality-v3','metadata_template':'source-title-and-link-v1',
            'native_upload_limit_evidence':native_limit_evidence(self.settings,policy)}


class NaturalReplayVideoWeb(HighestReplayVideoWeb):
    VERSION,POLICY,KIND,JOB_TYPE,STATE=VERSION,POLICY,KIND,JOB_TYPE,STATE
    DOWNLOAD_STATE,DOWNLOAD_VERSION=DOWNLOAD_STATE,DOWNLOAD_VERSION
    DOWNLOAD_JOB_TYPE,DOWNLOAD_DIRECTORY=DOWNLOAD_JOB_TYPE,DOWNLOAD_DIRECTORY
    RESULT_OBSERVATION_VERSION='replay-natural-browser-result-v3'
    POLICY_FILE='replay-natural-video-web-policy.toml'
    PUBLIC_MODE='workflow_authorization_and_natural_highest_machine_acceptance_v3'
    WORKFLOW_LAUNCH_RECEIPT=Path('/etc/lighthouse-hub/replay-natural-workflow-launch-v3.json')
    WORKFLOW_LAUNCH_VERSION='replay-natural-workflow-launch-acceptance-v3'
    WORKFLOW_AUTHORIZATION_VERSION='replay-natural-workflow-authorization-v3'
    MACHINE_PROOF_VERSION='verified-replay-natural-machine-checks-v3'
    MACHINE_PROOF_DIRECTORY='replay-natural-machine-checks-v3'
    NATIVE_PREPARATION_VERSION='replay-natural-native-preparation-v3'
    NATIVE_FORM_VERSION='replay-natural-native-form-observation-v3'
    NATIVE_DIRECTORY='replay-browser-natural-v3'
    NATIVE_RESERVATION_VERSION='replay-natural-native-reservation-v3'

    def __init__(self,settings,store,*,machine_acceptance_verifier=None):
        ReplayVideoWeb.__init__(self,settings,store,machine_acceptance_verifier=machine_acceptance_verifier or NaturalReplayMachineChecks(settings,store))
        self._admitting=False

    def _operator(self,principal):
        if principal==ADMISSION_PRINCIPAL and self._admitting:return
        return super()._operator(principal)

    def policy(self):
        policy=ReplayVideoWeb.policy(self)
        if (policy['source_scope']!='automatic_new_only' or policy['acceptance_video_ids'] or policy['automation_enabled'] is not True or
                policy.get('metadata_template')!='source-title-and-link-v1' or policy.get('operating_not_before')!=OPERATING_NOT_BEFORE):
            fail('REPLAY_NATURAL_WORKFLOW_POLICY_REQUIRED')
        policy['_native_upload_limits']=native_limit_evidence(self.settings,policy)
        return policy

    def _source_scope(self,source,policy):
        ReplayVideoWeb._source_scope(self,source,policy)
        checkpoint=source['result'][DOWNLOAD_STATE]
        natural_quality_check(source,checkpoint['binding'],checkpoint['source_asset'],checkpoint['receipt'],{'stream_config':checkpoint['source_asset'].get('stream_config')})
        if stamp(checkpoint['binding']['source_plan']['activated_at'])!=stamp(policy['activated_at']):fail('REPLAY_NATURAL_ACTIVATION_CHANGED')

    def _require_workflow_launch(self,policy):
        from .replay_natural_launch_v3 import read_launch
        return read_launch(self,policy)

    def queue_verified_download(self,download_job_id):
        policy=self.policy();grant=self._require_workflow_launch(policy)
        with resource_guard(self.settings.root/'data/locks/replay-natural-admission-v3',self.settings.root/'data/locks/heavy'):
            source=self.store.get(download_job_id)
            self._admitting=True
            try:
                job_id=self.create_candidate(ADMISSION_PRINCIPAL,download_job_id,natural_metadata(self.settings,source))
                job,_,state=self.snapshot(job_id)
                if job['state']!='awaiting_review' or state['state']!='prepared':return job_id
                self.record_automation_authorization(ADMISSION_PRINCIPAL,job_id,
                    actual_owner_workflow_response=grant['actual_owner_launch_response'],owner_response_reference=grant['owner_response_reference'],
                    workflow_authorization_reference=policy['workflow_authorization_reference'])
                job,_,state=self.snapshot(job_id)
                self._update(ADMISSION_PRINCIPAL,job,state,job_state='pending',stage='replay_natural_queued')
                return job_id
            finally:self._admitting=False


def natural_replay_manifest(settings,store,job,binding,state,attempt_id,*,duration_seconds,require_cover=True):
    from .replay_video_browser import replay_manifest
    source=store.get(binding['download_job_id']);checkpoint=source['result'][DOWNLOAD_STATE];download=checkpoint['binding']
    actual_start(source,download);policy=NaturalReplayVideoWeb(settings,store).policy()
    result=replay_manifest(settings,job,binding,state,attempt_id,duration_seconds=duration_seconds,require_cover=require_cover)
    return {**result,'version':'replay-natural-native-browser-manifest-v3','highest_contract':{
        'version':'replay-natural-source-contract-v3','download_policy_version':source['policy_version'],
        'download_binding':deepcopy(download),'download_binding_canonical':encode(download),
        'source_payload':deepcopy(source['payload']),'source_payload_canonical':encode(source['payload']),
        'source_inventory_canonical':encode(download['source_plan']['source_inventory']),
        'official_metadata_canonical':encode(source['payload']['raw']),
        'download_receipt_sha256':binding['download_receipt_sha256'],'native_upload_limits':policy['_native_upload_limits']}}
