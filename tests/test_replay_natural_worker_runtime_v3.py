"""Owned ordinary-queue integration; no live grant, network or browser."""
from copy import deepcopy
from types import SimpleNamespace
import json,time,uuid
import pytest
from test_short_video_web import short_video
from test_replay_highest_video_v2 import highest,UID
from test_replay_natural_video_v3 import natural
from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.youtube_monitor import YouTubeMonitor
from services.hub.replay_natural_video_v3 import JOB_TYPE,POLICY,STATE,KIND,NaturalReplayVideoWeb
from services.hub import replay_natural_worker_runtime_v3 as rt


def test_completed_verified_download_enters_normal_queue_once_without_individual_review(natural):
    f=natural
    one=rt.produce_publication_tick(f['s'],f['db'],f['service'])
    assert len(one['queued_publication_jobs'])==1
    job=f['db'].get(one['queued_publication_jobs'][0])
    assert job['job_type']==JOB_TYPE and job['state']=='pending'
    assert job['result'][STATE]['human_review'] is False and 'owner_acceptance' not in job['result'][STATE]
    assert rt.produce_publication_tick(f['s'],f['db'],f['service'])['queued_publication_jobs']==[]
    assert not f['db'].query('SELECT * FROM api_calls')


def test_fresh_official_observer_rows_drive_one_bounded_metadata_job_for_other_channel(natural):
    f=natural;raw=deepcopy(f['raw']);raw['id']='ownedNew002';raw['snippet']['channelId']='UCbbbbbbbbbbbbbbbbbbbbbb'
    YouTubeMonitor(f['s'],f['db'],None).observe_video(raw,raw['snippet']['channelId']);calls=[]
    def prepare(payload):
        calls.append(deepcopy(payload))
        return f['db'].enqueue('youtube',payload['video_id'],'youtube_highest_prepare_v3','video',payload,'youtube-highest-natural-download-v3')
    downloader=SimpleNamespace(policy=lambda:{},enqueue_if_eligible=prepare)
    one=rt.produce_download_tick(f['s'],f['db'],downloader)
    assert len(one['created_metadata_prepare_jobs'])==1 and len(calls)==1
    assert calls[0]['raw']==raw and calls[0]['source_api']=='youtube-data-v3-official'
    assert one['media_downloads_started']==one['publications']==0
    assert rt.produce_download_tick(f['s'],f['db'],downloader)['created_metadata_prepare_jobs']==[]
    assert len(calls)==1 and not f['db'].query('SELECT * FROM api_calls')


@pytest.fixture
def known(natural):
    f=natural;ident=f['service'].queue_verified_download(f['source']);current=f['db'].get(ident);binding=current['payload'];state=current['result'][STATE]
    attempt=str(uuid.uuid4());bv='BV1111111111'
    state.update(state='awaiting_moderation',attempt_id=attempt,result_observation={'version':'replay-natural-browser-result-v3',
        'attempt_id':attempt,'site_uid':UID,'source_url':binding['source_url'],'metadata_sha256':binding['metadata_sha256'],
        'external_id':bv,'url':'https://www.bilibili.com/video/'+bv,'state':'awaiting_moderation'})
    with f['db'].transaction() as con:
        con.execute("UPDATE jobs SET state='awaiting_moderation',attempts=1,updated_at=1000,result=?,external_publish_id=? WHERE job_id=?",
            (encode(current['result']),bv,ident))
        con.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',(state['key'],ident,'bilibili','youtube',current['source_id'],
            'site_uid:'+UID,KIND,'awaiting_moderation',bv,'https://www.bilibili.com/video/'+bv,1000))
        con.execute("INSERT INTO audit(at,principal,action,target,outcome) VALUES(1000,'Owned','short_video_reserve_submit',?,'one_artifact_one_click_reserved')",(ident,))
    return f,ident


