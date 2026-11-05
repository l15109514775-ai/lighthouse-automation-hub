"""Only run the frozen thirteen owned tests, with no production bind/network."""
from pathlib import Path,PurePosixPath
import argparse,hashlib,json,os,re,subprocess,tarfile,time,uuid,xml.etree.ElementTree as ET
ROOT=Path('/opt/lighthouse-hub');IMAGE='sha256:9f19b756ca081bfe033ffa01b0face5c9cb70b3d45de08394567150ac747de6e'
ARCHIVE='2510f3cca7abaa2da971b57dc02b2464599035dbb4be9286c58fa81b1c5225f4'
MANIFEST='c049632aa04b27dc2a6f2a4483527eb530434023f8807671dcce37cfea3133f5'
COMMIT='5d4e73741c90920b74d780764c8726d5f9296c2c'
GROUP='tests.ops.test_plan_published_pilot_master_confirmation_v2'
def sha(raw):return hashlib.sha256(raw).hexdigest()
def run(args,timeout=60):return subprocess.run(args,capture_output=True,text=True,check=True,timeout=timeout)
def production_identity():
 ids=run(['docker','compose','--project-directory',str(ROOT),'-f',str(ROOT/'compose.yaml'),'ps','-q']).stdout.splitlines()
 assert len(ids)==5 and len(set(ids))==5 and all(re.fullmatch('[a-f0-9]{64}',v) for v in ids)
 values=json.loads(run(['docker','inspect',*ids]).stdout);result={}
 for value in values:
  service=value['Config']['Labels']['com.docker.compose.service']
  assert service not in result and value['State']['Running']
  result[service]={'id':value['Id'],'image':value['Image'],'pid':value['State']['Pid'],'started_at':value['State']['StartedAt']}
 assert set(result)=={'api','system-worker','video-worker','tweet-worker','scheduler'}
 return result

