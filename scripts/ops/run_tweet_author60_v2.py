"""Only build and isolate the committed five-minute native repost-check candidate."""
from pathlib import Path,PurePosixPath
import argparse,hashlib,json,os,subprocess,tarfile,time,uuid
import xml.etree.ElementTree as ET
from collections import Counter
ROOT=Path('/opt/lighthouse-hub');BASE='sha256:b7d57771653934f7c00c442f5c9ae41ab3fa8861c1488c9da777a70631f8af5b'
TAG='lighthouse-hub:tweet-repost118-one-layer-3ff07bac1fdb';LIVE='4fe843f8e7a60a2586d1556d8ebcb6b0eaf953fdcef2269af8b612a8263212fd'
PARENT=Path('/home/lighthouse-ops/lighthouse-evidence/tweet-repost118-8805d04a-0fd3-42b3-b8ec-c2b7dca2facf/test-result.json');PARENT_SHA='c42799c3143f52cfac544035b9517f1d5160828b5f17f66f8e62278471f88bcd'
PROD=['services/hub/tweet_repost_check_v1.py', 'adapters/browser_tweet_repost_check_v1.mjs']
FILES={'services/hub/tweet_repost_check_v1.py': '681a984f138884a4d788fbd3e9c89f94b422b9c50a75836435facd53c9febea6', 'adapters/browser_tweet_repost_check_v1.mjs': 'bdd5c6a9e6af486367eeb9f5c8cb77fd6f2bd0cbf4db2275affd99d908b26d57', 'tests/test_tweet_repost_check_v1.py': 'c3683572c2f24c9baf5b9d37c8c950140953a8e17f4fc4b3d7d7314c5ffa4bb2', 'tests/test_tweet_repost_native_v1.py': '3224f2091d5c6f1f4e3ab6fe266e468e17aad4cf568b390f36c3979a15e6e8b0', 'tests/test_tweet_repost_author_navigation_v2.py': '3f0ce13faf7ba6fbdba3366b9223ecbc46b702cad39c1e358fd844cdbe2331d6', 'tests/test_tweet_repost_reinspection_v2.py': 'ef579d3610768926138c1d3f6e14bd11783de230d32f3ca50f702e5c0ac38d24', 'tests/tweet_repost_check_v1_fixture.mjs': 'a8d20c0434f4873a2596bd328861fa74c65956dd59bbb1cf108ae94ef1ebb337', 'tests/tweet_repost_author_navigation_v2_fixture.mjs': '0b6cea6f0ee51d2589e3a1a0cb37adbf377e8424503119e3ecd969558b4ef796'}
GROUPS={'tests.test_tweet_repost_check_v1': 20, 'tests.test_tweet_repost_native_v1': 12, 'tests.test_tweet_repost_author_navigation_v2': 20, 'tests.test_tweet_repost_reinspection_v2': 8}
SELECT=['tests/test_tweet_repost_check_v1.py', 'tests/test_tweet_repost_native_v1.py', 'tests/test_tweet_repost_author_navigation_v2.py', 'tests/test_tweet_repost_reinspection_v2.py']
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def run(args,**kw):return subprocess.run(args,check=True,capture_output=True,text=True,**kw)
def inspect(cid):return json.loads(run(['docker','inspect',cid]).stdout)[0]
def peers():
 out={}
 for name in ('api','scheduler','video-worker','system-worker'):
  cid=run(['docker','compose','--project-directory',str(ROOT),'-f',str(ROOT/'compose.yaml'),'ps','-q',name]).stdout.strip();v=inspect(cid);assert v['State']['Running'];out[name]={'id':v['Id'],'image':v['Image'],'pid':v['State']['Pid'],'started_at':v['State']['StartedAt']}
 return out
