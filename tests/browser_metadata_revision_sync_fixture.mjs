// Actual broker commands and Chromium; every document is owned/routed.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {Readable} from 'node:stream';
import {registerHooks} from 'node:module';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
const root=path.resolve(process.argv[2]),scenario=process.argv[3];
assert.ok(['sync_success','sync_wrong_old_description','sync_missing_capability','nav_unique','nav_duplicate','nav_draft_tab'].includes(scenario));
const navigation=scenario.startsWith('nav_');
const hash=x=>crypto.createHash('sha256').update(x).digest('hex');
const canonical=v=>Array.isArray(v)?'['+v.map(canonical).join(',')+']':v!==null&&typeof v==='object'?
  '{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+canonical(v[k])).join(',')+'}':JSON.stringify(v);
const digest=v=>hash(canonical(v)),id=()=>crypto.randomUUID();
const job=id(),attempt=id(),recovery=id(),download=id(),recorded=new Date().toISOString();
const sourceURL='https://www.youtube.com/watch?v=abcdefghijk',line='转载许可编号：114514（字幕组提供的许可范围）。';
const originalMetadata={title:'Owned metadata revision fixture',description:'Owned original line\n'+sourceURL+'\n'+line,
  source:sourceURL,category_label:'虚拟UP主',tags:['owned'],copyright:2};
const categoryMetadata={...originalMetadata,category_label:'娱乐'};
const metadata={...categoryMetadata,description:'Owned original line\n'+sourceURL};
const file=path.join(root,'media',download,'youtube-download-v1/original.mp4');
await fs.mkdir(path.dirname(file),{recursive:true});const artifact=Buffer.from('Owned synthetic artifact, not downloaded or uploaded');
await fs.writeFile(file,artifact);
const originalBinding={version:'short-replay-website-acceptance-v1',download_job_id:download,source_id:'abcdefghijk',
  source_url:sourceURL,channel_id:'OwnedFixtureChannel',artifact_sha256:hash(artifact),file_bytes:artifact.length,file,
  download_receipt_sha256:'a'.repeat(64),metadata:originalMetadata,metadata_sha256:digest(originalMetadata),
  site_uid:'123456',account_name:'Owned synthetic account',policy_sha256:'c'.repeat(64),human_review:false};
const categoryBinding={...originalBinding,metadata:categoryMetadata,metadata_sha256:digest(categoryMetadata)};
const finalBinding={...categoryBinding,metadata,metadata_sha256:digest(metadata)};
const first={version:'short-video-category-metadata-revision-v1',revision_id:id(),
  previous_binding:originalBinding,previous_binding_sha256:digest(originalBinding),
  previous_metadata_sha256:digest(originalMetadata),new_metadata_sha256:digest(categoryMetadata),
  new_binding_sha256:digest(categoryBinding),changed_fields:['category_label'],previous_category_label:'虚拟UP主',
  new_category_label:'娱乐',attempt_id:attempt,actual_operator_reference:'Owned category fixture',recorded_at:recorded,
  source_characteristic_confirmation:{version:'operator-transcribed-source-characteristic-v1',
    characteristic:'original_video_is_black',actual_owner_response:'Owned synthetic statement, not a real human review',
    reference:'Owned fixture',recorded_at:recorded,human_review:false,creator_original_bytes_verified:false,
    public_submission_authorized:false},remote_metadata_synchronized:false,public_submit:false};
const second={version:'public-permission-line-removal-v1',revision_id:id(),previous_binding:categoryBinding,
  previous_binding_sha256:digest(categoryBinding),previous_metadata_sha256:digest(categoryMetadata),
  new_metadata_sha256:digest(metadata),new_binding_sha256:digest(finalBinding),changed_fields:['description'],
  removed_last_line:line,previous_revision_id:first.revision_id,attempt_id:attempt,
  actual_owner_response:'转载许可编号删掉',owner_response_reference:'Owned description-only instruction fixture',
  actual_operator_reference:'Owned exact-footer fixture',recorded_at:recorded,public_submit:false,
  remote_metadata_synchronized:false,internal_rights_records_preserved:true};
const originalSpec={job_id:job,attempt_id:attempt,target_site_uid:'123456',metadata_sha256:digest(originalMetadata),
  artifact_sha256:hash(artifact),source_id:'abcdefghijk',source_url:sourceURL,download_job_id:download,file,file_bytes:artifact.length};
const spec={...originalSpec,version:'short-video-recovered-editor-v1',uploaded_attempt_id:attempt,recovery_id:recovery,
  recovery_entry:'saved_draft_manager_v1',metadata,metadata_sha256:digest(metadata),upload_metadata_sha256:digest(originalMetadata),
  metadata_revision_id:second.revision_id,metadata_revision:second,metadata_revision_history:[first,second],
  binding_sha256:digest(finalBinding),upload_reference_sha256:'b'.repeat(64),duration_seconds:297};
