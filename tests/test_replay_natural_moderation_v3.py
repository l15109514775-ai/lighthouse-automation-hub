"""Owned SQLite leases and native transport observations; no account or HTTP."""
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
import time
import pytest
from test_short_video_web import short_video
from test_replay_highest_video_v2 import highest
from test_replay_natural_video_v3 import natural
from test_replay_natural_worker_runtime_v3 import known
from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.worker import Worker
from services.hub import replay_natural_moderation_v3 as m
from services.hub.replay_natural_video_v3 import STATE,JOB_TYPE
from services.hub.replay_natural_worker_runtime_v3 import moderation_loop


def start(known,*,attempts=9):
    f,parent=known;now=time.time()
    with f['db'].transaction() as db:db.execute('UPDATE jobs SET attempts=?,updated_at=? WHERE job_id=?',(attempts,now-400,parent))
    receipt=m.schedule_tick(f['s'],f['db'],f['service'],now=now)
    assert len(receipt['scheduled_lookup_jobs'])==1
    return f,parent,receipt['scheduled_lookup_jobs'][0],now


def finish_child(f,child):
    with f['db'].transaction() as db:db.execute("UPDATE jobs SET state='completed' WHERE job_id=?",(child,))


def test_moderation_beyond_four_parent_attempts_and_fifteen_minutes_still_queries(known):
    f,parent,child,now=start(known)
    assert f['db'].get(parent)['attempts']==9
    assert m.schedule_tick(f['s'],f['db'],f['service'],now=now+301)['scheduled_lookup_jobs']==[] # Active child.
    finish_child(f,child)
    later=m.schedule_tick(f['s'],f['db'],f['service'],now=now+1800)
    assert len(later['scheduled_lookup_jobs'])==1
    cp=f['db'].get(parent)['result'][STATE]['moderation']
    assert cp['polls_total']==2 and cp['next_check_at']==now+2100
    finish_child(f,later['scheduled_lookup_jobs'][0])
    later=m.schedule_tick(f['s'],f['db'],f['service'],now=now+4000)
    assert len(later['scheduled_lookup_jobs'])==1
    assert f['db'].get(parent)['result'][STATE]['moderation']['next_check_at']==now+5800
    assert f['db'].get(parent)['attempts']==9
    assert len(f['db'].query('SELECT * FROM publications WHERE job_id=?',(parent,)))==1


def test_daily_budget_defers_only_queries_until_next_day(known):
    f,parent,child,now=start(known);finish_child(f,child)
    policy=f['service'].policy;f['service'].policy=lambda:{**policy(),'moderation_daily_polls':1}
    assert m.schedule_tick(f['s'],f['db'],f['service'],now=now+301)['scheduled_lookup_jobs']==[]
    assert f['db'].get(parent)['result'][STATE]['moderation']['next_check_at']==m.day_bounds(now)[1]
    assert f['db'].get(parent)['attempts']==9


def test_48_hour_limit_alerts_once_without_new_submit_or_attempt_reset(known):
    f,parent,child,now=start(known);finish_child(f,child)
    result=m.schedule_tick(f['s'],f['db'],f['service'],now=now+48*3600)
    assert result['stalled_parent_jobs']==[parent] and result['scheduled_lookup_jobs']==[]
    assert m.schedule_tick(f['s'],f['db'],f['service'],now=now+49*3600)['stalled_parent_jobs']==[]
    assert len(f['db'].query("SELECT * FROM events WHERE name='pipeline.stalled'"))==1
    job=f['db'].get(parent);assert job['state']=='awaiting_moderation' and job['attempts']==9


def test_role_claim_and_recover_cannot_steal_other_roles_processing_job(known):
    f,parent,child,now=start(known);store=f['db']
    main=store.enqueue('youtube','ownedOther1','youtube_download_v3','video',{'owned':True},'owned-policy')
    with store.transaction() as db:db.execute('UPDATE jobs SET created_at=-100 WHERE job_id=?',(main,))
    main_view=m.QueueView(store,lookups=False);lookup_view=m.QueueView(store,lookups=True)
    main_job=main_view.claim('video',60,4);assert main_job['job_id']==main
    lookup_job=lookup_view.claim('moderation',60,4);assert lookup_job['job_id']==child
    assert main_view.recover('video',4,all_processing=True)==1
    assert store.get(child)['state']=='processing' and store.get(child)['lease_owner']==lookup_job['lease_owner']
    resumed=main_view.claim('video',60,4);assert resumed['job_id']==main and resumed['attempts']==2
    assert lookup_view.recover('moderation',4,all_processing=True)==1
    assert store.get(main)['state']=='processing' and store.get(main)['lease_owner']==resumed['lease_owner']
    assert lookup_view.claim('moderation',60,4)['job_id']==child
    with pytest.raises(ValueError):main_view.claim('system',60,4)


