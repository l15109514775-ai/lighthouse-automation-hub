"""Real SQLite/recorded MockTransport; never live source or publishing proof."""
from copy import deepcopy
import json
import time
import uuid

import httpx
import pytest

from adapters.http import ProviderHTTP
from services.hub.budget import Budget
from services.hub.db import encode
from services.hub.errors import HubError
from scripts.ops import capture_two_reference_sources_v2 as m


def grant_value():
    now = time.time()
    return {'version': m.GRANT_VERSION, 'grant_id': str(uuid.uuid4()), 'source_ids': sorted(m.TARGETS),
        'target_site_uid': m.SITE_UID, 'author_id': m.AUTHOR_ID, 'author_handle': m.HANDLE,
        'owner_response': 'Owned fixture authorization for exactly two sources; no real owner approval',
        'owner_response_reference': 'owned-fixture-reference',
        'source_urls': ['https://x.com/'+m.HANDLE+'/status/'+ident for ident in sorted(m.TARGETS)],
        'issued_at': now-1, 'expires_at': now+300, 'live_hub_sha256': m.LIVE_HUB_SHA, 'scopes': m.SCOPES,
        'max_timeline_requests': 2, 'max_detail_requests': 3, 'max_user_requests': 1,
        'max_publications_per_source': 1, 'human_review': False}


@pytest.fixture
def owned(runtime, monkeypatch):
    s, d = runtime;s.config['target_bilibili_site_uid'] = m.SITE_UID
    s.config['budgets']['x'].update(paid_authorized=True, max_call_usd=.05, daily_usd=.5, monthly_usd=5,
        daily_calls=100, monthly_calls=1000)
    value = grant_value();ref = {'file': str(s.root/'config/oneoff-reference-backfill-v2/grant.json'), 'sha256': 'a'*64, 'bytes': 111}
    # Isolate the root-owned file boundary; real schema/expiry checks still run.
    def check(self, require_live=True):
        m.validate_grant(value, require_live=require_live)
        return deepcopy(value), deepcopy(ref)
    monkeypatch.setattr(m.RootGrant, 'check', check)
    grant = m.RootGrant.__new__(m.RootGrant);grant.settings = s;grant.value = value;grant.ref = ref
    monkeypatch.setenv('X_BEARER_TOKEN', 'owned-nonsecret-fake-token')
    calls = []
    author = {'id': m.AUTHOR_ID, 'username': m.HANDLE, 'name': 'Owned fixture author'}
    posts = [{'id': ident, 'author_id': m.AUTHOR_ID, 'text': 'Owned source fixture '+ident,
        'created_at': '2026-11-01T11:00:00Z', 'edit_history_tweet_ids': [ident],
        'referenced_tweets': [{'type': kind, 'id': target} for kind, target in refs]} for ident, refs in m.TARGETS.items()]
    def handler(request):
        calls.append(request.url.path)
        if '/by/username/' in request.url.path:
            return httpx.Response(200, json={'data': author})
        if request.url.path.endswith('/tweets'):
            return httpx.Response(200, json={'data': posts, 'meta': {'result_count': 2}})
        ident = request.url.path.rsplit('/', 1)[1]
        return httpx.Response(200, json={'data': {'id': ident, 'author_id': '12345', 'text': 'Owned referenced fixture',
            'created_at': '2026-11-01T10:00:00Z', 'edit_history_tweet_ids': [ident]},
            'includes': {'users': [{'id': '12345', 'username': 'owned_fixture', 'name': 'Owned context author'}]}})
    transport = httpx.MockTransport(handler)
    client = ProviderHTTP(Budget(d, s.config['budgets']), transport=transport)
    return s, d, grant, client, calls, posts


@pytest.mark.parametrize('change', ['expired', 'source', 'human', 'calls'])
def test_root_grant_cannot_expand_target_review_deadline_or_requests(change):
    value = grant_value()
    if change == 'expired':value['expires_at'] = time.time()-1
    elif change == 'source':value['source_ids'].append('123')
    elif change == 'human':value['human_review'] = True
    else:value['max_timeline_requests'] = 3
    with pytest.raises(HubError):m.validate_grant(value)


