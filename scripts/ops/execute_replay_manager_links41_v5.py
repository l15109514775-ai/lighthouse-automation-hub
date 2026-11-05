"""One pinned SSH session; no production operator, deployment or API call."""
from pathlib import Path
import base64,hashlib,json,subprocess,sys

ROOT=Path(__file__).resolve().parents[4];HERE=ROOT/'work'
ARCHIVE_SHA='f0069dc58e32831d150a23d7b12616866fdb7fbbe3b437902d241aa33152b379'
RUNNER_SHA='9c71e9c15de8bbea9df9e8aae7e58fc0b21391d5f6920c5fc81bc9c37a797ae7'

if __name__=='__main__':
 files={
  'replay-manager-links41-v5.tar.gz':(ROOT/'outputs/replay-manager-links41-v5.tar.gz',ARCHIVE_SHA),
  'qualify_replay_manager_links41_retention26_v5.py':(ROOT/'outputs/lighthouse-hub/scripts/ops/qualify_replay_manager_links41_retention26_v5.py',RUNNER_SHA),
  'manager41-frozen-contention42.py':(ROOT/'outputs/lighthouse-hub/scripts/ops/qualify_replay_contention42_retention26_v5.py','401ba7df4a291bc0dbdc46b5f72d6287f4afab3b5da5dc22926d48c2f956c387'),
  'manager41-frozen-retention276.py':(ROOT/'outputs/lighthouse-hub/scripts/ops/qualify_replay_parts_retention_v5.py','4e451f198c2f4e57534a71898d497693b57b0bf94aee1712453f0e1b266f70b3')}
 payload={}
 for name,(path,expected) in files.items():
  raw=path.read_bytes();assert hashlib.sha256(raw).hexdigest()==expected
  payload[name]=base64.b64encode(raw).decode()
 code="import os,base64,json,sys,subprocess,tempfile\nfrom pathlib import Path\nassert os.geteuid()==0\npayload="+repr(payload)+"\n"
 code+="for name,value in payload.items():\n p=Path('/home/lighthouse-ops')/name\n assert p.parent==Path('/home/lighthouse-ops') and not p.is_symlink()\n raw=base64.b64decode(value)\n if p.exists():assert p.read_bytes()==raw\n else:\n  fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644)\n  with os.fdopen(fd,'wb') as f:os.fchmod(f.fileno(),0o644);f.write(raw);f.flush();os.fsync(f.fileno())\n"
 command=['python3','/home/lighthouse-ops/qualify_replay_manager_links41_retention26_v5.py',
  '--archive','/home/lighthouse-ops/replay-manager-links41-v5.tar.gz','--sha256',ARCHIVE_SHA,
  '--suite-helper','/home/lighthouse-ops/manager41-frozen-contention42.py',
  '--retention-helper','/home/lighthouse-ops/manager41-frozen-retention276.py']
 code+="command="+repr(command)+"\nsummaries=[]\nwith tempfile.TemporaryFile() as errors:\n p=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=errors,text=True)\n for line in p.stdout:\n  value=json.loads(line);summaries.append(value);print(json.dumps({'progress':value}),flush=True)\n status=p.wait();errors.seek(0);diagnostic=errors.read(12000).decode(errors='replace')\n"
 code+="artifacts=[]\nfor summary in summaries:\n path=Path(summary['file']);assert path.name in ('test-result.json','test-attestation.json') and path.is_relative_to('/var/lib/lighthouse-backup-ops')\n raw=path.read_bytes();proof=json.loads(raw);assert len(raw)<2000000\n xml=Path(proof.get('junit_host_file',proof['junit_file'])).read_bytes();logs=(path.parent/'tests.log').read_bytes();assert len(xml)<2000000 and len(logs)<2000000\n item={'summary':summary,'proof':base64.b64encode(raw).decode(),'junit':base64.b64encode(xml).decode(),'logs':base64.b64encode(logs).decode()}\n if summary.get('release_template'):\n  release=Path(summary['release_template']).read_bytes();assert len(release)<100000;item['release']=base64.b64encode(release).decode()\n artifacts.append(item)\nprint(json.dumps({'final':True,'exit':status,'artifacts':artifacts,'diagnostic':diagnostic}),flush=True)\n"
 args=['ssh','-T','-i',str(HERE/'keys/lighthouse_ed25519'),'-o','UserKnownHostsFile='+str(HERE/'keys/known_hosts'),
  '-o','StrictHostKeyChecking=yes','-o','BatchMode=yes','-o','ConnectTimeout=12','-o','ConnectionAttempts=1',
  '-o','ServerAliveInterval=30','-o','ServerAliveCountMax=3','lighthouse-ops@203.0.113.10','sudo -n python3 -']
 process=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
 process.stdin.write(code);process.stdin.close();final=None
 for line in process.stdout:
  value=json.loads(line)
  if value.get('final'):final=value
  else:print(json.dumps(value),flush=True)
 exitcode=process.wait(timeout=30);stderr=process.stderr.read()
 if exitcode or final is None:
  (HERE/'manager-links41-retention26-failed-ssh.txt').write_text(stderr,encoding='utf-8')
  print(json.dumps({'ssh_exit':exitcode,'stderr':stderr,'final_present':final is not None}));raise SystemExit(exitcode or 1)
 out=ROOT/'outputs/replay-manager-links41-retention26-v5-actual';out.mkdir(exist_ok=False);verified=[]
 for item in final['artifacts']:
  summary=item['summary'];folder=out/summary['stage'];folder.mkdir()
  raw=base64.b64decode(item['proof']);xml=base64.b64decode(item['junit']);logs=base64.b64decode(item['logs']);proof=json.loads(raw)
  assert hashlib.sha256(raw).hexdigest()==summary['sha256'] and hashlib.sha256(xml).hexdigest()==proof['junit_sha256']
  (folder/'test-result.json').write_bytes(raw);(folder/'results.xml').write_bytes(xml);(folder/'tests.log').write_bytes(logs)
  if 'release' in item:
   release=base64.b64decode(item['release']);assert hashlib.sha256(release).hexdigest()==summary['release_template_sha256']
   (folder/'release-template.json').write_bytes(release)
  verified.append({'stage':summary['stage'],'server':summary,'tests':proof['tests'],'test_groups':proof['test_groups'],'image':proof['image'],
   'proof_sha256':hashlib.sha256(raw).hexdigest(),'junit_sha256':hashlib.sha256(xml).hexdigest(),'logs_sha256':hashlib.sha256(logs).hexdigest()})
 record={'version':'replay-manager-links41-retention26-local-verification-v5','verified':True,'ssh_connections':1,'exit_code':final['exit'],'artifacts':verified}
 (out/'local-verification.json').write_text(json.dumps(record,sort_keys=True,indent=2),encoding='utf-8')
 if final['diagnostic']:(out/'runner-stderr.txt').write_text(final['diagnostic'],encoding='utf-8')
 print(json.dumps({'directory':str(out),**record}),flush=True)
 if final['exit']:print(final['diagnostic'])
 raise SystemExit(final['exit'])
