// Owned native-shaped cached tasks. No files are selected and no post is sent.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {randomUUID,createHash} from 'node:crypto';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {canonicalDigest} from '../scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs';
import {reconcileCachedPrivateTasks} from '../scripts/ops/native_control_sources/cache_separate_private_tasks_v5.mjs';
const [scenario,root]=process.argv.slice(2);assert.ok(['save_current','save_all','unknown_save','duplicate_task','guard','known_info','other_modal'].includes(scenario));
const sha=b=>createHash('sha256').update(b).digest('hex'),put=async(file,body)=>{await fs.mkdir(path.dirname(file),{recursive:true});const raw=Buffer.from(JSON.stringify(body));await fs.writeFile(file,raw);return {file,bytes:raw.length,sha256:sha(raw)};};
const folder=path.join(root,'media','multipart-v4-native-real-samples-'+randomUUID()),parts=[];await fs.mkdir(folder,{recursive:true});
for(let i=1;i<=2;i++){const file=path.join(folder,`part-00${i}.mp4`),run=spawnSync('ffmpeg',['-v','error','-f','lavfi','-i','color=c=blue:s=128x72:r=10','-t','0.5','-c:v','libx264','-threads','1','-y',file],{encoding:'utf8',windowsHide:true});assert.equal(run.status,0);const raw=await fs.readFile(file);parts.push({index:i,file,filename:path.basename(file),bytes:raw.length,sha256:sha(raw),duration_seconds:.5});}
const source_id='9T5tn1WGlJg',source_url='https://www.youtube.com/watch?v='+source_id,oldJob=randomUUID(),oldAttempt=randomUUID(),uid='1000000000000000';
const sample=await put(path.join(folder,'samples.json'),{version:'real-source-private-multipart-samples-v4',source_id,private_upload_only:true,public_submission_allowed:false,formal_splitter_acceptance_claimed:false,
 parts:parts.map(p=>({path:p.file,bytes:p.bytes,sha256:p.sha256,probe:{format:{duration:String(p.duration_seconds)}}}))});
const original=await put(path.join(root,'media',oldJob,'multipart-private-v4/video-selection-intent.json'),{version:'multipart-private-one-use-intent-v4',job_id:oldJob,attempt_id:oldAttempt,site_uid:uid,source_id,source_url,kind:'video-selection',parts});
const originalBytes=await fs.readFile(original.file),sourceBytes=await fs.readFile(sample.file);
const tasks=parts.map((p,i)=>{const metadata={title:'【私有验收】真实片源样本 P'+(i+1)+'/2',description:'仅作独立私有草稿验收。\n原视频：'+source_url,copyright:2,source:source_url,category_label:'娱乐',tags:['Owned']};return {index:i+1,filename:p.filename,metadata,metadata_sha256:canonicalDigest(metadata)};});
const spec={version:'cached-separate-private-tasks-manifest-v5',maintenance_id:randomUUID(),original_job_id:oldJob,original_attempt_id:oldAttempt,target_site_uid:uid,source_id,source_url,sample_manifest:sample,original_selection_intent:original,tasks};
const cached=new Map(tasks.map(t=>[t.index,{title:t.filename.replace('.mp4',''),description:'',source:source_url,tags:['Owned']}])),saved=new Map();let selected=1,savedView=null,acknowledgements=0,saves=0,publics=0,choosers=0,external=0,fences=0,fillBeforeSave=false;
const editor='https://member.bilibili.com/platform/upload/video/frame',manager='https://member.bilibili.com/platform/upload-manager/article';
const esc=s=>s.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const browser=await chromium.launch({headless:true,chromiumSandbox:true}),context=await browser.newContext();
function form(index,fromSaved=false,withNotice=false){const data=(fromSaved?saved:cached).get(index);selected=index;
 const outer=fromSaved?'':[...cached].map(([i,x])=>'<div class="task-list-content-item"><div class="task '+(i===index?'task-selected':'')+'"><span class="task-title-text" onclick="selectTask('+i+')">'+esc(scenario==='duplicate_task'?tasks[0].filename.replace('.mp4',''):x.title)+'</span></div></div>').join('');
 return outer+'<input name="buploader" type="file" accept=".mp4"><div class="file-list"><div class="file-item"><span class="title-text">'+parts[index-1].filename.replace('.mp4','')+'</span><span>上传完成</span></div></div><input id="title" placeholder="请输入稿件标题" value="'+esc(data.title)+'"><div class="ql-editor" contenteditable="true">'+(data.description?data.description.split('\n').map(x=>'<p>'+esc(x)+'</p>').join(''):'')+'</div><input readonly placeholder="请选择符合您视频内容的创作声明" value="内容为转载"><input placeholder="转载视频请注明来源" value="'+data.source+'"><div class="video-human-type"><div class="select-controller">娱乐</div></div><div class="label-item-v2-container"><p class="label-item-v2-content">Owned</p></div><input placeholder="按回车键Enter创建标签"><span class="submit-draft" onclick="saveTask()">存草稿</span><button onclick="ownedPublic()">立即投稿</button><div id="toast"></div>'+ (['known_info','other_modal'].includes(scenario)&&withNotice?'<div role="dialog"><h3>部分草稿内容已失效</h3><p>以下内容已按默认值处理或清空，请检查后再投稿：</p><p>'+ (scenario==='other_modal'?'Other modal text':'试看时长不可用，已填写默认时长')+'</p><button onclick="acknowledgements();this.parentElement.remove()">我知道了</button></div>':'')+ (scenario==='guard'?'<div class="geetest_panel" style="width:100px;height:100px">Owned challenge</div>':'');
}
await context.exposeBinding('ownedForm',(_,index)=>form(index));
await context.exposeBinding('ownedUpdate',(_,data)=>{if(cached.has(data.index))Object.assign(cached.get(data.index),data);});
await context.exposeBinding('ownedPrivateSave',()=>{saves++;if(saves===1)fillBeforeSave=tasks.every(t=>cached.get(t.index)?.title===t.metadata.title&&cached.get(t.index)?.description===t.metadata.description);
 if(scenario==='save_all'){for(const [i,data]of cached)saved.set(i,{...data});cached.clear();}
 else if(scenario==='unknown_save'){saved.set(selected,{...cached.get(selected),source:'https://www.youtube.com/watch?v=wrongSource'});}
 else{saved.set(selected,{...cached.get(selected)});cached.delete(selected);}
 return scenario!=='unknown_save';});
