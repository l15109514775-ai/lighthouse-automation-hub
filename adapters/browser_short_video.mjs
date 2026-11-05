// Upload-only native UI bridge. No private API, Cookie/storage read or submit.
import fs from 'node:fs/promises';
import {createReadStream} from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {pathToFileURL} from 'node:url';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {inspectVideoProtection} from './bilibili_video_guard.mjs';

process.umask(0o077);
const VERSION='short-video-browser-transport-v1',UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const root=path.resolve(process.env.HUB_ROOT||'/opt/lighthouse-hub');
const expectedUid=process.env.BILIBILI_EXPECTED_SITE_UID;
const channel=process.env.HUB_BROWSER_CHANNEL||'chromium';
const raw=await new Promise((resolve,reject)=>{let text='';process.stdin.setEncoding('utf8');
 process.stdin.on('data',chunk=>{text+=chunk;if(Buffer.byteLength(text)>30000)reject(new Error('SHORT_VIDEO_SPEC_TOO_LARGE'));});
 process.stdin.on('end',()=>resolve(text));process.stdin.on('error',reject);});
const spec=JSON.parse(raw);
const nativeChooser=spec.version==='short-video-browser-transport-v2'&&spec.selection_mode==='native_filechooser_observed_area';
function fail(code){throw Object.assign(new Error(code),{code});}
const sha=value=>crypto.createHash('sha256').update(value).digest('hex');
function failureCategory(error){
 const message=typeof error?.message==='string'?error.message.toLowerCase():'';
 if(/resource temporarily unavailable|pthread_create|thread constructor failed|cannot fork|can't fork/.test(message))return 'PIDS_RESOURCE_UNAVAILABLE';
 if(/out of memory|cannot allocate memory|enomem/.test(message))return 'BROWSER_MEMORY_UNAVAILABLE';
 if(/target page, context or browser has been closed|target closed|browser has been closed|page has been closed/.test(message))return 'TARGET_CLOSED';
 if(error?.name==='TimeoutError')return 'PLAYWRIGHT_TIMEOUT';
 return 'UNCLASSIFIED';
}
if(!(spec.version===VERSION&&spec.selection_mode===undefined||nativeChooser)||![spec.job_id,spec.download_job_id,spec.attempt_id].every(x=>typeof x==='string'&&UUID.test(x))||
 typeof spec.target_site_uid!=='string'||!/^\d{1,30}$/.test(spec.target_site_uid)||spec.target_site_uid!==expectedUid||channel!=='chromium'||
 typeof spec.source_id!=='string'||!/^[A-Za-z0-9_-]{11}$/.test(spec.source_id)||spec.source_url!=='https://www.youtube.com/watch?v='+spec.source_id||
 typeof spec.artifact_sha256!=='string'||!/^[a-f0-9]{64}$/.test(spec.artifact_sha256)||
 typeof spec.metadata_sha256!=='string'||!/^[a-f0-9]{64}$/.test(spec.metadata_sha256)||
 !Number.isSafeInteger(spec.file_bytes)||spec.file_bytes<=0||spec.file_bytes>100_000_000||
 typeof spec.duration_seconds!=='number'||!Number.isFinite(spec.duration_seconds)||spec.duration_seconds<=0||spec.duration_seconds>600)
 fail('SHORT_VIDEO_SPEC_INVALID');
const expectedFile=path.join(root,'media',spec.download_job_id,'youtube-download-v1/original.mp4');
if(spec.file!==expectedFile||await fs.realpath(spec.file)!==expectedFile)fail('SHORT_VIDEO_ARTIFACT_PATH_INVALID');
for(let p=expectedFile;p!==path.dirname(p);p=path.dirname(p))if((await fs.lstat(p)).isSymbolicLink())fail('SHORT_VIDEO_ARTIFACT_PATH_INVALID');
const output=path.join(root,'media',spec.job_id,'short-video-attempts',spec.attempt_id);
await fs.mkdir(output,{recursive:true,mode:0o700});
for(let p=output;p!==path.dirname(p);p=path.dirname(p))if((await fs.lstat(p)).isSymbolicLink())fail('SHORT_VIDEO_ATTEMPT_PATH_INVALID');
const profile=path.join(root,'data/browser-profiles/bilibili-video');
if(await fs.realpath(profile)!==profile||(await fs.stat(profile)).mode&0o077)fail('SHORT_VIDEO_PROFILE_NOT_PRIVATE');
for(let p=profile;p!==path.dirname(p);p=path.dirname(p))if((await fs.lstat(p)).isSymbolicLink())fail('SHORT_VIDEO_PROFILE_NOT_PRIVATE');
async function write(name,value){const p=path.join(output,name);await fs.writeFile(p+'.partial',JSON.stringify(value,null,2),{mode:0o600});await fs.rename(p+'.partial',p);}
async function status(state,extra={}){
 const value={version:spec.version,state,job_id:spec.job_id,attempt_id:spec.attempt_id,updated_at:new Date().toISOString(),
  submitted:submissionObserved,submit_implemented:submitImplementation,...extra};
 await write('status.json',value);process.stdout.write(JSON.stringify({state,output,status_file:path.join(output,'status.json')})+'\n');
}
async function checkedFile(){
 const before=await fs.stat(spec.file);if(!before.isFile()||before.size!==spec.file_bytes)fail('SHORT_VIDEO_ARTIFACT_SIZE_MISMATCH');
 const hash=crypto.createHash('sha256');for await(const chunk of createReadStream(spec.file))hash.update(chunk);
 const after=await fs.stat(spec.file);
 if(after.size!==before.size||after.mtimeMs!==before.mtimeMs||after.ctimeMs!==before.ctimeMs||await fs.realpath(spec.file)!==expectedFile)fail('SHORT_VIDEO_ARTIFACT_CHANGED');
 if(hash.digest('hex')!==spec.artifact_sha256)fail('SHORT_VIDEO_ARTIFACT_HASH_MISMATCH');
}
let context,page,closing=false,selected=false,uploadObserved=false,blocked=false,sequence=0;
let submissionObserved=false,submitImplementation=false;
let browserStage='verify_source_file',accountStage='not_started';
const deadline=Date.now()+30*60*1000;
async function close(){if(closing)return;closing=true;await context?.close();context=null;}
process.on('SIGTERM',()=>{close().finally(()=>process.exit(0));});
process.on('SIGINT',()=>{close().finally(()=>process.exit(0));});
async function inventory(label){
 const name=String(++sequence).padStart(3,'0')+'-'+label;
 const value=await page.evaluate(()=>{
  function visible(el){const r=el.getBoundingClientRect(),s=getComputedStyle(el);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';}
  const cleanUrl=href=>{try{const u=new URL(href,document.baseURI);return u.origin+u.pathname;}catch{return null;}};
  const controls=[...document.querySelectorAll('input,textarea,button,select,[role="button"],[role="radio"],[role="checkbox"],[contenteditable="true"]')]
   .filter(visible).slice(0,180).map(el=>({tag:el.tagName,type:el.type||null,role:el.getAttribute('role'),
    name:el.getAttribute('name'),placeholder:el.getAttribute('placeholder'),aria_label:el.getAttribute('aria-label'),
    classes:[...el.classList].slice(0,10),accept:el.getAttribute('accept'),multiple:el.type==='file'?el.multiple:undefined,
    text:(el.innerText||'').trim().slice(0,300),value:['INPUT','TEXTAREA'].includes(el.tagName)&&['text','number','url','search','textarea'].includes(el.type)?el.value.slice(0,400):undefined,
    files:el.type==='file'?[...el.files].map(f=>({name:f.name,size:f.size})):undefined}));
  return {title:document.title,url:cleanUrl(location.href),controls,
   visible_text:(document.body?.innerText||'').slice(0,24000),
   links:[...document.querySelectorAll('a[href]')].filter(visible).slice(0,80).map(el=>({text:(el.innerText||'').trim().slice(0,160),href:cleanUrl(el.getAttribute('href'))})),
   frames:[...document.querySelectorAll('iframe')].filter(visible).map(el=>({url:cleanUrl(el.getAttribute('src')||'')}))};
 });
 const protection=await inspectVideoProtection(page);await write(name+'-inventory.json',{observed_at:new Date().toISOString(),protection,visible_dom:value});
 await page.screenshot({path:path.join(output,name+'.png'),fullPage:false,timeout:10000}).catch(()=>{});
 return {inventory_file:path.join(output,name+'-inventory.json'),screenshot_file:path.join(output,name+'.png'),protection};
}
async function accountIdentity(){
 accountStage='open_identity_tab';
 const account=await context.newPage();
 try{
  accountStage='navigate_identity_page';
  await account.goto('https://t.bilibili.com/',{waitUntil:'domcontentloaded',timeout:45000});
  accountStage='wait_identity_avatar';
  const avatar=account.locator('a.header-entry-mini');await avatar.waitFor({state:'visible',timeout:30000});
  accountStage='read_identity_avatar';
  if(await avatar.count()!==1)fail('SHORT_VIDEO_ACCOUNT_IDENTITY_AMBIGUOUS');
  const href=new URL(await avatar.getAttribute('href'),'https://t.bilibili.com');
  const actual=href.origin==='https://space.bilibili.com'&&/^\/(\d+)\/?$/.exec(href.pathname)?.[1];
  if(actual!==spec.target_site_uid)fail('TARGET_ACCOUNT_IDENTITY_MISMATCH');
  await write('account-identity.json',{version:'short-video-server-account-observation-v1',site_uid:actual,
   observed_at:new Date().toISOString(),method:'visible_native_avatar_href',same_browser_context:true});
  accountStage='identity_confirmed';return actual;
 }finally{await account.close();}
}
async function checkProtection(){
 const value=await inspectVideoProtection(page);
 if(value.blocked){blocked=true;await write('protection-required.json',{observed_at:new Date().toISOString(),...value});
  await inventory('protection');await status('blocked_by_native_protection',{error_code:value.error_code,selected});return false;}
 return true;
}
async function nativeCompletion(){
 return page.evaluate(filename=>{
  function visible(el){const r=el.getBoundingClientRect(),s=getComputedStyle(el);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';}
  const leaf=[...document.querySelectorAll('body *')].filter(el=>visible(el)&&el.children.length===0);
  const phrases=leaf.map(el=>(el.innerText||el.textContent||'').trim()).filter(t=>t==='上传完成'||t==='上传成功');
  const stem=filename.replace(/\.mp4$/i,'');
  const filenameObserved=leaf.some(el=>{const t=(el.innerText||el.textContent||'').trim();return t===filename||t===stem;});
  return {completion_texts:[...new Set(phrases)],filename_observed:filenameObserved};
 },path.basename(spec.file));
}
async function command(){
 const name=path.join(output,'command.json');let value;
 try{const stat=await fs.lstat(name);if(!stat.isFile()||stat.isSymbolicLink()||stat.size>4096)fail('SHORT_VIDEO_COMMAND_INVALID');value=JSON.parse(await fs.readFile(name,'utf8'));}
 catch(error){if(error.code==='ENOENT')return;throw error;}
 const keys=value?.action==='native_control'?['version','command_id','action','attempt_id','control_sha256','control_action']:['version','command_id','action','attempt_id'];
 if(!value||JSON.stringify(Object.keys(value).sort())!==JSON.stringify(keys.sort())||
  value.version!=='short-video-browser-command-v1'||!UUID.test(value.command_id||'')||value.attempt_id!==spec.attempt_id||!['inspect','close','native_control'].includes(value.action)||
  value.action==='native_control'&&(!/^[a-f0-9]{64}$/.test(value.control_sha256||'')||!['fill_metadata','submit','lookup'].includes(value.control_action)))
  fail('SHORT_VIDEO_COMMAND_INVALID');
 const processed=path.join(output,'command-'+value.command_id+'.processed.json');
 try{await fs.lstat(processed);fail('SHORT_VIDEO_COMMAND_ALREADY_PROCESSED');}catch(error){if(error.code!=='ENOENT')throw error;}
 await fs.rename(name,processed);
 if(value.action==='inspect'){
  const files=await inventory('inspect');await write('command-'+value.command_id+'-result.json',{state:'inspected',command_id:value.command_id,...files});
  await status(blocked?'blocked_waiting_for_operator':uploadObserved?'uploaded_waiting_for_operator':selected?'uploading_waiting_for_operator':'waiting_for_upload_intent',{command_id:value.command_id});
 }else if(value.action==='native_control'){
  // Only a root-maintained, read-only local mount may extend native controls.
  // There is no caller-supplied module path, selector, script or network fetch.
  const control='/run/short-video-controls/controls.mjs',directory=path.dirname(control);
  let outcome,controlInvoked=false,controlStage='guard';
  try{
   if(!await checkProtection())fail('SHORT_VIDEO_NATIVE_CONTROL_PROTECTION_REQUIRED');
   controlStage='verify_account';await accountIdentity();
   if(value.control_action==='submit'&&submissionObserved!==false)fail('SHORT_VIDEO_SUBMISSION_RECONCILE_REQUIRED');
   controlStage='verify_source_file';if(value.control_action!=='lookup')await checkedFile();
   controlStage='stat_root_control';
   const fileStat=await fs.lstat(control),dirStat=await fs.lstat(directory);
   if(!fileStat.isFile()||fileStat.isSymbolicLink()||dirStat.isSymbolicLink()||fileStat.uid!==0||dirStat.uid!==0||
    fileStat.mode&0o022||dirStat.mode&0o022||fileStat.size>262144)fail('SHORT_VIDEO_NATIVE_CONTROL_NOT_ROOT_OWNED');
   controlStage='read_readonly_mount';const mounts=await fs.readFile('/proc/self/mountinfo','utf8');
   if(!mounts.split('\n').some(line=>{const parts=line.split(' ');return parts[4]===directory&&parts[5]?.split(',').includes('ro');}))
    fail('SHORT_VIDEO_NATIVE_CONTROL_READONLY_MOUNT_REQUIRED');
   controlStage='verify_control_hash';const bytes=await fs.readFile(control);if(sha(bytes)!==value.control_sha256)fail('SHORT_VIDEO_NATIVE_CONTROL_HASH_MISMATCH');
   const url=pathToFileURL(control);url.searchParams.set('sha256',value.control_sha256);
   controlStage='import_control';const module=await import(url.href);
   controlStage='verify_imported_control';
   if(sha(await fs.readFile(control))!==value.control_sha256)fail('SHORT_VIDEO_NATIVE_CONTROL_HASH_MISMATCH');
   if(typeof module.handleNativeControl!=='function')fail('SHORT_VIDEO_NATIVE_CONTROL_UNSUPPORTED');
   // Capability declarations belong to the reviewed root-owned module. When
   // absent, distinguish unknown capability from a proven unsupported action.
   submitImplementation=typeof module.nativeControlCapabilities?.submit==='boolean'?module.nativeControlCapabilities.submit:null;
   controlStage='invoke_control';controlInvoked=true;
   if(value.control_action==='submit')submissionObserved=null;
   outcome=await module.handleNativeControl({page,context,spec:Object.freeze(structuredClone(spec)),output,
    control_action:value.control_action,helpers:Object.freeze({checkProtection,accountIdentity,checkedFile,write,inventory})});
   if(!outcome||typeof outcome!=='object'||Array.isArray(outcome)||Buffer.byteLength(JSON.stringify(outcome))>65536)
    fail('SHORT_VIDEO_NATIVE_CONTROL_RESULT_INVALID');
   if(typeof outcome.submit_implemented==='boolean')submitImplementation=outcome.submit_implemented;
   if(value.control_action==='submit'){
    submissionObserved=typeof outcome.submitted==='boolean'?outcome.submitted:null;
    if(outcome.submitted===true)submitImplementation=true;
    else if(outcome.state==='unsupported')submitImplementation=false;
    else if(typeof outcome.submitted==='boolean'&&submitImplementation===null)submitImplementation=true;
   }
  }catch(error){
   const codes=new Set(['SHORT_VIDEO_NATIVE_CONTROL_PROTECTION_REQUIRED','SHORT_VIDEO_NATIVE_CONTROL_NOT_ROOT_OWNED',
    'SHORT_VIDEO_NATIVE_CONTROL_READONLY_MOUNT_REQUIRED','SHORT_VIDEO_NATIVE_CONTROL_HASH_MISMATCH',
    'SHORT_VIDEO_NATIVE_CONTROL_UNSUPPORTED','SHORT_VIDEO_NATIVE_CONTROL_RESULT_INVALID','SHORT_VIDEO_SUBMISSION_RECONCILE_REQUIRED',
    'TARGET_ACCOUNT_IDENTITY_MISMATCH','SHORT_VIDEO_ACCOUNT_IDENTITY_AMBIGUOUS',
    'SHORT_VIDEO_ARTIFACT_SIZE_MISMATCH','SHORT_VIDEO_ARTIFACT_CHANGED','SHORT_VIDEO_ARTIFACT_HASH_MISMATCH']);
   // A module can fail after the actual click. Preserve unknown and prohibit
   // another submit control; read-only lookup and inspection remain available.
   outcome={state:controlInvoked&&value.control_action==='submit'?'submit_unknown':'unsupported',
    error_code:codes.has(error?.code)?error.code:'SHORT_VIDEO_NATIVE_CONTROL_REJECTED',submitted:submissionObserved,
    diagnostic:{error_name:['Error','TypeError','ReferenceError','SyntaxError','RangeError','TimeoutError'].includes(error?.name)?error.name:'OtherError',
     builtin_error_code:typeof error?.code==='string'&&(/^ERR_[A-Z_]+$/.test(error.code)||['ENOENT','EACCES','EROFS','EPERM','ENOTDIR'].includes(error.code))?error.code:null,
     controller_invoked:controlInvoked,control_stage:controlStage,account_stage:accountStage,failure_category:failureCategory(error)}};
  }
  await write('command-'+value.command_id+'-result.json',{version:'short-video-native-control-result-v1',
   command_id:value.command_id,attempt_id:spec.attempt_id,control_action:value.control_action,
   control_sha256:value.control_sha256,result:outcome});
  await status(blocked?'blocked_waiting_for_operator':uploadObserved?'uploaded_waiting_for_operator':selected?'uploading_waiting_for_operator':'waiting_for_upload_intent',
   {command_id:value.command_id,control_action:value.control_action,control_result_file:path.join(output,'command-'+value.command_id+'-result.json')});
 }else{await write('command-'+value.command_id+'-result.json',{state:'closed',command_id:value.command_id,profile_preserved:true});await status('closed_by_operator',{profile_preserved:true});await close();}
}
try{
 await checkedFile();
 const allowed=['PATH','HOME','TMP','TEMP','TMPDIR','LANG','LC_ALL','PLAYWRIGHT_BROWSERS_PATH','XDG_CONFIG_HOME','XDG_CACHE_HOME'];
 const browserEnv=Object.fromEntries(allowed.filter(k=>process.env[k]!==undefined).map(k=>[k,process.env[k]]));
 browserStage='launch_browser';context=await chromium.launchPersistentContext(profile,{headless:true,chromiumSandbox:true,channel,
  env:browserEnv,viewport:{width:1360,height:1000},locale:'zh-CN',timezoneId:'Asia/Shanghai'});
 context.setDefaultTimeout(15000);context.setDefaultNavigationTimeout(45000);
 browserStage='verify_account';await accountIdentity();page=context.pages()[0]||await context.newPage();
 browserStage='navigate_upload_page';
 await page.goto('https://member.bilibili.com/platform/upload/video/frame',{waitUntil:'domcontentloaded',timeout:45000});
 browserStage='wait_native_uploader';const videoInput=page.locator('input[name="buploader"][type="file"][accept*=".mp4"]');
 try{await videoInput.waitFor({state:'visible',timeout:45000});}
 catch{await inventory('hydration-timeout');await checkProtection();fail('SHORT_VIDEO_NATIVE_UPLOADER_NOT_READY');}
 if(await videoInput.count()!==1)fail('SHORT_VIDEO_NATIVE_UPLOADER_AMBIGUOUS');
 browserStage='inspect_initial_page';await inventory('before-selection');
 if(await checkProtection()){
  browserStage='wait_upload_intent';await status('waiting_for_upload_intent');
  let intent;
  while(Date.now()<deadline&&!closing){
   await command();if(closing)break;
   const p=path.join(output,'upload-intent.json');
   try{const st=await fs.lstat(p);if(!st.isFile()||st.isSymbolicLink()||st.size>4096)fail('SHORT_VIDEO_UPLOAD_INTENT_INVALID');intent=JSON.parse(await fs.readFile(p,'utf8'));}
   catch(error){if(error.code!=='ENOENT')throw error;}
   if(intent)break;await page.waitForTimeout(500);
  }
  if(!closing){
   if(!intent||intent.action!=='select_file'||intent.job_id!==spec.job_id||intent.attempt_id!==spec.attempt_id||
    intent.artifact_sha256!==spec.artifact_sha256||intent.file_bytes!==spec.file_bytes||intent.site_uid!==spec.target_site_uid)
    fail('SHORT_VIDEO_UPLOAD_INTENT_REQUIRED');
   browserStage='verify_account_before_selection';await accountIdentity();await checkedFile();
   if(await checkProtection()){
    const before=await nativeCompletion();if(before.completion_texts.length)fail('SHORT_VIDEO_PREEXISTING_UPLOAD_COMPLETION_REQUIRES_REVIEW');
    await write('native-selection-intent.json',{version:'short-video-native-selection-intent-v1',job_id:spec.job_id,
     attempt_id:spec.attempt_id,artifact_sha256:spec.artifact_sha256,file_bytes:spec.file_bytes,site_uid:spec.target_site_uid,at:new Date().toISOString(),submitted:false});
    browserStage='native_file_selection';let nativeInput;
    if(nativeChooser){
     const area=page.locator('.bcc-upload .upload-area:visible');
     if(await area.count()!==1)fail('SHORT_VIDEO_OBSERVED_UPLOAD_AREA_AMBIGUOUS');
     const prompt=await area.innerText();
     if(!prompt.includes('点击上传或将视频拖拽到此区域')||!prompt.includes('上传视频'))fail('SHORT_VIDEO_OBSERVED_UPLOAD_AREA_CHANGED');
     // Ordinary site click initializes its uploader and reveals the actual
     // native file chooser, which may belong to an unnamed hidden component.
     const pending=page.waitForEvent('filechooser',{timeout:15000});
     let chooser;
     try{await area.click({timeout:15000});chooser=await pending;}
     catch{await pending.catch(()=>{});await checkProtection();fail('SHORT_VIDEO_NATIVE_FILE_CHOOSER_REQUIRED');}
     nativeInput=chooser.element();
     const native=await nativeInput.evaluate(el=>({tag:el.tagName,type:el.type,accept:el.getAttribute('accept')||'',files:[...el.files].map(f=>({name:f.name,size:f.size}))}));
     if(native.tag!=='INPUT'||native.type!=='file'||!native.accept.toLowerCase().split(',').map(x=>x.trim()).includes('.mp4'))
      fail('SHORT_VIDEO_NATIVE_FILE_CHOOSER_TYPE_MISMATCH');
     if(native.files.length)fail('SHORT_VIDEO_PREEXISTING_NATIVE_FILES_REQUIRE_REVIEW');
     if(!await checkProtection())fail('SHORT_VIDEO_NATIVE_FILE_CHOOSER_PROTECTION_REQUIRED');
     await checkedFile();selected=true;await chooser.setFiles([spec.file],{timeout:30000});
    }else{
     nativeInput=await videoInput.elementHandle();if(!nativeInput)fail('SHORT_VIDEO_NATIVE_UPLOADER_NOT_READY');
     const priorFiles=await nativeInput.evaluate(el=>[...el.files].map(f=>({name:f.name,size:f.size})));
     if(priorFiles.length)fail('SHORT_VIDEO_PREEXISTING_NATIVE_FILES_REQUIRE_REVIEW');
     // Historical v1 route is retained for its owned fixture only. Production
     // activation uses explicit v2 plus the observed native chooser area.
     selected=true;await videoInput.setInputFiles([spec.file],{timeout:30000});
    }
    // Native upload frontends may immediately clear or replace their input.
    // Inspect the original element, never select again and never invent size
    // verification when the browser has already cleared the actual FileList.
    browserStage='observe_file_selection';const originalInput=await nativeInput.evaluate(el=>({attached:el.isConnected,files:[...el.files].map(f=>({name:f.name,size:f.size}))}));
    const files=originalInput.files;
    if(files.length>1||files.length===1&&(files[0].name!==path.basename(spec.file)||files[0].size!==spec.file_bytes))
     fail('SHORT_VIDEO_NATIVE_FILE_SELECTION_MISMATCH');
    let filenameAfterClear=false;
    if(files.length===0){
     const until=Math.min(deadline,Date.now()+5000);
     do{filenameAfterClear=(await nativeCompletion()).filename_observed;if(filenameAfterClear)break;await page.waitForTimeout(100);}while(Date.now()<until);
     if(!filenameAfterClear&&!nativeChooser)fail('SHORT_VIDEO_NATIVE_FILE_SELECTION_MISMATCH');
    }
    await write('native-file-selection.json',{version:'short-video-native-file-selection-v1',attempt_id:spec.attempt_id,files,
     observed_at:new Date().toISOString(),native_file_bytes_match:files.length===1,same_browser_attempt:true,
     original_input_attached:originalInput.attached,native_input_cleared_after_selection:files.length===0,
     native_filename_observed_after_clear:filenameAfterClear,source_file_sha256_verified_before_selection:true,
     selection_mode:nativeChooser?'native_filechooser_observed_area':'historical_direct_input',
     native_chooser_setfiles_resolved:nativeChooser});
    await inventory('after-selection');await status('native_upload_started');
   }
  }
 }
 browserStage='held_browser_commands';while(Date.now()<deadline&&!closing){
  await command();if(closing)break;
  if(selected&&!blocked&&!uploadObserved){
   if(await checkProtection()){
    const complete=await nativeCompletion();
    if(complete.filename_observed&&complete.completion_texts.length===1){
     await checkedFile();const files=await inventory('upload-completed');
     const causal={attempt_id:spec.attempt_id,site_uid:spec.target_site_uid,artifact_sha256:spec.artifact_sha256,file_bytes:spec.file_bytes,
      completion_text:complete.completion_texts[0],filename:path.basename(spec.file),observed_at:new Date().toISOString()};
     const observation={version:'short-video-browser-upload-v1',attempt_id:spec.attempt_id,site_uid:spec.target_site_uid,
      artifact_sha256:spec.artifact_sha256,file_bytes:spec.file_bytes,upload_reference_sha256:sha(JSON.stringify(causal)),
      same_attempt_observed:true,server_file_hash_verified:false};
     await write('upload-completion.json',{version:'short-video-upload-native-observation-v1',...causal,...files,
      reference_kind:'local_observation_digest_not_platform_upload_id',server_file_hash_verified:false});
     await write('upload-observation.json',observation);uploadObserved=true;
     await status('uploaded_waiting_for_operator',{upload_observation_file:path.join(output,'upload-observation.json')});
    }
   }
  }
  await page.waitForTimeout(1000);
 }
 if(!closing)await status('operator_window_expired',{selected,upload_observed:uploadObserved,profile_preserved:true});
}catch(error){
 const publicCodes=new Set(['SHORT_VIDEO_NATIVE_UPLOADER_NOT_READY','SHORT_VIDEO_NATIVE_UPLOADER_AMBIGUOUS',
  'SHORT_VIDEO_ACCOUNT_IDENTITY_AMBIGUOUS','TARGET_ACCOUNT_IDENTITY_MISMATCH','SHORT_VIDEO_UPLOAD_INTENT_INVALID',
  'SHORT_VIDEO_UPLOAD_INTENT_REQUIRED','SHORT_VIDEO_NATIVE_FILE_SELECTION_MISMATCH','SHORT_VIDEO_COMMAND_INVALID',
  'SHORT_VIDEO_COMMAND_ALREADY_PROCESSED','SHORT_VIDEO_PREEXISTING_UPLOAD_COMPLETION_REQUIRES_REVIEW','SHORT_VIDEO_PREEXISTING_NATIVE_FILES_REQUIRE_REVIEW',
  'SHORT_VIDEO_OBSERVED_UPLOAD_AREA_AMBIGUOUS','SHORT_VIDEO_OBSERVED_UPLOAD_AREA_CHANGED','SHORT_VIDEO_NATIVE_FILE_CHOOSER_REQUIRED',
  'SHORT_VIDEO_NATIVE_FILE_CHOOSER_TYPE_MISMATCH','SHORT_VIDEO_NATIVE_FILE_CHOOSER_PROTECTION_REQUIRED',
  'SHORT_VIDEO_ARTIFACT_SIZE_MISMATCH','SHORT_VIDEO_ARTIFACT_CHANGED','SHORT_VIDEO_ARTIFACT_HASH_MISMATCH']);
 const code=publicCodes.has(error?.code)?error.code:error?.name==='TimeoutError'?'SHORT_VIDEO_UI_TIMEOUT':'SHORT_VIDEO_BROWSER_ERROR';
 if(page&&!page.isClosed())await inventory('error').catch(()=>{});
 await status(submissionObserved===null?'submit_result_unknown':selected?'upload_result_unknown':'blocked_before_upload',
  {error_code:code,selected,upload_observed:uploadObserved,
   diagnostic:{browser_stage:browserStage,account_stage:accountStage,failure_category:failureCategory(error),error_name:['Error','TypeError','ReferenceError','SyntaxError','RangeError','TimeoutError'].includes(error?.name)?error.name:'OtherError',
    builtin_error_code:typeof error?.code==='string'&&(/^ERR_[A-Z_]+$/.test(error.code)||['ENOENT','EACCES','EROFS','EPERM','ENOTDIR'].includes(error.code))?error.code:null}});
}finally{await close();}
