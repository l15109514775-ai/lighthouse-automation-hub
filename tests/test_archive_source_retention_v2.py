"""Real owned SQLite/tar/files; mocked remote receipts, no network or accounts."""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import uuid

import pytest

from adapters.cloudbase_backup import encoded
from services.hub import archive_source_retention_v2 as module
from services.hub.archive_source_retention_v2 import ArchiveSourceRetentionV2 as Retention
from services.hub.cloudbase_backup_runtime import BackupLimits,OperationalArchive
from services.hub.db import encode
from services.hub.errors import HubError
from scripts.ops import prune_cloudbase_verified_clones_v1 as pruner


def sha(data):return hashlib.sha256(data).hexdigest()


def put(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(data if isinstance(data,bytes) else encoded(data));path.chmod(0o600)
    return path


def acceptance(settings,bundle):
    """Owned protocol fixture. Its archive/member/SQLite checks are real."""
    root=settings.root;op=bundle['manifest']['operation_id'];accept=str(uuid.uuid4());remoteop=str(uuid.uuid4())
    area=root/'backups/cloudbase-restore-check'/accept;area.mkdir(parents=True)
    archive=Path(bundle['archive']);body=archive.read_bytes();checksum=sha(body)
    now=datetime.now(timezone.utc).isoformat();key=pruner.PREFIX+'/'+remoteop+'/'+checksum+'.archive';mkey=pruner.PREFIX+'/'+remoteop+'/manifest.json'
    receipt={'version':'cloudbase-backup-v1','operation_id':remoteop,'env_id':pruner.ENV,'kind':'database_snapshot',
        'local_archive':archive.relative_to(root).as_posix(),'archive_key':key,'bytes':len(body),'sha256':checksum,
        'created_at':now,'remote_verified':True,'local_deletion_performed':False,'archive_file_id':'cos://'+pruner.BUCKET+'/'+key,
        'archive_readback':{'bytes':len(body),'sha256':checksum,'method':'full_remote_get'}}
    wire=encoded(receipt);receipt.update(manifest_key=mkey,manifest_file_id='cos://'+pruner.BUCKET+'/'+mkey,
        manifest_sha256=sha(wire),manifest_readback={'bytes':len(wire),'sha256':sha(wire),'method':'full_remote_get'},
        verified_at=now,verification_method='archive_and_manifest_full_remote_get')
    receiptfile=put(root/'backups/cloudbase-receipts'/(remoteop+'.verified.json'),receipt)
    canaryfile=root/'data/cloudbase-backup-canary-checks'/(op+'.json')
    check={'version':'cloudbase-runtime-private-canary-v1','operation_id':op,'checked_at':now,'private_storage_verified':True,
        'canary':{'bytes':106,'sha256':'1'*64},'signed_full_get':{'bytes':106,'sha256':'1'*64,'method':'full_remote_get'},
        'anonymous_observations':[{'host':host,'method':'GET','signed':False,'status':403} for host in
            (pruner.BUCKET+'.cos.ap-shanghai.myqcloud.com',pruner.BUCKET+'.tcb.qcloud.la')]}
    put(canaryfile,check);fresh={'file':str(canaryfile),'sha256':sha(canaryfile.read_bytes()),'private_storage_verified':True}
    remote={**receipt,'receipt_file':str(receiptfile)}
    runtime={'operation_id':op,'remote':remote,'bundle':bundle,'fresh_private_canary':fresh};put(area/'runtime-result.json',runtime)
    copied=put(area/'remote-archive.tar.gz',body);restored=[]
    with tarfile.open(copied) as tar:
        for item in tar.getmembers():
            data=tar.extractfile(item).read();file=put(area/'restored'/item.name,data)
            restored.append({'file':str(file),'bytes':len(data),'sha256':sha(data),'remote_member':item.name})
    manifest=bundle['manifest'];stage=archive.parent
    stagec=[{'file':str(file),'bytes':file.stat().st_size,'sha256':sha(file.read_bytes()),'scope':'this_exact_operation_stage_only'}
        for file in (archive,stage/'credential-excluded-operational.sqlite3',stage/'archive-manifest.json')]
    reportfile=area/'restoration-report.json'
    report={'version':'cloudbase-actual-remote-restore-acceptance-v1','acceptance_id':accept,'operation_id':op,'verified_at':now,
        'actual_archive_get_captured':True,'all_entries_verified':True,'credential_exclusions_verified':True,'additional_remote_gets':0,
        'local_deletions':0,'sqlite_integrity':'ok','sqlite_foreign_keys':'ok','remote_archive_reference':receipt['archive_file_id'],
        'remote_manifest_reference':receipt['manifest_file_id'],'archive':{'file':str(copied),'bytes':len(body),'sha256':checksum},
        'stage_prune_candidates':stagec,'restore_test_prune_candidates':[{'file':str(copied),'bytes':len(body),'sha256':checksum},*restored],
        'entries':len(restored),'keep_files':[str(area/'runtime-result.json'),str(reportfile),str(receiptfile)],'fresh_private_canary':fresh}
    put(reportfile,report)
    ledger={'operation':{'state':'verified','archive':str(archive),'receipt':str(receiptfile),'error_code':None},
        'requests':[{'method':method,'state':'done','error_code':None} for method in ('GET','GET','GET','PUT','GET','PUT','GET')]}
    return reportfile,lambda *_:ledger,copied,ledger


@pytest.fixture
def owned(runtime):
    settings,store=runtime
    ident=store.enqueue('x','owned-tweet-id','tweet_process','tweet',{'text':'Owned original'},'owned-only')
    image=put(settings.root/'media'/ident/'translated.png',b'\x89PNG\r\n\x1a\n'+b'owned image bytes')
    source=put(image.parent/'source.json',{'text':'Owned original'})
    proof=put(image.parent/'proof.json',{'image':{'file':str(image),'sha256':sha(image.read_bytes())}})
    result={'proof':{'file':str(proof),'sha256':sha(proof.read_bytes())}}
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='completed',result=?,external_publish_id='owned-post' WHERE job_id=?",(encode(result),ident))
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',('owned-key',ident,'bilibili','x','owned-tweet-id',
            'site_uid:'+settings.config['target_bilibili_site_uid'],'owned-image','completed','owned-post','https://t.bilibili.com/owned',1))
    sealed=Path(module.seal_structured_logs(settings.root,[{'at':time_now(),'level':'INFO','queue':'tweet',
        'state':'completed','job_id':ident}])['file'])
    active=put(settings.root/'logs/active.log',b'private free text never byte-exact archived\n')
    limits=BackupLimits(disk_floor_bytes=15_000_000_000)
    bundle=OperationalArchive(settings.root,limits).build(str(uuid.uuid4()))
    report,reader,archive,ledger=acceptance(settings,bundle)
    service=Retention(settings,store);registered=service.register_verified_archive(report,ledger_reader=reader)
    authorization=service.authorize(actual_owner_response='Owned synthetic retention instruction; not a real user quote',
        owner_response_reference='Owned fixture authorization only')
    return dict(settings=settings,store=store,id=ident,image=image,source=source,proof=proof,sealed=sealed,active=active,
        limits=limits,report=report,reader=reader,archive=archive,ledger=ledger,service=service,registered=registered,auth=authorization)


