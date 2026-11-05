"""One real YouTube replay group and independently published ordinary parts.

The source ID never changes. A positive set of 32 child job types expresses
the ordinal while preserving the legacy jobs UNIQUE constraint. Publications
are one-use per bound child, and foreign source publications remain blocked.
"""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import time
import tomllib
import uuid

from adapters.media_tools import empty_evidence, inspect_media
from .db import encode
from .errors import HubError
from .locks import resource_guard
from .media_processing import atomic_json,sync_directory
from .replay_fifo_v4 import VERSION as FIFO_POLICY, DOWNLOAD_JOB, CUTOFF
from .replay_machine_checks import ReplayMachineChecks, read_checked
from .replay_multipart_v4 import validate_whole_replay_plan
from .replay_natural_video_v3 import natural_metadata
from .replay_video_web import ReplayVideoWeb, WORKER_PRINCIPAL, _timestamp
from .short_video_web import _reference
from .youtube_download import CHANNELS, digest
from .youtube_download_v4 import VERSION as DOWNLOAD_VERSION, STATE as DOWNLOAD_STATE,transfer_plan
from .youtube_download_v3 import validate_quality,official_identity

GROUP_JOB = 'replay_parts_publish_parent_v5'
GROUP_STATE = 'replay_parts_v5'
VERSION = 'replay-part-video-website-v5'
POLICY = 'replay-part-video-policy-v5'
STATE = 'replay_part_video_v5'
KIND = 'video_website_replay_part_v5'
PART_JOB_TYPES = tuple('replay_part_publish_v5_p' + format(i+1,'02d') for i in range(32))
PART_KINDS = tuple(KIND+'_p'+format(i+1,'02d') for i in range(32))
ADMISSION = 'system:replay-parts-admission-v5'
LEGACY_PROCESS_RETIREMENT = 'retire-legacy-source-video-process-v5'
LEGACY_PROCESS_MARKER = 'youtube_legacy_video_process_superseded_v5'
MAX_SINGLE_BYTES, MAX_SINGLE_SECONDS = 14_000_000_000, 36000


def fail(code):
    raise HubError(code, review=True)


def part_type(index):
    if type(index) is not int or not 0 <= index < 32:
        fail('REPLAY_PART_INDEX_INVALID')
    return PART_JOB_TYPES[index]


def part_kind(index):
    part_type(index)
    return PART_KINDS[index]


def index_from_job(job):
    try:
        index = job['payload']['part_index']
        if job['job_type'] != part_type(index) or job['policy_version'] != POLICY:
            raise ValueError()
        return index
    except (KeyError,ValueError,TypeError):
        fail('REPLAY_PART_JOB_ORDINAL_MISMATCH')


def wire_seconds(value):
    if type(value) not in (int,float) or not math.isfinite(value) or value <= 0:
        fail('REPLAY_PART_DURATION_INVALID')
    return int(value) if float(value).is_integer() else value


def document(root,file,maximum=2_000_000,check=lambda:None):
    reference,raw=read_checked(root,file,maximum=maximum,check=check)
    try:return json.loads(raw),reference
    except (ValueError,TypeError):fail('REPLAY_PART_DOCUMENT_INVALID')


def reference_body(root,reference,*,maximum=2_000_000,expected=None,check=lambda:None,json_body=True):
    if (not isinstance(reference,dict) or set(reference)!={'file','bytes','sha256'} or type(reference['bytes']) is not int or
            not 0<reference['bytes']<=maximum or expected is not None and reference['file']!=str(expected)):
        fail('REPLAY_PART_EVIDENCE_REFERENCE_INVALID')
    actual,raw=read_checked(root,reference['file'],maximum=maximum,check=check)
    if actual!=reference:fail('REPLAY_PART_EVIDENCE_CHANGED')
    if not json_body:return raw
    try:return json.loads(raw)
    except (ValueError,TypeError):fail('REPLAY_PART_DOCUMENT_INVALID')


def native_surface(binding,value,*,metadata=False):
    try:
        allowed=[Path(binding['file']).name,Path(binding['file']).stem]
        if metadata:allowed.extend([binding['metadata']['title'],re.sub(' +',' ',binding['metadata']['title'])])
        if (set(value)!={'surface_mode','actual_outer_count','actual_inner_count','metadata_form_count','native_filename','selected_task_title'} or
                any(type(value[k]) is not int for k in ('actual_outer_count','actual_inner_count','metadata_form_count')) or
                value['actual_inner_count']!=1 or value['metadata_form_count']!=1 or value['native_filename'] not in allowed or
                value['actual_outer_count'] not in (0,1) or
                value['surface_mode']!=('single_editor_no_batch_sidebar' if value['actual_outer_count']==0 else 'single_selected_task_sidebar') or
                (value['actual_outer_count']==0 and value['selected_task_title'] is not None) or
                (value['actual_outer_count']==1 and value['selected_task_title'] not in allowed and
                 value['selected_task_title'] not in [re.sub(' +',' ',title) for title in allowed])):raise ValueError()
        return deepcopy(value)
    except (KeyError,ValueError,TypeError,AttributeError):fail('REPLAY_PART_SINGLE_SUBMISSION_SURFACE_REQUIRED')


def part_metadata(settings,source,index,count):
    """Only the title adds P1/N; the original four-line description is intact."""
    value=natural_metadata(settings,source)
    configured=[s for s in settings.config['sources'] if s['youtube_channel_id']==source['payload']['channel_id']]
    if len(configured)!=1 or type(index) is not int or type(count) is not int or not 1<=count<=32 or not 0<=index<count:
        fail('REPLAY_PART_METADATA_SCOPE_INVALID')
    started=datetime.fromisoformat(source['payload']['raw']['liveStreamingDetails']['actualStartTime'].replace('Z','+00:00')).astimezone(timezone(timedelta(hours=8)))
    prefix='【'+configured[0]['name']+'】'+started.strftime('%Y-%m-%d')+'「'
    ending='」直播回放 P'+str(index+1)+'/'+str(count)
    units=lambda text:len(text.encode('utf-16-le'))//2
    room=80-units(prefix+ending)
    if room<2:fail('REPLAY_PART_TITLE_LIMIT')
    original=source['payload']['raw']['snippet']['title'];display=original
    if units(original)>room:
        display=''
        for character in original:
            if units(display+character)>room-1:break
            display+=character
        display+='…'
    value['title']=prefix+display+ending
    return value


