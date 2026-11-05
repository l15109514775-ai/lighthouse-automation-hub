"""Synthetic policy/queue failure scenarios; never sends to a real account."""
from copy import deepcopy
from datetime import datetime,timezone
import time
import pytest
from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.machine_routes import MachineRoutes,machine_service,is_machine_policy
from services.hub.machine_tweets import MachineTweets,VERSION as V1
from services.hub.machine_tweets_v2 import MachineTweetsV2,VERSION,KIND,SCOPE
from services.hub.pipelines import TweetPipeline
from services.hub.worker import Worker


@pytest.fixture
def extended(runtime):
    s,d=runtime;s.config.update(machine_tweet_enabled=True,policy_version=VERSION,x_web_enabled=True)
    (s.root/'config').mkdir()
    p={'version':VERSION,'owner_confirmed_machine_mode':True,'human_review_required':False,
       'target_site_uid':s.config['target_bilibili_site_uid'],'x_handles':['example_handle_a','example-channel-b'],
       'rights':{'owner_confirmed':True,'actions':['publish_translation','publish_image'],'reference':'synthetic test grant'},
       'owner_response':'Synthetic authorization fixture; no human post verification',
       'authorization_basis':'existing_owner_automation_and_content_grant','max_post_age_seconds':7200,
       'activation_at':time.time()-60,'supported_content':SCOPE}
    (s.root/'config/machine-tweet-policy-v2.json').write_text(encode(p),'utf-8')
    source={'post':{'id':'1000000000000000002','text':'Owned source 20:00',
          'created_at':datetime.fromtimestamp(time.time()-120,timezone.utc).isoformat(),'referenced_tweets':[]},
        'author':{'username':'example_handle_a','name':'Owned synthetic author'},
        'url':'https://x.com/example_handle_a/status/1000000000000000002',
        'capture':{'human_review':False,'captured_at':datetime.now(timezone.utc).isoformat()}}
    ident=d.enqueue('x',source['post']['id'],'tweet_process','tweet',source,VERSION)
    job=d.get(ident);node={'role':'post','source':source,'media':[],'provenance':[]}
    return s,d,MachineTweetsV2(s,d),job,node


@pytest.fixture
def leased_extended(extended):
    """Direct process calls represent the worker after a real durable claim."""
    s,d,service,job,node=extended
    claimed=d.claim('tweet',180,3)
    assert claimed and claimed['job_id']==job['job_id']
    assert claimed['state']=='processing' and claimed['lease_owner'] and claimed['lease_until']>time.time()
    return s,d,service,claimed,node


def test_v2_policy_is_separate_and_never_changes_v1_or_human_flags(extended):
    s,d,service,job,node=extended
    old=s.root/'config/machine-tweet-policy.json';old.write_bytes(b'owned old policy sentinel')
    before=old.stat().st_mtime_ns
    assert service.policy()['human_review_required'] is False
    assert old.read_bytes()==b'owned old policy sentinel' and old.stat().st_mtime_ns==before
    assert job['payload']['capture']['human_review'] is False
    assert not d.query('SELECT * FROM publications')


@pytest.mark.parametrize('change',[{'human_review_required':True},{'owner_confirmed_machine_mode':False},
    {'supported_content':['all_content']},{'max_post_age_seconds':7201},
    {'activation_at':float('nan')},{'authorization_basis':'model_self_report'},
    {'target_site_uid':'123'},{'version':V1}])
def test_v2_rejects_invalid_or_broadened_policy(extended,change):
    s,d,service,job,node=extended;p=service.policy();p.update(change)
    (s.root/'config/machine-tweet-policy-v2.json').write_text(encode(p),'utf-8')
    with pytest.raises(HubError,match='MACHINE_TWEET_POLICY_INVALID'):service.policy()


