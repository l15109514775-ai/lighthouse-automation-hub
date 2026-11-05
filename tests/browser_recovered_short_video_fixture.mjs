// Owned routed native DOM only; no platform account, upload or public content.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {Readable} from 'node:stream';
import {registerHooks} from 'node:module';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
const root=path.resolve(process.argv[2]),scenario=process.argv[3];
assert.ok(['normal_cache','restored_cache','wrong_account','protection','expired_command_first_frame','missing_cache','wrong_cached_title','module_hash','submit_without_ticket','cover_image','cover_video_rejected','cover_inspect','cover_hq_image','cover_hq_wrong_url','cover_hq_wrong_path','cover_derived_image','cover_derived_wrong_provenance','cover_derived_wrong_output_hash'].includes(scenario));
const hash=x=>crypto.createHash('sha256').update(x).digest('hex');
const canonical=v=>Array.isArray(v)?'['+v.map(canonical).join(',')+']':v!==null&&typeof v==='object'?'{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+canonical(v[k])).join(',')+'}':JSON.stringify(v);
const digest=v=>hash(canonical(v));const job=crypto.randomUUID(),attempt=crypto.randomUUID(),download=crypto.randomUUID(),recovery=crypto.randomUUID();
const metadata={title:'Owned synthetic replay',description:'Owned line one\nhttps://www.youtube.com/watch?v=abcdefghijk',source:'https://www.youtube.com/watch?v=abcdefghijk',category_label:'虚拟UP主',tags:['owned'],copyright:2};
const profile=path.join(root,'data/browser-profiles/bilibili-video');await fs.mkdir(profile,{recursive:true,mode:0o700});
const file=path.join(root,'media',download,'youtube-download-v1/original.mp4');await fs.mkdir(path.dirname(file),{recursive:true});const bytes=Buffer.from('Owned synthetic local bytes; no real media');await fs.writeFile(file,bytes);
const old=path.join(root,'media',job,'short-video-attempts',attempt);await fs.mkdir(old,{recursive:true});
const original={job_id:job,attempt_id:attempt,target_site_uid:'123456',metadata_sha256:digest(metadata),artifact_sha256:hash(bytes),source_id:'abcdefghijk',source_url:metadata.source,download_job_id:download,file,file_bytes:bytes.length};
const upload={version:'short-video-browser-upload-v1',attempt_id:attempt,site_uid:'123456',artifact_sha256:hash(bytes),file_bytes:bytes.length,upload_reference_sha256:'b'.repeat(64),same_attempt_observed:true,server_file_hash_verified:false};
for(const [name,value]of Object.entries({'spec.json':original,'metadata.json':metadata,'upload-observation.json':upload}))await fs.writeFile(path.join(old,name),JSON.stringify(value));
const spec={...original,version:'short-video-recovered-editor-v1',uploaded_attempt_id:attempt,recovery_id:recovery,upload_metadata_sha256:digest(metadata),metadata,upload_reference_sha256:upload.upload_reference_sha256,binding_sha256:'c'.repeat(64),duration_seconds:297};
if(scenario.startsWith('cover_')){const cover=path.join(old,'cover-source-official-maxres.jpg'),ownedBytes=Buffer.from([255,216,255,224,1,2,3]);await fs.writeFile(cover,ownedBytes);spec.cover={file:cover,bytes:ownedBytes.length,sha256:hash(ownedBytes),source_url:'https://i.ytimg.com/vi/abcdefghijk/maxresdefault.jpg'};}
if(scenario.startsWith('cover_hq_')){const cover=path.join(root,'media',job,'cover-assets-v2',scenario==='cover_hq_wrong_path'?'unapproved-hq.jpg':'official-hqdefault.jpg');await fs.mkdir(path.dirname(cover),{recursive:true});
 const ownedHeader=Buffer.from([255,216,255,192,0,17,8,1,104,1,224,3,1,17,0,2,17,1,3,17,1,255,217]);await fs.writeFile(cover,ownedHeader);
 spec.cover={file:cover,bytes:ownedHeader.length,sha256:hash(ownedHeader),source_url:'https://i.ytimg.com/vi/abcdefghijk/'+(scenario==='cover_hq_wrong_url'?'maxresdefault.jpg':'hqdefault.jpg')};}
