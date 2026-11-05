"""One explicitly authorized maintenance share-dialog open, DB0/final-send0."""
from pathlib import Path
import json,os,subprocess,time,types,uuid
from services.hub.config import Settings
from services.hub.db import Store
from services.hub.locks import resource_guard
from services.hub import tweet_repost_check_v1 as old

NODE="\nimport fs from 'node:fs/promises';\nimport path from 'node:path';\nimport crypto from 'node:crypto';\nimport {chromium} from '/opt/lighthouse-hub/compositor/node_modules/playwright/index.mjs';\nimport {validateRepostSpec,observeRepostControl} from '/opt/lighthouse-hub/adapters/browser_tweet_repost_check_v1.mjs';\nconst sha=x=>crypto.createHash('sha256').update(x).digest('hex');\nlet input='';for await(const x of process.stdin){input+=x;if(input.length>40000)throw new Error('SPEC_TOO_LARGE');}\nconst envelope=JSON.parse(input);if(sha(envelope.request_json)!==envelope.request_sha256)throw new Error('REQUEST_HASH_CHANGED');\nconst spec=validateRepostSpec(JSON.parse(envelope.request_json));\nif(spec.external_id!=='1266077623570661433'||spec.check_job_id!=='50c564f6-3f35-4bf8-8ab7-47823bc61cf5')throw new Error('EXACT_DIAGNOSTIC_SCOPE_REQUIRED');\nconst folder=path.join('/opt/lighthouse-hub/media',spec.check_job_id,'tweet-repost-check-v1',spec.attempt_id);\nlet context,closing;async function close(){if(!closing)closing=(async()=>{if(context)await Promise.race([context.close(),new Promise(r=>setTimeout(r,5000))]);})();return closing;}\nprocess.on('SIGTERM',()=>{close().finally(()=>process.exit(0));});\ntry{\n context=await chromium.launchPersistentContext('/opt/lighthouse-hub/data/browser-profiles/bilibili',{headless:true,chromiumSandbox:true,channel:process.env.HUB_BROWSER_CHANNEL||undefined,viewport:{width:1280,height:900},locale:'zh-CN',timezoneId:'Asia/Shanghai'});\n const page=context.pages()[0]||await context.newPage();page.setDefaultTimeout(10000);\n const result=await observeRepostControl(page,spec,folder,{requestSHA:envelope.request_sha256,intentFile:path.join(folder,'explicit-maintenance-dialog-open-intent.json')});process.stdout.write(JSON.stringify(result));\n}finally{await close();}\n"

class ReadOnly(Store):
    def connect(self):
        import sqlite3
        c=sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON');return c
    def transaction(self):raise RuntimeError('READ_ONLY_REQUIRED')

def main():
    assert os.name=='posix' and os.getuid()==os.getgid()==10001
    s=Settings.load();db=ReadOnly(s.db_path);ident='50c564f6-3f35-4bf8-8ab7-47823bc61cf5';job=db.get(ident)
    assert job['state']=='completed' and job['attempts']==1 and job['payload']['external_id']=='1266077623570661433'
    final=job['result']['final_observation'];assert final['status']=='probe_unconfirmed'
    audits=db.query('SELECT at,outcome FROM audit WHERE action=? AND target=?',(old.REINSPECTION,ident))
    accepted=[r['at'] for r in audits if json.loads(r['outcome'])==final];assert accepted
    ref=final['raw_observation'];raw=old._read(ref['file'],s.root/'media'/ident,2_000_000);native=json.loads(raw)
    old_spec={**job['payload'],'check_job_id':ident,'attempt_id':native['attempt_id']}
    assert old.validate_observation(s,old_spec,native['attempt_id'],ref,now=min(accepted))==final
    assert native['content_binding']['author_uid'] is True and native['author_identity']['site_uid']==old.UID
    parent_id=job['payload']['submission_job_id']
    def state():
        return old.digest([db.get(ident),db.get(parent_id),db.query('SELECT * FROM publications WHERE job_id=?',(parent_id,))])
    before=state();end=time.monotonic()+175
    def check():assert time.monotonic()<end and state()==before
    spec={**job['payload'],'check_job_id':ident,'attempt_id':str(uuid.uuid4()),'previous_author_identity_evidence':ref}
    original=old.subprocess
    def launch(args,**kw):
        assert args==['node',str(s.root/'adapters/browser_tweet_repost_check_v1.mjs')]
        return subprocess.Popen(['node','--input-type=module','-e',NODE],**kw)
    with resource_guard(s.root/'data/locks/tweet',s.root/'data/locks/tweet-repost-check',s.root/'data/locks/heavy'):
        try:old.subprocess=types.SimpleNamespace(Popen=launch,PIPE=subprocess.PIPE,DEVNULL=subprocess.DEVNULL);out=old.native_observe(s,spec,check)
        finally:old.subprocess=original
        check()
    actual=old.validate_observation(s,spec,spec['attempt_id'],out)
    raw=json.loads(old._read(out['file'],s.root/'media'/ident,2_000_000))
    assert actual['status']=='repost_dialog_opened_no_send' and raw['status']=='dialog_opened'
    assert raw['actions']['fills']==raw['actions']['submits']==raw['actions']['file_selections']==0 and raw['functional_probe']['final_send_clicks']==0
    assert any(d.get('kind')=='bilibili_share_editor_v1' for d in raw['functional_probe']['dialogs'])
    print(json.dumps({'version':'explicit-one-tweet-modal-fix-live-verification-v1','reference':out,'validated_observation':actual,'native_status':raw['status'],'actions':raw['actions'],'target_rows_unchanged':True,'new_business_jobs':0,'final_send_clicks':0}))
if __name__=='__main__':main()
