"""Owned SQLite and mocked Worker phases only; never a browser or upload."""
from copy import deepcopy
from types import SimpleNamespace
import json
import uuid

import pytest

from services.hub import replay_worker_runtime as rt
from services.hub.db import encode
from services.hub.errors import HubError
from test_short_video_web import short_video,PRINCIPAL,result as native_result
from test_replay_video_web import replay,uploaded,authorize,owned_proof,native_prepare,reserve

BV='BV0000000000'


@pytest.fixture
def owned(runtime):
    settings,store=runtime
    policy={'source_scope':'pilot_single','acceptance_video_ids':[rt.PILOT_SOURCE],
        'automation_enabled':False,'_sha256':'a'*64,'target_site_uid':'123456'}
    service=SimpleNamespace(policy=lambda:deepcopy(policy))
    payload={'source_id':rt.PILOT_SOURCE,'policy_sha256':'a'*64,'site_uid':'123456',
        'source_url':'https://www.youtube.com/watch?v='+rt.PILOT_SOURCE,'metadata_sha256':'c'*64}
    ident=store.enqueue('youtube',rt.PILOT_SOURCE,'replay_video_publish','video',payload,rt.POLICY)
    attempt=str(uuid.uuid4())
    state={'state':'awaiting_moderation','key':'b'*64,'public_submission_approved':False,'attempt_id':attempt,
        'result_observation':{'version':'short-video-browser-result-v1','attempt_id':attempt,'site_uid':'123456',
            'source_url':payload['source_url'],'metadata_sha256':payload['metadata_sha256'],
            'external_id':BV,'url':'https://www.bilibili.com/video/'+BV,'state':'awaiting_moderation'}}
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='awaiting_moderation',attempts=1,updated_at=1000,result=?,external_publish_id=? WHERE job_id=?",
            (encode({rt.STATE:state}),BV,ident))
        db.execute("INSERT INTO publications VALUES(?,?,'bilibili','youtube',?,'site_uid:123456',?,'awaiting_moderation',?,?,1000)",
            ('b'*64,ident,rt.PILOT_SOURCE,rt.KIND,BV,'https://www.bilibili.com/video/'+BV))
        db.execute("INSERT INTO audit(at,principal,action,target,outcome) VALUES(1000,'owned fixture','short_video_reserve_submit',?,'one_artifact_one_click_reserved')",(ident,))
    return settings,store,service,policy,ident


def test_known_bv_tick_changes_only_scheduling_fields_and_never_resets_attempts(owned):
    settings,store,service,_,ident=owned
    before=store.get(ident);publications=store.query('SELECT * FROM publications')
    result=rt.reconcile_tick(settings,store,service,now=1401)
    after=store.get(ident)
    assert result['scheduled_known_bv_lookups']==[ident]
    assert after['state']=='retry_scheduled' and after['next_run']==1401
    for key in before:
        if key not in ('state','updated_at','next_run','stage'):assert before[key]==after[key]
    assert store.query('SELECT * FROM publications')==publications
    assert rt.reconcile_tick(settings,store,service,now=1402)['scheduled_known_bv_lookups']==[]
    audit=store.query('SELECT * FROM audit')
    assert len(audit)==2 and audit[-1]['principal']==rt.PRINCIPAL and audit[-1]['outcome']=='lookup_only_no_upload_or_approval'


@pytest.mark.parametrize('change',['unknown','no_bv','wrong_bv','wrong_uid','max_attempts','active_lease','too_recent','wrong_attempt','publication_unknown','no_dispatch_audit'])
def test_tick_does_not_blindly_requeue_unresolved_or_unbound_work(owned,change):
    settings,store,service,_,ident=owned
    job=store.get(ident);state=job['result'][rt.STATE]
    with store.transaction() as db:
        if change=='unknown':db.execute("UPDATE jobs SET state='publish_unknown' WHERE job_id=?",(ident,))
        elif change=='no_bv':db.execute('UPDATE jobs SET external_publish_id=NULL WHERE job_id=?',(ident,))
        elif change=='max_attempts':db.execute('UPDATE jobs SET attempts=? WHERE job_id=?',(settings.config['max_attempts'],ident))
        elif change=='active_lease':db.execute('UPDATE jobs SET lease_owner=?,lease_until=2000 WHERE job_id=?',('owned-lease',ident))
        elif change=='too_recent':db.execute('UPDATE jobs SET updated_at=1300 WHERE job_id=?',(ident,))
        elif change=='publication_unknown':db.execute("UPDATE publications SET state='unknown'")
        elif change=='no_dispatch_audit':db.execute('DELETE FROM audit')
        else:
            if change=='wrong_bv':state['result_observation']['external_id']='BV1111111111'
            elif change=='wrong_attempt':state['result_observation']['attempt_id']=str(uuid.uuid4())
            elif change=='wrong_uid':job['payload']['site_uid']='other'
            db.execute('UPDATE jobs SET payload=?,result=? WHERE job_id=?',(encode(job['payload']),encode(job['result']),ident))
    before=store.get(ident);audit=store.query('SELECT * FROM audit')
    assert rt.reconcile_tick(settings,store,service,now=1401)['scheduled_known_bv_lookups']==[]
    assert store.get(ident)==before and store.query('SELECT * FROM audit')==audit


