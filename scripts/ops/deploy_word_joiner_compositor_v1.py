"""Root-only, default-plan CBC tweet-worker core overlay; no job/API writes."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime,timezone
from pathlib import Path
import argparse,hashlib,json,os,re,stat,subprocess,sys,time,uuid,xml.etree.ElementTree as ET

ROOT=Path('/opt/lighthouse-hub');EV=Path('/home/lighthouse-ops/lighthouse-evidence')
INSTALL=Path('/usr/local/lib/lighthouse-compositor-word-joiner-v1')
STATE=Path('/var/lib/lighthouse-compositor-word-joiner-v1')
SERVICE='tweet-worker';OTHER=('api','scheduler','video-worker','system-worker')
CBC='sha256:cbc6cd5076960eba0adb723968bd10257c6691d62e3554cf48ae0ff0df5d8d10'
SOURCE='compositor/src/core.mjs'
NEW='01b073e7e725ea78c182ea5bc821ad73427f8dbab0bb5b14fc90daa799fb39b4'
OLD='1611885da255c9aa25a657a66edbaffdaaabef4883774b94b6c493fa4a389a3a'
REGRESSION='3292400a958b7fee669d2578750bfd786e4e1344f4fa3418a7a2612a48603775'
FILES={SOURCE,'compositor/tests/regression.test.mjs','tests/test_official_api_render.py','tests/official_api_render_fixture.mjs','tests/conftest.py'}
PROTECTED={'compositor/'+name for name in ('package.json','src/renderer.mjs','src/template.mjs','src/schema.json',
    'template.json','template-lock.json','tests/acceptance.test.mjs','scripts/fixtures.mjs','scripts/check-local.mjs',
    'assets/fonts/manifest.json','assets/fonts/NotoColorEmoji.ttf','assets/fonts/NotoSansCJKsc-Bold.otf','assets/fonts/NotoSansCJKsc-Regular.otf')}
PROTECTED|={'adapters/render_machine_official.mjs','adapters/static_media_bytes.mjs'}

def sha(raw):return hashlib.sha256(raw).hexdigest()
def wire(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()
def safe(path):
    path=Path(path);assert path.is_absolute() and path.resolve()==path and not any(p.is_symlink() for p in (path,*path.parents));return path
def read(path,limit=500000):
    path=safe(path);fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_BINARY',0))
    try:
        before=os.fstat(fd);assert stat.S_ISREG(before.st_mode) and 0<before.st_size<=limit
        chunks=[];remaining=limit+1
        while remaining:
            block=os.read(fd,min(remaining,1048576))
            if not block:break
            chunks.append(block);remaining-=len(block)
        raw=b''.join(chunks);after=os.fstat(fd)
        assert len(raw)==before.st_size and all(getattr(before,k)==getattr(after,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns'))
        return raw
    finally:os.close(fd)
def protected(path):
    path=safe(path)
    for p in (path,*path.parents):assert p.stat().st_uid==0 and p.stat().st_mode&0o022==0
def directory(path,mode=0o700):
    path=safe(path)
    if not path.exists():path.mkdir(mode=mode);path.chmod(mode)
    protected(path);assert path.is_dir() and stat.S_IMODE(path.stat().st_mode)==mode
def atomic(path,raw,*,mode=0o600,original=None):
    path=safe(path);temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.partial')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),mode)
    with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fchmod(f.fileno(),mode);os.fsync(f.fileno())
    if original is not None:os.chown(temp,original.st_uid,original.st_gid);os.chmod(temp,stat.S_IMODE(original.st_mode))
    os.replace(temp,path);fd=os.open(path.parent,os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)
def run(args,timeout=90):
    value=subprocess.run(args,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=timeout)
    if value.returncode or len(value.stdout)>8_000_000 or len(value.stderr)>1_000_000:raise RuntimeError('WORD_JOINER_DEPLOY_COMMAND_FAILED')
    return value.stdout
def compose(*args,path=None,timeout=90):return run(['/usr/bin/docker','compose','--project-directory',str(ROOT),'-f',str(path or ROOT/'compose.yaml'),*args],timeout)
def parsed(path=None):return json.loads(compose('config','--format','json','--no-env-resolution',path=path))
def mount():return {'type':'bind','source':str(INSTALL/'core.mjs'),'target':str(ROOT/SOURCE),'read_only':True,'bind':{'create_host_path':False}}

def mutate(raw):
    text=raw.decode('utf-8');assert not raw.startswith(b'\xef\xbb\xbf')
    if text.lstrip().startswith('{'):
        value=json.loads(text);service=value['services'][SERVICE];volumes=service.get('volumes');assert isinstance(volumes,list)
        assert not any(str(ROOT/SOURCE) in wire(v).decode() or str(INSTALL) in wire(v).decode() for v in volumes)
        service['volumes'].append(mount());return wire(value)
    ending='\r\n' if '\r\n' in text else '\n';lines=text.splitlines(keepends=True)
    start=next(i for i,x in enumerate(lines) if x.rstrip('\r\n')=='  '+SERVICE+':');end=start+1
    while end<len(lines):
        line=lines[end]
        if line.strip() and len(line)-len(line.lstrip(' '))<=2:break
        end+=1
    block=lines[start+1:end];assert not any(SOURCE in x or str(INSTALL) in x for x in block)
    indices=[i for i,x in enumerate(block) if re.fullmatch(r'    volumes:\s*(?:#.*)?\r?\n?',x)];assert len(indices)<=1
    if indices:
        position=indices[0]+1
        while position<len(block):
            line=block[position]
            if line.strip() and len(line)-len(line.lstrip(' '))<=4:break
            position+=1
        block[position:position]=['      - '+json.dumps(mount(),separators=(',',':'))+ending]
    else:
        a=next(i for i,x in enumerate(lines) if x.strip()=='x-runtime: &runtime');b=next(i for i,x in enumerate(lines) if x.strip()=='services:')
        common=lines[a+1:b];index=next(i for i,x in enumerate(common) if x.rstrip('\r\n')=='  volumes:');finish=index+1
        while finish<len(common):
            line=common[finish]
            if line.strip() and len(line)-len(line.lstrip(' '))<=2:break
            finish+=1
        inherited=common[index+1:finish];assert inherited and block[0].strip()=='<<: *runtime'
        block[1:1]=['    volumes:'+ending]+['  '+x for x in inherited]+['      - '+json.dumps(mount(),separators=(',',':'))+ending]
    lines[start+1:end]=block;return ''.join(lines).encode()

def assert_delta(before,after):
    expected=deepcopy(before);actual=deepcopy(after)
    assert set(expected['services'])==set(actual['services'])=={SERVICE,*OTHER}
    volumes=actual['services'][SERVICE]['volumes'];old=expected['services'][SERVICE]['volumes']
    matches=[x for x in volumes if x.get('target')==str(ROOT/SOURCE)];assert len(volumes)==len(old)+1 and len(matches)==1
    required=mount();assert all(matches[0].get(k)==required[k] for k in ('type','source','target','read_only'))
    assert matches[0].get('bind',{}).get('create_host_path') is False
    actual['services'][SERVICE]['volumes']=[x for x in volumes if x.get('target')!=str(ROOT/SOURCE)]
    assert actual==expected

def attest(path,digest):
    path=safe(path);assert path.name=='test-attestation.json' and path.parent.parent==EV and re.fullmatch('[a-f0-9]{64}',digest or '')
    prefix='word-joiner-cbc-';assert path.parent.name.startswith(prefix)
    ident=path.parent.name[len(prefix):];assert str(uuid.UUID(ident))==ident
    raw=read(path,200000);assert sha(raw)==digest;a=json.loads(raw);m=a['manifest'];r=a['result']
    assert a['version']=='word-joiner-cbc-ubuntu-tests-v1' and a['image']==m['image']==CBC and a['full_suite_passed'] is True
    assert a['network']=='none' and a['real_data_mounted'] is a['production_code_deployed'] is False and a['exit_code']==0 and a['oom_killed'] is False
    assert a['source_and_font_protections_verified'] is a['mount_allowlist_verified'] is a['runtime_verified'] is True
    assert m['version']=='word-joiner-cbc-test-overlay-v1' and set(m['files'])==FILES and m['node_tests']==25 and m['api_internal_cases']==48
    assert set(m['protected'])==PROTECTED and all(re.fullmatch('[a-f0-9]{64}',x['sha256']) and type(x['bytes']) is int and x['bytes']>0 for x in m['protected'].values())
    assert m['files'][SOURCE]['sha256']==NEW and m['files']['compositor/tests/regression.test.mjs']['sha256']==REGRESSION
    assert r['version']=='word-joiner-cbc-owned-results-v1' and r['passed'] is True and r['npm_exit_code']==r['api_pytest_exit_code']==0
    assert r['node_tests']==25 and r['api_pytest_tests']==1 and r['api_internal_cases_asserted']==48
    assert r['api_junit_clean'] is r['no_pid_or_oom_denial'] is r['all_source_and_protected_hashes_verified'] is True
    assert set(r['npm_footer_checks'])=={'tests','pass','fail','skipped','cancelled'} and all(x is True for x in r['npm_footer_checks'].values())
    owned=Path('/home/lighthouse-ops')/('lighthouse-word-joiner-owned-'+ident);assert a['owned']==str(owned)
    junit=read(owned/'api-results.xml',500000);assert sha(junit)==a['api_junit_sha256']
    cases=ET.fromstring(junit).findall('.//testcase');assert len(cases)==1 and all(c.find(t) is None for c in cases for t in ('failure','error','skipped'))
    assert cases[0].get('classname','').endswith('test_official_api_render')
    log=read(owned/'npm-tests.log',2000000);assert sha(log)==a['node_log_sha256']
    plain=re.sub(r'\x1b\[[0-9;]*m','',log.decode('utf-8'))
    for key,value in (('tests',25),('pass',25),('fail',0),('skipped',0),('cancelled',0)):
        assert re.search(r'(?:^|\n)\s*(?:#|ℹ)\s+'+key+r'\s+'+str(value)+r'\s*(?:\n|$)',plain)
    for name,item in m['files'].items():assert sha(read(path.parent/'overlay'/name))==item['sha256']
    body=read(path.parent/'overlay'/SOURCE);assert sha(body)==NEW
    return body,{'file':str(path),'sha256':digest,'image':CBC,'node_tests':25,'api_internal_cases':48},m['protected']

@contextmanager
def deployment_lock():
    import fcntl
    file=safe(Path('/run/lighthouse-word-joiner-v1-deploy.lock'));fd=os.open(file,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(fd);assert stat.S_ISREG(info.st_mode) and info.st_uid==0 and info.st_mode&0o077==0
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);yield
    finally:os.close(fd)
@contextmanager
def tweet_lock():
    import fcntl
    path=safe(ROOT/'data/locks/tweet/observer.lock');fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
    try:
        info=os.fstat(fd);assert stat.S_ISREG(info.st_mode) and info.st_uid==10001 and info.st_mode&0o022==0
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);yield
    finally:os.close(fd)

DB=r'''
from pathlib import Path
import json,os,sqlite3
assert os.getuid()==10001
db=sqlite3.connect(Path('/opt/lighthouse-hub/data/hub.sqlite3').as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON')
print(json.dumps({'processing':[dict(r) for r in db.execute("SELECT job_id,job_type,state FROM jobs WHERE queue='tweet' AND state='processing'")],
 'heartbeat':db.execute("SELECT COALESCE(MAX(at),0) FROM heartbeats WHERE name='worker:tweet'").fetchone()[0],
 'paused':db.execute("SELECT paused FROM pipelines WHERE name='tweet'").fetchone()[0]}));db.close()
'''
def dbstate(*,idle=False):
    def app():os.setgroups([]);os.setgid(10001);os.setuid(10001)
    value=subprocess.run(['/usr/bin/python3','-c',DB],preexec_fn=app,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=20,
        env={'PATH':'/usr/bin:/bin','HOME':'/tmp','PYTHONNOUSERSITE':'1'})
    assert value.returncode==0;result=json.loads(value.stdout)
    if idle:assert not result['processing']
    return result
def inspect(ident):return json.loads(run(['/usr/bin/docker','inspect',ident]))[0]
def cid(name):
    value=compose('ps','-a','-q',name).strip();assert re.fullmatch('[a-f0-9]{12,64}',value);return inspect(value)['Id']
def filtered(value):
    return {'id':value['Id'],'image':value['Image'],'running':value['State']['Running'],'started_at':value['State']['StartedAt'],
        'pid':value['State']['Pid'],'restart_count':value['RestartCount'],'user':value['Config']['User'],
        'core_mounts':[{k:x[k] for k in ('Type','Source','Destination','RW')} for x in value['Mounts'] if x['Destination']==str(ROOT/SOURCE)]}
def others():return {name:filtered(inspect(cid(name))) for name in OTHER}
def check_others(before):
    current=others();assert current==before and all(x['running'] for x in current.values());return current
def probe(ident,protected_files,expected):
    value=inspect(ident);assert value['Image']==CBC and value['Config']['User']=='10001:10001' and value['State']['Running']
    expected_files={name:item['sha256'] for name,item in protected_files.items()};expected_files[SOURCE]=expected
    code="from pathlib import Path;import hashlib,json,os;assert os.getuid()==10001;names="+repr(list(expected_files))+";print(json.dumps({n:hashlib.sha256((Path('/opt/lighthouse-hub')/n).read_bytes()).hexdigest() for n in names}))"
    result=json.loads(run(['/usr/bin/docker','exec',ident,'python','-c',code]));assert result==expected_files
    mounts=[m for m in value['Mounts'] if m['Destination']==str(ROOT/SOURCE)]
    if expected==NEW:
        assert len(mounts)==1 and mounts[0]['Type']=='bind' and mounts[0]['Source']==str(INSTALL/'core.mjs') and mounts[0]['RW'] is False
    else:assert not mounts
    return {'container':filtered(value),'hashes':result,'actual_uid':10001}
def wait_ready(start):
    import urllib.request
    deadline=time.monotonic()+60
    while time.monotonic()<deadline:
        try:
            current=dbstate()
            with urllib.request.urlopen('http://127.0.0.1:8080/ready',timeout=3) as response:ready=json.loads(response.read(8193))
            if current['heartbeat']>=start and ready['status']=='ready' and ready['stale_services']==[]:return {'heartbeat':current['heartbeat'],'ready':ready}
        except Exception:pass
        time.sleep(1)
    raise RuntimeError('WORD_JOINER_FRESH_TWEET_HEARTBEAT_REQUIRED')
def up():return compose('up','-d','--no-deps','--no-build','--pull','never','--force-recreate',SERVICE,timeout=100)

def deploy(args):
    body,evidence,protected_files=attest(Path(args.attestation_file),args.attestation_sha256)
    path=ROOT/'compose.yaml';original=read(path,2000000);original_stat=path.stat();changed=mutate(original);before=parsed()
    ident=str(uuid.uuid4());candidate=ROOT/('.compose-word-joiner-'+ident+'.yaml');atomic(candidate,changed)
    inode=(candidate.stat().st_dev,candidate.stat().st_ino)
    try:assert_delta(before,parsed(candidate))
    finally:
        if candidate.is_file() and not candidate.is_symlink() and (candidate.stat().st_dev,candidate.stat().st_ino)==inode:candidate.unlink()
    assert run(['/usr/bin/docker','image','inspect','-f','{{.Id}}',before['services'][SERVICE]['image']]).strip()==CBC
    fixed={p:read(p,2000000) for p in (ROOT/'config/hub.toml',)}
    protected_containers=others();assert all(x['running'] for x in protected_containers.values())
    old_probe=probe(cid(SERVICE),protected_files,OLD)
    summary={'version':'word-joiner-compositor-deployment-v1','attestation':evidence,'image':CBC,'core_before_sha256':OLD,'core_after_sha256':NEW,
        'compose_before_sha256':sha(original),'compose_after_sha256':sha(changed),'other_containers':protected_containers,
        'services_changed':[SERVICE],'jobs_created':0,'database_written_by_helper':False,'publication_requested':False,'protected_files':protected_files}
    if not args.activate:print(json.dumps({**summary,'state':'verified_plan_only'}));return 0
    directory(INSTALL,0o755);target=INSTALL/'core.mjs'
    if target.exists():protected(target);assert read(target)==body
    else:atomic(target,body,mode=0o644)
    directory(STATE,0o700);area=STATE/ident;directory(area,0o700)
    atomic(area/'compose-before.yaml',original);atomic(area/'core-before.json',wire(old_probe));atomic(area/'other-containers-before.json',wire(protected_containers))
    phase='tweet_idle';stopped=False;mutated=False
    def report(state,**extra):atomic(area/'deployment-report.json',wire({**summary,'state':state,'phase':phase,'at':datetime.now(timezone.utc).isoformat(),**extra}))
    def unchanged():assert all(read(p,2000000)==value for p,value in fixed.items());check_others(protected_containers)
    try:
        report('activation_prepared')
        with tweet_lock():
            dbstate(idle=True);unchanged();assert read(path,2000000)==original
            phase='stop_tweet_only';stopped=True;compose('stop','--timeout','45',SERVICE,timeout=60);assert not inspect(cid(SERVICE))['State']['Running']
            dbstate(idle=True);phase='canonical_overlay_install';mutated=True;atomic(path,changed,original=original_stat);assert_delta(before,parsed());report('compose_overlay_installed')
        # The queue lock must be released before up/heartbeat: Worker.once()
        # only advertises itself after acquiring the same OS lock.
        phase='start_tweet_only';started=time.time();up();phase='runtime_probe';new_probe=probe(cid(SERVICE),protected_files,NEW);unchanged()
        phase='fresh_heartbeat_ready';ready=wait_ready(started);unchanged()
        atomic(area/'core-after.json',wire(new_probe));atomic(area/'other-containers-after.json',wire(others()))
        report('activated_tweet_core_overlay',probe=new_probe,ready=ready,rollback_directory=str(area))
        atomic(INSTALL/'manifest.json',wire({**summary,'state':'active','rollback_directory':str(area)}),mode=0o644)
        print(json.dumps({'state':'activated_tweet_core_overlay','report_file':str(area/'deployment-report.json'),'other_four_containers_unchanged':True,'publication_requested':False}));return 0
    except BaseException as error:
        failed_phase=phase;phase='rollback';restored=False
        try:
            with tweet_lock():
                dbstate(idle=True);unchanged()
                if stopped:compose('stop','--timeout','45',SERVICE,timeout=60)
                current_raw=read(path,2000000);assert current_raw in (original,changed)
                if current_raw==changed:atomic(path,original,original=original_stat)
                assert parsed()==before
            if stopped:
                started=time.time();up();probe(cid(SERVICE),protected_files,OLD);wait_ready(started)
            unchanged();restored=True
        except BaseException:pass
        report('failed_rollback_verified' if restored else 'failed_rollback_not_verified',failed_phase=failed_phase,rollback_verified=restored,error_code=type(error).__name__)
        print(json.dumps({'state':'deployment_failed','rollback_verified':restored,'report_file':str(area/'deployment-report.json'),'error_code':type(error).__name__,'publication_requested':False}));return 2

def main():
    if sys.flags.optimize:raise RuntimeError('OPTIMIZED_PYTHON_UNSUPPORTED')
    assert os.name=='posix' and os.geteuid()==0;os.umask(0o077)
    p=argparse.ArgumentParser();p.add_argument('--attestation-file',required=True);p.add_argument('--attestation-sha256',required=True);p.add_argument('--activate',action='store_true');a=p.parse_args()
    with deployment_lock():return deploy(a)
if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'state':'blocked_before_deployment','error_code':type(error).__name__,'publication_requested':False}));raise SystemExit(2) from None
