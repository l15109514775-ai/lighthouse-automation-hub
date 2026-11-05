"""Idle-only two-file packet-proof optimization, with actual 295 evidence.

No policy, hub, entry, job or authorization changes. Existing source files
remain intact; the video service gains one new RO mount and one replacement.
"""
from copy import deepcopy
from pathlib import Path
import argparse,hashlib,json,os,subprocess,tempfile,time,types,uuid
import urllib.request

INSTALLER_SHA='7e1548ce72b662816ca052ab4bc057bd4de5fe8930842d71d1f9c38dc0c451c2'
COMMIT='cbb117c00e00cc25273f8110861e74a79b35b16b'
ARCHIVE='8fc11a3e27dca0dd2c08c12a34da111e70f37bc38ce74d6cdebb122840578e2a'
PILOT='594aed06-3aa1-45f4-90f0-1db49a602395'
MACHINE='services/hub/replay_machine_checks.py';CACHE='services/hub/replay_packet_cache_v1.py'
PINS={MACHINE:'1879757741d61173c9b5a27e6e3a210294b4c3efa9952e6b9483e97dd1fb6588',CACHE:'7727417e5064cd669d416ce9cee4209caaddc8b3e84a59c490031ad5c6b2a2f1'}
MOD={'services/hub/replay_moderation_common.py':'beb29443f24316c37859d4fdba94f4a0a9ad505a327af58323ac30f26728c9e1',
 'services/hub/replay_highest_moderation_v2b.py':'f285c85f2d576d221f5b760902c661e8942c51fd6eb4f7e33a7e416baa5529cd'}
DIRECTORIES={'/tmp/highest/home':'rw,size=8m,mode=0700,uid=10001,gid=10001',
 '/tmp/highest/config':'rw,size=16m,mode=0700,uid=10001,gid=10001',
 '/tmp/highest/cache':'rw,size=32m,mode=0700,uid=10001,gid=10001'}

def options(value):
    result={}
    for part in value.split(','):
        key,sep,text=part.partition('=');assert key not in result
        if key=='size':
            suffix=text[-1:].lower();result[key]=int(text[:-1])*{'k':1024,'m':1024**2,'g':1024**3}[suffix] if suffix in ('k','m','g') else int(text)
        elif key in ('uid','gid'):result[key]=int(text)
        elif key=='mode':result[key]=int(text,8)
        else:result[key]=text if sep else True
    return result

def validate_running(d,value,service):
    actual=value['HostConfig']['Tmpfs'];base={'/tmp',str(d.ROOT/'data/browser-profiles')};extra=set(DIRECTORIES)
    assert set(actual) in (base,base|extra)
    declared=dict(item.split(':',1) for item in service['tmpfs']);assert set(declared)==set(actual)
    assert all(options(actual[name])==options(declared[name]) for name in actual)
    normalized=deepcopy(value)
    if set(actual)==base|extra:
        assert all(options(actual[name])==options(expected) for name,expected in DIRECTORIES.items())
        # The legacy validator hard-codes two tmpfs targets. Remove only the
        # three already strictly validated additions, preserving every other
        # security/image/mount/environment/entry assertion in that validator.
        for name in DIRECTORIES:normalized['HostConfig']['Tmpfs'].pop(name)
    d.validate_running(normalized,service,{})

def load(path):
    file=Path(path);assert file.is_absolute() and file.resolve()==file and not file.is_symlink()
    raw=file.read_bytes();assert hashlib.sha256(raw).hexdigest()==INSTALLER_SHA
    module=types.ModuleType('pinned_natural_operator');module.__file__=str(file);exec(compile(raw,str(file),'exec'),module.__dict__);return module

