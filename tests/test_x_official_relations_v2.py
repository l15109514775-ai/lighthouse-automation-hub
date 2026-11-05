"""Owned recorded HTTP + decoded photos; no real source, model or publication."""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
import hashlib
import json
from pathlib import Path
import time

import httpx
import pytest

from adapters.http import ProviderHTTP
from adapters.x import XOfficial,OFFICIAL_VERSION,OFFICIAL_RELATIONS_VERSION
from services.hub.budget import Budget
from services.hub.db import Store,encode
from services.hub.errors import HubError
from services.hub.source_receipts import SourceReceipts,digest
from test_x_official_source_v2 import png

POLICY='machine-tweet-v4-api-relations'


@pytest.fixture
def relations(runtime,monkeypatch):
    settings,store=runtime
    settings.config['sources']=[{'x_handle':'fixture_only'}]
    settings.config['budgets']['x'].update(daily_calls=100,monthly_calls=100,daily_usd=10,monthly_usd=10,max_call_usd=1,paid_authorized=True)
    monkeypatch.setenv('X_BEARER_TOKEN','owned-relations-synthetic-secret')
    class Server:
        def __init__(self):
            self.calls=[];self.downloads=[];self.kind='mixed';self.photos=False;self.failure=None;self.bytes=png()
            self.author={'id':'12345','username':'fixture_only','name':'Owned author'}
            self.other={'id':'67890','username':'quote_fixture','name':'Owned artist'}
            stamp=(datetime.now(timezone.utc)-timedelta(seconds=20)).isoformat()
            def post(ident,author,text):return {'id':ident,'author_id':author,'text':text,'created_at':stamp,'edit_history_tweet_ids':[ident]}
            self.root=post('900','12345','Owned reply and quote\nhttps://t.co/Owned')
            self.parent=post('800','12345','Owned earlier parent')
            self.quote=post('700','67890','Owned quoted full text')
        def __call__(self,request):
            self.calls.append(request.url.path)
            if '/by/username/' in request.url.path:body={'data':self.author}
            elif request.url.path=='/2/users/12345/tweets':
                value=deepcopy(self.root)
                refs=[]
                if self.kind in ('quote','mixed'):refs.append({'type':'quoted','id':'700'})
                if self.kind in ('reply','mixed'):refs.append({'type':'replied_to','id':'800'})
                if self.kind=='retweet':refs=[{'type':'retweeted','id':'700'}]
                value['referenced_tweets']=refs
                body={'data':[value]};self.media(body,value,'root')
                if self.failure=='root_author':value['author_id']='111'
            elif request.url.path in ('/2/tweets/700','/2/tweets/800'):
                quote=request.url.path.endswith('700');value=deepcopy(self.quote if quote else self.parent)
                body={'data':value,'includes':{'users':[deepcopy(self.other if quote else self.author)]}}
                self.media(body,value,'quote' if quote else 'parent')
                if quote:
                    if self.failure=='detail_id':value['id']='701'
                    if self.failure=='detail_author':body['includes']['users'][0]['id']='111'
                    if self.failure=='detail_error':body['errors']=[{'title':'Owned partial error'}]
                    if self.failure=='duplicate_author':body['includes']['users']*=2
                    if self.failure=='missing_media':body.get('includes',{}).pop('media',None)
            else:return httpx.Response(404,json={'errors':[{'title':'Owned missing'}]})
            return httpx.Response(200,content=json.dumps(body,ensure_ascii=False,indent=2).encode())
        def media(self,body,value,role):
            if self.photos:
                key='3_'+role;value['attachments']={'media_keys':[key]}
                body.setdefault('includes',{})['media']=[{'media_key':key,'type':'photo','url':f'https://pbs.twimg.com/media/Owned_{role}.png','width':8,'height':6}]
        def download(self,url,file,**options):
            self.downloads.append(url);options['check_disk'](len(self.bytes));Path(file).write_bytes(self.bytes)
            return {'file':str(file),'sha256':hashlib.sha256(self.bytes).hexdigest(),'bytes':len(self.bytes)}
    server=Server();adapter=XOfficial(ProviderHTTP(Budget(store,settings.config['budgets']),httpx.MockTransport(server)))
    ident=store.enqueue('scheduler','owned-relations-discovery','x_discovery','tweet',{},POLICY)
    job=store.claim('tweet',600,4);assert job['job_id']==ident
    return settings,store,server,adapter,job,SourceReceipts(settings,store)


def capture(case,version=OFFICIAL_RELATIONS_VERSION,**options):
    settings,store,server,adapter,job,ledger=case
    return adapter.latest_official('fixture_only',job_id=job['job_id'],recorder=lambda item:ledger.record(job,item),
        not_before=time.time()-7200,source_version=version,**options)


