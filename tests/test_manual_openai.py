from pathlib import Path
import json
import httpx
import pytest
from adapters.model import ModelAPI
from adapters.http import ProviderHTTP
from adapters.compositor import compositor_input
from scheduler.service import DurableScheduler
from services.hub.budget import Budget
from services.hub.manual import import_tweet
from services.hub.errors import HubError
from conftest import enqueue


def test_no_x_api_schedule_under_owner_no_purchase_policy(runtime):
    settings, store = runtime
    DurableScheduler(settings,store).tick()
    assert not store.query("SELECT * FROM jobs WHERE job_type='x_discovery'")
    assert settings.config['bilibili_publish_mode'] == 'manual'


def test_manual_import_is_deduplicated_and_cannot_supply_review(runtime, tmp_path):
    settings, store = runtime
    file = tmp_path / 'manual.json'
    file.write_text(json.dumps({'author':{'name':'Synthetic source for test','username':'example_handle_a'},
        'post':{'id':'1000000000000000050','text':'synthetic only','created_at':'2026-10-30T12:00:00Z'},
        'review':{'sourceConfirmed':True,'translationConfirmed':True}}),'utf-8')
    ident = import_tweet(settings,store,file)
    assert import_tweet(settings,store,file) == ident
    source = store.get(ident)['payload']
    assert source['source_api'] == 'manual-input'
    rendered_input = compositor_input(source,'仅为模拟测试')
    assert rendered_input['post']['review']['sourceConfirmed'] is False
    assert rendered_input['post']['sourceMediaCount'] is None
    assert rendered_input['post']['sourceQuote'] == 'unknown'


def test_openai_responses_strict_schema_and_separate_qa(runtime, monkeypatch):
    settings, store = runtime
    for key in ('MODEL_BASE_URL','MODEL_API_KEY','MODEL_NAME','QA_MODEL_NAME','MODEL_API_STYLE','MODEL_ALLOWED_HOSTS'):
        monkeypatch.delenv(key,raising=False)
    monkeypatch.setenv('OPENAI_API_KEY','synthetic-only-key')
    monkeypatch.setenv('OPENAI_MODEL','synthetic-translation-model')
    monkeypatch.setenv('OPENAI_QA_MODEL','synthetic-qa-model')
    settings.config['budgets']['model'].update(daily_usd=1,monthly_usd=1,daily_calls=3,monthly_calls=3,max_call_usd=.1,paid_authorized=True)
    calls=[]
    def response(request):
        assert str(request.url) == 'https://api.openai.com/v1/responses'
        body=json.loads(request.content)
        assert body['store'] is False and body['text']['format']['strict'] is True
        assert 'tools' not in body
        calls.append(body)
        result={'translation':'模拟译文'} if body['model']=='synthetic-translation-model' else {'passed':True,'issues':[]}
        return httpx.Response(200,json={'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(result)}]}]})
    model=ModelAPI(ProviderHTTP(Budget(store,settings.config['budgets']),httpx.MockTransport(response)),store)
    original={'post':{'id':'1000000000000000051','text':'架空のテスト文章'},
              'author':{'name':'Synthetic protocol fixture','username':'fixture_only'},'synthetic':True}
    job=store.get(store.enqueue('synthetic_acceptance','responses-v2-fixture','tweet_process','tweet',original,'human-review-v1'))
    translated=model.invoke(job,'translation',original)
    qa=model.invoke(job,'quality',original,translated['translation'])
    assert len(calls)==2 and qa['passed'] is True
    assert calls[0]['model'] != calls[1]['model']
    assert not store.query('SELECT * FROM publication_reviews')
