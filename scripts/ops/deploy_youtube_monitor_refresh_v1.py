"""Default-plan-only third read-only source bind for the existing YouTube pilot.

Standalone incremental deployer. Never calls the old pilot deployment, resets
job counts, queues work, changes operating policy, or writes the application DB.
"""
from contextlib import ExitStack,contextmanager
from copy import deepcopy
from datetime import datetime,timezone
from pathlib import Path
import argparse,hashlib,json,os,re,stat,subprocess,sys,time,uuid

ROOT=Path('/opt/lighthouse-hub');EV=Path('/home/lighthouse-ops/lighthouse-evidence')
INSTALL=Path('/usr/local/lib/lighthouse-youtube-monitor-v1')
PILOT=Path('/usr/local/lib/lighthouse-video-pilot-v1')
SERVICES=('api','system-worker','video-worker')
CBC='sha256:cbc6cd5076960eba0adb723968bd10257c6691d62e3554cf48ae0ff0df5d8d10'
TEST_IMAGE='sha256:9f19b756ca081bfe033ffa01b0face5c9cb70b3d45de08394567150ac747de6e'
SOURCE='services/hub/youtube_monitor.py'
DIGEST='22e1424835378dda85317452c7e23f3fcbbeb51a2efc9970fa998d9f95a60ac7'
OLD={'services/hub/config.py':'b2e5659c523829a23b64934ce46a679ccc98f4e4fc02670e60f41288dacd399b',
     'services/hub/youtube_download.py':'d14f7d8c522481a6070f86afd46f402b09b6277212821986e89eba3bf0f0337a'}
GROUPS={'tests/test_youtube_download.py':71,'tests/test_youtube_download_acceptance_only.py':15,
        'tests/test_youtube_monitor.py':15,'tests/test_youtube_monitor_publication_refresh.py':4}

