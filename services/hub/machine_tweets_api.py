"""Official API source policy. Shares rendering/model/publishing mechanics only.

API receipts replace visual source observation, never human confirmation. The
retained v1/v2 policies and browser evidence cannot be converted into this grant.
"""
from .errors import HubError
from .machine_tweets_v2 import MachineTweetsV2

VERSION='machine-tweet-v3-api'
KIND='dynamic_website_machine_v3_api'
SCOPE=['official_full_text','official_static_photos','official_reply_parent']
REFERENCE_VERSION='machine-tweet-v4-api-relations'
REFERENCE_SCOPE=[*SCOPE,'official_quoted_post','official_reply_and_quote']


class MachineTweetsAPI(MachineTweetsV2):
    version=VERSION
    kind=KIND
    model_contract_version='model-text-contract-v6'
    policy_filename='machine-tweet-policy-api-v1.json'
    content_scope=SCOPE
    render_folder='machine-api-v1'
    render_script='render_machine_official.mjs'
    render_source_version='machine-render-source-evidence-api-v1'
    render_model_version='machine-render-model-evidence-api-v1'
    render_ticket_version='machine-render-authorization-api-v1'
    render_report_version='machine-render-api-v1'
    render_result_filename='machine-render-api-result.json'

    def policy(self):
        value=super().policy()
        if value.get('source_authority')!='x-v2-official':
            raise HubError('MACHINE_OFFICIAL_SOURCE_POLICY_INVALID')
        return value

    def source(self,job,*,require_fresh=True):
        from .source_receipts import SourceReceipts
        if job.get('payload',{}).get('source_api')!='x-v2-official':
            raise HubError('MACHINE_OFFICIAL_SOURCE_REQUIRED')
        source=SourceReceipts(self.settings,self.store).assess_official(job,require_fresh=require_fresh)
        if source['proof'].get('source_live_verified') is not True:
            raise HubError('MACHINE_OFFICIAL_LIVE_SOURCE_REQUIRED')
        return source

    def _render_source_metadata(self,raw,node):
        if raw.get('source_api')!='x-v2-official' or node.get('screenshot') is not None:
            raise HubError('MACHINE_OFFICIAL_SOURCE_REQUIRED')
        # The original schema honestly records that no webpage screenshot exists.
        raw['source_screenshot_file']=None
        raw['source_screenshot_captured_at']=None
        return {'source_screenshot':None,'source_authority':'x-v2-official'}

    def _media_caption(self):
        return '\n附图来自官方API所列素材；图片内文字未翻译。'

    def _link_card_caption(self,source):
        from adapters.x import is_youtube_live_link_card,native_context_card_notice
        text=('\n推文正文译图；YouTube链接预览缩略图未包含' if
            any(is_youtube_live_link_card(node['source']['post']) for node in source['nodes']) else '')
        native=False
        for node in source['nodes']:
            expected=native_context_card_notice(node['source']['post'])
            if expected is not None:
                if node['role'] not in ('parent','quote') or node['source'].get('native_card_context')!=expected:
                    raise HubError('MACHINE_NATIVE_CONTEXT_CARD_NOTICE_REQUIRED')
                native=True
            elif 'native_card_context' in node['source']:
                raise HubError('MACHINE_NATIVE_CONTEXT_CARD_NOTICE_REQUIRED')
        if native:text+='\n引用节点展示API提供的原帖文字；API未提供原帖卡片内容，译图未包含该卡片。'
        return text

    def _relation_caption(self,prepared,source):
        return super()._relation_caption(prepared,source)+self._link_card_caption(source)

    def _verify_render_source_metadata(self,actual,node):
        if (actual.get('sourceScreenshot')!={'file':None,'capturedAt':None,'reviewed':False} or
                node['source'].get('source_api')!='x-v2-official' or node.get('screenshot') is not None):
            raise HubError('MACHINE_POLICY_OR_ARTIFACT_INVALID')


def official_reference_graph(source):
    """One-hop typed graph, independently bound to every complete source node."""
    try:
        nodes=source['nodes'];primary=nodes[0]['source'];context=primary['reference_context']
        if (nodes[0]['role']!='post' or context['version']!='x-official-reference-context-v2' or
                context['human_review'] is not False or context['expansion']!={'max_depth':1,'complete':True,'unexpanded_relations':[]}):
            raise ValueError()
        relations=context['relations'];refs=primary['post'].get('referenced_tweets',[])
        if not isinstance(relations,list) or len(relations)>2:raise ValueError()
        byrole={};types=[]
        for relation in relations:
            role={'replied_to':'parent','quoted':'quote'}.get(relation['type'])
            target=relation['source']
            if (role is None or relation['role']!=role or role in byrole or
                    relation['post_id']!=target['post']['id'] or relation['post_id']==primary['post']['id'] or
                    target['post'].get('referenced_tweets',[])):
                raise ValueError()
            byrole[role]=relation;types.append(relation['type'])
        expected='reply_quote' if len(byrole)==2 else 'reply' if 'parent' in byrole else 'quote' if 'quote' in byrole else 'original'
        if (context['relationship_type']!=expected or len(refs)!=len(relations) or
                sorted((r['type'],r['id']) for r in refs)!=sorted((r['type'],r['post_id']) for r in relations) or
                len({r['post_id'] for r in relations})!=len(relations) or
                [n['role'] for n in nodes]!=['post',*(r for r in ('parent','quote') if r in byrole)]):
            raise ValueError()
        for node in nodes[1:]:
            if node['source']!=byrole[node['role']]['source']:raise ValueError()
        return context
    except (KeyError,ValueError,TypeError,IndexError):
        raise HubError('MACHINE_OFFICIAL_REFERENCE_GRAPH_INVALID') from None


