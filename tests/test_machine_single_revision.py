"""Owned policy fault injection only; no real live transport, source approval or publication.

The real MockTransport receipt/raw/cache/ledger chain is retained. A narrow
policy-module subclass simulates the live-transport gate in memory; recorded
receipts stay custom_transport and no publication adapter is invoked.
"""
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
import time
import uuid

import pytest

from adapters.model import ModelAPI
from services.hub.db import Store, encode
from services.hub.errors import HubError
from services.hub.machine_tweets_v2 import MachineTweetsV2
from services.hub.model_receipts import ModelReceipts
from test_machine_tweets_v2 import extended
from test_model_receipts import model_fixture


@pytest.fixture
def revision_policy(extended,model_fixture,monkeypatch):
    settings,store,service,parent,node=extended
    _,_,_,server,real_model,http=model_fixture
    lease=str(uuid.uuid4())
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='processing',lease_owner=?,lease_until=? WHERE job_id=?",
            (lease,time.time()+300,parent['job_id']))
    parent=store.get(parent['job_id'])
    case=SimpleNamespace(settings=settings,store=store,service=service,parent=parent,node=node,
        server=server,real_model=real_model,http=http,final_pass=True,rejected_roles={'post'},
        failed_proofs={},expire_after=None,clear_after=None,raise_phase=None,raise_status=None,
        phase_attempts=[])
    service.source=lambda *a,**kw:{'proof':{'owned_synthetic_source':True,'human_review':False},'nodes':[node]}
    class OwnedPolicyReceipts(ModelReceipts):
        def assess(self,job,translation):
            result=super().assess(job,translation)
            # This is an explicit owned policy simulation, never persisted and
            # never a claim about the underlying custom MockTransport.
            if result['proof_integrity_passed'] and result['separate_requests_verified']:
                result={**result,'live_transport_verified':True,
                    'failures':[x for x in result['failures'] if x!='MODEL_CUSTOM_TRANSPORT_NOT_LIVE_VERIFIED']}
            for field in case.failed_proofs.get(job['job_id'],[]):result[field]=False
            return result
    monkeypatch.setattr('services.hub.machine_tweets_v2.ModelReceipts',OwnedPolicyReceipts)
    ordinary_fresh=service.fresh
    def checked_fresh(job,**kwargs):
        if case.expire_after is not None and len(server.calls)>=case.expire_after:
            posted=datetime.fromisoformat(job['payload']['post']['created_at'].replace('Z','+00:00')).timestamp()
            return ordinary_fresh(job,now=posted+7201)
        return ordinary_fresh(job,**kwargs)
    service.fresh=checked_fresh
    class OwnedDriver:
        def invoke(self,child,phase,source,translation=None):
            current=store.get(child['job_id']);role=current['result']['role']
            revised=current['source_id'].endswith(':revision-1')
            assert current['state'] in ('upload_pending','completed','failed')
            assert current['payload']==source
            case.phase_attempts.append((current['job_id'],phase))
            server.text='Owned corrected translation 20:00' if revised else 'Owned translation 20:00'
            passed=case.final_pass if revised else role not in case.rejected_roles
            server.qa={'passed':passed,'issues':[] if passed else [{
                'source_span':'Owned source','translation_span':server.text,'reason':'Owned synthetic QA rejection'}]}
            server.status=case.raise_status if case.raise_phase==phase else 200
            value=case.real_model.invoke(current,phase,source,translation)
            if case.clear_after is not None and len(server.calls)>=case.clear_after:
                with store.transaction() as db:db.execute('INSERT OR IGNORE INTO source_tombstones VALUES(?,?,?,?,?,?)',
                    ('x',parent['source_id'],'OWNER_BACKLOG_CLEARED',time.time(),parent['job_id'],'owner_clear_all'))
            return value
    case.model=OwnedDriver()
    return case


def failed_v4_basis(case,*,node=None):
    node=node or case.node
    child=case.service.model_node(case.parent,node)
    with case.store.transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',
        (encode({**child['result'],'translation_contract_version':'model-text-contract-v4'}),child['job_id']))
    child=case.store.get(child['job_id'])
    translation=case.model.invoke(child,'translation',node['source'])
    quality=case.model.invoke(child,'quality',node['source'],translation['translation'])
    assert quality['passed'] is False
    return child,translation,quality


def model_nodes(case):
    return case.store.query("SELECT * FROM jobs WHERE source_platform='machine_model' AND source_id LIKE ? ORDER BY source_id",
        (case.parent['job_id']+':%',))


def assert_no_publication(case):
    assert not case.store.query('SELECT * FROM publications')
    assert not case.store.query('SELECT * FROM publication_reviews')
    assert not case.store.query("SELECT * FROM jobs WHERE source_platform='machine_web'")
    assert not case.store.query("SELECT * FROM jobs WHERE source_platform='machine_model' AND state IN ('pending','processing','retry_scheduled','awaiting_review')")


