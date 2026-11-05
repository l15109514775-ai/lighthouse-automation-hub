"""Root tweet-only cutover: plan -> prepare/stop+backup -> reviewed finish.

No X/network call is implemented here. Other four services and their config
files remain unchanged. A failed phase leaves the tweet worker stopped for
reconciliation; never restore an old DB over other services' newer writes.
"""
from copy import deepcopy
from pathlib import Path
import argparse,hashlib,json,os,re,stat,subprocess,sys,time,tomllib,types,uuid
import xml.etree.ElementTree as ET

ROOT=Path('/opt/lighthouse-hub');STATE=Path('/var/lib/lighthouse-tweet-capture-cost-v1')
CBC='sha256:cbc6cd5076960eba0adb723968bd10257c6691d62e3554cf48ae0ff0df5d8d10'
HELPER_SHA='9e9c868ec104e9582925f0cae8b8e5cb936d914665257647ca4d685d46687058'
CORE='01b073e7e725ea78c182ea5bc821ad73427f8dbab0bb5b14fc90daa799fb39b4'
OTHER=('api','scheduler','video-worker','system-worker');PERSIST={'config','data','media','logs','backups'}
PHASE='qualification_plan'
DELTA=('adapters/http.py','adapters/x.py','services/hub/budget.py','services/hub/cli.py',
 'services/hub/source_receipts.py','services/hub/worker.py','services/hub/x_api_discovery.py',
 'services/hub/x_cost_ledger.py','services/hub/x_official_capture_queue_v1.py','services/hub/errors.py',
 'scheduler/migrations/017_x_utc_cost_ledger.sql','scripts/ops/calibrate_x_utc_cost_v1.py')

