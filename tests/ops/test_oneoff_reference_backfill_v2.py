"""Owned SQLite/grant/transport only; real source/models/rootfs are boundaries."""
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from scripts.ops import oneoff_reference_backfill_v2 as m
from scripts.ops import capture_two_reference_sources_v2 as capture
from services.hub.db import encode
from services.hub.errors import HubError, PublishUnknown


@pytest.fixture
def scoped(runtime):
    live,store=runtime
    hub=live.root/'config/hub.toml';hub.parent.mkdir(mode=0o700);hub.write_bytes(b'# Owned synthetic settings only\n')
    support=SimpleNamespace(**{name:getattr(capture,name) for name in
        ('TARGETS','SITE_UID','AUTHOR_ID','HANDLE','target_post','strict_json','reference_text','write_new','VERSION')})
    support.LIVE_HUB_SHA=hashlib.sha256(hub.read_bytes()).hexdigest()
    scope=object.__new__(m.Scope)
    scope.live,scope.store,scope.support=live,store,support
    scope.settings=m.scoped_settings(live,support);scope.queue_depth=0;scope.worker_context_verified=False;scope.session_job=None
    grant={'grant_id':'owned-grant','issued_at':time.time()-10,'expires_at':time.time()+300}
    ref={'file':str(live.root/'config/owned-grant.json'),'sha256':'1'*64,'bytes':111}
    scope.grant=SimpleNamespace(ref=ref)
    # Explicit rootfs/auth boundary only; expiry remains real and checked by
    # the actual Scope.payload/AgeScopedReceipts/claim/commit methods below.
    def check(*,require_live=True):
        if require_live and time.time()>=grant['expires_at']:raise HubError('OWNED_GRANT_EXPIRED')
        return grant,ref
    scope.check=check
    scope.descriptor_ref={'file':str(live.root/'media/owned-descriptor.json'),'sha256':'2'*64,'bytes':222}
    capture_id=store.enqueue('owner_backfill','owned-capture','x_discovery','tweet',{},support.VERSION)
    scope.capture_job_id=capture_id
    source_id=sorted(support.TARGETS)[0]
    post={'id':source_id,'author_id':support.AUTHOR_ID,'text':'Owned original source',
          'created_at':'2026-11-01T12:00:00Z','referenced_tweets':[{'type':t,'id':i} for t,i in support.TARGETS[source_id]]}
    payload={'post':post,'author':{'id':support.AUTHOR_ID,'username':support.HANDLE},
             'url':'https://x.com/'+support.HANDLE+'/status/'+source_id,'source_api':'x-v2-official',
             'official_source':{'version':m.SOURCE_VERSION}}
    scope.descriptor={'targets':{source_id:{'original_post':deepcopy(post)}}}
    parent_id=store.enqueue('x',source_id,'tweet_process','tweet',payload,m.POLICY)
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='upload_pending',result=? WHERE job_id=?",(encode({'owner_reference_backfill':{'grant':ref},'old_evidence':{'human_review':False}}),parent_id))
        db.execute("UPDATE jobs SET state='upload_pending',result=? WHERE job_id=?",(encode({'oneoff_imported_parent_ids':{source_id:parent_id}}),capture_id))
    return scope,parent_id,source_id,grant


def test_scoped_settings_changes_only_policy_and_exact_avatar_scope_leaving_live_intact(scoped):
    scope,_,_,_=scoped;before=deepcopy(scope.live.config);raw=(scope.live.root/'config/hub.toml').read_bytes()
    result=m.scoped_settings(scope.live,scope.support)
    assert result.config['policy_version']==m.POLICY and scope.live.config==before
    assert {k for k in result.config if result.config[k]!=before.get(k)}=={'policy_version',m.AVATAR_CONFIG_KEY}
    assert result.config[m.AVATAR_CONFIG_KEY]==m.AVATAR_CONFIG
    assert result.config['allowed_media_hosts']==before['allowed_media_hosts']
    assert (scope.live.root/'config/hub.toml').read_bytes()==raw


