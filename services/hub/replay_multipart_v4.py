"""Whole-replay planning and durable lossless local parts; no download/publish route."""
from copy import deepcopy
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import time

from adapters.media_tools import inspect_media,packet_evidence
from .db import encode
from .errors import HubError
from .media_processing import MediaProcessing,atomic_json
from .locks import resource_guard
from .youtube_highest_plan_v2 import OPERATING_NOT_BEFORE
from .youtube_natural_plan_v3 import _source_plan,_validate_source_plan

VERSION='youtube-whole-replay-plan-v4'
POLICY_VERSION='whole-replay-planning-policy-v4'
PROCESSING_VERSION='replay-multipart-processing-v4'
DEFAULT_POLICY={'version':POLICY_VERSION,'enabled':False,'source_not_before':OPERATING_NOT_BEFORE,
    'target_site_uid':'1000000000000000','max_parts':32,'max_part_bytes':14_000_000_000,
    'max_part_seconds':36000,'disk_floor_bytes':15*1024**3,'workspace_overhead_bytes':32_000_000,
    'max_http_requests':20000,'max_processing_seconds':28800,'max_total_packets':64_000_000,
    'native_single_part_max_bytes':16_000_000_000,'native_single_part_max_seconds':36000}


def digest(value):return hashlib.sha256(encode(value).encode()).hexdigest()
def fail(code):raise HubError(code,review=True)


def validate_planning_policy(policy):
    optional={'raw_track_release_after_verified_merge','prefer_single_whole_passthrough_v5'}
    if not isinstance(policy,dict) or set(policy)-optional!=set(DEFAULT_POLICY):fail('REPLAY_WHOLE_POLICY_INVALID')
    if 'raw_track_release_after_verified_merge' in policy and type(policy['raw_track_release_after_verified_merge']) is not bool:
        fail('REPLAY_WHOLE_POLICY_INVALID')
    if 'prefer_single_whole_passthrough_v5' in policy and type(policy['prefer_single_whole_passthrough_v5']) is not bool:
        fail('REPLAY_WHOLE_POLICY_INVALID')
    if (policy['version']!=POLICY_VERSION or type(policy['enabled']) is not bool or
            policy['source_not_before']!=OPERATING_NOT_BEFORE or policy['target_site_uid']!='1000000000000000' or
            policy['native_single_part_max_bytes']!=16_000_000_000 or type(policy['native_single_part_max_bytes']) is not int or
            policy['native_single_part_max_seconds']!=36000 or type(policy['native_single_part_max_seconds']) is not int):
        fail('REPLAY_WHOLE_POLICY_INVALID')
    for name,minimum,maximum in (('max_parts',1,32),('max_part_bytes',16384,14_000_000_000),
            ('max_part_seconds',1,36000),('disk_floor_bytes',15*1024**3,100_000_000_000),
            ('workspace_overhead_bytes',32_000_000,1_000_000_000),('max_http_requests',128,20000),
            ('max_processing_seconds',30,28800),('max_total_packets',1,64_000_000)):
        if type(policy[name]) is not int or not minimum<=policy[name]<=maximum:fail('REPLAY_WHOLE_POLICY_INVALID')
    return deepcopy(policy)


def _bounds(policy):
    value={'version':'youtube-whole-replay-source-basis-v4','max_seconds':policy['max_part_seconds']*policy['max_parts'],
        'max_bytes':policy['max_part_bytes']*policy['max_parts'],'max_requests':policy['max_http_requests']}
    if policy.get('prefer_single_whole_passthrough_v5') is True:
        value['max_seconds']=max(value['max_seconds'],36000);value['max_bytes']=max(value['max_bytes'],14_000_000_000)
    return value