const old=path.join(root,'media',job,'short-video-attempts',attempt),profile=path.join(root,'data/browser-profiles/bilibili-video');
await fs.mkdir(old,{recursive:true});await fs.mkdir(profile,{recursive:true,mode:0o700});
const upload={version:'short-video-browser-upload-v1',attempt_id:attempt,site_uid:'123456',artifact_sha256:hash(artifact),
  file_bytes:artifact.length,upload_reference_sha256:spec.upload_reference_sha256,same_attempt_observed:true,server_file_hash_verified:false};
for(const [name,value] of Object.entries({'metadata.json':originalMetadata,'spec.json':originalSpec,'upload-observation.json':upload}))
  await fs.writeFile(path.join(old,name),JSON.stringify(value));
const originalFiles=Object.fromEntries(await Promise.all(['metadata.json','spec.json','upload-observation.json'].map(async name=>[name,await fs.readFile(path.join(old,name))])));
const output=path.join(root,'media',job,'recovered-editors',recovery);
process.env.HUB_ROOT=root;process.env.BILIBILI_EXPECTED_SITE_UID='123456';
Object.defineProperty(process,'stdin',{value:Readable.from([JSON.stringify(spec)]),configurable:true});
const control='/run/short-video-controls/controls.mjs';
// Execute the exact production controller bytes. The hook changes only their
// protected deployment location, not the code, capability, lineage or guards.
const controllerFile=navigation||scenario==='sync_missing_capability'?
  'short_video_saved_draft_navigation_control.mjs':'short_video_metadata_revision_sync_control.mjs';
const controlSource=await fs.readFile(new URL('../adapters/'+controllerFile,import.meta.url),'utf8');
const controlSHA=hash(controlSource),controlURL='owned-metadata-sync:'+controlSHA;
const ls=fs.lstat.bind(fs),rf=fs.readFile.bind(fs),wf=fs.writeFile.bind(fs);let syncStarts=0;
fs.writeFile=async(file,...args)=>{
  if(String(file)===path.join(output,'native-metadata-description-sync-before.json.partial'))syncStarts++;
  return wf(file,...args);
};
fs.lstat=async(file,...args)=>{
  if(String(file)===control)return {isFile:()=>true,isSymbolicLink:()=>false,uid:0,mode:0o100644,size:Buffer.byteLength(controlSource)};
  if(String(file)===path.dirname(control))return {isSymbolicLink:()=>false,uid:0,mode:0o40755};
  const value=await ls(file,...args);return process.platform==='win32'&&String(file)===profile?Object.assign(value,{mode:value.mode&~0o077}):value;
};
fs.readFile=async(file,...args)=>String(file)===control?Buffer.from(controlSource):String(file)==='/proc/self/mountinfo'?
  '999 1 0:1 / /run/short-video-controls ro - tmpfs owned-fixture ro\n':rf(file,...args);
const hooks=registerHooks({resolve(s,c,next){if(s==='./bilibili_video_guard.mjs'&&c.parentURL===controlURL)return {url:new URL('../adapters/bilibili_video_guard.mjs',import.meta.url).href,shortCircuit:true};if(s.startsWith('file:')&&s.includes('/run/short-video-controls/controls.mjs?'))return {url:controlURL,shortCircuit:true};return next(s,c);},
  load(url,c,next){if(url===controlURL)return {format:'module',source:controlSource,shortCircuit:true};return next(url,c);}});
