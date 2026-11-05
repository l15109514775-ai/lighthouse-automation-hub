"""Owned official-row/inventory fixtures, real SQLite and ordinary Worker.

No live API, media download, native upload or publication acceptance is
claimed. Publication enqueue doubles stand at the real machine-service gate.
"""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import json
import time
import pytest

from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.worker import Worker
from services.hub.replay_fifo_v4 import ReplayFIFOV4,DEFAULT_POLICY,VERSION,POLICY_FILE,PLAN_JOB,DOWNLOAD_JOB,REFRESH_JOB,PUBLISH_JOB,UID,sha
from services.hub.replay_multipart_v4 import DEFAULT_POLICY as WHOLE_POLICY,plan_whole_replay
from test_youtube_natural_plan_v3 import fixture as inventory_fixture


@pytest.fixture
def fifo(runtime):
    s,db=runtime;s.config['target_bilibili_site_uid']=UID;clock=[time.time()]
    whole={**deepcopy(WHOLE_POLICY),'enabled':True};p={**deepcopy(DEFAULT_POLICY),'enabled':True,
        'activated_at':datetime.fromtimestamp(clock[0]-10,timezone.utc).isoformat(),
        'owner_queue_reference':'Owned queue instruction fixture, not real authorization','planning_policy_sha256':sha(whole)}
    file=s.root/'config'/POLICY_FILE;file.parent.mkdir(parents=True,exist_ok=True);file.write_text(encode(p),encoding='utf-8');file.chmod(0o644)
    value=SimpleNamespace(s=s,db=db,clock=clock,whole=whole,p=p,q=ReplayFIFOV4(s,db,clock=lambda:clock[0]))
    value.handlers={k:(lambda job:None) for k in (PLAN_JOB,DOWNLOAD_JOB,REFRESH_JOB)}
    return value


def seed(f,index,*,stale=False,started=None,hours=1):
    raw,info=inventory_fixture();raw['id']=f'fifo{index:07d}';raw['snippet']['title']='Owned replay '+str(index)
    start=started or datetime(2026,11,1,tzinfo=timezone.utc)+timedelta(minutes=index*2)
    end=start+timedelta(hours=hours);raw['liveStreamingDetails']={'actualStartTime':start.isoformat(),'actualEndTime':end.isoformat()}
    raw['contentDetails']['duration']='PT'+str(hours)+'H';info.update(id=raw['id'],duration=hours*3600)
    if hours>10:
        info['formats'][1]['filesize']=30_000_000_000;info['formats'][2]['filesize']=29_000_000_000
    observed=f.clock[0]-3600 if stale else f.clock[0]
    with f.db.transaction() as db:
        db.execute("""INSERT INTO youtube_videos(video_id,channel_id,title,published_at,url,state,actual_start,actual_end,duration,
            first_seen_at,last_seen_at,last_api_check,source_api,metadata) VALUES(?,?,?,?,?,'ended',?,?,?,?,?,?,'youtube-data-v3-official',?)""",
            (raw['id'],raw['snippet']['channelId'],raw['snippet']['title'],raw['snippet']['publishedAt'],
            'https://www.youtube.com/watch?v='+raw['id'],start.isoformat(),end.isoformat(),raw['contentDetails']['duration'],
            observed,observed,observed,encode(raw)))
    return raw,info


def plan_one(f,infos):
    queued=f.q.enqueue_plan_tick(f.handlers);assert len(queued['created'])==1
    def process(job):
        raw=job['payload']['raw'];info=infos[raw['id']]
        plan=plan_whole_replay(raw,info,observed_at=datetime.fromtimestamp(f.clock[0]-1,timezone.utc).isoformat(),policy=f.whole,now=f.clock[0])
        return f.q.record_plan(job,plan,f.whole)
    worker=Worker(f.s,f.db,{PLAN_JOB:process})
    assert worker.once('video') is True
    job=f.db.get(queued['created'][0]);assert job['state']=='completed' and job['attempts']==1
    return job


def test_more_than_twenty_history_and_more_than_daily_four_are_all_registered(fifo):
    f=fifo
    for index in range(43):seed(f,index,stale=True)
    before=f.db.query('SELECT video_id,actual_start,actual_end,published_at,metadata FROM youtube_videos ORDER BY video_id')
    result=f.q.register_all_observed(page_size=3)
    assert len(result['registered'])==43 and not result['held'] and result['daily_slots_spent']==0
    assert len(f.db.query('SELECT * FROM replay_fifo_v4'))==43
    assert f.q.register_all_observed(page_size=2)['registered']==[]
    assert f.db.query('SELECT video_id,actual_start,actual_end,published_at,metadata FROM youtube_videos ORDER BY video_id')==before
    assert not f.db.query('SELECT * FROM jobs') and not f.db.query('SELECT * FROM api_calls') and not f.db.query('SELECT * FROM publications')


