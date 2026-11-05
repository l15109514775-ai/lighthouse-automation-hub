"""Real owned SQLite/FFmpeg delivery, never a real platform acceptance."""
from copy import deepcopy
import hashlib,json,time,uuid
from pathlib import Path
from types import SimpleNamespace
import pytest

from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.replay_parts_v5 import (ReplayPartVideoWeb,ReplayPartGroupsV5,PartMachineChecks,part_type,part_metadata,
    POLICY,STATE,KIND,GROUP_JOB,GROUP_STATE,verified_delivery)
from services.hub.replay_video_web import WORKER_PRINCIPAL
from services.hub.short_video_web import PRINCIPAL
from services.hub.youtube_download import digest
from test_replay_delivery_v5 import delivery
from test_replay_fifo_v4 import fifo
from test_ytdlp_highest_v2 import tracks
from test_youtube_download_v4 import toml


@pytest.fixture
def publisher(delivery):
    f=delivery;result=f.service.process(f.job);f.db.finish(f.job,'completed',result=result)
    f.s.config['target_bilibili_account_name']='Owned standalone replay publisher'
    f.publish=ReplayPartVideoWeb(f.s,f.db);f.groups=ReplayPartGroupsV5(f.s,f.db)
    p={'version':POLICY,'enabled':True,'target_site_uid':f.s.config['target_bilibili_site_uid'],
        'target_account_name':f.s.config['target_bilibili_account_name'],'public_submission_mode':f.publish.PUBLIC_MODE,
        'source_scope':'pilot_single','acceptance_video_ids':[f.raw['id']],'allowed_channel_ids':f.p['allowed_channel_ids'],
        'source_not_before':f.p['source_not_before'],'activated_at':f.p['activated_at'],'automation_enabled':False,
        'owner_authorization_reference':'Owned synthetic user scope, no live authorization',
        'owner_permission_reference':'Owned synthetic media rights','workflow_authorization_reference':'Owned V5 workflow scope',
        'actual_owner_workflow_response':'Owned test fixture, not an actual user response','owner_response_reference':'Owned fixture conversation'}
    policy=f.s.root/'config'/f.publish.POLICY_FILE;policy.write_text(toml(p),'utf-8');policy.chmod(0o644);f.publish_policy=p
    return f


def create(f):
    f.parent=f.groups.create(f.job['job_id']);f.children=f.db.query('SELECT * FROM replay_part_children_v5 WHERE parent_job_id=? ORDER BY part_index',(f.parent,))
    f.child=f.children[0]['child_job_id'];return f.publish.snapshot(f.child)


def test_true_delivery_n1_original_reference_stable_group_and_distinct_output_publication_keys(publisher):
    f=publisher;job,binding,state=create(f)
    source=f.db.get(f.job['job_id']);d=source['result']['youtube_download_v4']['delivery_v5']
    assert job['source_id']==source['source_id'] and job['job_type']==part_type(0) and job['attempts']==0
    assert binding['file']==source['result']['youtube_download_v4']['source_asset']['file']
    assert binding['source_contract']['output']['part_idempotency_key']!=binding['part_idempotency_key']==state['key']
    assert binding['parts_manifest_sha256']==d['manifest_reference']['sha256']
    assert binding['metadata']['title'].endswith(' P1/1') and '转载许可编号' not in binding['metadata']['description']
    assert binding['metadata']['description']==part_metadata(f.s,source,0,1)['description']
    assert f.groups.create(f.job['job_id'])==f.parent
    assert len(f.db.query('SELECT * FROM jobs WHERE job_type=?',(GROUP_JOB,)))==1 and not f.db.query('SELECT * FROM publications')
    assert state['automation_authorization']['human_review'] is state['public_submission_approved'] is False
    assert isinstance(f.publish.machine_acceptance_verifier,PartMachineChecks)


@pytest.mark.parametrize('change',['ordinal','part_sha','source_contract','metadata','manifest'])
def test_registry_and_source_changes_cannot_become_another_output(publisher,change):
    f=publisher;job,binding,state=create(f)
    if change=='ordinal':
        with f.db.transaction() as db:db.execute('UPDATE jobs SET job_type=? WHERE job_id=?',(part_type(1),f.child))
    elif change=='part_sha':
        with f.db.transaction() as db:db.execute("UPDATE replay_part_children_v5 SET part_sha256=? WHERE child_job_id=?",('a'*64,f.child))
    elif change in ('source_contract','metadata'):
        binding[change]['source_sha256' if change=='source_contract' else 'title']='changed'
        with f.db.transaction() as db:db.execute('UPDATE jobs SET payload=? WHERE job_id=?',(encode(binding),f.child))
    else:
        file=Path(binding['source_contract']['delivery_manifest']['file']);file.write_bytes(file.read_bytes()+b' ')
    with pytest.raises(HubError):f.publish.snapshot(f.child)
    assert not f.db.query('SELECT * FROM publications')


