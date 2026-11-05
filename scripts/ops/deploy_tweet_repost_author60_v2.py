"""Only replace the tweet image with the qualified delayed native-control check."""
from copy import deepcopy
from pathlib import Path
import argparse,hashlib,json,os,re,sys,time,types,uuid,xml.etree.ElementTree as ET
from collections import Counter
ROOT=Path('/opt/lighthouse-hub');STATE=Path('/var/lib/lighthouse-tweet-repost-author60-v2')
BASE='sha256:b7d57771653934f7c00c442f5c9ae41ab3fa8861c1488c9da777a70631f8af5b'
IMAGE='sha256:51a71793cebb9f0ab209957a36a3f7b86d7429064ee0ddebf71c3e52b8fb1fbc'
CURRENT='4fe843f8e7a60a2586d1556d8ebcb6b0eaf953fdcef2269af8b612a8263212fd'
PROOF_SHA='b58d6ad144e621249a5465f9cd1f336c3283e32cd1ca01069a829930a83f05e1'
ARCHIVE_SHA='c32c79ec72c3b9799db903f4ae88cda30e0aa80fd7fc22ef20452f9abcd570ed'
HELPER_SHA='9e9c868ec104e9582925f0cae8b8e5cb936d914665257647ca4d685d46687058'
GROUPS={'tests.test_tweet_repost_author_navigation_v2': 20, 'tests.test_tweet_repost_check_v1': 20, 'tests.test_tweet_repost_native_v1': 12, 'tests.test_tweet_repost_reinspection_v2': 8}
NEW={'adapters/browser_tweet_repost_check_v1.mjs': 'bdd5c6a9e6af486367eeb9f5c8cb77fd6f2bd0cbf4db2275affd99d908b26d57', 'services/hub/tweet_repost_check_v1.py': '681a984f138884a4d788fbd3e9c89f94b422b9c50a75836435facd53c9febea6'}
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
 need(q['version']=='tweet-author60-ubuntu-v2' and q['image']==IMAGE and q['base_image']==BASE and q['archive_sha256']==ARCHIVE_SHA,'WRONG_QUALIFICATION')
 need(q['passed'] is True and q['tests']==60 and q['exit_code']==q['failure']==q['error']==q['skipped']==0,'QUALIFICATION_FAILED')
 need(q['resource_limits_passed'] is q['sources_unchanged'] is q['production_tweet_unchanged'] is True and q['original_tweet_cid']==CURRENT,'SOURCE_OR_RUNTIME_PROOF_FAILED')
 need(q['network']=='none' and all(q[k] is False for k in ('production_data_mounted','credentials_injected','oom_killed')),'ISOLATION_FAILED')
 xml=d.read(Path(q['junit_file']),2_000_000);cases=list(ET.fromstring(xml).iter('testcase'))
 need(sha(xml)==q['junit_sha256'] and len(cases)==60 and dict(Counter(c.attrib['classname'] for c in cases))==GROUPS==q['test_groups'],'JUNIT_CHANGED')
 need(all(c.find(k) is None for c in cases for k in ('failure','error','skipped')),'JUNIT_NOT_PASSED')
 for name in ('source_before','source_after'):
  value=q[name];need(value['sources']==q['source_hashes'],'SOURCE_MAP_CHANGED');r=value['resources'];need(r['pids.events']['max']==r['memory.events']['oom']==r['memory.events']['oom_kill']==0,'CGROUP_DENIAL')
 need(all(q['source_hashes'][n]==v for n,v in NEW.items()),'WRONG_REPOST_SOURCE_DELTA')
 need(all(q['source_hashes'][n]==q['manifest']['baseline_source_sha256'][n] for n in COST),'COST_RECALIBRATION_WOULD_BE_REQUIRED')
 need(q['single_copy_layer_verified'] is True and q['machine_cli_cost_unchanged'] is True and q['manifest']['production_delta']==NEW,'TWO_SOURCE_DERIVATION_REQUIRED')
 need({n for n,v in q['source_hashes'].items() if not n.startswith('tests/') and v!=q['manifest']['baseline_source_sha256'].get(n)}==set(NEW),'RUNTIME_DELTA_EXPANDED')
 need(all(q['source_hashes'][n]==q['manifest']['baseline_source_sha256'][n] for n in ('services/hub/machine_tweets.py','services/hub/cli.py')),'MACHINE_OR_CLI_CHANGED')
 image=json.loads(d.run(['/usr/bin/docker','image','inspect',IMAGE]))[0];base=json.loads(d.run(['/usr/bin/docker','image','inspect',BASE]))[0]
 need(image['RootFS']['Layers'][:len(base['RootFS']['Layers'])]==base['RootFS']['Layers'] and len(image['RootFS']['Layers'])==len(base['RootFS']['Layers'])+1,'DERIVED_IMAGE_CHANGED');return q
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
 summary={'version':'tweet-repost-author60-cutover-v2','state':'verified_plan_only','image':IMAGE,'base_image':BASE,'qualification_sha256':PROOF_SHA,
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
  ready=d.wait_ready(start);d.atomic(area/'report.json',wire({**summary,'state':'tweet_repost_check_active','new_cid':new['Id'],'ready':ready}))
  print(json.dumps({'state':'tweet_repost_check_active','new_cid':new['Id'],'report':str(area/'report.json'),'new_paid_poll_not_asserted':True}))
if __name__=='__main__':
 try:main()
 except Exception as exc:
  value=exc.args[0] if isinstance(exc,ValueError) and exc.args else ''
  print(json.dumps({'state':'stopped_reconcile_before_retry','error_class':type(exc).__name__,'code':value if isinstance(value,str) and re.fullmatch('[A-Z][A-Z0-9_]{1,100}',value) else 'REPOST_AUTHOR60_CUTOVER_FAILED','automatic_rollback':False}));raise SystemExit(2) from None
