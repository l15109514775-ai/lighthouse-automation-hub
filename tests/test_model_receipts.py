"""Owned synthetic text and MockTransport only; no real model, human approval or publication."""
from copy import deepcopy
import hashlib,json,os
from types import SimpleNamespace
import httpx
import pytest
from adapters.http import ProviderHTTP
from adapters.model import ModelAPI
from adapters.compositor import compositor_input
from services.hub.budget import Budget
from services.hub.db import Store,encode
from services.hub.errors import HubError
from services.hub.model_receipts import ModelReceipts,digest


@pytest.fixture
def model_fixture(runtime,monkeypatch):
    settings,store=runtime
    for name in ('OPENAI_API_KEY','OPENAI_MODEL','OPENAI_QA_MODEL','MODEL_MAX_OUTPUT_TOKENS'):
        monkeypatch.delenv(name,raising=False)
    monkeypatch.setenv('MODEL_API_KEY','synthetic-private-model-key-123456789')
    monkeypatch.setenv('MODEL_API_STYLE','chat_completions')
    monkeypatch.setenv('MODEL_NAME','synthetic-translate')
    monkeypatch.setenv('QA_MODEL_NAME','synthetic-qa')
    monkeypatch.setenv('MODEL_BASE_URL','https://model.example.test/v1')
    monkeypatch.setenv('MODEL_ALLOWED_HOSTS','model.example.test')
    settings.config['budgets']['model'].update(daily_usd=10,monthly_usd=10,daily_calls=30,
        monthly_calls=30,max_call_usd=.01,paid_authorized=True)
    source={'post':{'id':'1000000000000000022','text':'架空のテスト20:00',
        'created_at':'2026-10-30T12:00:00Z'},'author':{'name':'Owned synthetic fixture','username':'fixture_only'},
        'source_api':'synthetic_only','url':'https://x.com/fixture_only/status/1000000000000000022'}
    ident=store.enqueue('synthetic_acceptance','model-receipt-fixture','tweet_process','tweet',source,settings.config['policy_version'])
    job=store.get(ident)
    class Server:
        def __init__(self):self.calls=[];self.qa={'passed':True,'issues':[]};self.status=200;self.text='架空测试20:00';self.raw=None
        def __call__(self,request):
            self.calls.append(request)
            if self.status=='timeout':raise httpx.ReadTimeout('synthetic timeout only',request=request)
            body=json.loads(request.content)
            assert request.url==httpx.URL('https://model.example.test/v1/chat/completions')
            if body['model']=='synthetic-qa':
                assert json.loads(body['messages'][1]['content'])['translation']==self.text
                value=self.qa
            else:value={'translation':self.text}
            envelope={'model':body['model'],'choices':[{'message':{'content':self.raw or encode(value)}}],
                'usage':{'prompt_tokens':23,'completion_tokens':7}}
            return httpx.Response(self.status,content=json.dumps(envelope,ensure_ascii=False,indent=2).encode())
    server=Server();http=ProviderHTTP(Budget(store,settings.config['budgets']),httpx.MockTransport(server))
    return settings,store,job,server,ModelAPI(http,store,settings=settings),http


def pair(fixture):
    settings,store,job,server,model,http=fixture
    value=model.invoke(job,'translation',job['payload'])
    quality=model.invoke(job,'quality',job['payload'],value['translation'])
    return value,quality


def test_two_recorded_requests_survive_reopen_without_claiming_live_or_human_review(model_fixture):
    settings,store,job,server,model,http=model_fixture
    value,qa=pair(model_fixture)
    evidence=ModelReceipts(store).assess(job,value['translation'])
    assert evidence['proof_integrity_passed'] and evidence['separate_requests_verified']
    assert evidence['qa_recommends_pass'] and not evidence['live_transport_verified']
    assert evidence['failures']==['MODEL_CUSTOM_TRANSPORT_NOT_LIVE_VERIFIED']
    assert len(set(evidence['call_ids']))==2 and len(server.calls)==2
    assert not evidence['human_review'] and not evidence['auto_publish_eligible']
    assert ModelReceipts(Store(settings.db_path)).assess(job,value['translation'])==evidence
    assert ModelAPI(http,Store(settings.db_path)).invoke(job,'translation',job['payload'])==value
    assert len(server.calls)==2
    for row in store.query('SELECT * FROM model_call_receipts'):
        file=model.receipts._file(row['job_id'],row['call_id'])
        raw=file.read_bytes()
        assert hashlib.sha256(raw).hexdigest()==row['response_wire_sha256']
        assert b'synthetic-private-model-key-123456789' not in raw
        assert row['request_sha256']!=row['request_wire_sha256']  # canonical vs actual wire ordering
        if os.name!='nt':
            assert file.stat().st_mode&0o777==0o600 and file.parent.stat().st_mode&0o777==0o700
    legacy=compositor_input(job['payload'],value['translation'])['post']
    assert not any(legacy['review'].values()) and not legacy['sourceScreenshot']['reviewed']
    assert not store.query('SELECT * FROM publication_reviews') and not store.query('SELECT * FROM publications')