def time_now():return datetime.now(timezone.utc).timestamp()


def plan(f,*paths):return f['service'].plan(list(paths or (f['image'],)),authorization_id=f['auth']['authorization_id'])


def release(f,monkeypatch):
    """Owned protocol fixture, never an actual deployment/release grant."""
    monkeypatch.setattr(module,'_root_protected',lambda path:None)  # root filesystem trust boundary only
    if os.name=='nt':
        original=f['service']._unlink_exact
        def windows_owned_readonly_attribute(item):
            if Path(item['file'])==f['sealed']:
                actual=module._inspect(f['settings'].root,f['sealed'])
                assert all(actual[key]==item[key] for key in actual)
                f['sealed'].chmod(0o600)  # Windows owned fixture boundary; Linux unlinks 0400 directly.
            return original(item)
        monkeypatch.setattr(f['service'],'_unlink_exact',windows_owned_readonly_attribute)
    base=Path(module.__file__).resolve().parents[2]
    sources={name:sha((base/name).read_bytes()) for name in module.SOURCE_FILES}
    cases=''.join('<testcase classname="'+name+'" name="owned-protocol-only-'+str(i)+'"/>'
        for name,count in module.REQUIRED_TEST_GROUPS.items() for i in range(count))
    junit=put(f['settings'].root/'data/owned-release-protocol.xml',('<testsuites><testsuite>'+cases+'</testsuite></testsuites>').encode())
    attest=put(f['settings'].root/'data/owned-release-protocol.json',{'version':'archive-source-retention-v2-ubuntu-tests-v1',
        'source_files':sources,'network':'none','application_data_mounted':False,'production_profile_mounted':False,
        'full_suite_passed':True,'oom_killed':False,'no_pid_or_oom_denial':True,
        'runtime_matches_declared_candidate':True,'mounts_match_isolated_allowlist':True,
        'exit_code':0,'junit_file':str(junit),'junit_sha256':sha(junit.read_bytes()),'tests':sum(module.REQUIRED_TEST_GROUPS.values())})
    return put(f['settings'].root/module.RELEASE_RELATIVE,{'version':'archive-source-retention-release-v2',
        'authorization_id':f['auth']['authorization_id'],'mode':'verified_archive_members_only','formal_video_launch_claimed':False,
        'source_files':sources,'test_attestation':{'file':str(attest),'sha256':sha(attest.read_bytes())}})


