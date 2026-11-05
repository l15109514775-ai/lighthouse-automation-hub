// Fixed native operations for the replay Worker. No internal site API/state.
import fs from 'node:fs/promises';
import {createReadStream,constants as fileConstants} from 'node:fs';
import path from 'node:path';
import {createHash,randomUUID as cryptoRandomUUID} from 'node:crypto';
import {inspectVideoProtection} from './bilibili_video_guard.mjs';
import {canonicalDigest,readNativeDescription,handleNativeControl as fillObservedMetadata} from '../scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs';
import {handleNativeControl as navigateSavedDraft} from './short_video_saved_draft_navigation_control.mjs';
import {validateHighestContract,checkHighestSourceFiles} from './replay_highest_contract_v2.mjs';
import {validateNaturalContract,checkNaturalSourceFiles} from './replay_natural_contract_v3.mjs';
import {validatePartContract,checkPartSourceFiles,canonicalPartFile,validatePartReservation} from './replay_part_contract_v5.mjs';
import {observeLaterReplayResult,replayDraftLink} from './replay_later_result_lookup_v2.mjs';
import {settleEmptyBodyProtection} from './replay_part_owner_handoff_v5.mjs';

const UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const SHA=/^[a-f0-9]{64}$/,BV=/^BV[A-Za-z0-9]{10}$/;
const ORIGIN='https://member.bilibili.com',EDITOR='/platform/upload/video/frame';
const CHANNELS=new Set(['UCaaaaaaaaaaaaaaaaaaaaaa','UCbbbbbbbbbbbbbbbbbbbbbb']);
const FILTER='[0:v]format=rgb24,split=2[bg0][fg];[bg0]scale=1280:960:force_original_aspect_ratio=increase:flags=lanczos,crop=1280:960,gblur=sigma=35:steps=3[bg];[bg][fg]overlay=0:120:format=rgb,format=rgb24[out]';
function fail(code){throw Object.assign(new Error(code),{code});}
// innerText collapses repeated ASCII spaces in the sidebar's rendered label.
// Keep file identity and the editable title checks exact elsewhere.
export function selectedTaskTitleMatches(observed,allowed){return allowed.some(value=>observed===value||observed===value.replace(/ +/g,' '));}
const ROUTES=Object.freeze({
 'replay-native-browser-manifest-v1':Object.freeze({maximum:3_800_000_000,seconds:86400,download:'youtube-download-v1',output:'replay-browser',prepare:'replay-native-preparation-v1',form:'replay-native-form-observation-v1',result:'short-video-browser-result-v1',reservation:'replay-native-reservation-v1'}),
 'replay-highest-native-browser-manifest-v2':Object.freeze({maximum:14_000_000_000,seconds:36000,download:'youtube-highest-v2',output:'replay-browser-highest-v2',prepare:'replay-highest-native-preparation-v2',form:'replay-highest-native-form-observation-v2',result:'replay-highest-browser-result-v2',reservation:'replay-highest-native-reservation-v2',highest:true}),
 'replay-natural-native-browser-manifest-v3':Object.freeze({maximum:14_000_000_000,seconds:36000,download:'youtube-highest-v3',output:'replay-browser-natural-v3',prepare:'replay-natural-native-preparation-v3',form:'replay-natural-native-form-observation-v3',result:'replay-natural-browser-result-v3',reservation:'replay-natural-native-reservation-v3',highest:true,natural:true}),
 'replay-part-native-browser-manifest-v5':Object.freeze({maximum:14_000_000_000,seconds:36000,output:'replay-browser-part-v5',prepare:'replay-part-native-preparation-v5',form:'replay-part-native-form-observation-v5',result:'replay-part-browser-result-v5',reservation:'replay-part-native-reservation-v5',upload:'replay-part-browser-upload-v5',uploadCoverage:'replay-part-native-upload-coverage-v5',intent:'replay-part-one-use-intent-v5',highest:true,standalone:true})
});
export function validateReplayManifest(spec){
 const route=ROUTES[spec?.version];
 if(!spec||!route||![spec.job_id,spec.download_job_id,spec.attempt_id].every(x=>UUID.test(x||''))||
  !CHANNELS.has(spec.channel_id)||typeof spec.target_site_uid!=='string'||!/^\d{1,30}$/.test(spec.target_site_uid)||
  !/^[A-Za-z0-9_-]{11}$/.test(spec.source_id||'')||spec.source_url!=='https://www.youtube.com/watch?v='+spec.source_id||
  ![spec.artifact_sha256,spec.metadata_sha256,spec.binding_sha256,spec.policy_sha256].every(x=>SHA.test(x||''))||
  !Number.isSafeInteger(spec.file_bytes)||spec.file_bytes<1||spec.file_bytes>route.maximum||
  !Number.isFinite(spec.duration_seconds)||spec.duration_seconds<=0||spec.duration_seconds>route.seconds||!spec.metadata||typeof spec.metadata!=='object'||Array.isArray(spec.metadata)||
  canonicalDigest(spec.metadata)!==spec.metadata_sha256||spec.metadata?.copyright!==2||spec.metadata?.source!==spec.source_url||
  spec.metadata?.category_label!=='娱乐'||typeof spec.metadata?.title!=='string'||!spec.metadata.title.trim()||spec.metadata.title.length>80||
  typeof spec.metadata?.description!=='string'||!spec.metadata.description.includes(spec.source_url)||spec.metadata.description.includes('转载许可编号')||
  !Array.isArray(spec.metadata.tags)||spec.metadata.tags.length<1||spec.metadata.tags.length>10||
  spec.metadata.description.length>2000||new Set(spec.metadata.tags).size!==spec.metadata.tags.length||spec.metadata.tags.some(t=>typeof t!=='string'||!t.trim()||t.length>20||t.includes(','))||
  spec.human_review!==false||spec.per_artifact_owner_acceptance!==false)fail('REPLAY_BROWSER_MANIFEST_INVALID');
 if(route.standalone){validatePartContract(spec);if('highest_contract' in spec||'natural_contract' in spec)fail('REPLAY_BROWSER_MANIFEST_INVALID');}else if(route.natural)validateNaturalContract(spec);else if(route.highest)validateHighestContract(spec);else if('highest_contract' in spec)fail('REPLAY_BROWSER_MANIFEST_INVALID');return spec;
}
async function noSymlinks(file){for(let p=file;p!==path.dirname(p);p=path.dirname(p))if((await fs.lstat(p)).isSymbolicLink())fail('REPLAY_BROWSER_PATH_INVALID');if(await fs.realpath(file)!==file)fail('REPLAY_BROWSER_PATH_INVALID');}
export async function streamCheckedFile(file,sha256,bytes,routeVersion='replay-native-browser-manifest-v1'){
 const route=ROUTES[routeVersion];
 if(!route||!path.isAbsolute(file||'')||!SHA.test(sha256||'')||!Number.isSafeInteger(bytes)||bytes<1||bytes>route.maximum)fail('REPLAY_BROWSER_FILE_INVALID');
 await noSymlinks(file);const before=await fs.stat(file);if(!before.isFile()||before.size!==bytes)fail('REPLAY_BROWSER_FILE_SIZE_CHANGED');
 const hash=createHash('sha256');for await(const chunk of createReadStream(file))hash.update(chunk);
 const after=await fs.stat(file);if(before.size!==after.size||before.mtimeMs!==after.mtimeMs||before.ctimeMs!==after.ctimeMs||before.ino!==after.ino||before.dev!==after.dev||before.mode!==after.mode)fail('REPLAY_BROWSER_FILE_CHANGED');
 if(hash.digest('hex')!==sha256)fail('REPLAY_BROWSER_FILE_HASH_CHANGED');await noSymlinks(file);return {size:after.size,mtimeMs:after.mtimeMs,ctimeMs:after.ctimeMs,
  ...(route.standalone?{ino:after.ino,dev:after.dev,mode:after.mode}:{}),
  ...(route.highest?{whole_file_streamed:true,streamed_bytes:after.size,streamed_sha256:sha256}:{})};
}
async function durableWrite(file,value,{exclusive=false}={}){
 const parent=path.dirname(file);await fs.mkdir(parent,{recursive:true,mode:0o700});await noSymlinks(parent);
 const target=exclusive?file:file+'.partial';const h=await fs.open(target,exclusive?'wx':'w',0o600);
 try{await h.writeFile(JSON.stringify(value,null,2)+'\n');await h.sync();}finally{await h.close();}
 if(!exclusive)await fs.rename(target,file);if(process.platform!=='win32'){const dir=await fs.open(parent,'r');try{await dir.sync();}finally{await dir.close();}}
}
async function readJSON(file,max=65536){await noSymlinks(file);const st=await fs.stat(file);if(!st.isFile()||st.size<1||st.size>max)fail('REPLAY_BROWSER_EVIDENCE_INVALID');return JSON.parse(await fs.readFile(file,'utf8'));}
async function fileEvidence(file){const st=await fs.stat(file),h=await fs.open(file,'r+');try{await h.sync();}finally{await h.close();}return {file,bytes:st.size,sha256:createHash('sha256').update(await fs.readFile(file)).digest('hex')};}