@pytest.mark.parametrize('kind',['publication','tombstone'])
def test_existing_source_prevents_new_group_without_alias_or_timestamp_change(publisher,kind):
    f=publisher;source=f.db.get(f.job['job_id']);before=deepcopy(source)
    with f.db.transaction() as db:
        if kind=='publication':db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',('f'*64,source['job_id'],'bilibili','youtube',source['source_id'],'site_uid:'+f.p['target_site_uid'],'old_kind','unknown',None,None,time.time()))
        else:
            db.execute("INSERT INTO source_tombstones VALUES('youtube',?,'OWNER_BACKLOG_CLEARED',?,?,'owner_clear_all')",
                (source['source_id'],time.time(),source['job_id']))
    with pytest.raises(HubError):f.groups.create(source['job_id'])
    assert f.db.get(source['job_id'])==before


def test_selected_complete_hash_cannot_be_replaced_by_a_passed_boolean(publisher):
    f=publisher;job,binding,state=create(f);file=Path(binding['file']);raw=file.read_bytes();file.write_bytes(raw[:-1]+bytes([raw[-1]^1]))
    with f.db.transaction() as db:
        result=f.db.get(f.child)['result'];result['passed']=True;db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(result),f.child))
    with pytest.raises(HubError):f.publish.snapshot(f.child)
    assert not f.db.query('SELECT * FROM publications')


def cover(f,attempt):
    from services.hub import cover_normalizer_v4 as module
    from test_cover_normalizer_v4 import make_jpeg
    source=f.s.root/'media'/f.child/'short-video-attempts'/attempt/'cover-source-official-maxres.jpg'
    source.parent.mkdir(parents=True);make_jpeg(source)
    return module.normalize_cover(root=f.s.root,job_id=f.child,source_video_id=f.raw['id'],source_file=source,
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),source_url='https://i.ytimg.com/vi/'+f.raw['id']+'/maxresdefault.jpg')


def test_default_qa_reopens_true_source_receipt_hash_probe_packet_chain_and_cover_rgb(publisher):
    f=publisher;job,binding,state=create(f);attempt=str(uuid.uuid4())
    f.publish.bind_browser_attempt(PRINCIPAL,f.child,attempt_id=attempt);cover(f,attempt)
    proof=f.publish.verify_for_publish(f.child)
    assert proof['version']==f.publish.MACHINE_PROOF_VERSION and proof['binding_sha256']==digest(binding)
    assert proof['checks']['cover']['compared_rgb_bytes']==2764800
    source=proof['checks']['source'];assert source['selected_output']['sha256']==binding['artifact_sha256']
    assert source['packet_verification_mode']=='producer_packet_chain_after_current_complete_selected_sha'
    assert source['selected_packet_coverage']['packet_after']==source['source_packet_evidence']
    actual=json.loads(Path(proof['evidence_file']).read_bytes())
    assert actual=={k:v for k,v in proof.items() if k not in ('evidence_file','evidence_bytes','evidence_sha256')}
    assert proof['human_review'] is proof['per_artifact_owner_acceptance'] is proof['publication_authorized'] is False
    assert not f.db.query('SELECT * FROM publications')


def ref(file):
    raw=file.read_bytes();return {'file':str(file),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}


def write(file,value):
    from services.hub.media_processing import atomic_json
    file.parent.mkdir(parents=True,exist_ok=True);atomic_json(file,value);return ref(file)


