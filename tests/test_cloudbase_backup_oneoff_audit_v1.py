"""Owned historical authorization archive fixtures; no real grant or network.

Root-only metadata is a named fixture seam. The real canonical file, exact
bytes/hashes, strict JSON, SQLite snapshot and tar/retention registration run.
These fixtures never authorize publication or claim a real root grant.
"""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import hashlib,json,sqlite3,stat,time,uuid
import pytest

from adapters.cloudbase_backup import encoded
from services.hub import cloudbase_backup_runtime as m
from services.hub.archive_source_retention_v2 import ArchiveSourceRetentionV2
from services.hub.db import encode
from services.hub.errors import HubError
from test_archive_source_retention_v2 import acceptance
from test_cloudbase_backup_runtime import unpack_bundle,environment,cached_user_receipt_fixture


def sha(raw):return hashlib.sha256(raw).hexdigest()

def write(file,value):
    raw=value if isinstance(value,bytes) else encoded(value)+b'\n'
    file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(raw);file.chmod(0o600)
    return {'file':str(file),'sha256':sha(raw),'bytes':len(raw)}


@pytest.fixture
def audited(runtime,monkeypatch):
    settings,store=runtime;root=settings.root;now=time.time();policy='owned-historical-policy'
    # Filesystem root ownership alone is isolated; its predicate is exercised
    # independently below. All files/paths/size/hash/schema checks remain real.
    monkeypatch.setattr(m,'_oneoff_root_metadata',lambda info:True)
    monkeypatch.setattr(m.shutil,'disk_usage',lambda _:SimpleNamespace(free=100_000_000_000))
    payload={'post':{'id':m.ONEOFF_AGE_SOURCE,'created_at':'2026-11-01T00:00:00Z','text':'Owned synthetic public source'},
        'author':{'username':'example-channel-b'},'url':'https://x.com/example-channel-b/status/'+m.ONEOFF_AGE_SOURCE}
    job=store.enqueue('x',m.ONEOFF_AGE_SOURCE,'tweet_process','tweet',payload,policy)
    model=store.enqueue('machine_model','owned-only','machine_translation_node','tweet',{},policy)
    with store.transaction() as db:
        db.execute('UPDATE jobs SET job_id=?,state=\'completed\' WHERE job_id=?',(m.ONEOFF_AGE_JOB,job))
        db.execute('UPDATE jobs SET job_id=?,state=\'completed\',result=? WHERE job_id=?',
            (m.ONEOFF_AGE_MODEL,encode({'parent_job_id':m.ONEOFF_AGE_JOB,'human_review':False}),model))
    source=write(root/'media'/m.ONEOFF_AGE_JOB/'source.json',payload)
    image=write(root/'media'/m.ONEOFF_AGE_JOB/'owned.png',b'\x89PNG\r\n\x1a\nowned image, no actual publication')
    receipt=write(root/'data/model-receipts'/m.ONEOFF_AGE_MODEL/(str(uuid.uuid4())+'.json'),{'owned_synthetic_receipt':True})
    caption='【补发】Owned caption for an offline backup fixture'
    manifest={'version':'owned-machine-manifest','job_id':m.ONEOFF_AGE_JOB,'job_policy_version':policy,
        'post_id':m.ONEOFF_AGE_SOURCE,'source_url':payload['url'],'source_sha256':source['sha256'],
        'target_site_uid':m.ONEOFF_AGE_TARGET,'target_name':'Owned synthetic target','policy_sha256':'a'*64,
        'caption':caption,'caption_sha256':sha(caption.encode()),'render':{'png':image['file'],'sha256':image['sha256']},
        'models':{'post':receipt},'human_review':False,'renderer_grants_publication':False}
    manifest_ref=write(root/'media'/m.ONEOFF_AGE_JOB/'machine-api-v1'/str(uuid.uuid4())/'oneoff-age-manifest-v1.json',manifest)
    ident=str(uuid.uuid4());plan_file=root/'media'/m.ONEOFF_AGE_JOB/'oneoff-age-v1'/ident/'plan.json'
    plan={'version':'owner-single-tweet-age-plan-v1','plan_id':ident,'created_at':now-1000,'job_id':m.ONEOFF_AGE_JOB,
        'source_id':m.ONEOFF_AGE_SOURCE,'target_site_uid':m.ONEOFF_AGE_TARGET,'before_row_sha256':'b'*64,
        'job_policy_version':policy,'payload_sha256':sha(encoded(payload)),'policy_sha256':'a'*64,
        'original_created_at':payload['post']['created_at'],'original_state':'failed',
        'original_error_code':'MACHINE_COMPOSITOR_REAL_CHECK_FAILED','original_attempts':1,
        'manifest':manifest_ref,'artifact_sha256':image['sha256'],'caption_sha256':manifest['caption_sha256'],
        'caption':caption,'model_job_id':m.ONEOFF_AGE_MODEL,'human_review':False,'publication_authorized':False,
        'job_unchanged':True,'paid_model_calls':0}
    plan_ref=write(plan_file,plan)
    grant={'version':'owner-single-tweet-age-grant-v1','grant_id':str(uuid.uuid4()),'job_id':m.ONEOFF_AGE_JOB,
        'source_id':m.ONEOFF_AGE_SOURCE,'target_site_uid':m.ONEOFF_AGE_TARGET,'owner_response':m.ONEOFF_AGE_OWNER_RESPONSE,
        'owner_response_reference':'Owned historical response reference only','issued_at':now-999,'expires_at':now-500,
        'plan_file':str(plan_file),'plan_sha256':plan_ref['sha256'],'manifest_sha256':manifest_ref['sha256'],
        **{key:plan[key] for key in ('artifact_sha256','caption_sha256','job_policy_version','payload_sha256','policy_sha256')},
        'max_dispatches':1,'scope':['ignore_source_post_age_for_this_job_once'],'human_review':False}
    grant_file=root/'config/oneoff-tweet-age-v1/grant.json';grant_ref=write(grant_file,grant)
    history={'version':'owner-single-tweet-age-exception-v1','grant_id':grant['grant_id'],'grant':grant_ref,'plan':plan_ref,
        'original_state':'failed','original_stage':'owned-old-render-failure','original_error_code':plan['original_error_code'],
        'original_attempts':1,'original_row_sha256':plan['before_row_sha256'],'owner_response':grant['owner_response'],
        'owner_response_reference':grant['owner_response_reference'],'age_limit_exception_only':True,'human_review':False,
        'created_at_unchanged':True}
    result={'source_sha256':source['sha256'],'machine_web':{'manifest_file':manifest_ref['file'],'manifest_sha256':manifest_ref['sha256'],
        'artifact_sha256':image['sha256'],'state':'completed','policy':'owned-machine-manifest','human_review':False},
        'oneoff_owner_age_exception':history}
    def persist(*,plan_change=None,grant_change=None,history_change=None):
        if plan_change:
            plan.update(plan_change);history['plan']=write(plan_file,plan)
            grant['plan_sha256']=history['plan']['sha256']
        if grant_change:grant.update(grant_change)
        history['grant']=write(grant_file,grant)
        if history_change:history.update(history_change)
        with store.transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(result),m.ONEOFF_AGE_JOB))
    persist()
    return SimpleNamespace(settings=settings,store=store,root=root,plan=plan,grant=grant,history=history,result=result,
        plan_file=plan_file,grant_file=grant_file,manifest=manifest,manifest_ref=manifest_ref,image=image,receipt=receipt,persist=persist)


