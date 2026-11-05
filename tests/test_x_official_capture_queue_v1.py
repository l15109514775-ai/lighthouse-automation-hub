"""Real SQLite/Worker and recorded MockTransport, never live X/model/publish.

Only the policy selection and photo transport are explicit owned boundaries;
raw receipts, typed quote/reply nodes, media decoding and lease fences are real.
"""
from copy import deepcopy
from types import SimpleNamespace
from pathlib import Path
import hashlib,json,time,uuid
import pytest
import httpx

from services.hub.db import encode,Store
from services.hub.errors import HubError
from services.hub.locks import resource_guard
from services.hub.worker import Worker
from services.hub.source_receipts import SourceReceipts
from services.hub.x_api_discovery import XAPIDiscovery
from services.hub.x_official_capture_queue_v1 import OfficialCaptureQueue,JOB_TYPE,TABLE,CapturedReadClient
from test_x_official_relations_v2 import relations,POLICY
from test_x_official_source_v2 import png


@pytest.fixture
def capture_case(relations,monkeypatch):
    import services.hub.machine_tweets_api as machines
    s,db,server,adapter,job,ledger=relations
    s.config.update(x_api_enabled=True,x_web_enabled=False,policy_version=POLICY,machine_tweet_enabled=True,machine_source_generation=1)
    server.photos=True
    machine=SimpleNamespace(version=POLICY,source_record_version='x-official-source-v2',
        policy=lambda:{'max_post_age_seconds':7200,'activation_at':time.time()-30},job_policy=lambda:POLICY,clear_backlog=lambda:[])
    monkeypatch.setattr(machines,'MachineTweetsAPIReference',lambda *args:machine)
    service=XAPIDiscovery(s,db,adapter.client)
    queue=OfficialCaptureQueue(s,db,downloader=server.download)
    return SimpleNamespace(s=s,db=db,server=server,job=job,ledger=ledger,service=service,queue=queue,http=adapter.client)


def discovered(f):
    result=f.service.run(f.job);f.db.finish(f.job,'completed',result=result)
    return f.db.query('SELECT * FROM jobs WHERE job_type=?',(JOB_TYPE,))[0]['job_id']


def test_one_paid_capture_eight_resource_waits_then_true_media_ingestion_has_zero_x_rereads(capture_case):
    f=capture_case
    with resource_guard(f.s.root/'data/locks/heavy'):
        child=discovered(f);calls=len(f.server.calls);ledger=f.db.query('SELECT * FROM api_calls')
        assert calls==4 and f.db.get(f.job['job_id'])['attempts']==1
        worker=Worker(f.s,f.db,{JOB_TYPE:f.queue.process})
        for _ in range(8):
            with f.db.transaction() as db:db.execute('UPDATE jobs SET next_run=0 WHERE job_id=?',(child,))
            began=time.time();assert worker.once('tweet');current=f.db.get(child)
            assert current['state']=='retry_scheduled' and current['attempts']==0 and began+15<=current['next_run']<=time.time()+30
            assert f.db.query('SELECT * FROM api_calls')==ledger and len(f.server.calls)==calls
        assert not f.server.downloads and not f.db.query("SELECT * FROM jobs WHERE job_type='tweet_process'")
    with f.db.transaction() as db:db.execute('UPDATE jobs SET next_run=0 WHERE job_id=?',(child,))
    assert worker.once('tweet') and f.db.get(child)['state']=='completed'
    tweets=f.db.query("SELECT * FROM jobs WHERE job_type='tweet_process'");assert len(tweets)==1
    real=SourceReceipts(f.s,f.db).assess_official(f.db.get(tweets[0]['job_id']),source_version='x-official-source-v2')
    assert [n['role'] for n in real['nodes']]==['post','parent','quote'] and len(f.server.downloads)==3
    assert len(f.server.calls)==calls and len(f.db.query('SELECT * FROM api_calls'))==4
    assert f.db.query('SELECT discovery_job_id FROM tweet_source_bindings')[0]['discovery_job_id']==f.job['job_id']
    assert not f.db.query('SELECT * FROM publications') and f.db.get(f.job['job_id'])['attempts']==1


def test_process_exit_after_durable_capture_restarts_same_job_from_real_receipts_without_http(capture_case,monkeypatch):
    f=capture_case;register=f.service.capture_queue.register
    class ExitAfterRegistration(BaseException):pass
    def crash(*args):register(*args);raise ExitAfterRegistration()
    monkeypatch.setattr(f.service.capture_queue,'register',crash)
    with pytest.raises(ExitAfterRegistration):f.service.run(f.job)
    calls=len(f.server.calls);assert calls==4
    resumed=XAPIDiscovery(f.s,Store(f.s.db_path),f.http)
    worker=Worker(f.s,f.db,{'x_discovery':resumed.run,JOB_TYPE:f.queue.process})
    assert worker.once('tweet')
    assert f.db.get(f.job['job_id'])['state']=='completed' and f.db.get(f.job['job_id'])['attempts']==2
    assert len(f.server.calls)==calls and len(f.db.query('SELECT * FROM '+TABLE))==1
    assert f.db.query("SELECT value FROM source_cursors WHERE key='x-api:fixture_only'")[0]['value']=='900'


