"""Owned native-evidence chains and real SQLite CAS; no external requests."""
from copy import deepcopy
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
import importlib.util,json,time,uuid
import pytest
from services.hub.db import Store,encode
from services.hub.short_video_web import PRINCIPAL
from test_replay_parts_v5 import publisher,create,cover,owned_upload,owned_preparation
from test_replay_delivery_v5 import delivery
from test_replay_fifo_v4 import fifo
from test_ytdlp_highest_v2 import tracks

FILE=Path(__file__).resolve().parents[2]/'scripts/ops/resume_unconsumed_replay_reservation_v5.py'
spec=importlib.util.spec_from_file_location('owned_unconsumed_v5',FILE);op=importlib.util.module_from_spec(spec);spec.loader.exec_module(op)

def native(root):
    base={'job_id':op.JOB,'attempt_id':op.ATTEMPT,'site_uid':op.UID,'source_id':op.SOURCE,
          'source_url':'https://www.youtube.com/watch?v='+op.SOURCE,'binding_sha256':'a'*64,'part_index':0,'part_count':1}
    common={**base,'file_selections':0,'save_clicks':0,'submit_clicks':0,'player_seeks':0,'source_or_media_modified':False}
    area=root/'media'/op.JOB/'replay-browser-part-v5'/op.ATTEMPT/'later-lookups-v2'/str(uuid.uuid4());area.mkdir(parents=True)
    def write(name,body):
        p=area/name;p.write_bytes(body if isinstance(body,bytes) else op.wire(body));return op.reference(p)[0]
    title='Owned fixture replay';now=time.time()
    row={'titles':[{'title':title,'links':[{'href':op.URL,'text':title}]}],
         'covers':[{'href':op.URL,'alts':[title]}],'saved_times':['Owned save time']}
    manager={'version':'replay-native-manager-row-v2',**common,'selected_row':None,'known_external_id':None,
             'visible_dom':{'truncated':False,'declared_all_count':1,'rows':[{'row_text_truncated':False,'title_links':[{'text':'Another owned replay'}]}]}}
    drafts={'version':'replay-part-native-draft-manager-v5',**common,'read_only':True,'declared_draft_count':1,
            'visible_dom':{'section_count':1,'truncated':False,'rows':[row],'pagination':['共1页 / 1个'],'selected_pages':['1']},
            'screenshot':write('draft-manager.png',b'Owned image fixture')}
    editor={'version':'replay-part-native-draft-editor-v5',**common,'read_only':True,
            'visible_dom':{'url':op.URL,'title':title,'source_url':base['source_url']},
            'screenshot':write('draft-editor.png',b'Owned image fixture')}
    body={'version':'replay-part-native-draft-observation-v5',**common,'state':'draft_found','draft_id':op.DRAFT,'draft_url':op.URL,
          'read_only':True,'public_accessible':False,'server_file_hash_verified':False,'human_review':False,'publication_authorized':False,
          'saved_at_text':'Owned save time','observed_at':datetime.fromtimestamp(now,timezone.utc).isoformat(),
          'navigation_actions':{'draft_tab_clicks':1,'draft_editor_navigations':1},
          'manager_observation':write('manager-row.json',manager),'draft_manager_observation':write('draft-manager.json',drafts),
          'draft_editor_observation':write('draft-editor.json',editor)}
    return SimpleNamespace(root=root,area=area,base=base,title=title,now=now,body=body,manager=manager,drafts=drafts,editor=editor,
                           write=write,ref=write('draft-observation.json',body))

def repack(f):
    for key,name,value in [('manager_observation','manager-row.json',f.manager),('draft_manager_observation','draft-manager.json',f.drafts),
                           ('draft_editor_observation','draft-editor.json',f.editor)]:f.body[key]=f.write(name,value)
    f.ref=f.write('draft-observation.json',f.body)

def test_exact_loaded_public_inventory_and_unique_saved_draft_accepted(tmp_path):
    f=native(tmp_path);before=deepcopy(f.body)
    assert op.checked_draft_reference(f.root,f.ref,f.base,f.title,now=f.now)==before
    assert f.body==before


