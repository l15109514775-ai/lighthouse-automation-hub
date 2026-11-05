"""Plan/activate the tested v2 known-BV side worker only when video is idle.

Validates original 234, scan-delta20 and query/queue11 raw evidence. Only
three read-only code mounts and the video command change. No job is created.
"""
from copy import deepcopy
from pathlib import Path
import argparse,hashlib,json,os,re,tempfile,time,types,uuid
import urllib.request
import xml.etree.ElementTree as ET

DEPL_SHA='50144dbe19d38329b549ca7fb5e64e2ea9454a07d4b48fd5ca02bd6efcf35cb1'
SCAN_SHA='136b8cf45c7274ca3ac5430cf6600c5763baa0f4c8955811c941a1cbcb59602d'
COMMIT='8ea790fdcf5f6a6625d36c5fc309d57d37abc66d'
ARCHIVE='7059be6f3e2dbd9dfc8ce83e0f2508d3e5c28c90021eeb3a5c22742b0efdd846'
GROUPS={'tests/test_replay_highest_moderation_v2b.py':7,'tests/test_queue_highest_pilot_v2.py':4}
ENTRY='services.hub.replay_highest_moderation_v2b'
PRODUCTION={'services/hub/replay_moderation_common.py':'beb29443f24316c37859d4fdba94f4a0a9ad505a327af58323ac30f26728c9e1',
 'services/hub/replay_highest_moderation_v2b.py':'f285c85f2d576d221f5b760902c661e8942c51fd6eb4f7e33a7e416baa5529cd',
 'services/hub/replay_video_browser.py':'a7cd7e743d50c7be226c5b087422b825707676494df974d4c7027d5bc9d52446'}

def load(file,expected,name):
    file=Path(file);assert file.is_absolute() and file.resolve()==file and not file.is_symlink()
    raw=file.read_bytes();assert hashlib.sha256(raw).hexdigest()==expected
    # Execute exactly the bytes just pinned; never reopen an ops-home path
    # between hashing and import. A later replacement cannot supply code.
    module=types.ModuleType(name);module.__file__=str(file);exec(compile(raw,str(file),'exec'),module.__dict__);return module

def protected(file):
    assert file.is_absolute() and file.resolve()==file
    for entry in (file,*file.parents):
        info=entry.lstat();assert not entry.is_symlink() and info.st_uid==0 and not info.st_mode&0o022

def precheck_script(d):
    check=d.PRECHECK.replace('from services.hub.replay_highest_worker_runtime_v2 import build_worker,load_youtube_key',
        'from services.hub.replay_highest_moderation_v2b import build_worker\nfrom services.hub.replay_worker_runtime import load_youtube_key')
    return check.replace('socket.socket.connect=refuse;Store.claim=refuse',
        'socket.socket.connect=refuse;Store.claim=Store.recover=Store.enqueue=refuse\nfrom services.hub.worker import Worker\nfrom services.hub.replay_moderation_common import QueueView\nWorker.once=Worker.run=refuse\nQueueView.claim=QueueView.recover=refuse')

def validate(value,d,unit):
    assert value['version']=='replay-highest-moderation-v2b-ubuntu-tests-v1' and value['image']==d.IMAGE
    assert value['full_suite_passed'] is True and value['tests']==11 and value['groups']==GROUPS
    assert value['failures']==value['errors']==value['skipped']==value['exit_code']==0 and value['oom_killed'] is False
    assert value['archive_sha256']==ARCHIVE and value['network']=='none'
    assert all(value[k] is True for k in ('runtime_readonly_overlays_only','runtime_matches_declared_candidate','mounts_match_isolated_allowlist','no_pid_or_oom_denial'))
    assert all(value[k] is False for k in ('application_data_mounted','production_profile_mounted','secrets_environment_injected','whole_tree_deployed'))
    m=value['manifest'];assert m['version']=='replay-highest-moderation-v2b-ro-overlay-v1' and m['source_freeze_commit']==COMMIT
    assert m['base_image']==d.IMAGE and m['expected_total_tests']==11 and m['expected_test_counts']==GROUPS and len(m['files'])==87
    assert m['previous_highest_234_archive_sha256']==d.ARCHIVE
    assert m['natural_v3_activated'] is m['publication_attempts_reset'] is m['whole_tree_deployment'] is m['application_data_included'] is m['credentials_included'] is False
    assert set(m['new_production_files'])==set(PRODUCTION)-{'services/hub/replay_video_browser.py'}
    for name,entry in unit['manifest']['files'].items():
        assert m['files'][name]['sha256']==(PRODUCTION[name] if name in PRODUCTION else entry['sha256'])
    assert all(m['files'][name]['sha256']==sha for name,sha in PRODUCTION.items())
    return m

