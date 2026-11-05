"""Reviewed, default-plan-only canonical YouTube acceptance pilot deployment.
No job creation, download invocation, credential copying, image build or cleanup.
"""
from contextlib import ExitStack,contextmanager
from copy import deepcopy
from datetime import datetime,timezone
import argparse,hashlib,json,os,re,sqlite3,stat,subprocess,sys,time,tomllib,uuid
from pathlib import Path
ROOT=Path('/opt/lighthouse-hub');HOME=Path('/home/lighthouse-ops');EV=HOME/'lighthouse-evidence'
INSTALL=Path('/usr/local/lib/lighthouse-video-pilot-v1')
SERVICES=('api','system-worker','video-worker')
CBC='sha256:cbc6cd5076960eba0adb723968bd10257c6691d62e3554cf48ae0ff0df5d8d10'
TEST_IMAGE='sha256:9f19b756ca081bfe033ffa01b0face5c9cb70b3d45de08394567150ac747de6e'
TARGET='9T5tn1WGlJg'
SOURCES={'services/hub/config.py':'b2e5659c523829a23b64934ce46a679ccc98f4e4fc02670e60f41288dacd399b',
         'services/hub/youtube_download.py':'d14f7d8c522481a6070f86afd46f402b09b6277212821986e89eba3bf0f0337a'}

