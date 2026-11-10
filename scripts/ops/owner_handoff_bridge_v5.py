#!/usr/bin/python3
"""Root's fixed V5 owner-display bridge. Never claim a job or change its state.

Only status, an explicitly captured eight-byte credential, and a validated
stdio relay to container 127.0.0.1:6084 are available. No caller supplies code,
file paths, shell commands, sockets, ports, cookies or application secrets.
"""
import argparse,hashlib,json,os,re,stat,subprocess,sys
from pathlib import Path

ROOT=Path('/opt/lighthouse-hub')
INSTALL=Path('/usr/local/lib/lighthouse-owner-handoff-v5/owner_handoff_bridge_v5.py')
DOCKER='/usr/bin/docker'
ENTRY='services.hub.replay_parts_worker_runtime_v5'
RUNTIME_ROLES={'video-worker':'--publish','replay-publication-worker':'--publication-only'}
def fail(code='OWNER_HANDOFF_BRIDGE_REFUSED'):raise RuntimeError(code)
def parse_scope(cid,image,job,attempt,nonce):
 import uuid
 if not re.fullmatch('[a-f0-9]{64}',cid or '') or not re.fullmatch('sha256:[a-f0-9]{64}',image or ''):fail('OWNER_HANDOFF_BRIDGE_SCOPE_INVALID')
 try:
  if any(str(uuid.UUID(v))!=v for v in (job,attempt,nonce)):raise ValueError()
 except (ValueError,TypeError,AttributeError):fail('OWNER_HANDOFF_BRIDGE_SCOPE_INVALID')
 return dict(cid=cid,image=image,job_id=job,attempt_id=attempt,nonce=nonce)
def validate_runtime(info,scope):
 try:
  c,h,s=info['Config'],info['HostConfig'],info['State'];labels=c['Labels']
  if (info['Id']!=scope['cid'] or info['Image']!=scope['image'] or s['Running'] is not True or
   s.get('Paused',False) is not False or s.get('Restarting',False) is not False or s.get('Dead',False) is not False or
   c['User']!='10001:10001' or c.get('Entrypoint') not in (None,[]) or c['WorkingDir']!=str(ROOT) or
   labels.get('com.docker.compose.service') not in RUNTIME_ROLES or
   c['Cmd']!=['python','-m',ENTRY,RUNTIME_ROLES.get(labels.get('com.docker.compose.service'))] or
   labels.get('com.docker.compose.project.working_dir')!=str(ROOT) or
   h.get('Privileged') is not False or h.get('ReadonlyRootfs') is not True or h.get('PublishAllPorts',False) is not False or
   h.get('PortBindings') not in (None,{}) or any(info.get('NetworkSettings',{}).get('Ports',{}).values()) or
   h.get('NetworkMode')=='host' or str(h.get('NetworkMode','')).startswith('container:')):raise ValueError()
  for name in ('data','media'):
   matches=[m for m in info['Mounts'] if m.get('Destination')==str(ROOT/name)]
   if len(matches)!=1 or matches[0]['Type']!='bind' or matches[0]['Source']!=str(ROOT/name) or matches[0]['RW'] is not True:raise ValueError()
  if any(m.get('Destination') in ('/','/var/run/docker.sock','/run/docker.sock','/host','/proc','/sys') for m in info['Mounts']):raise ValueError()
 except (KeyError,ValueError,TypeError,AttributeError):fail('OWNER_HANDOFF_BRIDGE_RUNTIME_INVALID')
 return True