def owned_upload(f,service,attempt):
    """Identified native boundary only; the source and QA remain real."""
    from datetime import datetime,timezone
    job,binding,state=service.snapshot(f.child,write=False);base=service._native_base(job,binding,state)
    folder=f.s.root/'media'/f.child/service.NATIVE_DIRECTORY/attempt;now=datetime.now(timezone.utc).isoformat()
    intent=write(f.s.root/'media'/f.child/'replay-native-intents/video-selection.json',{'version':'replay-part-one-use-intent-v5',**base,'kind':'video-selection','created_at':now})
    surface={'surface_mode':'single_editor_no_batch_sidebar','actual_outer_count':0,'actual_inner_count':1,'metadata_form_count':1,
        'native_filename':Path(binding['file']).name,'selected_task_title':None}
    coverage=write(folder/'upload-coverage.json',{'version':'replay-part-native-upload-coverage-v5',**base,'file':binding['file'],
        'selected_filename':Path(binding['file']).name,'native_filename':{'text':Path(binding['file']).name,'kind':'file_input','observed':True},
        'native_upload_complete':True,'whole_file_streamed':True,'streamed_bytes':binding['file_bytes'],'streamed_sha256':binding['artifact_sha256'],
        'observed_at':now,'server_file_hash_verified':False,'human_review':False})
    value={'version':'replay-part-browser-upload-v5',**base,'same_attempt_observed':True,'server_file_hash_verified':False,
        'native_surface':surface,'upload_coverage':coverage,'upload_reference_sha256':digest({**base,'local_causal_intent':intent,'native_upload_complete':True,'upload_coverage':coverage})}
    write(folder/'upload-observation.json',value);return value


def owned_preparation(f,service,proof):
    from datetime import datetime,timezone
    from test_cover_normalizer_v4 import make_jpeg
    job,binding,state=service.snapshot(f.child,write=False);folder=f.s.root/'media'/f.child/service.NATIVE_DIRECTORY/state['attempt_id']
    value={'version':service.NATIVE_FORM_VERSION,**service._native_base(job,binding,state),
        **{k:binding['metadata'][k] for k in ('title','description','tags')},'creation_statement':'内容为转载','source':binding['source_url'],'category':'娱乐',
        'cover_sha256':proof['checks']['cover']['output']['sha256'],'cover_provenance_sha256':proof['checks']['cover']['provenance']['sha256'],
        'upload_reference_sha256':state['upload_observation']['upload_reference_sha256'],'native_surface':state['upload_observation']['native_surface'],
        'metadata_verified':True,'cover_applied':True,'server_file_hash_verified':False,'human_review':False,'per_artifact_owner_acceptance':False,
        'observed_at':datetime.now(timezone.utc).isoformat()}
    # This is an owned native screenshot seam, a real PNG from the actual cover.
    screenshot=folder/'pre-submit.png';screenshot.write_bytes(Path(proof['checks']['cover']['output']['file']).read_bytes())
    inner=write(folder/'native-preparation-observation.json',value)
    return {**value,'version':service.NATIVE_PREPARATION_VERSION,'private_draft_saved':True,'pre_submit_screenshot':ref(screenshot),'native_observation_file':inner}


def test_real_worker_lease_upload_native_surface_and_once_reservation_keep_attempt_and_independent_qa(publisher):
    f=publisher;create(f);claimed=f.db.claim('video',120,4);assert claimed['job_id']==f.child
    attempt=str(uuid.uuid4())
    with f.publish.worker_context(claimed):
        f.publish.bind_browser_attempt(WORKER_PRINCIPAL,f.child,attempt_id=attempt);cover(f,attempt)
        f.publish.start_upload(WORKER_PRINCIPAL,f.child,attempt_id=attempt,site_uid=f.p['target_site_uid'])
        observation=owned_upload(f,f.publish,attempt);f.publish.record_upload(WORKER_PRINCIPAL,f.child,observation)
        first=f.publish.verify_for_publish(f.child)
        ticket=f.publish.reserve_submit(WORKER_PRINCIPAL,f.child,attempt_id=attempt,site_uid=f.p['target_site_uid'],
            upload_reference_sha256=observation['upload_reference_sha256'],native_refresh=lambda:owned_preparation(f,f.publish,first))
        assert ticket['can_click_submit'] is True and ticket['expires_at']-ticket['issued_at']<=300
        assert f.db.get(f.child)['state']=='processing' and f.db.get(f.child)['lease_owner']==claimed['lease_owner']
        assert ticket['idempotency_key']==f.children[0]['idempotency_key']
        with pytest.raises(HubError):f.publish.reserve_submit(WORKER_PRINCIPAL,f.child,attempt_id=attempt,site_uid=f.p['target_site_uid'],upload_reference_sha256=observation['upload_reference_sha256'])
        f.publish.mark_unknown(WORKER_PRINCIPAL,f.child)
    assert len(f.db.query('SELECT * FROM publications'))==1 and f.db.get(f.child)['attempts']==1
    assert f.db.query('SELECT state FROM replay_part_children_v5 WHERE child_job_id=?',(f.child,))[0]['state']=='unknown'
    assert len(f.db.query("SELECT * FROM audit WHERE action='replay_part_reserve_submit'"))==1


