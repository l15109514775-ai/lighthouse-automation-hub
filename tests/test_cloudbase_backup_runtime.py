"""Owned SQLite/media fixtures and simulated COS; no real credentials or calls."""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
from types import SimpleNamespace
import uuid

import pytest

from adapters.cloudbase_cos_backup import CloudBaseCOSConfig, CloudBaseCOSStorage
from services.hub.cloudbase_backup_runtime import BackupLedger, BackupLimits, CloudBaseBackupRuntime, OperationalArchive, PrivateCanary, LEDGER_RELATIVE, REQUEST_ALLOWANCE, safe_json
from services.hub.db import Store, encode
from services.hub.errors import HubError

NOW=1793520000


class Response:
    def __init__(self,data=b'',status=200):self.data=io.BytesIO(data);self.status=status
    def read(self,n):return self.data.read(n)
    def getheader(self,key,default=None):return default


class Connection:
    def close(self):pass


class COS:
    def __init__(self,cfg):self.cfg=cfg;self.objects={};self.calls=[];self.credentials_read=0;self.leak=False;self.put_unknown=False;self.fail_archive_get=False;self.put_http_rejected=False
    def credentials(self):self.credentials_read+=1;return {'SecretId':'AKIDMOCKEXAMPLE','SecretKey':'mock-key-no-real-credentials'}
    def request(self,url,hosts,*,method='GET',headers=None,body=None,timeout=20):
        self.calls.append((method,url,bool(headers.get('Authorization'))));key=url.split('/',3)[3]
        if method=='PUT':
            if self.put_http_rejected:return Connection(),Response(b'<Error><Code>RequestTimeout</Code><Message>owned-private-provider-message</Message></Error>',400)
            self.objects[key]=body.read()
            if self.put_unknown:raise TimeoutError('provider raw error must not be returned')
            return Connection(),Response()
        if not headers.get('Authorization'):
            if self.leak and '.tcb.qcloud.la/' in url:return Connection(),Response(self.objects[key])
            return Connection(),Response(b'Access Denied',403)
        if self.fail_archive_get and '/canary/' not in key:raise ConnectionResetError('owned interrupted readback')
        return Connection(),Response(self.objects[key])


def write(path,data):
    if isinstance(data,str):data=data.encode()
    path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data);return hashlib.sha256(data).hexdigest()


