"""Byte-level fake official server; no human reviews or live credentials are fabricated."""
from collections import Counter
import hashlib
import hmac
import json
import httpx
import pytest
from cryptography.fernet import Fernet
from adapters.bilibili import BilibiliOfficial, check_response, signed_headers
from adapters.http import ProviderHTTP
from services.hub.bilibili_service import BilibiliService
from services.hub.budget import Budget
from services.hub.db import Store
from services.hub.errors import HubError, PublishRejected, PublishUnknown
from services.hub.evidence import sha256_file
from services.hub.outcomes import JobOutcome
from services.hub.publishing import Publishing
from services.hub.worker import Worker

RESOURCE='BV0000000000'  # Deliberately fictional identifier; MockTransport only.


class OfficialServer:
    def __init__(self):
        self.paths=Counter()
        self.parts=Counter()
        self.bytes={}
        self.lose_part=None
        self.lose_merge=False
        self.lose_submit=False
        self.reject_submit=None
        self.view_state=0
        self.reject_reason=''
        self.identity='synthetic-account'
        self.scopes=['USER_INFO','ARC_BASE']
        self.token='synthetic-private-upload-token'

    def __call__(self,request):
        path=request.url.path
        self.paths[path]+=1
        if request.url.host=='openupos.bilivideo.com':
            assert request.headers['content-type']=='application/octet-stream'
            assert 'access-token' not in request.headers
            assert not any(key.startswith('x-bili-') for key in request.headers)
            assert request.url.params['upload_token']==self.token
            number=int(request.url.params['part_number'])
            assert number==1 or number-1 in self.bytes
            self.parts[number]+=1
            self.bytes[number]=request.content
            if self.lose_part==number:
                self.lose_part=None
                raise httpx.ReadTimeout('synthetic lost chunk acknowledgement',request=request)
            return httpx.Response(200,json={'code':0})
        assert request.url.host=='member.bilibili.com'
        assert request.headers['x-bili-content-md5']==hashlib.md5(request.content,usedforsecurity=False).hexdigest()
        fields=['x-bili-accesskeyid','x-bili-content-md5','x-bili-signature-method',
                'x-bili-signature-nonce','x-bili-signature-version','x-bili-timestamp']
        canonical='\n'.join(name+':'+request.headers[name] for name in fields)
        assert request.headers['authorization']==hmac.new(b'synthetic-app-secret',canonical.encode(),hashlib.sha256).hexdigest()
        assert request.headers['access-token']=='synthetic-access-token'
        if path.endswith('/user/account/info'):
            data={'openid':self.identity,'name':'synthetic-only'}
        elif path.endswith('/user/account/scopes'):
            data={'openid':self.identity,'scopes':self.scopes}
        elif path.endswith('/video/init'):
            assert json.loads(request.content)['utype']=='0'
            data={'upload_token':self.token}
        elif path.endswith('/video/complete'):
            assert request.content==b'' and request.url.params['upload_token']==self.token
            assert self.bytes
            if self.lose_merge:
                raise httpx.ReadTimeout('synthetic lost merge result',request=request)
            data={}
        elif path.endswith('/archive/add-by-utoken'):
            payload=json.loads(request.content)
            assert payload['copyright']==2 and payload['source'] in payload['desc']
            assert request.url.params['upload_token']==self.token
            if self.reject_submit:
                return httpx.Response(200,json={'code':self.reject_submit,'message':'synthetic rejection'})
            if self.lose_submit:
                raise httpx.ReadTimeout('synthetic lost final result',request=request)
            data={'resource_id':RESOURCE}
        elif path.endswith('/archive/viewlist'):
            # A similarly titled post is deliberately insufficient proof of matching file identity.
            data={'list':[{'resource_id':RESOURCE,'title':'Synthetic protocol acceptance'}],'page':{'total':1}}
        elif path.endswith('/archive/view'):
            assert request.url.params['resource_id']==RESOURCE
            data={'resource_id':RESOURCE,'addit_info':{'state':self.view_state,'reject_reason':self.reject_reason}}
        else:
            pytest.fail('Unexpected official request path: '+path)
        return httpx.Response(200,json={'code':0,'data':data})