class OwnedBrowser:
    def __init__(self,observation=None,error=None):self.observation,self.error,self.calls=observation,error,[]
    @contextmanager
    def session(self,manifest,*,assert_lease):
        assert_lease();self.calls.append(('manifest',manifest['version']));yield self
    def operation(self,op,**arguments):
        self.calls.append((op,arguments))
        if self.error:raise self.error
        return {'result_observation':deepcopy(self.observation)}


def test_ordinary_lookup_worker_records_reviewing_and_completes_child_not_parent(known):
    f,parent,child,now=start(known);f['file'].unlink()
    observation=f['db'].get(parent)['result'][STATE]['result_observation'];browser=OwnedBrowser(observation)
    service=m.ModerationService(f['s'],f['db']);handler=m.ModerationLookupHandler(f['s'],f['db'],service=service,browser=browser)
    worker=Worker(f['s'],m.QueueView(f['db'],lookups=True),{m.LOOKUP_JOB_TYPE:handler.process})
    assert worker.once('moderation') is True
    result=f['db'].get(child);assert result['state']=='completed' and result['attempts']==1
    cp=f['db'].get(parent);assert cp['state']=='awaiting_moderation' and cp['attempts']==9
    assert cp['result'][STATE]['moderation']['active_lookup_job_id'] is None
    assert browser.calls[1]==('lookup',{'external_id':observation['external_id']})
    assert not any(op in ('upload','prepare','submit') for op,_ in browser.calls)


def test_unknown_lookup_is_finitely_retried_without_touching_parent_publication(known):
    f,parent,child,now=start(known);public=f['db'].query('SELECT * FROM publications WHERE job_id=?',(parent,))
    browser=OwnedBrowser(error=HubError('REPLAY_BROWSER_RESPONSE_UNKNOWN',review=True))
    handler=m.ModerationLookupHandler(f['s'],f['db'],browser=browser)
    worker=Worker(f['s'],m.QueueView(f['db'],lookups=True),{m.LOOKUP_JOB_TYPE:handler.process})
    for number in range(4):
        with f['db'].transaction() as db:db.execute('UPDATE jobs SET next_run=0 WHERE job_id=?',(child,))
        assert worker.once('moderation')
        assert f['db'].get(child)['state']==('failed' if number==3 else 'retry_scheduled')
    assert f['db'].get(child)['attempts']==4 and f['db'].get(parent)['attempts']==9
    assert f['db'].query('SELECT * FROM publications WHERE job_id=?',(parent,))==public
    assert not any(op=='submit' for op,_ in browser.calls)


def test_known_bv_after_restart_publish_unknown_gets_only_independent_lookup(known):
    f,parent=known
    with f['db'].transaction() as db:db.execute("UPDATE jobs SET state='publish_unknown' WHERE job_id=?",(parent,))
    f,parent,child,_=start((f,parent))
    assert f['db'].get(parent)['state']=='publish_unknown'
    assert f['db'].get(child)['payload']['external_id']=='BV1111111111'


def test_lookup_side_heartbeat_does_not_mask_main_video_consumer(known):
    f,_,_,_=start(known);store=f['db']
    m.QueueView(store,lookups=True).beat('worker:video')
    names={row['name'] for row in store.query('SELECT * FROM heartbeats')}
    assert names=={'worker:video-moderation-v3'}
    m.QueueView(store,lookups=False).beat('worker:video')
    names={row['name'] for row in store.query('SELECT * FROM heartbeats')}
    assert names=={'worker:video','worker:video-main-v3','worker:video-moderation-v3'}


def test_dead_side_thread_invokes_process_restart_instead_of_false_health():
    calls=[]
    def once(queue):raise RuntimeError('Owned failure; text must not be logged')
    moderation_loop(SimpleNamespace(once=once),fatal=lambda code:calls.append(code))
    assert calls==[70]


def test_child_lease_or_different_bv_cannot_mutate_parent(known):
    f,parent,child,_=start(known);service=m.ModerationService(f['s'],f['db']);before=f['db'].get(parent)
    claimed=m.QueueView(f['db'],lookups=True).claim('moderation',60,4)
    wrong=deepcopy(before['result'][STATE]['result_observation']);wrong['external_id']='BV2222222222';wrong['url']='https://www.bilibili.com/video/BV2222222222'
    with pytest.raises(HubError):service.record_lookup(claimed,wrong)
    with f['db'].transaction() as db:db.execute('UPDATE jobs SET lease_until=0 WHERE job_id=?',(child,))
    with pytest.raises(HubError,match='LEASE_LOST'):service.record_lookup(claimed,before['result'][STATE]['result_observation'])
    assert f['db'].get(parent)==before
