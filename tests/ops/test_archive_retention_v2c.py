"""Owned proof/state fixtures; no cloud calls or production paths are used."""
from pathlib import Path
from types import SimpleNamespace
import ast,hashlib,importlib.util,json,os
import xml.etree.ElementTree as ET
import pytest
import tarfile
from historical_artifacts import historical_artifact

REPO=Path(__file__).parents[2]
def module(name):
    spec=importlib.util.spec_from_file_location(name,REPO/'scripts/ops'/(name+'.py'))
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value
b=module('prepare_archive_retention_v2c');i=module('install_archive_retention_v2c')


def proof(layer,*,autorun=False):
    counts={'tests.test_archive_source_autorun_v2':9} if autorun else b.COUNTS
    suite=ET.Element('testsuite')
    for name,count in counts.items():
        for n in range(count):ET.SubElement(suite,'testcase',classname=name,name='owned_'+str(n))
    junit=ET.tostring(suite)
    prefix='archive-autorun-v2-tests' if autorun else 'archive-retention-v2-tests'
    value={'version':'archive-source-autorun-v2-ubuntu-tests-v1' if autorun else 'archive-source-retention-v2-ubuntu-tests-v1',
      'image':b.IMAGE,'tests':sum(counts.values()),'failures':0,'errors':0,'skipped':0,'exit_code':0,
      'oom_killed':False,'application_data_mounted':False,'production_profile_mounted':False,'secrets_environment_injected':False,
      'full_suite_passed':True,'no_pid_or_oom_denial':True,'runtime_matches_declared_candidate':True,'mounts_match_isolated_allowlist':True,
      'network':'none','source_files':{name:layer[name] for name in (*b.SOURCE_FILES,'services/hub/archive_source_autorun_v2.py') if autorun or name in b.SOURCE_FILES},
      'junit_sha256':b.sha(junit),'junit_file':'/var/lib/lighthouse-backup-ops/'+prefix+'/12345678-1234-1234-1234-123456789012/accepted-results.xml'}
    return value,junit


@pytest.fixture
def kit(tmp_path):
    # Freeze a small owned copy; another agent may be editing the live runtime.
    repo=tmp_path/'repo';runtime='services/hub/cloudbase_backup_runtime.py'
    names={runtime,*b.UNCHANGED,*b.HOST,'scripts/ops/lighthouse-cloudbase-backup.service',*('scripts/ops/'+n for n in b.OLD_HELPERS)}
    # The old builder requires its original reviewed inputs, not today's fixed
    # operators. No original digest or current production source is rewritten.
    with tarfile.open(historical_artifact('historical-retention-v2c-source-inputs.tar')) as archived:
        expected=names-{runtime}
        assert {m.name for m in archived.getmembers()}==expected
        assert len(archived.getmembers())==len(expected)
        for name in names:
            target=repo/name;target.parent.mkdir(parents=True,exist_ok=True)
            if name==runtime:raw=(REPO/name).read_bytes()
            else:
                member=archived.getmember(name)
                assert member.isfile() and member.size<=150000
                with archived.extractfile(member) as stream:raw=stream.read(150001)
                assert len(raw)==member.size
            target.write_bytes(raw)
    runtime_sha=b.sha((repo/runtime).read_bytes());layer={runtime:runtime_sha,**b.UNCHANGED}
    core,cj=proof(layer);auto,aj=proof(layer,autorun=True)
    args=(repo,runtime_sha,b.wire(core),cj,b.wire(auto),aj)
    manifest,files=b.build(*args)
    return {'repo':repo,'args':args,'manifest':manifest,'files':files,'layer':layer,'core':core,'junit':cj,'auto':auto,'auto_junit':aj}


def test_reproducible_narrow_build_preserves_runtime_scope_state_and_old_templates(kit):
    manifest,files=b.build(*kit['args'])
    assert manifest==kit['manifest'] and files==kit['files']
    assert len(manifest['runtime_files'])==5 and manifest['runtime_files']==kit['layer']
    assert manifest['image']==b.IMAGE and manifest['daily_state'].endswith('/retention-v2b/daily-state')
    daily=files['helpers/cloudbase_backup_daily_retention_v2c.py'].decode()
    assert "BASE=OPS/'retention-v2c'" in daily and "d.STATE=Path('/var/lib/lighthouse-backup-ops/retention-v2b/daily-state')" in daily
    assert set(manifest['helper_files'])==i.HELPERS
    assert all(b'\r' not in raw for name,raw in files.items() if name.startswith('helpers/'))
    assert b.sha(files['previous.service'])==b.OLD_UNIT_SHA
    assert files['candidate.service']==files['previous.service'].replace(b'_retention_v2b.py',b'_retention_v2c.py')
    for name,digest in b.OLD_HELPERS.items():assert b.sha((kit['repo']/'scripts/ops'/name).read_bytes())==digest