@pytest.fixture
def environment(tmp_path,monkeypatch):
    root=tmp_path/'hub';root.mkdir();(root/'data').mkdir();store=Store(root/'data/hub.sqlite3');store.init()
    job=store.enqueue('x','12345','tweet_process','tweet',{'text':'public original fixture'},'test-policy')
    folder=root/'media'/job;png=folder/'translated.png';original=folder/'original-photo.bin'
    ph=write(png,b'\x89PNG\r\n\x1a\nowned translated fixture');oh=write(original,b'\xff\xd8\xffowned original JPEG fixture')
    payload={'text':'public original fixture'};source_sha=write(folder/'source.json',encode(payload).encode())
    model=store.enqueue('machine_model',job+':post','machine_translation_node','tweet',{},'test-policy')
    with store.transaction() as db:db.execute("UPDATE jobs SET state='completed',result=? WHERE job_id=?",(encode({'parent_job_id':job}),model))
    raw_file=root/'data/model-receipts'/model/(str(uuid.uuid4())+'.json')
    raw_sha=write(raw_file,encode({'choices':[{'text':'owned translation'}],'usage':{'total_tokens':1}}))
    model_proof=folder/'model-proof.json';model_sha=write(model_proof,encode({'receipts':[{'file':str(raw_file),'sha256':raw_sha}]}))
    source_proof=folder/'source-proof.json';source_proof_sha=write(source_proof,encode({'source_payload':{'file':str(folder/'source.json'),'sha256':source_sha}}))
    ticket=folder/'ticket.json';ticket_sha=write(ticket,encode({'nodes':[{'source_evidence':{'file':str(source_proof),'sha256':source_proof_sha},
        'model_evidence':{'file':str(model_proof),'sha256':model_sha}}]}))
    manifest={'render':{'png':str(png),'sha256':ph,'ticket_file':str(ticket),'ticket_sha256':ticket_sha},'media':[{'file':str(original),'sha256':oh}]}
    mh=write(folder/'manifest.json',encode(manifest).encode())
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='completed',result=? WHERE job_id=?",(encode({'manifest_file':str(folder/'manifest.json'),'manifest_sha256':mh}),job))
        db.execute("INSERT INTO bilibili_credentials(account_id,app_id,generation,encrypted_tokens,access_expires_at,state,updated_at) VALUES('acct','app',1,'private-oauth-token-needle',1,'auth_expired',1)")
        db.execute("INSERT INTO subscriptions VALUES('sub','owner','task.failed','{}','https://example.test','private-event-secret-needle',1,1,0)")
        db.execute("INSERT INTO audit(at,principal,action,target,outcome) VALUES(1,'test','test','test',?)",(encode({'apiKey':'private-json-key-needle',
            'SecretId':'private-secret-id-needle','SecretKey':'private-secret-key-needle','count':2}),))
    discovery=store.enqueue('x','discover','x_discovery','tweet',{},'test-policy');call=str(uuid.uuid4())
    with store.transaction() as db:db.execute("UPDATE jobs SET state='completed' WHERE job_id=?",(discovery,))
    body=encode({'data':[{'id':'12345','text':'public fixture'}],'meta':{'result_count':1,'next_token':'PUBLIC_PAGINATION_CURSOR'}}).encode()
    body_sha=write(root/'media'/discovery/'source-receipts'/(call+'.json'),body)
    shot=root/'media'/discovery/'screenshot.png';shot_sha=write(shot,b'\x89PNG\r\n\x1a\nowned source screenshot fixture')
    observation=folder/'web-observation.json';obs_sha=write(observation,encode({'screenshot_file':str(shot),'payload':{'capture':{'screenshot_sha256':shot_sha}}}))
    packet=root/'media'/discovery/'extended-packet.json';packet_sha=write(packet,encode({'public_fixture':True}))
    with store.transaction() as db:
        db.execute("INSERT INTO api_calls(call_id,provider,job_id,started_at,cost_usd,state,units) VALUES(?,?,?,1,0,'done',1)",(call,'x',discovery))
        db.execute('INSERT INTO source_http_receipts VALUES(?,?,?,?,?,?,?,?,?,?)',(call,discovery,'GET','api.x.com','/2/users/1/tweets',
            encode({'pagination_token':'PUBLIC_PAGINATION_CURSOR'}),body_sha,body_sha,'default_tls',1))
        db.execute('INSERT INTO tweet_source_bindings VALUES(?,?,?,?,?,?,?)',(str(uuid.uuid4()),job,discovery,call,call,source_sha,1))
        db.execute('INSERT INTO web_source_bindings VALUES(?,?,?,?,?,?,?,?)',(job,discovery,source_sha,str(observation),obs_sha,shot_sha,1,1))
        db.execute('INSERT INTO extended_web_source_bindings VALUES(?,?,?,?,?,?,?)',(job,discovery,source_sha,str(packet),packet_sha,1,1))
    previous=store.enqueue('machine_model',job+':post:old','machine_translation_node','tweet',{},'test-policy')
    with store.transaction() as db:db.execute("UPDATE jobs SET state='failed',result=? WHERE job_id=?",(encode({'parent_job_id':job}),previous))
    for owner,label in ((model,'selected'),(previous,'rejected')):
        for phase in ('translation','quality'):
            call=str(uuid.uuid4());request_key=str(uuid.uuid4())
            raw_sha=write(root/'data/model-receipts'/owner/(call+'.json'),encode({'fixture':label,'phase':phase,'public_model_response':True,
                'output':[{'internal_chat_message_metadata_passthrough':{'content_token_count':15575}}]}))
            with store.transaction() as db:
                db.execute('INSERT INTO model_requests VALUES(?,?,?,NULL)',(request_key,'done','{}'))
                db.execute("INSERT INTO api_calls(call_id,provider,job_id,started_at,cost_usd,state,units) VALUES(?,?,?,1,0,'done',1)",(call,'model',owner))
                db.execute('INSERT INTO model_call_receipts VALUES('+','.join('?'*18)+')',
                    (request_key,call,owner,phase,'fixture-contract','chat-completions','https://example.test','fixture-model','fixture-model',128,
                    '0'*64,'0'*64,'0'*64,'0'*64,raw_sha,'0'*64,'default_tls',1))
    pending=store.enqueue('x','pending','tweet_process','tweet',{},'test-policy')
    write(root/'media'/pending/'pending.png',b'\x89PNG\r\n\x1a\npending private fixture')
    video=store.enqueue('youtube','abcdefghijk','youtube_download','video',{},'test-policy')
    write(root/'media'/video/'original.mp4',b'large-video-fixture-must-not-be-exported')
    write(root/'data/browser-profiles/youtube/Default/Cookies',b'private-browser-cookie-needle')
    write(root/'secrets/secret.env',b'private-env-key-needle')
    write(root/'logs/app.log',(json.dumps({'level':'ERROR','queue':'tweet','error_code':'TEST_FAILURE','message':'Authorization: private-log-key-needle'})+'\n'+'Cookie: private-unstructured-needle\n').encode())
    bucket='example-backup-1250000000';host=bucket+'.cos.ap-shanghai.myqcloud.com'
    cfg=CloudBaseCOSConfig(enabled=True,env_id='example-env',storage_mode='classic',bucket=bucket,region='ap-shanghai',
        owner_app_id='1250000000',storage_hosts=(host,),existing_resource_authorized=True,private_storage_verified=True,
        max_archive_bytes=8000000,cloudbase_storage_binding_reference='owned fixture only')
    provider=COS(cfg);key=cfg.object_prefix+'/canary/'+str(uuid.uuid4())+'.json';body=b'non-sensitive owned canary'
    provider.objects[key]=body;proofdir=root/'data/canary-proof'
    write(proofdir/'intent.json',encode({'object_key':key,'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest(),'sensitive_data':False}))
    proof={'version':'cloudbase-canary-v1','private_storage_verified':True,'object_key':key,
        'at':datetime.fromtimestamp(NOW,timezone.utc).isoformat(),'observations':[
        {'host':host,'method':'GET','signed':True,'status':200,'same_canary_bytes':True},
        {'host':host,'method':'GET','signed':False,'status':403,'same_canary_bytes':False},
        {'host':bucket+'.tcb.qcloud.la','method':'GET','signed':False,'status':403,'same_canary_bytes':False}]}
    sha=write(proofdir/'result.json',encode(proof))
    clock=[NOW]
    storage=CloudBaseCOSStorage(cfg,provider.credentials,request=provider.request,clock=lambda:clock[0])
    monkeypatch.setattr('services.hub.cloudbase_backup_runtime.shutil.disk_usage',lambda p:SimpleNamespace(free=100_000_000_000))
    def runtime(limits=BackupLimits(),config=cfg):
        return CloudBaseBackupRuntime(root,config,limits,private_canary_reference=str(proofdir/'result.json'),
            private_canary_sha256=sha,policy_sha256='1'*64,storage=storage,clock=lambda:clock[0])
    return SimpleNamespace(root=root,store=store,job=job,pending=pending,video=video,provider=provider,cfg=cfg,
        storage=storage,clock=clock,runtime=runtime,proofdir=proofdir,proof=proof,proof_sha256=sha)


def unpack_bundle(bundle):
    with tarfile.open(bundle['archive']) as tar:return {m.name:tar.extractfile(m).read() for m in tar.getmembers()}


def test_snapshot_is_consistent_credential_excluded_and_contains_original_and_translated_tweet_images(environment,tmp_path):
    e=environment;before=e.store.get(e.job);bundle=OperationalArchive(e.root,BackupLimits()).build(str(uuid.uuid4()));files=unpack_bundle(bundle)
    names=set(files);assert any(n.endswith('original-photo.bin') for n in names) and any(n.endswith('translated.png') for n in names)
    assert any(n.endswith('ticket.json') for n in names) and any('/data/model-receipts/' in n for n in names)
    assert any(n.endswith('web-observation.json') for n in names) and any(n.endswith('screenshot.png') for n in names)
    assert any(n.endswith('extended-packet.json') for n in names)
    assert not any('original.mp4' in n or 'pending.png' in n or 'browser-profiles' in n or 'secret.env' in n for n in names)
    manifest=json.loads(files['MANIFEST.json']);assert manifest['snapshot']['kind']=='credential-excluded operational snapshot'
    assert manifest['database_operational_log_records']>=1 and manifest['directory_json_reports_captured'] is False
    assert json.loads(files['logs/operations.jsonl'].splitlines()[0])['source_table']=='audit'
    assert manifest['snapshot']['excluded_tables']['bilibili_credentials']==1
    copy=tmp_path/'restore.sqlite3';copy.write_bytes(files['database/credential-excluded-operational.sqlite3'])
    with sqlite3.connect(copy) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert db.execute('SELECT COUNT(*) FROM bilibili_credentials').fetchone()[0]==0
        assert db.execute('SELECT active FROM subscriptions').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM jobs WHERE job_id=?',(e.pending,)).fetchone()[0]==1
    everything=b''.join(files.values())
    for secret in (b'private-oauth-token-needle',b'private-event-secret-needle',b'private-json-key-needle',b'private-secret-id-needle',b'private-secret-key-needle',b'private-log-key-needle',b'private-unstructured-needle',b'private-browser-cookie-needle'):
        assert secret not in everything
    assert b'PUBLIC_PAGINATION_CURSOR' in everything
    assert b'"fixture":"rejected"' in everything and b'"fixture":"selected"' in everything
    assert b'"content_token_count":15575' in everything
    assert e.store.get(e.job)==before and e.store.query('SELECT encrypted_tokens FROM bilibili_credentials')[0]['encrypted_tokens']=='private-oauth-token-needle'
    assert (e.root/'media'/e.video/'original.mp4').exists()
    for name,item in manifest['files'].items():assert hashlib.sha256(files[name]).hexdigest()==item['sha256']


def test_completed_tweet_hash_mismatch_stops_export(environment):
    e=environment;write(e.root/'media'/e.job/'original-photo.bin',b'changed bytes')
    with pytest.raises(HubError,match='CLOUDBASE_BACKUP_EVIDENCE_HASH_MISMATCH'):
        OperationalArchive(e.root,BackupLimits()).build(str(uuid.uuid4()))


def cached_user_receipt_fixture(e,*,host='api.x.com',method='GET'):
    owner=e.store.enqueue('maintenance','owned-cached-user','x_discovery','tweet',{},'test-policy');call=str(uuid.uuid4())
    file=e.root/'media'/owner/'source-receipts'/(call+'.json');body=encode({'data':{'id':'1','username':'owned-public-user'}}).encode();sha=write(file,body)
    with e.store.transaction() as db:
        db.execute("UPDATE jobs SET state='completed' WHERE job_id=?",(owner,))
        db.execute("INSERT INTO api_calls(call_id,provider,job_id,started_at,cost_usd,state,units) VALUES(?,?,?,1,0,'done',1)",(call,'x',owner))
        db.execute('INSERT INTO source_http_receipts VALUES(?,?,?,?,?,?,?,?,?,?)',(call,owner,method,host,'/2/users/by/username/owned-public-user','{}',sha,sha,'default_tls',1))
        db.execute('UPDATE tweet_source_bindings SET user_call_id=? WHERE job_id=?',(call,e.job))
    return owner,file


def test_exact_cached_user_receipt_from_prior_maintenance_discovery_is_preserved(environment):
    e=environment;owner,file=cached_user_receipt_fixture(e)
    bundle=OperationalArchive(e.root,BackupLimits()).build(str(uuid.uuid4()));files=unpack_bundle(bundle)
    assert files['tweets/files/'+file.relative_to(e.root).as_posix()]==file.read_bytes()


def test_exact_cached_receipt_does_not_authorize_other_files_of_same_old_owner(environment):
    e=environment;owner,file=cached_user_receipt_fixture(e)
    unrelated=e.root/'media'/owner/'unrelated.json';sha=write(unrelated,encode({'owned_unrelated':True}))
    result=e.store.get(e.job)['result'];result['unrelated']={'file':str(unrelated),'sha256':sha}
    with e.store.transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(result),e.job))
    with pytest.raises(HubError,match='CLOUDBASE_BACKUP_EVIDENCE_OWNER_REJECTED'):
        OperationalArchive(e.root,BackupLimits()).build(str(uuid.uuid4()))


@pytest.mark.parametrize('value',[-1,True,'15575',10_000_001])
def test_model_passthrough_token_count_exception_requires_bounded_nonnegative_integer(value):
    raw={'output':[{'internal_chat_message_metadata_passthrough':{'content_token_count':value}}]}
    assert safe_json(raw,public_cursor='model_response')!=raw


def test_model_passthrough_count_is_preserved_only_in_hash_bound_model_response_schema():
    raw={'output':[{'internal_chat_message_metadata_passthrough':{'content_token_count':15575}}]}
    assert safe_json(raw,public_cursor='model_response')==raw
    assert safe_json(raw)!=raw
    for raw in ({'content_token_count':15575},{'output':[{'content_token_count':15575}]},
        {'output':[{'internal_chat_message_metadata_passthrough':{'access_token':'owned-fake-auth-needle'}}]}):
        assert safe_json(raw,public_cursor='model_response')!=raw


def audit_source_fixture(e,source=b'// Owned historical audit fixture; never execute.\nexport const mode = "lookup";\n',*,version='supervised-web-nonacceptance-v1',name='dispatch-implementation.mjs'):
    folder=e.root/'media'/e.job/'supervised-review-v1/nonacceptance-v1'
    dispatcher=folder/name;sha=write(dispatcher,source)
    proof={'version':version,'job_id':e.job,'observer_kind':'operator_transcribed_browser_evidence',
        'basis':'incomplete_upload_confirmation_left_unconfirmed','final_confirmation_not_clicked':True,
        'dispatch_implementation':{'file':str(dispatcher),'sha256':sha}}
    proof_file=folder/'proof.json';proof_sha=write(proof_file,encode(proof))
    result=e.store.get(e.job)['result'];result['web_nonacceptance']={'file':str(proof_file),'sha256':proof_sha}
    with e.store.transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(result),e.job))
    return dispatcher,proof_file


