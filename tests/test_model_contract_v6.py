"""Owned MockTransport evidence only; no live model, source or publishing grant."""
from collections import Counter
from copy import deepcopy
import json
import re
import unicodedata

import pytest

from adapters.model import ModelAPI
from services.hub.db import Store, encode
from services.hub.errors import HubError
from services.hub.evidence import machine_evidence
from services.hub.model_contract import VERSION, V6, payload_for
from services.hub.model_receipts import digest
from services.hub.model_revision import build_revision_context, validated_revision_context
from test_model_receipts import model_fixture


M3 = 'machine-tweet-v3-api'
V5 = 'model-text-contract-v5'
ISSUES = [{'source_span': '1日', 'translation_span': '一天', 'reason': 'Owned QA rejected the missing numeric token 1'}]
FROZEN_SOURCE = {'post': {'text': '１日の終わり 20:30\n1日 https://example.test/12', 'referenced_tweets': []},
                 'author': {'name': 'Owned fixture', 'username': 'owned_fixture'}}
FROZEN_V5 = {
    ('openai_responses', 'translation'): '308f32d382b6cad9f024caa7006177c5f0dc01e3f34be0d3f5f3f277df8e72af',
    ('openai_responses', 'quality'): '181eaa6ec92be13e9a1a0e18f5b05a943076a62c06662243b6e8b62fb7527c6c',
    ('chat_completions', 'translation'): '7f806b7f4a8b40714df4bba6969c9ac3fd441ae2d3649a844b53c977885861da',
    ('chat_completions', 'quality'): 'b6e6945e0c522b2a4ce6d1c50abef85a703f1376e02c2cdde6a4f02083621aa1',
}


def parts(payload, style):
    return (payload['instructions'], json.loads(payload['input'])) if style == 'openai_responses' else (
        payload['messages'][0]['content'], json.loads(payload['messages'][1]['content']))


def model_node(store, parent, source, *, contract=V6, revision=None):
    suffix = ':revision-1' if revision is not None else ''
    ident = store.enqueue('machine_model', parent['job_id'] + ':post' + suffix,
                          'machine_translation_node', 'tweet', source, M3)
    result = {'parent_job_id': parent['job_id'], 'role': 'post', 'human_review': False,
              'translation_contract_version': contract}
    if revision is not None:
        result['translation_revision'] = deepcopy(revision)
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='upload_pending',result=? WHERE job_id=?", (encode(result), ident))
    return store.get(ident)


@pytest.fixture
def numeric(model_fixture):
    settings, store, _, server, model, http = model_fixture
    source = deepcopy(model_fixture[2]['payload'])
    source['post'].update(text='1日の終わり20:00', referenced_tweets=[])
    ident = store.enqueue('x', source['post']['id'], 'tweet_process', 'tweet', source, M3)
    parent = store.get(ident)
    node = model_node(store, parent, source)
    server.text = '1天结束时20:00'
    return settings, store, parent, node, server, model, http


@pytest.mark.parametrize('style,phase', list(FROZEN_V5))
def test_v5_wire_bytes_and_global_default_remain_frozen(style, phase):
    candidate = None if phase == 'translation' else '1天结束时 20:30\n1天 https://example.test/12'
    assert VERSION == V5
    assert digest(payload_for(phase, FROZEN_SOURCE, candidate, style, 'owned-model', 1024, contract=V5)) == FROZEN_V5[(style, phase)]


@pytest.mark.parametrize('style', ['openai_responses', 'chat_completions'])
@pytest.mark.parametrize('phase', ['translation', 'quality'])
def test_v6_preserves_v5_source_view_and_adds_exact_body_number_multiset(style, phase):
    source = deepcopy(FROZEN_SOURCE)
    source['reply_context'] = {'parent_source': {'post': {'text': 'Parent context 999'}, 'author': {}}}
    candidate = None if phase == 'translation' else '1天结束时 20:30\n1天 https://example.test/12'
    old = payload_for(phase, source, candidate, style, 'owned-model', 1024, contract=V5)
    new = payload_for(phase, source, candidate, style, 'owned-model', 1024, contract=V6)
    old_system, old_request = parts(old, style)
    system, request = parts(new, style)
    assert system.startswith(old_system)
    assert {key: value for key, value in request.items() if key != 'required_source_numbers'} == old_request
    assert request['required_source_numbers'] == [{'token': '1', 'count': 2}, {'token': '20:30', 'count': 1}, {'token': '12', 'count': 1}]
    assert '1日' in system and '不得只写“一天”' in system
    assert not any(item['token'] == '999' for item in request['required_source_numbers'])
    if phase == 'quality':
        assert 'passed=false' in system


