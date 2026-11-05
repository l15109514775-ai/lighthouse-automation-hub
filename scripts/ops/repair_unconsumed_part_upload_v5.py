"""One fixed pre-selection checkpoint repair; default plan, no platform action.

Run only by the trusted root host operator as app UID in the accepted video
container. The owner must first clear the identified retired local caches.
This operator never opens a browser, claims work, changes a lease or resets
attempts. Ordinary Worker claim supplies the next attempt and complete QA.
"""
from copy import deepcopy
from contextlib import ExitStack,contextmanager
from datetime import datetime
from pathlib import Path
import argparse,hashlib,json,os,stat,time,uuid

CHILD='5c4be72a-e6ae-4d42-b030-9540f3d54baf'
ATTEMPT='e3a44d84-5e8f-4b70-8b3b-8d63581b17c3'
PARENT='cf7b357c-a547-4a50-9943-15e64d69888c'
DOWNLOAD='f920d1da-12dc-46e8-83d7-80b129ec1b68'
SOURCE='6wHqfDmNF0U'
UID='1000000000000000'
STATE='replay_part_video_v5'
ERROR='REPLAY_BROWSER_EXISTING_DRAFT_LOOKUP_REQUIRED'
ERROR_SHA='fd7232b10bea0b0f169ea3cc559b2dfd83e703146f27f4eee446f565045b1334'
INVENTORY_SHA='f8a8b12fc0e94a7d0853be6ecf9d6b513d3d079bfb885783a56d6fcb55fae9ff'

def need(value,code):
    if not value:raise ValueError(code)