def test_matching_qa_advice_without_ledger_does_not_establish_provenance(model_fixture):
    settings,store,job,server,model,http=model_fixture
    job['result']['model_evidence']={'proof_integrity_passed':True,'live_transport_verified':True}
    evidence=model.receipts.assess(job,server.text)
    assert evidence['failures']==['MODEL_RECEIPT_MISSING'] and not evidence['proof_integrity_passed']
    assert not server.calls


@pytest.mark.parametrize('changed',['raw','cache','api_state','api_time','request_hash','missing_file'])
def test_cache_provenance_tampering_never_triggers_repaid_call(model_fixture,changed):
    settings,store,job,server,model,http=model_fixture
    value=model.invoke(job,'translation',job['payload'])
    row=store.query('SELECT * FROM model_call_receipts')[0]
    if changed=='raw':
        file=model.receipts._file(job['job_id'],row['call_id']);file.write_bytes(file.read_bytes()+b'\n')
    elif changed=='missing_file':model.receipts._file(job['job_id'],row['call_id']).unlink()
    else:
        with store.transaction() as db:
            if changed=='cache':db.execute('UPDATE model_requests SET response=? WHERE request_key=?',
                (encode({'translation':'tampered20:00'}),row['request_key']))
            if changed=='api_state':db.execute("UPDATE api_calls SET state='unknown' WHERE call_id=?",(row['call_id'],))
            if changed=='api_time':db.execute('UPDATE api_calls SET finished_at=finished_at+1 WHERE call_id=?',(row['call_id'],))
            if changed=='request_hash':db.execute('UPDATE model_call_receipts SET request_sha256=?',('0'*64,))
    with pytest.raises(HubError):model.invoke(job,'translation',job['payload'])
    assert len(server.calls)==1 and len(store.query('SELECT * FROM api_calls'))==1


def test_legacy_cache_remains_unattested_and_does_not_get_a_fabricated_receipt(model_fixture):
    settings,store,job,server,model,http=model_fixture
    legacy=digest([job['job_id'],'translation','chat_completions','https://model.example.test/v1',
        'synthetic-translate',{'source':job['payload'],'translation':None}])
    with store.transaction() as db:db.execute('INSERT INTO model_requests VALUES(?,?,?,NULL)',
        (legacy,'done',encode({'translation':server.text})))
    assert model.invoke(job,'translation',job['payload'])=={'translation':server.text}
    assert not server.calls and model.receipts.status()['legacy_done_cache_rows']==1
    assert not store.query('SELECT * FROM model_call_receipts')
    assert model.receipts.assess(job,server.text)['failures']==['MODEL_RECEIPT_MISSING']


def test_legacy_unknown_request_is_not_evaded_by_new_contract_or_output_limit(model_fixture,monkeypatch):
    settings,store,job,server,model,http=model_fixture
    legacy=digest([job['job_id'],'translation','chat_completions','https://model.example.test/v1',
        'synthetic-translate',{'source':job['payload'],'translation':None}])
    with store.transaction() as db:db.execute('INSERT INTO model_requests VALUES(?,?,NULL,NULL)',(legacy,'unknown'))
    monkeypatch.setenv('MODEL_MAX_OUTPUT_TOKENS','1024')
    with pytest.raises(HubError,match='MODEL_RESULT_UNKNOWN_REVIEW_REQUIRED'):
        model.invoke(job,'translation',job['payload'])
    assert not server.calls and len(store.query('SELECT * FROM model_requests'))==1