def sha(data):return hashlib.sha256(data).hexdigest()
def wire(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()
def safe(path):
    path=Path(path);assert path.is_absolute() and path.resolve()==path and not path.is_symlink() and not any(p.is_symlink() for p in path.parents);return path

def read(path,limit=500000):
    path=safe(path);s=path.stat();assert stat.S_ISREG(s.st_mode) and s.st_size<=limit
    data=path.read_bytes();assert len(data)==s.st_size;return data

def set_root_toml(raw,key,value):
    bom=b'\xef\xbb\xbf' if raw.startswith(b'\xef\xbb\xbf') else b'';text=raw.decode('utf-8-sig')
    lines=text.splitlines(keepends=True);end=next((i for i,line in enumerate(lines) if re.match(r'^\s*\[',line)),len(lines))
    matches=[i for i in range(end) if re.match(r'^\s*'+re.escape(key)+r'\s*=',lines[i])];assert len(matches)<=1
    newline='\r\n' if '\r\n' in text else '\n';literal=json.dumps(value,ensure_ascii=False)
    if matches:
        i=matches[0];line=lines[i];ending='\r\n' if line.endswith('\r\n') else '\n' if line.endswith('\n') else ''
        content=line[:-len(ending)] if ending else line
        prefix,old=content.split('=',1);comment=(' #'+old.split('#',1)[1]) if '#' in old else ''
        lines[i]=prefix+'= '+literal+comment+ending
    else:
        if end and not lines[end-1].endswith(('\n','\r')):lines[end-1]+=newline
        lines.insert(end,key+' = '+literal+newline)
    return bom+''.join(lines).encode('utf-8')

def mutate_hub(raw):
    before=tomllib.loads(raw.decode('utf-8-sig'));out=set_root_toml(raw,'youtube_download_enabled',True)
    out=set_root_toml(out,'youtube_download_acceptance_only',True)
    expected={**before,'youtube_download_enabled':True,'youtube_download_acceptance_only':True}
    assert tomllib.loads(out.decode('utf-8-sig'))==expected;return out

def mutate_policy(raw):
    before=tomllib.loads(raw.decode('utf-8-sig'));old=before.get('acceptance_video_ids',[])
    assert isinstance(old,list) and len(old)<=1 and all(isinstance(v,str) and re.fullmatch('[A-Za-z0-9_-]{11}',v) for v in old)
    out=set_root_toml(raw,'acceptance_video_ids',[TARGET]);expected={**before,'acceptance_video_ids':[TARGET]}
    assert tomllib.loads(out.decode('utf-8-sig'))==expected;return out

def source_volume(name):
    return {'type':'bind','source':str(INSTALL/Path(name).name),'target':str(ROOT/name),'read_only':True,'bind':{'create_host_path':False}}

def _field(lines,key,indent):
    found=[i for i,line in enumerate(lines) if re.fullmatch(' '*indent+re.escape(key)+r':\s*(?:#.*)?\r?\n?',line)]
    assert len(found)<=1
    if not found:return None
    start=found[0];end=start+1
    while end<len(lines):
        line=lines[end]
        if line.strip() and len(line)-len(line.lstrip(' '))<=indent:break
        end+=1
    return start,end

def mutate_compose(raw):
    text=raw.decode('utf-8-sig');assert not raw.startswith(b'\xef\xbb\xbf')
    lines=text.splitlines(keepends=True);ending='\r\n' if '\r\n' in text else '\n'
    a=next(i for i,x in enumerate(lines) if x.strip()=='x-runtime: &runtime');b=next(i for i,x in enumerate(lines) if x.strip()=='services:')
    runtime=lines[a+1:b];field=_field(runtime,'volumes',2);assert field
    inherited=runtime[field[0]+1:field[1]];assert inherited and all(not x.strip() or len(x)-len(x.lstrip(' '))>=4 for x in inherited)
    for service in SERVICES:
        start=next(i for i,x in enumerate(lines) if x.rstrip('\r\n')=='  '+service+':');end=start+1
        while end<len(lines):
            x=lines[end]
            if x.strip() and len(x)-len(x.lstrip(' '))<=2:break
            end+=1
        block=lines[start+1:end];field=_field(block,'volumes',4)
        # Refuse a partial/previous unreviewed pilot binding instead of adding a
        # duplicate target or silently replacing another source.
        assert all('/usr/local/lib/lighthouse-video-pilot-v1' not in x and 'services/hub/config.py' not in x and 'services/hub/youtube_download.py' not in x for x in block)
        additions=['      - '+json.dumps(source_volume(name),separators=(',',':'))+ending for name in SOURCES]
        if field:block[field[1]:field[1]]=additions
        else:
            assert block and block[0].strip()=='<<: *runtime'
            block[1:1]=['    volumes:'+ending]+['  '+x for x in inherited]+additions
        lines[start+1:end]=block
    return ''.join(lines).encode('utf-8')

def assert_compose_delta(before,after):
    expected=deepcopy(before);actual=deepcopy(after)
    assert expected.get('name')==actual.get('name')=='lighthouse-hub'
    for service in SERVICES:
        volumes=actual['services'][service]['volumes'];old=expected['services'][service]['volumes']
        assert len(volumes)==len(old)+2
        extras=[];kept=[]
        for volume in volumes:
            if volume.get('target') in (str(ROOT/n) for n in SOURCES):extras.append(volume)
            else:kept.append(volume)
        assert kept==old and len(extras)==2
        for name in SOURCES:
            matching=[v for v in extras if v.get('target')==str(ROOT/name)];assert len(matching)==1
            v=matching[0];assert v.get('type')=='bind' and v.get('source')==str(INSTALL/Path(name).name) and v.get('read_only') is True
            assert v.get('bind',{}).get('create_host_path') is False
        actual['services'][service]['volumes']=kept
    assert actual==expected

def run(args,timeout=90):
    result=subprocess.run(args,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=timeout)
    if result.returncode:raise RuntimeError('VIDEO_PILOT_COMMAND_FAILED')
    assert len(result.stdout)<=3_000_000 and len(result.stderr)<=1_000_000;return result.stdout

def compose(*args,path=None,timeout=90):
    return run(['/usr/bin/docker','compose','--project-directory',str(ROOT),'-f',str(path or ROOT/'compose.yaml'),*args],timeout)

def parsed_compose(path):return json.loads(compose('config','--format','json',path=path))

def atomic(path,data,*,original=None,mode=0o600):
    path=safe(path);temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.partial')
    fd=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,mode)
    # Explicit mode is required: the root deployer uses umask 077, while the
    # two nonsecret source binds must remain readable by application UID10001.
    with os.fdopen(fd,'wb') as stream:
        stream.write(data);stream.flush();os.fchmod(stream.fileno(),mode);os.fsync(stream.fileno())
    if original:os.chown(temporary,original.st_uid,original.st_gid);os.chmod(temporary,stat.S_IMODE(original.st_mode))
    os.replace(temporary,path);fd=os.open(path.parent,os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)

