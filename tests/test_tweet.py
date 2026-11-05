from pathlib import Path
import json
import httpx
import pytest
from adapters.compositor import Compositor, compositor_input
from adapters.model import ModelAPI
from services.hub.budget import Budget
from services.hub.errors import HubError
from services.hub.evidence import machine_evidence
from adapters.http import ProviderHTTP
from services.hub.pipelines import TweetPipeline
from services.hub.worker import Worker
from conftest import enqueue


def source():
    return {'post':{'id':'1000000000000000012','text':'今日20:00\nよろしく！','created_at':'2026-10-30T12:00:00Z'},
            'author':{'name':'Synthetic test account','username':'fixture_only'},
            'url':'https://x.com/fixture_only/status/1000000000000000012'}


def test_machine_advice_cannot_fake_manual_review():
    item = compositor_input(source(), '今天20:00\n请多关照！')
    assert item['post']['review'] == {'translationConfirmed':False,'sourceConfirmed':False}
    assert item['post']['sourceScreenshot'] == {'file':None,'capturedAt':None,'reviewed':False}
    evidence = machine_evidence(source(), '今天21:00\n请多关照！', {'passed':True,'issues':[]})
    assert evidence['machine_checks_passed'] is False
    assert 'numbers' in evidence['deterministic_failures']
    assert not evidence['auto_publish_eligible']


def test_supplied_real_screenshot_path_never_claims_human_review():
    item=source();item['source_screenshot_file']='owned-screenshot.png';item['source_screenshot_captured_at']='2026-10-31T10:00:00.831Z'
    data=compositor_input(item,'今天20:00\n请多关照！')['post']
    assert data['sourceScreenshot']=={'file':'owned-screenshot.png','capturedAt':'2026-10-31T10:00:00+00:00','reviewed':False}
    assert data['review']=={'translationConfirmed':False,'sourceConfirmed':False}


def test_millisecond_capture_is_accepted_by_original_renderer_without_approving_source(tmp_path):
    import base64
    screenshot=tmp_path/'owned-screenshot.png'
    screenshot.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg=='))
    item=source();item['source_screenshot_file']=str(screenshot);item['source_screenshot_captured_at']='2026-10-31T10:00:00.831Z'
    data=compositor_input(item,'今天20:00\n请多关照！')
    rendered=Compositor(Path(__file__).resolve().parents[1]).render(data,tmp_path)
    codes={i['code'] for i in rendered['report']['issues']}
    assert 'CAPTURE_TIME' not in codes
    assert {'SCREENSHOT_UNREVIEWED','SOURCE_UNREVIEWED','TRANSLATION_UNREVIEWED'}<=codes
    assert not rendered['report']['publishable'] and data['post']['sourceScreenshot']['reviewed'] is False


def test_configured_avatar_and_hashed_screenshot_reach_preview_without_review_claims(runtime,monkeypatch):
    import hashlib
    settings,store=runtime;item=source();item['author']['username']='example_handle_a'
    settings.config['sources'][0]['profile_image_url']='https://pbs.twimg.com/profile_images/owned-synthetic-avatar.jpg'
    raw=b'owned synthetic screenshot bytes';item['capture']={'screenshot_sha256':hashlib.sha256(raw).hexdigest(),
        'captured_at':'2026-10-31T10:00:00Z'}
    ident=store.enqueue('synthetic_acceptance','render-assets-fixture','tweet_process','tweet',item,'human-review-v1')
    folder=settings.root/'media'/ident;folder.mkdir();(folder/'browser-source.png').write_bytes(raw)
    urls=[]
    def owned_download(url,target,**kwargs):
        urls.append(url);target.write_bytes(b'owned avatar bytes')
        return {'file':str(target),'sha256':hashlib.sha256(target.read_bytes()).hexdigest()}
    monkeypatch.setattr('services.hub.pipelines.download',owned_download)
    class Model:
        def invoke(self,job,phase,source,translation=None):
            return {'translation':'今天20:00\n请多关照！'} if phase=='translation' else {'passed':True,'issues':[]}
    seen=[]
    class Renderer:
        def render(self,data,folder):
            seen.append(data)
            return {'report':{'publishable':False,'issues':[]},'png':'owned-test-placeholder'}
    pipeline=TweetPipeline(settings,store,None);pipeline.model=Model();pipeline.compositor=Renderer()
    Worker(settings,store,{'tweet_process':pipeline.process}).once('tweet')
    assert urls==[settings.config['sources'][0]['profile_image_url']]
    assert seen[0]['post']['author']['avatar']==str(folder/'avatar.bin')
    assert seen[0]['post']['sourceScreenshot']['file']==str(folder/'browser-source.png')
    assert seen[0]['post']['sourceScreenshot']['reviewed'] is False
    assert not any(seen[0]['post']['review'].values())
    assert store.get(ident)['state']=='awaiting_review' and not store.query('SELECT * FROM publications')


