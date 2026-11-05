"""Root deployment of the tested v2 video service only; default is a plan.

No job creation, legacy-job mutation, paid request or credential-value output.
The four other Compose services and container identities must remain intact.
"""
from copy import deepcopy
from datetime import datetime,timezone
from pathlib import Path
import argparse,hashlib,json,os,re,stat,subprocess,tempfile,time,tomllib,uuid
import urllib.request
import xml.etree.ElementTree as ET

ROOT=Path('/opt/lighthouse-hub');HOME=Path('/home/lighthouse-ops');EV=HOME/'lighthouse-evidence'
INSTALL=Path('/usr/local/lib/lighthouse-highest-worker-v2')
IMAGE='sha256:9f19b756ca081bfe033ffa01b0face5c9cb70b3d45de08394567150ac747de6e'
COMMIT='b981bd03da0070c4acbde06df5d1a77457d656f6'
ARCHIVE='f8abae6bfe9ce8ada854b8954a7b67e2b8425c872fca13c5dd43fbb360adbc69'
DOWNLOAD_ARCHIVE='e3e60d113aca5d15bf4566984979e059adf1e117363906eea3b3f673fc5dc83b'
ENTRY='services.hub.replay_highest_worker_runtime_v2'
GROUPS={'tests/test_short_video_web.py':115,'tests/test_replay_video_web.py':30,
    'tests/test_replay_machine_checks.py':22,'tests/test_replay_worker.py':17,
    'tests/test_replay_video_worker_native.py':3,'tests/test_replay_native_driver.py':11,
    'tests/test_replay_highest_video_v2.py':28,'tests/test_replay_highest_worker_runtime_v2.py':8}
CORE=('api','scheduler','tweet-worker','system-worker')
SHA=re.compile('[a-f0-9]{64}')


def wire(value):return (json.dumps(value,sort_keys=True,separators=(',',':'))+'\n').encode()
def sha(raw):return hashlib.sha256(raw).hexdigest()
def safe(path):
    path=Path(path)
    assert path.is_absolute() and path.resolve()==path and not any(p.is_symlink() for p in (path,*path.parents))
    return path
