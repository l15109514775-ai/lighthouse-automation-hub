import os
import re
from copy import deepcopy
from datetime import datetime,timezone
import math
import time
import uuid
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
from services.hub.errors import HubError

OFFICIAL_VERSION='x-official-source-v1'
OFFICIAL_RELATIONS_VERSION='x-official-source-v2'
PRICING_SOURCE={'version':'x-resource-upper-bound-20261101','source':'https://docs.x.com/x-api/getting-started/pricing',
    'post_usd':0.005,'user_usd':0.010,'calculation':'requested resource upper bound; HTTP ledger may settle successful returned resource estimates',
    'actual_invoice_verified':False,'media_expansion_scope':'attachments of requested posts; no standalone media endpoint'}
TWEET_FIELDS='author_id,created_at,attachments,referenced_tweets,note_tweet,edit_history_tweet_ids,entities,possibly_sensitive,withheld,article,card_uri'
MEDIA_FIELDS='url,type,width,height'
USER_FIELDS='name,username,profile_image_url'

# Only named deterministic source-shape failures can be isolated per post.
# New transport, storage, budget or implementation errors must stop capture.
CONTENT_REJECTIONS=frozenset({'X_FULL_TEXT_NOT_VERIFIED','X_EDIT_HISTORY_NOT_VERIFIED',
    'X_REFERENCE_TYPE_UNSUPPORTED','X_CONTENT_TYPE_UNSUPPORTED','X_MEDIA_TYPE_UNSUPPORTED',
    'X_MEDIA_KEYS_INVALID','X_MEDIA_TYPE_OR_SOURCE_UNVERIFIED','X_MEDIA_SIZE_INVALID','X_MEDIA_URL_INVALID',
    'X_POST_IDENTITY_OR_TEXT_INVALID','X_REFERENCE_IDENTITY_MISMATCH','X_REFERENCE_AUTHOR_MISSING',
    'X_NESTED_REFERENCE_UNSUPPORTED'})

def parser_revision():return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _check(value,code):
    if not value:raise HubError(code,review=True)


def _id(value):return isinstance(value,str) and re.fullmatch(r'[0-9]{1,30}',value)


def _author(value,handle=None):
    _check(isinstance(value,dict) and _id(value.get('id')) and isinstance(value.get('username'),str) and
        re.fullmatch(r'[A-Za-z0-9_]{1,15}',value['username']) and isinstance(value.get('name'),str) and
        value['name'].strip() and len(value['name'])<=256,'X_IDENTITY_MISMATCH')
    if handle is not None:_check(value['username'].lower()==handle.lower(),'X_IDENTITY_MISMATCH')
    return deepcopy(value)


