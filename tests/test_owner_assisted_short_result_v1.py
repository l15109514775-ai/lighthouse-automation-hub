"""Owned domain reservations and screenshot-shaped fixtures; no real account."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

import pytest

from test_short_video_web import short_video, PRINCIPAL, STATE
from test_short_video_permission_line_removal import prepared, remove
from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.owner_assisted_short_result_v1 import record_owner_assisted_result_v1 as record
from services.hub.owner_assisted_short_result_v1 import record_owner_authorized_remote_deletion_v1 as deletion
from services.hub.youtube_download import digest

BV='BV0000000000'
PNG=b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'+(100).to_bytes(4,'big')+(100).to_bytes(4,'big')+bytes(9)
def sha(file):return hashlib.sha256(file.read_bytes()).hexdigest()
def ref(file):return {'file':str(file),'bytes':file.stat().st_size,'sha256':sha(file)}
def save(file,value):file.write_text(encode(value),'utf-8');return sha(file)


@pytest.fixture
def assisted(short_video):
    s,db,service,*_=short_video;job,attempt,_,_=prepared(short_video)
    remove(service,job,db.get(job)['payload']['metadata_sha256'])
    current,binding,state=service.snapshot(job)
    service.approve(PRINCIPAL,job,binding_sha256=digest(binding),actual_owner_response='Owned explicit one-video publication request',approval_reference='Owned only; not a real person')
    assert service.reserve_submit(PRINCIPAL,job,attempt_id=attempt,site_uid=binding['site_uid'],upload_reference_sha256='b'*64)['can_click_submit'] is True
    service.mark_unknown(PRINCIPAL,job);state=db.get(job)['result'][STATE]
    recovery=str(uuid.uuid4());root=s.root/'media'/job;output=root/'recovered-editors'/recovery;output.mkdir(parents=True)
    g={'job_id':job,'attempt_id':attempt,'recovery_id':recovery,'target_site_uid':binding['site_uid'],
        **{k:binding[k] for k in ('source_url','source_id','download_job_id','file','file_bytes','artifact_sha256','metadata_sha256')},
        'upload_metadata_sha256':state['metadata_revision_history'][0]['previous_metadata_sha256'],
        'binding_sha256':digest(binding),'metadata_revision_id':state['active_metadata_revision_id'],'upload_reference_sha256':'b'*64}
    gate={'version':'recovered-short-video-submit-intent-v1','state':'submit_reserved_result_unknown','binding':g,
        'ticket_id':str(uuid.uuid4()),'command_id':str(uuid.uuid4()),'approval_sha256':'c'*64,'reservation_sha256':'d'*64,
        'created_at':datetime.now(timezone.utc).isoformat(),'reconcile_before_retry':True,'native_click_performed':'unknown',
        'new_file_selection':False,'human_review':False}
    save(root/'recovered-submit-intent.json',gate)
    cover=root/'cover-assets-v4';cover.mkdir();(cover/'top-bottom-blur-cover.png').write_bytes(PNG);save(cover/'provenance.json',{'owned_only':True})
    click={'version':'short-native-submit-controller-v1','job_id':job,'attempt_id':attempt,'recovery_id':recovery,
        'metadata_sha256':binding['metadata_sha256'],'ticket_id':gate['ticket_id'],'command_id':gate['command_id'],
        'state':'native_click_reserved_result_unknown','title':binding['metadata']['title'],'description':binding['metadata']['description'],
        'source':binding['source_url'],'reconcile_before_retry':True,'new_video_file_selection':False,
        'created_at':datetime.now(timezone.utc).isoformat(),'cover':{'cover_sha256':sha(cover/'top-bottom-blur-cover.png'),
            'cover_provenance_sha256':sha(cover/'provenance.json'),'server_image_hash_verified':False}}
    save(output/'native-short-submit-click-intent.json',click)
    folder=root/'owner-assisted-v1'/str(uuid.uuid4());folder.mkdir(parents=True)
    for name in ('native.png','public.png','deletion-native.png','deletion-public.png'):(folder/name).write_bytes(PNG)
    native={'version':'short-owner-assisted-native-observation-v1','job_id':job,'site_uid':binding['site_uid'],
        'source_url':binding['source_url'],'metadata_sha256':binding['metadata_sha256'],'title':binding['metadata']['title'],
        'observed_description':binding['source_url']+'\n'+binding['metadata']['description'],
        'normalized_description':binding['metadata']['description'],'description_normalization':{'version':'bilibili-public-description-source-prefix-v1',
            'removed_prefix':binding['source_url']+'\n','occurrences_removed':1},'external_id':BV,'url':'https://www.bilibili.com/video/'+BV,
        'state':'completed','observed_at':datetime.now(timezone.utc).isoformat(),'screenshot':ref(folder/'native.png')}
    public={'version':'short-owner-assisted-public-observation-v1','job_id':job,'publisher_uid':binding['site_uid'],
        'source_url':binding['source_url'],'title':binding['metadata']['title'],'observed_description':binding['source_url']+'\n'+binding['metadata']['description'],
        'normalized_description':binding['metadata']['description'],'description_normalization':{'version':'bilibili-public-description-source-prefix-v1',
            'removed_prefix':binding['source_url']+'\n','occurrences_removed':1},'external_id':BV,'url':native['url'],
        'public_accessible':True,'observed_at':datetime.now(timezone.utc).isoformat(),'screenshot':ref(folder/'public.png')}
    args={'binding_sha256':digest(binding),'actual_auth_response':'验证成功了','auth_response_reference':'Owned authentication-only statement',
        'actual_operator_reference':'Owned native/public observation fixture','native_observation_file':str(folder/'native.json'),
        'native_observation_sha256':save(folder/'native.json',native),'public_observation_file':str(folder/'public.json'),
        'public_observation_sha256':save(folder/'public.json',public)}
    return s,db,service,job,binding,folder,args,native,public


def test_assistance_records_independent_origin_raw_five_lines_and_exact_one_prefix(assisted):
    s,db,service,job,binding,folder,args,native,public=assisted
    before=db.get(job);owner=deepcopy(before['result'][STATE]['owner_acceptance']);artifact=Path(binding['file']).read_bytes()
    answer=record(service,PRINCIPAL,job,**args);after=db.get(job);state=after['result'][STATE]
    assert answer['changed'] is True and after['state']=='completed' and after['external_publish_id']==BV
    assert state['owner_acceptance']==owner and state['human_review'] is False
    assert state['result_observation']['version']=='short-owner-assisted-result-v1'
    assert state['result_observation']['same_submit_attempt_observed'] is False
    assert 'attempt_id' not in state['result_observation']
    evidence=state['owner_assisted_result_v1'];assert evidence['human_review'] is evidence['same_submit_attempt_observed'] is False
    assert evidence['authentication_assistance']['meaning']=='authentication_assistance_only'
    assert evidence['authentication_assistance']['publication_authorization_inferred'] is False
    assert evidence['observed_public_description']==public['observed_description']
    assert evidence['normalized_public_description']==binding['metadata']['description']
    assert evidence['observed_native_description']==native['observed_description']
    assert evidence['normalized_native_description']==binding['metadata']['description']
    assert json.loads((folder/'public.json').read_bytes())['observed_description']==public['observed_description']
    assert db.query('SELECT state,external_id FROM publications')==[{'state':'completed','external_id':BV}]
    assert Path(binding['file']).read_bytes()==artifact
    count=len(db.query('SELECT * FROM audit'));assert record(service,PRINCIPAL,job,**args)['changed'] is False
    assert len(db.query('SELECT * FROM audit'))==count


@pytest.mark.parametrize('field',['native_uid','public_uid','source','metadata','bv','public_unavailable','double_prefix','native_double_prefix','no_prefix','extra_suffix','bad_screenshot','missing_gate','missing_click','no_reservation_audit','auth_is_not_approval'])
def test_unbound_missing_or_unverifiable_observations_do_not_resolve_unknown(assisted,field):
    s,db,service,job,binding,folder,args,native,public=assisted
    if field=='native_uid':native['site_uid']='other'
    elif field=='public_uid':public['publisher_uid']='other'
    elif field=='source':public['source_url']='https://www.youtube.com/watch?v=xxxxxxxxxxx'
    elif field=='metadata':native['metadata_sha256']='0'*64
    elif field=='bv':public['external_id']='BV1111111111'
    elif field=='public_unavailable':public['public_accessible']=False
    elif field=='double_prefix':public['observed_description']=binding['source_url']+'\n'+public['observed_description']
    elif field=='native_double_prefix':native['observed_description']=binding['source_url']+'\n'+native['observed_description']
    elif field=='no_prefix':public['observed_description']=binding['metadata']['description']
    elif field=='extra_suffix':public['observed_description']+='\nOwned unrelated tail'
    elif field=='bad_screenshot':(folder/'public.png').write_bytes(b'Owned invalid image')
    elif field=='missing_gate':(s.root/'media'/job/'recovered-submit-intent.json').unlink()
    elif field=='missing_click':
        g=json.loads((s.root/'media'/job/'recovered-submit-intent.json').read_bytes());(s.root/'media'/job/'recovered-editors'/g['binding']['recovery_id']/'native-short-submit-click-intent.json').unlink()
    elif field=='no_reservation_audit':
        with db.transaction() as connection:connection.execute("DELETE FROM audit WHERE action='short_video_reserve_submit'")
    else:
        state=db.get(job)['result'];state[STATE]['public_submission_approved']=False;state[STATE].pop('owner_acceptance')
        with db.transaction() as connection:connection.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(state),job))
    args['native_observation_sha256']=save(folder/'native.json',native);args['public_observation_sha256']=save(folder/'public.json',public)
    before=db.get(job);pub=db.query('SELECT * FROM publications')
    with pytest.raises(HubError,match='OWNER_ASSISTED_SHORT_EVIDENCE_REQUIRED'):record(service,PRINCIPAL,job,**args)
    assert db.get(job)==before and db.query('SELECT * FROM publications')==pub


def deletion_args(f):
    _,_,_,job,binding,folder,*_=f
    observed={'version':'short-owner-authorized-remote-deletion-observation-v1','job_id':job,'site_uid':binding['site_uid'],
        'source_url':binding['source_url'],'external_id':BV,'url':'https://www.bilibili.com/video/'+BV,
        'native_deletion_confirmed':True,'public_accessible':False,'observed_at':datetime.now(timezone.utc).isoformat(),
        'native_screenshot':ref(folder/'deletion-native.png'),'public_screenshot':ref(folder/'deletion-public.png')}
    args={'external_id':BV,'actual_owner_delete_response':'Owned request: delete this remote test after checking',
        'owner_response_reference':'Owned single-BV remote deletion request','actual_operator_reference':'Owned actual native/public deletion observation',
        'deletion_observation_file':str(folder/'deletion.json'),'deletion_observation_sha256':save(folder/'deletion.json',observed)}
    return args,observed


def test_deletion_tombstone_keeps_publication_raw_evidence_local_media_and_existing_hold(assisted):
    s,db,service,job,binding,folder,args,*_=assisted;record(service,PRINCIPAL,job,**args)
    before=db.get(job);publications=db.query('SELECT * FROM publications');artifact=Path(binding['file']).read_bytes()
    arguments,_=deletion_args(assisted);answer=deletion(service,PRINCIPAL,job,**arguments);after=db.get(job)
    assert answer['remote_state']=='owner_deleted' and answer['local_media_deleted'] is False and answer['publication_record_removed'] is False
    tombstone=after['result'][STATE]['owner_authorized_remote_deletion_v1']
    assert tombstone['browser_deletion_executed_by_this_method'] is False and tombstone['local_media_deletion_authorized'] is False
    assert after['state']==before['state']=='completed' and after['external_publish_id']==BV
    assert after['result'][STATE]['owner_assisted_result_v1']==before['result'][STATE]['owner_assisted_result_v1']
    assert after['result'][STATE]['test_retention_policy']==before['result'][STATE]['test_retention_policy']
    assert db.query('SELECT * FROM publications')==publications and Path(binding['file']).read_bytes()==artifact
    assert service.cleanup_eligibility(job)['eligible'] is False
    assert deletion(service,PRINCIPAL,job,**arguments)['changed'] is False


@pytest.mark.parametrize('change',['wrong_bv','not_deleted','still_public','no_request','changed_screenshot','unresolved_job'])
def test_remote_deletion_requires_bound_result_explicit_request_and_actual_evidence(assisted,change):
    s,db,service,job,binding,folder,args,*_=assisted
    if change!='unresolved_job':record(service,PRINCIPAL,job,**args)
    arguments,observed=deletion_args(assisted)
    if change=='wrong_bv':arguments['external_id']='BV1111111111'
    elif change=='not_deleted':observed['native_deletion_confirmed']=False
    elif change=='still_public':observed['public_accessible']=True
    elif change=='no_request':arguments['actual_owner_delete_response']=''
    elif change=='changed_screenshot':(folder/'deletion-native.png').write_bytes(b'Owned changed PNG')
    arguments['deletion_observation_sha256']=save(folder/'deletion.json',observed)
    before=db.get(job);publications=db.query('SELECT * FROM publications')
    with pytest.raises(HubError):deletion(service,PRINCIPAL,job,**arguments)
    assert db.get(job)==before and db.query('SELECT * FROM publications')==publications