# Executed only as app10001 by a fixed Docker argv. The test suite loads these
# functions under a non-main name to exercise them with owned data only.
APP_CODE=r'''
import hashlib,json,math,os,re,select,socket,sqlite3,stat,sys,time,uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path
ROOT=Path('/opt/lighthouse-hub');UID='1000000000000000';POLICY='replay-part-video-policy-v5'
STATE='replay_part_video_v5';OBS='replay-part-owner-handoff-observation-v5'
TERMINALS=('verification_cleared.json','page_readable.json','protection_changed.json','expired.json')
def refuse(code='OWNER_HANDOFF_BRIDGE_EVIDENCE_INVALID'):raise RuntimeError(code)
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
def timestamp(value):
 d=datetime.fromisoformat(value.replace('Z','+00:00'))
 if d.tzinfo is None or not math.isfinite(d.timestamp()):refuse()
 return d.timestamp()
def folder(scope):return ROOT/'media'/scope['job_id']/'replay-browser-part-v5'/scope['attempt_id']/'owner-handoff'/scope['nonce']
def safe(file):
 file=Path(file)
 if not file.is_absolute() or file.resolve()!=file or any(p.is_symlink() for p in (file,*file.parents)):refuse()
 return file
def read(file,maximum=2_000_000,*,private=True):
 file=safe(file);fd=os.open(file,os.O_RDONLY|os.O_NOFOLLOW)
 with os.fdopen(fd,'rb') as stream:
  a=os.fstat(stream.fileno())
  if not stat.S_ISREG(a.st_mode) or a.st_nlink!=1 or not 0<a.st_size<=maximum or private and (a.st_uid!=10001 or stat.S_IMODE(a.st_mode)!=0o600):refuse()
  raw=stream.read(maximum+1);b=os.fstat(stream.fileno());p=file.lstat()
 if len(raw)!=a.st_size or any(getattr(a,k)!=getattr(b,k) or getattr(a,k)!=getattr(p,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')):refuse()
 return raw,a
def json_file(file):
 raw,_=read(file);return json.loads(raw),{'file':str(file),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
def check_ref(reference,expected,*,image=False):
 if not isinstance(reference,dict) or set(reference)!={'file','bytes','sha256'} or reference['file']!=str(expected):refuse()
 raw,_=read(expected,20_000_000 if image else 2_000_000)
 if type(reference['bytes']) is not int or len(raw)!=reference['bytes'] or hashlib.sha256(raw).hexdigest()!=reference['sha256']:refuse()
 if image and (len(raw)<33 or raw[:16]!=b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' or not 0<int.from_bytes(raw[16:20],'big')<=10000 or not 0<int.from_bytes(raw[20:24],'big')<=10000):refuse()
 return json.loads(raw) if not image else None
def body_marker(protection):
 e=protection.get('evidence',{});c=e.get('visible_challenge_controls',[])
 return protection.get('blocked') is True and protection.get('error_code')=='BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED' and e.get('version')=='bilibili-video-protection-guard-v3' and e.get('inspection_failed') is not True and e.get('truncated') is False and e.get('top_level_authentication') is False and type(e.get('native_mp4_uploaders')) is int and e['native_mp4_uploaders']==0 and e.get('visible_dialogs')==[] and e.get('challenge_frames')==[] and len(c)==1 and c[0].get('tag')=='BODY' and c[0].get('role')=='' and sorted(c[0].get('classes',[]))==['risk-captcha-adapt','risk-captcha-adapt-pc']
def hold_matches(hold,scope,reference,reason):
 return isinstance(hold,dict) and hold.get('version')==OBS and hold.get('nonce')==scope['nonce'] and hold.get('event_reference')==reference and hold.get('reason')==reason and hold.get('authentication_assistance_only') is (reason=='visible_human_verification') and hold.get('automatic_retry') is False and hold.get('human_review') is False
def live(job,now):return job.get('state')=='processing' and bool(job.get('lease_owner')) and type(job.get('lease_until')) in (int,float) and math.isfinite(job['lease_until']) and job['lease_until']>now
def validate_documents(scope,ready,detected,ack,facts,now):
 base=('job_id','attempt_id','nonce','site_uid','source_id','binding_sha256','metadata_sha256','human_review','publication_authorized','authentication_assistance_only','automatic_resubmit','same_browser_context','same_page','ttl_seconds','reason','interactive_verification_observed','visible_body_empty','readiness_observation','expires_at')
 for key in base:
  if ready.get(key)!=detected.get(key) or type(ready.get(key)) is not type(detected.get(key)):refuse()
 for key in ('job_id','attempt_id','nonce'):
  if detected[key]!=scope[key]:refuse()
 if (ready.get('version')!=OBS or detected.get('version')!=OBS or ready.get('state')!='ready' or detected.get('state')!='detected' or
  detected['site_uid']!=UID or not re.fullmatch('[A-Za-z0-9_-]{11}',detected['source_id']) or
  any(not re.fullmatch('[a-f0-9]{64}',detected[k]) for k in ('binding_sha256','metadata_sha256')) or
  type(detected['ttl_seconds']) is not int or not 60<=detected['ttl_seconds']<=1200 or
  not timestamp(detected['observed_at'])<=timestamp(ready['observed_at'])<=now+1 or not 0<timestamp(ready['expires_at'])-now<=1201 or
  not 0<=timestamp(ready['expires_at'])-timestamp(detected['observed_at'])<=detected['ttl_seconds'] or
  detected['human_review'] is not False or detected['publication_authorized'] is not False or detected['automatic_resubmit'] is not False or
  detected['same_browser_context'] is not True or detected['same_page'] is not True):refuse()
 reason=detected['reason'];interactive=reason=='visible_human_verification'
 if reason not in ('visible_human_verification','unreadable_body_marker') or detected['interactive_verification_observed'] is not interactive or detected['authentication_assistance_only'] is not interactive or type(detected['visible_body_empty']) is not bool:refuse()
 protection=detected['protection'];e=protection.get('evidence',{})
 if protection.get('blocked') is not True or protection.get('error_code')!='BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED' or e.get('inspection_failed') is True:refuse()
 if interactive:
  if not(e.get('challenge_frames') or e.get('visible_dialogs') or any(c.get('tag')!='BODY' for c in e.get('visible_challenge_controls',[]))):refuse()
 elif not body_marker(protection) or detected['visible_body_empty'] is not True or detected['readiness_observation'] is None:refuse()
 for doc in (ready,detected):
  actions=doc['handoff_actions']
  if set(actions)!={'automatic_public_submit_clicks','file_selections','private_save_clicks'} or any(type(v) is not int or v!=0 for v in actions.values()):refuse()
  page=doc['page'];origin=page['origin'];p=page['path']
  if set(page)!={'origin','path','query_keys'} or origin not in ('https://member.bilibili.com','https://www.bilibili.com') or origin=='https://member.bilibili.com' and p not in ('/platform/upload/video/frame','/platform/upload-manager/article') or origin=='https://www.bilibili.com' and not re.fullmatch('/video/BV[A-Za-z0-9]{10}/?',p) or not isinstance(page['query_keys'],list) or len(page['query_keys'])>30 or any(not isinstance(k,str) or not re.fullmatch('[A-Za-z0-9_-]{1,100}',k) for k in page['query_keys']):refuse()
 if ready['page']!=detected['page'] or ready['web_host']!='127.0.0.1' or ready['vnc_host']!='127.0.0.1' or type(ready['web_port']) is not int or ready['web_port']!=6084 or type(ready['vnc_port']) is not int or ready['vnc_port']!=5904 or ready['password_file']!=str(folder(scope)/'vnc-password.txt'):refuse()
 reference=facts['detected_reference']
 if (set(ack)!={'version','event_reference','job_id','attempt_id','nonce','recorded_unknown_or_hold','automatic_retry','human_review','publication_authorized'} or
  ack['version']!='replay-part-owner-handoff-parent-ack-v5' or ack['event_reference']!=reference or any(ack[k]!=scope[k] for k in ('job_id','attempt_id','nonce')) or
  ack['recorded_unknown_or_hold'] is not True or ack['automatic_retry'] is not False or ack['human_review'] is not False or ack['publication_authorized'] is not False):refuse()
 parent=facts['parent'];payload=parent['payload'];state=parent['result'][STATE];index=payload['part_index']
 if (type(index) is not int or not 0<=index<32 or parent['job_id']!=scope['job_id'] or parent['job_type']!='replay_part_publish_v5_p'+format(index+1,'02d') or
  parent['queue']!='video' or parent['source_platform']!='youtube' or parent['policy_version']!=POLICY or parent['source_id']!=detected['source_id'] or
  payload['site_uid']!=UID or payload['source_id']!=parent['source_id'] or payload['source_url']!='https://www.youtube.com/watch?v='+parent['source_id'] or digest(payload)!=detected['binding_sha256'] or
  digest(payload['metadata'])!=payload['metadata_sha256'] or payload['metadata_sha256']!=detected['metadata_sha256'] or state['attempt_id']!=scope['attempt_id'] or
  payload.get('human_review') is not False or state.get('human_review') is not False or state.get('public_submission_approved') is not False):refuse()
 eligible=[]
 if live(parent,now) and parent.get('stage')=='replay_part_owner_handoff_wait' and hold_matches(state.get('owner_handoff'),scope,reference,reason):
  if state['state'] not in ('prepared','uploaded','upload_unknown','submit_unknown','awaiting_moderation','completed','failed'):refuse()
  eligible.append(('main',parent,state['owner_handoff'],'replay_part_owner_handoff_before_gui','worker:replay-video-v1'))
 for item in facts.get('lookups',[]):
  child=item['job'];p=child['payload'];hold=child['result'].get('owner_handoff');schedule=item['schedule']
  if not hold_matches(hold,scope,reference,reason):continue
  if (not live(child,now) or child['job_type']!='replay_part_lookup_v5' or child['policy_version']!=POLICY or child['source_platform']!='bilibili' or child['queue']!='video' or
   child.get('stage')!='replay_part_lookup_owner_handoff_wait' or parent['state'] not in ('publish_unknown','awaiting_moderation') or
   parent.get('lease_owner') is not None or parent.get('lease_until') is not None or p['version']!='replay-part-read-only-lookup-v5' or
   p['parent_publish_job_id']!=parent['job_id'] or p['attempt_id']!=scope['attempt_id'] or p['parent_binding_sha256']!=detected['binding_sha256'] or
   p['site_uid']!=UID or p['source_id']!=parent['source_id'] or p['metadata_sha256']!=payload['metadata_sha256'] or p['publication_key']!=state['key'] or
   schedule.get('active_child_id')!=child['job_id'] or schedule.get('status')!='waiting' or schedule.get('deadline',0)<=now or p['poll_number']!=schedule.get('polls_total')):refuse()
  eligible.append(('lookup',child,hold,'replay_part_lookup_owner_handoff_before_gui','worker:replay-part-lookup-v5'))
 if len(eligible)!=1:refuse('OWNER_HANDOFF_BRIDGE_LIVE_LEASE_REQUIRED')
 role,lease_job,hold,action,principal=eligible[0]
 if not any(a['target']==lease_job['job_id'] and a['action']==action and a['principal']==principal and a['outcome']==hold and timestamp(detected['observed_at'])-1<=a['at']<=now+1 for a in facts.get('audits',[])):refuse('OWNER_HANDOFF_BRIDGE_HOLD_AUDIT_REQUIRED')
 return {'version':'owner-handoff-bridge-status-v5','state':'ready','job_id':scope['job_id'],'attempt_id':scope['attempt_id'],'nonce':scope['nonce'],
  'site_uid':UID,'source_id':parent['source_id'],'reason':reason,'interactive_verification_observed':interactive,'expires_at':ready['expires_at'],
  'lease_role':role,'lease_job_id':lease_job['job_id'],'web_host':'127.0.0.1','web_port':6084}
def check_password(raw,item):
 if not stat.S_ISREG(item.st_mode) or item.st_uid!=10001 or stat.S_IMODE(item.st_mode)!=0o600 or item.st_nlink!=1 or not re.fullmatch(rb'[A-Za-z0-9+/]{8}\n?',raw):refuse('OWNER_HANDOFF_BRIDGE_CREDENTIAL_INVALID')
 return raw.rstrip(b'\n')
def terminal_check(directory):
 for name in TERMINALS:
  if (directory/name).exists() or (directory/name).is_symlink():refuse('OWNER_HANDOFF_BRIDGE_TERMINAL')
def db_facts(scope):
 with closing(sqlite3.connect((ROOT/'data/hub.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
  db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
  def job(row):
   if row is None:refuse()
   v=dict(row);v['payload']=json.loads(v['payload']);v['result']=json.loads(v['result']);return v
  parent=job(db.execute('SELECT * FROM jobs WHERE job_id=?',(scope['job_id'],)).fetchone());lookups=[]
  for row in db.execute("SELECT * FROM jobs WHERE job_type='replay_part_lookup_v5' AND policy_version=? AND state='processing' AND json_extract(payload,'$.parent_publish_job_id')=?",(POLICY,parent['job_id'])):
   child=job(row);schedule=db.execute('SELECT * FROM replay_part_lookup_schedule_v5 WHERE parent_job_id=?',(parent['job_id'],)).fetchone()
   lookups.append({'job':child,'schedule':dict(schedule) if schedule else {}})
  targets=[parent['job_id']]+[x['job']['job_id'] for x in lookups];audits=[]
  for target in targets:
   for row in db.execute("SELECT * FROM audit WHERE target=? AND action IN ('replay_part_owner_handoff_before_gui','replay_part_lookup_owner_handoff_before_gui') ORDER BY at DESC LIMIT 16",(target,)):
    a=dict(row);a['outcome']=json.loads(a['outcome']);audits.append(a)
  return {'parent':parent,'lookups':lookups,'audits':audits}
def validate(scope):
 directory=safe(folder(scope));item=directory.stat()
 if not stat.S_ISDIR(item.st_mode) or item.st_uid!=10001 or stat.S_IMODE(item.st_mode)!=0o700:refuse()
 terminal_check(directory)
 cfg=ROOT/'config/bilibili-video-handoff-v5.json';raw,_=read(cfg,4096,private=False)
 for p in (cfg,*cfg.parents):
  s=p.stat()
  if s.st_uid!=0 or s.st_mode&0o022:refuse()
 config=json.loads(raw)
 if set(config)!={'version','enabled','ttl_seconds'} or config['version']!='bilibili-video-owner-handoff-v5' or config['enabled'] is not True or type(config['ttl_seconds']) is not int or not 60<=config['ttl_seconds']<=1200:refuse()
 ready,readyref=json_file(directory/'ready.json');detected,detectedref=json_file(directory/'detected.json');ack,_=json_file(directory/'parent-ack.json')
 if detected['ttl_seconds']!=config['ttl_seconds']:refuse()
 check_ref(ack['event_reference'],directory/'detected.json')
 for name,doc in (('ready',ready),('detected',detected)):check_ref(doc['screenshot'],directory/(name+'.png'),image=True)
 reference=detected['readiness_observation']
 if reference is not None:
  file=Path(reference['file']);base=directory.parent.parent/'protection-readiness'
  if file.name!='observation.json' or file.parent.parent!=base or str(uuid.UUID(file.parent.name))!=file.parent.name:refuse()
  r=check_ref(reference,file)
  if r['version']!='replay-part-empty-body-readiness-v5' or r['read_only'] is not True or r['max_wait_ms']!=5000 or any(r[k]!=detected[k] for k in ('job_id','attempt_id','site_uid','source_id','binding_sha256','metadata_sha256')):refuse()
  for key in ('before','after'):
   check_ref(r[key]['screenshot'],file.parent/(key+'.png'),image=True)
   if detected['reason']=='unreadable_body_marker' and (r[key]['visible_body_text'].strip() or not body_marker(r[key]['protection'])):refuse()
  if detected['reason']=='unreadable_body_marker' and (r['outcome']!='still_unreadable_body_marker' or r['page']!=detected['page']):refuse()
 facts=db_facts(scope);facts['detected_reference']=detectedref
 status=validate_documents(scope,ready,detected,ack,facts,time.time());terminal_check(directory)
 # Listener declarations must also agree with the actual Linux socket table.
 rows=[line.split() for line in (Path('/proc/net/tcp').read_text().splitlines()[1:])]
 for port in (5904,6084):
  listeners=[r for r in rows if len(r)>3 and r[3]=='0A' and r[1].endswith(':'+format(port,'04X'))]
  if len(listeners)!=1 or listeners[0][1]!='0100007F:'+format(port,'04X'):refuse('OWNER_HANDOFF_BRIDGE_LISTENER_INVALID')
  six=[line.split() for line in Path('/proc/net/tcp6').read_text().splitlines()[1:]]
  if any(len(r)>3 and r[3]=='0A' and r[1].endswith(':'+format(port,'04X')) for r in six):refuse('OWNER_HANDOFF_BRIDGE_LISTENER_INVALID')
 return status
def relay(scope,status):
 s=socket.create_connection(('127.0.0.1',6084),timeout=5);s.setblocking(False);os.set_blocking(0,False);os.set_blocking(1,False)
 header={'state':'relay_ready',**{k:status[k] for k in ('job_id','attempt_id','nonce','expires_at')}}
 to_stdout=bytearray(json.dumps(header,separators=(',',':')).encode()+b'\n');to_socket=bytearray();next_check=0
 try:
  while time.time()<timestamp(status['expires_at']):
   if time.monotonic()>=next_check:validate(scope);next_check=time.monotonic()+2
   reads=([0] if len(to_socket)<1048576 else [])+([s] if len(to_stdout)<1048576 else [])
   writes=([1] if to_stdout else [])+([s] if to_socket else [])
   readable,writable,_=select.select(reads,writes,[],.5)
   if 0 in readable:
    data=os.read(0,65536)
    if not data:break
    to_socket.extend(data)
   if s in readable:
    data=s.recv(65536)
    if not data:break
    to_stdout.extend(data)
   if 1 in writable:del to_stdout[:os.write(1,to_stdout)]
   if s in writable:del to_socket[:s.send(to_socket)]
 finally:s.close()
def app_main():
 if os.getuid()!=10001 or os.getgid()!=10001:refuse()
 mode,job,attempt,nonce=sys.argv[1:];scope={'job_id':job,'attempt_id':attempt,'nonce':nonce}
 if mode not in ('status','credential','relay') or any(str(uuid.UUID(v))!=v for v in (job,attempt,nonce)):refuse()
 status=validate(scope)
 if mode=='status':print(json.dumps(status,separators=(',',':')),flush=True)
 elif mode=='credential':
  raw,item=read(folder(scope)/'vnc-password.txt',9);password=check_password(raw,item);validate(scope);os.write(1,password)
 else:relay(scope,status)
if __name__=='__main__':
 try:app_main()
 except BaseException:
  os.write(2,b'OWNER_HANDOFF_BRIDGE_APP_REFUSED\n');raise SystemExit(2)
'''

