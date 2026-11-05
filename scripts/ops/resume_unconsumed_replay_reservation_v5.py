"""One fixed V5 draft whose public-send reservation was never consumed.

Plan reopens immutable native lookup evidence only. Apply performs a fresh
native lookup and preparation, then reuses the original SQL publication for
its first native submit. No new upload, fake lease, counter reset or retry.
"""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit,parse_qsl
import argparse,hashlib,json,os,re,stat,time,uuid

JOB='19e6eb0a-7d5c-4135-950f-42fb9fe31c1d'
GROUP='1105fdca-687a-49d0-80a5-436b6e2aa586'
DOWNLOAD='4ca65e35-8a45-4746-b6c4-13de860409a8'
ATTEMPT='004211f0-c566-44fa-8085-8428d7a63b00'
SOURCE='synthC00003';UID='1000000000000000';DRAFT='4071738'
URL='https://member.bilibili.com/platform/upload/video/frame?type=draft&draftId='+DRAFT
BINDING_SHA='ef3221ab97ff62dbb12071dca9ad979e458d67475d68212f7c042953c053e00b'
KEY='b8c6e88438a09f5926807a26a0b4fac1830f0b75cc5128fdb2737856d34723af'
ERROR_SHA='49a246aa5ef6ef1bf77851bd8c94ebb4c3ef6754a6977898af9f267563384973'
VERSION='replay-unconsumed-reservation-recovery-v5'
STATE='replay_part_video_v5'
KINDS={'video-selection.json','cover-completion.json','private-draft-save.json'}
SOURCES=('adapters/replay_native_driver.mjs','adapters/replay_later_result_lookup_v2.mjs',
         'adapters/browser_replay_part_v5.mjs','services/hub/replay_parts_v5.py',
         'services/hub/replay_part_publish_worker_v5.py')

def need(value,code):
    if not value:raise ValueError(code)

