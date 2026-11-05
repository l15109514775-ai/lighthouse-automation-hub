"""Owned status rows only: no real source observation, credential or publication."""
import json
import time

import pytest

from services.hub.db import encode
from services.hub.machine_tweets_api import VERSION as API, SCOPE as API_SCOPE
from services.hub.machine_tweets_v2 import VERSION as DOM, SCOPE as DOM_SCOPE
from services.hub.readiness import automation_readiness


@pytest.fixture
def api_ready(runtime):
    settings, store = runtime
    settings.config.update(policy_version=API, machine_tweet_enabled=True, x_api_enabled=True, x_web_enabled=False)
    folder = settings.root / 'config'
    folder.mkdir(exist_ok=True)
    policy = {'version': API, 'owner_confirmed_machine_mode': True, 'human_review_required': False,
        'target_site_uid': settings.config['target_bilibili_site_uid'], 'x_handles': ['example_handle_a', 'example-channel-b'],
        'rights': {'owner_confirmed': True, 'actions': ['publish_translation', 'publish_image'], 'reference': 'Owned synthetic fixture'},
        'owner_response': 'Owned readiness configuration only', 'authorization_basis': 'existing_owner_automation_and_content_grant',
        'max_post_age_seconds': 7200, 'activation_at': time.time() - 10,
        'supported_content': API_SCOPE, 'source_authority': 'x-v2-official'}
    (folder / 'machine-tweet-policy-api-v1.json').write_text(encode(policy), encoding='utf-8')
    for name in ('scheduler', 'worker:tweet', 'worker:video', 'worker:system'):
        store.beat(name)
    return settings, store, policy


def poll(store, key, *, age=1, error=None, authority='x-v2-official', complete=True):
    now = time.time()
    metadata = {'state': 'blocked' if error else 'observed', 'source_authority': authority, 'catchup_complete': complete}
    with store.transaction() as db:
        db.execute('INSERT OR REPLACE INTO source_polls VALUES(?,?,?,?,?,?)',
                   (key, now, now - age, error, encode(metadata), now + 1800))


def both_polls(settings, store, prefix='x-api:'):
    for source in settings.config['sources']:
        poll(store, prefix + source['x_handle'])


def synthetic_publication(settings, store, kind, post_id='801'):
    ident = store.enqueue('x', post_id, 'tweet_process', 'tweet', {'owned_status_fixture': True}, 'owned-readiness-only')
    with store.transaction() as db:
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('owned-' + post_id, ident, 'bilibili', 'x', post_id, 'site_uid:' + settings.config['target_bilibili_site_uid'],
             kind, 'completed', 'owned-no-external-publication', None, time.time()))


def test_api_policy_freshness_and_old_v1_success_do_not_fabricate_first_api_publication(api_ready):
    settings, store, _ = api_ready
    both_polls(settings, store)
    both_polls(settings, store, 'x-web:')
    synthetic_publication(settings, store, 'dynamic_website_machine_v1')
    status = automation_readiness(settings, store)
    assert status['machine_tweet_policy_valid'] and status['publishing_enabled']
    assert status['x_api_observations_fresh'] and status['x_source_mode'] == 'x-v2-official'
    assert not status['x_web_enabled'] and not status['x_web_observations_fresh']
    assert len(status['x_api_sources']) == 2
    assert status['machine_website_publications_completed'] == 1
    assert status['active_machine_publication_kind'] == 'dynamic_website_machine_v3_api'
    assert status['active_machine_policy_publications_completed'] == 0
    assert status['machine_transport_historical_publication_accepted']
    assert not status['machine_tweet_publish_accepted'] and not status['machine_tweet_ready']
    assert not status['fully_automatic_ready'] and status['goal_status'] == 'in_progress'
    codes = {row['code'] for row in status['blockers']}
    assert 'MACHINE_PUBLISH_ACCEPTANCE_REQUIRED' in codes
    assert 'X_SOURCE_NOT_ENABLED' not in codes and 'X_WEB_OBSERVATION_REQUIRED' not in codes
    assert not store.query('SELECT * FROM api_calls')