def test_register_checks_actual_archive_and_retains_exact_remote_member_locator(owned):
    f=owned;reference=f['service'].lookup(f['image'],sha(f['image'].read_bytes()))
    assert f['registered']['mapped_files']==4 and f['registered']['external_requests']==0
    assert reference['location']['member']=='tweets/files/media/'+f['id']+'/translated.png'
    assert reference['archive']['archive_file_id'].startswith('cos://')
    assert f['service'].restore_bytes(reference,f['archive'])==f['image'].read_bytes()
    assert f['store'].get(f['id'])['state']=='completed' and f['image'].exists()


def test_default_dry_run_does_not_delete_or_relabel_old_hold_as_formal_launch(owned):
    p=plan(owned,owned['image'],owned['sealed']);assert len(p['candidates'])==2 and p['source_files_deleted']==0
    assert owned['auth']['formal_workflow_launch_claimed'] is False and all(Path(x['file']).exists() for x in p['candidates'])
    with pytest.raises(HubError,match='ARCHIVE_RETENTION_RESOLVER_NOT_READY'):owned['service'].apply(p)
    assert owned['image'].exists() and owned['sealed'].exists()


def test_owned_apply_restore_and_second_backup_preserve_transitive_recovery(owned,monkeypatch):
    f=owned;release(f,monkeypatch);original=f['image'].read_bytes();publications=f['store'].query('SELECT * FROM publications')
    p=plan(f,f['image'],f['proof'],f['source'],f['sealed']);result=f['service'].apply(p)
    assert result['source_files_deleted']==4 and all(not Path(item['file']).exists() for item in p['candidates'])
    assert f['store'].query('SELECT * FROM publications')==publications and f['store'].get(f['id'])['state']=='completed'
    assert f['service'].apply(p)['source_files_deleted']==4  # reconcile; no second unlink
    bundle=OperationalArchive(f['settings'].root,f['limits']).build(str(uuid.uuid4()))
    assert bundle['manifest']['recovery_requires_prior_archives'] is True and bundle['manifest']['archived_source_files']==4
    with tarfile.open(bundle['archive']) as tar:
        document=json.load(tar.extractfile('archives/source-locations-v2.json'))
        location=next(item for item in document['locations'] if item['relative_file'].endswith('/translated.png'))
        reference={'location':location,'archive':document['archives'][location['archive_sha256']]}
    assert Retention.restore_bytes(reference,f['archive'])==original
    # The old clone tar may subsequently be pruned: the index and new archive
    # still retain its exact COS key/member/hash for a future budgeted GET.
    f['archive'].unlink()
    assert f['service'].verify_registration(f['report'],sha(f['report'].read_bytes()))==f['registered']
    assert f['service'].lookup(f['image'],location['file_sha256'],deleted_only=True)['archive']['archive_file_id']==reference['archive']['archive_file_id']
    assert f['active'].exists()