BOOT=r'''
from pathlib import Path
import hashlib,json,os,sys
root=Path('/opt/lighthouse-hub');manifest=json.load(open('/run/master-thirteen-manifest.json'))
assert os.getuid()==os.getgid()==10001 and set(os.getgroups())<={10001}
for n in ('HOME','XDG_CONFIG_HOME','XDG_CACHE_HOME'):Path(os.environ[n]).mkdir(parents=True,exist_ok=True)
def snapshot():
 return {'sources':{n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in manifest['files']},
  'resources':{n:{k:int(v) for k,v in (line.split() for line in (Path('/sys/fs/cgroup')/n).read_text().splitlines())} for n in ('pids.events','memory.events')}}
before=snapshot();assert before['sources']=={n:v['sha256'] for n,v in manifest['files'].items()};Path('/owned/before.json').write_text(json.dumps(before))
import pytest
code=pytest.main(['-q','-p','no:cacheprovider',*manifest['test_selections'],'--basetemp=/owned/pytest','--junitxml=/owned/results.xml'])
Path('/owned/after.json').write_text(json.dumps(snapshot()));raise SystemExit(code)
'''
def validate(info,owned,overlay,manifest,seccomp):
 host=info['HostConfig'];env=info['Config'];binds=[v for v in info['Mounts'] if v['Type']=='bind']
 assert info['Image']==IMAGE and env['User']=='10001:10001' and host['Init'] and host['NetworkMode']=='none' and host['ReadonlyRootfs']
 assert host['CapDrop']==['ALL'] and host['Memory']==1400*1024**2 and host['NanoCpus']==1_000_000_000 and host['PidsLimit']==256 and host['ShmSize']==256*1024**2
 assert len(host['SecurityOpt'])==2 and 'no-new-privileges:true' in host['SecurityOpt']
 value=next(v.removeprefix('seccomp=') for v in host['SecurityOpt'] if v.startswith('seccomp='))
 assert value==str(ROOT/'config/browser-seccomp.json') or json.loads(value)==seccomp
 expected={'/owned':(str(owned),True),'/run/master-thirteen-manifest.json':(str(overlay/'manifest.json'),False),
  **{str(ROOT/n):(str(overlay/n),False) for n in manifest['files']}}
 assert len(binds)==len(expected) and {v['Destination']:(v['Source'],v['RW']) for v in binds}==expected
 assert all(v['Type'] in ('bind','tmpfs') for v in info['Mounts']) and host['Tmpfs']=={'/tmp':'rw,size=256m,mode=1777'}
 assert not any(re.search(r'(^|_)(KEY|TOKEN|PASSWORD|SECRET|COOKIE)(_|$)',v.split('=',1)[0]) for v in env['Env'])

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--archive',type=Path,required=True);p.add_argument('--sha256',required=True);a=p.parse_args()
 assert os.geteuid()==0 and a.archive.parent==Path('/home/lighthouse-ops') and a.archive.resolve()==a.archive and not a.archive.is_symlink() and sha(a.archive.read_bytes())==a.sha256==ARCHIVE
 assert json.loads(run(['docker','image','inspect',IMAGE]).stdout)[0]['Id']==IMAGE
 production_before=production_identity()
 base=Path('/var/lib/lighthouse-backup-ops/pilot-public-master-confirmation13-v2-tests');base.mkdir(mode=0o700,parents=True,exist_ok=True)
 assert all(v.stat().st_uid==0 and not v.stat().st_mode&0o022 and not v.is_symlink() for v in (base,*base.parents))
 area=base/str(uuid.uuid4());area.mkdir(mode=0o700);overlay=area/'overlay';overlay.mkdir(mode=0o755);overlay.chmod(0o755)
 with tarfile.open(a.archive,'r:gz') as tar:
  members=tar.getmembers();assert len(members)==115 and len({v.name for v in members})==115
  for item in members:
   name=PurePosixPath(item.name);assert item.isfile() and not item.issym() and not item.islnk() and not name.is_absolute() and '..' not in name.parts and 0<item.size<500000
   path=overlay/item.name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(tar.extractfile(item).read());path.chmod(0o644)
 manifest_raw=(overlay/'manifest.json').read_bytes();assert sha(manifest_raw)==MANIFEST;manifest=json.loads(manifest_raw)
 assert manifest['version']=='published-pilot-master-confirmation-thirteen-candidate-v2' and manifest['source_commit']==COMMIT and manifest['base_image']==IMAGE
 assert len(manifest['files'])==114 and manifest['tests']==13 and manifest['groups']=={GROUP:13}
 assert manifest['test_selections']==['tests/ops/test_plan_published_pilot_master_confirmation_v2.py'] and len(manifest['current45_source_sha256'])==45 and len(manifest['qualified43_release_source_sha256'])==50
 assert {v.relative_to(overlay).as_posix() for v in overlay.rglob('*') if v.is_file()}==set(manifest['files'])|{'manifest.json'}
 assert all(sha((overlay/n).read_bytes())==v['sha256'] and (overlay/n).stat().st_size==v['bytes'] for n,v in manifest['files'].items())
 owned=area/'owned';owned.mkdir(mode=0o700);os.chown(owned,10001,10001);os.chmod(owned,0o700)
 seccomp=json.loads((ROOT/'config/browser-seccomp.json').read_bytes())
 assert sha((ROOT/'config/browser-seccomp.json').read_bytes())=='6fbfe3f512f65445b8bbf468034a3bc0fee95dd1734916e0e3dcb1a75b101e3a'
 name='pilot-master-thirteen-'+uuid.uuid4().hex[:12]
 args=['docker','create','--name',name,'--label','org.lighthouse.master-thirteen='+name,'--pull=never','--init','--network=none','--read-only','--user=10001:10001',
  '--cap-drop=ALL','--security-opt=no-new-privileges:true','--security-opt=seccomp='+str(ROOT/'config/browser-seccomp.json'),'--memory=1400m','--cpus=1','--pids-limit=256','--shm-size=256m',
  '--tmpfs=/tmp:rw,size=256m,mode=1777','--env=HOME=/tmp/home','--env=XDG_CONFIG_HOME=/tmp/config','--env=XDG_CACHE_HOME=/tmp/cache','--env=PYTHONDONTWRITEBYTECODE=1',
  '--mount=type=bind,source='+str(owned)+',target=/owned','--mount=type=bind,source='+str(overlay/'manifest.json')+',target=/run/master-thirteen-manifest.json,readonly']
 for n in manifest['files']:args+=['--mount=type=bind,source='+str(overlay/n)+',target='+str(ROOT/n)+',readonly']
 cid=run([*args,'--entrypoint=python',IMAGE,'-c',BOOT]).stdout.strip();initial=json.loads(run(['docker','inspect',cid]).stdout)[0];validate(initial,owned,overlay,manifest,seccomp)
 started=time.time();run(['docker','start',cid]);timed_out=False
 try:code=int(run(['docker','wait',cid],timeout=180).stdout.strip())
 except subprocess.TimeoutExpired:
  timed_out=True;current=json.loads(run(['docker','inspect',cid]).stdout)[0];assert current['Config']['Labels']['org.lighthouse.master-thirteen']==name and current['Image']==IMAGE
  run(['docker','stop','--time','10',cid]);code=124
 logs=run(['docker','logs',cid]);(area/'tests.log').write_text(logs.stdout+'\n'+logs.stderr)
 final=json.loads(run(['docker','inspect',cid]).stdout)[0];validate(final,owned,overlay,manifest,seccomp)
 xml=(owned/'results.xml').read_bytes();cases=list(ET.fromstring(xml).iter('testcase'));groups={}
 for case in cases:groups[case.get('classname')]=groups.get(case.get('classname'),0)+1
 counts={k:sum(c.find(k) is not None for c in cases) for k in ('failure','error','skipped')}
 before=json.loads((owned/'before.json').read_bytes());after=json.loads((owned/'after.json').read_bytes());expected={n:v['sha256'] for n,v in manifest['files'].items()}
 pressure=all(v['resources']['pids.events'].get('max',-1)==v['resources']['memory.events'].get('max',-1)==v['resources']['memory.events'].get('oom',-1)==v['resources']['memory.events'].get('oom_kill',-1)==0 for v in (before,after))
 unchanged=before['sources']==after['sources']==expected
 production_after=production_identity();production_unchanged=production_before==production_after
 passed=code==0 and len(cases)==13 and groups=={GROUP:13} and not any(counts.values()) and pressure and unchanged and not timed_out and not final['State']['OOMKilled'] and production_unchanged
 (area/'accepted-results.xml').write_bytes(xml)
 proof={'version':'published-pilot-master-confirmation-thirteen-ubuntu-v2','source_commit':COMMIT,'archive_sha256':ARCHIVE,'manifest_sha256':MANIFEST,'manifest':manifest,
  'image':IMAGE,'container_id':cid,'tests':len(cases),'test_groups':groups,**counts,'exit_code':code,'passed':passed,'timed_out':timed_out,'oom_killed':final['State']['OOMKilled'],
  'source_before':before,'source_after':after,'sources_unchanged':unchanged,'no_pid_or_oom_denial':pressure,'network':'none','application_data_mounted':False,
  'production_before':production_before,'production_after':production_after,'production_identities_unchanged':production_unchanged,
  'production_profile_mounted':False,'credentials_injected':False,'operator_main_called':False,'rootfs_read_only':True,'app_uid':10001,'runtime_matches_allowlist':True,
  'junit_file':str(area/'accepted-results.xml'),'junit_sha256':sha(xml),'seconds':time.time()-started,'scope':'independent_public_master_cleanup_owned_plan_apply_only_not_8_or_43_rerun'}
 file=area/'test-result.json';file.write_text(json.dumps(proof,ensure_ascii=True,sort_keys=True,indent=2))
 print(json.dumps({'file':str(file),'sha256':sha(file.read_bytes()),'passed':passed,'tests':len(cases),**counts}),flush=True);raise SystemExit(0 if passed else 1)
if __name__=='__main__':main()
