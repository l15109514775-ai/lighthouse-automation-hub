"""Synthetic official-protocol responses; never a real platform or human-review claim."""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
import hashlib
import json
import time
import httpx
import pytest
from adapters.compositor import compositor_input
from adapters.http import ProviderHTTP
from adapters.x import XOfficial
from services.hub.budget import Budget
from services.hub.db import Store,encode
from services.hub.errors import HubError
from services.hub.pipelines import TweetPipeline
from services.hub.source_receipts import SourceReceipts


class SourceServer:
    def __init__(self):
        self.calls=[]
        self.author={'id':'12345','username':'fixture_only','name':'Synthetic owned fixture'}
        self.post={'id':'9876543210000000000','author_id':'12345','text':'今日20:00\nよろしく！',
            'created_at':(datetime.now(timezone.utc)-timedelta(seconds=30)).isoformat(),
            'edit_history_tweet_ids':['9876543210000000000']}
        self.timeline_errors=None
        self.note=None

    def __call__(self,request):
        self.calls.append(request)
        assert request.url.host=='api.x.com'
        assert request.headers['authorization']=='Bearer synthetic-private-x-token'
        if request.url.path.endswith('/by/username/fixture_only'):
            data={'data':self.author}
        else:
            assert request.url.path=='/2/users/12345/tweets'
            fields=set(request.url.params['tweet.fields'].split(','))
            assert {'author_id','created_at','attachments','note_tweet','referenced_tweets','edit_history_tweet_ids'}<=fields
            data={'data':[self.post],'meta':{'result_count':1}}
            if self.timeline_errors is not None:data['errors']=self.timeline_errors
        # Preserve a deliberately non-canonical wire representation to test both hashes.
        return httpx.Response(200,content=json.dumps(data,ensure_ascii=False,indent=2).encode(),
                              headers={'content-type':'application/json'})


@pytest.fixture
def capture(runtime,monkeypatch):
    settings,store=runtime
    settings.config['sources']=[{'x_handle':'fixture_only'}]
    settings.config['x_api_enabled']=True
    # Applies exclusively to MockTransport; production X remains disabled at zero budget.
    settings.config['budgets']['x'].update(daily_calls=100,monthly_calls=100,daily_usd=10,
        monthly_usd=10,max_call_usd=.05,paid_authorized=True)
    monkeypatch.setenv('X_BEARER_TOKEN','synthetic-private-x-token')
    server=SourceServer()
    http=ProviderHTTP(Budget(store,settings.config['budgets']),httpx.MockTransport(server))
    ident=store.enqueue('scheduler','owned-synthetic-discovery','x_discovery','tweet',{},settings.config['policy_version'])
    job=store.claim('tweet',180,4);assert job['job_id']==ident
    ledger=SourceReceipts(settings,store)
    return settings,store,server,http,job,ledger


def bind_source(capture):
    settings,store,server,http,job,ledger=capture
    response=XOfficial(http).latest('fixture_only',job_id=job['job_id'],recorder=lambda item:ledger.record(job,item))
    payload=response['posts'][0]
    child=store.enqueue('x',payload['post']['id'],'tweet_process','tweet',payload,settings.config['policy_version'])
    ledger.bind(job,child,payload,response['post_receipts'][payload['post']['id']])
    return store.get(child),response


def test_recorded_response_bytes_and_binding_survive_database_reopen(capture):
    settings,store,server,http,parent,ledger=capture
    job,response=bind_source(capture)
    evidence=ledger.assess(job)
    assert evidence['capture_integrity_passed'] and not evidence['source_live_verified']
    assert evidence['transport_modes']==['custom_transport','custom_transport']
    assert evidence['failures']==['SOURCE_CUSTOM_TRANSPORT_NOT_LIVE_VERIFIED']
    assert evidence['source_facts']['author_id']=='12345'
    assert not evidence['human_review'] and not evidence['auto_publish_eligible']
    restored=SourceReceipts(settings,Store(settings.db_path)).assess(store.get(job['job_id']))
    assert restored==evidence and len(server.calls)==2
    records=store.query('SELECT * FROM source_http_receipts')
    assert len(records)==2
    for row in records:
        body=ledger._file(row).read_bytes()
        assert hashlib.sha256(body).hexdigest()==row['raw_sha256']
        assert hashlib.sha256(encode(json.loads(body)).encode()).hexdigest()==row['canonical_sha256']
        assert row['raw_sha256']!=row['canonical_sha256']
        assert b'synthetic-private-x-token' not in body
    assert 'synthetic-private-x-token' not in json.dumps(records)
    legacy=compositor_input(job['payload'],'今天20:00\n请多关照！')['post']
    assert legacy['review']=={'translationConfirmed':False,'sourceConfirmed':False}
    assert legacy['sourceScreenshot']['reviewed'] is False
    assert store.query('SELECT COUNT(*) n FROM publications')[0]['n']==0


