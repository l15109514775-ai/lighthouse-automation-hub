import json
from services.hub.model_contract import payload_for
from services.hub.model_receipts import digest


def source():
    return {'post':{'id':'123','text':'ちーたま\n20:00','referenced_tweets':[]},
            'author':{'name':'Owned fixture','username':'fixture_only'}}


def test_v2_request_hashes_are_unchanged_for_existing_real_receipts():
    assert digest(payload_for('translation',source(),None,'openai_responses','fixture-model',4096,
        contract='model-text-contract-v2'))=='272d7c667909fee546a72e1eba8747d6ff5aaee961fffcc7c50a991b4ab6c7e7'
    assert digest(payload_for('quality',source(),'测试译文\n20:00','openai_responses','fixture-model',4096,
        contract='model-text-contract-v2'))=='09d5b1aa7e6cd038964c0e91d4dc18c7191721cadf6a122d39f61caf1a6159d5'


def test_v3_sends_parent_as_context_and_persistent_glossary_without_hidden_evidence():
    value=source();value['post']['text']='Owned reply only'
    value['reply_context']={'parent_source':{'post':{'text':'Owned original context'},'author':{'name':'Parent','username':'parent_fixture'}}}
    value['translation_glossary']={'ちーたま':'ちーたま'};value['capture']={'screenshot_file':'private-local-path','not_a_model_input':True}
    payload=payload_for('translation',value,None,'openai_responses','fixture-model',4096)
    data=json.loads(payload['input'])
    assert data['source']['post']['text']=='Owned reply only'
    assert data['context']['parent_post']['text']=='Owned original context'
    assert data['context']['parent_post']['relationship_status'].startswith('unreviewed')
    assert data['glossary']=={'ちーたま':'ちーたま'}
    assert 'private-local-path' not in payload['input']
