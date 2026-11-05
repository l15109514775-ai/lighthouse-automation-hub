// Genuine Chromium and real FFmpeg clips. All HTML is owned and every HTTP
// response is locally fulfilled; this is not evidence of platform upload.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {randomUUID,createHash} from 'node:crypto';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {createMultipartPrivateDriver} from '../adapters/browser_multipart_private_v4.mjs';
import {canonicalDigest} from '../scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs';
const scenario=process.argv[2],root=path.resolve(process.argv[3]);
assert.ok(['ordered_save','existing_draft','bad_chooser','reversed_rows','file_changed','save_unknown','wrong_uid','challenge','restore_saved','restore_duplicate'].includes(scenario));
await fs.mkdir(path.join(root,'media','samples'),{recursive:true});
const digest=raw=>createHash('sha256').update(raw).digest('hex');
const parts=[];
for(let i=1;i<=2;i++){
 const file=path.join(root,'media','samples',`part-00${i}.mp4`);
 const run=spawnSync('ffmpeg',['-v','error','-f','lavfi','-i',`color=c=${i===1?'blue':'green'}:s=128x72:r=10`,'-f','lavfi','-i','anullsrc=r=44100:cl=stereo','-t','0.6','-c:v','libx264','-threads','1','-c:a','aac','-y',file],{encoding:'utf8',windowsHide:true});
 assert.equal(run.status,0,'owned real FFmpeg clip failed');const raw=await fs.readFile(file);
 const probe=spawnSync('ffprobe',['-v','error','-show_entries','format=duration','-of','json',file],{encoding:'utf8',windowsHide:true});assert.equal(probe.status,0);
 parts.push({index:i,file,bytes:raw.length,sha256:digest(raw),duration_seconds:Number(JSON.parse(probe.stdout).format.duration),filename:path.basename(file)});
}
const source_id='9T5tn1WGlJg',source_url='https://www.youtube.com/watch?v='+source_id,uid='1000000000000000';
const sample={version:'real-source-private-multipart-samples-v4',source_id,method:'ffmpeg_stream_copy',private_upload_only:true,public_submission_allowed:false,formal_splitter_acceptance_claimed:false,
 parts:parts.map(p=>({path:p.file,bytes:p.bytes,sha256:p.sha256,probe:{format:{duration:String(p.duration_seconds)}}}))};
const manifestFile=path.join(root,'media','samples','manifest.json'),manifestBytes=Buffer.from(JSON.stringify(sample));await fs.writeFile(manifestFile,manifestBytes);
const metadata={title:'【多P私有验收】两段真实样本',description:'仅私有草稿。\n原视频：'+source_url,copyright:2,source:source_url,category_label:'娱乐',tags:['流程验收']};
const spec={version:'multipart-private-native-manifest-v4',job_id:randomUUID(),attempt_id:randomUUID(),target_site_uid:uid,source_id,source_url,metadata,metadata_sha256:canonicalDigest(metadata),
 sample_manifest:{file:manifestFile,bytes:manifestBytes.length,sha256:digest(manifestBytes)},parts,parts_manifest_sha256:canonicalDigest(parts),binding_sha256:canonicalDigest({owned:true,scenario}),human_review:false,publication_authorized:false,native_multipart_verified:false};
let chooserCount=0,saveClicks=0,publicClicks=0,unexpectedHttp=0,managerRequests=0,restoredEditors=0,draftTabs=0;const audit=[];
const browser=await chromium.launch({headless:true,chromiumSandbox:true});const context=await browser.newContext();
const editor='https://member.bilibili.com/platform/upload/video/frame';
const html=`<!doctype html><meta charset="utf-8"><style>input{display:block;width:650px} .upload-area{width:200px;height:50px;background:#ddd}.file-item{padding:10px}.ql-editor{height:100px}.ql-editor p{margin:0}span{display:inline-block}</style>
<div class="bcc-upload"><div class="upload-area" onclick="document.querySelector('#videos').click()">上传视频</div></div>
<input id="videos" name="buploader" type="file" accept="${scenario==='bad_chooser'?'.txt':'.mp4,.mkv'}" ${scenario==='bad_chooser'?'':'multiple'}>
<input type="file" accept=".txt" id="subtitles"><div id="rows"></div><div id="metadata" hidden>
<input placeholder="请输入稿件标题"><div class="ql-editor" contenteditable="true"></div>
<input placeholder="请选择符合您视频内容的创作声明" readonly value="内容为转载"><input placeholder="转载视频请注明来源" value="${source_url}">
<div class="video-human-type"><div class="select-controller">娱乐</div></div><div class="label-item-v2-container"><p class="label-item-v2-content">流程验收</p></div>
<input placeholder="按回车键Enter创建标签"><span class="submit-draft" onclick="console.log('OWNED_SAVE');${scenario==='save_unknown'?'':"document.querySelector('#toast').textContent='已存入草稿箱'"}">存草稿</span>
<button class="submit-add" onclick="console.log('OWNED_PUBLIC')">立即投稿</button></div><div id="toast"></div>
${scenario==='existing_draft'?'<button>继续编辑</button>':''}${scenario==='challenge'?'<div class="geetest_panel" style="width:100px;height:100px">human check</div>':''}
<script>document.querySelector('.ql-editor').onblur=e=>{const text=e.target.innerText;e.target.replaceChildren(...text.split('\\n').map(line=>{const p=document.createElement('p');p.textContent=line;return p;}));};
document.querySelector('#videos').onchange=e=>{const files=[...e.target.files];console.log('OWNED_SELECTED:'+JSON.stringify(files.map(f=>({name:f.name,bytes:f.size}))));
 setTimeout(()=>{document.querySelector('#metadata').hidden=false;const ordered=${scenario==='reversed_rows'?'files.reverse()':'files'};
 document.querySelector('#rows').innerHTML=ordered.map(f=>'<div class="file-item"><div class="title"><span class="title-text" title="'+f.name+'">'+f.name+'</span></div><div class="file-item-content-status-text"><span class="success">上传完成</span></div></div>').join('');
 document.querySelector('[placeholder="请输入稿件标题"]').value=files[0].name;
 },120);};</script>`;