@pytest.fixture
def reserved(publisher):
    """Actual trusted QA/SQLite up to a synthetic native DOM observation."""
    from datetime import datetime,timezone
    f=publisher;create(f);attempt=str(uuid.uuid4());s=f.publish
    s.bind_browser_attempt(PRINCIPAL,f.child,attempt_id=attempt);cover(f,attempt)
    s.start_upload(PRINCIPAL,f.child,attempt_id=attempt,site_uid=f.p['target_site_uid'])
    uploaded=owned_upload(f,s,attempt);s.record_upload(PRINCIPAL,f.child,uploaded)
    proof=s.verify_for_publish(f.child);prep=owned_preparation(f,s,proof);s.record_native_prepare(PRINCIPAL,f.child,prep)
    f.ticket=s.reserve_submit(PRINCIPAL,f.child,attempt_id=attempt,site_uid=f.p['target_site_uid'],upload_reference_sha256=uploaded['upload_reference_sha256'])
    job,binding,state=s.snapshot(f.child,write=False);f.clicked=datetime.now(timezone.utc)
    f.intent=write(f.s.root/'media'/f.child/'replay-native-intents/public-submit.json',
        {'version':'replay-part-one-use-intent-v5',**s._native_base(job,binding,state),'kind':'public-submit','created_at':f.clicked.isoformat(),'reservation':f.ticket})
    s.mark_unknown(PRINCIPAL,f.child)
    return f


def public_evidence(f,*,alter=None):
    """Owned visible-DOM seam, not a claim about a live Bilibili page."""
    from datetime import datetime,timezone,timedelta
    s=f.publish;job,binding,state=s.snapshot(f.child,write=False);base=s._native_base(job,binding,state)
    bv='BV1owned0001';now=datetime.now(timezone.utc).isoformat();url='https://www.bilibili.com/video/'+bv
    folder=f.s.root/'media'/f.child/s.NATIVE_DIRECTORY/state['attempt_id']/'later-lookups-v2'/str(uuid.uuid4())
    common={**base,'observed_at':now,'file_selections':0,'save_clicks':0,'submit_clicks':0,'player_seeks':0,'source_or_media_modified':False}
    duration=binding['duration_seconds'];seconds=round(duration)
    row={'class':'article-card clearfix v2','title_links':[{'tag':'A','class':'name ellipsis','text':binding['metadata']['title'],'href':url+'/','visible':True}],
        'covers':[{'tag':'A','class':'cover-wrp','text':'','href':url,'visible':True}],
        'dates':[{'text':f.clicked.astimezone(timezone(timedelta(hours=8))).strftime('%Y年%m月%d日 %H:%M:%S'),'class':'date','parent_class':'pubdate is-success'}],
        'durations':[f'{seconds//3600:02d}:{seconds//60%60:02d}:{seconds%60:02d}']}
    manager={'version':'replay-native-manager-row-v2',**common,'visible_dom':{'url':'https://member.bilibili.com/platform/upload-manager/article',
        'visible_text':'视频管理\n图文管理\n互动视频管理\n全部稿件 9\n','truncated':False,'rows':[row]},'selected_row':row,'known_external_id':None}
    rawdesc=binding['source_url']+'\n'+binding['metadata']['description']
    public={'version':'replay-part-public-player-observation-v5',**common,'player_seeks':1,'external_id':bv,'expected_title':binding['metadata']['title'],
        'visible_dom':{'url':url+'/','titles':[{'text':binding['metadata']['title'],'title':None,'class':'video-title'}],
            'owner_candidates':[{'text':binding['account_name'],'url':'https://space.bilibili.com/'+binding['site_uid'],'class':'up-name','visible':True,'card_text':binding['account_name']}],
            'descriptions':[{'class':'basic-desc-info','text':rawdesc,'visible':True}],
            'videos':[{'visible':True,'duration_seconds':duration,'current_time':duration-.01,'ready_state':4,'paused':False,
                'video_width':1920,'video_height':1080,'error_code':None,'seekable_ranges':[{'start':0,'end':duration}], 'seeking':False,'ended':False}]},
        'near_end_playback':{'seek_performed':True,'seeking':False,'before':.01,'after':duration-.01,'ended':False}}
    if alter:alter(manager,public)
    managerref=write(folder/'manager-row.json',manager);publicref=write(folder/'public-native-inventory.json',public)
    image=folder/'public-page.png';image.write_bytes(Path(state['native_preparation']['pre_submit_screenshot']['file']).read_bytes())
    ev=write(folder/'reconciliation.json',{'version':'replay-later-native-result-observation-v2',**common,'external_id':bv,
        'manager_observation':managerref,'public_observation':publicref,'public_screenshot':ref(image)})
    return {'version':s.RESULT_OBSERVATION_VERSION,**base,'external_id':bv,'url':url,'state':'completed','public_accessible':True,
        'public_lookup_observed_at':now,'native_result_evidence':ev,'origin':'later_native_lookup_reconciled_v5',
        'same_submit_attempt_observed':False,'human_review':False,'server_file_hash_verified':False}