@pytest.fixture
def protocol(runtime,monkeypatch):
    settings,store=runtime
    # These settings authorize only the in-process synthetic transport. No review rows are inserted.
    settings.dry_run=False
    settings.config.update(bilibili_publish_mode='official_video',auto_publish=True,first_publish_accepted=True,
        authorized_bilibili_accounts=['synthetic-account'],target_bilibili_account_name='synthetic-only')
    settings.config['budgets']['bilibili'].update(daily_calls=200,monthly_calls=200)
    for key,value in {'BILIBILI_ACCESS_KEY_ID':'synthetic-app','BILIBILI_ACCESS_KEY_SECRET':'synthetic-app-secret',
        'BILIBILI_ACCESS_TOKEN':'synthetic-access-token','BILIBILI_ACCOUNT_ID':'synthetic-account',
        'EVENT_ENCRYPTION_KEY':Fernet.generate_key().decode()}.items():
        monkeypatch.setenv(key,value)
    server=OfficialServer()
    http=ProviderHTTP(Budget(store,settings.config['budgets']),httpx.MockTransport(server))
    adapter=BilibiliOfficial(settings,store,http,probe=lambda _:2.0)
    file=settings.root/'media/synthetic-protocol.mp4'
    file.write_bytes(b'Synthetic transport bytes. Not a real video and never sent to a live service.')
    source='https://www.youtube.com/watch?v=abcdefghijk'
    artifact={'file':str(file),'sha256':sha256_file(file),'bilibili':{'title':'Synthetic protocol acceptance',
        'tid':1,'tag':'synthetic-only','desc':'Synthetic source: '+source,'copyright':2,'source':source}}
    ident=store.enqueue('synthetic_acceptance','official-video-protocol','test','video',{},'human-review-v1')
    return settings,store,server,http,adapter,artifact,store.get(ident)


def key():
    return hashlib.sha256(b'synthetic-private-upload').hexdigest()


def test_signed_body_is_exact_utf8_and_empty_body_is_md5_empty():
    content='{"name":"测试.mp4","utype":"0"}'.encode()
    header=signed_headers('synthetic-app','synthetic-app-secret','synthetic-access-token',content,timestamp=1,nonce='once')
    assert header['x-bili-content-md5']==hashlib.md5(content,usedforsecurity=False).hexdigest()
    assert signed_headers('a','b','c')['x-bili-content-md5']=='d41d8cd98f00b204e9800998ecf8427e'
    assert header['x-bili-timestamp']=='1' and header['x-bili-signature-nonce']=='once'


def test_complete_fake_official_protocol_and_encrypted_resume_state(protocol):
    settings,store,server,http,adapter,artifact,job=protocol
    adapter.prepare(key(),artifact)
    row=store.query('SELECT * FROM bilibili_uploads')[0]
    assert row['state']=='uploaded' and server.token not in row['encrypted_upload_token']
    assert b''.join(server.bytes[n] for n in sorted(server.bytes))==PathBytes(artifact)
    result=adapter.send(key(),artifact)
    assert result['state']=='awaiting_moderation'
    restored=BilibiliOfficial(settings,Store(settings.db_path),http,probe=lambda _:2)
    assert restored.lookup(key())['state']=='completed'
    assert server.paths['/arcopen/fn/archive/add-by-utoken']==1
    assert not store.query('SELECT * FROM publication_reviews')


def PathBytes(artifact):
    from pathlib import Path
    return Path(artifact['file']).read_bytes()


def test_chunk_ack_loss_retries_same_part_after_instance_restart(protocol):
    from pathlib import Path
    settings,store,server,http,adapter,artifact,job=protocol
    Path(artifact['file']).write_bytes(b'a'*(10*1024*1024)+b'b'*257)
    artifact['sha256']=sha256_file(artifact['file'])
    server.lose_part=2
    with pytest.raises(HubError,match='NETWORK_ERROR'):
        adapter.prepare(key(),artifact)
    assert store.query('SELECT next_part FROM bilibili_uploads')[0]['next_part']==2
    BilibiliOfficial(settings,Store(settings.db_path),http,probe=lambda _:2).prepare(key(),artifact)
    assert server.parts=={1:1,2:2}
    assert server.paths['/arcopen/fn/archive/video/complete']==1
    assert b''.join(server.bytes[n] for n in sorted(server.bytes))==PathBytes(artifact)


def test_merge_timeout_never_overwrites_already_merged_chunks(protocol):
    settings,store,server,http,adapter,artifact,job=protocol
    server.lose_merge=True
    with pytest.raises(HubError,match='NETWORK_ERROR'):
        adapter.prepare(key(),artifact)
    with pytest.raises(HubError,match='PRIVATE_UPLOAD_RESULT_REQUIRES_REVIEW'):
        BilibiliOfficial(settings,Store(settings.db_path),http,probe=lambda _:2).prepare(key(),artifact)
    assert server.parts=={1:1} and server.paths['/arcopen/fn/archive/video/complete']==1
    assert server.paths['/arcopen/fn/archive/add-by-utoken']==0


def test_final_timeout_queries_list_and_never_resubmits(protocol):
    settings,store,server,http,adapter,artifact,job=protocol
    server.lose_submit=True
    publisher=Publishing(settings,store,adapter)
    with pytest.raises(PublishUnknown):
        publisher._dispatch(job,artifact,'synthetic-account','video')
    with pytest.raises(PublishUnknown):
        publisher._dispatch(job,artifact,'synthetic-account','video')
    assert server.paths['/arcopen/fn/archive/add-by-utoken']==1
    assert server.paths['/arcopen/fn/archive/viewlist']==1
    assert server.parts=={1:1}
    assert store.query('SELECT state FROM publications')[0]['state']=='unknown'