def is_youtube_live_link_card(post):
    """Recognize the observed URL-only card; its preview is not a photo."""
    try:
        card=post.get('card_uri');entities=post.get('entities',{});urls=entities.get('urls')
        if (not isinstance(card,str) or not re.fullmatch(r'https://t\.co/[A-Za-z0-9]{1,64}',card) or
                not isinstance(urls,list) or len(urls)!=1 or not isinstance(urls[0],dict) or
                post.get('attachments',{})!={}):return False
        item=urls[0];text=post.get('text');start=item.get('start');end=item.get('end')
        if (item.get('url')!=card or not isinstance(text,str) or text.count(card)!=1 or
                type(start) is not int or type(end) is not int or not 0<=start<end<=len(text) or
                text[start:end]!=card or type(item.get('status')) is not int or item['status']!=200):return False
        expanded=item.get('expanded_url')
        if not isinstance(expanded,str) or item.get('unwound_url')!=expanded:return False
        url=urlsplit(expanded);query=parse_qs(url.query,strict_parsing=True,keep_blank_values=True) if url.query else {}
        return (url.scheme=='https' and url.netloc=='www.youtube.com' and not url.fragment and
            re.fullmatch(r'/live/[A-Za-z0-9_-]{11}',url.path) is not None and set(query)<={'si'} and
            all(len(v)==1 and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',v[0]) for v in query.values()))
    except (AttributeError,TypeError,ValueError):
        return False


def native_context_card_notice(post):
    """Explicit text-only context mode; never attest an opaque native card."""
    card=post.get('card_uri') if isinstance(post,dict) else None
    if not isinstance(card,str) or not re.fullmatch(r'card://[0-9]{1,30}',card):return None
    return {'version':'x-official-native-context-card-text-v1','card_uri':card,
        'content_scope':'official_text_and_api_declared_photos','card_body_available':False,
        'card_body_verified':False,'human_review':False}


def _post(value,captured_at,*,source_version=OFFICIAL_VERSION,reference_target=False):
    _check(source_version in (OFFICIAL_VERSION,OFFICIAL_RELATIONS_VERSION),'X_OFFICIAL_SOURCE_VERSION_INVALID')
    _check(isinstance(value,dict) and _id(value.get('id')) and _id(value.get('author_id')),'X_POST_IDENTITY_OR_TEXT_INVALID')
    result=deepcopy(value);note=result.get('note_tweet')
    if note is not None:
        _check(isinstance(note,dict) and isinstance(note.get('text'),str) and note['text'].strip() and not note.get('truncated'),'X_FULL_TEXT_NOT_VERIFIED')
        result['text']=note['text']
    _check(isinstance(result.get('text'),str) and result['text'].strip() and len(result['text'])<=100000 and not result.get('truncated'),'X_FULL_TEXT_NOT_VERIFIED')
    # An explicitly truncated-looking API excerpt without long-form data is
    # uncertain. Do not turn an ellipsis/t.co excerpt into a full-text claim.
    _check(note is not None or not re.search(r'…\s+https://t\.co/[A-Za-z0-9]+\s*$',result['text']),'X_FULL_TEXT_NOT_VERIFIED')
    try:
        published=datetime.fromisoformat(result['created_at'].replace('Z','+00:00'))
        if published.tzinfo is None or not 2000<=published.year<=2100 or published.timestamp()>captured_at+300:raise ValueError()
    except (KeyError,TypeError,ValueError,AttributeError):raise HubError('X_PUBLICATION_TIME_INVALID',review=True) from None
    edits=result.get('edit_history_tweet_ids')
    _check(isinstance(edits,list) and edits and result['id'] in edits and all(_id(v) for v in edits) and len(set(edits))==len(edits),'X_EDIT_HISTORY_NOT_VERIFIED')
    refs=result.get('referenced_tweets',[])
    if source_version==OFFICIAL_VERSION:
        _check(isinstance(refs,list) and len(refs)<=1 and all(isinstance(r,dict) and r.get('type')=='replied_to' and _id(r.get('id')) for r in refs),'X_REFERENCE_TYPE_UNSUPPORTED')
    else:
        allowed=('replied_to','quoted','retweeted') if reference_target else ('replied_to','quoted')
        _check(isinstance(refs,list) and len(refs)<=2 and all(isinstance(r,dict) and set(r)=={'type','id'} and
            r.get('type') in allowed and _id(r.get('id')) and r['id']!=result['id'] for r in refs) and
            len({r['type'] for r in refs})==len(refs) and len({r['id'] for r in refs})==len(refs),'X_REFERENCE_TYPE_UNSUPPORTED')
    _check(not result.get('withheld') and not result.get('article') and
        (not result.get('card_uri') or is_youtube_live_link_card(result) or
         (source_version==OFFICIAL_RELATIONS_VERSION and reference_target and native_context_card_notice(result) is not None)),
        'X_CONTENT_TYPE_UNSUPPORTED')
    attachments=result.get('attachments',{})
    _check(isinstance(attachments,dict) and not attachments.get('poll_ids') and not attachments.get('media_source_tweet_id'),'X_MEDIA_TYPE_UNSUPPORTED')
    keys=attachments.get('media_keys',[])
    _check(isinstance(keys,list) and len(keys)<=4 and all(isinstance(k,str) and re.fullmatch(r'[A-Za-z0-9_]{1,100}',k) for k in keys) and len(set(keys))==len(keys),'X_MEDIA_KEYS_INVALID')
    return result


def _photos(post,body):
    includes=body.get('includes',{})
    _check(isinstance(includes,dict),'X_SOURCE_RESPONSE_INVALID')
    items=includes.get('media',[])
    _check(isinstance(items,list) and all(isinstance(m,dict) and isinstance(m.get('media_key'),str) for m in items),'X_MEDIA_RESPONSE_INVALID')
    _check(len({m['media_key'] for m in items})==len(items),'X_MEDIA_RESPONSE_INVALID')
    media={m['media_key']:m for m in items};result=[]
    for index,key in enumerate(post.get('attachments',{}).get('media_keys',[]),1):
        item=media.get(key)
        _check(item and item.get('type')=='photo','X_MEDIA_TYPE_OR_SOURCE_UNVERIFIED')
        _check(type(item.get('width')) is int and type(item.get('height')) is int and min(item['width'],item['height'])>0 and
            item['width']*item['height']<=40000000,'X_MEDIA_DIMENSIONS_INVALID')
        try:
            url=urlsplit(item['url']);query=parse_qs(url.query,strict_parsing=True) if url.query else {}
            _check(url.scheme=='https' and url.hostname=='pbs.twimg.com' and not url.port and not url.username and not url.password and not url.fragment and
                re.fullmatch(r'/media/[A-Za-z0-9_-]+(?:\.(?:jpg|jpeg|png|webp))?',url.path) and set(query)<={'format','name'} and
                all(len(v)==1 for v in query.values()),'X_MEDIA_URL_INVALID')
            fmt=(query.get('format',[url.path.rsplit('.',1)[-1] if '.' in url.path else ''])[0]).replace('jpeg','jpg')
            _check(fmt in ('jpg','png','webp'),'X_MEDIA_URL_INVALID')
        except (KeyError,TypeError,ValueError):raise HubError('X_MEDIA_URL_INVALID',review=True) from None
        result.append({'index':index,'media_key':key,'type':'photo','source_url':item['url'],'url':item['url'],
            'format':fmt,'width':item['width'],'height':item['height'],'creator_original_upload_identity_verified':False})
    return result


def build_official_record(handle,post_id,user,timeline,details,refs,metadata,materials=None,*,source_version=OFFICIAL_VERSION):
    """Pure deterministic reconstruction from response bodies and ledger metadata."""
    if source_version==OFFICIAL_RELATIONS_VERSION:
        return _build_official_relations(handle,post_id,user,timeline,details,refs,metadata,materials)
    _check(source_version==OFFICIAL_VERSION,'X_OFFICIAL_SOURCE_VERSION_INVALID')
    author=_author(user.get('data') if isinstance(user,dict) else None,handle)
    _check(isinstance(timeline,dict) and not user.get('errors') and not timeline.get('errors') and isinstance(timeline.get('data',[]),list),'X_SOURCE_RESPONSE_ERRORS')
    posts=[p for p in timeline.get('data',[]) if isinstance(p,dict) and p.get('id')==post_id]
    _check(len(posts)==1,'X_SOURCE_POST_NOT_UNIQUE')
    page_id=refs['timeline_call_id'];post=_post(posts[0],metadata[page_id]['captured_at'])
    _check(post['author_id']==author['id'],'X_POST_IDENTITY_OR_TEXT_INVALID')
    used=[refs['user_call_id'],page_id];parent=None;parent_call=None;parent_body=None
    relation=post.get('referenced_tweets',[])
    if relation:
        pid=relation[0]['id'];candidates=[]
        for call_id,body in details.items():
            _check(isinstance(body,dict) and not body.get('errors') and isinstance(body.get('data'),dict),'X_REFERENCE_RESPONSE_INVALID')
            _check(body['data'].get('id')==pid,'X_REFERENCE_IDENTITY_MISMATCH')
            candidates.append((call_id,body))
        if not candidates:
            included=timeline.get('includes',{}).get('tweets',[])
            _check(isinstance(included,list),'X_REFERENCE_RESPONSE_INVALID')
            candidates=[(page_id,{**timeline,'data':p}) for p in included if isinstance(p,dict) and p.get('id')==pid]
        _check(len(candidates)==1,'X_REFERENCE_DETAIL_REQUIRED')
        parent_call,parent_body=candidates[0];parent_post=_post(parent_body['data'],metadata[parent_call]['captured_at'])
        _check(not parent_post.get('referenced_tweets'),'X_NESTED_REFERENCE_UNSUPPORTED')
        users=parent_body.get('includes',{}).get('users',[])
        _check(isinstance(users,list),'X_REFERENCE_AUTHOR_MISSING')
        matches=[v for v in users if isinstance(v,dict) and v.get('id')==parent_post['author_id']]
        if parent_post['author_id']==author['id'] and not matches:matches=[author]
        _check(len(matches)==1,'X_REFERENCE_AUTHOR_MISSING');parent_author=_author(matches[0])
        parent={'post':parent_post,'author':parent_author};used.extend(refs['detail_call_ids'])
    else:_check(not details and not refs['detail_call_ids'],'X_UNEXPECTED_DETAIL_RECEIPT')
    _check(len(used)==len(set(used)),'X_DUPLICATE_RECEIPT_REFERENCE')
    materials=materials or {}
    def source(p,a,body,call_id,role):
        photos=_photos(p,body);selected={item['media_key']:deepcopy(next(m for m in body.get('includes',{}).get('media',[]) if m['media_key']==item['media_key'])) for item in photos}
        live=all(metadata[c]['transport_mode']=='default_tls' for c in used) and all(
            item.get('transport_mode')=='default_tls' for items in materials.values() for item in items)
        result={'post':p,'author':a,'media':selected,'references':[],'users':[],
            'url':f"https://x.com/{a['username']}/status/{p['id']}",'source_api':'x-v2-official',
            'source_media_count':len(photos),'references_observed':True,
            'capture':{'protocol':OFFICIAL_VERSION,'captured_at':datetime.fromtimestamp(metadata[call_id]['captured_at'],timezone.utc).isoformat(),
                'official_api_receipt':live,'human_review':False},
            'official_source':{'version':OFFICIAL_VERSION,'receipt_refs':deepcopy(refs),'local_media':deepcopy(materials.get(role,[])),
                'pricing_source':deepcopy(PRICING_SOURCE)}}
        return result,photos
    parent_source=None;parent_photos=[]
    if parent:parent_source,parent_photos=source(parent['post'],parent['author'],parent_body,parent_call,'parent')
    post_source,post_photos=source(post,author,timeline,page_id,'post')
    if parent_source:
        post_source['reply_context']={'version':'x-official-reply-context-v1','parent_source':parent_source,
            'relationship_basis':'api_referenced_tweets.replied_to','relationship_verified_by_human':False,'human_review':False}
        post_source['references']=[deepcopy(parent['post'])]
        post_source['users']=[deepcopy(parent['author'])]
    nodes=[{'role':'post','source':post_source,'media':post_photos,'receipt_ids':list(used)}]
    if parent_source:nodes.append({'role':'parent','source':parent_source,'media':parent_photos,'receipt_ids':list(used)})
    return {'version':OFFICIAL_VERSION,'payload':post_source,'nodes':nodes,'receipt_refs':deepcopy(refs)}


def _build_official_relations(handle,post_id,user,timeline,details,refs,metadata,materials=None):
    """Direct reply/quote edges only. Further edges remain explicit, unexpanded facts."""
    version=OFFICIAL_RELATIONS_VERSION
    author=_author(user.get('data') if isinstance(user,dict) else None,handle)
    _check(isinstance(timeline,dict) and not user.get('errors') and not timeline.get('errors') and
        isinstance(timeline.get('data',[]),list),'X_SOURCE_RESPONSE_ERRORS')
    _check(isinstance(refs,dict) and set(refs)=={'user_call_id','timeline_call_id','detail_call_ids'} and
        isinstance(refs['detail_call_ids'],list) and len(refs['detail_call_ids'])<=2 and
        len(set(refs['detail_call_ids']))==len(refs['detail_call_ids']) and set(details)==set(refs['detail_call_ids']),
        'X_REFERENCE_RECEIPTS_INVALID')
    page_id=refs['timeline_call_id'];posts=[p for p in timeline.get('data',[]) if isinstance(p,dict) and p.get('id')==post_id]
    _check(len(posts)==1,'X_SOURCE_POST_NOT_UNIQUE')
    post=_post(posts[0],metadata[page_id]['captured_at'],source_version=version)
    _check(post['author_id']==author['id'],'X_POST_IDENTITY_OR_TEXT_INVALID')
    direct=post.get('referenced_tweets',[])
    expected_ids={r['id'] for r in direct};found={}
    for call_id,body in details.items():
        _check(isinstance(body,dict) and not body.get('errors') and isinstance(body.get('data'),dict),'X_REFERENCE_RESPONSE_INVALID')
        pid=body['data'].get('id')
        _check(pid in expected_ids and pid not in found,'X_REFERENCE_IDENTITY_MISMATCH')
        found[pid]=(call_id,body)
    _check(set(found)==expected_ids,'X_REFERENCE_DETAIL_REQUIRED')
    used=[refs['user_call_id'],page_id,*refs['detail_call_ids']]
    _check(len(used)==len(set(used)),'X_DUPLICATE_RECEIPT_REFERENCE')
    materials=materials or {};targets=[];unexpanded=[]
    # Stable role order does not discard the original API reference ordering.
    for relationship in sorted(direct,key=lambda r:0 if r['type']=='replied_to' else 1):
        role='parent' if relationship['type']=='replied_to' else 'quote'
        call_id,body=found[relationship['id']]
        target=_post(body['data'],metadata[call_id]['captured_at'],source_version=version,reference_target=True)
        _check(not target.get('referenced_tweets'),'X_NESTED_REFERENCE_UNSUPPORTED')
        users=body.get('includes',{}).get('users',[])
        _check(isinstance(users,list),'X_REFERENCE_AUTHOR_MISSING')
        matches=[v for v in users if isinstance(v,dict) and v.get('id')==target['author_id']]
        if target['author_id']==author['id'] and not matches:matches=[author]
        _check(len(matches)==1,'X_REFERENCE_AUTHOR_MISSING')
        target_author=_author(matches[0])
        targets.append({'role':role,'type':relationship['type'],'post':target,'author':target_author,'body':body,'call_id':call_id})
        for nested in target.get('referenced_tweets',[]):
            unexpanded.append({'source_post_id':target['id'],'type':nested['type'],'post_id':nested['id'],'reason':'depth_limit'})
    live=all(metadata[c]['transport_mode']=='default_tls' for c in used) and all(
        item.get('transport_mode')=='default_tls' for items in materials.values() for item in items)
    def source(p,a,body,call_id,role):
        photos=_photos(p,body)
        selected={item['media_key']:deepcopy(next(m for m in body.get('includes',{}).get('media',[]) if m['media_key']==item['media_key'])) for item in photos}
        value={'post':p,'author':a,'media':selected,'references':[],'users':[],
            'url':f"https://x.com/{a['username']}/status/{p['id']}",'source_api':'x-v2-official',
            'source_media_count':len(photos),'references_observed':True,
            'capture':{'protocol':version,'captured_at':datetime.fromtimestamp(metadata[call_id]['captured_at'],timezone.utc).isoformat(),
                'official_api_receipt':live,'human_review':False},
            'official_source':{'version':version,'receipt_refs':deepcopy(refs),'local_media':deepcopy(materials.get(role,[])),
                'pricing_source':deepcopy(PRICING_SOURCE)}}
        if role!='post' and native_context_card_notice(p) is not None:
            value['native_card_context']=native_context_card_notice(p)
        return value,photos
    post_source,post_photos=source(post,author,timeline,page_id,'post');nodes=[];relations=[]
    for target in targets:
        value,photos=source(target['post'],target['author'],target['body'],target['call_id'],target['role'])
        nodes.append({'role':target['role'],'source':value,'media':photos,'receipt_ids':list(used)})
        relations.append({'type':target['type'],'post_id':target['post']['id'],'role':target['role'],'source':deepcopy(value)})
    relation_type='reply_quote' if len(relations)==2 else ('reply' if relations and relations[0]['type']=='replied_to' else 'quote' if relations else 'original')
    post_source['reference_context']={'version':'x-official-reference-context-v2','relationship_type':relation_type,
        'relations':relations,'expansion':{'max_depth':1,'complete':not unexpanded,'unexpanded_relations':unexpanded},'human_review':False}
    post_source['references']=[deepcopy(t['post']) for t in targets]
    post_source['users']=[deepcopy(t['author']) for t in targets]
    nodes.insert(0,{'role':'post','source':post_source,'media':post_photos,'receipt_ids':list(used)})
    return {'version':version,'payload':post_source,'nodes':nodes,'receipt_refs':deepcopy(refs)}


class XOfficial:
    def __init__(self, client):
        self.client = client

    @staticmethod
    def normalize(post,author,data,handle):
        if not isinstance(post,dict) or not isinstance(author,dict) or not isinstance(data.get('includes',{}),dict):
            raise HubError('X_SOURCE_RESPONSE_INVALID',review=True)
        note=post.get('note_tweet')
        if note is not None and (not isinstance(note,dict) or not isinstance(note.get('text'),str) or not note['text']):
            raise HubError('X_FULL_TEXT_NOT_VERIFIED',review=True)
        post={**post,'text':note['text'] if note is not None else post.get('text','')}
        if (not isinstance(post.get('id'),str) or not re.fullmatch(r'[0-9]{1,30}',post['id']) or
                post.get('author_id')!=author['id'] or not isinstance(post['text'],str) or not post['text']):
            raise HubError('X_POST_IDENTITY_OR_TEXT_INVALID',review=True)
        includes=data.get('includes',{})
        media_items=includes.get('media',[])
        if (not isinstance(media_items,list) or any(not isinstance(item,dict) or not isinstance(item.get('media_key'),str) for item in media_items)
                or len({item['media_key'] for item in media_items})!=len(media_items) or
                any(not isinstance(includes.get(key,[]),list) for key in ('tweets','users'))):
            raise HubError('X_SOURCE_RESPONSE_INVALID',review=True)
        media={item['media_key']:item for item in media_items}
        return {'post':post,'author':author,'media':media,
                'references':data.get('includes',{}).get('tweets',[]),
                'users':data.get('includes',{}).get('users',[]),
                'url':f"https://x.com/{handle}/status/{post['id']}",'source_api':'x-v2-official'}

    def latest(self, handle, *, job_id=None, since_id=None, recorder=None):
        if not re.fullmatch(r'[A-Za-z0-9_]{1,15}', handle):
            raise HubError('INVALID_X_HANDLE', review=True)
        token = os.getenv('X_BEARER_TOKEN')
        if not token:
            raise HubError('X_CREDENTIAL_REQUIRED', review=True)
        headers = {'Authorization': 'Bearer ' + token}
        def get(url,params):
            bound=.010 if '/by/username/' in url else .050
            if recorder is None:
                return self.client.request('x','GET',url,headers=headers,params=params,job_id=job_id,reserved_cost_usd=bound),None
            value,receipt=self.client.request_recorded('x','GET',url,headers=headers,params=params,job_id=job_id,reserved_cost_usd=bound)
            return value,recorder(receipt)
        user,user_receipt=get(f'https://api.x.com/2/users/by/username/{handle}',
                              {'user.fields':'name,username,profile_image_url'})
        author = user.get('data', {}) if isinstance(user,dict) else None
        if (not isinstance(author,dict) or not isinstance(author.get('username'),str) or
                author['username'].lower()!=handle.lower() or not isinstance(author.get('id'),str) or
                not re.fullmatch(r'[0-9]{1,30}',author['id'])):
            raise HubError('X_IDENTITY_MISMATCH', review=True)
        params = {'max_results':10, 'tweet.fields':'author_id,created_at,attachments,referenced_tweets,public_metrics,note_tweet,edit_history_tweet_ids',
                  'expansions':'attachments.media_keys',
                  'media.fields':'url,preview_image_url,type,width,height', 'user.fields':'name,username,profile_image_url'}
        if since_id:
            if not str(since_id).isdigit():
                raise HubError('INVALID_X_CURSOR', review=True)
            params['since_id'] = since_id
        pages, posts, post_receipts = [], [], {}
        # Bounded catch-up. Advance no global cursor, so unresolved pagination never silently skips posts.
        for _ in range(5):
            data,page_receipt=get(f"https://api.x.com/2/users/{author['id']}/tweets",params)
            if not isinstance(data,dict) or not isinstance(data.get('data',[]),list):
                raise HubError('X_SOURCE_RESPONSE_INVALID',review=True)
            if data.get('errors') and not data.get('data'):
                raise HubError('X_SOURCE_RESPONSE_ERRORS',review=True)
            pages.append(data)
            for post in data.get('data', []):
                normalized=self.normalize(post,author,data,handle)
                posts.append(normalized)
                post_receipts[post['id']]={'user_call_id':user_receipt,'timeline_call_id':page_receipt}
            next_token = data.get('meta', {}).get('next_token')
            if not next_token:
                return {'posts':posts,'pages':pages,'post_receipts':post_receipts,'catchup_complete':True}
            params['pagination_token'] = next_token
        return {'posts':posts,'pages':pages,'post_receipts':post_receipts,'catchup_complete':False}

    def latest_official(self,handle,*,job_id,recorder,not_before,since_id=None,known_ids=(),author_cache=None,source_version=OFFICIAL_VERSION,initial_pagination_token=None,record_consumer=None,rejection_consumer=None):
        _check(source_version in (OFFICIAL_VERSION,OFFICIAL_RELATIONS_VERSION),'X_OFFICIAL_SOURCE_VERSION_INVALID')
        _check(isinstance(handle,str) and re.fullmatch(r'[A-Za-z0-9_]{1,15}',handle),'INVALID_X_HANDLE')
        _check(isinstance(not_before,(int,float)) and not isinstance(not_before,bool) and math.isfinite(not_before) and
            time.time()-7205<=not_before<=time.time(),'X_OFFICIAL_TIME_WINDOW_INVALID')
        _check(callable(recorder) and str(uuid.UUID(job_id))==job_id,'X_OFFICIAL_RECORDER_REQUIRED')
        _check(record_consumer is None or callable(record_consumer),'X_OFFICIAL_CONSUMER_REQUIRED')
        _check(rejection_consumer is None or callable(rejection_consumer),'X_OFFICIAL_CONSUMER_REQUIRED')
        known=set(known_ids);_check(len(known)<=10000 and all(_id(v) for v in known),'INVALID_X_KNOWN_IDS')
        token=os.getenv('X_BEARER_TOKEN');_check(token,'X_CREDENTIAL_REQUIRED')
        headers={'Authorization':'Bearer '+token};metadata={};details_cache={};records=[];rejected=[];observed=[];skipped=[]
        def get(url,params,bound):
            body,item=self.client.request_recorded('x','GET',url,headers=headers,params=params,job_id=job_id,reserved_cost_usd=bound)
            call_id=recorder(item);_check(call_id==item['call_id'],'X_RECEIPT_RECORDER_MISMATCH')
            metadata[call_id]={'captured_at':item['captured_at'],'transport_mode':item['transport_mode']}
            return body,call_id
        if author_cache is not None:
            _check(isinstance(author_cache,dict) and author_cache.get('version')=='x-official-author-cache-v1' and
                isinstance(author_cache.get('captured_at'),(int,float)) and 0<=time.time()-author_cache['captured_at']<=86400,'X_AUTHOR_CACHE_INVALID')
            author=_author(author_cache.get('author'),handle);user={'data':author};user_id=author_cache['user_call_id']
            _check(str(uuid.UUID(user_id))==user_id,'X_AUTHOR_CACHE_INVALID')
            metadata[user_id]={'captured_at':author_cache['captured_at'],'transport_mode':author_cache['transport_mode']}
        else:
            user,user_id=get(f'https://api.x.com/2/users/by/username/{handle}',{'user.fields':USER_FIELDS},.010)
            author=_author(user.get('data') if isinstance(user,dict) else None,handle)
        params={'max_results':10,'tweet.fields':TWEET_FIELDS,'expansions':'attachments.media_keys','media.fields':MEDIA_FIELDS,
            'user.fields':USER_FIELDS,'start_time':datetime.fromtimestamp(not_before,timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')}
        if since_id is not None:_check(_id(str(since_id)),'INVALID_X_CURSOR');params['since_id']=str(since_id)
        if initial_pagination_token is not None:
            _check(isinstance(initial_pagination_token,str) and 0<len(initial_pagination_token)<=2048,'X_PAGINATION_TOKEN_INVALID')
            params['pagination_token']=initial_pagination_token
        for _ in range(5):
            body,page_id=get(f"https://api.x.com/2/users/{author['id']}/tweets",params,.050)
            _check(isinstance(body,dict) and not body.get('errors') and isinstance(body.get('data',[]),list),'X_SOURCE_RESPONSE_ERRORS')
            for candidate in body.get('data',[]):
                _check(isinstance(candidate,dict) and _id(candidate.get('id')),'X_SOURCE_RESPONSE_INVALID')
                pid=candidate['id'];observed.append(pid)
                if pid in known:skipped.append({'post_id':pid,'reason':'known_source'});continue
                record=None;rejection=None
                refs={'user_call_id':user_id,'timeline_call_id':page_id,'detail_call_ids':[]}
                try:
                    parsed=_post(candidate,metadata[page_id]['captured_at'],source_version=source_version)
                    if datetime.fromisoformat(parsed['created_at'].replace('Z','+00:00')).timestamp()<max(not_before,time.time()-7200):
                        skipped.append({'post_id':pid,'reason':'outside_window'});continue
                    _check(parsed['author_id']==author['id'],'X_POST_IDENTITY_OR_TEXT_INVALID')
                    details={}
                    for relationship in parsed.get('referenced_tweets',[]):
                        parent_id=relationship['id']
                        if parent_id not in details_cache:
                            if source_version==OFFICIAL_RELATIONS_VERSION and len(details_cache)>=10:
                                # Real records already captured may be ingested;
                                # no global cursor advances over this unfinished
                                # source. The next poll skips those known IDs and
                                # uses its unchanged bounded budget for the rest.
                                return {'version':'x-official-discovery-v1','records':records,'rejected_posts':rejected,
                                    'skipped_posts':skipped,'observed_post_ids':list(dict.fromkeys(observed)),
                                    'catchup_complete':False,'partial_reason':'X_REFERENCE_FETCH_BOUND_REACHED','continuation_token':params.get('pagination_token')}
                            _check(len(details_cache)<10,'X_REFERENCE_FETCH_BOUND_REACHED')
                            detail,detail_id=get(f'https://api.x.com/2/tweets/{parent_id}',{'tweet.fields':TWEET_FIELDS,
                                'expansions':'attachments.media_keys,author_id','media.fields':MEDIA_FIELDS,'user.fields':USER_FIELDS},.015)
                            details_cache[parent_id]=(detail_id,detail)
                        detail_id,detail=details_cache[parent_id];refs['detail_call_ids'].append(detail_id);details[detail_id]=detail
                    record=build_official_record(handle,pid,user,body,details,refs,metadata,source_version=source_version)
                except HubError as error:
                    if error.retryable or error.code not in CONTENT_REJECTIONS:raise
                    rejected.append({'post_id':pid,'error_code':error.code})
                    rejection={'version':'x-official-rejected-source-v1','discovery_job_id':job_id,'handle':handle,
                        'post_id':pid,'source_version':source_version,'error_code':error.code,'receipt_refs':deepcopy(refs),
                        'original_post_sha256':hashlib.sha256(json.dumps(candidate,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
                        'parser_sha256':parser_revision(),'human_review':False}
                if rejection is not None and rejection_consumer is not None:
                    rejection_consumer(rejection)  # durable failure must precede any cursor advance
                if record is not None:
                    # Durable-admission failures are lease/evidence failures,
                    # never permission to skip a post as unsupported content.
                    if record_consumer is not None:record_consumer(record)
                    records.append(record)
            next_token=body.get('meta',{}).get('next_token')
            if not next_token:return {'version':'x-official-discovery-v1','records':records,'rejected_posts':rejected,'skipped_posts':skipped,
                'observed_post_ids':list(dict.fromkeys(observed)),'catchup_complete':True}
            _check(isinstance(next_token,str) and 0<len(next_token)<=2048,'X_PAGINATION_TOKEN_INVALID');params['pagination_token']=next_token
        return {'version':'x-official-discovery-v1','records':records,'rejected_posts':rejected,'skipped_posts':skipped,
            'observed_post_ids':list(dict.fromkeys(observed)),'catchup_complete':False,'continuation_token':params.get('pagination_token')}
