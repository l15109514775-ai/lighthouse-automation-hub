"""Actual owned SQLite coordinator revisions, with independent Node lineage checks."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import uuid

import pytest

from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.recovered_submit_ticket import issue_recovered_submit_ticket
from services.hub.short_video_web import PRINCIPAL, STATE
from services.hub.youtube_download import digest
from test_short_video_web import short_video
from test_short_video_permission_line_removal import prepared, remove


def make_spec(store, job, attempt):
    current = store.get(job);binding = current['payload'];state = current['result'][STATE]
    return {**{key: binding[key] for key in ('source_id', 'source_url', 'download_job_id',
        'file', 'file_bytes', 'artifact_sha256', 'metadata', 'metadata_sha256')},
        'job_id': job, 'attempt_id': attempt, 'uploaded_attempt_id': attempt, 'recovery_id': str(uuid.uuid4()),
        'target_site_uid': binding['site_uid'], 'binding_sha256': digest(binding),
        'upload_metadata_sha256': state['metadata_revision_history'][0]['previous_metadata_sha256'],
        'metadata_revision_id': state['active_metadata_revision_id'],
        'metadata_revision_history': deepcopy(state['metadata_revision_history']),
        'metadata_revision': deepcopy(state['metadata_revision_history'][-1]),
        'upload_reference_sha256': state['upload_observation']['upload_reference_sha256']}


def pipeline(f, *, second=True):
    settings,store,service,*_ = f
    job,attempt,folder,evidence = prepared(f)
    before = store.get(job);first = deepcopy(before['result'][STATE]['metadata_revision_history'][0])
    if second:
        remove(service, job, before['payload']['metadata_sha256'])
    return SimpleNamespace(settings=settings,store=store,service=service,job=job,attempt=attempt,
        folder=folder,evidence=evidence,first=first,retention=deepcopy(before['result'][STATE]['test_retention_policy']),
        spec=make_spec(store,job,attempt))


@pytest.fixture
def two_revisions(short_video):
    return pipeline(short_video)


def issue(value, spec=None, *, owner='Owned synthetic exact-artifact acceptance; not a real owner approval'):
    return issue_recovered_submit_ticket(value.service,value.job,spec or value.spec,principal=PRINCIPAL,
        actual_owner_response=owner,approval_reference='Owned isolated publication fixture only')


def node_check(value, spec=None, ticket=None):
    root = Path(__file__).resolve().parents[1]
    code = 'import {validateShortMetadataRevisionChain} from '+json.dumps((root/'adapters/short_video_metadata_chain.mjs').as_uri())+';'
    code += 'import {validateRecoveredTickets} from '+json.dumps((root/'adapters/recovered_submit_gate.mjs').as_uri())+';'
    code += ("let raw='';for await(const chunk of process.stdin)raw+=chunk;const v=JSON.parse(raw);"
             "const result=validateShortMetadataRevisionChain(v);"
             "if(v.tickets)validateRecoveredTickets({spec:v.spec,...v.tickets});console.log(JSON.stringify(result));")
    payload = {'spec': spec or value.spec,
        'originalMetadata': json.loads(value.evidence['metadata.json']),
        'originalSpec': json.loads(value.evidence['spec.json'])}
    if ticket:
        payload['tickets'] = {'approval': json.loads(ticket['approval_bytes']),
            'reservation': json.loads(ticket['reservation_bytes']),
            'approval_sha256': hashlib.sha256(ticket['approval_bytes']).hexdigest()}
    return subprocess.run(['node','--input-type=module','-e',code],input=json.dumps(payload,ensure_ascii=False),
        capture_output=True,text=True,encoding='utf-8',timeout=20)


def assert_not_approved(value):
    current = value.store.get(value.job)
    assert current['result'][STATE]['public_submission_approved'] is False
    assert 'owner_acceptance' not in current['result'][STATE]
    assert not value.store.query('SELECT * FROM publications')
    assert all((value.folder/name).read_bytes()==body for name,body in value.evidence.items())


def test_two_real_revisions_preserve_original_upload_and_need_separate_formal_acceptance(two_revisions):
    value=two_revisions;current=value.store.get(value.job);state=current['result'][STATE]
    assert_not_approved(value)
    assert state['metadata_revision_history'][0]==value.first and len(state['metadata_revision_history'])==2
    assert value.spec['upload_metadata_sha256']==value.first['previous_metadata_sha256']
    assert value.spec['upload_metadata_sha256']!=state['metadata_revision_history'][1]['previous_metadata_sha256']
    assert state['private_upload_selections']==2 and len(state['private_upload_history'])==1
    assert state['test_retention_policy']==value.retention
    assert state['test_retention_policy']['local_test_cleanup_requires_formal_workflow_launch'] is True
    assert state['test_retention_policy']['remote_video_deletion_requires_new_owner_confirmation'] is True
    result=node_check(value);assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['mode']=='category_and_public_line_removal'
    ticket=issue(value)
    checked=node_check(value,ticket=ticket);assert checked.returncode==0,checked.stderr
    assert value.store.get(value.job)['result'][STATE]['metadata_revision_history'][0]==value.first
    assert all((value.folder/name).read_bytes()==body for name,body in value.evidence.items())
    assert json.loads(ticket['approval_bytes'])['owner_acceptance']['human_review'] is False
    assert json.loads(ticket['reservation_bytes'])['binding']['metadata_revision_id']==value.spec['metadata_revision_id']
    assert value.store.query('SELECT state,external_id FROM publications')==[{'state':'dispatching','external_id':None}]


def test_original_single_revision_issuer_and_legacy_node_schema_remain_usable(short_video):
    value=pipeline(short_video,second=False)
    value.spec.pop('metadata_revision_history')
    result=node_check(value);assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['mode']=='legacy_category_single'
    ticket=issue(value)
    assert node_check(value,ticket=ticket).returncode==0
    assert value.store.get(value.job)['result'][STATE]['metadata_revision_history']==[value.first]


@pytest.mark.parametrize('change',['previous_revision_id','removed_last_line','scope_fields',
    'public_flag','rights_flag','previous_binding','fake_human_review','fake_nested_approval'])
def test_tampered_two_revision_evidence_cannot_become_an_approval(two_revisions,change):
    value=two_revisions;current=value.store.get(value.job);result=current['result']
    history=result[STATE]['metadata_revision_history'];last=history[-1]
    if change=='previous_revision_id':last['previous_revision_id']=str(uuid.uuid4())
    elif change=='removed_last_line':last['removed_last_line']='Owned unrelated footer'
    elif change=='scope_fields':last['changed_fields']=['description','title']
    elif change=='public_flag':last['public_submit']=True
    elif change=='rights_flag':last['internal_rights_records_preserved']=False
    elif change=='previous_binding':
        last['previous_binding']['file_bytes']+=1
        last['previous_binding_sha256']=digest(last['previous_binding'])
    elif change=='fake_human_review':history[0]['human_review']=True
    else:
        history[0]['source_characteristic_confirmation']['public_submission_authorized']=True
        result[STATE]['source_characteristic_confirmation']=deepcopy(history[0]['source_characteristic_confirmation'])
    with value.store.transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(result),value.job))
    spec=make_spec(value.store,value.job,value.attempt)
    with pytest.raises(HubError,match='RECOVERED_SUBMIT_ACTIVE_BINDING_REQUIRED'):issue(value,spec)
    assert_not_approved(value)


def test_even_coherently_rehashed_title_changes_are_outside_description_only_revision(two_revisions):
    value=two_revisions;current=value.store.get(value.job);binding=current['payload'];result=current['result']
    binding['metadata']['title']='Owned unauthorized title change'
    binding['metadata_sha256']=digest(binding['metadata'])
    result[STATE]['metadata_revision_history'][-1].update(new_metadata_sha256=binding['metadata_sha256'],
                                                        new_binding_sha256=digest(binding))
    with value.store.transaction() as db:
        db.execute('UPDATE jobs SET payload=?,result=? WHERE job_id=?',(encode(binding),encode(result),value.job))
    spec=make_spec(value.store,value.job,value.attempt)
    with pytest.raises(HubError):issue(value,spec)
    check=node_check(value,spec);assert check.returncode!=0 and 'RECOVERY_METADATA_REVISION_INVALID' in check.stderr
    assert_not_approved(value)


def test_intermediate_hash_cannot_replace_original_upload_hash(two_revisions):
    value=two_revisions;spec=deepcopy(value.spec)
    spec['upload_metadata_sha256']=spec['metadata_revision_history'][1]['previous_metadata_sha256']
    with pytest.raises(HubError):issue(value,spec)
    assert node_check(value,spec).returncode!=0
    assert_not_approved(value)


def test_two_revisions_cannot_hide_behind_legacy_single_record_field(two_revisions):
    value=two_revisions;spec=deepcopy(value.spec);spec.pop('metadata_revision_history')
    with pytest.raises(HubError):issue(value,spec)
    assert node_check(value,spec).returncode!=0
    assert_not_approved(value)


def test_public_description_instruction_does_not_supply_formal_publication_approval(two_revisions):
    value=two_revisions
    assert value.spec['metadata_revision']['actual_owner_response']=='转载许可编号删掉'
    with pytest.raises(HubError,match='SHORT_VIDEO_EXPLICIT_ARTIFACT_ACCEPTANCE_REQUIRED'):
        issue(value,owner='')
    assert_not_approved(value)


def test_historical_submission_reservation_blocks_fresh_ticket_even_if_publication_row_is_absent(two_revisions):
    value=two_revisions
    value.store.audit(PRINCIPAL,'short_video_reserve_submit',value.job,'one_artifact_one_click_reserved')
    with pytest.raises(HubError):issue(value)
    assert_not_approved(value)