def test_model_node_is_immutable_durable_and_not_claimable_by_another_worker(extended):
    s,d,service,job,node=extended
    a=service.model_node(job,node);b=service.model_node(job,node)
    assert a['job_id']==b['job_id'] and a['state']=='upload_pending'
    assert a['result']['parent_job_id']==job['job_id'] and a['result']['human_review'] is False
    with d.transaction() as db:db.execute("UPDATE jobs SET state='failed' WHERE job_id=?",(job['job_id'],))
    assert d.claim('tweet',180,3) is None
    changed=deepcopy(node);changed['source']['post']['text']='Changed source'
    with pytest.raises(HubError,match='MACHINE_V2_MODEL_INPUT_CHANGED'):service.model_node(job,changed)
    assert d.get(a['job_id'])['payload']==node['source']


def test_source_validation_happens_before_creating_model_nodes_or_charging(leased_extended):
    s,d,service,job,node=leased_extended
    def invalid(*a,**kw):raise HubError('EXTENDED_SOURCE_HASH_CHANGED')
    service.source=invalid
    class Never:
        def invoke(self,*a,**kw):pytest.fail('Unverified source must not invoke a model')
    with pytest.raises(HubError,match='EXTENDED_SOURCE_HASH_CHANGED'):service.process(job,Never())
    assert not d.query("SELECT 1 FROM jobs WHERE source_platform='machine_model'")


@pytest.mark.parametrize('blocked',['cleared','expired'])
def test_owner_window_or_tombstone_blocks_extended_source_and_paid_work(leased_extended,blocked):
    s,d,service,job,node=leased_extended
    if blocked=='cleared':
        with d.transaction() as db:db.execute('INSERT INTO source_tombstones VALUES(?,?,?,?,?,?)',
            ('x',job['source_id'],'OWNER_BACKLOG_CLEARED',time.time(),job['job_id'],'owner_clear_all'))
    else:
        job['payload']['post']['created_at']=datetime.fromtimestamp(time.time()-7201,timezone.utc).isoformat()
    service.source=lambda *a,**kw:pytest.fail('Blocked source must not reach source or model work')
    with pytest.raises(HubError,match='OWNER_'):service.process(job,None)
    assert not d.query('SELECT * FROM api_calls') and not d.query('SELECT * FROM publications')


def test_model_error_stops_without_human_review_or_publication(extended,monkeypatch):
    s,d,service,job,node=extended
    service.source=lambda *a,**kw:{'proof':{'synthetic':True},'nodes':[node]}
    class Broken:
        def invoke(self,*a,**kw):raise HubError('MODEL_PERMISSION_DENIED',review=True)
    pipeline=TweetPipeline(s,d,None);pipeline.model=Broken()
    monkeypatch.setattr(MachineTweetsV2,'source',service.source)
    assert Worker(s,d,{'tweet_process':pipeline.process}).once('tweet')
    saved=d.get(job['job_id']);assert saved['state']=='failed' and saved['error_code']=='MODEL_PERMISSION_DENIED'
    assert not d.query('SELECT * FROM publications')


def test_window_is_rechecked_between_translation_and_quality(leased_extended):
    s,d,service,job,node=leased_extended;service.source=lambda *a,**kw:{'proof':{},'nodes':[node]}
    phases=[]
    class ClearDuringTranslation:
        def invoke(self,child,phase,source,*args):
            phases.append(phase)
            with d.transaction() as db:db.execute('INSERT INTO source_tombstones VALUES(?,?,?,?,?,?)',
                ('x',job['source_id'],'OWNER_BACKLOG_CLEARED',time.time(),job['job_id'],'owner_clear_all'))
            return {'translation':'Owned translation 20:00'}
    with pytest.raises(HubError,match='OWNER_BACKLOG_CLEARED'):service.process(job,ClearDuringTranslation())
    assert phases==['translation'] and not d.query('SELECT * FROM publications')


