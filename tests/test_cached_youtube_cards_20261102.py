from copy import deepcopy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import time
import uuid

import pytest
from services.hub.config import Settings
from services.hub.db import Store
from services.hub.errors import HubError

spec=importlib.util.spec_from_file_location('youtube_card_backfill',Path(__file__).parents[1]/'scripts/ops/backfill_youtube_cards_20261102.py')
op=importlib.util.module_from_spec(spec);spec.loader.exec_module(op)

def test_grant_does_not_expand_day_sources_or_expiry():
    expected={'version':op.VERSION,'source_ids':list(op.TARGETS),'owner_quote':op.OWNER_QUOTE,'human_review':False}
    grant={'plan':deepcopy(expected),'grant_id':str(uuid.uuid4()),'issued_at':1000,'expires_at':2000,'owner_response_reference':'actual owner message'}
    op.validate_grant(grant,expected,now=1500)
    for broken in [dict(grant,expires_at=10000),dict(grant,plan={**expected,'source_ids':[*op.TARGETS,'123']}),dict(grant,plan={**expected,'human_review':True})]:
        with pytest.raises(HubError):op.validate_grant(broken,expected,now=1500)
    with pytest.raises(HubError,match='BACKFILL_GRANT_EXPIRED'):op.validate_grant(grant,expected,now=2001)
    op.validate_grant(grant,expected,now=2001,require_live=False)

def setup_store(tmp_path):
    settings=Settings(tmp_path,{'lease_seconds':300,'max_attempts':4},False)
    store=Store(settings.db_path);store.init();now=time.time()
    with store.transaction() as db:
        db.execute("INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,attempts,error_code,policy_version,payload,result,stage) VALUES(?,'x','x_discovery:actual','x_discovery','tweet','completed',?,?,1,NULL,'machine-tweet-v3-api','{}','{\"sources\":[\"original\"]}','x_official_observation')",(op.CAPTURE,now,now))
    scope=SimpleNamespace(store=store,settings=settings,grant=SimpleNamespace(ref={'file':'protected grant','sha256':'a'*64}))
    return store,scope

def test_real_lease_restore_preserves_completed_history_and_new_checkpoint(tmp_path):
    store,scope=setup_store(tmp_path)
    with pytest.raises(RuntimeError):
        with op.discovery_lease(scope) as job:
            assert job['state']=='processing' and job['attempts']==2 and job['lease_until']>time.time()
            assert store.heartbeat(job,300)
            store.checkpoint(job,'local_import',{'sources':['original'],'oneoff_imported_parent_ids':{'scope':'actual child'}})
            raise RuntimeError('simulated local process exception')
    restored=store.get(op.CAPTURE)
    assert (restored['state'],restored['error_code'],restored['stage'],restored['attempts'])==('completed',None,'x_official_observation',2)
    assert restored['lease_owner'] is None and restored['lease_until'] is None
    assert restored['result']['sources']==['original']
    assert restored['result']['oneoff_imported_parent_ids']=={'scope':'actual child'}
    assert store.query('SELECT count(*) n FROM api_calls')[0]['n']==0
    assert store.query('SELECT count(*) n FROM publications')[0]['n']==0
    audit=store.query("SELECT outcome FROM audit WHERE action='resume_cached_source_import'")
    assert len(audit)==1 and '"state":"completed"' in audit[0]['outcome']

def test_active_capture_is_not_stolen_or_attempt_reset(tmp_path):
    store,scope=setup_store(tmp_path)
    with op.discovery_lease(scope):
        before=store.get(op.CAPTURE)
        with pytest.raises(HubError,match='BACKFILL_CAPTURE_NOT_IDLE'):
            with op.discovery_lease(scope):pytest.fail('must not acquire')
        assert store.get(op.CAPTURE)==before

def test_source_author_time_and_id_are_immutable():
    post={'id':op.TARGETS[0],'created_at':'2026-11-02T06:32:46.000Z','text':'authentic'}
    fake=SimpleNamespace(check=lambda **kw:None,expected={'capture':{'original_posts':{op.TARGETS[0]:post}}})
    payload={'post':deepcopy(post),'author':{'id':op.AUTHOR,'username':op.HANDLE},'url':'https://x.com/'+op.HANDLE+'/status/'+post['id'],'source_api':'x-v2-official','official_source':{'version':'x-official-source-v2'}}
    assert op.Scope.payload(fake,payload)==post['id']
    for field,value in [('created_at','2026-11-02T17:32:46.000Z'),('text','rewritten')]:
        changed=deepcopy(payload);changed['post'][field]=value
        with pytest.raises(HubError,match='BACKFILL_SOURCE_SCOPE_CHANGED'):op.Scope.payload(fake,changed)
    changed=deepcopy(payload);changed['author']['id']='other'
    with pytest.raises(HubError,match='BACKFILL_SOURCE_SCOPE_CHANGED'):op.Scope.payload(fake,changed)