def test_forged_official_source_label_without_receipts_stays_unverified(capture):
    settings,store,server,http,parent,ledger=capture
    payload={'post':server.post,'author':server.author,'source_api':'x-v2-official',
             'source_live_verified':True,'capture_integrity_passed':True}
    ident=store.enqueue('x',server.post['id'],'tweet_process','tweet',payload,'human-review-v1')
    evidence=ledger.assess(store.get(ident))
    assert evidence['failures']==['SOURCE_RECEIPT_MISSING']
    assert not evidence['capture_integrity_passed'] and not evidence['source_live_verified']
    assert not server.calls


def test_raw_file_tamper_does_not_hide_behind_unchanged_canonical_json(capture):
    settings,store,server,http,parent,ledger=capture
    job,_=bind_source(capture)
    row=store.query('SELECT * FROM source_http_receipts LIMIT 1')[0]
    file=ledger._file(row)
    file.write_bytes(file.read_bytes()+b'\n')
    evidence=ledger.assess(job)
    assert not evidence['capture_integrity_passed']
    assert evidence['failures']==['SOURCE_RESPONSE_INTEGRITY_CHANGED']


def test_job_input_changed_after_capture_is_blocked(capture):
    settings,store,server,http,parent,ledger=capture
    job,_=bind_source(capture)
    payload=deepcopy(job['payload']);payload['post']['text']='changed after capture'
    with store.transaction() as db:db.execute('UPDATE jobs SET payload=? WHERE job_id=?',(encode(payload),job['job_id']))
    assert ledger.assess(store.get(job['job_id']))['failures']==['SOURCE_JOB_INPUT_CHANGED']


@pytest.mark.parametrize('offset',[-1,86401])
def test_old_or_future_receipts_are_not_current_source_proof(capture,monkeypatch,offset):
    settings,store,server,http,parent,ledger=capture
    job,_=bind_source(capture)
    captured=store.query('SELECT MIN(captured_at) n FROM source_http_receipts')[0]['n']
    monkeypatch.setattr('services.hub.source_receipts.time.time',lambda:captured+offset)
    assert ledger.assess(job)['failures']==['SOURCE_RECEIPT_EXPIRED_OR_FUTURE']


def test_receipt_timestamp_cannot_override_its_api_record(capture):
    settings,store,server,http,parent,ledger=capture
    job,_=bind_source(capture)
    with store.transaction() as db:db.execute('UPDATE source_http_receipts SET captured_at=captured_at-1')
    assert ledger.assess(job)['failures']==['SOURCE_API_CALL_NOT_CONFIRMED']


def test_repeated_reads_create_one_post_job_and_append_receipts(capture):
    settings,store,server,http,parent,ledger=capture
    first,_=bind_source(capture);second,_=bind_source(capture)
    assert first['job_id']==second['job_id']
    assert len(store.query("SELECT * FROM jobs WHERE job_type='tweet_process'"))==1
    assert len(store.query('SELECT * FROM source_http_receipts'))==4
    assert len(store.query('SELECT * FROM tweet_source_bindings'))==2
    assert ledger.assess(second)['capture_integrity_passed']


def test_existing_manual_task_is_not_upgraded_to_official_by_label(capture):
    settings,store,server,http,parent,ledger=capture
    payload={'post':server.post,'author':server.author,'source_api':'manual-input'}
    ident=store.enqueue('x',server.post['id'],'tweet_process','tweet',payload,settings.config['policy_version'])
    job,_=bind_source(capture)
    assert job['job_id']==ident and job['payload']['source_api']=='manual-input'
    assert not store.query('SELECT * FROM tweet_source_bindings')
    assert ledger.assess(job)['failures']==['SOURCE_RECEIPT_MISSING']
    assert store.query("SELECT name FROM events")==[{'name':'review.required'}]


def test_note_tweet_full_text_is_bound_and_partial_errors_do_not_pass(capture):
    settings,store,server,http,parent,ledger=capture
    server.post['note_tweet']={'text':'Full source text20:00\nSecond line'}
    job,_=bind_source(capture)
    assert job['payload']['post']['text']=='Full source text20:00\nSecond line'
    server.timeline_errors=[{'detail':'Synthetic unresolved reference'}]
    # Resource settlement now rejects a partial/error response before a done
    # receipt or source binding can be created. The successful earlier receipt
    # remains immutable; no failed page is discounted or relabelled complete.
    before=len(store.query('SELECT * FROM tweet_source_bindings'))
    with pytest.raises(HubError,match='X_RESOURCE_RESPONSE_INVALID'):bind_source(capture)
    assert len(store.query('SELECT * FROM tweet_source_bindings'))==before


