// Routed, owned Chromium pages only. No real account, media or publication.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {createReplayNativeDriver,validateReplayManifest,streamCheckedFile} from '../adapters/replay_native_driver.mjs';
import {canonicalDigest} from '../scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs';
const root=path.resolve(process.argv[2]),scenario=process.argv[3];
assert.ok(['upload_once','wrong_uid','challenge','txt_chooser','prepare_and_submit','submit_unknown','missing_reservation','source_changed','cover_sync','public_lookup','public_challenge'].includes(scenario));
const id=()=>crypto.randomUUID(),sha=b=>crypto.createHash('sha256').update(b).digest('hex');
const opened=fs.open.bind(fs),synced=new Set();fs.open=async(file,...args)=>{const handle=await opened(file,...args),sync=handle.sync.bind(handle);handle.sync=async()=>{await sync();synced.add(path.resolve(String(file)));};return handle;};
const job=id(),download=id(),attempt=id(),uid='123456',source='abcdefghijk',sourceURL='https://www.youtube.com/watch?v='+source;
const file=path.join(root,'media',download,'youtube-download-v1/original.mp4');
await fs.mkdir(path.dirname(file),{recursive:true});const artifact=Buffer.from('OWNED synthetic file chooser fixture; never a real replay');await fs.writeFile(file,artifact);
const metadata={title:'Owned seven hour replay fixture',description:'Owned synthetic description\n'+sourceURL,source:sourceURL,category_label:'娱乐',tags:['Owned'],copyright:2};
const original=path.join(root,'media',job,'short-video-attempts',attempt,'cover-source-official-maxres.jpg');
const cover=path.join(root,'media',job,'cover-assets-v4/top-bottom-blur-cover.png'),provenance=path.join(path.dirname(cover),'provenance.json');
await fs.mkdir(path.dirname(original),{recursive:true});await fs.mkdir(path.dirname(cover),{recursive:true});
const env=Object.fromEntries(['PATH','SystemRoot','SYSTEMROOT','TEMP','TMP'].filter(k=>process.env[k]).map(k=>[k,process.env[k]]));
for(const [filename,geometry,codec]of [[original,'1280x720','mjpeg'],[cover,'1280x960','png']]){
 const p=spawnSync('ffmpeg',['-v','error','-f','lavfi','-i','color=c=white:s='+geometry,'-frames:v','1','-c:v',codec,'-threads','1',filename],{env,encoding:'utf8'});assert.equal(p.status,0,p.stderr);
}
const originalBytes=await fs.readFile(original),coverBytes=await fs.readFile(cover);
const transform={filter_complex:'[0:v]format=rgb24,split=2[bg0][fg];[bg0]scale=1280:960:force_original_aspect_ratio=increase:flags=lanczos,crop=1280:960,gblur=sigma=35:steps=3[bg];[bg][fg]overlay=0:120:format=rgb,format=rgb24[out]',foreground_rect:{x:0,y:120,width:1280,height:720},safe_crop:{x:0,y:0,width:1280,height:960},background:{width:1280,height:960,blur_sigma:35},foreground_fit:'original_size',foreground_padding:'none'};
const p={version:'lighthouse-derived-cover-v4',policy_version:'top-bottom-blur-v1',input:{file:original,bytes:originalBytes.length,sha256:sha(originalBytes),source_url:'https://i.ytimg.com/vi/'+source+'/maxresdefault.jpg',width:1280,height:720},output:{file:cover,bytes:coverBytes.length,sha256:sha(coverBytes),width:1280,height:960,mime_type:'image/png'},transform,image_edited:true,no_text_regenerated:true,official_original_bytes:false,ffmpeg_version:'ffmpeg version owned fixture',created_at:new Date().toISOString()};
await fs.writeFile(provenance,JSON.stringify(p));
const spec={version:'replay-native-browser-manifest-v1',job_id:job,download_job_id:download,attempt_id:attempt,target_site_uid:uid,
 source_id:source,source_url:sourceURL,channel_id:'UCaaaaaaaaaaaaaaaaaaaaaa',file,file_bytes:artifact.length,artifact_sha256:sha(artifact),duration_seconds:25715,
 metadata,metadata_sha256:canonicalDigest(metadata),binding_sha256:'a'.repeat(64),policy_sha256:'b'.repeat(64),
 cover:{file:cover,bytes:coverBytes.length,sha256:sha(coverBytes),source_url:p.input.source_url},cover_derivation:{version:'derived-cover-binding-v2',provenance_file:provenance,provenance_sha256:sha(await fs.readFile(provenance)),provenance:p},human_review:false,per_artifact_owner_acceptance:false};
