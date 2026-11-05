// Exercises the real CLI bridge against owned routed DOM; never a real upload.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {Readable} from 'node:stream';
import {registerHooks} from 'node:module';
import {pathToFileURL} from 'node:url';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';

const root=path.resolve(process.argv[2]),scenario=process.argv[3];
assert.ok(['normal','clear','replace','two_files','no_completion','unsupported_control','bad_intent','submit_throws','submit_true','chooser_normal','chooser_clear','chooser_txt'].includes(scenario));
const nativeChooserCase=scenario.startsWith('chooser_');
const job=crypto.randomUUID(),download=crypto.randomUUID(),attempt=crypto.randomUUID();
const profile=path.join(root,'data/browser-profiles/bilibili-video');await fs.mkdir(profile,{recursive:true,mode:0o700});
const file=path.join(root,'media',download,'youtube-download-v1/original.mp4');await fs.mkdir(path.dirname(file),{recursive:true});
const bytes=Buffer.from('Owned synthetic fixture, not a real source video or platform upload.');await fs.writeFile(file,bytes);
const spec={version:'short-video-browser-transport-v1',job_id:job,download_job_id:download,attempt_id:attempt,
 file,artifact_sha256:crypto.createHash('sha256').update(bytes).digest('hex'),metadata_sha256:'c'.repeat(64),
 file_bytes:bytes.length,duration_seconds:297,source_id:'abcdefghijk',source_url:'https://www.youtube.com/watch?v=abcdefghijk',target_site_uid:'123456'};
if(nativeChooserCase){spec.version='short-video-browser-transport-v2';spec.selection_mode='native_filechooser_observed_area';}
const output=path.join(root,'media',job,'short-video-attempts',attempt);await fs.mkdir(output,{recursive:true});
await fs.writeFile(path.join(output,'upload-intent.json'),JSON.stringify({action:'select_file',job_id:job,attempt_id:attempt,
 artifact_sha256:scenario==='bad_intent'?'0'.repeat(64):spec.artifact_sha256,file_bytes:spec.file_bytes,site_uid:'123456'}));
process.env.HUB_ROOT=root;process.env.BILIBILI_EXPECTED_SITE_UID='123456';process.env.HUB_BROWSER_CHANNEL='chromium';
Object.defineProperty(process,'stdin',{value:Readable.from([JSON.stringify(spec)]),configurable:true});
// Windows does not expose POSIX privacy bits. Only this owned metadata boundary
// is mocked there; Ubuntu runs the actual 0700 directory check unchanged.
const originalStat=fs.stat.bind(fs);
if(process.platform==='win32')fs.stat=async function(name,...args){const value=await originalStat(name,...args);return String(name)===profile?{...value,mode:value.mode&~0o077}:value;};
// These two cases isolate the controller protocol, not real mount ownership.
// Only fixed /run control-file metadata/loading is supplied by an owned fixture;
// all browser DOM, file selection and command handling remain actual bridge code.
const controllerCase=scenario.startsWith('submit_'),controlPath='/run/short-video-controls/controls.mjs';
const controlUrl=pathToFileURL(controlPath).href;
const linuxControlUrl='file:///run/short-video-controls/controls.mjs';
const controllerSource=`export const nativeControlCapabilities={submit:true,lookup:true};export const ownedControlCounts={submit:0,lookup:0};
export async function handleNativeControl({control_action}){
 if(control_action==='submit'){ownedControlCounts.submit++;${scenario==='submit_throws'?"throw new Error('owned-private-query-value');":"return {state:'awaiting_moderation',submitted:true,submit_implemented:true};"}}
 if(control_action==='lookup'){ownedControlCounts.lookup++;return {state:'inspected',submitted:false,submit_implemented:true};}
 return {state:'unsupported',submitted:false};}`;
