"""Owned MockTransport node/version/cache tests; no real API or publication.

The policy test receipt adapter marks its in-memory custom evidence as simulated
live only to exercise resolve_node. Persisted receipts retain custom_transport.
"""
import uuid
import time

import pytest

from adapters.model import ModelAPI
from services.hub.db import Store,encode
from services.hub.errors import HubError
from services.hub.machine_tweets_api import MachineTweetsAPI,SCOPE,VERSION
from services.hub.machine_tweets_v2 import MachineTweetsV2
from services.hub.model_revision import build_revision_context
from test_machine_single_revision import revision_policy
from test_machine_tweets_v2 import extended
from test_model_receipts import model_fixture

V5='model-text-contract-v5'
V6='model-text-contract-v6'


class PreviousAPI(MachineTweetsAPI):
    """Explicit historical default, not an edit to any persisted model node."""
    model_contract_version=V5


@pytest.fixture
def api_nodes(revision_policy):
    c=revision_policy
    policy=c.service.policy()
    policy.update(version=VERSION,supported_content=SCOPE,source_authority='x-v2-official')
    (c.settings.root/'config/machine-tweet-policy-api-v1.json').write_text(encode(policy),'utf-8')
    c.settings.config.update(policy_version=VERSION,x_api_enabled=True,x_web_enabled=False)
    parent_id=c.store.enqueue('x',c.parent['source_id'],'tweet_process','tweet',c.parent['payload'],VERSION)
    with c.store.transaction() as db:
        db.execute("UPDATE jobs SET state='processing',lease_owner=?,lease_until=? WHERE job_id=?",
            (str(uuid.uuid4()),time.time()+300,parent_id))
    c.parent=c.store.get(parent_id)
    c.service=PreviousAPI(c.settings,c.store)
    c.rejected_roles=set()
    return c


def test_class_hook_keeps_dom_v5_and_new_api_v6_defaults():
    assert MachineTweetsV2.model_contract_version==V5
    assert MachineTweetsAPI.model_contract_version==V6


def test_existing_unpaid_v5_api_node_is_not_rewritten_by_new_default(api_nodes):
    c=api_nodes
    old=c.service.model_node(c.parent,c.node)
    assert old['result']['translation_contract_version']==V5
    latest=MachineTweetsAPI(c.settings,c.store)
    assert latest.model_node(c.parent,c.node)==old
    assert c.store.get(old['job_id'])==old
    assert not c.server.calls and not c.store.query('SELECT * FROM model_call_receipts')


def test_paid_v5_pair_resolves_after_store_and_profile_restart_without_new_calls(api_nodes,monkeypatch):
    c=api_nodes
    entry=c.service.resolve_node(c.parent,c.node,c.model)
    old=c.store.get(entry['job_id'])
    receipts=c.store.query('SELECT * FROM model_call_receipts WHERE job_id=? ORDER BY request_key',(old['job_id'],))
    ledger=c.store.query('SELECT * FROM api_calls ORDER BY call_id')
    raw={r['call_id']:c.real_model.receipts._file(old['job_id'],r['call_id']).read_bytes() for r in receipts}
    assert len(c.server.calls)==2 and {r['contract_version'] for r in receipts}=={V5}
    # Environment changes are for future nodes. Existing bound models, contract
    # and output limit must still select exactly the original request/cache.
    monkeypatch.setenv('MODEL_NAME','future-owned-translation')
    monkeypatch.setenv('QA_MODEL_NAME','future-owned-quality')
    monkeypatch.setenv('MODEL_MAX_OUTPUT_TOKENS','1024')
    reopened=Store(c.settings.db_path)
    c.real_model=ModelAPI(c.http,reopened,settings=c.settings)
    latest=MachineTweetsAPI(c.settings,reopened)
    again=latest.resolve_node(reopened.get(c.parent['job_id']),c.node,c.model)
    assert again==entry and len(c.server.calls)==2
    assert reopened.get(old['job_id'])['result']['translation_contract_version']==V5
    assert reopened.query('SELECT * FROM model_call_receipts WHERE job_id=? ORDER BY request_key',(old['job_id'],))==receipts
    assert reopened.query('SELECT * FROM api_calls ORDER BY call_id')==ledger
    assert all(c.real_model.receipts._file(old['job_id'],call).read_bytes()==body for call,body in raw.items())
    assert not reopened.query('SELECT * FROM publications')


def test_future_api_node_uses_v6_for_both_distinct_recorded_phases(api_nodes):
    c=api_nodes
    latest=MachineTweetsAPI(c.settings,c.store)
    node=latest.model_node(c.parent,c.node)
    assert node['result']['translation_contract_version']==V6
    entry=latest.resolve_node(c.parent,c.node,c.model)
    rows=c.store.query('SELECT * FROM model_call_receipts WHERE job_id=?',(entry['job_id'],))
    assert len(c.server.calls)==2 and {r['phase'] for r in rows}=={'translation','quality'}
    assert {r['contract_version'] for r in rows}=={V6}
    assert {r['transport_mode'] for r in rows}=={'custom_transport'}
    assert not c.store.get(entry['job_id'])['result']['human_review']
    assert not c.store.query('SELECT * FROM publications')


@pytest.mark.parametrize('historical_revision',[False,True])
def test_single_revision_slot_uses_hook_but_preserves_existing_v5_pin(api_nodes,historical_revision):
    c=api_nodes
    c.rejected_roles={'post'}
    if not historical_revision:c.service=MachineTweetsAPI(c.settings,c.store)
    base=c.service.model_node(c.parent,c.node)
    translation=c.model.invoke(base,'translation',c.node['source'])
    qa=c.model.invoke(base,'quality',c.node['source'],translation['translation'])
    assert qa['passed'] is False
    context=build_revision_context(c.store,base['job_id'],parent_job_id=c.parent['job_id'],role='post',source=c.node['source'])
    before=None
    if historical_revision:
        before=c.service.model_node(c.parent,c.node,context)
        assert before['result']['translation_contract_version']==V5
    latest=MachineTweetsAPI(c.settings,c.store)
    revision=latest.model_node(c.parent,c.node,context)
    assert revision['result']['translation_contract_version']==(V5 if historical_revision else V6)
    if before is not None:assert revision==before
    assert revision['payload']==base['payload'] and revision['source_id'].endswith(':post:revision-1')
    assert c.store.get(base['job_id'])['result']['translation_contract_version']==(V5 if historical_revision else V6)
    assert len(c.server.calls)==2 and not c.store.query('SELECT * FROM publications')


def test_latest_default_cannot_turn_an_existing_v5_denial_into_paid_v6_revision(api_nodes):
    c=api_nodes;c.rejected_roles={'post'}
    base=c.service.model_node(c.parent,c.node)
    value=c.model.invoke(base,'translation',c.node['source'])
    assert c.model.invoke(base,'quality',c.node['source'],value['translation'])['passed'] is False
    original_receipts=c.store.query('SELECT * FROM model_call_receipts ORDER BY request_key')
    original_calls=c.store.query('SELECT * FROM api_calls ORDER BY call_id')
    with pytest.raises(HubError):MachineTweetsAPI(c.settings,c.store).resolve_node(c.parent,c.node,c.model)
    assert len(c.server.calls)==2
    assert c.store.get(base['job_id'])['result']['translation_contract_version']==V5
    assert c.store.query('SELECT * FROM model_call_receipts ORDER BY request_key')==original_receipts
    assert c.store.query('SELECT * FROM api_calls ORDER BY call_id')==original_calls
    assert not c.store.query('SELECT * FROM publications')
