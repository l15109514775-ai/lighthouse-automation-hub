"""Recorded MockTransport + actual SQLite/Worker; no paid API/model/publish.

Only parser-revision/fault injection, policy selection and photo HTTP are owned
seams. Receipt hashes, deferred failure rows, source binding and leases are real.
"""
from copy import deepcopy
import json,time
import pytest

import adapters.x as adapter
import services.hub.x_official_reparse_v1 as module
from services.hub.x_official_reparse_v1 import OfficialSourceReparse,JOB_TYPE
from services.hub.x_api_discovery import XAPIDiscovery
from services.hub.db import encode,Store
from services.hub.errors import HubError
from services.hub.source_receipts import SourceReceipts
from services.hub.worker import Worker
from test_x_official_capture_queue_v1 import capture_case
from test_x_official_relations_v2 import relations

def rows(f):return f.db.query('SELECT * FROM jobs WHERE job_type=?',(JOB_TYPE,))

def reject(f,monkeypatch):
    original=adapter.build_official_record
    def reject_parser(*a,**kw):raise HubError('X_CONTENT_TYPE_UNSUPPORTED',review=True)
    monkeypatch.setattr(adapter,'build_official_record',reject_parser)
    result=f.service.run(f.job);f.db.finish(f.job,'completed',result=result)
    monkeypatch.setattr(adapter,'build_official_record',original)
    return rows(f)[0]['job_id']

def upgraded(monkeypatch):monkeypatch.setattr(module,'parser_revision',lambda:'d'*64)

def test_rejected_post_is_real_failed_child_before_cursor_not_a_publication_or_tombstone(capture_case,monkeypatch):
    f=capture_case;ident=reject(f,monkeypatch);job=f.db.get(ident)
    assert job['state']=='failed' and job['attempts']==0 and job['error_code']=='X_CONTENT_TYPE_UNSUPPORTED' and job['stage']=='source_parse_blocked'
    r=job['payload']['rejection'];assert r['post_id']=='900' and r['discovery_job_id']==f.job['job_id'] and r['human_review'] is False
    assert len(job['payload']['receipts'])==4 and all(v['raw_sha256'] and v['file'] for v in job['payload']['receipts'].values())
    assert f.db.query("SELECT value FROM source_cursors WHERE key='x-api:fixture_only'")[0]['value']=='900'
    events=f.db.query("SELECT data FROM events WHERE name='task.failed'");assert len(events)==1
    assert json.loads(events[0]['data'])['job_id']==ident and json.loads(events[0]['data'])['queue']=='tweet'
    assert not f.db.query('SELECT * FROM source_tombstones') and not f.db.query('SELECT * FROM publications')

def test_rejected_first_valid_second_is_admitted_and_cursor_still_covers_both(capture_case,monkeypatch):
    f=capture_case;f.server.kind='original'
    # Actual HTTP response contains two posts. Only the first parser is faulty.
    old=f.server.__class__.__call__
    def two(self,request):
        response=old(self,request)
        if request.url.path=='/2/users/12345/tweets':
            import httpx
            body=json.loads(response.content);other=deepcopy(body['data'][0]);other.update(id='901',edit_history_tweet_ids=['901']);body['data'].append(other)
            return httpx.Response(200,content=json.dumps(body).encode())
        return response
    monkeypatch.setattr(f.server.__class__,'__call__',two)
    build=adapter.build_official_record
    def parser(handle,pid,*a,**kw):
        if pid=='900':raise HubError('X_CONTENT_TYPE_UNSUPPORTED',review=True)
        return build(handle,pid,*a,**kw)
    monkeypatch.setattr(adapter,'build_official_record',parser)
    value=f.service.run(f.job)
    assert value['sources'][0]['rejected_posts']==[{'post_id':'900','error_code':'X_CONTENT_TYPE_UNSUPPORTED'}]
    assert len(rows(f))==1 and f.db.query("SELECT source_id FROM x_official_capture_queue_v1")[0]['source_id']=='901'
    assert f.db.query("SELECT value FROM source_cursors WHERE key='x-api:fixture_only'")[0]['value']=='901'

def test_same_capture_replay_creates_one_failure_and_zero_new_http_after_crash(capture_case,monkeypatch):
    f=capture_case
    monkeypatch.setattr(adapter,'build_official_record',lambda *a,**k:(_ for _ in ()).throw(HubError('X_CONTENT_TYPE_UNSUPPORTED')))
    register=f.service.reparse.register
    class ExitAfterFailure(BaseException):pass
    def crash(*a):register(*a);raise ExitAfterFailure()
    monkeypatch.setattr(f.service.reparse,'register',crash)
    with pytest.raises(ExitAfterFailure):f.service.run(f.job)
    calls=len(f.server.calls);assert len(rows(f))==1 and not f.db.query("SELECT * FROM source_cursors WHERE key='x-api:fixture_only'")
    resumed=XAPIDiscovery(f.s,Store(f.s.db_path),f.http)
    assert Worker(f.s,f.db,{'x_discovery':resumed.run}).once('tweet')
    assert len(rows(f))==1 and len(f.server.calls)==calls
    assert f.db.get(f.job['job_id'])['state']=='completed'

def test_failure_persistence_exception_stops_before_cursor(capture_case,monkeypatch):
    f=capture_case
    monkeypatch.setattr(adapter,'build_official_record',lambda *a,**k:(_ for _ in ()).throw(HubError('X_CONTENT_TYPE_UNSUPPORTED')))
    monkeypatch.setattr(f.service.reparse,'register',lambda *a:(_ for _ in ()).throw(HubError('DISK_LOW',review=True)))
    with pytest.raises(HubError,match='DISK_LOW'):f.service.run(f.job)
    assert not rows(f) and not f.db.query("SELECT * FROM source_cursors WHERE key='x-api:fixture_only'")