validateReplayManifest(spec);assert.throws(()=>validateReplayManifest({...spec,file_bytes:3_800_000_001}),e=>e.code==='REPLAY_BROWSER_MANIFEST_INVALID');
assert.throws(()=>validateReplayManifest({...spec,duration_seconds:86401}),e=>e.code==='REPLAY_BROWSER_MANIFEST_INVALID');
let outside=0,unowned=0,videoSelections=0,imageSelections=0,submits=0,saves=0,completes=0,syncs=0,uploaded=false,coverApplied=false,resultVisible=false;
const browser=await chromium.launch({headless:true,chromiumSandbox:true});const context=await browser.newContext({viewport:{width:1280,height:960}});
const page=await context.newPage();
await context.exposeBinding('ownedEvent',async({page},kind)=>{
 if(kind==='video'){
  const intent=JSON.parse(await fs.readFile(path.join(root,'media',job,'replay-native-intents/video-selection.json'),'utf8'));
  assert.ok(synced.has(path.join(root,'media',job,'replay-native-intents/video-selection.json')));if(process.platform!=='win32')assert.ok(synced.has(path.join(root,'media',job,'replay-native-intents')));
  assert.equal(intent.artifact_sha256,spec.artifact_sha256);assert.equal(intent.attempt_id,attempt);videoSelections++;uploaded=true;
 }else if(kind==='image'){imageSelections++;}else if(kind==='complete'){completes++;coverApplied=true;}else if(kind==='sync'){syncs++;}else if(kind==='save'){saves++;}else if(kind==='submit'){
  const intent=JSON.parse(await fs.readFile(path.join(root,'media',job,'replay-native-intents/public-submit.json'),'utf8'));
  assert.ok(synced.has(path.join(root,'media',job,'replay-native-intents/public-submit.json')));if(process.platform!=='win32')assert.ok(synced.has(path.join(root,'media',job,'replay-native-intents')));
  assert.equal(intent.reservation.can_click_submit,true);assert.equal(intent.metadata_sha256,spec.metadata_sha256);submits++;resultVisible=true;
 }
});
const esc=s=>s.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
function form(){return '<input name="buploader" type="file" accept=".mp4"><div>上传完成</div>'+`<input placeholder="请输入稿件标题" value="${esc(metadata.title)}"><div class="ql-editor" contenteditable="true"><p>Owned synthetic description</p><p>${esc(sourceURL)}</p></div>`+
 '<input readonly placeholder="请选择符合您视频内容的创作声明" value="内容为转载">'+`<input placeholder="转载视频请注明来源、时间、地点" value="${esc(sourceURL)}">`+
 '<div class="video-human-type"><div class="select-controller">娱乐</div></div><div class="label-item-v2-container"><p class="label-item-v2-content">Owned</p></div><input placeholder="按回车键Enter创建标签">'+
 '<div class="cover-main"><div class="cover-module-main" style="width:300px;height:120px;'+(coverApplied?'background-image:linear-gradient(white,white)':'')+'">'+(coverApplied?'':'添加封面')+'</div></div>'+
 '<div class="cover-editor bcc-dialog__wrap-mask" style="display:none"><p>封面制作</p><div class="cover-upload cover-editor-panel-select-item">上传封面</div><input id="image" type="file" accept="image/png,image/jpeg" style="display:none"><div class="cover-editor-button"><span class="button">完成</span></div></div>'+
 '<span class="submit-draft">存草稿</span><span class="submit-add">立即投稿</span><div id="toast"></div><div id="sync-modal" style="display:none">检测到个人空间封面（16:9）未修改<p>未修改可能导致不同场景封面展示不一致。是否将「4:3封面」的改动同步到「16:9封面」？下方是同步后的效果预览。</p><div class="modal-footer"><button>不同步，手动编辑</button><button class="confirm">确认同步</button></div></div><script>'+script()+'</script>';}
