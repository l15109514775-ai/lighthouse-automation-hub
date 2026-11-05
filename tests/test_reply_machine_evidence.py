"""Prototype contracts with owned fixtures and MockTransport; no live model/send."""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
import json
from pathlib import Path
import time
import uuid
import httpx
import pytest
from adapters.http import ProviderHTTP
from adapters.model import ModelAPI
from services.hub.budget import Budget
from services.hub.db import encode,Store
from services.hub.errors import HubError
from services.hub.evidence import sha256_file
from services.hub.reply_evidence import ReplyEvidence,PACKET_VERSION,SOURCE_VERSION,RULE


def iso(epoch):return datetime.fromtimestamp(epoch,timezone.utc).isoformat()


@pytest.fixture
def reply_packet(runtime):
    settings,store=runtime
    parent_id=store.enqueue('scheduler','owned-reply-discovery','x_discovery','tweet',{},'machine-reply-prototype')
    discovery=store.claim('tweet',180,4)
    base=settings.root/'media'/parent_id/'reply-machine'/str(uuid.uuid4());base.mkdir(parents=True)
    def artifact(name,value):
        file=base/name
        file.write_bytes(value if isinstance(value,bytes) else encode(value).encode())
        return {'file':str(file),'sha256':sha256_file(file)}
    now=time.time()-1;thread_nav=str(uuid.uuid4());reread_nav=str(uuid.uuid4())
    def observe(name,role,ident,handle,text,nav,captured,page_url):
        screenshot=artifact(name+'.png',b'owned synthetic screenshot, no public content')
        visibility=artifact(name+'-visibility.json',{'version':'tweet-visibility-v2','human_review':False,'used_ocr':False,
            'occluded':False,'clipped_text':False,'text_line_count':2,'points':[{'x':10,'y':10,'visible':True}],
            'fixed_intersections':[]})
        moment=int((now-120 if role!='reply_in_thread' else now-60)//60)*60
        published=iso(moment);displayed=datetime.fromtimestamp(moment,timezone(timedelta(hours=8)))
        source={'post':{'id':ident,'text':text,'created_at':published,'timestamp_precision':'minute','referenced_tweets':[]},
            'author':{'name':'Owned parent' if handle=='parent_fixture' else 'Owned reply','username':handle},
            'url':f'https://x.com/{handle}/status/{ident}','source_api':'x-web-dom-v1','source_media_count':None,
            'references_observed':False,'capture':{'human_review':False,'source_screenshot_reviewed':False,
                'official_api_receipt':False,'visibility_passed':True,'screenshot_sha256':screenshot['sha256'],'captured_at':iso(captured)}}
        facts={'post_id':ident,'handle':handle,'display_name':source['author']['name'],'observed_text':text,
            'observed_published_at':published,'body_count':1,'show_more':False,'clipped':False,
            'media_count':0,'quote_count':0,'card_count':0,'body_links':[],
            'visible_timestamp':f'{displayed.hour:02d}:{displayed.minute:02d} · {displayed.year}年{displayed.month}月{displayed.day}日',
            'display_timezone':'Asia/Shanghai'}
        observation={'version':'reply-visible-post-v1','role':role,'human_review':False,'official_api':False,
            'payload':source,'facts':facts,'screenshot':screenshot,'visibility':visibility,'navigation_id':nav,'page_url':page_url}
        return artifact(name+'.json',observation)
    parent_url='https://x.com/parent_fixture/status/1000000000000000001'
    reply_url='https://x.com/example-channel-b/status/1000000000000000002'
    p=observe('parent-thread','parent_in_thread','1000000000000000001','parent_fixture','架空原推20:00',thread_nav,now,reply_url)
    r=observe('reply-thread','reply_in_thread','1000000000000000002','example-channel-b','架空回复21:00',thread_nav,now,reply_url)
    reread=observe('parent-reread','parent_independent_reread','1000000000000000001','parent_fixture','架空原推20:00',reread_nav,now+.1,parent_url)
    packet={'version':PACKET_VERSION,'capture_id':base.name,'rule':RULE,'observer_kind':'server_rendered_dom',
        'human_review':False,'official_api':False,'parent_in_thread':p,'reply_in_thread':r,'parent_reread':reread,
        'thread':{'page_url':reply_url,'navigation_id':thread_nav,'context_before_reply_ids':['1000000000000000001'],
            'article_ids_through_reply':['1000000000000000001','1000000000000000002'],'parent_index':0,'reply_index':1,
            'collapsed_context_detected':False,'show_more_context':False,'parent_is_quote':False,
            'reply_to_label':{'article_id':'1000000000000000002','visible':True,'text':'回复 @parent_fixture',
                'target_handles':['parent_fixture'],'target_profile_urls':['https://x.com/parent_fixture']},
            'screenshot':artifact('conversation.png',b'owned synthetic conversation'),
            'viewport':{'width':1280,'height':1000},'parent_rect':{'x':0,'y':0,'width':800,'height':300},
            'reply_rect':{'x':0,'y':300,'width':800,'height':300},'reply_to_label_rect':{'x':100,'y':310,'width':400,'height':30}}}
    service=ReplyEvidence(settings,store);service.initialize()
    return settings,store,discovery,packet,service


def change_observation(packet,key,mutate):
    item=packet[key];file=Path(item['file']);value=json.loads(file.read_text('utf-8'))
    mutate(value);file.write_text(encode(value),'utf-8');item['sha256']=sha256_file(file)


def test_bounded_relationship_needs_all_evidence_and_survives_reopen(reply_packet):
    settings,store,discovery,packet,service=reply_packet
    checked=service.validate_packet(discovery['job_id'],packet)
    assert checked['machine_relationship_supported'] and checked['human_review'] is False and checked['official_api'] is False
    assert checked['automatic_publication_eligible'] is False
    saved=service.record(discovery,packet)
    again=service.record(discovery,packet)
    assert saved==again and len(store.query('SELECT * FROM reply_machine_receipts'))==1
    store.finish(discovery,'completed')
    assert ReplyEvidence(settings,Store(settings.db_path)).verify(saved['receipt_id'])['validated_source']==checked
    assert not store.query('SELECT * FROM publications') and not store.query('SELECT * FROM api_calls')


def test_old_candidate_record_cannot_be_upgraded_by_flipping_one_boolean(reply_packet):
    settings,store,discovery,packet,service=reply_packet
    candidate={'version':'reply-context-observation-v1','direct_parent_independently_verified':False,
        'requires_human_relationship_review':True,'human_review':False,'official_api':False}
    with pytest.raises(HubError,match='REPLY_MACHINE_RELATIONSHIP_EVIDENCE_REQUIRED'):
        service.validate_packet(discovery['job_id'],candidate)
    candidate['direct_parent_independently_verified']=True
    with pytest.raises(HubError,match='REPLY_MACHINE_RELATIONSHIP_EVIDENCE_REQUIRED'):
        service.validate_packet(discovery['job_id'],candidate)
    assert not store.query('SELECT * FROM reply_machine_receipts')


@pytest.mark.parametrize('mutate',[
    lambda p:p['thread'].pop('reply_to_label'),
    lambda p:p['thread']['reply_to_label'].update(visible=False),
    lambda p:p['thread']['reply_to_label'].update(text='回复 @someone_else'),
    lambda p:p['thread']['reply_to_label'].update(target_handles=['other']),
    lambda p:p['thread']['reply_to_label'].update(target_profile_urls=['https://x.com/other']),
    lambda p:p['thread'].update(context_before_reply_ids=['earlier','1000000000000000001']),
    lambda p:p['thread'].update(collapsed_context_detected=True),
    lambda p:p['thread'].update(show_more_context=True),
    lambda p:p['thread'].update(parent_is_quote=True),
    lambda p:p['thread'].update(reply_index=2),
    lambda p:p['thread']['reply_rect'].update(y=900),
    lambda p:p['thread']['reply_to_label_rect'].update(y=100),
    lambda p:p['thread'].update(navigation_id=str(uuid.uuid4())),
    lambda p:p.pop('parent_reread'),
    lambda p:p.update(human_review=True),
])
def test_missing_or_ambiguous_direct_relationship_measurements_fail(reply_packet,mutate):
    settings,store,discovery,packet,service=reply_packet
    mutate(packet)
    with pytest.raises(HubError):service.record(discovery,packet)
    assert not store.query('SELECT * FROM reply_machine_receipts')


@pytest.mark.parametrize('field,value', [('text','Different reread text'),('created_at','2026-10-01T00:00:00Z')])
def test_independent_parent_reread_must_match_full_text_and_time(reply_packet,field,value):
    settings,store,discovery,packet,service=reply_packet
    def change(v):
        v['payload']['post'][field]=value
        v['facts']['observed_text' if field=='text' else 'observed_published_at']=value
    change_observation(packet,'parent_reread',change)
    with pytest.raises(HubError):service.record(discovery,packet)


def test_same_navigation_is_not_an_independent_parent_reread(reply_packet):
    settings,store,discovery,packet,service=reply_packet
    change_observation(packet,'parent_reread',lambda v:v.update(navigation_id=packet['thread']['navigation_id']))
    with pytest.raises(HubError):service.record(discovery,packet)


def test_same_invented_parent_timestamp_in_both_payloads_does_not_override_visible_time(reply_packet):
    settings,store,discovery,packet,service=reply_packet
    for name in ('parent_in_thread','parent_reread'):
        def change(value):
            value['payload']['post']['created_at']='2026-01-01T00:00:00Z'
            value['facts']['observed_published_at']='2026-01-01T00:00:00Z'
        change_observation(packet,name,change)
    with pytest.raises(HubError,match='REPLY_POST_OBSERVATION_INVALID'):service.record(discovery,packet)


def test_changed_screenshot_or_occluded_body_is_never_accepted(reply_packet):
    settings,store,discovery,packet,service=reply_packet
    observation=json.loads(Path(packet['reply_in_thread']['file']).read_text('utf-8'))
    Path(observation['screenshot']['file']).write_bytes(b'changed bytes')
    with pytest.raises(HubError,match='REPLY_EVIDENCE_ARTIFACT_INVALID'):service.record(discovery,packet)


def test_visibility_report_must_have_actual_unobscured_hit_tests(reply_packet):
    settings,store,discovery,packet,service=reply_packet
    def change(v):
        file=Path(v['visibility']['file']);report=json.loads(file.read_text('utf-8'))
        report['points'][0]['visible']=False
        file.write_text(encode(report),'utf-8');v['visibility']['sha256']=sha256_file(file)
    change_observation(packet,'reply_in_thread',change)
    with pytest.raises(HubError,match='REPLY_SOURCE_NOT_FULLY_VISIBLE'):service.record(discovery,packet)


def test_receipt_is_lease_bound_and_immutable_after_recording(reply_packet):
    settings,store,discovery,packet,service=reply_packet
    saved=service.record(discovery,packet)
    with store.transaction() as db:db.execute("UPDATE jobs SET lease_owner='revoked' WHERE job_id=?",(discovery['job_id'],))
    with pytest.raises(HubError,match='REPLY_DISCOVERY_LEASE_REQUIRED'):service.record(discovery,packet)
    Path(saved['file']).write_text('{}','utf-8')
    with pytest.raises(HubError):service.verify(saved['receipt_id'])


def test_prototype_does_not_add_tables_or_handlers_until_explicit_initialization(runtime):
    settings,store=runtime;service=ReplyEvidence(settings,store)
    assert not store.query("SELECT name FROM sqlite_master WHERE name LIKE 'reply_machine_%'")
    with pytest.raises(HubError,match='REPLY_EVIDENCE_PROTOTYPE_NOT_INITIALIZED'):service.verify('missing')


@pytest.fixture
def model_pair(reply_packet,monkeypatch):
    settings,store,discovery,packet,service=reply_packet
    saved=service.record(discovery,packet);inputs=service.model_inputs(saved['receipt_id'])
    for key in ('OPENAI_API_KEY','OPENAI_MODEL','OPENAI_QA_MODEL'):monkeypatch.delenv(key,raising=False)
    for key,value in {'MODEL_API_KEY':'owned-synthetic-reply-key-123456789','MODEL_API_STYLE':'chat_completions',
        'MODEL_NAME':'owned-translate','QA_MODEL_NAME':'owned-check','MODEL_BASE_URL':'https://reply-model.example.test/v1',
        'MODEL_ALLOWED_HOSTS':'reply-model.example.test'}.items():monkeypatch.setenv(key,value)
    settings.config['budgets']['model'].update(daily_usd=10,monthly_usd=10,daily_calls=30,monthly_calls=30,max_call_usd=.01,paid_authorized=True)
    ids={name:store.enqueue('x',source['post']['id'],'reply_translation_prototype','tweet',source,'reply-machine-prototype') for name,source in inputs.items()}
    calls=[];qa_pass={'parent':True,'reply':True}
    def server(request):
        body=json.loads(request.content);data=json.loads(body['messages'][1]['content']);calls.append(data)
        text=data['source']['post']['text'];kind='reply' if '21:00' in text else 'parent'
        if body['model']=='owned-check':value={'passed':qa_pass[kind],'issues':[] if qa_pass[kind] else [
            {'source_span':text,'translation_span':data['translation'],'reason':'owned synthetic QA rejection'}]}
        else:value={'translation':'架空回复21:00' if kind=='reply' else '架空原推20:00'}
        return httpx.Response(200,json={'model':body['model'],'choices':[{'message':{'content':encode(value)}}]})
    http=ProviderHTTP(Budget(store,settings.config['budgets']),httpx.MockTransport(server));model=ModelAPI(http,store,settings=settings)
    return settings,store,service,saved,inputs,ids,model,calls,qa_pass


def call_pairs(model_pair):
    settings,store,service,saved,inputs,ids,model,calls,qa_pass=model_pair
    translations={}
    for name in ('parent','reply'):
        job=store.get(ids[name]);translations[name]=model.invoke(job,'translation',inputs[name])['translation']
        model.invoke(job,'quality',inputs[name],translations[name])
    return translations


def test_parent_and_reply_have_four_distinct_persisted_calls_but_mock_is_not_live(model_pair):
    settings,store,service,saved,inputs,ids,model,calls,qa_pass=model_pair
    translations=call_pairs(model_pair)
    result=service.assess_translations(saved['receipt_id'],ids['parent'],ids['reply'],translations['parent'],translations['reply'])
    assert result['four_separate_calls_verified'] and result['translation_checks_passed']
    assert result['live_transport_verified'] is False and result['automatic_publication_eligible'] is False
    assert result['failures']==['REPLY_MODEL_LIVE_TRANSPORT_REQUIRED'] and len(calls)==4
    assert len(store.query('SELECT * FROM reply_machine_translation_checks'))==1
    source=inputs['reply'];assert source['reply_context']['parent_source']==inputs['parent']
    assert source['reply_context']['relationship_verified_by_human'] is False
    assert calls[2]['source']['post']['text']=='架空回复21:00'
    assert calls[2]['context']['parent_post']['text']=='架空原推20:00'
    assert not store.query('SELECT * FROM publications')
    reopened=ReplyEvidence(settings,Store(settings.db_path))
    assert reopened.verify_translation_check(result['check_id'])['translation_checks_passed']


def test_good_reply_does_not_override_rejected_parent_translation(model_pair):
    settings,store,service,saved,inputs,ids,model,calls,qa_pass=model_pair
    qa_pass['parent']=False;translations=call_pairs(model_pair)
    result=service.assess_translations(saved['receipt_id'],ids['parent'],ids['reply'],translations['parent'],translations['reply'])
    assert result['four_separate_calls_verified'] and not result['translation_checks_passed']
    assert not result['pair']['parent']['machine_checks']['machine_checks_passed']
    assert result['pair']['reply']['machine_checks']['machine_checks_passed']
    assert result['automatic_publication_eligible'] is False


def test_one_job_or_swapped_translations_cannot_establish_pair_acceptance(model_pair):
    settings,store,service,saved,inputs,ids,model,calls,qa_pass=model_pair
    translations=call_pairs(model_pair)
    with pytest.raises(HubError,match='REPLY_SEPARATE_TRANSLATION_JOBS_REQUIRED'):
        service.assess_translations(saved['receipt_id'],ids['reply'],ids['reply'],translations['parent'],translations['reply'])
    result=service.assess_translations(saved['receipt_id'],ids['parent'],ids['reply'],translations['reply'],translations['parent'])
    assert not result['four_separate_calls_verified'] and not result['translation_checks_passed']
    assert result['automatic_publication_eligible'] is False


def test_missing_parent_quality_receipt_or_changed_source_blocks_pair(model_pair):
    settings,store,service,saved,inputs,ids,model,calls,qa_pass=model_pair
    job=store.get(ids['reply']);translated=model.invoke(job,'translation',inputs['reply'])['translation']
    model.invoke(job,'quality',inputs['reply'],translated)
    value=service.assess_translations(saved['receipt_id'],ids['parent'],ids['reply'],'架空原推20:00',translated)
    assert not value['four_separate_calls_verified'] and not value['translation_checks_passed']
    with store.transaction() as db:db.execute('UPDATE jobs SET payload=? WHERE job_id=?',(encode({'post':{'text':'changed'}}),ids['reply']))
    with pytest.raises(HubError,match='REPLY_SEPARATE_TRANSLATION_JOBS_REQUIRED'):
        service.assess_translations(saved['receipt_id'],ids['parent'],ids['reply'],'架空原推20:00',translated)


def test_persisted_pair_check_revalidates_raw_model_receipts_instead_of_trusting_pass_flag(model_pair):
    settings,store,service,saved,inputs,ids,model,calls,qa_pass=model_pair
    translations=call_pairs(model_pair)
    value=service.assess_translations(saved['receipt_id'],ids['parent'],ids['reply'],translations['parent'],translations['reply'])
    row=store.query('SELECT * FROM model_call_receipts WHERE job_id=?',(ids['parent'],))[0]
    raw=model.receipts._file(row['job_id'],row['call_id']);raw.write_bytes(raw.read_bytes()+b'changed')
    with pytest.raises(HubError,match='REPLY_TRANSLATION_CHECK_CHANGED'):
        service.verify_translation_check(value['check_id'])
    assert len(calls)==4 and not store.query('SELECT * FROM publications')