def verified_delivery(settings,store,download_job_id,*,check=lambda:None,verify_index=None,verify_all=False):
    """Reopen the real whole-download receipt and stable delivery manifest."""
    try:
        source=store.get(download_job_id);state=source['result'][DOWNLOAD_STATE]
        binding=state['binding'];asset=state['source_asset'];summary=state['delivery_v5']
        plan=validate_whole_replay_plan(source['payload']['whole_replay_plan'],binding['planning_policy'],official_raw=source['payload']['raw'])
        transfer=transfer_plan(plan)
        if (source['source_platform']!='youtube' or source['job_type']!=DOWNLOAD_JOB or source['policy_version']!=FIFO_POLICY or source['state']!='completed' or
                state['version']!=DOWNLOAD_VERSION or state['state'] not in ('single_output_verified_v5','parts_verified') or
                binding['job_id']!=source['job_id'] or binding['video_id']!=source['source_id'] or binding['source_payload_sha256']!=digest(source['payload']) or
                source['payload']['video_id']!=source['source_id'] or source['payload']['url']!='https://www.youtube.com/watch?v='+source['source_id'] or
                binding['channel_id']!=source['payload']['channel_id'] or binding['channel_id'] not in CHANNELS or
                source['result'].get('download_performed') is not True or any(source['result'].get(k) is not False for k in ('publish_performed','human_review','publication_authorized')) or
                plan['planning_policy'].get('prefer_single_whole_passthrough_v5') is not True or digest(plan)!=source['payload']['whole_replay_plan_sha256'] or
                binding['whole_replay_plan']!=plan or binding['source_plan']!=transfer or asset['source_plan']!=transfer or
                asset['selected_formats']!=transfer['selected_formats'] or not 0<asset['bytes']<=transfer['planned_output_max_bytes'] or
                abs(asset['duration']-plan['duration'])>2):
            raise ValueError()
        root=settings.root;area=root/'media'/source['job_id']/'youtube-whole-v4'
        if Path(asset['file'])!=area/'original.mp4':raise ValueError()
        receipt,receipt_ref=document(root,area/'verified.json',262144,check)
        if (receipt['version']!=DOWNLOAD_VERSION or receipt['binding_sha256']!=digest(binding) or receipt['source_asset']!=asset or
                receipt['receipt']!=state['receipt'] or receipt['receipt'].get('source_to_merged_packet_coverage_verified') is not True or
                receipt['receipt']['source_plan']!=transfer or receipt['receipt']['source_stream_config']!=asset['stream_config'] or
                any(receipt.get(k) is not False for k in ('human_review','publication_authorized','creator_original_bytes_verified'))):raise ValueError()
        delivery,manifest_ref=document(root,area/'delivery-v5/manifest.json',2_000_000,check)
        if (summary['manifest_reference']!=manifest_ref or summary['manifest']!=manifest_ref['file'] or
                summary['version']!='replay-parts-delivery-v5' or delivery['version']!='replay-parts-delivery-manifest-v5' or
                delivery['whole_plan_sha256']!=digest(plan) or summary['whole_plan_sha256']!=digest(plan) or
                summary['source_sha256']!=asset['sha256'] or summary['source_duration_seconds']!=asset['duration'] or
                summary['outputs']!=delivery['outputs'] or summary['processing_model']!=delivery['processing_model'] or
                summary.get('packet_coverage_verified') is not True or summary.get('publishable') is not False or
                any(delivery['source'].get(k)!=asset[k] for k in ('file','bytes','sha256','duration','stream_config','packet_evidence'))):raise ValueError()
        outputs=delivery['outputs'];model=delivery['processing_model'];fits=asset['bytes']<=MAX_SINGLE_BYTES and asset['duration']<=MAX_SINGLE_SECONDS
        if not 1<=len(outputs)<=32 or len(delivery['output_packet_coverage'])!=len(outputs):raise ValueError()
        if model=='single_whole_passthrough_v5':
            if not fits or len(outputs)!=1 or state['state']!='single_output_verified_v5':raise ValueError()
        elif model=='lossless_segmented_v4':
            if fits or state['state']!='parts_verified':raise ValueError()
        else:raise ValueError()
        combined=empty_evidence()
        for index,(output,packets) in enumerate(zip(outputs,delivery['output_packet_coverage'])):
            expected=area/'original.mp4' if model=='single_whole_passthrough_v5' else area/'parts-v4'/f'part-{index:03d}.mp4'
            output_key=digest({'version':'replay-delivery-output-idempotency-v5','job_id':source['job_id'],'whole_plan_sha256':digest(plan),
                'source_sha256':asset['sha256'],'processing_model':model,'index':index,'output_sha256':output['sha256']})
            if (set(output)!={'index','file','bytes','sha256','duration','part_idempotency_key'} or output['index']!=index or type(output['index']) is not int or
                    Path(output['file'])!=expected or type(output['bytes']) is not int or not 0<output['bytes']<=MAX_SINGLE_BYTES or
                    type(output['duration']) not in (int,float) or not math.isfinite(output['duration']) or not 0<output['duration']<=MAX_SINGLE_SECONDS or
                    not re.fullmatch('[a-f0-9]{64}',output['sha256']) or output['part_idempotency_key']!=output_key or
                    packets['index']!=index or packets['packet_before']!=combined or set(packets['packet_after'])!={'audio','video'} or
                    any(set(packets['packet_after'][kind])!={'packets','chain'} or type(packets['packet_after'][kind]['packets']) is not int or
                        packets['packet_after'][kind]['packets']<combined[kind]['packets'] or
                        not re.fullmatch('[a-f0-9]{64}',packets['packet_after'][kind]['chain']) for kind in ('audio','video'))):raise ValueError()
            combined=packets['packet_after']
            if verify_all or verify_index==index:
                current,_=read_checked(root,expected,maximum=MAX_SINGLE_BYTES,check=check,collect=False)
                details=inspect_media(expected,include_frame_rates=True,check=check)
                if (current['sha256']!=output['sha256'] or current['bytes']!=output['bytes'] or abs(details['duration']-output['duration'])>.05 or
                        details['stream_config']!=asset['stream_config']):raise ValueError()
                validate_quality(details['stream_config']['video'],details['stream_config']['audio'],binding)
        if combined!=asset['packet_evidence']:raise ValueError()
        if any(type(combined[k]['packets']) is not int or not 0<combined[k]['packets']<=64_000_000 for k in ('audio','video')):raise ValueError()
        proof,proof_ref=document(root,delivery['processing_evidence']['file'],2_097_152,check)
        if proof_ref!=delivery['processing_evidence']:raise ValueError()
        if model=='single_whole_passthrough_v5':
            if (Path(proof_ref['file'])!=area/'delivery-v5/whole-proof.json' or
                    proof['version']!='verified-whole-passthrough-proof-v5' or proof['job_id']!=source['job_id'] or
                    proof['whole_plan_sha256']!=digest(plan) or proof['source']!=delivery['source'] or
                    proof['human_review'] is not False or proof['publishable'] is not False or proof['media_copied'] is not False):raise ValueError()
        else:
            if (Path(proof_ref['file'])!=area/'parts-v4/manifest.json' or proof['source_sha256']!=asset['sha256'] or
                    proof['source_packets']!=asset['packet_evidence'] or proof['whole_plan_sha256']!=digest(plan) or
                    proof['packet_coverage_verified'] is not True):raise ValueError()
        ledger=store.query('SELECT * FROM replay_delivery_outputs_v5 WHERE job_id=? ORDER BY output_index',(source['job_id'],))
        if len(ledger)!=len(outputs):raise ValueError()
        for output,row in zip(outputs,ledger):
            if (row['output_index']!=output['index'] or row['idempotency_key']!=output['part_idempotency_key'] or
                    row['manifest_sha256']!=manifest_ref['sha256'] or json.loads(row['record'])!=output or row['state']!='prepared' or
                    row['external_publish_id'] is not None):raise ValueError()
        if abs(sum(o['duration'] for o in outputs)-asset['duration'])>2:raise ValueError()
        if model=='single_whole_passthrough_v5' and any(outputs[0][k]!=asset[k] for k in ('file','bytes','sha256','duration')):raise ValueError()
        if verify_all and model!='single_whole_passthrough_v5':
            current,_=read_checked(root,area/'original.mp4',maximum=32*MAX_SINGLE_BYTES,check=check,collect=False)
            if current['sha256']!=asset['sha256'] or current['bytes']!=asset['bytes']:raise ValueError()
        return {'source':source,'state':state,'asset':asset,'plan':plan,'delivery':delivery,'manifest':manifest_ref,'receipt':receipt_ref}
    except HubError:raise
    except (OSError,ValueError,KeyError,TypeError,AttributeError,OverflowError):fail('REPLAY_PART_VERIFIED_DELIVERY_REQUIRED')