def cache_gate(path,sha,m,d):
    m.SPECS['cache']=('replay-packet-cache-v1-ubuntu-tests','replay-packet-cache-v1-ro-overlay','replay-packet-cache-v1-ro-overlay-',
        'lighthouse-replay-packet-cache-owned-v1-',295,119,ARCHIVE)
    try:value,ref=m.gate(path,sha,'cache',d)
    finally:m.SPECS.pop('cache',None)
    manifest=value['manifest']
    assert manifest['source_freeze_commit']==COMMIT and set(manifest['production_runtime_changed_files'])==set(PINS)
    assert manifest['native_freshness_seconds_unchanged']==300 and manifest['native_observed_at_changed'] is False
    assert manifest['source_current_full_sha_required_each_call'] is True
    assert manifest['parent_21_archive_sha256']=='9169dbe158eb7e41818e5c303d84df0977ffb90e76d606f96565b49776d153e5'
    assert all(manifest['files'][name]['sha256']==sha for name,sha in PINS.items())
    return value,ref

def mixed_gate(path,sha,m,d):
    m.SPECS['mixed']=('replay-packet-cache-v2mix21-ubuntu-tests','replay-packet-cache-v2mix21-ro-overlay','replay-packet-cache-v2mix21-ro-overlay-',
        'lighthouse-replay-packet-cache-v2mix21-owned-',21,89,'fdea68bae719a0f20698558fd9601bc39bea930fd2000fbbbea1e26c370a843b')
    try:value,reference=m.gate(path,sha,'mixed',d)
    finally:m.SPECS.pop('mixed',None)
    manifest=value['manifest'];runtime=manifest['candidate_runtime_source_sha256']
    assert len(runtime)==45 and all(d.runtime_source(name) for name in runtime)
    assert manifest['natural_sources_installed'] is False and set(manifest['production_runtime_changed_files'])==set(PINS)
    assert manifest['parent_v2b_11_archive_sha256']=='7059be6f3e2dbd9dfc8ce83e0f2508d3e5c28c90021eeb3a5c22742b0efdd846'
    assert all(manifest['files'][name]['sha256']==sha for name,sha in runtime.items())
    assert all(runtime[name]==sha for name,sha in PINS.items())
    assert value['groups']=={'tests/test_replay_packet_cache_v1.py':20,'tests/test_replay_highest_video_v2.py':1}
    return value,reference

def validate_mixed_sources(d,candidate_sources,mixed):
    expected=mixed['manifest']['candidate_runtime_source_sha256']
    assert set(candidate_sources)==set(expected)
    assert all(d.sha(d.read(file,500000))==expected[name] for name,file in candidate_sources.items())

def current_sources(d,m,before,current,legacy,target):
    service=deepcopy(before['services']['video-worker'])
    for item in service['volumes']:item.setdefault('read_only',False)
    validate_running(d,current,service)
    assert service['command']==['python','-m','services.hub.replay_highest_moderation_v2b']
    known={name:entry['sha256'] for name,entry in legacy['manifest']['files'].items() if d.runtime_source(name)};assert len(known)==42
    volumes={v['target']:v for v in service['volumes'] if v['target'].startswith(str(d.ROOT)+'/') and d.runtime_source(v['target'][len(str(d.ROOT))+1:])}
    active=str(d.ROOT/CACHE) in volumes;expected=set(known)|set(MOD)|({CACHE} if active else set())
    assert set(volumes)=={str(d.ROOT/name) for name in expected}
    sources={}
    for name in expected:
        item=volumes[str(d.ROOT/name)];file=Path(item['source']);m.protected(file);assert item['read_only'] is True
        if name in PINS and active:assert file==target/name;expected_sha=PINS[name]
        elif name in MOD:assert file==d.INSTALL/'deltas'/'moderation-8ea790fdcf5f6a6625d36c5fc309d57d37abc66d'/name;expected_sha=MOD[name]
        elif name=='services/hub/replay_video_browser.py':
            assert file in (d.INSTALL/'deltas'/'moderation-8ea790fdcf5f6a6625d36c5fc309d57d37abc66d'/name,
                d.INSTALL/'deltas'/'243855ac107715350b59941b4118b415ac416b7d'/name)
            expected_sha='a7cd7e743d50c7be226c5b087422b825707676494df974d4c7027d5bc9d52446'
        else:assert file==d.INSTALL/'code'/name;expected_sha=known[name]
        assert d.sha(d.read(file,500000))==expected_sha;sources[name]=file
    return sources,active