@pytest.mark.parametrize('change',['failure','skip','wrong_count','wrong_source','network','production_mount','junit','proof_path'])
def test_proof_failures_cannot_be_packaged_or_installed(kit,change):
    value=json.loads(b.wire(kit['core']));junit=kit['junit']
    if change=='failure':value['failures']=1
    elif change=='skip':value['skipped']=1
    elif change=='wrong_count':value['tests']=140
    elif change=='wrong_source':value['source_files'][b.SOURCE_FILES[1]]='0'*64
    elif change=='network':value['network']='bridge'
    elif change=='production_mount':value['application_data_mounted']=True
    elif change=='junit':junit=b'<testsuite/>'
    elif change=='proof_path':value['junit_file']='/tmp/fake/accepted-results.xml'
    with pytest.raises(ValueError):b.proof(b.wire(value),junit,kit['layer'])
    if change!='proof_path':
        with pytest.raises(ValueError):i.verify_proof(b.wire(value),junit,kit['manifest'])


def guard_scope(kit,tmp_path):
    text=kit['files']['helpers/cloudbase_backup_daily_retention_v2c.py'].decode()
    names={'IMAGE','PINS','LEGACY_PINS','RECOVERY_DAILY','RECOVERY_OPERATION'};nodes=[]
    for node in ast.parse(text).body:
        if isinstance(node,ast.FunctionDef) and node.name in ('validate_previous','verify_recovered_gap'):nodes.append(node)
        elif isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id in names for t in node.targets):nodes.append(node)
    def path(value):
        if str(value)=='/var/lib/lighthouse-backup-ops/preupload-recovery-v1':return tmp_path/'recovery'
        if str(value)=='/var/lib/lighthouse-backup-ops/latest.json':return tmp_path/'legacy.json'
        raise AssertionError('Unexpected external path')
    def protected(file):assert file.resolve().is_relative_to(tmp_path)
    scope={'Path':path,'json':json,'hashlib':hashlib,'protected':protected}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'<owned-generated-guards>','exec'),scope)
    return scope


@pytest.mark.parametrize('pins,state,allowed',[('legacy','completed',True),('legacy','accept_failed',False),('legacy','accept_started',False),
                                             ('new','completed',True),('new','accept_failed',True),('unknown','completed',False)])
def test_daily_pin_transition_never_relabels_unfinished_legacy_record(kit,tmp_path,pins,state,allowed):
    scope=guard_scope(kit,tmp_path)
    record={'version':'cloudbase-daily-accept-prune-v1','image':b.IMAGE,'state':state,
            'helper_pins':scope['LEGACY_PINS'] if pins=='legacy' else scope['PINS'] if pins=='new' else {}}
    before=b.wire(record)
    if allowed:scope['validate_previous'](record)
    else:
        with pytest.raises(AssertionError):scope['validate_previous'](record)
    assert b.wire(record)==before


@pytest.mark.parametrize('change',['none','missing_receipt','history_changed','receipt_state','plan_changed','latest_reappeared','legacy_changed'])
def test_missing_latest_requires_real_fixed_recovery_closure(kit,tmp_path,change):
    scope=guard_scope(kit,tmp_path);daily=tmp_path/'daily';(daily/'history').mkdir(parents=True)
    ident=scope['RECOVERY_DAILY'];folder=tmp_path/'recovery'/ident;folder.mkdir(parents=True)
    failed={'version':'cloudbase-daily-accept-prune-v1','daily_id':ident,'state':'accept_failed','helper_pins':scope['LEGACY_PINS']}
    raw=b.wire(failed);(folder/'failed-latest.json').write_bytes(raw);history=daily/'history'/(ident+'.json');history.write_bytes(raw)
    legacy=b.wire({'state':'completed'});(tmp_path/'legacy.json').write_bytes(legacy)
    plan={'daily_id':ident,'operation_id':scope['RECOVERY_OPERATION'],'external_requests':0,'ledger_modified':False,
          'source_hashes':{'latest':b.sha(raw),'history':b.sha(raw),'legacy':b.sha(legacy)}}
    intent={'version':'cloudbase-preupload-daily-recovery-v1','state':'relocation_intent_saved','plan':plan,'plan_sha256':b.sha(b.wire(plan))}
    receipt={'version':intent['version'],'state':'preupload_failure_reconciled','plan_sha256':intent['plan_sha256'],
       'daily_id':ident,'operation_id':scope['RECOVERY_OPERATION'],'active_latest_absent':True,'history_preserved':True,
       'ledger_modified':False,'backup_success_claimed':False,'core_services_restarted':False,'external_requests':0}
    if change=='receipt_state':receipt['state']='invented'
    if change=='plan_changed':plan['operation_id']='wrong'
    (folder/'intent.json').write_bytes(b.wire(intent))
    if change!='missing_receipt':(folder/'receipt.json').write_bytes(b.wire(receipt))
    if change=='history_changed':history.write_text('{}')
    if change=='latest_reappeared':(daily/'latest.json').write_bytes(raw)
    if change=='legacy_changed':(tmp_path/'legacy.json').write_text('{}')
    if change=='none':scope['verify_recovered_gap'](SimpleNamespace(STATE=daily))
    else:
        with pytest.raises((AssertionError,FileNotFoundError)):scope['verify_recovered_gap'](SimpleNamespace(STATE=daily))
    assert (folder/'failed-latest.json').read_bytes()==raw


