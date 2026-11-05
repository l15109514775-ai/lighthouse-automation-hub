"""Fixed pilot maintenance: release only redundant raw tracks; retain master.

This is not the automatic V5 retention service. The already qualified file
primitive and immutable successful local packet proof are reused. No platform
request or freshness claim exists here.
"""
from pathlib import Path
import argparse,hashlib,importlib.util,json,os,sqlite3,time,uuid
from services.hub.config import Settings
from services.hub.db import Store,encode
from services.hub.locks import resource_guard
from services.hub.archive_source_retention_v2 import _inspect
from services.hub.pilot_published_retention_v2 import PilotPublishedRetentionV2,JOB,DOWNLOAD
from services.hub.video_source_retention_v2 import PUBLISHED

ROOT=Path('/opt/lighthouse-hub')
PREVIOUS=ROOT/'data/pilot-published-cleanup-v2/fee54175-6055-4b2a-9ba8-4186b91f48ba/plan.json'
PREVIOUS_SHA='b27f1559f47031132f548bb56fc0697bd9e3abf73095fc764729b74070ee1123'
EXPECTED={
 'stream-299.mp4':(10505641986,'d62df50879f7fbf4bad0907b2ed16a77192583e18cfaef830b8d2746a4b6b93b'),
 'stream-140.m4a':(416163003,'7a6da67c122faaadf42bcb84714167ebf9e3b8e12aadcfe2e66e0bb4ed976e65'),
}
MASTER=(10942323813,'b472d9dbc516973dbff246d2f7ae68a3eb83e8023e35bf24437a734f8056047b')
FOLDER=ROOT/'data/pilot-redundant-raw-tracks-v5'
QUAL='/var/lib/lighthouse-backup-ops/pilot-current45-retention43-v3-tests/4ddf01f8-0077-4e42-bcf9-72f7d212db3b/test-attestation.json'
QSHA='5e85d6e51ad4ad38d4286a12e14827ee8eb5de56a470fea7d3de34b7555311cb'

def sha(raw):return hashlib.sha256(raw).hexdigest()
def safe(p):
 p=Path(p);assert p.is_absolute() and p.resolve()==p and not any(x.is_symlink() for x in (p,*p.parents));return p