def test_exact_historical_nonacceptance_dispatch_source_is_hash_bound_audit_only(environment):
    e=environment;dispatcher,proof=audit_source_fixture(e)
    bundle=OperationalArchive(e.root,BackupLimits()).build(str(uuid.uuid4()));files=unpack_bundle(bundle)
    name='tweets/files/'+dispatcher.relative_to(e.root).as_posix()
    assert files[name]==dispatcher.read_bytes()
    record=bundle['manifest']['non_executable_audit_sources'][0]
    assert record['proof_file']==proof.relative_to(e.root).as_posix()
    assert record['proof_sha256']==hashlib.sha256(proof.read_bytes()).hexdigest()
    assert record['execute_or_import_authorized'] is False and record['restore_mode']=='0600'
    with tarfile.open(bundle['archive']) as tar:assert tar.getmember(name).mode==0o600


@pytest.mark.parametrize('options,code',[
    ({'version':'unattested-fixture'},'CLOUDBASE_BACKUP_EVIDENCE_TYPE_REJECTED'),
    ({'name':'arbitrary-script.mjs'},'CLOUDBASE_BACKUP_EVIDENCE_TYPE_REJECTED'),
    ({'source':b'const SecretKey = "owned-fake-key-must-not-be-archived";'},'CLOUDBASE_BACKUP_AUDIT_SOURCE_CONTAINS_CREDENTIAL'),
    ({'source':b'const h = "Bearer owned-fake-token-at-least-twelve";'},'CLOUDBASE_BACKUP_AUDIT_SOURCE_CONTAINS_CREDENTIAL'),
    ({'source':b'\xff\xfe\x00'},'CLOUDBASE_BACKUP_AUDIT_SOURCE_ENCODING_INVALID'),
    ({'source':b'// '+b'x'*65536},'CLOUDBASE_BACKUP_AUDIT_SOURCE_SIZE_LIMIT'),
])
def test_audit_source_exception_does_not_allow_unattested_code_or_literal_credentials(environment,options,code):
    e=environment;audit_source_fixture(e,**options)
    with pytest.raises(HubError,match=code):OperationalArchive(e.root,BackupLimits()).build(str(uuid.uuid4()))
    assert not e.provider.calls