@pytest.mark.parametrize('state',['accept_started','accept_failed','accepted_pending_prune','prune_failed'])
def test_installer_refuses_any_unfinished_daily_before_apply_or_rollback(kit,monkeypatch,state):
    record={'version':'cloudbase-daily-accept-prune-v1','image':b.IMAGE,'helper_pins':kit['manifest']['new_helper_pins'],'state':state}
    monkeypatch.setattr(Path,'exists',lambda self:True);monkeypatch.setattr(i,'read',lambda file:b.wire(record))
    with pytest.raises(ValueError,match='UNFINISHED_DAILY_RECONCILIATION_REQUIRED'):i.latest_state(kit['manifest'])


@pytest.mark.parametrize('failure',['release_check','unit_verify','reload'])
def test_failed_switch_restores_exact_old_unit_and_release(kit,monkeypatch,failure):
    files=kit['files'];old_release=b'owned existing release';mapping={i.UNIT:files['previous.service'],i.RELEASE:old_release};calls=[]
    monkeypatch.setattr(i,'materialize',lambda files:None);monkeypatch.setattr(i,'directory',lambda *a,**k:None)
    monkeypatch.setattr(i,'read',lambda path:mapping.get(path,old_release))
    monkeypatch.setattr(i,'sha',lambda raw:b.OLD_RELEASE_SHA if raw==old_release else b.sha(raw))
    monkeypatch.setattr(i,'write',lambda path,raw,*a,**k:mapping.__setitem__(path,raw))
    broken=[False]
    def run(args):
        calls.append(args)
        should_fail=(failure=='release_check' and args[0]=='/usr/bin/python3' or failure=='unit_verify' and args[0]=='systemd-analyze' or failure=='reload' and args==['systemctl','daemon-reload'])
        if should_fail and not broken[0]:broken[0]=True;raise ValueError('OWNED_FAILURE')
        if args[0]=='/usr/bin/python3':return json.dumps({'exit_code':0,'network':'none','result':{'test_attestation_sha256':kit['manifest']['core_proof']['attestation_sha256']}})
        return ''
    monkeypatch.setattr(i,'run',run)
    with pytest.raises(ValueError,match='OWNED_FAILURE'):i.apply({'kit_sha256':'a'*64},kit['manifest'],files)
    assert mapping[i.UNIT]==files['previous.service'] and mapping[i.RELEASE]==old_release
    assert all(not (args[:2]==['systemctl','start'] or args[:2]==['systemctl','restart']) for args in calls)


def test_actual_backup_lock_inode_excludes_v2c_install(tmp_path):
    from services.hub.locks import resource_guard
    app=tmp_path/'app';ops=tmp_path/'ops';daily=ops/'retention-v2b/daily-state';daily.mkdir(parents=True)
    paths=i.lock_paths(app,daily,ops)
    for file,_ in paths[:2]:file.write_bytes(b'');file.chmod(0o600)
    folder=app/'data/cloudbase-backup-state/runner.lock'
    with resource_guard(folder):
        assert paths[2][0]==folder/'observer.lock' and paths[2][0].is_file()
        if os.name=='posix':
            owned=[(file,os.getuid()) for file,_ in paths]
            with pytest.raises(BlockingIOError):i.acquire_locks(owned)
        else:
            import msvcrt
            with paths[2][0].open('r+b') as competing:
                with pytest.raises(OSError):msvcrt.locking(competing.fileno(),msvcrt.LK_NBLCK,1)
    if os.name=='posix':
        fds=i.acquire_locks(owned)
        for fd in fds:os.close(fd)