def test_output_limit_change_reuses_bound_job_and_applies_only_to_new_jobs(model_fixture,monkeypatch):
    settings,store,job,server,model,http=model_fixture
    model.invoke(job,'translation',job['payload'])
    monkeypatch.setenv('MODEL_MAX_OUTPUT_TOKENS','1024')
    model.invoke(job,'translation',job['payload'])
    assert len(server.calls)==1 and json.loads(server.calls[0].content)['max_tokens']==4096
    ident=store.enqueue('synthetic_acceptance','new-bound-output-limit','tweet_process','tweet',job['payload'],job['policy_version'])
    model.invoke(store.get(ident),'translation',job['payload'])
    assert len(server.calls)==2 and {json.loads(r.content)['max_tokens'] for r in server.calls}=={1024,4096}
    assert len(store.query('SELECT * FROM model_call_receipts'))==2


@pytest.mark.parametrize('invalid',['extra_human_field','duplicate_key','nonfinite','blank_translation'])
def test_model_response_shape_is_strict_and_never_imports_review_claims(model_fixture,invalid):
    settings,store,job,server,model,http=model_fixture
    server.raw={'extra_human_field':'{"translation":"owned test","sourceConfirmed":true}',
        'duplicate_key':'{"translation":"first","translation":"second"}',
        'nonfinite':'{"translation":NaN}','blank_translation':'{"translation":" "}'}[invalid]
    with pytest.raises(HubError,match='MODEL_OUTPUT_INVALID'):model.invoke(job,'translation',job['payload'])
    assert not store.query('SELECT * FROM model_call_receipts')
    assert store.query('SELECT state FROM model_requests')[0]['state']=='unknown'
    assert not list((store.path.parent/'model-receipts').rglob('*.json'))


def test_secret_echo_even_when_escaped_is_not_archived_or_cached(model_fixture):
    settings,store,job,server,model,http=model_fixture
    secret='synthetic-private-model-key-123456789'
    escaped=''.join('\\u'+format(ord(c),'04x') for c in secret)
    server.raw='{"translation":"'+escaped+'"}'
    with pytest.raises(HubError,match='MODEL_RESPONSE_CONTAINS_SECRET'):model.invoke(job,'translation',job['payload'])
    assert not store.query('SELECT * FROM model_call_receipts')
    assert store.query('SELECT response FROM model_requests')[0]['response'] is None
    assert not list((store.path.parent/'model-receipts').rglob('*.json'))


@pytest.mark.parametrize('problem',['phase','limit','empty_quality'])
def test_configuration_errors_do_not_leave_dispatching_requests_or_charge(model_fixture,monkeypatch,problem):
    settings,store,job,server,model,http=model_fixture
    if problem=='limit':monkeypatch.setenv('MODEL_MAX_OUTPUT_TOKENS','invalid')
    with pytest.raises(HubError):model.invoke(job,'wrong-phase' if problem=='phase' else 'quality' if problem=='empty_quality' else 'translation',job['payload'])
    assert not server.calls and not store.query('SELECT * FROM model_requests') and not store.query('SELECT * FROM api_calls')


def test_unknown_network_result_is_preserved_across_restart(model_fixture):
    settings,store,job,server,model,http=model_fixture
    server.status='timeout'
    with pytest.raises(HubError,match='NETWORK_ERROR'):model.invoke(job,'translation',job['payload'])
    with pytest.raises(HubError,match='MODEL_RESULT_UNKNOWN_REVIEW_REQUIRED'):
        ModelAPI(http,Store(settings.db_path)).invoke(job,'translation',job['payload'])
    assert len(server.calls)==1 and not store.query('SELECT * FROM model_call_receipts')
    assert store.query('SELECT state FROM api_calls')[0]['state']=='unknown'


def test_rate_limit_allows_later_bounded_worker_retry(model_fixture):
    settings,store,job,server,model,http=model_fixture
    server.status=429
    with pytest.raises(HubError,match='API_RATE_LIMITED'):model.invoke(job,'translation',job['payload'])
    assert store.query('SELECT state FROM model_requests')[0]['state']=='rejected'
    server.status=200
    model.invoke(job,'translation',job['payload'])
    assert len(server.calls)==2 and len(store.query('SELECT * FROM model_call_receipts'))==1


