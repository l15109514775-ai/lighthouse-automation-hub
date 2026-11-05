"""Owned text and MockTransport only: contract upgrades never fabricate approval."""
import json

import pytest

from adapters.model import ModelAPI
from services.hub.db import Store, encode
from services.hub.errors import HubError
from services.hub.model_contract import SUPPORTED_CONTRACTS, VERSION, payload_for, validate_value
from services.hub.model_receipts import digest
from test_model_receipts import model_fixture


V3 = 'model-text-contract-v3'
V4 = 'model-text-contract-v4'
SKILL_RULE = (' 称赞动作或技能熟练度时，应体现做得好或操作熟练，不要误译成物件本身厉害。'
              '不确定的术语可以保留原文，不补充未提供的游戏、施受对象或结果。')


def owned_source():
    return {
        'post': {'id': '123', 'text': '架空の技がうますぎる！\n20:00',
                 'referenced_tweets': [{'id': '122', 'type': 'replied_to'}]},
        'author': {'name': 'Owned fixture', 'username': 'fixture_only'},
        'reply_context': {'parent_source': {
            'post': {'text': 'Owned parent context'},
            'author': {'name': 'Parent fixture', 'username': 'parent_fixture'}}},
        'translation_glossary': {'架空語': '架空語'},
        'capture': {'screenshot_file': 'private-path-do-not-send', 'human_review': False},
    }


# The first twelve were recorded before v4; the final four before v5. Each
# historical wire identity is fixed independently of the upgraded implementation.
OLD_PAYLOAD_DIGESTS = {
    'model-text-contract-v1|chat_completions|translation': 'f5763f8ba022effe848093bc4623737cfa2faed92c065143dfd7ef78f2851534',
    'model-text-contract-v1|chat_completions|quality': '260bf704659ad877608931040e2fa66010d178e86332387bae72930cbf22211d',
    'model-text-contract-v1|openai_responses|translation': 'd47f93ea692ea4fc32b287ad741fc653f3ab07bd22b67e1d62b3925292a11f6f',
    'model-text-contract-v1|openai_responses|quality': '5f2a5ee4f76d68eda82c8787431fc9e9f1f9a66ff3c69b565181da4989404150',
    'model-text-contract-v2|chat_completions|translation': '6c5aafc31c623ed0763a5a91fd36402735ebadae7c8e25c8f7f8782df6e9097c',
    'model-text-contract-v2|chat_completions|quality': '1c5d3375132de93e0a66775edcaccd676205c20fec9e09017c02c4155872a464',
    'model-text-contract-v2|openai_responses|translation': '3f032a9f56a6060e3906625657e4df0ed7e6a5b953d4e8d9efc6a4744472a7f3',
    'model-text-contract-v2|openai_responses|quality': 'd4be86988505dba9e27f43424803bd4fd0f2e90a5f22938695b9caf5b240d6f2',
    'model-text-contract-v3|chat_completions|translation': '55ffe94ad6672aa0a8943a2ce54203b96f9a130426643b27f4664a1537547b10',
    'model-text-contract-v3|chat_completions|quality': '7363c6d941b6be6941cc6065c19a4823afb6f85b51ac3c3ce6650524f0dfc6fc',
    'model-text-contract-v3|openai_responses|translation': '39336ec6a5f0e769e8c2b3a5244d89003d6777778074063438a9e29ef89167db',
    'model-text-contract-v3|openai_responses|quality': '01ce3d3c0e71dd5799501c7a76046a461fc282c0c62d376f68e398ee2a82be1a',
    'model-text-contract-v4|chat_completions|translation': 'b4e69e4ed71ddda59bee89eaa3e5ef3d1eedc0d7ff0f67a86c4738e87d830b3d',
    'model-text-contract-v4|chat_completions|quality': '7363c6d941b6be6941cc6065c19a4823afb6f85b51ac3c3ce6650524f0dfc6fc',
    'model-text-contract-v4|openai_responses|translation': '50f554b07f3ce3bbb10a2589e16266be408247433d98a32cdfc678ed65c76c3c',
    'model-text-contract-v4|openai_responses|quality': '01ce3d3c0e71dd5799501c7a76046a461fc282c0c62d376f68e398ee2a82be1a',
}