def build(f):return m.OperationalArchive(f.root,m.BackupLimits()).build(str(uuid.uuid4()))
def row(f):return f.store.get(m.ONEOFF_AGE_JOB)|{'payload':encode(f.store.get(m.ONEOFF_AGE_JOB)['payload']),
    'result':encode(f.store.get(m.ONEOFF_AGE_JOB)['result'])}


def test_expired_grant_and_plan_are_byte_exact_audit_members_with_original_graph_and_db_unchanged(audited):
    f=audited;before=f.store.get(m.ONEOFF_AGE_JOB);grant=f.grant_file.read_bytes();plan=f.plan_file.read_bytes()
    bundle=build(f);files=unpack_bundle(bundle);record=bundle['manifest']['oneoff_owner_age_exception_audits'][0]
    assert record['version']=='cloudbase-owner-age-exception-audit-v1' and record['historical_only'] is True
    assert record['publication_authority_restored'] is record['execute_or_import_authorized'] is record['local_source_prune_authorized'] is False
    assert record['human_review'] is False and record['restore_mode']=='0600'
    for reference,raw in zip(record['references'],(grant,plan)):
        assert reference['member'].startswith('audit/oneoff-tweet-age-v1/')
        assert files[reference['member']]==raw and reference['sha256']==sha(raw) and reference['bytes']==len(raw)
        assert 'tweets/files/'+reference['relative_file'] not in files
    assert files['tweets/files/'+Path(f.manifest_ref['file']).relative_to(f.root).as_posix()]==Path(f.manifest_ref['file']).read_bytes()
    assert files['tweets/files/'+Path(f.image['file']).relative_to(f.root).as_posix()]==Path(f.image['file']).read_bytes()
    assert files['tweets/files/'+Path(f.receipt['file']).relative_to(f.root).as_posix()]==Path(f.receipt['file']).read_bytes()
    cloned=f.root/'owned-restored.sqlite3';cloned.write_bytes(files['database/credential-excluded-operational.sqlite3'])
    with sqlite3.connect(cloned) as db:
        saved=json.loads(db.execute('SELECT result FROM jobs WHERE job_id=?',(m.ONEOFF_AGE_JOB,)).fetchone()[0])
    assert saved==before['result'] and f.store.get(m.ONEOFF_AGE_JOB)==before
    assert f.grant_file.read_bytes()==grant and f.plan_file.read_bytes()==plan