await context.route('**/*',async route=>{
 const url=new URL(route.request().url());let body;
 if(url.href===editor){body=html;if(saveClicks){restoredEditors++;
  body+='<script>document.querySelector("#metadata").hidden=false;document.querySelector("[placeholder=请输入稿件标题]").value='+JSON.stringify(metadata.title)+';document.querySelector(".ql-editor").innerHTML='+JSON.stringify(metadata.description.split('\n').map(line=>'<p>'+line+'</p>').join(''))+';document.querySelector("#rows").innerHTML='+JSON.stringify(parts.map(p=>'<div class="file-item"><span class="title-text" title="'+p.filename+'">'+p.filename+'</span><div class="file-item-content-status-text"><span class="success">上传完成</span></div></div>').join(''))+';</script>';
 }}
 else if(url.href==='https://member.bilibili.com/platform/upload-manager/article'){
  managerRequests++;
  const links=Array(scenario==='restore_duplicate'?2:1).fill('<a href="'+editor+'">'+metadata.title+'</a>').join('');
  const manager='<h1>稿件管理</h1><a href="/platform/upload-manager/article">视频管理</a><a href="/platform/upload-manager/opus">图文管理</a><a href="/platform/upload-manager/inactArticle">互动视频管理</a><div>全部稿件 0</div><button onclick="console.log(\'OWNED_DRAFT_TAB\');document.querySelector(\'#drafts\').innerHTML='+JSON.stringify(links).replaceAll('&','&amp;').replaceAll('"','&quot;')+'">草稿 1</button><div id="drafts"></div>';
  body='<meta charset="utf-8"><body class="risk-captcha-adapt risk-captcha-adapt-pc"><main></main><script>setTimeout(()=>document.querySelector("main").innerHTML='+JSON.stringify(manager)+',150)</script>';
 }
 else if(url.href==='https://t.bilibili.com/')body=`<a class="header-entry-mini" href="https://space.bilibili.com/${scenario==='wrong_uid'?'999':uid}/">Owned identity</a>`;
 else{unexpectedHttp++;await route.abort();return;}
 await route.fulfill({status:200,contentType:'text/html',headers:{'x-owned-fixture':'multipart-private-v4'},body});
});
const page=await context.newPage();const consoleObserver=message=>{const text=message.text();if(text==='OWNED_SAVE')saveClicks++;else if(text==='OWNED_PUBLIC')publicClicks++;else if(text==='OWNED_DRAFT_TAB')draftTabs++;else if(text.startsWith('OWNED_SELECTED:'))audit.push(JSON.parse(text.slice(15)));};page.on('console',consoleObserver);
page.on('filechooser',async()=>{
 chooserCount++;
 // The actual native chooser event independently reads the already durable
 // intent, before setFiles. No helper authorizes the driver in this test.
 const file=path.join(root,'media',spec.job_id,'multipart-private-v4/video-selection-intent.json');
 const intent=JSON.parse(await fs.readFile(file,'utf8'));assert.equal(intent.parts_manifest_sha256,spec.parts_manifest_sha256);assert.equal(intent.binding_sha256,spec.binding_sha256);assert.deepEqual(intent.parts,spec.parts);
});
const driver=await createMultipartPrivateDriver({page,context,spec,root,uploadTimeoutMs:3000,saveTimeoutMs:400});
async function refusal(action){try{await action();assert.fail('expected refusal');}catch(e){assert.ok(e.code,e.stack);return e.code;}}
try{
 if(scenario==='file_changed')await fs.appendFile(parts[1].file,Buffer.from('changed'));
 if(['existing_draft','bad_chooser','reversed_rows','file_changed','wrong_uid','challenge'].includes(scenario)){
  const code=await refusal(()=>driver.perform('private_upload'));
  const expected={existing_draft:'MULTIPART_PRIVATE_EXISTING_DRAFT',bad_chooser:'MULTIPART_PRIVATE_CHOOSER_INVALID',reversed_rows:'MULTIPART_PRIVATE_NATIVE_ORDER_MISMATCH',file_changed:'REPLAY_BROWSER_FILE_SIZE_CHANGED',wrong_uid:'TARGET_ACCOUNT_IDENTITY_MISMATCH',challenge:'BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED'};
  assert.equal(code,expected[scenario]);assert.equal(chooserCount,['bad_chooser','reversed_rows'].includes(scenario)?1:0);assert.equal(saveClicks,0);
  const diagnostic=JSON.parse(await fs.readFile(path.join(driver.output,'upload-progress.json'),'utf8'));
  assert.equal(diagnostic.read_only,true);assert.equal(diagnostic.reason,'operation-error');
  if(scenario==='reversed_rows'){assert.equal(diagnostic.native.visible_file_item_count,2);assert.equal(diagnostic.native.visible_rows[0].title_nodes[0].text,parts[1].filename);assert.equal(diagnostic.native.uploaders[0].files.length,2);}
 }else{
  const uploaded=await driver.perform('private_upload');assert.equal(uploaded.state,'private_parts_uploaded');assert.equal(chooserCount,1);
  assert.deepEqual(uploaded.upload_receipt.ordered_parts.map(p=>p.native_filename),parts.map(p=>p.filename));
  for(const [i,p] of uploaded.upload_receipt.ordered_parts.entries()){assert.equal(p.whole_file_streamed,true);assert.equal(p.streamed_sha256,parts[i].sha256);assert.equal(p.streamed_bytes,parts[i].bytes);}
  const raw=await fs.readFile(uploaded.receipt_file.file);assert.equal(digest(raw),uploaded.receipt_file.sha256);assert.deepEqual(JSON.parse(raw),uploaded.upload_receipt);
  assert.equal(await refusal(()=>driver.perform('private_upload')),'MULTIPART_PRIVATE_RECONCILE_REQUIRED');assert.equal(chooserCount,1);
  const observed=await driver.perform('inspect');assert.equal(observed.observation.native_rows.length,2);assert.equal(chooserCount,1);
  if(scenario==='save_unknown'){
   assert.equal(await refusal(()=>driver.perform('save')),'MULTIPART_PRIVATE_SAVE_UNKNOWN');assert.equal(saveClicks,1);
   assert.equal(await refusal(()=>driver.perform('save')),'MULTIPART_PRIVATE_RECONCILE_REQUIRED');assert.equal(saveClicks,1);
   if(['restore_saved','restore_duplicate'].includes(scenario)){
    const newPage=await context.newPage();newPage.on('console',consoleObserver);const restored=await createMultipartPrivateDriver({page:newPage,context,spec,root});
    if(scenario==='restore_duplicate'){
     assert.equal(await refusal(()=>restored.perform('inspect')),'MULTIPART_PRIVATE_DRAFT_LOOKUP_NOT_CONFIRMED');assert.equal(restoredEditors,0);
    }else{
     const read=await restored.perform('inspect');assert.equal(read.observation.recovered_without_modification,true);assert.equal(read.observation.metadata_verified,true);
     assert.deepEqual(read.observation.native_rows.map(p=>p.native_filename),parts.map(p=>p.filename));assert.equal(restoredEditors,1);
     assert.deepEqual(read.observation.metadata_readback,saved.draft_receipt.metadata_readback);
    }
    assert.equal(managerRequests,1);assert.equal(draftTabs,1);assert.equal(chooserCount,1);assert.equal(saveClicks,1);await newPage.close();
   }
   await assert.rejects(fs.stat(path.join(driver.output,'draft-receipt.json')),e=>e.code==='ENOENT');
  }else{
   const saved=await driver.perform('save');assert.equal(saved.state,'private_draft_saved');assert.equal(saved.draft_receipt.metadata_verified,true);
   assert.deepEqual(saved.draft_receipt.metadata_readback,{title:metadata.title,description:metadata.description,creation_statement:'内容为转载',source:source_url,category:'娱乐',tags:metadata.tags});
   assert.equal(saved.draft_receipt.native_multipart_verified,false);assert.equal(saved.draft_receipt.publication_authorized,false);assert.equal(saveClicks,1);
   assert.equal(await refusal(()=>driver.perform('save')),'MULTIPART_PRIVATE_RECONCILE_REQUIRED');assert.equal(saveClicks,1);
  }
  assert.equal(await refusal(()=>driver.perform('submit')),'MULTIPART_PRIVATE_OPERATION_INVALID');
 }
 assert.equal(publicClicks,0);assert.equal(unexpectedHttp,0);
 console.log(JSON.stringify({scenario,owned_fixture:true,real_ffmpeg_clips:true,native_choosers:chooserCount,private_save_clicks:saveClicks,public_clicks:publicClicks,external_requests:unexpectedHttp,native_multipart_verified:false}));
}finally{await context.close();await browser.close();}