@pytest.mark.parametrize('field,value',[('source_scope','automatic_new_only'),('acceptance_video_ids',['differentID']),('automation_enabled',True)])
def test_runtime_refuses_all_automatic_scope_or_other_pilot(owned,field,value):
    settings,store,service,policy,ident=owned;policy[field]=value
    before=store.get(ident)
    with pytest.raises(HubError,match='REPLAY_RUNTIME_PILOT_SCOPE_REQUIRED'):
        rt.reconcile_tick(settings,store,service,now=1401)
    assert store.get(ident)==before


def test_guarded_handler_runs_only_correct_source_with_current_policy(owned):
    _,store,service,_,ident=owned;calls=[]
    wrapped=rt.guarded_handler(service,lambda job:calls.append(job['job_id']) or 'owned result')
    assert wrapped(store.get(ident))=='owned result' and calls==[ident]
    job=store.get(ident);job['source_id']='differentID'
    with pytest.raises(HubError,match='REPLAY_RUNTIME_JOB_OUTSIDE_PILOT'):wrapped(job)
    assert calls==[ident]


def test_loop_uses_ordinary_worker_video_once_and_cannot_queue_new_source(owned,monkeypatch):
    settings,store,service,_,_=owned;calls=[]
    worker=SimpleNamespace(once=lambda queue:calls.append(queue) or False)
    monkeypatch.setattr(rt,'reconcile_tick',lambda *a,**kw:calls.append('bounded tick'))
    assert rt.run_loop(settings,store,worker,service,once=True,clock=lambda:0,sleep=lambda n:pytest.fail('once cannot sleep')) is False
    assert calls==['bounded tick','video']


@pytest.mark.parametrize('prefix',['','YOUTUBE_API_KEY='])
def test_independent_owned_key_file_is_parent_only_not_printed(tmp_path,monkeypatch,capsys,prefix):
    value='OwnedSyntheticYoutubeKeyNeverReal12345'
    file=tmp_path/'youtube-api-key';file.write_text(prefix+value+'\n','ascii');file.chmod(0o600)
    monkeypatch.delenv('YOUTUBE_API_KEY',raising=False)
    rt.load_youtube_key(file)
    assert rt.os.environ['YOUTUBE_API_KEY']==value
    assert capsys.readouterr().out==capsys.readouterr().err==''
    monkeypatch.delenv('YOUTUBE_API_KEY')


@pytest.mark.parametrize('body',['too-short','YOUTUBE_API_KEY=OwnedValue1234567890123\nOTHER_SECRET=owned', 'x'*513])
def test_multi_secret_or_unbounded_key_file_rejected(tmp_path,body):
    file=tmp_path/'youtube-api-key';file.write_text(body,'ascii');file.chmod(0o600)
    with pytest.raises(HubError,match='REPLAY_RUNTIME_YOUTUBE_KEY_FILE_INVALID'):rt.load_youtube_key(file)


def test_real_domain_record_result_safe_shape_schedules_known_bv_lookup(replay,monkeypatch):
    settings,store,service,*_=replay
    ident,attempt,binding=uploaded(replay);authorize(replay,ident)
    owned_proof(replay,ident,monkeypatch);native_prepare(replay,ident,attempt)
    assert reserve(replay,ident,attempt)['can_click_submit'] is True
    service.record_result(PRINCIPAL,ident,native_result(attempt,binding))
    persisted=store.get(ident)
    assert 'same_submit_attempt_observed' not in persisted['result'][rt.STATE]['result_observation']
    monkeypatch.setattr(rt,'PILOT_SOURCE',binding['source_id'])
    outcome=rt.reconcile_tick(settings,store,service,now=persisted['updated_at']+301)
    assert outcome['scheduled_known_bv_lookups']==[ident]
    after=store.get(ident)
    assert after['result']==persisted['result'] and after['payload']==persisted['payload'] and after['attempts']==persisted['attempts']
