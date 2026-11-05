"""Only replace the tweet image with the actual qualified rejection/recovery fix."""
from copy import deepcopy
from pathlib import Path
import argparse,hashlib,json,os,re,sys,time,types,uuid,xml.etree.ElementTree as ET
from collections import Counter
ROOT=Path('/opt/lighthouse-hub');STATE=Path('/var/lib/lighthouse-x-reparse139-v1')
BASE='sha256:c1ed327379de306bdca7231a2fbd4c582dbd00da2792aa7299fd1ad8e5dddb7a'
IMAGE='sha256:2ad45319d7fa7fd9e7e4d228d375cdd8917cc0eb23f41573a425b27fbd831d29'
CURRENT='525c1196d8dec2dcec9b458fd7f0a0221c1d26a7cdfd753b369b154f25fc6046'
PROOF_SHA='77e6c2852e0aa86c593919f9ddb3c4a011626696130ac600865ecfff716b2db3'
ARCHIVE_SHA='9809c0dccfdce42ed401a3aa7ae7deeec52dd6ca9922214b0ae45c7363745acc'
HELPER_SHA='9e9c868ec104e9582925f0cae8b8e5cb936d914665257647ca4d685d46687058'
GROUPS={'tests.test_machine_api_relations': 7, 'tests.test_x_api_intake_isolation': 18, 'tests.test_x_api_routing': 24, 'tests.test_x_native_context_cards_v1': 13, 'tests.test_x_official_capture_queue_v1': 20, 'tests.test_x_official_relations_v2': 27, 'tests.test_x_official_reparse_v1': 14, 'tests.test_x_youtube_live_cards': 16}
NEW={'adapters/x.py': 'e6fbf3e0811fef6107ebd6322413ebb87c8e256227548abd31d58cf3bcb83769', 'services/hub/x_api_discovery.py': 'e9384b95149fd5d92b9a629d1154b9a090fee06a83a5cbb9719335cde5279948', 'services/hub/x_official_reparse_v1.py': '2495400d8ea77b756abf1ce01ea5df611990993ed9f99ef503cdc613919b8682', 'services/hub/cli.py': '977dcede30557f60c193d91c6402a1cc1682826fe8777ae1320af5be8c5627eb', 'services/hub/x_official_capture_queue_v1.py': 'e94c42b7569409df10c9be8dd51724cc95d861b6cb2f2b5633be5d1603c0f903'}
COST=('adapters/http.py','services/hub/budget.py','services/hub/x_cost_ledger.py','scheduler/migrations/017_x_utc_cost_ledger.sql')
OTHER=('api','scheduler','video-worker','system-worker')
def sha(raw):return hashlib.sha256(raw).hexdigest()
def wire(v):return json.dumps(v,sort_keys=True,separators=(',',':')).encode()
def need(v,code):
 if not v:raise ValueError(code)
def load(file):
 file=Path(file);need(file.resolve()==file and not any(p.is_symlink() for p in (file,*file.parents)) and file.stat().st_size<200000,'SAFE_FROZEN_HELPER_REQUIRED')
 raw=file.read_bytes();need(sha(raw)==HELPER_SHA,'HELPER_SHA_CHANGED');m=types.ModuleType('card58_frozen_helper');m.__file__=str(file);exec(compile(raw,str(file),'exec'),m.__dict__);return m
def hashes(d,cid,names):
 code="import json,hashlib;from pathlib import Path;r=Path('/opt/lighthouse-hub');names="+repr(sorted(names))+";print(json.dumps({n:hashlib.sha256((r/n).read_bytes()).hexdigest() for n in names}))"
 return json.loads(d.run(['/usr/bin/docker','exec','--user','10001:10001',cid,'python','-c',code]))
def identities(d):
 out={}
 for name in OTHER:
  value=d.inspect(d.cid(name));need(value['State']['Running'],'OTHER_CORE_STOPPED');out[name]={'id':value['Id'],'pid':value['State']['Pid'],'started_at':value['State']['StartedAt']}
 return out