@pytest.mark.parametrize('change',['source','created_at','expired'])
def test_age_exception_hooks_reject_outside_source_changed_time_and_expiry(scoped,change):
    scope,parent,_,grant=scoped;payload=scope.store.get(parent)['payload']
    assert m.AgeScopedReceipts(scope)._official_initial_job_state({'job_id':scope.capture_job_id},payload)=='upload_pending'
    if change=='source':payload['post']['id']='999'
    elif change=='created_at':payload['post']['created_at']='2026-11-01T11:59:00Z'
    else:grant['expires_at']=time.time()-1
    with pytest.raises(HubError):m.AgeScopedReceipts(scope)._official_publication_window({'job_id':scope.capture_job_id},payload)
    assert scope.store.get(parent)['attempts']==0


def test_held_claim_uses_real_lease_increments_once_and_preserves_result(scoped):
    scope,parent,_,_=scoped;before=scope.store.get(parent)
    with m.held_claim(scope,parent,expected_type='tweet_process',allowed_states=('upload_pending',)) as job:
        actual=scope.store.get(parent)
        assert actual['state']=='processing' and actual['attempts']==1 and actual['lease_owner']==job['lease_owner']
        assert actual['lease_until']>time.time() and actual['result']==before['result']
        scope.store.finish(job,'upload_pending',result=actual['result'])
    actual=scope.store.get(parent)
    assert actual['attempts']==1 and actual['lease_owner'] is actual['lease_until'] is None
    assert actual['result']==before['result']


@pytest.mark.parametrize('change',['active_lease','publication'])
def test_held_claim_cannot_steal_live_processing_or_existing_unknown_publication(scoped,change):
    scope,parent,source_id,_=scoped
    with scope.store.transaction() as db:
        if change=='active_lease':db.execute("UPDATE jobs SET state='processing',lease_owner='owned-other-live-owner',lease_until=? WHERE job_id=?",(time.time()+300,parent))
        else:db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('owned-unknown',parent,'bilibili','x',source_id,'site_uid:'+scope.support.SITE_UID,'dynamic_website_machine_v4_api_relations','unknown',None,None,time.time()))
    before=scope.store.get(parent)
    with pytest.raises(HubError):
        with m.held_claim(scope,parent,expected_type='tweet_process',allowed_states=('upload_pending','processing')):pytest.fail('Unsafe claim')
    assert scope.store.get(parent)==before


def test_claim_failure_persists_real_error_without_resetting_attempt_or_checkpoint(scoped):
    scope,parent,_,_=scoped;before=scope.store.get(parent)
    with pytest.raises(HubError,match='OWNED_PHASE_FAILURE'):
        with m.held_claim(scope,parent,expected_type='tweet_process',allowed_states=('upload_pending',)):
            raise HubError('OWNED_PHASE_FAILURE')
    actual=scope.store.get(parent)
    assert actual['state']=='failed' and actual['error_code']=='OWNED_PHASE_FAILURE' and actual['attempts']==1
    assert actual['result']==before['result'] and actual['lease_owner'] is actual['lease_until'] is None


@pytest.mark.parametrize('changed',[False,True])
def test_root_commit_requires_exact_prepared_artifacts_and_real_session_audit(scoped,monkeypatch,changed):
    scope,parent,source_id,grant=scoped
    sid=scope.store.enqueue('owner_backfill','owned-session','owner_reference_backfill_session','tweet',
        {'grant':scope.grant.ref,'descriptor':scope.descriptor_ref},m.VERSION)
    prepared={source_id:{'job_id':parent,'manifest':{'manifest_sha256':'3'*64},'artifact':{'sha256':'4'*64},'caption':'Owned caption'}}
    expected={source_id:{'job_id':parent,'manifest_sha256':'3'*64,'artifact_sha256':'4'*64,'caption_sha256':hashlib.sha256(b'Owned caption').hexdigest()}}
    if changed:expected[source_id]['artifact_sha256']='0'*64
    commit={'version':'owner-two-reference-root-commit-v2','action':'publish','grant_sha256':scope.grant.ref['sha256'],
            'descriptor_sha256':scope.descriptor_ref['sha256'],'prepared':expected,'actual_operator_reference':'Owned technical test only','issued_at':time.time()}
    file=scope.settings.root/'config/oneoff-reference-backfill-v2/commit.json';file.parent.mkdir();file.write_text(encode(commit),'utf-8')
    # POSIX-root protection itself is a named owned fixture boundary. The exact
    # JSON/hash checks, real session claim and SQLite audit are not mocked.
    actual_stat=Path.stat;chain={file,*file.parents}
    def protected(path,*args,**kwargs):
        value=actual_stat(path,*args,**kwargs)
        if path not in chain:return value
        items=list(value);items[0]&=~0o022;items[4]=0;return os.stat_result(items)
    monkeypatch.setattr(Path,'stat',protected)
    with m.held_claim(scope,sid,expected_type='owner_reference_backfill_session',allowed_states=('pending',)) as session:
        scope.session_job=session
        if changed:
            with pytest.raises(HubError,match='COMMIT_BINDING_INVALID'):m.wait_for_root_commit(scope,prepared)
        else:
            ref=m.wait_for_root_commit(scope,prepared)
            assert ref['sha256']==hashlib.sha256(file.read_bytes()).hexdigest()
        scope.store.finish(session,'upload_pending',result=session['result']);scope.session_job=None
    rows=scope.store.query("SELECT * FROM audit WHERE action='root_technical_reference_acceptance'")
    assert len(rows)==(0 if changed else 1)


