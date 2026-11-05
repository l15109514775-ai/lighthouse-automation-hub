"""Owned SQLite/file fixtures only; never edit a native browser or publish."""
from copy import deepcopy
from pathlib import Path
import uuid
import pytest
from test_short_video_web import short_video, category_amendment_uploaded, amend_category, PRINCIPAL, STATE, KIND, BV
from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.youtube_download import digest

LINE='转载许可编号：114514（字幕组提供的许可范围）。'


def prepared(f,*,suffix='\n'+LINE):
    s,db,service,_,metadata,_=f;metadata['description']+=suffix
    job,attempt,original=category_amendment_uploaded(f,second_attempt=True)
    folder=s.root/'media'/job/'short-video-attempts'/attempt;folder.mkdir(parents=True)
    evidence={'metadata.json':encode(original['metadata']),'spec.json':encode({'metadata_sha256':original['metadata_sha256']}),
        'upload-observation.json':encode(db.get(job)['result'][STATE]['upload_observation']),'private-save-observation.json':encode({'owned_fixture':True,'state':'saved'})}
    for name,value in evidence.items():(folder/name).write_text(value,'utf-8')
    amend_category(service,job,original['metadata_sha256'])
    service.set_test_retention_policy(PRINCIPAL,job,actual_owner_response='Owned fixture: preserve remote post and local tests until formal launch',reference='Owned retention reference')
    return job,attempt,folder,{name:(folder/name).read_bytes() for name in evidence}


def remove(service,job,metadata_hash,**changes):
    values={'expected_metadata_sha256':metadata_hash,'actual_owner_response':'转载许可编号删掉',
        'owner_response_reference':'Owned explicit public-description request','actual_operator_reference':'Owned observed exact last public line'}
    values.update(changes);return service.operator_remove_public_permission_line_v1(PRINCIPAL,job,**values)


def test_second_revision_removes_only_exact_last_public_line_preserving_all_evidence_and_retention(short_video):
    s,db,service,_,_,_=short_video;job,attempt,folder,evidence=prepared(short_video)
    rights=s.root/'config/rights.toml';rights.write_bytes(b'Owned internal rights fixture must remain unchanged')
    config={p:p.read_bytes() for p in (s.root/'config').iterdir() if p.is_file()}
    with db.transaction() as conn:conn.execute('UPDATE jobs SET attempts=3 WHERE job_id=?',(job,))
    before=db.get(job);binding=deepcopy(before['payload']);state=deepcopy(before['result'][STATE]);old_history_wire=encode(state['metadata_revision_history'][0]).encode()
    answer=remove(service,job,binding['metadata_sha256']);current,new,after=service.snapshot(job)
    expected={**binding['metadata'],'description':binding['metadata']['description'][:-len('\n'+LINE)]}
    assert new=={**binding,'metadata':expected,'metadata_sha256':digest(expected)}
    assert current['state']==before['state']=='upload_pending' and current['attempts']==before['attempts']==3
    assert current['external_publish_id']==before['external_publish_id'] is None
    assert len(after['metadata_revision_history'])==2 and encode(after['metadata_revision_history'][0]).encode()==old_history_wire
    assert {k:v for k,v in after.items() if k not in ('metadata_revision_history','active_metadata_revision_id','remote_metadata_synchronized')}=={
        k:v for k,v in state.items() if k not in ('metadata_revision_history','active_metadata_revision_id','remote_metadata_synchronized')}
    second=after['metadata_revision_history'][1]
    assert second['version']=='public-permission-line-removal-v1' and str(uuid.UUID(second['revision_id']))==second['revision_id']
    assert second['previous_revision_id']==state['metadata_revision_history'][0]['revision_id']
    assert second['previous_binding']==binding and second['previous_binding_sha256']==digest(binding)
    assert second['previous_metadata_sha256']==binding['metadata_sha256'] and second['new_metadata_sha256']==new['metadata_sha256']
    assert second['new_binding_sha256']==digest(new) and second['changed_fields']==['description'] and second['removed_last_line']==LINE
    assert second['attempt_id']==attempt and second['actual_owner_response']=='转载许可编号删掉'
    assert second['public_submit'] is second['remote_metadata_synchronized'] is False and second['internal_rights_records_preserved'] is True
    assert answer['metadata_revision_id']==after['active_metadata_revision_id']==second['revision_id']
    assert answer['public_submission_approved'] is answer['public_submit'] is False and answer['private_upload_selections']==2
    assert current['stage']=='short_video_metadata_revision_pending_native_sync'
    assert all(path.read_bytes()==value for path,value in config.items())
    assert all((folder/name).read_bytes()==value for name,value in evidence.items())
    assert not db.query('SELECT * FROM publications') and not db.query('SELECT * FROM publication_reviews')
    audit=db.query("SELECT outcome FROM audit WHERE target=? AND action='short_video_remove_public_permission_line_v1'",(job,))
    assert len(audit)==1 and audit[0]['outcome']=='public_description_footer_removed_internal_rights_preserved_no_public_authorization'
    assert service.cleanup_eligibility(job)['eligible'] is False
    with pytest.raises(HubError):service.reserve_submit(PRINCIPAL,job,attempt_id=attempt,site_uid=new['site_uid'],upload_reference_sha256='b'*64)
    with pytest.raises(HubError):service.approve(PRINCIPAL,job,binding_sha256=digest(binding),actual_owner_response='Owned stale binding',approval_reference='Owned stale approval')
    before_repeat=db.get(job)
    with pytest.raises(HubError):remove(service,job,new['metadata_sha256'])
    assert db.get(job)==before_repeat


