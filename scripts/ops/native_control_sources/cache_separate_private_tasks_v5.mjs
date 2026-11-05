// Reconcile only the two already selected private clips. No chooser, AddP,
// publication, DB mutation, or deletion operation exists in this controller.
import fs from 'node:fs/promises';
import {createReadStream} from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {inspectVideoProtection} from '../../../adapters/bilibili_video_guard.mjs';
import {handleNativeControl as navigateDraft} from '../../../adapters/short_video_saved_draft_navigation_control.mjs';
import {canonicalDigest,readNativeDescription,handleNativeControl as fillMetadata} from './short-video-native-metadata-v5.mjs';
export const nativeControlCapabilities=Object.freeze({fill_metadata:true,private_save:true,submit:false,upload:false,add_part:false});
const UUID=/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/,SHA=/^[a-f0-9]{64}$/;
const MEMBER='https://member.bilibili.com',EDITOR='/platform/upload/video/frame',MANAGER='/platform/upload-manager/article';
function fail(code){throw Object.assign(new Error(code),{code});}
const hash=b=>createHash('sha256').update(b).digest('hex');
async function safe(file){if(!path.isAbsolute(file)||path.resolve(file)!==file)fail('CACHED_PRIVATE_PATH_INVALID');for(let p=file;p!==path.dirname(p);p=path.dirname(p))if((await fs.lstat(p)).isSymbolicLink())fail('CACHED_PRIVATE_PATH_INVALID');if(await fs.realpath(file)!==file)fail('CACHED_PRIVATE_PATH_INVALID');}
async function syncdir(folder){if(process.platform!=='win32'){const h=await fs.open(folder,'r');try{await h.sync();}finally{await h.close();}}}
async function write(file,value,exclusive=false){await fs.mkdir(path.dirname(file),{recursive:true,mode:0o700});await safe(path.dirname(file));const target=exclusive?file:file+'.partial',h=await fs.open(target,exclusive?'wx':'w',0o600);try{await h.writeFile(JSON.stringify(value,null,2)+'\n');await h.sync();}finally{await h.close();}if(!exclusive)await fs.rename(target,file);await syncdir(path.dirname(file));}
async function evidence(file,max=1000000){await safe(file);const h=await fs.open(file,'r');let raw;try{const b=await h.stat();if(!b.isFile()||b.size<1||b.size>max)fail('CACHED_PRIVATE_DOCUMENT_INVALID');raw=await h.readFile();const a=await h.stat();if(raw.length!==b.size||['size','mtimeMs','ctimeMs','dev','ino'].some(k=>a[k]!==b[k]))fail('CACHED_PRIVATE_DOCUMENT_CHANGED');}finally{await h.close();}return {raw,ref:{file,bytes:raw.length,sha256:hash(raw)}};}
async function read(file){return JSON.parse((await evidence(file)).raw);}
async function checkedRef(ref){if(!ref||Object.keys(ref).sort().join()!=='bytes,file,sha256'||!SHA.test(ref.sha256||'')||!Number.isSafeInteger(ref.bytes))fail('CACHED_PRIVATE_DOCUMENT_INVALID');const actual=await evidence(ref.file);if(canonicalDigest(actual.ref)!==canonicalDigest(ref))fail('CACHED_PRIVATE_DOCUMENT_CHANGED');return JSON.parse(actual.raw);}

