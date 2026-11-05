"""Offline harness fences; no browser, account, daemon or production DB."""
import json,signal,sqlite3,time
from pathlib import Path
from types import SimpleNamespace
import pytest
from scripts.ops import run_cached_separate_private_tasks_v5 as m


@pytest.fixture
def fenced(tmp_path):
 root=tmp_path/'owned';(root/'data').mkdir(parents=True)
 payload={'source_id':m.SOURCE,'target_site_uid':m.UID}
 probe={'job_id':m.JOB,'job_type':'replay_multipart_private_probe_v4','policy_version':'replay-multipart-private-probe-v4',
  'source_platform':'maintenance','source_id':'multipart-private:owned','state':'upload_pending','attempts':1,'queue':'video',
  'lease_owner':None,'lease_until':None,'external_publish_id':None,'payload':m.encoded(payload).decode(),
  'result':m.encoded({'multipart_private_v4':{'state':'upload_unknown','binding_sha256':m.sha(m.encoded(payload)),
   'attempt_id':'11111111-1111-4111-8111-111111111111'}}).decode()}
 pilot={**probe,'job_id':m.PILOT,'job_type':'replay_highest_video_publish_v2','policy_version':'highest-owned',
  'source_platform':'youtube','source_id':m.SOURCE,'state':'completed','attempts':3,'external_publish_id':m.BV,'payload':'{}','result':'{}'}
 dbfile=root/'data/hub.sqlite3'
 with sqlite3.connect(dbfile) as db:
  db.execute('CREATE TABLE jobs('+','.join(k+(' INTEGER' if k=='attempts' else ' TEXT') for k in probe)+')')
  for row in (probe,pilot):db.execute('INSERT INTO jobs VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))
  db.execute('CREATE TABLE publications(idempotency_key TEXT,job_id TEXT,state TEXT,external_id TEXT,source_id TEXT,account_id TEXT)')
  db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?)',('a'*64,m.PILOT,'completed',m.BV,m.SOURCE,'site_uid:'+m.UID))
 small=root/'original-selection.json';small.write_bytes(b'{"owned":"original"}')
 clip=root/'owned-clip.mp4';clip.write_bytes(b'Owned small byte sentinel, not a media-acceptance fixture')
 value={'version':'cached-private-maintenance-fence-v5','root':str(root),'deadline_monotonic':time.monotonic()+100,
  'snapshot':m.snapshot(root),'small_refs':[m.ref(small)],'sample_files':[m.ref(clip)],'owner_reference':'Owned isolated test only'}
 return SimpleNamespace(root=root,file=dbfile,small=small,clip=clip,fence=value)


def test_cli_requires_explicit_exact_saved_plan_for_run_and_does_not_default_to_browser():
 a=m.arguments(['--owner-reference','Owned isolated scope']);assert a.run is False and a.check_fence is None
 for args in ([],['--run'],['--run','--plan','/owned/plan.json','--plan-sha256','bad'],
  ['--check-fence','/owned/fence.json','--fence-sha256','a'*64,'--run']):
  with pytest.raises(SystemExit):m.arguments(args)
 a=m.arguments(['--check-fence','/owned/fence.json','--fence-sha256','a'*64]);assert a.run is False


def test_real_query_only_fence_preserves_jobs_publication_and_existing_global_source(fenced):
 f=fenced;before=f.file.read_bytes();snapshot=m.snapshot(f.root)
 assert len(snapshot['publications'])==1 and snapshot['publications'][0]['source_id']==m.SOURCE
 assert m.check_fence(f.root,f.fence)=={'state':'fence_verified','fake_lease':False}
 assert m.snapshot(f.root)==snapshot and f.file.read_bytes()==before
 assert next(j for j in snapshot['jobs'] if j['job_id']==m.JOB)['attempts']==1


def test_expired_or_changed_real_db_fence_cannot_authorize_an_action(fenced):
 f=fenced
 with pytest.raises(RuntimeError,match='EXPIRED'):m.check_fence(f.root,f.fence,now=f.fence['deadline_monotonic'])
 with sqlite3.connect(f.file) as db:db.execute('UPDATE jobs SET attempts=2 WHERE job_id=?',(m.JOB,))
 with pytest.raises(RuntimeError,match='DB_SCOPE_CHANGED'):m.check_fence(f.root,f.fence)
 with sqlite3.connect(f.file) as db:assert db.execute('SELECT attempts FROM jobs WHERE job_id=?',(m.JOB,)).fetchone()[0]==2


def test_original_json_or_current_clip_changes_block_even_with_unchanged_job(fenced):
 f=fenced;before=m.snapshot(f.root);raw=f.small.read_bytes();f.small.write_bytes(raw+b' ')
 with pytest.raises(RuntimeError,match='REFERENCE_CHANGED'):m.check_fence(f.root,f.fence)
 f.small.write_bytes(raw);f.clip.write_bytes(b'X'+f.clip.read_bytes()[1:])
 with pytest.raises(RuntimeError,match='REFERENCE_CHANGED'):m.check_fence(f.root,f.fence)
 assert m.snapshot(f.root)==before


def test_owned_cleanup_never_signals_foreign_or_reused_pid(monkeypatch):
 child=m.OwnedChild.__new__(m.OwnedChild);child.process=SimpleNamespace(pid=100,poll=lambda:None);child.owned={100:7}
 rows={100:(1,7,'S'),101:(100,8,'S'),103:(900,30,'S')};monkeypatch.setattr(m,'process_rows',lambda:rows)
 killed=[];monkeypatch.setattr(m.os,'kill',lambda pid,sig:killed.append((pid,sig)))
 child.remember();assert child.owned=={100:7,101:8}
 rows[100]=(1,99,'S');rows[104]=(100,31,'S');child.signal_owned(signal.SIGTERM)
 assert killed==[(101,signal.SIGTERM)] and 104 not in child.owned and 103 not in child.owned
