"""Owned MockTransport only: X resource upper bounds are not invoice claims."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import math
import httpx
import pytest

from adapters.http import ProviderHTTP
from services.hub.budget import Budget
from services.hub.db import Store
from services.hub.errors import HubError


def configured(runtime, **overrides):
    settings, store = runtime
    limits = deepcopy(settings.config['budgets'])
    limits['x'].update(daily_usd=1, monthly_usd=2, daily_calls=100,
        monthly_calls=1000, max_call_usd=.10, paid_authorized=True)
    limits['x'].update(overrides)
    return Budget(store, limits)


def client_with_calls(budget, response=None):
    calls = []
    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={'data': []}) if response is None else response(request)
    return ProviderHTTP(budget, httpx.MockTransport(transport)), calls


def request(client, cost, **kwargs):
    kwargs.setdefault('params', {'max_results': 10})
    return client.request('x', 'GET', 'https://api.x.com/2/users/123/tweets',
        reserved_cost_usd=cost, **kwargs)


def test_x_requires_resource_reservation_and_keeps_default_zero_closed(runtime):
    b = configured(runtime)
    client, calls = client_with_calls(b)
    with pytest.raises(HubError, match='X_RESOURCE_COST_BOUND_REQUIRED'):
        request(client, None)
    settings, store = runtime
    default, _ = client_with_calls(Budget(store, settings.config['budgets']))
    with pytest.raises(HubError, match='BILLABLE_COST_BOUND_REQUIRED'):
        request(default, .05)
    assert not calls and not store.query('SELECT * FROM api_calls')


@pytest.mark.parametrize('cost', [0, -.005, math.nan, math.inf, -math.inf, True, False, '.005', [], {}])
def test_invalid_x_resource_cost_never_reserves_or_calls_network(runtime, cost):
    b = configured(runtime)
    client, calls = client_with_calls(b)
    with pytest.raises(HubError, match='X_RESOURCE_COST_BOUND_INVALID'):
        request(client, cost)
    assert not calls and not b.store.query('SELECT * FROM api_calls')


@pytest.mark.parametrize('units', [0, 2, 10, True, 1.0, '1', None])
def test_x_units_count_one_http_attempt_instead_of_returned_resources(runtime, units):
    b = configured(runtime)
    client, calls = client_with_calls(b)
    with pytest.raises(HubError, match='X_HTTP_ATTEMPT_UNIT_REQUIRED'):
        request(client, .05, units=units)
    assert not calls and not b.store.query('SELECT * FROM api_calls')


def test_user_timeline_and_parent_resource_prices_are_distinct_persisted_bounds(runtime):
    b = configured(runtime)
    def response(req):
        if '/by/username/' in req.url.path:
            return httpx.Response(200, json={'data': {'id': '123'}})
        if req.url.path.endswith('/tweets'):
            return httpx.Response(200, json={'data': [{'id': str(i)} for i in range(1, 11)]})
        return httpx.Response(200, json={'data': {'id': '456'}, 'includes': {'users': [{'id': '123'}]}})
    client, calls = client_with_calls(b, response)
    # Bounds supplied by the adapter: user1, posts10, then parent1+author1.
    client.request('x', 'GET', 'https://api.x.com/2/users/by/username/fixture', reserved_cost_usd=.010)
    request(client, .050)
    client.request('x', 'GET', 'https://api.x.com/2/tweets/456',
        params={'expansions': 'author_id,attachments.media_keys'}, reserved_cost_usd=.015)
    rows = b.store.query('SELECT units,cost_usd,state FROM api_calls ORDER BY started_at')
    assert [r['cost_usd'] for r in rows] == [.010, .050, .015]
    assert all(r['units'] == 1 and r['state'] == 'done' for r in rows)
    report = next(r for r in b.report()['daily'] if r['provider'] == 'x')
    assert report['attempts'] == report['quota_units'] == len(calls) == 3
    assert report['reserved_upper_bound_usd'] == pytest.approx(.075)
    assert 'not an invoice' in b.report()['x_cost_note']
    # A database reopen preserves real counts and the original maximum, not an invoice claim.
    reopened = Budget(Store(b.store.path), b.limits)
    assert reopened.report()['daily'][0]['reserved_upper_bound_usd'] == pytest.approx(.075)


@pytest.mark.parametrize('change,code', [({'max_call_usd': .04}, 'API_CALL_BUDGET_EXCEEDED'),
    ({'paid_authorized': False}, 'PAID_SERVICE_NOT_AUTHORIZED'),
    ({'unlimited': True}, 'UNLIMITED_BUDGET_NOT_AUTHORIZED')])
def test_x_configuration_still_controls_per_request_cost_and_paid_permission(runtime, change, code):
    b = configured(runtime)
    b.limits['x'].update(change)
    client, calls = client_with_calls(b)
    with pytest.raises(HubError, match=code):
        request(client, .05)
    assert not calls and not b.store.query('SELECT * FROM api_calls')


@pytest.mark.parametrize('period', ['daily', 'monthly'])
@pytest.mark.parametrize('ceiling_kind', ['usd', 'calls'])
def test_x_daily_monthly_money_and_request_ceilings_stop_before_second_network_call(runtime, period, ceiling_kind):
    b = configured(runtime)
    b.limits['x'][period + '_' + ceiling_kind] = .06 if ceiling_kind == 'usd' else 1
    client, calls = client_with_calls(b, lambda r: httpx.Response(200, json={'data': [{'id': str(i)} for i in range(1, 11)]}))
    request(client, .05)
    with pytest.raises(HubError, match='API_BUDGET_EXCEEDED'):
        request(client, .05)
    assert len(calls) == 1 and len(b.store.query('SELECT * FROM api_calls')) == 1


@pytest.mark.parametrize('field,value', [('daily_usd', math.nan), ('monthly_usd', math.inf),
    ('daily_usd', True), ('monthly_calls', math.inf), ('daily_calls', 1.5), ('monthly_calls', -1)])
def test_invalid_x_caps_cannot_disable_comparisons(runtime, field, value):
    b = configured(runtime)
    b.limits['x'][field] = value
    client, calls = client_with_calls(b)
    with pytest.raises(HubError, match='INVALID_BUDGET'):
        request(client, .05)
    assert not calls and not b.store.query('SELECT * FROM api_calls')


def test_x_concurrent_requests_never_overspend_resource_cost_envelope(runtime):
    b = configured(runtime)
    b.limits['x']['daily_usd'] = .20
    def attempt(_):
        try:
            return b.reserve('x', reserved_cost_usd=.05,
                x_resource_bounds={'endpoint_kind': 'timeline', 'posts': 10, 'users': 0})
        except HubError as exc:
            assert exc.code == 'API_BUDGET_EXCEEDED'
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(attempt, range(20)))
    assert sum(value is not None for value in results) == 4
    row = b.store.query('SELECT COUNT(*) n,SUM(cost_usd) cost,SUM(units) calls FROM api_calls')[0]
    assert row['n'] == row['calls'] == 4 and row['cost'] == pytest.approx(.20)


@pytest.mark.parametrize('status,code,retry', [(401, 'AUTH_EXPIRED', False),
    (402, 'INSUFFICIENT_BALANCE', False), (403, 'PERMISSION_DENIED', False),
    (429, 'API_RATE_LIMITED', True), (503, 'API_SERVER_ERROR', True)])
def test_x_provider_failures_are_distinct_and_full_reservation_stays_charged(runtime, status, code, retry):
    b = configured(runtime)
    client, _ = client_with_calls(b, lambda r: httpx.Response(status, json={}, headers={'Retry-After': '120'}))
    with pytest.raises(HubError) as exc:
        request(client, .05)
    assert exc.value.code == code and exc.value.retryable is retry
    if status == 429:
        assert exc.value.retry_after == 120
    assert b.store.query('SELECT state,error_code,cost_usd,units FROM api_calls')[0] == {
        'state': 'error', 'error_code': code, 'cost_usd': .05, 'units': 1}


def test_x_transport_unknown_is_charged_after_reopen_and_can_stop_next_attempt(runtime):
    b = configured(runtime)
    b.limits['x']['daily_usd'] = .05
    def transport(request):
        raise httpx.ReadTimeout('owned-test-secret-must-not-be-stored')
    client, calls = client_with_calls(b, transport)
    with pytest.raises(HubError, match='NETWORK_ERROR'):
        request(client, .05)
    rows = b.store.query('SELECT state,error_code,cost_usd FROM api_calls')
    assert rows == [{'state': 'unknown', 'error_code': 'NETWORK_ERROR', 'cost_usd': .05}]
    reopened = Budget(Store(b.store.path), b.limits)
    next_client, second_calls = client_with_calls(reopened)
    with pytest.raises(HubError, match='API_BUDGET_EXCEEDED'):
        next_client.request('x', 'GET', 'https://api.x.com/2/users/by/username/fixture', reserved_cost_usd=.01)
    assert len(calls) == 1 and not second_calls
    assert 'owned-test-secret' not in repr(b.store.query('SELECT * FROM api_calls'))


def test_x_response_validation_error_retains_bound_and_has_no_source_receipt(runtime):
    b = configured(runtime)
    client, calls = client_with_calls(b)
    def reject(value):
        raise HubError('X_RESOURCE_RESPONSE_BOUND_EXCEEDED')
    with pytest.raises(HubError, match='X_RESOURCE_RESPONSE_BOUND_EXCEEDED'):
        request(client, .05, response_check=reject)
    assert len(calls) == 1
    assert b.store.query('SELECT state,cost_usd FROM api_calls') == [{'state': 'error', 'cost_usd': .05}]
    assert not b.store.query('SELECT * FROM source_http_receipts')


def test_recorded_parent_details_preserve_exact_ledger_time_and_upper_bound(runtime):
    settings, store = runtime
    ident = store.enqueue('scheduler', 'owned-x-billing', 'x_discovery', 'tweet', {}, settings.config['policy_version'])
    b = configured(runtime)
    client, calls = client_with_calls(b, lambda r: httpx.Response(200, json={'data': {'id': '456'}, 'includes': {'users': [{'id': '123'}]}}))
    checked = []
    value, receipt = client.request_recorded('x', 'GET', 'https://api.x.com/2/tweets/456',
        job_id=ident, headers={'Authorization': 'Bearer owned-synthetic-only'},
        params={'tweet.fields': 'created_at', 'expansions': 'author_id'}, reserved_cost_usd=.015,
        response_check=lambda data: checked.append(data))
    row = store.query('SELECT * FROM api_calls')[0]
    assert checked == [value] and len(calls) == 1 and row['cost_usd'] == .015
    assert receipt['captured_at'] == row['finished_at']
    assert receipt['cost_reservation']['reserved_upper_bound_usd'] == .015
    assert receipt['cost_reservation']['http_attempt_units'] == 1
    assert receipt['cost_reservation']['actual_resource_charge_verified'] is False
    assert receipt['cost_reservation']['returned_posts'] == receipt['cost_reservation']['returned_users'] == 1
    assert receipt['cost_reservation']['pricing_source']['estimate_only'] is True
    assert store.query('SELECT captured_at FROM source_http_receipts')[0]['captured_at'] == row['finished_at']
    assert 'owned-synthetic-only' not in repr(store.query('SELECT * FROM source_http_receipts'))


@pytest.mark.parametrize('stamp', ['2026-10-31T12:00:00Z', '2026-10-31T12:00:00.123Z'])
def test_recorded_timeline_accepts_exact_past_utc_start_time(runtime, stamp):
    settings, store = runtime
    ident = store.enqueue('scheduler', 'owned-start-time-' + stamp, 'x_discovery', 'tweet', {}, settings.config['policy_version'])
    client, calls = client_with_calls(configured(runtime))
    client.request_recorded('x', 'GET', 'https://api.x.com/2/users/123/tweets', job_id=ident,
        headers={'Authorization': 'Bearer synthetic'}, params={'start_time': stamp, 'max_results': 10}, reserved_cost_usd=.05)
    assert len(calls) == 1 and calls[0].url.params['start_time'] == stamp


@pytest.mark.parametrize('stamp', ['not-a-date', '2026-10-31', '2026-10-31T12:00:00',
    '2026-10-31T12:00:00+08:00', '2099-01-01T00:00:00Z', '2026-02-30T00:00:00Z', None, True])
def test_invalid_recorded_start_time_stops_before_reservation(runtime, stamp):
    b = configured(runtime)
    client, calls = client_with_calls(b)
    with pytest.raises(HubError, match='SOURCE_REQUEST_NOT_SUPPORTED'):
        client.request_recorded('x', 'GET', 'https://api.x.com/2/users/123/tweets', job_id='owned',
            headers={'Authorization': 'Bearer synthetic'}, params={'start_time': stamp}, reserved_cost_usd=.05)
    assert not calls and not b.store.query('SELECT * FROM api_calls')


@pytest.mark.parametrize('url', ['https://api.x.com.attacker.test/2/tweets/456',
    'https://api.x.com/2/tweets/456?access_token=synthetic', 'https://api.x.com/2/tweets/456#fragment',
    'https://api.x.com:443/2/tweets/456', 'https://api.x.com/2/tweets', 'http://api.x.com/2/tweets/456'])
def test_recorded_details_never_expand_domain_or_secret_url_surface(runtime, url):
    b = configured(runtime)
    client, calls = client_with_calls(b)
    with pytest.raises(HubError, match='SOURCE_REQUEST_NOT_SUPPORTED'):
        client.request_recorded('x', 'GET', url, job_id='owned', headers={'Authorization': 'Bearer synthetic'}, reserved_cost_usd=.015)
    assert not calls and not b.store.query('SELECT * FROM api_calls')


def test_x_override_does_not_change_authorized_unlimited_model_semantics(runtime):
    settings, store = runtime
    settings.config['budgets']['model'].update(unlimited=True, paid_authorized=True)
    b = Budget(store, settings.config['budgets'])
    assert b.reserve('model')
    with pytest.raises(HubError, match='RESOURCE_COST_OVERRIDE_NOT_SUPPORTED'):
        b.reserve('model', reserved_cost_usd=.01)
    assert len(store.query('SELECT * FROM api_calls')) == 1
    assert b.report()['model_budget_unlimited'] is True


def test_empty_success_settles_to_zero_resource_estimate_without_consuming_max_posts_forever(runtime):
    b = configured(runtime, daily_usd=.05)
    client, calls = client_with_calls(b)
    request(client, .05)
    request(client, .05)
    rows = b.store.query('SELECT cost_usd,units,state FROM api_calls')
    assert len(calls) == len(rows) == 2
    assert all(r == {'cost_usd': 0.0, 'units': 1, 'state': 'done'} for r in rows)
    usage = b.store.query('SELECT * FROM api_resource_usage')
    assert all(r['max_posts'] == 10 and r['returned_posts'] == r['returned_users'] == 0 and
        r['reserved_upper_bound_usd'] == .05 and r['settled_upper_bound_usd'] == 0 and r['state'] == 'estimated'
        for r in usage)
    assert all('invoice_charge_verified' in r['pricing_source'] for r in usage)


def test_success_counts_data_and_includes_and_does_not_assume_repeat_read_is_free(runtime):
    b = configured(runtime)
    body = {'data': [{'id': '456'}], 'includes': {'tweets': [{'id': '789'}]}}
    client, calls = client_with_calls(b, lambda r: httpx.Response(200, json=body))
    request(client, .05)
    request(client, .05)
    assert len(calls) == 2
    assert [r['cost_usd'] for r in b.store.query('SELECT cost_usd FROM api_calls')] == [.010, .010]
    assert [r['returned_posts'] for r in b.store.query('SELECT returned_posts FROM api_resource_usage')] == [2, 2]


@pytest.mark.parametrize('params,cost', [({'max_results': 10}, .005),
    ({'max_results': 100}, .05), ({'max_results': 10, 'expansions': 'author_id'}, .15),
    ({'max_results': 10, 'expansions': 'referenced_tweets.id'}, .20),
    ({'max_results': 10, 'ids': '1,2,3'}, .05), ({'max_results': 101}, 1),
    ({'max_results': True}, .05), ({}, .05)])
def test_http_independently_rejects_underpriced_or_unbounded_timeline_contracts(runtime, params, cost):
    b = configured(runtime)
    b.limits['x']['max_call_usd'] = 1
    client, calls = client_with_calls(b)
    with pytest.raises(HubError, match='X_RESOURCE_COST_BOUND_TOO_LOW|X_RESOURCE_REQUEST_NOT_SUPPORTED'):
        request(client, cost, params=params)
    assert not calls and not b.store.query('SELECT * FROM api_calls')


@pytest.mark.parametrize('body,code', [({'data': [{'id': str(i)} for i in range(11)]}, 'X_RESOURCE_RESPONSE_BOUND_EXCEEDED'),
    ({'data': [], 'includes': {'users': [{'id': '123'}]}}, 'X_RESOURCE_RESPONSE_BOUND_EXCEEDED'),
    ({'data': [], 'errors': [{'title': 'denied'}]}, 'X_RESOURCE_RESPONSE_INVALID'),
    ({'data': [], 'includes': {'tweets': 'invalid'}}, 'X_RESOURCE_RESPONSE_INVALID')])
def test_unknown_or_out_of_contract_response_cannot_discount_the_reservation(runtime, body, code):
    b = configured(runtime)
    client, calls = client_with_calls(b, lambda r: httpx.Response(200, json=body))
    with pytest.raises(HubError, match=code):
        request(client, .05)
    assert len(calls) == 1
    assert b.store.query('SELECT state,cost_usd FROM api_calls') == [{'state': 'error', 'cost_usd': .05}]
    assert b.store.query('SELECT state,settled_upper_bound_usd FROM api_resource_usage') == [
        {'state': 'error_upper_bound', 'settled_upper_bound_usd': None}]


def test_x_unvalidated_success_cannot_directly_lower_the_persisted_reservation(runtime):
    b = configured(runtime)
    call = b.reserve('x', reserved_cost_usd=.05,
        x_resource_bounds={'endpoint_kind': 'timeline', 'posts': 10, 'users': 0})
    with pytest.raises(HubError, match='X_RESOURCE_USAGE_CONFIRMATION_REQUIRED'):
        b.finish(call, 'done')
    assert b.store.query('SELECT state,cost_usd FROM api_calls') == [{'state': 'reserved', 'cost_usd': .05}]
    b.finish(call, 'unknown', 'CALL_INTERRUPTED')
    assert b.store.query('SELECT state,cost_usd FROM api_calls') == [{'state': 'unknown', 'cost_usd': .05}]
    with pytest.raises(HubError, match='X_RESOURCE_SETTLEMENT_ALREADY_FINALIZED'):
        b.finish(call, 'done', resource_counts={'posts': 0, 'users': 0})
    assert b.store.query('SELECT state,cost_usd FROM api_calls') == [{'state': 'unknown', 'cost_usd': .05}]


def test_post_response_interruption_restores_max_bound_instead_of_leaving_a_zero_estimate(runtime):
    b = configured(runtime)
    client, _ = client_with_calls(b)
    request(client, .05)
    call = b.store.query('SELECT call_id FROM api_calls')[0]['call_id']
    b.finish(call, 'unknown', 'CALL_INTERRUPTED')
    assert b.store.query('SELECT state,cost_usd FROM api_calls') == [{'state': 'unknown', 'cost_usd': .05}]
    assert b.store.query('SELECT state,settled_upper_bound_usd FROM api_resource_usage') == [
        {'state': 'unknown_upper_bound', 'settled_upper_bound_usd': None}]