def read_attestation():
    pointer=safe(EV/'youtube-pilot-current-test.json');raw=read(pointer,65536);value=json.loads(raw);paths=set()
    def visit(node):
        if isinstance(node,dict):
            for v in node.values():visit(v)
        elif isinstance(node,list):
            for v in node:visit(v)
        elif isinstance(node,str):
            p=Path(node)
            if not p.is_absolute():return
            p=p.parent if p.name=='test-attestation.json' else p
            if p.parent==EV and p.name.startswith('youtube-pilot-overlay-v1-'):
                ident=p.name.removeprefix('youtube-pilot-overlay-v1-');assert str(uuid.UUID(ident))==ident;paths.add(p)
    visit(value);assert len(paths)==1;evidence=safe(paths.pop());attpath=evidence/'test-attestation.json';body=read(attpath,65536);a=json.loads(body)
    assert a['version']=='youtube-pilot-overlay-ubuntu-tests-v1' and a['image']==TEST_IMAGE and a['tests']==86
    assert a['full_suite_passed'] is True and a['failures']==a['errors']==a['skipped']==a['exit_code']==0 and a['oom_killed'] is False
    assert a['network']=='none' and a['application_data_mounted'] is False and a['whole_tree_deployed'] is False and a['runtime_readonly_overlays_only'] is True
    assert a['runtime_matches_production'] is True and a['no_pid_or_oom_denial'] is True
    assert a['manifest']['expected_test_counts']=={'tests/test_youtube_download.py':71,'tests/test_youtube_download_acceptance_only.py':15}
    assert a['manifest']['base_image']==TEST_IMAGE and a['manifest']['expected_total_tests']==86
    assert a['groups']==a['manifest']['expected_test_counts'] and a['real_uploads']==a['publications']==0
    sources={}
    for name,digest in SOURCES.items():
        assert a['manifest']['files'][name]['sha256']==digest;p=evidence/'overlay'/name;data=read(p,150000);assert sha(data)==digest;sources[name]=data
    return sources,{'pointer_file':str(pointer),'pointer_sha256':sha(raw),'attestation_file':str(attpath),'attestation_sha256':sha(body),'tests':86,'image':a['image']}

DB_CHECK=r'''
from pathlib import Path
import hashlib,json,os,sqlite3
root=Path('/opt/lighthouse-hub');assert os.getuid()==10001
db=sqlite3.connect((root/'data/hub.sqlite3').as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON')
active=[dict(r) for r in db.execute("SELECT job_id,job_type,state FROM jobs WHERE queue='video' AND state IN ('processing','publish_unknown')")]
runnable=[dict(r) for r in db.execute("SELECT job_id,job_type,state FROM jobs WHERE job_type='youtube_download' AND state IN ('pending','retry_scheduled','processing','publish_unknown')")]
rows=[dict(r) for r in db.execute("SELECT * FROM jobs WHERE source_id='0H6_bTvWeDQ' AND job_type IN ('youtube_download','short_video_publish') ORDER BY job_id")]
protected={r['job_id']:hashlib.sha256(json.dumps(r,sort_keys=True,separators=(',',':')).encode()).hexdigest() for r in rows}
target=db.execute("SELECT COUNT(*) FROM jobs WHERE source_id='9T5tn1WGlJg' AND job_type='youtube_download'").fetchone()[0]
count=db.execute("SELECT COUNT(*) FROM jobs WHERE job_type='youtube_download'").fetchone()[0]
print(json.dumps({'active_video_jobs':active,'runnable_download_jobs':runnable,'short_record_hashes':protected,'target_download_jobs':target,'download_job_count':count}));db.close()
'''
def database_state(*,require_idle=True):
    def app():os.setgroups([]);os.setgid(10001);os.setuid(10001)
    result=subprocess.run(['/usr/bin/python3','-c',DB_CHECK],stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=20,preexec_fn=app,
        env={'PATH':'/usr/bin:/bin','HOME':'/tmp','PYTHONNOUSERSITE':'1'})
    assert result.returncode==0;value=json.loads(result.stdout)
    if require_idle:assert not value['active_video_jobs']
    assert not value['runnable_download_jobs'] and value['target_download_jobs']==0;return value

@contextmanager
def worker_locks():
    import fcntl
    with ExitStack() as stack:
        for queue in ('system','video'):
            path=safe(ROOT/'data/locks'/queue/'observer.lock');fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW);stack.callback(os.close,fd)
            s=os.fstat(fd);assert stat.S_ISREG(s.st_mode) and s.st_uid==10001 and s.st_mode&0o022==0
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield

@contextmanager
def deployment_lock():
    import fcntl
    path=safe(Path('/run/lighthouse-video-pilot-v1-deploy.lock'))
    fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        s=os.fstat(fd);assert stat.S_ISREG(s.st_mode) and s.st_uid==0 and s.st_mode&0o077==0
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally:os.close(fd)

def install_configs(paths,new,stats,on_mutation):
    # Mark the attempted mutation BEFORE the first rename. Failure after any
    # subset of three atomic replacements must restore all three originals.
    on_mutation()
    for path in paths:atomic(path,new[path],original=stats[path])

def restart_services():
    compose('up','-d','--no-deps','--no-build','--pull','never','--force-recreate',*SERVICES,timeout=100)