def test_normal_known_bv_tick_uses_its_kind_and_preserves_attempt_and_publication(known):
    f,job=known;before=f['db'].get(job);public=f['db'].query('SELECT * FROM publications')
    f['file'].unlink()
    result=rt.reconcile_tick(f['s'],f['db'],f['service'],now=1401)
    assert len(result['scheduled_lookup_jobs'])==1
    child=f['db'].get(result['scheduled_lookup_jobs'][0]);assert child['payload']['parent_job_id']==job and child['queue']=='video'
    after=f['db'].get(job);assert after['state']=='awaiting_moderation' and after['attempts']==before['attempts']
    assert after['payload']==before['payload'] and after['result'][STATE]['moderation']['polls_total']==1
    assert f['db'].query('SELECT * FROM publications')==public
    assert result['submissions']==0


@pytest.mark.parametrize('change',['manual_block','no_bv'])
def test_normal_unknown_or_outside_bounds_does_not_requeue_or_resend(known,change):
    f,job=known
    with f['db'].transaction() as con:
        if change=='manual_block':con.execute("UPDATE jobs SET state='awaiting_review' WHERE job_id=?",(job,))
        elif change=='no_bv':con.execute('UPDATE jobs SET external_publish_id=NULL WHERE job_id=?',(job,))
    before=f['db'].get(job)
    assert rt.reconcile_tick(f['s'],f['db'],f['service'],now=1401)['scheduled_lookup_jobs']==[]
    assert f['db'].get(job)==before


def test_new_runtime_keeps_old_handlers_and_registers_prepare_download_and_publication(natural,monkeypatch):
    from services.hub import cli,replay_video_browser,youtube_download_v3
    f=natural;legacy=lambda job:'Owned legacy'
    monkeypatch.setattr(cli,'runtime',lambda *args:{'owned_legacy':legacy,'youtube_download':legacy})
    monkeypatch.setattr(NaturalReplayVideoWeb,'_root_readonly_file',lambda self,file,**kwargs:f['root_reader'](file,**kwargs))
    monkeypatch.setattr(youtube_download_v3.YouTubeNaturalPrepare,'policy',lambda self:{'activated_at':f['policy']['activated_at']})
    monkeypatch.setattr(replay_video_browser,'ReplayVideoWorkerHandler',lambda *args,**kwargs:SimpleNamespace(process=lambda job:'Owned new handler'))
    worker,service,downloader=rt.build_worker(f['s'],f['db'])
    assert worker.handlers['youtube_download'] is worker.handlers['owned_legacy'] is legacy
    assert {'replay_video_publish','replay_highest_video_publish_v2','youtube_download_v2','youtube_highest_prepare_v3','youtube_download_v3',JOB_TYPE}<=set(worker.handlers)
    assert 'youtube_highest_readiness_observation_v3' in worker.handlers
    assert set(worker.readiness_deferrals)=={'youtube_highest_prepare_v3'}
    job=service.queue_verified_download(f['source'])
    assert worker.handlers[JOB_TYPE](f['db'].get(job))=='Owned new handler'
    changed=f['db'].get(job);changed['payload']['channel_id']='not-an-allowed-channel'
    with pytest.raises(HubError,match='RUNTIME_SCOPE'):worker.handlers[JOB_TYPE](changed)


def test_loop_runs_bounded_producers_then_ordinary_worker_with_no_chat(natural,monkeypatch):
    f=natural;calls=[]
    for name in ('produce_download_tick','produce_publication_tick','reconcile_tick'):
        monkeypatch.setattr(rt,name,lambda *args,_name=name,**kwargs:calls.append(_name))
    worker=SimpleNamespace(once=lambda queue:calls.append(queue) or False,
        resource_waits=SimpleNamespace(tick=lambda:calls.append('resource_tick')),
        readiness_waits=SimpleNamespace(tick=lambda:calls.append('readiness_tick')))
    assert rt.run_loop(f['s'],f['db'],worker,f['service'],None,once=True,clock=lambda:0,sleep=lambda _:pytest.fail('once sleeps')) is False
    assert calls==['produce_download_tick','produce_publication_tick','reconcile_tick','resource_tick','readiness_tick','video']