@pytest.mark.parametrize('reason',['not_completed','publish_unknown','publication_pending','wrong_uid','active_lease','hold','shared','changed','unbacked','raw_log','protected_db','video'])
def test_in_use_unknown_shared_hold_unbacked_or_protected_source_is_rejected(owned,reason):
    f=owned;file=f['image']
    with f['store'].transaction() as db:
        if reason=='not_completed':db.execute("UPDATE jobs SET state='pending' WHERE job_id=?",(f['id'],))
        elif reason=='publish_unknown':db.execute("UPDATE jobs SET state='publish_unknown' WHERE job_id=?",(f['id'],))
        elif reason=='publication_pending':db.execute("UPDATE publications SET state='awaiting_moderation'")
        elif reason=='wrong_uid':db.execute("UPDATE publications SET account_id='site_uid:999'")
        elif reason=='active_lease':db.execute("UPDATE jobs SET lease_owner='owned-active',lease_until=? WHERE job_id=?",(time_now()+60,f['id']))
        elif reason=='hold':
            result=f['store'].get(f['id'])['result'];result['test_retention_policy']={'version':'owned-old-hold','formal_launch_required':True}
            db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(result),f['id']))
    if reason=='shared':
        other=f['store'].enqueue('x','second-owned-tweet','tweet_process','tweet',{'shared_file':str(file)},'owned-only')
        with f['store'].transaction() as db:db.execute("UPDATE jobs SET state='completed' WHERE job_id=?",(other,))
    elif reason=='changed':file.write_bytes(b'changed')
    elif reason=='unbacked':file=put(file.parent/'never-archived.png',b'\x89PNG\r\n\x1a\nnew')
    elif reason=='raw_log':file=f['active']
    elif reason=='protected_db':file=f['settings'].db_path
    elif reason=='video':file=put(file.parent/'original.mp4',b'owned not a real mp4')
    p=plan(f,file);assert not p['candidates'] and len(p['rejected'])==1 and file.exists()


def test_changed_source_after_plan_is_not_unlinked(owned,monkeypatch):
    release(owned,monkeypatch);p=plan(owned);owned['image'].write_bytes(b'changed after plan')
    with pytest.raises(HubError):owned['service'].apply(p)
    assert owned['image'].read_bytes()==b'changed after plan'


def test_new_reference_after_plan_prevents_all_deletions(owned,monkeypatch):
    release(owned,monkeypatch);p=plan(owned,owned['image'],owned['sealed'])
    owned['store'].enqueue('x','new-job','tweet_process','tweet',{'file':str(owned['image'])},'owned-only')
    with pytest.raises(HubError):owned['service'].apply(p)
    assert owned['image'].exists() and owned['sealed'].exists()


def test_symlink_and_hardlinked_originals_are_rejected(owned,monkeypatch):
    original=Path.is_symlink
    monkeypatch.setattr(Path,'is_symlink',lambda p:p==owned['image'] or original(p))
    assert not plan(owned)['candidates']


def test_hardlinked_original_is_not_a_unique_cleanup_candidate(owned):
    second=owned['image'].with_name('shared-hardlink.png');os.link(owned['image'],second)
    assert not plan(owned)['candidates'] and second.exists() and owned['image'].exists()


def test_durable_unlink_unknown_window_reconciles_without_second_unlink(owned,monkeypatch):
    f=owned;release(f,monkeypatch);p=plan(f);original=f['service']._unlink_exact;calls=[]
    def interrupted(item):
        calls.append(item['file']);original(item)
        raise OSError('owned crash after unlink before final database commit')
    monkeypatch.setattr(f['service'],'_unlink_exact',interrupted)
    with pytest.raises(OSError):f['service'].apply(p)
    assert not f['image'].exists() and f['service'].lookup(f['image'],p['candidates'][0]['sha256'])['state']=='deleting'
    result=f['service'].apply(p)
    assert result['source_files_deleted']==1 and calls==[str(f['image'])]


def test_new_source_binding_table_user_blocks_cached_receipt_cleanup(owned):
    f=owned;store=f['store'];discovery=store.enqueue('x','owned-discovery','x_discovery','tweet',{},'owned')
    call=str(uuid.uuid4());http=put(f['settings'].root/'media'/discovery/'source-receipts'/(call+'.json'),{'data':{'id':'owned-user'}})
    job=store.get(f['id']);job['result']['http']={'file':str(http),'sha256':sha(http.read_bytes())}
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='completed' WHERE job_id=?",(discovery,))
        db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(job['result']),f['id']))
        db.execute("INSERT INTO api_calls(call_id,provider,started_at,cost_usd,state) VALUES(?,'owned-api',?,0,'done')",(call,time_now()))
        db.execute('INSERT INTO source_http_receipts VALUES(?,?,?,?,?,?,?,?,?,?)',(call,discovery,'GET','api.x.com','/2/users/owned',
            '{}',sha(http.read_bytes()),'1'*64,'custom_transport',time_now()))
        db.execute('INSERT INTO tweet_source_bindings VALUES(?,?,?,?,?,?,?)',(str(uuid.uuid4()),f['id'],discovery,call,call,'1'*64,time_now()))
    bundle=OperationalArchive(f['settings'].root,f['limits']).build(str(uuid.uuid4()))
    report,reader,*_=acceptance(f['settings'],bundle);f['service'].register_verified_archive(report,ledger_reader=reader)
    assert len(plan(f,http)['candidates'])==1
    other=store.enqueue('x','new-reusing-tweet','tweet_process','tweet',{},'owned')
    with store.transaction() as db:
        db.execute('INSERT INTO tweet_source_bindings VALUES(?,?,?,?,?,?,?)',(str(uuid.uuid4()),other,discovery,call,call,'1'*64,time_now()))
    p=plan(f,http)
    assert not p['candidates'] and p['rejected'][0]['error_code']=='ARCHIVE_RETENTION_SHARED_SOURCE' and http.exists()