def install_sources(sources):
    safe(INSTALL);assert not INSTALL.exists() or INSTALL.is_dir()
    if not INSTALL.exists():INSTALL.mkdir(mode=0o755);INSTALL.chmod(0o755)
    for p in (INSTALL,*INSTALL.parents):assert p.stat().st_uid==0 and p.stat().st_mode&0o022==0 and not p.is_symlink()
    for name,data in sources.items():
        path=INSTALL/Path(name).name
        if path.exists():assert read(path)==data and path.stat().st_uid==0 and path.stat().st_mode&0o022==0
        else:atomic(path,data,mode=0o644)

PROBE=r'''
from pathlib import Path
import hashlib,json,os
from services.hub.config import Settings
from services.hub.youtube_download import YouTubeDownload
root=Path('/opt/lighthouse-hub');s=Settings.load();assert os.getuid()==10001
assert s.config['youtube_download_enabled'] is True and s.config['youtube_download_acceptance_only'] is True
class NoStore:
 def __getattr__(self,name):raise AssertionError('Pilot automatic enqueue must not touch persistence')
assert YouTubeDownload(s,NoStore()).enqueue_if_eligible(None) is None
p=YouTubeDownload(s,NoStore()).policy();assert p['acceptance_video_ids']==['9T5tn1WGlJg']
print(json.dumps({'uid':os.getuid(),'youtube_download_enabled':s.config['youtube_download_enabled'],'youtube_download_acceptance_only':s.config['youtube_download_acceptance_only'],
 'automatic_probe_skipped':True,'acceptance_video_ids':p['acceptance_video_ids'],'activation_at':p['activation_at'],
 'source_sha256':{n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in ('services/hub/config.py','services/hub/youtube_download.py')}}))
'''
def verify_services(policy_before):
    result={}
    for service in SERVICES:
        cid=compose('ps','-q',service).strip();assert re.fullmatch('[a-f0-9]{12,64}',cid)
        assert run(['/usr/bin/docker','inspect','--format','{{.Image}}',cid]).strip()==CBC
        assert run(['/usr/bin/docker','inspect','--format','{{.State.Running}}',cid]).strip()=='true'
        mounts=json.loads(run(['/usr/bin/docker','inspect','--format','{{json .Mounts}}',cid]))
        for name in SOURCES:
            selected=[m for m in mounts if m['Destination']==str(ROOT/name)];assert len(selected)==1
            assert selected[0]['Type']=='bind' and selected[0]['Source']==str(INSTALL/Path(name).name) and selected[0]['RW'] is False
        value=json.loads(compose('exec','-T',service,'python','-c',PROBE));assert value['source_sha256']==SOURCES and value['activation_at']==policy_before['activation_at']
        result[service]=value
    return result

def wait_ready():
    import urllib.request
    deadline=time.monotonic()+45
    while time.monotonic()<deadline:
        try:
            with urllib.request.urlopen('http://127.0.0.1:8080/ready',timeout=3) as response:
                value=json.loads(response.read(8193));assert value.get('status')=='ready' and value.get('stale_services')==[];return value
        except Exception:time.sleep(1)
    raise RuntimeError('VIDEO_PILOT_READY_TIMEOUT')