def docker_argv(scope,mode):
 if mode not in ('status','credential','relay'):fail()
 return [DOCKER,'exec','--user','10001:10001','--interactive',scope['cid'],'/usr/bin/env','-i','PATH=/usr/local/bin:/usr/bin:/bin',
  '/usr/local/bin/python','-I','-c',APP_CODE,mode,scope['job_id'],scope['attempt_id'],scope['nonce']]
def _run(arguments):
 try:
  p=subprocess.run(arguments,capture_output=True,timeout=15)
  if p.returncode!=0 or len(p.stdout)>4_000_000:fail('OWNER_HANDOFF_BRIDGE_COMMAND_REFUSED')
  return p.stdout
 except (OSError,subprocess.SubprocessError):fail('OWNER_HANDOFF_BRIDGE_COMMAND_REFUSED')
def runtime(scope):
 info=json.loads(_run([DOCKER,'inspect',scope['cid']]))
 if len(info)!=1:fail()
 validate_runtime(info[0],scope)
 current=[]
 for service in RUNTIME_ROLES:
  current.extend(_run([DOCKER,'ps','--no-trunc','--filter','label=com.docker.compose.service='+service,'--filter','status=running','--format','{{.ID}}']).decode().splitlines())
 if current!=[scope['cid']]:fail('OWNER_HANDOFF_BRIDGE_CURRENT_WORKER_CHANGED')