def test_known_v4_denial_gets_one_durable_v5_revision_and_reuses_four_calls_after_restart(revision_policy):
    c=revision_policy;old,translation,quality=failed_v4_basis(c)
    receipts=c.store.query('SELECT * FROM model_call_receipts WHERE job_id=? ORDER BY request_key',(old['job_id'],))
    raw={r['call_id']:c.real_model.receipts._file(old['job_id'],r['call_id']).read_bytes() for r in receipts}
    old_calls=c.store.query('SELECT * FROM api_calls ORDER BY call_id')
    entry=c.service.resolve_node(c.parent,c.node,c.model)
    assert entry['job_id']!=old['job_id'] and entry['translation']=='Owned corrected translation 20:00'
    revised=c.store.get(entry['job_id'])
    assert revised['source_id']==c.parent['job_id']+':post:revision-1' and revised['payload']==old['payload']
    assert revised['result']['translation_contract_version']=='model-text-contract-v5'
    assert revised['result']['translation_revision']['previous_model_job_id']==old['job_id']
    assert revised['result']['translation_revision']['index']==1
    assert c.store.get(old['job_id'])['result']['translation_contract_version']=='model-text-contract-v4'
    assert c.store.query('SELECT * FROM model_call_receipts WHERE job_id=? ORDER BY request_key',(old['job_id'],))==receipts
    assert all(c.real_model.receipts._file(old['job_id'],ident).read_bytes()==value for ident,value in raw.items())
    assert all(c.store.query('SELECT * FROM api_calls WHERE call_id=?',(r['call_id'],))[0]==r for r in old_calls)
    assert len(c.server.calls)==4 and len(model_nodes(c))==2
    assert c.store.get(old['job_id'])['state']=='failed'
    assert c.store.get(old['job_id'])['result']['human_review'] is False
    assert {r['transport_mode'] for r in c.store.query('SELECT transport_mode FROM model_call_receipts')}=={'custom_transport'}
    saved_calls=c.store.query('SELECT * FROM api_calls ORDER BY call_id')
    reopened=MachineTweetsV2(c.settings,Store(c.settings.db_path));reopened.source=c.service.source
    c.real_model=ModelAPI(c.http,Store(c.settings.db_path),settings=c.settings)
    again=reopened.resolve_node(c.store.get(c.parent['job_id']),c.node,c.model)
    assert again==entry and len(c.server.calls)==4 and len(model_nodes(c))==2
    assert c.store.query('SELECT * FROM api_calls ORDER BY call_id')==saved_calls
    assert_no_publication(c)


def test_second_qa_denial_stays_failed_without_review_or_third_paid_round(revision_policy):
    c=revision_policy;c.final_pass=False
    old,_,_=failed_v4_basis(c)
    with pytest.raises(HubError):c.service.resolve_node(c.parent,c.node,c.model)
    rows=model_nodes(c);assert len(rows)==2 and len(c.server.calls)==4
    revised=next(c.store.get(r['job_id']) for r in rows if r['source_id'].endswith(':revision-1'))
    assert revised['state']=='failed' and c.store.get(old['job_id'])['state']=='failed'
    assert revised['result']['human_review'] is False
    assert c.store.get(c.parent['job_id'])['result']['machine_quality_failure']['model_job_id']==revised['job_id']
    saved_calls=c.store.query('SELECT * FROM api_calls ORDER BY call_id')
    with pytest.raises(HubError):c.service.resolve_node(c.store.get(c.parent['job_id']),c.node,c.model)
    assert len(c.server.calls)==4 and len(model_nodes(c))==2
    assert c.store.query('SELECT * FROM api_calls ORDER BY call_id')==saved_calls
    assert_no_publication(c)


def test_passing_post_pair_is_preserved_when_only_parent_node_needs_revision(revision_policy):
    c=revision_policy;c.rejected_roles={'parent'}
    post=c.service.resolve_node(c.parent,c.node,c.model)
    post_rows=c.store.query('SELECT * FROM model_call_receipts WHERE job_id=? ORDER BY request_key',(post['job_id'],))
    parent_node=deepcopy(c.node);parent_node['role']='parent'
    old,_,_=failed_v4_basis(c,node=parent_node)
    parent=c.service.resolve_node(c.parent,parent_node,c.model)
    assert c.store.get(parent['job_id'])['source_id']==c.parent['job_id']+':parent:revision-1'
    assert c.store.get(post['job_id'])['source_id']==c.parent['job_id']+':post'
    assert c.store.query('SELECT * FROM model_call_receipts WHERE job_id=? ORDER BY request_key',(post['job_id'],))==post_rows
    assert c.store.get(old['job_id'])['state']=='failed' and len(c.server.calls)==6 and len(model_nodes(c))==3
    assert_no_publication(c)