def main():
 p=argparse.ArgumentParser();p.add_argument('--archive',type=Path,required=True);p.add_argument('--sha256',required=True);a=p.parse_args()
 assert os.geteuid()==0 and a.archive.parent==Path('/home/lighthouse-ops') and a.archive.resolve()==a.archive and not a.archive.is_symlink() and sha(a.archive)==a.sha256
 parent_raw=PARENT.read_bytes();assert hashlib.sha256(parent_raw).hexdigest()==PARENT_SHA
 parent=json.loads(parent_raw);assert parent['passed'] is True and parent['image']==BASE and parent['tests']==118 and parent['exit_code']==0 and not any(parent[k] for k in ('failure','error','skipped'))
 parent_xml=Path(parent['junit_file']);assert sha(parent_xml)==parent['junit_sha256'];parent_cases=list(ET.parse(parent_xml).getroot().iter('testcase'))
 assert len(parent_cases)==118 and all(all(c.find(k) is None for k in ('failure','error','skipped')) for c in parent_cases)
 live=inspect(LIVE);assert live['Image']==BASE and live['State']['Running'];peers_before=peers()
 assert run(['docker','image','inspect','-f','{{.Id}}',TAG]).stdout.strip()==BASE
 area=Path('/home/lighthouse-ops/lighthouse-evidence')/('tweet-author60-v2-'+str(uuid.uuid4()));area.mkdir(mode=0o755)
 context=area/'context';context.mkdir(mode=0o755)
 with tarfile.open(a.archive,'r:gz') as tar:
  members=tar.getmembers();assert len(members)==len(FILES)+1 and {v.name for v in members}==set(FILES)|{'manifest.json'}
  m=json.load(tar.extractfile('manifest.json'));assert m['source_commit']=='d53605830426f32968bfcbb50d1acc767653d7e7' and m['base_image']==BASE and m['delta_sha256']==FILES and m['test_groups']==GROUPS and m['version']=='tweet-author60-v2-source-candidate' and m['tests']==60 and m['test_selection']==SELECT and m['parent_proof_sha256']==PARENT_SHA and m['baseline_source_sha256']==parent['source_hashes'] and m['production_delta']=={n:FILES[n] for n in PROD} and m['credentials_included'] is False
  for v in members:
   n=PurePosixPath(v.name);assert v.isfile() and not v.issym() and not v.islnk() and not n.is_absolute() and '..' not in n.parts and 0<v.size<200000
   raw=tar.extractfile(v).read()
   if v.name=='manifest.json':continue
   assert hashlib.sha256(raw).hexdigest()==FILES[v.name]
   file=context/'payload'/v.name;file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(raw);file.chmod(0o644)
 (context/'Dockerfile').write_text('FROM '+TAG+'\nCOPY --chown=0:0 payload/ '+str(ROOT)+'/\nUSER 10001:10001\n')
 target='lighthouse-hub:tweet-author60-v2-one-layer-'+a.sha256[:12]
 built=run(['docker','build','--network=none','--pull=false','-t',target,str(context)],timeout=180);(area/'build.log').write_text(built.stdout+'\n'+built.stderr)
 image=run(['docker','image','inspect','-f','{{.Id}}',target]).stdout.strip();info=json.loads(run(['docker','image','inspect',image]).stdout)[0];base=json.loads(run(['docker','image','inspect',BASE]).stdout)[0]
 assert info['RootFS']['Layers'][:len(base['RootFS']['Layers'])]==base['RootFS']['Layers'] and len(info['RootFS']['Layers'])==len(base['RootFS']['Layers'])+1
 expected={**m['baseline_source_sha256'],**FILES}
 assert {n for n in expected if not n.startswith('tests/') and expected[n]!=parent['source_hashes'].get(n)}==set(PROD)
 for n in ('services/hub/machine_tweets.py','services/hub/cli.py','adapters/http.py','services/hub/budget.py','services/hub/x_cost_ledger.py','scheduler/migrations/017_x_utc_cost_ledger.sql'):assert expected[n]==parent['source_hashes'][n]
 owned=area/'owned';owned.mkdir(mode=0o700);os.chown(owned,10001,10001)
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
 cmd=['docker','create','--pull=never','--init','--name','repost118-'+uuid.uuid4().hex[:12],'--network=none','--read-only','--user=10001:10001',
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
 passed=code==0 and len(cases)==60 and groups==GROUPS and not any(counts.values()) and resource_ok and unchanged and not final['State']['OOMKilled']
 result={'version':'tweet-author60-ubuntu-v2','passed':passed,'tests':len(cases),**counts,'exit_code':code,'image':image,'base_image':BASE,
  'parent_proof_sha256':PARENT_SHA,'single_copy_layer_verified':True,'production_delta_files':PROD,'machine_cli_cost_unchanged':True,'source_commit':m['source_commit'],'archive_sha256':a.sha256,'manifest':m,'test_groups':groups,'source_hashes':expected,'source_before':before,'source_after':after,
  'resource_limits_passed':resource_ok,'sources_unchanged':unchanged,'network':'none','production_data_mounted':False,'credentials_injected':False,
  'oom_killed':final['State']['OOMKilled'],'junit_file':str(xml),'junit_sha256':sha(xml),'original_tweet_cid':LIVE,'production_tweet_unchanged':True,'other_four_before':peers_before,'other_four_after':peers(),'ports_opened':False,'production_profile_mounted':False}
 assert result['other_four_before']==result['other_four_after']
 out=area/'test-result.json';out.write_text(json.dumps(result,sort_keys=True,indent=2));print(json.dumps({'file':str(out),'sha256':sha(out),'image':image,'passed':passed,'tests':len(cases),**counts}),flush=True)
 raise SystemExit(0 if passed else 1)
if __name__=='__main__':main()