if(scenario.startsWith('cover_derived_')){
 const source=path.join(old,'cover-source-official-maxres.jpg'),sourceBytes=Buffer.from([255,216,255,192,0,17,8,2,208,5,0,3,1,17,0,2,17,1,3,17,1,255,217]);await fs.writeFile(source,sourceBytes);
 const folder=path.join(root,'media',job,'cover-assets-v3');await fs.mkdir(folder,{recursive:true});const cover=path.join(folder,'blur-safe-cover.jpg'),outputBytes=Buffer.concat([sourceBytes,Buffer.from('Owned derived header fixture, not an actual FFmpeg image')]);await fs.writeFile(cover,outputBytes);
 spec.cover={file:cover,bytes:outputBytes.length,sha256:hash(outputBytes),source_url:'https://i.ytimg.com/vi/abcdefghijk/maxresdefault.jpg'};
 const provenance={version:'lighthouse-derived-cover-v3',policy_version:'blur-safe-foreground-v1',
  input:{file:source,bytes:sourceBytes.length,sha256:hash(sourceBytes),source_url:spec.cover.source_url,width:1280,height:720},
  output:{file:cover,bytes:outputBytes.length,sha256:scenario==='cover_derived_wrong_output_hash'?'0'.repeat(64):hash(outputBytes),width:1280,height:720,mime_type:'image/jpeg'},
  transform:{filter_complex:'[0:v]split=2[bg0][fg0];[bg0]scale=1280:720:force_original_aspect_ratio=increase:flags=lanczos,crop=1280:720,gblur=sigma=35:steps=3[bg];[fg0]scale=896:504:force_original_aspect_ratio=decrease:flags=lanczos,pad=896:504:(ow-iw)/2:(oh-ih)/2:color=black[fg];[bg][fg]overlay=192:108:format=auto,setsar=1,format=yuvj420p[out]',
   foreground_rect:{x:scenario==='cover_derived_wrong_provenance'?0:192,y:108,width:896,height:504},safe_crop:{x:160,y:0,width:960,height:720},background:{width:1280,height:720,blur_sigma:35},foreground_fit:'contain',foreground_padding:'black'},
  image_edited:true,no_text_regenerated:true,official_original_bytes:false,ffmpeg_version:'Owned synthetic header fixture; not a real transformation',created_at:new Date().toISOString()};
 const provenanceFile=path.join(folder,'provenance.json'),raw=JSON.stringify(provenance);await fs.writeFile(provenanceFile,raw);
 spec.cover_derivation={version:'derived-cover-binding-v1',provenance_file:provenanceFile,provenance_sha256:hash(raw),provenance};
}
const output=path.join(root,'media',job,'recovered-editors',recovery);
process.env.HUB_ROOT=root;process.env.BILIBILI_EXPECTED_SITE_UID='123456';Object.defineProperty(process,'stdin',{value:Readable.from([JSON.stringify(spec)]),configurable:true});
const control='/run/short-video-controls/controls.mjs',controlSource=`export const nativeControlCapabilities={submit:${scenario==='submit_without_ticket'}};export const ownedCounts={fill:0,lookup:0,submit:0};export async function handleNativeControl({control_action,helpers,page}){ownedCounts[control_action==='fill_metadata'?'fill':control_action]++;${scenario.startsWith('cover_')?`if(control_action==='fill_metadata'){const outcome=await helpers.boundCoverChooser(()=>page.locator('#owned-cover-button').click(),{select:${scenario!=='cover_inspect'}});return {...outcome,submitted:false,native_click_performed:false};}`:''}return {state:control_action==='lookup'?'inspected':'owned_filled',submitted:false,native_click_performed:false};}`;
const controlHash=hash(controlSource),controlUrl='owned-recovered-control:'+controlHash;
const ls=fs.lstat.bind(fs),rf=fs.readFile.bind(fs),op=fs.open.bind(fs);
fs.lstat=async(name,...a)=>{if(String(name)===control)return {isFile:()=>true,isSymbolicLink:()=>false,uid:0,mode:0o100644,size:Buffer.byteLength(controlSource)};if(String(name)===path.dirname(control))return {isSymbolicLink:()=>false,uid:0,mode:0o40755};const st=await ls(name,...a);return process.platform==='win32'&&String(name)===profile?Object.assign(st,{mode:st.mode&~0o077}):st;};
fs.readFile=async(name,...a)=>String(name)===control?Buffer.from(controlSource):String(name)==='/proc/self/mountinfo'?'999 1 0:1 / /run/short-video-controls ro - tmpfs fixture ro\n':rf(name,...a);
if(process.platform==='win32')fs.open=async(name,flags,...a)=>String(name)===output&&flags==='r'?{sync:async()=>{},close:async()=>{}}:op(name,flags,...a);
const hooks=registerHooks({resolve(s,c,next){if(s.startsWith('file:')&&s.includes('/run/short-video-controls/controls.mjs?'))return {url:controlUrl,shortCircuit:true};return next(s,c);},load(url,c,next){if(url===controlUrl)return {format:'module',source:controlSource,shortCircuit:true};return next(url,c);}});
let captured={restore:0,selections:0,submit:0,cover_selections:0},moduleResult=null,closed=false,activeContext;
const launch=chromium.launchPersistentContext.bind(chromium);
chromium.launchPersistentContext=async(name,options)=>{
 assert.equal(name,profile);assert.equal(options.chromiumSandbox,true);assert.equal(Object.keys(options.env).some(k=>/KEY|TOKEN|COOKIE|SECRET/.test(k)),false);
 const context=await launch(name,options);
 activeContext=context;
 const form=`<input placeholder="请输入稿件标题" value="${scenario==='wrong_cached_title'?'Wrong owned title':metadata.title}"><input placeholder="转载视频请注明来源、时间、地点(例：转自https://www.xxxx.com/yyyy)，注明来源会更快地通过审核哦" value="${metadata.source}"><div class="ql-editor" contenteditable="true"><p>Owned line one</p><p>${metadata.source}</p></div><span>original</span><span>上传完成</span><span class="submit-add">立即投稿</span><button id="owned-cover-button">Owned cover control</button><input id="owned-cover-input" type="file" accept="${scenario==='cover_video_rejected'?'.mp4':'image/*,.jpg'}" hidden>`;
 await context.route('**/*',route=>{
  const url=new URL(route.request().url());let body='Owned routed surface';
  if(url.hostname==='t.bilibili.com')body=`<a class="header-entry-mini" href="https://space.bilibili.com/${scenario==='wrong_account'?'654321':'123456'}">Owned account</a>`;
  else if(url.hostname==='member.bilibili.com')body=`<input type="file" name="buploader" accept=".mp4"><main>${scenario==='restored_cache'?'<p>本地浏览器存在1个未提交的视频</p><button id="continue">继续编辑</button><button>不用了</button>':scenario==='missing_cache'?'Owned upload start page':form}</main>${scenario==='protection'?'<div class="geetest_panel">Owned verification</div>':''}<script>window.owned={restore:0,selections:0,submit:0,cover_selections:0};document.querySelector('input[type=file]').addEventListener('change',()=>owned.selections++);document.querySelector('#continue')?.addEventListener('click',()=>{owned.restore++;document.querySelector('main').innerHTML=${JSON.stringify(form)};});document.addEventListener('click',e=>{if(e.target.classList.contains('submit-add'))owned.submit++;if(e.target.id==='owned-cover-button')document.querySelector('#owned-cover-input').click();});document.addEventListener('change',e=>{if(e.target.id==='owned-cover-input')owned.cover_selections++;});</script>`;
  return route.fulfill({contentType:'text/html',body:'<!doctype html><meta charset="utf-8"><style>body{font:16px Arial}input{display:block;width:250px;height:35px}.ql-editor{width:600px;min-height:80px}</style><body class="risk-captcha-adapt-pc risk-captcha-adapt">'+body+'</body>'});
 });
 const close=context.close.bind(context);context.close=async(...a)=>{for(const p of context.pages())if(new URL(p.url()).hostname==='member.bilibili.com')captured=await p.evaluate(()=>window.owned).catch(()=>captured);closed=true;return close(...a);};
 if(scenario==='expired_command_first_frame'){phase=1;firstId=await command('native_control',{control_sha256:controlHash,control_action:'fill_metadata',created_at:new Date(Date.now()-10000).toISOString(),expires_at:new Date(Date.now()-5000).toISOString()});}
 return context;
};
async function exists(f){try{return JSON.parse(await rf(f,'utf8'));}catch(e){if(e.code==='ENOENT')return null;throw e;}}
async function command(action,extra={}){const id=crypto.randomUUID(),now=Date.now();const cmd={version:'short-video-recovered-editor-command-v1',command_id:id,recovery_id:recovery,action,created_at:new Date(now-1000).toISOString(),expires_at:new Date(now+60000).toISOString(),...extra};const f=path.join(output,'command.json');await fs.writeFile(f+'.fixture',JSON.stringify(cmd));await fs.rename(f+'.fixture',f);return id;}
let phase=0,pollActive=false,firstId,secondId,pollError;const watchdog=setTimeout(()=>{pollError??=new Error('Owned recovery fixture watchdog');activeContext?.close().catch(()=>{});},80000);
const poll=setInterval(async()=>{if(pollActive)return;pollActive=true;try{
 const st=await exists(path.join(output,'status.json'));if(!st)return;
 if(phase===0&&['recovered_editor_waiting_for_operator','cached_editor_not_verified'].includes(st.state)){
  phase=1;
  if(scenario==='expired_command_first_frame')firstId=await command('native_control',{control_sha256:controlHash,control_action:'fill_metadata',created_at:new Date(Date.now()-10000).toISOString(),expires_at:new Date(Date.now()-5000).toISOString()});
  else firstId=await command('native_control',{control_sha256:scenario==='module_hash'?'0'.repeat(64):controlHash,control_action:scenario==='submit_without_ticket'?'submit':'fill_metadata'});
 }else if(phase===1&&firstId){const r=await exists(path.join(output,'command-'+firstId+'-result.json'));if(r){moduleResult=r;phase=2;secondId=await command(scenario==='submit_without_ticket'?'native_control':'close',scenario==='submit_without_ticket'?{control_sha256:controlHash,control_action:'lookup'}:{});}}
 else if(phase===2&&scenario==='submit_without_ticket'&&secondId){const r=await exists(path.join(output,'command-'+secondId+'-result.json'));if(r){assert.equal(r.result.state,'inspected');phase=3;await command('close');}}
 }catch(e){pollError??=e;await activeContext?.close().catch(()=>{});}finally{pollActive=false;}},30);