def test_actual_restore_registration_keeps_audit_bytes_outside_source_prune_locations(audited):
    f=audited;bundle=build(f);report,reader,_archive,_ledger=acceptance(f.settings,bundle)
    retention=ArchiveSourceRetentionV2(f.settings,f.store);registered=retention.register_verified_archive(report,ledger_reader=reader)
    assert registered['state']=='archive_locations_registered'
    with sqlite3.connect(retention.path) as db:
        paths={r[0] for r in db.execute('SELECT relative_file FROM locations')}
    assert f.grant_file.relative_to(f.root).as_posix() not in paths and f.plan_file.relative_to(f.root).as_posix() not in paths
    assert Path(f.image['file']).relative_to(f.root).as_posix() in paths
    restored=report.parent/'restored'
    for ref in bundle['manifest']['oneoff_owner_age_exception_audits'][0]['references']:
        assert (restored/ref['member']).read_bytes()==Path(ref['original_file']).read_bytes()


@pytest.mark.parametrize('kind,field,value',[
    ('grant','version','unknown'),('grant','job_id','00000000-0000-0000-0000-000000000000'),
    ('grant','source_id','wrong-source'),('grant','target_site_uid','wrong-target'),('grant','human_review',True),
    ('grant','scope',['anything']),('grant','max_dispatches',True),('grant','max_dispatches',2),
    ('grant','plan_file','/owned/foreign.json'),('grant','plan_sha256','0'*64),('grant','manifest_sha256','0'*64),
    ('grant','artifact_sha256','0'*64),('grant','owner_response','Owned wrong scope'),
    ('grant','owner_response_reference','Bearer '+('a'*24)),('grant','credential','owned-secret'),
    ('plan','version','unknown'),('plan','plan_id','00000000-0000-0000-0000-000000000000'),
    ('plan','job_id','00000000-0000-0000-0000-000000000000'),('plan','source_id','wrong'),
    ('plan','target_site_uid','wrong'),('plan','model_job_id','00000000-0000-0000-0000-000000000000'),
    ('plan','human_review',True),('plan','publication_authorized',True),('plan','original_attempts',True),
    ('plan','paid_model_calls',True),('plan','job_unchanged',False),('plan','caption','Owned no prefix'),
    ('plan','payload_sha256','0'*64),('plan','before_row_sha256','0'*64),('plan','private_api_key','owned-secret'),
    ('history','version','unknown'),('history','human_review',True),('history','age_limit_exception_only',False),
    ('history','created_at_unchanged',False),('history','original_attempts',True),
    ('history','grant_id','00000000-0000-0000-0000-000000000000'),('history','owner_response_reference','wrong'),
])
def test_audit_scope_and_all_mutual_bindings_refuse_without_changing_original_job(audited,kind,field,value):
    f=audited;f.persist(**{kind+'_change':{field:value}});before=f.store.get(m.ONEOFF_AGE_JOB)
    with pytest.raises(HubError):build(f)
    assert f.store.get(m.ONEOFF_AGE_JOB)==before


@pytest.mark.parametrize('target', ['grant','plan'])
@pytest.mark.parametrize('change', ['hash','size_bool','missing','symlink','hardlink','duplicate_json'])
def test_single_regular_original_bytes_are_required(audited,monkeypatch,target,change):
    f=audited;file=getattr(f,target+'_file');ref=f.history[target]
    if change=='hash':file.write_bytes(file.read_bytes()+b' ')
    elif change=='size_bool':ref['bytes']=True;f.persist(history_change={target:ref})
    elif change=='missing':file.unlink()
    elif change=='symlink':
        original=Path.is_symlink;monkeypatch.setattr(Path,'is_symlink',lambda p:p==file or original(p))
    elif change=='hardlink':
        import os
        os.link(file,file.with_suffix('.hardlink'))
    elif change=='duplicate_json':
        raw=file.read_bytes().rstrip();raw=raw[:-1]+b',"human_review":false}'
        file.write_bytes(raw);ref.update(sha256=sha(raw),bytes=len(raw));f.persist(history_change={target:ref}) if target=='plan' else None
        if target=='grant':
            # Persist without replacing the intentionally malformed original.
            with f.store.transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(f.result),m.ONEOFF_AGE_JOB))
    with pytest.raises(HubError):build(f)


@pytest.mark.parametrize('uid,mode,expected',[(0,stat.S_IFREG|0o640,True),(10001,stat.S_IFREG|0o640,False),
    (0,stat.S_IFREG|0o660,False),(0,stat.S_IFREG|0o604,True),(0,stat.S_IFREG|0o642,False)])
def test_root_metadata_does_not_accept_app_owned_or_group_world_writable(uid,mode,expected):
    assert m._oneoff_root_metadata(SimpleNamespace(st_uid=uid,st_mode=mode)) is expected


