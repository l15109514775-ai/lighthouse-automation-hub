"""Real SQLite/Worker leases with owned historical-parent/native transport seams.

The raw HTML evidence validator is the publisher's separately tested boundary;
the identified OwnedLookup below replaces only that boundary. No live upload,
submission, account, native browser or publication acceptance is claimed.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime,timezone
from pathlib import Path
import json,time,uuid

import pytest

from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.locks import resource_guard
from services.hub.media_processing import atomic_json
from services.hub.replay_natural_video_v3 import natural_metadata
from services.hub.short_video_web import PRINCIPAL
from services.hub import replay_part_moderation_v5 as mod
from services.hub.replay_parts_v5 import STATE,KIND,POLICY,VERSION,ReplayPartGroupsV5,part_type,part_kind
from services.hub.youtube_download import digest
from types import SimpleNamespace

BV='BV1owned0001'

@pytest.fixture
def lookup(runtime):
    s,db=runtime;s.config['target_bilibili_site_uid']='1000000000000000'
    # Historical-only owned registry. There is deliberately no original media,
    # sending grant, uploader or claimed source acceptance in this fixture.
    source='ownedPart01';download=db.enqueue('youtube',source,'youtube_replay_download_v4','video',{},'replay-fifo-v4')
    group=db.enqueue('youtube',source,'replay_parts_publish_parent_v5','video',
        {'site_uid':s.config['target_bilibili_site_uid'],'part_count':1,'parts_manifest_sha256':'a'*64},POLICY)
    ReplayPartGroupsV5(s,db).initialize();url='https://www.youtube.com/watch?v='+source
    metadata={'title':'Owned historical P1/1','description':url,'tags':['owned'],'category':'entertainment'}
    binding={'version':VERSION,'source_id':source,'source_url':url,'site_uid':s.config['target_bilibili_site_uid'],
        'parent_job_id':group,'download_job_id':download,'part_index':0,'part_count':1,'parts_manifest_sha256':'a'*64,
        'whole_plan_sha256':'b'*64,'artifact_sha256':'c'*64,'policy_sha256':'d'*64,'file_bytes':123,
        'metadata':metadata,'metadata_sha256':digest(metadata),'human_review':False}
    key=digest(['bilibili','youtube',source,'site_uid:'+binding['site_uid'],binding['parts_manifest_sha256'],0,binding['artifact_sha256'],part_kind(0)])
    binding['part_idempotency_key']=key
    parent=db.enqueue('youtube',source,part_type(0),'video',binding,POLICY)
    state={'version':VERSION,'state':'submit_unknown','key':key,'attempt_id':str(uuid.uuid4()),'human_review':False,'public_submission_approved':False}
    with db.transaction() as connection:
        connection.execute("UPDATE jobs SET state='completed' WHERE job_id=?",(download,))
        connection.execute("UPDATE jobs SET state='upload_pending' WHERE job_id=?",(group,))
        connection.execute("UPDATE jobs SET state='publish_unknown',attempts=3,result=?,lease_owner=NULL,lease_until=NULL WHERE job_id=?",(encode({STATE:state}),parent))
        connection.execute('INSERT INTO replay_part_groups_v5 VALUES(?,?,?,?,?,?,?,?,?,?)',
            (group,source,binding['site_uid'],download,'a'*64,'d'*64,1,'publishing',time.time(),time.time()))
        connection.execute('INSERT INTO replay_part_children_v5 VALUES(?,?,?,?,?,?,?)',(group,0,parent,key,'c'*64,'publish_unknown',None))
        connection.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',(key,parent,'bilibili','youtube',source,
            'site_uid:'+binding['site_uid'],part_kind(0),'unknown',None,None,time.time()))
    f=SimpleNamespace(s=s,db=db,job=db.get(download),parent=parent,group=group)
    job=db.get(parent);state=job['result'][STATE]
    base=mod.LookupService(s,db,part_index=0)._native_base(job,job['payload'],state)
    intent=s.root/'media'/parent/'replay-native-intents/public-submit.json';intent.parent.mkdir(parents=True)
    atomic_json(intent,{**base,'version':'replay-part-one-use-intent-v5','kind':'public-submit',
        'created_at':datetime.now(timezone.utc).isoformat(),'reservation':{'idempotency_key':state['key']}})
    class OwnedLookup(mod.LookupService):
        def record_result(self,principal,parent_id,observation,*,lookup=False,result_reference=None):
            assert lookup is True and result_reference=={'owned_native_transport_only':True}
            self._operator(principal)  # Actual independent child lease/scope gate.
            job,binding,state=self.snapshot(parent_id,write=False)
            return self._update(principal,job,{**state,'state':observation['state'],'result_observation':observation},
                job_state=observation['state'],stage='owned_raw_validation_seam',publication={
                    'external_id':observation['external_id'],'url':observation['url'],'state':observation['state']})
    f.lookup=OwnedLookup(f.s,f.db);f.parent=parent;f.intent=intent;f.platform=[];f.mode='unknown'
    class OwnedBrowser:
        @contextmanager
        def session(self,manifest,*,assert_lease,before_launch):
            assert manifest=={'owned_query_manifest_only':True}
            with resource_guard(f.s.root/'data/locks/bilibili-video-profile'):
                with resource_guard(f.s.root/'data/locks/heavy'):assert_lease();before_launch();f.platform.append('owned_browser_launch')
                class Session:
                    def operation(self,name,**kwargs):
                        assert name=='lookup';f.platform.append(('lookup',kwargs));assert_lease()
                        if f.mode=='busy_after_launch':raise HubError('HEAVY_RESOURCE_BUSY',retryable=True)
                        if f.mode=='auth_expired':raise HubError('AUTH_EXPIRED')
                        if f.mode=='network':raise HubError('OWNED_LOOKUP_NETWORK_DOWN',retryable=True)
                        if f.mode=='unknown':return {'state':'unknown'}
                        job=f.db.get(f.parent);p=job['payload'];s=job['result'][STATE]
                        return {'result_observation':{'version':f.lookup.RESULT_OBSERVATION_VERSION,'attempt_id':s['attempt_id'],
                            'site_uid':p['site_uid'],'source_url':p['source_url'],'metadata_sha256':p['metadata_sha256'],
                            'external_id':BV,'url':'https://www.bilibili.com/video/'+BV,'state':f.mode},
                            'result_reference':{'owned_native_transport_only':True}}
                yield Session()
    f.browser=OwnedBrowser();f.manifest_builder=lambda *a,**k:{'owned_query_manifest_only':True}
    f.worker=mod.build_lookup_worker(f.s,f.db,service=f.lookup,browser=f.browser,manifest_builder=f.manifest_builder)
    return f

def schedule(f,*,now=None):return mod.schedule_tick(f.s,f.db,f.lookup,now=now)
def parent_snapshot(f):return f.db.get(f.parent),f.db.query('SELECT * FROM publications WHERE job_id=?',(f.parent,))

def test_unknown_without_bv_queries_original_intent_then_stays_unknown_without_reserve_or_parent_attempt(lookup):
    f=lookup;before=parent_snapshot(f);source=f.db.get(f.job['job_id']);made=schedule(f)['scheduled'];assert len(made)==1
    assert schedule(f)['scheduled']==[] and not f.platform
    assert f.worker.once('moderation')
    child=f.db.get(made[0]);assert child['state']=='completed' and child['attempts']==1
    assert f.platform==['owned_browser_launch',('lookup',{})] and parent_snapshot(f)==before and f.db.get(f.job['job_id'])==source
    assert len(f.db.query('SELECT * FROM '+mod.ATTEMPTS))==1 and f.intent.exists()

def test_missing_submit_intent_is_persistently_blocked_not_a_reason_to_resend(lookup):
    f=lookup;before=parent_snapshot(f);f.intent.unlink();out=schedule(f)
    assert not out['scheduled'] and out['blocked'][0]['code']=='REPLAY_PART_LOOKUP_SUBMIT_INTENT_REQUIRED'
    assert parent_snapshot(f)==before and not f.db.query('SELECT * FROM '+mod.ATTEMPTS) and not f.platform
    assert f.db.query("SELECT * FROM events WHERE name='review.required'")

@pytest.mark.parametrize('changed',['key','attempt','uid'])
def test_foreign_or_tampered_intent_never_reaches_platform(lookup,changed):
    f=lookup;body=json.loads(f.intent.read_bytes())
    if changed=='key':body['reservation']['idempotency_key']='0'*64
    elif changed=='attempt':body['attempt_id']=str(uuid.uuid4())
    else:body['site_uid']='999'
    atomic_json(f.intent,body);assert schedule(f)['scheduled']==[] and not f.platform

def test_busy_profile_or_heavy_before_launch_costs_no_platform_attempt_and_no_parent_mutation(lookup):
    f=lookup;before=parent_snapshot(f);child=schedule(f)['scheduled'][0]
    with resource_guard(f.s.root/'data/locks/bilibili-video-profile'):assert f.worker.once('moderation')
    row=f.db.get(child);assert row['state']=='retry_scheduled' and row['attempts']==0
    assert row['error_code']=='REPLAY_PART_LOOKUP_RESOURCE_BUSY' and not f.db.query('SELECT * FROM '+mod.ATTEMPTS)
    assert parent_snapshot(f)==before and not f.platform

def test_busy_after_real_launch_keeps_charged_attempt_and_never_marks_child_send_unknown(lookup):
    f=lookup;before=parent_snapshot(f);child=schedule(f)['scheduled'][0];f.mode='busy_after_launch'
    assert f.worker.once('moderation');row=f.db.get(child)
    assert row['state']=='retry_scheduled' and row['attempts']==1 and len(f.db.query('SELECT * FROM '+mod.ATTEMPTS))==1
    assert parent_snapshot(f)==before

def test_unverified_pending_bv_does_not_change_parent_then_next_poll_accepts_only_full_result(lookup):
    f=lookup;before=parent_snapshot(f);f.mode='awaiting_moderation';first=schedule(f)['scheduled'][0];assert f.worker.once('moderation')
    assert parent_snapshot(f)==before
    assert f.db.get(first)['state']=='completed'
    f.mode='completed';made=schedule(f,now=time.time()+301)['scheduled'];assert len(made)==1
    assert f.worker.once('moderation');parent=f.db.get(f.parent)
    assert parent['state']=='completed' and parent['attempts']==3 and parent['external_publish_id']==BV
    assert f.platform[-1]==('lookup',{})
    assert schedule(f,now=time.time()+1000)['scheduled']==[] and len(f.db.query('SELECT * FROM publications'))==1


def test_known_historical_bv_uses_that_exact_bv_and_unverified_failure_never_frees_parent_for_resend(lookup):
    f=lookup;parent=f.db.get(f.parent);binding=parent['payload'];state=parent['result'][STATE]
    state.update(state='awaiting_moderation',result_observation={'version':f.lookup.RESULT_OBSERVATION_VERSION,
        'state':'awaiting_moderation','external_id':BV,'attempt_id':state['attempt_id'],'site_uid':binding['site_uid'],
        'source_url':binding['source_url'],'metadata_sha256':binding['metadata_sha256']})
    with f.db.transaction() as db:
        db.execute("UPDATE jobs SET state='awaiting_moderation',external_publish_id=?,result=? WHERE job_id=?",(BV,encode(parent['result']),f.parent))
        db.execute("UPDATE publications SET state='awaiting_moderation',external_id=?,external_url=? WHERE job_id=?",(BV,'https://www.bilibili.com/video/'+BV,f.parent))
    before=parent_snapshot(f);f.mode='failed';child=schedule(f)['scheduled'][0]
    assert f.worker.once('moderation') and f.db.get(child)['state']=='completed'
    assert parent_snapshot(f)==before and f.platform[-1]==('lookup',{'external_id':BV})
    assert json.loads(f.db.query('SELECT * FROM '+mod.TABLE)[0]['record'])['unverified_browser_state']=='failed'

def test_48hour_timeout_alerts_and_keeps_original_unknown_not_failed_or_free_to_send(lookup):
    f=lookup;before=parent_snapshot(f);schedule(f)
    out=schedule(f,now=time.time()+48*3600+1)
    assert out['stalled']==[f.parent] and not out['scheduled'] and parent_snapshot(f)==before and not f.platform
    assert f.db.query("SELECT * FROM events WHERE name='pipeline.stalled'") and f.db.query("SELECT * FROM events WHERE name='task.failed'")

def test_daily_attempt_cap_defers_without_creating_more_queries_and_preserves_history(lookup):
    f=lookup;f.s.config['part_lookup_daily_attempts']=1;schedule(f);assert f.worker.once('moderation')
    before=f.db.query('SELECT * FROM '+mod.ATTEMPTS)
    assert schedule(f,now=time.time()+301)['scheduled']==[] and f.db.query('SELECT * FROM '+mod.ATTEMPTS)==before
    assert f.db.get(f.parent)['attempts']==3 and len(f.platform)==2

def test_parent_change_after_child_claim_is_rejected_before_launch(lookup):
    f=lookup;child=schedule(f)['scheduled'][0]
    parent=f.db.get(f.parent);parent['payload']['metadata_sha256']='0'*64
    with f.db.transaction() as db:db.execute('UPDATE jobs SET payload=? WHERE job_id=?',(encode(parent['payload']),f.parent))
    assert f.worker.once('moderation');assert f.db.get(child)['state']=='awaiting_review' and not f.platform

def test_auth_expiry_blocks_poll_stream_without_changing_parent_or_resetting_attempts(lookup):
    f=lookup;before=parent_snapshot(f);child=schedule(f)['scheduled'][0];f.mode='auth_expired'
    assert f.worker.once('moderation');assert f.db.get(child)['state']=='auth_expired'
    assert schedule(f,now=time.time()+301)['scheduled']==[] and parent_snapshot(f)==before
    assert f.db.query("SELECT * FROM events WHERE name='auth.expired'")

def test_lookup_worker_positive_scope_never_claims_or_recovers_old_video_or_publish_jobs(lookup):
    f=lookup;other=f.db.enqueue('youtube','ownedOld000','replay_natural_video_publish_v3','video',{},'owned-old-policy')
    parent_before=f.db.get(f.parent);old_before=f.db.get(other)
    assert not f.worker.once('moderation') and f.db.get(other)==old_before and f.db.get(f.parent)==parent_before
    child=schedule(f)['scheduled'][0];assert f.worker.once('moderation') and f.db.get(child)['state']=='completed'
    f.worker.store.beat('worker:video')
    assert f.db.query("SELECT * FROM heartbeats WHERE name='worker:video-part-moderation-v5'")

def test_no_sending_grant_or_original_media_read_is_needed_for_historical_lookup(lookup,monkeypatch):
    f=lookup
    monkeypatch.setattr(f.lookup,'_require_workflow_launch',lambda *a:pytest.fail('lookup cannot require a fresh sending grant'))
    monkeypatch.setattr(f.lookup,'_verified_source',lambda *a,**k:pytest.fail('lookup must not re-scan long originals'))
    assert schedule(f)['scheduled'] and f.worker.once('moderation')


def test_second_part_uses_its_own_type_key_and_bv_scope_while_first_sibling_is_already_public(lookup):
    f=lookup;parent=f.db.get(f.parent);binding=parent['payload'];binding.update(part_index=1,part_count=2)
    key=digest(['bilibili','youtube',parent['source_id'],'site_uid:'+binding['site_uid'],binding['parts_manifest_sha256'],1,binding['artifact_sha256'],part_kind(1)])
    oldkey=parent['result'][STATE]['key'];binding['part_idempotency_key']=key;parent['result'][STATE]['key']=key
    sibling=deepcopy(binding);sibling.update(part_index=0,artifact_sha256='e'*64)
    siblingkey=digest(['bilibili','youtube',parent['source_id'],'site_uid:'+binding['site_uid'],binding['parts_manifest_sha256'],0,sibling['artifact_sha256'],part_kind(0)])
    sibling['part_idempotency_key']=siblingkey
    with f.db.transaction() as db:
        group=f.db.get(f.group);group['payload']['part_count']=2
        db.execute('UPDATE jobs SET payload=? WHERE job_id=?',(encode(group['payload']),f.group))
        db.execute('UPDATE replay_part_groups_v5 SET part_count=2 WHERE parent_job_id=?',(f.group,))
        db.execute('UPDATE jobs SET job_type=?,payload=?,result=? WHERE job_id=?',(part_type(1),encode(binding),encode(parent['result']),f.parent))
        db.execute('UPDATE publications SET idempotency_key=?,kind=? WHERE idempotency_key=?',(key,part_kind(1),oldkey))
        db.execute('UPDATE replay_part_children_v5 SET part_index=1,idempotency_key=? WHERE child_job_id=?',(key,f.parent))
    sibling_id=f.db.enqueue('youtube',parent['source_id'],part_type(0),'video',sibling,POLICY)
    with f.db.transaction() as db:
        db.execute("UPDATE jobs SET state='completed',external_publish_id=? WHERE job_id=?",('BV1sibling01',sibling_id))
        db.execute('INSERT INTO replay_part_children_v5 VALUES(?,?,?,?,?,?,?)',(f.group,0,sibling_id,siblingkey,'e'*64,'completed','BV1sibling01'))
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',(siblingkey,sibling_id,'bilibili','youtube',parent['source_id'],
            'site_uid:'+binding['site_uid'],part_kind(0),'completed','BV1sibling01','https://www.bilibili.com/video/BV1sibling01',time.time()))
    parent=f.db.get(f.parent);state=parent['result'][STATE]
    service=mod.LookupService(f.s,f.db,part_index=1);base=service._native_base(parent,binding,state)
    atomic_json(f.intent,{**base,'version':'replay-part-one-use-intent-v5','kind':'public-submit','created_at':datetime.now(timezone.utc).isoformat(),
        'reservation':{'idempotency_key':key}})
    sibling_before=f.db.get(sibling_id);publications=f.db.query('SELECT * FROM publications ORDER BY job_id')
    made=mod.schedule_tick(f.s,f.db)['scheduled'];assert len(made)==1
    child=f.db.get(made[0]);assert child['payload']['parent_publish_job_id']==f.parent
    assert child['payload']['part_index']==1 and child['payload']['part_job_type']==part_type(1)
    # Production dynamic service selection must instantiate p02, not default p01.
    worker=mod.build_lookup_worker(f.s,f.db,browser=f.browser,manifest_builder=f.manifest_builder)
    assert worker.once('moderation') and f.db.get(made[0])['state']=='completed'
    assert f.platform[-1]==('lookup',{}) and f.db.get(sibling_id)==sibling_before
    assert f.db.query('SELECT * FROM publications ORDER BY job_id')==publications


@pytest.mark.parametrize('field,value',[('part_index',1),('part_job_type','replay_part_publish_v5_p02'),('parent_publish_job_id','owned-foreign-parent')])
def test_forged_lookup_part_ordinal_or_parent_is_rejected_before_browser(lookup,field,value):
    f=lookup;ident=schedule(f)['scheduled'][0];child=f.db.get(ident);child['payload'][field]=value
    with f.db.transaction() as db:db.execute('UPDATE jobs SET payload=? WHERE job_id=?',(encode(child['payload']),ident))
    before=parent_snapshot(f);assert f.worker.once('moderation')
    assert f.db.get(ident)['state']=='awaiting_review' and not f.platform and parent_snapshot(f)==before


def test_interrupted_lookup_recovers_only_query_child_even_with_unknown_parent_publication(lookup):
    f=lookup;ident=schedule(f)['scheduled'][0];before=parent_snapshot(f)
    claimed=f.worker.store.claim('moderation',180,4);assert claimed['job_id']==ident and claimed['attempts']==1
    with f.db.transaction() as db:db.execute('UPDATE jobs SET lease_until=? WHERE job_id=?',(time.time()-1,ident))
    assert f.worker.store.recover('moderation',4)==1
    assert f.db.get(ident)['state']=='retry_scheduled' and parent_snapshot(f)==before
    assert f.worker.once('moderation') and f.db.get(ident)['state']=='completed' and f.db.get(ident)['attempts']==2
    assert f.platform==['owned_browser_launch',('lookup',{})] and parent_snapshot(f)==before


def test_poll_cap_preserves_last_active_query_then_stalls_without_creating_new_parent_attempt(lookup):
    f=lookup;f.s.config['part_lookup_total_polls']=1;before=parent_snapshot(f)
    ident=schedule(f)['scheduled'][0]
    assert schedule(f)['stalled']==[] and f.db.get(ident)['state']=='pending'
    assert f.worker.once('moderation') and f.db.get(ident)['state']=='completed'
    result=schedule(f,now=time.time()+301)
    assert result['stalled']==[f.parent] and result['scheduled']==[] and parent_snapshot(f)==before
    assert len(f.db.query('SELECT * FROM '+mod.ATTEMPTS))==1


@pytest.mark.parametrize('when',['during_raw_evidence_read','after_final_check_before_cas'])
def test_real_publisher_result_write_is_fenced_by_lookup_child_lease_until_parent_cas(lookup,monkeypatch,when):
    """Only raw acceptance is an explicit seam; the actual result/CAS runs."""
    f=lookup;service=mod.LookupService(f.s,f.db,part_index=0)
    ident=mod.schedule_tick(f.s,f.db,service)['scheduled'][0]
    child=f.worker.store.claim('moderation',180,4);assert child['job_id']==ident
    before=parent_snapshot(f);parent=f.db.get(f.parent);binding=parent['payload'];state=parent['result'][STATE]
    observation={**service._native_base(parent,binding,state),'version':service.RESULT_OBSERVATION_VERSION,
        'state':'completed','external_id':BV,'url':'https://www.bilibili.com/video/'+BV,'public_accessible':True,
        'origin':'later_native_lookup_reconciled_v5','same_submit_attempt_observed':False,
        'native_result_evidence':{'owned_raw_validation_seam':True}}
    def revoke():
        with f.db.transaction() as db:db.execute('UPDATE jobs SET lease_until=? WHERE job_id=?',(time.time()-1,ident))
    import services.hub.replay_part_public_evidence_v5 as raw_boundary
    def owned_raw_validation(*args):
        if when=='during_raw_evidence_read':revoke()
        return {'owned_raw_validation_seam':True}
    monkeypatch.setattr(raw_boundary,'validate_result',owned_raw_validation)
    if when=='after_final_check_before_cas':
        original=service._checkpoint_job_state
        def expire_after_check(job,requested):
            target=original(job,requested);revoke();return target
        monkeypatch.setattr(service,'_checkpoint_job_state',expire_after_check)
    with pytest.raises(HubError,match='LOOKUP_LEASE_LOST'):
        service.record_lookup(child,observation,result_reference=None)
    assert parent_snapshot(f)==before and f.db.get(ident)['state']=='processing' and service._lookup_job is None
    assert f.db.get(f.parent)['attempts']==3 and not f.platform
