"""Synthetic browser observations only; no account, model call or human approval."""
import hashlib
from pathlib import Path
import pytest
from adapters.compositor import compositor_input
from services.hub.errors import HubError
from services.hub.worker import Worker
from services.hub.x_web_discovery import XWebDiscovery
from scheduler.service import DurableScheduler


def observation(tmp_path,handle,post_id='123'):
    image=tmp_path/(handle+'.png');image.write_bytes(b'owned synthetic fixture; not a public post image')
    return {'payload':{'post':{'id':post_id,'text':'Owned fixture','created_at':'2026-10-31T00:00:00Z','referenced_tweets':[]},
        'author':{'name':'Fixture','username':handle},'url':f'https://x.com/{handle}/status/{post_id}',
        'source_api':'x-web-dom-v1','source_media_count':None,'references_observed':False,
        'capture':{'human_review':False,'source_screenshot_reviewed':False,'screenshot_sha256':hashlib.sha256(image.read_bytes()).hexdigest()}},
        'screenshot_file':str(image)}


def test_repeat_observation_reuses_task_and_preserves_first_evidence(runtime,tmp_path):
    settings,store=runtime;settings.config['sources']=settings.config['sources'][:1]
    service=XWebDiscovery(settings,store);record=observation(tmp_path,'example_handle_a')
    service.browser.latest=lambda *a,**kw:{'state':'observed','result_file':'owned-fixture-result','posts':[record]}
    parent=store.enqueue('scheduler','fixture','x_discovery','tweet',{},settings.config['policy_version'])
    job=store.claim('tweet',180,4)
    first=service.run(job);child=store.query("SELECT job_id FROM jobs WHERE job_type='tweet_process'")[0]['job_id']
    assert first['new_jobs']==1
    image=settings.root/'media'/child/'browser-source.png';original=image.read_bytes()
    Path(record['screenshot_file']).write_bytes(b'a later fixture screenshot')
    assert service.run(job)['new_jobs']==0 and image.read_bytes()==original
    assert len(store.query("SELECT * FROM jobs WHERE job_type='tweet_process'"))==1
    assert not store.query('SELECT * FROM publications') and not store.query('SELECT * FROM api_calls')
    assert store.get(parent)['result']['cursor_advanced'] is False


def test_both_sources_checked_and_403_is_review_not_empty_success(runtime):
    settings,store=runtime;service=XWebDiscovery(settings,store);seen=[]
    def blocked(handle,**kwargs):
        seen.append(handle)
        return {'state':'blocked','result_file':'owned-fixture-result','posts':[],
                'error_code':'X_WEB_ACCESS_DENIED' if len(seen)==1 else 'X_WEB_RATE_LIMITED'}
    service.browser.latest=blocked
    ident=store.enqueue('scheduler','fixture','x_discovery','tweet',{},settings.config['policy_version'])
    assert Worker(settings,store,{'x_discovery':service.run}).once('tweet')
    job=store.get(ident)
    assert seen==['example_handle_a','example-channel-b'] and job['state']=='awaiting_review'
    assert job['error_code']=='X_WEB_ACCESS_DENIED' and job['result']['new_jobs']==0
    polls=store.query("SELECT * FROM source_polls WHERE source_key LIKE 'x-web:%'")
    assert len(polls)==2 and all(p['last_success_at'] is None and p['error_code'] for p in polls)
    assert not store.query("SELECT * FROM jobs WHERE job_type='tweet_process'")


def test_network_error_uses_bounded_persistent_retries(runtime):
    settings,store=runtime;settings.config['sources']=settings.config['sources'][:1]
    service=XWebDiscovery(settings,store)
    service.browser.latest=lambda *a,**kw:{'state':'blocked','result_file':'owned-fixture','posts':[],'error_code':'X_WEB_NETWORK_TIMEOUT'}
    ident=store.enqueue('scheduler','network-fixture','x_discovery','tweet',{},settings.config['policy_version'])
    worker=Worker(settings,store,{'x_discovery':service.run})
    for _ in range(settings.config['max_attempts']):
        with store.transaction() as db:db.execute('UPDATE jobs SET next_run=0 WHERE job_id=?',(ident,))
        assert worker.once('tweet')
    assert store.get(ident)['state']=='failed' and store.get(ident)['attempts']==settings.config['max_attempts']
    assert not worker.once('tweet')


def test_browser_schedule_does_not_enable_api_or_overlap_discovery(runtime):
    settings,store=runtime;settings.config['x_web_enabled']=True
    assert settings.config['x_api_enabled'] is False
    scheduler=DurableScheduler(settings,store);scheduler.tick(now=100)
    scheduler.tick(now=100+settings.config['tweet_poll_minutes']*60)
    assert len(store.query("SELECT * FROM jobs WHERE job_type='x_discovery'"))==1


def test_renderer_keeps_browser_media_and_reference_unknown(runtime,tmp_path):
    source=observation(tmp_path,'example_handle_a')['payload']
    value=compositor_input(source,'Owned fixture')['post']
    assert value['sourceMediaCount'] is None and value['sourceQuote']=='unknown'
    assert value['review']=={'sourceConfirmed':False,'translationConfirmed':False}
    assert value['sourceScreenshot']['reviewed'] is False


def test_failed_web_read_is_exposed_as_error_not_fresh_observation(runtime):
    from services.hub.readiness import automation_readiness
    settings,store=runtime;settings.config['x_web_enabled']=True
    with store.transaction() as db:
        db.execute('INSERT INTO source_polls VALUES(?,?,?,?,?,?)',('x-web:example_handle_a',0,None,'X_WEB_ACCESS_DENIED','{}',0))
    status=automation_readiness(settings,store)
    assert status['x_web_source_errors']=={'x-web:example_handle_a':'X_WEB_ACCESS_DENIED'}
    assert not status['x_web_observations_fresh'] and not status['fully_automatic_ready']
