"""Plan or recover stale Chromium singleton links; never navigate or publish.

Only three named IPC symlinks may be quarantined. Cookies/Storage contents
are not read, copied, deleted or exported. No job/database operation exists.
"""
from pathlib import Path
import argparse,errno,json,os,re,stat,subprocess,time,uuid

ROOT=Path('/opt/lighthouse-hub');PROFILE=ROOT/'data/browser-profiles/bilibili-video'
IMAGE='sha256:9f19b756ca081bfe033ffa01b0face5c9cb70b3d45de08394567150ac747de6e'
NAMES=('SingletonLock','SingletonCookie','SingletonSocket')
ENV={'HOME':'/tmp/highest/home','XDG_CONFIG_HOME':'/tmp/highest/config','XDG_CACHE_HOME':'/tmp/highest/cache'}

def wire(value):return (json.dumps(value,sort_keys=True,indent=2)+'\n').encode()
def run(args,timeout=90):return subprocess.check_output(args,text=True,timeout=timeout)

def stop_own_probe(name,identifier):
    assert re.fullmatch('lighthouse-replay-launch-probe-[a-f0-9]{12}',name) and re.fullmatch('[a-f0-9]{32}',identifier)
    value=json.loads(run(['/usr/bin/docker','inspect','--type=container',name],timeout=20))[0]
    assert re.fullmatch('[a-f0-9]{64}',value['Id']) and value['Image']==IMAGE and value['Config']['User']=='10001:10001'
    labels=value['Config']['Labels'];assert labels.get('lighthouse.task')=='replay-launch-only-v1' and labels.get('lighthouse.probe-id')==identifier
    mounts=[m for m in value['Mounts'] if m['Type']=='bind']
    assert len(mounts)==1 and mounts[0]['Source']==str(PROFILE) and mounts[0]['Destination']==str(PROFILE) and mounts[0]['RW'] is True
    assert value['HostConfig']['NetworkMode']=='none'
    run(['/usr/bin/docker','stop','--time','15',value['Id']],timeout=35)
    remaining=run(['/usr/bin/docker','ps','--all','--quiet','--no-trunc','--filter','id='+value['Id']],timeout=20).strip()
    if remaining:
        assert remaining==value['Id']
        assert json.loads(run(['/usr/bin/docker','inspect',value['Id']],timeout=20))[0]['State']['Running'] is False
    return {'container_id':value['Id'],'only_owned_probe_stopped':True,'remaining_running':False}
def directory(parts):
    flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW;fd=os.open(ROOT,flags)
    try:
        for part in parts:
            nextfd=os.open(part,flags,dir_fd=fd);os.close(fd);fd=nextfd
        result=fd;fd=None;return result
    finally:
        if fd is not None:os.close(fd)

def links(fd):
    found={}
    for name in NAMES:
        info=os.stat(name,dir_fd=fd,follow_symlinks=False)
        assert stat.S_ISLNK(info.st_mode) and info.st_uid==10001
        found[name]={'target':os.readlink(name,dir_fd=fd),'inode':info.st_ino,'device':info.st_dev,'mtime_ns':info.st_mtime_ns}
    return found

def owners(identity,*,proc=Path('/proc')):
    matches=[];unreadable=[]
    for folder in proc.iterdir():
        if not folder.name.isdigit():continue
        try:args=(folder/'cmdline').read_bytes().split(b'\x00')
        except (FileNotFoundError,ProcessLookupError):continue
        except PermissionError:unreadable.append(int(folder.name));continue
        for i,arg in enumerate(args):
            raw=arg.split(b'=',1)[1] if arg.startswith(b'--user-data-dir=') else args[i+1] if arg==b'--user-data-dir' and i+1<len(args) else None
            if not raw:continue
            value=os.fsdecode(raw);inside=folder/'root'/value.lstrip('/') if os.path.isabs(value) else folder/'cwd'/value
            try:info=inside.stat()
            except (FileNotFoundError,ProcessLookupError):continue
            except PermissionError:unreadable.append(int(folder.name));continue
            if (info.st_dev,info.st_ino)==identity:matches.append(int(folder.name));break
    return {'matching_owner_pids':sorted(set(matches)),'unreadable_pids':sorted(set(unreadable))}