@pytest.mark.parametrize('identity,expected', OLD_PAYLOAD_DIGESTS.items())
def test_v1_v2_v3_v4_payload_identities_remain_byte_stable(identity, expected):
    version, style, phase = identity.split('|')
    value = payload_for(phase, owned_source(),
                        None if phase == 'translation' else '架空技巧真熟练！\n20:00',
                        style, 'owned-model', 4096, contract=version)
    assert digest(value) == expected


@pytest.mark.parametrize('style', ['chat_completions', 'openai_responses'])
def test_v4_only_appends_generic_translation_guidance_and_keeps_v3_quality_exact(style):
    assert SUPPORTED_CONTRACTS[:4] == ('model-text-contract-v1', 'model-text-contract-v2', V3, V4)
    old = payload_for('translation', owned_source(), None, style, 'owned-model', 4096, contract=V3)
    new = payload_for('translation', owned_source(), None, style, 'owned-model', 4096, contract=V4)
    if style == 'chat_completions':
        assert new['messages'][0]['content'] == old['messages'][0]['content'] + SKILL_RULE
        new['messages'][0]['content'] = old['messages'][0]['content']
    else:
        assert new['instructions'] == old['instructions'] + SKILL_RULE
        new['instructions'] = old['instructions']
    assert new == old
    assert payload_for('quality', owned_source(), 'Owned translation', style, 'owned-model', 4096,
                       contract=V4) == payload_for('quality', owned_source(), 'Owned translation',
                                                   style, 'owned-model', 4096, contract=V3)


@pytest.mark.parametrize('phase', ['translation', 'quality'])
@pytest.mark.parametrize('style', ['chat_completions', 'openai_responses'])
def test_v4_inherits_text_only_parent_context_without_human_or_image_properties(phase, style):
    value = payload_for(phase, owned_source(), None if phase == 'translation' else 'Owned translation',
                        style, 'owned-model', 4096, contract=V4)
    raw = value['input'] if style == 'openai_responses' else value['messages'][1]['content']
    body = json.loads(raw)
    assert set(body) == {'source', 'translation', 'context', 'glossary'}
    assert body['source']['post'] == {'text': owned_source()['post']['text']}
    assert body['context']['parent_post'] == {
        'text': 'Owned parent context',
        'author': {'name': 'Parent fixture', 'username': 'parent_fixture'},
        'relationship_status': 'unreviewed_visible_conversation_candidate',
    }
    assert body['glossary'] == {'架空語': '架空語'}
    assert 'private-path-do-not-send' not in raw
    assert 'human_review' not in raw and 'screenshot_file' not in raw and 'image_url' not in raw
    if style == 'openai_responses':
        schema = value['text']['format']['schema']
        assert schema['additionalProperties'] is False
        assert set(schema['properties']) == ({'translation'} if phase == 'translation' else {'passed', 'issues'})
    output = {'translation': 'Owned translation'} if phase == 'translation' else {'passed': True, 'issues': []}
    with pytest.raises(HubError, match='MODEL_OUTPUT_INVALID'):
        validate_value({**output, 'human_review': True}, phase)


def pin_contract(store, job, contract):
    with store.transaction() as db:
        db.execute('UPDATE jobs SET result=? WHERE job_id=?',
                   (encode({'translation_contract_version': contract}) if contract else '{}', job['job_id']))


