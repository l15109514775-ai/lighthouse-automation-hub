"""Real SQLite compatibility fences; every row and source is an owned fixture."""
from copy import deepcopy
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
import hashlib,importlib.util,json,time,uuid
import pytest
from services.hub.db import encode
from services.hub.replay_fifo_v4 import ReplayFIFOV4,DEFAULT_POLICY,POLICY_FILE,UID,PLAN_JOB
from test_youtube_natural_plan_v3 import fixture as inventory_fixture

spec=importlib.util.spec_from_file_location('owned_metadata_rebind',Path(__file__).resolve().parents[1]/'scripts/ops/rebind_legacy_metadata_fifo_v1.py')
op=importlib.util.module_from_spec(spec);spec.loader.exec_module(op)

@pytest.fixture
def area(runtime,monkeypatch):
 s,store=runtime;now=time.time();s.config['target_bilibili_site_uid']=UID
 policy={**deepcopy(DEFAULT_POLICY),'enabled':True,'activated_at':datetime.fromtimestamp(now-5,timezone.utc).isoformat(),
         'owner_queue_reference':'Owned fixture maintenance, not actual content permission','planning_policy_sha256':'a'*64}
 file=s.root/'config'/POLICY_FILE;file.parent.mkdir(parents=True,exist_ok=True);raw=encode(policy).encode();file.write_bytes(raw);file.chmod(0o644)
 # Only this owned policy's bytes substitute the real server pin; all actual
 # policy and source validators run unchanged against the owned fixture.
 monkeypatch.setattr(op,'CURRENT_SHA',hashlib.sha256(raw).hexdigest())
 q=ReplayFIFOV4(s,store,clock=lambda:now);q.initialize()
 for index,(source,(candidate,legacy)) in enumerate(op.ALLOW.items()):
  raw,_=inventory_fixture();raw['id']=source;raw['snippet']['title']='Owned metadata-only replay '+str(index)
  raw['liveStreamingDetails']={'actualStartTime':'2026-11-01T00:00:00Z','actualEndTime':'2026-11-01T01:00:00Z'};raw['contentDetails']['duration']='PT1H'
  payload={'video_id':source,'channel_id':raw['snippet']['channelId'],'title':raw['snippet']['title'],'published_at':raw['snippet']['publishedAt'],
           'duration':'PT1H','url':'https://www.youtube.com/watch?v='+source,'source_api':'youtube-data-v3-official','raw':raw}
  result={'disposition':'metadata_only','url':payload['url'],'rights_authorized':False,'download_performed':False,'publish_performed':False}
  with store.transaction() as db:
   db.execute("INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,attempts,policy_version,payload,result,stage) VALUES(?,'youtube',?,'video_process','video','completed',?,?,1,'machine-tweet-v3-api',?,?,'done')",(legacy,source,now-60,now-30,encode(payload),encode(result)))
   db.execute("INSERT INTO youtube_videos(video_id,channel_id,title,published_at,url,state,actual_start,actual_end,duration,first_seen_at,last_seen_at,last_api_check,source_api,metadata,replay_job_id) VALUES(?,?,?,?,?,'ended',?,?,?,?,?,?,'youtube-data-v3-official',?,?)",
      (source,payload['channel_id'],payload['title'],payload['published_at'],payload['url'],raw['liveStreamingDetails']['actualStartTime'],raw['liveStreamingDetails']['actualEndTime'],'PT1H',now-60,now,now,encode(raw),legacy))
  folder=s.root/'media'/legacy;folder.mkdir();(folder/'metadata.json').write_text(encode(payload),encoding='utf-8')
 q.register_all_observed()
 with store.transaction() as db:
  for source,(candidate,_) in op.ALLOW.items():db.execute('UPDATE replay_fifo_v4 SET candidate_id=?,policy_sha256=? WHERE source_id=?',(candidate,op.OLD_SHA,source))
 return SimpleNamespace(s=s,db=store,now=now,policy_file=file,q=q)

def rows(a,table):return a.db.query('SELECT * FROM '+table+' ORDER BY 1')
def plan(a):return op.make_plan(a.s,'Owned exact compatibility repair',now=a.now)
def checksum(p):return op.digest(encode(p).encode())

def test_plan_is_db_readonly_then_only_two_candidate_fields_and_audit_change_once(area):
 a=area;before={n:rows(a,n) for n in ('jobs','youtube_videos','publications','source_tombstones','replay_fifo_v4','audit')}
 p=plan(a);assert {n:rows(a,n) for n in before}==before
 out=op.apply_plan(a.s,p,checksum(p),now=a.now+1);assert out['state']=='applied' and len(out['rebound'])==2
 for table in ('jobs','youtube_videos','publications','source_tombstones'):assert rows(a,table)==before[table]
 for old,new in zip(before['replay_fifo_v4'],rows(a,'replay_fifo_v4')):assert new=={**old,'policy_sha256':op.CURRENT_SHA,'updated_at':a.now+1}
 audit=rows(a,'audit');assert len(audit)==len(before['audit'])+2
 assert all(json.loads(r['outcome'])['authorization_changed'] is False for r in audit[-2:])
 with pytest.raises(RuntimeError):op.apply_plan(a.s,p,checksum(p),now=a.now+2)
 assert rows(a,'jobs')==before['jobs'] and rows(a,'audit')==audit