@pytest.mark.parametrize('native_intent',[None,'replay-native-intents/submit.json','recovered-submit-intent.json'])
def test_runtime_maps_prepared_disk_low_to_durable_wait_not_manual_review(natural,monkeypatch,native_intent):
    from services.hub import cli,replay_video_browser,youtube_download_v3
    from services.hub.maintenance import Maintenance
    from services.hub.errors import ResourceDeferred,PublishUnknown
    f=natural
    monkeypatch.setattr(cli,'runtime',lambda *args:{})
    monkeypatch.setattr(NaturalReplayVideoWeb,'_root_readonly_file',lambda self,file,**kwargs:f['root_reader'](file,**kwargs))
    monkeypatch.setattr(youtube_download_v3.YouTubeNaturalPrepare,'policy',lambda self:{'activated_at':f['policy']['activated_at']})
    monkeypatch.setattr(replay_video_browser,'ReplayVideoWorkerHandler',lambda *args,**kwargs:SimpleNamespace(process=lambda job:'Owned lookup path'))
    def disk(*args):raise HubError('DISK_LOW',review=True)
    monkeypatch.setattr(Maintenance,'check_disk',disk)
    worker,service,_=rt.build_worker(f['s'],f['db']);ident=service.queue_verified_download(f['source'])
    with f['db'].transaction() as db:db.execute('UPDATE jobs SET created_at=-100 WHERE job_id=?',(ident,))
    job=worker.store.claim('video',60,4)
    if native_intent:
        file=f['s'].root/'media'/ident/native_intent;file.parent.mkdir(parents=True,exist_ok=True);file.write_text('Owned native intent')
        with pytest.raises(PublishUnknown):worker.handlers[JOB_TYPE](job)
        assert f['db'].get(ident)['state']=='processing' and 'resource_wait_v1' not in f['db'].get(ident)['result']
        return
    with pytest.raises(ResourceDeferred) as error:worker.handlers[JOB_TYPE](job)
    worker.resource_waits.defer(job,error.value)
    waiting=f['db'].get(ident)
    assert waiting['state']=='retry_scheduled' and waiting['stage']=='waiting_resource' and waiting['attempts']==1
    assert waiting['result']['resource_wait_v1']['state']=='waiting_resource'
    assert set(worker.resource_waits.validators)=={'youtube_highest_prepare_v3','youtube_download_v3',JOB_TYPE}
    # Disk checks cannot prevent an existing publication from being queried.
    job['result'][STATE]['state']='submit_unknown'
    assert worker.handlers[JOB_TYPE](job)=='Owned lookup path'


def test_late_native_disk_error_cannot_be_converted_into_resource_resume(natural,monkeypatch):
    from services.hub import cli,replay_video_browser,youtube_download_v3
    from services.hub.maintenance import Maintenance
    from services.hub.errors import ResourceDeferred
    f=natural
    monkeypatch.setattr(cli,'runtime',lambda *args:{})
    monkeypatch.setattr(NaturalReplayVideoWeb,'_root_readonly_file',lambda self,file,**kwargs:f['root_reader'](file,**kwargs))
    monkeypatch.setattr(youtube_download_v3.YouTubeNaturalPrepare,'policy',lambda self:{'activated_at':f['policy']['activated_at']})
    def later_native(job):raise HubError('DISK_LOW',review=True)
    monkeypatch.setattr(replay_video_browser,'ReplayVideoWorkerHandler',lambda *args,**kwargs:SimpleNamespace(process=later_native))
    monkeypatch.setattr(Maintenance,'check_disk',lambda *args:None)
    worker,service,_=rt.build_worker(f['s'],f['db']);ident=service.queue_verified_download(f['source'])
    with pytest.raises(HubError) as error:worker.handlers[JOB_TYPE](f['db'].get(ident))
    assert error.value.code=='DISK_LOW' and not isinstance(error.value,ResourceDeferred)
    assert 'resource_wait_v1' not in f['db'].get(ident)['result']