def test_november_backlog_before_formal_activation_is_retained_but_october_is_excluded(fifo):
    f=fifo;old,_=seed(f,1,started=datetime(2026,10,31,15,59,59,tzinfo=timezone.utc));new,_=seed(f,2,started=datetime(2026,10,31,16,tzinfo=timezone.utc))
    result=f.q.register_all_observed()
    assert len(result['registered'])==len(result['excluded_or_existing'])==1
    rows={r['source_id']:r for r in f.db.query('SELECT * FROM replay_fifo_v4')}
    assert rows[old['id']]['state']=='excluded' and rows[new['id']]['state']=='registered'
    assert rows[new['id']]['actual_end']<datetime.fromisoformat(f.p['activated_at']).timestamp()
    assert json.loads(rows[new['id']]['source_payload'])['raw']==new


def test_twenty_prepared_or_published_sources_do_not_hide_the_next_new_source(fifo):
    f=fifo
    for i in range(25):
        raw,_=seed(f,i)
        f.db.enqueue('youtube',raw['id'],'youtube_highest_prepare_v3','video',{'owned_only':True},'Owned-old-v3')
    raw,_=seed(f,100)
    f.q.register_all_observed(page_size=2)
    result=f.q.enqueue_plan_tick(f.handlers)
    assert len(result['created'])==1 and f.db.get(result['created'][0])['source_id']==raw['id']
    assert len(f.db.query("SELECT * FROM replay_fifo_v4 WHERE state='existing'"))==25


def test_actual_end_fifo_is_independent_of_id_observation_and_registration_order(fifo):
    f=fifo;infos={}
    for index,start in ((8,3),(1,5),(6,1)):
        raw,info=seed(f,index,started=datetime(2026,11,1,start,tzinfo=timezone.utc));infos[raw['id']]=info
    f.q.register_all_observed(page_size=1)
    order=[plan_one(f,infos)['source_id'] for _ in range(3)]
    assert order==['fifo0000006','fifo0000008','fifo0000001']
    assert not f.db.query('SELECT * FROM replay_fifo_dispatches_v4')


def test_daily_four_only_throttles_media_dispatch_and_backlog_survives_next_day(fifo):
    f=fifo;infos={}
    for i in range(6):raw,info=seed(f,i);infos[raw['id']]=info
    f.q.register_all_observed()
    for _ in range(6):plan_one(f,infos)
    sent=[f.q.enqueue_download_tick(f.handlers,f.whole,free_bytes=10**15) for _ in range(4)]
    assert all(len(r['created'])==1 for r in sent)
    assert f.q.enqueue_download_tick(f.handlers,f.whole,free_bytes=10**15)['state']=='daily_dispatch_limit'
    assert len(f.db.query('SELECT * FROM replay_fifo_v4'))==6 and len(f.db.query('SELECT * FROM replay_fifo_dispatches_v4'))==4
    f.clock[0]+=86400
    with f.db.transaction() as db:db.execute('UPDATE youtube_videos SET last_api_check=?',(f.clock[0],))
    later=f.q.enqueue_download_tick(f.handlers,f.whole,free_bytes=10**15)
    assert len(later['created'])==1 and f.db.get(later['created'][0])['source_id']=='fifo0000004'
    assert len(f.db.query('SELECT * FROM replay_fifo_dispatches_v4'))==5


@pytest.mark.parametrize('other_state',['unknown','awaiting_moderation','hold'])
def test_another_sources_unknown_moderation_or_hold_does_not_block_new_download(fifo,other_state):
    f=fifo;infos={}
    for i in (0,1):raw,info=seed(f,i);infos[raw['id']]=info
    f.q.register_all_observed();plan_one(f,infos);plan_one(f,infos)
    first=f.db.query('SELECT * FROM replay_fifo_v4 ORDER BY actual_end')[0]
    with f.db.transaction() as db:
        db.execute('UPDATE replay_fifo_v4 SET state=? WHERE candidate_id=?',(other_state,first['candidate_id']))
        if other_state=='unknown':
            db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',('Owned-unknown',first['plan_job_id'],'bilibili','youtube',first['source_id'],
                'site_uid:'+UID,'Owned-unknown','unknown',None,None,f.clock[0]))
    result=f.q.enqueue_download_tick(f.handlers,f.whole,free_bytes=10**15)
    assert len(result['created'])==1 and f.db.get(result['created'][0])['source_id']=='fifo0000001'
    assert f.q.candidate(first['candidate_id'])['state']==other_state


