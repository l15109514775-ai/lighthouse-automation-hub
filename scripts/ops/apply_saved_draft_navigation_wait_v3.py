"""Default-plan, one RO navigation source only; never retry, save or publish."""
from copy import deepcopy
from pathlib import Path
import argparse,hashlib,io,json,os,stat,subprocess,sys,tarfile,time,types,uuid
import urllib.request
import xml.etree.ElementTree as ET

ROOT=Path('/opt/lighthouse-hub');EV=Path('/home/lighthouse-ops/lighthouse-evidence')
INSTALL=Path('/usr/local/lib/lighthouse-saved-draft-navigation-v3');STATE=Path('/var/lib/lighthouse-saved-draft-navigation-v3')
HELPER='9e9c868ec104e9582925f0cae8b8e5cb936d914665257647ca4d685d46687058'
IMAGE='sha256:9f19b756ca081bfe033ffa01b0face5c9cb70b3d45de08394567150ac747de6e'
ARCHIVE='88d9f5e39ad4d655e0943b49882ec9e4771e31354c7e4221f9bbb4805d97c110'
MANIFEST='94acaf19498c6d9aae16d53abd5b05eaacc44e2d28dcf050170c3bdd1cdb91a9'
ATTEST='743a27c47320c5cd8ba95932a0e8a55bec2babfac1b843c89776a651fa788fb3'
NAV='adapters/short_video_saved_draft_navigation_control.mjs'
OLD='4a97c6c9833a9c58705492ccdbd12db2ac00cbbd564f100e8324085e8d09ef0e'
NEW='27770670eb8fddc997f4e6b2c2c8f6f9a17804d42b41097fca45f04e8e090a69'
HUB='e2b874d7ffb40600083ec534fc8ae449b4dd6f42ff9b775dacbe1d6e89b43df8'
JOB='594aed06-3aa1-45f4-90f0-1db49a602395';ATTEMPT='150c1074-4aed-4f5d-b87a-83cf2840af24'
OTHERS=('api','scheduler','tweet-worker','system-worker')
HOST_FIELDS=('Memory','MemorySwap','NanoCpus','PidsLimit','ShmSize','ReadonlyRootfs','Init','CapDrop','CapAdd','Privileged',
 'SecurityOpt','PortBindings','Tmpfs','RestartPolicy','NetworkMode','Devices','DeviceRequests','Binds','Mounts')

def sha(raw):return hashlib.sha256(raw).hexdigest()
def load_helper(file):
    file=Path(file);assert file.is_absolute() and file.resolve()==file and not any(p.is_symlink() for p in (file,*file.parents))
    raw=file.read_bytes();assert len(raw)<200000 and sha(raw)==HELPER
    module=types.ModuleType('pinned_navigation_host_safe');module.__file__=str(file);exec(compile(raw,str(file),'exec'),module.__dict__);return module