@pytest.mark.parametrize('running',['backup','registry'])
def test_cutover_is_refused_when_any_backup_or_registry_is_running(monkeypatch,running):
    def run(args):
        if args[0]=='systemctl':return 'ActiveState=inactive\nFragmentPath='+str(i.UNIT)+'\nDropInPaths=\n'
        if running=='backup' and args[-1]=='label=org.lighthouse.backup.sidecar':return 'owned-running-sidecar\n'
        if running=='registry' and args[-1]=='name=^/lighthouse-archive-registry-':return 'owned-running-registry\n'
        return ''
    monkeypatch.setattr(i,'run',run)
    with pytest.raises(ValueError,match='BACKUP_OR_REGISTRY_RUNNING'):i.quiescent()


@pytest.mark.parametrize('fail_reload',[False,True])
def test_rollback_preserves_completed_history_and_restores_pointer_on_failure(kit,tmp_path,monkeypatch,fail_reload):
    daily=tmp_path/'daily';(daily/'history').mkdir(parents=True);state=tmp_path/'state'
    history=state/'install-history'/('a'*64);history.mkdir(parents=True)
    unit=tmp_path/'unit';release=tmp_path/'release';timer=tmp_path/'timer'
    old_release=b'owned-old-release';new_release=b'owned-new-release';timer_raw=b'owned-fixed-timer'
    (history/'previous.release.json').write_bytes(old_release);release.write_bytes(new_release);timer.write_bytes(timer_raw)
    files=kit['files'];unit.write_bytes(files['candidate.service'])
    ident='12345678-1234-1234-1234-123456789012'
    record={'version':'cloudbase-daily-accept-prune-v1','image':b.IMAGE,'daily_id':ident,'state':'completed','helper_pins':kit['manifest']['new_helper_pins']}
    raw=b.wire(record);(daily/'latest.json').write_bytes(raw);(daily/'history'/(ident+'.json')).write_bytes(raw)
    for name,value in [('DAILY',daily),('STATE',state),('UNIT',unit),('RELEASE',release),('TIMER',timer)]:monkeypatch.setattr(i,name,value)
    monkeypatch.setattr(i,'read',lambda path:path.read_bytes())
    monkeypatch.setattr(i,'sha',lambda raw:b.OLD_RELEASE_SHA if raw==old_release else b.TIMER_SHA if raw==timer_raw else b.sha(raw))
    monkeypatch.setattr(i,'write',lambda path,raw,*a,**k:path.write_bytes(raw))
    synced=[];monkeypatch.setattr(i,'sync_directories',lambda *paths:synced.append(paths))
    before={'owned-core':'unchanged'};monkeypatch.setattr(i,'core',lambda:before)
    failed=[False];calls=[]
    def run(args):
        calls.append(args)
        if args==['systemctl','daemon-reload']:
            if fail_reload and not failed[0]:failed[0]=True;raise ValueError('OWNED_RELOAD_FAILURE')
            return ''
        return 'owned-state'
    monkeypatch.setattr(i,'run',run)
    plan={'kit_sha256':'a'*64,'core_before':before,'timer_state':'owned-state'}
    retained=history/('completed-latest-'+ident+'.json')
    if fail_reload:
        with pytest.raises(ValueError,match='OWNED_RELOAD_FAILURE'):i.rollback(plan,kit['manifest'],files)
        assert (daily/'latest.json').read_bytes()==raw and not retained.exists()
        assert unit.read_bytes()==files['candidate.service'] and release.read_bytes()==new_release
    else:
        result=i.rollback(plan,kit['manifest'],files)
        assert result['files_deleted']==0 and result['timer_unchanged']
        assert not (daily/'latest.json').exists() and retained.read_bytes()==raw
        assert unit.read_bytes()==files['previous.service'] and release.read_bytes()==old_release
    assert (daily/'history'/(ident+'.json')).read_bytes()==raw and timer.read_bytes()==timer_raw
    assert synced and all(paths==(daily,history) for paths in synced)
    assert all(args[:2] not in (['systemctl','start'],['systemctl','restart']) for args in calls)


def test_historical_builder_refuses_changed_current_operator_instead_of_requalifying_it(kit):
    name='scripts/ops/prune_cloudbase_verified_clones_v1.py'
    raw=(REPO/name).read_bytes()
    assert b.sha(raw)!=b.UNCHANGED[name]
    (kit['repo']/name).write_bytes(raw)
    with pytest.raises(ValueError,match='REVIEWED_RUNTIME_OR_HOST_CHANGED'):
        b.build(*kit['args'])
