"""Reproduce only release verification; print bounded structured errors, not text."""
from pathlib import Path
import hashlib,json,os,types,uuid

HOST=Path('/usr/local/lib/lighthouse-backup-ops/video-natural-formats-v3/run.py')
HOST_SHA='0e76681055ff29faa8563e9fb970f383a423892452d9301aafe2d001d8d82d73'

DIAGNOSTIC=r'''
import json,os,re,sys,traceback
from pathlib import Path
phase='imports';settings=None
try:
 from services.hub.config import Settings
 from services.hub.video_source_retention_natural_formats_v3 import NaturalFormatVideoSourceRetentionV3
 from services.hub.video_source_retention_v2 import PUBLISHED
 phase='settings';settings=Settings.load()
 phase='readonly_store';store=ReadOnlyStore(settings.db_path)
 phase='authorization_query';service=NaturalFormatVideoSourceRetentionV3(settings,store)
 authorization=service._authorization(sys.argv[1]);assert authorization['scope']==PUBLISHED
 phase='release_gate';digest=service._release({'scope':PUBLISHED,'authorization_id':sys.argv[1],
  'proof':{'pipeline_state':'replay_natural_video_web_v3'}})
 result={'state':'verified_not_run','release_sha256':digest,'phase':phase,'files_deleted':0,'external_requests':0}
except Exception as exc:
 name=type(exc).__name__;name=name if re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,63}',name) else 'UnknownException'
 result={'state':'safe_diagnostic_failure','phase':phase,'exception_class':name}
 for key in ('sqlite_errorcode','errno'):
  value=getattr(exc,key,None)
  if type(value) is int and 0<=value<2**31:result[key]=value
 value=getattr(exc,'sqlite_errorname',None)
 if isinstance(value,str) and re.fullmatch('SQLITE_[A-Z0-9_]{1,64}',value):result['sqlite_errorname']=value
 value=getattr(exc,'code',None)
 if isinstance(value,str) and re.fullmatch('[A-Z][A-Z0-9_]{0,100}',value):result['error_code']=value
 frames=[]
 for frame in traceback.extract_tb(exc.__traceback__)[-12:]:
  file=frame.filename.replace('\\','/')
  if file=='<string>':label='diagnostic_app'
  elif file.startswith('/opt/lighthouse-hub/services/hub/'):label='services/hub/'+Path(file).name
  elif '/sqlite3/' in file:label='stdlib/sqlite3'
  else:label='other_controlled_frame'
  function=frame.name if re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,63}',frame.name) else 'controlled_call'
  frames.append({'file':label,'function':function,'line':frame.lineno})
 result['frames']=frames
if settings is not None:
 db=settings.db_path;files={}
 for suffix in ('','-wal','-shm'):
  try:files[suffix]={'exists':True,'bytes':Path(str(db)+suffix).stat().st_size}
  except FileNotFoundError:files[suffix]={'exists':False,'bytes':None}
  except OSError as error:files[suffix]={'exists':'unobservable','errno':error.errno}
 result['sqlite_files']=files
result.update(raw_exception_message_included=False,environment_included=False,credentials_included=False,files_deleted=0,external_requests=0)
print(json.dumps(result))
'''

def main():
 assert os.name=='posix' and os.geteuid()==0;os.umask(0o077)
 assert HOST.resolve()==HOST and all(not p.is_symlink() and p.stat().st_uid==0 and not p.stat().st_mode&0o022 for p in (HOST,*HOST.parents))
 raw=HOST.read_bytes();assert hashlib.sha256(raw).hexdigest()==HOST_SHA
 module=types.ModuleType('installed_retention_v3');module.__file__=str(HOST);exec(compile(raw,str(HOST),'exec'),module.__dict__)
 file=module.BASE/'runtime.json';module.protected(file);config=json.loads(file.read_bytes());assert module.uid(config['authorization_id'])
 result=module.app_run(config,['python','-c',module.READ_ONLY_STORE+DIAGNOSTIC,config['authorization_id']],timeout=90)
 folder=module.STATE/'diagnostics';module.mkdir(folder,0o700);file=folder/(str(uuid.uuid4())+'.json')
 module.write(file,module.dump(result),0o600)
 print(json.dumps({**result,'diagnostic_receipt':str(file)}));return result['exit_code']
if __name__=='__main__':raise SystemExit(main())