def test_manifest_can_be_missing_only_behind_already_validated_archive_registry(audited):
    f=audited;file=Path(f.manifest_ref['file']);file.unlink()
    with pytest.raises(HubError):m.oneoff_age_audit(f.root,row(f),{})
    # Registry validation is an existing independent boundary; this pure helper
    # never manufactures an archive record or performs a remote GET.
    proof=m.oneoff_age_audit(f.root,row(f),{(str(file),f.manifest_ref['sha256']):{'owned_prevalidated_registry_seam':True}})
    assert proof['record']['manifest_evidence']=='prior_byte_exact_archive'


def test_unrelated_config_or_arbitrary_grant_reference_remains_rejected(audited):
    f=audited;file=f.root/'config/unrelated/grant.json';ref=write(file,{'owned':True})
    f.result['unrelated_evidence']=ref;f.persist()
    with pytest.raises(HubError,match='CLOUDBASE_BACKUP_EVIDENCE_PATH_REJECTED'):build(f)


def test_grant_and_plan_permissions_never_become_publication_authority(audited):
    f=audited;before=f.store.query('SELECT * FROM publications');bundle=build(f)
    assert f.store.query('SELECT * FROM publications')==before==[]
    assert not f.store.query('SELECT * FROM api_calls') and not f.store.query('SELECT * FROM audit')
    assert bundle['manifest']['source_files_deleted'] is bundle['manifest']['production_database_modified'] is False


@pytest.fixture(params=list(m.REFERENCE_BACKFILL_AUDITS))
def backfilled(runtime,monkeypatch,request):
    settings,store=runtime;root=settings.root;version=request.param;spec=deepcopy(m.REFERENCE_BACKFILL_AUDITS[version])
    # Fixed production raw-SHA authority is an explicit synthetic fixture seam;
    # canonical paths, root predicate seam, strict reader, all semantic bindings,
    # SQLite, tar and actual registration remain exercised independently.
    monkeypatch.setattr(m,'REFERENCE_BACKFILL_AUDITS',{version:spec})
    monkeypatch.setattr(m,'_oneoff_root_metadata',lambda info:True)
    monkeypatch.setattr(m.shutil,'disk_usage',lambda _:SimpleNamespace(free=100_000_000_000))
    now=time.time();sources=sorted(spec['jobs'].values());posts={ident:{'id':ident,'created_at':'2026-11-02T00:00:00Z'} for ident in sources}
    handle='example_handle_a' if 'drink' in version else 'example-channel-b'
    payloads={}
    for ident,source in spec['jobs'].items():
        payload={'post':posts[source], 'author':{'username':handle},'url':'https://x.com/'+handle+'/status/'+source}
        allocated=store.enqueue('x',source,'tweet_process','tweet',payload,'owned-backfill-policy')
        with store.transaction() as db:db.execute("UPDATE jobs SET job_id=?,state='completed' WHERE job_id=?",(ident,allocated))
        payloads[ident]=payload;write(root/'media'/ident/'source.json',payload)
    grant_file=root/spec['path'];markers={};results={};descriptor=None
    if 'descriptor' in spec:
        grant={'version':'owner-two-reference-backfill-grant-v2','grant_id':str(uuid.uuid4()),'source_ids':sources,
            'target_site_uid':m.ONEOFF_AGE_TARGET,'author_id':'1986339274611159040','author_handle':'example-channel-b',
            'owner_response':'Owned offline historical permission, not publication authority',
            'owner_response_reference':'Owned offline reference','source_urls':['https://x.com/example-channel-b/status/'+s for s in sources],
            'issued_at':now-900,'expires_at':now-500,'live_hub_sha256':'a'*64,'scopes':list(m.REFERENCE_V2_SCOPES),
            'max_timeline_requests':2,'max_detail_requests':3,'max_user_requests':1,'max_publications_per_source':1,'human_review':False}
    else:
        plan={'version':version,'capture':{'capture_job_id':spec['capture'],'source_ids':sources,'original_posts':posts},
            'target_site_uid':m.ONEOFF_AGE_TARGET,'source_ids':sources,'scope':list(m.CACHED_BACKFILL_SCOPES),
            'human_review':False,'new_x_calls':0,'config_file':str(root/'config/tweet-runtime-v4.toml'),
            'config_sha256':'a'*64,'policy_sha256':'b'*64,'engine_sha256':'c'*64,'owner_quote':'Owned offline permission',
            'existing_jobs':[],'existing_publications':[],'tombstones':[]}
        grant={'grant_id':str(uuid.uuid4()),'issued_at':now-900,'expires_at':now-500,
            'owner_response_reference':'Owned offline reference','plan':plan}
    def persist(*,grant_change=None,plan_change=None,descriptor_change=None,marker_change=None):
        nonlocal descriptor
        if grant_change:grant.update(grant_change)
        if plan_change:grant['plan'].update(plan_change)
        grant_ref=write(grant_file,grant);spec.update(sha256=grant_ref['sha256'],bytes=grant_ref['bytes'])
        if 'descriptor' in spec:
            if descriptor is None:
                descriptor={'version':'owner-two-reference-source-capture-v2','capture_job_id':spec['capture'],
                    'grant':grant_ref,'source_ids':sources,'target_site_uid':m.ONEOFF_AGE_TARGET,
                    'author_id':'1986339274611159040','author_handle':'example-channel-b','human_review':False,
                    'publication_authorized':False,'targets':{ident:{'original_post':dict(post)} for ident,post in posts.items()}}
            else:descriptor['grant']=grant_ref
            if descriptor_change:descriptor.update(descriptor_change)
            descriptor_ref=write(root/spec['descriptor'],descriptor)
            spec.update(descriptor_sha256=descriptor_ref['sha256'],descriptor_bytes=descriptor_ref['bytes'])
        else:descriptor_ref=grant_ref
        for ident,source in spec['jobs'].items():
            marker={'version':version,'capture_job_id':spec['capture'],'grant':grant_ref,'descriptor':descriptor_ref,
                'human_review':False,'age_exception_only':True,'original_created_at':payloads[ident]['post']['created_at']}
            if 'descriptor' in spec:marker['publication_authorized']=False
            if marker_change:marker.update(marker_change)
            markers[ident]=marker;results[ident]={'owner_reference_backfill':marker}
            with store.transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(results[ident]),ident))
    persist()
    return SimpleNamespace(settings=settings,store=store,root=root,version=version,spec=spec,grant=grant,
        grant_file=grant_file,markers=markers,results=results,persist=persist,descriptor=lambda:descriptor)


