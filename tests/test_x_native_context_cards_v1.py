"""Owned API receipts; opaque cards are disclosed, never fabricated as media."""
from copy import deepcopy
import time
import pytest
from adapters.x import _post,OFFICIAL_VERSION,OFFICIAL_RELATIONS_VERSION,native_context_card_notice
from services.hub.errors import HubError
from services.hub.machine_tweets_api import MachineTweetsAPIReference
from test_x_official_relations_v2 import relations,capture,ingest

def test_context_native_card_keeps_real_text_photos_and_explicit_missing_card_scope(relations):
    settings,store,server,adapter,discovery,receipts=relations
    server.kind='quote';server.photos=True;server.quote['card_uri']='card://2116051792186191872'
    original=deepcopy(server.quote);job,response,_=ingest(relations)
    checked=receipts.assess_official(job,source_version=OFFICIAL_RELATIONS_VERSION)
    quote=checked['nodes'][1];notice=quote['source']['native_card_context']
    assert quote['role']=='quote' and quote['source']['post']['card_uri']==original['card_uri']
    assert quote['source']['post']['text']==original['text'] and quote['source']['post']['created_at']==original['created_at']
    assert notice==native_context_card_notice(original) and notice['version']=='x-official-native-context-card-text-v1'
    assert notice['card_body_available'] is notice['card_body_verified'] is notice['human_review'] is False
    assert len(quote['media'])==len(checked['nodes'][0]['media'])==1
    assert len(server.downloads)==2 and not store.query('SELECT * FROM publications')
    service=object.__new__(MachineTweetsAPIReference)
    caption=service._relation_caption({},checked)
    assert 'API未提供原帖卡片内容' in caption and '译图未包含该卡片' in caption
    assert server.quote==original

@pytest.mark.parametrize('version,target',[(OFFICIAL_VERSION,False),(OFFICIAL_VERSION,True),(OFFICIAL_RELATIONS_VERSION,False)])
def test_native_card_exception_cannot_enable_primary_or_legacy_posts(relations,version,target):
    post=deepcopy(relations[2].quote);post['card_uri']='card://123456'
    with pytest.raises(HubError,match='X_CONTENT_TYPE_UNSUPPORTED'):_post(post,time.time(),source_version=version,reference_target=target)

@pytest.mark.parametrize('change',['article','withheld','poll','malformed_card','bad_text'])
def test_native_context_still_rejects_unverifiable_content(relations,change):
    post=deepcopy(relations[2].quote);post['card_uri']='card://123456'
    if change=='article':post['article']={'id':'987'}
    elif change=='withheld':post['withheld']={'country_codes':['XX']}
    elif change=='poll':post['attachments']={'poll_ids':['888']}
    elif change=='malformed_card':post['card_uri']='card://123456/hidden'
    else:post['truncated']=True
    with pytest.raises(HubError):_post(post,time.time(),source_version=OFFICIAL_RELATIONS_VERSION,reference_target=True)

@pytest.mark.parametrize('change',['missing','claims_verified','wrong_role','unexpected_marker'])
def test_publication_caption_cannot_suppress_or_upgrade_the_card_notice(relations,change):
    _,_,server,_,_,_=relations;server.kind='quote';server.quote['card_uri']='card://123456'
    record=capture(relations)['records'][0];source={'nodes':deepcopy(record['nodes'])}
    if change=='missing':source['nodes'][1]['source'].pop('native_card_context')
    elif change=='claims_verified':source['nodes'][1]['source']['native_card_context']['card_body_verified']=True
    elif change=='wrong_role':source['nodes'][1]['role']='post'
    else:source['nodes'][0]['source']['native_card_context']=native_context_card_notice(server.quote)
    with pytest.raises(HubError,match='MACHINE_NATIVE_CONTEXT_CARD_NOTICE_REQUIRED'):
        object.__new__(MachineTweetsAPIReference)._link_card_caption(source)
