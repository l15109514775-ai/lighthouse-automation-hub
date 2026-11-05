"""One scoped compatibility maintenance: two untouched metadata-only candidates.

Default plan, then exact plan hash apply. No job claim, download, publication,
rights rewrite, counter reset or schema initialization is available.
"""
import argparse,hashlib,json,os,re,sqlite3,stat,time,uuid
from contextlib import contextmanager
from pathlib import Path
from services.hub.config import Settings
from services.hub.db import encode
from services.hub.locks import resource_guard
from services.hub.replay_fifo_v4 import ReplayFIFOV4,POLICY_FILE,UID,sha

VERSION='replay-fifo-legacy-metadata-rebind-v1'
OLD_SHA='e905bfc67be59aaf033fdcf25e9426c819b161d229ce500289f07ec52bf07b5a'
CURRENT_SHA='d41275f10d1cbcaebdde73d3e3def715f9196dff92086077a5a6502e97731ce0'
ALLOW={
 '6wHqfDmNF0U':('722c1f28-22d7-4683-8648-feafff1eeecb','914ab0e9-566e-4eff-b158-6daea33ec17c'),
 'YsSXSIYtCoE':('38bd13fb-8737-4250-82da-c04504311a43','c4e49b30-69b5-4e2f-949e-6e8ec1cde09e')}
LEGACY_DURATION_REVISIONS={
 '6wHqfDmNF0U':{'legacy_seconds':24636.0,'candidate_seconds':24628.0,'legacy_raw_sha256':'6ba7fc9143eb45b285b258bea1220a52c52b806ad9c6cec649e396d3c4e2cbfe'},
 'YsSXSIYtCoE':{'legacy_seconds':24262.0,'candidate_seconds':24232.0,'legacy_raw_sha256':'044022223885ec36e6be6f96976c8ceb197cb44b84988f1566707f5bcaaf828d'}}
PAYLOAD_KEYS={'channel_id','duration','published_at','raw','source_api','title','url','video_id'}
def fail(code='REPLAY_METADATA_REBIND_REFUSED'):raise RuntimeError(code)
def digest(raw):return hashlib.sha256(raw).hexdigest()
def safe(file):
 if file.resolve()!=file or any(p.is_symlink() for p in (file,*file.parents)):fail('REPLAY_METADATA_REBIND_PATH_INVALID')
 return file
