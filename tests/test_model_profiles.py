"""Owned MockTransport only; profiles never attest live TLS or QA truth."""
from copy import deepcopy
import json
import uuid
import pytest
from adapters.model import ModelAPI
from services.hub.db import Store, encode
from services.hub.errors import HubError
from services.hub.model_receipts import digest
from test_model_receipts import model_fixture, pair


def change_environment(monkeypatch):
    for key,value in {'MODEL_NAME':'other-translation','QA_MODEL_NAME':'other-quality',
                      'MODEL_BASE_URL':'https://new.example.test/v2','MODEL_API_STYLE':'openai_responses',
                      'MODEL_MAX_OUTPUT_TOKENS':'1024'}.items():
        monkeypatch.setenv(key,value)


def test_profile_is_durable_before_budget_and_contains_no_credentials(model_fixture, monkeypatch):
    settings,store,job,server,model,http=model_fixture
    actual=http.request
    def check(provider,method,url,**kwargs):
        row=store.query('SELECT * FROM model_request_profiles')[0]
        assert row['job_id']==job['job_id'] and row['phase']=='translation'
        assert row['requested_model']=='synthetic-translate' and row['max_output_tokens']==4096
        assert row['origin']=='first_invocation'
        assert store.query('SELECT state FROM model_requests')[0]['state']=='dispatching'
        assert not store.query('SELECT * FROM api_calls')
        assert 'synthetic-private-model-key-123456789' not in encode(row)
        return actual(provider,method,url,**kwargs)
    monkeypatch.setattr(http,'request',check)
    model.invoke(job,'translation',job['payload'])
    row=store.query('SELECT * FROM model_request_profiles')[0]
    receipt=store.query('SELECT * FROM model_call_receipts')[0]
    for key in ('job_id','phase','request_key','request_sha256','source_sha256','contract_version','protocol',
                'endpoint','requested_model','max_output_tokens'):
        assert row[key]==receipt[key]


def test_model_protocol_endpoint_and_limit_switches_reuse_both_bound_phases_after_restart(model_fixture, monkeypatch):
    settings,store,job,server,model,http=model_fixture
    translated,qa=pair(model_fixture)
    before=store.query('SELECT * FROM api_calls ORDER BY call_id')
    profile=store.query('SELECT * FROM model_request_profiles ORDER BY phase')
    proof=model.receipts.assess(job,translated['translation'])
    change_environment(monkeypatch)
    monkeypatch.setenv('MODEL_MAX_OUTPUT_TOKENS','invalid-new-setting')
    reopened=ModelAPI(http,Store(settings.db_path),settings=settings)
    assert reopened.invoke(job,'translation',job['payload'])==translated
    assert reopened.invoke(job,'quality',job['payload'],translated['translation'])==qa
    assert len(server.calls)==2 and store.query('SELECT * FROM api_calls ORDER BY call_id')==before
    assert store.query('SELECT * FROM model_request_profiles ORDER BY phase')==profile
    assert reopened.receipts.assess(job,translated['translation'])==proof


def test_bound_endpoint_must_still_be_in_current_host_allowlist(model_fixture, monkeypatch):
    settings,store,job,server,model,http=model_fixture
    model.invoke(job,'translation',job['payload'])
    change_environment(monkeypatch)
    monkeypatch.setenv('MODEL_ALLOWED_HOSTS','new.example.test')
    with pytest.raises(HubError,match='MODEL_ENDPOINT_NOT_AUTHORIZED'):
        ModelAPI(http,Store(settings.db_path)).invoke(job,'translation',job['payload'])
    assert len(server.calls)==1