def qualification(d,file,archive):
    file=d.safe(Path(file));assert file.parent.parent==EV and file.name=='test-attestation.json'
    suffix=file.parent.name.removeprefix('saved-draft-manager-wait-v3-five-ro-overlay-')
    assert str(uuid.UUID(suffix))==suffix and file.parent.name=='saved-draft-manager-wait-v3-five-ro-overlay-'+suffix
    raw=d.read(file,500000);assert sha(raw)==ATTEST;a=json.loads(raw)
    assert a['version']=='saved-draft-manager-wait-v3-five-ubuntu-tests-v1' and a['archive_sha256']==ARCHIVE and a['image']==IMAGE
    assert a['full_suite_passed'] is True and a['tests']==5 and a['failures']==a['errors']==a['skipped']==a['exit_code']==0
    assert a['network']=='none' and all(a[k] is False for k in ('application_data_mounted','production_profile_mounted','secrets_environment_injected','oom_killed','timed_out'))
    assert all(a[k] is True for k in ('runtime_matches_declared_candidate','mounts_match_isolated_allowlist','mounted_sources_match_manifest','host_source_sha256_unchanged','no_pid_or_oom_denial'))
    body=d.read(Path(archive),2000000);assert sha(body)==ARCHIVE
    with tarfile.open(fileobj=io.BytesIO(body),mode='r:gz') as tar:
        members=tar.getmembers();assert len(members)==95 and len({m.name for m in members})==95
        assert all(m.isfile() and not m.issym() and not m.islnk() and 0<m.size<500000 for m in members)
        mr=tar.extractfile('manifest.json').read();assert sha(mr)==MANIFEST;manifest=json.loads(mr)
        assert manifest==a['manifest'] and len(manifest['files'])==94 and len(manifest['runtime_source_sha256'])==45
        assert {m.name for m in members}==set(manifest['files'])|{'manifest.json'}
        for member in members:
            if member.name=='manifest.json':continue
            value=tar.extractfile(member).read();entry=manifest['files'][member.name]
            assert len(value)==entry['bytes'] and sha(value)==entry['sha256']
        source=tar.extractfile(NAV).read();assert sha(source)==NEW
    assert manifest['production_runtime_changed_files']==[NAV] and manifest['navigation_source_before_sha256']==OLD
    assert manifest['guard_changed'] is manifest['browser_driver_changed'] is manifest['production_deployed'] is False
    wanted={n:e['sha256'] for n,e in manifest['files'].items()}
    assert a['mounted_source_sha256_before']==a['mounted_source_sha256_after']==wanted
    junit=d.safe(Path(a['junit_file']));assert junit==file.parent/'results.xml'
    jr=d.read(junit,2000000);assert sha(jr)==a['junit_sha256'];cases=list(ET.fromstring(jr).iter('testcase'))
    assert len(cases)==5 and all(c.find(k) is None for c in cases for k in ('failure','error','skipped'))
    assert a['groups']=={'tests/test_saved_draft_navigation_wait.py':5}
    return manifest,source,{'file':str(file),'sha256':ATTEST,'junit_sha256':sha(jr),'archive_sha256':ARCHIVE}

def candidate(before,source):
    after=deepcopy(before);vs=after['services']['video-worker']['volumes'];matches=[v for v in vs if v['target']==str(ROOT/NAV)]
    assert len(matches)==1 and matches[0]['type']=='bind' and matches[0].get('read_only') is True
    previous=matches[0]['source'];matches[0]['source']=str(source)
    reverse=deepcopy(after);next(v for v in reverse['services']['video-worker']['volumes'] if v['target']==str(ROOT/NAV))['source']=previous
    assert reverse==before and all(after['services'][s]==before['services'][s] for s in OTHERS)
    return after