def test_video_or_profile_reference_is_rejected_even_from_completed_tweet(environment):
    e=environment;video=e.root/'media'/e.video/'original.mp4'
    with e.store.transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode({'file':str(video),'sha256':hashlib.sha256(video.read_bytes()).hexdigest()}),e.job))
    with pytest.raises(HubError,match='CLOUDBASE_BACKUP_VIDEO_OR_PROFILE_REFERENCE_REJECTED'):
        OperationalArchive(e.root,BackupLimits()).build(str(uuid.uuid4()))


def test_disabled_or_not_private_stops_before_secret_access_and_network(environment):
    e=environment
    for cfg in (replace(e.cfg,enabled=False),replace(e.cfg,private_storage_verified=False)):
        with pytest.raises(HubError):e.runtime(config=cfg).run_once()
    assert not e.provider.calls and e.provider.credentials_read==0
    assert not (e.root/LEDGER_RELATIVE).exists()


def test_prepare_only_is_local_and_is_not_a_verified_or_prunable_backup(environment):
    e=environment;result=e.runtime(config=replace(e.cfg,enabled=False)).run_once(prepare_only=True)
    assert result['external_requests']==0 and result['remote_verified'] is False and result['prune_candidate'] is None
    assert not e.provider.calls and Path(result['archive']).exists()