def gate(path,sha,d,unit):
    path=d.safe(path);assert path.name=='test-attestation.json' and path.parent.parent==d.EV
    prefix='replay-highest-moderation-v2b-ro-overlay-v1-';assert path.parent.name.startswith(prefix)
    ident=path.parent.name[len(prefix):];assert str(uuid.UUID(ident))==ident
    assert re.fullmatch('[a-f0-9]{64}',sha) and d.sha(d.read(path,500000))==sha
    value=json.loads(d.read(path,500000));m=validate(value,d,unit)
    owned=d.HOME/('lighthouse-highest-moderation-owned-v2b-'+ident)
    raw=d.read(owned/'results.xml',20_000_000);assert d.sha(raw)==value['junit_sha256']
    cases=list(ET.fromstring(raw).iter('testcase'));counts={name:0 for name in GROUPS}
    assert len(cases)==11 and all(not any(c.find(k) is not None for k in ('failure','error','skipped')) for c in cases)
    for c in cases:
        match=[name for name in GROUPS if c.attrib.get('classname','').endswith(Path(name).stem)];assert len(match)==1;counts[match[0]]+=1
    assert counts==GROUPS and d.sha(d.read(path.parent/'tests.log',20_000_000))==value['tests_log_sha256']
    assert json.loads(d.read(path.parent/'overlay/manifest.json'))==m
    for name,entry in m['files'].items():
        assert not Path(name).is_absolute() and '..' not in Path(name).parts
        raw=d.read(path.parent/'overlay'/name,500000);assert len(raw)==entry['bytes'] and d.sha(raw)==entry['sha256']
    container=json.loads(d.read(path.parent/'container.json',5_000_000));host=container['HostConfig']
    assert container['Id']==value['container_id'] and container['Image']==d.IMAGE and container['Config']['User']=='10001:10001'
    assert host.get('Init') is True and host['ReadonlyRootfs'] is True and host['NetworkMode']=='none'
    assert host['PidsLimit']==256 and host['Memory']==1400*1024**2 and host['ShmSize']==256*1024**2
    assert container['State']['ExitCode']==0 and container['State']['OOMKilled'] is False
    for label in ('before','after'):
        resource=json.loads(d.read(owned/('resource-'+label+'.json')));assert resource==value['resource_snapshot_'+label]
        assert int(resource['pids.events'].get('max',0))==int(resource['memory.events'].get('oom',0))==int(resource['memory.events'].get('oom_kill',0))==0
    return value,{'file':str(path),'sha256':sha,'tests':11}

def candidate(before,target,d):
    after=deepcopy(before);service=after['services']['video-worker'];assert service['command']==['python','-m',d.ENTRY]
    volumes=service['volumes'];handler='services/hub/replay_video_browser.py'
    matches=[v for v in volumes if v.get('target')==str(d.ROOT/handler)]
    assert len(matches)==1 and matches[0].get('read_only') is True
    allowed=(d.INSTALL/'code'/handler,d.INSTALL/'deltas'/'243855ac107715350b59941b4118b415ac416b7d'/handler)
    assert matches[0]['source'] in tuple(map(str,allowed));matches[0]['source']=str(target/handler)
    for name in PRODUCTION:
        if name==handler:continue
        assert not any(v.get('target')==str(d.ROOT/name) for v in volumes)
        volumes.append(d.bind(target/name,d.ROOT/name,True))
    service['command']=['python','-m',ENTRY];d.assert_other_services(before,after)
    return after

