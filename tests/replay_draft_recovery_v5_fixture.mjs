// One single-file editor, not an initial batch or a multipart UI assumption.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash,randomUUID} from 'node:crypto';
import readline from 'node:readline';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {createReplayNativeDriver,validateReplayManifest} from '../adapters/replay_native_driver.mjs';
import {canonicalDigest} from '../scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs';
const [root,file,scenario]=process.argv.slice(2),spec=JSON.parse(await fs.readFile(file,'utf8'));
validateReplayManifest(spec);assert.throws(()=>validateReplayManifest({...spec,highest_contract:{}}));
assert.throws(()=>validateReplayManifest({...spec,version:'replay-highest-native-browser-manifest-v2'}));
const bad=structuredClone(spec);bad.source_contract.source_actual_start_time='2026-10-31T15:59:59Z';assert.throws(()=>validateReplayManifest(bad));
let selections=0,images=0,saves=0,submits=0,outside=0,recovering=false,published=false;
const editor='https://member.bilibili.com/platform/upload/video/frame',draftURL=editor+'?type=draft&draftId=4069503',manager='https://member.bilibili.com/platform/upload-manager/article',bv='BV1234567890';
const esc=s=>s.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const browser=await chromium.launch({headless:true,chromiumSandbox:true}),context=await browser.newContext();
await context.exposeBinding('ownedVideo',async()=>{selections++;const p=path.join(root,'media',spec.job_id,'replay-native-intents/video-selection.json'),body=JSON.parse(await fs.readFile(p,'utf8'));assert.equal(body.version,'replay-part-one-use-intent-v5');assert.equal(body.parent_job_id,spec.parent_job_id);assert.equal(body.part_index,0);});
await context.exposeBinding('ownedSubmit',()=>{submits++;published=scenario==='once_public';});await context.exposeBinding('ownedSave',()=>saves++);await context.exposeBinding('ownedImage',()=>images++);
const media=await fs.readFile(spec.file);
await context.route('**/*',async r=>{const u=new URL(r.request().url());let html;
 if(u.href==='https://t.bilibili.com/')html='<a class="header-entry-mini" href="https://space.bilibili.com/'+spec.target_site_uid+'/">Owned identity</a>';
 else if(u.href===editor||u.href===draftURL){
  const sidebar=scenario==='foreign_sidebar'?'<div class="task-list-content-item"><div class="task task-selected"><span class="task-title-text">original</span></div></div><div class="task-list-content-item">Other draft</div>':'';
  const form=sidebar+'<input name="buploader" type="file" accept=".mp4"><div class="file-list"><div class="file-item"><span class="title-text" title="'+path.basename(spec.file)+'">'+path.basename(spec.file)+'</span><div>上传完成</div></div></div><input placeholder="请输入稿件标题" value="'+esc(spec.metadata.title)+'"><div class="ql-editor" contenteditable="true">'+spec.metadata.description.split('\n').map(t=>'<p>'+esc(t)+'</p>').join('')+'</div><input readonly placeholder="请选择符合您视频内容的创作声明" value="内容为转载"><input placeholder="转载视频请注明来源、时间、地点(例：转自https://www.xxxx.com/yyyy)，注明来源会更快地通过审核哦" value="'+spec.source_url+'"><div class="video-human-type"><div class="select-controller">娱乐</div></div><div class="label-item-v2-container">'+spec.metadata.tags.map(tag=>'<p class="label-item-v2-content">'+esc(tag)+'</p>').join('')+'</div><input placeholder="按回车键Enter创建标签"><div class="cover-main"><div class="cover-module-main" style="width:300px;height:120px" onclick="document.querySelector(\'.cover-editor\').style.display=\'block\'">添加封面</div></div><div class="cover-editor" style="display:none"><div class="cover-upload cover-editor-panel-select-item" onclick="document.querySelector(\'#image\').click()">上传封面</div><input id="image" type="file" accept="image/png" onchange="ownedImage()"><div class="cover-editor-button"><span class="button" onclick="document.querySelector(\'.cover-editor\').style.display=\'none\';const c=document.querySelector(\'.cover-module-main\');c.textContent=\'\';c.style.backgroundImage=\'linear-gradient(white,white)\'">完成</span></div></div><span class="submit-draft" onclick="ownedSave();document.querySelector(\'#toast\').textContent=\'已存入草稿箱\'">存草稿</span><div id="toast"></div><span class="submit-add" onclick="ownedSubmit();document.body.innerHTML=\'<input name=buploader type=file accept=.mp4><p>Owned submitted</p>\'">立即投稿</span>';
  html='<input id="video" type="file" name="buploader" multiple accept=".mp4"><div class="bcc-upload"><div class="upload-area" style="width:400px;height:80px">上传视频</div></div><p>视频大小16G以内，时长10小时以内</p><script>document.querySelector(".upload-area").onclick=()=>document.querySelector("#video").click();document.querySelector("#video").onchange=async e=>{if(e.target.files.length!==1)throw new Error("Owned forbids batch selection");await ownedVideo();document.body.innerHTML='+JSON.stringify(form)+';};</script>';
  if(recovering)html=form.replace('width:300px;height:120px','width:300px;height:120px;background-image:linear-gradient(white,white)').replace('>添加封面</div>','></div>');
 }else if(u.href===manager){
  const d=new Date(Date.now()+8*3600000),stamp=d.getUTCFullYear()+'年'+String(d.getUTCMonth()+1).padStart(2,'0')+'月'+String(d.getUTCDate()).padStart(2,'0')+'日 '+String(d.getUTCHours()).padStart(2,'0')+':'+String(d.getUTCMinutes()).padStart(2,'0')+':'+String(d.getUTCSeconds()).padStart(2,'0');
  html='<h1>稿件管理</h1><a href="/platform/upload-manager/article">视频管理</a><a href="/platform/upload-manager/opus">图文管理</a><a href="/platform/upload-manager/inactArticle">互动视频管理</a><div>全部稿件 42</div><div>草稿 1</div><div class="article-card clearfix v2"><div class="meta-title"><a class="name ellipsis" href="https://www.bilibili.com/video/'+bv+'/">'+esc(spec.metadata.title)+'</a></div><a class="cover-wrp" href="https://www.bilibili.com/video/'+bv+'/"><span class="duration">00:00:01</span></a><div class="pubdate is-success"><span class="date">'+stamp+'</span></div></div>';
  if(scenario==='pending_manager')html=html.replaceAll('href="https://www.bilibili.com/video/','href="//www.bilibili.com/video/').replace('00:00:01','00:00:00').replace('pubdate is-success','pubdate')+'<script>document.querySelector(".article-card").insertAdjacentHTML("beforeend","<div>转码中</div><div>审核中</div>")</script>';
  if(scenario==='submit_manager_loading')html='<aside>主站 投稿 首页 内容管理 稿件管理 申诉管理 数据中心</aside><script>setTimeout(()=>document.body.innerHTML='+JSON.stringify(html)+',6200)</script>';

  if(recovering&&!published){
   const card='<section class="bcc-content-manager-article-draft"><div class="bcc-cmad-content-box"><section class="bcc-cmad-card card"><a class="bcc-cmadc-cover-box" href="'+draftURL+'"><img alt="'+esc(spec.metadata.title)+'"></a><div class="bcc-cmadc-title-time"><p class="title" title="'+esc(spec.metadata.title)+'"><a href="'+draftURL+'">'+esc(spec.metadata.title)+'</a></p><p class="time">2026年11月03日 05:26:15</p></div></section></div></section><div class="bcc-pagination-total"> 共1页 / 1个， </div><li class="bcc-pagination-item selected"><a>1</a></li>';
   html='<h1>稿件管理</h1><a href="/platform/upload-manager/article">视频管理</a><a href="/platform/upload-manager/opus">图文管理</a><a href="/platform/upload-manager/inactArticle">互动视频管理</a><div>全部稿件 0</div><button id=draft>草稿 1</button><div id=listing></div><script>document.querySelector("#draft").onclick=()=>{history.pushState({},"","?group=draft&page=1");document.querySelector("#listing").innerHTML='+JSON.stringify(card)+'};</script>';
  }
 }else if(u.origin==='https://www.bilibili.com'&&['/video/'+bv,'/video/'+bv+'/'].includes(u.pathname))html='<h1>'+esc(spec.metadata.title)+'</h1><a class="header-entry-mini" href="https://space.bilibili.com/999/">Owned viewer</a><div class="up-info-container"><a class="up-name" href="https://space.bilibili.com/'+spec.target_site_uid+'/">'+esc(spec.account_name)+'</a></div><div class="basic-desc-info" style="white-space:pre-wrap">'+spec.source_url+'\n'+esc(spec.metadata.description)+'</div><video muted autoplay controls width=320 height=180 src="/owned.mp4"></video>'+
  (scenario==='public_no_progress'?'<script>HTMLMediaElement.prototype.play=function(){this.pause();return Promise.resolve();}</script>':'')+
  (scenario==='public_challenge'?'<div class="geetest_panel" style="width:200px;height:100px">请完成验证</div>':'');
 else if(u.href==='https://www.bilibili.com/owned.mp4'){const range=/^bytes=(\d+)-(\d*)$/.exec(r.request().headers().range||''),start=range?Number(range[1]):0,end=range&&range[2]?Math.min(Number(range[2]),media.length-1):media.length-1;await r.fulfill({status:range?206:200,contentType:'video/mp4',headers:{'x-owned-fixture':'true','accept-ranges':'bytes','content-length':String(end-start+1),...(range?{'content-range':`bytes ${start}-${end}/${media.length}`}:{})},body:media.subarray(start,end+1)});return;}
 else{outside++;await r.abort();return;}
 await r.fulfill({status:200,contentType:'text/html',headers:{'x-owned-fixture':'true'},body:'<!doctype html><meta charset="utf-8"><style>input{display:block;width:600px}.ql-editor{min-height:90px}.file-item{padding:5px}span{display:inline-block}a{display:inline-block;padding:3px}</style><body class="risk-captcha-adapt risk-captcha-adapt-pc">'+html+'</body>'});
});