def deploy(args):
    sources,attestation=read_attestation()
    paths=(ROOT/'compose.yaml',ROOT/'config/hub.toml',ROOT/'config/youtube-download-policy.toml')
    originals={p:read(p) for p in paths};stats={p:p.stat() for p in paths}
    policy_before=tomllib.loads(originals[paths[2]].decode('utf-8-sig'));hub=tomllib.loads(originals[paths[1]].decode('utf-8-sig'))
    assert hub.get('youtube_download_enabled',False) is False and hub.get('youtube_download_acceptance_only',False) is False
    new={paths[0]:mutate_compose(originals[paths[0]]),paths[1]:mutate_hub(originals[paths[1]]),paths[2]:mutate_policy(originals[paths[2]])}
    baseline=parsed_compose(paths[0]);assert set(baseline['services'])=={'api','system-worker','video-worker','scheduler','tweet-worker'}
    for name in SERVICES:assert run(['/usr/bin/docker','image','inspect','--format','{{.Id}}',baseline['services'][name]['image']]).strip()==CBC
    ident=str(uuid.uuid4());candidate=ROOT/('.compose-video-pilot-'+ident+'.yaml');assert not candidate.exists()
    atomic(candidate,new[paths[0]]);candidate_id=(candidate.stat().st_dev,candidate.stat().st_ino)
    try:assert_compose_delta(baseline,parsed_compose(candidate))
    finally:
        if candidate.is_file() and not candidate.is_symlink() and (candidate.stat().st_dev,candidate.stat().st_ino)==candidate_id:candidate.unlink()
    summary={'version':'youtube-acceptance-only-pilot-deployment-v1','target':TARGET,'services':list(SERVICES),'source_sha256':SOURCES,'attestation':attestation,
        'before_sha256':{str(p):sha(v) for p,v in originals.items()},'after_sha256':{str(p):sha(v) for p,v in new.items()},
        'pilot_target_job_queued_by_script':False,'activation_or_rights_or_limits_changed':False,'core_image':CBC,'full_automatic_download_or_bilibili_publish_enabled':False}
    if not args.deploy:print(json.dumps({**summary,'state':'verified_plan_only','configs_modified':False}));return 0
    # Source stays under root-only write control and survives canonical compose
    # restart/reboot. No external overlay file can be forgotten on the next boot.
    install_sources(sources)
    backup=ROOT/'backups/video-pilot-deploy'/ident;safe(backup);backup.mkdir(mode=0o700,parents=True);backup.chmod(0o700)
    stop_attempted=False;mutated=False;phase='acquire_worker_locks'
    report_path=backup/'deployment-report.json'
    def report(state,**extra):atomic(report_path,wire({**summary,'state':state,'phase':phase,'at':datetime.now(timezone.utc).isoformat(),**extra}))
    try:
        with worker_locks():
            before=database_state()
            for p in paths:assert read(p)==originals[p];atomic(backup/(p.name+'.before'),originals[p])
            phase='stop_three_services';stop_attempted=True;compose('stop',*SERVICES,timeout=60);database_state()
            phase='atomic_config_install'
            def mark_mutation():
                nonlocal mutated
                mutated=True
            install_configs(paths,new,stats,mark_mutation)
            assert_compose_delta(baseline,parsed_compose(paths[0]))
            phase='start_three_services';restart_services()
            phase='verify_loaded_sources_flags';probes=verify_services(policy_before)
            after=database_state();assert after==before
            report('sources_loaded_and_records_unchanged',probes=probes,protected_database_state=after)
        phase='verify_genuine_ready';ready=wait_ready();after=database_state(require_idle=False);assert after['short_record_hashes']==before['short_record_hashes'] and after['download_job_count']==before['download_job_count']
        manifest={**summary,'state':'deployed_acceptance_pilot','ready':ready,'probes':probes,'protected_database_state':after,
            'at':datetime.now(timezone.utc).isoformat(),'canonical_compose':str(paths[0]),'rollback_directory':str(backup),
            'future_updates_must_preserve_or_explicitly_retire_canonical_two_source_binds':True}
        atomic(INSTALL/'manifest.json',wire(manifest),mode=0o644);report('deployed_acceptance_pilot',ready=ready,probes=probes)
        print(json.dumps({'state':'deployed_acceptance_pilot','report_file':str(report_path),'manifest_file':str(INSTALL/'manifest.json'),'pilot_target_job_queued_by_script':False}));return 0
    except BaseException as error:
        failed_phase=phase;phase='rollback';rollback=False
        try:
            # Never stop a newly active job just to claim rollback succeeded.
            with worker_locks():
                database_state()
                if stop_attempted:compose('stop',*SERVICES,timeout=60)
                if mutated:
                    for p in paths:atomic(p,originals[p],original=stats[p])
                    assert parsed_compose(paths[0])==baseline
                if stop_attempted:restart_services()
            if stop_attempted:wait_ready()
            rollback=True
        except BaseException:rollback=False
        report('deployment_failed_rolled_back' if rollback else 'deployment_failed_rollback_not_verified',
            error_code=type(error).__name__,failed_phase=failed_phase,rollback_verified=rollback,database_or_media_restored_or_deleted=False)
        print(json.dumps({'state':'deployment_failed','rollback_verified':rollback,'report_file':str(report_path),'error_code':type(error).__name__,'pilot_target_job_queued_by_script':False}));return 2

def main():
    if sys.flags.optimize:raise RuntimeError('VIDEO_PILOT_OPTIMIZED_PYTHON_UNSUPPORTED')
    assert os.name=='posix' and os.geteuid()==0;os.umask(0o077)
    parser=argparse.ArgumentParser();parser.add_argument('--deploy',action='store_true');args=parser.parse_args()
    with deployment_lock():return deploy(args)
if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:print(json.dumps({'state':'blocked_before_deploy','error_code':type(exc).__name__,'pilot_target_job_queued_by_script':False}));raise SystemExit(2) from None
