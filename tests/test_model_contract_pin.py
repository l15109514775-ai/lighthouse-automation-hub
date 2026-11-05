"""Owned MockTransport only: completed receipts pin both phases of one job."""
import pytest

from adapters.model import ModelAPI
from services.hub.db import Store,encode
from services.hub.errors import HubError
from services.hub.model_contract import VERSION
from test_model_receipts import model_fixture


OLD=('model-text-contract-v1','model-text-contract-v2','model-text-contract-v3','model-text-contract-v4')


def set_pin(store,job,version):
    result={**store.get(job['job_id'])['result']}
    if version is None:result.pop('translation_contract_version',None)
    else:result['translation_contract_version']=version
    with store.transaction() as db:
        db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(result),job['job_id']))


@pytest.mark.parametrize('old',OLD)
@pytest.mark.parametrize('phase',['translation','quality'])
def test_switching_contract_after_a_recorded_translation_cannot_buy_another_phase_or_candidate(model_fixture,old,phase):
    _,store,job,server,model,_=model_fixture
    set_pin(store,job,old)
    translated=model.invoke(job,'translation',job['payload'])
    ledger=store.query('SELECT * FROM api_calls')
    requests=store.query('SELECT * FROM model_requests')
    receipts=store.query('SELECT * FROM model_call_receipts')
    set_pin(store,job,VERSION)
    with pytest.raises(HubError,match='MODEL_CONTRACT_PIN_CHANGED'):
        model.invoke(job,phase,job['payload'],None if phase=='translation' else translated['translation'])
    assert len(server.calls)==1 and store.query('SELECT * FROM api_calls')==ledger
    assert store.query('SELECT * FROM model_requests')==requests
    assert store.query('SELECT * FROM model_call_receipts')==receipts


@pytest.mark.parametrize('old',OLD)
def test_removing_result_pin_derives_unique_old_receipt_version_for_new_qa_and_reopened_cache(model_fixture,old):
    settings,store,job,server,model,http=model_fixture
    set_pin(store,job,old)
    translated=model.invoke(job,'translation',job['payload'])
    set_pin(store,job,None)
    reopened=ModelAPI(http,Store(settings.db_path),settings=settings)
    qa=reopened.invoke(job,'quality',job['payload'],translated['translation'])
    assert len(server.calls)==2
    assert {r['contract_version'] for r in store.query('SELECT * FROM model_call_receipts')}=={old}
    assert reopened.invoke(job,'translation',job['payload'])==translated
    assert reopened.invoke(job,'quality',job['payload'],translated['translation'])==qa
    assert len(server.calls)==2
    assert reopened.receipts.assess(store.get(job['job_id']),translated['translation'])['proof_integrity_passed']


@pytest.mark.parametrize('pin',[None,'model-text-contract-v4'])
@pytest.mark.parametrize('phase',['translation','quality'])
def test_inconsistent_historical_phase_versions_are_refused_before_any_new_charge(model_fixture,pin,phase):
    _,store,job,server,model,_=model_fixture
    set_pin(store,job,'model-text-contract-v4')
    translated=model.invoke(job,'translation',job['payload'])
    model.invoke(job,'quality',job['payload'],translated['translation'])
    # Deliberately inconsistent receipt metadata is a corruption fixture, not
    # fabricated valid provenance. The adapter must not choose the latest row.
    with store.transaction() as db:
        db.execute("UPDATE model_call_receipts SET contract_version='model-text-contract-v3' WHERE job_id=? AND phase='quality'",(job['job_id'],))
    set_pin(store,job,pin)
    before=store.query('SELECT * FROM api_calls ORDER BY call_id')
    with pytest.raises(HubError,match='MODEL_CONTRACT_PIN_CHANGED'):
        model.invoke(job,phase,job['payload'],None if phase=='translation' else translated['translation'])
    assert len(server.calls)==2 and store.query('SELECT * FROM api_calls ORDER BY call_id')==before
