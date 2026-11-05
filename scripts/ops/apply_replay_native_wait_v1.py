"""Idle-only exact V2-68 four-source repair; default plan never deploys."""
from copy import deepcopy
from pathlib import Path
import argparse,hashlib,json,os,re,stat,subprocess,time,types,uuid
import urllib.request

OPERATOR_SHA='3b6093639434dae80cadbe65392f444f7f45b3b4455d1ea53fd2ab4688e991b8'
PILOT='594aed06-3aa1-45f4-90f0-1db49a602395'
SOURCE='9T5tn1WGlJg'
DOWNLOAD='d28685f1-03fc-4a12-9d85-5dbf14107f82'
RELEASE='c898664-3b48ec2-native-wait-v1'
ARCHIVE='e092f9d0ce11e83feb8abf3444e90c822be6c64210a6cbeb14329d2fbdfb1ec9'
PARENT='fdea68bae719a0f20698558fd9601bc39bea930fd2000fbbbea1e26c370a843b'
PRODUCTION={
 'services/hub/short_video_web.py':'ca7f6c3530a9495ce276db4bfa3d11d50cad062a55d4d4e2c33973f73437db3b',
 'services/hub/replay_video_web.py':'d82ef20a0e990d835577b0737cf2eefd2324b4215026352963694ee18f0e1046',
 'services/hub/replay_video_browser.py':'2b0fcdf25c67ccf339d380b972a62d514bb82726bcd2ebb7a58c598b7c13573f',
 'scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs':'d25de4d718bff78d487fb3aae7e9b1739a4ddfbef2d5eb170843049d283d5e23'}
GROUPS={'tests/test_replay_packet_cache_v1.py':20,'tests/test_replay_highest_video_v2.py':2,
 'tests/test_replay_native_driver.py':1,'tests/test_replay_native_snapshot_delta.py':34,'tests/test_metadata_v5_async.py':11}
METADATA_ERRORS={'REPLAY_BROWSER_METADATA_PREPARATION_FAILED','METADATA_NATIVE_CONTROL_AMBIGUOUS','METADATA_CATEGORY_CHANGED'}

def sha(raw):return hashlib.sha256(raw).hexdigest()

def load_operator(file):
    file=Path(file);assert file.is_absolute() and file.resolve()==file and not file.is_symlink()
    raw=file.read_bytes();assert len(raw)<=500000 and sha(raw)==OPERATOR_SHA
    module=types.ModuleType('pinned_cache_operator');module.__file__=str(file)
    exec(compile(raw,str(file),'exec'),module.__dict__);return module

def qualification(path,digest,m,d):
    key='native_wait'
    assert key not in m.SPECS
    m.SPECS[key]=('native-snapshot-metadata-v2mix68-ubuntu-tests-v1','native-snapshot-metadata-v2mix68-ro-overlay-v1',
        'native-snapshot-metadata-v2mix68-v1-ro-overlay-','lighthouse-native-snapshot-metadata-v2mix68-v1-owned-',68,92,ARCHIVE)
    try:value,reference=m.gate(path,digest,key,d)
    finally:m.SPECS.pop(key,None)
    manifest=value['manifest']
    assert manifest['parent_archive_sha256']==PARENT and manifest['parent_expected_total_tests']==21
    assert manifest['python_source_commit']=='c8986648255244274bbfd1d2a882d5e06cf5681d'
    assert manifest['native_source_commit']=='3b48ec2cc092a13273ca6f7a2809319478d42d49'
    assert manifest['natural_sources_installed'] is False and manifest['native_freshness_seconds_unchanged']==300
    assert manifest['source_current_full_sha_required_each_call'] is True
    assert value['mounted_sources_match_manifest'] is value['host_source_sha256_unchanged'] is True and value['timed_out'] is False
    assert manifest['production_runtime_changed_files']==list(PRODUCTION) or set(manifest['production_runtime_changed_files'])==set(PRODUCTION)
    assert value['groups']==GROUPS and len(manifest['runtime_source_sha256'])==45
    for label in ('before','after'):
        assert value['mounted_source_sha256_'+label]=={name:entry['sha256'] for name,entry in manifest['files'].items()}
    assert all(manifest['runtime_source_sha256'][name]==digest for name,digest in PRODUCTION.items())
    return value,reference

def validate_source_change(old,new):
    assert len(old)==45 and set(old)==set(new)
    assert {name for name in old if old[name]!=new[name]}==set(PRODUCTION)
    assert all(new[name]==digest for name,digest in PRODUCTION.items())