def sha(data):return hashlib.sha256(data).hexdigest()
def wire(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()

def safe(path):
    path=Path(path)
    assert path.is_absolute() and path.resolve()==path and not any(p.is_symlink() for p in (path,*path.parents))
    return path

def read(path,limit=500000):
    path=safe(path);before=path.stat();assert stat.S_ISREG(before.st_mode) and before.st_size<=limit
    body=path.read_bytes();after=path.stat()
    assert len(body)==before.st_size and not path.is_symlink() and all(getattr(before,k)==getattr(after,k) for k in
        ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns'))
    return body

def protected(path,*,directory=False):
    safe(path)
    for p in (path,*path.parents):assert p.stat().st_uid==0 and p.stat().st_mode&0o022==0
    assert path.is_dir() if directory else path.is_file()

def atomic(path,body,*,original=None,mode=0o600):
    path=safe(path);temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.partial')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
    with os.fdopen(fd,'wb') as stream:
        stream.write(body);stream.flush();os.fchmod(stream.fileno(),mode);os.fsync(stream.fileno())
    if original:os.chown(temporary,original.st_uid,original.st_gid);os.chmod(temporary,stat.S_IMODE(original.st_mode))
    os.replace(temporary,path);fd=os.open(path.parent,os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)

def run(args,timeout=90):
    result=subprocess.run(args,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=timeout)
    if result.returncode:raise RuntimeError('MONITOR_REFRESH_COMMAND_FAILED')
    assert len(result.stdout)<=3_000_000 and len(result.stderr)<=1_000_000;return result.stdout

def compose(*args,path=None,timeout=90):
    return run(['/usr/bin/docker','compose','--project-directory',str(ROOT),'-f',str(path or ROOT/'compose.yaml'),*args],timeout)

def parsed(path):return json.loads(compose('config','--format','json',path=path))

def monitor_mount():
    return {'type':'bind','source':str(INSTALL/'youtube_monitor.py'),'target':str(ROOT/SOURCE),
            'read_only':True,'bind':{'create_host_path':False}}

def mutate_compose(raw):
    text=raw.decode('utf-8');assert not raw.startswith(b'\xef\xbb\xbf')
    ending='\r\n' if '\r\n' in text else '\n';lines=text.splitlines(keepends=True)
    for service in SERVICES:
        start=next(i for i,x in enumerate(lines) if x.rstrip('\r\n')=='  '+service+':');end=start+1
        while end<len(lines):
            line=lines[end]
            if line.strip() and len(line)-len(line.lstrip(' '))<=2:break
            end+=1
        block=lines[start+1:end]
        assert not any(SOURCE in x or str(INSTALL) in x for x in block)
        index=next(i for i,x in enumerate(block) if re.fullmatch(r'    volumes:\s*(?:#.*)?\r?\n?',x))
        finish=index+1
        while finish<len(block):
            line=block[finish]
            if line.strip() and len(line)-len(line.lstrip(' '))<=4:break
            finish+=1
        block[finish:finish]=['      - '+json.dumps(monitor_mount(),separators=(',',':'))+ending]
        lines[start+1:end]=block
    return ''.join(lines).encode()

def assert_delta(before,after):
    expected=deepcopy(before);actual=deepcopy(after)
    assert expected.get('name')==actual.get('name')=='lighthouse-hub'
    for name in SERVICES:
        old=expected['services'][name]['volumes'];volumes=actual['services'][name]['volumes']
        for source in OLD:
            matches=[v for v in old if v.get('target')==str(ROOT/source)]
            assert len(matches)==1 and matches[0].get('type')=='bind' and matches[0].get('read_only') is True
            assert matches[0]['source']==str(PILOT/Path(source).name)
        selected=[v for v in volumes if v.get('target')==str(ROOT/SOURCE)]
        assert len(volumes)==len(old)+1 and len(selected)==1
        observed=selected[0];required=monitor_mount()
        assert all(observed.get(k)==required[k] for k in ('type','source','target','read_only'))
        assert observed.get('bind',{}).get('create_host_path') is False
        actual['services'][name]['volumes']=[v for v in volumes if v.get('target')!=str(ROOT/SOURCE)]
    assert actual==expected

def attest(path,digest):
    path=safe(path);assert re.fullmatch('[a-f0-9]{64}',digest or '')
    assert path.name=='test-attestation.json' and path.parent.parent==EV
    prefix='youtube-monitor-publication-overlay-v1-';assert path.parent.name.startswith(prefix)
    value=path.parent.name[len(prefix):];assert str(uuid.UUID(value))==value
    raw=read(path,100000);assert sha(raw)==digest;a=json.loads(raw)
    assert a['version']=='youtube-monitor-publication-overlay-ubuntu-tests-v1' and a['image']==TEST_IMAGE
    assert a['tests']==105 and a['full_suite_passed'] is True and a['groups']==GROUPS
    assert a['failures']==a['errors']==a['skipped']==a['exit_code']==0 and a['oom_killed'] is False
    assert a['network']=='none' and a['application_data_mounted'] is False and a['whole_tree_deployed'] is False
    assert a['runtime_readonly_overlays_only'] is True and a['runtime_matches_production'] is True and a['no_pid_or_oom_denial'] is True
    manifest=a['manifest'];assert manifest['version']=='youtube-monitor-publication-ro-overlay-v1'
    assert manifest['base_image']==TEST_IMAGE and manifest['expected_total_tests']==105 and manifest['expected_test_counts']==GROUPS
    assert len(manifest['files'])==7 and a['real_uploads']==a['publications']==0
    for name,expected in {**OLD,SOURCE:DIGEST}.items():assert manifest['files'][name]['sha256']==expected
    body=read(path.parent/'overlay'/SOURCE,150000);assert sha(body)==DIGEST
    return body,{'file':str(path),'sha256':digest,'image':a['image'],'tests':105}

@contextmanager
def deployment_lock():
    import fcntl
    path=safe(Path('/run/lighthouse-youtube-monitor-v1-deploy.lock'))
    fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        s=os.fstat(fd);assert stat.S_ISREG(s.st_mode) and s.st_uid==0 and s.st_mode&0o077==0
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);yield
    finally:os.close(fd)

@contextmanager
def worker_locks():
    import fcntl
    with ExitStack() as stack:
        for queue in ('system','video'):
            path=safe(ROOT/'data/locks'/queue/'observer.lock');fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW);stack.callback(os.close,fd)
            s=os.fstat(fd);assert stat.S_ISREG(s.st_mode) and s.st_uid==10001 and s.st_mode&0o022==0
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield

