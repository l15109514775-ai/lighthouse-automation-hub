"""Synthetic configuration only; no source approval, browser session or send."""
import json
from pathlib import Path
import time
from services.hub.readiness import automation_readiness


def test_machine_switch_does_not_claim_policy_or_real_acceptance(runtime):
    settings,store=runtime
    settings.config['machine_tweet_enabled']=True
    status=automation_readiness(settings,store)
    assert status['machine_tweet_implemented'] and status['machine_tweet_enabled']
    assert not status['machine_tweet_policy_valid']
    assert status['machine_tweet_policy_error']=='MACHINE_TWEET_POLICY_INVALID'
    assert status['machine_website_publications_completed']==0
    assert not status['machine_tweet_publish_accepted'] and not status['machine_tweet_ready']
    assert not status['publishing_enabled'] and not status['fully_automatic_ready']


def test_valid_machine_policy_does_not_fabricate_real_publication(runtime):
    settings,store=runtime
    template=Path(__file__).resolve().parents[1]/'config/machine-tweet-policy.example.json'
    policy=json.loads(template.read_text('utf-8'))
    policy.update(owner_confirmed_machine_mode=True,owner_response='Owned synthetic configuration fixture; no external send',
        activation_at=time.time())
    policy['rights'].update(owner_confirmed=True,reference='Owned synthetic fixture scope')
    folder=settings.root/'config';folder.mkdir(exist_ok=True)
    (folder/'machine-tweet-policy.json').write_text(json.dumps(policy),'utf-8')
    settings.config['machine_tweet_enabled']=True
    for name in ('scheduler','worker:tweet','worker:video','worker:system'):store.beat(name)
    status=automation_readiness(settings,store)
    assert status['machine_tweet_policy_valid'] and status['publishing_enabled']
    assert status['machine_tweet_max_post_age_seconds']==7200
    assert status['machine_tweet_content_scope']==['visible_plain_text_without_media_or_context']
    assert status['machine_tweet_requires_per_post_human_review'] is False
    assert not status['machine_tweet_publish_accepted'] and not status['machine_tweet_ready']
    assert not status['fully_automatic_ready']
    assert 'MACHINE_PUBLISH_ACCEPTANCE_REQUIRED' in {x['code'] for x in status['blockers']}
    assert not store.query('SELECT * FROM publications') and not store.query('SELECT * FROM api_calls')


def test_example_is_unapproved_and_cannot_enable_machine_sender(runtime):
    settings,store=runtime
    template=Path(__file__).resolve().parents[1]/'config/machine-tweet-policy.example.json'
    folder=settings.root/'config';folder.mkdir(exist_ok=True)
    (folder/'machine-tweet-policy.json').write_bytes(template.read_bytes())
    settings.config['machine_tweet_enabled']=True
    status=automation_readiness(settings,store)
    assert not status['machine_tweet_policy_valid'] and not status['publishing_enabled']
    assert not status['machine_tweet_publish_accepted'] and not status['fully_automatic_ready']