def _assemble(basis,policy):
    size=basis['source_bytes'];merged=basis['planned_output_max_bytes'];duration=basis['duration']
    seconds=min(policy['max_part_seconds']*.97,duration*policy['max_part_bytes']/max(1,merged*1.1))
    planned_parts=max(1,math.ceil(duration/max(.1,seconds)))
    v5=policy.get('prefer_single_whole_passthrough_v5') is True
    guaranteed_single=v5 and merged<=14_000_000_000 and duration+2<=36000
    if planned_parts>policy['max_parts'] and not guaranteed_single:fail('REPLAY_WHOLE_PLANNED_PART_COUNT_EXCEEDED')
    segment_budget=merged+64_000_000*policy['max_parts']
    if guaranteed_single:segment_budget=0
    working=size+merged+segment_budget+policy['workspace_overhead_bytes']
    lifecycle={}
    if policy.get('raw_track_release_after_verified_merge') is True:
        working=max(size+merged,merged+segment_budget)+policy['workspace_overhead_bytes']
        lifecycle={'resource_model':'verified-merge-track-release-v1','source_tracks_must_be_released_before_split':True,
            'download_merge_peak_bytes':size+merged+policy['workspace_overhead_bytes'],
            'merged_split_peak_bytes':merged+segment_budget+policy['workspace_overhead_bytes'],
            'raw_container_bytes_recoverable':False}
    value={'version':VERSION,'policy_sha256':digest(policy),'planning_policy':deepcopy(policy),'source_id':basis['video_id'],'channel_id':basis['channel_id'],
        'source_actual_start_time':basis['source_actual_start_time'],'source_actual_end_time':basis['source_actual_end_time'],
        'source_not_before':policy['source_not_before'],'duration':duration,'source_plan_basis':deepcopy(basis),
        'split_policy':{'max_parts':policy['max_parts'],'max_part_bytes':policy['max_part_bytes'],
            'max_part_seconds':policy['max_part_seconds'],'mode':'stream-copy-keyframes-only',
            'planned_part_count':planned_parts,'keyframe_limits_require_actual_verification':True},
        'whole_budget':{'source_tracks_bytes':size,'merged_max_bytes':merged,'segment_output_max_bytes':segment_budget,
            'workspace_overhead_bytes':policy['workspace_overhead_bytes'],'required_working_bytes':working,
            'disk_floor_bytes':policy['disk_floor_bytes'],'required_free_bytes':working+policy['disk_floor_bytes'],
            'split_required_free_bytes':segment_budget+policy['workspace_overhead_bytes']+policy['disk_floor_bytes'],**lifecycle},
        'publication_target':'same-submission-multipart-preferred-but-not-verified','native_multipart_verified':False,
        'native_total_parts_limit_verified':False,'native_total_duration_limit_verified':False,
        'human_review':False,'publishable':False,'download_authorized_by_plan':False,'plan_grants_admission':False}
    if v5:
        value.pop('split_policy')
        value.update(processing_model='whole_or_lossless_segments_v5',
            output_policy={'version':'actual-whole-or-lossless-segments-v5','decision_uses_actual_verified_source':True,
                'single_max_bytes':14_000_000_000,'single_max_seconds':36000,'segment_max_bytes':policy['max_part_bytes'],
                'segment_max_seconds':policy['max_part_seconds'],'max_segments':policy['max_parts']},
            publication_target='independent-submissions-per-output-v5',native_independent_submission_verified=False)
        value['whole_budget'].update(guaranteed_single_by_plan=guaranteed_single,
            processing_required_free_bytes=segment_budget+policy['workspace_overhead_bytes']+policy['disk_floor_bytes'])
    return value


def plan_whole_replay(raw,info,*,observed_at,policy,now=None):
    policy=validate_planning_policy(policy)
    # This is a source-date lower bound, not an invented runtime activation time.
    basis=_source_plan(raw,info,activated_at=policy['source_not_before'],observed_at=observed_at,now=now,bounds=_bounds(policy))
    basis['source_not_before']=basis.pop('activated_at')
    return _assemble(basis,policy)