def ingest(case):
    settings,store,server,adapter,job,ledger=case
    response=capture(case);assert len(response['records'])==1,response
    result=ledger.ingest_official(job,response['records'][0],POLICY,downloader=server.download)
    return store.get(result['job_id']),response,result


@pytest.mark.parametrize('kind,roles,relationship',[('original',['post'],'original'),('reply',['post','parent'],'reply'),
    ('quote',['post','quote'],'quote'),('mixed',['post','parent','quote'],'reply_quote')])
def test_actual_recorded_relations_authors_text_time_and_node_photos(relations,kind,roles,relationship):
    settings,store,server,adapter,job,ledger=relations;server.kind=kind;server.photos=True
    child,response,result=ingest(relations)
    checked=ledger.assess_official(child,source_version=OFFICIAL_RELATIONS_VERSION)
    assert [n['role'] for n in checked['nodes']]==roles
    context=child['payload']['reference_context']
    assert context['relationship_type']==relationship and context['human_review'] is False
    assert context['expansion']=={'max_depth':1,'complete':True,'unexpanded_relations':[]}
    assert 'reply_context' not in child['payload']
    expected=[]
    if kind in ('quote','mixed'):expected.append({'type':'quoted','id':'700'})
    if kind in ('reply','mixed'):expected.append({'type':'replied_to','id':'800'})
    assert child['payload']['post']['referenced_tweets']==expected
    assert len(server.calls)==2+len(roles)-1 and len(server.downloads)==len(roles)
    for edge,node in zip(context['relations'],checked['nodes'][1:]):
        assert edge['source']==node['source'] and edge['post_id']==node['source']['post']['id']
        assert edge['role']==node['role'] and edge['type']==('quoted' if edge['role']=='quote' else 'replied_to')
        original=server.quote if edge['role']=='quote' else server.parent
        assert node['source']['post']['text']==original['text'] and node['source']['post']['created_at']==original['created_at']
    assert checked['proof']['version']=='x-official-source-proof-v2' and not checked['proof']['source_live_verified']
    assert all(n['source']['capture']['human_review'] is False for n in checked['nodes'])
    assert SourceReceipts(settings,Store(settings.db_path)).assess_official(child,source_version=OFFICIAL_RELATIONS_VERSION)==checked
    assert not store.query('SELECT * FROM publications') and not store.query("SELECT * FROM api_calls WHERE provider='model'")


@pytest.mark.parametrize('kind',['quote','mixed'])
def test_legacy_default_does_not_silently_expand_source_scope(relations,kind):
    settings,store,server,adapter,job,ledger=relations;server.kind=kind
    response=capture(relations,OFFICIAL_VERSION)
    assert response['records']==[] and response['rejected_posts']==[{'post_id':'900','error_code':'X_REFERENCE_TYPE_UNSUPPORTED'}]
    assert len(server.calls)==2


@pytest.mark.parametrize('failure,code',[('root_author','X_POST_IDENTITY_OR_TEXT_INVALID'),('detail_id','X_REFERENCE_IDENTITY_MISMATCH'),
    ('detail_author','X_REFERENCE_AUTHOR_MISSING'),('detail_error','X_RESOURCE_RESPONSE_INVALID'),
    ('duplicate_author','X_RESOURCE_RESPONSE_BOUND_EXCEEDED'),('missing_media','X_MEDIA_TYPE_OR_SOURCE_UNVERIFIED')])
def test_incomplete_or_wrong_reference_never_becomes_a_publishable_binding(relations,failure,code):
    settings,store,server,adapter,job,ledger=relations;server.failure=failure;server.photos=True
    if code in ('X_RESOURCE_RESPONSE_INVALID','X_RESOURCE_RESPONSE_BOUND_EXCEEDED'):
        with pytest.raises(HubError,match=code):capture(relations)
        assert not store.query('SELECT * FROM tweet_source_bindings') and not server.downloads
        return
    response=capture(relations)
    assert response['records']==[] and response['rejected_posts']==[{'post_id':'900','error_code':code}]
    assert not store.query('SELECT * FROM tweet_source_bindings') and not server.downloads


@pytest.mark.parametrize('kind',['quoted','replied_to','retweeted'])
def test_nested_reference_is_explicit_failure_after_raw_capture_not_fabricated(relations,kind):
    settings,store,server,adapter,job,ledger=relations
    server.quote['referenced_tweets']=[{'type':kind,'id':'600'}]
    response=capture(relations)
    assert not response['records'] and response['rejected_posts'][0]['error_code']=='X_NESTED_REFERENCE_UNSUPPORTED'
    rows=store.query("SELECT * FROM source_http_receipts WHERE path='/2/tweets/700'")
    assert len(rows)==1 and ledger._body(rows[0])['data']['referenced_tweets']==[{'type':kind,'id':'600'}]
    assert '/2/tweets/600' not in server.calls


