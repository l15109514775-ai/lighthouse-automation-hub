"""Immutable, root-installed workflow acceptance, never a per-video review.

The issuer copies genuine completed pilot evidence while it is fresh. Normal
lookups can subsequently refresh the pilot's player timestamps without
invalidating the root-owned acceptance snapshot.
"""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib,json,re,time
from pathlib import Path
from .db import encode
from .errors import HubError
from .youtube_download import CHANNELS,digest
from .replay_highest_video_v2 import HighestReplayVideoWeb,PILOT_SOURCE

VERSION='replay-natural-pilot-acceptance-snapshot-v3'
SNAPSHOT=Path('/etc/lighthouse-hub/replay-natural-acceptance-v3/pilot.json')


def fail():raise HubError('REPLAY_NATURAL_LAUNCH_ACCEPTANCE_REQUIRED',review=True)


def stamp(value):
    value=datetime.fromisoformat(value.replace('Z','+00:00'))
    if value.tzinfo is None:raise ValueError()
    return value.timestamp()


def validate(service,policy,grant,snapshot):
    fields={'version','policy_sha256','workflow_authorization_reference','target_site_uid','allowed_channel_ids',
        'acceptance_job_id','pilot_machine_evidence_sha256','pilot_public_result_sha256','pilot_cleanup_coverage_sha256',
        'pilot_acceptance_snapshot_sha256','actual_owner_launch_response','owner_response_reference','accepted_at'}
    if (set(grant)!=fields or grant['version']!=service.WORKFLOW_LAUNCH_VERSION or grant['policy_sha256']!=policy['_sha256'] or
            grant['workflow_authorization_reference']!=policy['workflow_authorization_reference'] or grant['target_site_uid']!=policy['target_site_uid'] or
            set(grant['allowed_channel_ids'])!=CHANNELS or len(grant['allowed_channel_ids'])!=2 or stamp(grant['accepted_at'])>time.time() or
            any(not isinstance(grant[k],str) or not 0<len(grant[k].strip())<=2048 for k in ('actual_owner_launch_response','owner_response_reference'))):raise ValueError()
    if (set(snapshot)!={'version','job_id','binding','publication','machine_acceptance','public_result','cleanup_coverage','accepted_at'} or
            snapshot['version']!=VERSION or snapshot['job_id']!=grant['acceptance_job_id'] or snapshot['accepted_at']!=grant['accepted_at']):raise ValueError()
    pilot=service.store.get(snapshot['job_id']);binding=snapshot['binding'];proof=snapshot['machine_acceptance']
    result=snapshot['public_result'];coverage=snapshot['cleanup_coverage'];publication=snapshot['publication']
    accepted=stamp(grant['accepted_at'])
    if (not pilot or pilot['job_type']!=HighestReplayVideoWeb.JOB_TYPE or pilot['policy_version']!=HighestReplayVideoWeb.POLICY or
            pilot['source_id']!=PILOT_SOURCE or pilot['payload']!=binding or binding['site_uid']!=policy['target_site_uid'] or
            result['version']!=HighestReplayVideoWeb.RESULT_OBSERVATION_VERSION or result['state']!='completed' or
            result.get('public_accessible') is not True or result['site_uid']!=binding['site_uid'] or result['source_url']!=binding['source_url'] or
            result['metadata_sha256']!=binding['metadata_sha256'] or not re.fullmatch('BV[A-Za-z0-9]{10}',result['external_id']) or
            result['url']!='https://www.bilibili.com/video/'+result['external_id'] or
            proof['version']!=HighestReplayVideoWeb.MACHINE_PROOF_VERSION or proof['job_id']!=pilot['job_id'] or
            proof['binding_sha256']!=digest(binding) or proof['human_review'] is not False or
            proof['per_artifact_owner_acceptance'] is not False or proof['publication_authorized'] is not False or
            proof['evidence_sha256']!=grant['pilot_machine_evidence_sha256'] or digest(result)!=grant['pilot_public_result_sha256'] or
            digest(coverage)!=grant['pilot_cleanup_coverage_sha256'] or coverage['external_id']!=result['external_id'] or
            coverage['artifact_sha256']!=binding['artifact_sha256'] or coverage['metadata_sha256']!=binding['metadata_sha256'] or
            coverage.get('source_to_merged_packet_coverage_verified') is not True or
            not 0<=accepted-stamp(coverage['public_player']['observed_at'])<=300 or
            not 0<=accepted-stamp(result['public_lookup_observed_at'])<=300):raise ValueError()
    rows=service.store.query('SELECT * FROM publications WHERE job_id=? AND idempotency_key=?',(pilot['job_id'],publication['idempotency_key']))
    if (len(rows)!=1 or publication['state']!='completed' or publication['kind']!=HighestReplayVideoWeb.KIND or
            publication['external_id']!=result['external_id'] or publication['external_url']!=result['url'] or
            publication['account_id']!='site_uid:'+binding['site_uid'] or publication['source_id']!=PILOT_SOURCE or
            rows[0]['state'] not in ('completed','owner_deleted') or
            any(rows[0][k]!=publication[k] for k in ('idempotency_key','job_id','platform','source_platform','source_id','account_id','kind','external_id','external_url'))):raise ValueError()
    return grant