def read(file,maximum=2_000_000):
 safe(file);fd=os.open(file,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
 with os.fdopen(fd,'rb') as stream:
  a=os.fstat(stream.fileno());raw=stream.read(maximum+1);b=os.fstat(stream.fileno());c=file.lstat()
  # Production is Linux-only. Windows 3.13 fstat/path stat disagree on the
  # deprecated creation-time alias; owned tests still check inode/size/mtime.
  keys=('st_dev','st_ino','st_size','st_mtime_ns')+(('st_ctime_ns',) if os.name=='posix' else ())
  if not stat.S_ISREG(a.st_mode) or a.st_nlink!=1 or not 0<len(raw)==a.st_size<=maximum or any(getattr(a,k)!=getattr(v,k) for v in (b,c) for k in keys):fail()
 return raw
def asset_files(settings,jobs):
 files={}
 for job in jobs:
  folder=safe(settings.root/'media'/job['job_id'])
  if not folder.exists():files[job['job_id']]=None;continue
  if not folder.is_dir() or {p.name for p in folder.iterdir()}!={'metadata.json'}:fail('REPLAY_METADATA_REBIND_ASSET_PRESENT')
  file=folder/'metadata.json';raw=read(file,1_000_000)
  if json.loads(raw)!=json.loads(job['payload']):fail('REPLAY_METADATA_REBIND_METADATA_FILE_CHANGED')
  files[job['job_id']]={'file':str(file),'bytes':len(raw),'sha256':digest(raw)}
 for source,(candidate,_) in ALLOW.items():
  for token in (source,candidate):
   if (settings.root/'media'/token).exists() or (settings.root/'media'/token).is_symlink():fail('REPLAY_METADATA_REBIND_ASSET_PRESENT')
 return files
def historical_identity(q,p,payload,official):
 # This constructs an identity-only view, never a fresh API observation or
 # a persisted row. The actual current official row is verified separately.
 if not isinstance(payload,dict) or set(payload)!=PAYLOAD_KEYS:fail()
 raw=payload['raw'];view=dict(official)
 view.update(video_id=payload['video_id'],channel_id=payload['channel_id'],title=payload['title'],
  published_at=payload['published_at'],duration=payload['duration'],url=payload['url'],source_api=payload['source_api'],
  metadata=encode(raw),actual_start=raw['liveStreamingDetails']['actualStartTime'],actual_end=raw['liveStreamingDetails']['actualEndTime'])
 return q._source(view,p)[0]
def snapshot(settings,db,*,now=None):
 now=time.time() if now is None else now;q=ReplayFIFOV4(settings,None,clock=lambda:now);p=q.policy()
 if p['_sha256']!=CURRENT_SHA:fail('REPLAY_METADATA_REBIND_CURRENT_POLICY_CHANGED')
 if db.execute("SELECT 1 FROM jobs WHERE queue='video' AND (state='processing' OR lease_owner IS NOT NULL OR lease_until IS NOT NULL) LIMIT 1").fetchone():fail('REPLAY_METADATA_REBIND_VIDEO_ACTIVE')
 candidates=[];jobs=[];officials=[];revisions={}
 for source,(candidate_id,legacy_id) in ALLOW.items():
  c=db.execute('SELECT * FROM replay_fifo_v4 WHERE candidate_id=?',(candidate_id,)).fetchone()
  j=db.execute('SELECT * FROM jobs WHERE job_id=?',(legacy_id,)).fetchone()
  o=db.execute('SELECT * FROM youtube_videos WHERE video_id=?',(source,)).fetchone()
  if not c or not j or not o:fail('REPLAY_METADATA_REBIND_SCOPE_MISSING')
  c,j,o=dict(c),dict(j),dict(o)
  if (c['source_id']!=source or c['target_uid']!=UID or c['state']!='registered' or c['reason'] is not None or c['policy_sha256']!=OLD_SHA or
      any(c[k] is not None for k in ('plan_job_id','download_job_id','publication_job_id','refresh_job_id')) or
      len(db.execute('SELECT 1 FROM replay_fifo_v4 WHERE source_id=?',(source,)).fetchall())!=1):fail('REPLAY_METADATA_REBIND_CANDIDATE_STARTED')
  expected={'disposition':'metadata_only','download_performed':False,'publish_performed':False,'rights_authorized':False,'url':'https://www.youtube.com/watch?v='+source}
  result=json.loads(j['result']);payload=json.loads(j['payload'])
  if (j['source_platform']!='youtube' or j['source_id']!=source or j['job_type']!='video_process' or j['queue']!='video' or
      j['policy_version']!='machine-tweet-v3-api' or j['state']!='completed' or j['stage']!='done' or j['error_code'] is not None or
      j['external_publish_id'] is not None or j['lease_owner'] is not None or j['lease_until'] is not None or type(j['attempts']) is not int or j['attempts']<1 or
      set(result)!=set(expected) or any(result[k]!=v or type(result[k]) is not type(v) for k,v in expected.items())):fail('REPLAY_METADATA_REBIND_NOT_METADATA_ONLY')
  related=db.execute("""SELECT job_id FROM jobs WHERE source_id=? OR json_extract(payload,'$.video_id')=?
      OR json_extract(payload,'$.source_id')=? OR json_extract(payload,'$.raw.id')=? OR json_extract(payload,'$.candidate_id')=?""",(source,source,source,source,candidate_id)).fetchall()
  if [r['job_id'] for r in related]!=[legacy_id]:fail('REPLAY_METADATA_REBIND_OTHER_JOB_PRESENT')
  identity,_=q._source(o,p);original=json.loads(c['source_payload'])
  legacy_identity=historical_identity(q,p,payload,o)
  if (sha(identity)!=c['identity_sha256'] or c['channel_id']!=identity['channel_id'] or c['actual_start']!=identity['actual_start'] or c['actual_end']!=identity['actual_end'] or
      historical_identity(q,p,original,o)!=identity or
      c['source_metadata_sha256']!=digest(encode(original['raw']).encode()) or o.get('replay_job_id') not in (None,legacy_id)):fail('REPLAY_METADATA_REBIND_SOURCE_IDENTITY_CHANGED')
  if legacy_identity!=identity:
   known=LEGACY_DURATION_REVISIONS[source];legacy_raw_sha=digest(encode(payload['raw']).encode())
   # Only these two observed metadata-only duration revisions are compatible.
   # Every identity/date/privacy field remains exact, and the actual candidate
   # and current official duration still must match their original identity.
   if (legacy_identity!={**identity,'duration':known['legacy_seconds']} or identity['duration']!=known['candidate_seconds'] or
       legacy_raw_sha!=known['legacy_raw_sha256']):fail('REPLAY_METADATA_REBIND_SOURCE_IDENTITY_CHANGED')
   revisions[source]={**known,'legacy_job_id':legacy_id,'legacy_record_preserved':True,'origin':'actual_readonly_identity_diff_20261102'}
  candidates.append(c);jobs.append(j);officials.append(o)
 # Any durable association outside these three original row types is a stop,
 # including future asset/resource tables with explicit source/job references.
 tokens=list(ALLOW)+[v for pair in ALLOW.values() for v in pair]
 reference_columns={'source_id','video_id','job_id','candidate_id','parent_job_id','download_job_id','child_job_id','parent_publish_job_id'}
 for item in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
  name=item['name']
  if name in ('jobs','youtube_videos','replay_fifo_v4','audit','events') or name.startswith('sqlite_'):continue
  if not re.fullmatch('[A-Za-z0-9_]+',name):fail()
  columns=[r['name'] for r in db.execute('PRAGMA table_info("'+name+'")') if r['name'] in reference_columns]
  for col in columns:
   if db.execute('SELECT 1 FROM "'+name+'" WHERE "'+col+'" IN ('+','.join('?' for _ in tokens)+') LIMIT 1',tokens).fetchone():fail('REPLAY_METADATA_REBIND_HISTORY_PRESENT')
 return {'candidates':candidates,'legacy_duration_revisions':revisions,'legacy_jobs_sha256':{r['job_id']:sha(r) for r in jobs},
         'official_rows_sha256':{r['video_id']:sha(r) for r in officials},'metadata_files':asset_files(settings,jobs)}
@contextmanager
def connection(settings,*,write=False):
 file=settings.db_path;safe(file)
 db=sqlite3.connect(str(file) if write else file.as_uri()+'?mode=ro',uri=not write,timeout=15,isolation_level=None);db.row_factory=sqlite3.Row
 try:
  db.execute('PRAGMA foreign_keys=ON');db.execute('PRAGMA busy_timeout=15000')
  if write:db.execute('PRAGMA synchronous=FULL')
  else:db.execute('PRAGMA query_only=ON')
  db.execute('BEGIN IMMEDIATE' if write else 'BEGIN');yield db
  if write:db.commit()
 except BaseException:db.rollback();raise
 finally:db.close()
def make_plan(settings,reference,*,now=None):
 now=time.time() if now is None else now
 if not isinstance(reference,str) or not 0<len(reference.strip())<=2048:fail()
 with resource_guard(settings.root/'data/locks/replay-fifo-dispatch-v4'):
  with connection(settings) as db:value=snapshot(settings,db,now=now)
 return {'version':VERSION,'plan_id':str(uuid.uuid4()),'observed_at':now,'expires_at':now+300,'maintenance_reference':reference,
         'old_policy_sha256':OLD_SHA,'new_policy_sha256':CURRENT_SHA,'snapshot':value,'human_review':False,'authorization_changed':False}
def apply_plan(settings,plan,checksum,*,now=None):
 now=time.time() if now is None else now
 if (digest(encode(plan).encode())!=checksum or plan.get('version')!=VERSION or plan.get('old_policy_sha256')!=OLD_SHA or plan.get('new_policy_sha256')!=CURRENT_SHA or
     plan.get('human_review') is not False or plan.get('authorization_changed') is not False or not 0<=now-plan['observed_at']<300 or
     plan['expires_at']!=plan['observed_at']+300 or not isinstance(plan.get('maintenance_reference'),str) or not 0<len(plan['maintenance_reference'].strip())<=2048):fail('REPLAY_METADATA_REBIND_PLAN_INVALID')
 with resource_guard(settings.root/'data/locks/replay-fifo-dispatch-v4'):
  with connection(settings,write=True) as db:
   before=snapshot(settings,db,now=now)
   if before!=plan['snapshot']:fail('REPLAY_METADATA_REBIND_CAS_CHANGED')
   for c in before['candidates']:
    if db.execute("UPDATE replay_fifo_v4 SET policy_sha256=?,updated_at=? WHERE candidate_id=? AND policy_sha256=? AND updated_at=? AND state='registered' AND reason IS NULL AND plan_job_id IS NULL AND download_job_id IS NULL AND publication_job_id IS NULL AND refresh_job_id IS NULL",
       (CURRENT_SHA,now,c['candidate_id'],OLD_SHA,c['updated_at'])).rowcount!=1:fail('REPLAY_METADATA_REBIND_CAS_CHANGED')
    db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',(now,'operator:ssh-sudo',VERSION,c['candidate_id'],encode({
     'plan_sha256':checksum,'legacy_metadata_job_id':ALLOW[c['source_id']][1],'legacy_job_sha256':before['legacy_jobs_sha256'][ALLOW[c['source_id']][1]],
     'old_policy_sha256':OLD_SHA,'new_policy_sha256':CURRENT_SHA,'maintenance_reference':plan['maintenance_reference'],
     'legacy_duration_revision':before['legacy_duration_revisions'].get(c['source_id']),
     'job_rows_changed':0,'publication_rows_changed':0,'source_rows_changed':0,'registered_at_preserved':True,'source_payload_preserved':True,'human_review':False,'authorization_changed':False})))
   for c in before['candidates']:
    current=dict(db.execute('SELECT * FROM replay_fifo_v4 WHERE candidate_id=?',(c['candidate_id'],)).fetchone())
    if current!={**c,'policy_sha256':CURRENT_SHA,'updated_at':now}:fail()
   for job,expected in before['legacy_jobs_sha256'].items():
    if sha(dict(db.execute('SELECT * FROM jobs WHERE job_id=?',(job,)).fetchone()))!=expected:fail()
   unchanged=[dict(db.execute('SELECT * FROM jobs WHERE job_id=?',(job,)).fetchone()) for job in before['legacy_jobs_sha256']]
   if asset_files(settings,unchanged)!=before['metadata_files'] or ReplayFIFOV4(settings,None,clock=lambda:now).policy()['_sha256']!=CURRENT_SHA:fail('REPLAY_METADATA_REBIND_CAS_CHANGED')
 return {'version':VERSION,'state':'applied','plan_sha256':checksum,'rebound':[c['candidate_id'] for c in before['candidates']],
         'job_rows_changed':0,'publication_rows_changed':0,'source_rows_changed':0,'human_review':False,'authorization_changed':False}