def test_accepted_bv_checkpoint_repairs_lost_publication_commit(protocol):
    settings,store,server,http,adapter,artifact,job=protocol
    publisher=Publishing(settings,store,adapter)
    publisher._dispatch(job,artifact,'synthetic-account','video')
    with store.transaction() as db:
        db.execute("UPDATE publications SET state='unknown',external_id=NULL")
    result=publisher.reconcile(store.query('SELECT idempotency_key FROM publications')[0]['idempotency_key'])
    assert result['state']=='completed' and result['external_id']==RESOURCE
    assert server.paths['/arcopen/fn/archive/add-by-utoken']==1


def test_explicit_auth_rejection_is_distinct_from_unknown_and_can_resume(protocol):
    settings,store,server,http,adapter,artifact,job=protocol
    server.reject_submit=127001
    publisher=Publishing(settings,store,adapter)
    with pytest.raises(PublishRejected,match='AUTH_EXPIRED'):
        publisher._dispatch(job,artifact,'synthetic-account','video')
    assert store.query('SELECT state FROM publications')[0]['state']=='rejected'
    assert store.query('SELECT error_code FROM api_calls ORDER BY started_at DESC LIMIT 1')[0]['error_code']=='AUTH_EXPIRED'
    server.reject_submit=None
    assert publisher._dispatch(job,artifact,'synthetic-account','video')['external_id']==RESOURCE
    assert server.parts=={1:1} and server.paths['/arcopen/fn/archive/video/complete']==1


@pytest.mark.parametrize('code,expected,retry',[(127001,'AUTH_EXPIRED',False),
    (127007,'BILIBILI_SCOPE_REQUIRED',False),(127306,'API_RATE_LIMITED',True),
    (123050,'BILIBILI_HUMAN_VERIFICATION_REQUIRED',False),(123052,'BILIBILI_CONTENT_REJECTED',False),
    (123015,'BILIBILI_UNCLASSIFIED_RESULT',False)])
def test_documented_business_failures_have_distinct_states(code,expected,retry):
    with pytest.raises(HubError) as error:
        check_response({'code':code,'message':'synthetic, never logged'})
    assert error.value.code==expected and error.value.retryable is retry


def test_missing_human_review_remains_a_hard_gate(protocol):
    settings,store,server,http,adapter,artifact,job=protocol
    with pytest.raises(HubError,match='PUBLISH_REVIEW_EVIDENCE_REQUIRED'):
        Publishing(settings,store,adapter).submit(job,artifact,'synthetic-account','video')
    assert not server.paths and not store.query('SELECT * FROM publication_reviews')
    assert not store.query('SELECT * FROM publications')


def test_dry_run_blocks_private_upload_before_any_network(protocol):
    settings,store,server,http,adapter,artifact,job=protocol
    settings.dry_run=True
    with pytest.raises(HubError,match='DRY_RUN_PUBLISH_BLOCKED'):
        adapter.prepare(key(),artifact)
    assert not server.paths


def test_wrong_account_and_missing_scope_do_not_upload(protocol):
    settings,store,server,http,adapter,artifact,job=protocol
    server.identity='wrong-synthetic-account'
    with pytest.raises(HubError,match='TARGET_ACCOUNT_IDENTITY_MISMATCH'):
        adapter.prepare(key(),artifact)
    server.identity='synthetic-account';server.scopes=['USER_INFO']
    with pytest.raises(HubError,match='BILIBILI_SCOPE_REQUIRED'):
        adapter.prepare(key(),artifact)
    assert not server.parts and not store.query('SELECT * FROM bilibili_uploads')


def test_five_hour_file_and_modified_file_block_before_upload(protocol):
    settings,store,server,http,adapter,artifact,job=protocol
    adapter.probe=lambda _:18000
    with pytest.raises(HubError,match='REQUIRES_SEGMENTATION'):
        adapter.prepare(key(),artifact)
    adapter.probe=lambda _:2
    artifact['sha256']='0'*64
    with pytest.raises(HubError,match='HASH_MISMATCH'):
        adapter.prepare(key(),artifact)
    assert not server.parts