def test_large_oldest_capacity_wait_is_explicit_and_next_fitting_replay_dispatches(fifo):
    f=fifo;raw,large=seed(f,0,hours=11);small_raw,small=seed(f,1,started=datetime(2026,11,1,12,tzinfo=timezone.utc))
    infos={raw['id']:large,small_raw['id']:small};f.q.register_all_observed();plan_one(f,infos);plan_one(f,infos)
    result=f.q.enqueue_download_tick(f.handlers,f.whole,free_bytes=30_000_000_000)
    assert len(result['created'])==1 and f.db.get(result['created'][0])['source_id']==small_raw['id']
    assert result['deferred'][0]['reason']=='INSUFFICIENT_PEAK_DISK'
    first=f.db.query('SELECT * FROM replay_fifo_v4 ORDER BY actual_end')[0]
    assert first['state']=='waiting_resource' and first['download_job_id'] is None
    hold=f.db.query('SELECT * FROM replay_fifo_capacity_v4')[0]
    assert hold['required_free_bytes']>64_000_000_000 and len(f.db.query('SELECT * FROM replay_fifo_dispatches_v4'))==1


def test_stale_oldest_queues_budgeted_refresh_and_does_not_starve_fresh_next_source(fifo):
    f=fifo;old,_=seed(f,0,stale=True);new,_=seed(f,1);f.q.register_all_observed()
    one=f.q.enqueue_plan_tick(f.handlers);assert one['state']=='waiting_source' and one['refresh_job_id']
    two=f.q.enqueue_plan_tick(f.handlers);assert f.db.get(two['created'][0])['source_id']==new['id']
    assert f.db.get(one['refresh_job_id'])['source_platform']=='maintenance'
    assert not f.db.query('SELECT * FROM replay_fifo_dispatches_v4')


def test_real_worker_refresh_uses_one_official_request_keeps_original_source_and_makes_plan_possible(fifo,monkeypatch):
    f=fifo;raw,_=seed(f,0,stale=True);f.q.register_all_observed();c=f.db.query('SELECT * FROM replay_fifo_v4')[0]
    original=c['source_payload'];result=f.q.enqueue_plan_tick(f.handlers);calls=[]
    monkeypatch.setenv('YOUTUBE_API_KEY','OwnedSyntheticNotLive')
    http=SimpleNamespace(request=lambda *args,**kwargs:(calls.append((args,kwargs)) or {'items':[raw]}))
    worker=Worker(f.s,f.db,{REFRESH_JOB:lambda job:f.q.process_refresh(job,http)})
    assert worker.once('video') is True and f.db.get(result['refresh_job_id'])['state']=='completed'
    assert len(calls)==1 and calls[0][1]['params']['id']==raw['id'] and calls[0][0][2]=='https://www.googleapis.com/youtube/v3/videos'
    assert f.db.query('SELECT * FROM replay_fifo_v4')[0]['source_payload']==original
    assert len(f.q.enqueue_plan_tick(f.handlers)['created'])==1


