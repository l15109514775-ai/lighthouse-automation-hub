from pathlib import Path
import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from cryptography.fernet import Fernet
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest
from services.hub.events import Events, headers
from services.hub.errors import HubError
from adapters.safe_https import PublicHTTPSConnection, validate_destination


def test_independent_stdio_mcp_session_and_readonly_permissions(runtime):
    settings, store = runtime
    root = Path(__file__).resolve().parents[1]
    async def exercise():
        params = StdioServerParameters(command=sys.executable,args=['-m','services.hub.mcp_server'],
            env={**os.environ,'HUB_ROOT':str(settings.root),'HUB_CONFIG':str(root / 'config/hub.toml'),
                 'HUB_READ_TOKEN':'synthetic-reader','HUB_MAINTAIN_TOKEN':'synthetic-maintainer',
                 'HUB_MCP_TOKEN':'synthetic-reader','PYTHONPATH':str(root)})
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize()
                listed = await session.list_tools()
                assert {item.name for item in listed.tools} == {'get_system_status','list_jobs','get_job_details',
                    'list_failures','retry_job','pause_pipeline','resume_pipeline','get_daily_report'}
                result = await session.call_tool('get_system_status',{})
                assert not result.isError
                denied = await session.call_tool('pause_pipeline',{'pipeline':'tweet'})
                assert denied.isError
    asyncio.run(exercise())
    assert store.query("SELECT paused FROM pipelines WHERE name='tweet'")[0]['paused'] == 0
    assert store.query("SELECT COUNT(*) n FROM audit WHERE outcome='denied'")[0]['n'] >= 1


def test_signing_format_covers_exact_body_and_timestamp():
    secret = 'whsec_' + base64.b64encode(bytes(range(32))).decode()
    result = headers(secret,'evt_test',b'{"a":1}','sub_test',timestamp=123456)
    # A receiver independently verifies the exact Standard Webhooks signed content.
    received = base64.b64decode(result['webhook-signature'].split(',')[1])
    expected = hmac.digest(bytes(range(32)), b'evt_test.123456.{"a":1}', 'sha256')
    assert hmac.compare_digest(received,expected)
    assert not hmac.compare_digest(received,hmac.digest(bytes(range(32)),b'evt_test.123456.{"a":2}','sha256'))


def event_service(runtime, monkeypatch, delivery_status=200):
    settings, store = runtime
    settings.config['event_callback_hosts'] = ['callback.example.test']
    monkeypatch.setenv('EVENT_ENCRYPTION_KEY', Fernet.generate_key().decode())
    calls = []
    def send(url, hosts, signature, body):
        payload = json.loads(body)
        calls.append((signature,payload))
        if payload.get('type') == 'verification':
            return 200, json.dumps({'challenge':payload['challenge']}).encode()
        return delivery_status, b'{}'
    return Events(settings,store,send),calls


def subscribe(service, **kwargs):
    return service.subscribe('test-owner','task.failed',{'queue':'tweet'},
        {'mode':'webhook','url':'https://callback.example.test/events',
         'secret':'whsec_' + base64.b64encode(b'synthetic-signing-key-not-real!!').decode()}, **kwargs)


def test_persistent_subscription_filters_and_expiry(runtime, monkeypatch):
    service, calls = event_service(runtime, monkeypatch)
    subscription = subscribe(service, cursor='0')
    assert subscribe(service, cursor='0')['id'] == subscription['id']
    store = runtime[1]
    stored = store.query('SELECT encrypted_secret FROM subscriptions')[0]['encrypted_secret']
    assert 'whsec_' not in stored
    store.event('task.failed',{'queue':'video'})
    store.event('task.failed',{'queue':'tweet'})
    # New service instance reads the persisted subscription after a process-style restart.
    Events(runtime[0],store,service.transport).dispatch()
    delivered = [data for _,data in calls if data.get('name') == 'task.failed']
    assert len(delivered) == 1 and delivered[0]['data']['queue'] == 'tweet'
    with store.transaction() as db: db.execute('UPDATE subscriptions SET expires_at=?',(time.time()-1,))
    store.event('task.failed',{'queue':'tweet'})
    assert service.dispatch()['delivery_attempts'] == 0


def test_transient_event_delivery_retries_same_id_and_stops_at_limit(runtime, monkeypatch):
    service, calls = event_service(runtime, monkeypatch, delivery_status=503)
    subscribe(service,cursor='0')
    store=runtime[1]
    store.event('task.failed',{'queue':'tweet'})
    for _ in range(7):
        service.dispatch()
        with store.transaction() as db: db.execute('UPDATE deliveries SET next_due=0')
    events = [payload for _,payload in calls if payload.get('name') == 'task.failed']
    assert len(events) == 5 and len({payload['eventId'] for payload in events}) == 1
    assert store.query('SELECT state,attempts FROM deliveries')[0] == {'state':'failed','attempts':5}


@pytest.mark.parametrize('status',[410,413])
def test_permanent_event_delivery_responses_not_retried(runtime, monkeypatch, status):
    service, calls = event_service(runtime,monkeypatch,delivery_status=status)
    subscribe(service,cursor='0')
    runtime[1].event('task.failed',{'queue':'tweet'})
    service.dispatch()
    with runtime[1].transaction() as db: db.execute('UPDATE deliveries SET next_due=0')
    service.dispatch()
    assert len([payload for _,payload in calls if payload.get('name')]) == 1


def test_callback_rejects_private_network_and_unapproved_hosts(monkeypatch):
    with pytest.raises(HubError,match='HTTPS_DESTINATION_NOT_AUTHORIZED'):
        validate_destination('http://127.0.0.1/secret',['127.0.0.1'])
    import socket
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**kw:[(2,1,6,'',('127.0.0.1',443))])
    with pytest.raises(HubError,match='NON_PUBLIC_DESTINATION'):
        PublicHTTPSConnection('callback.example.test').connect()


@pytest.mark.parametrize('failure', [None, 'tls', 'socket_option'])
def test_pinned_https_sets_tcp_nodelay_before_tls_and_closes_on_failure(monkeypatch, failure):
    import socket
    import ssl
    calls = []
    sentinel = object()

    class OwnedSocket:
        def setsockopt(self, level, option, value):
            calls.append(('option', level, option, value))
            if failure == 'socket_option':
                raise OSError('owned socket-option failure')

        def close(self):
            calls.append(('close',))

    raw = OwnedSocket()

    def connect(address, *, timeout):
        calls.append(('connect', address, timeout))
        return raw

    class OwnedTLS:
        def wrap_socket(self, connection, *, server_hostname):
            assert connection is raw
            calls.append(('tls', server_hostname))
            if failure == 'tls':
                raise ssl.SSLError('owned TLS failure')
            return sentinel

    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(2, 1, 6, '', ('1.1.1.1', 443))])
    monkeypatch.setattr(socket, 'create_connection', connect)
    monkeypatch.setattr(ssl, 'create_default_context', lambda: OwnedTLS())
    connection = PublicHTTPSConnection('callback.example.test', 443, timeout=20)
    if failure:
        with pytest.raises(OSError):
            connection.connect()
    else:
        connection.connect()
        assert connection.sock is sentinel
    expected = [('connect', ('1.1.1.1', 443), 20),
                ('option', socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)]
    if failure != 'socket_option':
        expected.append(('tls', 'callback.example.test'))
    if failure:
        expected.append(('close',))
    assert calls == expected