def test_moderation_rejection_and_worker_pending_state_are_preserved(protocol):
    settings,store,server,http,adapter,artifact,job=protocol
    publisher=Publishing(settings,store,adapter)
    def handler(claimed):
        result=publisher._dispatch(claimed,artifact,'synthetic-account','video')
        return JobOutcome(result['state'],result,result['external_id'])
    Worker(settings,store,{'test':handler}).once('video')
    assert store.get(job['job_id'])['state']=='awaiting_moderation'
    assert store.get(job['job_id'])['external_publish_id']==RESOURCE
    server.view_state=-1;server.reject_reason='synthetic moderation rejection'
    service=BilibiliService(settings,store,http)
    service.adapter=adapter;service.publisher=publisher
    result=service.reconcile({})
    assert result['publish_requests_sent']==0
    assert store.get(job['job_id'])['state']=='failed'
    assert store.get(job['job_id'])['error_code']=='BILIBILI_MODERATION_REJECTED'


def test_missing_api_credentials_preflight_never_calls_network(protocol,monkeypatch):
    settings,store,server,http,adapter,artifact,job=protocol
    for name in ('BILIBILI_ACCESS_KEY_ID','BILIBILI_ACCESS_KEY_SECRET','BILIBILI_ACCESS_TOKEN','BILIBILI_ACCOUNT_ID'):
        monkeypatch.delenv(name,raising=False)
    result=BilibiliService(settings,store,http).preflight()
    assert not result['account_verified'] and result['publish_requests_sent']==0
    assert result['error_code']=='BILIBILI_CREDENTIAL_AND_ACCOUNT_REQUIRED' and not server.paths


def test_busy_heavy_resource_defers_upload_without_creating_session(protocol):
    from services.hub.locks import exclusive
    settings,store,server,http,adapter,artifact,job=protocol
    with exclusive(settings.root/'data/locks/heavy'):
        with pytest.raises(HubError,match='HEAVY_RESOURCE_BUSY') as error:
            adapter.prepare(key(),artifact)
    assert error.value.retryable and not server.parts
    assert not store.query('SELECT * FROM bilibili_uploads')


def test_changed_rights_or_source_cannot_reuse_processed_review_checkpoint(protocol):
    settings,store,server,http,adapter,artifact,job=protocol
    job['result']={'processed':artifact,'source_sha256':'a'*64,'rights_evidence':'synthetic-reference-a'}
    grant={'actions':['publish'],'evidence':'synthetic-reference-b','authorized_by':'synthetic-policy-only',
           'source_sha256':'b'*64,'bilibili':artifact['bilibili']}
    with pytest.raises(HubError,match='MEDIA_RIGHTS_OR_SOURCE_CHANGED'):
        BilibiliService(settings,store,http).publish_reviewed_video(job,grant)
    assert not server.paths and not store.query('SELECT * FROM publication_reviews')


def test_explicit_auth_rejection_is_a_worker_auth_state_not_publish_unknown(protocol):
    settings,store,server,http,adapter,artifact,job=protocol
    server.reject_submit=127001
    publisher=Publishing(settings,store,adapter)
    Worker(settings,store,{'test':lambda claimed:publisher._dispatch(claimed,artifact,'synthetic-account','video')}).once('video')
    assert store.get(job['job_id'])['state']=='auth_expired'
    assert store.query('SELECT state FROM publications')[0]['state']=='rejected'
    assert store.query("SELECT COUNT(*) n FROM events WHERE name='auth.expired'")[0]['n']==1


def test_scheduler_recovers_known_completed_publication_without_resending(protocol):
    from scheduler.service import DurableScheduler
    settings,store,server,http,adapter,artifact,job=protocol
    publisher=Publishing(settings,store,adapter)
    publisher._dispatch(job,artifact,'synthetic-account','video')
    pkey=store.query('SELECT idempotency_key FROM publications')[0]['idempotency_key']
    publisher.reconcile(pkey)
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='publish_unknown' WHERE job_id=?",(job['job_id'],))
    DurableScheduler(settings,store).tick()
    assert store.query("SELECT COUNT(*) n FROM jobs WHERE job_type='bilibili_reconcile'")[0]['n']==1
    service=BilibiliService(settings,store,http)
    service.publisher=publisher
    service.reconcile({})
    assert store.get(job['job_id'])['state']=='completed'
    assert server.paths['/arcopen/fn/archive/add-by-utoken']==1


def test_revocation_after_upload_blocks_final_post_as_definite_local_rejection(protocol):
    settings,store,server,http,adapter,artifact,job=protocol
    checks=[]
    def revalidate():
        checks.append(True)
        if len(checks)==2:
            raise HubError('SIGNED_HUMAN_VIDEO_REVIEW_REQUIRED',review=True)
    with pytest.raises(PublishRejected,match='SIGNED_HUMAN_VIDEO_REVIEW_REQUIRED'):
        Publishing(settings,store,adapter)._dispatch(job,artifact,'synthetic-account','video',revalidate=revalidate)
    assert server.parts=={1:1} and server.paths['/arcopen/fn/archive/add-by-utoken']==0
    assert store.query('SELECT state FROM publications')[0]['state']=='rejected'