def candidate(before):
 after=deepcopy(before);after['services']['tweet-worker']['image']=IMAGE
 a=deepcopy(before);b=deepcopy(after);a['services']['tweet-worker'].pop('image');b['services']['tweet-worker'].pop('image');need(a==b,'ONLY_IMAGE_MAY_CHANGE');return after
def qualification(d,file):
 raw=d.read(Path(file),2_000_000);need(sha(raw)==PROOF_SHA,'EXACT_UBUNTU_PROOF_REQUIRED');q=json.loads(raw)
 need(q['version']=='x-reparse139-ubuntu-v1' and q['image']==IMAGE and q['base_image']==BASE and q['archive_sha256']==ARCHIVE_SHA,'WRONG_QUALIFICATION')
 need(q['passed'] is True and q['tests']==139 and q['exit_code']==q['failure']==q['error']==q['skipped']==0,'QUALIFICATION_FAILED')
 need(q['resource_limits_passed'] is q['sources_unchanged'] is q['production_tweet_unchanged'] is True and q['original_tweet_cid']==CURRENT,'SOURCE_OR_RUNTIME_PROOF_FAILED')
 need(q['network']=='none' and all(q[k] is False for k in ('production_data_mounted','credentials_injected','oom_killed')),'ISOLATION_FAILED')
 xml=d.read(Path(q['junit_file']),2_000_000);cases=list(ET.fromstring(xml).iter('testcase'))
 need(sha(xml)==q['junit_sha256'] and len(cases)==139 and dict(Counter(c.attrib['classname'] for c in cases))==GROUPS==q['test_groups'],'JUNIT_CHANGED')
 need(all(c.find(k) is None for c in cases for k in ('failure','error','skipped')),'JUNIT_NOT_PASSED')
 for name in ('source_before','source_after'):
  value=q[name];need(value['sources']==q['source_hashes'],'SOURCE_MAP_CHANGED');r=value['resources'];need(r['pids.events']['max']==r['memory.events']['oom']==r['memory.events']['oom_kill']==0,'CGROUP_DENIAL')
 need(all(q['source_hashes'][n]==v for n,v in NEW.items()),'WRONG_REPARSE_SOURCE_DELTA')
 need(all(q['source_hashes'][n]==q['manifest']['baseline_source_sha256'][n] for n in COST),'COST_RECALIBRATION_WOULD_BE_REQUIRED')
 image=json.loads(d.run(['/usr/bin/docker','image','inspect',IMAGE]))[0];base=json.loads(d.run(['/usr/bin/docker','image','inspect',BASE]))[0]
 need(image['RootFS']['Layers'][:len(base['RootFS']['Layers'])]==base['RootFS']['Layers'],'DERIVED_IMAGE_CHANGED');return q
def preserved(old,new):
 need(new['Image']==IMAGE and new['State']['Running'],'NEW_IMAGE_NOT_RUNNING')
 need(sorted(old['Config']['Env'])==sorted(new['Config']['Env']),'ENV_CHANGED')
 for k in ('User','Cmd','Entrypoint','WorkingDir'):need(old['Config'][k]==new['Config'][k],'PROCESS_OPTIONS_CHANGED')
 need(old['Mounts']==new['Mounts'],'ANY_PERSISTENT_OR_CONFIG_MOUNT_CHANGED')
 for k in old['HostConfig']:
  if k not in ('Binds','Mounts'):need(old['HostConfig'][k]==new['HostConfig'][k],'HOST_CONFIG_CHANGED')