QUERY=r'''
from pathlib import Path
import hashlib,json,os,sqlite3
root=Path('/opt/lighthouse-hub');job_id='594aed06-3aa1-45f4-90f0-1db49a602395';attempt='150c1074-4aed-4f5d-b87a-83cf2840af24'
def sha(raw):return hashlib.sha256(raw).hexdigest()
def wire(v):return json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
def safe(p):assert p.resolve()==p and not any(x.is_symlink() for x in (p,*p.parents));return p
assert os.getuid()==10001
db=sqlite3.connect((root/'data/hub.sqlite3').as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
row=dict(db.execute('SELECT * FROM jobs WHERE job_id=?',(job_id,)).fetchone());payload=json.loads(row['payload']);state=json.loads(row['result'])['replay_highest_video_web_v2']
assert row['state']=='publish_unknown' and row['attempts']==3 and row['source_id']=='9T5tn1WGlJg' and not row['external_publish_id']
assert row['lease_owner'] is None and row['lease_until'] is None and state['state']=='submit_unknown' and state['attempt_id']==attempt
assert payload['site_uid']=='1000000000000000' and state['public_submission_approved'] is False
pubs=[dict(r) for r in db.execute("SELECT * FROM publications WHERE job_id=? OR(source_platform='youtube' AND source_id=?)",(job_id,row['source_id']))]
assert len(pubs)==1 and pubs[0]['job_id']==job_id and pubs[0]['state']=='unknown' and pubs[0]['external_id'] is None and pubs[0]['external_url'] is None
assert not db.execute("SELECT 1 FROM jobs WHERE queue IN ('video','moderation') AND state='processing' LIMIT 1").fetchone()
beats=dict(db.execute("SELECT name,at FROM heartbeats WHERE name IN ('worker:video','worker:video-main-v2b','worker:video-moderation-v2b')"));db.close()
folder=safe(root/'media'/job_id/'replay-native-intents');files=list(folder.iterdir());assert {p.name for p in files}=={'video-selection.json','cover-completion.json','private-draft-save.json'}
intents={}
for p in files:safe(p);assert p.is_file() and 0<p.stat().st_size<100000;intents[p.name]=sha(p.read_bytes())
prep=safe(root/'media'/job_id/'replay-browser-highest-v2'/attempt/'native-preparation.json');raw=prep.read_bytes();p=json.loads(raw)
assert p['private_draft_saved'] is p['metadata_verified'] is p['cover_applied'] is True
assert p['attempt_id']==attempt and p['site_uid']==payload['site_uid'] and p['metadata_sha256']==payload['metadata_sha256']
print(json.dumps({'fingerprint':sha(wire({'job':row,'publications':pubs,'intents':intents,'preparation_sha256':sha(raw)})),'intents':intents,'heartbeats':beats}))
'''
def observation(d):
    def app():os.setgroups([]);os.setgid(10001);os.setuid(10001)
    p=subprocess.run(['/usr/bin/python3','-c',QUERY],preexec_fn=app,capture_output=True,text=True,timeout=20,cwd='/tmp',env={'PATH':'/usr/bin:/bin','HOME':'/tmp'})
    assert p.returncode==0;return json.loads(p.stdout)
def other_cores(d):
    result={}
    for name in OTHERS:
        x=d.inspect(d.cid(name));assert x['State']['Running'];result[name]={'id':x['Id'],'pid':x['State']['Pid'],'started_at':x['State']['StartedAt']}
    return result
def no_profile_processes():
    expected=b'/opt/lighthouse-hub/data/browser-profiles/bilibili-video';found=[]
    for p in Path('/proc').glob('[0-9]*/cmdline'):
        try:args=p.read_bytes().split(b'\0')
        except (OSError,ProcessLookupError):continue
        if b'--user-data-dir='+expected in args or any(a==b'--user-data-dir' and i+1<len(args) and args[i+1]==expected for i,a in enumerate(args)):found.append(int(p.parent.name))
    assert not found
def sources(d,cid,expected):
    code="from pathlib import Path;import hashlib,json,os;assert os.getuid()==10001;root=Path('/opt/lighthouse-hub');print(json.dumps({n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in "+repr(list(expected))+"}))"
    actual=json.loads(d.run(['/usr/bin/docker','exec','--user','10001:10001',cid,'python','-c',code]));assert actual==expected;return actual
def validate_runtime(before,after,source):
    assert before['Image']==after['Image']==IMAGE and after['State']['Running']
    for key in ('User','Cmd','Entrypoint','WorkingDir'):assert before['Config'][key]==after['Config'][key]
    assert after['Config']['User']=='10001:10001'
    def env(value):
        entries=value['Config']['Env'];out=dict(v.split('=',1) for v in entries);assert len(out)==len(entries);return out
    assert env(before)==env(after)
    for key in HOST_FIELDS:
        if key not in ('Binds','Mounts'):assert before['HostConfig'].get(key)==after['HostConfig'].get(key)
    def mounts(value):
        out={m['Destination']:(m['Type'],m['Source'],m['RW']) for m in value['Mounts']};assert len(out)==len(value['Mounts']);return out
    wanted=mounts(before);wanted[str(ROOT/NAV)]=('bind',str(source),False);assert mounts(after)==wanted