def preflight(d,source_path,runtime,candidate_service):
    key=[v['source'] for v in candidate_service['volumes'] if v['target']=='/run/secrets/youtube-api-key'];assert len(key)==1;d.key_file(key[0])
    command=['/usr/bin/docker','run','--rm','--pull=never','--network=none','--read-only','--user','10001:10001','--cap-drop=ALL','--security-opt','no-new-privileges:true',
        '--init','--pids-limit=256','--memory=1400m','--shm-size=256m','--tmpfs','/tmp:rw,size=192m,mode=1777','-e','HUB_ROOT='+str(d.ROOT),
        '-e','HUB_CONFIG='+str(d.ROOT/'config/hub.toml'),'-e','HOME=/tmp/moderation-preflight','-e','PYTHONDONTWRITEBYTECODE=1']
    for source,target in ((d.ROOT/'config',d.ROOT/'config'),(key[0],'/run/secrets/youtube-api-key')):
        command+=['--mount','type=bind,source='+str(source)+',target='+str(target)+',readonly']
    for name in set(runtime)|set(PRODUCTION):command+=['--mount','type=bind,source='+str(source_path.parent/'overlay'/name)+',target='+str(d.ROOT/name)+',readonly']
    check=precheck_script(d)
    with tempfile.TemporaryDirectory(prefix='lighthouse-moderation-preflight-',dir='/run') as folder:
        file,snapshot=d.online_snapshot(Path(folder));command+=['--mount','type=bind,source='+str(file)+',target=/run/preflight/hub.sqlite3,readonly']
        result=json.loads(d.run(command+[d.IMAGE,'python','-c',check],timeout=90));assert result['ready'] is True
    return {**result,'database_snapshot':snapshot,'new_claim_role':'video plus scoped known-BV moderation'}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for prefix in ('unit','scan','query'):
        parser.add_argument('--'+prefix+'-attestation',required=True);parser.add_argument('--'+prefix+'-sha256',required=True)
    parser.add_argument('--deployer-file',default=str(Path(__file__).with_name('deploy_highest_video_worker_v2.py')))
    parser.add_argument('--scan-applier-file',default=str(Path(__file__).with_name('apply_highest_video_worker_v2b.py')))
    parser.add_argument('--activate',action='store_true');args=parser.parse_args();assert os.name=='posix' and os.geteuid()==0
    d=load(args.deployer_file,DEPL_SHA,'frozen_highest_deployer');scan=load(args.scan_applier_file,SCAN_SHA,'frozen_scan_applier')
    unit,_=d.gate(Path(args.unit_attestation),args.unit_sha256);scan.delta_gate(Path(args.scan_attestation),args.scan_sha256,d)
    source_path=d.safe(Path(args.query_attestation));value,reference=gate(source_path,args.query_sha256,d,unit)
    runtime={name:d.read(d.INSTALL/'code'/name,500000) for name in unit['manifest']['files'] if d.runtime_source(name)}
    assert len(runtime)==42 and all(d.sha(body)==unit['manifest']['files'][name]['sha256'] for name,body in runtime.items())
    for name in runtime:protected(d.INSTALL/'code'/name)
    original=d.read(d.ROOT/'compose.yaml');original_stat=(d.ROOT/'compose.yaml').stat();before=d.compose_config()
    ids={name:d.container_id(name) for name in d.CORE};current=d.inspect(d.container_id('video-worker'));service=deepcopy(before['services']['video-worker'])
    for volume in service['volumes']:volume.setdefault('read_only',False)
    d.validate_running(current,service,runtime)
    target=d.INSTALL/'deltas'/('moderation-'+COMMIT)
    if service['command']==['python','-m',ENTRY]:
        for name,sha in PRODUCTION.items():
            matches=[v for v in service['volumes'] if v['target']==str(d.ROOT/name)]
            assert len(matches)==1 and matches[0]['source']==str(target/name) and matches[0]['read_only'] is True and d.sha(d.read(target/name,500000))==sha
            protected(target/name)
        print(json.dumps({'state':'already_applied_verified','activated':True,'video_container_id':current['Id'],'jobs_reset':False}));return
    proposed=candidate(before,target,d);prepared=preflight(d,source_path,runtime,proposed['services']['video-worker'])
    status={'version':'highest-moderation-v2b-deployment-v1','state':'verified_plan_only','query_attestation':reference,
        'unit_attestation_sha256':args.unit_sha256,'scan_attestation_sha256':args.scan_sha256,'source_commit':COMMIT,
        'changed_runtime_files':list(PRODUCTION),'preflight':prepared,'jobs_created':0,'jobs_reset':False,'activated':False,
        'other_service_container_ids':ids,'video_processing':d.snapshot()['video_processing'],'hub_or_policy_changed':False}
    if not args.activate:print(json.dumps(status));return
    import fcntl
    globalfd=os.open('/run/lighthouse-highest-moderation-v2b-deploy.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    fcntl.flock(globalfd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    fd=os.open(d.safe(d.ROOT/'data/locks/video/observer.lock'),os.O_RDONLY|os.O_NOFOLLOW)
    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd);os.close(globalfd);status['state']='deferred_active_video';print(json.dumps(status));return
    try:
        if d.snapshot()['video_processing']:
            status['state']='deferred_active_video';print(json.dumps(status));return
        assert d.read(d.ROOT/'compose.yaml')==original and all(d.container_id(n)==ident for n,ident in ids.items())
        directories=set()
        for name,sha in PRODUCTION.items():
            file=target/name;file.parent.mkdir(parents=True,mode=0o755,exist_ok=True);d.safe(file)
            for parent in (file.parent,*file.parent.parents):
                if not parent.is_relative_to(d.INSTALL):break
                assert parent.stat().st_uid==0 and not parent.stat().st_mode&0o022;os.chmod(parent,0o755);directories.add(parent)
            body=d.read(source_path.parent/'overlay'/name,500000);assert d.sha(body)==sha
            if file.exists():assert d.read(file,500000)==body
            else:d.atomic(file,body,mode=0o644)
            protected(file)
        for folder in sorted(directories,key=lambda p:len(p.parts),reverse=True):
            descriptor=os.open(folder,os.O_DIRECTORY)
            try:os.fsync(descriptor)
            finally:os.close(descriptor)
        evidence=d.EV/('highest-moderation-v2b-deploy-'+str(uuid.uuid4()));d.private_directory(evidence)
        d.atomic(evidence/'compose-before.yaml',original);d.atomic(evidence/'video-before.json',d.wire(d.filtered(current)))
        d.compose('stop','--timeout','45','video-worker');assert not d.inspect(current['Id'])['State']['Running']
        assert not d.snapshot()['video_processing']
        d.atomic(d.ROOT/'compose.yaml',d.wire(proposed),original=original_stat);d.assert_other_services(before,d.compose_config())
        started=time.time();d.compose('up','-d','--no-deps','--no-build','video-worker');updated=d.inspect(d.container_id('video-worker'))
    except BaseException:
        status.update(state='requires_reconciliation',automatic_rollback_performed=False)
        if 'evidence' in locals():d.atomic(evidence/'deployment-report.json',d.wire(status))
        print(json.dumps(status));raise RuntimeError('MODERATION_DEPLOY_REQUIRES_RECONCILIATION') from None
    finally:os.close(fd);os.close(globalfd)
    expected=deepcopy(proposed['services']['video-worker'])
    for volume in expected['volumes']:volume.setdefault('read_only',False)
    d.validate_running(updated,expected,runtime)
    assert all(d.sha(d.read(target/name,500000))==sha for name,sha in PRODUCTION.items())
    deadline=time.monotonic()+45
    while True:
        names=d.snapshot();raw=d.run(['/usr/bin/docker','exec',updated['Id'],'python','-c',
            "from services.hub.config import Settings;from services.hub.db import Store;import json;d=Store(Settings.load().db_path);print(json.dumps(d.query(\"SELECT * FROM heartbeats WHERE name='worker:video-moderation-v2b'\")))"])
        side=json.loads(raw)
        if names['heartbeat']>=started and len(side)==1 and side[0]['at']>=started:break
        assert d.inspect(updated['Id'])['State']['Running'] and time.monotonic()<deadline;time.sleep(1)
    with urllib.request.urlopen('http://127.0.0.1:8080/ready',timeout=5) as response:assert response.status==200
    assert all(d.container_id(n)==ident and d.inspect(ident)['State']['Running'] for n,ident in ids.items())
    d.assert_other_services(before,d.compose_config());status.update(state='active',activated=True,video_container_id=updated['Id'],ready=True,other_services_unchanged=True)
    d.atomic(evidence/'video-after.json',d.wire(d.filtered(updated)));d.atomic(evidence/'deployment-report.json',d.wire(status));print(json.dumps(status))

if __name__=='__main__':main()