@pytest.mark.parametrize('text', ['普通の一日', '１日 1日 3/4 3/4 １.５', 'x https://example.test/12:34 @owner99', '① ２０:３０ ١٢'])
def test_number_prompt_uses_the_unchanged_checker_nfkc_regex_and_counter(text):
    source = {'post': {'text': text}, 'author': {}}
    _, request = parts(payload_for('translation', source, None, 'chat_completions', 'owned', 1024, contract=V6), 'chat_completions')
    expected = Counter(re.findall(r'\d+(?:[.:/]\d+)*', unicodedata.normalize('NFKC', text)))
    assert request['required_source_numbers'] == [{'token': token, 'count': count} for token, count in expected.items()]


def test_v6_two_phase_receipts_profiles_and_restart_cache_do_not_claim_live_or_human_review(numeric):
    settings, store, parent, node, server, model, http = numeric
    translated = model.invoke(node, 'translation', node['payload'])
    qa = model.invoke(node, 'quality', node['payload'], translated['translation'])
    proof = model.receipts.assess(node, translated['translation'])
    assert proof['proof_integrity_passed'] and proof['separate_requests_verified'] and proof['qa_recommends_pass']
    assert not proof['live_transport_verified'] and not proof['human_review'] and not proof['auto_publish_eligible']
    assert machine_evidence(node['payload'], translated['translation'], qa)['machine_checks_passed']
    assert {row['contract_version'] for row in store.query('SELECT * FROM model_request_profiles')} == {V6}
    assert {row['contract_version'] for row in store.query('SELECT * FROM model_call_receipts')} == {V6}
    before = {table: store.query('SELECT * FROM ' + table + ' ORDER BY 1') for table in ('api_calls', 'model_request_profiles', 'model_call_receipts')}
    reopened = ModelAPI(http, Store(settings.db_path), settings=settings)
    assert reopened.invoke(node, 'translation', node['payload']) == translated
    assert reopened.invoke(node, 'quality', node['payload'], translated['translation']) == qa
    assert reopened.receipts.assess(node, translated['translation']) == proof
    assert len(server.calls) == 2 and all(store.query('SELECT * FROM ' + table + ' ORDER BY 1') == rows for table, rows in before.items())


def test_true_qa_does_not_waive_number_checker_or_become_a_fake_rejection(numeric):
    _, store, parent, node, server, model, _ = numeric
    server.text = '一天结束时20:00'
    translated = model.invoke(node, 'translation', node['payload'])
    qa = model.invoke(node, 'quality', node['payload'], translated['translation'])
    assert qa == {'passed': True, 'issues': []}
    checked = machine_evidence(node['payload'], translated['translation'], qa)
    assert checked['checks']['numbers'] is False and not checked['machine_checks_passed']
    assert checked['independent_model_advice'] == qa
    with pytest.raises(HubError, match='MODEL_REVISION_REJECTION_REQUIRED'):
        build_revision_context(store, node['job_id'], parent_job_id=parent['job_id'], role='post', source=node['payload'])
    assert len(server.calls) == 2 and not store.query('SELECT * FROM publications')


def test_v6_genuine_rejection_allows_only_one_revision_and_quality_stays_blind(numeric):
    settings, store, parent, base, server, model, http = numeric
    server.text = '一天结束时20:00'
    server.qa = {'passed': False, 'issues': deepcopy(ISSUES)}
    value = model.invoke(base, 'translation', base['payload'])
    model.invoke(base, 'quality', base['payload'], value['translation'])
    basis = build_revision_context(store, base['job_id'], parent_job_id=parent['job_id'], role='post', source=base['payload'])
    revised = model_node(store, parent, base['payload'], revision=basis)
    server.text, server.qa = '1天结束时20:00', {'passed': True, 'issues': []}
    fixed = model.invoke(revised, 'translation', revised['payload'])
    quality = model.invoke(revised, 'quality', revised['payload'], fixed['translation'])
    _, translation_wire = parts(json.loads(server.calls[2].content), 'chat_completions')
    _, quality_wire = parts(json.loads(server.calls[3].content), 'chat_completions')
    assert translation_wire['translation_revision'] == {'previous_translation': value['translation'], 'quality_issues': ISSUES}
    assert 'translation_revision' not in quality_wire and 'previous_translation' not in encode(quality_wire)
    assert value['translation'] not in encode(quality_wire) and 'quality_issues' not in encode(quality_wire)
    assert quality_wire['required_source_numbers'] == [{'token': '1', 'count': 1}, {'token': '20:00', 'count': 1}]
    assert model.receipts.assess(revised, fixed['translation'])['proof_integrity_passed']
    reopened = ModelAPI(http, Store(settings.db_path), settings=settings)
    assert reopened.invoke(revised, 'quality', revised['payload'], fixed['translation']) == quality
    with pytest.raises(HubError):
        build_revision_context(store, revised['job_id'], parent_job_id=parent['job_id'], role='post', source=revised['payload'])
    assert len(server.calls) == 4


