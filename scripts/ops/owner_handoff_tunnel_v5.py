"""Pinned SSH, fixed local 127.0.0.1:18789 -> validated V5 stdio bridge.

Run locally only. The existing key and known_hosts are read in place. A new
nonce password is written privately under D: work/keys and removed on exit.
No password, key, cookie, SSH command output or raw error is logged.
"""
import argparse,csv,io,json,os,re,select,shlex,socketserver,stat,subprocess,sys,threading,time
from datetime import datetime
from pathlib import Path
try:from .owner_handoff_bridge_v5 import parse_scope,INSTALL
except ImportError:from owner_handoff_bridge_v5 import parse_scope,INSTALL

WORK=Path(r'/opt/lighthouse-hub\Chats\2026-10-31\codex-ubuntu-24-04-lts-cpu\work')
KEYS=WORK/'keys';HOST='203.0.113.10';USER='lighthouse-ops';LOCAL=('127.0.0.1',18789)
def fail(code='OWNER_HANDOFF_TUNNEL_REFUSED'):raise RuntimeError(code)
def remote_command(scope,mode,checksum):
 parse_scope(scope['cid'],scope['image'],scope['job_id'],scope['attempt_id'],scope['nonce'])
 if mode not in ('status','credential','relay') or not re.fullmatch('[a-f0-9]{64}',checksum or ''):fail()
 args=['sudo','-n',INSTALL.as_posix(),'--mode',mode,'--cid',scope['cid'],'--image',scope['image'],'--job',scope['job_id'],
  '--attempt',scope['attempt_id'],'--nonce',scope['nonce'],'--bridge-sha256',checksum]
 return shlex.join(args)
def expiry(value):
 d=datetime.fromisoformat(value.replace('Z','+00:00'))
 if d.tzinfo is None:fail()
 return d.timestamp()
def validate_status(value,scope,checksum,*,now=None):
 now=time.time() if now is None else now
 if (value.get('version')!='owner-handoff-bridge-status-v5' or value.get('state')!='ready' or
  any(value.get(k)!=v for k,v in scope.items()) or value.get('bridge_sha256')!=checksum or value.get('site_uid')!='1000000000000000' or
  value.get('web_host')!='127.0.0.1' or value.get('web_port')!=6084 or type(value['web_port']) is not int or
  value.get('lease_role') not in ('main','lookup') or not 0<expiry(value['expires_at'])-now<=1201):fail('OWNER_HANDOFF_TUNNEL_STATUS_INVALID')
 return value
def credential_path(nonce):
 import uuid
 if str(uuid.UUID(nonce))!=nonce:fail()
 return KEYS/('owner-handoff-v5-'+nonce+'.password')
