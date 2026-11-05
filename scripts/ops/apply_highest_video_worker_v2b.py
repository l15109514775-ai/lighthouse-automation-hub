"""Default plan; apply one tested handler file only when video queue is idle.

Never stops an active download, changes policy/data or resets a task. The
standalone deployer is loaded only after its frozen source hash is verified.
"""
from copy import deepcopy
from pathlib import Path
import argparse,hashlib,importlib.util,json,os,re,time,uuid
import urllib.request
import xml.etree.ElementTree as ET

COMMIT='243855ac107715350b59941b4118b415ac416b7d'
ARCHIVE='050a3fcd3fd3f169d3bbfce89e0ffc7661255d9ddb4012633095ae46694f2249'
DEPL_SHA='50144dbe19d38329b549ca7fb5e64e2ea9454a07d4b48fd5ca02bd6efcf35cb1'
NAME='services/hub/replay_video_browser.py'
OLD='399ee4b5877c76e7d75ca2d9c4cd0e8e14df3fe3e3033487ea90ff56234ee8e4'
NEW='a7cd7e743d50c7be226c5b087422b825707676494df974d4c7027d5bc9d52446'
GROUPS={'tests/test_replay_video_worker_native.py':3,'tests/test_replay_worker.py':17}


def validate_delta(value,d):
    assert value['version']=='replay-highest-v2b-scan-delta-ubuntu-tests-v1' and value['image']==d.IMAGE
    assert value['archive_sha256']==ARCHIVE and value['full_suite_passed'] is True and value['tests']==20
    assert value['groups']==GROUPS and value['failures']==value['errors']==value['skipped']==value['exit_code']==0
    assert value['oom_killed'] is False and value['network']=='none'
    assert all(value[k] is True for k in ('runtime_readonly_overlays_only','runtime_matches_declared_candidate','mounts_match_isolated_allowlist','no_pid_or_oom_denial'))
    assert all(value[k] is False for k in ('application_data_mounted','production_profile_mounted','secrets_environment_injected','whole_tree_deployed'))
    manifest=value['manifest'];assert manifest['version']=='replay-highest-v2b-scan-delta-ro-overlay-v1'
    assert manifest['source_freeze_commit']==COMMIT and manifest['parent_source_archive_sha256']==d.ARCHIVE
    assert manifest['changed_files']==[NAME,'tests/test_replay_video_worker_native.py'] and manifest['production_runtime_changed_files']==[NAME]
    assert manifest['expected_total_tests']==20 and manifest['expected_test_counts']==GROUPS and len(manifest['files'])==82
    assert all(manifest[k] is False for k in ('freshness_seconds_changed','stored_observed_at_changed','final_reservation_verification_removed'))
    assert manifest['files'][NAME]['sha256']==NEW
    return manifest


def delta_gate(path,expected_sha,d):
    path=d.safe(path);assert path.name=='test-attestation.json' and path.parent.parent==d.EV
    prefix='replay-highest-v2b-scan-delta-ro-overlay-v1-';assert path.parent.name.startswith(prefix)
    ident=path.parent.name[len(prefix):];assert str(uuid.UUID(ident))==ident
    raw=d.read(path,500000);assert d.sha(raw)==expected_sha and re.fullmatch('[a-f0-9]{64}',expected_sha)
    value=json.loads(raw);manifest=validate_delta(value,d)
    owned=d.HOME/('lighthouse-replay-highest-owned-v2-'+ident)
    report=d.read(owned/'results.xml',20_000_000);assert d.sha(report)==value['junit_sha256']
    cases=list(ET.fromstring(report).iter('testcase'));counts={key:0 for key in GROUPS}
    assert len(cases)==20 and all(not any(c.find(key) is not None for key in ('failure','error','skipped')) for c in cases)
    for case in cases:
        names=[n for n in GROUPS if case.attrib.get('classname','').endswith(Path(n).stem)];assert len(names)==1;counts[names[0]]+=1
    assert counts==GROUPS and d.sha(d.read(path.parent/'tests.log',20_000_000))==value['tests_log_sha256']
    assert json.loads(d.read(path.parent/'overlay/manifest.json'))==manifest
    for name,entry in manifest['files'].items():
        assert not Path(name).is_absolute() and '..' not in Path(name).parts
        body=d.read(path.parent/'overlay'/name,500000);assert len(body)==entry['bytes'] and d.sha(body)==entry['sha256']
    container=json.loads(d.read(path.parent/'container.json',5_000_000));host=container['HostConfig']
    assert container['Id']==value['container_id'] and container['Image']==d.IMAGE and container['Config']['User']=='10001:10001'
    assert container['State']['ExitCode']==0 and container['State']['OOMKilled'] is False
    assert host.get('Init') is True and host['ReadonlyRootfs'] is True and host['NetworkMode']=='none'
    assert host['PidsLimit']==256 and host['Memory']==1400*1024**2 and host['ShmSize']==256*1024**2
    for label in ('before','after'):
        resource=json.loads(d.read(owned/('resource-'+label+'.json')));assert resource==value['resource_snapshot_'+label]
        assert int(resource['pids.events'].get('max',0))==int(resource['memory.events'].get('oom',0))==int(resource['memory.events'].get('oom_kill',0))==0
    return value,{'file':str(path),'sha256':expected_sha,'tests':20}