DB_CHECK=r'''
from pathlib import Path
import hashlib,json,os,sqlite3
assert os.getuid()==10001
db=sqlite3.connect(Path('/opt/lighthouse-hub/data/hub.sqlite3').as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON')
active=[dict(r) for r in db.execute("SELECT job_id,job_type,state FROM jobs WHERE queue IN ('system','video') AND state IN ('processing','publish_unknown')")]
runnable=[dict(r) for r in db.execute("SELECT job_id,state FROM jobs WHERE job_type='youtube_download' AND state IN ('pending','retry_scheduled','processing','publish_unknown')")]
rows=[dict(r) for r in db.execute("SELECT * FROM jobs WHERE source_id='0H6_bTvWeDQ' AND job_type IN ('youtube_download','short_video_publish') ORDER BY job_id")]
print(json.dumps({'active':active,'runnable':runnable,'short_record_hashes':{r['job_id']:hashlib.sha256(json.dumps(r,sort_keys=True,separators=(',',':')).encode()).hexdigest() for r in rows},
 'download_job_count':db.execute("SELECT COUNT(*) FROM jobs WHERE job_type='youtube_download'").fetchone()[0]}));db.close()
'''

def dbstate(*,idle=True):
    def app():os.setgroups([]);os.setgid(10001);os.setuid(10001)
    result=subprocess.run(['/usr/bin/python3','-c',DB_CHECK],stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=20,preexec_fn=app,
        env={'PATH':'/usr/bin:/bin','HOME':'/tmp','PYTHONNOUSERSITE':'1'})
    assert result.returncode==0;value=json.loads(result.stdout)
    if idle:assert not value['active']
    assert not value['runnable'];return value

PROBE=r'''
from pathlib import Path
import hashlib,json,os
from services.hub.config import Settings
from services.hub.youtube_monitor import YouTubeMonitor
s=Settings.load();assert os.getuid()==10001
assert s.config['youtube_download_enabled'] is True and s.config['youtube_download_acceptance_only'] is True
assert any(isinstance(c,str) and 'published_at=excluded.published_at' in c for c in YouTubeMonitor.observe_video.__code__.co_consts)
print(json.dumps({'youtube_download_enabled':True,'youtube_download_acceptance_only':True,'loaded_official_publication_refresh':True,
 'source_sha256':{n:hashlib.sha256((s.root/n).read_bytes()).hexdigest() for n in ('services/hub/config.py','services/hub/youtube_download.py','services/hub/youtube_monitor.py')}}))
'''

def probe_services():
    values={}
    for service in SERVICES:
        cid=compose('ps','-q',service).strip();assert re.fullmatch('[a-f0-9]{12,64}',cid)
        assert run(['/usr/bin/docker','inspect','--format','{{.Image}}',cid]).strip()==CBC
        assert run(['/usr/bin/docker','inspect','--format','{{.State.Running}}',cid]).strip()=='true'
        mounts=json.loads(run(['/usr/bin/docker','inspect','--format','{{json .Mounts}}',cid]))
        for name in (*OLD,SOURCE):
            source=(INSTALL if name==SOURCE else PILOT)/Path(name).name
            matches=[m for m in mounts if m['Destination']==str(ROOT/name)]
            assert len(matches)==1 and matches[0]['Type']=='bind' and matches[0]['Source']==str(source) and matches[0]['RW'] is False
        value=json.loads(compose('exec','-T',service,'python','-c',PROBE));assert value['source_sha256']=={**OLD,SOURCE:DIGEST}
        values[service]=value
    return values

def ready():
    import urllib.request
    deadline=time.monotonic()+45
    while time.monotonic()<deadline:
        try:
            with urllib.request.urlopen('http://127.0.0.1:8080/ready',timeout=3) as response:
                value=json.loads(response.read(8193));assert value['status']=='ready' and value['stale_services']==[];return value
        except Exception:time.sleep(1)
    raise RuntimeError('MONITOR_REFRESH_READY_TIMEOUT')

def restart():compose('up','-d','--no-deps','--no-build','--pull','never','--force-recreate',*SERVICES,timeout=100)

def install(body):
    safe(INSTALL)
    if not INSTALL.exists():INSTALL.mkdir(mode=0o755);INSTALL.chmod(0o755)
    protected(INSTALL,directory=True);path=INSTALL/'youtube_monitor.py'
    if path.exists():protected(path);assert read(path)==body
    else:atomic(path,body,mode=0o644)
    for name,expected in OLD.items():protected(PILOT/Path(name).name);assert sha(read(PILOT/Path(name).name))==expected

