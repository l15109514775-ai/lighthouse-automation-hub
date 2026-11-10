// Extend the real V5 fixture with observed native draft pagination/navigation.
// All HTTP responses are routed; no account, API or platform publication.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
const variant=process.argv[5];
let source=await fs.readFile(new URL('./replay_part_native_v5_fixture.mjs',import.meta.url),'utf8');
function replaceOnce(a,b){assert.equal(source.split(a).length,2,'fixture seam changed: '+a.slice(0,70));source=source.replace(a,b);}
for(const p of ['../compositor/node_modules/playwright/index.mjs','../adapters/replay_native_driver.mjs','../scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs'])
 replaceOnce("from '"+p+"'","from '"+new URL(p,import.meta.url).href+"'");
if(variant==='collapsed_title')replaceOnce('validateReplayManifest(spec);',
 "spec.metadata.title+='  extra';spec.metadata_sha256=canonicalDigest(spec.metadata);spec.binding_sha256=canonicalDigest({owned:true,source_contract:spec.source_contract,metadata:spec.metadata});validateReplayManifest(spec);");
replaceOnce('let selections=0,images=0,saves=0,submits=0,outside=0;',
 'let selections=0,images=0,saves=0,submits=0,outside=0;let savedForm="";const variant='+JSON.stringify(variant)+';');
replaceOnce('else if(u.href===editor){','else if(u.origin+u.pathname===editor){');
replaceOnce("  html='<input id=\"video\"", `
  savedForm=form.replace('>添加封面</div>','></div>').replace('style="width:300px;height:120px"','style="width:300px;height:120px;background-image:linear-gradient(white,white)"');
  html='<input id="video"`);
replaceOnce(' }else if(u.href===manager){', `
  if(saves){
   html=savedForm;
   if(variant==='wrong_source')html=html.replace('value="'+spec.source_url+'"','value="https://www.youtube.com/watch?v=other123456"');
   if(variant==='missing_cover')html=html.replace('background-image:linear-gradient(white,white)','background-image:none').replace('></div></div><div class="cover-editor"','>添加封面</div></div><div class="cover-editor"');
   if(variant==='challenge')html+='<div class="geetest_panel" style="width:200px;height:100px">请完成验证</div>';
  }else if(!['lost_receipt','fresh_context','changed_intent','existing_submit'].includes(variant)){
   html=html.replace("ownedSave();document.querySelector(\\'#toast\\').textContent=\\'已存入草稿箱\\'", "ownedSave();location.href=\\'"+manager+"\\'");
  }
 }else if(u.href===manager){`);
replaceOnce(" }else if(u.origin==='https://www.bilibili.com'", `
  if(saves&&submits===0){
   const rows=Array.from({length:16},(_,i)=>i===0||variant==='duplicate_other_page'&&i===15?spec.metadata.title:'Other  owned draft '+i);
   html='<h1>稿件管理</h1><nav><a href="/platform/upload-manager/article">视频管理</a><a href="/platform/upload-manager/opus">图文管理</a><a href="/platform/upload-manager/inactArticle">互动视频管理</a></nav><button id="draft-tab">草稿 16</button><section class="bcc-content-manager-article-draft"></section><ul id="pager"></ul><script>'+ 
    'const rows='+JSON.stringify(rows)+',editor='+JSON.stringify(editor)+',variant='+JSON.stringify(variant)+';'+
    'function show(n){document.querySelector("section").innerHTML="";rows.slice((n-1)*10,n*10).forEach(t=>{const c=document.createElement("section");c.className="bcc-cmad-card card";const d=document.createElement("div");d.className="bcc-cmadc-title-time";const p=document.createElement("p");p.className="title";p.title=t;const a=document.createElement("a");a.href=editor;a.target="_blank";a.textContent=t;a.onclick=e=>{e.preventDefault();window.open(variant==="foreign_popup"?"about:blank":editor+"?type=draft&draftId=777")};p.append(a);d.append(p);c.append(d);document.querySelector("section").append(c)});document.querySelector("#pager").innerHTML="";[1,2].forEach(i=>{const li=document.createElement("li");li.className="bcc-pagination-item"+(i===n?" selected":"");const a=document.createElement("a");a.textContent=i;a.onclick=()=>show(i);li.append(a);document.querySelector("#pager").append(li)})}document.querySelector("#draft-tab").onclick=()=>show(1);show(1);</script>';
  }
 }else if(u.origin==='https://www.bilibili.com'`);