def validate_whole_replay_plan(plan,policy,*,official_raw=None):
    policy=validate_planning_policy(policy)
    try:
        if not isinstance(plan,dict) or len(encode(plan).encode())>300000:raise ValueError()
        basis=deepcopy(plan['source_plan_basis'])
        if basis['source_not_before']!=policy['source_not_before']:raise ValueError()
        basis['activated_at']=basis.pop('source_not_before')
        _validate_source_plan(basis,official_raw=official_raw,bounds=_bounds(policy))
        basis['source_not_before']=basis.pop('activated_at')
        if plan!=_assemble(basis,policy):raise ValueError()
        return deepcopy(plan)
    except HubError:raise
    except (KeyError,ValueError,TypeError,OverflowError):fail('REPLAY_WHOLE_PLAN_CHANGED')


def evaluate_whole_resources(plan,free_bytes):
    if not isinstance(plan,dict) or type(free_bytes) is not int or free_bytes<0:fail('REPLAY_WHOLE_RESOURCE_INPUT_INVALID')
    validate_whole_replay_plan(plan,plan.get('planning_policy'))
    required=plan['whole_budget']['required_free_bytes']
    return {'version':'whole-replay-resource-observation-v4','state':'ready' if free_bytes>=required else 'waiting_resource',
        'required_free_bytes':required,'free_bytes':free_bytes,'plan_sha256':digest(plan),'external_calls':0,
        'files_deleted':0,'admission_granted':False}