def digest_runtime(d,value):
    return {'id':value['Id'],'image':value['Image'],'pid':value['State']['Pid'],'started_at':value['State']['StartedAt'],
        'config_fields_sha256':{k:sha(d.wire(v)) for k,v in value['Config'].items()},
        'host_fields_sha256':{k:sha(d.wire(v)) for k,v in value['HostConfig'].items()},
        'mounts':[{k:m[k] for k in ('Type','Source','Destination','RW')} for m in value['Mounts']]}
def directory(d,path):
    missing=[];p=path
    while not p.exists():missing.append(p);p=p.parent
    d.protected(p)
    for p in reversed(missing):d.directory(p,0o755)
    d.protected(path)
def role_lock(d,role):
    import fcntl
    path=d.safe(ROOT/'data/locks'/role);assert path.is_dir()
    for p in (path,*path.parents):assert p.stat().st_mode&0o022==0
    parentfd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        parent=os.fstat(parentfd);assert parent.st_uid==10001 and parent.st_mode&0o022==0
        fd=os.open('observer.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600,dir_fd=parentfd)
    finally:os.close(parentfd)
    try:
        info=os.fstat(fd);assert stat.S_ISREG(info.st_mode) and info.st_uid==10001 and info.st_mode&0o022==0 and info.st_nlink==1
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        current=path.stat(follow_symlinks=False);assert (current.st_dev,current.st_ino)==(parent.st_dev,parent.st_ino)
        return fd
    except BaseException:os.close(fd);raise
def main():
    assert os.name=='posix' and os.geteuid()==0 and not sys.flags.optimize;os.umask(0o077)
    import fcntl
    parser=argparse.ArgumentParser(description=__doc__)
    for n in ('helper-file','attestation','archive'):parser.add_argument('--'+n,required=True)
    parser.add_argument('--activate',action='store_true');args=parser.parse_args();d=load_helper(args.helper_file)
    manifest,body,proof=qualification(d,args.attestation,args.archive);new=manifest['runtime_source_sha256'];old={**new,NAV:OLD}
    d.protected(ROOT/'compose.yaml');original=d.read(ROOT/'compose.yaml',2000000);original_stat=(ROOT/'compose.yaml').stat();before=d.parsed()
    initial=observation(d);other=other_cores(d);current=d.inspect(d.cid('video-worker'));assert current['Image']==IMAGE and current['State']['Running']
    assert current['Config']['User']=='10001:10001' and current['HostConfig']['ReadonlyRootfs'] is current['HostConfig']['Init'] is True
    assert current['HostConfig']['CapDrop']==['ALL'] and current['HostConfig']['Privileged'] is False
    service=before['services']['video-worker'];vs=service['volumes'];targets={str(ROOT/n) for n in old}
    assert len({v['target'] for v in vs})==len(vs)
    mounted={v['target']:v for v in vs if v['target'] in targets};assert len(mounted)==45 and all(v['type']=='bind' and v.get('read_only') is True for v in mounted.values())
    for name,digest in old.items():
        file=Path(mounted[str(ROOT/name)]['source']);d.protected(file);assert sha(d.read(file,500000))==digest
    sources(d,current['Id'],old)
    config={str(p):sha(d.read(p,2000000)) for p in (ROOT/'config/hub.toml',ROOT/'config/youtube-highest-download-policy.toml',ROOT/'config/replay-highest-video-web-policy.toml')};assert config[str(ROOT/'config/hub.toml')]==HUB
    target=INSTALL/NEW/NAV;after=candidate(before,target)
    report={'version':'saved-draft-navigation-single-source-v3','state':'verified_plan_only','qualification':proof,
        'job_id':JOB,'attempt_id':ATTEMPT,'fingerprint':initial['fingerprint'],'intents':initial['intents'],'before_source_sha256':OLD,'after_source_sha256':NEW,
        'other_cores':other,'configuration_sha256':config,'entry_changed':False,'jobs_publications_attempts_intents_changed':False,
        'raw_environment_exported':False,'files_selected':0,'save_clicks':0,'submit_clicks':0}
    if not args.activate:print(json.dumps(report));return
    lock=os.open('/run/lighthouse-saved-draft-navigation-v3.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    li=os.fstat(lock);assert stat.S_ISREG(li.st_mode) and li.st_uid==0 and li.st_mode&0o077==0 and li.st_nlink==1
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);held=[];area=None
    try:
        for role in ('video','moderation','bilibili-video-profile','heavy'):held.append(role_lock(d,role))
        no_profile_processes();fresh=observation(d);assert fresh['fingerprint']==initial['fingerprint']
        assert d.read(ROOT/'compose.yaml',2000000)==original and other_cores(d)==other
        assert all(sha(d.read(Path(p),2000000))==h for p,h in config.items())
        directory(d,STATE);area=STATE/str(uuid.uuid4());d.directory(area,0o700)
        d.atomic(area/'compose-before.yaml',original,mode=0o600);d.atomic(area/'runtime-before.json',d.wire(digest_runtime(d,current)))
        directory(d,target.parent)
        if target.exists():d.protected(target);assert d.read(target,500000)==body
        else:d.atomic(target,body,mode=0o644)
        report.update(private_evidence=str(area),compose_before_sha256=sha(original));d.atomic(area/'deployment-report.json',d.wire(report))
        d.compose('stop','--timeout','45','video-worker',timeout=65);assert not d.inspect(current['Id'])['State']['Running']
        no_profile_processes();assert observation(d)['fingerprint']==initial['fingerprint']
        d.atomic(ROOT/'compose.yaml',d.wire(after),original=original_stat);assert d.parsed()==after
        for fd in reversed(held):os.close(fd)
        held=[];started=time.time();d.compose('up','-d','--no-deps','--no-build','--pull','never','--force-recreate','video-worker',timeout=180)
        updated=d.inspect(d.cid('video-worker'));validate_runtime(current,updated,target);sources(d,updated['Id'],new)
        deadline=time.monotonic()+70
        while True:
            final=observation(d)
            if all(final['heartbeats'].get(n,0)>=started for n in ('worker:video','worker:video-main-v2b','worker:video-moderation-v2b')):break
            assert time.monotonic()<deadline and d.inspect(updated['Id'])['State']['Running'];time.sleep(1)
        assert final['fingerprint']==initial['fingerprint'] and other_cores(d)==other and d.parsed()==after
        assert all(sha(d.read(Path(p),2000000))==h for p,h in config.items())
        with urllib.request.urlopen('http://127.0.0.1:8080/ready',timeout=5) as r:ready=json.loads(r.read(8193));assert r.status==200 and ready['status']=='ready'
        d.atomic(area/'runtime-after.json',d.wire(digest_runtime(d,updated)))
        report.update(state='active_verified_no_lookup_run',video_container_id=updated['Id'],ready=ready,fingerprint_unchanged=True,other_cores_unchanged=True)
        d.atomic(area/'deployment-report.json',d.wire(report));print(json.dumps(report))
    except BlockingIOError:
        report.update(state='deferred_resource_busy',video_restarted=False);print(json.dumps(report))
    except BaseException:
        report.update(state='requires_reconciliation',automatic_rollback=False)
        if area:d.atomic(area/'deployment-report.json',d.wire(report))
        print(json.dumps(report));raise RuntimeError('NAVIGATION_SINGLE_SOURCE_RECONCILIATION_REQUIRED') from None
    finally:
        for fd in reversed(held):os.close(fd)
        os.close(lock)
if __name__=='__main__':main()