def test_replayed_capture_poll_uses_original_observed_time_not_retry_wall_clock(capture_case,monkeypatch):
    f=capture_case;register=f.service.capture_queue.register
    class ExitAfterRegistration(BaseException):pass
    def crash(*args):register(*args);raise ExitAfterRegistration()
    monkeypatch.setattr(f.service.capture_queue,'register',crash)
    with pytest.raises(ExitAfterRegistration):f.service.run(f.job)
    last=f.db.query("SELECT captured_at FROM source_http_receipts WHERE path='/2/users/12345/tweets'")[0]['captured_at']
    now=time.time();monkeypatch.setattr(time,'time',lambda:now+120)
    assert Worker(f.s,f.db,{'x_discovery':XAPIDiscovery(f.s,f.db,f.http).run}).once('tweet')
    poll=f.db.query("SELECT * FROM source_polls WHERE source_key='x-api:fixture_only'")[0]
    assert poll['last_success_at']==last and poll['checked_at']==now+120
    assert json.loads(poll['metadata'])['capture_reused'] is True and len(f.server.calls)==4


def test_new_discovery_while_old_media_waits_sees_source_as_known_and_skips_reference_fetch(capture_case):
    f=capture_case;child=discovered(f)
    with resource_guard(f.s.root/'data/locks/heavy'):
        assert Worker(f.s,f.db,{JOB_TYPE:f.queue.process}).once('tweet')
        ident=f.db.enqueue('scheduler','owned-next-30min','x_discovery','tweet',{},POLICY)
        source=XAPIDiscovery(f.s,f.db,f.http);cut=len(f.server.calls)
        assert Worker(f.s,f.db,{'x_discovery':source.run}).once('tweet')
    assert f.db.get(ident)['state']=='completed' and len(f.db.query('SELECT * FROM '+TABLE))==1
    assert len(f.server.calls)>cut and not any('/2/tweets/' in v for v in f.server.calls[cut:])
    assert f.db.get(child)['state']=='retry_scheduled' and not f.db.query("SELECT * FROM jobs WHERE job_type='tweet_process'")


@pytest.mark.parametrize('changed',['lease','descriptor','foreign_source','policy','generation','disabled'])
def test_media_child_rejects_changed_binding_or_lost_lease_before_any_media_or_http(capture_case,changed):
    f=capture_case;child=discovered(f);claimed=f.db.claim('tweet',180,4);assert claimed['job_id']==child
    if changed=='descriptor':Path(claimed['payload']['descriptor']['file']).write_text('{}','utf-8')
    elif changed=='generation':f.s.config['machine_source_generation']=2
    elif changed=='disabled':f.s.config['machine_tweet_enabled']=False
    else:
        with f.db.transaction() as db:
            if changed=='lease':db.execute("UPDATE jobs SET lease_owner='owned stolen lease' WHERE job_id=?",(child,))
            elif changed=='foreign_source':db.execute("UPDATE jobs SET source_id='901' WHERE job_id=?",(child,))
            else:db.execute("UPDATE jobs SET policy_version='machine-tweet-v3-api' WHERE job_id=?",(child,))
    calls=len(f.server.calls)
    with pytest.raises(HubError):f.queue.process(claimed)
    assert len(f.server.calls)==calls and not f.server.downloads and not f.db.query("SELECT * FROM jobs WHERE job_type='tweet_process'")


def test_expired_captured_post_preserves_default_two_hour_gate_and_original_times(capture_case,monkeypatch):
    f=capture_case;child=discovered(f);before=f.db.get(child)['payload'];old=time.time()
    monkeypatch.setattr(time,'time',lambda:old+7300)
    assert Worker(f.s,f.db,{JOB_TYPE:f.queue.process}).once('tweet')
    current=f.db.get(child)
    assert current['state']=='completed' and current['payload']==before
    assert current['result']['ingestion']['error_code']=='OWNER_TWO_HOUR_WINDOW_EXPIRED'
    assert not f.server.downloads and not f.db.query("SELECT * FROM jobs WHERE job_type='tweet_process'")


