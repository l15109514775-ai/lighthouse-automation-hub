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
let selections=0,images=0,saves=0,submits=0,outside=0;
const editor='https://member.bilibili.com/platform/upload/video/frame',manager='https://member.bilibili.com/platform/upload-manager/article',bv='BV1234567890';
const esc=s=>s.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const browser=await chromium.launch({headless:true,chromiumSandbox:true}),context=await browser.newContext();
await context.exposeBinding('ownedVideo',async()=>{selections++;const p=path.join(root,'media',spec.job_id,'replay-native-intents/video-selection.json'),body=JSON.parse(await fs.readFile(p,'utf8'));assert.equal(body.version,'replay-part-one-use-intent-v5');assert.equal(body.parent_job_id,spec.parent_job_id);assert.equal(body.part_index,0);});
await context.exposeBinding('ownedSubmit',()=>submits++);await context.exposeBinding('ownedSave',()=>saves++);await context.exposeBinding('ownedImage',()=>images++);
const media=await fs.readFile(spec.file);
await context.route('**/*',async r=>{const u=new URL(r.request().url());let html;
 if(u.href==='https://t.bilibili.com/')html='<a class="header-entry-mini" href="https://space.bilibili.com/'+spec.target_site_uid+'/">Owned identity</a>';
 else if(u.href===editor){
  const sidebar=scenario==='foreign_sidebar'?'<div class="task-list-content-item"><div class="task task-selected"><span class="task-title-text">original</span></div></div><div class="task-list-content-item">Other draft</div>':'';
  const form=sidebar+'<input name="buploader" type="file" accept=".mp4"><div class="file-list"><div class="file-item"><span class="title-text" title="'+path.basename(spec.file)+'">'+path.basename(spec.file)+'</span><div>上传完成</div></div></div><input placeholder="请输入稿件标题" value="'+esc(spec.metadata.title)+'"><div class="ql-editor" contenteditable="true">'+spec.metadata.description.split('\n').map(t=>'<p>'+esc(t)+'</p>').join('')+'</div><input readonly placeholder="请选择符合您视频内容的创作声明" value="内容为转载"><input placeholder="转载视频请注明来源" value="'+spec.source_url+'"><div class="video-human-type"><div class="select-controller">娱乐</div></div><div class="label-item-v2-container">'+spec.metadata.tags.map(tag=>'<p class="label-item-v2-content">'+esc(tag)+'</p>').join('')+'</div><input placeholder="按回车键Enter创建标签"><div class="cover-main"><div class="cover-module-main" style="width:300px;height:120px" onclick="document.querySelector(\'.cover-editor\').style.display=\'block\'">添加封面</div></div><div class="cover-editor" style="display:none"><div class="cover-upload cover-editor-panel-select-item" onclick="document.querySelector(\'#image\').click()">上传封面</div><input id="image" type="file" accept="image/png" onchange="ownedImage()"><div class="cover-editor-button"><span class="button" onclick="document.querySelector(\'.cover-editor\').style.display=\'none\';const c=document.querySelector(\'.cover-module-main\');c.textContent=\'\';c.style.backgroundImage=\'linear-gradient(white,white)\'">完成</span></div></div><span class="submit-draft" onclick="ownedSave();document.querySelector(\'#toast\').textContent=\'已存入草稿箱\'">存草稿</span><div id="toast"></div><span class="submit-add" onclick="ownedSubmit();document.body.innerHTML=\'<input name=buploader type=file accept=.mp4><p>Owned submitted</p>\'">立即投稿</span>';
  html='<input id="video" type="file" name="buploader" multiple accept=".mp4"><div class="bcc-upload"><div class="upload-area" style="width:400px;height:80px">上传视频</div></div><p>视频大小16G以内，时长10小时以内</p><script>document.querySelector(".upload-area").onclick=()=>document.querySelector("#video").click();document.querySelector("#video").onchange=async e=>{if(e.target.files.length!==1)throw new Error("Owned forbids batch selection");await ownedVideo();document.body.innerHTML='+JSON.stringify(form)+';};</script>';
 }else if(u.href===manager){
  const d=new Date(Date.now()+8*3600000),stamp=d.getUTCFullYear()+'年'+String(d.getUTCMonth()+1).padStart(2,'0')+'月'+String(d.getUTCDate()).padStart(2,'0')+'日 '+String(d.getUTCHours()).padStart(2,'0')+':'+String(d.getUTCMinutes()).padStart(2,'0')+':'+String(d.getUTCSeconds()).padStart(2,'0');
  html='<h1>稿件管理</h1><a href="/platform/upload-manager/article">视频管理</a><a href="/platform/upload-manager/opus">图文管理</a><a href="/platform/upload-manager/inactArticle">互动视频管理</a><div>全部稿件 42</div><div>草稿 1</div><div class="article-card clearfix v2"><div class="meta-title"><a class="name ellipsis" href="https://www.bilibili.com/video/'+bv+'/">'+esc(spec.metadata.title)+'</a></div><a class="cover-wrp" href="https://www.bilibili.com/video/'+bv+'/"><span class="duration">00:00:01</span></a><div class="pubdate is-success"><span class="date">'+stamp+'</span></div></div>';
  if(scenario==='pending_manager')html=html.replaceAll('href="https://www.bilibili.com/video/','href="//www.bilibili.com/video/').replace('00:00:01','00:00:00').replace('pubdate is-success','pubdate')+'<script>document.querySelector(".article-card").insertAdjacentHTML("beforeend","<div>转码中</div><div>审核中</div>")</script>';
  if(scenario==='submit_manager_loading')html='<aside>主站 投稿 首页 内容管理 稿件管理 申诉管理 数据中心</aside><script>setTimeout(()=>document.body.innerHTML='+JSON.stringify(html)+',6200)</script>';
 }else if(u.origin==='https://www.bilibili.com'&&['/video/'+bv,'/video/'+bv+'/'].includes(u.pathname))html='<h1>'+esc(spec.metadata.title)+'</h1><a class="header-entry-mini" href="https://space.bilibili.com/999/">Owned viewer</a><div class="up-info-container"><a class="up-name" href="https://space.bilibili.com/'+spec.target_site_uid+'/">'+esc(spec.account_name)+'</a></div><div class="basic-desc-info" style="white-space:pre-wrap">'+spec.source_url+'\n'+esc(spec.metadata.description)+'</div><video muted autoplay controls width=320 height=180 src="/owned.mp4"></video>'+
  (scenario==='public_no_progress'?'<script>HTMLMediaElement.prototype.play=function(){this.pause();return Promise.resolve();}</script>':'')+
  (scenario==='public_challenge'?'<div class="geetest_panel" style="width:200px;height:100px">请完成验证</div>':'');
 else if(u.href==='https://www.bilibili.com/owned.mp4'){const range=/^bytes=(\d+)-(\d*)$/.exec(r.request().headers().range||''),start=range?Number(range[1]):0,end=range&&range[2]?Math.min(Number(range[2]),media.length-1):media.length-1;await r.fulfill({status:range?206:200,contentType:'video/mp4',headers:{'x-owned-fixture':'true','accept-ranges':'bytes','content-length':String(end-start+1),...(range?{'content-range':`bytes ${start}-${end}/${media.length}`}:{})},body:media.subarray(start,end+1)});return;}
 else{outside++;await r.abort();return;}
 await r.fulfill({status:200,contentType:'text/html',headers:{'x-owned-fixture':'true'},body:'<!doctype html><meta charset="utf-8"><style>input{display:block;width:600px}.ql-editor{min-height:90px}.file-item{padding:5px}span{display:inline-block}a{display:inline-block;padding:3px}</style><body class="risk-captcha-adapt risk-captcha-adapt-pc">'+html+'</body>'});
});
const page=await context.newPage(),driver=await createReplayNativeDriver({page,context,spec,root,uploadTimeoutMs:2000,resultTimeoutMs:300});
try{
 if(scenario==='integration'){
  console.log(JSON.stringify({state:'owned_ready'}));const input=readline.createInterface({input:process.stdin,crlfDelay:Infinity});
  for await(const line of input){const request=JSON.parse(line);if(request.operation==='close')break;
   assert.ok(['upload','prepare','submit','lookup'].includes(request.operation));
   try{const result=await driver.perform(request.operation,request.arguments||{});console.log(JSON.stringify({ok:true,result}));}
   catch(error){console.log(JSON.stringify({ok:false,error_code:error.code||error.message}));}
  }
  input.close();assert.equal(selections,1);assert.equal(saves,1);assert.equal(submits,1);assert.equal(outside,0);
  console.log(JSON.stringify({scenario,owned:true,selections,submits,images,saves,platform_actions:0,external_requests:outside}));
 }else{
 if(scenario==='delivery_changed'){await fs.appendFile(spec.source_contract.delivery_manifest.file,' ');await assert.rejects(()=>driver.perform('upload'),e=>e.code==='REPLAY_PART_DOCUMENT_CHANGED');assert.equal(selections,0);}
 else if(scenario==='foreign_sidebar'){await assert.rejects(()=>driver.perform('upload'),e=>e.code==='REPLAY_PART_MULTIPLE_SUBMISSIONS_OR_FILES');assert.equal(selections,1);assert.equal(saves,0);}
 else{
  const uploaded=await driver.perform('upload');assert.equal(selections,1);assert.equal(uploaded.upload_observation.version,'replay-part-browser-upload-v5');assert.equal(uploaded.upload_observation.parent_job_id,spec.parent_job_id);
  assert.equal(uploaded.upload_observation.native_surface.surface_mode,'single_editor_no_batch_sidebar');assert.equal(uploaded.upload_observation.native_surface.actual_outer_count,0);assert.equal(uploaded.upload_observation.native_surface.actual_inner_count,1);
  await assert.rejects(()=>driver.perform('upload'),e=>e.code==='REPLAY_BROWSER_RECONCILE_REQUIRED');assert.equal(selections,1);
  if(scenario==='selected_file_changed'){const b=await fs.readFile(spec.file);b[0]^=1;await fs.writeFile(spec.file,b);await assert.rejects(()=>driver.perform('prepare'),e=>e.code==='REPLAY_BROWSER_FILE_HASH_CHANGED');assert.equal(saves,0);}
  if(['prepare_public','submit_file_changed','upload_receipt_rewritten','expired_ticket','public_no_progress','public_challenge','pending_manager','submit_manager_loading'].includes(scenario)){
   const prepared=await driver.perform('prepare');assert.equal(prepared.native_preparation.version,'replay-part-native-preparation-v5');assert.equal(images,1);assert.equal(saves,1);
   const issued=Date.now()/1000;
   const ticket={...Object.fromEntries(['job_id','parent_job_id','attempt_id','site_uid','source_id','source_url','artifact_sha256','file_bytes','metadata_sha256','binding_sha256','part_index','part_count','part_idempotency_key','whole_plan_sha256','parts_manifest_sha256','policy_sha256'].map(k=>[k,prepared.native_preparation[k]])),version:'replay-part-native-reservation-v5',ticket_id:randomUUID(),idempotency_key:spec.part_idempotency_key,upload_reference_sha256:prepared.native_preparation.upload_reference_sha256,machine_evidence_sha256:'c'.repeat(64),can_click_submit:true,state:'dispatching',issued_at:issued-1,expires_at:issued+298};
   await assert.rejects(()=>driver.perform('submit',{reservation:{...ticket,parent_job_id:'11111111-1111-4111-8111-111111111111'}}),e=>e.code==='REPLAY_PART_RESERVATION_REQUIRED');assert.equal(submits,0);
   if(scenario==='upload_receipt_rewritten'){
    const raw=await fs.readFile(spec.file);raw[0]^=1;await fs.writeFile(spec.file,raw);
    const st=await fs.stat(spec.file),file=path.join(driver.output,'upload-coverage.json'),body=JSON.parse(await fs.readFile(file,'utf8'));
    body.local_file_fingerprint=Object.fromEntries(['size','mtimeMs','ctimeMs','ino','dev','mode'].map(k=>[k,st[k]]));await fs.writeFile(file,JSON.stringify(body)+'\n');
    const reference=async file=>{const b=await fs.readFile(file);return {file,bytes:b.length,sha256:createHash('sha256').update(b).digest('hex')};};
    const uploadFile=path.join(driver.output,'upload-observation.json'),upload=JSON.parse(await fs.readFile(uploadFile,'utf8'));
    upload.upload_coverage=await reference(file);const bound=Object.fromEntries(Object.keys(ticket).filter(k=>k in prepared.native_preparation&&k!=='upload_reference_sha256').map(k=>[k,ticket[k]]));
    delete bound.version;upload.upload_reference_sha256=canonicalDigest({...bound,local_causal_intent:await reference(path.join(root,'media',spec.job_id,'replay-native-intents/video-selection.json')),native_upload_complete:true,upload_coverage:upload.upload_coverage});
    await fs.writeFile(uploadFile,JSON.stringify(upload)+'\n');
    await assert.rejects(()=>driver.perform('submit',{reservation:ticket}),e=>e.code==='REPLAY_PART_UPLOAD_FINGERPRINT_CHANGED');assert.equal(submits,0);
   }else if(scenario==='submit_file_changed'){
    const raw=await fs.readFile(spec.file);raw[0]^=1;await fs.writeFile(spec.file,raw);
    await assert.rejects(()=>driver.perform('submit',{reservation:ticket}),e=>e.code==='REPLAY_PART_UPLOAD_FINGERPRINT_CHANGED');assert.equal(submits,0);
   }else if(scenario==='expired_ticket'){
    await assert.rejects(()=>driver.perform('submit',{reservation:{...ticket,expires_at:Date.now()/1000-1}}),e=>e.code==='REPLAY_BROWSER_RESERVATION_REQUIRED');assert.equal(submits,0);
   }else if(scenario==='pending_manager'){
    const submitted=await driver.perform('submit',{reservation:ticket});assert.equal(submits,1);assert.equal(submitted.state,'awaiting_moderation');
    assert.equal(submitted.result_observation.state,'awaiting_moderation');assert.equal(submitted.result_observation.public_accessible,false);
    assert.equal(submitted.candidate_external_id,bv);assert.ok(submitted.manager_observation.file.endsWith('manager-row.json'));
    await assert.rejects(()=>driver.perform('submit',{reservation:ticket}),e=>e.code==='REPLAY_BROWSER_RECONCILE_REQUIRED');assert.equal(submits,1);
    const looked=await driver.perform('lookup');assert.equal(looked.result_observation.state,'awaiting_moderation');assert.equal(submits,1);
   }else if(['public_no_progress','public_challenge'].includes(scenario)){
    await assert.rejects(()=>driver.perform('submit',{reservation:ticket}),e=>scenario==='public_no_progress'?e.code==='REPLAY_PART_NEAR_END_PLAY_UNKNOWN':e.code==='BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED');assert.equal(submits,1);
    await assert.rejects(()=>driver.perform('submit',{reservation:ticket}),e=>e.code==='REPLAY_BROWSER_RECONCILE_REQUIRED');assert.equal(submits,1);
   }else{
   const submitted=await driver.perform('submit',{reservation:ticket});assert.equal(submits,1);assert.equal(submitted.state,'completed');assert.equal(submitted.result_observation.version,'replay-part-browser-result-v5');assert.equal(submitted.result_observation.same_submit_attempt_observed,false);
   const resultRaw=await fs.readFile(submitted.result_reference.file);assert.deepEqual(JSON.parse(resultRaw),submitted.result_observation);assert.equal(createHash('sha256').update(resultRaw).digest('hex'),submitted.result_reference.sha256);assert.ok(submitted.result_reference.file.includes('result-history'));
   const reconciliation=JSON.parse(await fs.readFile(submitted.result_observation.native_result_evidence.file,'utf8')),publicBody=JSON.parse(await fs.readFile(reconciliation.public_observation.file,'utf8'));
   assert.equal(publicBody.version,'replay-part-public-player-observation-v5');assert.equal(publicBody.player_seeks,1);assert.equal(publicBody.near_end_playback.seek_performed,true);assert.ok(publicBody.near_end_playback.after-publicBody.near_end_playback.before>.1||publicBody.near_end_playback.ended);assert.equal(publicBody.near_end_playback.seeking,false);
   await assert.rejects(()=>driver.perform('submit',{reservation:ticket}),e=>e.code==='REPLAY_BROWSER_RECONCILE_REQUIRED');assert.equal(submits,1);
   const oldRefRaw=await fs.readFile(submitted.result_reference.file);await fs.unlink(spec.file);
   const found=await driver.perform('lookup',{external_id:bv});assert.equal(found.state,'completed');assert.equal(found.result_observation.native_result_evidence.sha256.length,64);assert.equal(selections,1);assert.equal(saves,1);
   assert.notEqual(found.result_reference.file,submitted.result_reference.file);assert.deepEqual(await fs.readFile(submitted.result_reference.file),oldRefRaw);
   }
  }
 }
 assert.equal(outside,0);console.log(JSON.stringify({scenario,owned:true,selections,submits,images,saves,platform_actions:0,external_requests:outside}));
 }
}finally{await context.close();await browser.close();}
