"""Owned URL/PNG and recorded MockTransport fixtures; no real CDN/API/model I/O."""
from copy import deepcopy
from pathlib import Path

import pytest

from adapters.official_photo_url import official_photo_retrieval,validate_photo_retrieval,RETRIEVAL_RULE
from services.hub.db import Store,encode
from services.hub.errors import HubError
from services.hub.source_receipts import SourceReceipts,digest
from test_x_official_source_v2 import official,ingest,capture,png


@pytest.mark.parametrize('source,expected',[
    ('https://pbs.twimg.com/media/Owned.jpg','https://pbs.twimg.com/media/Owned?format=jpg&name=orig'),
    ('https://pbs.twimg.com/media/Owned.jpeg','https://pbs.twimg.com/media/Owned?format=jpg&name=orig'),
    ('https://pbs.twimg.com/media/Owned.png','https://pbs.twimg.com/media/Owned?format=png&name=orig'),
    ('https://pbs.twimg.com/media/Owned.webp','https://pbs.twimg.com/media/Owned?format=webp&name=orig'),
    ('https://pbs.twimg.com/media/Owned?format=png&name=large','https://pbs.twimg.com/media/Owned?format=png&name=orig'),
])
def test_only_same_id_same_format_orig_rendition_is_derived(source,expected):
    rule=official_photo_retrieval(source)
    assert rule=={'download_url':expected,'retrieval_rule':RETRIEVAL_RULE}
    assert validate_photo_retrieval(rule,source)==expected
    assert validate_photo_retrieval({},source)==source


@pytest.mark.parametrize('url',[
    'http://pbs.twimg.com/media/Owned.jpg','https://evil.test/media/Owned.jpg',
    'https://pbs.twimg.com.evil.test/media/Owned.jpg','https://pbs.twimg.com:443/media/Owned.jpg',
    'https://user@pbs.twimg.com/media/Owned.jpg','https://pbs.twimg.com/media/Owned.jpg#fragment',
    'https://pbs.twimg.com/media/Owned.jpg?','https://pbs.twimg.com/media/Owned.jpg#',
    'https://pbs.twimg.com/media/../Owned.jpg','https://pbs.twimg.com/media/%4fwned.jpg',
    'https://pbs.twimg.com/media/Owned.gif','https://pbs.twimg.com/media/Owned',
    'https://pbs.twimg.com/media/Owned.jpg?format=png','https://pbs.twimg.com/media/Owned.jpg?format=jpg&format=jpg',
    'https://pbs.twimg.com/media/Owned.jpg?crop=1','https://pbs.twimg.com/media/Owned.jpg?name=',
    'https://pbs.twimg.com/media/Own\ned.jpg','https://pbs.twimg.com/media/Owned.jpg?format=',
])
def test_source_url_aliases_or_unbounded_transformations_are_rejected(url):
    with pytest.raises(HubError,match='X_OFFICIAL_MEDIA_RETRIEVAL_INVALID'):official_photo_retrieval(url)


@pytest.mark.parametrize('change',[
    {'download_url':None},{'retrieval_rule':None},{'retrieval_rule':'unversioned'},
    {'download_url':'https://pbs.twimg.com/media/Other?format=png&name=orig'},
    {'download_url':'https://pbs.twimg.com/media/Owned?format=jpg&name=orig'},
    {'download_url':'https://pbs.twimg.com/media/Owned?format=png&name=small'},
    {'download_url':'https://pbs.twimg.com/media/Owned?format=png&name=orig&crop=1'},
    {'download_url':'https://pbs.twimg.com/media/Owned?format=png&name=orig&name=orig'},
    {'download_url':'https://evil.test/media/Owned?format=png&name=orig'},
])
def test_local_retrieval_descriptor_cannot_change_or_half_omit_its_rule(change):
    source='https://pbs.twimg.com/media/Owned.png'
    item=official_photo_retrieval(source)
    for key,value in change.items():
        if value is None:del item[key]
        else:item[key]=value
    with pytest.raises(HubError,match='X_OFFICIAL_MEDIA_RETRIEVAL_INVALID'):validate_photo_retrieval(item,source)


