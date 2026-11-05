"""App-UID maintenance: default plan, explicit two cached PRIVATE saves.

No job claim/lease, DB mutation, file chooser, AddP or public-send operation.
The root operator installs this script and the four fixed JS files read-only.
"""
import argparse,hashlib,json,os,re,signal,sqlite3,stat,subprocess,sys,threading,time,uuid
from contextlib import ExitStack,contextmanager,closing
from pathlib import Path

ROOT=Path('/opt/lighthouse-hub')
JOB='62d59ed5-96c5-48d7-abaf-e6c24c6ae6dc'
PILOT='594aed06-3aa1-45f4-90f0-1db49a602395'
BV='BV1JExh6QELr'
SOURCE='9T5tn1WGlJg'
UID='1000000000000000'
SAMPLES='multipart-v4-native-real-samples-f78cd998-5114-4a84-a88e-e3b7f02bd7ed/samples.json'
SAMPLES_SHA='abdbf1ab1a007a13f51c44db30d8279335dc70b01f8d0823fae1e73a97b5722c'
PINS={
 'scripts/ops/native_control_sources/cache_separate_private_tasks_v5.mjs':'abf9fa5de1e9ba6cd2a45da9bc6fce82ec5a2d26a479266136d0ff12ef36713b',
 'adapters/bilibili_video_guard.mjs':'1ecb678df412dd4aeb2680b4762e82b04bf7083484da81ee8df90815fc43069c',
 'adapters/short_video_saved_draft_navigation_control.mjs':'27770670eb8fddc997f4e6b2c2c8f6f9a17804d42b41097fca45f04e8e090a69',
 'scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs':'d25de4d718bff78d487fb3aae7e9b1739a4ddfbef2d5eb170843049d283d5e23'}
