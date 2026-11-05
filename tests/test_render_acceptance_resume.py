"""Owned offline legacy-rejection recovery, not live transport/render acceptance.

Real ModelAPI + MockTransport records actual owned wire bytes and ledgers.  The
source-validator and renderer boundaries use owned fixtures here (their browser
checks have separate suites).  Only tests explicitly named policy_simulation
temporarily treat an otherwise fully verified custom-transport pair as live in
memory to exercise control flow.  Database/raw transport evidence stays custom;
no human-review flags, TLS records, source times or tombstones are rewritten.
"""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.evidence import sha256_file
from services.hub.extended_web_source import ExtendedWebSources
from services.hub.machine_tweets_v2 import MachineTweetsV2
from services.hub.model_receipts import ModelReceipts
from services.hub.render_acceptance import RenderAcceptance
from test_model_receipts import model_fixture


QA_DENIAL = {'passed': False, 'issues': [{'source_span': '架空', 'translation_span': '架空',
                                        'reason': 'Owned QA denial; no human review'}]}


@pytest.fixture
def owned_rejection(model_fixture, monkeypatch, request):
    settings, store, unused, server, model, http = model_fixture
    discovery_id = store.enqueue('operator_acceptance', 'owned-resume-discovery', 'x_discovery', 'tweet', {}, 'machine-tweet-v2')
    source = deepcopy(unused['payload'])
    source['author'] = {'name': 'Owned reply author', 'username': 'example-channel-b'}
    source['url'] = 'https://x.com/example-channel-b/status/' + source['post']['id']
    parent_source = deepcopy(source)
    parent_source['post']['id'] = '1000000000000000021'
    parent_source['post']['text'] = '架空の親テスト20:00'
    parent_source['author'] = {'name': 'Owned original author', 'username': 'fixture_parent'}
    parent_source['url'] = 'https://x.com/fixture_parent/status/' + parent_source['post']['id']
    source['post']['referenced_tweets'] = [{'type': 'replied_to', 'id': parent_source['post']['id']}]
    source['reply_context'] = {'parent_source': parent_source}
    discovery_folder = settings.root / 'media' / discovery_id
    discovery_folder.mkdir()
    screenshot = discovery_folder / 'owned-source.png'
    screenshot.write_bytes(b'Owned source-validator boundary fixture, not a real screenshot')
    record = {'payload': source, 'screenshot_file': str(screenshot)}
    checked = {'nodes': [{'role': role, 'source': raw, 'media': [], 'provenance': [],
                         'screenshot': {'file': str(screenshot), 'sha256': sha256_file(screenshot)}}
                        for role, raw in (('post', source), ('parent', parent_source))],
               'node_proof': {'owned_validator_boundary': True}, 'reply_proof': {'owned_relation_boundary': True}}
    expected_record = deepcopy(record)

    def owned_verify(self, discovery, observed, packet):
        assert discovery == discovery_id
        if observed != expected_record or json.loads(Path(packet).read_text('utf-8')) != expected_record:
            raise HubError('ACCEPTANCE_SOURCE_CHANGED')
        if sha256_file(screenshot) != checked['nodes'][0]['screenshot']['sha256']:
            raise HubError('EXTENDED_SOURCE_FILE_INVALID')
        return deepcopy(checked)

    monkeypatch.setattr(ExtendedWebSources, '_verify', owned_verify)
    ident = store.enqueue('acceptance', source['post']['id'], 'machine_render_acceptance', 'tweet', source,
                          'machine-render-acceptance-v1:model-text-contract-v4')
    folder = settings.root / 'media' / ident
    folder.mkdir()
    packet = folder / 'acceptance-source-packet.json'
    packet.write_text(encode(record), 'utf-8')
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='upload_pending' WHERE job_id=?", (ident,))
        db.execute('INSERT INTO source_tombstones VALUES(?,?,?,?,?,?)',
                   ('x', source['post']['id'], 'OWNER_BACKLOG_CLEARED', time.time(), discovery_id, 'owner_clear_all'))
    parent = store.get(ident)
    service = MachineTweetsV2(settings, store)
    children = {}
    translations = {'post': '架空回复20:00', 'parent': '架空原推20:00'}
    parent_qa_passed = getattr(request, 'param', False)
    for node in checked['nodes']:
        child = service.model_node(parent, node)
        with store.transaction() as db:
            db.execute('UPDATE jobs SET result=? WHERE job_id=?',
                       (encode({**child['result'], 'translation_contract_version': 'model-text-contract-v4'}), child['job_id']))
        child = store.get(child['job_id'])
        server.text = translations[node['role']]
        server.qa = {'passed': True, 'issues': []} if node['role'] == 'post' or parent_qa_passed else deepcopy(QA_DENIAL)
        translated = model.invoke(child, 'translation', node['source'])
        qa = model.invoke(child, 'quality', node['source'], translated['translation'])
        entry = {'job_id': child['job_id'], 'translation': translated['translation'], 'quality': qa}
        if node['role'] == 'parent':
            # Reconstruct a legacy failure using the real recorder; the prior
            # implementation never attempted a correction after this denial.
            service.record_model_rejection(parent, node, entry, 'MACHINE_TRANSLATION_CHECK_FAILED')
        else:
            with store.transaction() as db:
                db.execute("UPDATE jobs SET state='completed' WHERE job_id=?", (child['job_id'],))
        children[node['role']] = child['job_id']
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='failed',error_code='MACHINE_TRANSLATION_CHECK_FAILED' WHERE job_id=?", (ident,))
    assert len(server.calls) == 4
    before_tombstones = store.query('SELECT * FROM source_tombstones')
    rendered = []

    def owned_render(self, job, source_evidence, entries):
        assert set(entries) == {'post', 'parent'}
        # Exercise actual final proof checks, not just the resolver's result.
        for node in source_evidence['nodes']:
            self._model_proof(job, node, entries[node['role']])
        rendered.append(deepcopy(entries))
        output = folder / 'owned-render-only.png'
        output.write_bytes(b'Owned renderer boundary fixture; not an image acceptance')
        return {'render': {'png': str(output), 'sha256': sha256_file(output)}, 'models': deepcopy(entries)}

    monkeypatch.setattr(MachineTweetsV2, 'render_evidence', owned_render)
    return SimpleNamespace(settings=settings, store=store, server=server, model=model, service=RenderAcceptance(settings, store),
                           ident=ident, children=children, packet=packet, record=record, screenshot=screenshot,
                           nodes=checked['nodes'], rendered=rendered, tombstones=before_tombstones,
                           assess=ModelReceipts.assess)