def test_publication_excludes_twenty_old_completed_before_paging_and_recovers_link_after_crash(fifo):
    f=fifo;ids=[]
    for i in range(24):
        raw,_=seed(f,i);ids.append(raw['id'])
    f.q.register_all_observed()
    with f.db.transaction() as db:
        for i,c in enumerate(db.execute('SELECT * FROM replay_fifo_v4 ORDER BY actual_end').fetchall()):
            job=f'Owned-download-{i}'
            db.execute("INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,policy_version,payload) VALUES(?,'youtube',?,?,'video','completed',?,?,?,'{}')",
                (job,c['source_id'],DOWNLOAD_JOB,f.clock[0],f.clock[0],VERSION))
            db.execute("UPDATE replay_fifo_v4 SET state='download_completed',download_job_id=? WHERE candidate_id=?",(job,c['candidate_id']))
            if i<23:db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',(f'Owned-old-{i}',job,'bilibili','youtube',c['source_id'],'site_uid:'+UID,'Owned-old','completed','BV0000000000','https://www.bilibili.com/video/BV0000000000',f.clock[0]))
    calls=[]
    def queued(download):
        source=f.db.get(download)['source_id'];calls.append(source)
        return f.db.enqueue('youtube',source,PUBLISH_JOB,'video',{'owned_actual_service_boundary':True},'Owned-multipart-policy')
    result=f.q.publication_tick(SimpleNamespace(JOB_TYPE=PUBLISH_JOB,queue_verified_download=queued))
    assert calls==[ids[-1]] and len(result['created'])==1 and result['actual_publications']==0
    with f.db.transaction() as db:db.execute('UPDATE replay_fifo_v4 SET publication_job_id=NULL WHERE source_id=?',(ids[-1],))
    f.q.sync_states();assert f.db.query('SELECT publication_job_id FROM replay_fifo_v4 WHERE source_id=?',(ids[-1],))[0]['publication_job_id']==result['created'][0]
    assert f.q.publication_tick(SimpleNamespace(JOB_TYPE=PUBLISH_JOB,queue_verified_download=queued))['created']==[]


def test_resource_timeout_is_durable_hold_and_same_job_can_resume_without_resetting_attempts(fifo):
    f=fifo;raw,info=seed(f,0,hours=11);f.q.register_all_observed();plan_one(f,{raw['id']:info})
    result=f.q.enqueue_download_tick(f.handlers,f.whole,free_bytes=10**15);job=f.db.claim('video',600,4)
    assert job['job_id']==result['created'][0];original=deepcopy(job);binding=sha(job['payload'])
    partial=f.s.root/'media'/job['job_id']/'Owned-partial';partial.parent.mkdir(parents=True);partial.write_bytes(b'Owned partial retained')
    wait=f.q.resource_defer(job,required_free_bytes=100_000_000_000,binding_sha256=binding)
    assert f.db.get(job['job_id'])['attempts']==1 and wait['state']=='waiting_resource'
    f.clock[0]+=f.p['resource_wait_max_seconds']+1
    tick=f.q.resource_tick({DOWNLOAD_JOB:lambda j:sha(j['payload'])},free_bytes=0)
    assert tick['held']==[job['job_id']] and tick['external_requests']==0
    held=f.db.get(job['job_id']);assert held['state']=='upload_pending' and held['attempts']==1 and partial.read_bytes()==b'Owned partial retained'
    f.q.resume_resource_hold(job['job_id'],{DOWNLOAD_JOB:lambda j:sha(j['payload'])},maintenance_reference='Owned resource available',free_bytes=10**15)
    resumed=f.db.get(job['job_id']);record=f.db.query('SELECT * FROM replay_fifo_resource_holds_v4')[0]
    assert resumed['job_id']==original['job_id'] and resumed['payload']==original['payload'] and resumed['attempts']==1
    assert record['deadline']==wait['deadline'] and record['first_wait_at']==wait['first_wait_at'] and record['polls']==1
    assert not f.db.query('SELECT * FROM publications')


def test_default_disabled_and_missing_new_handlers_never_fall_back_to_old_v3(fifo):
    f=fifo;seed(f,0);f.q.register_all_observed()
    assert f.q.enqueue_plan_tick({})['state']=='waiting_handler'
    assert f.q.enqueue_download_tick({},f.whole)['state']=='waiting_handler'
    assert not f.db.query('SELECT * FROM jobs')
    file=f.s.root/'config'/POLICY_FILE;file.unlink()
    with pytest.raises(HubError,match='REPLAY_FIFO_V4_DISABLED'):f.q.register_all_observed()


@pytest.mark.parametrize('change',['private','wrong_channel','missing_actual_start','row_raw_mismatch'])
def test_unverified_source_has_a_durable_hold_and_no_new_job_or_day_slot(fifo,change):
    f=fifo;raw,_=seed(f,0)
    if change=='private':raw['status']['privacyStatus']='private'
    elif change=='wrong_channel':raw['snippet']['channelId']='UC'+'x'*22
    elif change=='missing_actual_start':raw['liveStreamingDetails'].pop('actualStartTime')
    else:raw['contentDetails']['duration']='PT2H'
    with f.db.transaction() as db:db.execute('UPDATE youtube_videos SET metadata=?',(encode(raw),))
    result=f.q.register_all_observed();assert len(result['held'])==1
    assert f.q.candidate(result['held'][0])['state']=='hold'
    assert not f.q.enqueue_plan_tick(f.handlers)['created']
    assert not f.db.query('SELECT * FROM jobs') and not f.db.query('SELECT * FROM replay_fifo_dispatches_v4')