def test_v6_cannot_escape_an_old_v5_unknown_profile_by_mutating_the_contract(numeric):
    _, store, _, node, server, model, _ = numeric
    with store.transaction() as db:
        result = {**node['result'], 'translation_contract_version': V5}
        db.execute('UPDATE jobs SET result=? WHERE job_id=?', (encode(result), node['job_id']))
    node = store.get(node['job_id'])
    server.status = 'timeout'
    with pytest.raises(HubError, match='NETWORK_ERROR'):
        model.invoke(node, 'translation', node['payload'])
    before = {table: store.query('SELECT * FROM ' + table + ' ORDER BY 1') for table in ('api_calls', 'model_requests', 'model_request_profiles')}
    with store.transaction() as db:
        db.execute('UPDATE jobs SET result=? WHERE job_id=?', (encode({**node['result'], 'translation_contract_version': V6}), node['job_id']))
    with pytest.raises(HubError, match='MODEL_CONTRACT_PIN_CHANGED'):
        model.invoke(store.get(node['job_id']), 'translation', node['payload'])
    assert len(server.calls) == 1 and all(store.query('SELECT * FROM ' + table + ' ORDER BY 1') == rows for table, rows in before.items())


def test_unbound_v6_node_does_not_ignore_an_unknown_v5_alternate_request_key(numeric):
    _, store, _, node, server, model, _ = numeric
    spec, _ = model._spec(node, 'translation', node['payload'], None)
    old_payload = payload_for('translation', node['payload'], None, spec['protocol'], spec['model'], spec['maximum'], contract=V5)
    old_key = digest([V5, node['job_id'], 'translation', spec['protocol'], spec['endpoint'], old_payload])
    assert old_key != spec['request_key'] and old_key in spec['compatible_keys']
    with store.transaction() as db:
        db.execute('INSERT INTO model_requests VALUES(?,?,NULL,NULL)', (old_key, 'unknown'))
    with pytest.raises(HubError, match='MODEL_RESULT_UNKNOWN_REVIEW_REQUIRED'):
        model.invoke(node, 'translation', node['payload'])
    assert not server.calls and not store.query('SELECT * FROM api_calls')


def test_v6_is_not_available_to_general_or_old_dom_model_jobs(model_fixture):
    _, store, job, server, model, _ = model_fixture
    with store.transaction() as db:
        db.execute('UPDATE jobs SET result=? WHERE job_id=?', (encode({'translation_contract_version': V6}), job['job_id']))
    with pytest.raises(HubError, match='MODEL_CONTRACT_V6_MACHINE_API_REQUIRED'):
        model.invoke(store.get(job['job_id']), 'translation', job['payload'])
    assert not server.calls and not store.query('SELECT * FROM model_request_profiles')


@pytest.mark.parametrize('state', ['done', 'unknown'])
def test_v6_never_reuses_legacy_done_translation_but_still_blocks_legacy_unknown(numeric, state):
    _, store, _, node, server, model, _ = numeric
    spec, _ = model._spec(node, 'translation', node['payload'], None)
    old_response = encode({'translation': 'Unattested legacy candidate20:00'}) if state == 'done' else None
    with store.transaction() as db:
        db.execute('INSERT INTO model_requests VALUES(?,?,?,NULL)', (spec['legacy_key'], state, old_response))
    if state == 'unknown':
        with pytest.raises(HubError, match='MODEL_RESULT_UNKNOWN_REVIEW_REQUIRED'):
            model.invoke(node, 'translation', node['payload'])
        assert not server.calls and not store.query('SELECT * FROM model_call_receipts')
    else:
        assert model.invoke(node, 'translation', node['payload']) == {'translation': server.text}
        assert len(server.calls) == 1
        assert store.query('SELECT contract_version FROM model_call_receipts') == [{'contract_version': V6}]
        assert store.query('SELECT response FROM model_requests WHERE request_key=?', (spec['legacy_key'],)) == [{'response': old_response}]
