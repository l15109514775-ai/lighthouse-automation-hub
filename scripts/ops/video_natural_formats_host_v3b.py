"""Natural formats v3b: consistent SQLite online snapshots for read-only verification.

No upload, public submission, network client, recursive deletion or core restart.
An actual 46-case isolated Ubuntu attestation is required before install.
"""
from datetime import datetime,timezone
from pathlib import Path
import argparse,hashlib,json,os,re,stat,subprocess,uuid
import xml.etree.ElementTree as ET

ROOT=Path('/opt/lighthouse-hub');BASE=Path('/usr/local/lib/lighthouse-backup-ops/video-natural-formats-v3b')
STATE=Path('/var/lib/lighthouse-backup-ops/video-natural-formats-v3b')
PREVIOUS=Path('/usr/local/lib/lighthouse-backup-ops/video-tracks-v2b/runtime.json')
CONTROL=ROOT/'data/video-source-retention-v2'
SNAPSHOT_TARGET='/run/retention-snapshot/hub.sqlite3'
SNAPSHOT_PREFIX='lighthouse-retention-snapshot-v3b-'
IMAGE='sha256:9f19b756ca081bfe033ffa01b0face5c9cb70b3d45de08394567150ac747de6e'
ARCHIVE='d2cbbacb026f89f853c29e5bb3fc1d9a77ec58e4b69e06300abff146dc0a2994'
GROUPS={'tests.test_video_source_retention_v2':19,'tests.test_video_source_retention_tracks_v2':13,'tests.test_video_retention_scheduler_v2':6,'tests.test_video_source_retention_natural_formats_v3':8}
CORE=('services/hub/video_source_retention_v2.py','services/hub/archive_source_retention_v2.py',
 'services/hub/video_retention_scheduler_v2.py','services/hub/replay_highest_video_v2.py',
 'services/hub/replay_highest_coverage_v2.py','services/hub/replay_natural_video_v3.py',
 'services/hub/video_source_retention_natural_formats_v3.py','services/hub/youtube_natural_plan_v3.py')
PRODUCTION_FILES=(
 'adapters/media_tools.py','adapters/safe_https.py','adapters/ytdlp_replay.py',
 'scripts/video-source-retention-v2.py','services/hub/archive_source_retention_v2.py',
 'services/hub/budget.py','services/hub/config.py','services/hub/cover_normalizer_v4.py',
 'services/hub/db.py','services/hub/errors.py','services/hub/evidence.py','services/hub/locks.py',
 'services/hub/maintenance.py','services/hub/media_processing.py','services/hub/outcomes.py',
 'services/hub/replay_highest_coverage_v2.py','services/hub/replay_highest_limits_v2.py',
 'services/hub/replay_highest_video_v2.py','services/hub/replay_machine_checks.py',
 'services/hub/replay_natural_launch_v3.py','services/hub/replay_natural_video_v3.py',
 'services/hub/replay_video_browser.py','services/hub/replay_video_cover.py','services/hub/replay_video_web.py',
 'services/hub/short_video_web.py','services/hub/video_retention_scheduler_v2.py',
 'services/hub/video_source_retention_v2.py','services/hub/youtube_download.py',
 'services/hub/youtube_download_v3.py','services/hub/youtube_highest_plan_v2.py',
 'services/hub/youtube_natural_plan_v3.py','services/hub/video_source_retention_natural_formats_v3.py',
 'vendor/subtitle-cloud-worker/observer/store.py')
RELEASE=ROOT/'config/video-source-retention-natural-formats-v3-release.json'
OWNER=ROOT/'config/video-source-retention-tracks-owner-v2.json'
SERVICE=Path('/etc/systemd/system/lighthouse-video-natural-formats-retention-v3b.service')
TIMER=Path('/etc/systemd/system/lighthouse-video-natural-formats-retention-v3b.timer')

def sha(raw):return hashlib.sha256(raw).hexdigest()
def safe(path):
 path=Path(path);assert path.is_absolute() and path.resolve()==path and not any(p.is_symlink() for p in (path,*path.parents));return path
def protected(path):
 path=safe(path)
 for p in (path,*path.parents):assert p.stat().st_uid==0 and p.stat().st_mode&0o022==0
 return path
def mkdir(path,mode=0o755,gid=0):
 safe(path)
 if not path.exists():protected(path.parent);path.mkdir(mode=mode);os.chown(path,0,gid);path.chmod(mode)
 protected(path);assert path.is_dir()
