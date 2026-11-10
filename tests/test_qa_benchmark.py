"""Owned HTTP simulation verifies the frozen benchmark controller, not model accuracy."""
import json,time
from pathlib import Path
from types import SimpleNamespace
import httpx,pytest
from adapters.http import ProviderHTTP
from adapters.model import ModelAPI
from services.hub.budget import Budget
from services.hub.db import Store,encode
from services.hub.errors import HubError
from services.hub.model_receipts import ModelReceipts
from scripts.qa_benchmark import QABenchmark
from test_model_receipts import model_fixture

@pytest.fixture
def benchmark(model_fixture):
    s,d,*_=model_fixture
    config=Path(__file__).resolve().parents[1]/'tests/fixtures/qa-benchmark-v1.json'
    file=s.root/'qa-benchmark-v1.json';file.write_bytes(config.read_bytes());plan=json.loads(file.read_text('utf-8'))
    ident=d.enqueue('acceptance','owned-qa-catalog','model_catalog_check','system',{},'owned-catalog-v1')
    with d.transaction() as db:db.execute("UPDATE jobs SET state='completed',result=? WHERE job_id=?",(encode({'ids':plan['models']}),ident))
    c=SimpleNamespace(settings=s,store=d,file=file,plan=plan,catalog=ident,calls=[],error_after=None,error=None)
    def server(request):
        c.calls.append(request);body=json.loads(request.content)
        data=json.loads(body['messages'][1]['content'])
        assert set(data)=={'source','translation','context','glossary'}
        assert 'expected' not in data and set(data['source'])=={'post','author','referenced_tweets'}
        if c.error_after==len(c.calls):
            if c.error=='timeout':raise httpx.ReadTimeout('owned timeout',request=request)
            if c.error=='schema':value={'passed':True,'issues':[],'human_review':True}
            elif c.error=='false_empty':value={'passed':False,'issues':[]}
            elif c.error=='true_issues':value={'passed':True,'issues':[{'source_span':'owned','translation_span':'owned','reason':'owned contradiction'}]}
            else:return httpx.Response(c.error,json={'error':'owned simulated rejection'})
        else:
            case=next(x for x in plan['cases'] if x['source']['post']['text']==data['source']['post']['text'])
            value={'passed':case['expected'],'issues':[] if case['expected'] else [{'source_span':case['source']['post']['text'],
                'translation_span':case['translation'],'reason':'Owned simulation, not semantic model verification'}]}
        return httpx.Response(200,json={'model':body['model'],'choices':[{'message':{'content':encode(value)}}],
            'usage':{'prompt_tokens':20,'completion_tokens':10}})
    c.http=ProviderHTTP(Budget(d,s.config['budgets']),httpx.MockTransport(server))
    c.factory=lambda:ModelAPI(c.http,d,settings=s)
    c.controller=QABenchmark(s,d,c.factory)
    return c


def no_publication(c):
    assert not c.store.query('SELECT * FROM publications')
    assert not c.store.query('SELECT * FROM publication_reviews')
    assert not c.store.query("SELECT * FROM jobs WHERE source_platform='machine_web'")
    assert all(x['state'] not in ('pending','processing','retry_scheduled') for x in c.store.query("SELECT state FROM jobs WHERE source_platform='qa_benchmark'"))


def test_fixed_sixteen_qa_only_cells_have_raw_proofs_but_cannot_grant_publication_and_restart_is_free(benchmark):
    c=benchmark;result=c.controller.run(c.file,catalog_job_id=c.catalog)
    assert len(c.calls)==16 and len(result['results'])==16
    assert result['per_model_verdicts']=={'gpt-6-luna':8,'gpt-6.1-sol':8}
    assert not result['model_promotion_authorized'] and not result['human_review'] and not result['publication_authorized']
    receipts=c.store.query('SELECT * FROM model_call_receipts');assert len(receipts)==16
    assert {x['phase'] for x in receipts}=={'quality'} and {x['transport_mode'] for x in receipts}=={'custom_transport'}
    for x in result['results']:
        row=c.store.query('SELECT job_id FROM model_call_receipts WHERE call_id=?',(x['raw_call_id'],))[0]
        proof=ModelReceipts(c.store).assess(c.store.get(row['job_id']),'owned candidate')
        assert not proof['proof_integrity_passed'] and not proof['auto_publish_eligible']
    saved=c.store.query('SELECT * FROM api_calls ORDER BY call_id')
    again=QABenchmark(c.settings,Store(c.store.path),c.factory).run(c.file,catalog_job_id=c.catalog)
    assert again==result and len(c.calls)==16 and c.store.query('SELECT * FROM api_calls ORDER BY call_id')==saved
    no_publication(c)