let active,counts,phase=0,firstCommand,secondCommand,pollBusy=false,error,firstResult,secondResult,outside=0,unownedResponses=0,firstInputEvents=null,editorNavigations=0,draftTabClicks=0;const outsideURLs=[];
const escape=value=>value.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const launch=chromium.launchPersistentContext.bind(chromium);
chromium.launchPersistentContext=async(name,options)=>{
  assert.equal(options.chromiumSandbox,true);assert.equal(Object.keys(options.env).some(k=>/KEY|TOKEN|COOKIE|SECRET/.test(k)),false);
  const context=await launch(name,options);active=context;
  function watchPage(page){page.on('console',message=>{if(message.text()==='OWNED_DRAFT_TAB_CLICKED')draftTabClicks++;});}
  for(const page of context.pages())watchPage(page);context.on('page',watchPage);
  context.on('response',response=>{if(response.url().startsWith('http')&&response.headers()['x-owned-fixture']!=='true')unownedResponses++;});
  await context.route('**/*',route=>{
    const url=new URL(route.request().url());
    if(url.hostname==='t.bilibili.com')return route.fulfill({contentType:'text/html',headers:{'x-owned-fixture':'true'},body:'<a class="header-entry-mini" href="https://space.bilibili.com/123456">Owned identity</a>'});
    if(url.origin!=='https://member.bilibili.com'){outside++;outsideURLs.push(url.origin+url.pathname);return route.abort();}
    if(navigation&&url.pathname==='/platform/upload-manager/article'){
      const anchor='<a target="_blank" href="https://member.bilibili.com/platform/upload/video/frame?draft=owned-slot">'+escape(metadata.title)+'</a>';
      if(scenario==='nav_draft_tab')return route.fulfill({contentType:'text/html',headers:{'x-owned-fixture':'true'},body:'<!doctype html><meta charset="utf-8">'+
        '<body class="risk-captcha-adapt risk-captcha-adapt-pc"><h1>稿件管理</h1><nav>'+
        '<a href="https://member.bilibili.com/platform/upload-manager/article">视频管理</a>'+
        '<a href="https://member.bilibili.com/platform/upload-manager/opus">图文管理</a>'+
        '<a href="https://member.bilibili.com/platform/upload-manager/inactArticle">互动视频管理</a></nav>'+
        '<button id="draft-tab">草稿 1</button><div id="draft-list"></div><script>window.owned={edits:0,selections:0,save:0,submit:0};'+
        'document.querySelector("#draft-tab").onclick=()=>{console.log("OWNED_DRAFT_TAB_CLICKED");'+
        'setTimeout(()=>document.querySelector("#draft-list").innerHTML='+JSON.stringify(anchor)+',120);};</script></body>'});
      return route.fulfill({contentType:'text/html',headers:{'x-owned-fixture':'true'},body:'<!doctype html><meta charset="utf-8">'+
        '<body class="risk-captcha-adapt risk-captcha-adapt-pc"><p>稿件管理 视频管理 草稿 1</p>'+anchor+
        (scenario==='nav_duplicate'?anchor:'')+'<script>window.owned={edits:0,selections:0,save:0,submit:0}</script></body>'});
    }
    if(navigation){assert.equal(url.pathname,'/platform/upload/video/frame');assert.equal(url.search,'?draft=owned-slot');editorNavigations++;}
    // An owned same-origin SPA transition supplies the post-navigation form.
    // Never send redirects: Playwright can route only the first request in a
    // redirect chain. Actual broker origin/identity/metadata guards still run.
    const description=categoryMetadata.description+(scenario==='sync_wrong_old_description'?'\nOwned unrelated content':'');
    const html='<input placeholder="请输入稿件标题" value="'+escape(metadata.title)+'">'+
      '<div class="ql-editor" contenteditable="true">'+description.split('\n').map(line=>'<p>'+escape(line)+'</p>').join('')+'</div>'+
      '<input placeholder="转载视频请注明来源、时间、地点" value="'+sourceURL+'"><span>上传完成</span><input id="video" type="file" hidden>'+
      '<span class="submit-draft">存草稿</span><span class="submit-add">立即投稿</span>'+
      '<script>'+(navigation?'':'history.replaceState(null,"","/platform/upload/video/frame?draft=owned");')+'window.owned={edits:0,selections:0,save:0,submit:0};'+
      'document.querySelector(".ql-editor").oninput=()=>owned.edits++;document.querySelector("#video").onchange=()=>owned.selections++;'+
      'document.querySelector(".submit-draft").onclick=()=>owned.save++;document.querySelector(".submit-add").onclick=()=>owned.submit++;</script>';
    return route.fulfill({contentType:'text/html',headers:{'x-owned-fixture':'true'},body:'<!doctype html><meta charset="utf-8"><style>input{display:block;width:600px;height:35px}.ql-editor{width:700px;min-height:120px}</style>'+html});
  });
  const close=context.close.bind(context);context.close=async(...args)=>{
    for(const page of context.pages())if(page.url().startsWith('https://member.bilibili.com/'))
      counts=await page.evaluate(()=>({...owned,description_blocks:[...(document.querySelector('.ql-editor')?.children||[])].map(el=>({tag:el.tagName,text:el.textContent}))}));
    return close(...args);
  };
  return context;
};
async function readJSON(file){try{return JSON.parse(await rf(file,'utf8'));}catch(e){if(e.code==='ENOENT')return null;throw e;}}
async function command(action,extra={}){
  const command_id=id(),now=Date.now(),value={version:'short-video-recovered-editor-command-v1',command_id,recovery_id:recovery,
    action,created_at:new Date(now-1000).toISOString(),expires_at:new Date(now+60000).toISOString(),...extra};
  const file=path.join(output,'command.json');await fs.writeFile(file+'.owned',JSON.stringify(value));await fs.rename(file+'.owned',file);return command_id;
}
const watchdog=setTimeout(()=>{error??=new Error('Owned metadata-sync watchdog');active?.close().catch(()=>{});},35000);
const poll=setInterval(async()=>{
  if(pollBusy)return;pollBusy=true;
  try{
    const status=await readJSON(path.join(output,'status.json'));if(!status)return;
    if(phase===0&&status.state==='saved_draft_manager_waiting_for_operator'){
      phase=1;firstCommand=await command('native_control',{control_sha256:controlSHA,control_action:navigation?'lookup':'sync_metadata_revision'});
    }else if(phase===1&&firstCommand){
      const result=await readJSON(path.join(output,'command-'+firstCommand+'-result.json'));
      if(result){firstResult=result.result;
        if(scenario==='sync_success'){const current=active.pages().find(page=>page.url().startsWith('https://member.bilibili.com/platform/upload/video/frame'));firstInputEvents=await current.evaluate(()=>owned.edits);}
        phase=2;secondCommand=await command(scenario==='sync_success'?'native_control':'close',
        scenario==='sync_success'?{control_sha256:controlSHA,control_action:'sync_metadata_revision'}:{});}
    }else if(phase===2&&scenario==='sync_success'&&secondCommand){
      const result=await readJSON(path.join(output,'command-'+secondCommand+'-result.json'));
      if(result){secondResult=result.result;phase=3;await command('close');}
    }
  }catch(e){error??=e;await active?.close().catch(()=>{});}
  finally{pollBusy=false;}
},30);
try{await import('../adapters/browser_recovered_short_video.mjs?owned-sync='+id());}
finally{clearInterval(poll);clearTimeout(watchdog);hooks.deregister();fs.lstat=ls;fs.readFile=rf;fs.writeFile=wf;}
assert.ifError(error);assert.equal(outside,0,JSON.stringify(outsideURLs));assert.equal(unownedResponses,0);
assert.equal(counts.selections,0);assert.equal(counts.save,0);assert.equal(counts.submit,0);
if(scenario==='sync_success'){
  assert.equal(firstResult.state,'metadata_revision_applied_pending_broker_verification');assert.equal(firstResult.native_metadata_revision_verified,true);
  assert.equal(firstResult.native_modified,true);assert.equal(firstResult.description,metadata.description);
  assert.equal(secondResult.state,'metadata_already_current');assert.equal(secondResult.native_modified,false);
  assert.equal(secondResult.controller_invoked,false);assert.equal(syncStarts,1);assert.ok(counts.edits>0);assert.equal(counts.edits,firstInputEvents);
  assert.deepEqual(counts.description_blocks.map(x=>x.text),metadata.description.split('\n'));
  assert.ok(counts.description_blocks.every(x=>['P','DIV'].includes(x.tag)));
  const correlation=JSON.parse(await rf(path.join(output,'recovered-editor-correlation.json'),'utf8'));
  assert.equal(correlation.metadata_sha256,spec.metadata_sha256);assert.equal(correlation.new_file_selection,false);assert.equal(correlation.server_file_hash_verified,false);
}else if(navigation){
  assert.equal(syncStarts,0);assert.equal(counts.edits,0);
  assert.equal(firstResult.state,scenario==='nav_unique'||scenario==='nav_draft_tab'?'saved_draft_editor_opened':'navigation_unknown');
  assert.equal(firstResult.metadata_modified,false);assert.equal(firstResult.new_video_file_selection,false);
  assert.equal(editorNavigations,scenario==='nav_unique'||scenario==='nav_draft_tab'?1:0);
  if(scenario==='nav_unique'||scenario==='nav_draft_tab')assert.equal(firstResult.server_file_hash_verified,false);
  else assert.equal(firstResult.error_code,'NATIVE_SAVED_DRAFT_NAVIGATION_NOT_CONFIRMED');
  if(scenario==='nav_draft_tab')assert.equal(firstResult.native_draft_tab_clicked,true);
}else{
  assert.equal(firstResult.state,'blocked');assert.equal(firstResult.controller_invoked,false);
  assert.equal(firstResult.error_code,scenario==='sync_wrong_old_description'?'RECOVERY_CACHED_EDITOR_DESCRIPTION_MISMATCH':'RECOVERY_METADATA_SYNC_CAPABILITY_REQUIRED');
  assert.equal(syncStarts,0);assert.equal(counts.edits,0);
}
for(const [name,body] of Object.entries(originalFiles))assert.deepEqual(await rf(path.join(old,name)),body);
assert.equal(draftTabClicks,scenario==='nav_draft_tab'?1:0);
console.log('FIXTURE_RESULT:'+JSON.stringify({scenario,controller_file:controllerFile,controller_sha256:controlSHA,sync_start_receipt_writes:syncStarts,description_input_events:counts.edits,
  before_repeat_input_events:firstInputEvents,
  native_editor_navigations:editorNavigations,
  draft_tab_clicks:draftTabClicks,
  file_selections:counts.selections,save_clicks:counts.save,public_submit:counts.submit,external_network_calls:outside,
  unowned_http_responses:unownedResponses,real_owner_approval:false,live_tested:false}));
