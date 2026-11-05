"""Issue the exact owner-authorized two-post grant and start cached backfill."""
import hashlib,json,os,subprocess,time,uuid
from pathlib import Path
OPS=Path('/home/lighthouse-ops');ROOT=Path('/opt/lighthouse-hub')
IMAGE='sha256:7d279abadd435c7bccc164f3870bcb560b93fdeda79c1497fc64232d5be298e8'
ENGINE='b2b3db799afbdb6efb41ec55476c938c2d6395eee43b783a73d7ca4ab1787fee'
TARGETS=['2117189314744590552','2117189497125486924']
def run(args,**kw):return subprocess.run(args,check=True,capture_output=True,text=True,**kw)
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
 import argparse
 p=argparse.ArgumentParser();p.add_argument('--plan-sha256');p.add_argument('--start',action='store_true');a=p.parse_args()
 assert os.geteuid()==0 and bool(a.start)==bool(a.plan_sha256);os.umask(0o077)
 operator=OPS/'backfill_youtube_cards_20261102.py';engine=OPS/'oneoff_reference_backfill_v2.py'
 assert sha(engine)==ENGINE
 actual=json.loads(run(['docker','inspect','lighthouse-hub-tweet-worker-1']).stdout)[0]
 assert actual['Image']==IMAGE and actual['State']['Running'] and actual['Config']['Hostname']=='lighthouse-tweet-worker'
 cid=actual['Id'];folder=ROOT/'config/owner-cached-youtube-links-v1'
 if not folder.exists():folder.mkdir(mode=0o750);os.chown(folder,0,10001)
 assert folder.resolve()==folder
 assert folder.stat().st_uid==0 and folder.stat().st_gid==10001
 folder.chmod(0o750)
 for src,name in [(operator,'operator.py'),(engine,'engine.py')]:
  dest=folder/name
  if dest.exists():assert dest.read_bytes()==src.read_bytes()
  else:
   with dest.open('xb') as f:os.fchmod(f.fileno(),0o640);f.write(src.read_bytes());f.flush();os.fsync(f.fileno())
   os.chown(dest,0,10001)
 current=json.loads(run(['docker','exec','-i','-u','10001','-w',str(ROOT),cid,'python','-','--mode','plan','--engine-file',str(folder/'engine.py')],input=operator.read_text()).stdout)
 assert current['source_ids']==TARGETS and current['new_x_calls']==0 and current['human_review'] is False
 assert current['existing_jobs']==current['existing_publications']==current['tombstones']==[]
 canonical=json.dumps(current,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode();digest=hashlib.sha256(canonical).hexdigest()
 if not a.start:print(json.dumps({'state':'plan_only','plan_sha256':digest,'plan':current},ensure_ascii=False));return
 assert a.plan_sha256==digest
 grant={'plan':current,'grant_id':str(uuid.uuid4()),'issued_at':time.time(),'expires_at':time.time()+7100,
  'owner_response_reference':'Current owner instruction in Codex thread 01a18f9e-3848-7492-b54a-8de0cdec6e17, 2026-11-02: '+current['owner_quote']}
 grantfile=folder/'grant.json'
 with grantfile.open('xb') as f:os.fchmod(f.fileno(),0o640);f.write((json.dumps(grant,ensure_ascii=False,sort_keys=True)+'\n').encode());f.flush();os.fsync(f.fileno())
 os.chown(grantfile,0,10001)
 launch=folder/'launch.sh'
 command='#!/bin/sh\nset -eu\nexec /usr/bin/docker exec -i -u 10001 -w /opt/lighthouse-hub '+cid+' python - --mode run --engine-file '+str(folder/'engine.py')+' --grant-file '+str(grantfile)+' --grant-sha256 '+sha(grantfile)+' --container-id '+cid+' --hostname lighthouse-tweet-worker < '+str(folder/'operator.py')+'\n'
 with launch.open('x') as f:os.fchmod(f.fileno(),0o750);f.write(command);f.flush();os.fsync(f.fileno())
 os.chown(launch,0,10001)
 result=run(['systemd-run','--unit=lighthouse-cached-youtube-links-v1','--no-block','--property=Type=oneshot','--property=TimeoutStartSec=7000',str(launch)])
 print(json.dumps({'state':'started','source_ids':TARGETS,'container_id':cid,'grant_sha256':sha(grantfile),'plan_sha256':digest,'operator_sha256':sha(operator)}))
if __name__=='__main__':main()