def candidate(before,target,d):
    after=deepcopy(before);volumes=after['services']['video-worker']['volumes'];matches=[v for v in volumes if v['target']==str(d.ROOT/MACHINE)]
    assert len(matches)==1 and matches[0].get('read_only') is True and matches[0]['source']==str(d.INSTALL/'code'/MACHINE)
    assert not any(v['target']==str(d.ROOT/CACHE) for v in volumes)
    matches[0]['source']=str(target/MACHINE);volumes.append(d.bind(target/CACHE,d.ROOT/CACHE,True));d.assert_other_services(before,after)
    tmpfs=after['services']['video-worker']['tmpfs'];assert not set(dict(x.split(':',1) for x in tmpfs))&set(DIRECTORIES)
    tmpfs.extend(path+':'+value for path,value in DIRECTORIES.items())
    left=deepcopy(after);left['services']['video-worker']['volumes']=[v for v in left['services']['video-worker']['volumes'] if v['target']!=str(d.ROOT/CACHE)]
    next(v for v in left['services']['video-worker']['volumes'] if v['target']==str(d.ROOT/MACHINE))['source']=str(d.INSTALL/'code'/MACHINE)
    left['services']['video-worker']['tmpfs']=[x for x in left['services']['video-worker']['tmpfs'] if x.split(':',1)[0] not in DIRECTORIES]
    assert left==before
    return after

QUERY=r'''
from pathlib import Path
import hashlib,json,os,sqlite3,sys
assert os.getuid()==10001
db=sqlite3.connect(Path(sys.argv[1]).as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
row=dict(db.execute('SELECT * FROM jobs WHERE job_id=?',(sys.argv[2],)).fetchone());result=json.loads(row['result']);state=result['replay_highest_video_web_v2']
assert row['state']=='awaiting_review' and row['attempts']==1 and row['external_publish_id'] is None and row['lease_owner'] is None
assert state['state']=='prepared' and row['job_type']=='replay_highest_video_publish_v2' and row['source_id']=='9T5tn1WGlJg'
assert db.execute('SELECT COUNT(*) FROM publications WHERE job_id=?',(sys.argv[2],)).fetchone()[0]==0
active=db.execute("SELECT COUNT(*) FROM jobs WHERE queue='video' AND state='processing'").fetchone()[0]
print(json.dumps({'video_processing':active,'job_row_sha256':hashlib.sha256(json.dumps(row,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
 'heartbeat':dict(db.execute("SELECT name,at FROM heartbeats WHERE name IN ('worker:video','worker:video-main-v2b','worker:video-moderation-v2b')"))}));db.close()
'''

def job_state(d):
    def app():os.setgroups([]);os.setgid(10001);os.setuid(10001)
    value=subprocess.run(['/usr/bin/python3','-c',QUERY,str(d.ROOT/'data/hub.sqlite3'),PILOT],capture_output=True,text=True,timeout=20,
        preexec_fn=app,cwd='/tmp',env={'PATH':'/usr/bin:/bin','HOME':'/tmp','PYTHONNOUSERSITE':'1'})
    assert value.returncode==0
    folder=d.ROOT/'media'/PILOT
    assert not (folder/'replay-native-intents').exists() and not (folder/'replay-native-intents').is_symlink()
    return json.loads(value.stdout)

