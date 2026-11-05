"""Owned offline source fixtures; no live model or platform requests."""
from pathlib import Path
import time
import pytest
from test_extended_web_source import extended
from test_model_receipts import model_fixture
from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.machine_tweets_v2 import MachineTweetsV2,VERSION,SCOPE
from services.hub.render_acceptance import RenderAcceptance


def policy(settings):
    settings.config.update(machine_tweet_enabled=True,policy_version=VERSION,x_web_enabled=True)
    folder=settings.root/'config';folder.mkdir(exist_ok=True)
    (folder/'machine-tweet-policy-v2.json').write_text(encode({'version':VERSION,
        'owner_confirmed_machine_mode':True,'human_review_required':False,'target_site_uid':settings.config['target_bilibili_site_uid'],
        'x_handles':['example_handle_a','example-channel-b'],'rights':{'owner_confirmed':True,
        'actions':['publish_translation','publish_image'],'reference':'Owned synthetic test grant'},
        'owner_response':'Owned synthetic fixture, never human post approval',
        'authorization_basis':'existing_owner_automation_and_content_grant','activation_at':time.time()-60,
        'max_post_age_seconds':7200,'supported_content':SCOPE}),'utf-8')


def test_no_model_or_acceptance_job_created_for_changed_source_files(extended):
    s,d,parent,record,validator,summary=extended
    Path(record['screenshot_file']).write_bytes(b'changed owned screenshot')
    class Never:
        def invoke(self,*a,**kw):pytest.fail('Unverified source must not call any model')
    with pytest.raises(HubError):RenderAcceptance(s,d).run(parent,record,Never())
    assert not d.query("SELECT 1 FROM jobs WHERE source_platform IN ('acceptance','machine_model')")
    assert not d.query('SELECT * FROM publications')


def test_render_acceptance_preserves_clear_marker_and_cannot_be_passed_to_publisher(extended):
    s,d,parent,record,validator,summary=extended;policy(s)
    source_id=record['payload']['post']['id']
    with d.transaction() as db:db.execute('INSERT INTO source_tombstones VALUES(?,?,?,?,?,?)',
        ('x',source_id,'OWNER_BACKLOG_CLEARED',time.time(),parent['job_id'],'owner_clear_all'))
    before=d.query('SELECT * FROM source_tombstones')
    class FailedModel:
        def invoke(self,*a,**kw):raise HubError('OWNED_MODEL_FAILURE')
    with pytest.raises(HubError,match='OWNED_MODEL_FAILURE'):RenderAcceptance(s,d).run(parent,record,FailedModel())
    rows=d.query("SELECT job_id,state,source_platform,job_type FROM jobs WHERE job_type='machine_render_acceptance'")
    assert len(rows)==1 and rows[0]['state']=='failed' and rows[0]['source_platform']=='acceptance'
    assert d.query('SELECT * FROM source_tombstones')==before
    with pytest.raises(HubError,match='MACHINE_POLICY_OR_ARTIFACT_INVALID'):
        MachineTweetsV2(s,d)._snapshot(rows[0]['job_id'])
    assert not d.query('SELECT * FROM publications')
    assert not d.query("SELECT * FROM jobs WHERE source_platform='machine_web'")


def test_render_acceptance_without_real_model_receipts_never_reaches_renderer(extended,monkeypatch):
    s,d,parent,record,validator,summary=extended;phases=[]
    class Unrecorded:
        def invoke(self,job,phase,source,translation=None):
            phases.append(phase)
            return {'translation':source['post']['text']} if phase=='translation' else {'passed':True,'issues':[]}
    monkeypatch.setattr(MachineTweetsV2,'render_evidence',lambda *a:pytest.fail('Model self-report must not authorize rendering acceptance'))
    with pytest.raises(HubError,match='MACHINE_TRANSLATION_CHECK_FAILED'):RenderAcceptance(s,d).run(parent,record,Unrecorded())
    assert phases==['translation','quality']
    assert not d.query('SELECT * FROM model_call_receipts') and not d.query('SELECT * FROM publications')
    parent=d.query("SELECT result FROM jobs WHERE job_type='machine_render_acceptance'")[0]
    import json
    failure=json.loads(parent['result'])['machine_quality_failure']
    assert failure['model_proof']['proof_integrity_passed'] is False
    assert failure['error_code']=='MACHINE_TRANSLATION_CHECK_FAILED' and failure['human_review'] is False


def test_expired_discovery_lease_cannot_start_acceptance_or_bill_models(extended):
    s,d,parent,record,validator,summary=extended
    with d.transaction() as db:db.execute('UPDATE jobs SET lease_until=? WHERE job_id=?',(time.time()-1,parent['job_id']))
    class Never:
        def invoke(self,*a,**kw):pytest.fail('Expired lease must stop before a model')
    with pytest.raises(HubError,match='WEB_SOURCE_CAPTURE_LEASE_REQUIRED'):RenderAcceptance(s,d).run(parent,record,Never())
    assert not d.query("SELECT * FROM jobs WHERE source_platform='acceptance'")


def test_actual_mock_qa_rejection_reason_and_receipts_survive_in_parent_status(extended,model_fixture):
    s,d,parent,record,validator,summary=extended
    _,same_store,_,server,model,_=model_fixture
    assert same_store.path==d.path
    server.text=record['payload']['post']['text']
    server.qa={'passed':False,'issues':[{'source_span':'20:00','translation_span':'20:00','reason':'Owned QA rejection fixture'}]}
    with pytest.raises(HubError,match='MACHINE_TRANSLATION_CHECK_FAILED'):RenderAcceptance(s,d).run(parent,record,model)
    row=d.query("SELECT result FROM jobs WHERE job_type='machine_render_acceptance'")[0]
    import json
    failure=json.loads(row['result'])['machine_quality_failure']
    assert failure['quality']['independent_model_advice']==server.qa
    assert failure['model_proof']['proof_integrity_passed'] is True
    assert failure['model_proof']['qa_recommends_pass'] is False
    assert len(server.calls)==2 and len(d.query('SELECT * FROM model_call_receipts'))==2
    assert not d.query('SELECT * FROM publications')
    assert d.get(failure['model_job_id'])['state']=='failed'


def test_new_contract_acceptance_preserves_legacy_failure_and_same_version_deduplicates(extended):
    s,d,parent,record,validator,summary=extended
    legacy=d.enqueue('acceptance',record['payload']['post']['id'],'machine_render_acceptance','tweet',record['payload'],'machine-render-acceptance-v1')
    with d.transaction() as db:db.execute("UPDATE jobs SET state='failed',error_code='OWNED_LEGACY_QA_REJECTION' WHERE job_id=?",(legacy,))
    before=d.get(legacy);calls=[]
    class Failed:
        def invoke(self,*a,**kw):calls.append('owned invocation');raise HubError('OWNED_MODEL_FAILURE')
    with pytest.raises(HubError,match='OWNED_MODEL_FAILURE'):RenderAcceptance(s,d).run(parent,record,Failed())
    assert d.get(legacy)==before and len(calls)==1
    assert len(d.query("SELECT * FROM jobs WHERE job_type='machine_render_acceptance'"))==2
    with pytest.raises(HubError,match='RENDER_ACCEPTANCE_ALREADY_RECORDED'):RenderAcceptance(s,d).run(parent,record,Failed())
    assert len(calls)==1 and not d.query('SELECT * FROM publications')