def deploy(args):
    body,evidence=attest(Path(args.attestation_file),args.attestation_sha256)
    path=ROOT/'compose.yaml';original=read(path);original_stat=path.stat();changed=mutate_compose(original)
    fixed={p:read(p) for p in (ROOT/'config/hub.toml',ROOT/'config/youtube-download-policy.toml')}
    baseline=parsed(path)
    for service in SERVICES:assert run(['/usr/bin/docker','image','inspect','--format','{{.Id}}',baseline['services'][service]['image']]).strip()==CBC
    ident=str(uuid.uuid4());candidate=ROOT/('.compose-youtube-monitor-'+ident+'.yaml');atomic(candidate,changed)
    inode=(candidate.stat().st_dev,candidate.stat().st_ino)
    try:assert_delta(baseline,parsed(candidate))
    finally:
        if candidate.is_file() and not candidate.is_symlink() and (candidate.stat().st_dev,candidate.stat().st_ino)==inode:candidate.unlink()
    summary={'version':'youtube-monitor-refresh-deployment-v1','source_sha256':DIGEST,'attestation':evidence,'core_image':CBC,
        'compose_before_sha256':sha(original),'compose_after_sha256':sha(changed),'operating_config_sha256':{str(p):sha(v) for p,v in fixed.items()},
        'job_queued':False,'database_written':False,'operating_policy_changed':False,'services':list(SERVICES)}
    if not args.deploy:print(json.dumps({**summary,'state':'verified_plan_only'}));return 0
    install(body)
    backup=safe(ROOT/'backups/youtube-monitor-deploy'/ident);backup.mkdir(mode=0o700,parents=True);backup.chmod(0o700)
    report_path=backup/'deployment-report.json';stop_attempted=False;mutated=False;phase='queue_locks'
    def config_unchanged():assert all(read(p)==data for p,data in fixed.items())
    def report(state,**extra):atomic(report_path,wire({**summary,'state':state,'phase':phase,'at':datetime.now(timezone.utc).isoformat(),**extra}))
    try:
        with worker_locks():
            before=dbstate();config_unchanged();assert read(path)==original
            atomic(backup/'compose.yaml.before',original)
            phase='stop_three_services';stop_attempted=True;compose('stop',*SERVICES,timeout=60);dbstate()
            phase='canonical_compose_install';mutated=True;atomic(path,changed,original=original_stat);assert_delta(baseline,parsed(path))
            phase='restart_three_services';restart();phase='source_and_flag_probe';probes=probe_services()
            after=dbstate();assert after==before;config_unchanged()
        phase='genuine_ready';observed=ready();after=dbstate(idle=False)
        assert after['short_record_hashes']==before['short_record_hashes'] and after['download_job_count']==before['download_job_count'];config_unchanged()
        manifest={**summary,'state':'deployed_monitor_refresh','at':datetime.now(timezone.utc).isoformat(),'probes':probes,
            'ready':observed,'protected_database_state':after,'rollback_directory':str(backup),
            'future_updates_must_preserve_or_explicitly_retire_all_three_source_binds':True}
        atomic(INSTALL/'manifest.json',wire(manifest),mode=0o644);report('deployed_monitor_refresh',probes=probes,ready=observed,protected_database_state=after)
        print(json.dumps({'state':'deployed_monitor_refresh','report_file':str(report_path),'manifest_file':str(INSTALL/'manifest.json'),'job_queued':False}));return 0
    except BaseException as error:
        failed_phase=phase;phase='rollback';rolled_back=False
        try:
            with worker_locks():
                dbstate();config_unchanged()
                if stop_attempted:compose('stop',*SERVICES,timeout=60)
                if mutated:atomic(path,original,original=original_stat);assert parsed(path)==baseline
                if stop_attempted:restart()
            if stop_attempted:ready()
            rolled_back=True
        except BaseException:pass
        report('deployment_failed_rolled_back' if rolled_back else 'deployment_failed_rollback_not_verified',failed_phase=failed_phase,
            rollback_verified=rolled_back,error_code=type(error).__name__)
        print(json.dumps({'state':'deployment_failed','rollback_verified':rolled_back,'report_file':str(report_path),'error_code':type(error).__name__,'job_queued':False}));return 2

def main():
    if sys.flags.optimize:raise RuntimeError('MONITOR_REFRESH_OPTIMIZED_PYTHON_UNSUPPORTED')
    assert os.name=='posix' and os.geteuid()==0;os.umask(0o077)
    parser=argparse.ArgumentParser();parser.add_argument('--attestation-file',required=True);parser.add_argument('--attestation-sha256',required=True)
    parser.add_argument('--deploy',action='store_true');args=parser.parse_args()
    with deployment_lock():return deploy(args)

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'state':'blocked_before_deployment','error_code':type(error).__name__,'job_queued':False}));raise SystemExit(2) from None