await context.exposeBinding('ownedPublic',()=>publics++);
await context.exposeBinding('acknowledgements',()=>acknowledgements++);
await context.exposeBinding('ownedOpenSaved',(_,index)=>{savedView=index;});
const script=`<script>window.ownedSelected=INDEX;function editorText(e){const nodes=[...e.childNodes];return nodes.length&&nodes.every(n=>n.nodeType===1&&['P','DIV'].includes(n.tagName))?nodes.map(n=>n.innerText).join('\\n'):e.innerText;}async function update(){await ownedUpdate({index:window.ownedSelected,title:document.querySelector('#title').value,description:editorText(document.querySelector('.ql-editor'))});}
function setup(){document.querySelector('#title').oninput=update;document.querySelector('.ql-editor').onblur=async e=>{const nodes=[...e.target.childNodes],text=nodes.length&&nodes.every(n=>n.nodeType===1&&['P','DIV'].includes(n.tagName))?nodes.map(n=>n.innerText).join('\\n'):e.target.innerText;e.target.replaceChildren(...text.split('\\n').map(line=>{const p=document.createElement('p');p.textContent=line;return p;}));await update();};}
async function selectTask(index){document.querySelector('#main').innerHTML=await ownedForm(index);window.ownedSelected=index;setup();}
async function saveTask(){const ok=await ownedPrivateSave();if(ok)document.querySelector('#toast').textContent='已存入草稿箱';}setup();</script>`;
await context.route('**/*',async r=>{const u=new URL(r.request().url());let body;
 if(u.href==='https://t.bilibili.com/')body='<a class="header-entry-mini" href="https://space.bilibili.com/'+uid+'/">Owned identity</a>';
 else if(u.origin==='https://member.bilibili.com'&&u.pathname==='/platform/upload/video/frame'){
  const fromSaved=savedView;savedView=null;const index=fromSaved?Number(fromSaved):cached.has(selected)?selected:[...cached.keys()][0];
  if(index){body='<main id="main">'+form(index,Boolean(fromSaved),true)+'</main>'+script.replace('INDEX',String(index));}
  else body='<input name="buploader" type=file accept=.mp4><div>上传入口，无本次剩余缓存</div>';
 }else if(u.href===manager){
  body='<h1>稿件管理</h1><a href="/platform/upload-manager/article">视频管理</a><a href="/platform/upload-manager/opus">图文管理</a><a href="/platform/upload-manager/inactArticle">互动视频管理</a><div>全部稿件 0</div><button>草稿 '+saved.size+'</button>'+[...saved].map(([i,x])=>'<a href="javascript:;" onclick="openDraft('+i+')">'+esc(x.title)+'</a>').join('')+'<script>async function openDraft(index){await ownedOpenSaved(index);location.href='+JSON.stringify(editor)+';}</script>';
 }else{external++;await r.abort();return;}
 await r.fulfill({status:200,contentType:'text/html',headers:{'x-owned-fixture':'true'},body:'<!doctype html><meta charset="utf-8"><style>input{display:block;width:600px}.file-item{padding:5px}.ql-editor{height:70px}.ql-editor p{margin:0}span{display:inline-block;padding:4px}</style>'+body});
});
const page=await context.newPage();page.on('filechooser',()=>choosers++);
const run=()=>reconcileCachedPrivateTasks({page,context,spec,root,beforeAction:async()=>{fences++;assert.deepEqual(await fs.readFile(original.file),originalBytes);},saveTimeoutMs:250});
try{
 if(['duplicate_task','guard','unknown_save','other_modal'].includes(scenario)){
  let code;try{await run();}catch(e){code=e.code;}assert.ok(code);
  if(scenario==='unknown_save'){assert.equal(saves,1);try{await run();}catch{}assert.equal(saves,1);}
  else{assert.equal(saves,0);assert.equal(acknowledgements,0);}
 }else{const result=await run();assert.equal(result.state,'two_separate_private_drafts_saved');assert.equal(result.result.tasks.length,2);assert.equal(fillBeforeSave,true);assert.equal(saves,scenario==='save_all'?1:2);assert.equal(saved.size,2);if(scenario==='known_info')assert.ok(acknowledgements>0);
  const before=saves,again=await run();assert.equal(again.state,'two_separate_private_drafts_saved');assert.equal(saves,before);
 }
 assert.equal(choosers,0);assert.equal(publics,0);assert.equal(external,0);assert.ok(fences>0);assert.deepEqual(await fs.readFile(original.file),originalBytes);assert.deepEqual(await fs.readFile(sample.file),sourceBytes);
 console.log(JSON.stringify({scenario,owned:true,saves,public_clicks:publics,file_choosers:choosers,external_requests:external,original_evidence_unchanged:true}));
}catch(e){console.error(JSON.stringify(await page.evaluate(()=>({title:document.querySelector('#title')?.value,description:document.querySelector('.ql-editor')?.innerHTML}))));throw e;}finally{await context.close();await browser.close();}