def test_runtime_does_three_live_privacy_checks_then_full_two_object_roundtrip(environment):
    e=environment;result=e.runtime().run_once()
    assert [m for m,_,_ in e.provider.calls]==['GET','GET','GET','PUT','GET','PUT','GET']
    assert result['remote']['remote_verified'] and result['fresh_private_canary']['private_storage_verified']
    assert result['prune_candidate']['additional_retention_policy_required'] and result['source_files_deleted'] is False
    assert Path(result['bundle']['archive']).exists()
    assert result['budget']['daily'][0]['requests']==7
    assert result['budget']['daily'][0]['reserved_network_bytes']<20_000_000
    assert result['budget']['pricing']['currency']=='CNY' and result['budget']['pricing']['rates']['internet_egress_cny_per_gb']==0.5
    assert result['budget']['pricing']['package_discount_verified'] is False and result['budget']['pricing']['stored_bytes_upper_bound']>0
    assert result['budget']['daily'][0]['list_price_upper_estimate_cny']['total']>0
    assert result['budget']['latest_verified']['age_seconds']==0 and result['budget']['automatic_failure_notification_integrated'] is False


def test_public_cdn_regression_never_uploads_sensitive_backup(environment):
    e=environment;e.provider.leak=True
    with pytest.raises(HubError,match='CLOUDBASE_BACKUP_ANONYMOUS_READ_NOT_DENIED'):e.runtime().run_once()
    assert [m for m,_,_ in e.provider.calls]==['GET','GET','GET']
    assert not list((e.root/'backups/cloudbase-receipts').glob('*.verified.json'))