def wire(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
def digest(value):return hashlib.sha256(wire(value)).hexdigest()
def read_ref(root,reference):
    need(isinstance(reference,dict) and set(reference)=={'file','bytes','sha256'},'REPAIR_REFERENCE_INVALID')
    file=Path(reference['file']);base=root/'media'/CHILD
    need(file.is_absolute() and file.resolve()==file and file.is_relative_to(base) and
        not any(p.is_symlink() for p in (file,*file.parents)),'REPAIR_EVIDENCE_PATH_INVALID')
    before=file.stat();need(stat.S_ISREG(before.st_mode) and type(reference['bytes']) is int and 0<before.st_size==reference['bytes']<=2_000_000,'REPAIR_EVIDENCE_SIZE_INVALID')
    raw=file.read_bytes();after=file.stat()
    need(len(raw)==before.st_size and all(getattr(before,k)==getattr(after,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')) and
        hashlib.sha256(raw).hexdigest()==reference['sha256'],'REPAIR_EVIDENCE_CHANGED')
    return json.loads(raw)
def reference(file):
    need(file.is_absolute() and file.resolve()==file and not any(p.is_symlink() for p in (file,*file.parents)), 'REPAIR_REFERENCE_PATH_INVALID')
    st=file.stat();need(stat.S_ISREG(st.st_mode) and 0<st.st_size<=2_000_000,'REPAIR_REFERENCE_SIZE_INVALID')
    raw=file.read_bytes();return {'file':str(file),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
def clean_entry(root,ref,*,now=None):
    value=read_ref(root,ref);at=datetime.fromisoformat(value['observed_at'].replace('Z','+00:00'));now=time.time() if now is None else now
    need(at.tzinfo is not None and 0<=now-at.timestamp()<=300,'REPAIR_CLEAN_ENTRY_EXPIRED')
    need(value['version']=='replay-native-clean-upload-entry-v1' and value['site_uid']==UID and
        value['url']=='https://member.bilibili.com/platform/upload/video/frame' and
        all(value[k]==0 and type(value[k]) is int for k in ('file_selections','metadata_edits','save_clicks','submit_clicks')),'REPAIR_CLEAN_ENTRY_REQUIRED')
    counts=value['counts'];need(counts=={'upload_area':1,'continue':0,'discard':0,'file_rows':0,'title_forms':0,'outer_tasks':0,'dialogs':0} and
        all(type(v) is int for v in counts.values()),'REPAIR_CACHE_NOT_CLEAN')
    p=value['protection'];e=p['evidence']
    need(p['blocked'] is False and e['version']=='bilibili-video-protection-guard-v3' and e.get('inspection_failed') is not True and
        e['truncated'] is False and e['top_level_authentication'] is False and
        e['visible_dialogs']==e['visible_challenge_controls']==e['challenge_frames']==[],'REPAIR_NATIVE_PROTECTION_REQUIRED')
    native=value['native'];uploaders=native['uploaders']
    need(value.get('all_clicks')==0 and value.get('url_query_keys')==[] and native['url']==value['url'] and
        native['total_file_item_count']==native['visible_file_item_count']==0 and
        '继续编辑' not in native['body_text'] and '本地浏览器存在' not in native['body_text'] and
        all(u['files']==[] for u in uploaders) and
        len([u for u in uploaders if u['visible'] is True and '.mp4' in u['accept'].lower().split(',')])==1,'REPAIR_NATIVE_ENTRY_INCONSISTENT')
    return value
def unconsumed(service,job,binding,state):
    store=service.store;root=service.settings.root
    need(job['job_id']==CHILD and job['job_type']=='replay_part_publish_v5_p01' and job['policy_version']=='replay-part-video-policy-v5' and
        job['source_platform']=='youtube' and job['source_id']==SOURCE and job['queue']=='video' and job['state']=='awaiting_review' and
        job['stage']=='short_video_reconcile_required' and job['error_code']==ERROR and job['attempts']==1 and
        job['lease_owner'] is None and job['lease_until'] is None and job['external_publish_id'] is None,'REPAIR_EXACT_JOB_REQUIRED')
    need(binding['parent_job_id']==PARENT and binding['download_job_id']==DOWNLOAD and binding['part_index']==0 and binding['part_count']==1 and
        binding['site_uid']==UID and binding['source_id']==SOURCE and state['state']=='upload_unknown' and state['attempt_id']==ATTEMPT and
        type(state.get('private_upload_selections')) is int and state['private_upload_selections']==1 and state['public_submission_approved'] is False and state['human_review'] is False and
        job['result']['publish_performed'] is False and 'preselection_repair_v1' not in state and
        not any(k in state for k in ('upload_observation','upload_coverage','upload_evidence','native_preparation','result_observation','owner_acceptance')),'REPAIR_CONSUMED_OR_CHANGED_CHECKPOINT')
    need(not store.query("SELECT 1 FROM publications WHERE source_platform='youtube' AND source_id=?",(SOURCE,)) and
        not store.query("SELECT 1 FROM source_tombstones WHERE source_platform='youtube' AND source_id=?",(SOURCE,)) and
        not store.query("SELECT 1 FROM jobs WHERE queue='video' AND (state='processing' OR lease_owner IS NOT NULL OR lease_until IS NOT NULL)") and
        store.query("SELECT paused FROM pipelines WHERE name='video'")[0]['paused']==1,'REPAIR_QUIET_PAUSED_SOURCE_REQUIRED')
    need(store.query('SELECT state,external_id FROM replay_part_children_v5 WHERE child_job_id=?',(CHILD,))==
        [{'state':'prepared','external_id':None}],'REPAIR_CHILD_REGISTRY_CONSUMED')
    audits=store.query('SELECT action,outcome FROM audit WHERE target=?',(CHILD,))
    need(not any('reserve_submit' in a['action'] or a['action']=='replay_part_unconsumed_preselection_repaired' or
        a['outcome'] in ('submitting','submit_unknown','completed','awaiting_moderation') for a in audits),'REPAIR_PRIOR_CONSUMPTION_OR_REPAIR')
    intents=root/'media'/CHILD/'replay-native-intents'
    need(not any(p.is_symlink() for p in (intents,*intents.parents)) and (not intents.exists() or intents.is_dir() and not list(intents.iterdir())),'REPAIR_SELECTION_INTENT_EXISTS')
    folder=root/'media'/CHILD/service.NATIVE_DIRECTORY/ATTEMPT
    need(not any((folder/name).exists() or (folder/name).is_symlink() for name in
        ('upload-coverage.json','upload-observation.json','native-preparation.json','reservation.json','public-result.json','lookup-part-result.json')),'REPAIR_UPLOAD_OR_SUBMIT_EVIDENCE_EXISTS')
    error_ref=reference(folder/'last-error.json');inventory_ref=reference(folder/'error-upload-inventory.json')
    need(error_ref['sha256']==ERROR_SHA and inventory_ref['sha256']==INVENTORY_SHA,'REPAIR_ORIGINAL_ERROR_CHANGED')
    body=read_ref(root,error_ref);read_ref(root,inventory_ref)
    need(body=={'action':'upload','error_code':ERROR,'publication_result_unknown':False,'video_selection_attempted_in_session':False},'REPAIR_NO_SELECTION_CAUSE_REQUIRED')
    policy=service._validate_snapshot_binding(job,binding,verify_artifact_hash=False);service._require_workflow_launch(policy)
    auth=state['automation_authorization']
    need(auth['version']==service.WORKFLOW_AUTHORIZATION_VERSION and auth['binding_sha256']==digest(binding) and
        auth['workflow_authorization_reference']==policy['workflow_authorization_reference'] and
        auth['human_review'] is False and auth['per_artifact_owner_acceptance'] is False,'REPAIR_AUTHORIZATION_CHANGED')
    file=Path(binding['file']);st=file.stat()
    need(file.resolve()==file and not any(p.is_symlink() for p in (file,*file.parents)) and stat.S_ISREG(st.st_mode) and st.st_size==binding['file_bytes'],'REPAIR_SOURCE_FILE_CHANGED')
    return {'last_error':error_ref,'error_inventory':inventory_ref}
def archive_originals(root,plan_sha,job,refs):
    folder=root/'media'/CHILD/'preselection-repair-v1'/plan_sha
    need(folder.resolve()==folder and not any(p.is_symlink() for p in (folder,*folder.parents)),'REPAIR_ARCHIVE_PATH_INVALID')
    folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    files={'original-job.json':wire(job)+b'\n'}
    for key,ref in refs.items():
        read_ref(root,ref);raw=Path(ref['file']).read_bytes()
        need(len(raw)==ref['bytes'] and hashlib.sha256(raw).hexdigest()==ref['sha256'],'REPAIR_EVIDENCE_CHANGED')
        files[key+'.json']=raw
    result={}
    for name,raw in files.items():
        file=folder/name
        try:
            with file.open('xb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
            file.chmod(0o600)
        except FileExistsError:need(not file.is_symlink() and file.read_bytes()==raw,'REPAIR_ARCHIVE_CHANGED')
        result[name]=reference(file)
    if os.name=='posix':
        fd=os.open(folder,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(fd)
        finally:os.close(fd)
    return result
def repair(service,request,*,apply=False,expected_plan_sha256=None):
    from services.hub.short_video_web import PRINCIPAL
    need(type(apply) is bool and isinstance(request,dict) and set(request)=={'version','job_id','before_row_sha256','clean_entry','operator_reference'},'REPAIR_REQUEST_INVALID')
    need(request['version']=='unconsumed-part-upload-repair-request-v1' and request['job_id']==CHILD and
        isinstance(request['operator_reference'],str) and 0<len(request['operator_reference'].strip())<=2048,'REPAIR_OPERATOR_REFERENCE_REQUIRED')
    service._operator(PRINCIPAL);job,binding,state=service.snapshot(CHILD,write=False)
    need(digest(job)==request['before_row_sha256'],'REPAIR_ROW_CHANGED')
    clean_entry(service.settings.root,request['clean_entry']);old_refs=unconsumed(service,job,binding,state)
    plan={**request,'original_error_evidence':old_refs,'native_attempt_id':ATTEMPT,'job_attempts_before':1,'next_checkpoint':'prepared',
        'next_job_state':'pending','next_unconsumed_selection_reservation':0,'fresh_complete_qa_required':True,'new_authorization':False,'files_selected':0,'public_submissions':0}
    plan_sha=digest(plan)
    if not apply:return {'state':'verified_plan_only','plan_sha256':plan_sha,'plan':plan}
    need(expected_plan_sha256==plan_sha,'REPAIR_EXACT_PLAN_REQUIRED')
    archived=archive_originals(service.settings.root,plan_sha,job,old_refs)
    # Include the complete old state in the same transaction as the existing
    # checkpoint audit. Do not misclassify the pre-chooser reservation as a blob.
    marker={'version':'unconsumed-part-upload-preselection-repair-v1','request':deepcopy(request),'plan_sha256':plan_sha,
        'old_checkpoint':deepcopy(state),'old_row_sha256':digest(job),'old_error_code':job['error_code'],
        'original_error_evidence':{key:archived[key+'.json'] for key in old_refs},
        'original_error_paths':{key:ref['file'] for key,ref in old_refs.items()},
        'immutable_originals':archived,'actual_file_selection_consumed':False,'recorded_at':time.time()}
    clean_entry(service.settings.root,request['clean_entry']);unconsumed(service,job,binding,state)
    updated={**state,'state':'prepared','private_upload_selections':0,'preselection_repair_v1':marker}
    service._update(PRINCIPAL,job,updated,job_state='pending',stage='replay_part_unconsumed_preselection_repaired')
    after=service.store.get(CHILD)
    need(after['attempts']==1 and after['lease_owner'] is None and after['lease_until'] is None and after['payload']==job['payload'] and
        after['result']=={**job['result'],STATE:updated} and after['external_publish_id'] is None,'REPAIR_POSTCONDITION_FAILED')
    service.store.audit(PRINCIPAL,'replay_part_unconsumed_preselection_repaired',CHILD,wire({'plan_sha256':plan_sha,
        'before_row_sha256':digest(job),'after_row_sha256':digest(after),'immutable_originals':archived,'attempts_reset':False,
        'actual_file_selections':0,'new_authorization':False}).decode())
    return {'state':'same_unconsumed_attempt_pending','job_id':CHILD,'attempts':after['attempts'],'native_attempt_id':ATTEMPT,'plan_sha256':plan_sha,
        'before_row_sha256':digest(job),'after_row_sha256':digest(after),'new_authorization':False,'files_selected':0,'public_submissions':0}

@contextmanager
def repair_locks(root):
    from services.hub.locks import resource_guard
    from services.hub.errors import HubError
    deadline=time.monotonic()+20;last=None
    while True:
        if last is not None and time.monotonic()>=deadline:raise last
        with ExitStack() as stack:
            try:stack.enter_context(resource_guard(*(root/'data/locks'/name for name in
                ('video','moderation','replay-multipart-producer-v4','replay-fifo-dispatch-v4','bilibili-video-profile','heavy'))))
            except HubError as error:
                if error.code!='HEAVY_RESOURCE_BUSY' or not error.retryable or time.monotonic()>=deadline:raise
                last=error
            else:
                yield
                return
        time.sleep(min(.4,max(0,deadline-time.monotonic())))

def main():
    from services.hub.config import Settings
    from services.hub.db import Store
    from services.hub.replay_parts_v5 import ReplayPartVideoWeb
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--clean-entry',type=Path,required=True);p.add_argument('--clean-sha256',required=True)
    p.add_argument('--operator-reference',required=True);p.add_argument('--apply-plan-sha256');a=p.parse_args()
    need(os.getuid()==os.getgid()==10001,'REPAIR_APP_UID_REQUIRED');settings=Settings.load();store=Store(settings.db_path);service=ReplayPartVideoWeb(settings,store)
    with repair_locks(settings.root):
        ref=reference(a.clean_entry);need(ref['sha256']==a.clean_sha256,'REPAIR_CLEAN_SHA_CHANGED')
        job=store.get(CHILD);request={'version':'unconsumed-part-upload-repair-request-v1','job_id':CHILD,'before_row_sha256':digest(job),'clean_entry':ref,'operator_reference':a.operator_reference}
        result=repair(service,request,apply=a.apply_plan_sha256 is not None,expected_plan_sha256=a.apply_plan_sha256)
        folder=settings.root/'media'/CHILD/'preselection-repair-v1'/str(uuid.uuid4());folder.mkdir(mode=0o700,parents=True)
        (folder/'result.json').write_bytes(wire(result)+b'\n');print(json.dumps({'result_file':str(folder/'result.json'),**result}))

if __name__=='__main__':main()