@pytest.mark.parametrize('code',['DISK_LOW','NEW_FATAL_TRANSPORT_ERROR'])
def test_detail_receipt_failure_is_not_swallowed_as_content_rejection(capture_case,monkeypatch,code):
    f=capture_case;record=f.service.receipts.record
    def disk(job,item):
        row=f.db.query('SELECT path FROM source_http_receipts WHERE call_id=?',(item['call_id'],))[0]
        if row['path'].startswith('/2/tweets/'):raise HubError(code,review=True)
        return record(job,item)
    monkeypatch.setattr(f.service.receipts,'record',disk)
    with pytest.raises(HubError,match=code):f.service.run(f.job)
    assert not rows(f) and not f.db.query("SELECT * FROM source_cursors WHERE key='x-api:fixture_only'")

def test_explicit_revision_retry_has_real_child_lease_and_zero_x_reads_parent_stays_completed(capture_case,monkeypatch):
    f=capture_case;ident=reject(f,monkeypatch);service=OfficialSourceReparse(f.s,f.db,downloader=f.server.download)
    before=f.db.get(f.job['job_id']);calls=len(f.server.calls);ledger=f.db.query('SELECT * FROM api_calls')
    with pytest.raises(HubError,match='PARSER_UNCHANGED'):service.retry_job(ident,expected_parser_sha256=module.parser_revision())
    upgraded(monkeypatch);assert service.retry_job(ident,expected_parser_sha256='d'*64)['attempts']==0
    with pytest.raises(HubError,match='CHILD_LEASE'):service.process(f.db.get(ident))
    assert Worker(f.s,f.db,{JOB_TYPE:service.process}).once('tweet')
    done=f.db.get(ident);assert done['state']=='completed' and done['attempts']==1 and len(f.server.calls)==calls and f.db.query('SELECT * FROM api_calls')==ledger
    assert f.db.get(f.job['job_id'])==before
    tweet=f.db.query("SELECT * FROM jobs WHERE job_type='tweet_process'")[0]
    assert f.db.query('SELECT discovery_job_id FROM tweet_source_bindings')[0]['discovery_job_id']==f.job['job_id']
    assert SourceReceipts(f.s,f.db).assess_official(f.db.get(tweet['job_id']),source_version='x-official-source-v2')['nodes'][0]['source']['post']['id']=='900'

@pytest.mark.parametrize('change',['stale','publication','tombstone','tampered_receipt','uid'])
def test_cached_recovery_preserves_normal_age_publication_tombstone_and_receipt_gates(capture_case,monkeypatch,change):
    f=capture_case;ident=reject(f,monkeypatch);service=OfficialSourceReparse(f.s,f.db);upgraded(monkeypatch)
    if change=='stale':
        now=time.time();monkeypatch.setattr(time,'time',lambda:now+7201)
    elif change=='publication':
        with f.db.transaction() as db:db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',('owned-existing',ident,'bilibili','x','900','site_uid:'+f.s.config['target_bilibili_site_uid'],'owned-kind','unknown',None,None,time.time()))
    elif change=='tombstone':
        with f.db.transaction() as db:db.execute('INSERT INTO source_tombstones VALUES(?,?,?,?,?,?)',('x','900','OWNER_BACKLOG_CLEARED',time.time(),ident,'owner_clear_all'))
    elif change=='uid':f.s.config['target_bilibili_site_uid']='999'
    else:
        from pathlib import Path
        ref=next(iter(f.db.get(ident)['payload']['receipts'].values()));Path(ref['file']).write_bytes(b'{}')
    calls=len(f.server.calls);before=f.db.get(ident)
    with pytest.raises(HubError):service.retry_job(ident,expected_parser_sha256='d'*64)
    assert f.db.get(ident)==before and len(f.server.calls)==calls

def test_bypassed_revision_gate_fails_closed_without_http_or_attempt_reset(capture_case,monkeypatch):
    f=capture_case;ident=reject(f,monkeypatch);before=len(f.server.calls)
    with f.db.transaction() as db:db.execute("UPDATE jobs SET state='pending' WHERE job_id=?",(ident,))
    assert Worker(f.s,f.db,{JOB_TYPE:OfficialSourceReparse(f.s,f.db).process}).once('tweet')
    current=f.db.get(ident);assert current['state']=='failed' and current['error_code']=='X_REPARSE_PARSER_UNCHANGED' and current['attempts']==1 and len(f.server.calls)==before

def test_selected_revision_survives_resource_wait_and_same_job_resume_without_new_x(capture_case,monkeypatch):
    from services.hub.locks import resource_guard
    f=capture_case;ident=reject(f,monkeypatch);service=OfficialSourceReparse(f.s,f.db,downloader=f.server.download);upgraded(monkeypatch)
    service.retry_job(ident,expected_parser_sha256='d'*64);calls=len(f.server.calls);ledger=f.db.query('SELECT * FROM api_calls')
    worker=Worker(f.s,f.db,{JOB_TYPE:service.process})
    with resource_guard(f.s.root/'data/locks/heavy'):
        assert worker.once('tweet');waiting=f.db.get(ident)
        assert waiting['state']=='retry_scheduled' and waiting['attempts']==0 and waiting['result']['last_parser_sha256']=='d'*64
        assert waiting['result']['retry_parser_sha256']=='d'*64
    with f.db.transaction() as db:db.execute('UPDATE jobs SET next_run=0 WHERE job_id=?',(ident,))
    assert worker.once('tweet') and f.db.get(ident)['state']=='completed' and f.db.get(ident)['attempts']==1
    assert len(f.server.calls)==calls and f.db.query('SELECT * FROM api_calls')==ledger