def test_sealed_segment_must_remain_immutable_before_archive_export(owned):
    file=owned['sealed'];file.chmod(0o600)
    with pytest.raises(HubError,match='ARCHIVE_RETENTION_UNSEALED_LOG'):
        OperationalArchive(owned['settings'].root,owned['limits']).build(str(uuid.uuid4()))


def test_register_refuses_absent_or_corrupted_same_get_archive(owned):
    f=owned;f['archive'].write_bytes(b'not the actual GET')
    with pytest.raises((HubError,AssertionError)):f['service'].register_verified_archive(f['report'],ledger_reader=f['reader'])


def test_unknown_or_insufficient_request_ledger_does_not_register(owned):
    f=owned;f['ledger']['requests'][3]['state']='unknown'
    with pytest.raises(AssertionError):f['service'].register_verified_archive(f['report'],ledger_reader=f['reader'])


def test_restore_rejects_recomputed_member_or_archive_locator(owned):
    f=owned;reference=f['service'].lookup(f['image'],sha(f['image'].read_bytes()));wrong=deepcopy(reference)
    wrong['location']['member']='database/credential-excluded-operational.sqlite3'
    with pytest.raises(HubError):f['service'].restore_bytes(wrong,f['archive'])
    f['archive'].write_bytes(b'other archive')
    with pytest.raises(HubError):f['service'].restore_bytes(reference,f['archive'])


def test_exact_new_owner_override_is_versioned_and_retains_old_hold(owned):
    f=owned;job=f['store'].get(f['id']);hold={'version':'owned-old-hold','formal_launch_required':True}
    job['result']['test_retention_policy']=hold
    with f['store'].transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(job['result']),f['id']))
    assert not plan(f)['candidates']
    authorization=f['service'].authorize(actual_owner_response='Owned new retention instruction, not a formal workflow launch',
        owner_response_reference='Owned distinct newer owner reference',superseded_holds={f['id']:module.digest([hold])})
    p=f['service'].plan([f['image']],authorization_id=authorization['authorization_id'])
    assert len(p['candidates'])==1 and f['store'].get(f['id'])['result']['test_retention_policy']==hold
    assert authorization['formal_workflow_launch_claimed'] is False


@pytest.mark.parametrize('record',[{'version':'lighthouse-sealed-log-v2','message':'secret free text'},
    {'version':'lighthouse-sealed-log-v2','cookie':'owned-cookie'}, {'version':'wrong','level':'INFO'}])
def test_raw_or_unstructured_log_text_cannot_become_byte_exact_sealed_export(record):
    with pytest.raises(HubError):module.validate_sealed_log(encoded(record)+b'\n')


def test_unexpected_missing_file_is_not_silently_called_archived(owned):
    f=owned;f['image'].unlink()
    with pytest.raises(HubError):OperationalArchive(f['settings'].root,f['limits']).build(str(uuid.uuid4()))


def test_release_needs_exact_tested_sources_and_no_failed_skipped_cases(owned,monkeypatch):
    file=release(owned,monkeypatch);record=json.loads(file.read_bytes());record['source_files'][module.SOURCE_FILES[0]]='0'*64;put(file,record)
    with pytest.raises(HubError,match='ARCHIVE_RETENTION_RESOLVER_NOT_READY'):owned['service'].apply(plan(owned))
    assert owned['image'].exists()
    file=release(owned,monkeypatch);record=json.loads(file.read_bytes());attest_file=Path(record['test_attestation']['file'])
    attest=json.loads(attest_file.read_bytes());attest['oom_killed']=True;put(attest_file,attest)
    record['test_attestation']['sha256']=sha(attest_file.read_bytes());put(file,record)
    with pytest.raises(HubError,match='ARCHIVE_RETENTION_RESOLVER_NOT_READY'):owned['service'].apply(plan(owned))
    assert owned['image'].exists()
