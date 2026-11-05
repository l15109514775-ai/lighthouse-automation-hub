"""V2 pilot query-only child leases. No v3 grant, platform or new upload."""
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
import time,uuid
import pytest
from test_short_video_web import short_video,PRINCIPAL
from test_replay_highest_video_v2 import highest
from services.hub.db import encode
from services.hub.worker import Worker
from services.hub import replay_highest_moderation_v2b as v


@pytest.fixture
def known(highest):
    f=highest;service=v.ModerationService(f['s'],f['db']);ident=service.queue_pilot(PRINCIPAL,f['source'],f['metadata'],
        actual_owner_workflow_response='Owned workflow only',owner_response_reference='Owned fixture')
    job=f['db'].get(ident);state=job['result'][service.STATE];attempt=str(uuid.uuid4());bv='BV1111111111';now=time.time()
    observation={'version':service.RESULT_OBSERVATION_VERSION,'attempt_id':attempt,'site_uid':job['payload']['site_uid'],
        'source_url':job['payload']['source_url'],'metadata_sha256':job['payload']['metadata_sha256'],
        'external_id':bv,'url':'https://www.bilibili.com/video/'+bv,'state':'awaiting_moderation'}
    state.update(state='awaiting_moderation',attempt_id=attempt,result_observation=observation)
    with f['db'].transaction() as db:
        db.execute("UPDATE jobs SET state='awaiting_moderation',attempts=12,updated_at=?,external_publish_id=?,result=? WHERE job_id=?",
            (now-500,bv,encode(job['result']),ident))
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',(state['key'],ident,'bilibili','youtube',job['source_id'],
            'site_uid:'+job['payload']['site_uid'],service.KIND,'awaiting_moderation',bv,observation['url'],now-500))
    f.update(service=service,parent=ident,observation=observation,now=now);return f


def test_v2_pilot_parent_attempt12_is_not_a_normal_moderation_query_limit(known):
    f=known;before=f['db'].get(f['parent']);result=v.schedule_tick(f['s'],f['db'],f['service'],now=f['now'])
    assert len(result['scheduled_lookup_jobs'])==1 and result['submissions']==0
    child=f['db'].get(result['scheduled_lookup_jobs'][0]);assert child['job_type']==v.LOOKUP_JOB_TYPE and child['policy_version']==f['service'].POLICY
    assert child['payload']['external_id']==before['external_publish_id']
    assert f['db'].get(f['parent'])['attempts']==12 and f['service'].policy()['automation_enabled'] is False
    assert f['service'].WORKFLOW_LAUNCH_RECEIPT.name!='replay-natural-workflow-launch-v3.json'


@pytest.mark.parametrize('change',['missing_bv','unknown_without_bound_result'])
def test_v2_lookup_requires_a_known_matching_bv_and_never_queues_submission(known,change):
    f=known
    with f['db'].transaction() as db:
        if change=='missing_bv':db.execute('UPDATE jobs SET external_publish_id=NULL WHERE job_id=?',(f['parent'],))
        else:
            job=f['db'].get(f['parent']);job['result'][f['service'].STATE].pop('result_observation')
            db.execute("UPDATE jobs SET state='publish_unknown',result=? WHERE job_id=?",(encode(job['result']),job['job_id']))
    assert not v.schedule_tick(f['s'],f['db'],f['service'],now=f['now'])['scheduled_lookup_jobs']
    assert f['db'].get(f['parent'])['attempts']==12


def test_v2_independent_worker_records_reviewing_without_source_bytes_or_v3_modules(known):
    f=known;calls=[];child=v.schedule_tick(f['s'],f['db'],f['service'],now=f['now'])['scheduled_lookup_jobs'][0];f['file'].unlink()
    class Browser:
        @contextmanager
        def session(self,manifest,*,assert_lease):
            assert manifest['version']=='replay-highest-native-browser-manifest-v2';assert_lease();yield self
        def operation(self,operation,**arguments):
            calls.append((operation,arguments));return {'result_observation':deepcopy(f['observation'])}
    handler=v.ModerationLookupHandler(f['s'],f['db'],service=f['service'],manifest_builder=v.highest_replay_manifest,browser=Browser())
    view=v.QueueView(f['db'],lookups=True,lookup_job_type=v.LOOKUP_JOB_TYPE,heartbeat_suffix='v2b')
    assert Worker(f['s'],view,{v.LOOKUP_JOB_TYPE:handler.process}).once('moderation')
    assert calls==[('lookup',{'external_id':'BV1111111111'})]
    assert f['db'].get(child)['state']=='completed' and f['db'].get(f['parent'])['attempts']==12
    assert f['db'].get(f['parent'])['state']=='awaiting_moderation'


def test_entry_keeps_existing_handlers_and_uses_distinct_claim_and_heartbeat_roles(known,monkeypatch):
    from services.hub import replay_highest_worker_runtime_v2
    f=known;legacy=lambda job:'Owned prior handler';old=Worker(f['s'],f['db'],{'owned_legacy':legacy})
    monkeypatch.setattr(replay_highest_worker_runtime_v2,'build_worker',lambda *args:(old,f['service']))
    worker,service=v.build_worker(f['s'],f['db'])
    assert worker.handlers['owned_legacy'] is legacy and set(worker.moderation_worker.handlers)=={v.LOOKUP_JOB_TYPE}
    assert worker.store.lookup_job_type==v.LOOKUP_JOB_TYPE and worker.moderation_worker.store.lookups is True
    worker.moderation_worker.store.beat('worker:video')
    assert {row['name'] for row in f['db'].query('SELECT * FROM heartbeats')}=={'worker:video-moderation-v2b'}


def test_v2_loop_orders_bounded_query_scheduling_and_keeps_once_readonly_submission(known,monkeypatch):
    f=known;calls=[]
    monkeypatch.setattr(v,'schedule_tick',lambda *args:calls.append('schedule known BV'))
    worker=SimpleNamespace(once=lambda role:calls.append(role) or False,
        moderation_worker=SimpleNamespace(once=lambda role:calls.append(role) or False))
    assert v.run_loop(f['s'],f['db'],worker,f['service'],once=True,clock=lambda:0) is False
    assert calls==['schedule known BV','video','moderation']


def test_pilot_side_restart_cannot_recover_active_download_and_main_cannot_take_lookup(known):
    f=known;child=v.schedule_tick(f['s'],f['db'],f['service'],now=f['now'])['scheduled_lookup_jobs'][0]
    main=f['db'].enqueue('youtube','ownedOther01','youtube_download_v2','video',{'owned':True},'Owned')
    with f['db'].transaction() as db:db.execute('UPDATE jobs SET created_at=-100 WHERE job_id=?',(main,))
    main_view=v.QueueView(f['db'],lookups=False,lookup_job_type=v.LOOKUP_JOB_TYPE,heartbeat_suffix='v2b')
    side=v.QueueView(f['db'],lookups=True,lookup_job_type=v.LOOKUP_JOB_TYPE,heartbeat_suffix='v2b')
    active=main_view.claim('video',60,4);query=side.claim('moderation',60,4)
    assert active['job_id']==main and query['job_id']==child
    assert side.recover('moderation',4,all_processing=True)==1
    assert f['db'].get(main)['lease_owner']==active['lease_owner'] and f['db'].get(main)['state']=='processing'
    recovered=side.claim('moderation',60,4);assert recovered['attempts']==2
    assert main_view.recover('video',4,all_processing=True)==1
    assert f['db'].get(child)['lease_owner']==recovered['lease_owner'] and f['db'].get(child)['state']=='processing'
