from concurrent.futures import ThreadPoolExecutor
import httpx
import pytest
from services.hub.budget import Budget
from services.hub.errors import HubError
from adapters.http import ProviderHTTP


def budget(runtime):
    settings, store = runtime
    limit = settings.config['budgets']['model']
    limit.update(daily_usd=1, monthly_usd=1, daily_calls=3, monthly_calls=3, max_call_usd=.25, paid_authorized=True)
    return Budget(store, settings.config['budgets'])


def test_concurrent_budget_never_overspends(runtime):
    b = budget(runtime)
    def attempt(_):
        try:
            return b.reserve('model')
        except HubError as exc:
            assert exc.code == 'API_BUDGET_EXCEEDED'
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(attempt, range(20)))
    assert sum(x is not None for x in results) == 3
    assert b.report()['daily'][0]['reserved_upper_bound_usd'] == .75


def test_default_spend_denied_before_network(runtime):
    settings, store = runtime
    called = []
    client = ProviderHTTP(Budget(store, settings.config['budgets']), httpx.MockTransport(lambda r: called.append(r)))
    with pytest.raises(HubError, match='BILLABLE_COST_BOUND_REQUIRED'):
        client.request('model', 'POST', 'https://api.example.test')
    assert called == []


@pytest.mark.parametrize('status,code,retry', [(401,'AUTH_EXPIRED',False),(402,'INSUFFICIENT_BALANCE',False),
    (403,'PERMISSION_DENIED',False),(429,'API_RATE_LIMITED',True),(503,'API_SERVER_ERROR',True),(302,'API_REQUEST_REJECTED',False)])
def test_provider_errors_classified(runtime, status, code, retry):
    b = budget(runtime)
    client = ProviderHTTP(b, httpx.MockTransport(lambda req: httpx.Response(status, json={}, headers={'Retry-After':'120'})))
    with pytest.raises(HubError) as err:
        client.request('model', 'POST', 'https://api.example.test')
    assert err.value.code == code and err.value.retryable == retry
    assert runtime[1].query('SELECT state,cost_usd FROM api_calls')[0] == {'state':'error','cost_usd':.25}


def test_network_timeout_keeps_budget_reservation(runtime):
    def timeout(req):
        raise httpx.ReadTimeout('secret-must-not-be-logged')
    b = budget(runtime)
    client = ProviderHTTP(b, httpx.MockTransport(timeout))
    with pytest.raises(HubError, match='NETWORK_ERROR'):
        client.request('model', 'POST', 'https://api.example.test')
    assert runtime[1].query('SELECT state,cost_usd FROM api_calls')[0] == {'state':'unknown','cost_usd':.25}


def test_explicit_unlimited_model_authorization_is_recorded(runtime):
    settings,store=runtime
    settings.config['budgets']['model'].update(unlimited=True,paid_authorized=True)
    b=Budget(store,settings.config['budgets'])
    assert b.reserve('model')
    assert b.report()['model_budget_unlimited'] is True
    assert b.report()['daily'][0]['unknown_cost_calls'] == 1