def test_actual_native_relative_href_forms_keep_unique_target_in_full_bundle(tmp_path):
    # Exact href forms from the read-only 2026-11-03 native draft manager.
    # Titles and bytes remain owned fixtures; no real authorization is copied.
    f=native(tmp_path);target=f.drafts['visible_dom']['rows'][0]
    target['titles'][0]['links'][0]['href']='/platform/upload/video/frame?type=draft&draftId=4071738'
    target['covers'][0]['href']='/platform/upload/video/frame?type=draft&draftId=4071738'
    for ident in ('4069503','4069103'):
        row=deepcopy(target);title='Another owned draft '+ident
        row['titles'][0]['title']=title;row['titles'][0]['links'][0]={'href':'/platform/upload/video/frame?type=draft&draftId='+ident,'text':title}
        row['covers'][0]={'href':'/platform/upload/video/frame?type=draft&draftId='+ident,'alts':[title]}
        f.drafts['visible_dom']['rows'].append(row)
    f.drafts['declared_draft_count']=3;f.drafts['visible_dom']['pagination']=['共1页 / 3个']
    repack(f);assert op.checked_draft_reference(f.root,f.ref,f.base,f.title,now=f.now)==f.body
    assert op.draft_link(target['covers'][0]['href'])==(op.DRAFT,op.URL)


@pytest.mark.parametrize('value',[
    '//evil.example/platform/upload/video/frame?type=draft&draftId=4071738',
    'https://evil.example/platform/upload/video/frame?type=draft&draftId=4071738',
    'https://member.bilibili.com.evil.example/platform/upload/video/frame?type=draft&draftId=4071738',
    'https://user@member.bilibili.com/platform/upload/video/frame?type=draft&draftId=4071738',
    '/\\evil.example/platform/upload/video/frame?type=draft&draftId=4071738',
    '/platform/upload/video/frame?type=draft&draftId=4071738&draftId=4069503',
    '/platform/upload/video/frame?type=draft&type=draft&draftId=4071738',
    '/platform/upload/video/frame?type=draft&draftId=4071738#fragment',
    '/platform/upload/video/frame?type=draft&draftId=4071738#',
    '/platform/upload/video/frame?type=draft&draftId=4071738\n',
    '/platform/upload/video/frame?type=draft&draftId=4071738&redirect=https://evil.example',
    'platform/upload/video/frame?type=draft&draftId=4071738',
    '/platform/upload/video/frame/../frame?type=draft&draftId=4071738',
    '/platform/upload/video/frame?type=draft&draftId=4071738\x00',
    'http://member.bilibili.com/platform/upload/video/frame?type=draft&draftId=4071738',
    'https://member.bilibili.com:443/platform/upload/video/frame?type=draft&draftId=4071738',
    '/platform%2fupload/video/frame?type=draft&draftId=4071738',
    '/platform/upload/video/frame?type=draft&%64raftId=4071738',
    '/platform/upload/video/frame?type=draft&&draftId=4071738',
])
def test_relative_href_does_not_relax_origin_identity_or_query(value):
    with pytest.raises(ValueError,match='NATIVE_DRAFT_URL_INVALID'):op.draft_link(value)

@pytest.mark.parametrize('change',['other_job','type_bool','writes','stale','future','published','truncated','missing_public_rows',
    'duplicate_draft','draft_missing','wrong_source','wrong_editor','screenshot_bytes','tampered_ref','foreign_ref','row_link_conflict','paged_drafts'])
def test_native_bundle_rejects_uncertainty_and_tampering(tmp_path,change):
    f=native(tmp_path)
    if change=='other_job':f.body['job_id']=str(uuid.uuid4())
    elif change=='type_bool':f.body['file_selections']=False
    elif change=='writes':f.body['save_clicks']=1
    elif change=='stale':f.body['observed_at']=datetime.fromtimestamp(f.now-301,timezone.utc).isoformat()
    elif change=='future':f.body['observed_at']=datetime.fromtimestamp(f.now+31,timezone.utc).isoformat()
    elif change=='published':f.manager['visible_dom']['rows'][0]['title_links'][0]['text']=f.title
    elif change=='truncated':f.manager['visible_dom']['rows'][0]['row_text_truncated']=True
    elif change=='missing_public_rows':f.manager['visible_dom']['declared_all_count']=2
    elif change=='duplicate_draft':
        f.drafts['declared_draft_count']=2;f.drafts['visible_dom']['rows']*=2;f.drafts['visible_dom']['pagination']=['共1页 / 2个']
    elif change=='draft_missing':f.drafts['visible_dom']['rows']=[]
    elif change=='wrong_source':f.editor['visible_dom']['source_url']='https://www.youtube.com/watch?v=another_vid'
    elif change=='wrong_editor':f.editor['visible_dom']['url']=op.URL.replace(op.DRAFT,'999')
    elif change=='row_link_conflict':f.drafts['visible_dom']['rows'][0]['covers'][0]['href']=op.URL.replace(op.DRAFT,'999')
    elif change=='paged_drafts':f.drafts['visible_dom']['pagination']=['共2页 / 1个']
    repack(f)
    if change=='screenshot_bytes':(f.area/'draft-editor.png').write_bytes(b'Modified image')
    elif change=='tampered_ref':f.ref['sha256']='0'*64
    elif change=='foreign_ref':f.ref['file']=str(f.area.parent/'draft-observation.json')
    with pytest.raises((ValueError,KeyError)):op.checked_draft_reference(f.root,f.ref,f.base,f.title,now=f.now)

