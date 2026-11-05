"""The existing renderer must retain both posts without fabricated review."""
from copy import deepcopy
from pathlib import Path
import pytest
from adapters.compositor import Compositor
from adapters.reply_compositor import reply_compositor_input
from services.hub.errors import HubError


def pair():
    parent={'post':{'id':'122','text':'Owned parent fixture','created_at':'2026-10-30T12:00:00Z'},
        'author':{'name':'Owned original fixture','username':'parent_fixture'},
        'url':'https://x.com/parent_fixture/status/122','source_api':'x-web-dom-v1',
        'source_media_count':None,'references_observed':False}
    reply={'post':{'id':'123','text':'Owned reply fixture','created_at':'2026-10-30T13:00:00Z',
        'referenced_tweets':[{'type':'replied_to','id':'122'}]},
        'author':{'name':'Owned reply fixture','username':'fixture_only'},
        'url':'https://x.com/fixture_only/status/123','source_api':'x-web-dom-v1',
        'source_media_count':None,'references_observed':False,
        'reply_context':{'version':'reply-context-draft-v1','parent_source':deepcopy(parent),
            'relationship_verified_by_human':False,'parent_candidate_only':True}}
    return reply,parent


def test_reply_requires_parent_text_translation_and_matching_id():
    reply,parent=pair()
    with pytest.raises(HubError,match='REPLY_PARENT_SOURCE_AND_TRANSLATION_REQUIRED'):
        reply_compositor_input(reply,'仅测试回复',parent,'')
    other=deepcopy(parent);other['post']['id']='999'
    with pytest.raises(HubError,match='REPLY_PARENT_SOURCE_AND_TRANSLATION_REQUIRED'):
        reply_compositor_input(reply,'仅测试回复',other,'仅测试原推')


def test_existing_renderer_contains_both_posts_and_still_blocks_publication(tmp_path):
    reply,parent=pair()
    value=reply_compositor_input(reply,'仅测试回复，未发布。',parent,'仅测试原推，未发布。')
    rendered=Compositor(Path(__file__).resolve().parents[1]).render(value,tmp_path)
    report=rendered['report']
    assert report['checks']['translationExact'] and report['checks']['quoteComplete']
    assert 'post.quote' in report['layout']['quotes']
    assert {'post.translation','post.quote.translation'}<={b['field'] for b in report['layout']['blocks']}
    assert not report['publishable']
    assert value['post']['relationship']=={'type':'reply','authorName':'Owned original fixture',
        'authorHandle':'parent_fixture','url':parent['url']}
    for post in (value['post'],value['post']['quote']):
        assert post['sourceScreenshot']['reviewed'] is False
        assert not any(post['review'].values())
        assert post['sourceMediaCount'] is None and post['sourceQuote']=='unknown'
