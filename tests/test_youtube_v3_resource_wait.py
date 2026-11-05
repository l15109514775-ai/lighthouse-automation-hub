"""Owned11GB plan and real Worker/SQLite resource transitions, no media HTTP."""
from types import SimpleNamespace
from datetime import datetime,timezone
import json,time
import pytest

from adapters.ytdlp_natural_v3 import natural_budget_limits
from services.hub.budget import Budget
from services.hub.db import encode
from services.hub.errors import ResourceDeferred
from services.hub.worker import Worker
from services.hub.youtube_download_v3 import (YouTubeNaturalPrepare,YouTubeNaturalDownload,resource_wait_validators,
    resource_coordinator,PREPARE_JOB_TYPE,PREPARE_STATE,JOB_TYPE)
from services.hub.resource_wait_v1 import ResourceWaitCoordinator,INTERVAL
from services.hub.youtube_natural_plan_v3 import natural_source_plan
from services.hub.youtube_download import digest
from services.hub.youtube_download_v3 import VERSION
from test_youtube_download_v3 import natural,owned_planner,prepare


def test_captured_metadata_plan_waits_on35gb_then_resumes_with_no_repeated_metadata(natural,monkeypatch):
    f=natural;clock=SimpleNamespace(value=time.time());volume=SimpleNamespace(free=35_000_000_000)
    monkeypatch.setattr('services.hub.db.time.time',lambda:clock.value)
    monkeypatch.setattr('services.hub.maintenance.shutil.disk_usage',lambda _:SimpleNamespace(free=volume.free))
    f.info['formats'][1]['filesize']=10_505_641_986;f.info['formats'][-1]['filesize']=416_163_003
    calls=[];base=owned_planner(f)
    class CountedMetadata(base):
        def acquire(self,job,binding,*,check):
            check();calls.append(job['job_id']);observed=datetime.fromtimestamp(clock.value-1,timezone.utc).isoformat().replace('+00:00','Z')
            plan=natural_source_plan(binding['official_raw'],f.info,activated_at=binding['activated_at'],observed_at=observed,now=clock.value)
            result={'version':VERSION,'binding_sha256':digest(binding),'source_plan':plan,
                'media_downloaded':False,'human_review':False,'publication_authorized':False}
            budget=Budget(f.db,natural_budget_limits(binding));key=budget.reserve('youtube_download_http',job['job_id']);budget.finish(key,'done')
            result['http']={'requests':1,'owned_metadata_response_only':True};return result
    service=YouTubeNaturalPrepare(f.s,f.db,downloader_factory=CountedMetadata)
    coordinator=ResourceWaitCoordinator(f.s,f.db,validators=resource_wait_validators(f.s,f.db),clock=lambda:clock.value,disk_free=lambda:volume.free)
    ident=service.enqueue_if_eligible(f.payload)
    worker=Worker(f.s,f.db,{PREPARE_JOB_TYPE:service.process},resource_deferrals=coordinator.callbacks())
    assert worker.once('video') is True
    waiting=f.db.get(ident);saved=waiting['result'][PREPARE_STATE];original_payload=waiting['payload']
    assert waiting['state']=='retry_scheduled' and waiting['error_code']=='WAITING_RESOURCE' and waiting['attempts']==1
    assert saved['state']=='metadata_verified' and saved['source_plan']['required_working_bytes']==21_939_609_978
    assert not f.db.query('SELECT * FROM jobs WHERE job_type=?',(JOB_TYPE,))
    ledger=f.db.query('SELECT * FROM api_calls');assert len(ledger)==1
    # Simulate a new coordinator process. Ten five-minute checks never claim,
    # spend metadata, alter the old receipt or exhaust the four Worker attempts.
    restarted=ResourceWaitCoordinator(f.s,f.db,validators=resource_wait_validators(f.s,f.db),clock=lambda:clock.value,disk_free=lambda:volume.free)
    for _ in range(10):
        clock.value+=INTERVAL;assert restarted.tick()['resumed']==[];assert worker.once('video') is False
    assert f.db.get(ident)['attempts']==1 and f.db.query('SELECT * FROM api_calls')==ledger
    volume.free=57_000_000_000;clock.value+=INTERVAL
    assert restarted.tick()['resumed']==[ident] and worker.once('video') is True
    resumed=f.db.get(ident)
    assert resumed['state']=='completed' and resumed['attempts']==2 and resumed['payload']==original_payload
    assert resumed['result'][PREPARE_STATE]['receipt']==saved['receipt'] and calls==[ident]
    assert f.db.query('SELECT * FROM api_calls')==ledger
    children=f.db.query('SELECT * FROM jobs WHERE job_type=?',(JOB_TYPE,));assert len(children)==1
    assert children[0]['state']=='pending' and children[0]['attempts']==0 and not f.db.query('SELECT * FROM publications')


def test_download_partial_and_binding_survive_deferred_mid_acquisition(natural,monkeypatch):
    f=natural;parent,result,job=prepare(f);service=YouTubeNaturalDownload(f.s,f.db);binding=service._binding(job)[1]
    area=f.s.root/'media'/job['job_id']/'youtube-highest-v3';area.mkdir(mode=0o700,parents=True)
    partial=area/'stream-owned60.mp4.part';partial.write_bytes(b'Owned partial survives disk wait and child exit')
    before=partial.read_bytes();clock=SimpleNamespace(value=time.time());monkeypatch.setattr('services.hub.db.time.time',lambda:clock.value)
    class LowSpace:
        def __init__(self,s):pass
        def acquire(self,*args,**kwargs):
            from services.hub.errors import HubError
            raise HubError('DISK_LOW',review=True)
    service.downloader_factory=LowSpace
    with pytest.raises(ResourceDeferred) as caught:service.process(job)
    error=caught.value
    assert error.binding_sha256==service.resource_binding_sha(f.db.get(job['job_id']))
    assert error.required_bytes==binding['source_plan']['required_working_bytes']-len(before)
    coordinator=ResourceWaitCoordinator(f.s,f.db,validators=resource_wait_validators(f.s,f.db),clock=lambda:clock.value,disk_free=lambda:60_000_000_000)
    coordinator.defer(job,error);clock.value+=INTERVAL
    assert coordinator.tick()['resumed']==[job['job_id']]
    reclaimed=f.db.claim('video',600,4);assert reclaimed['job_id']==job['job_id'] and reclaimed['attempts']==2
    assert service._binding(reclaimed)[1]==binding and partial.read_bytes()==before
    assert not f.db.query('SELECT * FROM api_calls') and not f.db.query('SELECT * FROM publications')


def test_expired_wait_cannot_execute_http_when_coordinator_is_unavailable(natural,monkeypatch):
    f=natural;ident=f.service.enqueue_if_eligible(f.payload);job=f.db.claim('video',600,4)
    result={'resource_wait_v1':{'version':'local-resource-wait-v1','state':'waiting_resource','deadline':time.time()-1}}
    with f.db.transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(result),ident))
    class NoNetwork:
        def __init__(self,s):raise AssertionError('Expired resource wait must stop before metadata helper')
    service=YouTubeNaturalPrepare(f.s,f.db,downloader_factory=NoNetwork)
    with pytest.raises(Exception,match='RESOURCE_WAIT_TIMEOUT'):service.process(f.db.get(ident))
    assert not f.db.query('SELECT * FROM api_calls')