def candidate_config(before,target,d):
    after=deepcopy(before);volumes=after['services']['video-worker']['volumes'];old={}
    for name in PRODUCTION:
        matches=[v for v in volumes if v['target']==str(d.ROOT/name)]
        assert len(matches)==1 and matches[0].get('read_only') is True
        old[name]=matches[0]['source'];matches[0]['source']=str(target/name)
    reverse=deepcopy(after)
    for v in reverse['services']['video-worker']['volumes']:
        if v['target'] in {str(d.ROOT/name) for name in PRODUCTION}:
            name=v['target'][len(str(d.ROOT))+1:];v['source']=old[name]
    assert reverse==before;d.assert_other_services(before,after)
    return after

def preflight(d,operator,sources,key):
    # Reuse the exact deployed snapshot/preflight path, adding hard fences
    # before factory imports. This only changes a private module view/string.
    marker='socket.socket.connect=refuse;Store.claim=refuse'
    assert d.PRECHECK.count(marker)==1
    view=types.SimpleNamespace(**vars(d))
    view.PRECHECK=d.PRECHECK.replace(marker,'import threading\nthreading.Thread.start=refuse;Store.finish=refuse\n'+marker)
    return operator.preflight(view,sources,key)

def job_payload_safe(row,result,pubs,intentnames):
    binding=row['payload'];state=result['replay_highest_video_web_v2']
    assert row['job_id']==PILOT and row['job_type']=='replay_highest_video_publish_v2' and row['source_platform']=='youtube'
    assert row['source_id']==SOURCE and binding['source_id']==SOURCE and binding['download_job_id']==DOWNLOAD
    assert row['state']=='awaiting_review' and row['attempts']==2 and row['error_code'] in METADATA_ERRORS
    assert row['lease_owner'] is None and row['lease_until'] is None and row['external_publish_id'] is None and not pubs
    assert state['state']=='uploaded' and state['version']=='replay-highest-video-website-v2'
    assert str(uuid.UUID(state['attempt_id']))==state['attempt_id']
    assert state['public_submission_approved'] is False and state['human_review'] is False and result['publish_performed'] is False
    assert 'owner_acceptance' not in state and not state.get('result_observation') and not state.get('machine_acceptance')
    assert intentnames=={'video-selection.json'}

QUERY=r'''
from pathlib import Path
import json,os,sqlite3,sys
assert os.getuid()==10001
db=sqlite3.connect(Path(sys.argv[1]).as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
row=dict(db.execute('SELECT * FROM jobs WHERE job_id=?',(sys.argv[2],)).fetchone())
row['payload']=json.loads(row['payload']);row['result']=json.loads(row['result'])
pubs=[dict(r) for r in db.execute('SELECT * FROM publications WHERE job_id=? OR (source_platform=? AND source_id=?)',(sys.argv[2],'youtube',row['source_id']))]
value={'row':row,'publications':pubs,'video_processing':db.execute("SELECT COUNT(*) FROM jobs WHERE queue='video' AND state='processing'").fetchone()[0],
 'heartbeats':dict(db.execute("SELECT name,at FROM heartbeats WHERE name IN ('worker:video','worker:video-main-v2b','worker:video-moderation-v2b')"))}
print(json.dumps(value));db.close()
'''

def observation(d):
    def app():os.setgroups([]);os.setgid(10001);os.setuid(10001)
    process=subprocess.run(['/usr/bin/python3','-c',QUERY,str(d.ROOT/'data/hub.sqlite3'),PILOT],
        capture_output=True,text=True,timeout=20,preexec_fn=app,cwd='/tmp',env={'PATH':'/usr/bin:/bin','HOME':'/tmp','PYTHONNOUSERSITE':'1'})
    assert process.returncode==0
    value=json.loads(process.stdout);value['row_sha256']=sha(d.wire(value['row']))
    folder=d.safe(d.ROOT/'media'/PILOT/'replay-native-intents');assert folder.is_dir()
    value['intents']={}
    for file in folder.iterdir():
        d.safe(file);assert file.is_file();value['intents'][file.name]=sha(d.read(file,500000))
    return value