class ReplayPartVideoWeb(ReplayVideoWeb):
    VERSION,POLICY,STATE,KIND=VERSION,POLICY,STATE,KIND
    MAX_BYTES,MAX_SECONDS=MAX_SINGLE_BYTES,MAX_SINGLE_SECONDS
    POLICY_FILE='replay-part-video-policy-v5.toml'
    PUBLIC_MODE='workflow_authorization_and_part_machine_acceptance_v5'
    NATIVE_DIRECTORY='replay-browser-part-v5'
    NATIVE_PREPARATION_VERSION='replay-part-native-preparation-v5'
    NATIVE_FORM_VERSION='replay-part-native-form-observation-v5'
    RESULT_OBSERVATION_VERSION='replay-part-browser-result-v5'
    NATIVE_RESERVATION_VERSION='replay-part-native-reservation-v5'
    WORKFLOW_AUTHORIZATION_VERSION='replay-part-workflow-authorization-v5'
    MACHINE_PROOF_VERSION='verified-replay-part-machine-checks-v5'
    MACHINE_PROOF_DIRECTORY='replay-part-machine-checks-v5'

    def __init__(self,settings,store,*,part_index=0,machine_acceptance_verifier=None):
        self.part_index=part_index;self.JOB_TYPE=part_type(part_index);self.KIND=part_kind(part_index)
        super().__init__(settings,store,machine_acceptance_verifier=machine_acceptance_verifier or PartMachineChecks(settings,store,part_index=part_index))
        self._admitting=False

    def policy(self):
        try:
            file=self.settings.root/'config'/self.POLICY_FILE
            if file.resolve()!=file or any(p.is_symlink() for p in (file,*file.parents)) or os.name=='posix' and file.stat().st_mode&0o022:raise ValueError()
            raw=file.read_bytes()
            if not 0<len(raw)<=16384:raise ValueError()
            p=tomllib.loads(raw.decode('utf-8-sig'));ids=p['acceptance_video_ids'];scope=p['source_scope']
            if (p['version']!=POLICY or p['enabled'] is not True or p['target_site_uid']!=self.settings.config['target_bilibili_site_uid'] or
                    p['target_account_name']!=self.settings.config['target_bilibili_account_name'] or p['public_submission_mode']!=self.PUBLIC_MODE or
                    p['source_not_before']!=CUTOFF or len(p['allowed_channel_ids'])!=2 or set(p['allowed_channel_ids'])!=CHANNELS or
                    type(p['automation_enabled']) is not bool or not isinstance(ids,list) or
                    scope not in ('pilot_single','all_eligible_ended_since_operating_cutoff') or
                    scope=='pilot_single' and (len(ids)!=1 or not re.fullmatch('[A-Za-z0-9_-]{11}',ids[0])) or
                    scope!='pilot_single' and ids or _timestamp(p['activated_at'])>time.time() or
                    any(not _reference(p.get(k)) or p[k].startswith(('REPLACE','UNCONFIGURED')) for k in ('owner_authorization_reference','owner_permission_reference','workflow_authorization_reference',
                        'actual_owner_workflow_response','owner_response_reference'))):raise ValueError()
            return {**p,'_sha256':hashlib.sha256(raw).hexdigest(),'_activated_epoch':_timestamp(p['activated_at'])}
        except (OSError,ValueError,KeyError,TypeError,AttributeError):fail('REPLAY_PART_POLICY_REQUIRED')

    def _operator(self,principal):
        if principal==ADMISSION and self._admitting:return
        return super()._operator(principal)

    def create_candidate(self,principal,*args,**kwargs):
        self._operator(principal);fail('REPLAY_PART_USE_REGISTERED_GROUP_ADMISSION')

    def create_acceptance(self,principal,*args,**kwargs):
        self._operator(principal);fail('REPLAY_PART_USE_REGISTERED_GROUP_ADMISSION')

    def operator_retry_private_upload(self,principal,*args,**kwargs):
        self._operator(principal);fail('REPLAY_PART_PRIVATE_UPLOAD_RECONCILIATION_REQUIRED')

    def _verified_source(self,download_job_id,*,verify_artifact_hash):
        context=verified_delivery(self.settings,self.store,download_job_id,check=self.assert_worker_lease,
            verify_index=self.part_index if verify_artifact_hash else None)
        if self.part_index>=len(context['delivery']['outputs']):fail('REPLAY_PART_INDEX_INVALID')
        output=deepcopy(context['delivery']['outputs'][self.part_index]);asset={**context['asset'],**output}
        asset['_delivery_context']=context
        return context['source'],asset,Path(context['receipt']['file'])

    def _source_scope(self,source,policy):
        try:
            raw=source['payload']['raw'];start=_timestamp(raw['liveStreamingDetails']['actualStartTime']);end=_timestamp(raw['liveStreamingDetails']['actualEndTime'])
            if (raw['id']!=source['source_id'] or source['payload']['source_api']!='youtube-data-v3-official' or
                    raw['snippet']['channelId']!=source['payload']['channel_id'] or source['payload']['channel_id'] not in CHANNELS or
                    raw['status']['privacyStatus']!='public' or not _timestamp(CUTOFF)<=start<=end<=time.time()+30 or
                    policy['source_scope']=='pilot_single' and policy['acceptance_video_ids']!=[source['source_id']]):raise ValueError()
            observed=self.store.query('SELECT * FROM youtube_videos WHERE video_id=?',(source['source_id'],))
            if (len(observed)!=1 or observed[0]['state']!='ended' or observed[0]['source_api']!='youtube-data-v3-official' or
                    official_identity(json.loads(observed[0]['metadata']))!=official_identity(raw)):raise ValueError()
        except (ValueError,KeyError,TypeError,AttributeError):fail('REPLAY_PART_SOURCE_SCOPE_REQUIRED')

    def _native_base(self,job,binding,state):
        return {'job_id':job['job_id'],'attempt_id':state['attempt_id'],'site_uid':binding['site_uid'],
            'source_id':binding['source_id'],'source_url':binding['source_url'],'artifact_sha256':binding['artifact_sha256'],
            'file_bytes':binding['file_bytes'],'metadata_sha256':binding['metadata_sha256'],'binding_sha256':digest(binding),
            'policy_sha256':binding['policy_sha256'],'parent_job_id':binding['parent_job_id'],'part_index':binding['part_index'],
            'part_count':binding['part_count'],'part_idempotency_key':binding['part_idempotency_key'],
            'parts_manifest_sha256':binding['parts_manifest_sha256'],'whole_plan_sha256':binding['whole_plan_sha256']}

    def _registry(self,job,binding,db=None):
        query=(lambda sql,args:db.execute(sql,args).fetchall()) if db is not None else self.store.query
        try:
            if (index_from_job(job)!=self.part_index or binding['version']!=VERSION or binding['source_id']!=job['source_id'] or
                    binding['human_review'] is not False or binding['part_index']!=self.part_index or
                    type(binding['part_count']) is not int or not self.part_index<binding['part_count']<=32):raise ValueError()
            groups=query('SELECT * FROM replay_part_groups_v5 WHERE parent_job_id=?',(binding['parent_job_id'],))
            children=query('SELECT * FROM replay_part_children_v5 WHERE parent_job_id=? ORDER BY part_index',(binding['parent_job_id'],))
            if len(groups)!=1:raise ValueError()
            group=groups[0]
            if (len(children)!=binding['part_count'] or group['source_id']!=job['source_id'] or group['target_uid']!=binding['site_uid'] or
                    group['download_job_id']!=binding['download_job_id'] or group['parts_manifest_sha256']!=binding['parts_manifest_sha256'] or
                    group['policy_sha256']!=binding['policy_sha256'] or group['part_count']!=binding['part_count']):raise ValueError()
            key=digest(['bilibili','youtube',job['source_id'],'site_uid:'+binding['site_uid'],binding['parts_manifest_sha256'],
                self.part_index,binding['artifact_sha256'],self.KIND])
            child=children[self.part_index]
            if (any(c['part_index']!=i for i,c in enumerate(children)) or child['child_job_id']!=job['job_id'] or
                    child['idempotency_key']!=key or binding['part_idempotency_key']!=key or child['part_sha256']!=binding['artifact_sha256']):raise ValueError()
            parent=self.store.get(group['parent_job_id'])
            if (parent['job_type']!=GROUP_JOB or parent['policy_version']!=POLICY or parent['source_id']!=job['source_id'] or
                    parent['payload']['site_uid']!=binding['site_uid'] or parent['payload']['part_count']!=len(children) or
                    parent['payload']['parts_manifest_sha256']!=binding['parts_manifest_sha256']):raise ValueError()
            return group,children,key
        except (KeyError,ValueError,TypeError,IndexError,AttributeError):fail('REPLAY_PART_REGISTRY_BINDING_CHANGED')

    def snapshot(self,job_id,*,write=True):
        job=self.store.get(job_id)
        if not job or job['job_type']!=self.JOB_TYPE or job['policy_version']!=POLICY or job['source_platform']!='youtube':fail('REPLAY_PART_JOB_REQUIRED')
        binding=job['payload'];state=job['result'].get(STATE,{})
        _,_,key=self._registry(job,binding)
        if state.get('version')!=VERSION or state.get('key')!=key or state.get('human_review') is not False:fail('REPLAY_PART_CHECKPOINT_INVALID')
        if write:self._validate_snapshot_binding(job,binding,verify_artifact_hash=True)
        return job,binding,state

    def _checkpoint_matches(self,current,job):
        if job['state']=='processing':return super()._checkpoint_matches(current,job)
        if current is None:return False
        current=dict(current)
        for key in ('payload','result'):current[key]=json.loads(current[key])
        return current==job

    def _validate_snapshot_binding(self,job,binding,*,verify_artifact_hash):
        policy=self.policy();self._registry(job,binding)
        source,asset,_=self._verified_source(binding['download_job_id'],verify_artifact_hash=verify_artifact_hash)
        self._source_scope(source,policy);context=asset['_delivery_context'];out=context['delivery']['outputs'][self.part_index]
        expected_contract={'version':'replay-part-source-contract-v5','processing_model':context['delivery']['processing_model'],
            'download_job_id':source['job_id'],'whole_plan_sha256':digest(context['plan']),'source_sha256':context['asset']['sha256'],
            'source_bytes':context['asset']['bytes'],'source_duration_seconds':wire_seconds(context['asset']['duration']),
            'source_actual_start_time':source['payload']['raw']['liveStreamingDetails']['actualStartTime'],'operating_not_before':CUTOFF,
            'delivery_manifest':context['manifest'],'download_receipt':context['receipt'],'output':out}
        if (binding['source_contract']!=expected_contract or binding['parts_manifest_sha256']!=context['manifest']['sha256'] or
                policy['target_site_uid']!=context['plan']['planning_policy']['target_site_uid'] or
                binding['policy_sha256']!=policy['_sha256'] or binding['site_uid']!=policy['target_site_uid'] or binding['account_name']!=policy['target_account_name'] or
                binding['source_url']!=source['payload']['url'] or binding['channel_id']!=source['payload']['channel_id'] or
                binding['metadata']!=part_metadata(self.settings,source,self.part_index,binding['part_count']) or
                binding['metadata_sha256']!=digest(self._metadata(binding['metadata'],binding['source_url'])) or
                any(binding[a]!=out[b] for a,b in (('file','file'),('file_bytes','bytes'),('artifact_sha256','sha256'),('duration_seconds','duration'))) or
                self.store.query("SELECT 1 FROM source_tombstones WHERE source_platform='youtube' AND source_id=?",(job['source_id'],))):fail('REPLAY_PART_SOURCE_BINDING_CHANGED')
        return policy

    def _require_workflow_launch(self,policy):
        return ReplayPartGroupsV5(self.settings,self.store).require_launch(policy)

    def start_upload(self,principal,job_id,*,attempt_id,site_uid):
        self._operator(principal);job,binding,state=self.snapshot(job_id)
        try:
            if str(uuid.UUID(attempt_id))!=attempt_id:raise ValueError()
        except (ValueError,TypeError,AttributeError):fail('REPLAY_PART_ATTEMPT_INVALID')
        if (state['state']!='prepared' or state.get('attempt_id')!=attempt_id or site_uid!=binding['site_uid'] or
                state.get('private_upload_selections',0)!=0):fail('REPLAY_PART_UPLOAD_RECONCILE_REQUIRED')
        with self.store.transaction() as db:
            self._source_publications(job,binding,db,allow_current=False)
            self._previous_parts_complete(job,binding,db)
        return self._update(principal,job,{**state,'state':'uploading','private_upload_selections':1},job_state='upload_pending',stage='replay_part_uploading')

    def _source_publications(self,job,binding,db,*,allow_current=True):
        _,children,key=self._registry(job,binding,db)
        siblings={c['child_job_id']:c for c in children}
        pubs=db.execute("SELECT * FROM publications WHERE source_platform='youtube' AND source_id=?",(job['source_id'],)).fetchall()
        for row in pubs:
            sibling=siblings.get(row['job_id'])
            if (not sibling or row['platform']!='bilibili' or row['kind']!=part_kind(sibling['part_index']) or row['account_id']!='site_uid:'+binding['site_uid'] or
                    row['idempotency_key']!=sibling['idempotency_key']):fail('REPLAY_PART_FOREIGN_SOURCE_PUBLICATION_REQUIRES_LOOKUP')
            sibling_job=self.store.get(sibling['child_job_id'])
            sibling_service=ReplayPartVideoWeb(self.settings,self.store,part_index=sibling['part_index'])
            sibling_service._registry(sibling_job,sibling_job['payload'],db)
            if not allow_current and row['idempotency_key']==key:fail('REPLAY_PART_UPLOAD_RECONCILE_REQUIRED')
        if db.execute("SELECT 1 FROM source_tombstones WHERE source_platform='youtube' AND source_id=?",(job['source_id'],)).fetchone():fail('REPLAY_PART_SOURCE_TOMBSTONED')
        return pubs

    def _previous_parts_complete(self,job,binding,db):
        _,children,_=self._registry(job,binding,db)
        for row in children[:self.part_index]:
            child=self.store.get(row['child_job_id']);state=child['result'].get(STATE,{})
            pubs=db.execute('SELECT * FROM publications WHERE job_id=? AND idempotency_key=?',(child['job_id'],row['idempotency_key'])).fetchall()
            o=state.get('result_observation',{});bv=row['external_id']
            if (row['state']!='completed' or child['state']!='completed' or state.get('state')!='completed' or len(pubs)!=1 or
                    not re.fullmatch('BV[A-Za-z0-9]{10}',bv or '') or pubs[0]['state']!='completed' or pubs[0]['kind']!=part_kind(row['part_index']) or
                    pubs[0]['external_id']!=bv or pubs[0]['external_url']!='https://www.bilibili.com/video/'+bv or child['external_publish_id']!=bv or
                    o.get('version')!=self.RESULT_OBSERVATION_VERSION or o.get('external_id')!=bv or o.get('public_accessible') is not True or
                    o.get('origin')!='later_native_lookup_reconciled_v5' or o.get('same_submit_attempt_observed') is not False):
                fail('REPLAY_PART_PREVIOUS_PART_REQUIRES_LOOKUP')
            # Historical small evidence, not a fresh sending grant or a second
            # public-player request. The prior ordinal's accepted BV is stable.
            evidence=reference_body(self.settings.root,o['native_result_evidence'])
            previous_service=ReplayPartVideoWeb(self.settings,self.store,part_index=row['part_index'])
            previous_base=previous_service._native_base(child,child['payload'],state)
            if (evidence['version']!='replay-later-native-result-observation-v2' or evidence['external_id']!=bv or
                    any(evidence.get(k)!=v for k,v in previous_base.items())):fail('REPLAY_PART_PREVIOUS_PART_REQUIRES_LOOKUP')

    def _reserve_submit_once(self,principal,job,binding,state,*,site_uid):
        if site_uid!=binding['site_uid'] or state['key']!=binding['part_idempotency_key']:fail('REPLAY_PART_RESERVATION_BINDING_CHANGED')
        target=self._checkpoint_job_state(job,'publish_unknown');now=time.time()
        with self.store.transaction() as db:
            current=db.execute('SELECT * FROM jobs WHERE job_id=?',(job['job_id'],)).fetchone()
            if not self._checkpoint_matches(current,job):fail('REPLAY_PART_JOB_CHANGED')
            pubs=self._source_publications(job,binding,db)
            existing=next((p for p in pubs if p['idempotency_key']==state['key']),None)
            if existing:return {'can_click_submit':False,'state':existing['state'],'reconcile_required':True}
            self._previous_parts_complete(job,binding,db)
            if db.execute("UPDATE replay_part_children_v5 SET state='dispatching' WHERE child_job_id=? AND idempotency_key=? AND state='prepared' AND external_id IS NULL",
                    (job['job_id'],state['key'])).rowcount!=1:fail('REPLAY_PART_CHILD_RESERVATION_CHANGED')
            db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',(state['key'],job['job_id'],'bilibili','youtube',job['source_id'],
                'site_uid:'+site_uid,self.KIND,'dispatching',None,None,now))
            db.execute("UPDATE jobs SET state=?,stage='replay_part_submission_reserved',error_code='RECONCILE_BEFORE_RETRY',result=?,updated_at=? WHERE job_id=?",
                (target,encode({**job['result'],STATE:{**state,'state':'submitting'}}),now,job['job_id']))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',(now,principal,'replay_part_reserve_submit',job['job_id'],'one_bound_part_one_click_reserved'))
        self._checkpoint_committed(job['job_id'])
        return {'can_click_submit':True,'state':'dispatching','idempotency_key':state['key']}

    def record_upload(self,principal,job_id,observation):
        self._operator(principal);job,binding,state=self.snapshot(job_id,write=False)
        self._validate_snapshot_binding(job,binding,verify_artifact_hash=False)
        base=self._native_base(job,binding,state);folder=self.settings.root/'media'/job_id/self.NATIVE_DIRECTORY/state['attempt_id']
        try:
            body,ref=document(self.settings.root,folder/'upload-observation.json',check=self.assert_worker_lease)
            if (state['state']!='uploading' or body!=observation or body['version']!='replay-part-browser-upload-v5' or
                    any(body.get(k)!=v or type(body.get(k)) is not type(v) for k,v in base.items()) or
                    body['same_attempt_observed'] is not True or body['server_file_hash_verified'] is not False):raise ValueError()
            native_surface(binding,body['native_surface'])
            cov=reference_body(self.settings.root,body['upload_coverage'],expected=folder/'upload-coverage.json',check=self.assert_worker_lease)
            if (cov['version']!='replay-part-native-upload-coverage-v5' or
                    any(cov.get(k)!=v or type(cov.get(k)) is not type(v) for k,v in base.items()) or
                    cov['file']!=binding['file'] or cov['selected_filename']!=Path(binding['file']).name or
                    cov['native_filename']['observed'] is not True or cov['native_filename']['kind'] not in ('file_input','visible_filename','native_title_input_after_upload') or
                    cov['native_filename']['text'] not in (Path(binding['file']).name,Path(binding['file']).stem) or
                    cov['native_upload_complete'] is not True or cov['whole_file_streamed'] is not True or
                    cov['streamed_sha256']!=binding['artifact_sha256'] or cov['streamed_bytes']!=binding['file_bytes'] or type(cov['streamed_bytes']) is not int or
                    cov['server_file_hash_verified'] is not False or cov['human_review'] is not False or
                    not 0<=time.time()-_timestamp(cov['observed_at'])<=300):raise ValueError()
            intent,intentref=document(self.settings.root,self.settings.root/'media'/job_id/'replay-native-intents/video-selection.json',check=self.assert_worker_lease)
            if (intent['version']!='replay-part-one-use-intent-v5' or intent['kind']!='video-selection' or
                    any(intent.get(k)!=v for k,v in base.items()) or _timestamp(intent['created_at'])>_timestamp(cov['observed_at']) or
                    body['upload_reference_sha256']!=digest({**base,'local_causal_intent':intentref,'native_upload_complete':True,'upload_coverage':body['upload_coverage']})):raise ValueError()
        except (KeyError,ValueError,TypeError,AttributeError):fail('REPLAY_PART_UPLOAD_EVIDENCE_REQUIRED')
        return self._update(principal,job,{**state,'state':'uploaded','upload_observation':body,'upload_evidence':ref,'upload_coverage':cov},
            job_state='upload_pending',stage='replay_part_uploaded')

    def _native_preparation(self,job,binding,state,observation,proof):
        try:
            expected={**self._native_base(job,binding,state),'cover_sha256':proof['checks']['cover']['output']['sha256'],
                'cover_provenance_sha256':proof['checks']['cover']['provenance']['sha256'],
                'upload_reference_sha256':state['upload_observation']['upload_reference_sha256'],
                'title':binding['metadata']['title'],'description':binding['metadata']['description'],'creation_statement':'内容为转载',
                'source':binding['source_url'],'category':binding['metadata']['category_label'],'metadata_verified':True,'cover_applied':True,
                'server_file_hash_verified':False,'human_review':False,'per_artifact_owner_acceptance':False}
            fields=set(expected)|{'version','tags','native_surface','observed_at','private_draft_saved','pre_submit_screenshot','native_observation_file'}
            if (not isinstance(observation,dict) or set(observation)!=fields or observation['version']!=self.NATIVE_PREPARATION_VERSION or
                    any(observation[k]!=v or type(observation[k]) is not type(v) for k,v in expected.items()) or
                    observation['private_draft_saved'] is not True or not isinstance(observation['tags'],list) or
                    len(observation['tags'])!=len(binding['metadata']['tags']) or set(observation['tags'])!=set(binding['metadata']['tags']) or
                    not 0<=time.time()-_timestamp(observation['observed_at'])<=300):raise ValueError()
            native_surface(binding,observation['native_surface'],metadata=True)
            folder=self.settings.root/'media'/job['job_id']/self.NATIVE_DIRECTORY/state['attempt_id']
            image=reference_body(self.settings.root,observation['pre_submit_screenshot'],maximum=20_000_000,
                expected=folder/'pre-submit.png',check=self.assert_worker_lease,json_body=False)
            inner=reference_body(self.settings.root,observation['native_observation_file'],expected=folder/'native-preparation-observation.json',check=self.assert_worker_lease)
            expected_inner={k:v for k,v in observation.items() if k not in ('pre_submit_screenshot','native_observation_file','private_draft_saved')}
            expected_inner['version']=self.NATIVE_FORM_VERSION
            if (inner!=expected_inner or len(image)<33 or image[:16]!=b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' or
                    not 0<int.from_bytes(image[16:20],'big')<=10000 or not 0<int.from_bytes(image[20:24],'big')<=10000):raise ValueError()
            return deepcopy(observation)
        except (KeyError,ValueError,TypeError,AttributeError):fail('REPLAY_PART_NATIVE_PREPARATION_REQUIRED')

    def reserve_submit(self,principal,job_id,*,attempt_id,site_uid,upload_reference_sha256,
            machine_acceptance_receipt_sha256=None,native_refresh=None):
        self._operator(principal);job,binding,state=self.snapshot(job_id,write=False)
        policy=self._validate_snapshot_binding(job,binding,verify_artifact_hash=False);self._require_workflow_launch(policy)
        auth=state.get('automation_authorization',{})
        if (state['state']!='uploaded' or state.get('public_submission_approved') is not False or 'owner_acceptance' in state or
                auth.get('version')!=self.WORKFLOW_AUTHORIZATION_VERSION or auth.get('binding_sha256')!=digest(binding) or
                auth.get('workflow_authorization_reference')!=policy['workflow_authorization_reference'] or
                auth.get('human_review') is not False or auth.get('per_artifact_owner_acceptance') is not False or
                any(not _reference(auth.get(k)) for k in ('actual_owner_workflow_response','owner_response_reference')) or
                state.get('attempt_id')!=attempt_id or site_uid!=binding['site_uid'] or
                upload_reference_sha256!=state.get('upload_observation',{}).get('upload_reference_sha256')):fail('REPLAY_PART_WORKFLOW_AUTHORIZATION_REQUIRED')
        proof=self._machine_acceptance(job,binding,state,policy,machine_acceptance_receipt_sha256)
        if native_refresh is not None:
            if self._worker_job is None or not callable(native_refresh):fail('REPLAY_PART_NATIVE_REFRESH_WORKER_REQUIRED')
            self.assert_worker_lease();observed=native_refresh();self.assert_worker_lease()
            prepared=self._native_preparation(job,binding,state,observed,proof)
            self._update(principal,job,{**state,'native_preparation':prepared},job_state=job['state'],stage='replay_part_native_refreshed')
            job,binding,state=self.snapshot(job_id,write=False)
        else:self._native_preparation(job,binding,state,state.get('native_preparation'),proof)
        result=self._reserve_submit_once(principal,job,binding,{**state,'machine_acceptance':proof},site_uid=site_uid)
        if result['can_click_submit'] is not True:return result
        issued=time.time();ticket={**result,**self._native_base(job,binding,state),'version':self.NATIVE_RESERVATION_VERSION,
            'ticket_id':str(uuid.uuid4()),'issued_at':issued,'expires_at':issued+120,'machine_evidence_sha256':proof['evidence_sha256'],
            'upload_reference_sha256':upload_reference_sha256}
        folder=self.settings.root/'media'/job_id/self.NATIVE_DIRECTORY/attempt_id
        atomic_json(folder/'reservation.json',ticket);(folder/'reservation.json').chmod(0o600)
        return ticket

    def mark_unknown(self,principal,job_id):
        result=super().mark_unknown(principal,job_id)
        job,_,state=self.snapshot(job_id,write=False)
        if state['state']=='submit_unknown':
            with self.store.transaction() as db:
                db.execute("UPDATE replay_part_children_v5 SET state='unknown' WHERE child_job_id=? AND idempotency_key=? AND state='dispatching'",
                    (job_id,state['key']))
        return result

    def record_result(self,principal,job_id,observation,*,lookup=False,result_reference=None):
        """A later observed ordinary BV; no same-click claim or sending gate."""
        self._operator(principal);job,binding,state=self.snapshot(job_id,write=False)
        if not lookup or state['state'] not in ('submitting','submit_unknown','awaiting_moderation','completed','failed'):
            fail('REPLAY_PART_LOOKUP_RESULT_REQUIRED')
        from .replay_part_public_evidence_v5 import validate_result
        coverage=validate_result(self,job,binding,state,observation)
        if result_reference is not None:
            raw=reference_body(self.settings.root,result_reference)
            if raw!=observation:fail('REPLAY_PART_RESULT_REFERENCE_CHANGED')
        self.assert_worker_lease();now=time.time();target=self._checkpoint_job_state(job,'completed')
        with self.store.transaction() as db:
            current=db.execute('SELECT * FROM jobs WHERE job_id=?',(job_id,)).fetchone()
            if not self._checkpoint_matches(current,job):fail('REPLAY_PART_JOB_CHANGED')
            pubs=self._source_publications(job,binding,db);pub=[p for p in pubs if p['idempotency_key']==state['key']]
            if (len(pub)!=1 or pub[0]['job_id']!=job_id or pub[0]['state'] not in ('dispatching','unknown','awaiting_moderation','completed') or
                    pub[0]['external_id'] not in (None,observation['external_id'])):fail('REPLAY_PART_PUBLICATION_CHANGED')
            updated={**state,'state':'completed','result_observation':deepcopy(observation),'public_coverage':coverage}
            db.execute("UPDATE publications SET state='completed',external_id=?,external_url=?,updated_at=? WHERE idempotency_key=? AND job_id=?",
                (observation['external_id'],observation['url'],now,state['key'],job_id))
            if db.execute("UPDATE replay_part_children_v5 SET state='completed',external_id=? WHERE child_job_id=? AND idempotency_key=? AND state IN ('dispatching','unknown','awaiting_moderation','completed') AND (external_id IS NULL OR external_id=?)",
                    (observation['external_id'],job_id,state['key'],observation['external_id'])).rowcount!=1:fail('REPLAY_PART_CHILD_RESULT_CHANGED')
            db.execute('UPDATE jobs SET state=?,stage=?,error_code=NULL,result=?,external_publish_id=?,updated_at=? WHERE job_id=?',
                (target,'replay_part_later_public_result_recorded',encode({**job['result'],STATE:updated,'publish_performed':True}),observation['external_id'],now,job_id))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                (now,principal,'replay_part_later_public_result_v5',job_id,encode({'external_id':observation['external_id'],
                    'native_result_evidence':observation['native_result_evidence'],'same_submit_attempt_observed':False,'human_review':False})))
        self._checkpoint_committed(job_id)
        aggregate=ReplayPartGroupsV5(self.settings,self.store).aggregate(binding['parent_job_id'])
        return {'job_id':job_id,'state':target,'checkpoint':'completed','external_id':observation['external_id'],'parent_state':aggregate['state']}

    def cleanup_eligibility(self,job_id):
        job,binding,state=self.snapshot(job_id,write=False)
        return {'job_id':job_id,'eligible':False,'reason':'REPLAY_PART_WHOLE_GROUP_REQUIRED',
            'parent_job_id':binding['parent_job_id'],'single_part_completion_does_not_release_whole_source':True}