def test_bootstrap_cdn_200_evidence_is_rejected_even_if_its_boolean_is_wrongly_true(environment):
    e=environment;proof=dict(e.proof);proof['observations']=[*e.proof['observations'][:2],{**e.proof['observations'][2],'status':200,'same_canary_bytes':True}]
    sha=write(e.proofdir/'result.json',encode(proof));checker=PrivateCanary(e.root,e.cfg,str(e.proofdir/'result.json'),sha,clock=lambda:NOW)
    with pytest.raises(HubError,match='CLOUDBASE_BACKUP_PRIVATE_CANARY_INVALID'):checker.load()
    assert not e.provider.calls


def test_old_bootstrap_is_rechecked_live_instead_of_permanently_disabling_next_day(environment):
    e=environment;e.clock[0]+=3*86400;result=e.runtime().run_once()
    fresh=json.loads(Path(result['fresh_private_canary']['file']).read_bytes())
    assert datetime.fromisoformat(fresh['checked_at']).timestamp()==e.clock[0]
    assert len(e.provider.calls)==7


def test_full_run_budget_preflight_stops_before_first_external_request(environment):
    e=environment;limits=replace(BackupLimits(),daily_requests=6)
    with pytest.raises(HubError,match='CLOUDBASE_BACKUP_BUDGET_EXCEEDED'):e.runtime(limits=limits).run_once()
    assert not e.provider.calls and e.provider.credentials_read==0


def test_byte_budget_preflight_stops_before_first_external_request(environment):
    e=environment;limits=replace(BackupLimits(),daily_network_bytes=1_000_000)
    with pytest.raises(HubError,match='CLOUDBASE_BACKUP_BUDGET_EXCEEDED'):e.runtime(limits=limits).run_once()
    assert not e.provider.calls