def patch_config(before,target,d):
    result=deepcopy(before);mounts=result['services']['video-worker']['volumes']
    matching=[v for v in mounts if v.get('type')=='bind' and v.get('target')==str(d.ROOT/NAME)]
    assert len(matching)==1 and matching[0].get('read_only') is True
    assert matching[0]['source']==str(d.INSTALL/'code'/NAME)
    matching[0]['source']=str(target);d.assert_other_services(before,result)
    expected=deepcopy(result);next(v for v in expected['services']['video-worker']['volumes'] if v.get('target')==str(d.ROOT/NAME))['source']=str(d.INSTALL/'code'/NAME)
    assert expected==before
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--attestation',required=True);parser.add_argument('--attestation-sha256',required=True)
    parser.add_argument('--deployer-file',default=str(Path(__file__).with_name('deploy_highest_video_worker_v2.py')))
    parser.add_argument('--activate',action='store_true');args=parser.parse_args()
    assert os.name=='posix' and os.geteuid()==0
    helper=Path(args.deployer_file);assert helper.is_absolute() and helper.resolve()==helper and not helper.is_symlink()
    assert hashlib.sha256(helper.read_bytes()).hexdigest()==DEPL_SHA
    module=importlib.util.spec_from_file_location('lighthouse_frozen_highest_deployer',helper);d=importlib.util.module_from_spec(module);module.loader.exec_module(d)
    source_path=d.safe(Path(args.attestation));value,reference=delta_gate(source_path,args.attestation_sha256,d)
    original=d.read(d.ROOT/'compose.yaml');original_stat=(d.ROOT/'compose.yaml').stat();before=d.compose_config()
    ids={n:d.container_id(n) for n in d.CORE};current=d.inspect(d.container_id('video-worker'))
    runtime={name:d.read(d.INSTALL/'code'/name,500000) for name in value['manifest']['files'] if d.runtime_source(name)}
    for name,body in runtime.items():assert d.sha(body)==(OLD if name==NAME else value['manifest']['files'][name]['sha256'])
    service=deepcopy(before['services']['video-worker'])
    for volume in service['volumes']:volume.setdefault('read_only',False)
    d.validate_running(current,service,runtime)
    target=d.INSTALL/'deltas'/COMMIT/NAME
    mounted=[v for v in before['services']['video-worker']['volumes'] if v.get('target')==str(d.ROOT/NAME)]
    if len(mounted)==1 and mounted[0]['source']==str(target):
        assert mounted[0].get('read_only') is True and d.sha(d.read(target,500000))==NEW
        print(json.dumps({'version':'highest-video-worker-v2b-patch-v1','state':'already_applied_verified','activated':True,
            'video_container_id':current['Id'],'new_sha256':NEW,'jobs_reset':False,'services_changed':False}));return
    candidate=patch_config(before,target,d)
    status={'version':'highest-video-worker-v2b-patch-v1','state':'verified_plan_only','attestation':reference,
        'parent_archive_sha256':d.ARCHIVE,'changed_runtime_files':[NAME],'old_sha256':OLD,'new_sha256':NEW,
        'source_commit':COMMIT,'other_service_container_ids':ids,'data_policy_or_hub_changed':False,'jobs_reset':False,
        'activated':False,'video_processing':d.snapshot()['video_processing']}
    if not args.activate:print(json.dumps(status));return
    import fcntl
    queue=d.safe(d.ROOT/'data/locks/video/observer.lock');fd=os.open(queue,os.O_RDONLY|os.O_NOFOLLOW)
    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd);status['state']='deferred_active_video';print(json.dumps(status));return
    try:
        if d.snapshot()['video_processing']:
            status['state']='deferred_active_video';print(json.dumps(status));return
        assert d.read(d.ROOT/'compose.yaml')==original and all(d.container_id(n)==i for n,i in ids.items())
        target.parent.mkdir(parents=True,mode=0o755,exist_ok=True);d.safe(target)
        directories=set()
        for parent in (target.parent,*target.parent.parents):
            if not parent.is_relative_to(d.INSTALL):break
            assert parent.stat().st_uid==0 and not parent.stat().st_mode&0o022;os.chmod(parent,0o755);directories.add(parent)
        body=d.read(source_path.parent/'overlay'/NAME,500000);assert d.sha(body)==NEW
        if target.exists():assert d.read(target,500000)==body
        else:d.atomic(target,body,mode=0o644)
        for directory in sorted(directories,key=lambda p:len(p.parts),reverse=True):
            descriptor=os.open(directory,os.O_DIRECTORY)
            try:os.fsync(descriptor)
            finally:os.close(descriptor)
        evidence=d.EV/('highest-video-worker-v2b-patch-'+str(uuid.uuid4()));d.private_directory(evidence)
        d.atomic(evidence/'compose-before.yaml',original);d.atomic(evidence/'video-before.json',d.wire(d.filtered(current)))
        d.compose('stop','--timeout','45','video-worker');assert d.inspect(current['Id'])['State']['Running'] is False
        assert not d.snapshot()['video_processing']
        d.atomic(d.ROOT/'compose.yaml',d.wire(candidate),original=original_stat);d.assert_other_services(before,d.compose_config())
        started=time.time();d.compose('up','-d','--no-deps','--no-build','video-worker')
        updated=d.inspect(d.container_id('video-worker'))
    except BaseException:
        status.update(state='patch_requires_reconciliation',activated=False,automatic_rollback_performed=False)
        if 'evidence' in locals():d.atomic(evidence/'patch-report.json',d.wire(status))
        print(json.dumps(status));raise RuntimeError('PATCH_REQUIRES_RECONCILIATION') from None
    finally:os.close(fd)
    # Release the queue lock before waiting for the new normal Worker heartbeat.
    expected=deepcopy(candidate['services']['video-worker'])
    for volume in expected['volumes']:volume.setdefault('read_only',False)
    try:
        d.validate_running(updated,expected,runtime);assert d.sha(d.read(target,500000))==NEW
        until=time.monotonic()+45
        while d.snapshot()['heartbeat']<started:
            assert d.inspect(updated['Id'])['State']['Running'] is True
            if time.monotonic()>until:raise RuntimeError('PATCH_HEARTBEAT_REQUIRED')
            time.sleep(1)
        with urllib.request.urlopen('http://127.0.0.1:8080/ready',timeout=5) as response:assert response.status==200
        assert all(d.container_id(n)==i and d.inspect(i)['State']['Running'] for n,i in ids.items())
        d.assert_other_services(before,d.compose_config())
        status.update(state='single_handler_patch_active',activated=True,video_container_id=updated['Id'],other_services_unchanged=True,ready=True)
    except BaseException:
        status.update(state='patch_requires_reconciliation',activated=False,automatic_rollback_performed=False)
        d.atomic(evidence/'patch-report.json',d.wire(status));print(json.dumps(status));raise RuntimeError('PATCH_REQUIRES_RECONCILIATION') from None
    d.atomic(evidence/'video-after.json',d.wire(d.filtered(d.inspect(updated['Id']))));d.atomic(evidence/'patch-report.json',d.wire(status));print(json.dumps(status))


if __name__=='__main__':main()
