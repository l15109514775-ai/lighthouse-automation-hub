"""One owner-requested Codex announcement; durable job intent; unknown => lookup."""
from pathlib import Path
import hashlib,json,os,re,subprocess,sys,time,uuid
from services.hub.config import Settings
from services.hub.db import Store,encode
from services.hub.locks import resource_guard

os.umask(0o077);s=Settings.load();d=Store(s.db_path);request=json.load(open(sys.argv[1]))
caption=request['caption'];digest=hashlib.sha256(caption.encode()).hexdigest()
assert request['version']=='owner-requested-codex-status-v1' and request['uid']=='1000000000000000'
assert request['caption_sha256']==digest and request['owner_requested_publication'] is True
assert request['operation_id']=='codex-progress-20261103-workflow-v1' and caption.startswith(request['heading'])
policy='owner-requested-codex-announcement-v1';proc=None;intent=False
with resource_guard(s.root/'data/locks/tweet',s.root/'data/locks/heavy'):
 existing=d.query("SELECT job_id FROM jobs WHERE source_platform='operator' AND source_id=? AND job_type='operator_announcement' AND policy_version=?",(request['operation_id'],policy))
 if existing:
  job=d.get(existing[0]['job_id']);assert job['payload']['caption_sha256']==digest
  if job['state']=='completed':print(encode({'already_completed':True,'url':job['result']['publication']['url'],'job_id':job['job_id']}));sys.exit(0)
  intent=bool(job['result'].get('dispatch_intent'));mode='lookup' if intent or job['state']=='publish_unknown' else 'publish'
  if '--verified-pre-send-retry' in sys.argv:
   assert job['state']=='publish_unknown' and job['attempts']<4
   old=Path(job['result']['evidence_folder']);assert old.resolve().is_relative_to(s.root/'media'/job['job_id'])
   proof=json.load(open(old/'result.json'))
   assert proof['state']=='blocked_before_send' and proof['sent'] is False and proof['publish_control_clicked'] is False
   with d.transaction() as db:db.execute("UPDATE jobs SET state='processing',stage='operator_prepare',attempts=attempts+1,error_code=NULL,updated_at=?,result=? WHERE job_id=?",
    (time.time(),encode({'dispatch_intent':False,'prior_pre_send_failure_preserved':str(old),'prior_result':proof}),job['job_id']))
   d.audit('codex','retry_announcement_after_explicit_pre_send_non_submission',job['job_id'],str(old))
   job=d.get(job['job_id']);intent=False;mode='publish'
 else:
  ident=str(uuid.uuid4());now=time.time()
  with d.transaction() as db:
   db.execute("""INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,attempts,policy_version,payload,stage)
    VALUES(?,'operator',?,'operator_announcement','tweet','processing',?,?,1,?,?,'operator_prepare')""",
    (ident,request['operation_id'],now,now,policy,encode(request)))
  job=d.get(ident);mode='publish';d.audit('codex','owner_requested_progress_announcement',ident,digest)
 attempt=str(uuid.uuid4());folder=s.root/'media'/job['job_id']/'operator-announcement'/attempt;folder.mkdir(parents=True,mode=0o700)
 spec={'version':request['version'],'uid':request['uid'],'name':'咕噜咕噜灯塔字幕组','mode':mode,'caption':caption,
  'caption_sha256':digest,'heading':request['heading'],'attempt_id':attempt,'folder':str(folder)}
 (folder/'request.json').write_text(encode(spec),'utf-8')
 env={k:os.environ[k] for k in ('PATH','PLAYWRIGHT_BROWSERS_PATH','TEMP','TMP','TMPDIR') if k in os.environ}
 env.update(XDG_CONFIG_HOME='/tmp/lighthouse-browser-config',XDG_CACHE_HOME='/tmp/lighthouse-browser-cache')
 proc=subprocess.Popen(['node','/tmp/publish-codex-progress-20261103.mjs'],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=env)
 proc.stdin.write(encode(spec).encode());proc.stdin.close();outcome=None
 try:
  deadline=time.monotonic()+230
  while time.monotonic()<deadline:
   result=folder/'result.json'
   if result.exists():outcome=json.load(open(result));break
   ready=folder/'ready.json'
   if ready.exists() and not intent and mode=='publish':
    actual=json.load(open(ready));assert actual=={'uid':request['uid'],'caption_sha256':digest,'attempt_id':attempt}
    with d.transaction() as db:
     current=db.execute('SELECT state,result FROM jobs WHERE job_id=?',(job['job_id'],)).fetchone()
     prior=json.loads(current['result']);assert not prior.get('dispatch_intent')
     durable={'dispatch_intent':True,'attempt_id':attempt,'caption_sha256':digest,'requested_by':'owner','actor':'Codex',
      'authority':'explicit_owner_request_for_project_progress_post','intent_at':time.time(),'evidence_folder':str(folder)}
     db.execute("UPDATE jobs SET state='publish_unknown',updated_at=?,stage='operator_dispatch_intent',result=? WHERE job_id=?",(time.time(),encode(durable),job['job_id']))
    intent=True;d.audit('codex','announcement_durable_send_intent',job['job_id'],digest)
    (folder/'commit.json').write_text(encode({'action':'publish','attempt_id':attempt,'caption_sha256':digest}),'utf-8')
   if proc.poll() is not None and not result.exists():raise RuntimeError('BROWSER_EXIT_WITHOUT_RESULT')
   time.sleep(.2)
  if outcome is None:raise TimeoutError('BROWSER_RESULT_TIMEOUT')
  state=outcome['state']
  if state in ('published_visible','existing_visible'):
   evidence=outcome['evidence'];assert evidence['site_uid']==request['uid'] and evidence['caption_sha256']==digest and evidence['full_approved_text_observed']
   found=re.fullmatch(r'https://(?:t\.bilibili\.com/(\d+)/?|www\.bilibili\.com/opus/(\d+)/?)',evidence['url']);assert found
   external=found[1] or found[2];record={'actor':'Codex','publication':{'state':'completed','external_id':external,'url':evidence['url'],
    'kind':'owner_requested_project_progress','transport':'server_website_ui'},'attempt_id':attempt,'evidence_folder':str(folder),'caption_sha256':digest,
    'human_translation_review_claimed':False,'automatic_tweet_policy_used':False}
   with d.transaction() as db:db.execute("UPDATE jobs SET state='completed',stage='operator_progress_visible',updated_at=?,error_code=NULL,external_publish_id=?,result=?,lease_owner=NULL,lease_until=NULL WHERE job_id=?",(time.time(),external,encode(record),job['job_id']))
   d.audit('codex','progress_announcement_visible',job['job_id'],evidence['url'])
   print(encode({'state':'completed','job_id':job['job_id'],'url':evidence['url'],'attempt_id':attempt,'caption_sha256':digest}))
  else:
   state='publish_unknown' if intent or outcome.get('publish_control_clicked') else 'failed'
   code=outcome.get('error_code','ANNOUNCEMENT_RESULT_UNKNOWN')
   with d.transaction() as db:db.execute('UPDATE jobs SET state=?,stage=?,updated_at=?,error_code=?,result=?,lease_owner=NULL,lease_until=NULL WHERE job_id=?',
    (state,'operator_'+outcome.get('stage','lookup'),time.time(),code,encode({'dispatch_intent':intent,'outcome':outcome,'evidence_folder':str(folder),'caption_sha256':digest}),job['job_id']))
   print(encode({'state':state,'error_code':code,'job_id':job['job_id'],'evidence_folder':str(folder),'sent':outcome.get('sent'),'stage':outcome.get('stage')}))
 except BaseException:
  with d.transaction() as db:db.execute("UPDATE jobs SET state=?,error_code='OPERATOR_ANNOUNCEMENT_INTERRUPTED',updated_at=?,result=? WHERE job_id=?",('publish_unknown' if intent else 'failed',time.time(),encode({'dispatch_intent':intent,'evidence_folder':str(folder),'caption_sha256':digest}),job['job_id']))
  raise
 finally:
  if proc.poll() is None:
   try:proc.wait(timeout=10)
   except subprocess.TimeoutExpired:
    proc.terminate()
    try:proc.wait(timeout=10)
    except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