@pytest.mark.parametrize('change',['expired_lease','changed_input','passed_bool'])
def test_record_plan_requires_actual_lease_raw_and_validated_whole_contract(fifo,change):
    f=fifo;raw,info=seed(f,0);f.q.register_all_observed();f.q.enqueue_plan_tick(f.handlers)
    job=f.db.claim('video',600,4)
    plan=plan_whole_replay(raw,info,observed_at=datetime.fromtimestamp(f.clock[0]-1,timezone.utc).isoformat(),policy=f.whole,now=f.clock[0])
    if change=='expired_lease':
        with f.db.transaction() as db:db.execute('UPDATE jobs SET lease_until=? WHERE job_id=?',(f.clock[0]-1,job['job_id']))
    elif change=='changed_input':job['payload']['raw']['snippet']['title']='Unbound title'
    else:plan['passed']=True
    with pytest.raises(HubError):f.q.record_plan(job,plan,f.whole)
    assert 'replay_fifo_plan_v4' not in f.db.get(job['job_id'])['result']
    assert not f.db.query('SELECT * FROM replay_fifo_dispatches_v4')


def test_expired_capacity_wait_remains_recoverable_without_losing_original_plan(fifo):
    f=fifo;raw,info=seed(f,0,hours=11);f.q.register_all_observed();prior=plan_one(f,{raw['id']:info})
    f.q.enqueue_download_tick(f.handlers,f.whole,free_bytes=20_000_000_000)
    c=f.db.query('SELECT * FROM replay_fifo_v4')[0];wait=f.db.query('SELECT * FROM replay_fifo_capacity_v4')[0]
    f.clock[0]+=f.p['resource_wait_max_seconds']+1
    result=f.q.capacity_tick(f.whole,free_bytes=10**15)
    assert result['held']==[c['candidate_id']] and not result['ready']
    assert f.q.candidate(c['candidate_id'])['reason']=='CAPACITY_WAIT_TIMEOUT'
    f.q.resume_capacity_hold(c['candidate_id'],f.whole,maintenance_reference='Owned storage change',free_bytes=10**15)
    after=f.db.query('SELECT * FROM replay_fifo_capacity_v4')[0]
    assert after['deadline']==wait['deadline'] and after['first_wait_at']==wait['first_wait_at'] and after['polls']==1
    assert f.db.get(prior['job_id'])==prior and not f.db.query('SELECT * FROM replay_fifo_dispatches_v4')


def test_same_source_unknown_publication_prevents_new_dispatch_and_hold_resume(fifo):
    f=fifo;raw,info=seed(f,0);f.q.register_all_observed();prior=plan_one(f,{raw['id']:info})
    with f.db.transaction() as db:db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        ('Owned-same-source',prior['job_id'],'bilibili','youtube',raw['id'],'site_uid:'+UID,'Owned-existing','unknown',None,None,f.clock[0]))
    before=f.db.query('SELECT * FROM publications')
    assert not f.q.enqueue_download_tick(f.handlers,f.whole,free_bytes=10**15)['created']
    assert f.db.query('SELECT * FROM publications')==before and f.db.get(prior['job_id'])==prior
    assert not f.db.query('SELECT * FROM replay_fifo_dispatches_v4')


def test_long_running_lease_check_preserves_identity_without_forging_source_refresh(fifo):
    f=fifo;raw,_=seed(f,0);f.q.register_all_observed();f.q.enqueue_plan_tick(f.handlers)
    job=f.db.claim('video',3600,4);f.clock[0]+=301
    before=f.db.query('SELECT * FROM youtube_videos')
    with pytest.raises(HubError,match='FRESH_SOURCE_REQUIRED'):f.q.assert_job_binding(job)
    assert f.q.assert_job_binding(job,require_fresh=False)['payload_sha256']==sha(job['payload'])
    assert f.db.query('SELECT * FROM youtube_videos')==before
    changed=deepcopy(raw);changed['liveStreamingDetails']['actualEndTime']='2026-11-01T01:01:00Z'
    with f.db.transaction() as db:db.execute('UPDATE youtube_videos SET metadata=?,actual_end=?',(encode(changed),changed['liveStreamingDetails']['actualEndTime']))
    with pytest.raises(HubError,match='SOURCE_IDENTITY_CHANGED'):f.q.assert_job_binding(job,require_fresh=False)