def protected_self(checksum):
 if os.name!='posix' or os.geteuid()!=0 or not re.fullmatch('[a-f0-9]{64}',checksum or ''):fail()
 file=Path(__file__)
 if file!=INSTALL or file.resolve()!=file or hashlib.sha256(file.read_bytes()).hexdigest()!=checksum:fail('OWNER_HANDOFF_BRIDGE_SOURCE_CHANGED')
 for p in (file,*file.parents):
  s=p.lstat()
  if stat.S_ISLNK(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:fail('OWNER_HANDOFF_BRIDGE_SOURCE_UNPROTECTED')
def main(argv=None):
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--mode',choices=('status','credential','relay'),required=True)
 for name in ('cid','image','job','attempt','nonce','bridge-sha256'):p.add_argument('--'+name,required=True)
 a=p.parse_args(argv);scope=parse_scope(a.cid,a.image,a.job,a.attempt,a.nonce);protected_self(a.bridge_sha256);runtime(scope)
 if a.mode=='relay':
  proc=subprocess.Popen(docker_argv(scope,'relay'),stdin=sys.stdin.buffer,stdout=sys.stdout.buffer,stderr=subprocess.DEVNULL)
  try:raise SystemExit(proc.wait(timeout=1250))
  finally:
   if proc.poll() is None:
    proc.terminate()
    try:proc.wait(timeout=5)
    except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
 raw=_run(docker_argv(scope,a.mode))
 if a.mode=='credential':
  if not re.fullmatch(rb'[A-Za-z0-9+/]{8}',raw):fail('OWNER_HANDOFF_BRIDGE_CREDENTIAL_INVALID')
  sys.stdout.buffer.write(raw);sys.stdout.buffer.flush()
 else:
  result=json.loads(raw)
  if result.get('state')!='ready':fail()
  print(json.dumps({**result,'cid':scope['cid'],'image':scope['image'],'bridge_sha256':a.bridge_sha256},separators=(',',':')))
if __name__=='__main__':
 try:main()
 except Exception as error:
  code=str(error);sys.stderr.write((code if re.fullmatch('[A-Z][A-Z0-9_]{1,100}',code) else 'OWNER_HANDOFF_BRIDGE_REFUSED')+'\n');raise SystemExit(2)