def test_four_observed_backfill_families_preserve_original_bytes_share_only_bound_jobs_and_register_safely(backfilled):
    f=backfilled;before=f.store.query('SELECT * FROM jobs');bundle=build(f);files=unpack_bundle(bundle)
    records=bundle['manifest']['owner_reference_backfill_audits']
    assert len(records)==len(f.spec['jobs'])
    assert {r['job_id'] for r in records}==set(f.spec['jobs'])
    expected_members=2 if 'descriptor' in f.spec else 1
    members={ref['member'] for record in records for ref in record['references']}
    assert len(members)==expected_members
    for record in records:
        assert record['historical_only'] is True
        assert record['publication_authority_restored'] is record['execute_or_import_authorized'] is record['local_source_prune_authorized'] is False
        assert record['human_review'] is False and record['restore_mode']=='0600'
        for ref in record['references']:
            assert files[ref['member']]==Path(ref['original_file']).read_bytes()
            assert sha(files[ref['member']])==ref['sha256'] and len(files[ref['member']])==ref['bytes']
            assert 'tweets/files/'+ref['relative_file'] not in files
        if 'descriptor' not in f.spec:assert record['references'][0]['reference_roles']==['grant','descriptor']
    report,reader,_archive,_ledger=acceptance(f.settings,bundle)
    retention=ArchiveSourceRetentionV2(f.settings,f.store);retention.register_verified_archive(report,ledger_reader=reader)
    with sqlite3.connect(retention.path) as db:locations={x[0] for x in db.execute('SELECT relative_file FROM locations')}
    assert f.spec['path'] not in locations and f.spec.get('descriptor') not in locations
    assert f.store.query('SELECT * FROM jobs')==before and not f.store.query('SELECT * FROM publications')


@pytest.mark.parametrize('change',['root','hash','path','version','capture','human','scope','source','target','extra_file','time'])
def test_backfill_metadata_hash_root_and_job_scope_cannot_be_expanded(backfilled,monkeypatch,change):
    f=backfilled
    if change=='root':monkeypatch.setattr(m,'_oneoff_root_metadata',lambda info:False)
    elif change=='hash':f.grant_file.write_bytes(f.grant_file.read_bytes()+b' ')
    elif change=='path':f.persist(marker_change={'grant':{**next(iter(f.markers.values()))['grant'],'file':str(f.root/'config/unknown/grant.json')}})
    elif change=='version':f.persist(marker_change={'version':'unknown-future-type'})
    elif change=='capture':f.persist(marker_change={'capture_job_id':str(uuid.uuid4())})
    elif change=='human':f.persist(marker_change={'human_review':True})
    elif change=='time':f.persist(grant_change={'expires_at':float('inf')})
    else:
        is_v2='descriptor' in f.spec
        if change=='scope':values={'scopes' if is_v2 else 'scope':['arbitrary-publication']}
        elif change=='source':values={'source_ids':['foreign-source']}
        elif change=='target':values={'target_site_uid':'foreign-target'}
        elif change=='extra_file':values={'unrelated_evidence':{'file':str(f.root/'config/private.env'),'sha256':'a'*64}}
        f.persist(**{('grant_change' if is_v2 else 'plan_change'):values})
    before=f.store.query('SELECT * FROM jobs')
    with pytest.raises(HubError):build(f)
    assert f.store.query('SELECT * FROM jobs')==before