try{await import('../adapters/browser_recovered_short_video.mjs?owned='+crypto.randomUUID());}
finally{clearInterval(poll);clearTimeout(watchdog);hooks.deregister();}
assert.ifError(pollError);assert.equal(closed,true);assert.equal(captured.selections,0);assert.equal(captured.submit,0);
const status=await exists(path.join(output,'status.json'));assert.equal(status.new_native_file_selection,false);assert.equal(status.submitted,false);
if(scenario==='wrong_account')assert.equal(status.error_code,'RECOVERY_ACCOUNT_MISMATCH');
else if(['normal_cache','restored_cache'].includes(scenario))assert.equal(moduleResult.result.state,'owned_filled');
else if(['cover_image','cover_hq_image','cover_derived_image'].includes(scenario))assert.equal(moduleResult.result.state,'native_bound_cover_assigned');
else if(scenario==='cover_inspect')assert.equal(moduleResult.result.state,'native_bound_cover_chooser_observed');
else if(scenario==='expired_command_first_frame')assert.equal(moduleResult.state,'command_expired');
else assert.equal(moduleResult.result.state,'blocked');
assert.equal(captured.restore,scenario==='restored_cache'?1:0);
assert.equal(captured.cover_selections,['cover_image','cover_hq_image','cover_derived_image'].includes(scenario)?1:0);
if(['normal_cache','cover_image','cover_inspect'].includes(scenario)){const f=(await fs.readdir(output)).find(x=>x.endsWith('-recovered-native-editor.json'));const observed=JSON.parse(await rf(path.join(output,f),'utf8'));assert.equal(observed.visible_dom.observed_native_source_inputs[0].value,metadata.source);}
console.log('FIXTURE_RESULT:'+JSON.stringify({scenario,...captured,real_uploads:0,external_network_calls:0,submitted:status.submitted,live_tested:false,publication_authorized:false}));