class ReplayPartGroupsV5:
    def __init__(self,settings,store):
        self.settings,self.store=settings,store

    def _retire_legacy_processes(self,db,source,download_job_id,group_id,now):
        rows=[
            dict(row) for row in db.execute(
                "SELECT * FROM jobs WHERE source_platform='youtube' AND source_id=? "
                "AND job_type='video_process' ORDER BY created_at,job_id",
                (source['source_id'],),
            )
        ]
        retired=[]
        for row in rows:
            if row['state'] in ('completed','failed'):
                continue
            if (
                row['state']!='pending'
                or row['attempts']!=0
                or row['error_code'] is not None
                or row['external_publish_id'] is not None
                or row['lease_owner'] is not None
                or row['lease_until'] is not None
                or row['next_run']!=0
                or row['stage']!='discovered'
            ):
                fail('REPLAY_PART_LEGACY_PROCESS_ACTIVE')
            try:
                payload=json.loads(row['payload'])
                result=json.loads(row['result'])
            except (TypeError,ValueError):
                fail('REPLAY_PART_LEGACY_PROCESS_RETIREMENT_UNSAFE')
            if (
                payload.get('video_id')!=source['source_id']
                or payload.get('url')!='https://www.youtube.com/watch?v='+source['source_id']
                or result!={}
            ):
                fail('REPLAY_PART_LEGACY_PROCESS_RETIREMENT_UNSAFE')
            old=deepcopy(row)
            old['payload']=payload
            old['result']=result
            old_sha=digest(old)
            record={
                'version':LEGACY_PROCESS_RETIREMENT,
                'source_id':source['source_id'],
                'download_job_id':download_job_id,
                'parent_job_id':group_id,
                'job':old,
                'job_sha256':old_sha,
                'publications':[],
                'attempts_preserved':0,
                'media_moved_or_deleted':False,
                'publications_created':0,
                'provider_requests':0,
            }
            plan_sha=digest(record)
            folder=self.settings.root/'media'/download_job_id/'legacy-source-process-retirement-v5'
            if folder.exists():
                if folder.is_symlink() or not folder.is_dir():
                    fail('REPLAY_PART_LEGACY_PROCESS_RETIREMENT_UNSAFE')
            else:
                folder.mkdir(parents=True,mode=0o700)
            record_file=folder/(plan_sha+'.json')
            if record_file.exists():
                if record_file.is_symlink() or not record_file.is_file():
                    fail('REPLAY_PART_LEGACY_PROCESS_RETIREMENT_UNSAFE')
                if json.loads(record_file.read_bytes())!=record:
                    fail('REPLAY_PART_LEGACY_PROCESS_RETIREMENT_RECORD_CHANGED')
            else:
                atomic_json(record_file,record)
                record_file.chmod(0o600)
            raw=record_file.read_bytes()
            reference={'file':str(record_file),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
            marker={
                'version':'youtube-legacy-video-process-superseded-v5',
                'plan_sha256':plan_sha,
                'record':reference,
                'old_job_sha256':old_sha,
                'source_id':source['source_id'],
                'group_id':group_id,
                'publications':[],
                'attempts_preserved':0,
                'media_moved_or_deleted':False,
                'publication_created':False,
                'remote_retry_performed':False,
                'provider_requests':0,
            }
            retired_result={**result,LEGACY_PROCESS_MARKER:marker}
            changed=db.execute(
                """UPDATE jobs SET state='failed',stage='superseded_by_replay_part_group',
                   error_code='YOUTUBE_LEGACY_VIDEO_PROCESS_SUPERSEDED',result=?,updated_at=?,
                   next_run=0,lease_owner=NULL,lease_until=NULL
                   WHERE job_id=? AND source_platform='youtube' AND source_id=?
                   AND job_type='video_process' AND state='pending' AND attempts=0
                   AND payload=? AND result=? AND updated_at=? AND stage='discovered'
                   AND error_code IS NULL AND external_publish_id IS NULL
                   AND lease_owner IS NULL AND lease_until IS NULL AND next_run=0""",
                (
                    encode(retired_result),now,row['job_id'],source['source_id'],
                    encode(payload),encode(result),row['updated_at'],
                ),
            ).rowcount
            if changed!=1:
                fail('REPLAY_PART_LEGACY_PROCESS_RETIREMENT_CAS_FAILED')
            audit={
                'version':LEGACY_PROCESS_RETIREMENT,
                'plan_sha256':plan_sha,
                'record':reference,
                'old_job_sha256':old_sha,
                'old_state':'pending',
                'new_state':'failed',
                'attempts_preserved':0,
                'parent_job_id':group_id,
                'publications':[],
                'media_moved_or_deleted':False,
                'publications_created':0,
                'provider_requests':0,
            }
            db.execute(
                'INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                (now,ADMISSION,'youtube_legacy_video_process_superseded_v5',
                 row['job_id'],encode(audit)),
            )
            retired.append({'job_id':row['job_id'],'plan_sha256':plan_sha,'record':reference})
        return retired

    def initialize(self):
        with self.store.transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS replay_part_groups_v5(
                parent_job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),source_id TEXT NOT NULL,target_uid TEXT NOT NULL,
                download_job_id TEXT NOT NULL REFERENCES jobs(job_id),parts_manifest_sha256 TEXT NOT NULL,
                policy_sha256 TEXT NOT NULL,part_count INTEGER NOT NULL CHECK(part_count BETWEEN 1 AND 32),
                state TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL,UNIQUE(source_id,target_uid))''')
            db.execute('''CREATE TABLE IF NOT EXISTS replay_part_children_v5(
                parent_job_id TEXT NOT NULL REFERENCES replay_part_groups_v5(parent_job_id),part_index INTEGER NOT NULL CHECK(part_index BETWEEN 0 AND 31),
                child_job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id),idempotency_key TEXT NOT NULL UNIQUE,
                part_sha256 TEXT NOT NULL,state TEXT NOT NULL,external_id TEXT,PRIMARY KEY(parent_job_id,part_index))''')

    def create(self,download_job_id):
        self.initialize();service=ReplayPartVideoWeb(self.settings,self.store);policy=service.policy()
        with resource_guard(self.settings.root/'data/locks/replay-part-group-admission-v5',self.settings.root/'data/locks/heavy'):
            context=verified_delivery(self.settings,self.store,download_job_id,verify_all=True)
            source=context['source'];service._source_scope(source,policy)
            if context['plan']['planning_policy']['target_site_uid']!=policy['target_site_uid']:fail('REPLAY_PART_TARGET_POLICY_CHANGED')
            launch=self.require_launch(policy);parts=context['delivery']['outputs'];manifest_sha=context['manifest']['sha256']
            now=time.time();parent=str(uuid.uuid4());result_ids=[]
            with self.store.transaction() as db:
                old=db.execute('SELECT * FROM replay_part_groups_v5 WHERE source_id=? AND target_uid=?',(source['source_id'],policy['target_site_uid'])).fetchone()
                if old:
                    if old['download_job_id']!=download_job_id or old['parts_manifest_sha256']!=manifest_sha or old['policy_sha256']!=policy['_sha256']:
                        fail('REPLAY_PART_GROUP_INPUT_CHANGED')
                    self._retire_legacy_processes(
                        db,source,download_job_id,old['parent_job_id'],time.time()
                    )
                    return old['parent_job_id']
                if (db.execute("SELECT 1 FROM publications WHERE source_platform='youtube' AND source_id=?",(source['source_id'],)).fetchone() or
                        db.execute("SELECT 1 FROM source_tombstones WHERE source_platform='youtube' AND source_id=?",(source['source_id'],)).fetchone() or
                        db.execute("SELECT 1 FROM jobs WHERE source_platform='youtube' AND source_id=? AND (job_type=? OR job_type IN (?,?,?,?))",
                            (source['source_id'],GROUP_JOB,'short_video_publish','replay_video_publish','replay_highest_video_publish_v2','replay_natural_video_publish_v3')).fetchone()):
                    fail('REPLAY_PART_GROUP_SOURCE_REQUIRES_LOOKUP')
                legacy_retired=self._retire_legacy_processes(
                    db,source,download_job_id,parent,now
                )
                group_binding={'version':'replay-parts-group-binding-v5','source_id':source['source_id'],'source_url':source['payload']['url'],
                    'channel_id':source['payload']['channel_id'],'download_job_id':download_job_id,'site_uid':policy['target_site_uid'],
                    'parts_manifest_sha256':manifest_sha,'whole_plan_sha256':context['source']['payload']['whole_replay_plan_sha256'],
                    'part_count':len(parts),'policy_sha256':policy['_sha256'],'delivery_manifest':context['manifest'],'human_review':False}
                group_result={GROUP_STATE:{'version':'replay-parts-group-v5','state':'publishing','part_count':len(parts),
                    'completed_parts':0,'human_review':False,'publications':[]},'download_performed':False,'publish_performed':False}
                db.execute("""INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,policy_version,payload,result,stage)
                    VALUES(?,'youtube',?,?,'video','upload_pending',?,?,?,?,?,'replay_parts_children_queued')""",
                    (parent,source['source_id'],GROUP_JOB,now,now,POLICY,encode(group_binding),encode(group_result)))
                db.execute('INSERT INTO replay_part_groups_v5 VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (parent,source['source_id'],policy['target_site_uid'],download_job_id,manifest_sha,policy['_sha256'],len(parts),'publishing',now,now))
                for index,output in enumerate(parts):
                    child=str(uuid.uuid4());key=digest(['bilibili','youtube',source['source_id'],'site_uid:'+policy['target_site_uid'],manifest_sha,index,output['sha256'],part_kind(index)])
                    meta=part_metadata(self.settings,source,index,len(parts))
                    contract={'version':'replay-part-source-contract-v5','processing_model':context['delivery']['processing_model'],
                        'download_job_id':download_job_id,'whole_plan_sha256':group_binding['whole_plan_sha256'],
                        'source_sha256':context['asset']['sha256'],'source_bytes':context['asset']['bytes'],'source_duration_seconds':wire_seconds(context['asset']['duration']),
                        'source_actual_start_time':source['payload']['raw']['liveStreamingDetails']['actualStartTime'],'operating_not_before':CUTOFF,
                        'delivery_manifest':context['manifest'],'download_receipt':context['receipt'],'output':deepcopy(output)}
                    binding={'version':VERSION,'parent_job_id':parent,'part_index':index,'part_count':len(parts),'part_idempotency_key':key,
                        'parts_manifest_sha256':manifest_sha,'whole_plan_sha256':group_binding['whole_plan_sha256'],'download_job_id':download_job_id,
                        'source_id':source['source_id'],'source_url':source['payload']['url'],'channel_id':source['payload']['channel_id'],
                        'site_uid':policy['target_site_uid'],'account_name':policy['target_account_name'],'file':output['file'],'file_bytes':output['bytes'],
                        'artifact_sha256':output['sha256'],'duration_seconds':wire_seconds(output['duration']),'source_duration_seconds':wire_seconds(context['asset']['duration']),
                        'download_receipt_sha256':context['receipt']['sha256'],'metadata':meta,'metadata_sha256':digest(meta),'policy_sha256':policy['_sha256'],
                        'source_contract':contract,'human_review':False}
                    auth={'version':'replay-part-workflow-authorization-v5','binding_sha256':digest(binding),
                        'workflow_authorization_reference':policy['workflow_authorization_reference'],
                        'actual_owner_workflow_response':launch['actual_owner_workflow_response'],'owner_response_reference':launch['owner_response_reference'],
                        'recorded_at':datetime.now(timezone.utc).isoformat(),'human_review':False,'per_artifact_owner_acceptance':False}
                    state={'version':VERSION,'state':'prepared','key':key,'automation_authorization':auth,'human_review':False,'public_submission_approved':False}
                    db.execute("""INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,policy_version,payload,result,stage)
                        VALUES(?,'youtube',?,?,'video','pending',?,?,?,?,?,'replay_part_queued')""",
                        (child,source['source_id'],part_type(index),now,now,POLICY,encode(binding),encode({STATE:state,'download_performed':False,'publish_performed':False})))
                    db.execute('INSERT INTO replay_part_children_v5 VALUES(?,?,?,?,?,?,NULL)',(parent,index,child,key,output['sha256'],'prepared'))
                    result_ids.append(child)
                db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                    (now,ADMISSION,'replay_parts_group_create',parent,encode({'children':result_ids,'source_id_preserved':True,
                        'part_count':len(parts),'parts_manifest_sha256':manifest_sha,
                        'legacy_processes_retired':legacy_retired,'human_review':False})))
            return parent

    def require_launch(self,policy):
        if policy['source_scope']=='pilot_single':
            return {'actual_owner_workflow_response':policy['actual_owner_workflow_response'],'owner_response_reference':policy['owner_response_reference']}
        service=ReplayPartVideoWeb(self.settings,self.store)
        if policy['automation_enabled'] is not True:fail('REPLAY_PART_WORKFLOW_LAUNCH_REQUIRED')
        try:
            file=Path('/etc/lighthouse-hub/replay-parts-workflow-launch-v5.json')
            raw=service._root_readonly_file(file,maximum=65536);grant=json.loads(raw)
            if (grant['version']!='replay-parts-workflow-launch-v5' or grant['policy_sha256']!=policy['_sha256'] or
                    grant['target_site_uid']!=policy['target_site_uid'] or grant['workflow_authorization_reference']!=policy['workflow_authorization_reference'] or
                    any(not _reference(grant.get(k)) for k in ('actual_owner_workflow_response','owner_response_reference')) or _timestamp(grant['accepted_at'])>time.time()):raise ValueError()
            parent=self.store.get(grant['acceptance_parent_job_id'])
            completion=reference_body(self.settings.root,parent['result']['replay_parts_first_completion_v5'],
                expected=self.settings.root/'media'/parent['job_id']/'replay-parts-v5/first-completion.json')
            if (parent['job_type']!=GROUP_JOB or parent['state']!='completed' or parent['payload']['site_uid']!=policy['target_site_uid'] or
                    completion['version']!='replay-parts-first-completion-v5' or completion['parent_job_id']!=parent['job_id'] or
                    completion['group_binding_sha256']!=digest(parent['payload']) or
                    digest(completion['summary'])!=grant['completed_group_result_sha256']):raise ValueError()
            children=self.store.query('SELECT * FROM replay_part_children_v5 WHERE parent_job_id=?',(parent['job_id'],))
            if len(children)!=parent['payload']['part_count'] or any(row['state']!='completed' or not re.fullmatch('BV[A-Za-z0-9]{10}',row['external_id'] or '') for row in children):raise ValueError()
            recorded=completion['summary']['publications']
            for row in children:
                service=ReplayPartVideoWeb(self.settings,self.store,part_index=row['part_index'])
                child,binding,state=service.snapshot(row['child_job_id'],write=False)
                pubs=self.store.query('SELECT * FROM publications WHERE job_id=? AND idempotency_key=?',(child['job_id'],row['idempotency_key']))
                entry=[v for v in recorded if v['job_id']==child['job_id']]
                originals=[p for p in completion['parts'] if p['job_id']==child['job_id']]
                if (child['state']!='completed' or len(pubs)!=1 or pubs[0]['kind']!=service.KIND or pubs[0]['state']!='completed' or
                        pubs[0]['account_id']!='site_uid:'+policy['target_site_uid'] or pubs[0]['external_id']!=row['external_id'] or
                        pubs[0]['external_url']!='https://www.bilibili.com/video/'+row['external_id'] or len(entry)!=1 or
                        len(originals)!=1 or originals[0]['binding_sha256']!=digest(binding) or
                        originals[0]['result_observation']['external_id']!=row['external_id'] or
                        entry[0]['public_result_sha256']!=digest(originals[0]['result_observation']) or
                        entry[0]['public_coverage_sha256']!=digest(originals[0]['public_coverage'])):raise ValueError()
                coverage=originals[0]['public_coverage']
                for key in ('native_result_evidence','manager_observation','public_observation','public_screenshot'):
                    reference_body(self.settings.root,coverage[key],maximum=20_000_000,json_body=key!='public_screenshot')
            return grant
        except HubError:raise
        except (OSError,ValueError,KeyError,TypeError,AttributeError):fail('REPLAY_PART_WORKFLOW_LAUNCH_REQUIRED')

    def aggregate(self,parent_job_id):
        """All actual child publications, never a fabricated shared BV."""
        self.initialize();parent=self.store.get(parent_job_id)
        if not parent or parent['job_type']!=GROUP_JOB or parent['policy_version']!=POLICY:fail('REPLAY_PART_PARENT_REQUIRED')
        with self.store.transaction() as db:
            children=db.execute('SELECT * FROM replay_part_children_v5 WHERE parent_job_id=? ORDER BY part_index',(parent_job_id,)).fetchall()
            if len(children)!=parent['payload']['part_count']:fail('REPLAY_PART_REGISTRY_BINDING_CHANGED')
            values=[]
            for index,row in enumerate(children):
                child=self.store.get(row['child_job_id'])
                service=ReplayPartVideoWeb(self.settings,self.store,part_index=index)
                service._registry(child,child['payload'],db)
                state=child['result'][STATE];pubs=db.execute('SELECT * FROM publications WHERE job_id=? AND idempotency_key=?',(child['job_id'],row['idempotency_key'])).fetchall()
                good=False;external=None;url=None
                if pubs:
                    if len(pubs)!=1:fail('REPLAY_PART_PUBLICATION_CHANGED')
                    p=pubs[0];external=p['external_id'];url=p['external_url'];o=state.get('result_observation',{})
                    good=(row['state']=='completed' and state['state']=='completed' and p['state']=='completed' and p['kind']==service.KIND and
                        p['account_id']=='site_uid:'+parent['payload']['site_uid'] and p['source_id']==parent['source_id'] and
                        re.fullmatch('BV[A-Za-z0-9]{10}',external or '') is not None and o.get('external_id')==external and
                        o.get('public_accessible') is True and o.get('origin')=='later_native_lookup_reconciled_v5' and
                        o.get('same_submit_attempt_observed') is False and state.get('public_coverage',{}).get('complete_part_playability_verified') is True)
                values.append({'part_index':index,'job_id':child['job_id'],'state':state['state'],'external_id':external,'url':url,'completed':good,
                    'public_result_sha256':digest(state['result_observation']) if good else None,
                    'public_coverage_sha256':digest(state['public_coverage']) if good else None})
            completed=sum(v['completed'] for v in values);target='completed' if completed==len(values) else 'publishing'
            summary={'version':'replay-parts-group-v5','state':target,'part_count':len(values),'completed_parts':completed,
                'human_review':False,'publications':values}
            first=parent['result'].get('replay_parts_first_completion_v5')
            if target=='completed':
                path=self.settings.root/'media'/parent_job_id/'replay-parts-v5/first-completion.json'
                if path.exists():
                    saved,actual=document(self.settings.root,path)
                    if (saved['version']!='replay-parts-first-completion-v5' or saved['parent_job_id']!=parent_job_id or
                            saved['group_binding_sha256']!=digest(parent['payload']) or
                            [(p['job_id'],p['external_id']) for p in saved['summary']['publications']]!=[(p['job_id'],p['external_id']) for p in values] or
                            first is not None and first!=actual):fail('REPLAY_PART_COMPLETION_SNAPSHOT_CHANGED')
                    first=actual
                else:
                    if path.resolve()!=path or any(p.is_symlink() for p in (path,*path.parents)):fail('REPLAY_PART_COMPLETION_PATH_INVALID')
                    body={'version':'replay-parts-first-completion-v5','parent_job_id':parent_job_id,'group_binding_sha256':digest(parent['payload']),
                        'summary':deepcopy(summary),'parts':[]}
                    for row in children:
                        child=self.store.get(row['child_job_id']);s=child['result'][STATE]
                        body['parts'].append({'job_id':child['job_id'],'binding_sha256':digest(child['payload']),
                            'result_observation':deepcopy(s['result_observation']),'public_coverage':deepcopy(s['public_coverage'])})
                    path.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
                    from .replay_delivery_v5 import _immutable
                    first=_immutable(path,body)  # fsynced temp, exclusive link, never a partially written final
            # A reconciliation may revoke an earlier public observation. Do
            # not erase any BV or allow a resend; the aggregate reflects it.
            db.execute('UPDATE jobs SET state=?,stage=?,result=?,error_code=NULL,updated_at=? WHERE job_id=?',
                ('completed' if target=='completed' else 'upload_pending','replay_parts_'+target,
                 encode({**parent['result'],GROUP_STATE:summary,'publish_performed':bool(completed),
                    **({'replay_parts_first_completion_v5':first} if first else {})}),time.time(),parent_job_id))
            db.execute('UPDATE replay_part_groups_v5 SET state=?,updated_at=? WHERE parent_job_id=?',(target,time.time(),parent_job_id))
        return summary

    def fifo_projection(self,db,candidate):
        groups=db.execute('SELECT * FROM replay_part_groups_v5 WHERE source_id=? AND target_uid=?',(candidate['source_id'],candidate['target_uid'])).fetchall()
        if not groups:return None
        if len(groups)!=1:fail('REPLAY_PART_GROUP_DUPLICATED')
        group=groups[0];parent=self.store.get(group['parent_job_id'])
        if (group['download_job_id']!=candidate['download_job_id'] or candidate['publication_job_id'] not in (None,parent['job_id']) or
                parent['job_type']!=GROUP_JOB or parent['source_id']!=candidate['source_id']):fail('REPLAY_PART_FIFO_GROUP_CHANGED')
        children=db.execute('SELECT * FROM replay_part_children_v5 WHERE parent_job_id=? ORDER BY part_index',(parent['job_id'],)).fetchall()
        if len(children)!=group['part_count']:fail('REPLAY_PART_REGISTRY_BINDING_CHANGED')
        all_pubs=db.execute("SELECT * FROM publications WHERE source_platform='youtube' AND source_id=?",(candidate['source_id'],)).fetchall()
        states=[]
        for index,child in enumerate(children):
            service=ReplayPartVideoWeb(self.settings,self.store,part_index=index);job=self.store.get(child['child_job_id'])
            service._registry(job,job['payload'],db);service._source_publications(job,job['payload'],db)
            state=job['result'][STATE]['state'];own=[p for p in all_pubs if p['job_id']==job['job_id']]
            if len(own)>1:fail('REPLAY_PART_PUBLICATION_CHANGED')
            if own and (own[0]['state'] in ('unknown','dispatching') or state in ('submitting','submit_unknown')):states.append('unknown')
            elif own and own[0]['state']=='awaiting_moderation':states.append('awaiting_moderation')
            elif child['state']=='completed' and state=='completed' and own and own[0]['state']=='completed' and own[0]['external_id']==child['external_id']:states.append('completed')
            elif job['state'] in ('failed','awaiting_review','auth_expired') or state=='upload_unknown':states.append('hold')
            else:states.append('publication_queued')
        if all(v=='completed' for v in states) and parent['state']=='completed':return 'completed',None,parent['job_id']
        if 'unknown' in states:return 'unknown','RECONCILE_BEFORE_RETRY',parent['job_id']
        if 'hold' in states:return 'hold','REPLAY_PART_CHILD_REQUIRES_RECONCILIATION',parent['job_id']
        if 'awaiting_moderation' in states:return 'awaiting_moderation',None,parent['job_id']
        return 'publication_queued',None,parent['job_id']

    def sync_completed_groups(self):
        """Recover a crash after child-result CAS, without a platform action."""
        self.initialize();completed=[];held=[]
        rows=self.store.query("""SELECT g.* FROM replay_part_groups_v5 g WHERE g.state<>'completed'
            AND g.part_count=(SELECT COUNT(*) FROM replay_part_children_v5 c WHERE c.parent_job_id=g.parent_job_id AND c.state='completed')""")
        for row in rows:
            try:
                value=self.aggregate(row['parent_job_id'])
                if value['state']=='completed':completed.append(row['parent_job_id'])
            except HubError as exc:
                # An invalid immutable first-completion proof is retained for
                # reconciliation; never overwrite it or block unrelated groups.
                held.append({'parent_job_id':row['parent_job_id'],'reason':exc.code})
                with self.store.transaction() as db:
                    self.store._event(db,'review.required',{'job_id':row['parent_job_id'],'code':exc.code,'stage':'replay_parts_group_aggregation'})
        return {'completed':completed,'held':held,'external_requests':0}

    def publication_tick(self,fifo):
        """Exclude existing source groups before paging, then admit at most one."""
        self.initialize();p=fifo.policy();fifo.initialize();publication_policy=ReplayPartVideoWeb(self.settings,self.store).policy()
        scope=" AND f.source_id=?" if publication_policy['source_scope']=='pilot_single' else ''
        args=(p['_sha256'],DOWNLOAD_JOB,FIFO_POLICY,*publication_policy['acceptance_video_ids'])
        rows=self.store.query('''SELECT f.* FROM replay_fifo_v4 f JOIN jobs d ON d.job_id=f.download_job_id
            WHERE f.policy_sha256=? AND f.publication_job_id IS NULL AND f.state IN ('download_queued','download_completed')
            AND d.job_type=? AND d.policy_version=? AND d.state='completed' AND NOT EXISTS(
                SELECT 1 FROM replay_part_groups_v5 g WHERE g.source_id=f.source_id AND g.target_uid=f.target_uid)
            AND NOT EXISTS(SELECT 1 FROM publications u WHERE u.source_platform='youtube' AND u.source_id=f.source_id)
            '''+scope+' ORDER BY f.actual_end,f.source_id LIMIT 1',args)
        if not rows:return {'created':[]}
        row=rows[0]
        try:ident=self.create(row['download_job_id'])
        except HubError as exc:
            if exc.code=='HEAVY_RESOURCE_BUSY':raise
            with self.store.transaction() as db:
                db.execute("UPDATE replay_fifo_v4 SET state='hold',reason=?,updated_at=? WHERE candidate_id=? AND publication_job_id IS NULL",
                    (exc.code,time.time(),row['candidate_id']))
                self.store._event(db,'task.failed',{'source_id':row['source_id'],'code':exc.code,'candidate_id':row['candidate_id'],'stage':'replay_part_admission'})
            return {'created':[],'held':[{'candidate_id':row['candidate_id'],'reason':exc.code}]}
        with self.store.transaction() as db:
            if db.execute("UPDATE replay_fifo_v4 SET publication_job_id=?,state='publication_queued',reason=NULL,updated_at=? WHERE candidate_id=? AND publication_job_id IS NULL AND download_job_id=?",
                    (ident,time.time(),row['candidate_id'],row['download_job_id'])).rowcount!=1:
                actual=db.execute('SELECT publication_job_id FROM replay_fifo_v4 WHERE candidate_id=?',(row['candidate_id'],)).fetchone()
                if not actual or actual['publication_job_id']!=ident:fail('REPLAY_PART_FIFO_GROUP_CHANGED')
        return {'created':[ident],'actual_publications':0}

    def cleanup_summary(self,parent_job_id,*,refresh_public_part=None):
        """Trusted retention input, not deletion authority or an unlink API.

        Optional refresh is a code-owned known-BV lookup callback after heavy
        hashing. It cannot replace real immutable public evidence with a bool.
        """
        parent=self.store.get(parent_job_id)
        if not parent or parent['job_type']!=GROUP_JOB or parent['state']!='completed':fail('REPLAY_PART_COMPLETE_GROUP_REQUIRED')
        children=self.store.query('SELECT * FROM replay_part_children_v5 WHERE parent_job_id=? ORDER BY part_index',(parent_job_id,))
        if len(children)!=parent['payload']['part_count']:fail('REPLAY_PART_COMPLETE_GROUP_REQUIRED')
        for row in children:
            job=self.store.get(row['child_job_id'])
            if (row['state']!='completed' or job['state']!='completed' or job.get('lease_owner') or job.get('lease_until') or
                    'test_retention_policy' in job['result'][STATE]):fail('REPLAY_PART_GROUP_IN_USE_OR_HELD')
        with resource_guard(self.settings.root/'data/locks/heavy'):
            context=verified_delivery(self.settings,self.store,parent['payload']['download_job_id'],verify_all=True)
            if context['manifest']['sha256']!=parent['payload']['parts_manifest_sha256']:fail('REPLAY_PART_GROUP_INPUT_CHANGED')
        if refresh_public_part is not None:
            if not callable(refresh_public_part):fail('REPLAY_PART_REFRESH_CALLBACK_INVALID')
            for row in children:refresh_public_part(row['child_job_id'],row['external_id'])
        from .replay_part_public_evidence_v5 import validate_result
        public=[]
        for row in children:
            service=ReplayPartVideoWeb(self.settings,self.store,part_index=row['part_index']);job,binding,state=service.snapshot(row['child_job_id'],write=False)
            if (job['state']!='completed' or state['state']!='completed' or state['result_observation']['external_id']!=row['external_id']):fail('REPLAY_PART_COMPLETE_GROUP_REQUIRED')
            coverage=validate_result(service,job,binding,state,state['result_observation'])
            if coverage!=state['public_coverage']:fail('REPLAY_PART_COVERAGE_CHANGED')
            public.append(coverage)
        if self.store.get(parent_job_id)['payload']!=parent['payload']:fail('REPLAY_PART_GROUP_INPUT_CHANGED')
        files={context['asset']['file']:{k:context['asset'][k] for k in ('file','bytes','sha256')}}
        for output in context['delivery']['outputs']:files[output['file']]={k:output[k] for k in ('file','bytes','sha256')}
        return {'version':'replay-whole-parts-retention-input-v5','parent_job_id':parent_job_id,'source_id':parent['source_id'],
            'target_site_uid':parent['payload']['site_uid'],'download_job_id':parent['payload']['download_job_id'],
            'delivery_manifest':context['manifest'],'source_packet_evidence':context['asset']['packet_evidence'],
            'files':list(files.values()),'public_parts':public,'complete_whole_playability_verified':True,'deletion_authorized':False}


class PartMachineChecks:
    """Current complete selected SHA plus the real producer's packet chain.

    The producer's immutable receipt/processing proof is reopened and tied to
    its original complete source hash. A current complete selected-file hash
    proves the bytes still have that packet coverage. No passed flag or fake
    legacy download envelope is accepted.
    """
    def __init__(self,settings,store,*,part_index):
        self.settings,self.store,self.part_index=settings,store,part_index

    def verify(self,job,binding,state,policy,*,cover_provenance_file=None,check=None):
        check=check or (lambda:None);check();root=self.settings.root
        service=ReplayPartVideoWeb(self.settings,self.store,part_index=self.part_index,machine_acceptance_verifier=self)
        try:
            current=self.store.get(job['job_id'])
            if (current['payload']!=binding or current['result'][STATE]!=state or state['state'] not in ('prepared','uploading','uploaded') or
                    current['result'].get('publish_performed') is not False or state.get('public_submission_approved') is not False or
                    state.get('human_review') is not False or 'owner_acceptance' in state):raise ValueError()
            service._registry(current,binding)
            with resource_guard(root/'data/locks/heavy',root/'data/locks'/('replay-part-qa-'+job['job_id'])):
                check();fresh_policy=service.policy()
                if fresh_policy!=policy:raise ValueError()
                context=verified_delivery(self.settings,self.store,binding['download_job_id'],check=check,verify_index=self.part_index)
                service._source_scope(context['source'],policy)
                service._validate_snapshot_binding(job,binding,verify_artifact_hash=False)
                details=inspect_media(binding['file'],include_frame_rates=True,check=check)
                if (details['stream_config']['video']['codec_name']!='h264' or details['stream_config']['audio']['codec_name']!='aac' or
                        abs(details['duration']-binding['duration_seconds'])>.05):raise ValueError()
                binary=shutil.which('ffmpeg')
                if not binary:fail('FFMPEG_REQUIRED')
                cover=ReplayMachineChecks(self.settings,self.store)._cover(root,job['job_id'],job['source_id'],
                    cover_provenance_file,binary,check,attempt_id=state.get('attempt_id'))
                check()
                if service.policy()!=policy or self.store.get(job['job_id'])['payload']!=binding:raise ValueError()
                proof={'version':service.MACHINE_PROOF_VERSION,'job_id':job['job_id'],'binding_sha256':digest(binding),
                    'policy_sha256':policy['_sha256'],'created_at':datetime.now(timezone.utc).isoformat(),'external_requests':0,
                    'checks':{'media':json.loads(encode(details)),'source':{'download_receipt':context['receipt'],
                        'delivery_manifest':context['manifest'],'processing_evidence':context['delivery']['processing_evidence'],
                        'selected_output':context['delivery']['outputs'][self.part_index],'source_sha256':context['asset']['sha256'],
                        'source_packet_evidence':context['asset']['packet_evidence'],
                        'selected_packet_coverage':context['delivery']['output_packet_coverage'][self.part_index],
                        'packet_verification_mode':'producer_packet_chain_after_current_complete_selected_sha'},'cover':cover},
                    'human_review':False,'per_artifact_owner_acceptance':False,'publication_authorized':False}
                folder=root/'media'/job['job_id']/service.MACHINE_PROOF_DIRECTORY/str(uuid.uuid4())
                folder.mkdir(parents=True,mode=0o700);atomic_json(folder/'receipt.json',proof);(folder/'receipt.json').chmod(0o600)
                actual,ref=document(root,folder/'receipt.json',1_000_000,check)
                if actual!=proof:raise ValueError()
                return {**proof,'evidence_file':ref['file'],'evidence_bytes':ref['bytes'],'evidence_sha256':ref['sha256']}
        except HubError:raise
        except (OSError,KeyError,ValueError,TypeError,AttributeError):fail('REPLAY_PART_MACHINE_ACCEPTANCE_REQUIRED')