def test_exact_production_registry_remains_four_fixed_paths_raw_hashes_and_eight_job_bindings():
    registry=m.REFERENCE_BACKFILL_AUDITS
    assert len(registry)==4 and sum(len(item['jobs']) for item in registry.values())==8
    assert {spec['sha256'] for spec in registry.values()}=={
        '29b076f4df26269d3c3fcdc526db094274612a7d72c51b9cf567fb9e133845d0',
        'abb8c86f43be1f7bb8dd315b0b59c9dab022274b880efa1084c08075a8011c4f',
        'e810e0c39ac665e0d43dc0e4700afc2416d4d9dd4f989a7dc76a89fbe4146863',
        '7953d1ab0a51824dd444fdb3216a3500953f6e53aa0e6f02737b0daa87dbf140'}
    assert all(spec['path'].startswith('config/') and spec['path'].endswith('/grant.json') for spec in registry.values())


def test_exact_bound_cached_user_receipt_from_scheduler_is_kept_with_original_bytes(environment):
    f=environment;owner,file=cached_user_receipt_fixture(f)
    with f.store.transaction() as db:db.execute('UPDATE jobs SET source_platform=? WHERE job_id=?',('scheduler',owner))
    before=f.store.query('SELECT * FROM jobs');bundle=build(f);files=unpack_bundle(bundle)
    assert files['tweets/files/'+file.relative_to(f.root).as_posix()]==file.read_bytes()
    assert f.store.query('SELECT * FROM jobs')==before and not f.provider.calls


@pytest.mark.parametrize('change',['unbound_file','wrong_host','wrong_method','wrong_type','wrong_queue','unknown_platform'])
def test_scheduler_origin_never_authorizes_unbound_or_different_receipts(environment,change):
    f=environment
    if change in ('wrong_host','wrong_method'):
        # The actual receipt schema already rejects these before an archive
        # can observe them. Never disable CHECK constraints to fabricate one.
        with pytest.raises(sqlite3.IntegrityError):
            cached_user_receipt_fixture(f,host='example.invalid' if change=='wrong_host' else 'api.x.com',
                method='POST' if change=='wrong_method' else 'GET')
        assert not f.store.query("SELECT * FROM source_http_receipts WHERE host<>'api.x.com' OR method<>'GET'")
        assert not f.provider.calls
        return
    owner,file=cached_user_receipt_fixture(f)
    with f.store.transaction() as db:
        db.execute('UPDATE jobs SET source_platform=? WHERE job_id=?',('scheduler',owner))
        if change=='wrong_type':db.execute('UPDATE jobs SET job_type=? WHERE job_id=?',('arbitrary',owner))
        if change=='wrong_queue':db.execute('UPDATE jobs SET queue=? WHERE job_id=?',('system',owner))
        if change=='unknown_platform':db.execute('UPDATE jobs SET source_platform=? WHERE job_id=?',('unapproved',owner))
    if change=='unbound_file':
        ref=write(file.parent/'unrelated.json',{'owned_unrelated':True})
        result=f.store.get(f.job)['result'];result['unrelated_evidence']=ref
        with f.store.transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(result),f.job))
    with pytest.raises(HubError,match='CLOUDBASE_BACKUP_EVIDENCE_OWNER_REJECTED'):build(f)
    assert not f.provider.calls


