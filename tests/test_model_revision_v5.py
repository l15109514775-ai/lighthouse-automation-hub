"""Owned text and MockTransport only: bounded revision never claims live or human approval."""
from copy import deepcopy
import json
import re

import pytest

from adapters.model import ModelAPI
from services.hub.db import Store, encode
from services.hub.errors import HubError
from services.hub.model_contract import payload_for
from services.hub.model_receipts import digest
from test_model_receipts import model_fixture


V5='model-text-contract-v5'
V4='model-text-contract-v4'
REVISION_FIELDS={'version','index','previous_model_job_id','source_sha256',
    'previous_translation_sha256','previous_quality_sha256','translation_request_key','quality_request_key'}
ISSUES=[{'source_span':'架空','translation_span':'架空','reason':'Owned synthetic QA correction; not human review'}]


def node(store,parent,source,*,role='post',revision=None):
    source_id=parent['job_id']+':'+role+(':revision-1' if revision is not None else '')
    ident=store.enqueue('machine_model',source_id,'machine_translation_node','tweet',source,'machine-tweet-v2')
    result={'translation_contract_version':V5,'parent_job_id':parent['job_id'],'role':role,'human_review':False}
    if revision is not None:result['translation_revision']=deepcopy(revision)
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='upload_pending',stage='model_input_bound',result=? WHERE job_id=?",
            (encode(result),ident))
    return store.get(ident)


def base_pair(fixture,*,qa_passed=False):
    settings,store,parent,server,model,http=fixture
    base=node(store,parent,parent['payload'])
    server.qa={'passed':qa_passed,'issues':[] if qa_passed else deepcopy(ISSUES)}
    value=model.invoke(base,'translation',base['payload'])
    qa=model.invoke(base,'quality',base['payload'],value['translation'])
    return base,value,qa


def revision_context(store,base,parent):
    from services.hub.model_revision import build_revision_context
    return build_revision_context(store,base['job_id'],parent_job_id=parent['job_id'],role='post',source=base['payload'])


@pytest.mark.parametrize('style',['chat_completions','openai_responses'])
@pytest.mark.parametrize('phase',['translation','quality'])
def test_v5_without_revision_preserves_v4_payload_exactly(style,phase):
    source={'post':{'text':'Owned synthetic 20:00','referenced_tweets':[]},
        'author':{'name':'Owned author','username':'fixture_only'}}
    translation=None if phase=='translation' else 'Owned synthetic translation20:00'
    assert payload_for(phase,source,translation,style,'owned-model',4096,contract=V5)==\
        payload_for(phase,source,translation,style,'owned-model',4096,contract=V4)