def read(p):return safe(p).read_bytes()
def write(p,value):
 raw=encode(value).encode();fd=os.open(safe(p),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'wb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
 d=os.open(p.parent,os.O_DIRECTORY);os.fsync(d);os.close(d);return sha(raw)
def snapshot(ret):
 j,b,s,d,p=ret._context(JOB)
 assert p['external_id']=='BV1JExh6QELr' and j['attempts']==3 and d['job_id']==DOWNLOAD
 return {'job':sha(encode(j).encode()),'source':sha(encode(d).encode()),'publication':sha(encode(p).encode())}

def main():
 assert os.name=='posix' and os.getuid()==os.getgid()==10001;os.umask(0o077)
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--apply',action='store_true');parser.add_argument('--plan');parser.add_argument('--plan-sha256');a=parser.parse_args()
 assert bool(a.apply)==bool(a.plan and a.plan_sha256)
 spec=importlib.util.spec_from_file_location('qualified_pilot_operator','/run/cleanup-pilot.py');helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
 helper.qualify(ROOT,file=Path(QUAL),checksum=QSHA)
 settings=Settings.load();store=Store(settings.db_path);ret=PilotPublishedRetentionV2(settings,store)
 original_raw=read(PREVIOUS);assert sha(original_raw)==PREVIOUS_SHA;original=json.loads(original_raw)
 assert original['authorization_id']=='1fb26fc9-0324-4f13-9c30-64a767fbb012' and original['job_id']==JOB and original['scope']==PUBLISHED
 owner=ret._authorization(original['authorization_id']);assert owner['actual_owner_response']=='还有就是对于异地备份完成的数据可以直接在硅谷服务器删除，录播视频上传到B站后不需要保留。11月前的录播就不要上传了。'
 assert owner['scope']==PUBLISHED and owner['artifact'] is None and sha(encode(owner).encode())==original['authorization_sha256']
 ret._release(original)
 master=original['candidate'];raws=original['candidates'][:2]
 assert Path(master['file'])==ROOT/'media'/DOWNLOAD/'youtube-highest-v2/original.mp4' and (master['bytes'],master['sha256'])==MASTER
 assert len(raws)==2 and {Path(v['file']).name for v in raws}==set(EXPECTED)
 assert all(Path(v['file']).parent==Path(master['file']).parent and (v['bytes'],v['sha256'])==EXPECTED[Path(v['file']).name] for v in raws)
 # This operation has zero browser access and never deletes the master. The
 # video/moderation locks and immediate DB transaction fence media users;
 # the owner's independently locked Bilibili viewing session may stay open.
 with resource_guard(ROOT/'data/locks/video',ROOT/'data/locks/moderation',ROOT/'data/locks/heavy'):
  FOLDER.mkdir(mode=0o700,exist_ok=True);safe(FOLDER)
  if not a.apply:
   before=snapshot(ret);job,binding,state,source,pub=ret._context(JOB)
   asset=source['result']['youtube_download_v2']['source_asset']
   assert asset['sha256']==master['sha256'] and asset['bytes']==master['bytes'] and asset['file']==master['file']
   assert asset['packet_evidence']==original['proof']['basis']['local_packet_evidence']
   assert _inspect(ROOT,Path(master['file']),14_000_000_000)==master
   # Reuse the actual qualified track-receipt/format/packet-chain validator.
   checked,proof=ret._tracks(JOB,original['proof'],reuse_packets=original['source_track_cleanup']['source_to_merged_packet_evidence'])
   assert checked==raws and snapshot(ret)==before
   plan={'version':'pilot-redundant-raw-only-plan-v5','plan_id':str(uuid.uuid4()),'authorization_id':owner['authorization_id'],
    'previous_full_local_verification':{'file':str(PREVIOUS),'sha256':PREVIOUS_SHA},'snapshot':before,
    'master_retained':master,'candidates':raws,'bound_track_chain':proof,'source_before':source,'platform_requests':0,
    'operator_sha256':sha(read(Path(__file__))),
    'historical_public_confirmation_not_refreshed':True,'automatic_v5_workflow_claimed':False}
   file=FOLDER/(plan['plan_id']+'.json');checksum=write(file,plan)
   print(json.dumps({'state':'raw_two_reviewable_plan','plan_file':str(file),'plan_sha256':checksum,
    'raw_files':2,'raw_bytes':sum(v['bytes'] for v in raws),'master_retained_bytes':master['bytes'],'files_deleted':0}));return
  file=safe(Path(a.plan));assert file.parent==FOLDER
  body=read(file);assert sha(body)==a.plan_sha256;plan=json.loads(body)
  assert plan['version']=='pilot-redundant-raw-only-plan-v5' and file.name==plan['plan_id']+'.json'
  assert plan['operator_sha256']==sha(read(Path(__file__)))
  assert plan['candidates']==raws and plan['master_retained']==master and plan['authorization_id']==owner['authorization_id']
  index=sqlite3.connect(FOLDER/'intents.sqlite3');index.execute('PRAGMA journal_mode=WAL');index.execute('PRAGMA synchronous=FULL')
  index.execute('CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,plan_sha TEXT NOT NULL,record TEXT NOT NULL,state TEXT NOT NULL)')
  row=index.execute('SELECT plan_sha,record,state FROM operations WHERE id=?',(plan['plan_id'],)).fetchone()
  if row:
   assert row[0]==a.plan_sha256
   if row[2]=='completed':print(json.dumps(json.loads(row[1])['receipt']));return
   intent=json.loads(row[1]);assert intent['plan']==plan
  else:intent={'plan':plan,'progress':{v['file']:'pending' for v in raws}}
  def persist(state='deleting'):
   index.execute('INSERT INTO operations VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET record=excluded.record,state=excluded.state',
    (plan['plan_id'],a.plan_sha256,encode(intent),state));index.commit()
  current_source=store.get(DOWNLOAD);marker=current_source['result'].get('redundant_source_tracks_retention_pilot_v5')
  if marker and marker.get('plan_id')==plan['plan_id']:
   restored=json.loads(encode(current_source));restored['result'].pop('redundant_source_tracks_retention_pilot_v5');restored['updated_at']=plan['source_before']['updated_at']
   assert restored==plan['source_before'] and all(not Path(v['file']).exists() for v in raws)
   assert marker['raw_files_deleted']==2 and marker['master_retained']=={k:master[k] for k in ('file','bytes','sha256')}
   current=snapshot(ret);assert all(current[k]==plan['snapshot'][k] for k in ('job','publication'))
   intent={'receipt':marker};persist('completed');print(json.dumps(marker));return
  assert snapshot(ret)==plan['snapshot']
  assert _inspect(ROOT,Path(master['file']),14_000_000_000)==master
  ctimes={}
  for item in raws:
   path=safe(Path(item['file']));phase=intent['progress'][str(path)];assert phase in ('pending','deleting','deleted')
   if path.exists():
    assert phase!='deleted' and _inspect(ROOT,path,14_000_000_000)==item;ctimes[str(path)]=path.stat().st_ctime_ns
   else:assert phase in ('deleting','deleted')
  with store.transaction() as db:
   assert snapshot(ret)==plan['snapshot']
   ret._unshared(db,[v['file'] for v in raws],JOB,DOWNLOAD)
   persist()
   for item in raws:
    path=Path(item['file'])
    if intent['progress'][str(path)]=='deleted':continue
    intent['progress'][str(path)]='deleting';persist()
    if path.exists():ret._stat_unlink(item,ctimes[str(path)])
    intent['progress'][str(path)]='deleted';persist()
   source=store.get(DOWNLOAD)
   receipt={'version':'pilot-redundant-raw-only-receipt-v5','plan_id':plan['plan_id'],'source_id':'9T5tn1WGlJg',
    'download_job_id':DOWNLOAD,'raw_files_deleted':2,'raw_logical_bytes_deleted':sum(v['bytes'] for v in raws),
    'master_retained':{k:master[k] for k in ('file','bytes','sha256')},'original_packet_proof_reused_after_current_full_sha':True,
    'platform_requests':0,'publication_records_changed':False,'historical_public_confirmation_not_refreshed':True,
    'original_receipts_preserved':True,'automatic_v5_workflow_claimed':False,'deleted_at':time.time()}
   db.execute('UPDATE jobs SET result=?,updated_at=? WHERE job_id=?',(encode({**source['result'],'redundant_source_tracks_retention_pilot_v5':receipt}),time.time(),DOWNLOAD))
   db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
    (time.time(),'operator:pilot-raw2-v5','release_redundant_raw_tracks',DOWNLOAD,encode(receipt)))
  intent={'receipt':receipt};persist('completed');index.close();print(json.dumps(receipt))

if __name__=='__main__':main()