function script(){return `
document.querySelector('.cover-module-main').onclick=()=>document.querySelector('.cover-editor').style.display='block';
document.querySelector('.cover-upload').onclick=()=>document.querySelector('#image').click();
document.querySelector('#image').onchange=async()=>{await ownedEvent('image');};
let firstComplete=true,synchronized=false;
document.querySelector('.cover-editor .button').onclick=async()=>{await ownedEvent('complete');if(${scenario==='cover_sync'}&&firstComplete){firstComplete=false;document.querySelector('#sync-modal').style.display='block';return;}document.querySelector('.cover-editor').style.display='none';const e=document.querySelector('.cover-module-main');e.textContent='';e.style.backgroundImage='linear-gradient(white,white)';};
document.querySelector('.modal-footer .confirm').onclick=async()=>{await ownedEvent('sync');synchronized=true;document.querySelector('#sync-modal').style.display='none';};
document.querySelector('.submit-draft').onclick=async()=>{await ownedEvent('save');document.querySelector('#toast').textContent='已存入草稿箱';};
document.querySelector('.submit-add').onclick=async()=>{await ownedEvent('submit');document.body.innerHTML=${JSON.stringify(resultHTML())};};
`;}
function resultHTML(){return '<input name="buploader" type="file" accept=".mp4"><div>'+esc(metadata.title)+'</div><div>'+sourceURL+'</div>'+(scenario==='submit_unknown'?'结果处理中':'<a href="https://www.bilibili.com/video/BV1234567890">查看视频</a><p>审核中</p>');}
context.on('response',r=>{if(r.url().startsWith('http')&&r.headers()['x-owned-fixture']!=='true')unowned++;});
await context.route('**/*',async route=>{
 const u=new URL(route.request().url());let html;
 if(u.hostname==='t.bilibili.com')html='<a class="header-entry-mini" href="https://space.bilibili.com/'+(scenario==='wrong_uid'?'999999':uid)+'">Owned account</a>';
 else if(u.origin==='https://member.bilibili.com'&&u.pathname==='/platform/upload/video/frame'){
  html=uploaded?form():'<input id="video" name="buploader" type="file" accept="'+(scenario==='txt_chooser'?'.txt':'.mp4')+'">'+(scenario==='txt_chooser'?'<input name="buploader" type="file" accept=".mp4">':'')+'<div class="bcc-upload"><div class="upload-area" style="width:400px;height:80px">上传视频</div></div>'+(scenario==='challenge'?'<div class="geetest_challenge" style="width:100px;height:40px">verification</div>':'')+
   '<script>document.querySelector(".upload-area").onclick=()=>document.querySelector("#video").click();document.querySelector("#video").onchange=async()=>{await ownedEvent("video");document.body.innerHTML='+JSON.stringify(form()).replaceAll('<','\\u003c')+';const s=document.querySelector("script");eval(s.textContent);};</script>';
 }else if(u.origin==='https://member.bilibili.com'&&u.pathname==='/platform/upload-manager/article')html='<h1>稿件管理</h1><a href="/platform/upload-manager/article">视频管理</a><a href="/platform/upload-manager/opus">图文管理</a><a href="/platform/upload-manager/inactArticle">互动视频管理</a><button>草稿 1</button>'+resultHTML();
 else if(['public_lookup','public_challenge'].includes(scenario)&&u.origin==='https://www.bilibili.com'&&u.pathname==='/video/BV1234567890')html='<h1>'+esc(metadata.title)+'</h1><a href="https://space.bilibili.com/'+uid+'">Owned uploader</a><div>'+sourceURL+'</div>'+(scenario==='public_challenge'?'<div class="geetest_challenge" style="width:100px;height:40px">verification</div>':'');
 else{outside++;return route.abort();}
 return route.fulfill({contentType:'text/html',headers:{'x-owned-fixture':'true'},body:'<!doctype html><meta charset="utf-8"><body class="risk-captcha-adapt risk-captcha-adapt-pc">'+html+'</body>'});
});
try{
 const driver=await createReplayNativeDriver({page,context,spec,root,uploadTimeoutMs:2000,resultTimeoutMs:600});
 if(scenario==='source_changed'){
  await fs.appendFile(file,'changed');await assert.rejects(()=>driver.perform('upload'),e=>e.code==='REPLAY_BROWSER_FILE_SIZE_CHANGED');
 }else if(['wrong_uid','challenge','txt_chooser'].includes(scenario)){
  await assert.rejects(()=>driver.perform('upload'),scenario==='txt_chooser'?e=>e.code==='REPLAY_BROWSER_VIDEO_CHOOSER_INVALID':undefined);assert.equal(videoSelections,0);
 }else{
  const upload=await driver.perform('upload');assert.equal(upload.state,'uploaded');assert.equal(upload.upload_observation.server_file_hash_verified,false);assert.equal(videoSelections,1);
  await assert.rejects(()=>driver.perform('upload'),e=>e.code==='REPLAY_BROWSER_RECONCILE_REQUIRED');assert.equal(videoSelections,1);
  if(scenario!=='upload_once'){
   const prepared=await driver.perform('prepare');assert.equal(prepared.native_preparation.private_draft_saved,true);assert.equal(saves,1);assert.equal(completes,scenario==='cover_sync'?2:1);assert.equal(syncs,scenario==='cover_sync'?1:0);assert.equal(imageSelections,1);
   const again=await driver.perform('prepare');assert.equal(again.recovered_without_modification,true);assert.equal(saves,1);assert.equal(imageSelections,1);
   if(scenario==='missing_reservation')await assert.rejects(()=>driver.perform('submit'),e=>e.code==='REPLAY_BROWSER_RESERVATION_REQUIRED');
   else{
    const reservation={version:'replay-native-reservation-v1',job_id:job,attempt_id:attempt,binding_sha256:spec.binding_sha256,metadata_sha256:spec.metadata_sha256,can_click_submit:true,state:'dispatching',idempotency_key:'c'.repeat(64),machine_evidence_sha256:'d'.repeat(64),expires_at:Date.now()/1000+120};
    const result=await driver.perform('submit',{reservation});assert.equal(submits,1);
    assert.equal(result.state,scenario==='submit_unknown'?'unknown':'awaiting_moderation');
    await assert.rejects(()=>driver.perform('submit',{reservation}),e=>e.code==='REPLAY_BROWSER_RECONCILE_REQUIRED');assert.equal(submits,1);
    const lookup=await driver.perform('lookup',['public_lookup','public_challenge'].includes(scenario)?{external_id:'BV1234567890'}:{});assert.equal(lookup.state,scenario==='submit_unknown'?'unknown':scenario==='public_lookup'?'completed':'awaiting_moderation');if(scenario==='public_lookup')assert.equal(lookup.result_observation.public_accessible,true);if(scenario==='public_challenge')assert.notEqual(lookup.result_observation.public_accessible,true);assert.equal(submits,1);
   }
  }
 }
 assert.equal(outside,0);assert.equal(unowned,0);
 process.stdout.write(JSON.stringify({scenario,videoSelections,imageSelections,submits,saves,completes,external_network_calls:outside,unowned_http_responses:unowned,real_owner_approval:false,live_tested:false,human_review:false})+'\n');
}finally{await context.close();await browser.close();fs.open=opened;}