def wire(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def sha(raw):return hashlib.sha256(raw).hexdigest()
def digest(value):return sha(wire(value))

def read(file,maximum=2_000_000):
    file=Path(file)
    need(file.is_absolute() and file.resolve()==file and not any(p.is_symlink() for p in (file,*file.parents)),'RECOVERY_PATH_CHANGED')
    with os.fdopen(os.open(file,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)),'rb') as stream:
        before=os.fstat(stream.fileno())
        need(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and 0<before.st_size<=maximum,'RECOVERY_FILE_INVALID')
        raw=stream.read(maximum+1);after=os.fstat(stream.fileno())
    need(len(raw)==before.st_size and all(getattr(before,k)==getattr(after,k) for k in
        ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')),'RECOVERY_FILE_CHANGED')
    return raw

def reference(file,maximum=2_000_000):
    raw=read(file,maximum);return {'file':str(file),'bytes':len(raw),'sha256':sha(raw)},raw

def save_raw_once(file,raw):
    from services.hub.media_processing import sync_directory
    need(file.parent.resolve()==file.parent and not any(p.is_symlink() for p in (file,*file.parents)),'RECOVERY_PATH_CHANGED')
    need(type(raw) is bytes and 0<len(raw)<=2_000_000,'RECOVERY_RAW_ARCHIVE_INVALID')
    with os.fdopen(os.open(file,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600),'wb') as stream:
        stream.write(raw);stream.flush();os.fsync(stream.fileno())
    sync_directory(file.parent)

def save_once(file,value):save_raw_once(file,wire(value)+b'\n')

def archive_raw(file,destination,expected=None):
    old,raw=reference(file)
    need(expected is None or old==expected,'ORIGINAL_RAW_EVIDENCE_CHANGED')
    save_raw_once(destination,raw);new,check=reference(destination)
    need(raw==check and old['bytes']==new['bytes'] and old['sha256']==new['sha256'],'RAW_ARCHIVE_MISMATCH')
    return {'original':old,'archive':new}

def same(a,b):return type(a) is type(b) and wire(a)==wire(b)

def draft_link(value):
    need(type(value) is str and 0<len(value)<=2048 and value.strip()==value and
         not any(ord(c)<32 or ord(c)==127 for c in value) and '\\' not in value and '#' not in value,
         'NATIVE_DRAFT_URL_INVALID')
    # Native manager cards use root-relative hrefs; resolve only this exact
    # route against the fixed official origin, never a supplied base or host.
    if value.startswith('/'):
        need(value.startswith('/platform/upload/video/frame?'),'NATIVE_DRAFT_URL_INVALID')
        value='https://member.bilibili.com'+value
    u=urlsplit(value);q=parse_qsl(u.query,keep_blank_values=True)
    need(u.scheme=='https' and u.netloc=='member.bilibili.com' and u.path=='/platform/upload/video/frame' and not u.fragment and
         re.fullmatch(r'(?:type=draft&draftId=[1-9][0-9]{0,17}|draftId=[1-9][0-9]{0,17}&type=draft)',u.query) and
         sorted(k for k,v in q)==['draftId','type'] and dict(q)['type']=='draft' and re.fullmatch('[1-9][0-9]{0,17}',dict(q)['draftId']),
         'NATIVE_DRAFT_URL_INVALID')
    return dict(q)['draftId'],u.geturl()

def checked_draft_reference(root,ref,base,title,*,now=None,maximum_age=300):
    """Reopen the complete manager -> unique draft -> exact editor chain."""
    file=Path(ref['file']);folder=file.parent
    need(folder.parent==root/'media'/JOB/'replay-browser-part-v5'/ATTEMPT/'later-lookups-v2' and
         file.name=='draft-observation.json' and str(uuid.UUID(folder.name))==folder.name,'NATIVE_DRAFT_PATH_REQUIRED')
    actual,raw=reference(file);need(actual==ref,'NATIVE_DRAFT_REFERENCE_CHANGED');body=json.loads(raw)
    def common(value):
        need(all(k in value and same(value[k],v) for k,v in base.items()) and
             all(type(value.get(k)) is int and value[k]==0 for k in ('file_selections','save_clicks','submit_clicks','player_seeks')) and
             value.get('source_or_media_modified') is False,'NATIVE_DRAFT_BINDING_CHANGED')
    common(body)
    need(body.get('version')=='replay-part-native-draft-observation-v5' and body.get('state')=='draft_found' and
         body.get('draft_id')==DRAFT and body.get('draft_url')==URL and body.get('read_only') is True and
         all(body.get(k) is False for k in ('public_accessible','server_file_hash_verified','human_review','publication_authorized')) and
         body.get('navigation_actions')=={'draft_tab_clicks':1,'draft_editor_navigations':1},'NATIVE_DRAFT_SCOPE_CHANGED')
    observed=datetime.fromisoformat(body['observed_at'].replace('Z','+00:00'))
    need(observed.tzinfo is not None and -30<=(time.time() if now is None else now)-observed.timestamp()<=maximum_age,'NATIVE_DRAFT_PROOF_EXPIRED')
    records={}
    for key,name,version in (('manager_observation','manager-row.json','replay-native-manager-row-v2'),
            ('draft_manager_observation','draft-manager.json','replay-part-native-draft-manager-v5'),
            ('draft_editor_observation','draft-editor.json','replay-part-native-draft-editor-v5')):
        item=body[key];need(item['file']==str(folder/name),'NATIVE_DRAFT_BUNDLE_PATH_CHANGED')
        actual,raw=reference(folder/name);need(actual==item,'NATIVE_DRAFT_BUNDLE_CHANGED');v=json.loads(raw);common(v)
        need(v.get('version')==version,'NATIVE_DRAFT_BUNDLE_VERSION_CHANGED');records[key]=v
        if key!='manager_observation':
            need(v.get('read_only') is True,'NATIVE_DRAFT_BUNDLE_NOT_READONLY')
            shot=v['screenshot'];name='draft-manager.png' if key=='draft_manager_observation' else 'draft-editor.png'
            need(shot['file']==str(folder/name) and reference(folder/name,5_000_000)[0]==shot,'NATIVE_DRAFT_SCREENSHOT_CHANGED')
    manager=records['manager_observation'];dom=manager['visible_dom']
    need(manager.get('selected_row') is None and manager.get('known_external_id') is None and dom['truncated'] is False and
         type(dom.get('declared_all_count')) is int and 0<=dom['declared_all_count']<=60 and dom['declared_all_count']==len(dom['rows']) and
         not any(r.get('row_text_truncated') is not False for r in dom['rows']) and
         not any(a['text']==title for r in dom['rows'] for a in r['title_links']),'NATIVE_PUBLIC_INVENTORY_INCOMPLETE_OR_MATCHED')
    drafts=records['draft_manager_observation'];dom=drafts['visible_dom'];count=drafts['declared_draft_count']
    need(type(count) is int and 1<=count<=60 and dom['section_count']==1 and dom['truncated'] is False and len(dom['rows'])==count and
         len(dom['pagination'])==1 and re.fullmatch(r'\s*共\s*1\s*页\s*/\s*'+str(count)+r'\s*个\s*[，,]?\s*',dom['pagination'][0]) and
         dom['selected_pages']==['1'],'NATIVE_DRAFT_INVENTORY_INCOMPLETE')
    matches=[];ids=set()
    for row in dom['rows']:
        need(len(row['titles'])==len(row['covers'])==len(row['saved_times'])==1 and
             len(row['titles'][0]['links'])==len(row['covers'][0]['alts'])==1,'NATIVE_DRAFT_ROW_AMBIGUOUS')
        t=row['titles'][0];a=t['links'][0];identity=draft_link(a['href'])
        need(identity==draft_link(row['covers'][0]['href']) and t['title']==a['text']==row['covers'][0]['alts'][0] and identity[0] not in ids,
             'NATIVE_DRAFT_ROW_AMBIGUOUS');ids.add(identity[0])
        if a['text']==title:matches.append((identity,row['saved_times'][0]))
    need(matches==[((DRAFT,URL),body['saved_at_text'])],'NATIVE_DRAFT_TARGET_NOT_UNIQUE')
    editor=records['draft_editor_observation']['visible_dom']
    need(draft_link(editor['url'])==(DRAFT,URL) and editor['title']==title and editor['source_url']==base['source_url'],'NATIVE_DRAFT_EDITOR_CHANGED')
    return body

def publications(store):
    return store.query("SELECT * FROM publications WHERE job_id=? OR(source_platform='youtube' AND source_id=?)",(JOB,SOURCE))

def validate_original(job,state,pubs,binding):
    need(job['job_id']==JOB and job['source_id']==SOURCE and job['job_type']=='replay_part_publish_v5_p01' and
         job['policy_version']=='replay-part-video-policy-v5' and job['state']=='publish_unknown' and job['attempts']==1 and
         job['lease_owner'] is None and job['lease_until'] is None and job['external_publish_id'] is None,'ORIGINAL_JOB_CHANGED')
    need(digest(binding)==BINDING_SHA and binding['site_uid']==UID and binding['parent_job_id']==GROUP and
         binding['download_job_id']==DOWNLOAD and binding['part_index']==0 and binding['part_count']==1 and binding['human_review'] is False,
         'ORIGINAL_BINDING_CHANGED')
    need(state['state']=='submit_unknown' and state['attempt_id']==ATTEMPT and state['key']==KEY and state['human_review'] is False and
         state['public_submission_approved'] is False and not state.get('result_observation') and 'owner_acceptance' not in state and
         'unconsumed_recovery_v5' not in state and job['result']['publish_performed'] is False,'ORIGINAL_CHECKPOINT_CHANGED')
    need(len(pubs)==1,'ORIGINAL_PUBLICATION_CHANGED');pub=pubs[0]
    expected={'idempotency_key':KEY,'job_id':JOB,'platform':'bilibili','source_platform':'youtube','source_id':SOURCE,
              'account_id':'site_uid:'+UID,'kind':'video_website_replay_part_v5_p01','state':'unknown','external_id':None,'external_url':None}
    need(all(k in pub and same(pub[k],v) for k,v in expected.items()),'ORIGINAL_PUBLICATION_CHANGED')

def intent_hashes(root):
    folder=root/'media'/JOB/'replay-native-intents'
    return {p.name:sha(read(p)) for p in folder.iterdir()}

def quiescent(store):
    need(store.query("SELECT paused FROM pipelines WHERE name='video'")==[{'paused':1}],'VIDEO_PAUSE_REQUIRED')
    need(not store.query("SELECT 1 FROM jobs WHERE queue='video' AND (state='processing' OR lease_owner IS NOT NULL OR lease_until IS NOT NULL)"),'ACTIVE_VIDEO_REFUSED')

def snapshot(settings,store,pins,release_sha):
    from services.hub.replay_parts_v5 import ReplayPartVideoWeb
    from services.hub.replay_parts_retention_v5 import ReplayPartsRetentionV5
    need(set(pins)==set(SOURCES) and all(re.fullmatch('[a-f0-9]{64}',v) for v in pins.values()),'EXACT_RUNTIME_PINS_REQUIRED')
    need(all(sha(read(settings.root/n))==v for n,v in pins.items()),'QUALIFIED_RUNTIME_CHANGED')
    need(ReplayPartsRetentionV5(settings,store)._release()[1]==release_sha,'QUALIFIED_RELEASE_CHANGED');quiescent(store)
    service=ReplayPartVideoWeb(settings,store);job,binding,state=service.snapshot(JOB,write=False);pubs=publications(store)
    validate_original(job,state,pubs,binding);service._require_workflow_launch(service._validate_snapshot_binding(job,binding,verify_artifact_hash=False))
    intents=intent_hashes(settings.root);need(set(intents)==KINDS,'NATIVE_SUBMIT_MAY_HAVE_OCCURRED')
    base=service._native_base(job,binding,state)
    for name in KINDS:
        body=json.loads(read(settings.root/'media'/JOB/'replay-native-intents'/name))
        need(body.get('version')=='replay-part-one-use-intent-v5' and body.get('kind')==name[:-5] and
             all(k in body and same(body[k],v) for k,v in base.items()),'ORIGINAL_NATIVE_INTENT_CHANGED')
    folder=settings.root/'media'/JOB/service.NATIVE_DIRECTORY/ATTEMPT
    error_ref,raw=reference(folder/'last-error.json');error=json.loads(raw)
    need(error_ref['sha256']==ERROR_SHA and error=={'action':'submit','error_code':'REPLAY_BROWSER_EDITOR_NOT_UPLOADED',
         'publication_result_unknown':True,'video_selection_attempted_in_session':True},'ORIGINAL_ERROR_CHANGED')
    prep_ref,raw=reference(folder/'native-preparation.json');prep=json.loads(raw)
    need(prep==state['native_preparation'] and prep['private_draft_saved'] is prep['metadata_verified'] is prep['cover_applied'] is True and
         all(k in prep and same(prep[k],v) for k,v in base.items()),'ORIGINAL_PREPARATION_CHANGED')
    upload_ref,raw=reference(folder/'upload-observation.json');upload=json.loads(raw)
    need(upload==state['upload_observation'],'ORIGINAL_UPLOAD_CHANGED')
    coverage_ref,raw=reference(folder/'upload-coverage.json');coverage=json.loads(raw)
    need(coverage_ref==upload['upload_coverage'] and coverage==state['upload_coverage'] and coverage['native_upload_complete'] is True and
         coverage['whole_file_streamed'] is True and coverage['streamed_bytes']==binding['file_bytes'] and
         coverage['streamed_sha256']==binding['artifact_sha256'],'ORIGINAL_UPLOAD_COVERAGE_CHANGED')
    frozen={'job_sha256':digest(job),'publication':pubs[0],'group_sha256':digest(store.get(GROUP)),
            'download_sha256':digest(store.get(DOWNLOAD)),'intents':intents,'binding':base,'error':error_ref,
            'preparation':prep_ref,'upload':upload_ref,'coverage':coverage_ref,'runtime_pins':pins,'release_sha256':release_sha}
    return frozen,(service,job,binding,state)

class Guard:
    def __init__(self,settings,store,job,pub,frozen):
        self.settings,self.store,self.original,self.expected,self.pub,self.frozen=settings,store,deepcopy(job),deepcopy(job),deepcopy(pub),frozen
        self.deadline=time.monotonic()+1200;self.ticket=None
    def __call__(self):
        need(time.monotonic()<self.deadline,'RECOVERY_DEADLINE_EXCEEDED');quiescent(self.store)
        need(self.store.get(JOB)==self.expected and publications(self.store)==[self.pub],'RECOVERY_DATABASE_CHANGED')
        need(digest(self.store.get(GROUP))==self.frozen['group_sha256'] and digest(self.store.get(DOWNLOAD))==self.frozen['download_sha256'],'RECOVERY_SOURCE_OR_GROUP_CHANGED')
        need(all(sha(read(self.settings.root/n))==v for n,v in self.frozen['runtime_pins'].items()),'QUALIFIED_RUNTIME_CHANGED')
        current=intent_hashes(self.settings.root)
        need({k:v for k,v in current.items() if k!='public-submit.json'}==self.frozen['intents'],'ORIGINAL_INTENTS_CHANGED')
        if 'public-submit.json' in current:
            body=json.loads(read(self.settings.root/'media'/JOB/'replay-native-intents/public-submit.json'))
            need(self.ticket is not None and body.get('reservation')==self.ticket and body.get('kind')=='public-submit' and
                 body.get('version')=='replay-part-one-use-intent-v5' and
                 all(k in body and same(body[k],v) for k,v in self.frozen['binding'].items()),'NATIVE_SUBMIT_INTENT_CHANGED')
    def adopt(self):
        new=self.store.get(JOB)
        need(all(same(new[k],v) for k,v in self.original.items() if k not in ('result','state','stage','error_code','updated_at','external_publish_id')),'RECOVERY_IMMUTABLE_JOB_CHANGED')
        need(new['state']=='publish_unknown' and new['external_publish_id'] is None and
             new['result'][STATE]['state'] in ('uploaded','submitting','submit_unknown'),'RECOVERY_CHECKPOINT_CHANGED')
        pubs=publications(self.store);need(len(pubs)==1,'RECOVERY_PUBLICATION_CHANGED')
        need(all(same(pubs[0][k],v) for k,v in self.frozen['publication'].items() if k not in ('state','updated_at')) and
             pubs[0]['state'] in ('unknown','dispatching'),'RECOVERY_PUBLICATION_CHANGED')
        self.expected,self.pub=new,pubs[0];self()

def reused_service(settings,store,guard,operation):
    from services.hub.replay_parts_v5 import ReplayPartVideoWeb
    from services.hub.db import encode
    class Service(ReplayPartVideoWeb):
        def assert_worker_lease(self):guard()
        def _checkpoint_committed(self,job_id):need(job_id==JOB,'RECOVERY_JOB_SCOPE');guard.adopt()
        def _reserve_submit_once(self,principal,job,binding,state,*,site_uid):
            self._operator(principal);guard();need(not self.used and state['state']=='uploaded' and site_uid==UID and
                state['unconsumed_recovery_v5']['operation_id']==operation,'RECOVERY_RESERVATION_ALREADY_USED')
            need(intent_hashes(settings.root)==guard.frozen['intents'],'NATIVE_SUBMIT_MAY_HAVE_OCCURRED');now=time.time()
            with store.transaction() as db:
                need(self._checkpoint_matches(db.execute('SELECT * FROM jobs WHERE job_id=?',(JOB,)).fetchone(),job),'RECOVERY_JOB_CHANGED')
                pubs=[dict(r) for r in self._source_publications(job,binding,db)]
                need(pubs==[guard.pub] and guard.pub['state']=='unknown','RECOVERY_PUBLICATION_CHANGED');self._previous_parts_complete(job,binding,db)
                need(db.execute("UPDATE replay_part_children_v5 SET state='dispatching' WHERE child_job_id=? AND idempotency_key=? AND state='unknown' AND external_id IS NULL",(JOB,KEY)).rowcount==1,'RECOVERY_CHILD_CHANGED')
                need(db.execute("UPDATE publications SET state='dispatching',updated_at=? WHERE idempotency_key=? AND job_id=? AND state='unknown' AND external_id IS NULL AND external_url IS NULL",(now,KEY,JOB)).rowcount==1,'RECOVERY_PUBLICATION_CHANGED')
                db.execute("UPDATE jobs SET state='publish_unknown',stage='replay_part_unconsumed_reservation_reused',error_code='RECONCILE_BEFORE_RETRY',result=?,updated_at=? WHERE job_id=?",
                    (encode({**job['result'],STATE:{**state,'state':'submitting'}}),now,JOB))
                db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',(now,principal,'replay_part_unconsumed_reservation_reused',JOB,
                    encode({'operation_id':operation,'original_idempotency_key':KEY,'attempts_reset':False,'new_upload':False,'first_native_submit_only':True})))
            self.used=True;self._checkpoint_committed(JOB)
            return {'can_click_submit':True,'state':'dispatching','idempotency_key':KEY}
    service=Service(settings,store);service.used=False;return service