@pytest.fixture
def reposted(environment):
    """Synthetic saved observations, genuine SQLite/tar/hash paths; no browser."""
    f=environment;f.external='1266077623570661433';f.key='e'*64;f.caption='Owned archived caption'
    f.parent_result=f.store.get(f.job)['result'];f.parent_result['machine_web']={'manifest_sha256':'f'*64}
    f.request={'version':'tweet-native-repost-check-v1','submission_job_id':f.job,'source_id':'12345',
        'external_id':f.external,'target_site_uid':m.ONEOFF_AGE_TARGET,'url':'https://t.bilibili.com/'+f.external,
        'manifest_sha256':'f'*64,'caption':f.caption,'caption_sha256':sha(f.caption.encode()),
        'publication_key':f.key,'publication_kind':'dynamic_website_machine_v4_api_relations'}
    f.check=f.store.enqueue('bilibili',f.external,'tweet_repost_check_v1','tweet',f.request,'tweet-native-repost-check-v1')
    f.observations=[];f.bodies=[];f.refs=[];f.policy='tweet-native-repost-check-v1';f.marker='post_submission_repost_check'
    with f.store.transaction() as db:
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',(f.key,f.job,'bilibili','x','12345',
            'site_uid:'+m.ONEOFF_AGE_TARGET,f.request['publication_kind'],'completed',f.external,None,time.time()))
    def add_observation():
        attempt=str(uuid.uuid4());area=f.root/'media'/f.check/'tweet-repost-check-v1'/attempt
        image=write(area/'observed.png',b'\x89PNG\r\n\x1a\nowned saved probe '+attempt.encode())
        body={'version':'tweet-native-repost-observation-v1','check_job_id':f.check,'attempt_id':attempt,
            'request_sha256':sha(encoded({**f.request,'check_job_id':f.check,'attempt_id':attempt})),
            'external_id':f.external,'target_site_uid':m.ONEOFF_AGE_TARGET,'screenshot':image,
            'human_review':False,'platform_cause_verified':False,
            'actions':{'clicks':0,'file_selections':0,'fills':0,'submits':0}}
        if f.policy=='tweet-native-repost-reinspection-v2':
            body['version']='tweet-native-repost-observation-v2'
            body['actions'].update(clicks=2,author_navigation_clicks=1,repost_dialog_clicks=1)
            for key,name in (('before_probe_screenshot','before-probe.png'),('author_profile_screenshot','author-profile.png')):
                extra=write(area/name,b'\x89PNG\r\n\x1a\nowned extra '+name.encode());body[key]=extra;f.refs.append(extra)
        raw=write(area/'observation.json',body)
        outcome={'version':'tweet-native-repost-check-v1','external_id':f.external,'status':'ui_unrecognized',
            'human_review':False,'platform_cause_verified':False,'public_visibility_verified':False,
            'send_requests':0,'raw_observation':raw,'screenshot':image}
        f.observations.append(outcome);f.bodies.append(body);f.refs.extend((raw,image))
    def persist(state='completed',final=True):
        saved={'observations':f.observations}
        if final:saved['final_observation']=f.observations[-1]
        f.parent_result[f.marker]=f.observations[-1]
        with f.store.transaction() as db:
            db.execute('UPDATE jobs SET state=?,external_publish_id=?,result=? WHERE job_id=?',
                ('completed',f.external,encode(f.parent_result),f.job))
            db.execute('UPDATE jobs SET payload=?,result=?,state=?,lease_owner=?,lease_until=? WHERE job_id=?',
                (encode(f.request),encode(saved),state,'worker-fixture' if state=='processing' else None,
                 time.time()+60 if state=='processing' else None,f.check))
            db.execute('DELETE FROM audit WHERE target=? AND action=?',(f.check,f.policy))
            for observation in f.observations:
                db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                    (time.time(),'worker:tweet-repost-check-v1',f.policy,f.check,encode(observation)))
    f.add_observation=add_observation;f.persist=persist
    add_observation();add_observation();persist()
    return f


def test_native_repost_all_historical_files_are_exact_archived_and_not_prune_authorized(reposted):
    f=reposted;before=f.store.query('SELECT * FROM jobs');bundle=build(f);files=unpack_bundle(bundle)
    record=bundle['manifest']['native_repost_check_evidence'][0]
    assert record['observations']==2 and len(record['references'])==4 and record['snapshot_state']=='completed'
    assert record['historical_only'] and not record['restore_requires_reconciliation']
    assert all(record[k] is False for k in ('human_review','publication_authority_restored','public_visibility_inferred','local_source_prune_authorized'))
    for ref in f.refs:
        assert files['tweets/files/'+Path(ref['file']).relative_to(f.root).as_posix()]==Path(ref['file']).read_bytes()
    assert f.store.query('SELECT * FROM jobs')==before and not f.provider.calls


@pytest.mark.parametrize('state',['processing','retry_scheduled','awaiting_review','failed'])
def test_native_repost_inflight_durable_history_is_snapshot_not_execution_permission(reposted,state):
    f=reposted;f.persist(state=state,final=False);before=f.store.get(f.check)
    record=build(f)['manifest']['native_repost_check_evidence'][0]
    assert record['snapshot_state']==state and record['restore_requires_reconciliation']
    assert record['snapshot_lease_owner']==before['lease_owner']
    assert record['snapshot_lease_until']==before['lease_until'] and f.store.get(f.check)==before
    assert record['local_source_prune_authorized'] is False and not f.provider.calls


@pytest.mark.parametrize('change',['no_audit','wrong_action','wrong_principal','bad_parent','bad_publication',
    'bad_manifest','bad_caption','bad_raw_request','send_action','cross_attempt','arbitrary_file',
    'parent_not_last','completed_without_final','changed_png','missing_history'])
def test_native_repost_never_broadens_owner_or_loses_integrity(reposted,change):
    f=reposted
    if change=='no_audit':
        with f.store.transaction() as db:db.execute('DELETE FROM audit WHERE target=?',(f.check,))
    elif change in ('wrong_action','wrong_principal'):
        with f.store.transaction() as db:db.execute('UPDATE audit SET '+('action' if change=='wrong_action' else 'principal')+'=? WHERE target=?',('wrong',f.check))
    elif change=='bad_publication':
        with f.store.transaction() as db:db.execute("UPDATE publications SET account_id='site_uid:1' WHERE job_id=?",(f.job,))
    elif change in ('bad_parent','bad_manifest','bad_caption'):
        f.request[{'bad_parent':'submission_job_id','bad_manifest':'manifest_sha256','bad_caption':'caption'}[change]]='changed';f.persist()
    elif change in ('bad_raw_request','send_action','cross_attempt','arbitrary_file'):
        body=f.bodies[-1]
        if change=='bad_raw_request':body['request_sha256']='0'*64
        if change=='send_action':body['actions']['submits']=1
        if change=='cross_attempt':body['screenshot']=f.refs[1]
        if change=='arbitrary_file':body['screenshot']=write(Path(f.refs[-1]['file']).parent/'other.png',b'\x89PNG\r\n\x1a\nowned')
        f.observations[-1]['raw_observation']=write(Path(f.observations[-1]['raw_observation']['file']),body);f.persist()
    elif change=='parent_not_last':
        f.parent_result['post_submission_repost_check']=f.observations[0]
        with f.store.transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(f.parent_result),f.job))
    elif change=='completed_without_final':f.persist(final=False)
    elif change=='changed_png':Path(f.refs[-1]['file']).write_bytes(b'\x89PNG\r\n\x1a\nchanged')
    elif change=='missing_history':Path(f.refs[0]['file']).unlink()
    with pytest.raises(HubError):build(f)
    assert not f.provider.calls