def test_only_api_kind_and_fresh_api_polls_can_satisfy_the_api_tweet_gate_in_a_synthetic_status_fixture(api_ready):
    settings, store, _ = api_ready
    both_polls(settings, store)
    synthetic_publication(settings, store, 'dynamic_website_machine_v3_api')
    status = automation_readiness(settings, store)
    assert status['active_machine_policy_publications_completed'] == 1
    assert status['machine_website_publications_completed'] == 1
    assert status['machine_tweet_publish_accepted'] and status['machine_tweet_ready']
    assert not status['fully_automatic_ready']  # Video/backup/MCP completion is not implied.


@pytest.mark.parametrize('change', ['missing', 'error', 'stale', 'future', 'partial', 'wrong-authority', 'malformed-metadata'])
def test_missing_or_untrustworthy_api_poll_cannot_be_replaced_by_fresh_browser_history(api_ready, change):
    settings, store, _ = api_ready
    both_polls(settings, store)
    both_polls(settings, store, 'x-web:')
    synthetic_publication(settings, store, 'dynamic_website_machine_v3_api')
    key = 'x-api:' + settings.config['sources'][0]['x_handle']
    if change == 'missing':
        with store.transaction() as db:
            db.execute('DELETE FROM source_polls WHERE source_key=?', (key,))
    elif change == 'error':
        poll(store, key, error='PERMISSION_DENIED')
    elif change in ('stale', 'future'):
        poll(store, key, age=99999 if change == 'stale' else -30)
    elif change == 'partial':
        poll(store, key, complete=False)
    elif change == 'wrong-authority':
        poll(store, key, authority='x-web-dom-v1')
    else:
        with store.transaction() as db:
            db.execute('UPDATE source_polls SET metadata=? WHERE source_key=?', ('not JSON', key))
    status = automation_readiness(settings, store)
    assert not status['x_api_observations_fresh'] and not status['machine_tweet_ready']
    assert not status['x_web_observations_fresh']
    assert 'X_API_OBSERVATION_REQUIRED' in {row['code'] for row in status['blockers']}
    if change == 'error':
        assert status['x_api_source_errors'] == {key: 'PERMISSION_DENIED'}


def test_api_policy_validation_uses_its_own_authority_field_not_a_legacy_dom_file(api_ready):
    settings, store, policy = api_ready
    policy['source_authority'] = 'x-web-dom-v1'
    (settings.root / 'config/machine-tweet-policy-api-v1.json').write_text(json.dumps(policy), encoding='utf-8')
    status = automation_readiness(settings, store)
    assert not status['machine_tweet_policy_valid']
    assert status['machine_tweet_policy_error'] == 'MACHINE_OFFICIAL_SOURCE_POLICY_INVALID'
    assert status['active_machine_publication_kind'] == 'dynamic_website_machine_v3_api'
    assert not status['publishing_enabled']


def test_dom_policy_keeps_existing_observation_fields_and_does_not_use_api_history(api_ready):
    settings, store, policy = api_ready
    settings.config.update(policy_version=DOM, x_api_enabled=False, x_web_enabled=True)
    policy.update(version=DOM, supported_content=DOM_SCOPE)
    policy.pop('source_authority')
    (settings.root / 'config/machine-tweet-policy-v2.json').write_text(encode(policy), encoding='utf-8')
    both_polls(settings, store)
    both_polls(settings, store, 'x-web:')
    synthetic_publication(settings, store, 'dynamic_website_machine_v2')
    status = automation_readiness(settings, store)
    assert status['x_web_observations_fresh'] and status['machine_tweet_ready']
    assert not status['x_api_enabled'] and not status['x_api_observations_fresh']
    assert status['active_machine_publication_kind'] == 'dynamic_website_machine_v2'
    assert 'X_WEB_CATCHUP_COVERAGE_LIMITED' in {row['code'] for row in status['blockers']}