def preflight(d,sources,key):
    command=['/usr/bin/docker','run','--rm','--pull=never','--network=none','--read-only','--user','10001:10001','--cap-drop=ALL','--security-opt','no-new-privileges:true',
        '--init','--pids-limit=256','--memory=1400m','--shm-size=256m','--tmpfs','/tmp:rw,size=192m,mode=1777','-e','HUB_ROOT='+str(d.ROOT),
        '-e','HUB_CONFIG='+str(d.ROOT/'config/hub.toml'),'-e','HOME=/tmp/cache-preflight','-e','PYTHONDONTWRITEBYTECODE=1']
    for path,value in DIRECTORIES.items():command+=['--tmpfs',path+':'+value]
    mounts=[(d.ROOT/'config',d.ROOT/'config'),(key,'/run/secrets/youtube-api-key')]+[(file,d.ROOT/name) for name,file in sources.items()]
    for file,target in mounts:command+=['--mount','type=bind,source='+str(file)+',target='+str(target)+',readonly']
    check=d.PRECHECK.replace('from services.hub.replay_highest_worker_runtime_v2 import build_worker,load_youtube_key',
        'from services.hub.replay_highest_moderation_v2b import build_worker\nfrom services.hub.replay_worker_runtime import load_youtube_key')
    check=check.replace('socket.socket.connect=refuse;Store.claim=refuse',
        'socket.socket.connect=refuse;Store.claim=Store.recover=Store.enqueue=refuse\nfrom services.hub.worker import Worker\nfrom services.hub.replay_moderation_common import QueueView\nWorker.once=Worker.run=QueueView.claim=QueueView.recover=refuse')
    check=VERIFY_ENV.split('print(json.dumps')[0]+'\n'+check
    with tempfile.TemporaryDirectory(prefix='lighthouse-cache-preflight-',dir='/run') as folder:
        file,snapshot=d.online_snapshot(Path(folder));command+=['--mount','type=bind,source='+str(file)+',target=/run/preflight/hub.sqlite3,readonly']
        value=json.loads(d.run(command+[d.IMAGE,'python','-c',check],timeout=90));assert value['ready'] is True;return {**value,'snapshot':snapshot}

