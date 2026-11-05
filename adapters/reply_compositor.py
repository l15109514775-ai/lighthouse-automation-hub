"""Use the original quote container for a parent+reply review draft."""
from adapters.compositor import compositor_input
from services.hub.errors import HubError


def reply_compositor_input(reply_source,reply_translation,parent_source,parent_translation):
    try:
        context=reply_source['reply_context'];reply=reply_source['post'];parent=parent_source['post']
        refs=[r['id'] for r in reply.get('referenced_tweets',[]) if r.get('type')=='replied_to']
        if (context['version']!='reply-context-draft-v1' or context['parent_source']!=parent_source or
                refs!=[parent['id']] or parent['id']==reply['id'] or not parent['text'].strip() or
                not isinstance(parent_translation,str) or not parent_translation.strip() or
                not isinstance(reply_translation,str) or not reply_translation.strip() or
                any(r.get('type')=='replied_to' for r in parent.get('referenced_tweets',[]))):raise ValueError()
        for item in (reply_source,parent_source):
            if item['url']!=f"https://x.com/{item['author']['username']}/status/{item['post']['id']}":raise ValueError()
    except (KeyError,TypeError,ValueError,AttributeError):
        raise HubError('REPLY_PARENT_SOURCE_AND_TRANSLATION_REQUIRED',review=True) from None
    value=compositor_input(reply_source,reply_translation)
    original=compositor_input(parent_source,parent_translation)['post']
    value['post']['quote']=original
    value['post']['relationship']={'type':'reply','authorName':parent_source['author']['name'],
        'authorHandle':parent_source['author']['username'],'url':parent_source['url']}
    # Source/translation flags, media count and quote completeness remain unknown.
    value['post']['notes'].insert(0,'回复上下文草稿：下方引用区保留原推；原推、回复及关系仍须真实审核。')
    return value