def main():
 need(os.name=='posix' and os.geteuid()==0 and not sys.flags.optimize,'ROOT_HOST_REQUIRED');os.umask(0o077)
 p=argparse.ArgumentParser();p.add_argument('--base-helper',required=True);p.add_argument('--qualification',required=True);p.add_argument('--activate',action='store_true');a=p.parse_args();d=load(a.base_helper)
 q=qualification(d,a.qualification);old=d.inspect(CURRENT);need(d.cid('tweet-worker')==CURRENT and old['Image']==BASE and old['State']['Running'],'CURRENT_TWEET_CHANGED')
 expected=q['manifest']['baseline_source_sha256'];need(hashes(d,CURRENT,expected)==expected,'BASE_SOURCE_OR_FIXTURE_CHANGED')
 others=identities(d);raw=d.read(ROOT/'compose.yaml',2_000_000);d.protected(ROOT/'compose.yaml');before=d.parsed();after=candidate(before)
 env=dict(v.split('=',1) for v in old['Config']['Env']);cfg=d.safe(Path(env['HUB_CONFIG']));mounted=[v for v in old['Mounts'] if v['Destination']==str(cfg)]
 need(len(mounted)==1 and mounted[0]['Type']=='bind' and mounted[0]['RW'] is False,'COST_CONFIG_BIND_REQUIRED');actual_cfg=Path(mounted[0]['Source']);d.protected(actual_cfg);cfgraw=d.read(actual_cfg,2_000_000)
 summary={'version':'x-reparse139-cutover-v1','state':'verified_plan_only','image':IMAGE,'base_image':BASE,'qualification_sha256':PROOF_SHA,
  'old_cid':CURRENT,'other_four':others,'cost_sources_unchanged':True,'cost_recalibrated':False,'config_sha256':sha(cfgraw),'paid_calls_by_operator':0,'database_written_by_operator':False}
 if not a.activate:print(json.dumps(summary));return
 d.directory(STATE,0o700);area=STATE/str(uuid.uuid4());d.directory(area,0o700)
 with d.deployment_lock():
  d.atomic(area/'compose-before.yaml',raw);d.atomic(area/'report.json',wire(summary))
  with d.tweet_lock():
   d.dbstate(idle=True);need(d.cid('tweet-worker')==CURRENT and identities(d)==others and d.read(ROOT/'compose.yaml',2_000_000)==raw,'CONCURRENT_CHANGE')
   d.compose('stop','--timeout','45','tweet-worker',timeout=60);need(not d.inspect(CURRENT)['State']['Running'],'TWEET_STOP_FAILED');d.dbstate(idle=True)
   d.atomic(ROOT/'compose.yaml',wire(after),original=(ROOT/'compose.yaml').stat());need(d.parsed()==after,'COMPOSE_READBACK_FAILED')
   d.atomic(area/'report.json',wire({**summary,'state':'image_installed_start_pending'}))
  start=time.time();d.up();new=d.inspect(d.cid('tweet-worker'));preserved(old,new)
  need(hashes(d,new['Id'],q['source_hashes'])==q['source_hashes'],'NEW_IMAGE_SOURCES_CHANGED');need(d.read(actual_cfg,2_000_000)==cfgraw and identities(d)==others,'OTHER_CORE_OR_CONFIG_CHANGED')
  ready=d.wait_ready(start);d.atomic(area/'report.json',wire({**summary,'state':'tweet_cached_recovery_active','new_cid':new['Id'],'ready':ready}))
  print(json.dumps({'state':'tweet_cached_recovery_active','new_cid':new['Id'],'report':str(area/'report.json'),'new_paid_poll_not_asserted':True}))
if __name__=='__main__':
 try:main()
 except Exception as exc:
  value=exc.args[0] if isinstance(exc,ValueError) and exc.args else ''
  print(json.dumps({'state':'stopped_reconcile_before_retry','error_class':type(exc).__name__,'code':value if isinstance(value,str) and re.fullmatch('[A-Z][A-Z0-9_]{1,100}',value) else 'REPARSE139_CUTOVER_FAILED','automatic_rollback':False}));raise SystemExit(2) from None