@pytest.mark.parametrize('final_pass',[True,False])
def test_base_then_single_revision_has_four_receipts_blind_qa_and_restart_cache(model_fixture,final_pass):
    from services.hub.model_revision import validated_revision_context
    settings,store,parent,server,model,http=model_fixture
    base,old_translation,old_quality=base_pair(model_fixture)
    context=revision_context(store,base,parent)
    assert set(context)==REVISION_FIELDS
    assert context['version']=='model-translation-revision-v1' and context['index']==1
    assert context['previous_model_job_id']==base['job_id'] and context['source_sha256']==digest(base['payload'])
    for field in REVISION_FIELDS-{'version','index','previous_model_job_id'}:
        assert re.fullmatch('[a-f0-9]{64}',context[field])
    old_rows={row['phase']:row for row in store.query('SELECT * FROM model_call_receipts WHERE job_id=?',(base['job_id'],))}
    assert context['translation_request_key']==old_rows['translation']['request_key']
    assert context['quality_request_key']==old_rows['quality']['request_key']
    revised=node(store,parent,base['payload'],revision=context)
    assert validated_revision_context(store,revised)=={
        'previous_translation':old_translation['translation'],'quality_issues':old_quality['issues']}
    assert validated_revision_context(store,base) is None
    server.text='架空测试改稿20:00'
    server.qa={'passed':final_pass,'issues':[] if final_pass else deepcopy(ISSUES)}
    new_translation=model.invoke(revised,'translation',revised['payload'])
    new_quality=model.invoke(revised,'quality',revised['payload'],new_translation['translation'])
    wire_translation=json.loads(json.loads(server.calls[2].content)['messages'][1]['content'])
    wire_quality=json.loads(json.loads(server.calls[3].content)['messages'][1]['content'])
    assert wire_translation['translation_revision']=={
        'previous_translation':old_translation['translation'],'quality_issues':old_quality['issues']}
    assert 'translation_revision' not in wire_quality
    assert 'previous_translation' not in encode(wire_quality) and 'quality_issues' not in encode(wire_quality)
    assert old_translation['translation'] not in encode(wire_quality)
    assert wire_quality['translation']==new_translation['translation']
    assert json.loads(server.calls[3].content)==payload_for('quality',revised['payload'],new_translation['translation'],
        'chat_completions','synthetic-qa',4096,contract=V4)
    proof=model.receipts.assess(revised,new_translation['translation'])
    assert proof['proof_integrity_passed'] and proof['separate_requests_verified']
    assert proof['qa_recommends_pass'] is final_pass
    assert proof['live_transport_verified'] is False and proof['auto_publish_eligible'] is False and proof['human_review'] is False
    receipts=store.query('SELECT * FROM model_call_receipts ORDER BY request_key')
    assert len(receipts)==4 and len({r['call_id'] for r in receipts})==4
    raw={r['call_id']:model.receipts._file(r['job_id'],r['call_id']).read_bytes() for r in receipts}
    calls=store.query('SELECT * FROM api_calls ORDER BY call_id')
    reopened_store=Store(settings.db_path);reopened=ModelAPI(http,reopened_store,settings=settings)
    assert reopened.invoke(base,'translation',base['payload'])==old_translation
    assert reopened.invoke(base,'quality',base['payload'],old_translation['translation'])==old_quality
    assert reopened.invoke(revised,'translation',revised['payload'])==new_translation
    assert reopened.invoke(revised,'quality',revised['payload'],new_translation['translation'])==new_quality
    assert len(server.calls)==4 and reopened_store.query('SELECT * FROM api_calls ORDER BY call_id')==calls
    assert reopened_store.query('SELECT * FROM model_call_receipts ORDER BY request_key')==receipts
    assert all(reopened.receipts._file(r['job_id'],r['call_id']).read_bytes()==raw[r['call_id']] for r in receipts)
    from services.hub.model_revision import build_revision_context
    with pytest.raises(HubError):build_revision_context(reopened_store,revised['job_id'],
        parent_job_id=parent['job_id'],role='post',source=revised['payload'])
    assert len(server.calls)==4
    assert not store.query('SELECT * FROM publications') and not store.query('SELECT * FROM publication_reviews')


def test_accepted_base_cannot_be_used_as_a_reason_to_charge_for_revision(model_fixture):
    _,store,parent,server,_,_=model_fixture
    base,_,_=base_pair(model_fixture,qa_passed=True)
    with pytest.raises(HubError):revision_context(store,base,parent)
    assert len(server.calls)==2 and len(store.query('SELECT * FROM api_calls'))==2


@pytest.mark.parametrize('change',['index','version','previous_model_job_id','source_sha256',
    'previous_translation_sha256','previous_quality_sha256','translation_request_key','quality_request_key','extra_key'])
def test_forged_revision_context_is_rejected_before_model_or_budget_reservation(model_fixture,change):
    settings,store,parent,server,model,http=model_fixture
    base,_,_=base_pair(model_fixture);context=revision_context(store,base,parent)
    if change=='index':context[change]=2
    elif change=='version':context[change]='invented-revision-contract'
    elif change=='previous_model_job_id':context[change]=parent['job_id']
    elif change=='extra_key':context[change]='invented approval'
    else:context[change]='0'*64
    revised=node(store,parent,base['payload'],revision=context)
    with pytest.raises(HubError):model.invoke(revised,'translation',revised['payload'])
    assert len(server.calls)==2 and len(store.query('SELECT * FROM api_calls'))==2
    assert not store.query('SELECT * FROM model_call_receipts WHERE job_id=?',(revised['job_id'],))
    assert len(store.query('SELECT * FROM model_requests'))==2