def test_native_repost_exact_refs_do_not_admit_another_file_of_same_check_owner(reposted):
    f=reposted;ref=write(f.root/'media'/f.check/'unrelated.json',{'owned_unrelated':True})
    f.parent_result['unrelated_evidence']=ref;f.persist()
    with pytest.raises(HubError,match='CLOUDBASE_BACKUP_EVIDENCE_OWNER_REJECTED'):build(f)
    assert not f.provider.calls


@pytest.fixture
def reinspected(reposted):
    f=reposted;f.previous=f.check;previous=f.store.get(f.previous)
    f.policy='tweet-native-repost-reinspection-v2';f.marker='post_submission_repost_reinspection_v2'
    f.request={**f.request,'reinspection':{'version':f.policy,'previous_check_job_id':f.previous,
        'previous_result_sha256':sha(encoded(previous['result'])),'owner_report_reference':'Owned historical synthetic report'}}
    f.check=f.store.enqueue('bilibili',f.external,'tweet_repost_check_v1','tweet',f.request,f.policy)
    f.observations=[];f.bodies=[];f.add_observation();f.persist();return f


def test_native_reinspection_keeps_previous_four_file_graph_and_navigation_images(reinspected):
    f=reinspected;bundle=build(f);files=unpack_bundle(bundle)
    records=bundle['manifest']['native_repost_check_evidence']
    assert len(records)==2 and [r['observations'] for r in records]==[2,1]
    assert len(records[1]['references'])==4 and records[1]['policy_version']==f.policy
    for ref in f.refs:assert files['tweets/files/'+Path(ref['file']).relative_to(f.root).as_posix()]==Path(ref['file']).read_bytes()
    assert not f.provider.calls


@pytest.mark.parametrize('change',['previous_result','previous_payload','wrong_previous','changed_version','orphan_image'])
def test_native_reinspection_requires_original_previous_result_and_exact_navigation_files(reinspected,change):
    f=reinspected
    if change in ('previous_result','previous_payload'):
        with f.store.transaction() as db:db.execute('UPDATE jobs SET '+('result' if change=='previous_result' else 'payload')+'=? WHERE job_id=?',
            (encode({'changed':True}),f.previous))
    elif change=='wrong_previous':f.request['reinspection']['previous_check_job_id']=str(uuid.uuid4());f.persist()
    elif change=='changed_version':f.request['reinspection']['version']='unapproved-v3';f.persist()
    elif change=='orphan_image':Path(f.bodies[-1]['author_profile_screenshot']['file']).unlink()
    with pytest.raises(HubError):build(f)
    assert not f.provider.calls


def test_native_repost_restore_registration_leaves_probe_sources_outside_automatic_pruning(reposted):
    f=reposted;bundle=build(f);settings=SimpleNamespace(root=f.root)
    report,reader,_,_=acceptance(settings,bundle)
    retention=ArchiveSourceRetentionV2(settings,f.store);retention.register_verified_archive(report,ledger_reader=reader)
    with sqlite3.connect(retention.path) as db:locations={row[0] for row in db.execute('SELECT relative_file FROM locations')}
    assert not any(Path(ref['file']).relative_to(f.root).as_posix() in locations for ref in f.refs)
    assert (f.root/'media'/f.job/'translated.png').relative_to(f.root).as_posix() in locations
    assert all(Path(ref['file']).exists() for ref in f.refs) and not f.provider.calls


def test_seen_native_file_never_escapes_its_exact_parent_binding(reposted):
    f=reposted;other=f.store.enqueue('x','another-tweet','tweet_process','tweet',{},'test-policy')
    with f.store.transaction() as db:
        db.execute('UPDATE jobs SET state=?,updated_at=?,result=? WHERE job_id=?',
            ('completed',time.time()+10,encode({'borrowed_evidence':f.refs[0]}),other))
    with pytest.raises(HubError,match='CLOUDBASE_BACKUP_EVIDENCE_OWNER_REJECTED'):build(f)
    assert not f.provider.calls