def plan_folder(settings):return settings.root/'data/maintenance'/VERSION
def write_plan(settings,plan):
 folder=plan_folder(settings);safe(folder);folder.mkdir(parents=True,exist_ok=True,mode=0o700)
 if os.name=='posix' and (folder.stat().st_uid!=10001 or stat.S_IMODE(folder.stat().st_mode)!=0o700):fail()
 file=folder/(plan['plan_id']+'.json');raw=encode(plan).encode();fd=os.open(file,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
 with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
 if os.name=='posix':
  fd=os.open(folder,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)
 return file,digest(raw)
def main(argv=None):
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--maintenance-reference');p.add_argument('--apply',action='store_true');p.add_argument('--plan');p.add_argument('--sha256');a=p.parse_args(argv)
 if os.name!='posix' or os.getuid()!=10001 or os.getgid()!=10001:fail('REPLAY_METADATA_REBIND_APP_UID_REQUIRED')
 settings=Settings.load()
 if settings.root!=Path('/opt/lighthouse-hub'):fail()
 policy=safe(settings.root/'config'/POLICY_FILE)
 if any(p.stat().st_uid!=0 or p.stat().st_mode&0o022 for p in (policy,*policy.parents)):fail('REPLAY_METADATA_REBIND_POLICY_NOT_ROOT_RO')
 if not a.apply:
  if a.plan or a.sha256:fail()
  value=make_plan(settings,a.maintenance_reference);file,checksum=write_plan(settings,value)
  print(json.dumps({'state':'plan_only','plan':str(file),'sha256':checksum,'candidates':list(ALLOW),'database_writes':0}));return
 if a.maintenance_reference or not a.plan or not re.fullmatch('[a-f0-9]{64}',a.sha256 or ''):fail()
 file=Path(a.plan)
 if file.parent!=plan_folder(settings) or str(uuid.UUID(file.stem))!=file.stem or file.suffix!='.json':fail()
 item=safe(file).stat()
 if item.st_uid!=10001 or stat.S_IMODE(item.st_mode)!=0o600:fail()
 raw=read(file,4_000_000)
 if digest(raw)!=a.sha256:fail()
 value=json.loads(raw)
 if file.stem!=value['plan_id'] or encode(value).encode()!=raw:fail()
 print(json.dumps(apply_plan(settings,value,a.sha256)))
if __name__=='__main__':
 try:main()
 except Exception as e:
  code=str(e);print(json.dumps({'state':'refused','code':code if re.fullmatch('[A-Z][A-Z0-9_]{1,100}',code) else 'REPLAY_METADATA_REBIND_REFUSED'}));raise SystemExit(2)