def test_normal_retweet_remains_out_of_scope(relations):
    settings,store,server,adapter,job,ledger=relations;server.kind='retweet'
    assert capture(relations)['rejected_posts'][0]['error_code']=='X_REFERENCE_TYPE_UNSUPPORTED'
    assert len(server.calls)==2


def test_new_source_requires_matching_policy_and_old_reader_rejects_v2(relations):
    settings,store,server,adapter,job,ledger=relations;response=capture(relations)
    with pytest.raises(HubError,match='X_OFFICIAL_POLICY_INVALID'):
        ledger.ingest_official(job,response['records'][0],'machine-tweet-v3-api')
    child,_,_=ingest(relations)
    with pytest.raises(HubError,match='X_OFFICIAL_SOURCE_VERSION_INVALID'):ledger.assess_official(child)


@pytest.mark.parametrize('tamper',['edge_type','edge_source','post_relations','photo'])
def test_receipt_reconstruction_detects_coherent_payload_or_photo_tampering(relations,tamper):
    settings,store,server,adapter,job,ledger=relations;server.photos=True
    child,_,_=ingest(relations);value=deepcopy(child['payload'])
    if tamper=='edge_type':value['reference_context']['relations'][0]['type']='quoted'
    if tamper=='edge_source':value['reference_context']['relations'][1]['source']['author']['name']='Owned forged artist'
    if tamper=='post_relations':value['post']['referenced_tweets'].reverse()
    if tamper=='photo':
        Path(value['reference_context']['relations'][1]['source']['official_source']['local_media'][0]['file']).write_bytes(png(7,6))
    else:
        with store.transaction() as db:
            db.execute('UPDATE jobs SET payload=? WHERE job_id=?',(encode(value),child['job_id']))
            db.execute('UPDATE tweet_source_bindings SET payload_sha256=? WHERE job_id=?',(digest(value),child['job_id']))
    with pytest.raises(HubError):ledger.assess_official(store.get(child['job_id']),source_version=OFFICIAL_RELATIONS_VERSION)


def test_policy_upgrade_does_not_recreate_existing_legacy_bound_source(relations):
    settings,store,server,adapter,job,ledger=relations;server.kind='original'
    first=capture(relations,OFFICIAL_VERSION)
    old=ledger.ingest_official(job,first['records'][0],'machine-tweet-v3-api')
    second=capture(relations)
    result=ledger.ingest_official(job,second['records'][0],POLICY)
    assert result['created'] is False and result['job_id']==old['job_id']
    assert len(store.query("SELECT * FROM jobs WHERE job_type='tweet_process'"))==1


def test_expired_normal_source_is_skipped_before_detail_or_photo(relations):
    settings,store,server,adapter,job,ledger=relations
    server.root['created_at']=(datetime.now(timezone.utc)-timedelta(hours=3)).isoformat()
    response=capture(relations)
    assert response['records']==[] and response['skipped_posts']==[{'post_id':'900','reason':'outside_window'}]
    assert len(server.calls)==2 and not server.downloads
    with pytest.raises(HubError,match='OWNER_TWO_HOUR_WINDOW_EXPIRED'):
        ledger._official_publication_window(job,{'post':server.root})


def test_scoped_hold_is_atomic_and_never_claimable_as_new_machine_work(relations):
    settings,store,server,adapter,job,ledger=relations
    class Held(SourceReceipts):
        def _official_initial_job_state(self,discovery,payload):return 'upload_pending'
    response=capture(relations)
    result=Held(settings,store).ingest_official(job,response['records'][0],POLICY)
    assert store.get(result['job_id'])['state']=='upload_pending'
    assert store.claim('tweet',600,4) is None
    checked=ledger.assess_official(store.get(result['job_id']),source_version=OFFICIAL_RELATIONS_VERSION)
    assert checked['proof']['capture_integrity_passed'] and not checked['proof']['human_review']


def test_invalid_initial_state_rolls_back_parent_and_binding_together(relations):
    settings,store,server,adapter,job,ledger=relations
    class Invalid(SourceReceipts):
        def _official_initial_job_state(self,discovery,payload):return 'completed'
    response=capture(relations)
    with pytest.raises(HubError,match='X_OFFICIAL_INITIAL_STATE_INVALID'):
        Invalid(settings,store).ingest_official(job,response['records'][0],POLICY)
    assert not store.query("SELECT * FROM jobs WHERE job_type='tweet_process'")
    assert not store.query('SELECT * FROM tweet_source_bindings')