def test_new_media_binds_api_url_and_download_rule_without_replacing_raw_receipts(official):
    settings,store,server,adapter,parent,ledger=official;server.photo=True
    child,response,_=ingest(official)
    source=child['payload'];item=source['official_source']['local_media'][0]
    assert source['media']['3_owned']['url']==item['source_url']=='https://pbs.twimg.com/media/Owned.png'
    assert item['download_url']==server.downloads[0]=='https://pbs.twimg.com/media/Owned?format=png&name=orig'
    assert item['retrieval_rule']==RETRIEVAL_RULE
    raw={r['call_id']:ledger._file(r).read_bytes() for r in store.query('SELECT * FROM source_http_receipts')}
    assessed=ledger.assess_official(child)
    assert assessed['nodes'][0]['media']==source['official_source']['local_media']
    assert SourceReceipts(settings,Store(settings.db_path)).assess_official(child)==assessed
    assert all(ledger._file(r).read_bytes()==raw[r['call_id']] for r in store.query('SELECT * FROM source_http_receipts'))
    assert not assessed['proof']['source_live_verified'] and not source['capture']['human_review']
    assert not store.query('SELECT * FROM publications') and not store.query("SELECT * FROM api_calls WHERE provider='model'")


def test_owned_legacy_media_without_new_fields_keeps_exact_api_url_semantics(official):
    settings,store,server,adapter,parent,ledger=official;server.photo=True
    child,_,_=ingest(official)
    # Explicit owned historical persistence fixture, not a rewrite of real data.
    legacy=deepcopy(child['payload']);item=legacy['official_source']['local_media'][0]
    del item['download_url'];del item['retrieval_rule']
    with store.transaction() as db:
        db.execute('UPDATE jobs SET payload=? WHERE job_id=?',(encode(legacy),child['job_id']))
        db.execute('UPDATE tweet_source_bindings SET payload_sha256=? WHERE job_id=?',(digest(legacy),child['job_id']))
    old=store.get(child['job_id']);checked=ledger.assess_official(old)
    assert checked['nodes'][0]['source']==legacy and checked['nodes'][0]['media']==[item]
    assert not {'download_url','retrieval_rule'}&set(checked['nodes'][0]['media'][0])
    assert SourceReceipts(settings,Store(settings.db_path)).assess_official(old)==checked


@pytest.mark.parametrize('change',[
    {'download_url':'https://pbs.twimg.com/media/Other?format=png&name=orig'},
    {'download_url':'https://pbs.twimg.com/media/Owned?format=png&name=orig&crop=1'},
    {'retrieval_rule':'unversioned'},{'download_url':None},
])
def test_assessment_rederives_retrieval_even_for_a_self_consistent_persisted_packet(official,change):
    settings,store,server,adapter,parent,ledger=official;server.photo=True
    child,_,_=ingest(official);payload=deepcopy(child['payload'])
    item=payload['official_source']['local_media'][0]
    for key,value in change.items():
        if value is None:del item[key]
        else:item[key]=value
    # Owned fault injection makes the outer stored hash self-consistent to
    # specifically exercise derivation from the untouched recorded API URL.
    with store.transaction() as db:
        db.execute('UPDATE jobs SET payload=? WHERE job_id=?',(encode(payload),child['job_id']))
        db.execute('UPDATE tweet_source_bindings SET payload_sha256=? WHERE job_id=?',(digest(payload),child['job_id']))
    with pytest.raises(HubError,match='X_OFFICIAL_MEDIA_RETRIEVAL_INVALID'):
        ledger.assess_official(store.get(child['job_id']))
    assert not store.query('SELECT * FROM publications') and not store.query("SELECT * FROM api_calls WHERE provider='model'")


def test_plain_payload_and_persistent_authority_bytes_are_unchanged(official,monkeypatch):
    settings,store,server,adapter,parent,ledger=official
    def forbidden(*args):pytest.fail('No photo retrieval derivation belongs in a plain historical source')
    monkeypatch.setattr('services.hub.source_receipts.official_photo_retrieval',forbidden)
    child,response,_=ingest(official)
    assert child['payload']==response['records'][0]['payload']
    before=encode(child['payload']).encode();checked=ledger.assess_official(child)
    file=Path(checked['nodes'][0]['api_authority']['file']);raw=file.read_bytes()
    assert SourceReceipts(settings,Store(settings.db_path)).assess_official(child)==checked
    assert file.read_bytes()==raw and encode(store.get(child['job_id'])['payload']).encode()==before
    assert child['payload']['official_source']['local_media']==[]


def test_orig_rendition_still_requires_api_exact_decoded_pixels_before_child(official):
    settings,store,server,adapter,parent,ledger=official;server.photo=True;server.bytes=png(7,6)
    response=capture(official)
    with pytest.raises(HubError,match='X_MEDIA_DECLARED_DIMENSIONS_MISMATCH'):
        ledger.ingest_official(parent,response['records'][0],'machine-tweet-v3-api',downloader=server.download)
    assert server.downloads==['https://pbs.twimg.com/media/Owned?format=png&name=orig']
    assert not store.query('SELECT * FROM tweet_source_bindings')
    assert not store.query("SELECT * FROM jobs WHERE job_type='tweet_process'")
    assert not store.query("SELECT * FROM api_calls WHERE provider='model'")