def test_paid_discovery_busy_fallback_never_refunds_attempt_or_replays_http(capture_case):
    f=capture_case
    with f.db.transaction() as db:db.execute("UPDATE jobs SET state='pending',attempts=0,lease_owner=NULL,lease_until=NULL WHERE job_id=?",(f.job['job_id'],))
    def broken(job):
        f.http.request('x','GET','https://api.x.com/2/users/by/username/fixture_only',job_id=job['job_id'],params={'user.fields':'name'},reserved_cost_usd=.01)
        raise HubError('HEAVY_RESOURCE_BUSY',retryable=True)
    worker=Worker(f.s,f.db,{'x_discovery':broken});assert worker.once('tweet')
    current=f.db.get(f.job['job_id']);assert current['state']=='awaiting_review' and current['attempts']==1
    assert current['error_code']=='X_PAID_CAPTURE_RECONCILIATION_REQUIRED' and not worker.once('tweet')
    assert len(f.server.calls)==1 and len(f.db.query('SELECT * FROM api_calls'))==1


def test_cached_read_raw_tamper_is_not_replaced_by_a_paid_refetch(capture_case):
    f=capture_case;f.service.run(f.job);calls=len(f.server.calls)
    descriptor=next((f.s.root/'media'/f.job['job_id']/'official-capture-v1/reads').glob('*.json'))
    values=[json.loads(p.read_bytes()) for p in descriptor.parent.glob('*.json')]
    captured=next(v for v in values if 'call_id' in v)
    row=f.ledger._receipt(captured['call_id']);f.ledger._file(row).write_bytes(b'{}')
    reader=CapturedReadClient(f.s,f.db,f.http,f.job,f.ledger)
    with pytest.raises(HubError):reader.request_recorded('x','GET',captured['url'],job_id=f.job['job_id'],headers={'Authorization':'Bearer owned'},params=captured['params'],reserved_cost_usd=.05)
    assert len(f.server.calls)==calls


def test_runtime_registers_fixed_media_type_without_publication_permission(capture_case):
    from services.hub.cli import runtime
    f=capture_case;handlers=runtime(f.s,f.db)
    assert callable(handlers[JOB_TYPE]) and callable(handlers['x_discovery']) and callable(handlers['tweet_process'])


def test_request_error_keeps_original_upper_bound_and_never_reissues_same_read(capture_case):
    f=capture_case;calls=[]
    def error(request):calls.append(request.url.path);return httpx.Response(503,json={'errors':[{'title':'Owned server error'}]})
    from adapters.http import ProviderHTTP
    bad=ProviderHTTP(f.http.budget,httpx.MockTransport(error));reader=CapturedReadClient(f.s,f.db,bad,f.job,f.ledger)
    options={'job_id':f.job['job_id'],'headers':{'Authorization':'Bearer owned'},'params':{'user.fields':'name'},'reserved_cost_usd':.01}
    with pytest.raises(HubError,match='API_SERVER_ERROR'):reader.request_recorded('x','GET','https://api.x.com/2/users/by/username/fixture_only',**options)
    ledger=f.db.query('SELECT * FROM api_calls')
    with pytest.raises(HubError,match='X_CAPTURE_READ_RECONCILIATION_REQUIRED'):reader.request_recorded('x','GET','https://api.x.com/2/users/by/username/fixture_only',**options)
    assert len(calls)==1 and f.db.query('SELECT * FROM api_calls')==ledger and ledger[0]['cost_usd']==.01


def test_saved_raw_receipt_recovers_after_exit_before_descriptor_without_new_http(capture_case,monkeypatch):
    from services.hub import x_official_capture_queue_v1 as module
    f=capture_case;original=module._write
    class ExitBeforeDescriptor(BaseException):pass
    def crash(root,file,value):
        if 'call_id' in value:raise ExitBeforeDescriptor()
        return original(root,file,value)
    reader=CapturedReadClient(f.s,f.db,f.http,f.job,f.ledger)
    options={'job_id':f.job['job_id'],'headers':{'Authorization':'Bearer owned'},'params':{'user.fields':'name'},'reserved_cost_usd':.01}
    with monkeypatch.context() as m:
        m.setattr(module,'_write',crash)
        with pytest.raises(ExitBeforeDescriptor):reader.request_recorded('x','GET','https://api.x.com/2/users/by/username/fixture_only',**options)
    assert len(f.server.calls)==1
    value,receipt=reader.request_recorded('x','GET','https://api.x.com/2/users/by/username/fixture_only',**options)
    assert value['data']==f.server.author and len(f.server.calls)==1 and f.ledger._body(f.ledger._receipt(receipt['call_id']))==value


def test_lease_stolen_during_actual_photo_download_cannot_create_model_job(capture_case):
    f=capture_case;child=discovered(f);claimed=f.db.claim('tweet',180,4);original=f.server.download
    def steal(*args,**kwargs):
        value=original(*args,**kwargs)
        with f.db.transaction() as db:db.execute("UPDATE jobs SET lease_owner='owned competing media owner' WHERE job_id=?",(child,))
        return value
    f.queue.downloader=steal
    with pytest.raises(HubError,match='X_CAPTURE_MEDIA_LEASE_REQUIRED'):f.queue.process(claimed)
    assert len(f.server.downloads)==1 and not f.db.query("SELECT * FROM jobs WHERE job_type='tweet_process'")
    assert len(f.server.calls)==4 and not f.db.query('SELECT * FROM publications')