class MachineTweetsAPIReference(MachineTweetsAPI):
    """Explicit new policy; old API records, models and layouts retain meaning."""
    version=REFERENCE_VERSION
    kind='dynamic_website_machine_v4_api_relations'
    source_record_version='x-official-source-v2'
    model_contract_version='model-text-contract-v7'
    policy_filename='machine-tweet-policy-api-relations-v1.json'
    content_scope=REFERENCE_SCOPE
    node_roles=('post','parent','quote')
    render_folder='machine-api-relations-v2'
    render_script='render_machine_official_relations.mjs'
    render_source_version='machine-render-source-evidence-api-v2'
    render_model_version='machine-render-model-evidence-api-v2'
    render_ticket_version='machine-render-authorization-api-v2'
    render_report_version='machine-render-api-v2'
    render_result_filename='machine-render-api-v2-result.json'

    def _model_proof(self,parent,node,entry):
        proof,quality=super()._model_proof(parent,node,entry)
        child=self._node_job(parent,node,entry)
        rows=[self.store.query('SELECT contract_version FROM model_call_receipts WHERE request_key=?',(key,)) for key in proof['request_keys']]
        if (child['result'].get('translation_contract_version')!=self.model_contract_version or len(rows)!=2 or
                any(len(r)!=1 or r[0]['contract_version']!=self.model_contract_version for r in rows)):
            raise HubError('MACHINE_REFERENCE_MODEL_CONTRACT_REQUIRED')
        return proof,quality

    def _render_model_metadata(self):
        return {'contract_version':self.model_contract_version}

    def source(self,job,*,require_fresh=True):
        from .source_receipts import SourceReceipts
        if job.get('payload',{}).get('source_api')!='x-v2-official':
            raise HubError('MACHINE_OFFICIAL_SOURCE_REQUIRED')
        source=SourceReceipts(self.settings,self.store).assess_official(job,require_fresh=require_fresh,
            source_version=self.source_record_version)
        if source['proof'].get('source_live_verified') is not True:
            raise HubError('MACHINE_OFFICIAL_LIVE_SOURCE_REQUIRED')
        official_reference_graph(source)
        return source

    def _render_field(self,role):
        if role not in self.node_roles:raise HubError('MACHINE_V2_NODE_ROLE_INVALID')
        return {'post':'post','parent':'post.parent','quote':'post.quoted'}[role]

    def _render_input(self,prepared,source):
        from copy import deepcopy
        context=official_reference_graph(source)
        if set(prepared)!={n['role'] for n in source['nodes']}:
            raise HubError('MACHINE_OFFICIAL_REFERENCE_GRAPH_INVALID')
        post=prepared['post'];post['quote']=None
        post['sourceQuote']='present' if any(r['role']=='quote' for r in context['relations']) else 'none'
        post['relationship']={'type':context['relationship_type']}
        contexts={}
        for role in ('parent','quote'):
            if role in prepared:
                contexts[role]=prepared[role];contexts[role]['quote']=None
                contexts[role]['sourceQuote']='none';contexts[role]['relationship']={'type':'original'}
        return {'machineSchemaVersion':'official-reference-cards-v2','displayTimezone':'Asia/Shanghai',
                'post':post,'contextPosts':contexts,'reference_context':deepcopy(context)}

    def _render_node(self,item,role):
        return item['post'] if role=='post' else item['contextPosts'][role]

    def _validate_render_relationships(self,item,source):
        context=official_reference_graph(source)
        if (item.get('machineSchemaVersion')!='official-reference-cards-v2' or item.get('reference_context')!=context or
                item['post']['relationship']!={'type':context['relationship_type']} or item['post']['quote'] is not None or
                item['post']['sourceQuote']!=('present' if any(r['role']=='quote' for r in context['relations']) else 'none') or
                set(item['contextPosts'])!={n['role'] for n in source['nodes'][1:]}):raise ValueError()
        for node in source['nodes']:
            actual=self._render_node(item,node['role'])
            if actual['id']!=node['source']['post']['id']:raise ValueError()
            if node['role']!='post' and (actual['relationship']!={'type':'original'} or actual['quote'] is not None or actual['sourceQuote']!='none'):
                raise ValueError()

    def _relation_caption(self,prepared,source):
        kind=official_reference_graph(source)['relationship_type']
        return {'original':'','reply':'\n附回复原帖及主播回复。','quote':'\n附被引用原帖及主播引推。',
                'reply_quote':'\n主播同时回复并引用：分别附回复原帖与被引用原帖。'}[kind]+self._link_card_caption(source)