class MultipartMediaProcessingV4(MediaProcessing):
    """Reuse the existing stream-copy/checkpoint/packet-chain recovery engine.

    A prepared part is not an upload reservation. This class has no platform
    transport and cannot claim a job, authorize publication or remove sources.
    """
    VERSION=PROCESSING_VERSION
    RESULT_KEY='replay_multipart_processing_v4'
    DIRECTORY_NAME='parts-v4'

    def __init__(self,settings,store,*,policy,plan,check=lambda:None):
        super().__init__(settings,store)
        self.policy=validate_planning_policy(policy);self.whole_plan=validate_whole_replay_plan(plan,self.policy)
        self.plan_sha256=digest(self.whole_plan);self.check=check;self.active_job=None;self.next_lease_check=0
        self.MAX_SOURCE_SECONDS=self.policy['max_parts']*self.policy['max_part_seconds']
        self.source_path=None
        self.maintenance.check_disk=self._check_disk

    def _check_disk(self,reserve_bytes=0):
        self._guard()
        if type(reserve_bytes) is not int or reserve_bytes<0:fail('REPLAY_WHOLE_RESOURCE_INPUT_INVALID')
        free=shutil.disk_usage(self.settings.root).free
        floor=max(self.policy['disk_floor_bytes'],int(self.settings.config['disk_floor_gb']*1_000_000_000))
        if free-reserve_bytes<floor:
            self.store.event('disk.low',{'free_bytes':free,'floor_bytes':floor,'resource_policy':POLICY_VERSION},dedup_key='disk.low')
            raise HubError('DISK_LOW',review=True)
        return free

    def _guard(self):
        if time.monotonic()<self.next_lease_check:return
        self.check()
        if self.active_job is not None:
            rows=self.store.query('SELECT state,lease_owner,lease_until,source_id,payload FROM jobs WHERE job_id=?',(self.active_job['job_id'],))
            if (len(rows)!=1 or rows[0]['state']!='processing' or rows[0]['lease_owner']!=self.active_job['lease_owner'] or
                    not isinstance(rows[0]['lease_until'],(int,float)) or rows[0]['lease_until']<=time.time() or
                    rows[0]['source_id']!=self.whole_plan['source_id'] or json.loads(rows[0]['payload'])!=self.active_job['payload']):
                raise RuntimeError('LEASE_LOST')
        self.next_lease_check=time.monotonic()+.25

    def _sha(self,file):
        self.next_lease_check=0;self._guard();file=Path(file)
        if file.resolve()!=file or any(p.is_symlink() for p in (file,*file.parents)):fail('MEDIA_SOURCE_PATH_INVALID')
        path_before=file.stat();fd=os.open(file,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_BINARY',0));h=hashlib.sha256()
        with os.fdopen(fd,'rb') as source:
            before=os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):fail('MEDIA_SOURCE_PATH_INVALID')
            while block:=source.read(1024*1024):self._guard();h.update(block)
            opened_after=os.fstat(source.fileno())
        after=file.stat();self.next_lease_check=0;self._guard()
        cross=('st_dev','st_ino','st_size','st_mtime_ns')+(() if os.name=='nt' else ('st_ctime_ns',))
        if any(getattr(before,k)!=getattr(after,k) for k in cross):
            fail('MEDIA_SOURCE_INTEGRITY_CHANGED')
        if any(getattr(path_before,k)!=getattr(after,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')):fail('MEDIA_SOURCE_INTEGRITY_CHANGED')
        if any(getattr(before,k)!=getattr(opened_after,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')):fail('MEDIA_SOURCE_INTEGRITY_CHANGED')
        return h.hexdigest()

    def _source(self,job,info):
        file=Path(info['file']);root=self.settings.root/'media'/job['job_id']
        if (not file.is_absolute() or file.resolve()!=file or not file.is_relative_to(root) or not file.is_file() or
                any(p.is_symlink() for p in (file,*file.parents))):fail('MEDIA_SOURCE_PATH_INVALID')
        if (type(info.get('bytes')) is not int or not 0<info['bytes']<=self.whole_plan['whole_budget']['merged_max_bytes'] or
                file.stat().st_size!=info['bytes'] or not re.fullmatch('[a-f0-9]{64}',info.get('sha256','')) or
                self._sha(file)!=info['sha256']):fail('MEDIA_SOURCE_INTEGRITY_CHANGED')
        self.source_path=file;return file

    def recipe(self):
        p=self.policy
        return {'version':self.VERSION,'max_part_seconds':p['max_part_seconds'],'max_part_bytes':p['max_part_bytes'],
            'max_parts':p['max_parts'],'max_render_attempts':3,'timeout_seconds':p['max_processing_seconds'],
            'max_total_packets':p['max_total_packets'],'whole_plan_sha256':self.plan_sha256}

    def _inspect(self,file,**kwargs):
        caller=kwargs.pop('check',lambda:None)
        def check():self._guard();caller()
        value=inspect_media(file,check=check,include_frame_rates=True)
        if Path(file)==self.source_path:
            selected=self.whole_plan['source_plan_basis']['selected_formats'][0];video=value['stream_config']['video'];audio=value['stream_config']['audio']
            try:
                valid=(abs(value['duration']-self.whole_plan['duration'])<=2 and video['codec_name']=='h264' and audio['codec_name']=='aac' and
                    video['width']==selected['width'] and video['height']==selected['height'] and all(
                    abs(float(Fraction(video[k]))-selected['fps'])<=max(.01,selected['fps']*.0011) for k in ('avg_frame_rate','r_frame_rate')))
            except (KeyError,TypeError,ValueError,ZeroDivisionError):valid=False
            if not valid:fail('REPLAY_WHOLE_SOURCE_QUALITY_CHANGED')
        return value

    def _packets(self,file,streams,**kwargs):
        caller=kwargs.pop('check',lambda:None)
        def check():self._guard();caller()
        return packet_evidence(file,streams,check=check,max_packets=self.policy['max_total_packets'],**kwargs)

    def run(self,job,info):
        if self.active_job is not None:fail('REPLAY_MULTIPART_PROCESSOR_BUSY')
        if (job.get('job_type') not in ('replay_multipart_process_v4','youtube_replay_download_v4') or job.get('queue')!='video' or
                job.get('source_id')!=self.whole_plan['source_id'] or job.get('payload',{}).get('whole_replay_plan_sha256')!=self.plan_sha256 or
                job.get('payload',{}).get('whole_replay_plan')!=self.whole_plan or not job.get('lease_owner')):fail('REPLAY_MULTIPART_JOB_BINDING_CHANGED')
        self.active_job=job;self.next_lease_check=0
        try:
            self._guard()
            if self.store.query("SELECT 1 FROM sqlite_master WHERE type='table' AND name='replay_multipart_parts_v4'"):
                if self.store.query("SELECT 1 FROM replay_multipart_parts_v4 WHERE job_id=? AND (state<>'prepared' OR external_publish_id IS NOT NULL OR external_part_id IS NOT NULL)",(job['job_id'],)):
                    fail('REPLAY_MULTIPART_EXTERNAL_STATE_RECONCILIATION_REQUIRED')
            with resource_guard(self.settings.root/'data/locks/heavy'):
                return self._run_prepared(job,info)
        finally:self.active_job=None

    def _run_prepared(self,job,info):
        result=super().run(job,info);self._guard();document=Path(result['manifest'])
        manifest=json.loads(document.read_bytes());recipe_sha=job['result'][self.RESULT_KEY]['recipe_sha256'];parts=[]
        for index,output in enumerate(result['outputs']):
            key=digest({'version':'replay-prepared-part-idempotency-v4','job_id':job['job_id'],'source_sha256':info['sha256'],
                'whole_plan_sha256':self.plan_sha256,'recipe_sha256':recipe_sha,'index':index,'part_sha256':output['sha256']})
            parts.append({**output,'index':index,'part_idempotency_key':key,'state':'prepared','external_publish_id':None,'external_part_id':None})
        manifest.update(whole_plan_sha256=self.plan_sha256,parts_prepared=parts,native_multipart_verified=False,
            acquisition_authority_verified=False,publishable=False,quality_preserved_by_packet_chain=True)
        atomic_json(document,manifest)
        self._guard()
        with self.store.transaction() as db:
            actual=db.execute('SELECT state,lease_owner,lease_until,payload FROM jobs WHERE job_id=?',(job['job_id'],)).fetchone()
            if (actual is None or actual['state']!='processing' or actual['lease_owner']!=job['lease_owner'] or
                    actual['lease_until']<=time.time() or json.loads(actual['payload'])!=job['payload']):raise RuntimeError('LEASE_LOST')
            db.execute('''CREATE TABLE IF NOT EXISTS replay_multipart_parts_v4(job_id TEXT NOT NULL,part_index INTEGER NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,whole_plan_sha256 TEXT NOT NULL,part_sha256 TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('prepared','dispatching','publish_unknown','completed','failed')),
                external_publish_id TEXT,external_part_id TEXT,record TEXT NOT NULL,PRIMARY KEY(job_id,part_index))''')
            for part in parts:
                record=encode(part);old=db.execute('SELECT * FROM replay_multipart_parts_v4 WHERE job_id=? AND part_index=?',(job['job_id'],part['index'])).fetchone()
                if old is not None:
                    if (old['record']!=record or old['state']!='prepared' or old['idempotency_key']!=part['part_idempotency_key'] or
                            old['external_publish_id'] is not None or old['external_part_id'] is not None):fail('REPLAY_MULTIPART_PART_BINDING_CHANGED')
                else:db.execute('INSERT INTO replay_multipart_parts_v4 VALUES(?,?,?,?,?,?,?,?,?)',
                    (job['job_id'],part['index'],part['part_idempotency_key'],self.plan_sha256,part['sha256'],'prepared',None,None,record))
        return {**result,'version':PROCESSING_VERSION,'whole_plan_sha256':self.plan_sha256,'parts':parts,
            'publishable':False,'native_multipart_verified':False,'publications_performed':0,'sources_deleted':0}