@pytest.mark.parametrize('phase',['translation','quality'])
def test_unknown_charge_blocks_every_phase_before_changed_environment_is_considered(model_fixture, monkeypatch, phase):
    settings,store,job,server,model,http=model_fixture
    server.status='timeout'
    with pytest.raises(HubError,match='NETWORK_ERROR'):model.invoke(job,phase,job['payload'],server.text if phase=='quality' else None)
    before={name:store.query('SELECT * FROM '+name) for name in ('api_calls','model_requests','model_request_profiles')}
    change_environment(monkeypatch)
    monkeypatch.delenv('MODEL_API_KEY')
    server.status=200
    reopened=ModelAPI(http,Store(settings.db_path))
    for retry_phase in ('translation','quality'):
        with pytest.raises(HubError,match='MODEL_RESULT_UNKNOWN_REVIEW_REQUIRED'):
            reopened.invoke(job,retry_phase,job['payload'],server.text if retry_phase=='quality' else None)
    assert len(server.calls)==1
    assert all(store.query('SELECT * FROM '+name)==rows for name,rows in before.items())


@pytest.mark.parametrize('state',['reserved','unknown'])
def test_pre_profile_job_ledger_uncertainty_blocks_unrelated_new_selection(model_fixture, monkeypatch, state):
    settings,store,job,server,model,http=model_fixture
    with store.transaction() as db:
        db.execute('INSERT INTO api_calls(call_id,provider,job_id,started_at,cost_usd,state) VALUES(?,?,?,?,?,?)',
                   (str(uuid.uuid4()),'model',job['job_id'],1,.01,state))
    change_environment(monkeypatch)
    with pytest.raises(HubError,match='MODEL_RESULT_UNKNOWN_REVIEW_REQUIRED'):
        model.invoke(job,'quality',job['payload'],server.text)
    assert not server.calls and not store.query('SELECT * FROM model_request_profiles')


def test_dispatching_profile_without_an_api_ledger_record_still_blocks_after_restart(model_fixture, monkeypatch):
    settings,store,job,server,model,http=model_fixture
    spec,key=model._spec(job,'translation',job['payload'],None)
    with store.transaction() as db:
        model.profiles.bind(db,spec)
        db.execute('INSERT INTO model_requests VALUES(?,?,NULL,NULL)',(spec['request_key'],'dispatching'))
    change_environment(monkeypatch)
    with pytest.raises(HubError,match='MODEL_RESULT_UNKNOWN_REVIEW_REQUIRED'):
        ModelAPI(http,Store(settings.db_path)).invoke(job,'translation',job['payload'])
    assert not server.calls and not store.query('SELECT * FROM api_calls')


def test_quality_first_is_allowed_but_another_candidate_cannot_purchase_second_qa(model_fixture):
    settings,store,job,server,model,http=model_fixture
    first=model.invoke(job,'quality',job['payload'],server.text)
    assert first=={'passed':True,'issues':[]} and len(server.calls)==1
    assert store.query('SELECT phase FROM model_call_receipts')==[{'phase':'quality'}]
    with pytest.raises(HubError,match='MODEL_REQUEST_PROFILE_CHANGED'):
        model.invoke(job,'quality',job['payload'],'A different owned candidate20:00')
    assert len(server.calls)==1 and len(store.query('SELECT * FROM api_calls'))==1


def test_same_job_changed_durable_source_cannot_buy_another_body(model_fixture):
    settings,store,job,server,model,http=model_fixture
    model.invoke(job,'translation',job['payload'])
    changed=deepcopy(job['payload']);changed['post']['text']='Changed owned source20:00'
    with store.transaction() as db:db.execute('UPDATE jobs SET payload=? WHERE job_id=?',(encode(changed),job['job_id']))
    with pytest.raises(HubError,match='MODEL_REQUEST_PROFILE_CHANGED'):
        model.invoke(store.get(job['job_id']),'translation',changed)
    assert len(server.calls)==1


def test_first_phase_profile_pins_contract_before_any_receipt_exists(model_fixture):
    settings,store,job,server,model,http=model_fixture
    spec,key=model._spec(job,'translation',job['payload'],None)
    with store.transaction() as db:
        model.profiles.bind(db,spec)
        db.execute('UPDATE jobs SET result=? WHERE job_id=?',
                   (encode({'translation_contract_version':'model-text-contract-v4'}),job['job_id']))
    with pytest.raises(HubError,match='MODEL_CONTRACT_PIN_CHANGED'):
        model.invoke(store.get(job['job_id']),'quality',job['payload'],server.text)
    assert not server.calls and not store.query('SELECT * FROM api_calls')


