// Recovered native editor only: no upload or file-selection branch.
import fs from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
import {createReadStream} from 'node:fs';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {inspectVideoProtection} from './bilibili_video_guard.mjs';
import {admitRecoveredSubmit} from './recovered_submit_gate.mjs';
import {validateShortMetadataRevisionChain} from './short_video_metadata_chain.mjs';
process.umask(0o077);
const root=path.resolve(process.env.HUB_ROOT||'/opt/lighthouse-hub');
const UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/,SHA=/^[a-f0-9]{64}$/;
function fail(code){throw Object.assign(new Error(code),{code});}
function digest(value){function c(v){return Array.isArray(v)?'['+v.map(c).join(',')+']':v!==null&&typeof v==='object'?'{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+c(v[k])).join(',')+'}':JSON.stringify(v);}return createHash('sha256').update(c(value)).digest('hex');}
const raw=await new Promise((resolve,reject)=>{let s='';process.stdin.setEncoding('utf8');process.stdin.on('data',b=>{s+=b;if(Buffer.byteLength(s)>20000)reject(new Error('RECOVERY_SPEC_LIMIT'));});process.stdin.on('end',()=>resolve(s));});
const spec=JSON.parse(raw);
if(spec.recovery_entry!==undefined&&spec.recovery_entry!=='saved_draft_manager_v1')fail('RECOVERY_ENTRY_MODE_INVALID');
if(spec.version!=='short-video-recovered-editor-v1'||![spec.job_id,spec.uploaded_attempt_id,spec.recovery_id].every(x=>typeof x==='string'&&UUID.test(x))||
 spec.target_site_uid!==process.env.BILIBILI_EXPECTED_SITE_UID||!/^\d{1,30}$/.test(spec.target_site_uid||'')||!SHA.test(spec.metadata_sha256||'')||!SHA.test(spec.artifact_sha256||''))fail('RECOVERY_SPEC_INVALID');
const old=path.join(root,'media',spec.job_id,'short-video-attempts',spec.uploaded_attempt_id);
for(let p=old;p!==path.dirname(p);p=path.dirname(p))if((await fs.lstat(p)).isSymbolicLink())fail('RECOVERY_PATH_INVALID');
const previousMetadata=JSON.parse(await fs.readFile(path.join(old,'metadata.json'),'utf8'));
const meta=spec.metadata;
const original=JSON.parse(await fs.readFile(path.join(old,'spec.json'),'utf8'));
const upload=JSON.parse(await fs.readFile(path.join(old,'upload-observation.json'),'utf8'));
if(digest(previousMetadata)!==spec.upload_metadata_sha256||original.metadata_sha256!==spec.upload_metadata_sha256||original.artifact_sha256!==spec.artifact_sha256||
 original.job_id!==spec.job_id||original.attempt_id!==spec.uploaded_attempt_id||original.target_site_uid!==spec.target_site_uid||
 upload.attempt_id!==spec.uploaded_attempt_id||upload.site_uid!==spec.target_site_uid||upload.artifact_sha256!==spec.artifact_sha256||
 upload.same_attempt_observed!==true||upload.server_file_hash_verified!==false)fail('RECOVERY_UPLOAD_BINDING_INVALID');
if(spec.attempt_id!==spec.uploaded_attempt_id||!SHA.test(spec.upload_metadata_sha256||'')||
 !meta||digest(meta)!==spec.metadata_sha256||spec.source_url!==original.source_url||spec.source_id!==original.source_id||
 spec.download_job_id!==original.download_job_id||spec.file!==original.file||spec.file_bytes!==original.file_bytes||
 meta.title!==previousMetadata.title||meta.source!==spec.source_url||
 upload.upload_reference_sha256!==spec.upload_reference_sha256||!SHA.test(spec.binding_sha256||''))fail('RECOVERY_ACTIVE_BINDING_INVALID');
const validatedRevisionChain=validateShortMetadataRevisionChain({spec,originalMetadata:previousMetadata,originalSpec:original});
const expectedFile=path.join(root,'media',spec.download_job_id,'youtube-download-v1/original.mp4');
if(spec.file!==expectedFile||!Number.isSafeInteger(spec.file_bytes)||spec.file_bytes<1||spec.file_bytes>100000000||
 typeof spec.duration_seconds!=='number'||!Number.isFinite(spec.duration_seconds)||spec.duration_seconds<=0||spec.duration_seconds>600)fail('RECOVERY_SOURCE_INVALID');
async function checkedFile(){
 for(let p=expectedFile;p!==path.dirname(p);p=path.dirname(p))if((await fs.lstat(p)).isSymbolicLink())fail('RECOVERY_SOURCE_INVALID');
 const before=await fs.stat(expectedFile);if(!before.isFile()||before.size!==spec.file_bytes||await fs.realpath(expectedFile)!==expectedFile)fail('RECOVERY_SOURCE_INVALID');
 const hash=createHash('sha256');for await(const chunk of createReadStream(expectedFile))hash.update(chunk);
 const after=await fs.stat(expectedFile);if(hash.digest('hex')!==spec.artifact_sha256||after.size!==before.size||after.mtimeMs!==before.mtimeMs||after.ctimeMs!==before.ctimeMs)fail('RECOVERY_SOURCE_CHANGED');
}
await checkedFile();
const output=path.join(root,'media',spec.job_id,'recovered-editors',spec.recovery_id);
await fs.mkdir(path.dirname(output),{recursive:true,mode:0o700});
await fs.mkdir(output,{recursive:false,mode:0o700});
for(let p=output;p!==path.dirname(p);p=path.dirname(p))if((await fs.lstat(p)).isSymbolicLink())fail('RECOVERY_PATH_INVALID');
const profile=path.join(root,'data/browser-profiles/bilibili-video'),stat=await fs.lstat(profile);
if(!stat.isDirectory()||stat.isSymbolicLink()||stat.mode&0o077||await fs.realpath(profile)!==profile)fail('RECOVERY_PROFILE_INVALID');
async function write(name,value){const f=path.join(output,name);await fs.writeFile(f+'.partial',JSON.stringify(value,null,2),{mode:0o600});await fs.rename(f+'.partial',f);}
await write('metadata.json',meta);
async function status(state,extra={}){await write('status.json',{version:spec.version,state,job_id:spec.job_id,uploaded_attempt_id:spec.uploaded_attempt_id,recovery_id:spec.recovery_id,
 observed_at:new Date().toISOString(),native_existing_draft_restore_allowed:true,metadata_sha256:spec.metadata_sha256,submitted:submissionObserved,submit_implemented:submitImplementation,new_native_file_selection:false,public_submit:submissionObserved,draft_identity_verified:false,...extra});process.stdout.write(JSON.stringify({state,output})+'\n');}
let context,page,closed=false,sequence=0,editorReady=false,unexpectedChooser=false,submissionObserved=false,submitImplementation=false,coverPending=false;
async function close(){if(closed)return;closed=true;await context?.close();}
process.on('SIGTERM',()=>{close().finally(()=>process.exit(0));});
async function identity(){const p=await context.newPage();try{await p.goto('https://t.bilibili.com/',{waitUntil:'domcontentloaded',timeout:45000});const a=p.locator('a.header-entry-mini');await a.waitFor({state:'visible',timeout:20000});
 if(await a.count()!==1)fail('RECOVERY_ACCOUNT_AMBIGUOUS');const u=new URL(await a.getAttribute('href'),'https://t.bilibili.com');
 if(u.origin!=='https://space.bilibili.com'||/^\/(\d+)\/?$/.exec(u.pathname)?.[1]!==spec.target_site_uid)fail('RECOVERY_ACCOUNT_MISMATCH');
 await write('account-identity.json',{site_uid:spec.target_site_uid,method:'visible_native_avatar_href',same_browser_context:true});return spec.target_site_uid;}finally{await p.close();}}
async function inspect(label){const protection=await inspectVideoProtection(page);const value=await page.evaluate(expected=>{
 function visible(e){const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>1&&r.height>1&&s.display!=='none'&&s.visibility!=='hidden';}
 const safe=raw=>{try{const u=new URL(raw,location.href);return u.origin+u.pathname;}catch{return null;}};
 const all=[...document.querySelectorAll('body *')].filter(visible);
 return {url:safe(location.href),title:document.title,visible_text:(document.body?.innerText||'').slice(0,24000),
  exact_title_matches:all.filter(e=>e.children.length===0&&(e.innerText||'').trim()===expected).slice(0,20).map(e=>({tag:e.tagName,classes:[...e.classList],text:e.innerText,parent_text:(e.parentElement?.innerText||'').slice(0,2000)})),
  links:[...document.querySelectorAll('a[href]')].filter(visible).slice(0,120).map(e=>({text:(e.innerText||'').trim().slice(0,200),url:safe(e.getAttribute('href'))})),
  visible_dialogs:[...document.querySelectorAll('dialog,[role="dialog"],[role="alertdialog"],[aria-modal="true"]')].filter(visible).slice(0,20).map(e=>({tag:e.tagName,classes:[...e.classList],role:e.getAttribute('role'),text:(e.innerText||'').slice(0,12000),controls:[...e.querySelectorAll('button,[role="button"],a[href],input')].filter(visible).slice(0,30).map(c=>({tag:c.tagName,classes:[...c.classList],text:(c.innerText||'').trim().slice(0,300),placeholder:c.getAttribute('placeholder')}))})),
  controls:[...document.querySelectorAll('button,[role="button"],input,textarea,[role="tab"]')].filter(visible).slice(0,120).map(e=>({tag:e.tagName,classes:[...e.classList],role:e.getAttribute('role'),placeholder:e.getAttribute('placeholder'),text:(e.innerText||'').trim().slice(0,300)})),
  observed_native_title_inputs:[...document.querySelectorAll('input[placeholder="请输入稿件标题"]')].filter(visible).slice(0,10).map(e=>({tag:e.tagName,type:e.type,placeholder:e.placeholder,value:e.value,bound_title_exact_match:e.value===expected})),
  observed_native_source_inputs:[...document.querySelectorAll('input[placeholder^="转载视频请注明来源"]')].filter(visible).slice(0,10).map(e=>({tag:e.tagName,type:e.type,placeholder:e.placeholder,value:e.value})),
  observed_description_editors:[...document.querySelectorAll('.ql-editor[contenteditable="true"]')].filter(visible).slice(0,5).map(e=>({text:(e.innerText||'').slice(0,5000)}))};
 },meta.title);
 const name=String(++sequence).padStart(3,'0')+'-'+label;await write(name+'.json',{version:'native-draft-recovery-visible-observation-v2',observed_at:new Date().toISOString(),
  protection,visible_dom:value,metadata_title:meta.title,source_url:meta.source,draft_identity_verified:false,title_match_is_only_candidate:true,private_blob_retention:'unknown'});
 await page.screenshot({path:path.join(output,name+'.png'),fullPage:false,timeout:10000}).catch(()=>{});return protection;}
async function checkProtection(){const value=await inspectVideoProtection(page);if(value.blocked){await write('protection-required.json',value);return false;}return true;}
async function inventory(label){const protection=await inspect(label);const prefix=String(sequence).padStart(3,'0')+'-'+label;return {protection,inventory_file:path.join(output,prefix+'.json'),screenshot_file:path.join(output,prefix+'.png')};}
function jpegDimensions(bytes){
 if(bytes.length<4||bytes[0]!==255||bytes[1]!==216)return null;
 let at=2;const sof=new Set([192,193,194,195,197,198,199,201,202,203,205,206,207]);
 while(at<bytes.length){if(bytes[at++]!==255)return null;while(at<bytes.length&&bytes[at]===255)at++;
  if(at>=bytes.length)return null;const marker=bytes[at++];if(marker===217||marker===218)return null;
  if(marker===1||marker>=208&&marker<=215)continue;
  if(at+2>bytes.length)return null;const length=bytes.readUInt16BE(at);if(length<2||at+length>bytes.length)return null;
  if(sof.has(marker)){if(length<8)return null;return {width:bytes.readUInt16BE(at+5),height:bytes.readUInt16BE(at+3)};}
  at+=length;
 }return null;
}
const DERIVED_COVER_FILTER='[0:v]split=2[bg0][fg0];[bg0]scale=1280:720:force_original_aspect_ratio=increase:flags=lanczos,crop=1280:720,gblur=sigma=35:steps=3[bg];[fg0]scale=896:504:force_original_aspect_ratio=decrease:flags=lanczos,pad=896:504:(ow-iw)/2:(oh-ih)/2:color=black[fg];[bg][fg]overlay=192:108:format=auto,setsar=1,format=yuvj420p[out]';
const TOP_BOTTOM_COVER_FILTER='[0:v]format=rgb24,split=2[bg0][fg];[bg0]scale=1280:960:force_original_aspect_ratio=increase:flags=lanczos,crop=1280:960,gblur=sigma=35:steps=3[bg];[bg][fg]overlay=0:120:format=rgb,format=rgb24[out]';
function pngDimensions(bytes){
 if(bytes.length<33||!bytes.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10]))||bytes.readUInt32BE(8)!==13||bytes.toString('ascii',12,16)!=='IHDR')return null;
 return {width:bytes.readUInt32BE(16),height:bytes.readUInt32BE(20)};
}
async function checkedCoverDerivation(cover){
 const evidence=spec.cover_derivation,v4=evidence?.version==='derived-cover-binding-v2',canonical=path.join(root,'media',spec.job_id,v4?'cover-assets-v4/provenance.json':'cover-assets-v3/provenance.json');
 if(!evidence||JSON.stringify(Object.keys(evidence).sort())!==JSON.stringify(['version','provenance_file','provenance_sha256','provenance'].sort())||
  !['derived-cover-binding-v1','derived-cover-binding-v2'].includes(evidence.version)||evidence.provenance_file!==canonical||!SHA.test(evidence.provenance_sha256||''))fail('RECOVERY_COVER_DERIVATION_REQUIRED');
 for(let p=canonical;p!==path.dirname(p);p=path.dirname(p))if((await fs.lstat(p)).isSymbolicLink())fail('RECOVERY_COVER_DERIVATION_INVALID');
 const stat=await fs.stat(canonical);if(!stat.isFile()||stat.size>32768||await fs.realpath(canonical)!==canonical)fail('RECOVERY_COVER_DERIVATION_INVALID');
 const bytes=await fs.readFile(canonical);if(createHash('sha256').update(bytes).digest('hex')!==evidence.provenance_sha256)fail('RECOVERY_COVER_DERIVATION_CHANGED');
 const p=JSON.parse(bytes.toString('utf8'));
 if(digest(p)!==digest(evidence.provenance)||p.version!==(v4?'lighthouse-derived-cover-v4':'lighthouse-derived-cover-v3')||p.policy_version!==(v4?'top-bottom-blur-v1':'blur-safe-foreground-v1')||
  p.image_edited!==true||p.no_text_regenerated!==true||p.official_original_bytes!==false||
  typeof p.ffmpeg_version!=='string'||p.ffmpeg_version.length<1||p.ffmpeg_version.length>2048||
  !Number.isFinite(Date.parse(p.created_at))||Date.parse(p.created_at)>Date.now()+60000)fail('RECOVERY_COVER_DERIVATION_INVALID');
 const expectedTransform=v4?{filter_complex:TOP_BOTTOM_COVER_FILTER,foreground_rect:{x:0,y:120,width:1280,height:720},
  safe_crop:{x:0,y:0,width:1280,height:960},background:{width:1280,height:960,blur_sigma:35},foreground_fit:'original_size',foreground_padding:'none'}:{filter_complex:DERIVED_COVER_FILTER,foreground_rect:{x:192,y:108,width:896,height:504},
  safe_crop:{x:160,y:0,width:960,height:720},background:{width:1280,height:720,blur_sigma:35},foreground_fit:'contain',foreground_padding:'black'};
 if(digest(p.transform)!==digest(expectedTransform)||
  digest(p.output)!==digest({file:cover.file,bytes:cover.bytes,sha256:cover.sha256,width:1280,height:v4?960:720,mime_type:v4?'image/png':'image/jpeg'})||
  cover.file!==path.join(root,'media',spec.job_id,v4?'cover-assets-v4/top-bottom-blur-cover.png':'cover-assets-v3/blur-safe-cover.jpg'))fail('RECOVERY_COVER_DERIVATION_INVALID');
 const original=path.join(old,'cover-source-official-maxres.jpg');
 if(p.input?.file!==original||p.input?.source_url!=='https://i.ytimg.com/vi/'+spec.source_id+'/maxresdefault.jpg'||
  !Number.isSafeInteger(p.input?.bytes)||p.input.bytes<4||p.input.bytes>1000000||!SHA.test(p.input?.sha256||'')||p.input.width!==1280||p.input.height!==720)fail('RECOVERY_COVER_DERIVATION_INVALID');
 for(let f=original;f!==path.dirname(f);f=path.dirname(f))if((await fs.lstat(f)).isSymbolicLink())fail('RECOVERY_COVER_DERIVATION_INVALID');
 const sourceStat=await fs.stat(original);if(!sourceStat.isFile()||sourceStat.size!==p.input.bytes)fail('RECOVERY_COVER_DERIVATION_SOURCE_CHANGED');
 const source=await fs.readFile(original),dimensions=jpegDimensions(source);
 if(sourceStat.size!==p.input.bytes||source.length!==p.input.bytes||createHash('sha256').update(source).digest('hex')!==p.input.sha256||
  dimensions?.width!==1280||dimensions?.height!==720||await fs.realpath(original)!==original)fail('RECOVERY_COVER_DERIVATION_SOURCE_CHANGED');
}
async function checkedCover(){
 const cover=spec.cover,keys=['file','bytes','sha256','source_url'];
 const maxres=cover?.file===path.join(old,'cover-source-official-maxres.jpg')&&cover?.source_url==='https://i.ytimg.com/vi/'+spec.source_id+'/maxresdefault.jpg';
 const hq=cover?.file===path.join(root,'media',spec.job_id,'cover-assets-v2/official-hqdefault.jpg')&&cover?.source_url==='https://i.ytimg.com/vi/'+spec.source_id+'/hqdefault.jpg';
 const derived=cover?.file===path.join(root,'media',spec.job_id,'cover-assets-v3/blur-safe-cover.jpg')&&cover?.source_url==='https://i.ytimg.com/vi/'+spec.source_id+'/maxresdefault.jpg';
 const derivedV4=cover?.file===path.join(root,'media',spec.job_id,'cover-assets-v4/top-bottom-blur-cover.png')&&cover?.source_url==='https://i.ytimg.com/vi/'+spec.source_id+'/maxresdefault.jpg';
 if(spec.cover_derivation!==undefined&&!derived&&!derivedV4)fail('RECOVERY_COVER_DERIVATION_INVALID');
 if(!cover||JSON.stringify(Object.keys(cover).sort())!==JSON.stringify(keys.sort())||
  !maxres&&!hq&&!derived&&!derivedV4||!Number.isSafeInteger(cover.bytes)||cover.bytes<4||cover.bytes>(derivedV4?5000000:1000000)||
  !SHA.test(cover.sha256||''))fail('RECOVERY_BOUND_COVER_INVALID');
 for(let p=cover.file;p!==path.dirname(p);p=path.dirname(p))if((await fs.lstat(p)).isSymbolicLink())fail('RECOVERY_BOUND_COVER_INVALID');
 const before=await fs.stat(cover.file);if(!before.isFile()||before.size!==cover.bytes)fail('RECOVERY_BOUND_COVER_CHANGED');
 const bytes=await fs.readFile(cover.file),after=await fs.stat(cover.file);
 if(!before.isFile()||before.size!==cover.bytes||bytes.length!==cover.bytes||!derivedV4&&(bytes[0]!==255||bytes[1]!==216||bytes[2]!==255)||
  createHash('sha256').update(bytes).digest('hex')!==cover.sha256||before.mtimeMs!==after.mtimeMs||before.ctimeMs!==after.ctimeMs||
  await fs.realpath(cover.file)!==cover.file)fail('RECOVERY_BOUND_COVER_CHANGED');
 if(hq){const dimensions=jpegDimensions(bytes);if(dimensions?.width!==480||dimensions?.height!==360)fail('RECOVERY_BOUND_COVER_DIMENSIONS_INVALID');}
 if(derived){const dimensions=jpegDimensions(bytes);if(dimensions?.width!==1280||dimensions?.height!==720)fail('RECOVERY_BOUND_COVER_DIMENSIONS_INVALID');await checkedCoverDerivation(cover);}
 if(derivedV4){const dimensions=pngDimensions(bytes);if(dimensions?.width!==1280||dimensions?.height!==960)fail('RECOVERY_BOUND_COVER_DIMENSIONS_INVALID');await checkedCoverDerivation(cover);}
 return cover;
}
async function boundCoverChooser(trigger,{select=false}={}){
 if(typeof trigger!=='function'||typeof select!=='boolean'||coverPending||unexpectedChooser)fail('RECOVERY_COVER_CONTROL_INVALID');
 const cover=await checkedCover();if(!await checkProtection())fail('RECOVERY_PROTECTION_REQUIRED');
 coverPending=true;const pending=page.waitForEvent('filechooser',{timeout:5000}).then(value=>({value}),()=>({timeout:true}));
 try{
  await trigger();const observed=await pending;
  if(observed.timeout){if(select)fail('RECOVERY_COVER_CHOOSER_NOT_OBSERVED');return {state:'native_cover_ui_opened_no_filechooser',files_assigned:false};}
  const chooser=observed.value,element=await chooser.element();
  const native=await element.evaluate(el=>({tag:el.tagName,type:el.type,accept:el.getAttribute('accept'),multiple:el.multiple}));
  const accepted=(native.accept||'').toLowerCase().split(',').map(x=>x.trim()).filter(Boolean);
  if(native.tag!=='INPUT'||native.type!=='file'||accepted.length===0||
   !accepted.every(x=>/^image\/[a-z0-9.*+-]+$/.test(x)||['.jpg','.jpeg','.png','.webp','.gif','.bmp','.avif'].includes(x))){unexpectedChooser=true;fail('RECOVERY_COVER_INPUT_NOT_IMAGE_ONLY');}
  await write('cover-native-chooser-observation.json',{native,files_assigned:false,new_video_file_selection:false});
  if(!select)return {state:'native_bound_cover_chooser_observed',files_assigned:false,native};
  const intent=await fs.open(path.join(output,'cover-selection-intent.json'),'wx',0o600);
  try{await intent.writeFile(JSON.stringify({version:'bound-native-cover-selection-v1',cover,job_id:spec.job_id,attempt_id:spec.attempt_id,
   recovery_id:spec.recovery_id,metadata_sha256:spec.metadata_sha256,public_submit:false,new_video_file_selection:false}));await intent.sync();}finally{await intent.close();}
  const dir=await fs.open(output,'r');try{await dir.sync();}finally{await dir.close();}
  await chooser.setFiles(cover.file);await checkedCover();
  await write('cover-selection-observation.json',{version:'bound-native-cover-selection-v1',cover,native,files_assigned:true,
   ...(spec.cover_derivation?{cover_derivation:spec.cover_derivation,image_edited:true,no_text_regenerated:true,official_original_bytes:false}:{}),
   local_source_hash_verified:true,server_file_hash_verified:false,new_video_file_selection:false,public_submit:false});
  return {state:'native_bound_cover_assigned',files_assigned:true,server_file_hash_verified:false,new_video_file_selection:false};
 }finally{coverPending=false;}
}
async function verifyEditor({previousDescription=false}={}){
 if(unexpectedChooser)fail('RECOVERY_UNEXPECTED_FILECHOOSER');
 const u=new URL(page.url());if(u.origin!=='https://member.bilibili.com'||u.pathname!=='/platform/upload/video/frame')fail('RECOVERY_ORIGIN_CHANGED');
 if(!await checkProtection())fail('RECOVERY_PROTECTION_REQUIRED');
 const title=page.locator('input[placeholder="请输入稿件标题"]:visible'),description=page.locator('.ql-editor[contenteditable="true"]:visible');
 if(await title.count()!==1||await description.count()!==1||await title.inputValue()!==meta.title)fail('RECOVERY_CACHED_EDITOR_TITLE_MISMATCH');
 const desc=await description.evaluate(el=>{
  const allowed=new Set(['SPAN','A','B','I','U','S','STRONG','EM','CODE']);
  function inline(n){if(n.nodeType===Node.TEXT_NODE)return n.nodeValue;if(n.nodeType===Node.COMMENT_NODE)return '';if(n.nodeType!==Node.ELEMENT_NODE)throw 0;if(n.tagName==='BR')return '\n';if(!allowed.has(n.tagName)||n.getAttribute('contenteditable')==='false'||n.classList.contains('ql-cursor'))throw 0;const s=getComputedStyle(n);if(s.display==='none'||s.visibility==='hidden')throw 0;return [...n.childNodes].map(inline).join('');}
  try{const nodes=[...el.childNodes].filter(n=>n.nodeType!==Node.COMMENT_NODE&&!(n.nodeType===Node.TEXT_NODE&&n.nodeValue===''));return {supported:true,text:nodes.some(n=>n.nodeType===Node.ELEMENT_NODE&&['P','DIV'].includes(n.tagName))?nodes.map(n=>{if(n.nodeType!==Node.ELEMENT_NODE||!['P','DIV'].includes(n.tagName))throw 0;return n.childNodes.length===1&&n.firstChild.nodeName==='BR'?'':[...n.childNodes].map(inline).join('');}).join('\n'):nodes.map(inline).join('')};}catch{return {supported:false};}
 });
 const expectedDescription=previousDescription?spec.metadata_revision_history?.[1]?.previous_binding?.metadata?.description:meta.description;
 if(previousDescription&&validatedRevisionChain.mode!=='category_and_public_line_removal')fail('RECOVERY_METADATA_SYNC_SCOPE_REQUIRED');
 if(!desc.supported||desc.text!==expectedDescription)fail('RECOVERY_CACHED_EDITOR_DESCRIPTION_MISMATCH');
 const completion=await page.evaluate(()=>[...document.querySelectorAll('body *')].some(e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>1&&r.height>1&&s.display!=='none'&&s.visibility!=='hidden'&&e.children.length===0&&(e.innerText||'').trim()==='上传完成';}));
 if(!completion)fail('RECOVERY_CACHED_UPLOAD_COMPLETION_REQUIRED');
 await checkedFile();if(previousDescription){await write('native-prior-description-for-sync.json',{job_id:spec.job_id,attempt_id:spec.attempt_id,recovery_id:spec.recovery_id,
  previous_metadata_sha256:spec.metadata_revision_history[1].previous_metadata_sha256,pending_metadata_sha256:spec.metadata_sha256,expected_exact_suffix_removal:true,new_file_selection:false,public_submit:false});return true;}
 editorReady=true;
 await write('recovered-editor-correlation.json',{version:'native-recovered-editor-correlation-v1',job_id:spec.job_id,attempt_id:spec.attempt_id,
  recovery_id:spec.recovery_id,metadata_sha256:spec.metadata_sha256,upload_metadata_sha256:spec.upload_metadata_sha256,
  upload_reference_sha256:spec.upload_reference_sha256,same_profile_cached_title_description_source_correlated:true,
  native_upload_complete_observed:true,server_file_hash_verified:false,new_file_selection:false,public_submit:false});
 return true;
}
async function nativeControl(command){
 const control='/run/short-video-controls/controls.mjs',directory=path.dirname(control);let invoked=false,outcome;
 try{
  await identity();
  if(command.control_action==='sync_metadata_revision'){
   if(validatedRevisionChain.mode!=='category_and_public_line_removal')fail('RECOVERY_METADATA_SYNC_SCOPE_REQUIRED');
   if(!await checkProtection())fail('RECOVERY_PROTECTION_REQUIRED');await checkedFile();
   try{await verifyEditor();await write('command-'+command.command_id+'-result.json',{version:'recovered-native-control-result-v1',command_id:command.command_id,control_action:command.control_action,
    result:{state:'metadata_already_current',native_metadata_revision_verified:true,native_modified:false,controller_invoked:false,submitted:submissionObserved}});await status('recovered_editor_waiting_for_operator',{last_command_id:command.command_id});return;}
   catch(e){if(e?.code!=='RECOVERY_CACHED_EDITOR_DESCRIPTION_MISMATCH')throw e;}
   await verifyEditor({previousDescription:true});
  }else if(command.control_action==='lookup'){if(!await checkProtection())fail('RECOVERY_PROTECTION_REQUIRED');}else await verifyEditor();
  if(command.control_action==='submit'&&submissionObserved!==false)fail('RECOVERY_SUBMISSION_RECONCILE_REQUIRED');
  const st=await fs.lstat(control),ds=await fs.lstat(directory);
  if(!st.isFile()||st.isSymbolicLink()||ds.isSymbolicLink()||st.uid!==0||ds.uid!==0||st.mode&0o022||ds.mode&0o022||st.size>262144)fail('RECOVERY_CONTROL_NOT_ROOT_OWNED');
  const mounts=await fs.readFile('/proc/self/mountinfo','utf8');if(!mounts.split('\n').some(line=>{const p=line.split(' ');return p[4]===directory&&p[5]?.split(',').includes('ro');}))fail('RECOVERY_CONTROL_READONLY_MOUNT_REQUIRED');
  const sha=bytes=>createHash('sha256').update(bytes).digest('hex');if(sha(await fs.readFile(control))!==command.control_sha256)fail('RECOVERY_CONTROL_HASH_MISMATCH');
  const url=pathToFileURL(control);url.searchParams.set('sha256',command.control_sha256);const module=await import(url.href);
  if(sha(await fs.readFile(control))!==command.control_sha256||typeof module.handleNativeControl!=='function')fail('RECOVERY_CONTROL_HASH_MISMATCH');
  submitImplementation=module.nativeControlCapabilities?.submit===true;
  if(command.control_action==='sync_metadata_revision'&&module.nativeControlCapabilities?.sync_metadata_revision!==true)fail('RECOVERY_METADATA_SYNC_CAPABILITY_REQUIRED');
  if(command.control_action==='submit'){
   if(module.nativeControlCapabilities?.submit!==true)fail('RECOVERY_SUBMIT_CAPABILITY_DISABLED');
   await admitRecoveredSubmit({root,spec,output,command_id:command.command_id});submissionObserved=null;
  }
  invoked=true;outcome=await module.handleNativeControl({page,context,spec:Object.freeze(structuredClone(spec)),output,control_action:command.control_action,
   helpers:Object.freeze({checkProtection,accountIdentity:identity,checkedFile,write,inventory,boundCoverChooser})});
  if(!outcome||typeof outcome!=='object'||Array.isArray(outcome)||Buffer.byteLength(JSON.stringify(outcome))>65536)fail('RECOVERY_CONTROL_RESULT_INVALID');
  if(command.control_action==='sync_metadata_revision'){await verifyEditor();outcome={...outcome,native_metadata_revision_verified:true};}
  if(command.control_action==='submit')submissionObserved=outcome.submitted===true?true:
   outcome.submitted===false&&outcome.native_click_performed===false?false:null;
 }catch(e){outcome={state:command.control_action==='submit'&&submissionObserved!==false?'submit_unknown':'blocked',
  error_code:typeof e?.code==='string'&&/^(RECOVERY_|RECOVERED_SUBMIT_)[A-Z_]+$/.test(e.code)?e.code:'RECOVERY_NATIVE_CONTROL_FAILED',
  submitted:submissionObserved,controller_invoked:invoked,submit_implemented:submitImplementation};}
 await write('command-'+command.command_id+'-result.json',{version:'recovered-native-control-result-v1',command_id:command.command_id,control_action:command.control_action,result:outcome});
 await status(submissionObserved===null?'submit_unknown':'recovered_editor_waiting_for_operator',{last_command_id:command.command_id});
}
async function restoreExistingOnce(commandId){
 if(!await checkProtection())fail('RECOVERY_PROTECTION_REQUIRED');await identity();
 if(!(await page.locator('body').innerText()).includes('本地浏览器存在1个未提交的视频'))return false;
 const matches=page.getByText('继续编辑',{exact:true});let visible=[];for(let i=0;i<await matches.count();i++)if(await matches.nth(i).isVisible())visible.push(matches.nth(i));
 if(visible.length!==1)fail('RECOVERY_NATIVE_RESTORE_AMBIGUOUS');
 const h=await fs.open(path.join(output,'continue-editing-intent.json'),'wx',0o600);try{await h.writeFile(JSON.stringify({job_id:spec.job_id,attempt_id:spec.attempt_id,recovery_id:spec.recovery_id,command_id:commandId,new_file_selection:false,public_submit:false}));await h.sync();}finally{await h.close();}
 const d=await fs.open(output,'r');try{await d.sync();}finally{await d.close();}
 await visible[0].click({timeout:10000});await write('continue-editing-clicked.json',{clicked_at:new Date().toISOString(),existing_upload_continuation_possible:true,new_file_selection:false,public_submit:false});return true;
}