def sha(raw):return hashlib.sha256(raw).hexdigest()
def wire(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
def need(value,code):
 if not value:raise ValueError(code)
def error_code(exc):
 value=getattr(exc,'code',None) or (exc.args[0] if isinstance(exc,ValueError) and exc.args else '')
 return value if isinstance(value,str) and re.fullmatch('[A-Z][A-Z0-9_]{1,100}',value) else 'TWEET_CAPTURE_COST_PHASE_FAILED'

def helper(file):
 file=Path(file);need(file.is_absolute() and file.resolve()==file and not any(p.is_symlink() for p in (file,*file.parents)) and file.stat().st_size<200000,'SAFE_HELPER_REQUIRED')
 raw=file.read_bytes();need(sha(raw)==HELPER_SHA,'FROZEN_HELPER_REQUIRED')
 m=types.ModuleType('tweet_cutover_frozen_helper');m.__file__=str(file);exec(compile(raw,str(file),'exec'),m.__dict__);return m

def identities(d):
 out={}
 for name in OTHER:
  value=d.inspect(d.cid(name));need(value['State']['Running'],'OTHER_SERVICE_NOT_RUNNING')
  out[name]={'id':value['Id'],'pid':value['State']['Pid'],'started_at':value['State']['StartedAt']}
 return out

def source_hashes(d,cid,names):
 code="import hashlib,json;from pathlib import Path;r=Path('/opt/lighthouse-hub');names="+repr(sorted(names))+";print(json.dumps({n:hashlib.sha256((r/n).read_bytes()).hexdigest() for n in names}))"
 return json.loads(d.run(['/usr/bin/docker','exec','--user','10001:10001',cid,'python','-c',code]))

def qualification(d,args):
 raw=d.read(Path(args.qualified_run),2_000_000);need(sha(raw)==args.qualified_sha256,'QUALIFICATION_SHA_CHANGED');q=json.loads(raw)
 need(q['version']=='tweet-capture-cost-ubuntu-result-v1' and q['passed'] is True and q['image']==args.image and q['base_image']==CBC,'WRONG_QUALIFICATION')
 need(q['network']=='none' and all(q[k] is False for k in ('production_data_mounted','credentials_injected','oom_killed')),'QUALIFICATION_ISOLATION_REQUIRED')
 need(q['exit_code']==q['failure']==q['error']==q['skipped']==0 and q['resource_limits_passed'] is True,'QUALIFICATION_FAILED')
 m=q['source_manifest'];need(tuple(m['runtime_delta'])==DELTA and m['base_image']==CBC,'EXACT_TWELVE_DELTA_REQUIRED')
 need(q['effective_runtime']['archive_sha256']==args.archive_sha256 and q['effective_runtime']['original_container_id']==args.expected_current_cid,'WRONG_CURRENT_RUNTIME_PARENT')
 xml=d.read(Path(q['junit_file']),2_000_000);need(sha(xml)==q['junit_sha256'],'JUNIT_SHA_CHANGED')
 cases=list(ET.fromstring(xml).iter('testcase'));groups={}
 for case in cases:
  name=case.attrib['classname'].split('.')[1];groups[name]=groups.get(name,0)+1
 need(len(cases)==q['tests']==m['expected_tests'] and groups==q['test_groups']==m['expected_test_groups'],'EXACT_SUITE_REQUIRED')
 need(all(c.find(k) is None for c in cases for k in ('failure','error','skipped')),'RAW_JUNIT_FAILED')
 runtime=q['runtime_verification'];need(runtime['unchanged'] is True and runtime['source_hashes']==q['effective_runtime']['effective_runtime_sha256'],'SOURCE_VERIFICATION_REQUIRED')
 for name in ('resources_before','resources_after'):
  counters=runtime[name]
  need(counters['pids.events']['max']==counters['memory.events']['oom']==counters['memory.events']['oom_kill']==0,'CGROUP_DENIAL')
 need(q['effective_runtime']['effective_runtime_sha256']['compositor/src/core.mjs']==CORE,'WORD_JOINER_CORE_CHANGED')
 image=json.loads(d.run(['/usr/bin/docker','image','inspect',args.image]))[0];base=json.loads(d.run(['/usr/bin/docker','image','inspect',CBC]))[0]
 need(image['Id']==args.image and image['RootFS']['Layers'][:len(base['RootFS']['Layers'])]==base['RootFS']['Layers'],'QUALIFIED_IMAGE_PARENT_CHANGED')
 return q

def enable_config(raw):
 text=raw.decode('utf-8-sig');before=tomllib.loads(text);budget=before['budgets']['x']
 need(budget['daily_usd']==.5 and budget['monthly_usd']==5,'OWNER_BUDGET_CAPS_CHANGED')
 need(type(budget.get('utc_resource_dedup_estimate_enabled',False)) is bool and budget.get('utc_resource_dedup_estimate_enabled',False) is False,'EXPECT_OLD_GROSS_MODE')
 match=re.search(r'(?m)^\[budgets\.x\]\s*$',text);need(match is not None,'X_BUDGET_TABLE_REQUIRED')
 end=re.search(r'(?m)^\[',text[match.end():]);limit=match.end()+end.start() if end else len(text)
 section=text[match.end():limit]
 if 'utc_resource_dedup_estimate_enabled' in budget:
  section,count=re.subn(r'(?m)^utc_resource_dedup_estimate_enabled\s*=.*$','utc_resource_dedup_estimate_enabled = true',section);need(count==1,'EXACT_BOOLEAN_REQUIRED')
 else:section=section.rstrip()+'\nutc_resource_dedup_estimate_enabled = true\n\n'
 updated=text[:match.end()]+section+text[limit:];after=tomllib.loads(updated)
 expected=deepcopy(before);expected['budgets']['x']['utc_resource_dedup_estimate_enabled']=True
 need(after==expected,'CONFIG_DIFFERENCE_OUTSIDE_BOOLEAN')
 return (b'\xef\xbb\xbf' if raw.startswith(b'\xef\xbb\xbf') else b'')+updated.encode()

def candidate(before,image,config_file,config_target):
 after=deepcopy(before);service=after['services']['tweet-worker'];old=service['volumes']
 kept=[v for v in old if v.get('target') in {str(ROOT/n) for n in PERSIST}]
 need(len(kept)==5 and len({v['target'] for v in kept})==5,'EXACT_FIVE_PERSISTENT_BINDS_REQUIRED')
 need(all(v['type']=='bind' and v.get('read_only',False)==(v['target']==str(ROOT/'config')) for v in kept),'PERSISTENT_BIND_MODE_CHANGED')
 need(config_target.is_relative_to(ROOT/'config') and str(config_target) not in {v['target'] for v in kept},'CONFIG_TARGET_REQUIRED')
 service['image']=image;service['volumes']=kept+[{'type':'bind','source':str(config_file),'target':str(config_target),'read_only':True,'bind':{'create_host_path':False}}]
 a=deepcopy(before);b=deepcopy(after)
 for cfg in (a,b):cfg['services']['tweet-worker'].pop('image');cfg['services']['tweet-worker'].pop('volumes')
 need(a==b,'SERVICE_FIELDS_CHANGED');return after

APP=r'''
import hashlib,json,os,sqlite3,sys
from pathlib import Path
from services.hub.db import Store,encode
from services.hub import x_cost_ledger as cost
assert os.getuid()==10001
root=Path('/opt/lighthouse-hub');out=Path('/operation');store=Store(root/'data/hub.sqlite3');mode=sys.argv[1]
def write(name,value):
 raw=encode(value).encode()+b'\n';file=out/name
 with file.open('xb') as f:os.chmod(file,0o600);f.write(raw);f.flush();os.fsync(f.fileno())
 if os.name=='posix':
  fd=os.open(out,os.O_RDONLY|os.O_DIRECTORY)
  try:os.fsync(fd)
  finally:os.close(fd)
if mode=='prepare':
 src=sqlite3.connect(store.path.as_uri()+'?mode=ro',uri=True);backup=out/'pre-migration.sqlite3';dst=sqlite3.connect(backup)
 try:
  src.backup(dst);assert dst.execute('PRAGMA integrity_check').fetchone()[0]=='ok';assert dst.execute('PRAGMA foreign_key_check').fetchall()==[]
 finally:dst.close();src.close()
 backup.chmod(0o600)
 db=store.connect()
 try:
  db.execute('BEGIN IMMEDIATE');before=cost.history_fingerprints(db)
  assert db.execute('SELECT 1 FROM schema_migrations WHERE version=16').fetchone()
  text=(root/'scheduler/migrations/017_x_utc_cost_ledger.sql').read_text();buffer=''
  for line in text.splitlines(keepends=True):
   buffer+=line
   if sqlite3.complete_statement(buffer):
    statement=buffer.strip();buffer=''
    if statement not in ('BEGIN IMMEDIATE;','COMMIT;'):db.execute(statement)
  assert not buffer.strip();after=cost.history_fingerprints(db);assert before==after;db.commit()
 except BaseException:db.rollback();raise
 finally:db.close()
 from scripts.ops.calibrate_x_utc_cost_v1 import ReadOnlyStore
 plan=cost.calibration_plan(root,ReadOnlyStore(store.path));write('calibration-plan.json',plan)
 write('prepare-result.json',{'state':'prepared_calibration_review','backup_file':str(backup),'backup_bytes':backup.stat().st_size,
  'backup_sha256':hashlib.sha256(backup.read_bytes()).hexdigest(),'backup_integrity':'ok','backup_foreign_keys':'ok',
  'history_before':before,'history_after':after,'plan_sha256':cost.digest(plan),'periods':plan['periods'],'source_files':plan['source_files']})
elif mode=='finish':
 plan=json.loads(Path('/plan/calibration-plan.json').read_bytes());assert cost.digest(plan)==sys.argv[2]
 value=cost.apply_calibration(root,store,plan);assert value['history_before']==value['history_after'];write('apply-result.json',value)
else:raise ValueError('FIXED_PHASE_REQUIRED')
'''

def app_phase(d,image,area,mode,plan_sha=None):
 output=area/('prepared' if mode=='prepare' else 'applied');output.mkdir(mode=0o700);os.chown(output,10001,10001);output.chmod(0o700)
 cmd=['/usr/bin/docker','create','--pull=never','--init','--name','tweet-cost-'+mode+'-'+uuid.uuid4().hex[:12],
  '--network=none','--read-only','--user=10001:10001','--cap-drop=ALL','--security-opt=no-new-privileges:true',
  '--security-opt=seccomp='+str(ROOT/'config/browser-seccomp.json'),'--memory=1400m','--cpus=1','--pids-limit=256',
  '--tmpfs=/tmp:rw,size=256m,mode=1777','--tmpfs='+str(ROOT/'data/browser-profiles')+':rw,size=1m,mode=000',
  '--env=HOME=/tmp/home','--env=XDG_CONFIG_HOME=/tmp/config','--env=XDG_CACHE_HOME=/tmp/cache',
  '--mount=type=bind,source='+str(ROOT/'data')+',target='+str(ROOT/'data'),
  '--mount=type=bind,source='+str(ROOT/'media')+',target='+str(ROOT/'media')+',readonly',
  '--mount=type=bind,source='+str(output)+',target=/operation']
 if mode=='finish':cmd+=['--mount=type=bind,source='+str(area/'prepared')+',target=/plan,readonly']
 cid=d.run(cmd+['--entrypoint=python',image,'-c',APP,mode,*([plan_sha] if plan_sha else [])]).strip()
 actual=d.inspect(cid);need(actual['Image']==image and actual['HostConfig']['NetworkMode']=='none' and actual['HostConfig']['ReadonlyRootfs'],'CALIBRATION_ISOLATION_CHANGED')
 d.atomic(area/(mode+'-container.json'),wire({'container_id':cid,'image':image,'phase':mode}))
 need(not any(re.search(r'(^|_)(KEY|TOKEN|SECRET|PASSWORD|COOKIE)(_|$)',v.split('=',1)[0]) for v in actual['Config']['Env']),'CALIBRATION_SECRET_ENV_REJECTED')
 d.run(['/usr/bin/docker','start',cid]);code=d.run(['/usr/bin/docker','wait',cid],timeout=600).strip()
 logs=subprocess.run(['/usr/bin/docker','logs',cid],capture_output=True,text=True,timeout=30,check=True)
 d.atomic(area/(mode+'-log.txt'),(logs.stdout+'\n'+logs.stderr).encode());need(code=='0','CALIBRATION_PHASE_FAILED')
 for file in output.iterdir():
  need(file.is_file() and not file.is_symlink(),'UNEXPECTED_PHASE_ARTIFACT');os.chown(file,0,10001);file.chmod(0o600 if file.suffix=='.sqlite3' else 0o640)
 os.chown(output,0,10001);output.chmod(0o750)
 result=json.loads(d.read(output/('prepare-result.json' if mode=='prepare' else 'apply-result.json'),4_000_000))
 need(result['history_before']==result['history_after'],'OLD_LEDGER_CHANGED');return result

def db_idle(d):
 def app():os.setgroups([]);os.setgid(10001);os.setuid(10001)
 code="import sqlite3,json;from pathlib import Path;c=sqlite3.connect(Path('/opt/lighthouse-hub/data/hub.sqlite3').as_uri()+'?mode=ro',uri=True);print(json.dumps({'processing':c.execute(\"SELECT COUNT(*) FROM jobs WHERE queue='tweet' AND state='processing'\").fetchone()[0]}))"
 p=subprocess.run(['/usr/bin/python3','-c',code],preexec_fn=app,capture_output=True,text=True,timeout=20,env={'PATH':'/usr/bin:/bin','HOME':'/tmp'})
 need(p.returncode==0,'READONLY_DB_CHECK_FAILED');need(json.loads(p.stdout)['processing']==0,'ACTIVE_TWEET_LEASE');return True

def runtime_preserved(old,new,config_source,config_target):
 need(new['State']['Running'] and new['Config']['User']==old['Config']['User']=='10001:10001','NEW_TWEET_NOT_READY')
 for key in ('Cmd','Entrypoint','WorkingDir'):need(new['Config'][key]==old['Config'][key],'ENTRY_CHANGED')
 need(sorted(new['Config']['Env'])==sorted(old['Config']['Env']),'ENV_CHANGED')
 need(len({v.split('=',1)[0] for v in new['Config']['Env']})==len(new['Config']['Env']),'DUPLICATE_ENV')
 for key in ('Memory','MemorySwap','NanoCpus','PidsLimit','ShmSize','ReadonlyRootfs','Init','CapDrop','Privileged','SecurityOpt','PortBindings','Tmpfs','RestartPolicy'):
  need(new['HostConfig'][key]==old['HostConfig'][key],'HOST_RESOURCE_CHANGED')
 for name in PERSIST:
  before=[v for v in old['Mounts'] if v['Destination']==str(ROOT/name)];after=[v for v in new['Mounts'] if v['Destination']==str(ROOT/name)]
  need(before==after,'PERSISTENT_MOUNT_CHANGED')
 cfg=[v for v in new['Mounts'] if v['Destination']==str(config_target)]
 need(len(new['Mounts'])==6 and len(cfg)==1 and cfg[0]['Type']=='bind' and cfg[0]['Source']==str(config_source) and cfg[0]['RW'] is False,'CONFIG_OVERRIDE_CHANGED')

def runtime_fingerprint(value):
 return {'id':value['Id'],'image':value['Image'],'config_sha256':sha(wire(value['Config'])),
  'host_config_sha256':sha(wire(value['HostConfig'])),'mounts_sha256':sha(wire(value['Mounts']))}

def main():
 global PHASE
 p=argparse.ArgumentParser();p.add_argument('--base-helper',required=True);p.add_argument('--qualified-run',required=True);p.add_argument('--qualified-sha256',required=True)
 p.add_argument('--archive-sha256',required=True);p.add_argument('--image',required=True);p.add_argument('--expected-current-cid',required=True)
 mode=p.add_mutually_exclusive_group();mode.add_argument('--apply',action='store_true');mode.add_argument('--finish',action='store_true')
 p.add_argument('--operation-id');p.add_argument('--plan-sha256');a=p.parse_args()
 need(os.name=='posix' and os.geteuid()==0 and not sys.flags.optimize,'ROOT_HOST_REQUIRED');os.umask(0o077);d=helper(a.base_helper)
 need(re.fullmatch('sha256:[a-f0-9]{64}',a.image) and re.fullmatch('[a-f0-9]{64}',a.expected_current_cid),'PINNED_RUNTIME_REQUIRED')
 q=qualification(d,a);compose=ROOT/'compose.yaml';d.protected(compose);raw=d.read(compose,2_000_000);before=d.parsed()
 old=d.inspect(a.expected_current_cid);need(d.cid('tweet-worker')==a.expected_current_cid and old['Image']==CBC,'CURRENT_TWEET_CHANGED')
 others=identities(d);env=dict(v.split('=',1) for v in old['Config']['Env']);cfg=d.safe(Path(env.get('HUB_CONFIG',str(ROOT/'config/hub.toml'))))
 need(cfg.is_relative_to(ROOT/'config'),'CONFIG_OUTSIDE_DECLARED_ROOT');d.protected(cfg);config_raw=d.read(cfg,2_000_000);newconfig=enable_config(config_raw)
 inherited=q['effective_runtime']['inherited_overlays']
 code_mounts={str(Path(v['Destination']).relative_to(ROOT)):v for v in old['Mounts'] if v['Destination'] not in {str(ROOT/n) for n in PERSIST}}
 need(set(code_mounts)==set(inherited),'INHERITED_MOUNTS_CHANGED')
 for name,value in inherited.items():
  mount=code_mounts[name];need(mount['Type']=='bind' and mount['RW'] is False and mount['Source']==value['source'],'INHERITED_MOUNT_SOURCE_CHANGED')
  d.protected(Path(value['source']));need(sha(d.read(Path(value['source']),2_000_000))==value['sha256'],'INHERITED_SOURCE_CHANGED')
 base={'version':'tweet-capture-cost-cutover-v1','qualification_sha256':a.qualified_sha256,'image':a.image,'old_cid':a.expected_current_cid,
  'compose_before_sha256':sha(raw),'config_file':str(cfg),'config_before_sha256':sha(config_raw),'enabled_config_sha256':sha(newconfig),
  'other_four':others,'old_runtime_fingerprint':runtime_fingerprint(old),'budget_caps_unchanged':True,
  'api_monitor_still_reports_gross':True,'no_paid_call_by_operator':True}
 if not a.apply and not a.finish:need(old['State']['Running'],'CURRENT_TWEET_NOT_RUNNING');print(json.dumps({**base,'state':'verified_plan_only'}));return
 d.directory(STATE,0o700)
 with d.deployment_lock():
  if a.apply:
   PHASE='stop_backup_migrate_plan'
   need(old['State']['Running'],'CURRENT_TWEET_NOT_RUNNING');area=STATE/str(uuid.uuid4());d.directory(area,0o700)
   d.atomic(area/'compose-before.yaml',raw);d.atomic(area/'config-before.toml',config_raw);d.atomic(area/'runtime-before.json',wire(runtime_fingerprint(old)))
   report={**base,'operation_id':area.name,'state':'prepare_started'};d.atomic(area/'report.json',wire(report))
   with d.tweet_lock():
    db_idle(d);need(identities(d)==others and d.read(compose,2_000_000)==raw,'CONCURRENT_CHANGE')
    d.compose('stop','--timeout','45','tweet-worker',timeout=60);need(not d.inspect(a.expected_current_cid)['State']['Running'],'TWEET_NOT_STOPPED');db_idle(d)
    try:prepared=app_phase(d,a.image,area,'prepare');report.update(state='prepared_calibration_review',preparation=prepared)
    except BaseException as exc:report.update(state='prepare_failed_tweet_kept_stopped',error_code=error_code(exc),phase=PHASE);d.atomic(area/'report.json',wire(report));raise
   need(identities(d)==others,'OTHER_SERVICE_CHANGED');d.atomic(area/'report.json',wire(report));print(json.dumps({'state':report['state'],'operation_id':area.name,'report':str(area/'report.json'),'plan_sha256':prepared['plan_sha256'],'periods':prepared['periods']}));return
  need(a.operation_id and str(uuid.UUID(a.operation_id))==a.operation_id,'EXACT_OPERATION_REQUIRED');area=STATE/a.operation_id;d.protected(area/'report.json')
  report=json.loads(d.read(area/'report.json',4_000_000));need(report['state']=='prepared_calibration_review' and all(report[k]==v for k,v in base.items()),'PREPARATION_CHANGED')
  need(a.plan_sha256==report['preparation']['plan_sha256'],'EXACT_REVIEWED_CALIBRATION_REQUIRED');need(not old['State']['Running'],'OLD_TWEET_STILL_RUNNING')
  with d.tweet_lock():
   PHASE='apply_reviewed_calibration_and_config'
   db_idle(d);need(identities(d)==others and d.read(compose,2_000_000)==raw,'CONCURRENT_CHANGE')
   try:
    applied=app_phase(d,a.image,area,'finish',a.plan_sha256);report.update(state='calibrated_before_start',calibration=applied);d.atomic(area/'report.json',wire(report))
    target=ROOT/'config'/('tweet-capture-cost-'+area.name+'.toml');d.atomic(target,newconfig,mode=0o640);os.chown(target,0,10001)
    after=candidate(before,a.image,target,cfg);d.atomic(compose,wire(after),original=compose.stat());need(d.parsed()==after,'COMPOSE_READBACK_FAILED')
    report.update(state='enabled_config_installed_start_pending');d.atomic(area/'report.json',wire(report))
   except BaseException as exc:report.update(state='finish_failed_tweet_kept_stopped',error_code=error_code(exc),phase=PHASE);d.atomic(area/'report.json',wire(report));raise
  PHASE='start_tweet_and_verify_ready'
  start=time.time();d.up();new=d.inspect(d.cid('tweet-worker'));need(new['Image']==a.image,'NEW_IMAGE_CHANGED')
  runtime_preserved(old,new,target,cfg);need(source_hashes(d,new['Id'],q['effective_runtime']['effective_runtime_sha256'])==q['effective_runtime']['effective_runtime_sha256'],'NEW_SOURCE_CHANGED')
  ready=d.wait_ready(start);need(identities(d)==others and d.read(cfg,2_000_000)==config_raw,'OTHER_SERVICE_OR_SHARED_CONFIG_CHANGED')
  report.update(state='tweet_capture_cost_active',new_cid=new['Id'],ready=ready);d.atomic(area/'report.json',wire(report));print(json.dumps({'state':report['state'],'report':str(area/'report.json'),'new_cid':new['Id'],'first_new_poll_not_asserted':True}))

if __name__=='__main__':
 try:main()
 except Exception as exc:print(json.dumps({'state':'stopped_reconcile_before_retry','error_class':type(exc).__name__,'error_code':error_code(exc),'phase':PHASE,'automatic_database_rollback':False}));raise SystemExit(2) from None