def test_later_public_result_keeps_raw_description_and_completes_whole_group_without_same_click_claim(reserved):
    f=reserved;o=public_evidence(f);r=f.publish.record_result(PRINCIPAL,f.child,o,lookup=True)
    current=f.db.get(f.child);state=current['result'][STATE]
    assert r['parent_state']=='completed' and current['state']=='completed'
    assert state['result_observation']['same_submit_attempt_observed'] is False
    assert state['public_coverage']['observed_description'].startswith(current['payload']['source_url']+'\n')
    assert state['public_coverage']['normalized_description']==current['payload']['metadata']['description']
    assert f.db.get(f.parent)['result'][GROUP_STATE]['completed_parts']==1
    assert f.db.get(f.parent)['external_publish_id'] is None
    assert current['attempts']==0 and len(f.db.query('SELECT * FROM publications'))==1


@pytest.mark.parametrize('changed',['owner','extra_prefix','wrong_part_title','duration','ready_bool','no_progress','manager_old','truncated','false_human'])
def test_wrong_public_binding_or_boolean_claim_never_clears_unknown(reserved,changed):
    f=reserved
    def alter(manager,public):
        dom=public['visible_dom']
        if changed=='owner':dom['owner_candidates'][0]['url']='https://space.bilibili.com/1'
        elif changed=='extra_prefix':dom['descriptions'][0]['text']=f.db.get(f.child)['payload']['source_url']+'\n'+dom['descriptions'][0]['text']
        elif changed=='wrong_part_title':dom['titles'][0]['text']='Same source but P2/2'
        elif changed=='duration':dom['videos'][0]['duration_seconds']+=10
        elif changed=='ready_bool':dom['videos'][0]['ready_state']=True
        elif changed=='no_progress':public['near_end_playback']['before']=public['near_end_playback']['after']
        elif changed=='manager_old':manager['selected_row']['dates'][0]['text']='2026年01月01日 00:00:00'
        elif changed=='truncated':manager['visible_dom']['truncated']=True
    o=public_evidence(f,alter=alter)
    if changed=='false_human':o['human_review']=True
    with pytest.raises(HubError):f.publish.record_result(PRINCIPAL,f.child,o,lookup=True)
    assert f.db.get(f.child)['result'][STATE]['state']=='submit_unknown'
    assert f.db.query('SELECT state,external_id FROM publications')[0]=={'state':'unknown','external_id':None}


def test_historical_unknown_lookup_uses_original_proofs_after_media_and_current_send_policy_removed(reserved):
    f=reserved;job,binding,state=f.publish.snapshot(f.child,write=False);Path(binding['file']).unlink()
    (f.s.root/'config'/f.publish.POLICY_FILE).unlink()
    from services.hub.replay_part_publish_worker_v5 import part_replay_manifest
    spec=part_replay_manifest(f.s,f.db,job,binding,state,state['attempt_id'],require_cover=False)
    assert spec['file']==binding['file'] and spec['cover'] is None
    result=f.publish.record_result(PRINCIPAL,f.child,public_evidence(f),lookup=True)
    assert result['checkpoint']=='completed' and not Path(binding['file']).exists()


