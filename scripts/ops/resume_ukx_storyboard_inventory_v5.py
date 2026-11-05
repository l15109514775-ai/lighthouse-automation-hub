"""Resume one exact held download after storyboard-only inventory normalization.

Default is a read-only plan. Apply is CAS-bound to the preserved failed job,
keeps attempts and the original plan, and schedules no new job or provider
request. Original manifests, observations, tickets and failure receipts remain.
"""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import time

from adapters.ytdlp_whole_v4 import optional_storyboard,plan_from_info,select_from_info
from services.hub.config import Settings
from services.hub.db import Store,encode
from services.hub.errors import HubError
from services.hub.locks import resource_guard
from services.hub.replay_multipart_v4 import digest,evaluate_whole_resources
from services.hub.replay_parts_retention_v5 import ReplayPartsRetentionV5
from services.hub.youtube_download_v4 import YouTubeWholeReplayDownload,STATE

VERSION='resume-ukx-storyboard-inventory-v5'
JOB='bca759c3-65f3-4a1f-b949-33181cbc403d'
SOURCE='uKXvoszCwek'
PLAN_JOB='5eaa0889-1613-4149-8699-f94b93cd1ec1'
CANDIDATE='644bfe85-3005-4b3b-948a-2ba61b8170a4'
ERROR='YOUTUBE_WHOLE_FRESH_INVENTORY_CHANGED'
SELECTED=(('299',7424268409),('140',357831278))
STORIES=('sb0','sb1','sb2','sb3')
OWN_REQUESTS=6
OWN_RESPONSE_BYTES=7030633


def need(value,code):
    if not value:
        raise ValueError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def plan_digest(value):
    item=deepcopy(value)
    item.pop('planned_at',None)
    return digest(item)


def descriptor(file):
    file=Path(file)
    need(file.is_absolute() and file.resolve()==file and not any(path.is_symlink() for path in (file,*file.parents)),
         'RESUME_STORYBOARD_PATH_INVALID')
    info=file.lstat()
    need(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and 0<info.st_size<=2_097_152,
         'RESUME_STORYBOARD_FILE_INVALID')
    with os.fdopen(os.open(file,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)),'rb') as stream:
        before=os.fstat(stream.fileno());raw=stream.read(2_097_153);after=os.fstat(stream.fileno())
    keys=('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')
    need(len(raw)==before.st_size and all(getattr(before,k)==getattr(after,k) for k in keys),
         'RESUME_STORYBOARD_FILE_CHANGED')
    return raw,{'bytes':len(raw),'sha256':sha(raw),'device':before.st_dev,'inode':before.st_ino,
        'mtime_ns':before.st_mtime_ns,'ctime_ns':before.st_ctime_ns}


def document(file):
    raw,info=descriptor(file)
    return json.loads(raw),info