@pytest.mark.parametrize('phase',['translation','quality'])
@pytest.mark.parametrize('failure',['unknown','auth','budget'])
def test_unconfirmed_or_unauthorized_api_result_never_creates_revision(revision_policy,phase,failure):
    c=revision_policy;c.raise_phase=phase
    if failure=='unknown':c.raise_status='timeout'
    elif failure=='auth':c.raise_status=401
    else:
        c.raise_phase=None
        c.settings.config['budgets']['model']['daily_calls']=0 if phase=='translation' else 1
    with pytest.raises(HubError):c.service.resolve_node(c.parent,c.node,c.model)
    assert len(model_nodes(c))<=1 and not any(r['source_id'].endswith(':revision-1') for r in model_nodes(c))
    before=c.store.query('SELECT * FROM api_calls ORDER BY call_id');count=len(c.server.calls)
    if failure=='unknown':
        with pytest.raises(HubError):c.service.resolve_node(c.parent,c.node,c.model)
        assert len(c.server.calls)==count and c.store.query('SELECT * FROM api_calls ORDER BY call_id')==before
    assert_no_publication(c)


@pytest.mark.parametrize('after',[1,2,3])
@pytest.mark.parametrize('guard',['cleared','expired'])
def test_owner_clear_or_age_boundary_is_checked_before_each_next_paid_phase(revision_policy,after,guard):
    c=revision_policy
    if guard=='cleared':c.clear_after=after
    else:c.expire_after=after
    expected='OWNER_BACKLOG_CLEARED' if guard=='cleared' else 'OWNER_TWO_HOUR_WINDOW_EXPIRED'
    with pytest.raises(HubError,match=expected):c.service.resolve_node(c.parent,c.node,c.model)
    assert len(c.server.calls)==after and len(c.store.query('SELECT * FROM api_calls'))==after
    assert len(model_nodes(c))==(1 if after<3 else 2)
    assert_no_publication(c)


@pytest.mark.parametrize('target,field',[('base','live_transport_verified'),('base','separate_requests_verified'),
    ('revision','live_transport_verified'),('revision','proof_integrity_passed')])
def test_final_proof_requires_both_original_and_revision_independent_live_chains(revision_policy,target,field):
    c=revision_policy;old,_,_=failed_v4_basis(c)
    entry=c.service.resolve_node(c.parent,c.node,c.model)
    c.failed_proofs[old['job_id'] if target=='base' else entry['job_id']]=[field]
    with pytest.raises(HubError):c.service._model_proof(c.parent,c.node,entry)
    assert len(c.server.calls)==4 and len(model_nodes(c))==2
    assert_no_publication(c)


def test_deleting_persisted_revision_context_cannot_reset_slot_or_reuse_unbound_cached_proof(revision_policy):
    c=revision_policy;failed_v4_basis(c)
    entry=c.service.resolve_node(c.parent,c.node,c.model)
    revised=c.store.get(entry['job_id']);unbound={**revised['result']};unbound.pop('translation_revision')
    with c.store.transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(unbound),revised['job_id']))
    before_calls=c.store.query('SELECT * FROM api_calls ORDER BY call_id')
    before_receipts=c.store.query('SELECT * FROM model_call_receipts ORDER BY request_key')
    with pytest.raises(HubError):c.service._model_proof(c.parent,c.node,entry)
    with pytest.raises(HubError):c.real_model.invoke(c.store.get(revised['job_id']),'translation',revised['payload'])
    with pytest.raises(HubError):c.service.resolve_node(c.store.get(c.parent['job_id']),c.node,c.model)
    assert len(c.server.calls)==4 and len(model_nodes(c))==2
    assert c.store.query('SELECT * FROM api_calls ORDER BY call_id')==before_calls
    assert c.store.query('SELECT * FROM model_call_receipts ORDER BY request_key')==before_receipts
    assert_no_publication(c)


def test_render_only_freshness_exception_cannot_be_requested_for_regular_tweet(revision_policy):
    c=revision_policy
    with pytest.raises(HubError):c.service.resolve_node(c.parent,c.node,c.model,require_fresh=False)
    assert not c.server.calls and not c.store.query('SELECT * FROM api_calls')
    assert_no_publication(c)


def test_render_only_acceptance_can_resolve_without_scheduling_or_publishing(revision_policy):
    c=revision_policy
    ident=c.store.enqueue('acceptance','owned-render-only','machine_render_acceptance','tweet',c.parent['payload'],'machine-tweet-v2')
    with c.store.transaction() as db:db.execute("UPDATE jobs SET state='upload_pending' WHERE job_id=?",(ident,))
    c.parent=c.store.get(ident);c.rejected_roles=set()
    entry=c.service.resolve_node(c.parent,c.node,c.model,require_fresh=False)
    assert entry['quality']['passed'] is True and len(c.server.calls)==2
    assert c.store.get(entry['job_id'])['source_id']==ident+':post'
    assert c.store.get(ident)['state']=='upload_pending'
    assert_no_publication(c)