export async function reconcileCachedPrivateTasks({page,context,spec,root,beforeAction,saveTimeoutMs=15000}){
 if(typeof beforeAction!=='function')fail('CACHED_PRIVATE_PARENT_FENCE_REQUIRED');
 root=path.resolve(root);
 if(spec?.version!=='cached-separate-private-tasks-manifest-v5'||![spec.maintenance_id,spec.original_job_id,spec.original_attempt_id].every(x=>UUID.test(x||''))||
  typeof spec.target_site_uid!=='string'||!/^\d{1,30}$/.test(spec.target_site_uid)||spec.source_id!=='9T5tn1WGlJg'||spec.source_url!=='https://www.youtube.com/watch?v='+spec.source_id||
  !Array.isArray(spec.tasks)||spec.tasks.length!==2||new Set(spec.tasks.map(t=>t.metadata?.title)).size!==2||!Number.isSafeInteger(saveTimeoutMs)||saveTimeoutMs<1||saveTimeoutMs>15000)fail('CACHED_PRIVATE_SCOPE_INVALID');
 for(const [i,t] of spec.tasks.entries()){
  const m=t.metadata;
  if(t.index!==i+1||t.filename!==`part-00${i+1}.mp4`||!m||canonicalDigest(m)!==t.metadata_sha256||typeof m.title!=='string'||!m.title.includes('私有验收')||!m.title.endsWith(` P${i+1}/2`)||m.title.length>80||
   m.copyright!==2||m.source!==spec.source_url||m.category_label!=='娱乐'||typeof m.description!=='string'||!m.description.includes(spec.source_url)||m.description.includes('转载许可编号')||m.description.length>2000||!Array.isArray(m.tags)||m.tags.length<1||m.tags.length>10||new Set(m.tags).size!==m.tags.length||m.tags.some(t=>typeof t!=='string'||!t.trim()||t.length>20||t.includes(',')))fail('CACHED_PRIVATE_METADATA_INVALID');
 }
 const source=await checkedRef(spec.sample_manifest),old=await checkedRef(spec.original_selection_intent);
 if(!spec.sample_manifest.file.startsWith(path.join(root,'media','multipart-v4-native-real-samples-'))||path.basename(spec.sample_manifest.file)!=='samples.json'||
  source.version!=='real-source-private-multipart-samples-v4'||source.source_id!==spec.source_id||source.private_upload_only!==true||source.public_submission_allowed!==false||source.formal_splitter_acceptance_claimed!==false||source.parts?.length!==2||
  spec.original_selection_intent.file!==path.join(root,'media',spec.original_job_id,'multipart-private-v4','video-selection-intent.json')||old.version!=='multipart-private-one-use-intent-v4'||old.kind!=='video-selection'||old.job_id!==spec.original_job_id||old.attempt_id!==spec.original_attempt_id||old.site_uid!==spec.target_site_uid||old.source_id!==spec.source_id||old.source_url!==spec.source_url||old.parts?.length!==2)fail('CACHED_PRIVATE_ORIGINAL_SELECTION_REQUIRED');
 const base={maintenance_id:spec.maintenance_id,original_job_id:spec.original_job_id,original_attempt_id:spec.original_attempt_id,site_uid:spec.target_site_uid,source_id:spec.source_id,source_url:spec.source_url,
  spec_sha256:canonicalDigest(spec),sample_manifest:spec.sample_manifest,original_selection_intent:spec.original_selection_intent,origin:'later_separate_tasks_private_v5',same_upload_session_observed:false,human_review:false,publication_authorized:false,native_multipart_verified:false,server_file_hash_verified:false};
 const parent=path.join(root,'media',spec.original_job_id,'cached-separate-private-v5'),output=path.join(parent,spec.maintenance_id);await fs.mkdir(output,{recursive:true,mode:0o700});await safe(output);
 let chooser=false;const onChooser=()=>{chooser=true;};page.on('filechooser',onChooser);
 const completed=new Map();let informationalAcknowledgements=0;
 async function fence(){await beforeAction();await checkedRef(spec.sample_manifest);await checkedRef(spec.original_selection_intent);if(chooser)fail('CACHED_PRIVATE_UNEXPECTED_CHOOSER');}
 function informationalOnly(p){const e=p?.evidence;return !p.blocked||p.error_code==='BILIBILI_VIDEO_VISIBLE_DIALOG_REQUIRES_REVIEW'&&e?.inspection_failed!==true&&e.truncated===false&&e.top_level_authentication===false&&e.visible_challenge_controls?.length===0&&e.challenge_frames?.length===0&&e.visible_dialogs?.length===1;}
 async function dismissKnownInformation(protection){
  if(!informationalOnly(protection))return false;
  const title=page.getByText('部分草稿内容已失效',{exact:true}).filter({visible:true}),message=page.getByText('试看时长不可用，已填写默认时长',{exact:true}).filter({visible:true}),ack=page.getByText('我知道了',{exact:true}).filter({visible:true});
  const counts=await Promise.all([title.count(),message.count(),ack.count()]);
  if(counts.every(n=>n===0))return false;
  if(counts.some(n=>n!==1))fail('CACHED_PRIVATE_INFORMATION_NOT_CONFIRMED');
  const nodes=await Promise.all([title.elementHandle(),message.elementHandle(),ack.elementHandle()]);
  const exact=await page.evaluate(([a,b,c])=>{let common=c;while(common&&!common.contains(a)||common&&!common.contains(b))common=common.parentElement;
   const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';};
   const dialogs=[...document.querySelectorAll('[role="dialog"],[role="alertdialog"],dialog[open],[aria-modal="true"]')].filter(visible);
   const text=common?.innerText.replace(/\s+/g,' ').trim();
   return ['部分草稿内容已失效 试看时长不可用，已填写默认时长 我知道了',
    '部分草稿内容已失效 以下内容已按默认值处理或清空，请检查后再投稿： 试看时长不可用，已填写默认时长 我知道了'].includes(text)&&
    (dialogs.length===0||dialogs.length===1&&dialogs[0].contains(common))?text:false;},nodes);
  if(!exact||informationalAcknowledgements>=4)fail('CACHED_PRIVATE_INFORMATION_NOT_CONFIRMED');
  await fence();const fresh=await inspectVideoProtection(page);if(!informationalOnly(fresh))fail(fresh.error_code);
  if(await title.count()!==1||await message.count()!==1||await ack.count()!==1)fail('CACHED_PRIVATE_INFORMATION_CHANGED');
  await write(path.join(output,'information-ack-'+String(informationalAcknowledgements+1)+'.json'),{...base,kind:'exact_expired_preview_information',observed_at:new Date().toISOString(),exact_visible_text:exact,protection:fresh});
  await fence();const final=await inspectVideoProtection(page);if(!informationalOnly(final))fail(final.error_code);await ack.click({timeout:5000});informationalAcknowledgements++;
  return true;
 }
 async function guard(){await fence();const u=new URL(page.url());if(u.origin!==MEMBER||![EDITOR,MANAGER].includes(u.pathname)||u.hash)fail('CACHED_PRIVATE_ORIGIN_CHANGED');if(u.search)fail('CACHED_PRIVATE_DRAFT_QUERY_UNOBSERVED');let p=await inspectVideoProtection(page);if(await dismissKnownInformation(p))p=await inspectVideoProtection(page);if(p.blocked)fail(p.error_code);return true;}
 async function identity(){await fence();const p=await context.newPage();try{await p.goto('https://t.bilibili.com/',{waitUntil:'domcontentloaded',timeout:45000});const a=p.locator('a.header-entry-mini:visible');await a.waitFor({state:'visible',timeout:30000});if(await a.count()!==1)fail('CACHED_PRIVATE_UID_AMBIGUOUS');const u=new URL(await a.getAttribute('href'),'https://t.bilibili.com/');if(u.origin!=='https://space.bilibili.com'||u.username||u.password||u.search||u.hash||/^\/(\d+)\/?$/.exec(u.pathname)?.[1]!==spec.target_site_uid)fail('TARGET_ACCOUNT_IDENTITY_MISMATCH');return spec.target_site_uid;}finally{await p.close();}}
 async function verify(){await guard();await identity();await guard();}
 async function fileCheck(index){
  await fence();const p=source.parts[index-1],o=old.parts[index-1];
  if(p.path!==path.join(path.dirname(spec.sample_manifest.file),spec.tasks[index-1].filename)||p.path!==o.file||p.bytes!==o.bytes||p.sha256!==o.sha256||p.probe?.format?.duration===undefined||Number(p.probe.format.duration)!==o.duration_seconds||!Number.isSafeInteger(p.bytes)||p.bytes<1||p.bytes>100000000||!SHA.test(p.sha256||''))fail('CACHED_PRIVATE_SAMPLE_BINDING_CHANGED');
  await safe(p.path);const b=await fs.stat(p.path);if(!b.isFile()||b.size!==p.bytes)fail('CACHED_PRIVATE_SAMPLE_CHANGED');const h=createHash('sha256');for await(const chunk of createReadStream(p.path))h.update(chunk);const a=await fs.stat(p.path);if(['size','mtimeMs','ctimeMs','ino','dev'].some(k=>a[k]!==b[k])||h.digest('hex')!==p.sha256)fail('CACHED_PRIVATE_SAMPLE_CHANGED');
  return {file:p.path,bytes:p.bytes,sha256:p.sha256,duration_seconds:Number(p.probe.format.duration)};
 }
 async function unique(l){if(await l.count()!==1)fail('CACHED_PRIVATE_CONTROL_AMBIGUOUS');return l;}
 async function inventory(label){const raw=await page.evaluate(()=>({url:location.origin+location.pathname,url_query_keys:[...new Set(new URL(location.href).searchParams.keys())].sort(),body:(document.body?.innerText||'').slice(0,30000)}));await write(path.join(output,label+'.json'),{...base,observed_at:new Date().toISOString(),visible_dom:raw,read_only:true});return {};}
 async function photo(name){const file=path.join(output,name);await page.screenshot({path:file,fullPage:true,timeout:15000});const h=await fs.open(file,'r+');try{await h.sync();}finally{await h.close();}await syncdir(output);return (await evidence(file,10000000)).ref;}
 async function readback(t){await guard();const title=await (await unique(page.locator('input[placeholder="请输入稿件标题"]:visible'))).inputValue(),desc=await readNativeDescription(await unique(page.locator('.ql-editor:visible'))),creation_statement=await (await unique(page.locator('input[placeholder="请选择符合您视频内容的创作声明"]:visible'))).inputValue(),sourceURL=await (await unique(page.locator('input[placeholder^="转载视频请注明来源"]:visible'))).inputValue(),category=(await (await unique(page.locator('.video-human-type .select-controller:visible'))).innerText()).trim(),tags=(await page.locator('.label-item-v2-container:visible p.label-item-v2-content').allTextContents()).map(x=>x.trim());
  if(title!==t.metadata.title||!desc.supported||desc.text!==t.metadata.description||creation_statement!=='内容为转载'||sourceURL!==spec.source_url||category!=='娱乐'||tags.length!==t.metadata.tags.length||new Set(tags).size!==tags.length||t.metadata.tags.some(x=>!tags.includes(x)))fail('CACHED_PRIVATE_METADATA_READBACK_FAILED');return {title,description:desc.text,creation_statement,source:sourceURL,category,tags};}
 async function surface(t){
  await guard();const outer=page.locator('.task-list-content-item:visible'),inner=page.locator('.file-list .file-item:visible');
  const names=await outer.locator('.task-title-text:visible').allTextContents();if(names.length!==await outer.count()||names.length>2||await inner.count()!==1)fail('CACHED_PRIVATE_TASK_SURFACE_CHANGED');
  const allowed=new Set(spec.tasks.flatMap(x=>[x.filename,x.filename.replace(/\.mp4$/i,''),x.metadata.title]));if(names.some(n=>!allowed.has(n.trim())))fail('CACHED_PRIVATE_FOREIGN_TASK_PRESENT');
  const title=await unique(inner.locator('.title-text:visible')),native=(await title.innerText()).trim();if(![t.filename,t.filename.replace(/\.mp4$/i,''),t.metadata.title].includes(native))fail('CACHED_PRIVATE_SELECTED_FILE_CHANGED');
  if(names.length){const selected=await unique(page.locator('.task.task-selected:visible')),n=await unique(selected.locator('.task-title-text:visible'));if(![t.filename,t.filename.replace(/\.mp4$/i,''),t.metadata.title].includes((await n.innerText()).trim()))fail('CACHED_PRIVATE_SELECTED_TASK_CHANGED');}
  const success=inner.getByText('上传完成',{exact:true}).filter({visible:true});if(await success.count()!==1)fail('CACHED_PRIVATE_UPLOAD_NOT_COMPLETE');
  return {surface_mode:names.length?'selected_cached_task_sidebar':'single_editor_no_batch_sidebar',actual_outer_count:names.length,actual_inner_count:1,outer_titles:names,native_filename:native,native_upload_complete:true};
 }
 async function openCache(){
  await fence();await page.goto(MEMBER+EDITOR,{waitUntil:'domcontentloaded',timeout:45000});const end=Date.now()+20000;
  do{await guard();if(await page.locator('input[placeholder="请输入稿件标题"]:visible').count()===1){await verify();return;}
   const notice=page.getByText(/^本地浏览器存在\s*[12]\s*个未提交的视频$/,{exact:true}).filter({visible:true});
   if(await notice.count()>1)fail('CACHED_PRIVATE_NOTICE_AMBIGUOUS');if(await notice.count()===1){const keep=await unique(page.getByText('继续编辑',{exact:true}).filter({visible:true}));await verify();await keep.click({timeout:5000});await page.locator('input[placeholder="请输入稿件标题"]:visible').waitFor({state:'visible',timeout:20000});await verify();return;}
   await page.waitForTimeout(150);
  }while(Date.now()<end);fail('CACHED_PRIVATE_EDITOR_NOT_CONFIRMED');
 }
 async function select(t){
  const u=new URL(page.url());if(u.origin!==MEMBER||u.pathname!==EDITOR||await page.locator('input[placeholder="请输入稿件标题"]:visible').count()!==1)await openCache();
  await verify();const candidates=page.locator('.task-list-content-item:visible'),matches=[];
  for(let i=0;i<await candidates.count();i++){const x=candidates.nth(i).locator('.task-title-text:visible');if(await x.count()!==1)fail('CACHED_PRIVATE_TASK_AMBIGUOUS');if([t.filename,t.filename.replace(/\.mp4$/i,''),t.metadata.title].includes((await x.innerText()).trim()))matches.push(candidates.nth(i));}
  if(matches.length>1)fail('CACHED_PRIVATE_TASK_AMBIGUOUS');
  if(matches.length===1){await guard();await matches[0].locator('.task-title-text:visible').click({timeout:5000});}
  const end=Date.now()+3000;do{await guard();try{return await surface(t);}catch(e){if(!['CACHED_PRIVATE_SELECTED_FILE_CHANGED','CACHED_PRIVATE_SELECTED_TASK_CHANGED'].includes(e.code))throw e;}await page.waitForTimeout(100);}while(Date.now()<end);fail('CACHED_PRIVATE_TASK_NOT_SELECTED');
 }
 async function lookupSaved(t){
  await fence();await page.goto(MEMBER+MANAGER,{waitUntil:'domcontentloaded',timeout:45000});
  const nav=await navigateDraft({page,spec:{job_id:spec.original_job_id,attempt_id:spec.original_attempt_id,recovery_id:spec.maintenance_id,target_site_uid:spec.target_site_uid,metadata:t.metadata},output,control_action:'lookup',helpers:{checkProtection:guard,accountIdentity:identity,write:(n,v)=>write(path.join(output,'task-'+t.index+'-'+n),v),inventory:label=>inventory('task-'+t.index+'-'+label)}});
  if(nav.state!=='saved_draft_editor_opened')return null;
  await verify();return {readback:await readback(t),native_surface:await surface(t),navigation:nav};
 }
 async function saveIntent(t){const file=path.join(parent,'save-task-'+t.index+'-intent.json');try{return {file,body:await read(file)};}catch(e){if(e.code!=='ENOENT')throw e;return null;}}
 async function recordSaved(t,observed,{toast=false,later=false}={}){
  const receipt={version:'later-separate-private-task-saved-v5',...base,index:t.index,filename:t.filename,source_asset:await fileCheck(t.index),metadata_sha256:t.metadata_sha256,
   private_draft_saved:true,native_readback:observed.readback,native_surface:observed.native_surface,readback_phase:toast?'immediately_before_private_save':'later_manager_opened_saved_editor',save_toast_observed:toast,later_manager_lookup_observed:later,
   screenshot:await photo('task-'+t.index+'-saved.png'),observed_at:new Date().toISOString(),does_not_reclassify_original_probe:true};
  await write(path.join(output,'task-'+t.index+'-saved.json'),receipt);await write(path.join(parent,'task-'+t.index+'-latest.json'),receipt);completed.set(t.index,receipt);return receipt;
 }
 try{
  await fileCheck(1);await fileCheck(2);
  // Fill BOTH before the first save. Native save may save current or all;
  // the second task is confirmed via manager before another click is allowed.
  for(const t of spec.tasks){
   const previous=await saveIntent(t);
   let priorSaved=null;try{priorSaved=await read(path.join(parent,'task-'+t.index+'-latest.json'));}catch(e){if(e.code!=='ENOENT')throw e;}
   if(previous||priorSaved){
    const item=previous?.body||priorSaved;
    if(item.original_job_id!==spec.original_job_id||item.original_attempt_id!==spec.original_attempt_id||item.site_uid!==spec.target_site_uid||item.source_id!==spec.source_id||item.source_url!==spec.source_url||item.index!==t.index||item.filename!==t.filename||item.metadata_sha256!==t.metadata_sha256||
      (previous?item.source_sha256:item.source_asset?.sha256)!==source.parts[t.index-1].sha256||canonicalDigest(item.sample_manifest)!==canonicalDigest(spec.sample_manifest)||canonicalDigest(item.original_selection_intent)!==canonicalDigest(spec.original_selection_intent))fail('CACHED_PRIVATE_SAVE_INTENT_CHANGED');
    const found=await lookupSaved(t);if(!found)fail('CACHED_PRIVATE_SAVE_RECONCILE_REQUIRED');await recordSaved(t,found,{later:true});continue;
   }
   const before=await select(t),folder=path.join(output,'task-'+t.index);await fs.mkdir(folder,{recursive:true,mode:0o700});await write(path.join(folder,'metadata.json'),t.metadata);
   const title=await unique(page.locator('input[placeholder="请输入稿件标题"]:visible')),description=await unique(page.locator('.ql-editor:visible'));
   if(!await description.evaluate(e=>e.getAttribute('contenteditable')==='true'))fail('CACHED_PRIVATE_DESCRIPTION_INVALID');
   if(await title.inputValue()!==t.metadata.title){await guard();await title.fill(t.metadata.title);}
   const currentDescription=await readNativeDescription(description);if(!currentDescription.supported)fail('CACHED_PRIVATE_DESCRIPTION_INVALID');
   if(currentDescription.text!==t.metadata.description){await guard();await description.fill(t.metadata.description);await guard();await description.blur();}
   const filled=await fillMetadata({page,spec:{job_id:spec.original_job_id,attempt_id:spec.original_attempt_id,recovery_id:spec.maintenance_id,target_site_uid:spec.target_site_uid,source_url:spec.source_url,metadata_sha256:t.metadata_sha256},output:folder,control_action:'fill_metadata',helpers:{checkProtection:guard,accountIdentity:identity,checkedFile:()=>fileCheck(t.index),write:(n,v)=>write(path.join(folder,n),v),inventory:label=>inventory('task-'+t.index+'-'+label)}});
   if(filled.text_metadata_verified!==true)fail(filled.error_code||'CACHED_PRIVATE_METADATA_FAILED');
   const observed={...base,index:t.index,before_surface:before,native_surface:await surface(t),readback:await readback(t),screenshot:await photo('task-'+t.index+'-filled.png'),observed_at:new Date().toISOString(),private_draft_saved:false};
   await write(path.join(output,'task-'+t.index+'-filled.json'),observed);
  }
  for(const t of spec.tasks){
   if(completed.has(t.index))continue;
   // A save of task 1 may also have saved task 2. Look first; never assume.
   if(t.index===2){const found=await lookupSaved(t);if(found){await recordSaved(t,found,{later:true});continue;}}
   await select(t);const observed={readback:await readback(t),native_surface:await surface(t)};
   const button=await unique(page.locator('span.submit-draft:visible').filter({hasText:/^\s*存草稿\s*$/}));
   if(await page.getByText('已存入草稿箱',{exact:true}).filter({visible:true}).count())fail('CACHED_PRIVATE_STALE_SAVE_TOAST');
   await verify();await fileCheck(t.index);const intentFile=path.join(parent,'save-task-'+t.index+'-intent.json');
   try{await write(intentFile,{version:'cached-private-save-one-use-intent-v5',...base,index:t.index,filename:t.filename,source_sha256:source.parts[t.index-1].sha256,metadata_sha256:t.metadata_sha256,metadata:t.metadata,created_at:new Date().toISOString()},true);}catch(e){if(e.code==='EEXIST')fail('CACHED_PRIVATE_SAVE_RECONCILE_REQUIRED');throw e;}
   await guard();await button.click({timeout:5000});const end=Date.now()+saveTimeoutMs;let toast=false;
   do{await fence();const protection=await inspectVideoProtection(page);if(protection.blocked)fail(protection.error_code);const u=new URL(page.url());if(u.origin!==MEMBER||![EDITOR,MANAGER].includes(u.pathname))fail('CACHED_PRIVATE_ORIGIN_CHANGED');toast=await page.getByText('已存入草稿箱',{exact:true}).filter({visible:true}).count()===1;if(toast)break;await page.waitForTimeout(100);}while(Date.now()<end);
   if(toast)await recordSaved(t,observed,{toast:true});
   else{const found=await lookupSaved(t);if(!found)fail('CACHED_PRIVATE_SAVE_RECONCILE_REQUIRED');await recordSaved(t,found,{later:true});}
  }
  const result={version:'cached-separate-private-tasks-result-v5',...base,state:'two_separate_private_drafts_saved',tasks:[...completed.values()],observed_at:new Date().toISOString(),informational_acknowledgements_count:informationalAcknowledgements,file_selections:0,add_part_clicks:0,submit_clicks:0,does_not_reclassify_original_probe:true};
  await write(path.join(output,'result.json'),result);return {state:result.state,result_file:(await evidence(path.join(output,'result.json'))).ref,result};
 }catch(e){await inventory('blocked').catch(()=>{});await photo('blocked.png').catch(()=>{});const result={version:'cached-separate-private-tasks-result-v5',...base,state:'reconcile_required',error_code:/^[A-Z][A-Z0-9_]{1,100}$/.test(e?.code||'')?e.code:'CACHED_PRIVATE_OPERATION_FAILED',tasks:[...completed.values()],informational_acknowledgements_count:informationalAcknowledgements,file_selections:0,add_part_clicks:0,submit_clicks:0,does_not_reclassify_original_probe:true};await write(path.join(output,'result.json'),result);throw Object.assign(new Error(result.error_code),{code:result.error_code,output});}
 finally{page.off('filechooser',onChooser);}
}