@pytest.mark.parametrize('error',['timeout','schema','false_empty','true_issues',429,401,402])
def test_error_consumes_cell_and_terminal_run_cannot_be_restarted_to_complete_the_matrix(benchmark,error):
    c=benchmark;c.error=error;c.error_after=3
    with pytest.raises(HubError):c.controller.run(c.file,catalog_job_id=c.catalog)
    assert len(c.calls)==3 and len(c.store.query('SELECT * FROM api_calls'))==3
    rows=c.store.query("SELECT * FROM jobs WHERE job_type='qa_benchmark_cell'")
    assert len(rows)==16 and sum(x['state']=='completed' for x in rows)==2 and sum(x['state']=='failed' for x in rows)==1
    before=c.store.query('SELECT * FROM api_calls ORDER BY call_id')
    with pytest.raises(HubError,match='QA_BENCHMARK_TERMINAL_FAILURE'):c.controller.run(c.file,catalog_job_id=c.catalog)
    assert len(c.calls)==3 and c.store.query('SELECT * FROM api_calls ORDER BY call_id')==before
    no_publication(c)


def test_modified_expected_or_candidate_cannot_reset_the_frozen_plan(benchmark):
    c=benchmark;changed=json.loads(c.file.read_text('utf-8'));changed['cases'][0]['expected']=False;c.file.write_text(encode(changed),'utf-8')
    with pytest.raises(HubError,match='QA_BENCHMARK_PLAN_CHANGED'):c.controller.run(c.file,catalog_job_id=c.catalog)
    assert not c.calls and not c.store.query('SELECT * FROM api_calls');no_publication(c)


def test_changed_profile_parameters_cannot_create_a_second_benchmark(benchmark,monkeypatch):
    c=benchmark;c.controller.run(c.file,catalog_job_id=c.catalog);before=c.store.query('SELECT * FROM api_calls ORDER BY call_id')
    monkeypatch.setenv('MODEL_MAX_OUTPUT_TOKENS','2048')
    with pytest.raises(HubError,match='QA_BENCHMARK_PLAN_CHANGED'):c.controller.run(c.file,catalog_job_id=c.catalog)
    assert len(c.calls)==16 and c.store.query('SELECT * FROM api_calls ORDER BY call_id')==before;no_publication(c)


def test_finished_response_tampering_is_detected_without_another_dispatch(benchmark):
    c=benchmark;result=c.controller.run(c.file,catalog_job_id=c.catalog);row=c.store.query('SELECT * FROM model_call_receipts ORDER BY finished_at LIMIT 1')[0]
    ModelReceipts(c.store)._file(row['job_id'],row['call_id']).write_bytes(b'owned altered bytes')
    with pytest.raises(HubError):c.controller.run(c.file,catalog_job_id=c.catalog)
    assert len(c.calls)==16;no_publication(c)


def test_unlisted_model_cannot_be_used_despite_being_in_a_plan(benchmark):
    c=benchmark
    with c.store.transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode({'ids':['gpt-6-luna']}),c.catalog))
    with pytest.raises(HubError,match='QA_BENCHMARK_MODEL_NOT_LISTED'):c.controller.run(c.file,catalog_job_id=c.catalog)
    assert not c.calls and not c.store.query('SELECT * FROM api_calls');no_publication(c)


def test_budget_exhaustion_stops_the_matrix_without_an_http_attempt_or_restart(benchmark):
    c=benchmark;c.settings.config['budgets']['model']['daily_calls']=2
    with pytest.raises(HubError,match='API_BUDGET_EXCEEDED'):c.controller.run(c.file,catalog_job_id=c.catalog)
    assert len(c.calls)==2 and len(c.store.query('SELECT * FROM api_calls'))==2
    before=c.store.query('SELECT * FROM api_calls ORDER BY call_id')
    with pytest.raises(HubError,match='QA_BENCHMARK_TERMINAL_FAILURE'):c.controller.run(c.file,catalog_job_id=c.catalog)
    assert len(c.calls)==2 and c.store.query('SELECT * FROM api_calls ORDER BY call_id')==before;no_publication(c)