def fail(code):raise RuntimeError(code)
def sha(raw):return hashlib.sha256(raw).hexdigest()
def encoded(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'),sort_keys=True).encode()
def safe(file):
 file=Path(file)
 if not file.is_absolute() or file.resolve()!=file or any(p.is_symlink() for p in (file,*file.parents)):fail('CACHED_HARNESS_UNSAFE_PATH')
 return file
def read(file,maximum=1_000_000):
 file=safe(file);fd=os.open(file,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_BINARY',0))
 with os.fdopen(fd,'rb') as stream:
  before=os.fstat(stream.fileno())
  if not stat.S_ISREG(before.st_mode) or not 0<before.st_size<=maximum:fail('CACHED_HARNESS_FILE_BOUND')
  raw=stream.read(maximum+1);after=os.fstat(stream.fileno())
 if len(raw)!=before.st_size or any(getattr(before,k)!=getattr(after,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')):fail('CACHED_HARNESS_FILE_CHANGED')
 return raw
def ref(file,maximum=1_000_000):
 raw=read(file,maximum);return {'file':str(file),'bytes':len(raw),'sha256':sha(raw)}
def check_ref(value,maximum=1_000_000):
 if set(value)!={'file','bytes','sha256'} or ref(value['file'],maximum)!=value:fail('CACHED_HARNESS_REFERENCE_CHANGED')
def write(file,value):
 file=safe(file);fd=os.open(file,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
 with os.fdopen(fd,'wb') as stream:stream.write(encoded(value));stream.flush();os.fsync(stream.fileno())
 if os.name=='posix':
  fd=os.open(file.parent,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)
 return ref(file,2_000_000)
def snapshot(root):
 with closing(sqlite3.connect(safe(root/'data/hub.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=10)) as db:
  db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
  one=lambda query,args=():[dict(r) for r in db.execute(query,args)]
  jobs=one('SELECT * FROM jobs WHERE job_id IN (?,?) ORDER BY job_id',(JOB,PILOT))
  pubs=one('SELECT * FROM publications WHERE job_id IN (?,?) ORDER BY idempotency_key',(JOB,PILOT))
  if len(jobs)!=2:fail('CACHED_HARNESS_JOB_REQUIRED')
  probe=next(r for r in jobs if r['job_id']==JOB);pilot=next(r for r in jobs if r['job_id']==PILOT)
  state=json.loads(probe['result']).get('multipart_private_v4',{});payload=json.loads(probe['payload'])
  if (probe['job_type']!='replay_multipart_private_probe_v4' or probe['policy_version']!='replay-multipart-private-probe-v4' or
   probe['source_platform']!='maintenance' or probe['state']!='upload_pending' or probe['attempts']!=1 or probe['queue']!='video' or
   probe['lease_owner'] is not None or probe['lease_until'] is not None or state.get('state')!='upload_unknown' or
   payload.get('source_id')!=SOURCE or payload.get('target_site_uid')!=UID or state.get('binding_sha256')!=sha(encoded(payload)) or
   pilot['source_id']!=SOURCE or pilot['state']!='completed' or pilot['attempts']!=3 or pilot['external_publish_id']!=BV or
   pilot['lease_owner'] is not None or pilot['lease_until'] is not None or any(r['job_id']==JOB for r in pubs) or
   len(pubs)!=1 or pubs[0]['job_id']!=PILOT or pubs[0]['state']!='completed' or pubs[0]['external_id']!=BV or
   pubs[0]['source_id']!=SOURCE or pubs[0]['account_id']!='site_uid:'+UID or
   one("SELECT 1 FROM jobs WHERE queue='video' AND state='processing' LIMIT 1")):fail('CACHED_HARNESS_DB_SCOPE_CHANGED')
  if str(uuid.UUID(state['attempt_id']))!=state['attempt_id']:fail('CACHED_HARNESS_ATTEMPT_INVALID')
  return {'jobs':jobs,'publications':pubs}
def source_refs(root):
 refs=[]
 for relative,checksum in PINS.items():
  file=root/relative;value=ref(file)
  if value['sha256']!=checksum:fail('CACHED_HARNESS_SOURCE_PIN_CHANGED')
  protected(file)
  refs.append(value)
 return refs
def protected(file):
 if os.name=='posix' and any(p.stat().st_uid!=0 or p.stat().st_mode&0o022 for p in (safe(file),*Path(file).parents)):fail('CACHED_HARNESS_SOURCE_NOT_ROOT_RO')
def check_fence(root,value,*,now=None):
 if value.get('version')!='cached-private-maintenance-fence-v5' or value['root']!=str(root) or (time.monotonic() if now is None else now)>=value['deadline_monotonic']:fail('CACHED_HARNESS_EXPIRED')
 if snapshot(root)!=value['snapshot']:fail('CACHED_HARNESS_DB_FINGERPRINT_CHANGED')
 for reference in value['small_refs']:check_ref(reference,2_000_000)
 for reference in value['sample_files']:check_ref(reference,100_000_000)
 return {'state':'fence_verified','fake_lease':False}
def profile(root):
 file=safe(root/'data/browser-profiles/bilibili-video');st=file.stat()
 if not stat.S_ISDIR(st.st_mode) or st.st_uid!=10001 or st.st_gid!=10001 or st.st_mode&0o077:fail('CACHED_HARNESS_PROFILE_PERMISSION_CHANGED')
 return file
def build_plan(root,output,owner_reference,seconds,script):
 if not isinstance(owner_reference,str) or not 0<len(owner_reference.strip())<=2048 or owner_reference.startswith(('REPLACE','UNCONFIGURED')):fail('CACHED_HARNESS_REAL_OWNER_REFERENCE_REQUIRED')
 if type(seconds) is not int or not 60<=seconds<=900:fail('CACHED_HARNESS_DEADLINE_INVALID')
 before=snapshot(root);probe=next(j for j in before['jobs'] if j['job_id']==JOB);pilot=next(j for j in before['jobs'] if j['job_id']==PILOT)
 payload=json.loads(probe['payload']);state=json.loads(probe['result'])['multipart_private_v4']
 samples=root/'media'/SAMPLES;sample=ref(samples)
 if sample['sha256']!=SAMPLES_SHA or sample!=payload['sample_manifest']:fail('CACHED_HARNESS_SAMPLE_CHANGED')
 body=json.loads(read(samples));selection=root/'media'/JOB/'multipart-private-v4/video-selection-intent.json';old=json.loads(read(selection))
 if (body['source_id']!=SOURCE or len(body['parts'])!=2 or old['version']!='multipart-private-one-use-intent-v4' or old['kind']!='video-selection' or
  old['job_id']!=JOB or old['attempt_id']!=state['attempt_id'] or old['site_uid']!=UID or old['source_id']!=SOURCE or
  old['source_url']!='https://www.youtube.com/watch?v='+SOURCE or old['parts']!=payload['parts']):fail('CACHED_HARNESS_ORIGINAL_SELECTION_CHANGED')
 metadata=json.loads(pilot['payload'])['metadata'];url='https://www.youtube.com/watch?v='+SOURCE
 if metadata['source']!=url or metadata['copyright']!=2 or '转载许可编号' in metadata['description'] or url not in metadata['description']:fail('CACHED_HARNESS_METADATA_SOURCE_CHANGED')
 tasks=[];files=[]
 for i,(item,part) in enumerate(zip(body['parts'],payload['parts']),1):
  file=samples.parent/f'part-{i:03d}.mp4';actual=ref(file,100_000_000)
  if (actual!={'file':item['path'],'bytes':item['bytes'],'sha256':item['sha256']} or str(file)!=part['file'] or
   any(actual[k]!=part[k] for k in ('bytes','sha256')) or float(item['probe']['format']['duration'])!=part['duration_seconds']):fail('CACHED_HARNESS_CLIP_CHANGED')
  meta={**metadata,'title':'【私有验收】示例主播A 缓存独立片段 P'+str(i)+'/2'}
  tasks.append({'index':i,'filename':file.name,'metadata':meta,'metadata_sha256':sha(encoded(meta))});files.append(actual)
 spec={'version':'cached-separate-private-tasks-manifest-v5','maintenance_id':output.name,'original_job_id':JOB,
  'original_attempt_id':state['attempt_id'],'target_site_uid':UID,'source_id':SOURCE,'source_url':url,'sample_manifest':sample,
  'original_selection_intent':ref(selection),'tasks':tasks}
 refs=[sample,ref(selection),*source_refs(root),ref(script)]
 intent_files=sorted((root/'media'/PILOT/'replay-native-intents').glob('*.json'))
 if {file.name for file in intent_files}!={'video-selection.json','cover-completion.json','private-draft-save.json','public-submit.json'}:fail('CACHED_HARNESS_PILOT_INTENT_COUNT_CHANGED')
 for file in intent_files:refs.append(ref(file))
 fence={'version':'cached-private-maintenance-fence-v5','root':str(root),'snapshot':before,'small_refs':refs,'sample_files':files,
  'deadline_monotonic':time.monotonic()+seconds,'owner_reference':owner_reference,'no_database_write':True,'fake_lease':False}
 return spec,fence

@contextmanager
def four_locks(root):
 # Existing Worker-compatible observer.lock files, opened without following
 # links. Never chown/chmod an existing appdata path as a root operation.
 import fcntl
 with ExitStack() as stack:
  for name in ('video','moderation','bilibili-video-profile','heavy'):
   folder=safe(root/'data/locks'/name);folder.mkdir(mode=0o700,parents=True,exist_ok=True);safe(folder)
   d=os.open(folder,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);stack.callback(os.close,d)
   fd=os.open('observer.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=d);stack.callback(os.close,fd)
   if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_uid!=10001:fail('CACHED_HARNESS_LOCK_OWNER_CHANGED')
   fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  yield

JS=r'''
import fs from 'node:fs/promises';import {execFileSync} from 'node:child_process';
import {chromium} from '/opt/lighthouse-hub/compositor/node_modules/playwright/index.mjs';
import {reconcileCachedPrivateTasks} from '/opt/lighthouse-hub/scripts/ops/native_control_sources/cache_separate_private_tasks_v5.mjs';
const [specfile,fencefile,fencesha,script,python,seconds]=process.argv.slice(1),root='/opt/lighthouse-hub';
let context,opening,closing;const close=()=>closing??=(async()=>{await (context||await opening?.catch(()=>null))?.close();})();
process.once('SIGTERM',()=>close().finally(()=>process.exit(2)));process.once('SIGINT',()=>close().finally(()=>process.exit(2)));
const timer=setTimeout(()=>close().finally(()=>process.exit(2)),Number(seconds)*1000);
const fence=()=>{const raw=execFileSync(python,[script,'--check-fence',fencefile,'--fence-sha256',fencesha],{timeout:10000,stdio:['ignore','pipe','ignore']});if(JSON.parse(raw).state!=='fence_verified')throw new Error('CACHED_HARNESS_FENCE_FAILED');};
try{
 fence();const spec=JSON.parse(await fs.readFile(specfile,'utf8'));
 opening=chromium.launchPersistentContext(root+'/data/browser-profiles/bilibili-video',{headless:true,channel:'chromium',chromiumSandbox:true,viewport:{width:1324,height:960},acceptDownloads:false});
 context=await opening;if(context.pages().length>1)throw new Error('CACHED_HARNESS_MULTIPLE_PAGES');
 const page=context.pages()[0]||await context.newPage();
 const result=await reconcileCachedPrivateTasks({page,context,spec,root,beforeAction:fence});
 fence();console.log(JSON.stringify({state:result.state,result_file:result.result_file,public_submission:false,file_selection:false}));
}catch(e){console.log(JSON.stringify({state:'private_save_unknown_or_blocked',error_code:/^[A-Z][A-Z0-9_]{1,100}$/.test(e?.code||e?.message||'')?(e.code||e.message):'CACHED_HARNESS_NATIVE_FAILED'}));process.exitCode=2;}
finally{clearTimeout(timer);await close();}
'''
def process_rows():
 result={}
 for file in Path('/proc').glob('[0-9]*/stat'):
  try:
   s=file.read_text();p=s[s.rfind(')')+2:].split();result[int(file.parent.name)]=(int(p[1]),int(p[19]),p[0])
  except (OSError,ValueError,IndexError):pass
 return result
class OwnedChild:
 def __init__(self,process):
  self.process=process;self.owned={};self.stop=threading.Event();self.remember()
  self.thread=threading.Thread(target=self.track,daemon=True);self.thread.start()
 def remember(self):
  rows=process_rows()
  if self.process.poll() is None and self.process.pid in rows:self.owned.setdefault(self.process.pid,rows[self.process.pid][1])
  changed=True
  while changed:
   changed=False
   for pid,(parent,start,_) in rows.items():
    if pid not in self.owned and parent in self.owned and parent in rows and rows[parent][1]==self.owned[parent]:self.owned[pid]=start;changed=True
 def track(self):
  while not self.stop.wait(.05):self.remember()
 def live(self):
  self.remember();return [pid for pid,(_,start,state) in process_rows().items() if self.owned.get(pid)==start and state!='Z']
 def signal_owned(self,sig):
  for pid in self.live():
   try:os.kill(pid,sig)
   except (ProcessLookupError,PermissionError):pass
 def close(self):
  self.remember()
  # Ask Node to close the actual context before escalating its captured PG.
  if self.process.poll() is None:
   try:self.process.terminate()
   except (ProcessLookupError,PermissionError):pass
  until=time.monotonic()+15
  while self.live() and time.monotonic()<until:time.sleep(.05)
  for sig in (signal.SIGTERM,signal.SIGKILL):
   rows=process_rows()
   if self.process.pid in rows and self.owned.get(self.process.pid)==rows[self.process.pid][1]:
    try:
     if os.getpgid(self.process.pid)==self.process.pid:os.killpg(self.process.pid,sig)
    except (ProcessLookupError,PermissionError):pass
   self.signal_owned(sig);until=time.monotonic()+5
   while self.live() and time.monotonic()<until:time.sleep(.05)
  remaining=self.live()
  if not remaining:self.stop.set();self.thread.join(timeout=1);self.process.wait(timeout=1)
  return remaining
def profile_owners(root):
 target=str(root/'data/browser-profiles/bilibili-video');result=[]
 for file in Path('/proc').glob('[0-9]*/cmdline'):
  try:
   args=file.read_bytes().split(b'\0');matches=[]
   for i,arg in enumerate(args):
    if arg.startswith(b'--user-data-dir='):matches.append(os.fsdecode(arg.split(b'=',1)[1]))
    elif arg==b'--user-data-dir' and i+1<len(args):matches.append(os.fsdecode(args[i+1]))
   if any(str(Path(item).resolve())==target for item in matches):result.append(int(file.parent.name))
  except (OSError,ValueError):pass
 return result
def validate_result(output,spec,result):
 if set(result)!={'state','result_file','public_submission','file_selection'} or result['state']!='two_separate_private_drafts_saved' or result['public_submission'] is not False or result['file_selection'] is not False:fail('CACHED_HARNESS_RESULT_INVALID')
 reference=result['result_file'];check_ref(reference,2_000_000)
 if reference['file']!=str(output/'result.json'):fail('CACHED_HARNESS_RESULT_PATH_CHANGED')
 value=json.loads(read(reference['file'],2_000_000))
 if (value['version']!='cached-separate-private-tasks-result-v5' or value['state']!=result['state'] or value['maintenance_id']!=spec['maintenance_id'] or
  value['original_job_id']!=JOB or value['original_attempt_id']!=spec['original_attempt_id'] or value['site_uid']!=UID or value['source_id']!=SOURCE or
  value['origin']!='later_separate_tasks_private_v5' or value['same_upload_session_observed'] is not False or value['human_review'] is not False or
  value['publication_authorized'] is not False or value['native_multipart_verified'] is not False or value['server_file_hash_verified'] is not False or
  any(type(value[k]) is not int or value[k]!=0 for k in ('file_selections','add_part_clicks','submit_clicks')) or
  len(value['tasks'])!=2 or [t['index'] for t in value['tasks']]!=[1,2]):fail('CACHED_HARNESS_PRIVATE_RESULT_REQUIRED')
 for task,expected in zip(value['tasks'],spec['tasks']):
  if (task['private_draft_saved'] is not True or task['metadata_sha256']!=expected['metadata_sha256'] or task['native_readback']['title']!=expected['metadata']['title'] or
   task['native_readback']['description']!=expected['metadata']['description'] or task['native_readback']['source']!=spec['source_url'] or
   task['native_surface']['actual_inner_count']!=1 or task['native_surface']['native_upload_complete'] is not True):fail('CACHED_HARNESS_SAVED_TASK_REQUIRED')
  check_ref(task['screenshot'],10_000_000)
 return result

def arguments(argv=None):
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--owner-reference');p.add_argument('--max-seconds',type=int,default=600)
 p.add_argument('--run',action='store_true');p.add_argument('--plan');p.add_argument('--plan-sha256')
 p.add_argument('--check-fence');p.add_argument('--fence-sha256');a=p.parse_args(argv)
 if a.check_fence:
  if a.run or a.plan or a.plan_sha256 or not re.fullmatch('[a-f0-9]{64}',a.fence_sha256 or ''):p.error('exact fence read only')
 elif a.run:
  if not a.plan or not re.fullmatch('[a-f0-9]{64}',a.plan_sha256 or ''):p.error('--run requires the exact saved --plan and --plan-sha256')
 elif a.plan or a.plan_sha256 or not a.owner_reference:p.error('default plan requires --owner-reference')
 return a
def main(argv=None):
 a=arguments(argv)
 if os.name!='posix' or os.getuid()!=10001 or os.getgid()!=10001:fail('CACHED_HARNESS_APP_UID_REQUIRED')
 if a.check_fence:
  raw=read(a.check_fence,2_000_000)
  if sha(raw)!=a.fence_sha256:fail('CACHED_HARNESS_FENCE_CHANGED')
  print(json.dumps(check_fence(ROOT,json.loads(raw))));return
 script=safe(Path(__file__).resolve())
 protected(script)
 with four_locks(ROOT):
  profile(ROOT)
  if not a.run:
   output=safe(ROOT/'media'/JOB/'cached-separate-private-v5'/str(uuid.uuid4()));output.mkdir(mode=0o700,parents=True)
   spec,fence=build_plan(ROOT,output,a.owner_reference,a.max_seconds,script)
   spec_ref=write(output/'spec.json',spec);fence['small_refs'].append(spec_ref);fence_ref=write(output/'fence.json',fence)
   plan={'version':'cached-private-maintenance-plan-v5','root':str(ROOT),'output':str(output),'spec':spec_ref,'fence':fence_ref}
   saved=write(output/'plan.json',plan);print(json.dumps({'state':'verified_plan_only','plan':saved,'browser_started':False,'database_modified':False}));return
  raw=read(a.plan)
  if sha(raw)!=a.plan_sha256:fail('CACHED_HARNESS_PLAN_CHANGED')
  plan=json.loads(raw);output=safe(Path(plan['output']))
  if (plan['version']!='cached-private-maintenance-plan-v5' or plan['root']!=str(ROOT) or output.parent!=ROOT/'media'/JOB/'cached-separate-private-v5' or
   str(uuid.UUID(output.name))!=output.name or Path(a.plan)!=output/'plan.json' or plan['spec']['file']!=str(output/'spec.json') or plan['fence']['file']!=str(output/'fence.json')):fail('CACHED_HARNESS_PLAN_SCOPE_CHANGED')
  check_ref(plan['spec']);check_ref(plan['fence'],2_000_000);fence=json.loads(read(plan['fence']['file'],2_000_000));check_fence(ROOT,fence)
  if profile_owners(ROOT):fail('CACHED_HARNESS_PROFILE_ALREADY_OWNED')
  remaining=fence['deadline_monotonic']-time.monotonic()
  env={k:os.environ[k] for k in ('PATH','HOME','LANG','LC_ALL','TMP','TEMP','TMPDIR','XDG_CONFIG_HOME','XDG_CACHE_HOME','PLAYWRIGHT_BROWSERS_PATH') if k in os.environ}
  process=subprocess.Popen(['node','--input-type=module','-e',JS,plan['spec']['file'],plan['fence']['file'],plan['fence']['sha256'],str(script),sys.executable,str(remaining)],
   cwd=ROOT,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf-8',start_new_session=True)
  owned=OwnedChild(process);stdout='';timed_out=False
  try:
   try:stdout,_=process.communicate(timeout=remaining+5)
   except subprocess.TimeoutExpired:timed_out=True
  finally:
   left=sorted(set(owned.close()+profile_owners(ROOT)))
   if left:write(output/('cleanup-blocked-'+str(uuid.uuid4())+'.json'),{'state':'holding_locks_for_owned_browser_exit','remaining_owned_processes':left})
   # If a kernel-stuck owned process survives SIGKILL, keep the profile locks
   # until it is actually gone. Never release them over a living Chromium.
   while left:owned.signal_owned(signal.SIGKILL);time.sleep(1);left=sorted(set(owned.live()+profile_owners(ROOT)))
   owned.stop.set();owned.thread.join(timeout=1)
  db_unchanged=snapshot(ROOT)==fence['snapshot']
  report={'version':'cached-private-maintenance-run-v5','state':'private_save_unknown_or_blocked','timed_out':timed_out,
   'database_unchanged':db_unchanged,'remaining_owned_processes':0,'fake_lease':False,'public_submission':False,'file_selection':False,
   'node_exit_code':process.returncode,'owner_reference':fence['owner_reference'],'spec':plan['spec'],'fence':plan['fence']}
  if not timed_out and process.returncode==0 and db_unchanged:
   lines=stdout.strip().splitlines();result=json.loads(lines[-1]) if lines else {};spec=json.loads(read(plan['spec']['file']))
   report.update(validate_result(output,spec,result))
  receipt=write(output/('harness-result-'+str(uuid.uuid4())+'.json'),report)
  print(json.dumps({'state':report['state'],'report':receipt,'database_unchanged':db_unchanged,'remaining_owned_processes':0}))
  if report['state']!='two_separate_private_drafts_saved' or not db_unchanged:raise SystemExit(2)
if __name__=='__main__':main()