def test_later_refresh_preserves_immutable_first_completion_launch_evidence(reserved,monkeypatch):
    f=reserved;f.publish.record_result(PRINCIPAL,f.child,public_evidence(f),lookup=True)
    first=f.db.get(f.parent)['result']['replay_parts_first_completion_v5'];file=Path(first['file']);saved=(file.read_bytes(),file.stat().st_mtime_ns)
    f.publish.record_result(PRINCIPAL,f.child,public_evidence(f),lookup=True)
    parent=f.db.get(f.parent);snapshot=json.loads(file.read_bytes())
    assert parent['result']['replay_parts_first_completion_v5']==first and (file.read_bytes(),file.stat().st_mtime_ns)==saved
    assert digest(parent['result'][GROUP_STATE])!=digest(snapshot['summary'])
    # Only the root filesystem ownership boundary is an explicit owned seam.
    # The actual completed group, historical refs and per-child publication
    # checks remain the production path; this is no real launch permission.
    normal=deepcopy(f.publish_policy);normal.update(source_scope='all_eligible_ended_since_operating_cutoff',acceptance_video_ids=[],automation_enabled=True)
    path=f.s.root/'config'/f.publish.POLICY_FILE;path.write_text(toml(normal),'utf-8')
    p=f.publish.policy();grant={'version':'replay-parts-workflow-launch-v5','policy_sha256':p['_sha256'],
        'target_site_uid':p['target_site_uid'],'workflow_authorization_reference':p['workflow_authorization_reference'],
        'actual_owner_workflow_response':'Owned synthetic workflow only','owner_response_reference':'Owned reference',
        'accepted_at':f.clicked.isoformat(),'acceptance_parent_job_id':f.parent,'completed_group_result_sha256':digest(snapshot['summary'])}
    monkeypatch.setattr(ReplayPartVideoWeb,'_root_readonly_file',lambda self,file,**kw:encode(grant).encode())
    assert f.groups.require_launch(p)==grant


def test_crash_after_child_result_cas_recovers_group_without_new_dispatch(reserved,monkeypatch):
    f=reserved;real=ReplayPartGroupsV5.aggregate;calls=[0]
    def interrupted(self,parent):
        calls[0]+=1
        if calls[0]==1:raise HubError('OWNED_COMPLETION_WRITE_INTERRUPTED',review=True)
        return real(self,parent)
    monkeypatch.setattr(ReplayPartGroupsV5,'aggregate',interrupted)
    with pytest.raises(HubError):f.publish.record_result(PRINCIPAL,f.child,public_evidence(f),lookup=True)
    assert f.db.get(f.child)['state']=='completed' and f.db.get(f.parent)['state']=='upload_pending'
    pubs=f.db.query('SELECT * FROM publications')
    assert f.groups.sync_completed_groups()['completed']==[f.parent]
    assert f.db.get(f.parent)['state']=='completed' and f.db.query('SELECT * FROM publications')==pubs


def test_interrupted_completion_publish_never_exposes_half_json_and_reconciles_without_send(reserved,monkeypatch):
    import services.hub.replay_delivery_v5 as delivery_module
    f=reserved;link=delivery_module.os.link;calls=[0]
    def interrupted(source,destination):
        calls[0]+=1
        if calls[0]==1:raise OSError('Owned final-link interruption')
        return link(source,destination)
    monkeypatch.setattr(delivery_module.os,'link',interrupted)
    with pytest.raises(OSError):f.publish.record_result(PRINCIPAL,f.child,public_evidence(f),lookup=True)
    file=f.s.root/'media'/f.parent/'replay-parts-v5/first-completion.json'
    assert not file.exists() and f.db.get(f.child)['state']=='completed'
    rows=f.db.query('SELECT * FROM publications')
    assert f.groups.sync_completed_groups()['completed']==[f.parent]
    assert json.loads(file.read_bytes())['parent_job_id']==f.parent and f.db.query('SELECT * FROM publications')==rows


@pytest.mark.parametrize('field,value',[('actual_outer_count',2),('actual_inner_count',2),('metadata_form_count',True)])
def test_batch_or_ambiguous_native_surface_cannot_be_counted_as_single_submission(field,value):
    from services.hub.replay_parts_v5 import native_surface
    binding={'file':str(Path.cwd()/'owned.mp4'),'metadata':{'title':'Owned ordinary single file'}}
    surface={'surface_mode':'single_editor_no_batch_sidebar','actual_outer_count':0,'actual_inner_count':1,'metadata_form_count':1,
        'native_filename':'owned.mp4','selected_task_title':None};surface[field]=value
    with pytest.raises(HubError):native_surface(binding,surface)
