"""Exact 276-qualified video parent plus one planner source, 54 offline tests."""
from pathlib import Path,PurePosixPath
import argparse,hashlib,json,os,subprocess,tarfile,time,uuid
import xml.etree.ElementTree as ET
from collections import Counter
ROOT=Path('/opt/lighthouse-hub');BASE='sha256:536447e514b8656f010e31c869327df5a03035c502893f22c8e8023bd8710d95'
TAG='lighthouse-hub:candidate-7cce4b26f1f32ef5-gui';LIVE='lighthouse-hub-video-worker-1'
COMMIT='8a0e9886663d9a9f4dc6a4c9f432f818dfcde256'
ARCHIVE='946528e10ce71515bd5f108f7508ed482d798069a52e50e4877d95574e4bf1d3'
MANIFEST='0594d0dc2edfac385b1d2c9c7d46c5eee48ef0b8278541eb933c5dabde3b7a3d'
FILES={'services/hub/youtube_natural_plan_v3.py':'e6763f9a8ec4a4a2fe95ccd061a36d4701499a9f32cc0797cace4b8ce2467ee9',
 'tests/test_youtube_real_inventory_v5.py':'eb9bcf0566a9a6bcab67fc7a043a07d0e3f2df23adc44905be22709cb20e4df7',
 'tests/fixtures/youtube_whole_observations_v5/6wHqfDmNF0U.json':'a44bf7a3f805b06ea6ded1719d756be87bb63a60c7197fca66528a2ad356a790',
 'tests/fixtures/youtube_whole_observations_v5/YsSXSIYtCoE.json':'a6a18e873de09d8be1c6c803ae531ebb293b3435e66ce4468ba815398e47830b'}