const controllerHash=crypto.createHash('sha256').update(controllerSource).digest('hex');
const originalLstat=fs.lstat.bind(fs),originalReadFile=fs.readFile.bind(fs);let hooks;
const hookTrace={resolves:0,loads:0};const ownedControlUrl='owned-short-video-control:sha256-'+controllerHash;
if(controllerCase){
 fs.lstat=async function(name,...args){
  if(String(name)===controlPath)return {isFile:()=>true,isSymbolicLink:()=>false,uid:0,mode:0o100644,size:Buffer.byteLength(controllerSource)};
  if(String(name)==='/run/short-video-controls')return {isSymbolicLink:()=>false,uid:0,mode:0o40755};
  return originalLstat(name,...args);
 };
 fs.readFile=async function(name,...args){
  if(String(name)===controlPath)return Buffer.from(controllerSource);
  if(String(name)==='/proc/self/mountinfo')return '999 1 0:1 / /run/short-video-controls ro - tmpfs owned-fixture ro\n';
  return originalReadFile(name,...args);
 };
 hooks=registerHooks({
  resolve(specifier,context,next){
   if([controlUrl,linuxControlUrl].some(base=>specifier.startsWith(base+'?'))){hookTrace.resolves++;return {url:ownedControlUrl,shortCircuit:true};}
   return next(specifier,context);
  },
  load(url,context,next){if(url===ownedControlUrl){hookTrace.loads++;return {format:'module',source:controllerSource,shortCircuit:true};}return next(url,context);}
 });
}
let routed=0,captured=null,launchFailure=null;const originalLaunch=chromium.launchPersistentContext.bind(chromium);
function launchCategory(error){
 const message=typeof error?.message==='string'?error.message.toLowerCase():'';
 if(message.includes('--database is required'))return 'CHROMIUM_CRASHPAD_DATABASE_REQUIRED';
 if(/read.only file system|erofs/.test(message))return 'CHROMIUM_READONLY_FILESYSTEM';
 if(/no usable sandbox|failed to move to new namespace|sandbox.*operation not permitted|zygote.*operation not permitted/.test(message))return 'CHROMIUM_SANDBOX_OR_NAMESPACE_DENIED';
 if(/permission denied|eacces/.test(message))return 'CHROMIUM_FILESYSTEM_PERMISSION_DENIED';
 if(/executable doesn.t exist|executable does not exist/.test(message))return 'CHROMIUM_EXECUTABLE_MISSING';
 return error?.name==='TimeoutError'?'CHROMIUM_LAUNCH_TIMEOUT':'CHROMIUM_LAUNCH_FAILED';
}
chromium.launchPersistentContext=async function(name,options){
 assert.equal(name,profile);assert.equal(options.chromiumSandbox,true);assert.equal(options.headless,true);
 assert.equal(Object.keys(options.env).some(k=>/KEY|TOKEN|COOKIE|SECRET/.test(k)),false);
 let context;
 try{context=await originalLaunch(name,options);}
 catch(error){
  launchFailure={error_name:error?.name==='TimeoutError'?'TimeoutError':error?.name==='Error'?'Error':'OtherError',
   category:launchCategory(error),chromium_sandbox:true,browser_channel:'chromium',
   home_configured:typeof options.env.HOME==='string',xdg_config_configured:typeof options.env.XDG_CONFIG_HOME==='string',
   xdg_cache_configured:typeof options.env.XDG_CACHE_HOME==='string'};
  await fs.writeFile(path.join(output,'fixture-launch-diagnostic.json'),JSON.stringify(launchFailure,null,2));
  throw error; // The real bridge converts this to a safe public status code.
 }
 await context.route('**/*',route=>{
  routed++;let html='<!doctype html><body>Owned fixture</body>';
  const url=new URL(route.request().url());
  if(url.hostname==='t.bilibili.com')html='<a class="header-entry-mini" href="https://space.bilibili.com/123456">Owned account</a>';
  if(url.hostname==='member.bilibili.com')html=`<!doctype html><meta charset="utf-8"><style>body{font:16px Arial}input{display:block;width:220px;height:40px}</style>
   <body class="risk-captcha-adapt-pc risk-captcha-adapt"><input name="buploader" type="file" accept=".mp4,.mkv" multiple>
   <input name="buploader" type="file" accept=".txt"><input id="owned-native-uploader" type="file" accept="${scenario==='chooser_txt'?'.txt':'.mp4,.mkv'}" hidden>
   <div class="bcc-upload"><div class="upload-area" style="width:400px;height:160px"><div class="upload-text no-events">点击上传或将视频拖拽到此区域</div><div class="upload-btn no-events">上传视频</div></div></div><div id="owned-result"></div>
   <script>window.ownedCounts={selected:0,clicked:0,selected_input:null,area_clicked:0,placeholder_selected:0};document.addEventListener('click',()=>ownedCounts.clicked++);
   const nativeCase=${JSON.stringify(nativeChooserCase)};
   if(nativeCase){document.querySelector('.upload-area').addEventListener('click',()=>{ownedCounts.area_clicked++;document.querySelector('#owned-native-uploader').click();});
    document.querySelector('input[name="buploader"][accept*=".mp4"]').addEventListener('change',event=>{ownedCounts.placeholder_selected++;event.target.value='';});}
   const file=document.querySelector(nativeCase?'#owned-native-uploader':'input[name="buploader"][accept*=".mp4"]');file.addEventListener('change',event=>{
    ownedCounts.selected++;ownedCounts.selected_input=event.target.accept;const selected=event.target.files[0];
    document.querySelector('#owned-result').innerHTML='<span>'+selected.name+'</span>'+${JSON.stringify(scenario==='no_completion'?'':'<span>上传完成</span>')};
    if(${JSON.stringify(scenario)}==='chooser_clear'){event.target.value='';document.querySelector('#owned-result').innerHTML='<input type="text" class="owned-filename" value="original"><span>上传完成</span>';}
    if(${JSON.stringify(scenario)}==='clear')event.target.value='';
    if(${JSON.stringify(scenario)}==='replace')event.target.replaceWith(event.target.cloneNode());
    if(${JSON.stringify(scenario)}==='two_files'){const transfer=new DataTransfer();transfer.items.add(selected);transfer.items.add(new File(['owned second'],'second.mp4'));event.target.files=transfer.files;}
   });</script></body>`;
  return route.fulfill({contentType:'text/html',body:html});
 });
 const close=context.close.bind(context);
 context.close=async function(...args){
  const page=context.pages().find(p=>p.url().startsWith('https://member.bilibili.com'));
  if(page)captured=await page.evaluate(()=>window.ownedCounts).catch(()=>null);
  return close(...args);
 };
 return context;
};
let phase=0,commandId=null,pollError=null,lastControlResult=null,pollActive=false;
function safeError(error){return {error_name:['Error','TypeError','ReferenceError','SyntaxError','RangeError','TimeoutError','AssertionError'].includes(error?.name)?error.name:'OtherError',
 builtin_error_code:typeof error?.code==='string'&&(/^ERR_[A-Z_]+$/.test(error.code)||['ENOENT','EACCES','EROFS','EPERM','ENOTDIR'].includes(error.code))?error.code:null};}