def test_new_jobs_use_current_contract_existing_v3_receipts_and_qa_denial_survive_restart(model_fixture):
    settings, store, job, server, model, http = model_fixture
    pin_contract(store, job, V3)
    translated = model.invoke(job, 'translation', job['payload'])
    server.qa = {'passed': False, 'issues': [
        {'source_span': '架空', 'translation_span': '架空', 'reason': 'Owned synthetic QA denial'}]}
    qa = model.invoke(job, 'quality', job['payload'], translated['translation'])
    assert qa['passed'] is False
    before = store.query('SELECT * FROM model_call_receipts ORDER BY phase')
    raw_before = {row['call_id']: model.receipts._file(job['job_id'], row['call_id']).read_bytes() for row in before}
    assert {row['contract_version'] for row in before} == {V3}
    pin_contract(store, job, None)  # The persisted receipt, not a caller marker, pins the old version.
    reopened = ModelAPI(http, Store(settings.db_path), settings=settings)
    assert reopened.invoke(job, 'translation', job['payload']) == translated
    assert reopened.invoke(job, 'quality', job['payload'], translated['translation']) == qa
    assert len(server.calls) == 2
    assert store.query('SELECT * FROM model_call_receipts ORDER BY phase') == before
    assert all(reopened.receipts._file(job['job_id'], ident).read_bytes() == raw for ident, raw in raw_before.items())
    proof = reopened.receipts.assess(store.get(job['job_id']), translated['translation'])
    assert proof['proof_integrity_passed'] and not proof['qa_recommends_pass']
    assert not proof['human_review'] and not proof['auto_publish_eligible'] and not proof['live_transport_verified']
    new_id = store.enqueue('synthetic_acceptance', 'owned-new-v4', 'tweet_process', 'tweet',
                           job['payload'], settings.config['policy_version'])
    new_job = store.get(new_id)
    reopened.invoke(new_job, 'translation', new_job['payload'])
    new_rows = store.query('SELECT * FROM model_call_receipts WHERE job_id=?', (new_id,))
    assert len(new_rows) == 1 and new_rows[0]['contract_version'] == VERSION
    assert SKILL_RULE in json.loads(server.calls[-1].content)['messages'][0]['content']
    assert len(server.calls) == 3 and len(store.query('SELECT * FROM api_calls')) == 3
    assert not store.query('SELECT * FROM publications') and not store.query('SELECT * FROM publication_reviews')


@pytest.mark.parametrize('old_version', SUPPORTED_CONTRACTS[:3])
@pytest.mark.parametrize('state', ['unknown', 'dispatching'])
@pytest.mark.parametrize('phase', ['translation', 'quality'])
def test_current_contract_cannot_evade_old_unresolved_requests_or_charge_again(model_fixture, old_version, state, phase):
    settings, store, job, server, model, http = model_fixture
    translation = None if phase == 'translation' else server.text
    name = 'synthetic-translate' if phase == 'translation' else 'synthetic-qa'
    old_payload = payload_for(phase, job['payload'], translation, 'chat_completions', name, 4096,
                              contract=old_version)
    key = digest([old_version, job['job_id'], phase, 'chat_completions',
                  'https://model.example.test/v1/chat/completions', old_payload])
    with store.transaction() as db:
        db.execute('INSERT INTO model_requests VALUES(?,?,NULL,NULL)', (key, state))
    with pytest.raises(HubError, match='MODEL_RESULT_UNKNOWN_REVIEW_REQUIRED'):
        model.invoke(job, phase, job['payload'], translation)
    assert not server.calls and not store.query('SELECT * FROM api_calls')
    assert not store.query('SELECT * FROM model_call_receipts')
    assert store.query('SELECT request_key,state FROM model_requests') == [{'request_key': key, 'state': state}]


def test_actual_v3_mock_timeout_remains_unknown_after_switching_to_current_default(model_fixture):
    settings, store, job, server, model, http = model_fixture
    pin_contract(store, job, V3)
    server.status = 'timeout'
    with pytest.raises(HubError, match='NETWORK_ERROR'):
        model.invoke(job, 'translation', job['payload'])
    requests_before = store.query('SELECT * FROM model_requests')
    costs_before = store.query('SELECT * FROM api_calls')
    assert requests_before[0]['state'] == 'unknown' and costs_before[0]['state'] == 'unknown'
    pin_contract(store, job, None)
    server.status = 200
    reopened = ModelAPI(http, Store(settings.db_path), settings=settings)
    with pytest.raises(HubError, match='MODEL_RESULT_UNKNOWN_REVIEW_REQUIRED'):
        reopened.invoke(job, 'translation', job['payload'])
    assert len(server.calls) == 1 and store.query('SELECT * FROM model_requests') == requests_before
    assert store.query('SELECT * FROM api_calls') == costs_before
    assert not store.query('SELECT * FROM model_call_receipts')