def quarantine(fd,expected,lock_target):
    assert set(expected)==set(NAMES) and expected['SingletonLock']['target']==lock_target
    assert re.fullmatch(r'.+-[0-9]+',lock_target) and links(fd)==expected
    backup='.singleton-stale-quarantine-'+str(uuid.uuid4());os.mkdir(backup,0o700,dir_fd=fd)
    dest=os.open(backup,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
    try:
        for name in NAMES:
            assert links_one(fd,name)==expected[name]
            os.rename(name,name,src_dir_fd=fd,dst_dir_fd=dest)
        os.fsync(dest);os.fsync(fd);os.fchown(dest,10001,10001);os.fchmod(dest,0o700);os.fsync(dest)
    finally:os.close(dest)
    return backup

def links_one(fd,name):
    info=os.stat(name,dir_fd=fd,follow_symlinks=False);assert stat.S_ISLNK(info.st_mode)
    return {'target':os.readlink(name,dir_fd=fd),'inode':info.st_ino,'device':info.st_dev,'mtime_ns':info.st_mtime_ns}

PREPARE=r'''
from pathlib import Path
import os,json
assert os.getuid()==os.getgid()==10001
base=Path('/tmp/highest');assert base.resolve()==base and not base.is_symlink()
base.mkdir(mode=0o700,exist_ok=True);assert base.stat().st_uid==10001
out={}
for name in ('home','config','cache'):
 path=base/name;assert path.resolve()==path and not path.is_symlink()
 path.mkdir(mode=0o700,exist_ok=True);assert path.stat().st_uid==10001
 out[name]={'uid':path.stat().st_uid,'writable':os.access(path,os.W_OK)}
print(json.dumps(out))
'''

PROBE=r'''
import fs from 'node:fs/promises';
const {chromium}=await import('file:///opt/lighthouse-hub/compositor/node_modules/playwright/index.mjs');
for(const p of ['/tmp/highest/home','/tmp/highest/config','/tmp/highest/cache'])await fs.mkdir(p,{recursive:true,mode:0o700});
let context=null;
const classify=e=>{
 const text=String(e?.message||'');
 if(/profile appears to be in use|ProcessSingleton|SingletonLock|another Chromium process/i.test(text))return 'PROFILE_IN_USE';
 if(/Executable doesn.t exist|browser executable|ENOENT.*chrome/i.test(text))return 'EXECUTABLE_UNAVAILABLE';
 if(/crashpad.*database|database.*crashpad/i.test(text))return 'CRASHPAD_DATABASE';
 if(/No usable sandbox|namespace.*Operation not permitted|Failed to move to new namespace/i.test(text))return 'SANDBOX_UNAVAILABLE';
 if(/pthread_create|Resource temporarily unavailable/i.test(text))return 'PROCESS_RESOURCE_UNAVAILABLE';
 if(/EACCES|EROFS|Permission denied|Read-only file system/i.test(text))return 'FILESYSTEM_UNWRITABLE';
 return 'UNCLASSIFIED_LAUNCH_ERROR';
};
let result;
try{
 context=await chromium.launchPersistentContext('/opt/lighthouse-hub/data/browser-profiles/bilibili-video',
  {headless:true,channel:'chromium',chromiumSandbox:true,viewport:{width:1280,height:960},acceptDownloads:false});
 result={version:'replay-launch-only-probe-v1',state:'launched_offline',error_category:null};
}catch(e){result={version:'replay-launch-only-probe-v1',state:'launch_failed',error_category:classify(e),error_name:['Error','TypeError','ReferenceError'].includes(e?.name)?e.name:'Other'};}
finally{await context?.close();}
console.log(JSON.stringify({...result,navigation_calls:0,file_selections:0,submissions:0,cookies_or_storage_exported:false}));
'''

def launch_only(hostname):
    identifier=uuid.uuid4().hex;probe_name='lighthouse-replay-launch-probe-'+identifier[:12]
    command=['/usr/bin/docker','run','--rm','--pull=never','--init','--name',probe_name,
        '--label','lighthouse.task=replay-launch-only-v1','--label','lighthouse.probe-id='+identifier,
        '--network=none','--read-only','--user','10001:10001','--hostname',hostname,
        '--cap-drop','ALL','--security-opt','no-new-privileges:true','--security-opt','seccomp='+str(ROOT/'config/browser-seccomp.json'),
        '--memory','1400m','--pids-limit','256','--shm-size','256m','--tmpfs','/tmp:rw,size=192m,mode=1777',
        '--mount','type=bind,source='+str(PROFILE)+',target='+str(PROFILE),'-e','PLAYWRIGHT_BROWSERS_PATH=/opt/lighthouse-hub/compositor/runtime/browsers']
    for env_name,env_value in ENV.items():command+=['-e',env_name+'='+env_value]
    report={}
    try:
        result=subprocess.run(command+[IMAGE,'node','--input-type=module','-e',PROBE],capture_output=True,text=True,timeout=90)
        assert len(result.stdout)<16384;report['launch_probe']=json.loads(result.stdout);report['stderr_bytes']=len(result.stderr)
        assert report['launch_probe']['version']=='replay-launch-only-probe-v1'
        report.update(state='quarantined_and_probed',runtime_probe_exit_code=result.returncode)
    except subprocess.TimeoutExpired:
        report.update(state='probe_timeout_reconciliation_required',runtime_probe_exit_code=124,
            launch_probe={'state':'timeout','error_category':'PROBE_TIMEOUT','navigation_calls':0,'file_selections':0,'submissions':0})
        try:report['probe_cleanup']=stop_own_probe(probe_name,identifier)
        except BaseException:report['probe_cleanup']={'only_owned_probe_stopped':False,'error_code':'PROBE_CLEANUP_NOT_VERIFIED'}
    return report

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--container-id',required=True)
    parser.add_argument('--expected-lock-target',required=True);parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    assert os.name=='posix' and os.geteuid()==0 and re.fullmatch('[a-f0-9]{64}',args.container_id)
    os.umask(0o077);value=json.loads(run(['/usr/bin/docker','inspect',args.container_id]))[0]
    assert value['Image']==IMAGE and value['Config']['User']=='10001:10001' and value['State']['Running']
    assert value['HostConfig']['ReadonlyRootfs'] and value['HostConfig']['PidsLimit']==256 and value['HostConfig']['Memory']==1400*1024**2
    assert value['Config']['Labels']['com.docker.compose.service']=='video-worker'
    environment=dict(x.split('=',1) for x in value['Config']['Env']);assert all(environment.get(k)==v for k,v in ENV.items())
    fd=directory(('data','browser-profiles','bilibili-video'));info=os.fstat(fd)
    assert info.st_uid==10001 and stat.S_IMODE(info.st_mode)==0o700;identity=(info.st_dev,info.st_ino)
    try:
        initial=links(fd);assert initial['SingletonLock']['target']==args.expected_lock_target
        owner=owners(identity);report={'version':'replay-browser-runtime-recovery-v1','state':'plan_only','profile_owner_uid':info.st_uid,
            'links':initial,'owners':owner,'job_or_database_modified':False,'cookies_or_storage_read':False,'files_selected':0,'submissions':0}
        if not args.apply:print(json.dumps(report));return
        import fcntl
        locks=[]
        try:
            for name in ('video','bilibili-video-profile','heavy'):
                folder=directory(('data','locks',name))
                try:lock=os.open('observer.lock',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=folder)
                finally:os.close(folder)
                lock_info=os.fstat(lock)
                assert stat.S_ISREG(lock_info.st_mode) and lock_info.st_nlink==1 and not lock_info.st_mode&0o022
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:os.close(lock);raise RuntimeError('ACTIVE_RESOURCE_OWNER_NO_CHANGES') from None
                locks.append(lock)
            owner=owners(identity);assert not owner['matching_owner_pids'] and not owner['unreadable_pids']
            assert (PROFILE.stat().st_dev,PROFILE.stat().st_ino)==identity and links(fd)==initial
            evidence=Path('/home/lighthouse-ops/lighthouse-evidence')/('replay-browser-runtime-recovery-'+str(uuid.uuid4()));evidence.mkdir(mode=0o700)
            (evidence/'before.json').write_bytes(wire(report))
            backup=quarantine(fd,initial,args.expected_lock_target);report['quarantine_directory']=str(PROFILE/backup)
            report['state']='stale_links_quarantined';(evidence/'quarantine.json').write_bytes(wire(report))
            report['runtime_environment']=json.loads(run(['/usr/bin/docker','exec','--user','10001:10001',args.container_id,'python','-c',PREPARE]))
            report.update(launch_only(value['Config']['Hostname']))
            (evidence/'result.json').write_bytes(wire(report));report['evidence']=str(evidence);print(json.dumps(report))
        finally:
            for lock in reversed(locks):os.close(lock)
    finally:os.close(fd)

if __name__=='__main__':main()
