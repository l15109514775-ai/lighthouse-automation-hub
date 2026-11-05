// Real driver; routed native pages and async UI only, never the public site.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {createReplayNativeDriver} from '../adapters/replay_native_driver.mjs';
const [root,file,scenario]=process.argv.slice(2),spec=JSON.parse(await fs.readFile(file,'utf8'));
const editor='https://member.bilibili.com/platform/upload/video/frame',manager='https://member.bilibili.com/platform/upload-manager/article',bv='BV1234567890';
const esc=s=>s.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
let selections=0,images=0,saves=0,submits=0,confirmations=0,outside=0,originalManagerNavigations=0,secondaryManagers=0,protectedPages=[],failedScreenshots=0;
let original;
const browser=await chromium.launch({headless:true,chromiumSandbox:true}),context=await browser.newContext();
await context.exposeBinding('ownedVideo',()=>selections++);await context.exposeBinding('ownedImage',()=>images++);
await context.exposeBinding('ownedSave',()=>saves++);await context.exposeBinding('ownedSubmit',()=>submits++);await context.exposeBinding('ownedConfirm',()=>confirmations++);
const media=await fs.readFile(spec.file);
await context.route('**/*',async r=>{
 const u=new URL(r.request().url());let html;
 if(u.href==='https://t.bilibili.com/')html='<a class="header-entry-mini" href="https://space.bilibili.com/'+spec.target_site_uid+'/">Owned identity</a>';
 else if(u.href===editor){
  const form='<input name="buploader" type="file" accept=".mp4"><div class="file-list"><div class="file-item"><span class="title-text" title="'+path.basename(spec.file)+'">'+path.basename(spec.file)+'</span><div>上传完成</div></div></div><input placeholder="请输入稿件标题" value="'+esc(spec.metadata.title)+'"><div class="ql-editor" contenteditable="true">'+spec.metadata.description.split('\n').map(t=>'<p>'+esc(t)+'</p>').join('')+'</div><input readonly placeholder="请选择符合您视频内容的创作声明" value="内容为转载"><input placeholder="转载视频请注明来源" value="'+spec.source_url+'"><div class="video-human-type"><div class="select-controller">娱乐</div></div><div class="label-item-v2-container">'+spec.metadata.tags.map(t=>'<p class="label-item-v2-content">'+esc(t)+'</p>').join('')+'</div><input placeholder="按回车键Enter创建标签"><div class="cover-main"><div class="cover-module-main" style="width:300px;height:120px" onclick="document.querySelector(\'.cover-editor\').style.display=\'block\'">添加封面</div></div><div class="cover-editor" style="display:none"><div class="cover-upload cover-editor-panel-select-item" onclick="document.querySelector(\'#image\').click()">上传封面</div><input id="image" type="file" accept="image/png" onchange="ownedImage()"><div class="cover-editor-button"><span class="button" onclick="document.querySelector(\'.cover-editor\').style.display=\'none\';const c=document.querySelector(\'.cover-module-main\');c.textContent=\'\';c.style.backgroundImage=\'linear-gradient(white,white)\'">完成</span></div></div><span class="submit-draft" onclick="ownedSave();document.querySelector(\'#toast\').textContent=\'已存入草稿箱\'">存草稿</span><div id="toast"></div><span class="submit-add">立即投稿</span>';
  const later=scenario==='async_validation'?'<p role="alert">Owned validation pending: title rejected</p><input required aria-label="Owned required field">':
   scenario==='async_confirmation'?'<div role="dialog" style="width:400px;height:120px">Owned second confirmation<button onclick="ownedConfirm()">继续投稿</button></div>':
   ['async_challenge','diagnostic_screenshot_failure'].includes(scenario)?'<div class="geetest_panel" style="width:400px;height:120px">请完成验证 Owned actual challenge</div>':'<p>Owned submission received</p>';
  const clicked='ownedSubmit();setTimeout(()=>document.body.insertAdjacentHTML("beforeend",'+JSON.stringify(later)+'),140);';
  html='<input id="video" type="file" name="buploader" accept=".mp4"><div class="bcc-upload"><div class="upload-area" style="width:400px;height:80px">上传视频</div></div><p>视频大小16G以内，时长10小时以内</p><script>document.querySelector(".upload-area").onclick=()=>document.querySelector("#video").click();document.querySelector("#video").onchange=async()=>{await ownedVideo();document.body.innerHTML='+JSON.stringify(form)+';document.querySelector(".submit-add").onclick=()=>{'+clicked+'};};</script>';
 }else if(u.href===manager){
  if(r.request().frame().page()===original)originalManagerNavigations++;else secondaryManagers++;
  html='<h1>稿件管理</h1><a href="/platform/upload-manager/article">视频管理</a><a href="/platform/upload-manager/opus">图文管理</a><a href="/platform/upload-manager/inactArticle">互动视频管理</a><div>全部稿件 2</div><div>草稿 2</div>';
  if(scenario==='manager_challenge_retained')html+='<div class="geetest_panel" style="width:400px;height:120px">请完成验证 Owned manager challenge</div>';
  if(scenario==='accepted_public'){
   const d=new Date(Date.now()+8*3600000),stamp=d.getUTCFullYear()+'年'+String(d.getUTCMonth()+1).padStart(2,'0')+'月'+String(d.getUTCDate()).padStart(2,'0')+'日 '+String(d.getUTCHours()).padStart(2,'0')+':'+String(d.getUTCMinutes()).padStart(2,'0')+':'+String(d.getUTCSeconds()).padStart(2,'0');
   html+='<div class="article-card clearfix v2"><a class="name ellipsis" href="https://www.bilibili.com/video/'+bv+'/">'+esc(spec.metadata.title)+'</a><a class="cover-wrp" href="https://www.bilibili.com/video/'+bv+'/"><span class="duration">00:00:01</span></a><div class="pubdate is-success"><span class="date">'+stamp+'</span></div></div>';
  }
 }else if(u.origin==='https://www.bilibili.com'&&['/video/'+bv,'/video/'+bv+'/'].includes(u.pathname)){
  html='<h1>'+esc(spec.metadata.title)+'</h1><div class="up-info-container"><a class="up-name" href="https://space.bilibili.com/'+spec.target_site_uid+'/">'+esc(spec.account_name)+'</a></div><div class="basic-desc-info" style="white-space:pre-wrap">'+esc(spec.metadata.description)+'</div><video muted autoplay controls width=320 height=180 src="/owned.mp4"></video>';
 }else if(u.href==='https://www.bilibili.com/owned.mp4'){
  const range=/^bytes=(\d+)-(\d*)$/.exec(r.request().headers().range||''),start=range?Number(range[1]):0,end=range&&range[2]?Math.min(Number(range[2]),media.length-1):media.length-1;
  await r.fulfill({status:range?206:200,contentType:'video/mp4',headers:{'accept-ranges':'bytes','content-length':String(end-start+1),...(range?{'content-range':`bytes ${start}-${end}/${media.length}`}:{})},body:media.subarray(start,end+1)});return;
 }else{outside++;await r.abort();return;}
 await r.fulfill({status:200,contentType:'text/html',body:'<!doctype html><meta charset="utf-8"><style>input{display:block;width:600px}.ql-editor{min-height:90px}span,a{display:inline-block;padding:3px}</style><body class="risk-captcha-adapt risk-captcha-adapt-pc">'+html+'</body>'});
});
try{
 original=await context.newPage();
 const driver=await createReplayNativeDriver({page:original,context,spec,root,uploadTimeoutMs:2000,resultTimeoutMs:300,postSubmitObservationMs:600,
  onProtection:async event=>{if(scenario==='manager_challenge_retained')assert.notEqual(event.page,original);else assert.equal(event.page,original);protectedPages.push(event.page);}});
 await driver.perform('upload');const prepared=(await driver.perform('prepare')).native_preparation,issued=Date.now()/1000;
 if(scenario==='diagnostic_screenshot_failure'){
  const realScreenshot=original.screenshot.bind(original);
  original.screenshot=async options=>{if(String(options.path).includes('post-submit-observations')){failedScreenshots++;throw new Error('Owned optional screenshot failure');}return realScreenshot(options);};
 }
 const ticket={...Object.fromEntries(['job_id','parent_job_id','attempt_id','site_uid','source_id','source_url','artifact_sha256','file_bytes','metadata_sha256','binding_sha256','part_index','part_count','part_idempotency_key','whole_plan_sha256','parts_manifest_sha256','policy_sha256'].map(k=>[k,prepared[k]])),version:'replay-part-native-reservation-v5',ticket_id:randomUUID(),idempotency_key:spec.part_idempotency_key,upload_reference_sha256:prepared.upload_reference_sha256,machine_evidence_sha256:'c'.repeat(64),can_click_submit:true,state:'dispatching',issued_at:issued-1,expires_at:issued+298};
 let result,error;try{result=await driver.perform('submit',{reservation:ticket});}catch(e){error=e;}
 assert.equal(submits,1);assert.equal(confirmations,0);assert.equal(originalManagerNavigations,0,'result lookup must not navigate the original submit page');assert.equal(original.url(),editor);
 const folder=path.join(driver.output,'post-submit-observations'),names=await fs.readdir(folder),records=[];
 for(const name of names){const directory=path.join(folder,name);try{records.push(JSON.parse(await fs.readFile(path.join(directory,'observation.json'),'utf8')));}catch(e){if(e.code!=='ENOENT')throw e;}}
 if(scenario!=='diagnostic_screenshot_failure')assert.ok(records.length>=2);
 assert.ok(records.every(r=>r.additional_submit_clicks===0&&r.human_review===false&&r.public_submission_confirmed===false));
 assert.ok(records.every(r=>r.visible_dom.url===editor));
 for(const r of records)assert.ok((await fs.stat(r.screenshot.file)).size>0);
 if(scenario==='async_validation'){assert.equal(result?.state,'unknown');assert.ok(records.some(r=>r.visible_dom.visible_text.includes('Owned validation pending')));assert.ok(secondaryManagers>=1);assert.equal(protectedPages.length,0);}
 if(scenario==='async_confirmation'){assert.equal(error?.code,'BILIBILI_VIDEO_VISIBLE_DIALOG_REQUIRES_REVIEW');assert.ok(records.some(r=>r.visible_dom.visible_text.includes('Owned second confirmation')));assert.equal(protectedPages.length,1);}
 if(scenario==='async_challenge'){assert.equal(error?.code,'BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED');assert.ok(records.some(r=>r.visible_dom.visible_text.includes('Owned actual challenge')));assert.equal(protectedPages.length,1);}
 if(scenario==='accepted_public'){assert.equal(result?.state,'completed');assert.equal(result.result_observation.same_submit_attempt_observed,false);assert.ok(secondaryManagers>=1);}
 if(scenario==='manager_challenge_retained'){assert.equal(error?.code,'BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED');assert.equal(protectedPages.length,1);assert.equal(protectedPages[0].isClosed(),false);assert.equal(protectedPages[0].url(),manager);assert.ok(secondaryManagers>=1);}
 if(scenario==='diagnostic_screenshot_failure'){assert.equal(error?.code,'BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED');assert.equal(protectedPages.length,1);assert.equal(protectedPages[0],original);assert.ok(failedScreenshots>=2);assert.equal(original.isClosed(),false);assert.equal(records.length,0);}
 const intent=await fs.readFile(path.join(root,'media',spec.job_id,'replay-native-intents/public-submit.json'));
 await assert.rejects(()=>driver.perform('submit',{reservation:ticket}),e=>e.code==='REPLAY_BROWSER_RECONCILE_REQUIRED');
 assert.deepEqual(await fs.readFile(path.join(root,'media',spec.job_id,'replay-native-intents/public-submit.json')),intent);
 assert.equal(submits,1);assert.equal(selections,1);assert.equal(saves,1);assert.equal(outside,0);
 console.log(JSON.stringify({scenario,owned:true,selections,submits,images,saves,platform_actions:0,external_requests:outside}));
}finally{await context.close();await browser.close();}