@pytest.mark.parametrize('arguments',[{'expected_metadata_sha256':'0'*64},{'expected_metadata_sha256':'invalid'},
    {'actual_owner_response':''},{'owner_response_reference':' '},{'actual_operator_reference':''}])
def test_exact_current_hash_and_all_owner_operator_references_are_required(short_video,arguments):
    _,db,service,*_=short_video;job,*_=prepared(short_video);before=db.get(job)
    with pytest.raises(HubError):remove(service,job,before['payload']['metadata_sha256'],**arguments)
    assert db.get(job)==before


@pytest.mark.parametrize('tamper',['previous_binding','previous_binding_hash','new_binding_hash','wrong_version','active_id','extra_history','confirmation'])
def test_first_category_revision_must_fold_exactly_to_current_binding(short_video,tamper):
    _,db,service,*_=short_video;job,*_=prepared(short_video);current=db.get(job);value=deepcopy(current['result']);state=value[STATE];first=state['metadata_revision_history'][0]
    if tamper=='previous_binding':
        first['previous_binding']['metadata']['description']+='Owned unbound text';first['previous_binding']['metadata_sha256']=digest(first['previous_binding']['metadata'])
        first['previous_binding_sha256']=digest(first['previous_binding']);first['previous_metadata_sha256']=first['previous_binding']['metadata_sha256']
    elif tamper=='previous_binding_hash':first['previous_binding_sha256']='0'*64
    elif tamper=='new_binding_hash':first['new_binding_sha256']='0'*64
    elif tamper=='wrong_version':first['version']='unrelated-revision'
    elif tamper=='active_id':state['active_metadata_revision_id']=str(uuid.uuid4())
    elif tamper=='extra_history':state['metadata_revision_history'].append(deepcopy(first))
    else:first['source_characteristic_confirmation']['public_submission_authorized']=True
    with db.transaction() as conn:conn.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(value),job))
    before=db.get(job)
    with pytest.raises(HubError):remove(service,job,before['payload']['metadata_sha256'])
    assert db.get(job)==before