def test_exclusive_dispatch_cannot_be_overwritten(tmp_path):
    p=tmp_path/'dispatch.json';op.save_once(p,{'operation':'first'})
    with pytest.raises(FileExistsError):op.save_once(p,{'operation':'second'})
    assert json.loads(p.read_bytes())=={'operation':'first'}


def test_exact_error_and_reservation_bytes_survive_mutable_native_overwrite(tmp_path):
    raw=b'{\n  "error_code": "ORIGINAL_NATIVE_FAILURE",\n  "unknown": true\n}\n'
    original=tmp_path/'last-error.json';original.write_bytes(raw)
    frozen=op.reference(original)[0]
    archive=op.archive_raw(original,tmp_path/'original-error.json',frozen)
    original.write_bytes(b'{"error_code":"NEW_NATIVE_FAILURE"}')
    assert op.reference(Path(archive['archive']['file']))[0]==archive['archive']
    assert op.read(Path(archive['archive']['file']))==raw and archive['archive']['sha256']==frozen['sha256']
    assert archive['original']==frozen
    with pytest.raises(FileExistsError):op.archive_raw(original,tmp_path/'original-error.json')
    with pytest.raises(ValueError,match='ORIGINAL_RAW_EVIDENCE_CHANGED'):op.archive_raw(original,tmp_path/'different-archive.json',frozen)

@pytest.fixture
def database(tmp_path,monkeypatch):
    db=Store(tmp_path/'data/hub.sqlite3');db.init();now=time.time()
    binding={'site_uid':op.UID,'parent_job_id':op.GROUP,'download_job_id':op.DOWNLOAD,'part_index':0,'part_count':1,'human_review':False}
    monkeypatch.setattr(op,'BINDING_SHA',op.digest(binding))
    state={'state':'submit_unknown','attempt_id':op.ATTEMPT,'key':op.KEY,'human_review':False,'public_submission_approved':False}
    with db.transaction() as c:
        c.execute("UPDATE pipelines SET paused=1 WHERE name='video'")
        for ident,kind,result,st,attempts in [(op.JOB,'replay_part_publish_v5_p01',{op.STATE:state,'publish_performed':False},'publish_unknown',1),
            (op.GROUP,'replay_parts_publish_parent_v5',{},'upload_pending',0),(op.DOWNLOAD,'youtube_replay_download_v4',{},'completed',1)]:
            c.execute('INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,attempts,policy_version,payload,result) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (ident,'youtube',op.SOURCE,kind,'video',st,now,now,attempts,'replay-part-video-policy-v5',encode(binding),encode(result)))
        c.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',(op.KEY,op.JOB,'bilibili','youtube',op.SOURCE,'site_uid:'+op.UID,
            'video_website_replay_part_v5_p01','unknown',None,None,now))
        c.execute('CREATE TABLE replay_part_children_v5(child_job_id TEXT PRIMARY KEY,idempotency_key TEXT,state TEXT,external_id TEXT)')
        c.execute('INSERT INTO replay_part_children_v5 VALUES(?,?,?,NULL)',(op.JOB,op.KEY,'unknown'))
    job=db.get(op.JOB);pub=op.publications(db)[0]
    folder=tmp_path/'media'/op.JOB/'replay-native-intents';folder.mkdir(parents=True)
    for name in op.KINDS:(folder/name).write_bytes(op.wire({'owned':name}))
    frozen={'publication':pub,'group_sha256':op.digest(db.get(op.GROUP)),'download_sha256':op.digest(db.get(op.DOWNLOAD)),
            'intents':op.intent_hashes(tmp_path),'runtime_pins':{},'binding':{'job_id':op.JOB,'attempt_id':op.ATTEMPT}}
    return SimpleNamespace(db=db,root=tmp_path,job=job,binding=binding,state=state,pub=pub,frozen=frozen,settings=SimpleNamespace(root=tmp_path))