def test_v14_receipt_parameters_are_adopted_without_charging_after_environment_switch(model_fixture, monkeypatch):
    settings,store,job,server,model,http=model_fixture
    translated,qa=pair(model_fixture)
    original_proof=model.receipts.assess(job,translated['translation'])
    raw={r['call_id']:model.receipts._file(r['job_id'],r['call_id']).read_bytes() for r in store.query('SELECT * FROM model_call_receipts')}
    with store.transaction() as db:
        db.execute('DROP TABLE model_request_profiles')
        db.execute('DELETE FROM schema_migrations WHERE version=15')
    before={table:store.query('SELECT * FROM '+table) for table in ('jobs','model_requests','model_call_receipts','api_calls')}
    store.init();store.init()
    assert store.query('SELECT version FROM schema_migrations WHERE version=15')==[{'version':15}]
    assert store.query('SELECT MAX(version) n FROM schema_migrations')[0]['n']==16
    assert not store.query('SELECT * FROM model_request_profiles')
    assert all(store.query('SELECT * FROM '+table)==rows for table,rows in before.items())
    change_environment(monkeypatch)
    reopened=ModelAPI(http,Store(settings.db_path))
    assert reopened.invoke(job,'translation',job['payload'])==translated
    assert reopened.invoke(job,'quality',job['payload'],translated['translation'])==qa
    assert len(server.calls)==2 and reopened.receipts.assess(job,translated['translation'])==original_proof
    assert {r['origin'] for r in store.query('SELECT * FROM model_request_profiles')}=={'existing_receipt'}
    assert all(reopened.receipts._file(r['job_id'],r['call_id']).read_bytes()==raw[r['call_id']]
               for r in store.query('SELECT * FROM model_call_receipts'))


def test_multiple_historical_phase_parameter_sets_are_rejected_instead_of_selecting_one(model_fixture, monkeypatch):
    settings,store,job,server,model,http=model_fixture
    model.invoke(job,'translation',job['payload'])
    first=store.query('SELECT * FROM model_call_receipts')[0]
    # Emulate pre-profile history with two genuinely captured MockTransport
    # requests, retaining their original wire bytes/ledger/caches.
    with store.transaction() as db:
        db.execute('DELETE FROM model_request_profiles')
        db.execute('DELETE FROM model_call_receipts WHERE request_key=?',(first['request_key'],))
    monkeypatch.setenv('MODEL_NAME','synthetic-second-translation')
    model.invoke(job,'translation',job['payload'])
    with store.transaction() as db:
        db.execute('DELETE FROM model_request_profiles')
        db.execute('INSERT INTO model_call_receipts('+','.join(first)+') VALUES('+','.join('?' for _ in first)+')',tuple(first.values()))
    assert len(server.calls)==2
    with pytest.raises(HubError,match='MODEL_REQUEST_PROFILE_AMBIGUOUS'):
        ModelAPI(http,Store(settings.db_path)).invoke(job,'translation',job['payload'])
    assert len(server.calls)==2 and not store.query('SELECT * FROM model_request_profiles')


def test_concurrent_different_specs_cannot_bind_two_paid_requests(model_fixture, monkeypatch):
    settings,store,job,server,model,http=model_fixture
    first,key=model._spec(job,'translation',job['payload'],None)
    monkeypatch.setenv('MODEL_NAME','racing-owned-model')
    second,key=model._spec(job,'translation',job['payload'],None)
    with store.transaction() as db:model.profiles.bind(db,first)
    with pytest.raises(HubError,match='MODEL_REQUEST_PROFILE_CHANGED'):
        with store.transaction() as db:model.profiles.bind(db,second)
    assert len(store.query('SELECT * FROM model_request_profiles'))==1
    assert not server.calls and not store.query('SELECT * FROM api_calls')