def test_author_id_mismatch_never_enqueues_source(capture):
    settings,store,server,http,parent,ledger=capture
    server.post['author_id']='99999'
    with pytest.raises(HubError,match='X_POST_IDENTITY_OR_TEXT_INVALID'):bind_source(capture)
    assert not store.query('SELECT * FROM tweet_source_bindings')
    assert not store.query("SELECT * FROM jobs WHERE job_type='tweet_process'")


def test_incomplete_note_tweet_is_not_silently_replaced_by_truncated_text(capture):
    settings,store,server,http,parent,ledger=capture
    server.post['note_tweet']={}
    with pytest.raises(HubError,match='X_FULL_TEXT_NOT_VERIFIED'):bind_source(capture)


def test_expired_discovery_lease_cannot_archive_or_bind(capture):
    settings,store,server,http,parent,ledger=capture
    with store.transaction() as db:db.execute('UPDATE jobs SET lease_until=0 WHERE job_id=?',(parent['job_id'],))
    with pytest.raises(HubError,match='SOURCE_CAPTURE_LEASE_REQUIRED'):bind_source(capture)
    assert not list((settings.root/'media').rglob('*.json'))


def test_missing_response_file_is_not_reconstructed_from_caller_flags(capture):
    settings,store,server,http,parent,ledger=capture
    job,_=bind_source(capture)
    row=store.query('SELECT * FROM source_http_receipts LIMIT 1')[0]
    ledger._file(row).unlink()
    assert ledger.assess(job)['failures']==['SOURCE_RESPONSE_FILE_MISSING']


def test_default_zero_x_budget_stops_before_network_and_receipt_creation(capture):
    settings,store,server,http,parent,ledger=capture
    settings.config['budgets']['x'].update(max_call_usd=0,paid_authorized=False,daily_calls=0)
    with pytest.raises(HubError,match='BILLABLE_COST_BOUND_REQUIRED'):bind_source(capture)
    assert not server.calls and not store.query('SELECT * FROM source_http_receipts')


@pytest.mark.parametrize('provider,method,url',[
    ('model','POST','https://api.x.com/2/users/12345/tweets'),
    ('x','GET','https://api.x.com.attacker.test/2/users/12345/tweets'),
    ('x','GET','https://api.x.com:invalid/2/users/12345/tweets')])
def test_recorded_interface_cannot_capture_other_provider_or_secret_url(capture,provider,method,url):
    settings,store,server,http,parent,ledger=capture
    with pytest.raises(HubError,match='SOURCE_REQUEST_NOT_SUPPORTED'):
        http.request_recorded(provider,method,url,job_id=parent['job_id'],headers={'Authorization':'Bearer synthetic'})
    assert not server.calls and not store.query('SELECT * FROM api_calls')


def test_secret_echo_is_not_saved_in_source_archives(capture):
    settings,store,server,http,parent,ledger=capture
    server.author['name']='synthetic-private-x-token'
    with pytest.raises(HubError,match='SOURCE_RESPONSE_CONTAINS_SECRET'):bind_source(capture)
    assert not list((settings.root/'media').rglob('*.json'))


@pytest.mark.parametrize('field,value,code',[
    ('referenced_tweets',['unparsed-reference'],'SOURCE_RELATIONSHIP_OR_MEDIA_INVALID'),
    ('referenced_tweets',[{'type':'quoted','id':'not-a-post-id'}],'SOURCE_RELATIONSHIP_OR_MEDIA_INVALID'),
    ('attachments',{'media_keys':[{}]},'SOURCE_RELATIONSHIP_OR_MEDIA_INVALID'),
    ('edit_history_tweet_ids',[],'SOURCE_EDIT_HISTORY_NOT_VERIFIED')])
def test_malformed_relationships_media_and_edits_are_not_silently_dropped(capture,field,value,code):
    settings,store,server,http,parent,ledger=capture
    server.post[field]=value
    job,_=bind_source(capture)
    evidence=ledger.assess(job)
    assert not evidence['capture_integrity_passed'] and evidence['failures']==[code]


def test_changed_api_call_state_revokes_source_evidence(capture):
    settings,store,server,http,parent,ledger=capture
    job,_=bind_source(capture)
    with store.transaction() as db:db.execute("UPDATE api_calls SET state='unknown' WHERE provider='x'")
    assert ledger.assess(job)['failures']==['SOURCE_API_CALL_NOT_CONFIRMED']