def test_daily_and_monthly_request_budget_survive_database_reopen(environment):
    e=environment;limits=replace(BackupLimits(),daily_requests=7,monthly_requests=7)
    e.runtime(limits=limits).run_once()
    with pytest.raises(HubError,match='CLOUDBASE_BACKUP_BUDGET_EXCEEDED'):e.runtime(limits=limits).run_once()
    assert len(e.provider.calls)==7
    e.clock[0]+=86400
    with pytest.raises(HubError,match='CLOUDBASE_BACKUP_BUDGET_EXCEEDED'):e.runtime(limits=limits).run_once()
    assert len(e.provider.calls)==7


def test_natural_beijing_day_boundary_is_used_for_budget(environment):
    e=environment;limits=replace(BackupLimits(),daily_requests=7,monthly_requests=200)
    e.clock[0]=datetime(2026,11,1,15,59,tzinfo=timezone.utc).timestamp();e.runtime(limits=limits).run_once()
    e.clock[0]+=120;e.runtime(limits=limits).run_once()
    days=BackupLedger(e.root/LEDGER_RELATIVE,limits).status()['daily']
    assert [x['day'] for x in days]==['2026-11-01','2026-11-02'] and all(x['requests']==7 for x in days)


def test_put_unknown_preserves_budget_archive_and_blocks_blind_new_upload(environment):
    e=environment;e.provider.put_unknown=True
    with pytest.raises(HubError,match='CLOUDBASE_BACKUP_UPLOAD_UNKNOWN'):e.runtime().run_once()
    count=len(e.provider.calls)
    with pytest.raises(HubError,match='CLOUDBASE_BACKUP_RECONCILIATION_REQUIRED'):e.runtime().run_once()
    assert len(e.provider.calls)==count==4
    status=BackupLedger(e.root/LEDGER_RELATIVE,BackupLimits()).status()
    assert status['operations'][0]['state']=='upload_unknown' and status['daily'][0]['requests']==4
    assert Path(status['operations'][0]['archive']).exists()


def test_confirmed_archive_put_then_failed_readback_requires_reconciliation_before_new_uuid(environment):
    e=environment;e.provider.fail_archive_get=True
    with pytest.raises(HubError,match='CLOUDBASE_BACKUP_NETWORK_ERROR'):e.runtime().run_once()
    count=len(e.provider.calls)
    with pytest.raises(HubError,match='CLOUDBASE_BACKUP_RECONCILIATION_REQUIRED'):e.runtime().run_once()
    assert len(e.provider.calls)==count==5
    status=BackupLedger(e.root/LEDGER_RELATIVE,BackupLimits()).status()
    assert status['operations'][0]['state']=='upload_unknown'
    assert status['last_error']['error_code']=='CLOUDBASE_BACKUP_NETWORK_ERROR'


def test_ambiguous_put_http_failure_is_persisted_safely_and_blocks_next_write(environment):
    e=environment;e.provider.put_http_rejected=True
    with pytest.raises(HubError,match='CLOUDBASE_BACKUP_UPLOAD_UNKNOWN'):e.runtime().run_once()
    with pytest.raises(HubError,match='CLOUDBASE_BACKUP_RECONCILIATION_REQUIRED'):e.runtime().run_once()
    assert len(e.provider.calls)==4
    status=BackupLedger(e.root/LEDGER_RELATIVE,BackupLimits()).status();assert status['operations'][0]['state']=='upload_unknown'
    files=list((e.root/'data/cloudbase-backup-state/http-failures').glob('*.json'));assert len(files)==1
    value=json.loads(files[0].read_bytes());assert value['response']=={'http_status':400,'provider_code':'RequestTimeout'}
    assert value['cause_code']=='CLOUDBASE_BACKUP_HTTP_REJECTED' and value['reconciliation_required'] is True
    assert value['raw_response_or_credentials_recorded'] is False and b'owned-private-provider-message' not in files[0].read_bytes()


def test_low_disk_pauses_archive_creation_without_modifying_production(environment,monkeypatch):
    e=environment;monkeypatch.setattr('services.hub.cloudbase_backup_runtime.shutil.disk_usage',lambda p:SimpleNamespace(free=15_000_000_001))
    with pytest.raises(HubError,match='DISK_LOW'):OperationalArchive(e.root,BackupLimits()).build(str(uuid.uuid4()))
    assert e.store.get(e.job)['state']=='completed' and not e.provider.calls