def simulate_live_policy_in_memory(monkeypatch, fixture):
    """Control-flow simulation only; never alter a raw or database TLS field."""
    actual = fixture.assess

    def owned_policy_simulation(self, job, translation):
        proof = actual(self, job, translation)
        if proof['proof_integrity_passed'] and proof['separate_requests_verified']:
            proof = deepcopy(proof)
            proof['live_transport_verified'] = True
            proof['failures'] = [x for x in proof['failures'] if x != 'MODEL_CUSTOM_TRANSPORT_NOT_LIVE_VERIFIED']
        return proof

    monkeypatch.setattr(ModelReceipts, 'assess', owned_policy_simulation)


def unchanged_publication_boundary(fixture):
    d = fixture.store
    assert not d.query('SELECT * FROM publications')
    assert not d.query('SELECT * FROM publication_reviews')
    assert not d.query("SELECT * FROM jobs WHERE source_platform='machine_web' OR job_type='tweet_machine_publish'")
    assert d.query('SELECT * FROM source_tombstones') == fixture.tombstones
    assert all(row['transport_mode'] == 'custom_transport' for row in d.query('SELECT * FROM model_call_receipts'))
    for row in d.query('SELECT * FROM model_call_receipts'):
        capture = json.loads(ModelReceipts(d)._file(row['job_id'], row['call_id']).read_text('utf-8'))
        # The archived provider response never contains a test-made live claim.
        assert capture.get('live_transport_verified') is not True