def validate_installed(d,m,operator,before,current,target,new):
    service=deepcopy(before['services']['video-worker'])
    for v in service['volumes']:v.setdefault('read_only',False)
    operator.validate_running(d,current,service)
    volumes={v['target']:v for v in service['volumes'] if v['target'].startswith(str(d.ROOT)+'/') and d.runtime_source(v['target'][len(str(d.ROOT))+1:])}
    assert set(volumes)=={str(d.ROOT/name) for name in new}
    for name,digest in new.items():
        v=volumes[str(d.ROOT/name)];file=Path(v['source']);m.protected(file);assert v['read_only'] is True
        expected=(target/name if name in PRODUCTION else d.INSTALL/'deltas'/('packet-cache-'+operator.COMMIT)/name if name in operator.PINS else
            d.INSTALL/'deltas'/'moderation-8ea790fdcf5f6a6625d36c5fc309d57d37abc66d'/name if name in operator.MOD else d.INSTALL/'code'/name)
        assert file==expected and sha(d.read(file,500000))==digest
    return service

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for kind in ('legacy','mix','repair'):p.add_argument('--'+kind+'-attestation',required=True);p.add_argument('--'+kind+'-sha256',required=True)
    p.add_argument('--installer-file',required=True);p.add_argument('--deployer-file',required=True);p.add_argument('--cache-operator-file',required=True)
    p.add_argument('--activate',action='store_true');args=p.parse_args()
    assert os.name=='posix' and os.geteuid()==0
    operator=load_operator(args.cache_operator_file);m=operator.load(args.installer_file);d=m.helper(args.deployer_file)
    legacy,lref=d.gate(Path(args.legacy_attestation),args.legacy_sha256)
    mixed,mref=operator.mixed_gate(Path(args.mix_attestation),args.mix_sha256,m,d)
    repair,rref=qualification(Path(args.repair_attestation),args.repair_sha256,m,d)
    old=mixed['manifest']['candidate_runtime_source_sha256'];new=repair['manifest']['runtime_source_sha256'];validate_source_change(old,new)
    overlay=Path(args.repair_attestation).parent/'overlay';target=d.INSTALL/'deltas'/RELEASE
    m.protected(d.ROOT/'compose.yaml');original=d.read(d.ROOT/'compose.yaml');original_stat=(d.ROOT/'compose.yaml').stat()
    before=d.compose_config();current=d.inspect(d.container_id('video-worker'));processes=m.core_processes(d)
    service=before['services']['video-worker'];volumes={v['target']:v for v in service['volumes']}
    active=all(volumes.get(str(d.ROOT/name),{}).get('source')==str(target/name) for name in PRODUCTION)
    if active:
        validate_installed(d,m,operator,before,current,target,new)
        environment=json.loads(d.run(['/usr/bin/docker','exec','--user','10001:10001',current['Id'],'python','-c',operator.VERIFY_ENV]))
        assert environment['persistent_runtime_environment_directories_verified'] is True
        print(json.dumps({'version':'replay-native-wait-v1-deployment','state':'already_applied_verified','activated':True,'video_container_id':current['Id'],'repair_attestation':rref}));return
    sources,cache_active=operator.current_sources(d,m,before,current,legacy,d.INSTALL/'deltas'/('packet-cache-'+operator.COMMIT));assert cache_active
    operator.validate_mixed_sources(d,sources,mixed)
    initial=observation(d);job_payload_safe(initial['row'],initial['row']['result'],initial['publications'],set(initial['intents']))
    assert initial['video_processing']==0
    key=[v['source'] for v in service['volumes'] if v['target']=='/run/secrets/youtube-api-key'];assert len(key)==1;key=d.key_file(key[0])
    candidate_sources={name:overlay/name for name in new}
    assert all(sha(d.read(file,500000))==new[name] for name,file in candidate_sources.items())
    prepared=preflight(d,operator,candidate_sources,key)
    configuration={str(file):sha(d.read(file,1048576)) for file in (d.ROOT/'config/hub.toml',d.ROOT/'config/youtube-highest-download-policy.toml',d.ROOT/'config/replay-highest-video-web-policy.toml')}
    after=candidate_config(before,target,d)
    report={'version':'replay-native-wait-v1-deployment','state':'verified_plan_only','activated':False,
        'references':{'legacy':lref,'current_mix':mref,'repair':rref},'production_changes':PRODUCTION,
        'preflight':prepared,'target_job_id':PILOT,'job_row_sha256':initial['row_sha256'],'intents_sha256':initial['intents'],
        'configuration_sha256':configuration,'other_core_processes':processes,'entry_changed':False,'hub_or_policy_changed':False,
        'jobs_or_attempts_changed':False,'files_selected':0,'public_submissions':0}
    if not args.activate:print(json.dumps(report));return
    import fcntl
    globalfd=os.open('/run/lighthouse-replay-native-wait-v1-deploy.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    fcntl.flock(globalfd,fcntl.LOCK_EX|fcntl.LOCK_NB);locks=[]
    try:
        for role in ('video','moderation'):
            fd=m.role_lock(d,role)
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:os.close(fd);report['state']='deferred_active_video_or_lookup';print(json.dumps(report));return
            locks.append(fd)
        fresh=observation(d);job_payload_safe(fresh['row'],fresh['row']['result'],fresh['publications'],set(fresh['intents']))
        assert fresh['video_processing']==0 and fresh['row_sha256']==initial['row_sha256'] and fresh['intents']==initial['intents']
        assert d.read(d.ROOT/'compose.yaml')==original and m.core_processes(d)==processes
        assert all(sha(d.read(Path(file),1048576))==digest for file,digest in configuration.items())
        operation_id=str(uuid.uuid4());evidence=d.EV/('replay-native-wait-v1-deploy-'+operation_id);d.private_directory(evidence)
        report.update(evidence=str(evidence),deployment_report=str(evidence/'deployment-report.json'))
        # Raw Compose can contain environment values. Keep its rollback copy
        # outside exported evidence, root-readable only; never serialize Env.
        private_backup=Path('/var/lib/lighthouse-replay-ops/native-wait')/operation_id/'compose-before.yaml'
        m.install_file(d,private_backup,original,mode=0o600,group=0)
        report.update(private_compose_backup_file=str(private_backup),compose_before_sha256=sha(original),raw_config_exported=False)
        d.atomic(evidence/'video-before.json',d.wire(d.filtered(current)))
        for name,digest in PRODUCTION.items():
            body=d.read(overlay/name,500000);assert sha(body)==digest
            if (target/name).exists():m.protected(target/name);assert d.read(target/name,500000)==body
            else:m.install_file(d,target/name,body,mode=0o644,group=0)
        d.compose('stop','--timeout','45','video-worker');assert not d.inspect(current['Id'])['State']['Running']
        stopped=observation(d);assert stopped['video_processing']==0 and stopped['row_sha256']==initial['row_sha256'] and stopped['intents']==initial['intents']
        d.atomic(d.ROOT/'compose.yaml',d.wire(after),original=original_stat);d.assert_other_services(before,d.compose_config())
        started=time.time();d.compose('up','-d','--no-deps','--no-build','video-worker');updated=d.inspect(d.container_id('video-worker'))
        for fd in reversed(locks):os.close(fd)
        locks=[]
        validate_installed(d,m,operator,d.compose_config(),updated,target,new)
        environment=json.loads(d.run(['/usr/bin/docker','exec','--user','10001:10001',updated['Id'],'python','-c',operator.VERIFY_ENV]))
        assert environment['persistent_runtime_environment_directories_verified'] is True
        deadline=time.monotonic()+60
        while True:
            final=observation(d)
            if all(final['heartbeats'].get(name,0)>=started for name in ('worker:video','worker:video-main-v2b','worker:video-moderation-v2b')):break
            assert d.inspect(updated['Id'])['State']['Running'] and time.monotonic()<deadline;time.sleep(1)
        assert final['row_sha256']==initial['row_sha256'] and final['intents']==initial['intents']
        assert all(sha(d.read(Path(file),1048576))==digest for file,digest in configuration.items()) and m.core_processes(d)==processes
        d.assert_other_services(before,d.compose_config())
        with urllib.request.urlopen('http://127.0.0.1:8080/ready',timeout=5) as response:assert response.status==200
        report.update(state='active',activated=True,video_container_id=updated['Id'],ready=True,job_row_unchanged=True,
            runtime_environment=environment,other_core_processes_unchanged=True)
        d.atomic(evidence/'video-after.json',d.wire(d.filtered(updated)));d.atomic(evidence/'deployment-report.json',d.wire(report));print(json.dumps(report))
    except BaseException:
        report.update(state='requires_reconciliation',automatic_rollback_performed=False)
        if 'evidence' in locals():d.atomic(evidence/'deployment-report.json',d.wire(report))
        print(json.dumps(report));raise RuntimeError('NATIVE_WAIT_DEPLOY_REQUIRES_RECONCILIATION') from None
    finally:
        for fd in reversed(locks):os.close(fd)
        os.close(globalfd)

if __name__=='__main__':main()