def maintenance_reserve(service,principal,guard,native_refresh):
    """V5 reserve_submit gates, ordered QA -> fresh form -> one-use SQL CAS.

    The only contextual difference is the real maintenance lock/guard, rather
    than worker_context. No worker job or lease is invented. Source, launch,
    authorization, machine proof, native form and ticket schemas remain V5.
    """
    from services.hub.short_video_web import _reference
    from services.hub.media_processing import atomic_json
    service._operator(principal);guard();job,binding,state=service.snapshot(JOB,write=False)
    policy=service._validate_snapshot_binding(job,binding,verify_artifact_hash=False)
    service._require_workflow_launch(policy)
    auth=state.get('automation_authorization',{})
    need(service._worker_job is None and callable(native_refresh),'REAL_MAINTENANCE_CONTEXT_REQUIRED')
    need(state['state']=='uploaded' and state.get('public_submission_approved') is False and 'owner_acceptance' not in state and
         auth.get('version')==service.WORKFLOW_AUTHORIZATION_VERSION and auth.get('binding_sha256')==digest(binding) and
         auth.get('workflow_authorization_reference')==policy['workflow_authorization_reference'] and
         auth.get('human_review') is False and auth.get('per_artifact_owner_acceptance') is False and
         all(_reference(auth.get(k)) for k in ('actual_owner_workflow_response','owner_response_reference')) and
         state.get('attempt_id')==ATTEMPT and binding['site_uid']==UID and state['key']==KEY and
         state.get('upload_observation',{}).get('upload_reference_sha256'), 'REPLAY_PART_WORKFLOW_AUTHORIZATION_REQUIRED')
    proof=service._machine_acceptance(job,binding,state,policy)
    guard();response=native_refresh();guard()
    need(response.get('recovered_without_modification') is True,'READONLY_PREPARATION_REQUIRED')
    need(intent_hashes(service.settings.root)==guard.frozen['intents'],'NATIVE_INTENTS_CHANGED_BEFORE_FIRST_SUBMIT')
    prepared=service._native_preparation(job,binding,state,response['native_preparation'],proof)
    service._update(principal,job,{**state,'native_preparation':prepared},job_state=job['state'],stage='replay_part_maintenance_native_refreshed',code='RECONCILE_BEFORE_RETRY')
    job,binding,state=service.snapshot(JOB,write=False);guard()
    result=service._reserve_submit_once(principal,job,binding,{**state,'machine_acceptance':proof},site_uid=UID)
    need(result.get('can_click_submit') is True,'FIRST_NATIVE_SUBMIT_RESERVATION_REQUIRED')
    issued=time.time();ticket={**result,**service._native_base(job,binding,state),'version':service.NATIVE_RESERVATION_VERSION,
        'ticket_id':str(uuid.uuid4()),'issued_at':issued,'expires_at':issued+120,'machine_evidence_sha256':proof['evidence_sha256'],
        'upload_reference_sha256':state['upload_observation']['upload_reference_sha256']}
    file=service.settings.root/'media'/JOB/service.NATIVE_DIRECTORY/ATTEMPT/'reservation.json'
    atomic_json(file,ticket);file.chmod(0o600);return ticket