@pytest.mark.parametrize('case',['asset','completed_download','active','foreign','tombstone','resource_history','identity','residual_lease'])
def test_any_nonmetadata_asset_state_or_history_refuses_without_rebinding(area,case):
 a=area;source,(candidate,legacy)=next(iter(op.ALLOW.items()));before=rows(a,'replay_fifo_v4')
 if case=='asset':(a.s.root/'media'/legacy/'original.mp4').write_bytes(b'owned video asset must block')
 elif case=='completed_download':
  with a.db.transaction() as db:
   value=json.loads(db.execute('SELECT result FROM jobs WHERE job_id=?',(legacy,)).fetchone()['result']);value['download_performed']=True
   db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(value),legacy))
 elif case=='active':
  with a.db.transaction() as db:db.execute("UPDATE jobs SET state='processing',lease_owner='owned',lease_until=? WHERE job_id=?",(a.now+60,legacy))
 elif case=='foreign':a.db.enqueue('youtube',source,'youtube_replay_download_v4','video',{'video_id':source},'replay-fifo-v4')
 elif case=='tombstone':
  with a.db.transaction() as db:db.execute('INSERT INTO source_tombstones VALUES(?,?,?,?,?,?)',('youtube',source,'OWNER_BACKLOG_CLEARED',a.now,legacy,'owner_clear_all'))
 elif case=='resource_history':
  with a.db.transaction() as db:db.execute('INSERT INTO replay_fifo_capacity_v4 VALUES(?,?,?,?,?,?,?,?,?)',(candidate,'hold',a.now,a.now+100,a.now+10,100,'a'*64,0,'{}'))
 elif case=='identity':
  with a.db.transaction() as db:
   raw=json.loads(db.execute('SELECT metadata FROM youtube_videos WHERE video_id=?',(source,)).fetchone()['metadata']);raw['liveStreamingDetails']['actualStartTime']='2026-11-01T00:00:01Z'
   db.execute('UPDATE youtube_videos SET metadata=?,actual_start=? WHERE video_id=?',(encode(raw),raw['liveStreamingDetails']['actualStartTime'],source))
 else:
  with a.db.transaction() as db:db.execute('UPDATE jobs SET lease_until=? WHERE job_id=?',(a.now+30,legacy))
 expected={'asset':'ASSET_PRESENT','completed_download':'NOT_METADATA_ONLY','active':'VIDEO_ACTIVE','foreign':'OTHER_JOB_PRESENT',
           'tombstone':'HISTORY_PRESENT','resource_history':'HISTORY_PRESENT','identity':'SOURCE_IDENTITY_CHANGED','residual_lease':'VIDEO_ACTIVE'}[case]
 with pytest.raises(RuntimeError,match='REPLAY_METADATA_REBIND_'+expected):plan(a)
 assert rows(a,'replay_fifo_v4')==before and not a.db.query('SELECT * FROM audit WHERE action=?',(op.VERSION,))

def test_concurrent_real_plan_job_after_planning_fails_cas_and_preserves_all_history(area):
 a=area;p=plan(a);source,(candidate,legacy)=next(iter(op.ALLOW.items()))
 ident=a.db.enqueue('youtube',source,PLAN_JOB,'video',{'video_id':source},'replay-fifo-v4')
 with a.db.transaction() as db:db.execute("UPDATE replay_fifo_v4 SET plan_job_id=?,state='plan_queued' WHERE candidate_id=?",(ident,candidate))
 before=rows(a,'replay_fifo_v4');jobs=rows(a,'jobs')
 with pytest.raises(RuntimeError):op.apply_plan(a.s,p,checksum(p),now=a.now+1)
 assert rows(a,'jobs')==jobs and rows(a,'replay_fifo_v4')==before and not a.db.query('SELECT * FROM audit WHERE action=?',(op.VERSION,))