def test_qa_rejection_is_valid_provenance_but_never_quality_acceptance(model_fixture):
    settings,store,job,server,model,http=model_fixture
    server.qa={'passed':False,'issues':[{'source_span':'20:00','translation_span':'20:00','reason':'synthetic quality denial'}]}
    value,qa=pair(model_fixture);evidence=model.receipts.assess(job,value['translation'])
    assert evidence['proof_integrity_passed'] and not evidence['qa_recommends_pass']
    assert not evidence['human_review'] and not evidence['auto_publish_eligible']


def test_low_disk_retains_budget_and_marks_unpersisted_result_unknown(model_fixture,monkeypatch):
    settings,store,job,server,model,http=model_fixture
    monkeypatch.setattr('services.hub.model_receipts.shutil.disk_usage',lambda _:SimpleNamespace(free=1000))
    with pytest.raises(HubError,match='DISK_LOW'):model.invoke(job,'translation',job['payload'])
    assert len(server.calls)==1 and store.query('SELECT state FROM model_requests')[0]['state']=='unknown'
    assert store.query('SELECT state FROM api_calls')[0]['state']=='done'
    assert not store.query('SELECT * FROM model_call_receipts')


def test_raw_receipt_paths_are_not_caller_controlled(model_fixture):
    settings,store,job,server,model,http=model_fixture
    with pytest.raises(HubError,match='MODEL_RECEIPT_PATH_INVALID'):model.receipts._file('../outside','not-a-uuid')


def test_caller_payload_cannot_replace_the_actual_job_during_assessment(model_fixture):
    settings,store,job,server,model,http=model_fixture
    value,qa=pair(model_fixture)
    changed=deepcopy(job);changed['payload']['post']['text']='caller changed source20:00'
    assert model.receipts.assess(changed,value['translation'])['failures']==['MODEL_JOB_INPUT_CHANGED']
    with pytest.raises(HubError,match='MODEL_JOB_INPUT_CHANGED'):
        model.invoke(job,'translation',changed['payload'])
    with pytest.raises(HubError,match='MODEL_JOB_REQUIRED'):
        model.invoke({'job_id':'not-an-existing-job'},'translation',job['payload'])
    assert len(server.calls)==2


def test_v2_sends_post_body_not_intake_or_capture_metadata(model_fixture):
    settings,store,job,server,model,http=model_fixture
    job['payload']['capture']={'actor':'never-send-operator-context','path':'never-send-local-path'}
    job['payload']['intake_claim']='never-send-intake-claim'
    with store.transaction() as db:db.execute('UPDATE jobs SET payload=? WHERE job_id=?',(encode(job['payload']),job['job_id']))
    value,qa=pair((settings,store,job,server,model,http))
    for request in server.calls:
        body=json.loads(request.content);data=json.loads(body['messages'][1]['content'])
        assert data['source']['post']=={'text':job['payload']['post']['text']}
        assert 'never-send' not in request.content.decode() and 'manual_input_sha256' not in request.content.decode()
    assert model.receipts.assess(job,value['translation'])['proof_integrity_passed']


def test_v1_receipts_and_paid_cache_remain_usable_after_v2_upgrade(model_fixture):
    settings,store,job,server,model,http=model_fixture
    with store.transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',
        (encode({'translation_contract_version':'model-text-contract-v1'}),job['job_id']))
    value,qa=pair(model_fixture)
    with store.transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',('{}',job['job_id']))
    assert ModelAPI(http,Store(settings.db_path)).invoke(job,'translation',job['payload'])==value
    assert len(server.calls)==2
    assert {r['contract_version'] for r in store.query('SELECT contract_version FROM model_call_receipts')}=={'model-text-contract-v1'}
    assert ModelReceipts(Store(settings.db_path)).assess(job,value['translation'])['proof_integrity_passed']