def test_original_state_validation_refuses_already_consumed_attempt(database):
    f=database;op.validate_original(f.job,f.state,[f.pub],f.binding)
    for change in ('attempt','known_bv','pub_state','approved','result','lease'):
        j,s,p=deepcopy(f.job),deepcopy(f.state),deepcopy(f.pub)
        if change=='attempt':j['attempts']=2
        elif change=='known_bv':p['external_id']='BV1234567890'
        elif change=='pub_state':p['state']='dispatching'
        elif change=='approved':s['public_submission_approved']=True
        elif change=='result':s['result_observation']={'state':'awaiting_moderation'}
        else:j['lease_owner']='other'
        with pytest.raises(ValueError):op.validate_original(j,s,[p],f.binding)

@pytest.mark.parametrize('change',['unpaused','active','pub_change','old_intent','submit_without_ticket','group_change','source_change'])
def test_guard_rejects_live_concurrent_or_consumed_state(database,change):
    f=database;g=op.Guard(f.settings,f.db,f.job,f.pub,f.frozen);g()
    with f.db.transaction() as db:
        if change=='unpaused':db.execute("UPDATE pipelines SET paused=0 WHERE name='video'")
        elif change=='active':db.execute("UPDATE jobs SET state='processing' WHERE job_id=?",(op.DOWNLOAD,))
        elif change=='pub_change':db.execute("UPDATE publications SET state='completed'")
        elif change=='group_change':db.execute("UPDATE jobs SET stage='changed' WHERE job_id=?",(op.GROUP,))
        elif change=='source_change':db.execute("UPDATE jobs SET stage='changed' WHERE job_id=?",(op.DOWNLOAD,))
    folder=f.root/'media'/op.JOB/'replay-native-intents'
    if change=='old_intent':(folder/'private-draft-save.json').write_bytes(b'changed')
    elif change=='submit_without_ticket':(folder/'public-submit.json').write_bytes(b'{}')
    with pytest.raises(ValueError):g()

def test_reused_publication_preserves_counts_and_fails_second_reservation(database,monkeypatch):
    f=database;g=op.Guard(f.settings,f.db,f.job,f.pub,f.frozen);service=op.reused_service(f.settings,f.db,g,'owned-operation')
    # Only source graph validation is a seam. SQL, checkpoint and native gates
    # are real; full source/quality validation remains in inherited reserve_submit.
    monkeypatch.setattr(service,'_source_publications',lambda j,b,db:[dict(r) for r in db.execute('SELECT * FROM publications')])
    monkeypatch.setattr(service,'_previous_parts_complete',lambda j,b,db:None)
    uploaded={**f.state,'state':'uploaded','unconsumed_recovery_v5':{'operation_id':'owned-operation'}}
    service._update(PRINCIPAL,f.job,uploaded,job_state='publish_unknown',stage='owned-preclick',code='RECONCILE_BEFORE_RETRY')
    current=f.db.get(op.JOB)
    ticket=service._reserve_submit_once(PRINCIPAL,current,f.binding,uploaded,site_uid=op.UID)
    assert ticket=={'can_click_submit':True,'state':'dispatching','idempotency_key':op.KEY}
    after=f.db.get(op.JOB);pub=op.publications(f.db)[0]
    assert after['attempts']==1 and after['lease_owner'] is after['lease_until'] is None
    assert len(op.publications(f.db))==1 and pub['idempotency_key']==f.pub['idempotency_key']
    assert all(pub[k]==v for k,v in f.pub.items() if k not in ('state','updated_at'))
    assert after['result'][op.STATE]['state']=='submitting' and len(f.db.query('SELECT * FROM api_calls'))==0
    assert op.intent_hashes(f.root)==f.frozen['intents'] and f.db.query('SELECT state FROM replay_part_children_v5')==[{'state':'dispatching'}]
    with pytest.raises(ValueError):service._reserve_submit_once(PRINCIPAL,after,f.binding,after['result'][op.STATE],site_uid=op.UID)