const start=source.indexOf('\ntry{\n if(scenario===');assert.ok(start>0);
if(variant!=='python_integration')source=source.slice(0,start)+`
try{
 await driver.perform('upload');assert.equal(selections,1);
 const intents=path.join(root,'media',spec.job_id,'replay-native-intents');
 let first;
 if(['lost_receipt','fresh_context','changed_intent','existing_submit'].includes(variant)){
  first=await driver.perform('prepare');assert.equal(first.native_preparation.private_draft_saved,true);
  await fs.unlink(path.join(driver.output,'native-preparation.json'));
  if(variant==='changed_intent'){
   const p=path.join(intents,'private-draft-save.json'),b=JSON.parse(await fs.readFile(p));b.attempt_id=randomUUID();await fs.writeFile(p,JSON.stringify(b));
  }
  if(variant==='existing_submit')await fs.writeFile(path.join(intents,'public-submit.json'),'{}');
  await page.goto(manager);
 }
 const before=await Promise.all(['video-selection','cover-completion','private-draft-save'].map(async n=>{try{return await fs.readFile(path.join(intents,n+'.json'),'utf8');}catch(e){if(e.code!=='ENOENT')throw e;return null;}}));
 const resumedPage=variant==='fresh_context'?await context.newPage():page;
 const current=await createReplayNativeDriver({page:resumedPage,context,spec,root,uploadTimeoutMs:2000,resultTimeoutMs:300});
 const errors={wrong_source:'REPLAY_BROWSER_METADATA_READBACK_FAILED',missing_cover:'REPLAY_BROWSER_COVER_MISSING',changed_intent:'REPLAY_BROWSER_PREPARATION_BINDING_CHANGED',existing_submit:'REPLAY_BROWSER_RECONCILE_REQUIRED',challenge:'REPLAY_BROWSER_DRAFT_LOOKUP_REQUIRED',duplicate_other_page:'REPLAY_BROWSER_DRAFT_LOOKUP_REQUIRED',foreign_popup:'REPLAY_BROWSER_DRAFT_LOOKUP_REQUIRED'};
 if(errors[variant]){
  await assert.rejects(()=>current.perform('prepare'),e=>e.code===errors[variant]);
  await assert.rejects(()=>fs.stat(path.join(driver.output,'native-preparation.json')),e=>e.code==='ENOENT');
 }else{
  const prepared=await current.perform('prepare'),ack=JSON.parse(await fs.readFile(path.join(driver.output,'native-draft-save-acknowledgement.json')));
  assert.equal(prepared.native_preparation.private_draft_saved,true);assert.equal(ack.method,'native_saved_draft_readback');assert.equal(ack.url,editor+'?type=draft&draftId=777');
  assert.equal(ack.new_file_selections+ack.save_clicks+ack.submit_clicks,0);
  await current.perform('prepare');
 }
 for(let i=0;i<before.length;i++)if(before[i]!==null)assert.equal(await fs.readFile(path.join(intents,['video-selection','cover-completion','private-draft-save'][i]+'.json'),'utf8'),before[i]);
 assert.equal(selections,1);assert.equal(images,1);assert.equal(saves,1);assert.equal(submits,0);assert.equal(outside,0);
 console.log(JSON.stringify({scenario,variant,owned:true,selections,images,saves,submits,platform_actions:0,external_requests:outside}));
}finally{await context.close();await browser.close();}
`;
await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