def test_v1_unknown_request_cannot_be_evaded_by_a_v2_selection(model_fixture):
    from services.hub.model_contract import payload_for
    settings,store,job,server,model,http=model_fixture
    payload=payload_for('translation',job['payload'],None,'chat_completions','synthetic-translate',4096,contract='model-text-contract-v1')
    key=digest(['model-text-contract-v1',job['job_id'],'translation','chat_completions','https://model.example.test/v1/chat/completions',payload])
    with store.transaction() as db:db.execute('INSERT INTO model_requests VALUES(?,?,NULL,NULL)',(key,'unknown'))
    with pytest.raises(HubError,match='MODEL_RESULT_UNKNOWN_REVIEW_REQUIRED'):model.invoke(job,'translation',job['payload'])
    assert not server.calls and not store.query('SELECT * FROM api_calls')


def failed_v1(fixture):
    from services.hub.evidence import machine_evidence
    settings,store,job,server,model,http=fixture
    with store.transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',
        (encode({'translation_contract_version':'model-text-contract-v1'}),job['job_id']))
    server.qa={'passed':False,'issues':[{'source_span':'20:00','translation_span':'20:00','reason':'Owned synthetic QA denial'}]}
    value,qa=pair(fixture);proof=model.receipts.assess(job,value['translation'])
    result={'translation_contract_version':'model-text-contract-v1','model_evidence':proof,
        'evidence':machine_evidence(job['payload'],value['translation'],qa)}
    with store.transaction() as db:db.execute("UPDATE jobs SET state='awaiting_review',error_code='TRANSLATION_CHECK_FAILED',stage='translated',result=? WHERE job_id=?",
        (encode(result),job['job_id']))
    folder=settings.root/'media'/job['job_id'];folder.mkdir()
    for name,data in [('source.json',job['payload']),('translation.json',value),
                      ('machine-evidence-v1.json',result['evidence']),('model-receipt-v1.json',proof)]:
        (folder/name).write_text(encode(data),encoding='utf-8')
    return folder,result


def test_explicit_upgrade_refuses_paid_contract_switch_and_preserves_original_data(model_fixture):
    from services.hub.translation_revision import retranslate_failed
    settings,store,job,server,model,http=model_fixture
    folder,result=failed_v1(model_fixture);old={p.name:p.read_bytes() for p in folder.iterdir()}
    before={table:store.query('SELECT * FROM '+table) for table in
        ('jobs','model_call_receipts','model_requests','api_calls','api_usage_details','audit')}
    with pytest.raises(HubError,match='MODEL_CONTRACT_PIN_CHANGED'):
        retranslate_failed(settings,store,job['job_id'],'maintenance')
    assert all((folder/name).read_bytes()==data for name,data in old.items())
    assert not (folder/'translation-history').exists()
    assert all(store.query('SELECT * FROM '+table)==rows for table,rows in before.items())
    assert store.get(job['job_id'])['state']=='awaiting_review' and len(server.calls)==2
    with pytest.raises(HubError,match='MODEL_CONTRACT_PIN_CHANGED'):
        retranslate_failed(settings,Store(settings.db_path),job['job_id'],'maintenance')
    value,qa=pair(model_fixture)
    assert len(server.calls)==2 and len(store.query('SELECT * FROM model_call_receipts'))==2
    assert model.receipts.assess(job,value['translation'])['proof_integrity_passed']
    assert not qa['passed'] and not model.receipts.assess(job,value['translation'])['qa_recommends_pass']
    assert not model.receipts.assess(job,value['translation'])['auto_publish_eligible']
    assert all((folder/name).read_bytes()==data for name,data in old.items())
    assert all(store.query('SELECT * FROM '+table)==rows for table,rows in before.items())


@pytest.mark.parametrize('reason',['reader','unknown_call','changed_source'])
def test_upgrade_denials_do_not_reset_jobs_or_make_model_calls(model_fixture,reason):
    from services.hub.translation_revision import retranslate_failed
    settings,store,job,server,model,http=model_fixture
    folder,result=failed_v1(model_fixture)
    if reason=='unknown_call':
        with store.transaction() as db:db.execute("UPDATE api_calls SET state='unknown' WHERE job_id=?",(job['job_id'],))
    if reason=='changed_source':(folder/'source.json').write_text(encode({'changed':True}),encoding='utf-8')
    with pytest.raises(HubError):retranslate_failed(settings,store,job['job_id'],'reader' if reason=='reader' else 'maintenance')
    assert len(server.calls)==2 and store.get(job['job_id'])['state']=='awaiting_review'
    assert not store.query('SELECT * FROM publications') and not store.query('SELECT * FROM publication_reviews')