def test_missing_source_real_renderer_returns_review_errors(tmp_path):
    root = Path(__file__).resolve().parents[1]
    rendered = Compositor(root).render(compositor_input(source(), '今天20:00\n请多关照！'), tmp_path)
    assert Path(rendered['png']).is_file()
    assert not rendered['report']['publishable']
    codes = {i['code'] for i in rendered['report']['issues']}
    assert {'SOURCE_UNREVIEWED','TRANSLATION_UNREVIEWED','SCREENSHOT_MISSING'} <= codes
    assert 'FONT_LOAD_FAILED' not in codes and rendered['report']['layout']['fontsLoaded'] is True


def test_real_browser_missing_is_not_reported_as_success(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    # Use the real renderer with a genuinely absent browser installation.
    with pytest.raises(HubError, match='BROWSER_NOT_INSTALLED'):
        Compositor(root, browser_path=tmp_path / 'missing-browsers').render(
            compositor_input(source(), '今天20:00\n请多关照！'), tmp_path / 'job')


def test_translation_check_failure_never_reaches_renderer(runtime):
    settings, store = runtime
    ident = store.enqueue('x', source()['post']['id'], 'tweet_process', 'tweet', source(), 'human-review-v1')
    class FakeModel:
        def invoke(self, job, phase, source, translation=None):
            return {'translation':'今天21:00\n请多关照！'} if phase == 'translation' else {'passed':True,'issues':[]}
    pipeline = TweetPipeline(settings, store, None)
    pipeline.model = FakeModel()
    pipeline.compositor = type('Forbidden', (), {'render':lambda *a: pytest.fail('Renderer must be blocked')})()
    Worker(settings, store, {'tweet_process':pipeline.process}).once('tweet')
    assert store.get(ident)['state'] == 'awaiting_review'
    assert store.get(ident)['error_code'] == 'TRANSLATION_CHECK_FAILED'
    assert not store.query('SELECT * FROM publications')


def test_model_success_cached_across_instance_restart(runtime, monkeypatch):
    settings, store = runtime
    monkeypatch.setenv('MODEL_BASE_URL', 'https://model.example.test/v1')
    monkeypatch.setenv('MODEL_API_STYLE', 'chat_completions')
    monkeypatch.setenv('MODEL_ALLOWED_HOSTS', 'model.example.test')
    monkeypatch.setenv('MODEL_API_KEY', 'synthetic-key')
    monkeypatch.setenv('MODEL_NAME', 'synthetic-model')
    settings.config['budgets']['model'].update(daily_usd=1,monthly_usd=1,daily_calls=3,monthly_calls=3,max_call_usd=.1,paid_authorized=True)
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={'choices':[{'message':{'content':json.dumps({'translation':'今天20:00\n请多关照！'})}}]})
    client = ProviderHTTP(Budget(store, settings.config['budgets']), httpx.MockTransport(respond))
    ident = store.enqueue('synthetic_acceptance','cache-fixture','tweet_process','tweet',source(),'human-review-v1')
    job = store.get(ident)
    ModelAPI(client, store).invoke(job, 'translation', source())
    ModelAPI(client, store).invoke(job, 'translation', source())
    assert len(calls) == 1
