// Actual FFmpeg-processed files from Python, fake owned web UI, no account.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {createMultipartReplayDriver,withMultipartSubmitDeadline} from '../adapters/browser_multipart_replay_v4.mjs';
import {canonicalDigest} from '../scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs';
const [scenario,root,document]=process.argv.slice(2),spec=JSON.parse(await fs.readFile(document,'utf8'));
const editor='https://member.bilibili.com/platform/upload/video/frame',manager='https://member.bilibili.com/platform/upload-manager/article',bv='BV1234567890';
let videoChoices=0,imageChoices=0,saves=0,submits=0,external=0,bodySaved=false;
const esc=s=>s.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const row=name=>'<div class="file-item"><span class="title-text" title="'+esc(name)+'">'+esc(name)+'</span><div class="file-item-content-status-text"><span class="success">上传完成</span></div></div>';
const html=`<style>input{display:block;width:600px}.upload-area{height:50px;background:#eee}.ql-editor{height:90px}.ql-editor p{margin:0}.file-item{padding:5px}.cover-module-main{width:180px;height:100px}.cover-editor{border:1px solid black}span.button,span.submit-draft{display:inline-block;padding:10px}</style>
<div class="bcc-upload"><div class="upload-area" onclick="document.querySelector('#videos').click()">上传视频</div></div><input id="videos" name="buploader" type="file" multiple accept=".mp4,.mkv"><div id="rows"></div><div id="form" hidden>
<input placeholder="请输入稿件标题"><div class="ql-editor" contenteditable="true"></div><input placeholder="请选择符合您视频内容的创作声明" readonly value="内容为转载"><input placeholder="转载视频请注明来源" value="${spec.source_url}">
<div class="video-human-type"><div class="select-controller">娱乐</div></div><div class="label-item-v2-container"><p class="label-item-v2-content">Owned</p></div><input placeholder="按回车键Enter创建标签">
<div class="cover-main"><div class="cover-module-main" onclick="document.querySelector('.cover-editor').hidden=false">添加封面</div></div><span class="submit-draft" onclick="console.log('OWNED_SAVE');document.querySelector('#toast').textContent='已存入草稿箱'">存草稿</span><span class="submit-add" onclick="console.log('OWNED_SUBMIT')">立即投稿</span></div>
<div class="cover-editor" hidden><div class="cover-upload cover-editor-panel-select-item" onclick="document.querySelector('#image').click()">上传封面</div><input id="image" type="file" accept="image/png,.png"><div class="cover-editor-button"><span class="button" id="complete">完成</span></div></div><div id="toast"></div>
<script>document.querySelector('.ql-editor').onblur=e=>{const text=e.target.innerText;e.target.replaceChildren(...text.split('\\n').map(line=>{const p=document.createElement('p');p.textContent=line;return p;}));};
document.querySelector('[placeholder="请输入稿件标题"]').oninput=e=>{const first=document.querySelector('.title-text');if(first){first.textContent=e.target.value;first.title=e.target.value;}};
document.querySelector('#videos').onchange=e=>{const files=[...e.target.files];document.querySelector('#form').hidden=false;document.querySelector('#rows').innerHTML=files.map(f=>${JSON.stringify(row('OWNED_FILENAME'))}.replaceAll('OWNED_FILENAME',f.name)).join('');document.querySelector('[placeholder="请输入稿件标题"]').value=files[0].name;};
document.querySelector('#complete').onclick=()=>{document.querySelector('.cover-editor').hidden=true;const img=document.createElement('img');img.src=window.ownedImage;img.width=100;document.querySelector('.cover-module-main').replaceChildren(img);};
document.querySelector('#image').onchange=e=>{const reader=new FileReader();reader.onload=()=>window.ownedImage=reader.result;reader.readAsDataURL(e.target.files[0]);};</script>`;
const browser=await chromium.launch({headless:true,chromiumSandbox:true}),context=await browser.newContext();
await context.route('**/*',async r=>{
 const u=new URL(r.request().url());let body;
 if(u.href===editor)body=html;
 else if(u.href==='https://t.bilibili.com/')body='<a class="header-entry-mini" href="https://space.bilibili.com/'+spec.target_site_uid+'/">Owned identity</a>';
 else if(u.href===manager)body='<body class="risk-captcha-adapt risk-captcha-adapt-pc"><div>稿件管理</div><a href="/platform/upload-manager/article">视频管理</a><a href="/platform/upload-manager/opus">图文管理</a><a href="/platform/upload-manager/inactArticle">互动视频管理</a><div>全部稿件 2</div><div>草稿 1</div><div class="article-card clearfix v2"><div class="meta-title"><a class="name ellipsis" href="https://www.bilibili.com/video/'+bv+'/">'+spec.metadata.title+'</a></div><a class="cover-wrp" href="https://www.bilibili.com/video/'+bv+'/"><span class="duration">00:00:06</span></a><div class="pubdate is-success"><span class="date">2026年11月02日 10:13:11</span></div></div>';
 else if(u.origin==='https://www.bilibili.com'&&u.pathname==='/video/'+bv+'/'){
  const index=Number(u.searchParams.get('p')||1),list=spec.parts.map((p,i)=>'<a href="/video/'+bv+'/?p='+(i+1)+'">'+(i===0?spec.metadata.title:p.filename)+'</a>');
  if(scenario==='missing_part')list.pop();if(scenario==='duplicate_playlist')list.push(list[0]);
  body='<h1>'+spec.metadata.title+'</h1><div class="up-info-container"><a class="up-name" href="https://space.bilibili.com/'+spec.target_site_uid+'/">'+spec.account_name+'</a></div><div class="basic-desc-info" style="white-space:pre-wrap">'+spec.source_url+'\n'+spec.metadata.description+'</div>'+list.join('')+'<video muted autoplay controls style="width:640px;height:360px" src="/owned-part-'+index+'.mp4"></video>';
 }else if(u.origin==='https://www.bilibili.com'&&/^\/owned-part-\d+\.mp4$/.test(u.pathname)){
  const index=Number(/(\d+)\.mp4$/.exec(u.pathname)[1])-1,bytes=await fs.readFile(spec.parts[index].file),range=/^bytes=(\d+)-(\d*)$/.exec(r.request().headers().range||''),start=range?Number(range[1]):0,end=range&&range[2]?Math.min(Number(range[2]),bytes.length-1):bytes.length-1;
  await r.fulfill({status:range?206:200,contentType:'video/mp4',headers:{'accept-ranges':'bytes','content-length':String(end-start+1),...(range?{'content-range':`bytes ${start}-${end}/${bytes.length}`}:{})},body:bytes.subarray(start,end+1)});return;
 }else{external++;await r.abort();return;}
 await r.fulfill({status:200,contentType:'text/html',headers:{'x-owned-fixture':'true'},body:'<!doctype html><meta charset="utf-8">'+body});
});
context.on('page',p=>p.on('console',m=>{if(m.text()==='OWNED_SAVE'){saves++;bodySaved=true;}if(m.text()==='OWNED_SUBMIT')submits++;}));
const page=await context.newPage();page.on('filechooser',async c=>{if((await c.element().getAttribute('accept')).includes('.mp4'))videoChoices++;else imageChoices++;});
const driver=await createMultipartReplayDriver({page,context,spec,root,uploadTimeoutMs:2000,resultTimeoutMs:10,publicPartTimeoutMs:2000});
const reject=async fn=>{try{await fn();assert.fail('expected refusal');}catch(e){assert.ok(e.code,e.stack);return e.code;}};
try{
 const up=await driver.perform('upload');assert.equal(up.state,'uploaded');assert.equal(videoChoices,1);const {upload_reference_sha256,...body}=up.upload_receipt;assert.equal(upload_reference_sha256,canonicalDigest(body));
 assert.equal(await reject(()=>driver.perform('upload')),'MULTIPART_NATIVE_RECONCILE_REQUIRED');assert.equal(videoChoices,1);
 const prep=await driver.perform('prepare');assert.equal(prep.state,'prepared');assert.equal(prep.native_preparation.native_part_count,spec.parts.length);assert.equal(saves,1);assert.equal(imageChoices,1);
 const fresh=await driver.perform('prepare');assert.equal(fresh.recovered_without_modification,true);assert.equal(saves,1);assert.equal(imageChoices,1);
 const ticket={...Object.fromEntries(['job_id','attempt_id','site_uid','source_id','source_url','artifact_sha256','file_bytes','metadata_sha256','binding_sha256','policy_sha256','whole_plan_sha256','parts_manifest_sha256'].map(k=>[k,prep.native_preparation[k]])),version:'replay-multipart-native-reservation-v4',ticket_id:randomUUID(),idempotency_key:'a'.repeat(64),machine_evidence_sha256:'c'.repeat(64),issued_at:Date.now()/1000-1,expires_at:Date.now()/1000+120,can_click_submit:true,upload_reference_sha256};
 if(scenario==='wrong_ticket'){ticket.binding_sha256='d'.repeat(64);assert.equal(await reject(()=>driver.perform('submit',{reservation:ticket})),'MULTIPART_NATIVE_TICKET_EXPIRED');assert.equal(submits,0);}
 else if(scenario==='changed_file'){await fs.chmod(spec.parts[0].file,0o600);await fs.appendFile(spec.parts[0].file,Buffer.from('changed'));assert.equal(await reject(()=>driver.perform('submit',{reservation:ticket})),'MULTIPART_NATIVE_FILE_STAT_CHANGED');assert.equal(submits,0);}
 else if(scenario==='expired_ticket'){ticket.expires_at=Date.now()/1000-1;assert.equal(await reject(()=>withMultipartSubmitDeadline(()=>driver.perform('submit',{reservation:ticket}),ticket,()=>context.close())),'MULTIPART_NATIVE_TICKET_EXPIRED');assert.equal(submits,0);}
 else{
  const result=await driver.perform('submit',{reservation:ticket});assert.equal(submits,1);assert.equal(result.result_observation.same_submit_attempt_observed,false);
  if(scenario==='complete'){assert.equal(result.state,'completed');assert.equal(result.result_observation.public_parts.length,spec.parts.length);assert.ok(result.result_observation.public_parts.every(p=>p.near_end_playability_observed&&p.near_end_seek_performed));assert.equal(result.result_observation.public_parts.every(p=>p.cid===null),true);}
  else assert.equal(result.state,'awaiting_moderation');
  assert.equal(await reject(()=>driver.perform('submit',{reservation:ticket})),'MULTIPART_NATIVE_RECONCILE_REQUIRED');assert.equal(submits,1);
 }
 assert.equal(videoChoices,1);assert.equal(saves,1);assert.equal(external,0);console.log(JSON.stringify({scenario,owned:true,actual_owned_submit_clicks:submits,actual_platform_actions:false,platform_publications:0,external_requests:external,real_processed_parts:spec.parts.length}));
}catch(e){console.error(JSON.stringify(await page.locator('.cover-editor').evaluateAll(nodes=>nodes.map(n=>({visible:!!n.getBoundingClientRect().height,html:n.innerHTML.slice(0,2000)})))));throw e;}finally{await context.close();await browser.close();}