GROUPS={'tests.test_youtube_real_inventory_v5':11,'tests.test_youtube_natural_plan_v3':27,'tests.test_replay_multipart_v4':16}
SELECT=['tests/'+k.split('.')[1]+'.py' for k in GROUPS]

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def run(args,**kw):return subprocess.run(args,check=True,capture_output=True,text=True,**kw)
def inspect(cid):return json.loads(run(['docker','inspect',cid]).stdout)[0]
def main():
 p=argparse.ArgumentParser();p.add_argument('--archive',type=Path,required=True);p.add_argument('--sha256',required=True);a=p.parse_args()
 assert os.geteuid()==0 and a.archive.parent==Path('/home/lighthouse-ops') and a.archive.resolve()==a.archive and not a.archive.is_symlink() and a.sha256==ARCHIVE and sha(a.archive)==ARCHIVE
 live=inspect(LIVE);assert live['Image']==BASE and live['State']['Running'] and live['Config']['Cmd']==['python','-m','services.hub.replay_parts_worker_runtime_v5','--publish']
 assert run(['docker','image','inspect','-f','{{.Id}}',TAG]).stdout.strip()==BASE
 area=Path('/home/lighthouse-ops/lighthouse-evidence')/('youtube-real-inventory54-'+str(uuid.uuid4()));area.mkdir(mode=0o755)
 context=area/'context';context.mkdir(mode=0o755)
 with tarfile.open(a.archive,'r:gz') as tar:
  members=tar.getmembers();assert len(members)==5 and {v.name for v in members}==set(FILES)|{'manifest.json'}
  mraw=tar.extractfile('manifest.json').read();assert hashlib.sha256(mraw).hexdigest()==MANIFEST
  m=json.loads(mraw);assert m['version']=='youtube-real-inventory54-delta-v1' and m['source_commit']==COMMIT and m['base_image']==BASE and m['delta_sha256']==FILES and m['test_groups']==GROUPS
  old=m['base_source_manifest'];current=m['source_manifest'];wire=lambda v:json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
  assert hashlib.sha256(wire(old)).hexdigest()=='46998201ff72f5e55b5374ebff551cdfaf38cfc7db64b1e46e9f27ccf639fe87'
  assert old['expected_test_count']==276 and len(old['files'])==848 and current['expected_test_count']==54 and len(current['files'])==851
  assert {n for n in old['files'] if old['files'][n]!=current['files'][n]}=={'services/hub/youtube_natural_plan_v3.py'}
  assert set(current['files'])-set(old['files'])==set(FILES)-{'services/hub/youtube_natural_plan_v3.py'}
  assert all(current['files'][n]['sha256']==v for n,v in FILES.items())
  assert current['test_selections']==SELECT and current['source_commit']==COMMIT
  for v in members:
   n=PurePosixPath(v.name);assert v.isfile() and not v.issym() and not v.islnk() and not n.is_absolute() and '..' not in n.parts and 0<v.size<500000
   raw=tar.extractfile(v).read()
   if v.name=='manifest.json':continue
   assert hashlib.sha256(raw).hexdigest()==FILES[v.name]
   file=context/v.name;file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(raw);file.chmod(0o644)
 (context/'candidate-manifest.json').write_bytes((json.dumps(current,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode())
 (context/'Dockerfile').write_text('FROM '+TAG+'\n'+''.join('COPY --chown=0:0 '+n+' '+str(ROOT/n)+'\n' for n in [*FILES,'candidate-manifest.json'])+'USER 10001:10001\n')
 target='lighthouse-hub:youtube-real-inventory54-'+a.sha256[:12]
 built=run(['docker','build','--network=none','--pull=false','-t',target,str(context)],timeout=180);(area/'build.log').write_text(built.stdout+'\n'+built.stderr)
 image=run(['docker','image','inspect','-f','{{.Id}}',target]).stdout.strip();info=json.loads(run(['docker','image','inspect',image]).stdout)[0];base=json.loads(run(['docker','image','inspect',BASE]).stdout)[0]
 assert info['RootFS']['Layers'][:len(base['RootFS']['Layers'])]==base['RootFS']['Layers']
 expected={n:v['sha256'] for n,v in current['files'].items()};owned=area/'owned';owned.mkdir(mode=0o700);os.chown(owned,10001,10001)
 boot="""import os,sys,json,hashlib
from pathlib import Path
root=Path('/opt/lighthouse-hub');expected=json.loads(sys.argv[1])
[Path(os.environ[k]).mkdir(parents=True,exist_ok=True) for k in ('HOME','XDG_CONFIG_HOME','XDG_CACHE_HOME')]
def sources():return {n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in expected}
def counters():return {n:{k:int(v) for k,v in (line.split() for line in (Path('/sys/fs/cgroup')/n).read_text().splitlines())} for n in ('pids.events','memory.events')}
before=sources();assert before==expected
Path('/owned/before.json').write_text(json.dumps({'sources':before,'resources':counters()}))
import pytest
status=pytest.main(['-q','-p','no:cacheprovider',*sys.argv[2:],'--basetemp=/owned/pytest','--junitxml=/owned/results.xml'])
Path('/owned/after.json').write_text(json.dumps({'sources':sources(),'resources':counters()}));raise SystemExit(status)
"""
 cmd=['docker','create','--pull=never','--init','--name','inventory54-'+uuid.uuid4().hex[:12],'--network=none','--read-only','--user=10001:10001',
  '--cap-drop=ALL','--security-opt=no-new-privileges:true','--security-opt=seccomp='+str(ROOT/'config/browser-seccomp.json'),
  '--memory=1400m','--cpus=1','--pids-limit=256','--shm-size=256m','--tmpfs=/tmp:rw,size=256m,mode=1777',
  '--env=HOME=/tmp/home','--env=XDG_CONFIG_HOME=/tmp/config','--env=XDG_CACHE_HOME=/tmp/cache','--mount=type=bind,source='+str(owned)+',target=/owned',
  '--entrypoint=python',image,'-c',boot,json.dumps(expected),*SELECT]
 cid=run(cmd).stdout.strip();test=inspect(cid);hc=test['HostConfig']
 assert test['Image']==image and test['Config']['User']=='10001:10001' and hc['NetworkMode']=='none' and hc['ReadonlyRootfs'] and hc['Init'] and hc['CapDrop']==['ALL']
 assert hc['Memory']==1400*1024**2 and hc['NanoCpus']==1000000000 and hc['PidsLimit']==256 and hc['ShmSize']==256*1024**2
 assert len(test['Mounts'])==1 and test['Mounts'][0]['Source']==str(owned) and test['Mounts'][0]['Destination']=='/owned' and test['Mounts'][0]['RW']
 assert not any(v.split('=',1)[0] in ('X_BEARER_TOKEN','MODEL_API_KEY','OPENAI_API_KEY','YOUTUBE_API_KEY','EVENT_ENCRYPTION_KEY') for v in test['Config']['Env'])
 (area/'container.json').write_text(json.dumps(test,sort_keys=True,indent=2));run(['docker','start',cid]);print(json.dumps({'state':'started','evidence':str(area),'image':image}),flush=True)
 code=int(run(['docker','wait',cid],timeout=600).stdout.strip());logs=run(['docker','logs',cid]);(area/'tests.log').write_text(logs.stdout+'\n'+logs.stderr)
 xml=owned/'results.xml';cases=list(ET.parse(xml).getroot().iter('testcase'));groups=dict(Counter(v.attrib['classname'] for v in cases));counts={k:sum(v.find(k) is not None for v in cases) for k in ('failure','error','skipped')}
 before=json.loads((owned/'before.json').read_bytes());after=json.loads((owned/'after.json').read_bytes());final=inspect(cid)
 resource_ok=all(v['resources']['pids.events']['max']==v['resources']['memory.events']['oom']==v['resources']['memory.events']['oom_kill']==0 for v in (before,after))
 unchanged=before['sources']==after['sources']==expected;production=inspect(LIVE)
 assert production['Id']==live['Id'] and production['Image']==live['Image'] and production['State']['Pid']==live['State']['Pid'] and production['State']['StartedAt']==live['State']['StartedAt']
 passed=code==0 and len(cases)==54 and groups==GROUPS and not any(counts.values()) and resource_ok and unchanged and not final['State']['OOMKilled']
 result={'version':'youtube-real-inventory54-ubuntu-v1','passed':passed,'tests':len(cases),**counts,'exit_code':code,'image':image,'base_image':BASE,
  'source_commit':m['source_commit'],'archive_sha256':a.sha256,'manifest':m,'test_groups':groups,'source_hashes':expected,'source_before':before,'source_after':after,
  'resource_limits_passed':resource_ok,'sources_unchanged':unchanged,'network':'none','production_data_mounted':False,'credentials_injected':False,
  'oom_killed':final['State']['OOMKilled'],'junit_file':str(xml),'junit_sha256':sha(xml),'original_video_cid':live['Id'],'production_video_unchanged':True,'source_manifest':current,'parent_276_source_manifest':old,'qualified_parent_image':BASE,'production_runtime_changed_files':['services/hub/youtube_natural_plan_v3.py']}
 out=area/'test-result.json';out.write_text(json.dumps(result,sort_keys=True,indent=2));print(json.dumps({'file':str(out),'sha256':sha(out),'image':image,'passed':passed,'tests':len(cases),**counts}),flush=True)
 raise SystemExit(0 if passed else 1)
if __name__=='__main__':main()