def test_sqlite_backup_includes_committed_wal_but_not_an_uncommitted_writer(environment,tmp_path):
    e=environment;writer=e.store.connect();writer.execute('BEGIN IMMEDIATE')
    writer.execute("INSERT INTO audit(at,principal,action,target,outcome) VALUES(2,'test','uncommitted','test','private not committed')")
    try:bundle=OperationalArchive(e.root,BackupLimits()).build(str(uuid.uuid4()))
    finally:writer.rollback();writer.close()
    copy=tmp_path/'wal-restore.sqlite3';copy.write_bytes(unpack_bundle(bundle)['database/credential-excluded-operational.sqlite3'])
    with sqlite3.connect(copy) as db:
        assert db.execute("SELECT COUNT(*) FROM audit WHERE action='uncommitted'").fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM jobs WHERE job_id=?',(e.job,)).fetchone()[0]==1


def test_budget_reservation_is_exclusive_under_concurrent_callers(environment):
    from concurrent.futures import ThreadPoolExecutor
    e=environment;ledger=BackupLedger(e.root/LEDGER_RELATIVE,BackupLimits(),clock=lambda:NOW)
    op=str(uuid.uuid4());ledger.start(op,'1'*64);ledger.finish(op,'uploading');ledger.preflight(op,network_bytes=REQUEST_ALLOWANCE+10,requests=1)
    def reserve():
        try:ledger.reserve(op,'PUT',10);return True
        except HubError:return False
    with ThreadPoolExecutor(max_workers=2) as pool:assert sorted(pool.map(lambda _:reserve(),range(2)))==[False,True]
    assert ledger.status()['daily'][0]['requests']==1


def test_month_boundary_does_not_erase_history_and_new_month_has_new_cap(environment):
    e=environment;limits=replace(BackupLimits(),daily_requests=7,monthly_requests=7)
    e.clock[0]=datetime(2026,11,30,15,59,tzinfo=timezone.utc).timestamp();e.runtime(limits=limits).run_once()
    e.clock[0]+=120;e.runtime(limits=limits).run_once()
    months=BackupLedger(e.root/LEDGER_RELATIVE,limits).status()['monthly']
    assert [x['month'] for x in months]==['2026-11','2026-12'] and all(x['requests']==7 for x in months)


def test_cli_missing_config_and_disabled_template_do_not_read_credentials(environment):
    e=environment;script=Path(__file__).resolve().parents[1]/'scripts/cloudbase-backup.py'
    result=subprocess.run([sys.executable,str(script),'--root',str(e.root),'--status'],capture_output=True,text=True,check=True)
    assert json.loads(result.stdout)['enabled'] is False
    template=Path(__file__).resolve().parents[1]/'config/cloudbase-backup-runtime.example.toml'
    policy=e.root/'disabled-backup.toml'
    policy.write_bytes(template.read_bytes())
    policy.chmod(0o600)
    command=[sys.executable,str(script),'--root',str(e.root),'--config',str(policy),'--run-once']
    result=subprocess.run(command,capture_output=True,text=True)
    assert result.returncode==2 and json.loads(result.stdout)['error_code']=='CLOUDBASE_BACKUP_DISABLED'
    if os.name=='posix':
        policy.chmod(0o660)
        result=subprocess.run(command,capture_output=True,text=True)
        assert result.returncode==2 and json.loads(result.stdout)['error_code']=='CLOUDBASE_BACKUP_CONFIG_NOT_PRIVATE'
    assert not (e.root/LEDGER_RELATIVE).exists()


def test_interrupted_pre_upload_operation_recovers_without_refunding_any_recorded_get(environment):
    e=environment;ledger=BackupLedger(e.root/LEDGER_RELATIVE,BackupLimits(),clock=lambda:NOW)
    op=str(uuid.uuid4());ledger.start(op,'1'*64);ledger.finish(op,'uploading');ledger.preflight(op,network_bytes=REQUEST_ALLOWANCE+100,requests=1)
    ledger.reserve(op,'GET',100)
    ledger.recover_after_exclusive_lock()
    assert ledger.status()['operations'][0]['state']=='interrupted_before_upload'
    assert ledger.status()['daily'][0]['reserved_network_bytes']==REQUEST_ALLOWANCE+100
    ledger.start(str(uuid.uuid4()),'1'*64)
