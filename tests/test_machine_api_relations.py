"""Owned HTTP/SQLite/render evidence only; never live or publication approval."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess

import pytest

from adapters.compositor import compositor_input
from services.hub.errors import HubError
from services.hub.machine_tweets_api import (MachineTweetsAPI, MachineTweetsAPIReference,
    REFERENCE_VERSION, official_reference_graph)
from services.hub.machine_routes import machine_service, is_machine_policy
from services.hub.model_contract import V7, payload_for
from services.hub.model_receipts import ModelReceipts
from test_x_official_relations_v2 import relations, ingest
from test_model_receipts import model_fixture


@pytest.mark.parametrize('kind,roles', [('quote',['post','quote']),('mixed',['post','parent','quote'])])
def test_recorded_source_graph_and_input_keep_reply_and_quote_distinct(relations,kind,roles):
    settings,store,server,*_=relations;server.kind=kind
    job,_,_=ingest(relations)
    source=relations[-1].assess_official(job,source_version='x-official-source-v2')
    before=deepcopy(source);context=official_reference_graph(source)
    assert [n['role'] for n in source['nodes']]==roles
    service=MachineTweetsAPIReference(settings,store)
    prepared={node['role']:compositor_input(node['source'],'Owned complete translation')['post'] for node in source['nodes']}
    for node in prepared.values():node['sourceMediaCount']=0;node['media']=[]
    item=service._render_input(prepared,source)
    service._validate_render_relationships(item,source)
    assert item['post']['relationship']['type']==('reply_quote' if kind=='mixed' else 'quote')
    assert set(item['contextPosts'])==set(roles)-{'post'} and source==before
    assert '引' in service._relation_caption(prepared,source)
    changed=deepcopy(item);changed['post']['relationship']={'type':'reply'}
    with pytest.raises(ValueError):service._validate_render_relationships(changed,source)
    assert all(not any(p['review'].values()) for p in [item['post'],*item['contextPosts'].values()])
    assert context['expansion']['complete'] is True and not store.query('SELECT * FROM publications')


def test_graph_cannot_drop_a_node_swap_reference_type_or_hide_nested_context(relations):
    job,_,_=ingest(relations);source=relations[-1].assess_official(job,source_version='x-official-source-v2')
    for change in ('missing','type','nested','incomplete'):
        changed=deepcopy(source)
        if change=='missing':changed['nodes'].pop()
        elif change=='type':changed['nodes'][0]['source']['reference_context']['relations'][0]['type']='quoted'
        elif change=='nested':changed['nodes'][1]['source']['post']['referenced_tweets']=[{'type':'quoted','id':'999'}]
        else:changed['nodes'][0]['source']['reference_context']['expansion']['complete']=False
        with pytest.raises(HubError,match='REFERENCE_GRAPH_INVALID'):official_reference_graph(changed)


def test_v7_each_node_has_independent_persisted_translation_and_qa_with_typed_context(model_fixture):
    settings,store,_,server,model,_=model_fixture
    settings.config['policy_version']=REFERENCE_VERSION
    original=deepcopy(model_fixture[2]['payload']);original['post']['referenced_tweets']=[]
    parent=deepcopy(original);parent['post']['id']='710';parent['url']='https://x.com/fixture_only/status/710'
    quote=deepcopy(original);quote['post']['id']='720';quote['url']='https://x.com/fixture_only/status/720'
    original['post']['referenced_tweets']=[{'type':'quoted','id':'720'},{'type':'replied_to','id':'710'}]
    original['reference_context']={'version':'x-official-reference-context-v2','relationship_type':'reply_quote',
        'relations':[{'type':'replied_to','post_id':'710','role':'parent','source':parent},
                     {'type':'quoted','post_id':'720','role':'quote','source':quote}],
        'expansion':{'max_depth':1,'complete':True,'unexpanded_relations':[]},'human_review':False}
    ident=store.enqueue('x',original['post']['id'],'tweet_process','tweet',original,REFERENCE_VERSION)
    root=store.get(ident);service=MachineTweetsAPIReference(settings,store)
    children=[]
    for role,source in (('post',original),('parent',parent),('quote',quote)):
        child=service.model_node(root,{'role':role,'source':source});children.append(child)
        translated=model.invoke(child,'translation',source)
        advice=model.invoke(child,'quality',source,translated['translation'])
        assert advice['passed'] is True
        proof=ModelReceipts(store).assess(child,translated['translation'])
        assert proof['proof_integrity_passed'] and proof['separate_requests_verified'] and proof['qa_recommends_pass']
        assert not proof['live_transport_verified'] and proof['human_review'] is False
    rows=store.query('SELECT * FROM model_call_receipts')
    assert len(rows)==6 and len({r['call_id'] for r in rows})==6 and {r['contract_version'] for r in rows}=={V7}
    assert len(server.calls)==6 and len({c['job_id'] for c in rows})==3
    for request in server.calls[:2]:
        view=json.loads(json.loads(request.content)['messages'][1]['content'])
        assert [x['type'] for x in view['context']['direct_relations']]==['replied_to','quoted']
        assert [x['text'] for x in view['context']['direct_relations']]==[parent['post']['text'],quote['post']['text']]
        assert view['source']['post']['text']==original['post']['text']
    # Restart reuses the exact node/contract and recorded pair, not another QA.
    assert service.model_node(root,{'role':'quote','source':quote})['job_id']==children[-1]['job_id']
    model.invoke(children[-1],'translation',quote)
    assert len(server.calls)==6 and not store.query('SELECT * FROM publications')
    assert not store.query('SELECT * FROM publication_reviews')


def test_new_policy_dispatch_does_not_expand_old_api_scope(runtime):
    settings,store=runtime
    assert isinstance(machine_service(settings,store,REFERENCE_VERSION),MachineTweetsAPIReference)
    assert type(machine_service(settings,store,'machine-tweet-v3-api')) is MachineTweetsAPI
    assert is_machine_policy(REFERENCE_VERSION+':source-2')
    assert MachineTweetsAPI.model_contract_version=='model-text-contract-v6'
    assert MachineTweetsAPIReference.model_contract_version==V7
    assert MachineTweetsAPI.node_roles==('post','parent')
    assert MachineTweetsAPIReference.content_scope!=MachineTweetsAPI.content_scope


def test_original_core_template_renders_both_complete_graphs_and_blocks_wrong_edges_or_quote_qa(tmp_path):
    root=Path(__file__).resolve().parents[1]
    env={**os.environ,'PLAYWRIGHT_BROWSERS_PATH':str(root/'compositor/runtime/browsers')}
    result=subprocess.run(['node',str(root/'tests/official_api_relations_render_fixture.mjs'),str(tmp_path/'owned-relations')],
        cwd=root,env=env,capture_output=True,text=True,encoding='utf-8',timeout=150)
    assert result.returncode==0,result.stderr
    record=json.loads(result.stdout)
    assert record['passed']==5 and record['external_calls']==record['model_calls']==record['publish_requests']==0
    assert record['human_review'] is False and len(record['reports'])==2


def test_new_publication_policy_still_has_same_deadline_check():
    root=Path(__file__).resolve().parents[1]
    code="""import assert from 'node:assert/strict';
import {assertPublicationWindow} from './adapters/publication_window.mjs';
for(const policy of ['machine-tweet-v3-api','machine-tweet-v4-api-relations']){
 assert.doesNotThrow(()=>assertPublicationWindow({machine_policy:policy,dispatch_expires_at:2000},1000000));
 assert.throws(()=>assertPublicationWindow({machine_policy:policy,dispatch_expires_at:1000},1000000),/OWNER_TWO_HOUR_WINDOW_EXPIRED/);
}console.log('owned-window-ok');"""
    result=subprocess.run(['node','--input-type=module','-e',code],cwd=root,capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