const page=await context.newPage(),driver=await createReplayNativeDriver({page,context,spec,root,uploadTimeoutMs:2000,resultTimeoutMs:300,postSubmitObservationMs:100});
const reference=async file=>{const raw=await fs.readFile(file);return {file,bytes:raw.length,sha256:createHash('sha256').update(raw).digest('hex')};};
const grantFile=path.join(root,'media',spec.job_id,'draft-recovery-v1/grant.json');
const originalLstat=fs.lstat,originalOpen=fs.open;let readonlyGrantOpens=0;
// Explicit owned root-filesystem seam: the Ubuntu test process is UID10001.
// Only this exact fixture grant receives simulated root ownership/mode. All
// raw file bytes, stable inode checks, refs, source QA and native UI are real.
const sealed=stat=>{stat.uid=0;stat.mode=(stat.mode&~0o777)|(scenario==='writable_grant'?0o644:0o444);return stat;};
fs.lstat=async(file,...args)=>{const st=await originalLstat(file,...args);return file===grantFile?sealed(st):st;};
fs.open=async(file,...args)=>{
 if(file===grantFile){assert.ok(typeof args[0]==='number'&&args[0]===0x20000||typeof args[0]==='number'&&args[0]===0,'root grant must use read-only open');readonlyGrantOpens++;const h=await originalOpen(file,...args);return {stat:async()=>sealed(await h.stat()),readFile:(...a)=>h.readFile(...a),close:()=>h.close()};}
 return originalOpen(file,...args);
};
try{
 await driver.perform('upload');const prepared=(await driver.perform('prepare')).native_preparation;
 const binding=Object.fromEntries(['job_id','parent_job_id','attempt_id','site_uid','source_id','source_url','artifact_sha256','file_bytes','metadata_sha256','binding_sha256','part_index','part_count','part_idempotency_key','whole_plan_sha256','parts_manifest_sha256','policy_sha256'].map(k=>[k,prepared[k]]));
 const intentFile=path.join(root,'media',spec.job_id,'replay-native-intents/public-submit.json');
 const originalTicket={version:'replay-part-native-reservation-v5',...binding,idempotency_key:spec.part_idempotency_key,machine_evidence_sha256:'c'.repeat(64),upload_reference_sha256:prepared.upload_reference_sha256};
 await fs.writeFile(intentFile,JSON.stringify({version:'replay-part-one-use-intent-v5',...binding,kind:'public-submit',created_at:new Date().toISOString(),reservation:originalTicket})+'\n',{flag:'wx',mode:0o600});
 const oldIntent=await fs.readFile(intentFile),oldRef=await reference(intentFile);
 recovering=true;const observed=await driver.perform('lookup');assert.equal(observed.native_result_evidence.state,'draft_found');
 const draft=observed.native_result_evidence,issued=Date.now()/1000;
 const grant={version:'replay-part-draft-recovery-grant-v1',grant_id:randomUUID(),binding,idempotency_key:spec.part_idempotency_key,
  original_submit_intent_sha256:oldRef.sha256,draft_id:'4069503',draft_url:draftURL,upload_reference_sha256:prepared.upload_reference_sha256,
  read_only_draft_reference:draft.draft_reference,issued_at:issued-1,expires_at:issued+298,owner_response:'Owned authorization fixture only',owner_response_reference:'owned-fixture',
  max_clicks:1,upload_allowed:false,save_allowed:false,human_review:false,publication_authorized:true};
 if(scenario==='wrong_draft'){grant.draft_id='4069103';grant.draft_url=editor+'?type=draft&draftId=4069103';}
 await fs.mkdir(path.dirname(grantFile),{recursive:true});await fs.writeFile(grantFile,JSON.stringify(grant)+'\n',{flag:'wx',mode:0o600});
 const ticket={version:'replay-part-draft-recovery-reservation-v1',...binding,ticket_id:randomUUID(),idempotency_key:spec.part_idempotency_key,
  grant_reference:await reference(grantFile),original_submit_intent_sha256:oldRef.sha256,draft_id:grant.draft_id,draft_url:grant.draft_url,
  upload_reference_sha256:prepared.upload_reference_sha256,machine_evidence_sha256:originalTicket.machine_evidence_sha256,
  state:'recovery_dispatching',can_click_submit:true,issued_at:issued,expires_at:grant.expires_at};
 if(scenario==='expired_grant')ticket.expires_at=issued-1;
 if(scenario==='intent_changed')await fs.appendFile(intentFile,' ');
 if(scenario==='file_changed')await fs.appendFile(spec.file,'x');
 if(scenario==='grant_changed')await fs.appendFile(grantFile,' ');
 let result,error;try{result=await driver.perform('submit',{reservation:ticket});}catch(e){error=e.code;}
 const positive=scenario==='once_unknown'||scenario==='once_public';
 if(positive){
  assert.equal(error,undefined);assert.equal(submits,1);assert.equal(result.state,scenario==='once_public'?'completed':'unknown');
  const recovery=JSON.parse(await fs.readFile(path.join(root,'media',spec.job_id,'replay-native-intents/public-submit-draft-recovery-v1.json'),'utf8'));
  assert.equal(recovery.version,'replay-part-draft-recovery-one-use-v1');assert.deepEqual(recovery.original_submit_intent,oldRef);
  assert.deepEqual(recovery.grant_reference,ticket.grant_reference);
  await assert.rejects(()=>driver.perform('submit',{reservation:ticket}),e=>e.code==='REPLAY_BROWSER_RECONCILE_REQUIRED');
  assert.equal(submits,1);assert.deepEqual(await fs.readFile(intentFile),oldIntent);
 }else{
  assert.ok(error,scenario+' must fail before any recovery click');assert.equal(submits,0);
  await assert.rejects(()=>fs.stat(path.join(root,'media',spec.job_id,'replay-native-intents/public-submit-draft-recovery-v1.json')),e=>e.code==='ENOENT');
 }
 assert.equal(selections,1);assert.equal(saves,1);assert.equal(images,1);assert.equal(outside,0);
 if(positive)assert.ok(readonlyGrantOpens>=3);
 console.log(JSON.stringify({scenario,owned:true,root_filesystem_seam:true,recovery_submits:submits,extra_selections:selections-1,extra_saves:saves-1,external_requests:outside}));
}finally{fs.lstat=originalLstat;fs.open=originalOpen;await context.close();await browser.close();}