def snapshot(settings,store,db,expected_runtime,qualified_release_sha):
    root=settings.root
    need(root.as_posix()=='/opt/lighthouse-hub' and isinstance(expected_runtime,str) and
         re.fullmatch('[a-f0-9]{64}',expected_runtime) and isinstance(qualified_release_sha,str) and
         re.fullmatch('[a-f0-9]{64}',qualified_release_sha),'RESUME_STORYBOARD_PINS_REQUIRED')
    raw,_=descriptor(root/'adapters/ytdlp_whole_v4.py')
    need(sha(raw)==expected_runtime,'RESUME_STORYBOARD_RUNTIME_CHANGED')
    _,release_sha=ReplayPartsRetentionV5(settings,store)._release()
    need(release_sha==qualified_release_sha,'RESUME_STORYBOARD_RELEASE_CHANGED')
    need(db.execute("SELECT paused FROM pipelines WHERE name='video'").fetchone()['paused']==0,
         'VIDEO_PIPELINE_PAUSED')
    need(not db.execute("SELECT 1 FROM jobs WHERE queue='video' AND "
        "(state='processing' OR lease_owner IS NOT NULL OR lease_until IS NOT NULL)").fetchone(),
        'ACTIVE_VIDEO_PROCESSING_REFUSED')
    job=store.get(JOB);plan_job=store.get(PLAN_JOB)
    need(job is not None and plan_job is not None,'RESUME_STORYBOARD_JOBS_REQUIRED')
    need((job['source_platform'],job['source_id'],job['job_type'],job['queue'],job['policy_version'],
          job['state'],job['attempts'],job['error_code'],job['stage'],job['lease_owner'],job['lease_until'],
          job['external_publish_id'])==
         ('youtube',SOURCE,'youtube_replay_download_v4','video','replay-fifo-v4',
          'awaiting_review',1,ERROR,'youtube_whole_acquiring',None,None,None),
         'RESUME_STORYBOARD_HELD_JOB_CHANGED')
    need((plan_job['source_platform'],plan_job['source_id'],plan_job['job_type'],plan_job['state'],
          plan_job['error_code'])==('youtube',SOURCE,'youtube_replay_plan_v4','completed',None),
         'RESUME_STORYBOARD_PLAN_JOB_CHANGED')
    candidates=db.execute('SELECT * FROM replay_fifo_v4 WHERE candidate_id=?',(CANDIDATE,)).fetchall()
    need(len(candidates)==1,'RESUME_STORYBOARD_FIFO_REQUIRED');candidate=dict(candidates[0])
    need((candidate['source_id'],candidate['state'],candidate['reason'],candidate['plan_job_id'],
          candidate['download_job_id'],candidate['publication_job_id'])==
         (SOURCE,'hold',ERROR,PLAN_JOB,JOB,None),'RESUME_STORYBOARD_FIFO_CHANGED')
    need(not db.execute('SELECT 1 FROM publications WHERE source_platform=? AND source_id=?',('youtube',SOURCE)).fetchone(),
         'RESUME_STORYBOARD_PUBLICATION_EXISTS')
    need(not db.execute('SELECT 1 FROM source_tombstones WHERE source_platform=? AND source_id=?',('youtube',SOURCE)).fetchone(),
         'RESUME_STORYBOARD_SOURCE_TOMBSTONED')
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='replay_part_groups_v5'").fetchone():
        need(not db.execute('SELECT 1 FROM replay_part_groups_v5 WHERE source_id=?',(SOURCE,)).fetchone(),
             'RESUME_STORYBOARD_GROUP_EXISTS')
    need(not db.execute('SELECT 1 FROM audit WHERE action=? AND target=?',(VERSION,JOB)).fetchone(),
         'RESUME_STORYBOARD_ALREADY_APPLIED')

    service=YouTubeWholeReplayDownload(settings,store)
    binding=service.resource_binding(job)
    state=job['result'][STATE]
    need(state['state']=='acquiring' and state['binding']==binding and state['human_review'] is False and
         state['publication_authorized'] is False and not any(k in state for k in
         ('source_asset','receipt','parts','delivery_v5')),'RESUME_STORYBOARD_BINDING_STATE_CHANGED')
    need(binding['whole_replay_plan']==job['payload']['whole_replay_plan'] and
         digest(binding['whole_replay_plan'])==job['payload']['whole_replay_plan_sha256'],
         'RESUME_STORYBOARD_ORIGINAL_PLAN_CHANGED')
    need([(item['format_id'],item['filesize']) for item in binding['source_plan']['selected_formats']]==list(SELECTED),
         'RESUME_STORYBOARD_SELECTED_FORMATS_CHANGED')

    area=root/'media'/JOB/'youtube-whole-v4'
    files={path.name:path for path in area.iterdir()}
    need(area.is_dir() and set(files)=={'download-result.json','ticket.json',
        'official-source-5814d89c-18ad-412a-8edc-5c5176eb4e22.json',
        'extractor-observation-9209866c-d904-4e69-8440-181bf5ea8fa5.json'},
        'RESUME_STORYBOARD_WORKSPACE_CHANGED')
    failure,failure_info=document(files['download-result.json'])
    ticket,ticket_info=document(files['ticket.json'])
    official,official_info=document(files['official-source-5814d89c-18ad-412a-8edc-5c5176eb4e22.json'])
    observation,observation_info=document(files['extractor-observation-9209866c-d904-4e69-8440-181bf5ea8fa5.json'])
    need(ticket['job_id']==JOB and ticket['binding']==binding,'RESUME_STORYBOARD_TICKET_CHANGED')
    need(official.get('version')=='youtube-official-acquisition-source-v4' and
         official.get('job_id')==JOB and official.get('source_id')==SOURCE and
         official.get('identity_sha256')==binding['official_identity_sha256'] and
         (official.get('official_raw') or {}).get('id')==binding['official_raw'].get('id') and
         official.get('human_review') is official.get('publication_authorized') is False,
         'RESUME_STORYBOARD_OFFICIAL_SOURCE_CHANGED')
    need(failure['error_code']==ERROR and failure['review'] is True and failure['retryable'] is False and
         failure['source_plan']==binding['source_plan'] and failure['http']['requests']==OWN_REQUESTS and
         failure['http']['response_bytes']==OWN_RESPONSE_BYTES,'RESUME_STORYBOARD_FAILURE_CHANGED')
    observation_path=files['extractor-observation-9209866c-d904-4e69-8440-181bf5ea8fa5.json']
    observation_ref={'file':str(observation_path),
        'bytes':observation_info['bytes'],'sha256':observation_info['sha256'],'version':'youtube-extractor-observation-v4'}
    need(failure['metadata_observation']==observation_ref,'RESUME_STORYBOARD_OBSERVATION_REF_CHANGED')
    need(observation['version']=='youtube-extractor-observation-v4' and observation['job_id']==JOB and
         observation['binding_sha256']==digest(binding) and all(observation[k] is False for k in
         ('media_downloaded','human_review','publication_authorized')),'RESUME_STORYBOARD_OBSERVATION_CHANGED')
    observed=datetime.fromisoformat(observation['recorded_at'].replace('Z','+00:00')).timestamp()
    need(0<=time.time()-observed<=86400,'RESUME_STORYBOARD_OBSERVATION_OUT_OF_BOUND')
    selected=select_from_info(observation['extractor_info'],binding)
    need([(item['format_id'],item['filesize']) for item in selected]==list(SELECTED) and
         all(item.get('has_drm') is False for item in selected),'RESUME_STORYBOARD_SELECTOR_REFUSED')
    fresh_plan=plan_from_info(observation['extractor_info'],binding)
    inventory=fresh_plan['source_plan_basis']['source_inventory']
    storyboards=sorted(item['format_id'] for item in inventory if optional_storyboard(item))
    need(storyboards==list(STORIES),'RESUME_STORYBOARD_EXACT_DIFF_REQUIRED')
    calls=[dict(row) for row in db.execute('SELECT * FROM api_calls WHERE job_id=? ORDER BY call_id',(JOB,))]
    need(len(calls)==OWN_REQUESTS and all(row['provider']=='youtube_download_http' and row['state']=='done' for row in calls),
         'RESUME_STORYBOARD_HTTP_LEDGER_CHANGED')
    resources=evaluate_whole_resources(binding['whole_replay_plan'],shutil.disk_usage(root).free)
    need(resources['state']=='ready','RESUME_STORYBOARD_PEAK_DISK_REQUIRED')
    return {
        'version':VERSION,'job':job,'plan_job':plan_job,'candidate':candidate,
        'job_sha256':digest(job),'plan_job_sha256':digest(plan_job),'candidate_sha256':digest(candidate),
        'binding_sha256':digest(binding),'plan_sha256':digest(binding['whole_replay_plan']),
        'runtime_sha256':sha(raw),'qualified_release_sha256':release_sha,
        'workspace':{'download-result.json':failure_info,'ticket.json':ticket_info,
            files['official-source-5814d89c-18ad-412a-8edc-5c5176eb4e22.json'].name:official_info,
            files['extractor-observation-9209866c-d904-4e69-8440-181bf5ea8fa5.json'].name:observation_info},
        'metadata_observation':observation_ref,
        'storyboards_ignored':storyboards,'selected_formats':binding['source_plan']['selected_formats'],
        'api_ledger_sha256':digest(calls),'resource_state':resources['state'],
        'required_free_bytes':binding['whole_replay_plan']['whole_budget']['required_free_bytes'],
        'attempts_preserved':1,
        'provider_requests':0,'new_jobs':0,'publication_created':False,'planned_at':time.time(),
    }


