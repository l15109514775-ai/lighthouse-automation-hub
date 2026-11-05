"""Owned API/PNG fixtures only; no real API, model, browser or publication.

The retained tweet.fields/note_tweet wire aliases are not live-verified. Current
official docs also expose post.fields/note_post; first live verification must
record actual endpoint compatibility, never treat these mocks as that evidence.
"""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
import hashlib
import json
from pathlib import Path
import struct
import time
import zlib
import httpx
import pytest
from adapters.http import ProviderHTTP
from adapters.x import XOfficial
from services.hub.budget import Budget
from services.hub.db import Store,encode
from services.hub.errors import HubError
from services.hub.source_receipts import SourceReceipts


def png(width=8,height=6):
    def chunk(name,data):return struct.pack('>I',len(data))+name+data+struct.pack('>I',zlib.crc32(name+data)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress((b'\0'+b'\x33\x66\x99'*width)*height))+chunk(b'IEND',b'')


@pytest.fixture
def official(runtime,monkeypatch):
    settings,store=runtime
    settings.config['sources']=[{'x_handle':'fixture_only'}]
    settings.config['budgets']['x'].update(daily_calls=100,monthly_calls=100,daily_usd=10,monthly_usd=10,max_call_usd=1,paid_authorized=True)
    monkeypatch.setenv('X_BEARER_TOKEN','owned-synthetic-official-token-only')
    class Server:
        def __init__(self):
            self.calls=[];self.downloads=[];self.reply=False;self.photo=False;self.bytes=png()
            self.author={'id':'12345','username':'fixture_only','name':'Owned fixture'}
            self.parent_author={'id':'67890','username':'parent_fixture','name':'Owned parent'}
            stamp=(datetime.now(timezone.utc)-timedelta(seconds=30)).isoformat()
            self.post={'id':'9876543210000000000','author_id':'12345','text':'Owned full source20:00','created_at':stamp,'edit_history_tweet_ids':['9876543210000000000']}
            self.parent={'id':'9876543200000000000','author_id':'67890','text':'Owned full parent19:00','created_at':stamp,'edit_history_tweet_ids':['9876543200000000000']}
        def __call__(self,request):
            self.calls.append(str(request.url))
            if '/by/username/' in request.url.path:body={'data':self.author}
            elif request.url.path=='/2/users/12345/tweets':
                post=deepcopy(self.post)
                if self.reply:post['referenced_tweets']=[{'type':'replied_to','id':self.parent['id']}]
                body={'data':[post]}
                if self.photo:
                    post['attachments']={'media_keys':['3_owned']}
                    body['includes']={'media':[{'media_key':'3_owned','type':'photo','url':'https://pbs.twimg.com/media/Owned.png','width':8,'height':6}]}
            elif request.url.path=='/2/tweets/'+self.parent['id']:body={'data':self.parent,'includes':{'users':[self.parent_author]}}
            else:return httpx.Response(404,json={'errors':[{'detail':'Owned missing'}]})
            return httpx.Response(200,content=json.dumps(body,ensure_ascii=False,indent=2).encode())
        def download(self,url,destination,**options):
            self.downloads.append(url);options['check_disk'](len(self.bytes));Path(destination).write_bytes(self.bytes)
            return {'file':str(destination),'sha256':hashlib.sha256(self.bytes).hexdigest(),'bytes':len(self.bytes)}
    server=Server();http=ProviderHTTP(Budget(store,settings.config['budgets']),httpx.MockTransport(server))
    ident=store.enqueue('scheduler','owned-official-source','x_discovery','tweet',{},'machine-tweet-v3-api')
    parent=store.claim('tweet',600,4);assert parent['job_id']==ident
    return settings,store,server,XOfficial(http),parent,SourceReceipts(settings,store)


def capture(case,**options):
    settings,store,server,adapter,parent,ledger=case
    return adapter.latest_official('fixture_only',job_id=parent['job_id'],recorder=lambda item:ledger.record(parent,item),not_before=time.time()-7200,**options)


def ingest(case):
    settings,store,server,adapter,parent,ledger=case
    response=capture(case);assert len(response['records'])==1,response
    result=ledger.ingest_official(parent,response['records'][0],'machine-tweet-v3-api',downloader=server.download)
    return store.get(result['job_id']),response,result


def test_plain_raw_receipts_atomic_binding_and_reopen_never_claim_custom_tls_live(official):
    settings,store,server,adapter,parent,ledger=official
    child,response,result=ingest(official);checked=ledger.assess_official(child)
    assert result['created'] and result['binding_present'] and len(server.calls)==2
    assert checked['nodes'][0]['source']==child['payload'] and checked['nodes'][0]['role']=='post'
    assert checked['proof']['capture_integrity_passed'] and not checked['proof']['source_live_verified']
    assert child['payload']['capture']['human_review'] is False and child['payload']['capture']['official_api_receipt'] is False
    assert 'screenshot' not in checked['nodes'][0] and 'screenshot_file' not in child['payload']['capture']
    assert SourceReceipts(settings,Store(settings.db_path)).assess_official(child)==checked
    authority=json.loads(Path(checked['nodes'][0]['api_authority']['file']).read_text('utf8'))
    assert authority['source_sha256']==checked['proof']['payload_sha256']
    assert all(c['http_status_class']=='2xx' and c['transport_mode']=='custom_transport' for c in authority['api_calls'])
    assert not store.query('SELECT * FROM publications') and not store.query("SELECT * FROM api_calls WHERE provider='model'")


