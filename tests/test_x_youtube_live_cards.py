"""Recorded-shape public URL cards; owned HTTP only, no API/model/publication."""
from copy import deepcopy
import time

import pytest

from adapters.x import OFFICIAL_VERSION,OFFICIAL_RELATIONS_VERSION,_post,is_youtube_live_link_card
from services.hub.errors import HubError
from services.hub.machine_tweets_api import MachineTweetsAPI,MachineTweetsAPIReference
from test_x_official_source_v2 import official,capture


def observed_card(index=0):
    # Public fields observed in saved call 6bf20216-b5af-4be8-8793-2349f6aaed23.
    texts=[
        '【#甘結もか2周年】素敵な3年目スタート🌸グッズ紹介するで :D【 ぶいすぽっ！甘結もか 】 https://t.co/ueBS7rTDLf @YouTubeより',
        '【＃蝶屋はなび2周年記念3Dライブ 】 烈日【 ぶいすぽ / 蝶屋はなび 】 https://t.co/WYtMQi12B1 @YouTubeより']
    short=['https://t.co/ueBS7rTDLf','https://t.co/WYtMQi12B1'][index]
    target=['https://www.youtube.com/live/NezWdClAj7I?si=5JtEjNuII3P6v-ib',
        'https://www.youtube.com/live/Iffi0n_GZ98?si=fhILRFf6agWuShB-'][index]
    text=texts[index];start=text.index(short)
    return {'text':text,'card_uri':short,'possibly_sensitive':False,'entities':{'urls':[
        {'start':start,'end':start+len(short),'url':short,'expanded_url':target,'unwound_url':target,
         'display_url':'youtube.com/live/owned…','status':200,'title':'Public YouTube live preview',
         'description':'Preview excerpt only…','images':[{'height':720,'width':1280,
             'url':'https://pbs.twimg.com/news_img/2117189497725300736/-TCc768r?format=jpg&name=orig'}]}]}}


@pytest.mark.parametrize('index',[0,1])
@pytest.mark.parametrize('version',[OFFICIAL_VERSION,OFFICIAL_RELATIONS_VERSION])
def test_saved_shape_card_survives_record_ingest_reopen_without_preview_download(official,index,version):
    settings,store,server,adapter,parent,ledger=official
    server.post.update(observed_card(index));before=deepcopy(server.post)
    response=capture(official,source_version=version)
    assert len(response['records'])==1 and not response['rejected_posts']
    policy='machine-tweet-v3-api' if version==OFFICIAL_VERSION else 'machine-tweet-v4-api-relations'
    result=ledger.ingest_official(parent,response['records'][0],policy,downloader=server.download)
    child=store.get(result['job_id']);source=ledger.assess_official(child,source_version=version)
    assert source['nodes'][0]['source']['post']==before==server.post
    assert child['payload']['post']['entities']==before['entities']
    assert child['payload']['source_media_count']==0 and source['nodes'][0]['media']==[]
    assert server.downloads==[] and len(server.calls)==2
    assert not source['proof']['source_live_verified'] and not child['payload']['capture']['human_review']
    service=(MachineTweetsAPI if version==OFFICIAL_VERSION else MachineTweetsAPIReference)(settings,store)
    caption=service._relation_caption({'post':{}},source)
    assert caption=='\n推文正文译图；YouTube链接预览缩略图未包含'
    plain=deepcopy(source);plain['nodes'][0]['source']['post'].pop('card_uri')
    assert service._relation_caption({'post':{}},plain)==''
    assert not store.query('SELECT * FROM publications') and not store.query("SELECT * FROM api_calls WHERE provider='model'")


@pytest.mark.parametrize('change',[
    'uri','duplicate','host','path','unwound','status','offset','text','empty_si','article','withheld','attached'])
def test_unverified_cards_and_unrelated_content_remain_rejected(change):
    post={'id':'123','author_id':'456','created_at':'2026-11-02T09:38:06Z','edit_history_tweet_ids':['123'],**observed_card()}
    item=post['entities']['urls'][0]
    if change=='uri':post['card_uri']='https://t.co/Other'
    elif change=='duplicate':post['entities']['urls'].append(deepcopy(item))
    elif change=='host':item['expanded_url']=item['unwound_url']='https://www.youtube.com.evil.invalid/live/NezWdClAj7I'
    elif change=='path':item['expanded_url']=item['unwound_url']='https://www.youtube.com/redirect?q=NezWdClAj7I'
    elif change=='unwound':item['unwound_url']='https://example.invalid/'
    elif change=='status':item['status']=True
    elif change=='offset':item['start']+=1
    elif change=='text':post['text']+=' '+post['card_uri']
    elif change=='empty_si':item['expanded_url']=item['unwound_url']='https://www.youtube.com/live/NezWdClAj7I?si='
    elif change=='article':post['article']={'id':'999'}
    elif change=='withheld':post['withheld']={'country_codes':['ZZ']}
    elif change=='attached':post['attachments']={'media_keys':['3_owned']}
    before=deepcopy(post)
    with pytest.raises(HubError,match='X_CONTENT_TYPE_UNSUPPORTED'):_post(post,time.time())
    assert post==before
    if change not in ('article','withheld'):assert not is_youtube_live_link_card(post)
