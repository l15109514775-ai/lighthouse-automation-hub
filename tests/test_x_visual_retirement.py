"""Local backup remains usable; the retired server route rejects before import."""
import pytest
from services.hub.config import Settings
from services.hub.errors import HubError
from services.hub.pipelines import TweetPipeline

def test_retired_route_stops_before_browser_import_or_paid_call(runtime):
    settings,store=runtime
    settings.config.update(x_visual_retired=True,x_web_enabled=True,x_api_enabled=False)
    pipeline=TweetPipeline(settings,store,None)
    with pytest.raises(HubError,match='X_VISUAL_TRANSPORT_RETIRED'):pipeline.discovery({})
    assert not store.query('SELECT * FROM api_calls')

def test_retired_server_configuration_cannot_enable_the_backup(runtime,monkeypatch):
    settings,store=runtime
    original=__import__('pathlib').Path(__file__).resolve().parents[1]/'config/hub.toml'
    text=original.read_text('utf-8').replace('x_web_enabled = false','x_web_enabled = true')
    file=settings.root/'retired.toml';file.write_text('x_visual_retired = true\n'+text,'utf-8')
    monkeypatch.setenv('HUB_ROOT',str(settings.root));monkeypatch.setenv('HUB_CONFIG',str(file))
    with pytest.raises(ValueError,match='visual source transport has been retired'):Settings.load()

def test_retired_api_network_error_does_not_import_or_restore_web_route(runtime,monkeypatch):
    import builtins
    from services.hub.x_api_discovery import XAPIDiscovery
    settings,store=runtime
    settings.config.update(policy_version='machine-tweet-v3-api',x_visual_retired=True,x_web_enabled=False,x_api_enabled=True)
    original=builtins.__import__
    def guarded(name,*args,**kwargs):
        if name.endswith(('x_web','x_web_discovery')):pytest.fail('Retired source import attempted')
        return original(name,*args,**kwargs)
    def unavailable(*args,**kwargs):raise HubError('NETWORK_ERROR',retryable=True)
    monkeypatch.setattr(builtins,'__import__',guarded);monkeypatch.setattr(XAPIDiscovery,'run',unavailable)
    with pytest.raises(HubError,match='NETWORK_ERROR'):TweetPipeline(settings,store,None).discovery({})
    assert settings.config['x_web_enabled'] is False and not store.query('SELECT * FROM api_calls')

def test_retired_readiness_reports_only_api_and_retains_local_backup_default(runtime):
    from services.hub.readiness import automation_readiness
    settings,store=runtime
    assert automation_readiness(settings,store)['x_web_implementation'] is True
    settings.config['x_visual_retired']=True
    status=automation_readiness(settings,store)
    assert status['x_web_implementation'] is False and status['x_visual_retired'] is True