def test_complete_first_source_is_durable_known_before_a_later_paid_read_fails(capture_case):
    f=capture_case;original=f.http.transport;second=deepcopy(f.server.root)
    second.update(id='901',edit_history_tweet_ids=['901'],referenced_tweets=[{'type':'quoted','id':'600'}])
    def response(request):
        if request.url.path=='/2/users/12345/tweets':
            f.server.calls.append(request.url.path)
            first=deepcopy(f.server.root);first['referenced_tweets']=[{'type':'quoted','id':'700'},{'type':'replied_to','id':'800'}]
            return httpx.Response(200,json={'data':[first,second]})
        if request.url.path=='/2/tweets/600':
            f.server.calls.append(request.url.path);return httpx.Response(503,json={'errors':[{'title':'Owned unavailable second quote'}]})
        return f.server(request)
    f.http.transport=httpx.MockTransport(response)
    with pytest.raises(HubError,match='API_SERVER_ERROR'):f.service.run(f.job)
    assert f.queue.known_ids()==['900'] and len(f.db.query('SELECT * FROM '+TABLE))==1
    count=len(f.server.calls)
    with pytest.raises(HubError,match='X_CAPTURE_READ_RECONCILIATION_REQUIRED'):XAPIDiscovery(f.s,f.db,f.http).run(f.job)
    assert len(f.server.calls)==count and not f.db.query("SELECT * FROM source_cursors WHERE key='x-api:fixture_only'")


def test_capture_admission_evidence_failure_never_skips_content_or_advances_cursor(capture_case,monkeypatch):
    f=capture_case
    def reject(*args):raise HubError('X_OFFICIAL_SOURCE_DOCUMENT_INVALID',review=True)
    monkeypatch.setattr(f.service.capture_queue,'register',reject)
    with pytest.raises(HubError,match='X_OFFICIAL_SOURCE_DOCUMENT_INVALID'):f.service.run(f.job)
    assert not f.db.query("SELECT * FROM source_cursors WHERE key='x-api:fixture_only'") and not f.db.query('SELECT * FROM '+TABLE)
    assert f.db.get(f.job['job_id'])['result']['sources'][0]['state']=='blocked' and len(f.server.calls)==4


def test_real_bad_media_child_isolated_from_next_captured_post_and_never_bypasses_decode(capture_case):
    f=capture_case;first=discovered(f)
    f.server.root['id']='901';f.server.root['edit_history_tweet_ids']=['901']
    next_id=f.db.enqueue('scheduler','owned-next-valid-root','x_discovery','tweet',{},POLICY)
    parent=f.db.get(next_id)
    # The prior media child is pending; claim the new ordinary discovery using
    # the same real queue after a legitimate resource deferral.
    with resource_guard(f.s.root/'data/locks/heavy'):assert Worker(f.s,f.db,{JOB_TYPE:f.queue.process}).once('tweet')
    source=XAPIDiscovery(f.s,f.db,f.http)
    assert Worker(f.s,f.db,{'x_discovery':source.run}).once('tweet') and f.db.get(next_id)['state']=='completed'
    calls=len(f.server.calls);original=f.server.download;once=[False]
    def bad_first(url,file,**options):
        if not once[0]:
            once[0]=True;options['check_disk'](100);Path(file).write_bytes(png(7,6))
            return {'file':str(file),'sha256':hashlib.sha256(Path(file).read_bytes()).hexdigest(),'bytes':Path(file).stat().st_size}
        return original(url,file,**options)
    f.queue.downloader=bad_first
    with f.db.transaction() as db:db.execute('UPDATE jobs SET next_run=0 WHERE job_id=?',(first,))
    worker=Worker(f.s,f.db,{JOB_TYPE:f.queue.process})
    assert worker.once('tweet') and f.db.get(first)['result']['ingestion']['disposition']=='rejected_asset'
    failed=[json.loads(row['data']) for row in f.db.query("SELECT data FROM events WHERE name='task.failed'")]
    assert failed==[{'job_id':first,'queue':'tweet','source_id':'900',
        'code':'X_MEDIA_DECLARED_DIMENSIONS_MISMATCH','media_ingest_only':True}]
    assert worker.once('tweet')
    tweets=f.db.query("SELECT * FROM jobs WHERE job_type='tweet_process'")
    assert len(tweets)==1 and tweets[0]['source_id']=='901' and len(f.server.calls)==calls