def test_policy_or_plan_hash_expiry_and_metadata_bytes_cannot_be_accepted(area):
 a=area;p=plan(a);before=rows(a,'replay_fifo_v4')
 for value,hash_value,now in ((p,'0'*64,a.now+1),(p,checksum(p),a.now+301)):
  with pytest.raises(RuntimeError):op.apply_plan(a.s,value,hash_value,now=now)
 original=a.policy_file.read_bytes();value=json.loads(original);value['max_download_dispatches_per_day']=3;a.policy_file.write_text(encode(value),encoding='utf-8')
 with pytest.raises(RuntimeError):op.apply_plan(a.s,p,checksum(p),now=a.now+1)
 a.policy_file.write_bytes(original)
 legacy=next(iter(op.ALLOW.values()))[1];file=a.s.root/'media'/legacy/'metadata.json';file.write_bytes(file.read_bytes()+b'\n')
 with pytest.raises(RuntimeError,match='CAS_CHANGED'):op.apply_plan(a.s,p,checksum(p),now=a.now+1)
 assert rows(a,'replay_fifo_v4')==before

def exact_owned_revisions(a,monkeypatch):
 revisions=deepcopy(op.LEGACY_DURATION_REVISIONS)
 for source,(_,legacy) in op.ALLOW.items():
  known=revisions[source]
  with a.db.transaction() as db:
   official=dict(db.execute('SELECT * FROM youtube_videos WHERE video_id=?',(source,)).fetchone());raw=json.loads(official['metadata'])
   raw['contentDetails']['duration']='PT'+str(int(known['candidate_seconds']))+'S';official.update(metadata=encode(raw),duration=raw['contentDetails']['duration'])
   identity,payload=a.q._source(official,a.q.policy())
   db.execute('UPDATE youtube_videos SET metadata=?,duration=? WHERE video_id=?',(official['metadata'],official['duration'],source))
   db.execute('UPDATE replay_fifo_v4 SET identity_sha256=?,source_payload=?,source_metadata_sha256=? WHERE source_id=?',
      (op.sha(identity),encode(payload),op.digest(encode(raw).encode()),source))
   old=deepcopy(payload);old['duration']='PT'+str(int(known['legacy_seconds']))+'S';old['raw']['contentDetails']['duration']=old['duration']
   db.execute('UPDATE jobs SET payload=? WHERE job_id=?',(encode(old),legacy))
  (a.s.root/'media'/legacy/'metadata.json').write_text(encode(old),encoding='utf-8')
  # Synthetic raw fixture bytes stand in for the two actual recorded hashes.
  # The exact actual duration pairs and all real source validators remain.
  known['legacy_raw_sha256']=op.digest(encode(old['raw']).encode())
 monkeypatch.setattr(op,'LEGACY_DURATION_REVISIONS',revisions)

def test_exact_two_observed_duration_revisions_preserve_old_jobs_and_new_candidate_identity(area,monkeypatch):
 a=area;exact_owned_revisions(a,monkeypatch);old=rows(a,'jobs');candidates=rows(a,'replay_fifo_v4');p=plan(a)
 assert set(p['snapshot']['legacy_duration_revisions'])==set(op.ALLOW)
 op.apply_plan(a.s,p,checksum(p),now=a.now+1)
 assert rows(a,'jobs')==old
 for before,after in zip(candidates,rows(a,'replay_fifo_v4')):assert after=={**before,'policy_sha256':op.CURRENT_SHA,'updated_at':a.now+1}
 for source,(candidate,_) in op.ALLOW.items():
  record=json.loads(a.db.query('SELECT outcome FROM audit WHERE action=? AND target=?',(op.VERSION,candidate))[0]['outcome'])
  assert record['legacy_duration_revision']['legacy_record_preserved'] is True

@pytest.mark.parametrize('change',['start','other_duration'])
def test_known_legacy_duration_exception_never_masks_date_or_unobserved_duration_change(area,monkeypatch,change):
 a=area;exact_owned_revisions(a,monkeypatch);source,(_,legacy)=next(iter(op.ALLOW.items()))
 with a.db.transaction() as db:
  payload=json.loads(db.execute('SELECT payload FROM jobs WHERE job_id=?',(legacy,)).fetchone()['payload'])
  if change=='start':payload['raw']['liveStreamingDetails']['actualStartTime']='2026-11-01T00:00:01Z'
  else:payload['duration']='PT24637S';payload['raw']['contentDetails']['duration']=payload['duration']
  db.execute('UPDATE jobs SET payload=? WHERE job_id=?',(encode(payload),legacy))
 (a.s.root/'media'/legacy/'metadata.json').write_text(encode(payload),encoding='utf-8')
 revisions=deepcopy(op.LEGACY_DURATION_REVISIONS);revisions[source]['legacy_raw_sha256']=op.digest(encode(payload['raw']).encode());monkeypatch.setattr(op,'LEGACY_DURATION_REVISIONS',revisions)
 before=rows(a,'replay_fifo_v4')
 with pytest.raises(RuntimeError,match='SOURCE_IDENTITY_CHANGED'):plan(a)
 assert rows(a,'replay_fifo_v4')==before