@pytest.mark.parametrize('change',['raw','missing_raw','cache','api_state','api_time','request_hash'])
def test_revision_revalidates_real_base_raw_cache_and_ledger_chain_before_new_call(model_fixture,change):
    _,store,parent,server,model,_=model_fixture
    base,_,_=base_pair(model_fixture);context=revision_context(store,base,parent)
    revised=node(store,parent,base['payload'],revision=context)
    row=store.query("SELECT * FROM model_call_receipts WHERE job_id=? AND phase='quality'",(base['job_id'],))[0]
    if change in ('raw','missing_raw'):
        file=model.receipts._file(base['job_id'],row['call_id'])
        if change=='raw':file.write_bytes(file.read_bytes()+b'\n')
        else:file.unlink()
    else:
        with store.transaction() as db:
            if change=='cache':db.execute('UPDATE model_requests SET response=? WHERE request_key=?',
                (encode({'passed':False,'issues':[{'source_span':'tampered','translation_span':'tampered','reason':'tampered'}]}),row['request_key']))
            if change=='api_state':db.execute("UPDATE api_calls SET state='unknown' WHERE call_id=?",(row['call_id'],))
            if change=='api_time':db.execute('UPDATE api_calls SET finished_at=finished_at+1 WHERE call_id=?',(row['call_id'],))
            if change=='request_hash':db.execute('UPDATE model_call_receipts SET request_sha256=? WHERE request_key=?',('0'*64,row['request_key']))
    with pytest.raises(HubError):model.invoke(revised,'translation',revised['payload'])
    with pytest.raises(HubError):revision_context(store,base,parent)
    assert len(server.calls)==2 and len(store.query('SELECT * FROM api_calls'))==2
    assert len(store.query('SELECT * FROM model_requests'))==2


@pytest.mark.parametrize('change',['source','parent','role','prior_revision'])
def test_revision_cannot_cross_source_parent_role_or_start_another_revision(model_fixture,change):
    from services.hub.model_revision import build_revision_context
    _,store,parent,server,model,_=model_fixture
    base,_,_=base_pair(model_fixture)
    source=deepcopy(base['payload']);parent_id=parent['job_id'];role='post'
    if change=='source':source['post']['text']='Different synthetic source20:00'
    if change=='parent':parent_id=base['job_id']
    if change=='role':role='parent'
    if change=='prior_revision':
        result={**base['result'],'translation_revision':{'version':'model-translation-revision-v1','index':1}}
        with store.transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(result),base['job_id']))
    with pytest.raises(HubError):build_revision_context(store,base['job_id'],parent_job_id=parent_id,role=role,source=source)
    assert len(server.calls)==2 and len(store.query('SELECT * FROM api_calls'))==2


@pytest.mark.parametrize('old_contract',['model-text-contract-v1','model-text-contract-v2','model-text-contract-v3',V4])
@pytest.mark.parametrize('state',['unknown','dispatching'])
@pytest.mark.parametrize('phase',['translation','quality'])
def test_v5_selection_cannot_evade_unresolved_old_contract_or_charge_again(model_fixture,old_contract,state,phase):
    _,store,parent,server,model,_=model_fixture
    base=node(store,parent,parent['payload']);translation=None if phase=='translation' else server.text
    model_name='synthetic-translate' if phase=='translation' else 'synthetic-qa'
    old=payload_for(phase,base['payload'],translation,'chat_completions',model_name,4096,contract=old_contract)
    key=digest([old_contract,base['job_id'],phase,'chat_completions','https://model.example.test/v1/chat/completions',old])
    with store.transaction() as db:db.execute('INSERT INTO model_requests VALUES(?,?,NULL,NULL)',(key,state))
    with pytest.raises(HubError,match='MODEL_RESULT_UNKNOWN_REVIEW_REQUIRED'):
        model.invoke(base,phase,base['payload'],translation)
    assert not server.calls and not store.query('SELECT * FROM api_calls')
    assert store.query('SELECT request_key,state FROM model_requests')==[{'request_key':key,'state':state}]


def test_actual_v4_network_unknown_profile_keeps_contract_pin_after_reopen_and_v5_selection(model_fixture):
    settings,store,parent,server,model,http=model_fixture
    base=node(store,parent,parent['payload'])
    with store.transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',
        (encode({**base['result'],'translation_contract_version':V4}),base['job_id']))
    server.status='timeout'
    with pytest.raises(HubError,match='NETWORK_ERROR'):model.invoke(base,'translation',base['payload'])
    before=store.query('SELECT * FROM api_calls');requests=store.query('SELECT * FROM model_requests')
    with store.transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(base['result']),base['job_id']))
    server.status=200
    with pytest.raises(HubError,match='MODEL_CONTRACT_PIN_CHANGED'):
        ModelAPI(http,Store(settings.db_path),settings=settings).invoke(base,'translation',base['payload'])
    assert len(server.calls)==1 and store.query('SELECT * FROM api_calls')==before
    assert store.query('SELECT * FROM model_requests')==requests