def read_launch(service,policy):
    try:
        grant=json.loads(service._root_readonly_file(service.WORKFLOW_LAUNCH_RECEIPT,maximum=65536))
        raw=service._root_readonly_file(SNAPSHOT,maximum=1048576)
        if hashlib.sha256(raw).hexdigest()!=grant['pilot_acceptance_snapshot_sha256']:raise ValueError()
        return validate(service,policy,grant,json.loads(raw))
    except (HubError,OSError,KeyError,ValueError,TypeError,AttributeError,OverflowError):fail()


def issue_launch(service,acceptance_job_id,*,actual_owner_launch_response,owner_response_reference):
    """Return bytes for a root operator to install; do not write or enable.

    Caller must supply the genuine workflow authorization. This routine does
    not create one, does not accept platform agreements, and never publishes.
    """
    from .replay_machine_checks import read_checked
    try:
        policy=service.policy();pilot=HighestReplayVideoWeb(service.settings,service.store)
        summary=pilot.cleanup_coverage_summary(acceptance_job_id)
        job,binding,state=pilot.snapshot(acceptance_job_id,write=False)
        if job['state']!='completed' or state['state']!='completed':raise ValueError()
        proof=state['machine_acceptance'];file=Path(proof['evidence_file'])
        parent=service.settings.root/'media'/job['job_id']/pilot.MACHINE_PROOF_DIRECTORY
        if file.parent.parent!=parent or file.name!='receipt.json':raise ValueError()
        reference,raw=read_checked(service.settings.root,file,maximum=262144,check=lambda:None)
        if (reference['sha256']!=proof['evidence_sha256'] or reference['bytes']!=proof['evidence_bytes'] or
                json.loads(raw)!={k:v for k,v in proof.items() if k not in ('evidence_file','evidence_bytes','evidence_sha256')}):raise ValueError()
        publications=service.store.query('SELECT * FROM publications WHERE job_id=? AND idempotency_key=?',(job['job_id'],state['key']))
        if len(publications)!=1:raise ValueError()
        accepted=datetime.now(timezone.utc).isoformat()
        snapshot={'version':VERSION,'job_id':job['job_id'],'binding':deepcopy(binding),'publication':dict(publications[0]),
            'machine_acceptance':deepcopy(proof),'public_result':deepcopy(state['result_observation']),
            'cleanup_coverage':deepcopy(summary['coverage']),'accepted_at':accepted}
        snapshot_raw=encode(snapshot).encode()
        grant={'version':service.WORKFLOW_LAUNCH_VERSION,'policy_sha256':policy['_sha256'],
            'workflow_authorization_reference':policy['workflow_authorization_reference'],'target_site_uid':policy['target_site_uid'],
            'allowed_channel_ids':sorted(CHANNELS),'acceptance_job_id':job['job_id'],'pilot_machine_evidence_sha256':proof['evidence_sha256'],
            'pilot_public_result_sha256':digest(snapshot['public_result']),'pilot_cleanup_coverage_sha256':digest(snapshot['cleanup_coverage']),
            'pilot_acceptance_snapshot_sha256':hashlib.sha256(snapshot_raw).hexdigest(),'actual_owner_launch_response':actual_owner_launch_response,
            'owner_response_reference':owner_response_reference,'accepted_at':accepted}
        validate(service,policy,grant,snapshot)
        return {'grant_bytes':encode(grant).encode(),'snapshot_bytes':snapshot_raw,'publication_authorized':False,'files_written':False}
    except (HubError,OSError,KeyError,ValueError,TypeError,AttributeError,OverflowError):fail()