def test_existing_completed_and_unknown_prepare_only_reopen_evidence_without_claim_or_models(scoped,monkeypatch):
    scope,parent,source_id,grant=scoped;job=scope.store.get(parent)
    with scope.store.transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',
        (encode({**job['result'],'machine_web':{'manifest_sha256':'5'*64}}),parent))
    manifest={'render':{'sha256':'6'*64},'caption':'Owned prepared evidence'}
    service=SimpleNamespace(kind='dynamic_website_machine_v4_api_relations',_snapshot=lambda ident:(scope.store.get(ident),manifest),
        fresh=lambda *a:pytest.fail('Existing publication must not reacquire dispatch authority'))
    monkeypatch.setattr(m,'machine',lambda s:service)
    monkeypatch.setattr(m,'held_claim',lambda *a,**k:pytest.fail('No new claim on known result'))
    for state in ('completed','unknown'):
        with scope.store.transaction() as db:
            db.execute('INSERT OR REPLACE INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                ('owned-known',parent,'bilibili','x',source_id,'site_uid:'+scope.support.SITE_UID,service.kind,state,None,None,time.time()))
        result=m.prepare(scope,source_id)
        assert result['paid_model_calls_started_by_resume']==0 and result['publication']['state']==state
    assert scope.store.get(parent)['attempts']==0 and not scope.store.query('SELECT * FROM api_calls')


def test_expired_unknown_dispatch_is_lookup_only_and_never_reserves(scoped,monkeypatch):
    scope,parent,source_id,grant=scoped;grant['expires_at']=time.time()-1
    job=scope.store.get(parent);manifest={'source_url':job['payload']['url'],'target_site_uid':scope.support.SITE_UID,
        'target_name':'Owned target','render':{'png':str(scope.settings.root/'media'/parent/'owned.png'),'sha256':'7'*64},
        'caption':'Owned '+job['payload']['url'],'caption_sha256':'8'*64}
    service=SimpleNamespace(kind='dynamic_website_machine_v4_api_relations',version=m.POLICY,
        _snapshot=lambda ident:(scope.store.get(ident),manifest),reserve=lambda *a,**k:pytest.fail('Unknown cannot reserve'),
        fresh=lambda *a,**k:pytest.fail('Lookup must not require new age exception'))
    monkeypatch.setattr(m,'machine',lambda s:service)
    with scope.store.transaction() as db:db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        ('owned-unknown',parent,'bilibili','x',source_id,'site_uid:'+scope.support.SITE_UID,service.kind,'unknown',None,None,time.time()))
    seen=[]
    class Input(io.BytesIO):
        def close(self):
            spec=json.loads(self.getvalue());seen.append(spec)
            file=scope.settings.root/'media'/parent/'website-attempts'/spec['attempt_id']/'result.json'
            file.write_text(encode({'state':'not_found_result_unknown','sent':False}),'utf-8');super().close()
    class Process:
        def __init__(self,*a,**k):self.stdin=Input()
        def poll(self):return 0
    monkeypatch.setattr(m.subprocess,'Popen',Process)
    with pytest.raises(PublishUnknown):m.dispatch(scope,source_id,lookup_only=True)
    assert len(seen)==1 and seen[0]['mode']=='lookup' and seen[0]['dispatch_expires_at']<time.time()
    assert scope.store.get(parent)['attempts']==0 and scope.store.query('SELECT state FROM publications')[0]['state']=='unknown'