def read(path,maximum=1000000):
    path=safe(path)
    with path.open('rb') as source:
        before=os.fstat(source.fileno());raw=source.read(maximum+1);after=os.fstat(source.fileno())
    assert stat.S_ISREG(before.st_mode) and 0<len(raw)==before.st_size<=maximum
    assert all(getattr(before,k)==getattr(after,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns'))
    return raw
def private_directory(path):
    safe(path);path.mkdir(mode=0o700);os.chmod(path,0o700)
    fd=os.open(path.parent,os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)
def atomic(path,body,*,mode=0o600,original=None):
    path=safe(path);temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.partial')
    fd=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,mode)
    with os.fdopen(fd,'wb') as target:
        target.write(body);target.flush();os.fchmod(target.fileno(),mode);os.fsync(target.fileno())
    if original is not None:os.chown(temporary,original.st_uid,original.st_gid);os.chmod(temporary,stat.S_IMODE(original.st_mode))
    os.replace(temporary,path);fd=os.open(path.parent,os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)
def run(args,*,timeout=90):
    result=subprocess.run(args,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=timeout)
    if result.returncode or len(result.stdout)>8_000_000 or len(result.stderr)>1_000_000:
        raise RuntimeError('HIGHEST_DEPLOY_COMMAND_FAILED')
    return result.stdout
def compose(*args,path=None):
    return run(['/usr/bin/docker','compose','--project-directory',str(ROOT),'-f',str(path or ROOT/'compose.yaml'),*args])
def compose_config(path=None):return json.loads(compose('config','--format','json','--no-env-resolution',path=path))
def inspect(ident):return json.loads(run(['/usr/bin/docker','inspect',ident]))[0]
def container_id(name):
    result=compose('ps','-a','-q',name).strip();assert re.fullmatch('[a-f0-9]{12,64}',result);return inspect(result)['Id']
def filtered(value):
    return {'id':value['Id'],'image':value['Image'],'name':value['Name'],
        'state':{k:value['State'].get(k) for k in ('Status','Running','ExitCode','OOMKilled','StartedAt','FinishedAt')},
        'restart_policy':value['HostConfig']['RestartPolicy'],
        'mounts':[{k:m.get(k) for k in ('Type','Source','Destination','RW')} for m in value['Mounts']]}


def validate_attestation(value,*,download=False):
    total=61 if download else 234
    assert value['version']==('youtube-highest-download-v2-ubuntu-tests-v1' if download else 'replay-highest-worker-ubuntu-tests-v2')
    assert value['image']==IMAGE and value['tests']==total and value['full_suite_passed'] is True
    assert value['failures']==value['errors']==value['skipped']==value['exit_code']==0 and value['oom_killed'] is False
    assert value['archive_sha256']==(DOWNLOAD_ARCHIVE if download else ARCHIVE)
    assert value['network']=='none' and value['application_data_mounted'] is value['production_profile_mounted'] is False
    assert value['secrets_environment_injected'] is value['whole_tree_deployed'] is False
    assert all(value[k] is True for k in ('runtime_readonly_overlays_only','runtime_matches_declared_candidate','mounts_match_isolated_allowlist','no_pid_or_oom_denial'))
    assert value['real_uploads']==value['publications']==0
    manifest=value['manifest'];assert manifest['expected_total_tests']==total and manifest['expected_test_counts']==value['groups']
    assert sum(value['groups'].values())==total
    assert manifest['base_image']==IMAGE and manifest['whole_tree_deployment'] is manifest['application_data_included'] is manifest['credentials_included'] is False
    if not download:
        assert value['groups']==GROUPS and manifest['source_freeze_commit']==COMMIT and len(manifest['files'])==82
    return manifest


def gate(path,expected_sha,*,download=False):
    path=safe(path);assert path.parent.parent==EV and path.name=='test-attestation.json'
    prefix='youtube-highest-download-v2-overlay-v1-' if download else 'replay-highest-worker-ro-overlay-v2-'
    assert path.parent.name.startswith(prefix);ident=path.parent.name[len(prefix):];assert str(uuid.UUID(ident))==ident
    raw=read(path,500000);assert SHA.fullmatch(expected_sha) and sha(raw)==expected_sha
    value=json.loads(raw);manifest=validate_attestation(value,download=download)
    owned=HOME/(('lighthouse-youtube-highest-owned-' if download else 'lighthouse-replay-highest-owned-v2-')+ident)
    report=read(owned/'results.xml',20_000_000);assert sha(report)==value['junit_sha256']
    cases=list(ET.fromstring(report).iter('testcase'));groups={name:0 for name in value['groups']}
    assert len(cases)==value['tests'] and all(not any(case.find(key) is not None for key in ('failure','error','skipped')) for case in cases)
    for case in cases:
        matches=[name for name in groups if case.attrib.get('classname','').endswith(Path(name).stem)]
        assert len(matches)==1;groups[matches[0]]+=1
    assert groups==value['groups'] and sha(read(path.parent/'tests.log',20_000_000))==value['tests_log_sha256']
    assert json.loads(read(path.parent/'overlay/manifest.json'))==manifest
    for name,entry in manifest['files'].items():
        assert not Path(name).is_absolute() and '..' not in Path(name).parts
        body=read(path.parent/'overlay'/name,500000);assert len(body)==entry['bytes'] and sha(body)==entry['sha256']
    container=json.loads(read(path.parent/'container.json',5_000_000));host=container['HostConfig']
    assert container['Id']==value['container_id'] and container['Image']==IMAGE and container['Config']['User']=='10001:10001'
    assert host.get('Init') is True and host['NetworkMode']=='none' and host['ReadonlyRootfs'] is True
    assert host['PidsLimit']==256 and host['Memory']==1400*1024**2 and host['ShmSize']==256*1024**2
    assert container['State']['ExitCode']==0 and container['State']['OOMKilled'] is False
    for label in ('before','after'):
        resources=json.loads(read(owned/('resource-'+label+'.json')));assert resources==value['resource_snapshot_'+label]
        assert int(resources['pids.events'].get('max',0))==int(resources['memory.events'].get('oom',0))==int(resources['memory.events'].get('oom_kill',0))==0
    return value,{'file':str(path),'sha256':expected_sha,'tests':value['tests']}


def runtime_source(name):
    return (name.startswith(('services/hub/','adapters/','vendor/')) or name in (
        'scheduler/service.py','scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs'))
def bundle(unit_path,unit,download):
    files={name:entry for name,entry in unit['manifest']['files'].items() if runtime_source(name)}
    assert 'services/hub/replay_highest_worker_runtime_v2.py' in files
    for name,entry in download['manifest']['files'].items():
        if name in files:assert files[name]['sha256']==entry['sha256']
    return {name:read(unit_path.parent/'overlay'/name,500000) for name in files}


def key_file(path):
    path=safe(path);info=path.stat()
    assert stat.S_ISREG(info.st_mode) and 1<=info.st_size<=512 and info.st_uid==0 and info.st_gid==10001 and stat.S_IMODE(info.st_mode)==0o640
    for parent in path.parents:assert parent.stat().st_uid==0 and not parent.stat().st_mode&0o022
    return path
def bind(source,target,readonly=False):
    return {'type':'bind','source':str(source),'target':str(target),'read_only':readonly,'bind':{'create_host_path':False}}
def new_video_service(key,code,*,code_root=INSTALL/'code'):
    volumes=[bind(ROOT/name,ROOT/name,name=='config') for name in ('data','media','logs','backups','config')]
    volumes += [bind(ROOT/'data/browser-profiles/bilibili-video',ROOT/'data/browser-profiles/bilibili-video'),
        bind(ROOT/'data/browser-profiles/youtube',ROOT/'data/browser-profiles/youtube',True),bind(key,'/run/secrets/youtube-api-key',True)]
    volumes += [bind(code_root/name,ROOT/name,True) for name in sorted(code)]
    return {'image':IMAGE,'pull_policy':'never','restart':'unless-stopped','init':True,'user':'10001:10001',
        'entrypoint':[],'command':['python','-m',ENTRY],'read_only':True,'cap_drop':['ALL'],
        'security_opt':['no-new-privileges:true','seccomp='+str(ROOT/'config/browser-seccomp.json')],
        'cpus':1.5,'mem_limit':'1400m','pids_limit':256,'shm_size':'256m','ports':[],
        'environment':{'HUB_ROOT':str(ROOT),'HUB_CONFIG':str(ROOT/'config/hub.toml'),'DRY_RUN':'true',
            'HOME':'/tmp/highest/home','XDG_CONFIG_HOME':'/tmp/highest/config','XDG_CACHE_HOME':'/tmp/highest/cache',
            'PYTHONDONTWRITEBYTECODE':'1','YOUTUBE_API_KEY_FILE':'/run/secrets/youtube-api-key'},
        'volumes':volumes,'tmpfs':['/tmp:rw,size=192m,mode=1777',str(ROOT/'data/browser-profiles')+':rw,size=1m,mode=0700,uid=10001,gid=10001'],
        'logging':{'driver':'local','options':{'max-size':'10m','max-file':'3'}}}
def candidate_config(before,video):
    assert set(before['services'])==set(CORE)|{'video-worker'}
    result=deepcopy(before);result['services']['video-worker']=video;assert_other_services(before,result);return result
def assert_other_services(before,after):
    assert set(before)==set(after)
    for key in before:
        if key!='services':assert before[key]==after[key]
    assert set(before['services'])==set(after['services'])
    assert all(before['services'][name]==after['services'][name] for name in CORE)
def can_rollback(snapshot):return snapshot['v2_jobs']==0 and not snapshot['video_processing']
def candidate_hub(raw):
    """Change exactly the global highest-v2 boolean; preserve all other bytes."""
    text=raw.decode('utf-8-sig');before=tomllib.loads(text);name='youtube_highest_download_enabled'
    assert type(before.get(name,False)) is bool
    lines=text.splitlines(keepends=True);end=next((i for i,line in enumerate(lines) if re.match(r'^\s*\[',line)),len(lines))
    matches=[i for i,line in enumerate(lines[:end]) if re.match(r'^\s*'+name+r'\s*=',line)]
    ending='\r\n' if '\r\n' in text else '\n'
    if name in before:
        assert len(matches)==1;i=matches[0]
        match=re.fullmatch(r'(\s*'+name+r'\s*=\s*)(true|false)(\s*(?:#[^\r\n]*)?)(\r?\n)?',lines[i]);assert match
        lines[i]=match[1]+'true'+match[3]+(match[4] or '')
    else:
        assert not matches
        if end and not lines[end-1].endswith(('\n','\r')):lines[end-1]+=ending
        lines.insert(end,name+' = true'+ending)
    output=''.join(lines);assert tomllib.loads(output)=={**before,name:True}
    return (b'\xef\xbb\xbf' if raw.startswith(b'\xef\xbb\xbf') else b'')+output.encode('utf-8')


QUERY="""
from pathlib import Path
import json,os,sqlite3,sys
assert os.getuid()==10001
db=sqlite3.connect(Path(sys.argv[1]).as_uri()+'?mode=ro',uri=True);db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
count=db.execute("SELECT COUNT(*) FROM jobs WHERE job_type IN ('youtube_download_v2','replay_highest_video_publish_v2')").fetchone()[0]
active=db.execute("SELECT COUNT(*) FROM jobs WHERE queue='video' AND state='processing'").fetchone()[0]
heartbeat=db.execute("SELECT at FROM heartbeats WHERE name='worker:video'").fetchone()
print(json.dumps({'v2_jobs':count,'video_processing':active,'heartbeat':heartbeat[0] if heartbeat else 0}));db.close()
"""
def snapshot():
    def app():os.setgroups([]);os.setgid(10001);os.setuid(10001)
    result=subprocess.run(['/usr/bin/python3','-c',QUERY,str(ROOT/'data/hub.sqlite3')],capture_output=True,text=True,timeout=20,
        preexec_fn=app,env={'PATH':'/usr/bin:/bin','HOME':'/tmp','PYTHONNOUSERSITE':'1'})
    assert result.returncode==0;return json.loads(result.stdout)


PRECHECK="""
import json,os,socket,sqlite3
from pathlib import Path
from services.hub.config import Settings
from services.hub.db import Store
from services.hub.replay_highest_worker_runtime_v2 import build_worker,load_youtube_key
assert os.getuid()==os.getgid()==10001
def refuse(*a,**k):raise RuntimeError('PREFLIGHT_NETWORK_OR_CLAIM_FORBIDDEN')
socket.socket.connect=refuse;Store.claim=refuse
class ReadOnlyStore(Store):
 def __init__(self,path):self.path=Path(path)
 def connect(self):
  connection=sqlite3.connect(Path('/run/preflight/hub.sqlite3').as_uri()+'?mode=ro&immutable=1',uri=True,timeout=15,isolation_level=None)
  connection.row_factory=sqlite3.Row;connection.execute('PRAGMA query_only=ON');connection.execute('PRAGMA busy_timeout=15000')
  return connection
 def transaction(self):return refuse()
 def init(self):return refuse()
s=Settings.load();db=ReadOnlyStore(s.db_path);load_youtube_key('/run/secrets/youtube-api-key');worker,service=build_worker(s,db)
assert {'youtube_download_v2','replay_highest_video_publish_v2','youtube_download','replay_video_publish'}<=set(worker.handlers)
print(json.dumps({'ready':True,'policy_sha256':service.policy()['_sha256'],'claim_called':False,'network_enabled':False}))
"""
SNAPSHOT="""
from pathlib import Path
import hashlib,json,os,sqlite3,sys,time
assert os.getuid()==os.getgid()==10001
source=sqlite3.connect(Path(sys.argv[1]).as_uri()+'?mode=ro',uri=True,timeout=15)
source.execute('PRAGMA query_only=ON');target=sqlite3.connect(sys.argv[2])
try:
 source.backup(target,pages=256,sleep=.01)
 assert target.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
 assert not target.execute('PRAGMA foreign_key_check').fetchall()
 assert target.execute('PRAGMA journal_mode=DELETE').fetchone()[0]=='delete';target.commit()
finally:target.close();source.close()
file=Path(sys.argv[2]);fd=os.open(file,os.O_RDONLY)
try:os.fsync(fd)
finally:os.close(fd)
print(json.dumps({'sha256':hashlib.sha256(file.read_bytes()).hexdigest(),'bytes':file.stat().st_size,'observed_at':time.time(),
 'online_sqlite_backup':True,'integrity_check':'ok','foreign_key_check':True,'immutable_applies_to_backup_only':True}))
"""
def online_snapshot(directory):
    # The app identity may maintain its normal live WAL/SHM. Root never opens
    # the live database, and immutable is used only on this consistent backup.
    os.chown(directory,10001,10001);os.chmod(directory,0o700);file=directory/'hub.sqlite3'
    def app():os.setgroups([]);os.setgid(10001);os.setuid(10001)
    result=subprocess.run(['/usr/bin/python3','-c',SNAPSHOT,str(ROOT/'data/hub.sqlite3'),str(file)],
        capture_output=True,text=True,timeout=60,preexec_fn=app,cwd='/tmp',
        env={'PATH':'/usr/bin:/bin','HOME':'/tmp','PYTHONNOUSERSITE':'1'})
    if result.returncode:raise RuntimeError('HIGHEST_PREFLIGHT_ONLINE_SNAPSHOT_FAILED')
    record=json.loads(result.stdout);assert sha(read(file,100_000_000))==record['sha256']
    os.chown(file,0,10001);os.chmod(file,0o640);os.chown(directory,0,10001);os.chmod(directory,0o750)
    return file,record
def preflight(unit_path,code,key,hub):
    command=['/usr/bin/docker','run','--rm','--pull=never','--network=none','--read-only','--user','10001:10001','--cap-drop=ALL',
        '--security-opt','no-new-privileges:true','--init','--pids-limit=256','--memory=1400m','--shm-size=256m',
        '--tmpfs','/tmp:rw,size=192m,mode=1777','-e','HUB_ROOT='+str(ROOT),'-e','HUB_CONFIG='+str(ROOT/'config/hub.toml'),
        '-e','HOME=/tmp/highest-preflight','-e','PYTHONDONTWRITEBYTECODE=1']
    for source,target in ((ROOT/'config',ROOT/'config'),(key,Path('/run/secrets/youtube-api-key'))):
        command+=['--mount','type=bind,source='+str(source)+',target='+str(target)+',readonly']
    for name in code:command+=['--mount','type=bind,source='+str(unit_path.parent/'overlay'/name)+',target='+str(ROOT/name)+',readonly']
    # The real flag stays untouched in plan mode. The one-off container reads
    # this candidate as a nested RO bind over the existing config-directory RO bind.
    with tempfile.TemporaryDirectory(prefix='lighthouse-highest-preflight-',dir='/run') as directory:
        snapshot_file,snapshot_record=online_snapshot(Path(directory))
        candidate=Path(directory)/'hub.toml';candidate.write_bytes(hub);os.chown(candidate,0,10001);os.chmod(candidate,0o640)
        command+=['--mount','type=bind,source='+str(candidate)+',target='+str(ROOT/'config/hub.toml')+',readonly',
            '--mount','type=bind,source='+str(snapshot_file)+',target=/run/preflight/hub.sqlite3,readonly']
        result=json.loads(run(command+[IMAGE,'python','-c',PRECHECK],timeout=90));assert result['ready'] is True
        result['database_snapshot']=snapshot_record
    return result


def validate_running(value,service,code):
    host=value['HostConfig'];assert value['Image']==IMAGE and value['State']['Running'] is True and value['State']['OOMKilled'] is False
    assert value['Config']['User']=='10001:10001' and host.get('Init') is True and host['ReadonlyRootfs'] is True
    assert host['PidsLimit']==256 and host['Memory']==1400*1024**2 and host['ShmSize']==256*1024**2
    assert not host.get('Privileged') and not host.get('PortBindings') and not host.get('Devices')
    expected={v['target']:(v['source'],not v['read_only']) for v in service['volumes']}
    binds={m['Destination']:(m['Source'],m['RW']) for m in value['Mounts'] if m['Type']=='bind'}
    assert binds==expected and all('docker.sock' not in str(v) for v in binds.values())
    assert host['CapDrop']==['ALL'] and any(v.startswith('no-new-privileges') for v in host['SecurityOpt'])
    assert any(v.startswith('seccomp=') for v in host['SecurityOpt'])
    assert set(host['Tmpfs'])=={'/tmp',str(ROOT/'data/browser-profiles')}
    env=dict(item.split('=',1) for item in value['Config']['Env'])
    assert all(env.get(k)==v for k,v in service['environment'].items())
    assert all(k not in env for k in ('OPENAI_API_KEY','X_BEARER_TOKEN','YOUTUBE_API_KEY','BILIBILI_ACCESS_TOKEN','EVENT_ENCRYPTION_KEY'))
    assert value['Config']['Cmd']==service['command']
    for name,body in code.items():assert sha(read(INSTALL/'code'/name,500000))==sha(body)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--unit-attestation',required=True);parser.add_argument('--unit-sha256',required=True)
    parser.add_argument('--download-attestation',required=True);parser.add_argument('--download-sha256',required=True)
    parser.add_argument('--youtube-key-file',required=True);parser.add_argument('--activate',action='store_true');args=parser.parse_args()
    assert os.name=='posix' and os.geteuid()==0
    unit_path=safe(Path(args.unit_attestation));unit,uref=gate(unit_path,args.unit_sha256)
    downloaded,dref=gate(Path(args.download_attestation),args.download_sha256,download=True)
    code=bundle(unit_path,unit,downloaded);key=key_file(args.youtube_key_file)
    assert run(['/usr/bin/docker','image','inspect','-f','{{.Id}}',IMAGE]).strip()==IMAGE
    for name in ('bilibili-video','youtube'):
        directory=safe(ROOT/'data/browser-profiles'/name);info=directory.stat()
        assert directory.is_dir() and info.st_uid==10001 and stat.S_IMODE(info.st_mode)==0o700
    original=read(ROOT/'compose.yaml');original_stat=(ROOT/'compose.yaml').stat();before=compose_config()
    hub_original=read(ROOT/'config/hub.toml');hub_stat=(ROOT/'config/hub.toml').stat();hub_candidate=candidate_hub(hub_original)
    ids={name:container_id(name) for name in CORE};legacy=inspect(container_id('video-worker'))
    prepared=preflight(unit_path,code,key,hub_candidate);assert not snapshot()['video_processing']
    service=new_video_service(key,code);candidate=candidate_config(before,service)
    summary={'version':'highest-video-worker-compose-deployment-v2','state':'verified_plan_only','image':IMAGE,
        'source_commit':COMMIT,'unit_attestation':uref,'download_attestation':dref,'runtime_files':len(code),
        'candidate_sha256':sha(wire(candidate)),'compose_before_sha256':sha(original),'preflight':prepared,
        'hub_before_sha256':sha(hub_original),'hub_candidate_sha256':sha(hub_candidate),'only_hub_flag_changed':'youtube_highest_download_enabled',
        'other_service_container_ids':ids,'jobs_created':0,'paid_requests':0,'secrets_logged':False,'activated':False}
    if not args.activate:print(json.dumps(summary));return
    import fcntl
    lock=os.open('/run/lighthouse-highest-worker-v2-deploy.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    evidence=EV/('highest-video-worker-v2-deploy-'+str(uuid.uuid4()));private_directory(evidence)
    atomic(evidence/'compose-before.yaml',original);atomic(evidence/'video-before.json',wire(filtered(legacy)))
    for name in ('hub.toml','youtube-highest-download-policy.toml','replay-highest-video-web-policy.toml'):
        atomic(evidence/name,read(ROOT/'config'/name))
    INSTALL.mkdir(parents=True,mode=0o755,exist_ok=True);assert INSTALL.stat().st_uid==0 and INSTALL.stat().st_mode&0o022==0
    os.chmod(INSTALL,0o755)
    directories={INSTALL,INSTALL.parent}
    for name,body in code.items():
        target=INSTALL/'code'/name;target.parent.mkdir(parents=True,mode=0o755,exist_ok=True);safe(target)
        for parent in (target.parent,*target.parent.parents):
            if not parent.is_relative_to(INSTALL):break
            assert parent.stat().st_uid==0 and not parent.stat().st_mode&0o022;os.chmod(parent,0o755);directories.add(parent)
        if target.exists():assert read(target,500000)==body
        else:atomic(target,body,mode=0o644)
    for directory in sorted(directories,key=lambda p:len(p.parts),reverse=True):
        fd=os.open(directory,os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    atomic(evidence/'runtime-source-manifest.json',wire({name:sha(body) for name,body in code.items()}))
    changed=False;hub_changed=False
    try:
        assert read(ROOT/'compose.yaml')==original and read(ROOT/'config/hub.toml')==hub_original and all(container_id(n)==i for n,i in ids.items())
        compose('stop','--timeout','45','video-worker');assert inspect(legacy['Id'])['State']['Running'] is False
        assert not snapshot()['video_processing']
        hub_changed=True;atomic(ROOT/'config/hub.toml',hub_candidate,original=hub_stat)
        changed=True;atomic(ROOT/'compose.yaml',wire(candidate),original=original_stat)
        assert_other_services(before,compose_config())
        started=time.time();compose('up','-d','--no-deps','--no-build','video-worker')
        current=inspect(container_id('video-worker'));validate_running(current,service,code)
        end=time.monotonic()+45
        while snapshot()['heartbeat']<started:
            assert inspect(current['Id'])['State']['Running'] is True
            if time.monotonic()>end:raise RuntimeError('HIGHEST_WORKER_HEARTBEAT_REQUIRED')
            time.sleep(1)
        with urllib.request.urlopen('http://127.0.0.1:8080/ready',timeout=5) as response:assert response.status==200
        assert all(container_id(n)==i and inspect(i)['State']['Running'] for n,i in ids.items())
        assert_other_services(before,compose_config());atomic(evidence/'video-after.json',wire(filtered(inspect(current['Id']))))
        summary.update(state='video_service_active',activated=True,video_container_id=current['Id'],compose_after_sha256=sha(read(ROOT/'compose.yaml')),
            heartbeat=snapshot()['heartbeat'],ready=True,other_services_unchanged=True)
    except BaseException:
        # Do not restore an unaware legacy consumer once a v2 job exists.
        allowed=can_rollback(snapshot())
        if allowed and (changed or hub_changed):
            compose('stop','--timeout','45','video-worker');assert can_rollback(snapshot())
            assert read(ROOT/'config/hub.toml') in (hub_original,hub_candidate)
            atomic(ROOT/'config/hub.toml',hub_original,original=hub_stat)
            atomic(ROOT/'compose.yaml',original,original=original_stat)
            if legacy['State']['Running']:compose('up','-d','--no-deps','--no-build','video-worker')
            else:compose('up','--no-start','--no-deps','--no-build','video-worker')
        elif not changed and legacy['State']['Running'] and not inspect(legacy['Id'])['State']['Running']:
            run(['/usr/bin/docker','start',legacy['Id']])
        summary.update(state='activation_failed',rollback_permitted=allowed,rollback_performed=bool(allowed and (changed or hub_changed)),jobs_reset=False)
        atomic(evidence/'deployment-report.json',wire(summary));print(json.dumps(summary));raise RuntimeError('HIGHEST_VIDEO_DEPLOYMENT_REQUIRES_RECONCILIATION') from None
    finally:os.close(lock)
    atomic(evidence/'deployment-report.json',wire(summary));print(json.dumps(summary))


if __name__=='__main__':main()