def test_transaction_rolls_back_if_child_was_changed(database,monkeypatch):
    f=database;g=op.Guard(f.settings,f.db,f.job,f.pub,f.frozen);service=op.reused_service(f.settings,f.db,g,'owned-operation')
    monkeypatch.setattr(service,'_source_publications',lambda j,b,db:[dict(r) for r in db.execute('SELECT * FROM publications')])
    monkeypatch.setattr(service,'_previous_parts_complete',lambda j,b,db:None)
    uploaded={**f.state,'state':'uploaded','unconsumed_recovery_v5':{'operation_id':'owned-operation'}}
    service._update(PRINCIPAL,f.job,uploaded,job_state='publish_unknown',stage='owned-preclick')
    with f.db.transaction() as db:db.execute("UPDATE replay_part_children_v5 SET state='completed'")
    before=(f.db.get(op.JOB),op.publications(f.db),f.db.query('SELECT * FROM audit'))
    with pytest.raises(ValueError,match='RECOVERY_CHILD_CHANGED'):service._reserve_submit_once(PRINCIPAL,before[0],f.binding,uploaded,site_uid=op.UID)
    assert (f.db.get(op.JOB),op.publications(f.db),f.db.query('SELECT * FROM audit'))==before


@pytest.fixture
def real_preclick(publisher,monkeypatch):
    """Real FFmpeg tracks, delivery, cover QA, source graph and publication.

    Only native observations use owned fixtures. No signed owner claim is
    fabricated: this is the repository's explicit pilot test policy.
    """
    f=publisher;create(f);attempt=str(uuid.uuid4());s=f.publish
    s.bind_browser_attempt(PRINCIPAL,f.child,attempt_id=attempt);cover(f,attempt)
    s.start_upload(PRINCIPAL,f.child,attempt_id=attempt,site_uid=f.p['target_site_uid'])
    uploaded=owned_upload(f,s,attempt);s.record_upload(PRINCIPAL,f.child,uploaded)
    proof=s.verify_for_publish(f.child);prep=owned_preparation(f,s,proof);s.record_native_prepare(PRINCIPAL,f.child,prep)
    s.reserve_submit(PRINCIPAL,f.child,attempt_id=attempt,site_uid=f.p['target_site_uid'],upload_reference_sha256=uploaded['upload_reference_sha256'])
    s.mark_unknown(PRINCIPAL,f.child)
    with f.db.transaction() as db:
        db.execute('UPDATE jobs SET attempts=1 WHERE job_id=?',(f.child,));db.execute("UPDATE pipelines SET paused=1 WHERE name='video'")
    job,binding,state=s.snapshot(f.child,write=False)
    for name,value in {'JOB':f.child,'GROUP':f.parent,'DOWNLOAD':f.job['job_id'],'ATTEMPT':attempt,
                       'SOURCE':job['source_id'],'UID':binding['site_uid'],'KEY':state['key'],'BINDING_SHA':op.digest(binding)}.items():monkeypatch.setattr(op,name,value)
    folder=f.s.root/'media'/f.child/'replay-native-intents'
    for name in ('cover-completion.json','private-draft-save.json'):(folder/name).write_bytes(op.wire({'owned_synthetic_native_boundary':name}))
    pub=op.publications(f.db)[0]
    frozen={'publication':pub,'group_sha256':op.digest(f.db.get(f.parent)),'download_sha256':op.digest(f.db.get(f.job['job_id'])),
            'intents':op.intent_hashes(f.s.root),'runtime_pins':{},'binding':s._native_base(job,binding,state)}
    guard=op.Guard(f.s,f.db,job,pub,frozen);service=op.reused_service(f.s,f.db,guard,'owned-full-flow')
    service._update(PRINCIPAL,job,{**state,'state':'uploaded','unconsumed_recovery_v5':{'operation_id':'owned-full-flow'}},
                    job_state='publish_unknown',stage='owned-maintenance-start')
    f.recovery=service;f.guard=guard;f.oldpub=pub;f.binding=binding;f.proof=proof;f.oldjob=job;return f


