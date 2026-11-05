"""Additional owned MockTransport checks for mutable-result and uncertain-call edges."""
from copy import deepcopy

import pytest

from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.model_contract import payload_for
from services.hub.model_receipts import digest
from services.hub.model_revision import build_revision_context
from test_model_receipts import model_fixture
from test_model_revision_v5 import base_pair, node, revision_context, V4


@pytest.mark.parametrize('bad_id', [None, [], {}, 'not-a-uuid'])
def test_invalid_previous_id_fails_with_validated_error_before_any_charge(model_fixture,bad_id):
    _,store,parent,server,_,_=model_fixture
    with pytest.raises(HubError,match='MODEL_REVISION_CONTEXT_INVALID'):
        build_revision_context(store,bad_id,parent_job_id=parent['job_id'],role='post',source=parent['payload'])
    assert not server.calls and not store.query('SELECT * FROM api_calls')


@pytest.mark.parametrize('change', ['remove_revision','change_basis_hash','change_parent','change_contract'])
def test_recorded_revision_loses_provenance_if_result_basis_is_removed_or_changed(model_fixture,change):
    _,store,parent,server,model,_=model_fixture
    base,_,_=base_pair(model_fixture)
    context=revision_context(store,base,parent)
    revised=node(store,parent,base['payload'],revision=context)
    server.text='架空测试改稿20:00';server.qa={'passed':True,'issues':[]}
    translated=model.invoke(revised,'translation',revised['payload'])
    model.invoke(revised,'quality',revised['payload'],translated['translation'])
    assert model.receipts.assess(revised,translated['translation'])['proof_integrity_passed']
    ledger=store.query('SELECT * FROM api_calls ORDER BY call_id')
    receipts=store.query('SELECT * FROM model_call_receipts ORDER BY call_id')
    changed=deepcopy(revised['result'])
    if change=='remove_revision':changed.pop('translation_revision')
    if change=='change_basis_hash':changed['translation_revision']['previous_quality_sha256']='0'*64
    if change=='change_parent':changed['parent_job_id']=base['job_id']
    if change=='change_contract':changed['translation_contract_version']=V4
    with store.transaction() as db:
        db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(changed),revised['job_id']))
    assert not model.receipts.assess(revised,translated['translation'])['proof_integrity_passed']
    with pytest.raises(HubError):model.invoke(revised,'translation',revised['payload'])
    assert len(server.calls)==4 and store.query('SELECT * FROM api_calls ORDER BY call_id')==ledger
    assert store.query('SELECT * FROM model_call_receipts ORDER BY call_id')==receipts


def test_complete_rejected_pair_cannot_hide_an_unresolved_alternate_contract_request(model_fixture):
    _,store,parent,server,model,_=model_fixture
    base,_,_=base_pair(model_fixture)
    old_payload=payload_for('translation',base['payload'],None,'chat_completions','synthetic-translate',4096,contract=V4)
    old_key=digest([V4,base['job_id'],'translation','chat_completions',
                    'https://model.example.test/v1/chat/completions',old_payload])
    with store.transaction() as db:
        db.execute('INSERT INTO model_requests VALUES(?,?,NULL,NULL)',(old_key,'unknown'))
    with pytest.raises(HubError,match='MODEL_REVISION_BASIS_UNKNOWN'):
        revision_context(store,base,parent)
    assert len(server.calls)==2 and len(store.query('SELECT * FROM api_calls'))==2


def test_missing_context_on_a_second_revision_slot_cannot_turn_it_into_a_new_base_call(model_fixture):
    _,store,parent,server,model,_=model_fixture
    base=node(store,parent,parent['payload'])
    with store.transaction() as db:
        db.execute('UPDATE jobs SET source_id=? WHERE job_id=?',
                   (parent['job_id']+':post:revision-2',base['job_id']))
    with pytest.raises(HubError,match='MODEL_REVISION_CONTEXT_INVALID'):
        model.invoke(base,'translation',base['payload'])
    assert not server.calls and not store.query('SELECT * FROM api_calls')
