import json
import pytest
from fastapi.testclient import TestClient
from services.hub.api import create_app
from services.hub.readiness import automation_readiness


@pytest.mark.parametrize('statement,rights_code',[
    ('not_granted','CONTENT_RIGHTS_NOT_GRANTED'),
    ('owner_claimed_scope_unverified','CONTENT_RIGHTS_SCOPE_UNVERIFIED'),
    ('owner_confirmed_scope','CONTENT_SOURCE_AND_PERMISSION_BINDING_REQUIRED'),
])
def test_credentials_never_enable_publishing_or_grant_rights(runtime,monkeypatch,statement,rights_code):
    settings,store=runtime
    settings.config['owner_content_permission_statement']=statement
    monkeypatch.setenv('YOUTUBE_API_KEY','synthetic-secret-never-displayed')
    monkeypatch.setenv('BILIBILI_ACCESS_TOKEN','synthetic-bili-secret-never-displayed')
    status=automation_readiness(settings,store)
    assert not status['fully_automatic_ready'] and not status['publishing_enabled']
    assert not status['youtube_live_verified']
    assert {rights_code,'BILIBILI_VIDEO_TRANSPORT_NOT_CONNECTED',
            'YOUTUBE_LIVE_READ_NOT_VERIFIED'} <= {row['code'] for row in status['blockers']}
    assert status['owner_permission_statement']==statement
    assert 'synthetic-secret' not in json.dumps(status)
    assert 'synthetic-bili' not in json.dumps(status)


def test_readiness_is_authenticated_and_independent_of_liveness(runtime,monkeypatch):
    settings,store=runtime
    monkeypatch.setenv('HUB_READ_TOKEN','synthetic-reader')
    monkeypatch.setenv('HUB_MAINTAIN_TOKEN','synthetic-maintainer')
    for name in ('scheduler','worker:tweet','worker:video','worker:system'):
        store.beat(name)
    client=TestClient(create_app(settings))
    assert client.get('/ready').status_code==200
    assert client.get('/automation-readiness').status_code==403
    result=client.get('/automation-readiness',headers={'Authorization':'Bearer synthetic-reader'})
    assert result.status_code==200 and result.json()['core_independent_of_chat']
    assert not result.json()['fully_automatic_ready']
