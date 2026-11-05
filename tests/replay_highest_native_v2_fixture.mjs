// Real Chromium over fully routed owned pages; no account or live upload.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {createReplayNativeDriver,validateReplayManifest,streamCheckedFile} from '../adapters/replay_native_driver.mjs';
import {validateHighestContract} from '../adapters/replay_highest_contract_v2.mjs';
import {validateNaturalContract} from '../adapters/replay_natural_contract_v3.mjs';
const [root,specFile,scenario]=process.argv.slice(2),spec=JSON.parse(await fs.readFile(specFile,'utf8'));
const natural=spec.version==='replay-natural-native-browser-manifest-v3',validateContract=natural?validateNaturalContract:validateHighestContract;
const types=natural?{prepare:'replay-natural-native-preparation-v3',form:'replay-natural-native-form-observation-v3',ticket:'replay-natural-native-reservation-v3',result:'replay-natural-browser-result-v3'}:
 {prepare:'replay-highest-native-preparation-v2',form:'replay-highest-native-form-observation-v2',ticket:'replay-highest-native-reservation-v2',result:'replay-highest-browser-result-v2'};
assert.ok(['upload_once','native_limit_changed','receipt_changed','cutoff_at_submit','prepare_submit','public_coverage'].includes(scenario));
validateReplayManifest(spec);
const large=structuredClone(spec),plan=large.highest_contract.download_binding.source_plan;
if(!natural){
plan.selected_formats[0].filesize=10505641986;plan.selected_formats[1].filesize=416163003;
Object.assign(plan,{source_bytes:10921804989,planned_output_max_bytes:10985804989,workspace_overhead_bytes:32000000,
 required_working_bytes:21939609978,minimum_range_bytes:9961472,media_requests_plan:1097,metadata_request_allowance:64,
 redirect_resume_request_allowance:275,max_http_requests:1436});
large.file_bytes=10_921_804_989;large.duration_seconds=25715;large.highest_contract.download_binding.duration=25715;
large.highest_contract.download_binding_canonical=JSON.stringify(large.highest_contract.download_binding);
validateReplayManifest(large); // Only admission; no claim a 10GB file was sent.
assert.throws(()=>validateReplayManifest({...large,file_bytes:14_000_000_001}),e=>e.code==='REPLAY_BROWSER_MANIFEST_INVALID');
assert.throws(()=>validateReplayManifest({...large,duration_seconds:36001}),e=>e.code==='REPLAY_BROWSER_MANIFEST_INVALID');
assert.throws(()=>validateReplayManifest({...large,version:'replay-native-browser-manifest-v1'}),e=>e.code==='REPLAY_BROWSER_MANIFEST_INVALID');
}else assert.throws(()=>validateReplayManifest({...spec,file_bytes:14000000001}),e=>e.code==='REPLAY_BROWSER_MANIFEST_INVALID');
assert.throws(()=>validateContract({...spec,highest_contract:{...spec.highest_contract,download_policy_version:'youtube-download-v1'}}));
await streamCheckedFile(spec.file,spec.artifact_sha256,spec.file_bytes,spec.version);
let selections=0,submits=0,outside=0,unowned=0,images=0,saves=0;
const browser=await chromium.launch({headless:true,chromiumSandbox:true}),context=await browser.newContext(),page=await context.newPage();
const escape=s=>s.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
await context.exposeBinding('ownedVideo',async()=>{selections++;
 const intent=JSON.parse(await fs.readFile(path.join(root,'media',spec.job_id,'replay-native-intents/video-selection.json'),'utf8'));
 assert.equal(intent.attempt_id,spec.attempt_id);assert.equal(intent.artifact_sha256,spec.artifact_sha256);
});
await context.exposeBinding('ownedSubmit',()=>{submits++;});
await context.exposeBinding('ownedImage',()=>{images++;});
await context.exposeBinding('ownedSave',()=>{saves++;});
context.on('response',r=>{if(r.url().startsWith('http')&&r.headers()['x-owned-fixture']!=='true')unowned++;});
await context.route('**/*',async route=>{
 const u=new URL(route.request().url());let html;
 if(u.origin==='https://t.bilibili.com')html='<a class="header-entry-mini" href="https://space.bilibili.com/'+spec.target_site_uid+'">Owned UID</a>';
 else if(u.origin==='https://member.bilibili.com'&&u.pathname==='/platform/upload/video/frame'){
  const form='<input type="file" name="buploader" accept=".mp4"><div>上传完成</div><input placeholder="请输入稿件标题" value="'+escape(spec.metadata.title)+'"><div class="ql-editor" contenteditable="true">'+spec.metadata.description.split('\n').map(t=>'<p>'+escape(t)+'</p>').join('')+'</div>'+
   '<input readonly placeholder="请选择符合您视频内容的创作声明" value="内容为转载"><input placeholder="转载视频请注明来源、时间、地点" value="'+spec.source_url+'">'+
   '<div class="video-human-type"><div class="select-controller">娱乐</div></div>'+spec.metadata.tags.map(t=>'<div class="label-item-v2-container"><p class="label-item-v2-content">'+escape(t)+'</p></div>').join('')+'<input placeholder="按回车键Enter创建标签">'+
   '<div class="cover-main"><div class="cover-module-main" style="width:300px;height:120px" onclick="document.querySelector(\'.cover-editor\').style.display=\'block\'">添加封面</div></div>'+
   '<div class="cover-editor" style="display:none"><div class="cover-upload cover-editor-panel-select-item" onclick="document.querySelector(\'#image\').click()">上传封面</div><input id="image" type="file" accept="image/png" style="display:none" onchange="ownedImage()">'+
   '<div class="cover-editor-button"><span class="button" onclick="document.querySelector(\'.cover-editor\').style.display=\'none\';const c=document.querySelector(\'.cover-module-main\');c.textContent=\'\';c.style.backgroundImage=\'linear-gradient(white,white)\'">完成</span></div></div>'+
   '<span class="submit-draft" onclick="ownedSave();document.querySelector(\'#toast\').textContent=\'已存入草稿箱\'">存草稿</span><div id="toast"></div>'+
   '<span class="submit-add" onclick="ownedSubmit();document.body.innerHTML='+escape(JSON.stringify('<input name="buploader" type="file" accept=".mp4"><div>'+escape(spec.metadata.title)+'</div><div>'+spec.source_url+'</div><a href="https://www.bilibili.com/video/BV1234567890">查看视频</a><p>审核中</p>'))+'">立即投稿</span>';
  html='<input id="owned" type="file" name="buploader" accept=".mp4"><div class="bcc-upload"><div class="upload-area" style="width:400px;height:80px">上传视频</div></div><p>'+
   (scenario==='native_limit_changed'?'视频大小4G以内，时长10小时以内':'视频大小16G以内，时长10小时以内')+'</p><script>'+ 
   'document.querySelector(".upload-area").onclick=()=>document.querySelector("#owned").click();document.querySelector("#owned").onchange=async()=>{await ownedVideo();document.body.innerHTML='+JSON.stringify(form)+';};</script>';
 }else if(u.origin==='https://member.bilibili.com'&&u.pathname==='/platform/upload-manager/article')html='<h1>稿件管理</h1><a href="/platform/upload-manager/article">视频管理</a><a href="/platform/upload-manager/opus">图文管理</a><a href="/platform/upload-manager/inactArticle">互动视频管理</a><button>草稿 1</button><div>'+escape(spec.metadata.title)+'</div><div>'+spec.source_url+'</div><a href="https://www.bilibili.com/video/BV1234567890">查看视频</a><p>审核中</p>'+(scenario==='public_coverage'?'<a href="https://www.bilibili.com/video/BV0000000000">Unrelated old owned result</a>':'');
 else if(u.origin==='https://www.bilibili.com'&&u.pathname==='/video/BV1234567890')html=scenario==='public_coverage'?'<h1>'+escape(spec.metadata.title)+'</h1><a href="https://space.bilibili.com/'+spec.target_site_uid+'">Owned uploader</a><div>'+spec.source_url+'</div><video controls muted preload="auto" width="320" height="180" src="/owned.mp4"></video>':'<p>Owned unavailable public result</p>';
 else if(scenario==='public_coverage'&&u.origin==='https://www.bilibili.com'&&u.pathname==='/owned.mp4'){
  const raw=await fs.readFile(spec.file),range=/^bytes=(\d+)-(\d*)$/.exec(route.request().headers()['range']||'');
  const start=range?Number(range[1]):0,end=range&&range[2]?Math.min(Number(range[2]),raw.length-1):raw.length-1,body=raw.subarray(start,end+1);
  return route.fulfill({status:range?206:200,contentType:'video/mp4',headers:{'x-owned-fixture':'true','accept-ranges':'bytes','content-length':String(body.length),...(range?{'content-range':`bytes ${start}-${end}/${raw.length}`}:{})},body});
 }
 else{outside++;return route.abort();}
 return route.fulfill({contentType:'text/html',headers:{'x-owned-fixture':'true'},body:'<!doctype html><meta charset="utf-8"><body class="risk-captcha-adapt risk-captcha-adapt-pc">'+html+'</body>'});
});
try{
 const driver=await createReplayNativeDriver({page,context,spec,root,uploadTimeoutMs:2000,resultTimeoutMs:500});
 if(scenario==='receipt_changed'){
  await fs.appendFile(path.join(root,'media',spec.download_job_id,natural?'youtube-highest-v3/verified.json':'youtube-highest-v2/verified.json'),' ');
  await assert.rejects(()=>driver.perform('upload'),e=>e.code===(natural?'REPLAY_NATURAL_CONTRACT_REQUIRED':'REPLAY_HIGHEST_CONTRACT_REQUIRED'));assert.equal(selections,0);
 }else if(scenario==='native_limit_changed'){
  await assert.rejects(()=>driver.perform('upload'),e=>e.code==='REPLAY_HIGHEST_NATIVE_UPLOAD_LIMIT_CHANGED');assert.equal(selections,0);
 }else{
  const result=await driver.perform('upload');assert.equal(result.state,'uploaded');assert.equal(result.upload_observation.server_file_hash_verified,false);
  const uploaded=JSON.parse(await fs.readFile(result.upload_observation.upload_coverage.file,'utf8'));assert.equal(uploaded.native_filename.observed,true);assert.equal(uploaded.whole_file_streamed,true);assert.equal(uploaded.streamed_sha256,spec.artifact_sha256);
  assert.equal(selections,1);await assert.rejects(()=>driver.perform('upload'),e=>e.code==='REPLAY_BROWSER_RECONCILE_REQUIRED');assert.equal(selections,1);
  const native=JSON.parse(await fs.readFile(path.join(driver.output,'fresh-native-upload-limits.json'),'utf8'));
  assert.equal(native.site_uid,spec.target_site_uid);assert.equal(native.max_file_bytes,16000000000);assert.equal(native.server_file_hash_verified,false);
  assert.ok(spec.highest_contract.download_binding_canonical.includes(natural?'30.0':'60.0')); // Exact Python JSON preserved.
  if(scenario==='cutoff_at_submit'){
   spec.highest_contract.source_payload.raw.liveStreamingDetails.actualStartTime='2026-10-31T15:59:59Z';
   await assert.rejects(()=>driver.perform('submit',{reservation:{}}),e=>e.code===(natural?'REPLAY_NATURAL_CONTRACT_REQUIRED':'REPLAY_HIGHEST_ACTUAL_START_REQUIRED'));
   await assert.rejects(()=>fs.stat(path.join(root,'media',spec.job_id,'replay-native-intents/public-submit.json')),e=>e.code==='ENOENT');
  }
  if(['prepare_submit','public_coverage'].includes(scenario)){
   const prepared=await driver.perform('prepare');assert.equal(prepared.native_preparation.version,types.prepare);
   const inner=JSON.parse(await fs.readFile(prepared.native_preparation.native_observation_file.file,'utf8'));
   assert.equal(inner.version,types.form);assert.equal(images,1);assert.equal(saves,1);
   const ticket={version:types.ticket,job_id:spec.job_id,attempt_id:spec.attempt_id,binding_sha256:spec.binding_sha256,
    metadata_sha256:spec.metadata_sha256,can_click_submit:true,state:'dispatching',idempotency_key:'a'.repeat(64),machine_evidence_sha256:'b'.repeat(64),expires_at:Date.now()/1000+120};
   await assert.rejects(()=>driver.perform('submit',{reservation:{...ticket,version:'replay-native-reservation-v1'}}),e=>e.code==='REPLAY_BROWSER_RESERVATION_REQUIRED');
   const submitted=await driver.perform('submit',{reservation:ticket});assert.equal(submitted.state,'awaiting_moderation');
   assert.equal(submitted.result_observation.version,types.result);assert.equal(submits,1);
   await assert.rejects(()=>driver.perform('submit',{reservation:ticket}),e=>e.code==='REPLAY_BROWSER_RECONCILE_REQUIRED');assert.equal(submits,1);
  }
  const lookup=await driver.perform('lookup',{external_id:'BV1234567890'});
  // Public page unavailable here: keep moderation status, never infer public success.
  assert.equal(lookup.result_observation.version,types.result);
  assert.equal(lookup.state,scenario==='public_coverage'?'completed':'awaiting_moderation');
  if(scenario==='public_coverage'){
   assert.equal(lookup.result_observation.public_accessible,true);const coverage=JSON.parse(await fs.readFile(lookup.result_observation.cleanup_coverage.file,'utf8'));
   assert.ok(Math.abs(coverage.public_player.duration_seconds-spec.duration_seconds)<=2);assert.ok(coverage.public_player.ready_state>=2);assert.ok(coverage.public_player.seekable_ranges.length);
   assert.equal(coverage.server_file_hash_verified,false);await fs.writeFile(path.join(driver.output,'owned-public-result.json'),JSON.stringify(lookup.result_observation));
  }else assert.notEqual(lookup.result_observation.public_accessible,true);
 }
 assert.equal(submits,['prepare_submit','public_coverage'].includes(scenario)?1:0);assert.equal(unowned,0);
 assert.equal(outside,0);
 process.stdout.write(JSON.stringify({scenario,selections,submits,unowned_http_responses:unowned,live_tested:false,human_review:false})+'\n');
}finally{await context.close();await browser.close();}