def private_credential(scope,raw):
 if not re.fullmatch(rb'[A-Za-z0-9+/]{8}',raw):fail('OWNER_HANDOFF_TUNNEL_CREDENTIAL_INVALID')
 directory=KEYS
 if not directory.is_dir():fail('OWNER_HANDOFF_TUNNEL_EXISTING_KEYS_REQUIRED')
 file=credential_path(scope['nonce'])
 fd=os.open(file,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
 opened=os.fstat(fd);identity=(opened.st_dev,opened.st_ino)
 try:
  if os.name=='nt':
   # Resolve the actual Windows SID without trusting a username environment
   # string. Neither command receives password bytes or uses a shell.
   import ctypes
   buf=ctypes.create_unicode_buffer(32768);length=ctypes.windll.kernel32.GetSystemDirectoryW(buf,len(buf))
   if not 0<length<len(buf):fail('OWNER_HANDOFF_TUNNEL_LOCAL_ACL_FAILED')
   system=Path(buf.value)
   who=subprocess.run([str(system/'whoami.exe'),'/user','/fo','csv','/nh'],capture_output=True,text=True,encoding='utf-8',timeout=5)
   rows=list(csv.reader(io.StringIO(who.stdout.strip())))
   if who.returncode or len(rows)!=1 or len(rows[0])!=2 or not re.fullmatch(r'S-\d+(?:-\d+)+',rows[0][1]):fail('OWNER_HANDOFF_TUNNEL_LOCAL_ACL_FAILED')
   secured=subprocess.run([str(system/'icacls.exe'),str(file),'/inheritance:r','/grant:r','*'+rows[0][1]+':(F)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5)
   if secured.returncode:fail('OWNER_HANDOFF_TUNNEL_LOCAL_ACL_FAILED')
  item=file.lstat()
  if not stat.S_ISREG(item.st_mode) or item.st_nlink!=1 or (item.st_dev,item.st_ino)!=identity:fail()
  # The file is still empty until its Windows ACL has been restricted.
  with os.fdopen(fd,'wb') as stream:
   fd=None;stream.write(raw+b'\n');stream.flush();os.fsync(stream.fileno())
  return file,(item.st_dev,item.st_ino)
 except BaseException:
  if fd is not None:os.close(fd)
  remove_credential(file,identity)
  raise
def remove_credential(file,identity):
 try:s=file.lstat()
 except FileNotFoundError:return
 if file.parent!=KEYS or not file.name.startswith('owner-handoff-v5-') or not file.name.endswith('.password') or file.is_symlink() or (s.st_dev,s.st_ino)!=identity or not stat.S_ISREG(s.st_mode) or s.st_nlink!=1:fail('OWNER_HANDOFF_TUNNEL_LOCAL_CREDENTIAL_CHANGED')
 file.unlink()
def connect():
 import paramiko
 if not WORK.is_absolute() or not WORK.is_dir() or not str(WORK).startswith('D:'):fail('OWNER_HANDOFF_TUNNEL_D_DRIVE_REQUIRED')
 client=paramiko.SSHClient();client.load_host_keys(str(KEYS/'known_hosts'));client.set_missing_host_key_policy(paramiko.RejectPolicy())
 key=paramiko.Ed25519Key.from_private_key_file(str(KEYS/'lighthouse_ed25519'))
 try:client.connect(HOST,username=USER,pkey=key,allow_agent=False,look_for_keys=False,timeout=20,banner_timeout=20,auth_timeout=20)
 except BaseException:client.close();raise
 client.get_transport().set_keepalive(15);return client
def open_session(client,command):
 transport=client.get_transport()
 if transport is None or not transport.is_active():fail('OWNER_HANDOFF_TUNNEL_SSH_CLOSED')
 channel=transport.open_session(timeout=10);channel.settimeout(15);channel.exec_command(command);return channel
def capture(client,command,maximum):
 channel=open_session(client,command);data=bytearray();end=time.monotonic()+25
 try:
  while time.monotonic()<end:
   if channel.recv_ready():
    data.extend(channel.recv(65536))
    if len(data)>maximum:fail('OWNER_HANDOFF_TUNNEL_RESPONSE_BOUND')
   if channel.recv_stderr_ready():channel.recv_stderr(65536)  # discard; never log command/credential output
   if channel.exit_status_ready() and not channel.recv_ready():
    if channel.recv_exit_status()!=0:fail('OWNER_HANDOFF_TUNNEL_SERVER_REFUSED')
    return bytes(data)
   time.sleep(.01)
  fail('OWNER_HANDOFF_TUNNEL_SERVER_TIMEOUT')
 finally:channel.close()
def relay_header(channel,scope,expected_expiry):
 data=bytearray();end=time.monotonic()+20
 while b'\n' not in data:
  if time.monotonic()>=end or len(data)>4096:fail('OWNER_HANDOFF_TUNNEL_RELAY_NOT_READY')
  if channel.recv_ready():
   chunk=channel.recv(4096)
   if not chunk:fail('OWNER_HANDOFF_TUNNEL_RELAY_NOT_READY')
   data.extend(chunk)
  elif channel.exit_status_ready():fail('OWNER_HANDOFF_TUNNEL_SERVER_REFUSED')
  else:time.sleep(.01)
 line,extra=bytes(data).split(b'\n',1);value=json.loads(line)
 if (set(value)!={'state','job_id','attempt_id','nonce','expires_at'} or value['state']!='relay_ready' or
  any(value[k]!=scope[k] for k in ('job_id','attempt_id','nonce')) or value['expires_at']!=expected_expiry):fail('OWNER_HANDOFF_TUNNEL_RELAY_SCOPE_CHANGED')
 return extra
def run(scope,checksum):
 client=None;server=None;file=None;identity=None;stop=threading.Event();channels=set();lock=threading.Lock();slots=threading.BoundedSemaphore(12)
 try:
  client=connect()
  def status():return validate_status(json.loads(capture(client,remote_command(scope,'status',checksum),4096)),scope,checksum)
  first=status();raw=capture(client,remote_command(scope,'credential',checksum),8);file,identity=private_credential(scope,raw);del raw
  deadline=time.monotonic()+min(1200,expiry(first['expires_at'])-time.time())
  if deadline<=time.monotonic():fail('OWNER_HANDOFF_TUNNEL_EXPIRED')
  class Handler(socketserver.BaseRequestHandler):
   def handle(self):
    if self.client_address[0]!='127.0.0.1' or stop.is_set() or not slots.acquire(blocking=False):return
    channel=None
    try:
     self.request.settimeout(2)
     channel=open_session(client,remote_command(scope,'relay',checksum))
     channel.settimeout(2)
     with lock:channels.add(channel)
     extra=relay_header(channel,scope,first['expires_at'])
     if extra:self.request.sendall(extra)
     while not stop.is_set() and time.monotonic()<deadline:
      readable,_,_=select.select([self.request,channel],[],[],.5)
      if self.request in readable:
       chunk=self.request.recv(65536)
       if not chunk:break
       channel.sendall(chunk)
      if channel in readable:
       chunk=channel.recv(65536)
       if not chunk:break
       self.request.sendall(chunk)
    except Exception:pass  # fail closed, no raw HTTP/VNC/credential or command log
    finally:
     if channel:
      with lock:channels.discard(channel)
      channel.close()
     slots.release()
  class Server(socketserver.ThreadingMixIn,socketserver.TCPServer):
   daemon_threads=True;allow_reuse_address=False
  server=Server(LOCAL,Handler)
  worker=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.25},daemon=True);worker.start()
  def monitor():
   while not stop.wait(5):
    if time.monotonic()>=deadline:stop.set();return
    try:
     if status()['expires_at']!=first['expires_at']:stop.set();return
    except Exception:stop.set();return
  monitor_thread=threading.Thread(target=monitor,daemon=True);monitor_thread.start()
  print(json.dumps({'ready':True,'url':'http://127.0.0.1:18789/vnc.html','expires_at':first['expires_at']}),flush=True)
  while not stop.wait(.25):
   if time.monotonic()>=deadline:break
 except KeyboardInterrupt:pass
 finally:
  stop.set()
  if server:server.shutdown();server.server_close()
  with lock:
   for channel in channels:channel.close()
  if client:client.close()
  if file:remove_credential(file,identity)
def main(argv=None):
 p=argparse.ArgumentParser(description=__doc__)
 for name in ('cid','image','job','attempt','nonce','bridge-sha256'):p.add_argument('--'+name,required=True)
 a=p.parse_args(argv);scope=parse_scope(a.cid,a.image,a.job,a.attempt,a.nonce)
 if not re.fullmatch('[a-f0-9]{64}',a.bridge_sha256):fail()
 run(scope,a.bridge_sha256)
if __name__=='__main__':
 try:main()
 except Exception:
  sys.stderr.write('OWNER_HANDOFF_TUNNEL_REFUSED\n');raise SystemExit(2)