VERIFY_ENV=r'''
from pathlib import Path
import os,json,stat
assert os.getuid()==os.getgid()==10001
base=Path('/tmp/highest');assert base.resolve()==base and not base.is_symlink()
for name in ('home','config','cache'):
 path=base/name;assert path.resolve()==path and not path.is_symlink()
 assert path.is_dir() and path.stat().st_uid==path.stat().st_gid==10001 and stat.S_IMODE(path.stat().st_mode)==0o700 and os.access(path,os.W_OK)
print(json.dumps({'persistent_runtime_environment_directories_verified':True}))
'''

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for kind in ('legacy','cache','mix'):parser.add_argument('--'+kind+'-attestation',required=True);parser.add_argument('--'+kind+'-sha256',required=True)
    parser.add_argument('--installer-file',required=True);parser.add_argument('--deployer-file',required=True);parser.add_argument('--activate',action='store_true');args=parser.parse_args()
    assert os.name=='posix' and os.geteuid()==0;m=load(args.installer_file);d=m.helper(args.deployer_file)
    legacy,lref=d.gate(Path(args.legacy_attestation),args.legacy_sha256);value,cref=cache_gate(Path(args.cache_attestation),args.cache_sha256,m,d)
    mixed,mref=mixed_gate(Path(args.mix_attestation),args.mix_sha256,m,d)
    cache_overlay=Path(args.mix_attestation).parent/'overlay';target=d.INSTALL/'deltas'/('packet-cache-'+COMMIT)
    m.protected(d.ROOT/'compose.yaml');original=d.read(d.ROOT/'compose.yaml');oldstat=(d.ROOT/'compose.yaml').stat();before=d.compose_config();current=d.inspect(d.container_id('video-worker'))
    sources,active=current_sources(d,m,before,current,legacy,target);processes=m.core_processes(d);initial=job_state(d)
    candidate_sources={**sources,**{name:cache_overlay/name for name in PINS}}
    validate_mixed_sources(d,candidate_sources,mixed)
    if active:print(json.dumps({'state':'already_applied_verified','activated':True,'job':initial,'jobs_changed':False}));return
    candidate_config=candidate(before,target,d)
    candidate_sources={name:cache_overlay/name for name in mixed['manifest']['candidate_runtime_source_sha256']}
    key=[v['source'] for v in before['services']['video-worker']['volumes'] if v['target']=='/run/secrets/youtube-api-key'];assert len(key)==1;key=d.key_file(key[0])
    prepared=preflight(d,candidate_sources,key)
    configuration={file:d.sha(d.read(file,65536)) for file in (d.ROOT/'config/hub.toml',d.ROOT/'config/youtube-highest-download-policy.toml',d.ROOT/'config/replay-highest-video-web-policy.toml')}
    report={'version':'replay-packet-cache-v1-deployment','state':'verified_plan_only','activated':False,'legacy_attestation':lref,'cache_attestation':cref,'actual_mix_attestation':mref,
        'production_changed_files':list(PINS),'entry_changed':False,'hub_or_policy_changed':False,'job_or_attempt_changed':False,'job':initial,
        'other_core_processes':processes,'preflight':prepared,'source_commit':COMMIT,'native_freshness_seconds':300,'bounded_home_tmpfs':DIRECTORIES,
        'configuration_sha256':{str(file):sha for file,sha in configuration.items()}}
    if not args.activate:print(json.dumps(report));return
    import fcntl
    descriptors=[];globalfd=os.open('/run/lighthouse-replay-packet-cache-v1-deploy.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600);fcntl.flock(globalfd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    try:
        for role in ('video','moderation'):
            fd=m.role_lock(d,role)
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:os.close(fd);report['state']='deferred_active_video_or_lookup';print(json.dumps(report));return
            descriptors.append(fd)
        fresh=job_state(d)
        if fresh['video_processing']:report['state']='deferred_active_video_or_lookup';print(json.dumps(report));return
        assert fresh['job_row_sha256']==initial['job_row_sha256'] and d.read(d.ROOT/'compose.yaml')==original and m.core_processes(d)==processes
        assert all(d.sha(d.read(file,65536))==sha for file,sha in configuration.items())
        evidence=d.EV/('replay-packet-cache-v1-deploy-'+str(uuid.uuid4()));d.private_directory(evidence)
        d.atomic(evidence/'compose-before.yaml',original);d.atomic(evidence/'video-before.json',d.wire(d.filtered(current)))
        for name,sha in PINS.items():
            body=d.read(cache_overlay/name,500000);assert d.sha(body)==sha
            if (target/name).exists():m.protected(target/name);assert d.read(target/name,500000)==body
            else:m.install_file(d,target/name,body,mode=0o644,group=0)
        d.compose('stop','--timeout','45','video-worker');assert not d.inspect(current['Id'])['State']['Running'];assert job_state(d)['video_processing']==0
        d.atomic(d.ROOT/'compose.yaml',d.wire(candidate_config),original=oldstat);d.assert_other_services(before,d.compose_config())
        started=time.time();d.compose('up','-d','--no-deps','--no-build','video-worker');updated=d.inspect(d.container_id('video-worker'))
        prepared_env=json.loads(d.run(['/usr/bin/docker','exec','--user','10001:10001',updated['Id'],'python','-c',VERIFY_ENV]));assert prepared_env['persistent_runtime_environment_directories_verified'] is True
    except BaseException:
        report.update(state='requires_reconciliation',automatic_rollback_performed=False)
        if 'evidence' in locals():d.atomic(evidence/'deployment-report.json',d.wire(report))
        print(json.dumps(report));raise RuntimeError('CACHE_DEPLOY_REQUIRES_RECONCILIATION') from None
    finally:
        for fd in reversed(descriptors):os.close(fd)
        os.close(globalfd)
    expected=deepcopy(candidate_config['services']['video-worker'])
    for volume in expected['volumes']:volume.setdefault('read_only',False)
    validate_running(d,updated,expected)
    assert all(d.sha(d.read(target/name,500000))==sha for name,sha in PINS.items()) and m.core_processes(d)==processes
    deadline=time.monotonic()+45
    while not all(job_state(d)['heartbeat'].get(name,0)>=started for name in ('worker:video','worker:video-main-v2b','worker:video-moderation-v2b')):
        assert d.inspect(updated['Id'])['State']['Running'] and time.monotonic()<deadline;time.sleep(1)
    final=job_state(d);assert final['job_row_sha256']==initial['job_row_sha256'];d.assert_other_services(before,d.compose_config())
    assert all(d.sha(d.read(file,65536))==sha for file,sha in configuration.items())
    with urllib.request.urlopen('http://127.0.0.1:8080/ready',timeout=5) as response:assert response.status==200
    report.update(state='active',activated=True,video_container_id=updated['Id'],job=final,other_core_processes_unchanged=True,ready=True)
    d.atomic(evidence/'video-after.json',d.wire(d.filtered(updated)));d.atomic(evidence/'deployment-report.json',d.wire(report));print(json.dumps(report))

if __name__=='__main__':main()