def test_reply_parent_exact_id_full_text_time_and_receipts_are_bound(official):
    settings,store,server,adapter,parent,ledger=official;server.reply=True
    server.parent['note_tweet']={'text':'Owned complete long parent19:00\nSecond line'}
    child,response,result=ingest(official);checked=ledger.assess_official(child)
    assert len(server.calls)==3 and [n['role'] for n in checked['nodes']]==['post','parent']
    post,p=checked['nodes'];assert post['source']['reply_context']['parent_source']==p['source']
    assert p['source']['post']['text']==server.parent['note_tweet']['text']
    assert p['source']['post']['created_at']==server.parent['created_at']
    assert post['source']['post']['referenced_tweets']==[{'type':'replied_to','id':server.parent['id']}]
    assert len(post['source']['official_source']['receipt_refs']['detail_call_ids'])==1


def test_api_photo_is_really_decoded_before_child_exists_and_byte_tamper_blocks(official):
    settings,store,server,adapter,parent,ledger=official;server.photo=True
    child,response,result=ingest(official);checked=ledger.assess_official(child)
    media=checked['nodes'][0]['media'][0]
    assert server.downloads==['https://pbs.twimg.com/media/Owned?format=png&name=orig']
    assert (media['width'],media['height'],media['format'],media['completeness'])==(8,6,'png','complete')
    assert media['transport_mode']=='custom_transport' and not checked['proof']['source_live_verified']
    Path(media['file']).write_bytes(png(7,6))
    with pytest.raises(HubError):ledger.assess_official(child)


def test_bad_photo_stops_before_any_executable_child(official):
    settings,store,server,adapter,parent,ledger=official;server.photo=True;server.bytes=png(7,6)
    response=capture(official)
    with pytest.raises(HubError,match='X_MEDIA_DECLARED_DIMENSIONS_MISMATCH'):
        ledger.ingest_official(parent,response['records'][0],'machine-tweet-v3-api',downloader=server.download)
    assert not store.query("SELECT * FROM jobs WHERE job_type='tweet_process'") and not store.query('SELECT * FROM tweet_source_bindings')


def test_raw_or_ledger_tamper_cannot_be_replaced_by_official_flags(official):
    settings,store,server,adapter,parent,ledger=official;child,_,_=ingest(official)
    row=store.query('SELECT * FROM source_http_receipts LIMIT 1')[0];ledger._file(row).write_bytes(ledger._file(row).read_bytes()+b'\n')
    with pytest.raises(HubError,match='SOURCE_RESPONSE_INTEGRITY_CHANGED'):ledger.assess_official(child)


def test_repeat_and_cross_policy_publication_stop_before_model_or_media(official):
    settings,store,server,adapter,parent,ledger=official;child,response,result=ingest(official)
    repeat=ledger.ingest_official(parent,response['records'][0],'machine-tweet-v3-api',downloader=server.download)
    assert not repeat['created'] and repeat['job_id']==child['job_id']
    with store.transaction() as db:db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        ('owned-old-key',child['job_id'],'bilibili','x',child['source_id'],'site_uid:'+settings.config['target_bilibili_site_uid'],
         'dynamic_website_supervised','completed','123','https://t.bilibili.com/123',time.time()))
    repeat=ledger.ingest_official(parent,response['records'][0],'machine-tweet-v3-api:source-2',downloader=server.download)
    assert not repeat['created'] and repeat['already_published'] and not server.downloads


def test_author_cache_is_receipt_bound_and_expired_or_tampered_cache_is_ignored(official):
    settings,store,server,adapter,parent,ledger=official;capture(official)
    assert ledger.cached_author('fixture_only') is None
    cache=ledger.cached_author('fixture_only',require_live=False);assert cache['author']['id']=='12345'
    count=len(server.calls);capture(official,author_cache=cache);assert len(server.calls)==count+1
    with store.transaction() as db:db.execute("UPDATE api_calls SET state='unknown' WHERE call_id=?",(cache['user_call_id'],))
    assert ledger.cached_author('fixture_only',require_live=False) is None


def test_old_and_known_reply_never_fetch_parent_or_download_and_budget_zero_never_networks(official):
    settings,store,server,adapter,parent,ledger=official;server.reply=True
    response=capture(official,known_ids=[server.post['id']]);assert not response['records'] and len(server.calls)==2
    server.post['created_at']=(datetime.now(timezone.utc)-timedelta(hours=3)).isoformat()
    response=capture(official);assert not response['records'] and not any('/2/tweets/' in url for url in server.calls)
    settings.config['budgets']['x'].update(max_call_usd=0,paid_authorized=False,daily_usd=0,monthly_usd=0,daily_calls=0,monthly_calls=0)
    count=len(server.calls)
    with pytest.raises(HubError,match='BILLABLE_COST_BOUND_REQUIRED'):capture(official)
    assert len(server.calls)==count
