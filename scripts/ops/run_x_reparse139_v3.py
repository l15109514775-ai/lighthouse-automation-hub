"""Only build and isolate the committed durable rejection and cached recovery delta."""
from pathlib import Path,PurePosixPath
import argparse,hashlib,json,os,subprocess,tarfile,time,uuid
import xml.etree.ElementTree as ET
from collections import Counter
ROOT=Path('/opt/lighthouse-hub');BASE='sha256:c1ed327379de306bdca7231a2fbd4c582dbd00da2792aa7299fd1ad8e5dddb7a'
TAG='lighthouse-hub:x-native-context63-22996572b3dc';LIVE='525c1196d8dec2dcec9b458fd7f0a0221c1d26a7cdfd753b369b154f25fc6046'
FILES={'adapters/x.py': 'e6fbf3e0811fef6107ebd6322413ebb87c8e256227548abd31d58cf3bcb83769', 'services/hub/x_api_discovery.py': 'e9384b95149fd5d92b9a629d1154b9a090fee06a83a5cbb9719335cde5279948', 'services/hub/x_official_reparse_v1.py': '2495400d8ea77b756abf1ce01ea5df611990993ed9f99ef503cdc613919b8682', 'services/hub/cli.py': '977dcede30557f60c193d91c6402a1cc1682826fe8777ae1320af5be8c5627eb', 'services/hub/x_official_capture_queue_v1.py': 'e94c42b7569409df10c9be8dd51724cc95d861b6cb2f2b5633be5d1603c0f903', 'tests/test_x_official_reparse_v1.py': '9e450cc23f137989c370582ebfb61e2d337f46d3548afdc6d6679daecb802345', 'tests/test_x_official_capture_queue_v1.py': '97d1649319e229f26da33d632716dbd794eb224669ac86ac43fc6bc5f1886aaa', 'tests/test_x_api_intake_isolation.py': '00589c51b3d1965976063161581c9ac82672adf64461f4169f735c6c6ae94c32', 'tests/test_x_api_routing.py': 'c174ff6248ba20eb8ec2e9799778e0cc30485e30d254fe67bd2d7e06963ae462', 'tests/test_x_official_relations_v2.py': 'c9dc08aa41e7c3a523a241f9d25bf57ee1be4151dc972e2df9ffcef1c1cdeb70', 'tests/test_x_native_context_cards_v1.py': '932a4bbfe998f7e3c5cf0860755aadb8930a999b33972ed643e3e9d0d8bae669', 'tests/test_x_youtube_live_cards.py': '1e28eeb70aa28abfb23f4ab04bc60d5505fe0bdc2aa0d730d9613cf36b0b4453', 'tests/test_machine_api_relations.py': '028a9f9656379b6f57c049ebfba1279a3a80c739c87855d7682483de36a5981d'}
GROUPS={'tests.test_x_official_reparse_v1': 14, 'tests.test_x_official_capture_queue_v1': 20, 'tests.test_x_api_intake_isolation': 18, 'tests.test_x_api_routing': 24, 'tests.test_x_official_relations_v2': 27, 'tests.test_x_native_context_cards_v1': 13, 'tests.test_x_youtube_live_cards': 16, 'tests.test_machine_api_relations': 7}
SELECT=['tests/'+k.split('.')[1]+'.py' for k in GROUPS]
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def run(args,**kw):return subprocess.run(args,check=True,capture_output=True,text=True,**kw)
def inspect(cid):return json.loads(run(['docker','inspect',cid]).stdout)[0]
def main():
 p=argparse.ArgumentParser();p.add_argument('--archive',type=Path,required=True);p.add_argument('--sha256',required=True);a=p.parse_args()
 assert os.geteuid()==0 and a.archive.parent==Path('/home/lighthouse-ops') and a.archive.resolve()==a.archive and not a.archive.is_symlink() and sha(a.archive)==a.sha256
 live=inspect(LIVE);assert live['Image']==BASE and live['State']['Running']
 assert run(['docker','image','inspect','-f','{{.Id}}',TAG]).stdout.strip()==BASE
 area=Path('/home/lighthouse-ops/lighthouse-evidence')/('x-reparse139-'+str(uuid.uuid4()));area.mkdir(mode=0o755)
 context=area/'context';context.mkdir(mode=0o755)
 with tarfile.open(a.archive,'r:gz') as tar:
  members=tar.getmembers();assert len(members)==14 and {v.name for v in members}==set(FILES)|{'manifest.json'}
  m=json.load(tar.extractfile('manifest.json'));assert m['source_commit']=='c1a9ffe9e69abe221f38f71060d71445b6a0a8af' and m['base_image']==BASE and m['delta_sha256']==FILES and m['test_groups']==GROUPS
  for v in members:
   n=PurePosixPath(v.name);assert v.isfile() and not v.issym() and not v.islnk() and not n.is_absolute() and '..' not in n.parts and 0<v.size<200000
   raw=tar.extractfile(v).read()
   if v.name=='manifest.json':continue
   assert hashlib.sha256(raw).hexdigest()==FILES[v.name]
   file=context/'payload'/v.name;file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(raw);file.chmod(0o644)
 (context/'Dockerfile').write_text('FROM '+TAG+'\nCOPY --chown=0:0 payload/ '+str(ROOT)+'/\nUSER 10001:10001\n')
 target='lighthouse-hub:x-reparse139-one-layer-'+a.sha256[:12]
 built=run(['docker','build','--network=none','--pull=false','-t',target,str(context)],timeout=180);(area/'build.log').write_text(built.stdout+'\n'+built.stderr)
 image=run(['docker','image','inspect','-f','{{.Id}}',target]).stdout.strip();info=json.loads(run(['docker','image','inspect',image]).stdout)[0];base=json.loads(run(['docker','image','inspect',BASE]).stdout)[0]
 assert info['RootFS']['Layers'][:len(base['RootFS']['Layers'])]==base['RootFS']['Layers']
 expected={**m['baseline_source_sha256'],**FILES};owned=area/'owned';owned.mkdir(mode=0o700);os.chown(owned,10001,10001)
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
 cmd=['docker','create','--pull=never','--init','--name','reparse139-'+uuid.uuid4().hex[:12],'--network=none','--read-only','--user=10001:10001',
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
 assert production['Image']==live['Image'] and production['State']['Pid']==live['State']['Pid'] and production['State']['StartedAt']==live['State']['StartedAt']
 passed=code==0 and len(cases)==139 and groups==GROUPS and not any(counts.values()) and resource_ok and unchanged and not final['State']['OOMKilled']
 result={'version':'x-reparse139-ubuntu-v1','passed':passed,'tests':len(cases),**counts,'exit_code':code,'image':image,'base_image':BASE,
  'source_commit':m['source_commit'],'archive_sha256':a.sha256,'manifest':m,'test_groups':groups,'source_hashes':expected,'source_before':before,'source_after':after,
  'resource_limits_passed':resource_ok,'sources_unchanged':unchanged,'network':'none','production_data_mounted':False,'credentials_injected':False,
  'oom_killed':final['State']['OOMKilled'],'junit_file':str(xml),'junit_sha256':sha(xml),'original_tweet_cid':LIVE,'production_tweet_unchanged':True}
 out=area/'test-result.json';out.write_text(json.dumps(result,sort_keys=True,indent=2));print(json.dumps({'file':str(out),'sha256':sha(out),'image':image,'passed':passed,'tests':len(cases),**counts}),flush=True)
 raise SystemExit(0 if passed else 1)
if __name__=='__main__':main()