def test_real_recorded_capture_has_same_owner_receipts_original_time_and_no_models_or_publish(owned):
    s, d, grant, client, calls, posts = owned
    d.query('SELECT * FROM source_cursors')
    before = m.normal_cursors(d)
    result = m.capture(s, d, grant, http=client)
    assert result['state'] == 'sources_captured_no_import_or_publication'
    assert result['http_attempts'] == len(calls) == 5  #1 real owned user,1 timeline,3 details.
    assert result['all_transport_default_tls'] is False  # MockTransport never qualifies as live.
    descriptor = m.validate_descriptor(s, d, result['capture_descriptor'], grant_ref=grant.ref)
    assert {ident: item['original_post']['created_at'] for ident, item in descriptor['targets'].items()} == {p['id']: p['created_at'] for p in posts}
    capture_job = d.get(result['capture_job_id'])
    assert capture_job['state'] == 'upload_pending' and capture_job['attempts'] == 1 and capture_job['lease_owner'] is None
    assert len(d.query('SELECT * FROM api_calls')) == len(d.query('SELECT * FROM source_http_receipts')) == 5
    assert len(d.query('SELECT * FROM api_resource_usage')) == 5
    assert not d.query("SELECT * FROM jobs WHERE source_platform IN ('x','machine_model')")
    assert not d.query('SELECT * FROM publications') and m.normal_cursors(d) == before
    again = m.capture(s, d, grant, http=client)
    assert again['state'] == 'existing_verified_capture' and len(calls) == 5
    assert d.get(capture_job['job_id'])['attempts'] == 1


def test_tombstone_blocks_before_any_claim_or_paid_capture(owned):
    s, d, grant, client, calls, _ = owned
    evidence_job = d.enqueue('owned_fixture', 'cleared-source-evidence', 'owned_fixture', 'tweet', {}, 'owned-fixture-v1')
    with d.transaction() as db:
        db.execute('INSERT INTO source_tombstones VALUES(?,?,?,?,?,?)', ('x', next(iter(m.TARGETS)), 'OWNER_BACKLOG_CLEARED', time.time(), evidence_job, 'owner_clear_all'))
    with pytest.raises(HubError, match='OWNER_BACKLOG_CLEARED'):m.capture(s, d, grant, http=client)
    assert not calls and not d.query('SELECT * FROM api_calls') and not d.query("SELECT * FROM jobs WHERE source_platform='owner_backfill'")


def test_tampered_descriptor_raw_hash_cannot_qualify(owned):
    s, d, grant, client, calls, _ = owned
    result = m.capture(s, d, grant, http=client)
    ref = result['capture_descriptor'];value = m.strict_json(__import__('pathlib').Path(ref['file']).read_bytes())
    ident = next(iter(value['receipt_raw_sha256']));value['receipt_raw_sha256'][ident] = '0'*64
    file = __import__('pathlib').Path(ref['file']);raw = (encode(value)+'\n').encode();file.write_bytes(raw)
    changed = {'file': str(file), 'bytes': len(raw), 'sha256': __import__('hashlib').sha256(raw).hexdigest()}
    with pytest.raises(HubError, match='RAW_HASH_CHANGED'):m.validate_descriptor(s, d, changed, grant_ref=grant.ref)


def test_unknown_http_intents_are_persisted_and_never_reset_when_request_bound_exhausts(owned):
    s, d, grant, client, calls, _ = owned
    class Broken:
        def request_recorded(self, *args, **kwargs):raise HubError('NETWORK_ERROR', retryable=True)
    with pytest.raises(HubError, match='NETWORK_ERROR'):m.capture(s, d, grant, http=Broken())
    job = d.query("SELECT job_id FROM jobs WHERE source_platform='owner_backfill'")[0]
    first = d.get(job['job_id'])
    assert first['attempts'] == 1 and first['result']['capture']['requests'][0]['state'] == 'reserved_result_unknown'
    with pytest.raises(HubError, match='REQUEST_BOUND_REACHED'):m.capture(s, d, grant, http=Broken())
    second = d.get(job['job_id'])
    assert second['attempts'] == 2 and second['result']['capture']['requests'] == first['result']['capture']['requests']
    assert not calls and not d.query('SELECT * FROM publications')