def test_contract_edit_after_preparing_request_is_rechecked_inside_binding_transaction(model_fixture):
    settings,store,job,server,model,http=model_fixture
    spec,key=model._spec(job,'quality',job['payload'],server.text)
    with store.transaction() as db:
        db.execute('UPDATE jobs SET result=? WHERE job_id=?',
                   (encode({'translation_contract_version':'model-text-contract-v4'}),job['job_id']))
    with pytest.raises(HubError,match='MODEL_CONTRACT_PIN_CHANGED'):
        with store.transaction() as db:model.profiles.bind(db,spec)
    assert not store.query('SELECT * FROM model_request_profiles') and not server.calls


def test_known_rate_limit_retry_keeps_original_bound_parameters_after_environment_change(model_fixture, monkeypatch):
    settings,store,job,server,model,http=model_fixture
    server.status=429
    with pytest.raises(HubError,match='API_RATE_LIMITED'):model.invoke(job,'translation',job['payload'])
    profile=store.query('SELECT * FROM model_request_profiles')
    change_environment(monkeypatch)
    server.status=200
    ModelAPI(http,Store(settings.db_path)).invoke(job,'translation',job['payload'])
    assert len(server.calls)==2
    assert all(str(r.url)=='https://model.example.test/v1/chat/completions' for r in server.calls)
    assert all(json.loads(r.content)['model']=='synthetic-translate' and json.loads(r.content)['max_tokens']==4096 for r in server.calls)
    assert store.query('SELECT * FROM model_request_profiles')==profile


def test_unattested_legacy_cache_retains_exact_trailing_slash_key_after_profile_and_restart(model_fixture, monkeypatch):
    settings,store,job,server,model,http=model_fixture
    base='https://model.example.test/v1/'
    monkeypatch.setenv('MODEL_BASE_URL',base)
    legacy=digest([job['job_id'],'translation','chat_completions',base,'synthetic-translate',
                   {'source':job['payload'],'translation':None}])
    with store.transaction() as db:
        db.execute('INSERT INTO model_requests VALUES(?,?,?,NULL)',(legacy,'done',encode({'translation':server.text})))
    first=model.invoke(job,'translation',job['payload'])
    change_environment(monkeypatch)
    assert ModelAPI(http,Store(settings.db_path)).invoke(job,'translation',job['payload'])==first
    assert not server.calls and not store.query('SELECT * FROM api_calls')
    assert not store.query('SELECT * FROM model_call_receipts')
    assert store.query('SELECT legacy_key FROM model_request_profiles')==[{'legacy_key':legacy}]


@pytest.mark.parametrize('field',['MODEL_NAME','MODEL_BASE_URL'])
def test_credentials_accidentally_used_as_selection_are_not_persisted(model_fixture, monkeypatch, field):
    settings,store,job,server,model,http=model_fixture
    secret='synthetic-private-model-key-123456789'
    monkeypatch.setenv(field,secret if field=='MODEL_NAME' else 'https://model.example.test/'+secret)
    with pytest.raises(HubError,match='MODEL_CREDENTIAL_IN_SELECTION'):
        model.invoke(job,'translation',job['payload'])
    assert not server.calls and not store.query('SELECT * FROM model_request_profiles')
    assert not store.query('SELECT * FROM model_requests') and not store.query('SELECT * FROM api_calls')


def test_receipt_committed_after_spec_preparation_is_rechecked_before_first_binding(model_fixture, monkeypatch):
    settings,store,job,server,model,http=model_fixture
    stale,key=model._spec(job,'translation',job['payload'],None)
    monkeypatch.setenv('MODEL_NAME','owned-historical-concurrent-model')
    model.invoke(job,'translation',job['payload'])
    # Model a legacy writer which committed a real receipt but no profile.
    with store.transaction() as db:db.execute('DELETE FROM model_request_profiles')
    with pytest.raises(HubError,match='MODEL_REQUEST_PROFILE_CHANGED'):
        with store.transaction() as db:model.profiles.bind(db,stale)
    assert len(server.calls)==1 and not store.query('SELECT * FROM model_request_profiles')