@pytest.mark.parametrize('evidence',['approval','owner_acceptance','result_observation','bv','historical_reserve','historical_submitting','publication_rejected','publication_unknown','intent'])
def test_any_existing_public_or_submit_evidence_blocks_description_revision(short_video,evidence):
    s,db,service,*_=short_video;job,*_=prepared(short_video);current=db.get(job);value=deepcopy(current['result'])
    if evidence=='approval':value[STATE]['public_submission_approved']=True
    elif evidence=='owner_acceptance':value[STATE]['owner_acceptance']={}
    elif evidence=='result_observation':value[STATE]['result_observation']={'external_id':BV}
    elif evidence=='bv':
        with db.transaction() as conn:conn.execute('UPDATE jobs SET external_publish_id=? WHERE job_id=?',(BV,job))
    elif evidence.startswith('historical_'):
        db.audit(PRINCIPAL,'short_video_reserve_submit' if evidence.endswith('reserve') else 'short_video_checkpoint',job,
            'one_artifact_one_click_reserved' if evidence.endswith('reserve') else 'submitting')
    elif evidence.startswith('publication_'):
        with db.transaction() as conn:conn.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('e'*64,job,'bilibili','youtube',current['source_id'],'site_uid:123456',KIND,evidence.removeprefix('publication_'),None,None,1))
    else:(s.root/'media'/job/'recovered-submit-intent.json').write_bytes(b'Owned existing intent; never ignored')
    with db.transaction() as conn:conn.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(value),job))
    before=db.get(job)
    with pytest.raises(HubError):remove(service,job,before['payload']['metadata_sha256'])
    assert db.get(job)==before


@pytest.mark.parametrize('suffix',['',' '+LINE,'\n'+LINE+'\n','\n'+LINE+' ','\r\n'+LINE,'\n'+LINE+'\nOwned following line'])
def test_only_exact_final_lf_and_public_permission_line_can_be_removed(short_video,suffix):
    _,db,service,*_=short_video;job,*_=prepared(short_video,suffix=suffix);before=db.get(job)
    with pytest.raises(HubError,match='SHORT_VIDEO_PERMISSION_LINE_REMOVAL_EXACT_LAST_LINE_REQUIRED'):
        remove(service,job,before['payload']['metadata_sha256'])
    assert db.get(job)==before


@pytest.mark.parametrize('change',['source_changed','wrong_upload','count_mismatch','reader'])
def test_existing_source_observation_count_and_operator_guards_are_preserved(short_video,change):
    _,db,service,*_=short_video;job,*_=prepared(short_video);current=db.get(job);value=deepcopy(current['result'])
    if change=='source_changed':Path(current['payload']['file']).write_bytes(b'Owned changed source fixture')
    elif change=='wrong_upload':value[STATE]['upload_observation']['same_attempt_observed']=False
    elif change=='count_mismatch':value[STATE]['private_upload_selections']=1
    with db.transaction() as conn:conn.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(value),job))
    before=db.get(job)
    with pytest.raises(HubError):
        if change=='reader':service.operator_remove_public_permission_line_v1('reader',job,expected_metadata_sha256=before['payload']['metadata_sha256'],actual_owner_response='Owned request',owner_response_reference='Owned reference',actual_operator_reference='Owned operator')
        else:remove(service,job,before['payload']['metadata_sha256'])
    assert db.get(job)==before


def test_concurrent_checkpoint_change_stops_before_any_payload_revision(short_video,monkeypatch):
    _,db,service,*_=short_video;job,*_=prepared(short_video);before=db.get(job);real=service.snapshot
    def race(job_id,*args,**kwargs):
        observed=real(job_id,*args,**kwargs)
        with db.transaction() as conn:conn.execute('UPDATE jobs SET updated_at=updated_at+1 WHERE job_id=?',(job_id,))
        return observed
    monkeypatch.setattr(service,'snapshot',race)
    with pytest.raises(HubError,match='SHORT_VIDEO_JOB_CHANGED'):remove(service,job,before['payload']['metadata_sha256'])
    assert db.get(job)['payload']==before['payload'] and db.get(job)['result']==before['result']
