"""Credential-excluded operational archives and a durable, bounded COS runner.

Independent of production job migrations. This module never deletes a source,
backup, log, media file, or cloud object and does not activate a scheduler.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import http.client
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tarfile
import time
import uuid
from zoneinfo import ZoneInfo

from adapters.cloudbase_backup import CloudBaseBackup, encoded, hash_file, _atomic_receipt, _private_directory
from adapters.cloudbase_cos_backup import CloudBaseCOSStorage
from .errors import HubError
from .locks import resource_guard

VERSION = 'cloudbase-operational-backup-v1'
REQUEST_ALLOWANCE = 300000
LEDGER_RELATIVE = Path('data/cloudbase-backup-state/ledger.sqlite3')
SENSITIVE_KEY = re.compile(r'(?i)(?:^|[_-])(?:authorization|cookie|password|passwd|secret[_-]?(?:id|key)|secret|credentials?|session|token|api[_-]?key|access[_-]?token|refresh[_-]?token|upload[_-]?token|sessdata|bili[_-]?jct)(?:$|[_-])')
REFERENCE_KEYS = (('file','sha256'), ('png','sha256'), ('input_file','input_sha256'),
                  ('report_file','report_sha256'), ('manifest_file','manifest_sha256'),
                  ('source_packet_file','source_packet_sha256'), ('inputFile','sha256'),
                  ('ticket_file','ticket_sha256'), ('screenshot_file','screenshot_sha256'))
AUDIT_LITERAL_CREDENTIAL = re.compile(r'''(?ix)
    \b(?:AKID[A-Za-z0-9]{12,}|sk-[A-Za-z0-9_-]{12,})\b |
    \b(?:authorization|cookie|password|passwd|secret_?id|secret_?key|sessdata|bili_?jct|
         access_?token|refresh_?token|api_?key)\b[\s'"`]*[:=]\s*['"`][^'"`\r\n]+['"`] |
    (?:Bearer\s+[A-Za-z0-9._~+/-]{12,}|SESSDATA=|bili_jct=)
''')
ONEOFF_AGE_JOB = '55c4ed05-8650-46bb-aefc-a2b0c11323a1'
ONEOFF_AGE_SOURCE = '2116863357798215887'
ONEOFF_AGE_TARGET = '1000000000000000'
ONEOFF_AGE_MODEL = 'c9632433-0c77-443f-9bcb-b43d0c1b6c1e'
ONEOFF_AGE_OWNER_RESPONSE = '那个超过了2小时限制的旧贴先补发，只有这次这次忽略2小时限制'
ONEOFF_AGE_GRANT_KEYS = {'version','grant_id','job_id','source_id','target_site_uid','owner_response',
    'owner_response_reference','issued_at','expires_at','plan_file','plan_sha256','manifest_sha256',
    'artifact_sha256','caption_sha256','job_policy_version','payload_sha256','policy_sha256',
    'max_dispatches','scope','human_review'}
ONEOFF_AGE_PLAN_KEYS = {'version','plan_id','created_at','job_id','source_id','target_site_uid','before_row_sha256',
    'job_policy_version','payload_sha256','policy_sha256','original_created_at','original_state','original_error_code',
    'original_attempts','manifest','artifact_sha256','caption_sha256','caption','model_job_id',
    'human_review','publication_authorized','job_unchanged','paid_model_calls'}
REFERENCE_BACKFILL_AUDITS = {
    'owner-two-reference-backfill-operator-v2': {
        'path':'config/oneoff-reference-backfill-v2/grant.json',
        'sha256':'29b076f4df26269d3c3fcdc526db094274612a7d72c51b9cf567fb9e133845d0','bytes':1099,
        'capture':'5d441566-6d03-4838-9467-d4a977d7cf26',
        'jobs':{'46b10d04-45c1-4931-9bb4-4f565c905c49':'2116836170986664205',
                'a8152b2d-faf1-4e27-a090-8dc7538e09e8':'2116863407962108290'},
        'descriptor':'media/5d441566-6d03-4838-9467-d4a977d7cf26/owner-two-reference-capture-v2/71671431-dac9-4fce-aafe-589ab1e3cc81/capture-descriptor.json',
        'descriptor_sha256':'a8843d0760d77a67387574d6ab0b0e9d098219c8831ebe04c165c3e2a2fe825b','descriptor_bytes':3734},
    'owner-cached-today-backfill-v1': {
        'path':'config/owner-cached-today-backfill-v1/grant.json',
        'sha256':'abb8c86f43be1f7bb8dd315b0b59c9dab022274b880efa1084c08075a8011c4f','bytes':5408,
        'capture':'cae832f9-5446-4839-a6fa-1f5df7118021',
        'jobs':{'42cbb771-7d0f-4f4a-b813-c327b7be0e0f':'2117142856553083135',
                '4a3277b3-5630-4b3b-b2a7-79dc2c1df179':'2117143292328706377',
                '86dbc6f8-c3d9-436c-8748-f1f88e0249ea':'2117149448958816687'}},
    'owner-cached-youtube-links-v1': {
        'path':'config/owner-cached-youtube-links-v1/grant.json',
        'sha256':'e810e0c39ac665e0d43dc0e4700afc2416d4d9dd4f989a7dc76a89fbe4146863','bytes':4869,
        'capture':'b6c10a88-3144-4704-a26d-af606ca5c294',
        'jobs':{'6e84caa9-f5ed-4969-b37b-9bb9bdabc7a2':'2117189314744590552',
                '5d0aeb3b-41e2-4a2f-b051-51a5169dc854':'2117189497125486924'}},
    'owner-cached-drink-quote-v1': {
        'path':'config/owner-cached-drink-quote-v1/grant.json',
        'sha256':'7953d1ab0a51824dd444fdb3216a3500953f6e53aa0e6f02737b0daa87dbf140','bytes':3462,
        'capture':'2605df1c-77ec-4441-b4e9-a314afc52817',
        'jobs':{'f46a7ae3-2b94-4d2e-aa25-fd062fa53efb':'2117215807201788251'}},
}
REFERENCE_V2_SCOPES = ['capture_exact_sources_with_post_age_exception','translate_check_render_exact_sources','publish_each_source_once']
CACHED_BACKFILL_SCOPES = ['cached_source_import','translate_independent_check_render','publish_once_per_source','today_only_age_exception']


def _oneoff_audit_need(value):
    if not value:raise HubError('CLOUDBASE_BACKUP_ONEOFF_AUDIT_INVALID',review=True)


def _oneoff_root_metadata(info):
    return info.st_uid==0 and not info.st_mode & 0o022


def _oneoff_root_protected(file):
    for path in (file,*file.parents):
        _oneoff_audit_need(_oneoff_root_metadata(path.stat()))


def _oneoff_reference(reference,file,maximum):
    _oneoff_audit_need(type(reference) is dict and set(reference)=={'file','sha256','bytes'} and
        reference['file']==str(file) and type(reference['bytes']) is int and 0<reference['bytes']<=maximum and
        isinstance(reference['sha256'],str) and re.fullmatch('[a-f0-9]{64}',reference['sha256']) is not None)


def _oneoff_reference_scope(value,allowed,depth=0):
    _oneoff_audit_need(depth<=32)
    if isinstance(value,list):
        for item in value:_oneoff_reference_scope(item,allowed,depth+1)
    elif isinstance(value,dict):
        for file_key,hash_key in REFERENCE_KEYS:
            file,checksum=value.get(file_key),value.get(hash_key)
            if isinstance(file,str) and Path(file).is_absolute() and isinstance(checksum,str):
                _oneoff_audit_need((file,checksum) in allowed)
        for item in value.values():_oneoff_reference_scope(item,allowed,depth+1)


def _oneoff_read(root,file,reference,maximum,*,root_owned=False):
    _oneoff_reference(reference,file,maximum)
    safe_file(root,file,maximum=maximum)
    _oneoff_audit_need(file.resolve()==file)
    if root_owned:_oneoff_root_protected(file)
    with os.fdopen(os.open(file,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0)),'rb') as stream:
        before=os.fstat(stream.fileno())
        _oneoff_audit_need(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and 0<before.st_size<=maximum)
        if root_owned:_oneoff_audit_need(_oneoff_root_metadata(before))
        raw=stream.read(maximum+1);after=os.fstat(stream.fileno())
    _oneoff_audit_need(len(raw)==reference['bytes']==before.st_size and
        all(getattr(before,k)==getattr(after,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')) and
        hashlib.sha256(raw).hexdigest()==reference['sha256'])
    text=raw.decode('utf-8')
    def pairs(items):
        value={}
        for key,item in items:
            if key in value:raise ValueError()
            value[key]=item
        return value
    def invalid(_value):raise ValueError()
    value=json.loads(text,object_pairs_hook=pairs,parse_constant=invalid)
    _oneoff_audit_need(type(value) is dict and safe_json(value)==value and '\x00' not in text and
        AUDIT_LITERAL_CREDENTIAL.search(text) is None)
    return raw,value


def oneoff_age_audit(root,row,archived_sources):
    """Archive one authentic expired age exception, never restore its authority.

    This is the exact historical producer shape, not a generic config/grant
    allowlist. Original DB/JSON references remain untouched. Grant and plan
    become independent non-executable audit members, outside source pruning.
    """
    payload=json.loads(row['payload']);result=json.loads(row['result'])
    if not isinstance(result,dict) or 'oneoff_owner_age_exception' not in result:return None
    try:
        history=result['oneoff_owner_age_exception'];grant_ref=history['grant'];plan_ref=history['plan']
        _oneoff_audit_need(row['job_id']==ONEOFF_AGE_JOB and row['source_platform']=='x' and
            row['source_id']==ONEOFF_AGE_SOURCE and row['job_type']=='tweet_process' and row['queue']=='tweet' and row['state']=='completed')
        _oneoff_audit_need(type(history) is dict and set(history)=={'version','grant_id','grant','plan','original_state',
            'original_stage','original_error_code','original_attempts','original_row_sha256','owner_response',
            'owner_response_reference','age_limit_exception_only','human_review','created_at_unchanged'} and
            history['version']=='owner-single-tweet-age-exception-v1' and history['human_review'] is False and
            history['age_limit_exception_only'] is history['created_at_unchanged'] is True)
        grant_file=root/'config/oneoff-tweet-age-v1/grant.json'
        grant_raw,grant=_oneoff_read(root,grant_file,grant_ref,16384,root_owned=True)
        _oneoff_reference_scope(grant,set())
        _oneoff_audit_need(set(grant)==ONEOFF_AGE_GRANT_KEYS)
        plan_file=Path(plan_ref['file']);plan_id=str(uuid.UUID(plan_file.parent.name))
        _oneoff_audit_need(plan_id==plan_file.parent.name and
            plan_file==root/'media'/ONEOFF_AGE_JOB/'oneoff-age-v1'/plan_id/'plan.json')
        plan_raw,plan=_oneoff_read(root,plan_file,plan_ref,65536)
        _oneoff_audit_need(set(plan)==ONEOFF_AGE_PLAN_KEYS and plan['version']=='owner-single-tweet-age-plan-v1' and
            plan['plan_id']==plan_id and plan['model_job_id']==ONEOFF_AGE_MODEL and
            plan['original_state']=='failed' and plan['original_error_code']=='MACHINE_COMPOSITOR_REAL_CHECK_FAILED' and
            type(plan['original_attempts']) is int and plan['original_attempts']==1 and
            plan['human_review'] is plan['publication_authorized'] is False and plan['job_unchanged'] is True and
            type(plan['paid_model_calls']) is int and plan['paid_model_calls']==0)
        _oneoff_audit_need(grant['version']=='owner-single-tweet-age-grant-v1' and
            str(uuid.UUID(grant['grant_id']))==grant['grant_id']==history['grant_id'] and
            grant['job_id']==plan['job_id']==ONEOFF_AGE_JOB and grant['source_id']==plan['source_id']==ONEOFF_AGE_SOURCE and
            grant['target_site_uid']==plan['target_site_uid']==ONEOFF_AGE_TARGET and grant['human_review'] is False and
            type(grant['max_dispatches']) is int and grant['max_dispatches']==1 and
            grant['scope']==['ignore_source_post_age_for_this_job_once'] and
            grant['owner_response']==history['owner_response']==ONEOFF_AGE_OWNER_RESPONSE and
            isinstance(grant['owner_response_reference'],str) and 0<len(grant['owner_response_reference'].strip())<=1000 and
            grant['owner_response_reference']==history['owner_response_reference'] and
            grant['plan_file']==plan_ref['file'] and grant['plan_sha256']==plan_ref['sha256'])
        _oneoff_audit_need(all(type(v) in (int,float) and math.isfinite(v) for v in
            (grant['issued_at'],grant['expires_at'],plan['created_at'])) and
            0<=plan['created_at']<=time.time()+5 and plan['created_at']-5<=grant['issued_at']<=time.time()+5 and
            0<grant['expires_at']-grant['issued_at']<=3600)
        fields=('artifact_sha256','caption_sha256','payload_sha256','policy_sha256')
        _oneoff_audit_need(all(isinstance(plan[k],str) and re.fullmatch('[a-f0-9]{64}',plan[k]) and grant[k]==plan[k] for k in fields) and
            isinstance(plan['before_row_sha256'],str) and re.fullmatch('[a-f0-9]{64}',plan['before_row_sha256']) and
            history['original_row_sha256']==plan['before_row_sha256'] and
            history['original_state']==plan['original_state'] and history['original_error_code']==plan['original_error_code'] and
            type(history['original_attempts']) is int and history['original_attempts']==plan['original_attempts'] and
            grant['job_policy_version']==plan['job_policy_version']==row['policy_version'] and
            plan['payload_sha256']==hashlib.sha256(encoded(payload)).hexdigest() and
            payload['post']['id']==ONEOFF_AGE_SOURCE and payload['author']['username']=='example-channel-b' and
            payload['url']=='https://x.com/example-channel-b/status/'+ONEOFF_AGE_SOURCE and
            payload['post']['created_at']==plan['original_created_at'] and
            isinstance(plan['caption'],str) and plan['caption'].startswith('【补发】') and
            hashlib.sha256(plan['caption'].encode()).hexdigest()==plan['caption_sha256'])
        manifest_ref=plan['manifest'];manifest_file=Path(manifest_ref['file']);attempt=str(uuid.UUID(manifest_file.parent.name))
        _oneoff_audit_need(attempt==manifest_file.parent.name and
            manifest_file==root/'media'/ONEOFF_AGE_JOB/'machine-api-v1'/attempt/'oneoff-age-manifest-v1.json')
        _oneoff_reference(manifest_ref,manifest_file,1000000)
        _oneoff_reference_scope(plan,{(str(manifest_file),manifest_ref['sha256'])})
        machine=result['machine_web']
        _oneoff_audit_need(grant['manifest_sha256']==manifest_ref['sha256']==machine['manifest_sha256'] and
            machine['manifest_file']==str(manifest_file) and machine['artifact_sha256']==plan['artifact_sha256'] and
            machine['human_review'] is False)
        # The normal visitor exports and validates the whole existing manifest
        # graph. Already byte-exact archived descendants may be absent locally.
        if not manifest_file.exists() and (str(manifest_file),manifest_ref['sha256']) in archived_sources:
            manifest_state='prior_byte_exact_archive'
        else:
            _,manifest=_oneoff_read(root,manifest_file,manifest_ref,1000000)
            _oneoff_audit_need(manifest['job_id']==ONEOFF_AGE_JOB and manifest['post_id']==ONEOFF_AGE_SOURCE and
                manifest['target_site_uid']==ONEOFF_AGE_TARGET and manifest['job_policy_version']==row['policy_version'] and
                manifest['policy_sha256']==plan['policy_sha256'] and manifest['render']['sha256']==plan['artifact_sha256'] and
                manifest['caption']==plan['caption'] and manifest['caption_sha256']==plan['caption_sha256'] and
                manifest['source_url']==payload['url'] and manifest['human_review'] is False)
            manifest_state='local_original_bytes'
        prefix='audit/oneoff-tweet-age-v1/'+ONEOFF_AGE_JOB+'/'+grant['grant_id']+'/'
        members={prefix+'grant.json':grant_raw,prefix+'plan.json':plan_raw}
        references=[{'original_file':str(file),'relative_file':file.relative_to(root).as_posix(),
            'member':prefix+kind+'.json','sha256':reference['sha256'],'bytes':len(raw)}
            for kind,file,reference,raw in (('grant',grant_file,grant_ref,grant_raw),('plan',plan_file,plan_ref,plan_raw))]
        return {'members':members,'references':references,'record':{
            'version':'cloudbase-owner-age-exception-audit-v1','kind':'owner_age_exception_audit_non_executable',
            'job_id':ONEOFF_AGE_JOB,'source_id':ONEOFF_AGE_SOURCE,'grant_id':grant['grant_id'],'plan_id':plan_id,
            'references':references,'bound_manifest':manifest_ref,'manifest_evidence':manifest_state,
            'historical_only':True,'human_review':False,'publication_authority_restored':False,
            'execute_or_import_authorized':False,'restore_mode':'0600','local_source_prune_authorized':False}}
    except (OSError,ValueError,KeyError,TypeError,RecursionError,OverflowError):
        raise HubError('CLOUDBASE_BACKUP_ONEOFF_AUDIT_INVALID',review=True) from None


def reference_backfill_audit(root,row):
    """Metadata-only admission of four observed, raw-hash-pinned audit types.

    Never execute producer logic, recheck live permissions, import cached
    sources, or follow arbitrary file references inside the historical grant.
    """
    result=json.loads(row['result'])
    if not isinstance(result,dict) or 'owner_reference_backfill' not in result:return None
    try:
        marker=result['owner_reference_backfill'];version=marker['version'];spec=REFERENCE_BACKFILL_AUDITS[version]
        payload=json.loads(row['payload']);source=spec['jobs'][row['job_id']];sources=sorted(spec['jobs'].values())
        keys={'version','capture_job_id','grant','descriptor','human_review','age_exception_only','original_created_at'}
        if 'descriptor' in spec:keys.add('publication_authorized')
        _oneoff_audit_need(type(marker) is dict and set(marker)==keys and row['source_platform']=='x' and
            row['job_type']=='tweet_process' and row['queue']=='tweet' and row['state']=='completed' and
            row['source_id']==source==payload['post']['id'] and marker['capture_job_id']==spec['capture'] and
            marker['human_review'] is False and marker['age_exception_only'] is True and
            marker['original_created_at']==payload['post']['created_at'])
        grant_file=root/spec['path'];grant_ref=marker['grant']
        _oneoff_audit_need(grant_ref['sha256']==spec['sha256'] and grant_ref['bytes']==spec['bytes'])
        raw,grant=_oneoff_read(root,grant_file,grant_ref,16384,root_owned=True)
        _oneoff_reference_scope(grant,set())
        _oneoff_audit_need(str(uuid.UUID(grant['grant_id']))==grant['grant_id'] and
            all(type(grant[k]) in (int,float) and math.isfinite(grant[k]) for k in ('issued_at','expires_at')) and
            0<=grant['issued_at']<=time.time()+5 and 0<grant['expires_at']-grant['issued_at']<=7200)
        prefix='audit/owner-reference-backfill-v1/'+version+'/'+grant['grant_id']+'/'
        members={prefix+'grant.json':raw}
        refs=[{'original_file':str(grant_file),'relative_file':spec['path'],'member':prefix+'grant.json',
            'sha256':grant_ref['sha256'],'bytes':len(raw),'reference_roles':['grant']}]
        if 'descriptor' in spec:
            _oneoff_audit_need(marker['publication_authorized'] is False and
                set(grant)=={'version','grant_id','source_ids','target_site_uid','author_id','author_handle','owner_response',
                    'owner_response_reference','source_urls','issued_at','expires_at','live_hub_sha256','scopes',
                    'max_timeline_requests','max_detail_requests','max_user_requests','max_publications_per_source','human_review'} and
                grant['version']=='owner-two-reference-backfill-grant-v2' and grant['source_ids']==sources and
                grant['target_site_uid']==ONEOFF_AGE_TARGET and grant['human_review'] is False and grant['scopes']==REFERENCE_V2_SCOPES and
                grant['author_handle']=='example-channel-b' and grant['author_id']=='1986339274611159040' and
                grant['source_urls']==['https://x.com/example-channel-b/status/'+ident for ident in sources] and
                all(type(grant[k]) is int and grant[k]==v for k,v in
                    (('max_timeline_requests',2),('max_detail_requests',3),('max_user_requests',1),('max_publications_per_source',1))))
            descriptor_file=root/spec['descriptor'];descriptor_ref=marker['descriptor']
            _oneoff_audit_need(descriptor_ref['sha256']==spec['descriptor_sha256'] and descriptor_ref['bytes']==spec['descriptor_bytes'])
            descriptor_raw,descriptor=_oneoff_read(root,descriptor_file,descriptor_ref,16384)
            _oneoff_reference_scope(descriptor,{(str(grant_file),grant_ref['sha256'])})
            _oneoff_audit_need(descriptor['version']=='owner-two-reference-source-capture-v2' and
                descriptor['capture_job_id']==spec['capture'] and descriptor['grant']==grant_ref and
                descriptor['source_ids']==sources and descriptor['target_site_uid']==ONEOFF_AGE_TARGET and
                descriptor['author_handle']==grant['author_handle'] and descriptor['author_id']==grant['author_id'] and
                descriptor['human_review'] is descriptor['publication_authorized'] is False and
                descriptor['targets'][source]['original_post']['id']==source and
                descriptor['targets'][source]['original_post']['created_at']==payload['post']['created_at'])
            members[prefix+'capture-descriptor.json']=descriptor_raw
            refs.append({'original_file':str(descriptor_file),'relative_file':spec['descriptor'],
                'member':prefix+'capture-descriptor.json','sha256':descriptor_ref['sha256'],'bytes':len(descriptor_raw),
                'reference_roles':['descriptor']})
        else:
            _oneoff_audit_need(set(grant)=={'plan','grant_id','issued_at','expires_at','owner_response_reference'} and
                marker['descriptor']==grant_ref)
            plan=grant['plan'];capture=plan['capture']
            _oneoff_audit_need(plan['version']==version and plan['source_ids']==sources and capture['source_ids']==sources and
                plan['target_site_uid']==ONEOFF_AGE_TARGET and plan['scope']==CACHED_BACKFILL_SCOPES and
                plan['human_review'] is False and type(plan['new_x_calls']) is int and plan['new_x_calls']==0 and
                capture['capture_job_id']==spec['capture'] and capture['original_posts'][source]['id']==source and
                capture['original_posts'][source]['created_at']==payload['post']['created_at'])
            refs[0]['reference_roles'].append('descriptor')
        return {'members':members,'references':refs,'record':{
            'version':'cloudbase-owner-reference-backfill-audit-v1','kind':'owner_reference_backfill_audit_non_executable',
            'producer_version':version,'job_id':row['job_id'],'source_id':source,'capture_job_id':spec['capture'],
            'grant_id':grant['grant_id'],'references':refs,'historical_only':True,'human_review':False,
            'publication_authority_restored':False,'execute_or_import_authorized':False,'restore_mode':'0600',
            'local_source_prune_authorized':False}}
    except (OSError,ValueError,KeyError,TypeError,RecursionError,OverflowError):
        raise HubError('CLOUDBASE_BACKUP_ONEOFF_AUDIT_INVALID',review=True) from None


def native_repost_evidence(root,parent,db):
    """Bind saved no-send native checks to their exact completed publication.

    Historical observation/audit records are retained, never reinterpreted as
    current visibility, new posting permission, or a generic maintenance owner.
    """
    def need(value):
        if not value:raise HubError('CLOUDBASE_BACKUP_REPOST_EVIDENCE_INVALID',review=True)
    parent_result=json.loads(parent['result']);answer=[];visited=set()
    if not isinstance(parent_result,dict):return answer
    try:
        for marker,policy in (('post_submission_repost_check','tweet-native-repost-check-v1'),
                              ('post_submission_repost_reinspection_v2','tweet-native-repost-reinspection-v2')):
            if marker not in parent_result:continue
            outcome=parent_result[marker];ref=outcome['raw_observation'];file=Path(ref['file'])
            relative=file.relative_to(root);parts=relative.parts
            need(len(parts)==5 and parts[0]=='media' and str(uuid.UUID(parts[1]))==parts[1] and
                 parts[2]=='tweet-repost-check-v1' and str(uuid.UUID(parts[3]))==parts[3] and parts[4]=='observation.json')
            check_id=parts[1]
            if check_id in visited:continue
            visited.add(check_id)
            rows=list(db.execute('SELECT * FROM jobs WHERE job_id=?',(check_id,)));need(len(rows)==1);check=rows[0]
            request=json.loads(check['payload']);saved=json.loads(check['result']);observations=saved['observations']
            need(check['source_platform']=='bilibili' and check['job_type']=='tweet_repost_check_v1' and
                 check['queue']=='tweet' and check['state'] in ('completed','processing','retry_scheduled','awaiting_review','failed') and
                 check['policy_version']==policy and request['version']=='tweet-native-repost-check-v1' and
                 request['submission_job_id']==parent['job_id'] and request['source_id']==parent['source_id'] and
                 request['external_id']==parent['external_publish_id']==check['source_id'] and
                 request['target_site_uid']==ONEOFF_AGE_TARGET and
                 request['url']=='https://t.bilibili.com/'+request['external_id'] and
                 request['manifest_sha256']==parent_result['machine_web']['manifest_sha256'] and
                 hashlib.sha256(request['caption'].encode()).hexdigest()==request['caption_sha256'] and
                 type(observations) is list and 1<=len(observations)<=4 and outcome==observations[-1] and
                 ('final_observation' not in saved or saved['final_observation']==observations[-1]) and
                 (check['state']!='completed' or saved.get('final_observation')==observations[-1]))
            publications=list(db.execute('SELECT * FROM publications WHERE job_id=? AND idempotency_key=?',
                (parent['job_id'],request['publication_key'])))
            need(len(publications)==1);publication=publications[0]
            need(publication['platform']=='bilibili' and publication['source_platform']=='x' and
                 publication['source_id']==parent['source_id'] and publication['state']=='completed' and
                 publication['account_id']=='site_uid:'+ONEOFF_AGE_TARGET and
                 publication['kind']==request['publication_kind'] and publication['external_id']==request['external_id'])
            if policy=='tweet-native-repost-reinspection-v2':
                recheck=request['reinspection'];previous=list(db.execute('SELECT payload,result FROM jobs WHERE job_id=?',
                    (recheck['previous_check_job_id'],)))
                need(recheck['version']==policy and len(previous)==1 and
                     json.loads(previous[0]['payload'])=={k:v for k,v in request.items() if k!='reinspection'} and
                     hashlib.sha256(encoded(json.loads(previous[0]['result']))).hexdigest()==recheck['previous_result_sha256'])
            else:need('reinspection' not in request)
            audits=[json.loads(a['outcome']) for a in db.execute(
                'SELECT outcome FROM audit WHERE action=? AND target=? AND principal=?',
                (policy,check_id,'worker:tweet-repost-check-v1'))]
            references={}
            for observation in observations:
                need(observation in audits and observation['version']=='tweet-native-repost-check-v1' and
                     observation['external_id']==request['external_id'] and observation['human_review'] is False and
                     observation['platform_cause_verified'] is observation['public_visibility_verified'] is False and
                     type(observation['send_requests']) is int and observation['send_requests']==0)
                original=observation['raw_observation'];raw_file=Path(original['file']);attempt=str(uuid.UUID(raw_file.parent.name))
                area=root/'media'/check_id/'tweet-repost-check-v1'/attempt
                need(attempt==raw_file.parent.name and raw_file==area/'observation.json')
                _raw,body=_oneoff_read(root,raw_file,original,2_000_000)
                spec={**request,'check_job_id':check_id,'attempt_id':attempt};actions=body['actions']
                need(body['version'] in ('tweet-native-repost-observation-v1','tweet-native-repost-observation-v2') and
                     body['request_sha256']==hashlib.sha256(encoded(spec)).hexdigest() and
                     body['check_job_id']==check_id and body['attempt_id']==attempt and
                     body['external_id']==request['external_id'] and body['target_site_uid']==ONEOFF_AGE_TARGET and
                     body['human_review'] is body['platform_cause_verified'] is False and
                     all(type(actions[k]) is int and actions[k]==0 for k in ('file_selections','fills','submits')))
                def collect(value,depth=0):
                    need(depth<=32)
                    if isinstance(value,list):
                        for item in value:collect(item,depth+1)
                    elif isinstance(value,dict):
                        for file_key,hash_key in REFERENCE_KEYS:
                            path,checksum=value.get(file_key),value.get(hash_key)
                            if not isinstance(path,str) or not Path(path).is_absolute() or not isinstance(checksum,str):continue
                            target=Path(path);need(target.parent==area and target.name in
                                ('observation.json','observed.png','before-probe.png','author-profile.png'))
                            _oneoff_reference(value,target,2_000_000 if target.suffix=='.json' else 20_000_000)
                            old=references.setdefault((path,checksum),dict(value));need(old==value)
                        for item in value.values():collect(item,depth+1)
                collect(observation);collect(body)
            answer.append({'version':'cloudbase-native-repost-evidence-v1','check_job_id':check_id,
                'parent_job_id':parent['job_id'],'external_id':request['external_id'],'policy_version':policy,
                'observations':len(observations),'references':list(references.values()),
                'snapshot_state':check['state'],'snapshot_lease_owner':check['lease_owner'],
                'snapshot_lease_until':check['lease_until'],'restore_requires_reconciliation':check['state']!='completed',
                'historical_only':True,'human_review':False,'publication_authority_restored':False,
                'public_visibility_inferred':False,'local_source_prune_authorized':False})
        return answer
    except (OSError,ValueError,KeyError,TypeError,RecursionError,OverflowError):
        raise HubError('CLOUDBASE_BACKUP_REPOST_EVIDENCE_INVALID',review=True) from None


def safe_json(value, *, public_cursor=None, path=()):
    if isinstance(value, dict):
        def clean(key, item):
            public=(public_cursor=='x_params' and path==() and key=='pagination_token' or
                    public_cursor=='x_response' and path==('meta',) and key in ('next_token','previous_token') or
                    public_cursor=='model_response' and path==('output','[]','internal_chat_message_metadata_passthrough') and
                    key=='content_token_count' and type(item) is int and 0<=item<=10_000_000)
            return '[CREDENTIAL EXCLUDED]' if SENSITIVE_KEY.search(key) and not public else safe_json(item,public_cursor=public_cursor,path=(*path,key))
        return {k:clean(k,v) for k,v in value.items()}
    if isinstance(value, list): return [safe_json(v,public_cursor=public_cursor,path=(*path,'[]')) for v in value]
    return value


def safe_file(root, file, *, maximum):
    file = Path(file)
    if (not root.is_absolute() or root.is_symlink() or any(p.is_symlink() for p in root.parents) or
            not file.is_absolute() or file.is_symlink() or any(p.is_symlink() for p in file.parents) or
            not file.is_file() or not file.resolve().is_relative_to(root.resolve()) or file.stat().st_size > maximum):
        raise HubError('CLOUDBASE_BACKUP_EXPORT_PATH_REJECTED', review=True)
    return file


@dataclass(frozen=True)
class BackupLimits:
    daily_network_bytes: int = 20_000_000
    monthly_network_bytes: int = 100_000_000
    daily_requests: int = 50
    monthly_requests: int = 200
    max_archive_bytes: int = 8_000_000
    max_uncompressed_bytes: int = 50_000_000
    max_log_source_bytes: int = 524288
    max_tweet_jobs: int = 200
    disk_floor_bytes: int = 15_000_000_000

    def validate(self):
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if type(value) is not int or value <= 0: raise HubError('CLOUDBASE_BACKUP_LIMITS_INVALID')
        if (self.daily_network_bytes > self.monthly_network_bytes or self.daily_requests > self.monthly_requests or
                self.max_archive_bytes > 1_000_000_000 or self.max_uncompressed_bytes > 1_000_000_000 or
                self.max_log_source_bytes > 5_000_000 or self.max_tweet_jobs > 5000 or
                self.disk_floor_bytes < 15_000_000_000):
            raise HubError('CLOUDBASE_BACKUP_LIMITS_INVALID')


@dataclass(frozen=True)
class ListRates:
    standard_storage_cny_per_gb_month: float = 0.118
    internet_egress_cny_per_gb: float = 0.5
    read_cny_per_10000: float = 0.01
    write_cny_per_10000: float = 0.01
    gb_bytes: int = 1073741824
    source: str = 'https://buy.cloud.tencent.com/price/cos'
    as_of: str = '2026-11-01T08:29:56.251Z'

    def validate(self):
        for name in ('standard_storage_cny_per_gb_month','internet_egress_cny_per_gb','read_cny_per_10000','write_cny_per_10000'):
            value=getattr(self,name)
            if type(value) not in (int,float) or not math.isfinite(value) or value<0: raise HubError('CLOUDBASE_BACKUP_PRICE_CONFIG_INVALID')
        if self.gb_bytes!=1073741824 or not self.source.startswith('https://') or not self.as_of:
            raise HubError('CLOUDBASE_BACKUP_PRICE_CONFIG_INVALID')


class BackupLedger:
    """Conservative byte reservations are not refunded after unknown transfers."""
    def __init__(self, path, limits, *, clock=time.time,list_rates=None):
        self.path, self.limits, self.clock = Path(path), limits, clock
        self.rates=list_rates or ListRates();self.rates.validate()
        limits.validate()
        if self.path.is_symlink() or any(p.is_symlink() for p in self.path.parents):
            raise HubError('CLOUDBASE_BACKUP_LEDGER_PATH_INVALID')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            descriptor = os.open(self.path, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600);os.close(descriptor)
        if os.name == 'posix' and self.path.stat().st_mode & 0o077:
            raise HubError('CLOUDBASE_BACKUP_LEDGER_NOT_PRIVATE')
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''CREATE TABLE IF NOT EXISTS operations(
                operation_id TEXT PRIMARY KEY,created_at REAL NOT NULL,updated_at REAL NOT NULL,
                state TEXT NOT NULL,archive TEXT,receipt TEXT,error_code TEXT,policy_sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requests(
                request_id TEXT PRIMARY KEY,operation_id TEXT NOT NULL,created_at REAL NOT NULL,
                day TEXT NOT NULL,month TEXT NOT NULL,method TEXT NOT NULL,reserved_bytes INTEGER NOT NULL,
                state TEXT NOT NULL,error_code TEXT);
                CREATE INDEX IF NOT EXISTS backup_request_period ON requests(day,month);
                CREATE TABLE IF NOT EXISTS plans(operation_id TEXT PRIMARY KEY,created_at REAL NOT NULL,
                maximum_network_bytes INTEGER NOT NULL,maximum_requests INTEGER NOT NULL);''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None);db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=15000');db.execute('PRAGMA synchronous=FULL');return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try: db.execute('BEGIN IMMEDIATE');yield db;db.commit()
        except BaseException: db.rollback();raise
        finally: db.close()

    def start(self, operation, policy_sha256):
        now = self.clock()
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM operations WHERE state IN ('preparing','uploading','upload_unknown')").fetchone():
                raise HubError('CLOUDBASE_BACKUP_RECONCILIATION_REQUIRED', review=True)
            db.execute('INSERT INTO operations VALUES(?,?,?,?,NULL,NULL,NULL,?)',(operation,now,now,'preparing',policy_sha256))

    def recover_after_exclusive_lock(self):
        """Caller must own the runner OS lock; never infer a live worker died."""
        with self.transaction() as db:
            for row in db.execute("SELECT operation_id,state FROM operations WHERE state IN ('preparing','uploading')").fetchall():
                writes=db.execute("SELECT COUNT(*) FROM requests WHERE operation_id=? AND method='PUT'",(row['operation_id'],)).fetchone()[0]
                state='interrupted_before_upload' if writes==0 else 'upload_unknown'
                code='PROCESS_INTERRUPTED_BEFORE_OBJECT_UPLOAD' if writes==0 else 'CLOUDBASE_BACKUP_RECONCILIATION_REQUIRED'
                db.execute('UPDATE operations SET state=?,error_code=?,updated_at=? WHERE operation_id=?',
                           (state,code,self.clock(),row['operation_id']))

    def finish(self, operation, state, *, archive=None, receipt=None, code=None):
        with self.transaction() as db:
            if db.execute('UPDATE operations SET updated_at=?,state=?,archive=COALESCE(?,archive),receipt=COALESCE(?,receipt),error_code=? WHERE operation_id=?',
                    (self.clock(),state,archive,receipt,code,operation)).rowcount != 1:
                raise HubError('CLOUDBASE_BACKUP_OPERATION_MISSING')
        self.write_status()

    def incomplete_remote_write(self, operation):
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM requests WHERE operation_id=? AND method='PUT' AND state IN ('done','reserved','unknown') LIMIT 1",(operation,)).fetchone())

    def reserve(self, operation, method, body_bytes):
        # Includes a conservative allowance for headers/error body per request.
        # Provider invoice bytes may differ; this is an upper planning budget.
        if method not in ('PUT','GET','HEAD') or type(body_bytes) is not int or body_bytes < 0:
            raise HubError('CLOUDBASE_BACKUP_RESERVATION_INVALID')
        reserved = body_bytes + REQUEST_ALLOWANCE;now = self.clock()
        moment = datetime.fromtimestamp(now, ZoneInfo('Asia/Shanghai'));day = moment.strftime('%Y-%m-%d');month = moment.strftime('%Y-%m')
        request_id = str(uuid.uuid4())
        with self.transaction() as db:
            current = db.execute('SELECT state FROM operations WHERE operation_id=?',(operation,)).fetchone()
            if not current or current['state'] != 'uploading': raise HubError('CLOUDBASE_BACKUP_OPERATION_NOT_UPLOADING')
            plan = db.execute('SELECT * FROM plans WHERE operation_id=?',(operation,)).fetchone()
            used = db.execute('SELECT COUNT(*) calls,COALESCE(SUM(reserved_bytes),0) bytes FROM requests WHERE operation_id=?',(operation,)).fetchone()
            if not plan or used['calls']+1>plan['maximum_requests'] or used['bytes']+reserved>plan['maximum_network_bytes']:
                raise HubError('CLOUDBASE_BACKUP_OPERATION_PLAN_EXCEEDED',review=True)
            for field, value, byte_limit, call_limit in (('day',day,self.limits.daily_network_bytes,self.limits.daily_requests),
                                                         ('month',month,self.limits.monthly_network_bytes,self.limits.monthly_requests)):
                row = db.execute(f'SELECT COUNT(*) calls,COALESCE(SUM(reserved_bytes),0) bytes FROM requests WHERE {field}=?',(value,)).fetchone()
                if row['calls'] + 1 > call_limit or row['bytes'] + reserved > byte_limit:
                    raise HubError('CLOUDBASE_BACKUP_BUDGET_EXCEEDED', review=True)
            db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,NULL)',(request_id,operation,now,day,month,method,reserved,'reserved'))
        return request_id

    def preflight(self, operation, *, network_bytes, requests):
        """Persist a worst-case plan before the first network operation.

        The whole runner holds an OS lock; each actual request is separately
        charged before dispatch, including unknown/failed outcomes. The plan is
        not an invoice and unused planned transfers are not claimed performed.
        """
        if type(network_bytes) is not int or type(requests) is not int or network_bytes<=0 or requests<=0:
            raise HubError('CLOUDBASE_BACKUP_RESERVATION_INVALID')
        now=self.clock();moment=datetime.fromtimestamp(now,ZoneInfo('Asia/Shanghai'))
        with self.transaction() as db:
            for field,value,byte_limit,call_limit in (('day',moment.strftime('%Y-%m-%d'),self.limits.daily_network_bytes,self.limits.daily_requests),
                ('month',moment.strftime('%Y-%m'),self.limits.monthly_network_bytes,self.limits.monthly_requests)):
                used=db.execute(f'SELECT COUNT(*) calls,COALESCE(SUM(reserved_bytes),0) bytes FROM requests WHERE {field}=?',(value,)).fetchone()
                if used['calls']+requests>call_limit or used['bytes']+network_bytes>byte_limit:
                    raise HubError('CLOUDBASE_BACKUP_BUDGET_EXCEEDED',review=True)
            db.execute('INSERT INTO plans VALUES(?,?,?,?)',(operation,now,network_bytes,requests))

    def request_result(self, request_id, state, code=None):
        with self.transaction() as db:
            db.execute('UPDATE requests SET state=?,error_code=? WHERE request_id=?',(state,code,request_id))

    def status(self):
        with self.connect() as db:
            operations=[dict(r) for r in db.execute('SELECT * FROM operations ORDER BY created_at DESC LIMIT 20')]
            stored=db.execute("SELECT COALESCE(SUM(MAX(0,reserved_bytes-?)),0) FROM requests WHERE method='PUT'",(REQUEST_ALLOWANCE,)).fetchone()[0]
            periods={}
            for label,field in (('daily','day'),('monthly','month')):
                rows=[]
                for row in db.execute(f'''SELECT {field},COUNT(*) requests,SUM(reserved_bytes) reserved_network_bytes,
                    SUM(CASE WHEN method IN ('GET','HEAD') THEN 1 ELSE 0 END) read_requests,
                    SUM(CASE WHEN method='PUT' THEN 1 ELSE 0 END) write_requests,
                    SUM(CASE WHEN method IN ('GET','HEAD') THEN reserved_bytes ELSE 0 END) egress_bytes_upper
                    FROM requests GROUP BY {field}'''):
                    value=dict(row);r=self.rates
                    storage=stored/r.gb_bytes*r.standard_storage_cny_per_gb_month/(28 if label=='daily' else 1)
                    egress=value['egress_bytes_upper']/r.gb_bytes*r.internet_egress_cny_per_gb
                    requests=value['read_requests']/10000*r.read_cny_per_10000+value['write_requests']/10000*r.write_cny_per_10000
                    value['list_price_upper_estimate_cny']={'storage':round(storage,10),'egress':round(egress,10),'requests':round(requests,10),
                        'total':round(storage+egress+requests,10),'not_a_platform_invoice':True}
                    rows.append(value)
                periods[label]=rows
            latest=db.execute("SELECT operation_id,updated_at,receipt FROM operations WHERE state='verified' ORDER BY updated_at DESC LIMIT 1").fetchone()
            last_error=db.execute('SELECT operation_id,error_code,updated_at FROM operations WHERE error_code IS NOT NULL ORDER BY updated_at DESC LIMIT 1').fetchone()
            return {'operations':operations,**periods,'last_error':dict(last_error) if last_error else None,
                    'latest_verified':{**dict(latest),'age_seconds':max(0,self.clock()-latest['updated_at'])} if latest else None,
                    'budget_counts_unknown_and_failed_transfers':True,'currency_cost_known':False,
                    'pricing':{'currency':'CNY','rates':self.rates.__dict__,'package_discount_verified':False,
                               'stored_bytes_upper_bound':stored,'unknown_puts_counted_as_stored':True,'daily_storage_divisor':28},
                    'automatic_failure_notification_integrated':False}

    def write_status(self):
        path=self.path.parent/'status.json'
        if path.is_symlink():raise HubError('CLOUDBASE_BACKUP_STATUS_PATH_INVALID')
        temporary=path.with_name('status.'+uuid.uuid4().hex+'.partial')
        descriptor=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(descriptor,'wb') as output:output.write(encoded(self.status()));output.flush();os.fsync(output.fileno())
        os.replace(temporary,path)
        if os.name=='posix':
            descriptor=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(descriptor)
            finally:os.close(descriptor)


class BudgetedStorage:
    def __init__(self, storage, ledger, operation): self.storage,self.ledger,self.operation = storage,ledger,operation
    def _call(self, method, size, invoke):
        request_id = self.ledger.reserve(self.operation, method, size)
        try:
            result = invoke();self.ledger.request_result(request_id,'done');return result
        except HubError as exc:
            self.ledger.request_result(request_id,'unknown' if exc.code=='CLOUDBASE_BACKUP_UPLOAD_UNKNOWN' else 'error',exc.code)
            diagnostic=getattr(exc,'safe_http',None)
            if isinstance(diagnostic,dict) and type(diagnostic.get('http_status')) is int and 100<=diagnostic['http_status']<=599:
                safe={'http_status':diagnostic['http_status']}
                code=diagnostic.get('provider_code')
                if isinstance(code,str) and re.fullmatch('[A-Za-z_]{1,80}',code):safe['provider_code']=code
                cause=getattr(exc,'cause_code',exc.code)
                _atomic_receipt(self.ledger.path.parent/'http-failures'/(request_id+'.json'),{
                    'version':'cloudbase-safe-http-failure-v1','operation_id':self.operation,'request_id':request_id,
                    'at':datetime.fromtimestamp(self.ledger.clock(),timezone.utc).isoformat(),'method':method,
                    'error_code':exc.code,'cause_code':cause if re.fullmatch('[A-Z_]{1,100}',cause) else 'UNKNOWN',
                    'response':safe,'raw_response_or_credentials_recorded':False,
                    'reconciliation_required':exc.code=='CLOUDBASE_BACKUP_UPLOAD_UNKNOWN'})
            raise
        except BaseException:
            self.ledger.request_result(request_id,'unknown','CLOUDBASE_BACKUP_REQUEST_INTERRUPTED');raise
    def put(self,key,source,size): return self._call('PUT',size,lambda:self.storage.put(key,source,size))
    def verify(self,reference,key,size,sha): return self._call('GET',size,lambda:self.storage.verify(reference,key,size,sha))
    def head(self,reference,key): return self._call('HEAD',0,lambda:self.storage.head(reference,key))


class OperationalArchive:
    def __init__(self, root, limits): self.root,self.limits = Path(root),limits;limits.validate()

    def _snapshot(self, target):
        source = safe_file(self.root,self.root/'data/hub.sqlite3',maximum=self.limits.max_uncompressed_bytes)
        origin = sqlite3.connect(source.as_uri()+'?mode=ro',uri=True);copy = sqlite3.connect(target)
        excluded = {};redacted = {};modified_json_cells = 0
        try:
            origin.backup(copy,pages=128,sleep=.01);copy.execute('PRAGMA journal_mode=DELETE');copy.execute('PRAGMA secure_delete=ON')
            tables = [r[0] for r in copy.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            for table in tables:
                if not re.fullmatch(r'[a-zA-Z0-9_]+',table): raise HubError('CLOUDBASE_BACKUP_SCHEMA_UNSUPPORTED')
                if re.search(r'(?i)credential|secret|session|token',table):
                    excluded[table] = copy.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
                    copy.execute('DELETE FROM '+table);continue
                columns = list(copy.execute('PRAGMA table_info('+table+')'))
                for _,name,kind,not_null,*_ in columns:
                    if not re.fullmatch(r'[a-zA-Z0-9_]+',name): raise HubError('CLOUDBASE_BACKUP_SCHEMA_UNSUPPORTED')
                    if re.search(r'(?i)encrypted|password|cookie|secret|access_token|refresh_token|upload_token',name):
                        count = copy.execute(f'SELECT COUNT(*) FROM {table} WHERE {name} IS NOT NULL').fetchone()[0]
                        if count:
                            copy.execute(f'UPDATE {table} SET {name}=?',('[CREDENTIAL EXCLUDED]' if not_null else None,));redacted[table+'.'+name] = count
                    elif kind.upper() == 'TEXT':
                        for rowid,value in copy.execute(f'SELECT rowid,{name} FROM {table} WHERE {name} IS NOT NULL').fetchall():
                            if not isinstance(value,str) or not value.startswith(('{','[')): continue
                            try:
                                parsed=json.loads(value)
                                clean=safe_json(parsed,public_cursor='x_params' if table=='source_http_receipts' and name=='params' else None)
                            except (ValueError,TypeError,RecursionError): continue
                            if clean != parsed:
                                copy.execute(f'UPDATE {table} SET {name}=? WHERE rowid=?',(encoded(clean).decode(),rowid));modified_json_cells += 1
            if 'subscriptions' in tables: copy.execute('UPDATE subscriptions SET active=0')
            copy.commit();copy.execute('VACUUM');copy.commit()
            if copy.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise HubError('CLOUDBASE_BACKUP_SNAPSHOT_INTEGRITY_FAILED')
        finally: origin.close();copy.close()
        if os.name == 'posix': target.chmod(0o600)
        return {'kind':'credential-excluded operational snapshot','excluded_tables':excluded,'redacted_columns':redacted,
                'redacted_json_cells':modified_json_cells,'database_integrity':'ok',
                'restore_limitations':['Credentials and subscription secrets must be reprovisioned.',
                    'Ongoing upload/authentication state requires operator reconciliation; never blindly resubmit.',
                    'Private deployment policy and runtime account profiles are not included.']}

    @staticmethod
    def _image(data):
        return (data.startswith(b'\x89PNG\r\n\x1a\n') or data.startswith(b'\xff\xd8\xff') or data.startswith((b'GIF87a',b'GIF89a')) or
                len(data)>=12 and data[:4]==b'RIFF' and data[8:12]==b'WEBP' or
                len(data)>=12 and data[4:8]==b'ftyp' and data[8:12] in (b'avif',b'avis'))

    def build(self, operation):
        if str(uuid.UUID(operation)) != operation: raise HubError('CLOUDBASE_BACKUP_OPERATION_INVALID')
        if shutil.disk_usage(self.root).free-self.limits.max_uncompressed_bytes*2 < self.limits.disk_floor_bytes:
            raise HubError('DISK_LOW',review=True)
        stage = self.root/'backups/cloudbase-staging'/operation;_private_directory(stage)
        snapshot = stage/'credential-excluded-operational.sqlite3';assert not snapshot.exists()
        privacy = self._snapshot(snapshot);db = sqlite3.connect(snapshot);db.row_factory = sqlite3.Row
        export = {};total = snapshot.stat().st_size;seen = set();selected = [];allowed_owners=set();completed_tweets=set();operation_logs=[]
        audit_sources={};audit_source_records=[];bound_source_receipts=set()
        oneoff_audits=[];backfill_audits=[];oneoff_references={};export_owner=None
        repost_records=[];repost_references={};repost_by_parent={}
        archived_sources={};archived_export=None
        # The optional versioned index is populated only from a complete,
        # verified remote restoration before its clones are pruned. It stores
        # immutable archive/member hashes; a missing file alone is never an
        # archived-file claim. No GET occurs while building the next snapshot.
        index_path=self.root/'data/archive-source-retention-v2/index.sqlite3'
        if index_path.exists():
            from .archive_source_retention_v2 import ArchiveSourceRetentionV2
            from types import SimpleNamespace
            archived_export=ArchiveSourceRetentionV2(SimpleNamespace(root=self.root),None).backup_export()
            if archived_export:
                for item in archived_export['locations']:
                    archived_sources[(str(self.root/item['relative_file']),item['file_sha256'])]=item
        def add(name, data):
            nonlocal total
            if name in export: return
            total += len(data)
            if total > self.limits.max_uncompressed_bytes: raise HubError('CLOUDBASE_BACKUP_EXPORT_SIZE_LIMIT')
            export[name] = data
        def visit(value, depth=0):
            if depth>32: raise HubError('CLOUDBASE_BACKUP_EVIDENCE_DEPTH_LIMIT')
            if isinstance(value, list):
                for item in value: visit(item,depth+1)
            elif isinstance(value,dict):
                capture=value.get('payload',{}).get('capture',{}) if isinstance(value.get('payload'),dict) else {}
                legacy_sha=capture.get('screenshot_sha256') if isinstance(capture,dict) else None
                legacy_file=value.get('screenshot_file')
                if (isinstance(legacy_file,str) and Path(legacy_file).is_absolute() and 'screenshot_sha256' not in value and
                    isinstance(legacy_sha,str) and re.fullmatch('[a-f0-9]{64}',legacy_sha)):
                    include(legacy_file,legacy_sha,depth+1)
                for file_key,hash_key in REFERENCE_KEYS:
                    file,sha = value.get(file_key),value.get(hash_key)
                    if isinstance(file,str) and Path(file).is_absolute() and isinstance(sha,str) and re.fullmatch('[a-f0-9]{64}',sha):
                        include(file,sha,depth+1)
                for item in value.values(): visit(item,depth+1)
        def include(file, expected, depth):
            path = Path(file)
            audit=oneoff_references.get((file,expected))
            if audit is not None:
                _oneoff_audit_need(export_owner in audit[2])
            if (file,expected) in repost_references and export_owner not in repost_references[(file,expected)]:
                raise HubError('CLOUDBASE_BACKUP_EVIDENCE_OWNER_REJECTED',review=True)
            if (file,expected) in seen: return
            if len(seen)>=5000: raise HubError('CLOUDBASE_BACKUP_EXPORT_FILE_LIMIT')
            seen.add((file,expected))
            if audit is not None:
                member,data,_owners=audit;add(member,data);return
            if any(part.lower() in ('browser-profiles','secrets','youtube-download-v1','parts-v1') for part in path.parts) or path.suffix.lower() in ('.mp4','.mkv','.m4a','.webm','.ts'):
                raise HubError('CLOUDBASE_BACKUP_VIDEO_OR_PROFILE_REFERENCE_REJECTED',review=True)
            if not (path.is_relative_to(self.root/'media') or path.is_relative_to(self.root/'data/model-receipts')):
                # Versioned compositor fonts/templates belong to the source
                # deployment, not the tweet's image/provenance export.
                if path.is_relative_to(self.root/'compositor'): return
                raise HubError('CLOUDBASE_BACKUP_EVIDENCE_PATH_REJECTED',review=True)
            if not path.exists() and (str(path),expected) in archived_sources:
                # The complete graph's locations, including descendants of a
                # missing JSON artifact, are exported together below. Restore
                # still requires the prior immutable COS objects, not merely
                # the latest SQLite snapshot.
                return
            path = safe_file(self.root,path,maximum=self.limits.max_uncompressed_bytes)
            relative = path.relative_to(self.root)
            owner_id = relative.parts[1] if relative.parts[0]=='media' else relative.parts[2]
            row = db.execute('SELECT source_platform,queue,job_type,result FROM jobs WHERE job_id=?',(owner_id,)).fetchone()
            related_model=(row and row['source_platform']=='machine_model' and json.loads(row['result']).get('parent_job_id') in completed_tweets)
            bound_http=(row and row['source_platform'] in ('x','maintenance','scheduler') and row['job_type']=='x_discovery' and
                (str(path),expected) in bound_source_receipts)
            bound_repost=export_owner in repost_references.get((str(path),expected),set())
            if not row or row['queue']!='tweet' or (owner_id not in allowed_owners and not related_model and not bound_http and not bound_repost):
                raise HubError('CLOUDBASE_BACKUP_EVIDENCE_OWNER_REJECTED',review=True)
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest()!=expected: raise HubError('CLOUDBASE_BACKUP_EVIDENCE_HASH_MISMATCH',review=True)
            if path.suffix.lower()=='.json':
                try: value = json.loads(data)
                except (ValueError,UnicodeError): raise HubError('CLOUDBASE_BACKUP_EVIDENCE_JSON_INVALID') from None
                cursor=None
                if path.parent.name=='source-receipts' and path.suffix=='.json':
                    origin=db.execute('SELECT host,raw_sha256 FROM source_http_receipts WHERE call_id=? AND discovery_job_id=?',(path.stem,owner_id)).fetchone()
                    if origin and origin['host']=='api.x.com' and origin['raw_sha256']==expected:cursor='x_response'
                elif relative.parts[:2]==('data','model-receipts'):
                    origin=db.execute('SELECT response_wire_sha256 FROM model_call_receipts WHERE call_id=? AND job_id=?',(path.stem,owner_id)).fetchone()
                    if origin and origin['response_wire_sha256']==expected:cursor='model_response'
                if safe_json(value,public_cursor=cursor)!=value: raise HubError('CLOUDBASE_BACKUP_EVIDENCE_CONTAINS_CREDENTIAL',review=True)
                # A historical, hash-bound nonacceptance attestation retains
                # the exact dispatcher source solely as non-executable audit
                # evidence. No other source file or generic JS reference is
                # admitted, and the proof itself must belong to this completed
                # tweet, at its existing versioned path.
                if (owner_id in completed_tweets and path==self.root/'media'/owner_id/'supervised-review-v1/nonacceptance-v1/proof.json' and
                    isinstance(value,dict) and value.get('version')=='supervised-web-nonacceptance-v1' and
                    value.get('job_id')==owner_id and value.get('observer_kind')=='operator_transcribed_browser_evidence' and
                    value.get('basis')=='incomplete_upload_confirmation_left_unconfirmed' and value.get('final_confirmation_not_clicked') is True):
                    reference=value.get('dispatch_implementation',{})
                    if isinstance(reference,dict):
                        dispatch=path.parent/'dispatch-implementation.mjs'
                        if (reference.get('file')==str(dispatch) and isinstance(reference.get('sha256'),str) and
                            re.fullmatch('[a-f0-9]{64}',reference['sha256'])):
                            audit_sources[(str(dispatch),reference['sha256'])]={'proof_file':relative.as_posix(),'proof_sha256':expected}
                add('tweets/files/'+relative.as_posix(),data);visit(value,depth+1)
            elif self._image(data): add('tweets/files/'+relative.as_posix(),data)
            elif (file,expected) in audit_sources and path.name=='dispatch-implementation.mjs':
                if len(data)>65536:raise HubError('CLOUDBASE_BACKUP_AUDIT_SOURCE_SIZE_LIMIT',review=True)
                try:source=data.decode('utf-8')
                except UnicodeError:raise HubError('CLOUDBASE_BACKUP_AUDIT_SOURCE_ENCODING_INVALID',review=True) from None
                if '\x00' in source or AUDIT_LITERAL_CREDENTIAL.search(source):
                    raise HubError('CLOUDBASE_BACKUP_AUDIT_SOURCE_CONTAINS_CREDENTIAL',review=True)
                add('tweets/files/'+relative.as_posix(),data)
                audit_source_records.append({'file':relative.as_posix(),'sha256':expected,'bytes':len(data),
                    **audit_sources[(file,expected)],'kind':'audit_source_non_executable','restore_mode':'0600',
                    'execute_or_import_authorized':False,'credential_scan':'known_literal_patterns_rejected'})
            else: raise HubError('CLOUDBASE_BACKUP_EVIDENCE_TYPE_REJECTED',review=True)
        try:
            rows = db.execute("SELECT * FROM jobs WHERE source_platform='x' AND job_type='tweet_process' AND queue='tweet' AND state='completed' ORDER BY updated_at,job_id").fetchall()
            if len(rows)>self.limits.max_tweet_jobs: raise HubError('CLOUDBASE_BACKUP_TWEET_BATCH_LIMIT',review=True)
            allowed_owners.update(row['job_id'] for row in rows)
            completed_tweets.update(allowed_owners)
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            model_children={parent:set() for parent in completed_tweets}
            for child in db.execute("SELECT job_id,result FROM jobs WHERE source_platform='machine_model' AND queue='tweet'"):
                parent=json.loads(child['result']).get('parent_job_id')
                if parent in completed_tweets:model_children[parent].add(child['job_id'])
            for row in rows:
                for audit,records in ((oneoff_age_audit(self.root,row,archived_sources),oneoff_audits),
                                      (reference_backfill_audit(self.root,row),backfill_audits)):
                    if audit is None:continue
                    records.append(audit['record'])
                    for reference in audit['references']:
                        key=(reference['original_file'],reference['sha256'])
                        value=oneoff_references.get(key)
                        member,data=reference['member'],audit['members'][reference['member']]
                        if value is not None:
                            _oneoff_audit_need(value[0]==member and value[1]==data);value[2].add(row['job_id'])
                        else:oneoff_references[key]=(member,data,{row['job_id']})
                for record in native_repost_evidence(self.root,row,db):
                    repost_records.append(record)
                    for reference in record['references']:
                        key=(reference['file'],reference['sha256'])
                        repost_references.setdefault(key,set()).add(row['job_id'])
                        repost_by_parent.setdefault(row['job_id'],set()).add(key)
            for row in rows:
                for table in ('tweet_source_bindings','web_source_bindings','extended_web_source_bindings'):
                    if table in tables:
                        allowed_owners.update(r[0] for r in db.execute('SELECT discovery_job_id FROM '+table+' WHERE job_id=?',(row['job_id'],)))
                if 'tweet_source_bindings' in tables:
                    # A cached official user call can belong to an older
                    # discovery job. Permit only this bound receipt's exact
                    # path + recorded raw hash, never that owner's other files.
                    for receipt in db.execute('''SELECT DISTINCT r.discovery_job_id,r.call_id,r.raw_sha256 FROM tweet_source_bindings b
                        JOIN source_http_receipts r ON r.call_id=b.user_call_id OR r.call_id=b.timeline_call_id
                        WHERE b.job_id=? AND r.method='GET' AND r.host='api.x.com' ''',(row['job_id'],)):
                        bound_source_receipts.add((str(self.root/'media'/receipt['discovery_job_id']/'source-receipts'/(receipt['call_id']+'.json')),receipt['raw_sha256']))
            for row in rows:
                export_owner=row['job_id']
                item = dict(row)
                for name in ('payload','result'): item[name]=json.loads(item[name])
                selected.append(safe_json(item));visit(item['result'])
                # Earlier attempts remain necessary audit history even when
                # the parent only points at the latest immutable observation.
                for file,checksum in sorted(repost_by_parent.get(row['job_id'],set())):
                    include(file,checksum,0)
                source = self.root/'media'/row['job_id']/'source.json'
                if source.exists():
                    safe_file(self.root,source,maximum=self.limits.max_uncompressed_bytes)
                    raw=source.read_bytes()
                    if json.loads(raw)!=item['payload']: raise HubError('CLOUDBASE_BACKUP_TWEET_SOURCE_MISMATCH')
                    include(str(source),hashlib.sha256(raw).hexdigest(),0)
                if 'tweet_source_bindings' in tables:
                    for receipt in db.execute('''SELECT DISTINCT r.discovery_job_id,r.call_id,r.raw_sha256 FROM tweet_source_bindings b
                        JOIN source_http_receipts r ON r.call_id=b.user_call_id OR r.call_id=b.timeline_call_id WHERE b.job_id=?''',(row['job_id'],)):
                        include(str(self.root/'media'/receipt['discovery_job_id']/'source-receipts'/(receipt['call_id']+'.json')),receipt['raw_sha256'],0)
                for table,file_key,hash_key in (('web_source_bindings','observation_file','observation_sha256'),
                    ('extended_web_source_bindings','packet_file','packet_sha256')):
                    if table in tables:
                        for binding in db.execute('SELECT * FROM '+table+' WHERE job_id=?',(row['job_id'],)):
                            include(binding[file_key],binding[hash_key],0)
                if 'model_call_receipts' in tables:
                    # Include both the selected revision and its real rejected
                    # predecessor. Restore verifies previous_model_job_id too.
                    for owner in {row['job_id'],*model_children[row['job_id']]}:
                        for receipt in db.execute('SELECT call_id,response_wire_sha256 FROM model_call_receipts WHERE job_id=?',(owner,)):
                            include(str(self.root/'data/model-receipts'/owner/(receipt['call_id']+'.json')),receipt['response_wire_sha256'],0)
            add('tweets/completed-jobs.jsonl',b''.join(encoded(item)+b'\n' for item in selected))
            for table in ('events','audit'):
                if table not in tables:continue
                for row in db.execute('SELECT * FROM '+table+' ORDER BY seq'):
                    raw=dict(row);entry={'source_table':table,'snapshot_row_sha256':hashlib.sha256(encoded(raw)).hexdigest()}
                    for key in ('seq','at','occurred_at'):
                        if type(raw.get(key)) in (int,float) and math.isfinite(raw[key]):entry[key]=raw[key]
                    for key in ('principal','action','target','outcome','name'):
                        value=raw.get(key)
                        if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_.:@/-]{1,160}',value) and not value.startswith(('AKID','sk-')):
                            entry[key]=value
                    for key in ('data','outcome'):
                        try:value=json.loads(raw.get(key,''))
                        except (ValueError,TypeError):continue
                        if isinstance(value,dict):
                            entry[key]={k:v for k,v in safe_json(value).items() if not SENSITIVE_KEY.search(k) and
                                (v is None or type(v) in (bool,int,float) or isinstance(v,str) and re.fullmatch(r'[A-Za-z0-9_.:@/-]{1,160}',v))}
                    operation_logs.append(entry)
            add('logs/operations.jsonl',b''.join(encoded(item)+b'\n' for item in operation_logs))
        finally: db.close()
        logs=[];remaining=self.limits.max_log_source_bytes
        folder=self.root/'logs'
        if folder.is_dir() and not folder.is_symlink():
            for path in sorted(folder.iterdir()):
                if remaining<=0: break
                if path.suffix.lower() not in ('.log','.jsonl') or path.is_symlink() or not path.is_file(): continue
                safe_file(self.root,path,maximum=1_000_000_000)
                with path.open('rb') as stream: raw=stream.read(remaining)
                remaining-=len(raw)
                for line in raw.splitlines():
                    entry={'source_name_sha256':hashlib.sha256(path.name.encode()).hexdigest(),
                           'line_sha256':hashlib.sha256(line).hexdigest(),'free_text_omitted':True}
                    try: value=json.loads(line)
                    except (ValueError,UnicodeError): value={}
                    if isinstance(value,dict):
                        for key in ('level','queue','state','error_code','job_id','event'):
                            field=value.get(key)
                            allowed=(key=='level' and field in ('DEBUG','INFO','WARNING','ERROR','CRITICAL') or
                                key=='queue' and field in ('tweet','video','system') or
                                key=='state' and field in ('pending','processing','completed','failed','publish_unknown','awaiting_review') or
                                key=='error_code' and isinstance(field,str) and re.fullmatch('[A-Z][A-Z0-9_]{1,79}',field) or
                                key=='job_id' and isinstance(field,str) and re.fullmatch('[a-f0-9-]{36}',field) or
                                key=='event' and field in ('task.failed','auth.expired','review.required','disk.low','pipeline.stalled'))
                            if allowed: entry[key]=field
                    logs.append(entry)
        add('logs/redacted-frozen-log.jsonl',b''.join(encoded(x)+b'\n' for x in logs))
        if (self.root/'logs/sealed-v2').exists():
            from .archive_source_retention_v2 import sealed_log_exports
            for name,data in sealed_log_exports(self.root,self.limits.max_log_source_bytes).items():add(name,data)
        if archived_export:add('archives/source-locations-v2.json',encoded(archived_export))
        manifest={'version':VERSION,'operation_id':operation,'created_at':datetime.now(timezone.utc).isoformat(),
            'snapshot':privacy,'tweet_jobs':len(selected),'frozen_log_records':len(logs)+len(operation_logs),
            'database_operational_log_records':len(operation_logs),'raw_docker_logs_captured':False,'directory_json_reports_captured':False,
            'non_executable_audit_sources':audit_source_records,
            'files':{'database/credential-excluded-operational.sqlite3':{'bytes':snapshot.stat().st_size,'sha256':hash_file(snapshot)},
                     **{n:{'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()} for n,data in export.items()}},
            'excluded':['secret environment files','browser/account profiles','video/audio material','unstructured/raw log text'],
            'source_files_deleted':False,'production_database_modified':False}
        if archived_export:
            manifest['archived_source_files']=len(archived_export['locations'])
            manifest['recovery_requires_prior_archives']=True
        if oneoff_audits:manifest['oneoff_owner_age_exception_audits']=oneoff_audits
        if backfill_audits:manifest['owner_reference_backfill_audits']=backfill_audits
        if repost_records:manifest['native_repost_check_evidence']=repost_records
        archive=stage/'operational-backup.tar.gz'
        with tarfile.open(archive,'w:gz') as tar:
            tar.add(snapshot,arcname='database/credential-excluded-operational.sqlite3',recursive=False)
            for name,data in {**export,'MANIFEST.json':encoded(manifest)}.items():
                info=tarfile.TarInfo(name);info.size=len(data);info.mode=0o600;tar.addfile(info,io.BytesIO(data))
        if os.name=='posix': archive.chmod(0o600)
        if archive.stat().st_size>self.limits.max_archive_bytes: raise HubError('CLOUDBASE_BACKUP_ARCHIVE_SIZE_LIMIT',review=True)
        _atomic_receipt(stage/'archive-manifest.json',manifest)
        return {'archive':str(archive),'bytes':archive.stat().st_size,'sha256':hash_file(archive),'manifest':manifest}


class PrivateCanary:
    """Pinned owner bootstrap + live checks before every sensitive upload."""
    def __init__(self,root,config,reference,expected_sha256,*,clock=time.time):
        self.root,self.config,self.clock=Path(root),config,clock
        path=Path(reference);self.path=path if path.is_absolute() else self.root/path
        self.expected_sha256=expected_sha256

    def load(self):
        if not re.fullmatch('[a-f0-9]{64}',self.expected_sha256): raise HubError('CLOUDBASE_BACKUP_PRIVATE_CANARY_REQUIRED',review=True)
        path=safe_file(self.root,self.path,maximum=65536)
        if hash_file(path)!=self.expected_sha256: raise HubError('CLOUDBASE_BACKUP_PRIVATE_CANARY_CHANGED',review=True)
        try:
            proof=json.loads(path.read_bytes());intent=json.loads(safe_file(self.root,path.parent/'intent.json',maximum=65536).read_bytes())
            key=intent['object_key'];origin=self.config.host;cdn=self.config.bucket+'.tcb.qcloud.la'
            observations=proof['observations'];moment=datetime.fromisoformat(proof['at'])
            if (proof['version']!='cloudbase-canary-v1' or proof['private_storage_verified'] is not True or
                proof['object_key']!=key or intent['sensitive_data'] is not False or not key.startswith(self.config.object_prefix+'/canary/') or
                not re.fullmatch('[a-zA-Z0-9._/-]+',key) or '..' in key.split('/') or
                type(intent['bytes']) is not int or not 0<intent['bytes']<=8192 or not re.fullmatch('[a-f0-9]{64}',intent['sha256']) or
                moment.tzinfo is None or moment.timestamp()>self.clock()+30 or
                not any(x['host']==origin and x['method']=='GET' and x['signed'] is True and x['status']==200 and
                        x.get('same_canary_bytes') is True for x in observations) or
                not all(any(x['host']==host and x['method']=='GET' and x['signed'] is False and x['status'] in (401,403) and
                            x.get('same_canary_bytes') is False for x in observations) for host in (origin,cdn))):
                raise ValueError()
            return {'object_key':key,'bytes':intent['bytes'],'sha256':intent['sha256'],'origin_host':origin,'cdn_host':cdn}
        except (ValueError,KeyError,TypeError,UnicodeError):
            raise HubError('CLOUDBASE_BACKUP_PRIVATE_CANARY_INVALID',review=True) from None

    def check_live(self,storage,ledger,operation,canary):
        budgeted=BudgetedStorage(storage,ledger,operation);key=canary['object_key']
        signed=budgeted.verify('cos://'+self.config.bucket+'/'+key,key,canary['bytes'],canary['sha256'])
        observations=[]
        for host in (canary['origin_host'],canary['cdn_host']):
            request_id=ledger.reserve(operation,'GET',8192);connection=None
            try:
                connection,response=storage.request('https://'+host+'/'+key,[host],method='GET',
                    headers={'Accept-Encoding':'identity','Cache-Control':'no-cache'},body=None)
                raw=response.read(8193)
                if len(raw)>8192 or response.status not in (401,403):
                    raise HubError('CLOUDBASE_BACKUP_ANONYMOUS_READ_NOT_DENIED',review=True)
                observations.append({'host':host,'method':'GET','signed':False,'status':response.status,
                    'response_bytes':len(raw),'response_sha256':hashlib.sha256(raw).hexdigest()})
                ledger.request_result(request_id,'done')
            except (OSError,http.client.HTTPException):
                ledger.request_result(request_id,'error','CLOUDBASE_BACKUP_NETWORK_ERROR')
                raise HubError('CLOUDBASE_BACKUP_NETWORK_ERROR',retryable=True) from None
            except BaseException as exc:
                ledger.request_result(request_id,'error',getattr(exc,'code','CLOUDBASE_BACKUP_PRIVATE_CHECK_INTERRUPTED'));raise
            finally:
                if connection:connection.close()
        result={'version':'cloudbase-runtime-private-canary-v1','checked_at':datetime.fromtimestamp(self.clock(),timezone.utc).isoformat(),
            'bootstrap_file':str(self.path),'bootstrap_sha256':self.expected_sha256,'operation_id':operation,
            'canary':canary,'signed_full_get':signed,'anonymous_observations':observations,'private_storage_verified':True}
        target=self.root/'data/cloudbase-backup-canary-checks'/(operation+'.json');_atomic_receipt(target,result)
        return {'file':str(target),'sha256':hash_file(target),'private_storage_verified':True}


class CloudBaseBackupRuntime:
    def __init__(self,root,config,limits,*,private_canary_reference,policy_sha256,private_canary_sha256='',credentials=None,storage=None,clock=time.time,list_rates=None):
        self.root,self.config,self.limits=Path(root),config,limits
        self.canary,self.policy_sha256=private_canary_reference,policy_sha256
        self.canary_sha256=private_canary_sha256
        self.credentials,self.storage,self.clock=credentials,storage,clock
        self.rates=list_rates or ListRates()

    def run_once(self,*,prepare_only=False):
        self.limits.validate()
        if not prepare_only:
            self.config.validate()
            if not self.canary or not re.fullmatch('[a-f0-9]{64}',self.policy_sha256):
                raise HubError('CLOUDBASE_BACKUP_PRIVATE_CANARY_REQUIRED',review=True)
            checker=PrivateCanary(self.root,self.config,self.canary,self.canary_sha256,clock=self.clock)
            canary=checker.load()
        state=self.root/'data/cloudbase-backup-state';_private_directory(state)
        with resource_guard(state/'runner.lock'):
            ledger=BackupLedger(self.root/LEDGER_RELATIVE,self.limits,clock=self.clock,list_rates=self.rates)
            ledger.recover_after_exclusive_lock()
            operation=str(uuid.uuid4());ledger.start(operation,self.policy_sha256)
            archive=None
            try:
                bundle=OperationalArchive(self.root,self.limits).build(operation);archive=bundle['archive']
                if prepare_only:
                    ledger.finish(operation,'prepared_no_network',archive=archive)
                    return {**bundle,'operation_id':operation,'external_requests':0,'remote_verified':False,'prune_candidate':None}
                ledger.finish(operation,'uploading',archive=archive)
                # Three live canary GETs plus two PUTs and two full GETs.
                # Manifest is bounded by the
                # adapter's 256KiB receipt bound, not an unbounded model reply.
                ledger.preflight(operation,network_bytes=2*bundle['bytes']+2*262144+canary['bytes']+2*8192+7*REQUEST_ALLOWANCE,requests=7)
                raw=self.storage or CloudBaseCOSStorage(self.config,self.credentials)
                fresh_canary=checker.check_live(raw,ledger,operation,canary)
                storage=BudgetedStorage(raw,ledger,operation)
                backup=CloudBaseBackup(self.root,self.config,storage)
                result=backup.upload_archive(archive,kind='database_snapshot')
                candidate=backup.archive_prune_eligibility(archive,result['receipt_file'])
                ledger.finish(operation,'verified',archive=archive,receipt=result['receipt_file'])
                return {'version':VERSION,'operation_id':operation,'bundle':bundle,'remote':result,
                    'prune_candidate':candidate,'budget':ledger.status(),'source_files_deleted':False,
                    'private_canary_reference':self.canary,'fresh_private_canary':fresh_canary}
            except HubError as exc:
                ledger.finish(operation,'upload_unknown' if exc.code=='CLOUDBASE_BACKUP_UPLOAD_UNKNOWN' or ledger.incomplete_remote_write(operation) else 'failed',archive=archive,code=exc.code);raise
            except BaseException:
                ledger.finish(operation,'upload_unknown' if archive and not prepare_only else 'failed',archive=archive,
                              code='CLOUDBASE_BACKUP_OPERATION_INTERRUPTED');raise