def test_full_real_source_machine_native_and_same_publication_flow(real_preclick):
    f=real_preclick;native_called=[]
    def refresh():
        # The fresh QA file already exists before the native form is observed.
        proofs=list((f.s.root/'media'/f.child/f.recovery.MACHINE_PROOF_DIRECTORY).glob('*/receipt.json'))
        native_called.append(len(proofs))
        return {'recovered_without_modification':True,'native_preparation':owned_preparation(f,f.recovery,f.proof)}
    before_proofs=len(list((f.s.root/'media'/f.child/f.recovery.MACHINE_PROOF_DIRECTORY).glob('*/receipt.json')))
    ticket=op.maintenance_reserve(f.recovery,PRINCIPAL,f.guard,refresh)
    assert native_called==[before_proofs+1]
    op.validate_ticket(ticket,f.guard.frozen['binding'])
    assert ticket['expires_at']-ticket['issued_at']==120 and f.recovery._worker_job is None
    job=f.db.get(f.child);assert job['attempts']==1 and job['lease_owner'] is job['lease_until'] is None
    assert len(op.publications(f.db))==1 and op.publications(f.db)[0]['idempotency_key']==f.oldpub['idempotency_key']
    assert job['result'][op.STATE]['machine_acceptance']['checks']['source']['selected_output']['sha256']==f.binding['artifact_sha256']
    assert job['result'][op.STATE]['native_preparation']['human_review'] is False
    assert op.intent_hashes(f.s.root)==f.guard.frozen['intents']
    # Native boundary returns uncertain: preserve its one-use intent, mark
    # publication/child unknown and prove ordinary lookup is now eligible.
    f.guard.ticket=ticket
    body={'version':'replay-part-one-use-intent-v5',**f.guard.frozen['binding'],'kind':'public-submit',
          'reservation':ticket,'created_at':datetime.now(timezone.utc).isoformat()}
    file=f.s.root/'media'/f.child/'replay-native-intents/public-submit.json';op.save_once(file,body)
    f.guard();f.recovery.mark_unknown(PRINCIPAL,f.child);f.guard.adopt()
    from services.hub.replay_part_moderation_v5 import LookupService,parent_context
    context=parent_context(f.s,f.db,LookupService(f.s,f.db),f.child)
    assert context[0]['state']=='publish_unknown' and context[2]['state']=='submit_unknown' and context[3]['state']=='unknown'
    assert context[4]==op.reference(file)[0]
    assert f.db.query('SELECT state FROM replay_part_children_v5 WHERE child_job_id=?',(f.child,))==[{'state':'unknown'}]


@pytest.mark.parametrize('change',['source_bytes','auth','form_title','form_age','prepare_wrote','native_intent'])
def test_full_machine_workflow_and_native_gates_stop_before_reuse(real_preclick,change):
    from services.hub.errors import HubError
    f=real_preclick
    if change=='source_bytes':
        p=Path(f.binding['file']);raw=p.read_bytes();p.write_bytes(raw[:-1]+bytes([raw[-1]^1]))
    if change=='auth':
        job=f.db.get(f.child);state=deepcopy(job['result'][op.STATE]);state['automation_authorization']['human_review']=True
        f.recovery._update(PRINCIPAL,job,state,job_state='publish_unknown',stage='owned-auth-tamper')
    def refresh():
        p=owned_preparation(f,f.recovery,f.proof)
        if change=='form_title':p['title']='Other owned title'
        elif change=='form_age':p['observed_at']=datetime.fromtimestamp(time.time()-301,timezone.utc).isoformat()
        elif change=='native_intent':(f.s.root/'media'/f.child/'replay-native-intents/public-submit.json').write_bytes(b'{}')
        return {'recovered_without_modification':change!='prepare_wrote','native_preparation':p}
    before=deepcopy(op.publications(f.db))
    with pytest.raises((ValueError,HubError)):op.maintenance_reserve(f.recovery,PRINCIPAL,f.guard,refresh)
    assert op.publications(f.db)==before and f.db.get(f.child)['attempts']==1 and f.recovery.used is False


@pytest.mark.parametrize('change',['expired','future','long_ttl','bool_time','different_key','different_job','no_permission'])
def test_expired_or_changed_ticket_cannot_reach_submit(change):
    base={'job_id':op.JOB,'attempt_id':op.ATTEMPT}
    ticket={**base,'version':'replay-part-native-reservation-v5','can_click_submit':True,'state':'dispatching','idempotency_key':op.KEY,
            'ticket_id':str(uuid.uuid4()),'issued_at':1000.0,'expires_at':1120.0,'machine_evidence_sha256':'a'*64}
    op.validate_ticket(ticket,base,now=1001.0)
    if change=='expired':ticket['expires_at']=1000.0
    elif change=='future':ticket['issued_at']=1002.0
    elif change=='long_ttl':ticket['expires_at']=1121.0
    elif change=='bool_time':ticket['issued_at']=True
    elif change=='different_key':ticket['idempotency_key']='b'*64
    elif change=='different_job':ticket['job_id']=str(uuid.uuid4())
    else:ticket['can_click_submit']=False
    with pytest.raises(ValueError):op.validate_ticket(ticket,base,now=1001.0)