@pytest.mark.parametrize('change', ['platform', 'job_type', 'completed', 'different_error', 'resumed', 'missing'])
def test_resume_accepts_only_original_failed_acceptance_metadata_before_model_work(owned_rejection, change):
    f = owned_rejection
    row = f.store.get(f.ident)
    with f.store.transaction() as db:
        if change == 'platform': db.execute("UPDATE jobs SET source_platform='x' WHERE job_id=?", (f.ident,))
        if change == 'job_type': db.execute("UPDATE jobs SET job_type='tweet_process' WHERE job_id=?", (f.ident,))
        if change == 'completed': db.execute("UPDATE jobs SET state='completed' WHERE job_id=?", (f.ident,))
        if change == 'different_error': db.execute("UPDATE jobs SET error_code='NETWORK_ERROR' WHERE job_id=?", (f.ident,))
        if change == 'resumed': db.execute('UPDATE jobs SET result=? WHERE job_id=?',
                                         (encode({**row['result'], 'acceptance_resume': {'already_attempted': True}}), f.ident))
    with pytest.raises(HubError, match='RENDER_ACCEPTANCE_RESUME_FORBIDDEN'):
        f.service.resume_rejected('not-an-existing-job' if change == 'missing' else f.ident, f.model)
    assert len(f.server.calls) == 4 and not f.rendered
    unchanged_publication_boundary(f)


def test_valid_owned_qa_denial_is_integrity_evidence_but_custom_transport_cannot_resume(owned_rejection):
    f = owned_rejection
    failure = f.store.get(f.ident)['result']['machine_quality_failure']
    proof = f.assess(ModelReceipts(f.store), f.store.get(f.children['parent']), failure['translation'])
    assert proof['proof_integrity_passed'] and proof['separate_requests_verified']
    assert proof['qa_recommends_pass'] is False and proof['live_transport_verified'] is False
    with pytest.raises(HubError, match='RENDER_ACCEPTANCE_RESUME_FORBIDDEN'):
        f.service.resume_rejected(f.ident, f.model)
    assert len(f.server.calls) == 4 and 'acceptance_resume' not in f.store.get(f.ident)['result']
    unchanged_publication_boundary(f)


@pytest.mark.parametrize('change', ['raw_receipt', 'missing_receipt', 'cache', 'fake_translation', 'source_payload', 'screenshot'])
def test_fake_or_changed_resume_basis_stops_before_new_calls_even_in_policy_simulation(owned_rejection, monkeypatch, change):
    f = owned_rejection
    simulate_live_policy_in_memory(monkeypatch, f)
    quality = f.store.query("SELECT * FROM model_call_receipts WHERE job_id=? AND phase='quality'", (f.children['parent'],))[0]
    if change == 'raw_receipt':
        raw = ModelReceipts(f.store)._file(quality['job_id'], quality['call_id'])
        raw.write_bytes(raw.read_bytes() + b'\n')
    elif change == 'missing_receipt':
        ModelReceipts(f.store)._file(quality['job_id'], quality['call_id']).unlink()
    elif change == 'cache':
        with f.store.transaction() as db:
            db.execute('UPDATE model_requests SET response=? WHERE request_key=?',
                       (encode({'passed': False, 'issues': [{'source_span': '', 'translation_span': '', 'reason': 'forged'}]}), quality['request_key']))
    elif change == 'fake_translation':
        row = f.store.get(f.ident)
        row['result']['machine_quality_failure']['translation'] = 'unrecorded fake translation20:00'
        with f.store.transaction() as db:
            db.execute('UPDATE jobs SET result=? WHERE job_id=?', (encode(row['result']), f.ident))
    elif change == 'source_payload':
        record = deepcopy(f.record)
        record['payload']['post']['text'] = 'Changed source20:00'
        f.packet.write_text(encode(record), 'utf-8')
    else:
        f.screenshot.write_bytes(b'Changed owned source bytes')
    with pytest.raises(HubError):
        f.service.resume_rejected(f.ident, f.model)
    assert len(f.server.calls) == 4 and not f.rendered
    assert 'acceptance_resume' not in f.store.get(f.ident)['result']
    assert not f.store.query("SELECT * FROM jobs WHERE source_id LIKE '%:revision-1'")
    # Corrupted/missing receipts are intentionally retained for this negative.
    assert not f.store.query('SELECT * FROM publications')
    assert f.store.query('SELECT * FROM source_tombstones') == f.tombstones