const command=async(action,extra={})=>{
 const id=crypto.randomUUID();const temporary=path.join(output,'command-fixture.partial');
 await fs.writeFile(temporary,JSON.stringify({version:'short-video-browser-command-v1',command_id:id,action,attempt_id:attempt,...extra}));
 await fs.rename(temporary,path.join(output,'command.json'));return id;
};
const timer=setInterval(async()=>{
 if(pollError||pollActive)return;
 pollActive=true;
 try{
  const status=JSON.parse(await fs.readFile(path.join(output,'status.json'),'utf8'));
  if(controllerCase){
   const expected=scenario==='submit_throws'?null:true;
   if(phase===0&&status.state==='uploaded_waiting_for_operator'){
    phase=1;commandId=await command('native_control',{control_sha256:controllerHash,control_action:'submit'});
   }else if(phase>=1&&phase<=3){
    const readPhase=phase,readCommandId=commandId;
    let receipt;try{receipt=JSON.parse(await fs.readFile(path.join(output,'command-'+readCommandId+'-result.json'),'utf8'));}catch{return;}
    if(phase!==readPhase||commandId!==readCommandId)return;
    lastControlResult=Object.fromEntries(['state','error_code','submitted','diagnostic'].filter(k=>k in receipt.result).map(k=>[k,receipt.result[k]]));
    if(status.command_id!==readCommandId)return;
    assert.equal(receipt.command_id,readCommandId);
    assert.equal(status.submitted,expected);assert.equal(status.submit_implemented,true);
    assert.equal(JSON.stringify(receipt).includes('owned-private-query-value'),false);
    if(phase===1){
     assert.equal(receipt.result.state,scenario==='submit_throws'?'submit_unknown':'awaiting_moderation');
     assert.equal(receipt.result.submitted,expected);phase=2;
     commandId=await command('native_control',{control_sha256:controllerHash,control_action:'submit'});
    }else if(phase===2){
     assert.equal(receipt.result.error_code,'SHORT_VIDEO_SUBMISSION_RECONCILE_REQUIRED');
     assert.equal(receipt.result.submitted,expected);phase=3;
     commandId=await command('native_control',{control_sha256:controllerHash,control_action:'lookup'});
    }else{
     assert.equal(receipt.result.state,'inspected');phase=4;await command('close');
    }
   }
   return;
  }
  if(phase===0&&(status.state==='uploaded_waiting_for_operator'||['no_completion','chooser_clear'].includes(scenario)&&status.state==='native_upload_started')){
   phase=1;commandId=await command('inspect');
  }else if(phase===1){
   try{await fs.stat(path.join(output,'command-'+commandId+'-result.json'));}catch{return;}
   phase=2;
   commandId=scenario==='unsupported_control'?await command('native_control',{control_sha256:'d'.repeat(64),control_action:'submit'}):await command('close');
  }else if(phase===2&&scenario==='unsupported_control'){
   let receipt;try{receipt=JSON.parse(await fs.readFile(path.join(output,'command-'+commandId+'-result.json'),'utf8'));}catch{return;}
   assert.equal(receipt.result.state,'unsupported');assert.equal(receipt.result.submitted,false);phase=3;await command('close');
  }
 }catch(error){if(error.code!=='ENOENT'&&!pollError){pollError=error;command('close').catch(()=>{});}}
 finally{pollActive=false;}
},30);
const watchdog=setTimeout(()=>{if(!pollError)pollError=new Error('Owned bridge fixture exceeded 20 seconds');command('close').catch(()=>{});},20000);
try{
 await import('../adapters/browser_short_video.mjs');clearInterval(timer);clearTimeout(watchdog);
 if(pollError){
  const diagnostic={failure:'OWNED_FIXTURE_FIRST_POLL_ERROR',phase,...safeError(pollError),last_control_result:lastControlResult,controller_loader:hookTrace};
  await fs.writeFile(path.join(output,'fixture-first-poll-error.json'),JSON.stringify(diagnostic,null,2));
  throw new Error(JSON.stringify(diagnostic));
 }
 const status=JSON.parse(await fs.readFile(path.join(output,'status.json'),'utf8'));
 const safeStatus=Object.fromEntries(['state','error_code','selected','upload_observed','submitted','submit_implemented'].filter(k=>k in status).map(k=>[k,status[k]]));
 const profileStat=await originalStat(profile);
 assert.ok(captured!==null,JSON.stringify({failure:'OWNED_BROWSER_CLOSED_BEFORE_UPLOAD_PAGE_CAPTURE',status:safeStatus,
  launch_failure:launchFailure,routed_requests:routed,fixture_profile_uid:profileStat.uid,
  fixture_profile_mode:(profileStat.mode&0o777).toString(8),posix_mode_mocked:process.platform==='win32'}));
 assert.equal(status.submitted,scenario==='submit_throws'?null:scenario==='submit_true'?true:false);
 if(nativeChooserCase){assert.equal(captured.area_clicked,1);assert.ok(captured.clicked>=1);assert.equal(captured.placeholder_selected,0);}
 else assert.equal(captured.clicked,0);
 assert.ok(routed>=2);
 if(controllerCase){
  assert.equal(status.state,'closed_by_operator');assert.equal(status.submit_implemented,true);
  const module=await import(controlUrl+'?sha256='+controllerHash);
  assert.equal((await import(linuxControlUrl+'?sha256='+controllerHash)).ownedControlCounts,module.ownedControlCounts);
  assert.deepEqual(module.ownedControlCounts,{submit:1,lookup:1});
 }else if(scenario==='chooser_txt'){
  assert.equal(captured.selected,0);assert.equal(status.state,'blocked_before_upload');assert.equal(status.error_code,'SHORT_VIDEO_NATIVE_FILE_CHOOSER_TYPE_MISMATCH');
 }else if(scenario==='bad_intent'){
  assert.equal(captured.selected,0);assert.equal(status.state,'blocked_before_upload');assert.equal(status.error_code,'SHORT_VIDEO_UPLOAD_INTENT_REQUIRED');
 }else if(scenario==='two_files'){
  assert.equal(captured.selected,1);assert.equal(status.state,'upload_result_unknown');assert.equal(status.error_code,'SHORT_VIDEO_NATIVE_FILE_SELECTION_MISMATCH');
 }else{
  assert.equal(captured.selected,1);assert.equal(captured.selected_input,'.mp4,.mkv');assert.equal(status.state,'closed_by_operator');
  const selection=JSON.parse(await fs.readFile(path.join(output,'native-file-selection.json'),'utf8'));
  if(['clear','chooser_clear'].includes(scenario)){assert.equal(selection.native_file_bytes_match,false);assert.equal(selection.native_input_cleared_after_selection,true);assert.equal(selection.files.length,0);}
  else assert.equal(selection.native_file_bytes_match,true);
  if(nativeChooserCase){assert.equal(selection.selection_mode,'native_filechooser_observed_area');assert.equal(selection.native_chooser_setfiles_resolved,true);}
  if(scenario==='replace')assert.equal(selection.original_input_attached,false);
  if(['no_completion','chooser_clear'].includes(scenario))await assert.rejects(fs.readFile(path.join(output,'upload-observation.json')),{code:'ENOENT'});
  else{
   const observation=JSON.parse(await fs.readFile(path.join(output,'upload-observation.json'),'utf8'));
   assert.equal(observation.same_attempt_observed,true);assert.equal(observation.server_file_hash_verified,false);
   assert.equal(observation.artifact_sha256,spec.artifact_sha256);assert.equal(observation.file_bytes,spec.file_bytes);
   const complete=JSON.parse(await fs.readFile(path.join(output,'upload-completion.json'),'utf8'));
   assert.equal(complete.reference_kind,'local_observation_digest_not_platform_upload_id');
  }
 }
 process.stdout.write('FIXTURE_RESULT:'+JSON.stringify({scenario,native_selections:captured.selected,native_clicks:captured.clicked,
  native_area_clicks:captured.area_clicked,formal_submit_clicks:0,
  external_network_calls:0,routed_requests:routed,real_uploads:0,live_tested:false,human_review:false,publication_authorized:false,
  posix_mode_mocked:process.platform==='win32',controller_mount_metadata_mocked:controllerCase,
  submitted_after_close:status.submitted,submit_implemented:status.submit_implemented})+'\n');
}finally{clearInterval(timer);clearTimeout(watchdog);fs.stat=originalStat;fs.lstat=originalLstat;fs.readFile=originalReadFile;hooks?.deregister();chromium.launchPersistentContext=originalLaunch;}