def apply(store,plan,refresh,*,expected_sha):
    need(plan_digest(plan)==expected_sha,'RESUME_STORYBOARD_PLAN_CHANGED')
    with store.transaction() as db:
        need(plan_digest(refresh(db))==expected_sha,'RESUME_STORYBOARD_SNAPSHOT_CHANGED')
        old=plan['job'];candidate=plan['candidate'];now=time.time()
        need(db.execute("""UPDATE jobs SET state='retry_scheduled',error_code=NULL,next_run=?,updated_at=?
            WHERE job_id=? AND state='awaiting_review' AND error_code=? AND stage='youtube_whole_acquiring'
            AND attempts=1 AND updated_at=? AND payload=? AND result=? AND lease_owner IS NULL
            AND lease_until IS NULL AND external_publish_id IS NULL""",
            (now,now,JOB,ERROR,old['updated_at'],encode(old['payload']),encode(old['result']))).rowcount==1,
            'RESUME_STORYBOARD_JOB_CAS_FAILED')
        need(db.execute("""UPDATE replay_fifo_v4 SET state='download_queued',reason=NULL,updated_at=?
            WHERE candidate_id=? AND state='hold' AND reason=? AND updated_at=? AND plan_job_id=?
            AND download_job_id=? AND publication_job_id IS NULL""",
            (now,CANDIDATE,ERROR,candidate['updated_at'],PLAN_JOB,JOB)).rowcount==1,
            'RESUME_STORYBOARD_FIFO_CAS_FAILED')
        outcome={'plan_sha256':expected_sha,'original_plan':plan,'attempts_preserved':1,
            'original_error_preserved':ERROR,'provider_requests':0,'new_jobs':0,
            'publication_created':False,'storyboards_ignored':plan['storyboards_ignored'],
            'selected_formats':plan['selected_formats'],'workspace_deleted_or_rewritten':False}
        db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
            (now,'owner-authorized-maintenance',VERSION,JOB,encode(outcome)))
    return {'state':'existing_download_retry_scheduled','job_id':JOB,'attempts_preserved':1,
        'storyboards_ignored':list(STORIES),'provider_requests':0,'new_jobs':0,
        'publication_created':False,'workspace_deleted_or_rewritten':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-sha256',required=True)
    parser.add_argument('--qualified-release-sha256',required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--plan-sha256')
    args=parser.parse_args()
    need(os.name=='posix' and os.getuid()==os.getgid()==10001 and __debug__,'RESUME_STORYBOARD_APP_UID_REQUIRED')
    need(bool(args.apply)==bool(args.plan_sha256),'RESUME_STORYBOARD_EXACT_PLAN_REQUIRED')
    settings=Settings.load();store=Store(settings.db_path)
    with resource_guard(*(settings.root/'data/locks'/name for name in
                          ('video','moderation','replay-fifo-dispatch-v4'))):
        refresh=lambda db:snapshot(settings,store,db,args.runtime_sha256,args.qualified_release_sha256)
        with store.transaction() as db:plan=refresh(db)
        expected=plan_digest(plan)
        if not args.apply:
            print(encode({'state':'readonly_plan','job_id':JOB,'plan_sha256':expected,
                'attempts_preserved':1,'storyboards_ignored':plan['storyboards_ignored'],
                'selected_formats':plan['selected_formats'],'provider_requests':0}))
            return
        print(encode(apply(store,plan,refresh,expected_sha=args.plan_sha256)))


if __name__=='__main__':
    main()
