"""Owned Python routing fixtures; mocked intake never fabricates a source receipt."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time
from types import ModuleType, SimpleNamespace

import pytest

from adapters.compositor import compositor_input
from services.hub.config import Settings
from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.machine_routes import machine_service, MachineRoutes
from services.hub.machine_tweets import MachineTweets, VERSION as V1
from services.hub.machine_tweets_api import MachineTweetsAPI, VERSION as API, SCOPE as API_SCOPE
from services.hub.machine_tweets_v2 import MachineTweetsV2, VERSION as V2, SCOPE as DOM_SCOPE
from services.hub.pipelines import TweetPipeline
from services.hub.x_api_discovery import XAPIDiscovery


def policy(settings, version=API):
    return {'version': version, 'owner_confirmed_machine_mode': True, 'human_review_required': False,
        'target_site_uid': settings.config['target_bilibili_site_uid'],
        'x_handles': ['example_handle_a', 'example-channel-b'],
        'rights': {'owner_confirmed': True, 'actions': ['publish_translation', 'publish_image'],
                   'reference': 'Owned synthetic routing grant, not a real content review'},
        'owner_response': 'Owned routing fixture only',
        'authorization_basis': 'existing_owner_automation_and_content_grant',
        'max_post_age_seconds': 7200, 'activation_at': time.time() - 30,
        'supported_content': API_SCOPE if version == API else DOM_SCOPE,
        **({'source_authority': 'x-v2-official'} if version == API else {})}


@pytest.fixture
def official(runtime, monkeypatch):
    settings, store = runtime
    settings.config.update(policy_version=API, machine_tweet_enabled=True,
                           x_api_enabled=True, x_web_enabled=False, machine_source_generation=1)
    (settings.root / 'config').mkdir(exist_ok=True)
    (settings.root / 'config/machine-tweet-policy-api-v1.json').write_text(encode(policy(settings)), encoding='utf-8')
    ident = store.enqueue('scheduler', 'owned-api-routing-discovery', 'x_discovery', 'tweet', {}, API)
    discovery = store.claim('tweet', 180, 4)
    assert discovery['job_id'] == ident
    monkeypatch.setattr(MachineTweetsAPI, 'clear_backlog', lambda _: {'owned_mock_cleanup': True})
    return settings, store, discovery


def response(ids=(), *, records=(), complete=True, rejected=()):
    return {'version': 'x-official-discovery-v1', 'observed_post_ids': list(ids),
            'records': list(records), 'skipped_posts': [], 'rejected_posts': list(rejected),
            'catchup_complete': complete}


def service_with_owned_intake(settings, store):
    service = XAPIDiscovery(settings, store, None)
    captured = []
    def ingest(parent, record, job_policy):
        # Test only delegation/counting with a durable ordinary job. No
        # source_http_receipts or tweet_source_bindings are created or implied.
        payload = record['payload']
        prior = store.query("SELECT job_id FROM jobs WHERE source_platform='x' AND source_id=? AND policy_version=?",
                            (payload['post']['id'], job_policy))
        ident = store.enqueue('x', payload['post']['id'], 'tweet_process', 'tweet', payload, job_policy)
        captured.append((parent['job_id'], record, job_policy))
        return {'job_id': ident, 'created': not prior, 'binding_present': False}
    service.receipts = SimpleNamespace(cached_author=lambda _: None, ingest_official=ingest,
        record=lambda *_: pytest.fail('Owned mocked adapter must not claim a real HTTP capture'))
    # The routing-only seam now stands at durable capture registration. The
    # real deferred media/source acceptance is exercised in its own tests.
    service.capture_queue = SimpleNamespace(initialize=lambda:None,known_ids=lambda:[],register=ingest)
    return service, captured


def source(post_id='911', handle='example-channel-b'):
    return {'post': {'id': post_id, 'text': 'Owned API routing source',
                     'created_at': datetime.now(timezone.utc).isoformat(), 'referenced_tweets': []},
            'author': {'name': 'Owned routing author', 'username': handle},
            'url': f'https://x.com/{handle}/status/{post_id}', 'source_api': 'x-v2-official',
            'capture': {'human_review': False, 'official_api_receipt': False}}


def test_two_sources_continue_independently_and_first_api_failure_never_becomes_browser(official, monkeypatch):
    settings, store, job = official
    service, delegated = service_with_owned_intake(settings, store)
    calls = []
    def latest(handle, **kwargs):
        calls.append(handle)
        if handle == 'example_handle_a':
            raise HubError('PERMISSION_DENIED')
        return response(['911'], records=[{'payload': source()}])
    service.x.latest_official = latest
    with pytest.raises(HubError, match='PERMISSION_DENIED'):
        service.run(job)
    assert calls == ['example_handle_a', 'example-channel-b'] and len(delegated) == 1
    result = store.get(job['job_id'])['result']
    assert [row['state'] for row in result['sources']] == ['blocked', 'observed']
    assert result['new_jobs'] == 1 and result['browser_fallback'] is False
    polls = {row['source_key']: row for row in store.query('SELECT * FROM source_polls')}
    assert polls['x-api:example_handle_a']['last_success_at'] is None
    assert polls['x-api:example-channel-b']['last_success_at'] is not None
    assert not store.query('SELECT * FROM api_calls') and not store.query('SELECT * FROM source_http_receipts')


def test_partial_first_source_keeps_its_cursor_but_second_source_can_advance(official):
    settings, store, job = official
    with store.transaction() as db:
        db.execute('INSERT INTO source_cursors VALUES(?,?)', ('x-api:example_handle_a', '100'))
    service, _ = service_with_owned_intake(settings, store)
    seen = []
    def latest(handle, **kwargs):
        seen.append((handle, kwargs['since_id']))
        return response(['109'], complete=False) if handle == 'example_handle_a' else response(['212', '211'])
    service.x.latest_official = latest
    with pytest.raises(HubError, match='X_CATCHUP_BOUND_REACHED') as error:
        service.run(job)
    assert error.value.retryable
    assert seen == [('example_handle_a', '100'), ('example-channel-b', None)]
    cursors = {row['key']: row['value'] for row in store.query('SELECT * FROM source_cursors')}
    assert cursors == {'x-api:example_handle_a': '100', 'x-api:example-channel-b': '212'}
    assert store.get(job['job_id'])['result']['sources'][0]['catchup_complete'] is False


def test_global_publications_and_tombstones_are_sent_as_known_ids_across_old_policies(official):
    settings, store, discovery = official
    originals = []
    for post_id, kind, state in [('301', 'dynamic_website_machine_v1', 'completed'),
                                 ('302', 'dynamic_website_machine_v2', 'unknown'),
                                 ('303', 'dynamic_website_supervised', 'dispatching'),
                                 ('304', 'dynamic_website_machine_v2', 'rejected')]:
        ident = store.enqueue('x', post_id, 'tweet_process', 'tweet', source(post_id), V2)
        with store.transaction() as db:
            db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                ('owned-' + post_id, ident, 'bilibili', 'x', post_id,
                 'site_uid:' + settings.config['target_bilibili_site_uid'], kind, state, None, None, time.time()))
        originals.append(store.get(ident))
    marked = store.enqueue('x', '305', 'tweet_process', 'tweet', source('305'), V2)
    with store.transaction() as db:
        db.execute('INSERT INTO source_tombstones VALUES(?,?,?,?,?,?)',
                   ('x', '305', 'OWNER_BACKLOG_CLEARED', time.time(), marked, 'owner_clear_all'))
    before_publications = store.query('SELECT * FROM publications ORDER BY idempotency_key')
    before_marks = store.query('SELECT * FROM source_tombstones')
    service, delegated = service_with_owned_intake(settings, store)
    seen = []
    def latest(handle, **kwargs):
        seen.append(set(kwargs['known_ids']))
        return response()
    service.x.latest_official = latest
    service.run(discovery)
    assert len(seen) == 2 and all({'301', '302', '303', '305'} <= ids for ids in seen)
    assert all('304' not in ids for ids in seen) and not delegated
    assert store.query('SELECT * FROM publications ORDER BY idempotency_key') == before_publications
    assert store.query('SELECT * FROM source_tombstones') == before_marks
    assert all(store.get(old['job_id']) == old for old in originals)


def test_rejected_content_is_recorded_without_blocking_other_source_or_manufacturing_receipts(official):
    settings, store, job = official
    service, delegated = service_with_owned_intake(settings, store)
    service.x.latest_official = lambda handle, **_: response(['401'], rejected=[{
        'post_id': '401', 'error_code': 'X_OFFICIAL_UNSUPPORTED_MEDIA'}]) if handle == 'example_handle_a' else response()
    result = service.run(job)
    assert result['new_jobs'] == 0 and not delegated
    assert [row['state'] for row in result['sources']] == ['observed', 'observed']
    assert store.query("SELECT * FROM events WHERE name='task.failed'")
    assert not store.query('SELECT * FROM source_http_receipts')


@pytest.mark.parametrize('enabled,web', [(False, False), (True, True)])
def test_direct_official_discovery_rejects_disabled_or_mixed_transport_before_any_adapter(official, enabled, web):
    settings, store, job = official
    settings.config.update(x_api_enabled=enabled, x_web_enabled=web)
    service, _ = service_with_owned_intake(settings, store)
    service.x.latest_official = lambda *a, **kw: pytest.fail('Transport guard must precede HTTP')
    with pytest.raises(HubError, match='X_OFFICIAL_TRANSPORT_REQUIRED'):
        service.run(job)


def test_pipeline_official_error_has_no_implicit_dom_or_legacy_api_fallback(official, monkeypatch):
    settings, store, job = official
    pipeline = TweetPipeline(settings, store, None)
    chosen = []
    def api_run(self, actual):
        chosen.append(actual['job_id'])
        raise HubError('AUTH_EXPIRED')
    monkeypatch.setattr(XAPIDiscovery, 'run', api_run)
    # The production API-only image deliberately removes the retired module.
    # An owned sentinel makes an accidental import/call observable in both that
    # image and the retained open-source tree, without restoring browser intake.
    retired = ModuleType('services.hub.x_web_discovery')
    class ForbiddenBrowser:
        def __init__(self, *args, **kwargs):
            pytest.fail('Browser fallback is forbidden')
    retired.XWebDiscovery = ForbiddenBrowser
    monkeypatch.setitem(sys.modules, retired.__name__, retired)
    pipeline.x.latest = lambda *a, **kw: pytest.fail('Legacy unauthoritative intake is not M3')
    with pytest.raises(HubError, match='AUTH_EXPIRED'):
        pipeline.discovery(job)
    assert chosen == [job['job_id']] and not store.query('SELECT * FROM api_calls')


def test_machine_route_uses_job_policy_and_api_source_does_not_accept_dom_payload(official):
    settings, store, job = official
    assert isinstance(machine_service(settings, store, API + ':source-2'), MachineTweetsAPI)
    dom = machine_service(settings, store, V2)
    assert type(dom) is MachineTweetsV2
    with pytest.raises(HubError, match='MACHINE_OFFICIAL_SOURCE_REQUIRED'):
        MachineTweetsAPI(settings, store).source({'payload': {'source_api': 'x-web-dom-v1'}})


def test_custom_api_receipts_stop_before_a_billable_model_phase(official, monkeypatch):
    settings, store, _ = official
    from services.hub.source_receipts import SourceReceipts
    monkeypatch.setattr(SourceReceipts, 'assess_official', lambda *a, **kw: {
        'proof': {'source_live_verified': False, 'human_review': False}, 'nodes': []})
    service = MachineTweetsAPI(settings, store)
    with pytest.raises(HubError, match='MACHINE_OFFICIAL_LIVE_SOURCE_REQUIRED'):
        service.source({'payload': source()})
    assert not store.query('SELECT * FROM api_calls')
    assert not store.query("SELECT * FROM jobs WHERE source_platform='machine_model'")


@pytest.mark.parametrize('old_version,old_class,kind', [
    (V1, MachineTweets, 'dynamic_website_machine_v1'),
    (V2, MachineTweetsV2, 'dynamic_website_machine_v2'),
])
def test_api_switch_keeps_existing_old_unknown_on_its_original_lookup_route(official, monkeypatch, old_version, old_class, kind):
    settings, store, _ = official
    parent = store.enqueue('x', '501', 'tweet_process', 'tweet', source('501'), old_version)
    with store.transaction() as db:
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('owned-legacy-unknown', parent, 'bilibili', 'x', '501',
             'site_uid:' + settings.config['target_bilibili_site_uid'], kind, 'unknown', None, None, time.time()))
    before = store.query('SELECT * FROM publications')
    calls = []
    monkeypatch.setattr(old_class, 'dispatch', lambda self, job: calls.append(job) or {'owned_lookup_only': True})
    monkeypatch.setattr(MachineTweetsAPI, 'dispatch', lambda *a: pytest.fail('Old uncertainty cannot become a new API send'))
    request = {'payload': {'version': old_version, 'parent_job_id': parent}}
    assert MachineRoutes(settings, store).dispatch(request) == {'owned_lookup_only': True}
    assert calls == [request] and store.query('SELECT * FROM publications') == before
    assert not store.query('SELECT * FROM api_calls')


@pytest.mark.parametrize('old_version,old_class', [(V1, MachineTweets), (V2, MachineTweetsV2)])
def test_api_switch_does_not_allow_a_new_unreserved_old_policy_dispatch(official, monkeypatch, old_version, old_class):
    settings, store, _ = official
    parent = store.enqueue('x', '502', 'tweet_process', 'tweet', source('502'), old_version)
    monkeypatch.setattr(old_class, 'dispatch', lambda *a: pytest.fail('No old publication exists to reconcile'))
    with pytest.raises(HubError, match='MACHINE_SOURCE_GENERATION_NOT_ACTIVE'):
        MachineRoutes(settings, store).dispatch({'payload': {'version': old_version, 'parent_job_id': parent}})


def test_api_policy_and_null_screenshot_metadata_do_not_change_v2_contract(official):
    settings, store, _ = official
    api, dom = MachineTweetsAPI(settings, store), MachineTweetsV2(settings, store)
    assert api.policy()['source_authority'] == 'x-v2-official'
    dom_policy = policy(settings, V2)
    old_path = settings.root / 'config/machine-tweet-policy-v2.json'
    old_path.write_text(encode(dom_policy), encoding='utf-8')
    old_bytes = old_path.read_bytes()
    raw = source()
    node = {'source': raw, 'screenshot': None}
    assert api._render_source_metadata(raw, node) == {'source_screenshot': None, 'source_authority': 'x-v2-official'}
    composed = compositor_input(raw, 'Owned translation')['post']
    assert composed['sourceScreenshot'] == {'file': None, 'capturedAt': None, 'reviewed': False}
    assert composed['review'] == {'sourceConfirmed': False, 'translationConfirmed': False}
    api._verify_render_source_metadata(composed, node)
    raw_dom = {'source_api': 'x-web-dom-v1', 'capture': {'captured_at': '2026-10-31T12:00:00Z'}}
    screenshot = {'file': 'owned-existing-source.png', 'sha256': 'a' * 64}
    assert dom._render_source_metadata(raw_dom, {'screenshot': screenshot}) == {'source_screenshot': screenshot}
    assert raw_dom['source_screenshot_file'] == screenshot['file']
    assert dom.policy() == dom_policy and old_path.read_bytes() == old_bytes
    assert dom.render_script == 'render_machine_extended.mjs' and dom.render_report_version == 'machine-render-v2'
    assert api.render_script == 'render_machine_official.mjs' and api.render_report_version == 'machine-render-api-v1'
    with pytest.raises(HubError, match='MACHINE_OFFICIAL_SOURCE_REQUIRED'):
        api._render_source_metadata(source(), {'screenshot': screenshot})


def configured_load(tmp_path, monkeypatch, **changes):
    root = Path(__file__).resolve().parents[1]
    text = (root / 'config/hub.toml').read_text(encoding='utf-8')
    for key, value in changes.items():
        encoded = json.dumps(value, ensure_ascii=False)
        text, count = re.subn(r'(?m)^' + re.escape(key) + r'\s*=.*$', key + ' = ' + encoded, text)
        assert count == 1, key
    file = tmp_path / 'owned-hub.toml'
    file.write_text(text, encoding='utf-8')
    monkeypatch.setenv('HUB_ROOT', str(tmp_path))
    monkeypatch.setenv('HUB_CONFIG', str(file))
    return Settings.load()


def test_distribution_defaults_keep_browser_and_paid_api_disabled(tmp_path, monkeypatch):
    settings = configured_load(tmp_path, monkeypatch)
    assert settings.config['x_source_transport'] == 'official_api'
    assert settings.config['x_api_enabled'] is False and settings.config['x_web_enabled'] is False
    assert settings.config['machine_tweet_enabled'] is False


@pytest.mark.parametrize('version,api,web', [(API, True, False), (V2, False, True)])
def test_explicit_matching_transport_can_enable_its_own_machine_policy(tmp_path, monkeypatch, version, api, web):
    settings = configured_load(tmp_path, monkeypatch, policy_version=version, x_api_enabled=api,
                               x_web_enabled=web, machine_tweet_enabled=True)
    assert settings.config['policy_version'] == version


@pytest.mark.parametrize('version,api,web', [(API, True, True), (API, False, True), (API, False, False), (V2, True, False)])
def test_machine_policy_cannot_enable_wrong_or_mixed_source_transport(tmp_path, monkeypatch, version, api, web):
    with pytest.raises(ValueError):
        configured_load(tmp_path, monkeypatch, policy_version=version, x_api_enabled=api,
                        x_web_enabled=web, machine_tweet_enabled=True)


@pytest.mark.parametrize('invalid', ['false', 1, 0])
def test_paid_api_switch_requires_a_real_boolean_not_truthy_configuration(tmp_path, monkeypatch, invalid):
    with pytest.raises(ValueError):
        configured_load(tmp_path, monkeypatch, policy_version=API, x_api_enabled=invalid,
                        x_web_enabled=False, machine_tweet_enabled=False)