def validate_ticket(ticket,base,*,now=None):
    now=time.time() if now is None else now
    need(all(k in ticket and same(ticket[k],v) for k,v in base.items()) and
         ticket.get('version')=='replay-part-native-reservation-v5' and ticket.get('can_click_submit') is True and
         ticket.get('state')=='dispatching' and ticket.get('idempotency_key')==KEY and
         str(uuid.UUID(ticket['ticket_id']))==ticket['ticket_id'] and
         all(type(ticket.get(k)) in (float,int) for k in ('issued_at','expires_at')) and
         0<=now-ticket['issued_at']<120 and 0<ticket['expires_at']-now<=120 and
         0<ticket['expires_at']-ticket['issued_at']<=120 and
         re.fullmatch('[a-f0-9]{64}',ticket.get('machine_evidence_sha256','')),
         'RECOVERY_TICKET_EXPIRED_OR_CHANGED')

def error_code(exc):
    code=getattr(exc,'code',None) or (str(exc) if isinstance(exc,ValueError) else '')
    return code if re.fullmatch('[A-Z][A-Z0-9_]{0,100}',code) else 'REPLAY_UNCONSUMED_RECOVERY_STOPPED'

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--runtime-pins',required=True);p.add_argument('--qualified-release-sha256',required=True)
    p.add_argument('--lookup-reference');p.add_argument('--apply',action='store_true');p.add_argument('--plan-file',type=Path);p.add_argument('--plan-sha256')
    a=p.parse_args();need(os.name=='posix' and os.getuid()==os.getgid()==10001 and __debug__,'APP_IDENTITY_REQUIRED');os.umask(0o077)
    need(bool(a.plan_file and a.plan_sha256)==a.apply and bool(a.lookup_reference)!=a.apply,'EXACT_PLAN_OR_LOOKUP_REQUIRED')
    from services.hub.config import Settings
    from services.hub.db import Store
    from services.hub.locks import resource_guard
    from services.hub.short_video_web import PRINCIPAL
    from services.hub.replay_part_publish_worker_v5 import PartReplayBrowser,part_replay_manifest
    settings=Settings.load();need(settings.root==Path('/opt/lighthouse-hub'),'FIXED_ROOT_REQUIRED');store=Store(settings.db_path)
    area=settings.root/'media'/JOB/'unconsumed-reservation-recovery-v5';area.mkdir(mode=0o700,exist_ok=True)
    need(area.resolve()==area and not any(p.is_symlink() for p in (area,*area.parents)),'RECOVERY_PATH_CHANGED')
    with resource_guard(*(settings.root/'data/locks'/n for n in ('video','moderation','part-lookup-schedule-v5'))):
        frozen,context=snapshot(settings,store,json.loads(a.runtime_pins),a.qualified_release_sha256);service,job,binding,state=context
        need(not (area/'dispatch.json').exists(),'RECOVERY_ALREADY_DISPATCHED')
        if not a.apply:
            ref=json.loads(a.lookup_reference)
            # This historical read-only bundle is rechecked live during apply.
            checked_draft_reference(settings.root,ref,frozen['binding'],binding['metadata']['title'],maximum_age=86400)
            plan={'version':VERSION,'job_id':JOB,'draft_id':DRAFT,'draft_url':URL,'snapshot':frozen,'lookup_reference':ref,
                  'created_at':time.time(),'new_uploads':0,'fake_lease':False,'first_native_submit_only':True}
            file=area/('plan-'+str(uuid.uuid4())+'.json');save_once(file,plan)
            print(json.dumps({'state':'plan_only','plan':reference(file)[0]}));return
        need(a.plan_file.parent==area,'PLAN_PATH_CHANGED');raw=read(a.plan_file);need(sha(raw)==a.plan_sha256,'PLAN_HASH_CHANGED');plan=json.loads(raw)
        need(plan['version']==VERSION and plan['job_id']==JOB and plan['draft_id']==DRAFT and plan['draft_url']==URL and plan['snapshot']==frozen and
             type(plan['created_at']) in (float,int) and 0<=time.time()-plan['created_at']<=1800,'PLAN_EXPIRED_OR_CHANGED')
        guard=Guard(settings,store,job,frozen['publication'],frozen);operation=str(uuid.uuid4());service=reused_service(settings,store,guard,operation)
        result_folder=area/operation;result_folder.mkdir(mode=0o700)
        manifest=part_replay_manifest(settings,store,job,binding,state,ATTEMPT,require_cover=True);changed=False;outcome=None;code=None
        original_error=archive_raw(Path(frozen['error']['file']),result_folder/'original-error.json',frozen['error'])
        original_reservation=archive_raw(settings.root/'media'/JOB/service.NATIVE_DIRECTORY/ATTEMPT/'reservation.json',result_folder/'original-reservation.json')
        save_once(result_folder/'original-state.json',{'job':job,'publication':frozen['publication'],'error_reference':original_error,
            'reservation_reference':original_reservation,'native_intents':frozen['intents'],'preparation_reference':frozen['preparation'],
            'preparation':json.loads(read(Path(frozen['preparation']['file'])))})
        phase='fresh_lookup'
        def progress(value):
            print(json.dumps({'version':VERSION,'operation_id':operation,'phase':value,'public_submit_reserved':guard.ticket is not None}),flush=True)
        def handoff(body,ref):
            guard();save_once(result_folder/('handoff-'+body['nonce']+'.json'),{'body':body,'reference':ref,'original_attempt_preserved':True})
            print(json.dumps({'state':'owner_verification_required','reference':ref,'reason':body['reason']}),flush=True)
        try:
            with PartReplayBrowser(settings).session(manifest,assert_lease=guard,owner_handoff=handoff) as session:
                progress(phase)
                observed=session.operation('lookup');evidence=observed.get('native_result_evidence',{})
                need(observed.get('state')=='unknown' and evidence.get('state')=='draft_found' and observed.get('candidate_external_id') is None,'FRESH_NATIVE_DRAFT_REQUIRED')
                fresh=evidence['draft_reference'];checked_draft_reference(settings.root,fresh,frozen['binding'],binding['metadata']['title'])
                need(intent_hashes(settings.root)==frozen['intents'],'NATIVE_INTENTS_CHANGED_BEFORE_FIRST_SUBMIT')
                dispatch={'version':VERSION,'operation_id':operation,'plan':reference(a.plan_file)[0],'fresh_draft':fresh,
                    'previous_job':job,'previous_publication':frozen['publication'],'previous_error':original_error,
                    'previous_reservation':original_reservation,
                    'attempts_reset':False,'new_uploads':0,'fake_lease':False,'first_native_submit_only':True}
                save_once(area/'dispatch.json',dispatch)
                recovery={'version':VERSION,'operation_id':operation,'original_error':original_error['archive'],
                    'original_error_location':original_error['original'],'original_reservation':original_reservation,
                    'fresh_draft':fresh,'attempts_reset':False}
                service._update(PRINCIPAL,job,{**state,'state':'uploaded','unconsumed_recovery_v5':recovery},
                    job_state='publish_unknown',stage='replay_part_preclick_recovery_verified',code='RECONCILE_BEFORE_RETRY');changed=True
                phase='machine_checks';progress(phase)
                def refresh():
                    nonlocal phase
                    phase='native_preparation';progress(phase);return session.operation('prepare')
                ticket=maintenance_reserve(service,PRINCIPAL,guard,refresh)
                need(ticket.get('can_click_submit') is True and ticket.get('idempotency_key')==KEY,'FIRST_NATIVE_SUBMIT_RESERVATION_REQUIRED');guard.ticket=ticket;guard()
                validate_ticket(ticket,frozen['binding'])
                phase='first_native_submit';progress(phase)
                outcome=session.operation('submit',reservation=ticket);guard()
                # Only independent ordinary lookup may establish public success.
                save_once(result_folder/'native-return.json',{'result':outcome,'at':time.time(),'repeat_submit_allowed':False})
                service.mark_unknown(PRINCIPAL,JOB);guard.adopt()
        except BaseException as exc:
            code=error_code(exc)
            if changed:
                try:
                    guard();current=store.get(JOB);checkpoint=current['result'][STATE]
                    if checkpoint['state']=='uploaded':service._update(PRINCIPAL,current,{**checkpoint,'state':'submit_unknown'},job_state='publish_unknown',stage='replay_part_preclick_recovery_stopped',code='PUBLISH_UNKNOWN')
                    elif checkpoint['state']=='submitting':service.mark_unknown(PRINCIPAL,JOB)
                    guard.adopt()
                except Exception:pass
        value={'version':VERSION,'state':'readonly_reconciliation_required' if code is None else 'stopped','error_code':code,
               'operation_id':operation,'phase':phase,'result':outcome,'repeat_submit_allowed':False,'new_uploads':0,'attempts_reset':False,'fake_lease':False,
               'native_submit_intent_present':(settings.root/'media'/JOB/'replay-native-intents/public-submit.json').exists()}
        save_once(result_folder/'result.json',value);print(json.dumps(value),flush=True)
        if code:raise SystemExit(2)

if __name__=='__main__':main()