@pytest.mark.parametrize('owned_rejection', [True], indirect=True)
def test_mutable_failure_cannot_replace_an_actual_qa_pass_as_revision_basis(owned_rejection, monkeypatch):
    f = owned_rejection
    simulate_live_policy_in_memory(monkeypatch, f)
    row = f.store.get(f.ident)
    row['result']['machine_quality_failure']['quality']['independent_model_advice'] = deepcopy(QA_DENIAL)
    with f.store.transaction() as db:
        db.execute('UPDATE jobs SET result=? WHERE job_id=?', (encode(row['result']), f.ident))
    with pytest.raises(HubError, match='MODEL_REVISION_REJECTION_REQUIRED'):
        f.service.resume_rejected(f.ident, f.model)
    assert len(f.server.calls) == 4 and not f.rendered
    unchanged_publication_boundary(f)


@pytest.mark.parametrize('revision_passed', [True, False])
def test_shared_resume_reuses_post_cache_and_only_revises_parent_once_in_policy_simulation(owned_rejection, monkeypatch, revision_passed):
    f = owned_rejection
    original = f.store.get(f.ident)
    old_receipts = f.store.query('SELECT * FROM model_call_receipts ORDER BY request_key')
    original_raw = {r['call_id']: ModelReceipts(f.store)._file(r['job_id'], r['call_id']).read_bytes() for r in old_receipts}
    simulate_live_policy_in_memory(monkeypatch, f)
    f.server.text = '架空原推修订20:00'
    f.server.qa = {'passed': True, 'issues': []} if revision_passed else deepcopy(QA_DENIAL)
    if revision_passed:
        result = f.service.resume_rejected(f.ident, f.model)
        assert result['publication_authorized'] is False and result['nodes'] == 2
        assert len(f.rendered) == 1
        entries = f.rendered[0]
        assert entries['post']['job_id'] == f.children['post'] and entries['post'].get('revision_index', 0) == 0
        assert entries['parent']['revision_index'] == 1 and entries['parent']['previous_model_job_id'] == f.children['parent']
        assert f.store.get(f.ident)['state'] == 'completed'
    else:
        with pytest.raises(HubError, match='MACHINE_TRANSLATION_CHECK_FAILED'):
            f.service.resume_rejected(f.ident, f.model)
        assert f.store.get(f.ident)['state'] == 'failed' and not f.rendered
    current = f.store.get(f.ident)
    assert current['payload'] == original['payload'] and current['policy_version'] == original['policy_version']
    assert current['source_platform'] == 'acceptance' and current['job_type'] == 'machine_render_acceptance'
    marker = current['result']['acceptance_resume']
    assert marker['original_rejection'] == original['result']['machine_quality_failure']
    assert marker['publication_authorized'] is False and marker['new_source_capture'] is False
    assert len(f.server.calls) == 6
    revised = f.store.query("SELECT * FROM jobs WHERE source_id LIKE '%:revision-1'")
    assert len(revised) == 1 and revised[0]['source_id'] == f.ident + ':parent:revision-1'
    assert len(f.store.query('SELECT * FROM model_call_receipts')) == 6
    for row in old_receipts:
        assert ModelReceipts(f.store)._file(row['job_id'], row['call_id']).read_bytes() == original_raw[row['call_id']]
    translation_wire = json.loads(json.loads(f.server.calls[4].content)['messages'][1]['content'])
    quality_wire = json.loads(json.loads(f.server.calls[5].content)['messages'][1]['content'])
    assert translation_wire['translation_revision']['quality_issues'] == QA_DENIAL['issues']
    assert 'translation_revision' not in quality_wire and 'previous_translation' not in encode(quality_wire)
    with pytest.raises(HubError, match='RENDER_ACCEPTANCE_RESUME_FORBIDDEN'):
        f.service.resume_rejected(f.ident, f.model)
    assert len(f.server.calls) == 6
    unchanged_publication_boundary(f)