def test_mocked_model_receipts_never_qualify_as_live_evidence(extended,monkeypatch):
    s,d,service,job,node=extended;child=service.model_node(job,node)
    monkeypatch.setattr('services.hub.machine_tweets_v2.ModelReceipts.assess',lambda *a,**kw:{
        'proof_integrity_passed':True,'live_transport_verified':False,'separate_requests_verified':True,'qa_recommends_pass':True})
    with pytest.raises(HubError,match='MACHINE_TRANSLATION_CHECK_FAILED'):
        service._model_proof(job,node,{'job_id':child['job_id'],'translation':'Owned translation 20:00','quality':{'passed':True,'issues':[]}})
    assert not d.query('SELECT * FROM publications')


def test_existing_publication_across_versions_prevents_all_model_calls(leased_extended):
    s,d,service,job,node=leased_extended
    with d.transaction() as db:db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        ('owned-legacy-key',job['job_id'],'bilibili','x',job['source_id'],
         'site_uid:'+s.config['target_bilibili_site_uid'],'dynamic_website_supervised','completed','123',
         'https://t.bilibili.com/123',time.time()))
    service.source=lambda *a,**kw:pytest.fail('Duplicate should reuse known publication without new source/model work')
    result=service.process(job,None)
    assert result.state=='completed' and result.external_id=='123'
    assert len(d.query('SELECT * FROM publications'))==1


def test_old_pending_dispatch_cannot_get_v2_permission(extended,monkeypatch):
    s,d,service,job,node=extended
    child={'payload':{'version':V1,'parent_job_id':job['job_id']}}
    monkeypatch.setattr(MachineTweets,'dispatch',lambda *a:pytest.fail('Old unreserved send must be stopped'))
    with pytest.raises(HubError,match='MACHINE_SOURCE_GENERATION_NOT_ACTIVE'):MachineRoutes(s,d).dispatch(child)


def test_old_unknown_dispatch_uses_its_original_lookup_service(extended,monkeypatch):
    s,d,service,job,node=extended
    with d.transaction() as db:db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        ('owned-unknown-key',job['job_id'],'bilibili','x',job['source_id'],
         'site_uid:'+s.config['target_bilibili_site_uid'],'dynamic_website_machine_v1','unknown',None,None,time.time()))
    monkeypatch.setattr(MachineTweets,'dispatch',lambda self,child:{'original_version':self.version,'lookup_only':True})
    value=MachineRoutes(s,d).dispatch({'payload':{'version':V1,'parent_job_id':job['job_id']}})
    assert value=={'original_version':V1,'lookup_only':True}


def test_explicit_unknown_policy_never_falls_into_human_or_other_machine_mode(extended):
    s,d,service,job,node=extended
    with pytest.raises(HubError,match='MACHINE_TWEET_POLICY_UNSUPPORTED'):machine_service(s,d,'forged-policy')
    assert is_machine_policy(VERSION+':source-2') and not is_machine_policy('human-review-v1')


def test_worker_preserves_v2_qa_rejection_on_failed_parent_without_manual_review(extended,monkeypatch):
    s,d,service,job,node=extended
    monkeypatch.setattr(MachineTweetsV2,'source',lambda *a,**kw:{'proof':{},'nodes':[node]})
    class Rejected:
        def invoke(self,child,phase,source,translation=None):
            return {'translation':'Owned translation 20:00'} if phase=='translation' else {
                'passed':False,'issues':[{'source_span':'Owned source','translation_span':'Owned translation','reason':'Owned QA rejected'}]}
    pipeline=TweetPipeline(s,d,None);pipeline.model=Rejected()
    assert Worker(s,d,{'tweet_process':pipeline.process}).once('tweet')
    saved=d.get(job['job_id']);failure=saved['result']['machine_quality_failure']
    assert saved['state']=='failed' and saved['error_code']=='MACHINE_TRANSLATION_CHECK_FAILED'
    assert failure['quality']['independent_model_advice']['issues'][0]['reason']=='Owned QA rejected'
    assert failure['human_review'] is False and d.get(failure['model_job_id'])['state']=='failed'
    assert not d.query('SELECT * FROM publications')
    assert not d.query("SELECT * FROM jobs WHERE source_platform='machine_web'")