def write(path,raw,mode=0o644,gid=0):
 safe(path);protected(path.parent)
 if path.exists():protected(path);assert path.is_file()
 temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.partial')
 fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
 with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fchmod(f.fileno(),mode);os.fchown(f.fileno(),0,gid);os.fsync(f.fileno())
 os.replace(temp,path);fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
 try:os.fsync(fd)
 finally:os.close(fd)
def uid(value):return isinstance(value,str) and str(uuid.UUID(value))==value
def command(*args,timeout=90):return subprocess.check_output(args,text=True,timeout=timeout)
def dump(value):return (json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode()

def sha256_argument(value):
 if not isinstance(value,str) or re.fullmatch('[a-f0-9]{64}',value) is None:
  raise argparse.ArgumentTypeError('SHA256 must contain exactly 64 lowercase hexadecimal characters; use the raw receipt, not terminal-wrapped text')
 return value

def verify_proof(proof,expected_sha):
 sha256_argument(expected_sha)
 protected(proof);raw=(proof/'test-attestation.json').read_bytes();assert sha(raw)==expected_sha
 value=json.loads(raw);assert value['version']=='video-source-retention-natural-formats-v3-ubuntu-tests-v1'
 assert value['image']==IMAGE and value['archive_sha256']==ARCHIVE and value['tests']==46
 assert value['exit_code']==value['failures']==value['errors']==value['skipped']==0 and value['oom_killed'] is False
 assert value['network']=='none' and value['application_data_mounted'] is value['production_profile_mounted'] is value['secrets_environment_injected'] is False
 for key in ('full_suite_passed','no_pid_or_oom_denial','runtime_matches_declared_candidate','mounts_match_isolated_allowlist'):assert value[key] is True
 junit=proof/'accepted-results.xml';protected(junit);jraw=junit.read_bytes()
 assert value['junit_file']==str(junit) and sha(jraw)==value['junit_sha256']
 cases=ET.fromstring(jraw).findall('.//testcase');groups={}
 for case in cases:
  groups[case.get('classname')]=groups.get(case.get('classname'),0)+1
  assert all(case.find(tag) is None for tag in ('failure','error','skipped'))
 assert len(cases)==46 and groups==GROUPS
 manifest=value['manifest'];assert manifest['version']=='video-natural-formats-ro-overlay-v3' and len(manifest['files'])==95
 assert manifest['retention_commit']=='520da5d20fd382dc8d7e951d99e81cad5dfd972a'
 assert manifest['parent_archive_sha256']=='51b6f980f25bfe18da248625939873dc33078125ecbe9c2cd49b7624aa9f15ff'
 assert manifest['parent_actual_38_attestation_sha256']=='deb675fc049a5639d445d85d5039e571eb361ea2b54e11b711b3a992057e6a71'
 assert manifest['whole_tree_deployment'] is manifest['application_data_included'] is manifest['credentials_included'] is manifest['production_execution'] is False
 assert manifest['highest_v2_identifier_contract_unchanged'] is manifest['natural_v3_producer_IDENT_reused'] is True
 assert set(value['source_files'])==set(CORE) and all(value['source_files'][n]==manifest['files'][n]['sha256'] for n in CORE)
 bodies={}
 for name,item in manifest['files'].items():
  path=Path(name);assert not path.is_absolute() and '..' not in path.parts and str(path.as_posix())==name
  file=proof/'overlay'/name;protected(file);body=file.read_bytes()
  assert len(body)==item['bytes'] and 0<len(body)<500000 and sha(body)==item['sha256'];bodies[name]=body
 return raw,jraw,value,bodies

def owner_request(raw):
 assert 0<len(raw)<=16384;value=json.loads(raw)
 assert set(value)=={'actual_owner_response','owner_response_reference'}
 assert all(isinstance(v,str) and v.strip() and len(v)<=2048 for v in value.values())
 return value

def validate_prior_runtime(value,owner_sha256):
 assert value['archive_sha256']=='51b6f980f25bfe18da248625939873dc33078125ecbe9c2cd49b7624aa9f15ff'
 assert value['proof_sha256']=='deb675fc049a5639d445d85d5039e571eb361ea2b54e11b711b3a992057e6a71'
 assert value['owner_request_sha256']==owner_sha256 and uid(value['authorization_id'])
 return value['authorization_id']

def production_sources(manifest):
 assert len(PRODUCTION_FILES)==len(set(PRODUCTION_FILES))==33
 assert set(PRODUCTION_FILES)<=set(manifest['files'])
 assert all(name.endswith('.py') and not name.startswith(('tests/','config/')) for name in PRODUCTION_FILES)
 return {name:manifest['files'][name] for name in PRODUCTION_FILES}

def configuration_sources():
 return tuple(file for file in (ROOT/'config/hub.toml',RELEASE) if file.exists())

READ_ONLY_STORE=r'''
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from services.hub.db import Store
class ReadOnlyStore(Store):
 def __init__(self,path):
  # Never put an immutable connection on the live DB or a copy beside live WAL.
  assert Path(path)==Path('/opt/lighthouse-hub/data/hub.sqlite3')
  self.path=Path('/run/retention-snapshot/hub.sqlite3')
  assert self.path.is_file()
 def connect(self):
  db=sqlite3.connect(self.path.as_uri()+'?mode=ro&immutable=1',uri=True,timeout=15,isolation_level=None)
  db.row_factory=sqlite3.Row
  db.execute('PRAGMA query_only=ON');db.execute('PRAGMA busy_timeout=15000')
  return db
 @contextmanager
 def transaction(self):
  db=self.connect()
  try:
   db.execute('BEGIN');yield db
  finally:db.rollback();db.close()
'''

ONLINE_SNAPSHOT=r'''
from pathlib import Path
import hashlib,json,os,re,sqlite3,sys,time
assert os.getuid()==os.getgid()==10001
source=target=None;phase='source_open'
try:
 source=sqlite3.connect(Path(sys.argv[1]).as_uri()+'?mode=ro',uri=True,timeout=15)
 source.execute('PRAGMA query_only=ON');phase='online_backup';target=sqlite3.connect(sys.argv[2])
 deadline=time.monotonic()+45
 def progress(status,remaining,total):
  if time.monotonic()>deadline:raise RuntimeError('ONLINE_SNAPSHOT_TIMEOUT')
 source.backup(target,pages=256,sleep=.01,progress=progress)
 phase='integrity_check';assert target.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
 assert not target.execute('PRAGMA foreign_key_check').fetchall()
 assert target.execute('PRAGMA journal_mode=DELETE').fetchone()[0]=='delete';target.commit()
 target.close();target=None;source.close();source=None
 file=Path(sys.argv[2]);assert file.is_file() and not file.is_symlink() and 0<file.stat().st_size<=100_000_000
 fd=os.open(file,os.O_RDONLY)
 try:os.fchmod(fd,0o600);os.fsync(fd)
 finally:os.close(fd)
 with file.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
 result={'state':'consistent_snapshot_created','sha256':digest,'bytes':file.stat().st_size,'observed_at':time.time(),
  'online_sqlite_backup':True,'integrity_check':'ok','foreign_key_check':True,'journal_mode':'delete',
  'immutable_applies_to_backup_only':True,'source_rows_modified':False}
except Exception as exc:
 name=type(exc).__name__;name=name if re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,63}',name) else 'UnknownException'
 result={'state':'snapshot_failed','phase':phase,'exception_class':name,'raw_message_included':False}
 for key in ('sqlite_errorcode','errno'):
  value=getattr(exc,key,None)
  if type(value) is int and 0<=value<2**31:result[key]=value
 value=getattr(exc,'sqlite_errorname',None)
 if isinstance(value,str) and re.fullmatch('SQLITE_[A-Z0-9_]{1,64}',value):result['sqlite_errorname']=value
finally:
 if target is not None:target.close()
 if source is not None:source.close()
print(json.dumps(result))
sys.exit(0 if result['state']=='consistent_snapshot_created' else 2)
'''

SAFE_APP=r'''
import json,re,sys,traceback
payload=sys.argv.pop(1)
try:exec(compile(payload,'<retention_app>','exec'),{'__name__':'__main__'})
except Exception as exc:
 name=type(exc).__name__;name=name if re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,63}',name) else 'UnknownException'
 value={'state':'controlled_app_failed','exception_class':name,'raw_message_included':False}
 for key in ('sqlite_errorcode','errno'):
  item=getattr(exc,key,None)
  if type(item) is int and 0<=item<2**31:value[key]=item
 item=getattr(exc,'sqlite_errorname',None)
 if isinstance(item,str) and re.fullmatch('SQLITE_[A-Z0-9_]{1,64}',item):value['sqlite_errorname']=item
 item=getattr(exc,'code',None)
 if isinstance(item,str) and re.fullmatch('[A-Z][A-Z0-9_]{0,100}',item):value['error_code']=item
 frames=[]
 for frame in traceback.extract_tb(exc.__traceback__)[-12:]:
  file=frame.filename
  if file in ('<string>','<retention_app>'):label='controlled_app'
  elif file.startswith('/opt/lighthouse-hub/services/hub/'):label='services/hub/'+file.rsplit('/',1)[-1]
  elif '/sqlite3/' in file:label='stdlib/sqlite3'
  else:label='other_controlled_frame'
  function=frame.name if re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,63}',frame.name) else 'controlled_call'
  frames.append({'file':label,'function':function,'line':frame.lineno})
 value['frames']=frames;print(json.dumps(value));sys.exit(2)
'''

def wrapped_arguments(arguments):
 # Existing code/CLI is fixed. Only structured, bounded exception metadata is
 # returned; never the raw traceback, SQL text, provider body or environment.
 assert arguments[0]=='python'
 if arguments[1:2]==['-c']:code=arguments[2];tail=arguments[3:]
 else:
  assert arguments[1]==str(ROOT/'scripts/video-source-retention-v2.py')
  code="import runpy,sys;file=sys.argv.pop(1);runpy.run_path(file,run_name='__main__')";tail=arguments[1:]
 return ['python','-c',SAFE_APP,code,*tail]

def create_snapshot(directory):
 safe(directory);assert directory.parent==Path('/run') and directory.name.startswith(SNAPSHOT_PREFIX) and uid(directory.name[len(SNAPSHOT_PREFIX):])
 protected(directory.parent);assert not directory.exists();directory.mkdir(mode=0o700)
 os.chown(directory,10001,10001);directory.chmod(0o700);file=directory/'hub.sqlite3'
 def app():os.setgroups([]);os.setgid(10001);os.setuid(10001)
 try:
  result=subprocess.run(['/usr/bin/python3','-c',ONLINE_SNAPSHOT,str(ROOT/'data/hub.sqlite3'),str(file)],
   capture_output=True,text=True,timeout=60,preexec_fn=app,cwd='/tmp',
   env={'PATH':'/usr/bin:/bin','HOME':'/tmp','PYTHONNOUSERSITE':'1'})
 except (OSError,subprocess.TimeoutExpired) as exc:
  os.chown(directory,0,0);directory.chmod(0o700)
  return None,{'state':'snapshot_failed','exception_class':type(exc).__name__,'raw_message_included':False}
 assert len(result.stdout)<65536 and len(result.stderr)<65536
 try:record=json.loads(result.stdout)
 except ValueError:record={'state':'snapshot_failed','exception_class':'SnapshotOutputInvalid','raw_message_included':False}
 if result.returncode or record.get('state')!='consistent_snapshot_created':
  os.chown(directory,0,0);directory.chmod(0o700)
  return None,record
 assert sha(file.read_bytes())==sha256_argument(record['sha256']) and file.stat().st_size==record['bytes']
 os.chown(file,0,10001);file.chmod(0o640);os.chown(directory,0,10001);directory.chmod(0o750)
 protected(directory);protected(file)
 return file,record

def remove_snapshot(directory):
 safe(directory);assert directory.parent==Path('/run') and directory.name.startswith(SNAPSHOT_PREFIX) and uid(directory.name[len(SNAPSHOT_PREFIX):])
 protected(directory);allowed={'hub.sqlite3','hub.sqlite3-wal','hub.sqlite3-shm','hub.sqlite3-journal'}
 entries=list(directory.iterdir())
 assert all(file.name in allowed and not file.is_symlink() and stat.S_ISREG(file.lstat().st_mode) and file.stat().st_nlink==1 for file in entries)
 for file in entries:file.unlink()
 directory.rmdir()

READ_ONLY_MEDIA_SCRATCH=r'''
# The reviewed media helpers put only bounded stderr diagnostics in `folder`.
# A read-only plan must put those diagnostics in tmpfs, not beside the MP4.
# Commands, file arguments, SHA/packet algorithms, checks and limits stay exact.
from pathlib import Path
import adapters.media_tools as _media_tools
import adapters.ytdlp_replay as _replay_tools
_original_process_lines=_media_tools.process_lines
def _readonly_media_lines(command,folder,timeout,check=lambda:None):
 return _original_process_lines(command,Path('/tmp'),timeout,check)
_media_tools.process_lines=_readonly_media_lines
_replay_tools.process_lines=_readonly_media_lines
'''

def data_mounts(apply):
 assert type(apply) is bool
 # Read-only plans need the same real OS locks as the writer. Only control
 # lock files may change; main SQLite and original media remain read-only.
 mounts=[(ROOT/'data',ROOT/'data',apply),(ROOT/'media',ROOT/'media',apply)]
 if not apply:mounts.extend(((ROOT/'data/locks',ROOT/'data/locks',True),(CONTROL,CONTROL,True)))
 return mounts

def _app_run(config,arguments,*,media_write=False,input_plan=None,snapshot=None,timeout=7200):
 manifest=config['manifest'];runtime=BASE/'runtime'
 assert command('/usr/bin/docker','image','inspect','-f','{{.Id}}',IMAGE).strip()==IMAGE
 for name,item in production_sources(manifest).items():
  file=runtime/name;protected(file);assert sha(file.read_bytes())==item['sha256']
 container_name='lighthouse-video-retention-'+uuid.uuid4().hex[:10]
 cmd=['/usr/bin/docker','run','--pull=never','--rm','--init','--name',container_name,
  '--network','none','--read-only','--user','10001:10001','--cap-drop','ALL','--security-opt','no-new-privileges:true',
  '--memory','512m','--cpus','1','--pids-limit','96','--tmpfs','/tmp:rw,size=96m,mode=1777',
  '--tmpfs',str(ROOT/'data/browser-profiles')+':rw,size=1024,mode=0700,uid=10001,gid=10001',
  '-e','HUB_ROOT='+str(ROOT),'-e','PYTHONDONTWRITEBYTECODE=1','-e','PYTHONNOUSERSITE=1']
 bind_targets=set()
 def mount(source,target,*,writable=False):
  target=str(target);assert target not in bind_targets,'DUPLICATE_PRODUCTION_MOUNT_TARGET'
  bind_targets.add(target)
  cmd.extend(['--mount','type=bind,src='+str(source)+',dst='+target+('' if writable else ',readonly')])
 for source,target,writable in data_mounts(media_write):mount(source,target,writable=writable)
 # Only these harmless individual configuration files are mounted. No env-file,
 # account secret, browser profile, backup archive or API credential is mounted.
 for file in configuration_sources():protected(file);mount(file,file)
 mount(STATE/'readiness',config['proof_directory'])
 if snapshot is not None:
  assert media_write is False;protected(snapshot);mount(snapshot,SNAPSHOT_TARGET)
 if input_plan is not None:
  assert media_write is True;protected(input_plan);mount(input_plan,'/run/retention-input-plan.json')
 for name in production_sources(manifest):mount(runtime/name,ROOT/name)
 try:r=subprocess.run(cmd+[IMAGE,*wrapped_arguments(arguments)],capture_output=True,text=True,timeout=timeout,stdin=subprocess.DEVNULL)
 except subprocess.TimeoutExpired:
  stopped=subprocess.run(['/usr/bin/docker','stop','--time','30',container_name],capture_output=True,text=True,timeout=90)
  return {'version':'video-natural-formats-host-result-v3b','at':datetime.now(timezone.utc).isoformat(),'exit_code':124,
   'image':IMAGE,'network':'none','credentials_mounted':False,'profile_mounted':False,'metadata_config_only':True,
   'production_code_files_mounted':33,'test_or_example_configuration_mounted':False,'mount_targets_unique':True,
   'main_database_writable':media_write,'control_locks_writable':True,'natural_policy_or_grant_mounted':False,
   'media_writable':media_write,'container_name':container_name,'graceful_stop_returncode':stopped.returncode,
   'result':{'state':'operation_timeout_reconciliation_required','files_deleted_unknown':True},'stderr_bytes':0}
 assert len(r.stdout)<2_000_000 and len(r.stderr)<200_000
 try:value=json.loads(r.stdout)
 except ValueError:value={'state':'app_result_not_json','stderr_bytes':len(r.stderr)}
 return {'version':'video-natural-formats-host-result-v3b','at':datetime.now(timezone.utc).isoformat(),
  'exit_code':r.returncode,'image':IMAGE,'network':'none','credentials_mounted':False,'profile_mounted':False,
  'metadata_config_only':True,'production_code_files_mounted':33,'test_or_example_configuration_mounted':False,
  'mount_targets_unique':True,'main_database_writable':media_write,'control_locks_writable':True,
  'natural_policy_or_grant_mounted':False,'media_writable':media_write,'result':value,'stderr_bytes':len(r.stderr)}

def app_run(config,arguments,*,media_write=False,input_plan=None,timeout=7200):
 if media_write:return _app_run(config,arguments,media_write=True,input_plan=input_plan,timeout=timeout)
 assert input_plan is None
 directory=Path('/run')/(SNAPSHOT_PREFIX+str(uuid.uuid4()))
 file,record=create_snapshot(directory)
 if file is None:
  return {'version':'video-natural-formats-host-result-v3b','exit_code':2,'image':IMAGE,'network':'none',
   'credentials_mounted':False,'profile_mounted':False,'main_database_writable':False,'media_writable':False,
   'result':record,'snapshot_preserved_for_reconciliation':str(directory)}
 try:
  result=_app_run(config,arguments,snapshot=file,timeout=timeout)
  result['database_snapshot']={**record,'target':SNAPSHOT_TARGET,'live_database_path_overlay':False}
  assert sha(file.read_bytes())==record['sha256']
  return result
 finally:remove_snapshot(directory)

def cli(*args):return ['python',str(ROOT/'scripts/video-source-retention-v2.py'),'--include-source-tracks','--natural-format-ids-v3',*args]
def verify_release(config):
 code=READ_ONLY_STORE+"\nfrom services.hub.config import Settings;from services.hub.video_source_retention_natural_formats_v3 import NaturalFormatVideoSourceRetentionV3;from services.hub.video_source_retention_v2 import PUBLISHED;import sys,json;s=Settings.load();r=NaturalFormatVideoSourceRetentionV3(s,ReadOnlyStore(s.db_path));a=r._authorization(sys.argv[1]);assert a['scope']==PUBLISHED;print(json.dumps({'state':'verified_not_run','release_sha256':r._release({'scope':PUBLISHED,'authorization_id':sys.argv[1],'proof':{'pipeline_state':'replay_natural_video_web_v3'}}),'external_requests':0,'files_deleted':0}))"
 return app_run(config,['python','-c',code,config['authorization_id']],timeout=90)

def plan_arguments(config,job_id):
 assert uid(job_id)
 code=READ_ONLY_STORE+READ_ONLY_MEDIA_SCRATCH+"\nfrom services.hub.config import Settings;from services.hub.video_source_retention_natural_formats_v3 import NaturalFormatVideoSourceRetentionV3;import sys,json;s=Settings.load();r=NaturalFormatVideoSourceRetentionV3(s,ReadOnlyStore(s.db_path));p=r.plan(sys.argv[1],authorization_id=sys.argv[2]);print(json.dumps({'state':'dry_run_prepared','plan':p,'local_files_deleted':0,'diagnostics_location':'/tmp tmpfs'}))"
 return ['python','-c',code,job_id,config['authorization_id']]

APPLY_SAVED_PLAN=r'''
from pathlib import Path
import json,runpy,sys
from services.hub.archive_source_retention_v2 import _atomic,_path,_uuid
from services.hub.config import Settings
from services.hub.db import Store,encode
from services.hub.video_source_retention_natural_formats_v3 import NaturalFormatVideoSourceRetentionV3
s=Settings.load();r=NaturalFormatVideoSourceRetentionV3(s,Store(s.db_path))
p=json.loads(Path('/run/retention-input-plan.json').read_bytes());assert _uuid(p['plan_id'])
r.initialize();folder=_path(s.root,r.folder/'plans');folder.mkdir(mode=0o700,exist_ok=True)
file=folder/(p['plan_id']+'.json')
if file.exists():assert json.loads(file.read_bytes())==p
else:_atomic(file,p)
sys.argv=[str(s.root/'scripts/video-source-retention-v2.py'),'--include-source-tracks','--natural-format-ids-v3','--apply-plan',str(file)]
runpy.run_path(sys.argv[0],run_name='__main__')
'''

def main():
 assert os.name=='posix' and os.geteuid()==0 and __debug__;os.umask(0o077)
 p=argparse.ArgumentParser(description=__doc__);m=p.add_mutually_exclusive_group()
 m.add_argument('--plan',action='store_true');m.add_argument('--install',action='store_true');m.add_argument('--runtime',action='store_true')
 p.add_argument('--proof',default='/var/lib/lighthouse-backup-ops/video-natural-formats-v3-tests/bdd56668-22a1-47cd-baa0-0e11d499ad5a')
 p.add_argument('--proof-sha256',type=sha256_argument,default='5824da326998a5857bdde4cc38ef605b20f870cfc4a62940f2ef7c715a80400b')
 p.add_argument('--owner-request')
 r=p.add_mutually_exclusive_group();r.add_argument('--verify-only',action='store_true');r.add_argument('--plan-job');r.add_argument('--apply-plan');r.add_argument('--run-completed',action='store_true')
 p.add_argument('--apply',action='store_true');p.add_argument('--max-jobs',type=int,default=1);a=p.parse_args()
 if a.runtime:
  protected(BASE/'runtime.json');config=json.loads((BASE/'runtime.json').read_bytes())
  assert uid(config['authorization_id']) and config['archive_sha256']==ARCHIVE
  protected(RELEASE);assert sha(RELEASE.read_bytes())==config['release_sha256']
  protected(STATE/'readiness');assert sha((STATE/'readiness/test-attestation.json').read_bytes())==config['proof_sha256']
  assert 1<=a.max_jobs<=3 and (not a.apply or a.run_completed)
  input_plan=None
  if a.plan_job:arguments=plan_arguments(config,a.plan_job)
  elif a.apply_plan:
   input_plan=safe(Path(a.apply_plan));assert input_plan.parent==STATE/'plans' and input_plan.suffix=='.json' and uid(input_plan.stem)
   protected(input_plan);assert 0<input_plan.stat().st_size<1_000_000
   arguments=['python','-c',APPLY_SAVED_PLAN]
  elif a.run_completed:
   assert a.apply,'COMPLETED_SCHEDULER_REQUIRES_EXPLICIT_APPLY'
   arguments=cli('--run-completed','--authorization-id',config['authorization_id'],'--max-jobs',str(a.max_jobs),'--apply')
  else:arguments=None
  result=verify_release(config) if arguments is None else app_run(config,arguments,media_write=bool(a.apply_plan or a.apply),input_plan=input_plan)
  if a.plan_job and result['exit_code']==0:
   plan=result['result']['plan'];assert uid(plan['plan_id']) and plan['job_id']==a.plan_job and plan['authorization_id']==config['authorization_id']
   assert sha(json.dumps({k:v for k,v in plan.items() if k!='plan_sha256'},ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())==plan['plan_sha256']
   directory=STATE/'plans';mkdir(directory,0o700);file=directory/(plan['plan_id']+'.json');write(file,dump(plan),0o640,10001)
   result['result']['plan_file']=str(file)
  directory=STATE/'runtime-history';mkdir(directory,0o700);file=directory/(str(uuid.uuid4())+'.json');write(file,dump(result),0o600)
  print(json.dumps({**result,'receipt_file':str(file)},ensure_ascii=False));return result['exit_code']
 assert not any((a.plan_job,a.apply_plan,a.run_completed,a.apply,a.verify_only))
 assert a.proof and a.proof_sha256 and a.owner_request
 proof=safe(Path(a.proof));raw,jraw,value,bodies=verify_proof(proof,a.proof_sha256)
 request_file=safe(Path(a.owner_request));protected(request_file);request_raw=request_file.read_bytes();owner_request(request_raw)
 protected(PREVIOUS);old=json.loads(PREVIOUS.read_bytes())
 authorization_id=validate_prior_runtime(old,sha(request_raw))
 protected(ROOT/'config/hub.toml');assert command('/usr/bin/docker','image','inspect','-f','{{.Id}}',IMAGE).strip()==IMAGE
 plan={'version':'video-natural-formats-install-v3b','state':'planned_not_installed','image':IMAGE,'proof_sha256':a.proof_sha256,
   'tests':46,'source_files':value['source_files'],'exact_proof_overlay_files':len(bodies),'production_code_files':33,
   'production_sources':production_sources(value['manifest']),'test_or_example_configuration_mounted':False,
   'mount_targets_unique':True,'network_requests':0,
  'authorization_id':old['authorization_id'],'existing_real_owner_authorization_reused':True,'new_authorization_rows_created':0,
  'core_restarts':0,'files_deleted':0,'timer_enable_requested':False,'completed_only':True,'unknown_and_active_retained':True,
  'natural_policy_or_grant_mounted':False,'main_database_readonly_except_apply':True}
 if not a.install:print(json.dumps(plan));return 0
 protected(BASE.parent);mkdir(BASE);mkdir(BASE/'runtime');mkdir(STATE,0o700);mkdir(STATE/'install-history',0o700)
 import fcntl
 lock=os.open(STATE/'install.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
 try:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  assert subprocess.run(['/usr/bin/systemctl','is-active','--quiet',SERVICE.name]).returncode!=0
  assert subprocess.run(['/usr/bin/systemctl','is-active','--quiet',TIMER.name]).returncode!=0
  history=STATE/'install-history'/str(uuid.uuid4());mkdir(history,0o700)
  for name in production_sources(value['manifest']):
   body=bodies[name]
   file=BASE/'runtime'/name;chain=[];parent=file.parent
   while not parent.exists():chain.append(parent);parent=parent.parent
   for path in reversed(chain):mkdir(path)
   write(file,body)
  mkdir(STATE/'readiness',0o750,10001);write(STATE/'readiness/test-attestation.json',raw,0o640,10001)
  write(STATE/'readiness/accepted-results.xml',jraw,0o640,10001)
  selfraw=Path(__file__).read_bytes();write(BASE/'run.py',selfraw)
  config={'version':'video-natural-formats-runtime-v3b','manifest':value['manifest'],'proof_directory':str(proof),
   'proof_sha256':a.proof_sha256,'archive_sha256':ARCHIVE,'owner_request_sha256':sha(request_raw),'authorization_id':old['authorization_id']}
  prior=BASE/'runtime.json'
  if prior.exists():
   protected(prior);prior_value=json.loads(prior.read_bytes());assert prior_value['owner_request_sha256']==sha(request_raw)
   assert prior_value['proof_sha256']==a.proof_sha256 and prior_value['archive_sha256']==ARCHIVE
   assert prior_value['authorization_id']==config['authorization_id']
  release={'version':'video-source-retention-release-v2','authorization_id':config['authorization_id'],
   'scope':'confirmed_full_duration_public_video','source_files':value['source_files'],
   'test_attestation':{'file':str(proof/'test-attestation.json'),'sha256':a.proof_sha256}}
  if RELEASE.exists():write(history/'previous-release.json',RELEASE.read_bytes(),0o600)
  write(RELEASE,dump(release),0o640,10001);config['release_sha256']=sha(RELEASE.read_bytes());write(BASE/'runtime.json',dump(config))
  checked=verify_release(config);write(history/'release-verification.json',dump(checked),0o600)
  assert checked['exit_code']==0 and checked['result']['state']=='verified_not_run'
  service='''[Unit]\nDescription=Bounded completed-video local source retention\nAfter=docker.service\nRequires=docker.service\n[Service]\nType=oneshot\nExecStart=/usr/bin/python3 /usr/local/lib/lighthouse-backup-ops/video-natural-formats-v3b/run.py --runtime --run-completed --apply\nTimeoutStartSec=7200\nNice=15\nIOSchedulingClass=idle\nProtectSystem=strict\nReadWritePaths=/var/lib/lighthouse-backup-ops/video-natural-formats-v3b /opt/lighthouse-hub/data /opt/lighthouse-hub/media /run\nPrivateTmp=true\nNoNewPrivileges=yes\nRestrictAddressFamilies=AF_UNIX\nRestart=no\n'''.encode()
  timer='''[Unit]\nDescription=Check completed-video local source retention every two minutes\n[Timer]\nOnBootSec=3min\nOnUnitInactiveSec=2min\nRandomizedDelaySec=10s\nUnit=lighthouse-video-natural-formats-retention-v3b.service\n[Install]\nWantedBy=timers.target\n'''.encode()
  previous={}
  for file in (SERVICE,TIMER):
   if file.exists():protected(file);previous[file]=file.read_bytes();write(history/(file.name+'.previous'),previous[file],0o600)
   else:previous[file]=None
  try:
   for file,body in ((SERVICE,service),(TIMER,timer)):write(file,body)
   command('/usr/bin/systemd-analyze','verify',str(SERVICE),str(TIMER));command('/usr/bin/systemctl','daemon-reload')
  except BaseException:
   # Exact unit files only. Never touch the previous video worker, data or media.
   for file,body in previous.items():
    if body is not None:write(file,body)
    elif file.exists():protected(file);file.unlink()
   command('/usr/bin/systemctl','daemon-reload');raise
  result={**plan,'state':'installed_not_run','authorization_id':config['authorization_id'],'release_sha256':config['release_sha256'],
   'host_runner_sha256':sha(selfraw),'timer_enabled_by_this_run':False,'timer_service_started_by_installer':False,
   'service_sha256':sha(service),'timer_sha256':sha(timer),'proof_mirror_bytes_unchanged':True}
  write(history/'result.json',dump(result),0o600);print(json.dumps({**result,'receipt_file':str(history/'result.json')}));return 0
 finally:os.close(lock)
if __name__=='__main__':raise SystemExit(main())