try{
 const allowed=['PATH','HOME','LANG','LC_ALL','TMP','TEMP','TMPDIR','PLAYWRIGHT_BROWSERS_PATH','XDG_CONFIG_HOME','XDG_CACHE_HOME'];
 const env=Object.fromEntries(allowed.filter(k=>process.env[k]!==undefined).map(k=>[k,process.env[k]]));
 context=await chromium.launchPersistentContext(profile,{headless:true,chromiumSandbox:true,channel:'chromium',env,viewport:{width:1360,height:1000},locale:'zh-CN',timezoneId:'Asia/Shanghai'});
 context.setDefaultTimeout(15000);await identity();page=context.pages()[0]||await context.newPage();
 page.on('filechooser',()=>{if(coverPending)return;unexpectedChooser=true;write('unexpected-native-filechooser.json',{state:'blocked_no_files_assigned',observed_at:new Date().toISOString()}).catch(()=>{});});
 await page.goto(spec.recovery_entry==='saved_draft_manager_v1'?'https://member.bilibili.com/platform/upload-manager/article':'https://member.bilibili.com/platform/upload/video/frame',{waitUntil:'domcontentloaded',timeout:45000});
 await page.waitForTimeout(1500);await inspect('initial-recovery');
 if(spec.recovery_entry!=='saved_draft_manager_v1'){
 const readyBy=Date.now()+45000;let restoreAttempted=false;
 while(Date.now()<readyBy&&!closed){
  const protection=await inspectVideoProtection(page);
  if(protection.blocked&&(protection.evidence?.visible_dialogs?.length||protection.evidence?.challenge_frames?.length||
   protection.evidence?.top_level_authentication||protection.evidence?.visible_challenge_controls?.some(x=>x.tag!=='BODY'))){await write('protection-required.json',protection);break;}
  const title=page.locator('input[placeholder="请输入稿件标题"]:visible');
  if(await title.count()===1){try{await verifyEditor();}catch{}if(editorReady)break;
   if((await title.inputValue())&&await title.inputValue()!==meta.title)break;}
  if(!restoreAttempted&&(await page.locator('body').innerText()).includes('本地浏览器存在1个未提交的视频')&&await checkProtection()){
   restoreAttempted=true;try{await restoreExistingOnce('startup');}catch{await status('native_restore_unknown');break;}
  }
  await page.waitForTimeout(500);
 }
 await inspect('recovered-native-editor');await status(editorReady?'recovered_editor_waiting_for_operator':'cached_editor_not_verified');
 }else await status('saved_draft_manager_waiting_for_operator',{editor_ready:false,requires_native_draft_navigation:true});
 const end=Date.now()+25*60*1000;
 while(!closed&&Date.now()<end){
  const f=path.join(output,'command.json');let command;
  try{const s=await fs.lstat(f);if(!s.isFile()||s.isSymbolicLink()||s.size>4096)fail('RECOVERY_COMMAND_INVALID');command=JSON.parse(await fs.readFile(f,'utf8'));}
  catch(e){if(e.code!=='ENOENT')throw e;}
  if(command){
   const keys=['version','command_id','recovery_id','action','created_at','expires_at'];if(command.action==='native_control')keys.push('control_sha256','control_action');
   if(JSON.stringify(Object.keys(command).sort())!==JSON.stringify(keys.sort())||command.version!=='short-video-recovered-editor-command-v1'||!UUID.test(command.command_id||'')||command.recovery_id!==spec.recovery_id||!['inspect','close','native_control'].includes(command.action)||
    command.action==='native_control'&&(!SHA.test(command.control_sha256||'')||!['fill_metadata','submit','lookup','sync_metadata_revision'].includes(command.control_action)))fail('RECOVERY_COMMAND_INVALID');
   const done=path.join(output,'command-'+command.command_id+'.processed.json');try{await fs.lstat(done);fail('RECOVERY_COMMAND_ALREADY_CONSUMED');}catch(e){if(e.code!=='ENOENT')throw e;}
   await fs.rename(f,done);
   const created=Date.parse(command.created_at),expires=Date.parse(command.expires_at),now=Date.now();
   if(!Number.isFinite(created)||!Number.isFinite(expires)||created>now+30000||expires<=created||expires-created>300000||created<now-300000||expires<=now){await write('command-'+command.command_id+'-result.json',{state:'command_expired',controller_invoked:false,submitted:submissionObserved});continue;}
   if(command.action==='native_control'){await nativeControl(command);continue;}
   if(command.action==='close'){await status('closed_by_operator');await close();break;}
   await inspect('operator-'+command.action);await status('recovered_editor_waiting_for_operator',{last_command_id:command.command_id});
  }
  await page.waitForTimeout(500);
 }
 if(!closed)await status('recovered_editor_window_expired');
}catch(error){
 const code=typeof error?.code==='string'&&/^RECOVERY_[A-Z_]+$/.test(error.code)?error.code:'RECOVERY_NATIVE_UI_UNAVAILABLE';
 if(page&&!page.isClosed())await inspect('error').catch(()=>{});await status('recovery_unknown',{error_code:code});
}finally{await close();}
