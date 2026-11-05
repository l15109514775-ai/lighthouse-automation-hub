"""Synthetic OAuth transport only. No human attestations or official account credentials."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import threading
import time
from collections import Counter
import httpx
import pytest
from cryptography.fernet import Fernet
from adapters.bilibili import BilibiliOfficial
from adapters.http import ProviderHTTP
from scheduler.service import DurableScheduler
from services.hub.bilibili_tokens import BilibiliTokens, REFRESH_URL
from services.hub.budget import Budget
from services.hub.db import Store
from services.hub.errors import HubError


class OAuthServer:
    def __init__(self):
        self.paths=Counter()
        self.access='synthetic-access-initial'
        self.refresh='synthetic-refresh-once'
        self.timeout=False
        self.fail_validation=False
        self.scopes=['USER_INFO','ARC_BASE']
        self.identity='synthetic-account'
        self.response=None
        self.refresh_status=200
        self.entered=self.release=None

    def __call__(self,request):
        path=request.url.path
        self.paths[path]+=1
        if str(request.url).startswith(REFRESH_URL+'?'):
            assert request.method=='POST' and request.content==b''
            assert request.headers['content-type']=='application/x-www-form-urlencoded'
            assert dict(request.url.params)=={'client_id':'synthetic-app','client_secret':'synthetic-secret',
                'grant_type':'refresh_token','refresh_token':self.refresh}
            assert request.url.host=='api.bilibili.com'
            if self.entered:
                self.entered.set()
                assert self.release.wait(5)
            if self.timeout:
                raise httpx.ReadTimeout('Synthetic lost response; must never be replayed.',request=request)
            if self.refresh_status!=200:
                return httpx.Response(self.refresh_status,json={'code':-1})
            if self.response is not None:
                return httpx.Response(200,json=self.response)
            self.access='synthetic-access-rotated'
            self.refresh='synthetic-refresh-rotated'
            return httpx.Response(200,json={'code':0,'data':{'access_token':self.access,
                'refresh_token':self.refresh,'expires_in':int(time.time()+7200)}})
        assert request.url.host=='member.bilibili.com'
        assert request.headers['access-token']==self.access
        if self.fail_validation:
            return httpx.Response(429,headers={'Retry-After':'120'})
        if path.endswith('/info'):
            data={'openid':self.identity,'name':'synthetic-account-only'}
        else:
            assert path.endswith('/scopes')
            data={'openid':self.identity,'scopes':self.scopes}
        return httpx.Response(200,json={'code':0,'data':data})


@pytest.fixture
def oauth(runtime,monkeypatch):
    settings,store=runtime
    settings.config.update(bilibili_token_refresh_enabled=True,
        authorized_bilibili_accounts=['synthetic-account'],target_bilibili_account_name='synthetic-account-only')
    settings.config['budgets']['bilibili'].update(daily_calls=100,monthly_calls=100)
    for key,value in {'BILIBILI_ACCOUNT_ID':'synthetic-account','BILIBILI_ACCESS_KEY_ID':'synthetic-app',
        'BILIBILI_ACCESS_KEY_SECRET':'synthetic-secret','BILIBILI_ACCESS_TOKEN':'synthetic-legacy-stale',
        'EVENT_ENCRYPTION_KEY':Fernet.generate_key().decode()}.items():
        monkeypatch.setenv(key,value)
    server=OAuthServer()
    http=ProviderHTTP(Budget(store,settings.config['budgets']),httpx.MockTransport(server))
    service=BilibiliTokens(settings,store,http)
    file=settings.root/'synthetic-oauth.json'
    file.write_text(json.dumps({'version':1,'app_id':'synthetic-app','account_id':'synthetic-account',
        'access_token':server.access,'refresh_token':server.refresh,'access_expires_at':int(time.time()+600)}))
    file.chmod(0o600)
    return settings,store,server,http,service,file


def activate(oauth):
    settings,store,server,http,service,file=oauth
    service.import_file(file,'maintenance')
    assert service.tick()['results'][0]['state']=='active'
    return oauth


def test_import_is_encrypted_audited_and_read_only_verification_required(oauth):
    settings,store,server,http,service,file=oauth
    result=service.import_file(file,'maintenance')
    assert result['state']=='validating' and result['network_requests_sent']==0 and not server.paths
    with pytest.raises(HubError,match='OAUTH_VALIDATION_OR_REAUTH_REQUIRED'):
        BilibiliOfficial(settings,store,http).credentials()
    row=store.query('SELECT * FROM bilibili_credentials')[0]
    assert server.access not in row['encrypted_tokens'] and server.refresh not in row['encrypted_tokens']
    assert service.tick()['results'][0]['identity_and_scopes_verified']
    assert BilibiliOfficial(settings,store,http).credentials()['BILIBILI_ACCESS_TOKEN']==server.access
    assert sum(server.paths.values())==2
    public=json.dumps(service.status())+json.dumps(store.query('SELECT * FROM audit'))
    assert 'synthetic-access' not in public and 'synthetic-refresh' not in public
    assert store.query('SELECT COUNT(*) n FROM publications')[0]['n']==0


def test_once_only_refresh_then_verified_pair_survives_restart(oauth):
    settings,store,server,http,service,file=activate(oauth)
    assert service.tick()['results'][0]['state']=='active'
    assert server.paths['/x/account-oauth2/v1/refresh_token']==1
    store=Store(settings.db_path);store.init()
    restored=BilibiliTokens(settings,store,http)
    assert restored.access_token('synthetic-account','synthetic-app')=='synthetic-access-rotated'
    assert restored.tick()['results'][0]['refresh_due'] is False
    assert server.paths['/x/account-oauth2/v1/refresh_token']==1
    assert store.query('SELECT state FROM bilibili_token_refreshes')==[{'state':'confirmed'}]


def test_lost_refresh_result_never_repeats_or_falls_back_to_env(oauth):
    settings,store,server,http,service,file=activate(oauth)
    server.timeout=True
    assert service.tick()['results'][0]['state']=='refresh_unknown'
    restored=BilibiliTokens(settings,Store(settings.db_path),http)
    for _ in range(5):
        assert restored.tick()['results'][0]['state']=='refresh_unknown'
    assert server.paths['/x/account-oauth2/v1/refresh_token']==1
    with pytest.raises(HubError,match='OAUTH_REFRESH_UNKNOWN'):
        BilibiliOfficial(settings,store,http).credentials()
    assert store.query('SELECT name FROM events')==[{'name':'auth.expired'}]


def test_crash_checkpoint_requires_reauthorization_without_any_refresh_request(oauth):
    settings,store,server,http,service,file=activate(oauth)
    with store.transaction() as db:
        db.execute("UPDATE bilibili_credentials SET state='refreshing'")
        db.execute("INSERT INTO bilibili_token_refreshes VALUES('synthetic-crash','synthetic-account',1,'synthetic-fingerprint','started',0,0,NULL)")
    restored=BilibiliTokens(settings,Store(settings.db_path),http)
    assert restored.tick()['results'][0]['error_code']=='OAUTH_REFRESH_INTERRUPTED'
    assert not server.paths['/x/account-oauth2/v1/refresh_token']


def test_rotated_pair_persists_before_validation_and_resumes_reads_only(oauth):
    settings,store,server,http,service,file=activate(oauth)
    server.fail_validation=True
    first=service.tick()['results'][0]
    assert first=={'state':'validating','error_code':'API_RATE_LIMITED'}
    row=store.query('SELECT * FROM bilibili_credentials')[0]
    assert row['generation']==2 and row['next_check_at']>=time.time()+115
    assert row['validation_attempts']==1
    restored=BilibiliTokens(settings,Store(settings.db_path),http)
    assert restored.tick()['results'][0]['backoff']
    server.fail_validation=False
    with store.transaction() as db: db.execute('UPDATE bilibili_credentials SET next_check_at=0')
    assert restored.tick()['results'][0]['state']=='active'
    assert server.paths['/x/account-oauth2/v1/refresh_token']==1


def test_validation_attempts_bounded_across_restarts(oauth):
    settings,store,server,http,service,file=oauth
    service.import_file(file,'maintenance')
    server.fail_validation=True
    for _ in range(settings.config['max_attempts']+1):
        with store.transaction() as db: db.execute('UPDATE bilibili_credentials SET next_check_at=0')
        result=BilibiliTokens(settings,Store(settings.db_path),http).tick()['results'][0]
    assert result=={'state':'auth_expired','error_code':'OAUTH_VALIDATION_RETRY_EXHAUSTED'}
    assert sum(server.paths.values())==settings.config['max_attempts']


def test_expired_access_token_does_not_attempt_refresh(oauth):
    settings,store,server,http,service,file=activate(oauth)
    with store.transaction() as db: db.execute('UPDATE bilibili_credentials SET access_expires_at=0')
    assert service.tick()['results'][0]=={'state':'auth_expired','error_code':'AUTH_EXPIRED'}
    assert not server.paths['/x/account-oauth2/v1/refresh_token']


def test_budget_denial_does_not_consume_refresh_token_or_call_network(oauth):
    settings,store,server,http,service,file=activate(oauth)
    settings.config['budgets']['bilibili'].update(daily_calls=0,monthly_calls=0)
    previous=sum(server.paths.values())
    assert service.tick()['results'][0]=={'state':'active','error_code':'API_BUDGET_EXCEEDED'}
    assert sum(server.paths.values())==previous
    assert not store.query('SELECT * FROM bilibili_token_refreshes')
    settings.config['budgets']['bilibili'].update(daily_calls=100,monthly_calls=100)
    assert service.tick()['results'][0]['state']=='active'
    assert server.paths['/x/account-oauth2/v1/refresh_token']==1


@pytest.mark.parametrize('change,code',[
    ('identity','TARGET_ACCOUNT_IDENTITY_MISMATCH'),('scopes','BILIBILI_SCOPE_REQUIRED')])
def test_identity_mismatch_or_scope_reduction_never_activates(oauth,change,code):
    settings,store,server,http,service,file=oauth
    service.import_file(file,'maintenance')
    if change=='identity': server.identity='synthetic-other-account'
    else: server.scopes=['USER_INFO']
    assert service.tick()['results'][0]=={'state':'auth_expired','error_code':code}
    with pytest.raises(HubError,match=code):
        service.access_token('synthetic-account','synthetic-app')


@pytest.mark.parametrize('data',[
    {}, {'access_token':'synthetic-new','refresh_token':'synthetic-new','expires_in':3600},
    {'access_token':'synthetic-new','refresh_token':'synthetic-refresh-once','expires_in':9999999999},
    {'access_token':'synthetic-new','refresh_token':'synthetic-new','expires_in':True}])
def test_malformed_refresh_response_is_unknown_and_never_replayed(oauth,data):
    settings,store,server,http,service,file=activate(oauth)
    server.response={'code':0,'data':data}
    assert service.tick()['results'][0]['state']=='refresh_unknown'
    assert service.tick()['results'][0]['state']=='refresh_unknown'
    assert server.paths['/x/account-oauth2/v1/refresh_token']==1


def test_ciphertext_tamper_and_app_binding_change_stop_credentials(oauth,monkeypatch):
    settings,store,server,http,service,file=activate(oauth)
    monkeypatch.setenv('BILIBILI_ACCESS_KEY_ID','synthetic-other-app')
    with pytest.raises(HubError,match='OAUTH_ACCOUNT_OR_APP_CHANGED'):
        service.access_token('synthetic-account','synthetic-other-app')
    monkeypatch.setenv('BILIBILI_ACCESS_KEY_ID','synthetic-app')
    with store.transaction() as db: db.execute("UPDATE bilibili_credentials SET encrypted_tokens='tampered'")
    assert service.tick()['results'][0]['error_code']=='OAUTH_CREDENTIAL_INTEGRITY_FAILED'
    assert not server.paths['/x/account-oauth2/v1/refresh_token']


@pytest.mark.parametrize('status,error',[(401,'AUTH_EXPIRED'),(429,'API_RATE_LIMITED'),(503,'API_SERVER_ERROR')])
def test_refresh_http_errors_are_counted_separately_but_never_replay_one_use_token(oauth,status,error):
    settings,store,server,http,service,file=activate(oauth)
    server.refresh_status=status
    assert service.tick()['results'][0]['state']=='refresh_unknown'
    assert service.tick()['results'][0]['state']=='refresh_unknown'
    assert server.paths['/x/account-oauth2/v1/refresh_token']==1
    assert store.query('SELECT error_code FROM api_calls WHERE error_code IS NOT NULL')==[{'error_code':error}]


def test_import_requires_maintenance_and_explicit_replacement(oauth):
    settings,store,server,http,service,file=oauth
    with pytest.raises(HubError,match='ACCESS_DENIED'): service.import_file(file,'reader')
    service.import_file(file,'maintenance')
    with pytest.raises(HubError,match='OAUTH_IMPORT_REPLACEMENT_REQUIRED'): service.import_file(file,'maintenance')
    assert service.import_file(file,'maintenance',replace=True)['state']=='validating'
    assert store.query('SELECT generation FROM bilibili_credentials')[0]['generation']==2
    assert not server.paths


def test_reimporting_consumed_unknown_refresh_token_cannot_authorize_replay(oauth):
    settings,store,server,http,service,file=activate(oauth)
    server.timeout=True
    assert service.tick()['results'][0]['state']=='refresh_unknown'
    with pytest.raises(HubError,match='OAUTH_REPLACEMENT_CONSUMED_TOKEN'):
        service.import_file(file,'maintenance',replace=True)
    assert server.paths['/x/account-oauth2/v1/refresh_token']==1


@pytest.mark.skipif(os.name=='nt',reason='POSIX permission mode check; Windows uses administrator ACLs')
def test_group_readable_import_is_rejected(oauth):
    settings,store,server,http,service,file=oauth
    file.chmod(0o640)
    with pytest.raises(HubError,match='OAUTH_IMPORT_INVALID'): service.import_file(file,'maintenance')
    assert not store.query('SELECT * FROM bilibili_credentials')


def test_refresh_disabled_and_scheduler_never_creates_production_work_by_default(oauth):
    settings,store,server,http,service,file=oauth
    settings.config['bilibili_token_refresh_enabled']=False
    assert service.tick()=={'enabled':False,'accounts_checked':0,'network_requests_sent':0}
    scheduler=DurableScheduler(settings,store)
    scheduler.tick()
    assert not store.query("SELECT * FROM jobs WHERE job_type='bilibili_token_check'")
    settings.config['bilibili_token_refresh_enabled']=True
    scheduler.tick();scheduler.tick(now=time.time()+120)
    assert len(store.query("SELECT * FROM jobs WHERE job_type='bilibili_token_check'"))==1
    assert service.tick()['accounts_checked']==0 and not server.paths


def test_concurrent_tick_uses_shared_os_lock_and_sends_one_refresh(oauth):
    settings,store,server,http,service,file=activate(oauth)
    server.entered,server.release=threading.Event(),threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        first=pool.submit(service.tick)
        try:
            assert server.entered.wait(3)
            with pytest.raises(HubError,match='HEAVY_RESOURCE_BUSY'):
                BilibiliTokens(settings,Store(settings.db_path),http).tick()
        finally:
            server.release.set()
        assert first.result(timeout=5)['results'][0]['state']=='active'
    assert server.paths['/x/account-oauth2/v1/refresh_token']==1
