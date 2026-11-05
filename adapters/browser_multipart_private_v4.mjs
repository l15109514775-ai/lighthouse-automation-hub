// Two small, ordered real-source clips. Private upload/save only; no publish API.
import fs from 'node:fs/promises';
import path from 'node:path';
import readline from 'node:readline';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {inspectVideoProtection} from './bilibili_video_guard.mjs';
import {streamCheckedFile} from './replay_native_driver.mjs';
import {handleNativeControl as navigateSavedDraft} from './short_video_saved_draft_navigation_control.mjs';
import {canonicalDigest,readNativeDescription,handleNativeControl} from '../scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs';

const UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const SHA=/^[a-f0-9]{64}$/,ORIGIN='https://member.bilibili.com',EDITOR='/platform/upload/video/frame';
function fail(code){throw Object.assign(new Error(code),{code});}
const hash=bytes=>createHash('sha256').update(bytes).digest('hex');
async function safe(file){
 if(!path.isAbsolute(file)||path.resolve(file)!==file)fail('MULTIPART_PRIVATE_PATH_INVALID');
 for(let p=file;p!==path.dirname(p);p=path.dirname(p))if((await fs.lstat(p)).isSymbolicLink())fail('MULTIPART_PRIVATE_PATH_INVALID');
 if(await fs.realpath(file)!==file)fail('MULTIPART_PRIVATE_PATH_INVALID');return file;
}
async function directorySync(folder){if(process.platform!=='win32'){const h=await fs.open(folder,'r');try{await h.sync();}finally{await h.close();}}}
async function write(file,value,exclusive=false){
 const parent=path.dirname(file);await fs.mkdir(parent,{recursive:true,mode:0o700});await safe(parent);
 const target=exclusive?file:file+'.partial',h=await fs.open(target,exclusive?'wx':'w',0o600);
 try{await h.writeFile(JSON.stringify(value,null,2)+'\n');await h.sync();}finally{await h.close();}
 if(!exclusive)await fs.rename(target,file);await directorySync(parent);
}
async function bytesRef(file,max=131072){
 await safe(file);const h=await fs.open(file,'r');let raw;
 try{const b=await h.stat();if(!b.isFile()||b.size<1||b.size>max)fail('MULTIPART_PRIVATE_EVIDENCE_INVALID');raw=await h.readFile();const a=await h.stat();
  if(raw.length!==b.size||a.size!==b.size||a.mtimeMs!==b.mtimeMs||a.ctimeMs!==b.ctimeMs||a.ino!==b.ino)fail('MULTIPART_PRIVATE_EVIDENCE_CHANGED');
 }finally{await h.close();}return {raw,ref:{file,bytes:raw.length,sha256:hash(raw)}};
}
async function json(file){return JSON.parse((await bytesRef(file)).raw);}
export function validateMultipartPrivateManifest(s){
 const keys=['version','job_id','attempt_id','target_site_uid','source_id','source_url','metadata','metadata_sha256','sample_manifest','parts','parts_manifest_sha256','binding_sha256','human_review','publication_authorized','native_multipart_verified'];
 if(!s||Object.keys(s).sort().join()!==keys.sort().join()||s.version!=='multipart-private-native-manifest-v4'||![s.job_id,s.attempt_id].every(x=>UUID.test(x||''))||
  typeof s.target_site_uid!=='string'||!/^\d{1,30}$/.test(s.target_site_uid)||!/^[\w-]{11}$/.test(s.source_id||'')||s.source_url!=='https://www.youtube.com/watch?v='+s.source_id||
  ![s.metadata_sha256,s.parts_manifest_sha256,s.binding_sha256].every(x=>SHA.test(x||''))||canonicalDigest(s.metadata)!==s.metadata_sha256||
  s.human_review!==false||s.publication_authorized!==false||s.native_multipart_verified!==false||!Array.isArray(s.parts)||s.parts.length!==2||
  new Set(s.parts.map(p=>p.file)).size!==2||new Set(s.parts.map(p=>p.filename)).size!==2||canonicalDigest(s.parts)!==s.parts_manifest_sha256)fail('MULTIPART_PRIVATE_MANIFEST_INVALID');
 const m=s.metadata;
 if(!m||m.copyright!==2||m.source!==s.source_url||m.category_label!=='娱乐'||typeof m.title!=='string'||!m.title.trim()||m.title.length>80||
  typeof m.description!=='string'||!m.description.includes(s.source_url)||m.description.length>2000||!Array.isArray(m.tags)||m.tags.length<1||m.tags.length>10||
  new Set(m.tags).size!==m.tags.length||m.tags.some(t=>typeof t!=='string'||!t.trim()||t.length>20||t.includes(',')))fail('MULTIPART_PRIVATE_METADATA_INVALID');
 for(const [i,p] of s.parts.entries())if(Object.keys(p).sort().join()!=='bytes,duration_seconds,file,filename,index,sha256'||p.index!==i+1||
  !Number.isSafeInteger(p.bytes)||p.bytes<1||p.bytes>100000000||!SHA.test(p.sha256||'')||!Number.isFinite(p.duration_seconds)||p.duration_seconds<=0||p.duration_seconds>120||
  !path.isAbsolute(p.file||'')||path.basename(p.file)!==p.filename||!/^part-00[12]\.mp4$/.test(p.filename))fail('MULTIPART_PRIVATE_PART_INVALID');
 return s;
}
export async function observeMultipartPrivateProgress(page){
 return page.evaluate(()=>{
  const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';};
  const all=[...document.querySelectorAll('.file-item')],rows=all.filter(visible);
  return {url:location.origin+location.pathname,body_text:(document.body?.innerText||'').slice(0,30000),
   total_file_item_count:all.length,visible_file_item_count:rows.length,rows_truncated:rows.length>32,
   visible_rows:rows.slice(0,32).map((e,index)=>({index:index+1,classes:e.className,text:e.innerText.slice(0,2000),
    title_nodes:[...e.querySelectorAll('.title-text')].filter(visible).map(n=>({tag:n.tagName,classes:n.className,text:n.innerText,title:n.getAttribute('title')})),
    status_nodes:[...e.querySelectorAll('.file-item-content-status-text')].filter(visible).map(n=>({tag:n.tagName,classes:n.className,text:n.innerText,
     visible_children:[...n.children].filter(visible).map(x=>({tag:x.tagName,classes:x.getAttribute('class'),text:x instanceof HTMLElement?x.innerText:''}))}))})),
   uploaders:[...document.querySelectorAll('input[name="buploader"][type="file"]')].map(e=>({visible:visible(e),accept:e.accept,multiple:e.multiple,files:[...e.files].map(f=>({name:f.name,bytes:f.size}))}))};
 });
}
export async function createMultipartPrivateDriver({page,context,spec,root,uploadTimeoutMs=600000,saveTimeoutMs=15000}){
 validateMultipartPrivateManifest(spec);root=path.resolve(root);
 const parent=path.join(root,'media',spec.job_id,'multipart-private-v4'),output=path.join(parent,spec.attempt_id);
 await fs.mkdir(output,{recursive:true,mode:0o700});await safe(output);await write(path.join(output,'metadata.json'),spec.metadata);
 const base={job_id:spec.job_id,attempt_id:spec.attempt_id,site_uid:spec.target_site_uid,source_id:spec.source_id,source_url:spec.source_url,
  binding_sha256:spec.binding_sha256,metadata_sha256:spec.metadata_sha256,parts_manifest_sha256:spec.parts_manifest_sha256};
 let busy=false,permittedChooser=false,unexpectedChooser=false;
 let progressCount=0,stallImage=false;
 async function diagnostic(reason,{image=false}={}){
  const body={version:'multipart-private-native-progress-v4',...base,reason,observed_at:new Date().toISOString(),
   native:await observeMultipartPrivateProgress(page),protection:await inspectVideoProtection(page),read_only:true};
  await write(path.join(output,'upload-progress.json'),body);
  if(progressCount<60){await write(path.join(output,'progress',String(progressCount++).padStart(3,'0')+'.json'),body,true);}
  if(image){const file=path.join(output,reason==='upload-waiting'?'upload-waiting.png':'last-observation.png');await page.screenshot({path:file,fullPage:true,timeout:10000});const h=await fs.open(file,'r+');try{await h.sync();}finally{await h.close();}await directorySync(output);}
 }
 page.on('filechooser',()=>{if(!permittedChooser)unexpectedChooser=true;});
 async function guard(){
  const u=new URL(page.url());if(u.origin!==ORIGIN||u.pathname!==EDITOR||u.search||u.hash||unexpectedChooser)fail('MULTIPART_PRIVATE_ORIGIN_CHANGED');
  const p=await inspectVideoProtection(page);if(p.blocked){await write(path.join(output,'protection-required.json'),p);fail(p.error_code);}return true;
 }
 async function identity(){
  const p=await context.newPage();try{await p.goto('https://t.bilibili.com/',{waitUntil:'domcontentloaded',timeout:45000});const a=p.locator('a.header-entry-mini:visible');
   await a.waitFor({state:'visible',timeout:30000});if(await a.count()!==1)fail('MULTIPART_PRIVATE_UID_AMBIGUOUS');
   const u=new URL(await a.getAttribute('href'),'https://t.bilibili.com/');if(u.origin!=='https://space.bilibili.com'||u.username||u.password||u.search||u.hash||/^\/(\d+)\/?$/.exec(u.pathname)?.[1]!==spec.target_site_uid)fail('TARGET_ACCOUNT_IDENTITY_MISMATCH');
   return spec.target_site_uid;
  }finally{await p.close();}
 }
 async function verify(){await guard();await identity();await guard();}
 async function unique(l){if(await l.count()!==1)fail('MULTIPART_PRIVATE_CONTROL_AMBIGUOUS');return l;}
 async function checkedParts(){
  const r=spec.sample_manifest;if(!r||Object.keys(r).sort().join()!=='bytes,file,sha256'||!SHA.test(r.sha256||'')||!Number.isSafeInteger(r.bytes)||r.bytes<1||r.bytes>131072)fail('MULTIPART_PRIVATE_SAMPLE_INVALID');
  const {raw,ref}=await bytesRef(r.file);if(canonicalDigest(ref)!==canonicalDigest(r))fail('MULTIPART_PRIVATE_SAMPLE_CHANGED');
  const sample=JSON.parse(raw);if(sample.version!=='real-source-private-multipart-samples-v4'||sample.source_id!==spec.source_id||sample.private_upload_only!==true||sample.public_submission_allowed!==false||sample.formal_splitter_acceptance_claimed!==false||sample.method!=='ffmpeg_stream_copy'||sample.parts?.length!==2)fail('MULTIPART_PRIVATE_SAMPLE_INVALID');
  const streamed=[];
  for(const [i,p] of spec.parts.entries()){
   const source=sample.parts[i];if(source.path!==p.file||source.bytes!==p.bytes||source.sha256!==p.sha256||Number(source.probe?.format?.duration)!==p.duration_seconds||!p.file.startsWith(path.join(root,'media')+path.sep))fail('MULTIPART_PRIVATE_SAMPLE_BINDING_CHANGED');
   streamed.push(await streamCheckedFile(p.file,p.sha256,p.bytes,'replay-highest-native-browser-manifest-v2'));
  }return streamed;
 }
 async function rejectIntent(kind){try{await fs.lstat(path.join(parent,kind+'-intent.json'));fail('MULTIPART_PRIVATE_RECONCILE_REQUIRED');}catch(e){if(e.code!=='ENOENT')throw e;}}
 async function intent(kind){try{await write(path.join(parent,kind+'-intent.json'),{version:'multipart-private-one-use-intent-v4',...base,kind,parts:spec.parts,created_at:new Date().toISOString()},true);}catch(e){if(e.code==='EEXIST')fail('MULTIPART_PRIVATE_RECONCILE_REQUIRED');throw e;}}
 async function rows(){
  const all=page.locator('.file-item:visible'),out=[];
  for(let i=0;i<await all.count();i++){
   const row=all.nth(i),name=await unique(row.locator('.title-text:visible'));
   const text=(await name.innerText()).trim(),attribute=await name.getAttribute('title');
   if(attribute!==null&&attribute!==text)fail('MULTIPART_PRIVATE_NATIVE_FILENAME_TRUNCATED');
   const status=row.locator('.file-item-content-status-text .success:visible');
   out.push({index:i+1,native_filename:text,native_upload_complete:await status.count()===1&&(await status.innerText()).trim()==='上传完成'});
  }return out;
 }
 function matchRows(r,{metadata=false}={}){
  if(r.length!==2||r.some((n,i)=>!n.native_upload_complete||![spec.parts[i].filename,spec.parts[i].filename.replace(/\.mp4$/i,''),...(metadata&&i===0?[spec.metadata.title]:[])].includes(n.native_filename)))fail('MULTIPART_PRIVATE_NATIVE_ORDER_MISMATCH');return r;
 }
 function receipt(kind,ordered,extra={}){return {version:'multipart-private-'+kind+'-v4',...base,ordered_parts:ordered,observed_at:new Date().toISOString(),human_review:false,publication_authorized:false,server_file_hash_verified:false,native_multipart_verified:false,...extra};}
 async function persist(name,body){const file=path.join(output,name);await write(file,body);return (await bytesRef(file)).ref;}
 async function loadedUpload(){
  const r=await json(path.join(output,'upload-receipt.json'));
  if(r.version!=='multipart-private-upload-v4'||Object.entries(base).some(([k,v])=>r[k]!==v)||!Array.isArray(r.ordered_parts)||r.ordered_parts.length!==2||r.publication_authorized!==false||r.native_multipart_verified!==false||r.human_review!==false||r.server_file_hash_verified!==false)fail('MULTIPART_PRIVATE_UPLOAD_RECEIPT_CHANGED');
  const proof=await json(path.join(parent,'video-selection-intent.json'));
  if(proof.kind!=='video-selection'||Object.entries(base).some(([k,v])=>proof[k]!==v)||canonicalDigest(proof.parts)!==spec.parts_manifest_sha256)fail('MULTIPART_PRIVATE_UPLOAD_RECEIPT_CHANGED');
  for(const [i,p] of spec.parts.entries()){const n=r.ordered_parts[i];if(Object.entries(p).filter(([k])=>k!=='file').some(([k,v])=>n[k]!==v)||![p.filename,p.filename.replace(/\.mp4$/i,'')].includes(n.native_filename)||n.whole_file_streamed!==true||n.streamed_bytes!==p.bytes||n.streamed_sha256!==p.sha256||n.native_upload_complete!==true)fail('MULTIPART_PRIVATE_UPLOAD_RECEIPT_CHANGED');}
  return r;
 }
 async function metadataReadback(){
  await guard();const m=spec.metadata,title=await (await unique(page.locator('input[placeholder="请输入稿件标题"]:visible'))).inputValue();
  const description=await readNativeDescription(await unique(page.locator('.ql-editor:visible')));
  const creation_statement=await (await unique(page.locator('input[placeholder="请选择符合您视频内容的创作声明"]:visible'))).inputValue();
  const source=await (await unique(page.locator('input[placeholder^="转载视频请注明来源"]:visible'))).inputValue();
  const category=(await (await unique(page.locator('.video-human-type .select-controller:visible'))).innerText()).trim();
  const tags=(await page.locator('.label-item-v2-container:visible p.label-item-v2-content').allTextContents()).map(t=>t.trim());
  if(title!==m.title||!description.supported||description.text!==m.description||creation_statement!=='内容为转载'||source!==spec.source_url||category!=='娱乐'||tags.length!==m.tags.length||new Set(tags).size!==tags.length||m.tags.some(t=>!tags.includes(t)))fail('MULTIPART_PRIVATE_METADATA_READBACK_FAILED');
  return {title,description:description.text,creation_statement,source,category,tags};
 }
 async function upload(){
  await rejectIntent('video-selection');await checkedParts();await page.goto(ORIGIN+EDITOR,{waitUntil:'domcontentloaded',timeout:45000});
  const area=page.locator('.bcc-upload .upload-area:visible');await area.waitFor({state:'visible',timeout:30000});await verify();
  if(await page.getByText('继续编辑',{exact:true}).filter({visible:true}).count()||await page.locator('.file-item:visible').count()||await page.locator('input[placeholder="请输入稿件标题"]:visible').count())fail('MULTIPART_PRIVATE_EXISTING_DRAFT');
  await unique(area);await checkedParts();await guard();await intent('video-selection');await guard();let chooser;permittedChooser=true;
  try{[chooser]=await Promise.all([page.waitForEvent('filechooser',{timeout:5000}),area.click({timeout:5000})]);}finally{permittedChooser=false;}
  const el=chooser.element(),type=await el.evaluate(e=>({tag:e.tagName,type:e.type,accept:e.accept,multiple:e.multiple}));
  if(type.tag!=='INPUT'||type.type!=='file'||type.multiple!==true||!chooser.isMultiple()||!type.accept.toLowerCase().split(',').map(x=>x.trim()).includes('.mp4')||/\.txt|image\//i.test(type.accept))fail('MULTIPART_PRIVATE_CHOOSER_INVALID');
  await verify();await checkedParts();await guard();await chooser.setFiles(spec.parts.map(p=>p.file),{timeout:30000});
  const selected=await el.evaluate(e=>[...e.files].map(f=>({filename:f.name,bytes:f.size})));
  if(canonicalDigest(selected)!==canonicalDigest(spec.parts.map(p=>({filename:p.filename,bytes:p.bytes}))))fail('MULTIPART_PRIVATE_SELECTED_FILES_MISMATCH');
  const started=Date.now(),end=started+uploadTimeoutMs;let native,nextProgress=0;
  do{await guard();native=await rows();if(native.length>2)fail('MULTIPART_PRIVATE_NATIVE_ORDER_MISMATCH');if(native.length===2&&native.every(n=>n.native_upload_complete))break;
   if(Date.now()>=nextProgress){const image=!stallImage&&Date.now()-started>=15000;await diagnostic('upload-waiting',{image});stallImage||=image;nextProgress=Date.now()+10000;}
   if(Date.now()>=end)fail('MULTIPART_PRIVATE_UPLOAD_UNKNOWN');await page.waitForTimeout(200);}while(true);
  matchRows(native);await verify();const streams=await checkedParts();
  const ordered=spec.parts.map(({file,...p},i)=>({...p,...native[i],whole_file_streamed:streams[i].whole_file_streamed,streamed_bytes:streams[i].streamed_bytes,streamed_sha256:streams[i].streamed_sha256}));
  const upload_receipt=receipt('upload',ordered,{metadata_verified:false});const receipt_file=await persist('upload-receipt.json',upload_receipt);
  return {state:'private_parts_uploaded',upload_receipt,receipt_file};
 }
 async function save(){
  await rejectIntent('private-save');const upload=await loadedUpload();await checkedParts();await verify();const before=matchRows(await rows());
  const title=await unique(page.locator('input[placeholder="请输入稿件标题"]:visible')),description=await unique(page.locator('.ql-editor:visible'));
  if(!await description.evaluate(e=>e.getAttribute('contenteditable')==='true'))fail('MULTIPART_PRIVATE_DESCRIPTION_INVALID');
  await guard();await title.fill(spec.metadata.title);await guard();await description.fill(spec.metadata.description);await guard();await description.blur();
  const filled=await handleNativeControl({page,spec:{...spec,recovery_id:spec.attempt_id},output,control_action:'fill_metadata',helpers:{checkProtection:guard,accountIdentity:identity,checkedFile:checkedParts,write:(name,value)=>write(path.join(output,name),value),inventory:async()=>({})}});
  if(filled.text_metadata_verified!==true)fail(filled.error_code||'MULTIPART_PRIVATE_METADATA_FAILED');
  const readback=await metadataReadback(),after=matchRows(await rows(),{metadata:true});
  const button=await unique(page.locator('span.submit-draft:visible').filter({hasText:/^\s*存草稿\s*$/}));
  if(await button.isDisabled()||await button.getAttribute('aria-disabled')==='true')fail('MULTIPART_PRIVATE_DRAFT_SAVE_UNAVAILABLE');
  const toast=page.getByText('已存入草稿箱',{exact:true}).filter({visible:true});if(await toast.count())fail('MULTIPART_PRIVATE_STALE_DRAFT_TOAST');
  await verify();await metadataReadback();await checkedParts();await guard();await intent('private-save');await guard();await button.click({timeout:5000});
  const end=Date.now()+saveTimeoutMs;do{
   const protection=await inspectVideoProtection(page);if(protection.blocked)fail(protection.error_code);
   const u=new URL(page.url());if(u.origin!==ORIGIN||![EDITOR,'/platform/upload-manager/article'].includes(u.pathname))fail('MULTIPART_PRIVATE_SAVE_UNKNOWN');
   if(await toast.count()===1)break;if(Date.now()>=end)fail('MULTIPART_PRIVATE_SAVE_UNKNOWN');await page.waitForTimeout(100);
  }while(true);
  await identity();const screenshot=path.join(output,'private-draft.png');await page.screenshot({path:screenshot,fullPage:true,timeout:15000});
  const image=await fs.open(screenshot,'r+');try{await image.sync();}finally{await image.close();}await directorySync(output);
  const screenshot_ref=(await bytesRef(screenshot,5000000)).ref;
  const draft_receipt=receipt('draft',upload.ordered_parts,{private_draft_saved:true,metadata_verified:true,metadata_readback:readback,title:readback.title,source:readback.source,native_rows_before:before,native_rows_after:after,screenshot:screenshot_ref});
  const receipt_file=await persist('draft-receipt.json',draft_receipt);return {state:'private_draft_saved',draft_receipt,receipt_file};
 }
 async function inspect(){
  const upload=await loadedUpload();await checkedParts();
  const current=new URL(page.url()),editorTitle=page.locator('input[placeholder="请输入稿件标题"]:visible');
  const titleValue=await editorTitle.count()===1?await editorTitle.inputValue():null;
  const alreadyEditor=current.origin===ORIGIN&&current.pathname===EDITOR&&!current.search&&!current.hash&&
   [spec.metadata.title,spec.parts[0].filename,spec.parts[0].filename.replace(/\.mp4$/i,'')].includes(titleValue);
  let navigation=null;
  if(!alreadyEditor){
   await page.goto(ORIGIN+'/platform/upload-manager/article',{waitUntil:'domcontentloaded',timeout:45000});
   const navigationGuard=async()=>{
    const u=new URL(page.url());if(u.origin!==ORIGIN||![EDITOR,'/platform/upload-manager/article'].includes(u.pathname)||u.search||u.hash||unexpectedChooser)fail('MULTIPART_PRIVATE_ORIGIN_CHANGED');
    const protection=await inspectVideoProtection(page);if(protection.blocked)fail(protection.error_code);return true;
   };
   navigation=await navigateSavedDraft({page,spec:{...spec,recovery_id:spec.attempt_id},output,control_action:'lookup',helpers:{checkProtection:navigationGuard,accountIdentity:identity,write:(name,value)=>write(path.join(output,name),value),inventory:async()=>({})}});
   if(navigation.state!=='saved_draft_editor_opened')fail('MULTIPART_PRIVATE_DRAFT_LOOKUP_NOT_CONFIRMED');
  }
  await verify();const native=matchRows(await rows(),{metadata:true});
  let readback=null;
  if(titleValue===spec.metadata.title||navigation)readback=await metadataReadback();
  const observation=receipt('observation',upload.ordered_parts,{native_rows:native,metadata_verified:Boolean(readback),metadata_readback:readback,
   private_draft_saved:false,recovered_without_modification:Boolean(navigation),native_navigation:navigation});
  const receipt_file=await persist('inspect-observation.json',observation);return {state:'private_parts_observed',observation,receipt_file};
 }
 return {output,async perform(operation,args={}){if(busy||Object.keys(args).length||!['private_upload','save','inspect'].includes(operation))fail('MULTIPART_PRIVATE_OPERATION_INVALID');busy=true;try{return await {private_upload:upload,save,inspect}[operation]();}catch(e){await diagnostic('operation-error',{image:true}).catch(()=>{});throw e;}finally{busy=false;}}};
}

// Running the entry requires the same fixed app identity/profile/sandbox as the
// regular native child. Importing only exposes the private owned-test factory.
async function main(){
 process.umask(0o077);const root=path.resolve(process.env.HUB_ROOT||'/opt/lighthouse-hub');
 const lines=readline.createInterface({input:process.stdin,crlfDelay:Infinity});let context=null,opening=null,closed=false,closing=null;
 const reply=v=>process.stdout.write(JSON.stringify(v)+'\n');
 const close=()=>{if(closing)return closing;closed=true;closing=(async()=>{const c=context||(opening?await opening.catch(()=>null):null);await c?.close();lines.close();})();return closing;};
 lines.once('close',()=>{void close();});process.on('SIGTERM',()=>close().finally(()=>process.exit(0)));process.on('SIGINT',()=>close().finally(()=>process.exit(0)));
 const deadline=setTimeout(()=>close().finally(()=>process.exit(2)),30*60000);deadline.unref();
 try{
  const iterator=lines[Symbol.asyncIterator](),first=await iterator.next();if(first.done||Buffer.byteLength(first.value)>131072)fail('MULTIPART_PRIVATE_MANIFEST_INVALID');
  const spec=validateMultipartPrivateManifest(JSON.parse(first.value));
  if(typeof process.getuid!=='function'||process.getuid()!==10001||process.getgid()!==10001||spec.target_site_uid!==process.env.BILIBILI_EXPECTED_SITE_UID||process.env.HUB_BROWSER_CHANNEL!=='chromium')fail('MULTIPART_PRIVATE_RUNTIME_IDENTITY_INVALID');
  const profile=path.join(root,'data/browser-profiles/bilibili-video');await safe(profile);const st=await fs.stat(profile);
  if(!st.isDirectory()||st.uid!==10001||st.gid!==10001||st.mode&0o077)fail('MULTIPART_PRIVATE_PROFILE_INVALID');
  if(closed)fail('MULTIPART_PRIVATE_INPUT_CLOSED');opening=chromium.launchPersistentContext(profile,{headless:true,channel:'chromium',chromiumSandbox:true,viewport:{width:1280,height:960},acceptDownloads:false});context=await opening;
  if(closed){await close();fail('MULTIPART_PRIVATE_INPUT_CLOSED');}const page=context.pages()[0]||await context.newPage(),driver=await createMultipartPrivateDriver({page,context,spec,root});
  reply({version:'replay-native-browser-response-v1',state:'ready',job_id:spec.job_id,attempt_id:spec.attempt_id,output:driver.output});const seen=new Set();
  while(!closed){const next=await iterator.next();if(next.done)break;let request;
   try{
    if(Buffer.byteLength(next.value)>16384)fail('MULTIPART_PRIVATE_REQUEST_INVALID');request=JSON.parse(next.value);
    if(!request||Object.keys(request).sort().join()!=='arguments,operation,request_id,version'||request.version!=='replay-native-browser-request-v1'||!UUID.test(request.request_id||'')||seen.has(request.request_id)||!['private_upload','save','inspect'].includes(request.operation)||!request.arguments||typeof request.arguments!=='object'||Array.isArray(request.arguments)||Object.keys(request.arguments).length)fail('MULTIPART_PRIVATE_REQUEST_INVALID');
    seen.add(request.request_id);const result=await driver.perform(request.operation,request.arguments);reply({version:'replay-native-browser-response-v1',request_id:request.request_id,operation:request.operation,ok:true,result});
   }catch(e){reply({version:'replay-native-browser-response-v1',request_id:request?.request_id||null,operation:request?.operation||null,ok:false,error_code:/^[A-Z][A-Z0-9_]{1,100}$/.test(e?.code||'')?e.code:'MULTIPART_PRIVATE_REQUEST_FAILED'});}
  }
 }catch(e){reply({version:'replay-native-browser-response-v1',state:'unavailable',ok:false,error_code:/^[A-Z][A-Z0-9_]{1,100}$/.test(e?.code||'')?e.code:'MULTIPART_PRIVATE_RUNTIME_FAILED'});process.exitCode=2;}
 finally{clearTimeout(deadline);await close();}
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url))await main();