def test_new_discovery_explicitly_selects_v2_and_excludes_legacy_bound_jobs(relations,monkeypatch):
    from types import SimpleNamespace
    import services.hub.machine_tweets_api as machines
    from services.hub.x_api_discovery import XAPIDiscovery
    settings,store,server,adapter,job,ledger=relations;server.kind='original'
    response=capture(relations,OFFICIAL_VERSION)
    old=ledger.ingest_official(job,response['records'][0],'machine-tweet-v3-api')
    settings.config.update(x_api_enabled=True,x_web_enabled=False,policy_version=POLICY)
    fake=SimpleNamespace(version=POLICY,source_record_version=OFFICIAL_RELATIONS_VERSION,
        policy=lambda:{'max_post_age_seconds':7200,'activation_at':time.time()-30},job_policy=lambda:POLICY,clear_backlog=lambda:[])
    monkeypatch.setattr(machines,'MachineTweetsAPIReference',lambda *a:fake,raising=False)
    service=XAPIDiscovery(settings,store,None);seen=[]
    def latest(handle,**options):
        seen.append(options)
        return {'version':'x-official-discovery-v1','records':[],'skipped_posts':[],'rejected_posts':[],
            'observed_post_ids':[],'catchup_complete':True}
    service.x.latest_official=latest
    service.run(job)
    assert len(seen)==1 and seen[0]['source_version']==OFFICIAL_RELATIONS_VERSION
    assert server.root['id'] in seen[0]['known_ids']
    assert store.get(old['job_id'])['policy_version']=='machine-tweet-v3-api'


def test_dense_mixed_pages_make_progress_without_advancing_cursor_or_refetching_known(relations,monkeypatch):
    from types import SimpleNamespace
    import services.hub.machine_tweets_api as machines
    from services.hub.x_api_discovery import XAPIDiscovery
    settings,store,server,adapter,job,ledger=relations
    settings.config.update(x_api_enabled=True,x_web_enabled=False,policy_version=POLICY)
    fake=SimpleNamespace(version=POLICY,source_record_version=OFFICIAL_RELATIONS_VERSION,
        policy=lambda:{'max_post_age_seconds':7200,'activation_at':time.time()-30},job_policy=lambda:POLICY,clear_backlog=lambda:[])
    monkeypatch.setattr(machines,'MachineTweetsAPIReference',lambda *a:fake,raising=False)
    roots=[];targets={};calls=[]
    for index in range(8):
        value=deepcopy(server.root);value['id']=str(990-index);value['edit_history_tweet_ids']=[value['id']]
        parent_id=str(600-index*2);quote_id=str(599-index*2)
        value['referenced_tweets']=[{'type':'quoted','id':quote_id},{'type':'replied_to','id':parent_id}]
        roots.append(value)
        for ident in (parent_id,quote_id):
            target=deepcopy(server.quote);target['id']=ident;target['edit_history_tweet_ids']=[ident]
            targets[ident]=target
    def dense(request):
        calls.append(request.url.path)
        if '/by/username/' in request.url.path:body={'data':server.author}
        elif request.url.path=='/2/users/12345/tweets':
            second=request.url.params.get('pagination_token')=='owned-second-page'
            body={'data':roots[4:] if second else roots[:4]}
            if not second:body['meta']={'next_token':'owned-second-page'}
        else:
            ident=request.url.path.rsplit('/',1)[-1]
            assert ident in targets
            body={'data':targets[ident],'includes':{'users':[server.other]}}
        return httpx.Response(200,json=body)
    http=ProviderHTTP(Budget(store,settings.config['budgets']),httpx.MockTransport(dense))
    service=XAPIDiscovery(settings,store,http)
    # An existing cursor is preserved even though the first partial response
    # contains newer observed IDs and five durably captured media-stage jobs.
    with store.transaction() as db:db.execute('INSERT INTO source_cursors VALUES(?,?)',('x-api:fixture_only','100'))
    with pytest.raises(HubError,match='X_CATCHUP_BOUND_REACHED'):service.run(job)
    first=store.query("SELECT source_id FROM jobs WHERE job_type='x_official_media_ingest_v1'")
    assert len(first)==5 and len([v for v in calls if v.startswith('/2/tweets/')])==10
    assert store.query("SELECT value FROM source_cursors WHERE key='x-api:fixture_only'")[0]['value']=='100'
    assert not store.query("SELECT * FROM events WHERE name='task.failed'")
    cut=len(calls);result=service.run(job)
    assert result['new_jobs']==3 and len(store.query("SELECT * FROM jobs WHERE job_type='x_official_media_ingest_v1'"))==8
    assert len([v for v in calls[cut:] if v.startswith('/2/tweets/')])==6
    assert store.query("SELECT value FROM source_cursors WHERE key='x-api:fixture_only'")[0]['value']=='990'
    assert not store.query('SELECT * FROM tweet_source_bindings')  # photos/source acceptance belongs to the separate real-lease child
    assert not store.query('SELECT * FROM publications')