export async function createReplayNativeDriver({page,context,spec,root,uploadTimeoutMs=12*3600*1000,resultTimeoutMs=30000,postSubmitObservationMs=5000,onProtection=null}){
 validateReplayManifest(spec);root=path.resolve(root);
 const route=ROUTES[spec.version],source=route.standalone?canonicalPartFile(root,spec):path.join(root,'media',spec.download_job_id,route.download,'original.mp4');
  if(route.standalone&&(!Number.isSafeInteger(postSubmitObservationMs)||postSubmitObservationMs<1||postSubmitObservationMs>10000))fail('REPLAY_PART_POST_SUBMIT_OBSERVATION_BUDGET_INVALID');
 if(spec.file!==source)fail('REPLAY_BROWSER_FILE_PATH_INVALID');
 const output=path.join(root,'media',spec.job_id,route.output,spec.attempt_id),intents=path.join(root,'media',spec.job_id,'replay-native-intents');
 await fs.mkdir(output,{recursive:true,mode:0o700});await noSymlinks(output);
 await durableWrite(path.join(output,'metadata.json'),spec.metadata);
 const binding={job_id:spec.job_id,attempt_id:spec.attempt_id,site_uid:spec.target_site_uid,source_id:spec.source_id,source_url:spec.source_url,
  artifact_sha256:spec.artifact_sha256,file_bytes:spec.file_bytes,metadata_sha256:spec.metadata_sha256,binding_sha256:spec.binding_sha256,
  ...(route.standalone?{parent_job_id:spec.parent_job_id,part_index:spec.part_index,part_count:spec.part_count,part_idempotency_key:spec.part_idempotency_key,whole_plan_sha256:spec.whole_plan_sha256,parts_manifest_sha256:spec.parts_manifest_sha256,policy_sha256:spec.policy_sha256}:{})};
  let busy=false,expectedImageChooser=false,unexpectedChooser=false,uploadReference=null,videoSelectionAttempted=false;
  let observingSubmittedPage=false,submitClickState='not_issued',submittedPageProtectionObserved=false;
 // A video chooser is permitted only in the one reserved upload operation.
 let permittedVideoChooser=false;
 page.on('filechooser',()=>{if(!expectedImageChooser&&!permittedVideoChooser)unexpectedChooser=true;});
 const write=(name,value)=>durableWrite(path.join(output,name),value);
 async function guard(){
  const url=new URL(page.url());if(url.origin!==ORIGIN||unexpectedChooser)fail('REPLAY_BROWSER_ORIGIN_OR_CHOOSER_CHANGED');
  let protection=await inspectVideoProtection(page),readiness_observation=null;
  if(route.standalone&&protection.blocked){const settled=await settleEmptyBodyProtection({page,protection,output,spec});protection=settled.protection;readiness_observation=settled.readiness_observation;}
  if(route.standalone&&(new URL(page.url()).origin!==ORIGIN||unexpectedChooser))fail('REPLAY_BROWSER_ORIGIN_OR_CHOOSER_CHANGED');
   if(protection.blocked){await write('protection-required.json',protection);if(observingSubmittedPage){submittedPageProtectionObserved=true;await tryCaptureSubmittedPage('protection',protection);}if(route.standalone&&onProtection)await onProtection({page,protection,readiness_observation});fail(protection.error_code);}
  return true;
 }
 async function accountIdentity(){
  const identity=await context.newPage();try{
   await identity.goto('https://t.bilibili.com/',{waitUntil:'domcontentloaded',timeout:45000});
   const avatar=identity.locator('a.header-entry-mini:visible');await avatar.waitFor({state:'visible',timeout:30000});
   if(await avatar.count()!==1)fail('REPLAY_BROWSER_UID_AMBIGUOUS');
   const link=new URL(await avatar.getAttribute('href'),'https://t.bilibili.com/');
   const uid=link.origin==='https://space.bilibili.com'&&!link.username&&!link.password&&/^\/(\d+)\/?$/.exec(link.pathname)?.[1];
   if(uid!==spec.target_site_uid)fail('TARGET_ACCOUNT_IDENTITY_MISMATCH');return uid;
  }finally{await identity.close();}
 }
 async function verify(){await guard();await accountIdentity();return true;}
 async function checkedFile(){return streamCheckedFile(spec.file,spec.artifact_sha256,spec.file_bytes,spec.version);}
 async function checkedUploadedFingerprint(expectedUploadReference){
  // V5 upload and parent QA already read every selected byte. Immediately
  // before submission, preserve that file identity under the same writer
  // locks; this stat check is explicitly not another SHA observation.
  const observed=await readJSON(path.join(output,'upload-observation.json'));
  const ref=observed.upload_coverage,expected=path.join(output,'upload-coverage.json');
  if(observed.version!==route.upload||Object.entries(binding).some(([k,v])=>observed[k]!==v)||
   observed.upload_reference_sha256!==expectedUploadReference||!SHA.test(expectedUploadReference||'')||
   ref?.file!==expected||canonicalDigest(await fileEvidence(expected))!==canonicalDigest(ref)||
   canonicalDigest({...binding,local_causal_intent:await fileEvidence(path.join(intents,'video-selection.json')),
    native_upload_complete:true,upload_coverage:ref})!==expectedUploadReference)fail('REPLAY_PART_UPLOAD_FINGERPRINT_CHANGED');
  const coverage=await readJSON(expected),fp=coverage.local_file_fingerprint;
  if(coverage.version!==route.uploadCoverage||Object.entries(binding).some(([k,v])=>coverage[k]!==v)||
   coverage.file!==spec.file||coverage.streamed_sha256!==spec.artifact_sha256||coverage.streamed_bytes!==spec.file_bytes||
   coverage.whole_file_streamed!==true||!fp||Object.keys(fp).sort().join()!=='ctimeMs,dev,ino,mode,mtimeMs,size'||
   Object.values(fp).some(v=>typeof v!=='number'||!Number.isFinite(v))||fp.size!==spec.file_bytes)fail('REPLAY_PART_UPLOAD_FINGERPRINT_CHANGED');
  await noSymlinks(spec.file);const st=await fs.stat(spec.file);
  if(!st.isFile()||Object.keys(fp).some(k=>st[k]!==fp[k]))fail('REPLAY_PART_UPLOAD_FINGERPRINT_CHANGED');
  return {same_uploaded_file_fingerprint:true,file_rehashed:false};
 }
 async function highestSourceCheck(){if(route.standalone)return checkPartSourceFiles(root,spec);if(route.natural)return checkNaturalSourceFiles(root,spec);if(route.highest)return checkHighestSourceFiles(root,spec);}
 async function freshNativeLimits(){
  if(!route.highest)return;
  await highestSourceCheck();await verify();
  const body=await page.locator('body').innerText();
  if(!body.includes('视频大小16G以内，时长10小时以内'))fail('REPLAY_HIGHEST_NATIVE_UPLOAD_LIMIT_CHANGED');
  const observed={version:route.standalone?'bilibili-part-native-upload-limits-v5':'bilibili-highest-native-upload-limits-v2',job_id:spec.job_id,attempt_id:spec.attempt_id,
   site_uid:spec.target_site_uid,url:page.url(),observed_at:new Date().toISOString(),
   native_text:'视频大小16G以内，时长10小时以内',max_file_bytes:16_000_000_000,max_duration_seconds:36000,
   source_actual_start_time:route.standalone?spec.source_contract.source_actual_start_time:spec.highest_contract.download_binding.source_actual_start_time,
   operating_not_before:route.standalone?spec.source_contract.operating_not_before:spec.highest_contract.download_binding.operating_not_before,
   server_file_hash_verified:false,human_review:false};
  await write('fresh-native-upload-limits.json',observed);
 }
 async function inventory(label){
  const value=await page.evaluate(()=>({url:location.origin+location.pathname,title:document.title,
   visible_text:(document.body?.innerText||'').slice(0,30000),
   links:[...document.querySelectorAll('a[href]')].filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0;}).slice(0,150).map(e=>{let url=null;try{const u=new URL(e.getAttribute('href'),document.baseURI);url=u.origin+u.pathname;}catch{}return {text:(e.innerText||'').trim().slice(0,160),url};})}));
  const file=path.join(output,label+'-inventory.json');await durableWrite(file,{observed_at:new Date().toISOString(),visible_dom:value});
  return {inventory_file:file};
 }
  async function captureSubmittedPage(phase,protection=null){
   // Diagnostic only: no inferred acceptance, form changes or second click.
   const directory=path.join(output,'post-submit-observations',cryptoRandomUUID());
   await fs.mkdir(directory,{recursive:true,mode:0o700});await noSymlinks(directory);
   const screenshot=path.join(directory,'page.png');await page.screenshot({path:screenshot,fullPage:true,timeout:5000});await fs.chmod(screenshot,0o600);
   const visible=await page.evaluate(()=>{
    const shown=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';};
    const text=document.body?.innerText||'',items=selector=>[...document.querySelectorAll(selector)].filter(shown);
    const dialogs=items('[role="dialog"],[role="alertdialog"],dialog[open],[aria-modal="true"]'),alerts=items('[role="alert"]'),invalid=items('input:invalid,textarea:invalid,select:invalid');
    return {url:location.origin+location.pathname,title:document.title,visible_text:text.slice(0,30000),truncated:text.length>30000||dialogs.length>20||alerts.length>20||invalid.length>20,
     dialogs:dialogs.slice(0,20).map(e=>({tag:e.tagName,role:e.getAttribute('role'),text:e.innerText.slice(0,1200)})),
     alerts:alerts.slice(0,20).map(e=>({tag:e.tagName,text:e.innerText.slice(0,1200)})),
     invalid_controls:invalid.slice(0,20).map(e=>({tag:e.tagName,name:e.getAttribute('name'),placeholder:e.getAttribute('placeholder')}))};
   });
   const file=path.join(directory,'observation.json');
   await durableWrite(file,{version:'replay-part-post-submit-page-observation-v1',...binding,phase,observed_at:new Date().toISOString(),
    click_state:submitClickState,original_page_preserved:true,additional_submit_clicks:0,public_submission_confirmed:false,
    human_review:false,visible_dom:visible,protection,screenshot:await fileEvidence(screenshot)},{exclusive:true});
   return fileEvidence(file);
  }
  async function observeSubmittedPage(){
   await tryCaptureSubmittedPage('click_returned');const end=Date.now()+postSubmitObservationMs;
   do{await guard();if(Date.now()>=end)break;await page.waitForTimeout(Math.min(200,end-Date.now()));}while(true);
   await tryCaptureSubmittedPage('passive_observation_finished');
  }
  async function tryCaptureSubmittedPage(phase,protection=null){
   try{return await captureSubmittedPage(phase,protection);}catch(error){
    // Optional diagnostics must not mask a real challenge or prevent handoff.
    const kind=/^[A-Za-z][A-Za-z0-9_]{0,50}$/.test(error?.name||'')?error.name:'Error';
    await durableWrite(path.join(output,'post-submit-diagnostic-errors',cryptoRandomUUID()+'.json'),
     {version:'replay-part-post-submit-diagnostic-error-v1',...binding,phase,error_type:kind,
      observed_at:new Date().toISOString(),public_submission_confirmed:false,human_review:false},{exclusive:true}).catch(()=>{});
    return null;
   }
  }
 const helpers={checkProtection:guard,accountIdentity,checkedFile,write,inventory};
 async function unique(locator,code='REPLAY_BROWSER_CONTROL_AMBIGUOUS'){if(await locator.count()!==1)fail(code);return locator;}
 async function standaloneSurface(){
  if(!route.standalone)return null;
  const end=Date.now()+3000;
  do{
   await guard();const outer=page.locator('.task-list-content-item:visible'),inner=page.locator('.file-list .file-item:visible');
   const outerCount=await outer.count(),innerCount=await inner.count(),formCount=await page.locator('input[placeholder="请输入稿件标题"]:visible').count();
   if(outerCount>1||innerCount>1||formCount>1)fail('REPLAY_PART_MULTIPLE_SUBMISSIONS_OR_FILES');
   if(innerCount===1&&formCount===1){
    const title=inner.locator('.title-text:visible');if(await title.count()!==1)fail('REPLAY_PART_NATIVE_FILE_AMBIGUOUS');
    const name=(await title.innerText()).trim(),allowed=[path.basename(spec.file),path.basename(spec.file).replace(/\.mp4$/i,''),spec.metadata.title];
    if(!allowed.includes(name)&&!selectedTaskTitleMatches(name,[spec.metadata.title]))fail('REPLAY_PART_NATIVE_FILE_CHANGED');let selectedTitle=null;
    if(outerCount===1){const selected=page.locator('.task.task-selected:visible');if(await selected.count()!==1)fail('REPLAY_PART_SELECTED_TASK_AMBIGUOUS');const label=selected.locator('.task-title-text:visible');if(await label.count()!==1)fail('REPLAY_PART_SELECTED_TASK_AMBIGUOUS');selectedTitle=(await label.innerText()).trim();if(!selectedTaskTitleMatches(selectedTitle,allowed))fail('REPLAY_PART_SELECTED_TASK_CHANGED');}
    const value={surface_mode:outerCount===0?'single_editor_no_batch_sidebar':'single_selected_task_sidebar',actual_outer_count:outerCount,actual_inner_count:innerCount,metadata_form_count:formCount,native_filename:name,selected_task_title:selectedTitle};
    await write('native-single-surface.json',{version:'replay-part-single-surface-v5',...binding,...value,observed_at:new Date().toISOString()});return value;
   }
   await page.waitForTimeout(100);
  }while(Date.now()<end);fail('REPLAY_PART_SINGLE_EDITOR_NOT_READY');
 }
 async function completion(){return page.evaluate(()=>{
  const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';};
  const statuses=[...document.querySelectorAll('.file-item-content-status-text .success')].filter(visible);
  if(statuses.length===1&&['上传完成','上传成功'].includes((statuses[0].innerText||statuses[0].textContent||'').trim()))return true;
  return [...document.querySelectorAll('body *')].some(e=>visible(e)&&e.children.length===0&&['上传完成','上传成功'].includes((e.innerText||e.textContent||'').trim()));
 });}
 async function editor(){
  await verify();const end=Date.now()+20000;
  do{
   await guard();if(new URL(page.url()).pathname!==EDITOR)fail('REPLAY_BROWSER_EDITOR_NOT_UPLOADED');
   const titles=page.locator('input[placeholder="请输入稿件标题"]:visible');
   if(await titles.count()===1&&await completion()&&await titles.inputValue()===spec.metadata.title){
    await verify();await standaloneSurface();return titles;
   }
   if(Date.now()>=end)break;await page.waitForTimeout(200);
  }while(true);
  fail('REPLAY_BROWSER_EDITOR_NOT_UPLOADED');
 }
 async function openEditorOnce(){
  if(new URL(page.url()).origin===ORIGIN&&new URL(page.url()).pathname===EDITOR&&await page.locator('input[placeholder="请输入稿件标题"]:visible').count()===1){await editor();return;}
  await page.goto(ORIGIN+EDITOR,{waitUntil:'domcontentloaded',timeout:45000});
  const end=Date.now()+20000;
  do{
   if(await page.locator('input[placeholder="请输入稿件标题"]:visible').count()===1){await editor();return;}
   const notice=page.getByText('本地浏览器存在1个未提交的视频',{exact:true}).filter({visible:true});
   if(await notice.count()===1){
    await verify();const keep=await unique(page.getByText('继续编辑',{exact:true}).filter({visible:true}));await keep.click({timeout:5000});
    await page.locator('input[placeholder="请输入稿件标题"]:visible').waitFor({state:'visible',timeout:20000});await editor();return;
   }
   await page.waitForTimeout(200);
  }while(Date.now()<end);
  await page.goto(ORIGIN+'/platform/upload-manager/article',{waitUntil:'domcontentloaded',timeout:45000});await page.waitForTimeout(1500);
  const result=await navigateSavedDraft({page,spec:{...spec,recovery_id:spec.attempt_id},output,control_action:'lookup',helpers});
  if(result.state!=='saved_draft_editor_opened')fail('REPLAY_BROWSER_DRAFT_LOOKUP_REQUIRED');await editor();
 }
 async function openEditor(){
  const entry=new URL(page.url()),entryPath=entry.origin+entry.pathname;
  try{return await openEditorOnce();}
  catch(error){
   // Saving a draft asynchronously navigates the original tab. An editor or
   // manager entry can race that redirect during the UID check; a freshly
   // opened saved editor can also still be hydrating. Only these known native
   // routes, with the exact saved preparation, may recover once before clicking.
   const current=new URL(page.url());
   if(!route.standalone||error?.code!=='REPLAY_BROWSER_EDITOR_NOT_UPLOADED'||
    entry.origin!==ORIGIN||![EDITOR,'/platform/upload-manager/article'].includes(entry.pathname)||current.origin!==ORIGIN||
    ![EDITOR,'/platform/upload-manager/article'].includes(current.pathname))throw error;
   const prepared=await readJSON(path.join(output,'native-preparation.json'));
   if(prepared.version!==route.prepare||prepared.private_draft_saved!==true||
    prepared.source!==spec.source_url||Object.entries(binding).some(([k,v])=>prepared[k]!==v))
    fail('REPLAY_BROWSER_PREPARATION_BINDING_CHANGED');
   await rejectExistingIntent('public-submit');
   const directory=path.join(output,'editor-redirect-recovery',cryptoRandomUUID());
   await fs.mkdir(directory,{recursive:true,mode:0o700});await noSymlinks(directory);
   const trace={version:'replay-part-saved-draft-redirect-recovery-v1',...binding,
    entry_url:entryPath,redirect_url:current.origin+current.pathname,original_error_code:error.code,
    observed_at:new Date().toISOString(),maximum_editor_wait_ms:15000,maximum_draft_navigations:1,
    new_file_selections:0,save_clicks:0,submit_clicks:0,source_verified:false,ready:false,polls:[]};
   try{
    await verify();
    if(current.pathname==='/platform/upload-manager/article'){
     const result=await navigateSavedDraft({page,spec:{...spec,recovery_id:spec.attempt_id},output,
      control_action:'lookup',helpers});
     trace.navigation_state=result.state;trace.draft_navigations=1;
     if(result.state!=='saved_draft_editor_opened')fail('REPLAY_BROWSER_DRAFT_LOOKUP_REQUIRED');
    }else{trace.navigation_state='saved_editor_already_open';trace.draft_navigations=0;}
    const start=Date.now(),end=start+15000;let stable=0;
    do{
     await guard();const actual=new URL(page.url());
     if(actual.origin!==ORIGIN||actual.pathname!==EDITOR)fail('REPLAY_BROWSER_DRAFT_IDENTITY_CHANGED');
     const title=page.locator('input[placeholder="请输入稿件标题"]:visible'),source=page.locator('input[placeholder^="转载视频请注明来源"]:visible');
     const tc=await title.count(),sc=await source.count();
     if(tc>1||sc>1)fail('REPLAY_BROWSER_DRAFT_IDENTITY_CHANGED');
     const titleValue=tc===1?await title.inputValue():null,sourceValue=sc===1?await source.inputValue():null;
     if(titleValue&&titleValue!==spec.metadata.title||sourceValue&&sourceValue!==spec.source_url)
      fail('REPLAY_BROWSER_DRAFT_IDENTITY_CHANGED');
     const complete=await completion(),matches=tc===1&&sc===1&&titleValue===spec.metadata.title&&sourceValue===spec.source_url&&complete;
     stable=matches?stable+1:0;trace.polls.push({elapsed_ms:Date.now()-start,title_count:tc,source_count:sc,
      exact_title: titleValue===spec.metadata.title,exact_source:sourceValue===spec.source_url,
      native_upload_complete:complete,consecutive_matches:stable});
     if(stable>=2){await editor();
      if(await source.count()!==1||await source.inputValue()!==spec.source_url)fail('REPLAY_BROWSER_DRAFT_IDENTITY_CHANGED');
      trace.source_verified=true;trace.ready=true;return;
     }
     const remaining=end-Date.now();if(remaining<=0)fail('REPLAY_BROWSER_EDITOR_NOT_UPLOADED');
     await page.waitForTimeout(Math.min(200,remaining));
    }while(true);
   }catch(recoveryError){trace.error_code=/^[A-Z][A-Z0-9_]{1,100}$/.test(recoveryError?.code||'')?recoveryError.code:'REPLAY_BROWSER_DRAFT_LOOKUP_REQUIRED';throw recoveryError;}
   finally{await durableWrite(path.join(directory,'observation.json'),trace,{exclusive:true});}
  }
 }
 async function metadataReadback(){
  await editor();const title=await page.locator('input[placeholder="请输入稿件标题"]:visible').inputValue();
  const description=await readNativeDescription(await unique(page.locator('.ql-editor:visible')));
  const creation_statement=await (await unique(page.locator('input[placeholder="请选择符合您视频内容的创作声明"]:visible'))).inputValue();
  const source=await (await unique(page.locator('input[placeholder^="转载视频请注明来源"]:visible'))).inputValue();
  const category=(await (await unique(page.locator('.video-human-type .select-controller:visible'))).innerText()).trim();
  const tags=(await page.locator('.label-item-v2-container:visible p.label-item-v2-content').allTextContents()).map(t=>t.trim());
  if(title!==spec.metadata.title||!description.supported||description.text!==spec.metadata.description||creation_statement!=='内容为转载'||source!==spec.source_url||category!==spec.metadata.category_label||tags.length!==spec.metadata.tags.length||new Set(tags).size!==tags.length||spec.metadata.tags.some(t=>!tags.includes(t)))fail('REPLAY_BROWSER_METADATA_READBACK_FAILED');
  return {title,description:description.text,creation_statement,source,category,tags};
 }
 async function checkedCover(){
  const c=spec.cover,d=spec.cover_derivation,file=path.join(root,'media',spec.job_id,'cover-assets-v4/top-bottom-blur-cover.png'),provenance=path.join(root,'media',spec.job_id,'cover-assets-v4/provenance.json');
  if(!c||c.file!==file||!SHA.test(c.sha256||'')||!Number.isInteger(c.bytes)||c.bytes<33||c.bytes>5_000_000||c.source_url!=='https://i.ytimg.com/vi/'+spec.source_id+'/maxresdefault.jpg'||d?.version!=='derived-cover-binding-v2'||d.provenance_file!==provenance||!SHA.test(d.provenance_sha256||''))fail('REPLAY_BROWSER_COVER_BINDING_INVALID');
  await noSymlinks(file);const st=await fs.stat(file);if(!st.isFile()||st.size!==c.bytes)fail('REPLAY_BROWSER_COVER_CHANGED');
  const bytes=await fs.readFile(file);if(createHash('sha256').update(bytes).digest('hex')!==c.sha256||!bytes.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10]))||bytes.readUInt32BE(8)!==13||bytes.toString('ascii',12,16)!=='IHDR'||bytes.readUInt32BE(16)!==1280||bytes.readUInt32BE(20)!==960)fail('REPLAY_BROWSER_COVER_CHANGED');
  const body=await readJSON(provenance),raw=await fs.readFile(provenance);
  if(createHash('sha256').update(raw).digest('hex')!==d.provenance_sha256||canonicalDigest(body)!==canonicalDigest(d.provenance)||body.version!=='lighthouse-derived-cover-v4'||body.policy_version!=='top-bottom-blur-v1'||body.image_edited!==true||body.no_text_regenerated!==true||body.official_original_bytes!==false||
   body.output?.file!==file||body.output?.bytes!==c.bytes||body.output?.sha256!==c.sha256||body.output?.width!==1280||body.output?.height!==960||body.output?.mime_type!=='image/png')fail('REPLAY_BROWSER_COVER_PROVENANCE_INVALID');
  const transform={filter_complex:FILTER,foreground_rect:{x:0,y:120,width:1280,height:720},safe_crop:{x:0,y:0,width:1280,height:960},background:{width:1280,height:960,blur_sigma:35},foreground_fit:'original_size',foreground_padding:'none'};
  if(canonicalDigest(body.transform)!==canonicalDigest(transform)||body.input?.source_url!==c.source_url||body.input?.width!==1280||body.input?.height!==720||!SHA.test(body.input?.sha256||'')||body.input?.bytes>1_000_000||
   body.input?.file!==path.join(root,'media',spec.job_id,'short-video-attempts',spec.attempt_id,'cover-source-official-maxres.jpg'))fail('REPLAY_BROWSER_COVER_PROVENANCE_INVALID');
  await streamCheckedFile(body.input.file,body.input.sha256,body.input.bytes);return c;
 }
 async function reserve(kind,extra={}){const file=path.join(intents,kind+'.json');try{await durableWrite(file,{version:route.intent||'replay-native-one-use-intent-v1',...binding,kind,created_at:new Date().toISOString(),...extra},{exclusive:true});}catch(e){if(e.code==='EEXIST')fail('REPLAY_BROWSER_RECONCILE_REQUIRED');throw e;}return file;}
 async function rejectExistingIntent(kind){try{await fs.lstat(path.join(intents,kind+'.json'));fail('REPLAY_BROWSER_RECONCILE_REQUIRED');}catch(e){if(e.code!=='ENOENT')throw e;}}
 async function upload(){
  await rejectExistingIntent('video-selection');
  await checkedFile();await page.goto(ORIGIN+EDITOR,{waitUntil:'domcontentloaded',timeout:45000});
  const area=page.locator('.bcc-upload .upload-area:visible');await area.waitFor({state:'visible',timeout:30000});await verify();
  if(await area.count()!==1||await page.getByText('继续编辑',{exact:true}).filter({visible:true}).count())fail('REPLAY_BROWSER_EXISTING_DRAFT_LOOKUP_REQUIRED');
  await freshNativeLimits();
  await reserve('video-selection');if(route.standalone)await guard();permittedVideoChooser=true;
  let chooser;try{[chooser]=await Promise.all([page.waitForEvent('filechooser',{timeout:5000}),area.click({timeout:5000})]);}finally{permittedVideoChooser=false;}
  const type=await chooser.element().evaluate(e=>({tag:e.tagName,type:e.type,accept:e.getAttribute('accept')||''}));
  if(type.tag!=='INPUT'||type.type!=='file'||!type.accept.toLowerCase().split(',').map(x=>x.trim()).includes('.mp4')||/\.txt|image\//i.test(type.accept))fail('REPLAY_BROWSER_VIDEO_CHOOSER_INVALID');
  await checkedFile();if(route.standalone)await guard();videoSelectionAttempted=true;await chooser.setFiles(spec.file,{timeout:30000});
  const nativeFiles=route.highest?await chooser.element().evaluate(e=>[...e.files||[]].map(f=>({name:f.name,bytes:f.size}))).catch(()=>[]):[];
  if(route.highest&&(nativeFiles.length>1||nativeFiles.length===1&&(nativeFiles[0].name!==path.basename(spec.file)||nativeFiles[0].bytes!==spec.file_bytes)))fail('REPLAY_HIGHEST_NATIVE_FILE_SELECTION_MISMATCH');
  const until=Date.now()+uploadTimeoutMs;
  while(!await completion()){
   await guard();if(Date.now()>until)fail('REPLAY_BROWSER_UPLOAD_UNKNOWN');await page.waitForTimeout(500);
  }
  await verify();const nativeSurface=await standaloneSurface(),streamed=await checkedFile();
  const title=await unique(page.locator('input[placeholder="请输入稿件标题"]:visible'));
  let uploadCoverage=null;
  if(route.highest){
   const filename=path.basename(spec.file),nativeTitle=await title.inputValue();let observed=null;
   if(nativeFiles.length===1)observed={text:nativeFiles[0].name,kind:'file_input',observed:true};
   else if([filename,filename.replace(/\.mp4$/i,'')].includes(nativeTitle))observed={text:nativeTitle,kind:'native_title_input_after_upload',observed:true};
   else if(await page.getByText(filename,{exact:true}).filter({visible:true}).count()===1)observed={text:filename,kind:'visible_filename',observed:true};
   if(!observed)fail('REPLAY_HIGHEST_NATIVE_FILENAME_UNVERIFIED');
   const coverage={version:route.uploadCoverage||'replay-highest-native-upload-coverage-v2',...binding,file:spec.file,selected_filename:filename,native_filename:observed,
    native_upload_complete:true,whole_file_streamed:streamed.whole_file_streamed,streamed_bytes:streamed.streamed_bytes,streamed_sha256:streamed.streamed_sha256,
    ...(route.standalone?{local_file_fingerprint:Object.fromEntries(['size','mtimeMs','ctimeMs','ino','dev','mode'].map(k=>[k,streamed[k]]))}:{}),
    observed_at:new Date().toISOString(),server_file_hash_verified:false,human_review:false};
   await write('upload-coverage.json',coverage);uploadCoverage=await fileEvidence(path.join(output,'upload-coverage.json'));
  }
  if(route.standalone)await guard();await title.fill(spec.metadata.title);const description=await unique(page.locator('.ql-editor:visible'));
  if(!await description.evaluate(e=>e.getAttribute('contenteditable')==='true'))fail('REPLAY_BROWSER_DESCRIPTION_CONTROL_INVALID');
  if(route.standalone)await guard();await description.fill(spec.metadata.description);if(route.standalone)await guard();await description.blur();await metadataTitleDescription();
  uploadReference=canonicalDigest({...binding,local_causal_intent:await fileEvidence(path.join(intents,'video-selection.json')),native_upload_complete:true,...(uploadCoverage?{upload_coverage:uploadCoverage}:{})});
  const observation={version:route.upload||'short-video-browser-upload-v1',...(route.standalone?binding:{}),attempt_id:spec.attempt_id,site_uid:spec.target_site_uid,artifact_sha256:spec.artifact_sha256,file_bytes:spec.file_bytes,upload_reference_sha256:uploadReference,same_attempt_observed:true,server_file_hash_verified:false,
   ...(uploadCoverage?{upload_coverage:uploadCoverage}:{}),...(route.standalone?{native_surface:nativeSurface}:{})};
  await write('upload-observation.json',observation);await inventory('uploaded');return {state:'uploaded',upload_observation:observation};
 }
 async function metadataTitleDescription(){
  const title=await (await unique(page.locator('input[placeholder="请输入稿件标题"]:visible'))).inputValue();
  const desc=await readNativeDescription(await unique(page.locator('.ql-editor:visible')));
  if(title!==spec.metadata.title||!desc.supported||desc.text!==spec.metadata.description)fail('REPLAY_BROWSER_DRAFT_IDENTITY_CHANGED');
 }
 async function ensureMetadataReadback(){
  try{return {native:await metadataReadback(),repaired:false};}
  catch(error){
   // Saving and reopening a draft can clear Bilibili's declaration/source
   // controls while retaining the rest of the form. Repair only those text
   // controls and never select or replace the uploaded video.
   if(error?.code!=='REPLAY_BROWSER_METADATA_READBACK_FAILED')throw error;
   const filled=await fillObservedMetadata({page,spec:{...spec,recovery_id:spec.attempt_id},output,control_action:'fill_metadata',helpers});
   if(filled.text_metadata_verified!==true)fail('REPLAY_BROWSER_METADATA_PREPARATION_FAILED');
   await write('metadata-reopen-repair.json',{version:'replay-part-metadata-reopen-repair-v5',...binding,
    cover_sha256:spec.cover.sha256,observed_at:new Date().toISOString(),metadata_repaired_on_reopen:true,
    video_reselected:false,cover_reselected:false,submit_clicked:false,human_review:false});
   return {native:await metadataReadback(),repaired:true};
  }
 }
 async function prepare(){
  await highestSourceCheck();await openEditor();await checkedFile();await checkedCover();
  try{
   const recorded=await readJSON(path.join(output,'native-preparation.json'));
   if(recorded.version!==route.prepare||Object.entries(binding).some(([key,value])=>recorded[key]!==value)||recorded.cover_sha256!==spec.cover.sha256||recorded.cover_provenance_sha256!==spec.cover_derivation.provenance_sha256||recorded.private_draft_saved!==true)fail('REPLAY_BROWSER_PREPARATION_BINDING_CHANGED');
   for(const [key,name] of [['pre_submit_screenshot','pre-submit.png'],['native_observation_file','native-preparation-observation.json']]){
    const expected=recorded[key],file=path.join(output,name);if(expected?.file!==file||!Number.isInteger(expected.bytes)||!SHA.test(expected.sha256||'')||canonicalDigest(await fileEvidence(file))!==canonicalDigest(expected))fail('REPLAY_BROWSER_PREPARATION_BINDING_CHANGED');
   }
   const repairedMetadata=await ensureMetadataReadback(),metadataRepairedOnReopen=repairedMetadata.repaired;
   const current=page.locator('.cover-main .cover-module-main:visible');
   if(await current.count()!==1||await current.getByText('添加封面',{exact:true}).count())fail('REPLAY_BROWSER_COVER_MISSING');
   const archive=path.join(output,'preparation-history',cryptoRandomUUID());await fs.mkdir(archive,{recursive:true,mode:0o700});
   for(const name of ['native-preparation.json','native-preparation-observation.json','pre-submit.png']){await fs.copyFile(path.join(output,name),path.join(archive,name));await fs.chmod(path.join(archive,name),0o600);await fileEvidence(path.join(archive,name));}
   const observed=await metadataReadback(),updated=await snapshotPreparation(observed);
   const refreshed={...updated,version:route.prepare,private_draft_saved:true};await write('native-preparation.json',refreshed);
   return {state:'prepared',native_preparation:refreshed,recovered_without_modification:!metadataRepairedOnReopen,
    metadata_repaired_on_reopen:metadataRepairedOnReopen};
  }catch(e){if(e.code!=='ENOENT')throw e;}
  const filled=await fillObservedMetadata({page,spec:{...spec,recovery_id:spec.attempt_id},output,control_action:'fill_metadata',helpers});
  if(filled.text_metadata_verified!==true)fail('REPLAY_BROWSER_METADATA_PREPARATION_FAILED');
  const native=await metadataReadback();
  const cover=await unique(page.locator('.cover-main .cover-module-main:visible'));await verify();await cover.click();
  const coverEditor=page.locator('.cover-editor:visible');await coverEditor.waitFor({state:'visible',timeout:10000});
  const panel=await unique(coverEditor.locator('.cover-upload.cover-editor-panel-select-item:visible'));
  if(!(await panel.innerText()).includes('上传封面'))fail('REPLAY_BROWSER_COVER_CHOOSER_INVALID');
  await checkedCover();let imageChooser;expectedImageChooser=true;
  try{[imageChooser]=await Promise.all([page.waitForEvent('filechooser',{timeout:5000}),panel.click({timeout:5000})]);}
  finally{expectedImageChooser=false;}
  // This is the observed native image panel. Never select a video input here.
  if(!await imageChooser.element().evaluate(e=>{const a=(e.accept||'').toLowerCase();return e.type==='file'&&(/image\//.test(a)||/\.(png|jpg|jpeg)/.test(a))&&!/video\/|\.mp4|\.txt/.test(a);}))fail('REPLAY_BROWSER_COVER_CHOOSER_INVALID');
  if(route.standalone)await guard();await imageChooser.setFiles(spec.cover.file,{timeout:10000});
  const complete=coverEditor.locator('.cover-editor-button .button:visible').filter({hasText:/^\s*完成\s*$/});await unique(complete);
  await page.waitForTimeout(500);await verify();await reserve('cover-completion',{cover_sha256:spec.cover.sha256});if(route.standalone)await guard();await complete.click({timeout:5000});
  await finishCoverOrExactSync(coverEditor);
  if(await cover.getByText('添加封面',{exact:true}).count()||!await cover.evaluate(e=>Boolean(e.querySelector('img,canvas')||[e,...e.querySelectorAll('*')].some(n=>getComputedStyle(n).backgroundImage!=='none'))))fail('REPLAY_BROWSER_COVER_APPLY_UNKNOWN');
  await verify();await metadataReadback();await checkedCover();
  const preparedSnapshot=await snapshotPreparation(native);
  const {pre_submit_screenshot,native_observation_file,...observation}=preparedSnapshot;
  const draft=await unique(page.locator('span.submit-draft:visible').filter({hasText:/^\s*存草稿\s*$/}));
  if(await page.getByText('已存入草稿箱',{exact:true}).filter({visible:true}).count())fail('REPLAY_BROWSER_STALE_DRAFT_TOAST');
  await verify();await reserve('private-draft-save',{cover_sha256:spec.cover.sha256});if(route.standalone)await guard();await draft.click({timeout:5000});
  const saved=page.getByText('已存入草稿箱',{exact:true}).filter({visible:true});await saved.waitFor({state:'visible',timeout:15000});
  const receipt={...observation,version:route.prepare,pre_submit_screenshot,native_observation_file,private_draft_saved:true};
  await write('native-preparation.json',receipt);return {state:'prepared',native_preparation:receipt};
 }
 async function snapshotPreparation(native){
  const screenshot=path.join(output,'pre-submit.png');await page.screenshot({path:screenshot,fullPage:true,timeout:20000});
  const upload=await readJSON(path.join(output,'upload-observation.json'));
  uploadReference=upload.upload_reference_sha256;
  if(upload.attempt_id!==spec.attempt_id||upload.artifact_sha256!==spec.artifact_sha256||upload.site_uid!==spec.target_site_uid||!SHA.test(uploadReference||''))fail('REPLAY_BROWSER_UPLOAD_RECEIPT_REQUIRED');
  const observed_at=new Date().toISOString(),observation={version:route.form,...binding,...native,...(route.standalone?{native_surface:await standaloneSurface()}:{}),cover_sha256:spec.cover.sha256,cover_provenance_sha256:spec.cover_derivation.provenance_sha256,upload_reference_sha256:uploadReference,
   metadata_verified:true,cover_applied:true,server_file_hash_verified:false,human_review:false,per_artifact_owner_acceptance:false,observed_at};
  const file=path.join(output,'native-preparation-observation.json');await durableWrite(file,observation);
  return {...observation,pre_submit_screenshot:await fileEvidence(screenshot),native_observation_file:await fileEvidence(file)};
 }
 async function finishCoverOrExactSync(coverEditor){
  const end=Date.now()+10000,heading='检测到个人空间封面（16:9）未修改',question='未修改可能导致不同场景封面展示不一致。是否将「4:3封面」的改动同步到「16:9封面」？下方是同步后的效果预览。';
  let synced=false,finalApplied=false;
  do{
   await guard();if(!await coverEditor.isVisible())return;
   const body=await page.locator('body').innerText();
   if(body.includes(heading)&&body.includes(question)){
    if(synced)fail('REPLAY_BROWSER_COVER_SYNC_UNKNOWN');
    const buttons=page.locator('.modal-footer button:visible'),yes=buttons.filter({hasText:/^\s*确认同步\s*$/}),no=buttons.filter({hasText:/^\s*不同步，手动编辑\s*$/});
    if(await yes.count()!==1||await no.count()!==1||await buttons.count()!==2)fail('REPLAY_BROWSER_COVER_SYNC_AMBIGUOUS');
    await verify();await checkedCover();await reserve('cover-sync',{cover_sha256:spec.cover.sha256,cover_provenance_sha256:spec.cover_derivation.provenance_sha256});await guard();
    synced=true;await yes.click({timeout:5000});
   }else if(synced&&!finalApplied){
    // The observed confirmation synchronizes the two crops, then leaves the
    // native editor open. Its final apply is a separate persistent boundary.
    const final=await unique(coverEditor.locator('.cover-editor-button .button:visible').filter({hasText:/^\s*完成\s*$/}));
    await verify();await checkedCover();await metadataReadback();
    await reserve('cover-completion-after-sync',{cover_sha256:spec.cover.sha256,cover_provenance_sha256:spec.cover_derivation.provenance_sha256});await guard();
    finalApplied=true;await final.click({timeout:5000});
   }
   await page.waitForTimeout(100);
  }while(Date.now()<end);fail('REPLAY_BROWSER_COVER_APPLY_UNKNOWN');
 }
 async function resultObservation({known=null,sameSubmit=false,publicLookup=false}={}){
  if(route.standalone){
    // Never replace the page that may still hold asynchronous submit UI.
    const lookupPage=sameSubmit?await context.newPage():page;let evidence,retainedLookupPage=false;
    const lookupProtection=sameSubmit?async event=>{if(event.page===lookupPage)retainedLookupPage=true;if(onProtection)await onProtection(event);}:onProtection;
    try{evidence=await observeLaterReplayResult({page:lookupPage,context,spec,root,known_external_id:known,onProtection:lookupProtection});}
    finally{if(sameSubmit&&!retainedLookupPage)await lookupPage.close();}
   if(evidence.state==='awaiting_moderation')return evidence; // Unverified native processing; never a completed public result.
   if(evidence.state!=='later_native_evidence_observed')return {state:'unknown',candidate_external_id:evidence.candidate_external_id||null,reconcile_required:true,native_result_evidence:evidence};
   const raw=await readJSON(evidence.reconciliation_observation.file),publicBody=await readJSON(raw.public_observation.file);
   const observation={version:route.result,...binding,external_id:evidence.candidate_external_id,url:'https://www.bilibili.com/video/'+evidence.candidate_external_id,state:'completed',
    public_accessible:true,public_lookup_observed_at:publicBody.observed_at,native_result_evidence:evidence.reconciliation_observation,
    origin:'later_native_lookup_reconciled_v5',same_submit_attempt_observed:false,human_review:false,server_file_hash_verified:false};
   const history=path.join(output,'result-history');await fs.mkdir(history,{recursive:true,mode:0o700});await noSymlinks(history);
   const file=path.join(history,cryptoRandomUUID()+'.json');await durableWrite(file,observation,{exclusive:true});
   return {state:'completed',result_observation:observation,result_reference:await fileEvidence(file)};
  }
  await verify();const links=page.locator('a[href]:visible'),candidates=[];
  for(let i=0;i<await links.count();i++){const l=links.nth(i);let u;try{u=new URL(await l.getAttribute('href'),page.url());}catch{continue;}const match=/^\/video\/(BV[A-Za-z0-9]{10})\/?$/.exec(u.pathname);if(u.origin==='https://www.bilibili.com'&&match&&!u.username&&!u.password)candidates.push({id:match[1],link:l});}
  let ids=[...new Set(candidates.map(c=>c.id))];if(route.highest&&known&&publicLookup)ids=[known];if(ids.length!==1||known&&ids[0]!==known)return {state:'unknown'};
  // A result is correlated only with this exact submitted native title/source.
  const body=await page.locator('body').innerText(),managerMatched=body.includes(spec.metadata.title)&&body.includes(spec.source_url);
  if(!managerMatched&&!(route.highest&&known&&publicLookup))return {state:'unknown'};
  let state=managerMatched?(body.includes('审核中')?'awaiting_moderation':body.includes('审核不通过')?'failed':null):null;
  let accessible=false,player=null;
  if(publicLookup&&known){
   const publicPage=await context.newPage();try{
    await publicPage.goto('https://www.bilibili.com/video/'+known,{waitUntil:'domcontentloaded',timeout:45000});
    const h=publicPage.locator('h1:visible'),account=publicPage.locator('a[href^="//space.bilibili.com/"],a[href^="https://space.bilibili.com/"]');
    let owner=false;for(let i=0;i<await account.count();i++){const u=new URL(await account.nth(i).getAttribute('href'),publicPage.url());if(u.hostname==='space.bilibili.com'&&u.pathname.replace(/\/$/,'')==='/'+spec.target_site_uid)owner=true;}
    const text=await publicPage.locator('body').innerText();accessible=await h.count()===1&&(await h.innerText()).trim()===spec.metadata.title&&owner&&text.includes(spec.source_url)&&!/(视频不见了|稿件不存在|审核中)/.test(text);
    const protection=await inspectVideoProtection(publicPage),ev=protection.evidence;
    // The official public page may carry the same two document-only risk
    // classes. Exclude only that exact BODY after title/owner/source identity
    // is observed. Any actual child control, dialog or challenge iframe stops.
    const bodyOnly=accessible&&new URL(publicPage.url()).origin==='https://www.bilibili.com'&&new URL(publicPage.url()).pathname==='/video/'+known&&
     protection.error_code==='BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED'&&ev?.visible_dialogs?.length===0&&ev?.challenge_frames?.length===0&&!ev?.top_level_authentication&&
     ev?.visible_challenge_controls?.length===1&&ev.visible_challenge_controls[0].tag==='BODY'&&[...ev.visible_challenge_controls[0].classes].sort().join(',')==='risk-captcha-adapt,risk-captcha-adapt-pc';
    if(protection.blocked&&!bodyOnly)accessible=false;
    if(accessible&&route.highest){
     try{
      await publicPage.waitForFunction(()=>[...document.querySelectorAll('video')].some(v=>Number.isFinite(v.duration)&&v.duration>0&&v.readyState>=2&&!v.error&&v.seekable.length>0&&v.seekable.end(v.seekable.length-1)>0),null,{timeout:20000});
      player=await publicPage.evaluate(()=>{
       const visible=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0;},videos=[...document.querySelectorAll('video')].filter(visible);
       if(videos.length!==1)return null;const v=videos[0],ranges=[];for(let i=0;i<v.seekable.length;i++)ranges.push({start:v.seekable.start(i),end:v.seekable.end(i)});
       return {duration_seconds:v.duration,current_time:v.currentTime,ready_state:v.readyState,seekable_ranges:ranges,
        observed_at:new Date().toISOString(),error_code:v.error?.code||null,near_end_playability_observed:v.readyState>=2&&v.currentTime>=Math.max(0,v.duration-5)&&v.currentTime>0,
        near_end_seek_performed:false};
      });
      accessible=Boolean(player&&Number.isFinite(player.duration_seconds)&&Math.abs(player.duration_seconds-spec.duration_seconds)<=2&&
       player.ready_state>=2&&player.error_code===null&&player.seekable_ranges.length&&player.seekable_ranges.some(r=>r.end>r.start));
     }catch{accessible=false;}
    }
    if(route.highest)await write('public-player-observation.json',{version:'replay-highest-public-player-observation-v2',external_id:known,site_uid:spec.target_site_uid,observed_at:new Date().toISOString(),public_identity_and_player_verified:accessible,player,server_file_hash_verified:false});
    if(accessible)state='completed';
   }finally{await publicPage.close();}
  }
  if(!state)return {state:'unknown',external_id:ids[0]};
  const value={version:route.result,attempt_id:spec.attempt_id,site_uid:spec.target_site_uid,source_url:spec.source_url,metadata_sha256:spec.metadata_sha256,external_id:ids[0],url:'https://www.bilibili.com/video/'+ids[0],state,
   ...(sameSubmit?{same_submit_attempt_observed:true}:{}),...(state==='failed'?{error_code:'BILIBILI_MODERATION_REJECTED'}:{}),...(accessible?{public_accessible:true,public_lookup_observed_at:new Date().toISOString()}: {})};
  if(accessible&&route.highest){
   const upload=await readJSON(path.join(output,'upload-observation.json'));
   if(!upload.upload_coverage||upload.attempt_id!==spec.attempt_id||upload.artifact_sha256!==spec.artifact_sha256)fail('REPLAY_HIGHEST_UPLOAD_COVERAGE_REQUIRED');
   const coverage={version:'replay-highest-public-coverage-v2',...binding,external_id:ids[0],url:value.url,
    source_duration_seconds:spec.duration_seconds,upload_coverage_ref:upload.upload_coverage,public_player:player,
    source_to_merged_packet_coverage_verified:true,server_file_hash_verified:false,human_review:false};
   await write('public-coverage.json',coverage);value.cleanup_coverage=await fileEvidence(path.join(output,'public-coverage.json'));
  }
  return {state,result_observation:value};
 }
 async function submit(reservation){
  if(reservation?.version==='replay-part-draft-recovery-reservation-v1')return recoverSavedDraft(reservation);
  await rejectExistingIntent('public-submit');
  await highestSourceCheck();
  if(!reservation||reservation.version!==route.reservation||reservation.job_id!==spec.job_id||reservation.attempt_id!==spec.attempt_id||reservation.binding_sha256!==spec.binding_sha256||reservation.metadata_sha256!==spec.metadata_sha256||reservation.can_click_submit!==true||reservation.state!=='dispatching'||!SHA.test(reservation.idempotency_key||'')||!SHA.test(reservation.machine_evidence_sha256||'')||!Number.isFinite(reservation.expires_at)||Date.now()/1000>reservation.expires_at||reservation.expires_at>Date.now()/1000+900)fail('REPLAY_BROWSER_RESERVATION_REQUIRED');
  const prepared=await readJSON(path.join(output,'native-preparation.json'));if(prepared.binding_sha256!==spec.binding_sha256||prepared.cover_sha256!==spec.cover.sha256||prepared.private_draft_saved!==true)fail('REPLAY_BROWSER_PREPARATION_REQUIRED');
  if(route.standalone)validatePartReservation(reservation,spec,binding,prepared);
  await openEditor();await ensureMetadataReadback();if(route.standalone)await checkedUploadedFingerprint(prepared.upload_reference_sha256);else await checkedFile();await checkedCover();
  const cover=page.locator('.cover-main .cover-module-main:visible');if(await cover.count()!==1||await cover.getByText('添加封面',{exact:true}).count())fail('REPLAY_BROWSER_COVER_MISSING');
  await verify();const button=await unique(page.locator('span.submit-add:visible').filter({hasText:/^\s*立即投稿\s*$/}));
  await page.screenshot({path:path.join(output,'immediately-before-submit.png'),fullPage:true,timeout:20000});
   await highestSourceCheck();await reserve('public-submit',{reservation});await verify();if(route.standalone){await checkedUploadedFingerprint(prepared.upload_reference_sha256);await guard();validatePartReservation(reservation,spec,binding,prepared);observingSubmittedPage=true;submittedPageProtectionObserved=false;submitClickState='issued_not_confirmed';}
   return clickAndObserve(button);
 }
 async function clickAndObserve(button){
   try{
    await button.click({timeout:5000});if(route.standalone){submitClickState='returned';await observeSubmittedPage();}
    const end=Date.now()+resultTimeoutMs;let observed;
    do{
     try{observed=await resultObservation({sameSubmit:true});}
     finally{if(route.standalone){await tryCaptureSubmittedPage('after_result_lookup');await guard();}}
     if(observed.state!=='unknown'){await write('public-result.json',observed);return observed;}await page.waitForTimeout(200);
    }while(Date.now()<end);
    await write('public-result.json',{state:'unknown',submitted:null,reconcile_required:true});return {state:'unknown',submitted:null,reconcile_required:true};
   }catch(error){
    if(route.standalone){await tryCaptureSubmittedPage('operation_failed');if(!submittedPageProtectionObserved)await guard();}
    throw error;
   }finally{observingSubmittedPage=false;}
 }
 async function recoveryPermission(ticket){
  const now=Date.now()/1000,ref=ticket?.grant_reference,expected=path.join(root,'media',spec.job_id,'draft-recovery-v1','grant.json');
  const sameBase=value=>value&&Object.entries(binding).every(([k,v])=>value[k]===v);
  if(!route.standalone||!sameBase(ticket)||ticket.version!=='replay-part-draft-recovery-reservation-v1'||!UUID.test(ticket.ticket_id||'')||
   ticket.idempotency_key!==spec.part_idempotency_key||ticket.state!=='recovery_dispatching'||ticket.can_click_submit!==true||
   typeof ticket.issued_at!=='number'||typeof ticket.expires_at!=='number'||!Number.isFinite(ticket.issued_at)||!Number.isFinite(ticket.expires_at)||
   ticket.issued_at>now||ticket.expires_at<=now||ticket.expires_at-ticket.issued_at>300||!SHA.test(ticket.machine_evidence_sha256||'')||
   !ref||ref.file!==expected||!Number.isSafeInteger(ref.bytes)||ref.bytes<1||ref.bytes>100000||!SHA.test(ref.sha256||''))fail('REPLAY_PART_DRAFT_RECOVERY_PERMISSION_REQUIRED');
  await noSymlinks(expected);const handle=await fs.open(expected,fileConstants.O_RDONLY|(fileConstants.O_NOFOLLOW||0));let grant;
  try{
   const stat=await handle.stat();
   if(!stat.isFile()||stat.uid!==0||(stat.mode&0o777)!==0o444||stat.nlink!==1||stat.size!==ref.bytes)fail('REPLAY_PART_DRAFT_RECOVERY_ROOT_GRANT_REQUIRED');
   const raw=await handle.readFile(),after=await handle.stat(),current=await fs.lstat(expected);
   if(raw.length!==ref.bytes||createHash('sha256').update(raw).digest('hex')!==ref.sha256||
    ['dev','ino','size','mtimeMs','ctimeMs','uid','mode','nlink'].some(k=>stat[k]!==after[k]||stat[k]!==current[k]))fail('REPLAY_PART_DRAFT_RECOVERY_ROOT_GRANT_REQUIRED');
   grant=JSON.parse(raw);
  }finally{await handle.close();}
  const keys=['version','grant_id','binding','idempotency_key','original_submit_intent_sha256','draft_id','draft_url','upload_reference_sha256',
   'read_only_draft_reference','issued_at','expires_at','owner_response','owner_response_reference','max_clicks','upload_allowed','save_allowed','human_review','publication_authorized'];
  if(Object.keys(grant).sort().join()!==keys.sort().join()||grant.version!=='replay-part-draft-recovery-grant-v1'||!UUID.test(grant.grant_id||'')||
   canonicalDigest(grant.binding)!==canonicalDigest(binding)||grant.idempotency_key!==ticket.idempotency_key||
   grant.original_submit_intent_sha256!==ticket.original_submit_intent_sha256||!SHA.test(grant.original_submit_intent_sha256||'')||
   grant.draft_id!==ticket.draft_id||grant.draft_url!==ticket.draft_url||replayDraftLink(grant.draft_url)?.draft_id!==grant.draft_id||
   grant.upload_reference_sha256!==ticket.upload_reference_sha256||!SHA.test(grant.upload_reference_sha256||'')||
   typeof grant.issued_at!=='number'||typeof grant.expires_at!=='number'||!Number.isFinite(grant.issued_at)||!Number.isFinite(grant.expires_at)||
   grant.issued_at>now||grant.expires_at<=now||grant.expires_at-grant.issued_at>300||ticket.issued_at<grant.issued_at||ticket.expires_at>grant.expires_at||
   grant.max_clicks!==1||grant.upload_allowed!==false||grant.save_allowed!==false||grant.human_review!==false||grant.publication_authorized!==true||
   typeof grant.owner_response!=='string'||!grant.owner_response.trim()||grant.owner_response.length>2000||
   typeof grant.owner_response_reference!=='string'||!grant.owner_response_reference.trim()||grant.owner_response_reference.length>2000)fail('REPLAY_PART_DRAFT_RECOVERY_PERMISSION_REQUIRED');
  const original=path.join(intents,'public-submit.json'),originalRef=await fileEvidence(original),intent=await readJSON(original);
  if(originalRef.sha256!==grant.original_submit_intent_sha256||intent.version!==route.intent||intent.kind!=='public-submit'||!sameBase(intent)||
   intent.reservation?.idempotency_key!==ticket.idempotency_key||intent.reservation.machine_evidence_sha256!==ticket.machine_evidence_sha256||
   intent.reservation.upload_reference_sha256!==ticket.upload_reference_sha256)fail('REPLAY_PART_DRAFT_RECOVERY_ORIGINAL_INTENT_CHANGED');
  const evidence=grant.read_only_draft_reference;
  if(!evidence||!Number.isSafeInteger(evidence.bytes)||evidence.bytes<1||evidence.bytes>100000||!SHA.test(evidence.sha256||'')||
   path.dirname(path.dirname(evidence.file||''))!==path.join(output,'later-lookups-v2')||path.basename(evidence.file)!=='draft-observation.json'||
   !UUID.test(path.basename(path.dirname(evidence.file))))fail('REPLAY_PART_DRAFT_RECOVERY_DISCOVERY_REQUIRED');
  if(canonicalDigest(await fileEvidence(evidence.file))!==canonicalDigest(evidence))fail('REPLAY_PART_DRAFT_RECOVERY_DISCOVERY_REQUIRED');
  const body=await readJSON(evidence.file);
  if(!sameBase(body)||body.version!=='replay-part-native-draft-observation-v5'||body.state!=='draft_found'||body.draft_id!==grant.draft_id||body.draft_url!==grant.draft_url||
   body.publication_authorized!==false||body.human_review!==false||body.public_accessible!==false||body.read_only!==true)fail('REPLAY_PART_DRAFT_RECOVERY_DISCOVERY_REQUIRED');
  return {grant,originalRef};
 }
 async function recoverSavedDraft(ticket){
  await rejectExistingIntent('public-submit-draft-recovery-v1');
  const {grant,originalRef}=await recoveryPermission(ticket);await highestSourceCheck();await checkedCover();await checkedUploadedFingerprint(ticket.upload_reference_sha256);
  const prepared=await readJSON(path.join(output,'native-preparation.json'));
  if(Object.entries(binding).some(([k,v])=>prepared[k]!==v)||prepared.version!==route.prepare||prepared.private_draft_saved!==true||
   prepared.metadata_verified!==true||prepared.cover_applied!==true||prepared.cover_sha256!==spec.cover.sha256||
   prepared.cover_provenance_sha256!==spec.cover_derivation.provenance_sha256||prepared.upload_reference_sha256!==ticket.upload_reference_sha256)fail('REPLAY_PART_DRAFT_RECOVERY_PREPARATION_REQUIRED');
  const evidence=await observeLaterReplayResult({page,context,spec,root,onProtection});
  if(evidence.state!=='draft_found'||evidence.draft_observation.draft_id!==grant.draft_id||evidence.draft_observation.draft_url!==grant.draft_url)fail('REPLAY_PART_DRAFT_RECOVERY_NATIVE_DRAFT_REQUIRED');
  await metadataReadback();await checkedCover();await checkedUploadedFingerprint(ticket.upload_reference_sha256);await verify();
  const cover=page.locator('.cover-main .cover-module-main:visible');
  if(await cover.count()!==1||await cover.getByText('添加封面',{exact:true}).count())fail('REPLAY_BROWSER_COVER_MISSING');
  const button=await unique(page.locator('span.submit-add:visible').filter({hasText:/^\s*立即投稿\s*$/}));
  await recoveryPermission(ticket);
  await reserve('public-submit-draft-recovery-v1',{version:'replay-part-draft-recovery-one-use-v1',original_submit_intent:originalRef,
   grant_reference:ticket.grant_reference,fresh_draft_reference:evidence.draft_reference,reservation:ticket});
  await recoveryPermission(ticket);await verify();await metadataReadback();await checkedCover();await checkedUploadedFingerprint(ticket.upload_reference_sha256);
  const current=replayDraftLink(page.url());if(!current||current.draft_id!==grant.draft_id||current.draft_url!==grant.draft_url)fail('REPLAY_PART_DRAFT_RECOVERY_NATIVE_DRAFT_REQUIRED');
  await recoveryPermission(ticket);await guard();
  observingSubmittedPage=true;submittedPageProtectionObserved=false;submitClickState='issued_not_confirmed';
  return clickAndObserve(button);
 }
 async function lookup({external_id=null}={}){
  if(external_id!==null&&!BV.test(external_id))fail('REPLAY_BROWSER_BV_INVALID');
  if(route.standalone){const result=await resultObservation({known:external_id,publicLookup:true});await write('lookup-part-result.json',result);return result;}
  await page.goto(ORIGIN+'/platform/upload-manager/article',{waitUntil:'domcontentloaded',timeout:45000});await page.waitForTimeout(1500);await verify();
  const result=await resultObservation({known:external_id,publicLookup:Boolean(external_id)});await inventory('lookup');return result;
 }
 async function perform(action,value={}){
  if(!['upload','prepare','submit','lookup'].includes(action)||busy)fail('REPLAY_BROWSER_OPERATION_INVALID');busy=true;
  try{return await ({upload,prepare,submit:()=>submit(value.reservation),lookup:()=>lookup(value)})[action]();}
  catch(e){const code=typeof e?.code==='string'&&/^[A-Z][A-Z0-9_]{1,100}$/.test(e.code)?e.code:'REPLAY_BROWSER_NATIVE_OPERATION_FAILED';await inventory('error-'+action).catch(()=>{});const evidence={action,error_code:code,publication_result_unknown:action==='submit',video_selection_attempted_in_session:videoSelectionAttempted};await write('error-'+cryptoRandomUUID()+'.json',evidence);await write('last-error.json',evidence);throw Object.assign(new Error(code,{cause:e}),{code});}
  finally{busy=false;}
 }
 return Object.freeze({perform,output});
}
