// Native UI controls v3: select exact bound repost statement; inspect category/tags/cover only.
import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash} from 'node:crypto';

export function canonicalDigest(value){
 function canonical(item){
  if(Array.isArray(item))return '['+item.map(canonical).join(',')+']';
  if(item!==null&&typeof item==='object')return '{'+Object.keys(item).sort().map(key=>JSON.stringify(key)+':'+canonical(item[key])).join(',')+'}';
  return JSON.stringify(item);
 }
 return createHash('sha256').update(canonical(value),'utf8').digest('hex');
}
function fail(code){throw Object.assign(new Error(code),{code});}
function normalText(value){return value.replace(/\r\n/g,'\n');}
export async function readNativeDescription(locator){
 return locator.evaluate(editor=>{
  const blocks=[];const inlineTags=new Set(['SPAN','A','STRONG','B','EM','I','U','S','DEL','CODE']);
  function unsupported(){throw new Error('UNSUPPORTED_NATIVE_DESCRIPTION_STRUCTURE');}
  function inline(node){
   if(node.nodeType===Node.TEXT_NODE)return node.nodeValue.replace(/\r\n/g,'\n');
   if(node.nodeType===Node.COMMENT_NODE)return '';
   if(node.nodeType!==Node.ELEMENT_NODE)unsupported();
   if(node.tagName==='BR')return '\n';
   if(!inlineTags.has(node.tagName)||node.getAttribute('contenteditable')==='false'||node.classList.contains('ql-cursor'))unsupported();
   const style=getComputedStyle(node);if(style.display==='none'||style.visibility==='hidden')unsupported();
   return [...node.childNodes].map(inline).join('');
  }
  try{
   const nodes=[...editor.childNodes].filter(n=>n.nodeType!==Node.COMMENT_NODE&&!(n.nodeType===Node.TEXT_NODE&&n.nodeValue===''));
   const blockMode=nodes.some(n=>n.nodeType===Node.ELEMENT_NODE&&['P','DIV'].includes(n.tagName));
   if(blockMode){
    for(const node of nodes){
     if(node.nodeType!==Node.ELEMENT_NODE||!['P','DIV'].includes(node.tagName))unsupported();
     const children=[...node.childNodes].filter(n=>n.nodeType!==Node.COMMENT_NODE&&!(n.nodeType===Node.TEXT_NODE&&n.nodeValue===''));
     const text=children.length===1&&children[0].nodeType===Node.ELEMENT_NODE&&children[0].tagName==='BR'?'':children.map(inline).join('');
     blocks.push({tag:node.tagName,text,child_tags:children.map(n=>n.nodeType===Node.TEXT_NODE?'#text':n.tagName)});
    }
    return {supported:true,text:blocks.map(b=>b.text).join('\n'),blocks,serialization:'native_P_DIV_block_sequence_preserving_empty_blocks'};
   }
   return {supported:true,text:nodes.map(inline).join(''),blocks:[],serialization:'native_inline_text_and_BR_sequence'};
  }catch{return {supported:false,error_code:'SHORT_VIDEO_NATIVE_DESCRIPTION_STRUCTURE_UNSUPPORTED',
   direct_child_tags:[...editor.childNodes].map(n=>n.nodeType===Node.TEXT_NODE?'#text':n.tagName||'#other').slice(0,40)};}
 });
}


const digest=canonicalDigest;
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
async function checkedCoverDerivation(cover,{root,spec,old}){
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
const UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/,SHA=/^[a-f0-9]{64}$/;
async function readJSON(file,limit=65536){const st=await fs.lstat(file);if(!st.isFile()||st.isSymbolicLink()||st.size>limit)fail('NATIVE_BOUND_FILE_INVALID');return JSON.parse(await fs.readFile(file,'utf8'));}
function paths(spec,output){const root=path.resolve(process.env.HUB_ROOT||'/opt/lighthouse-hub');if(![spec.job_id,spec.attempt_id,spec.recovery_id].every(x=>UUID.test(x||''))||output!==path.join(root,'media',spec.job_id,'recovered-editors',spec.recovery_id))fail('NATIVE_OUTPUT_BINDING_INVALID');return {root,old:path.join(root,'media',spec.job_id,'short-video-attempts',spec.attempt_id)};}
async function guard(page,spec,helpers){const u=new URL(page.url());if(u.origin!=='https://member.bilibili.com'||u.pathname!=='/platform/upload/video/frame')fail('NATIVE_ORIGIN_CHANGED');if(!await helpers.checkProtection())fail('NATIVE_PROTECTION_REQUIRED');if(await helpers.accountIdentity()!==spec.target_site_uid)fail('NATIVE_ACCOUNT_CHANGED');await helpers.checkedFile();}
async function unique(locator){if(await locator.count()!==1)fail('NATIVE_CONTROL_AMBIGUOUS');return locator;}
async function metadataReadback(page,spec,output){
 const meta=await readJSON(path.join(output,'metadata.json'),20000);if(digest(meta)!==spec.metadata_sha256||meta.source!==spec.source_url||meta.copyright!==2)fail('NATIVE_METADATA_BINDING_INVALID');
 const title=await unique(page.locator('input[placeholder="请输入稿件标题"]:visible')),desc=await unique(page.locator('.ql-editor[contenteditable="true"]:visible'));
 const description=await readNativeDescription(desc),source=await unique(page.locator('input[placeholder^="转载视频请注明来源"]:visible'));
 const category=await unique(page.locator('.video-human-type .select-controller:visible')),statement=await unique(page.locator('input[placeholder="请选择符合您视频内容的创作声明"]:visible'));
 const tags=(await page.locator('.label-item-v2-container:visible p.label-item-v2-content').allTextContents()).map(x=>x.trim());
 if(await title.inputValue()!==meta.title||!description.supported||description.text!==meta.description||await source.inputValue()!==meta.source||
  (await category.innerText()).trim()!==meta.category_label||(await statement.inputValue()).trim()!=='内容为转载'||tags.length!==meta.tags.length||new Set(tags).size!==tags.length||meta.tags.some(x=>!tags.includes(x)))fail('NATIVE_METADATA_READBACK_MISMATCH');
 return {title:meta.title,description:description.text,source:meta.source,category:meta.category_label,tags,creation_statement:'内容为转载'};
}
async function coverProof(spec,output){
 const location=paths(spec,output),cover=spec.cover,v4=spec.cover_derivation?.version==='derived-cover-binding-v2';if(!cover||!SHA.test(cover.sha256||'')||cover.file!==path.join(location.root,'media',spec.job_id,v4?'cover-assets-v4/top-bottom-blur-cover.png':'cover-assets-v3/blur-safe-cover.jpg'))fail('NATIVE_DERIVED_COVER_REQUIRED');
 await checkedCoverDerivation(cover,{...location,spec});
 const stat=await fs.stat(cover.file);if(!stat.isFile()||!Number.isSafeInteger(cover.bytes)||cover.bytes<4||cover.bytes>(v4?5000000:1000000)||stat.size!==cover.bytes)fail('NATIVE_DERIVED_COVER_CHANGED');
 const image=await fs.readFile(cover.file),dims=v4?pngDimensions(image):jpegDimensions(image);if(image.length!==cover.bytes||createHash('sha256').update(image).digest('hex')!==cover.sha256||dims?.width!==1280||dims?.height!==(v4?960:720))fail('NATIVE_DERIVED_COVER_CHANGED');
 const observation=await readJSON(path.join(output,'cover-selection-observation.json'));
 if(observation.version!=='bound-native-cover-selection-v1'||observation.files_assigned!==true||observation.server_file_hash_verified!==false||digest(observation.cover)!==digest(cover)||digest(observation.cover_derivation)!==digest(spec.cover_derivation))fail('NATIVE_COVER_SELECTION_RECEIPT_REQUIRED');
 return {local_cover_sha256:cover.sha256,provenance_sha256:spec.cover_derivation.provenance_sha256,server_file_hash_verified:false,image_edited:true,no_text_regenerated:true,official_original_bytes:false};
}
async function reserveIntent(spec,output,kind,extra={}){
 const {root}=paths(spec,output),folder=path.join(root,'media',spec.job_id,'native-control-intents');await fs.mkdir(folder,{mode:0o700,recursive:true});
 for(let p=folder;p!==path.dirname(p);p=path.dirname(p))if((await fs.lstat(p)).isSymbolicLink())fail('NATIVE_INTENT_PATH_INVALID');
 const key=digest([spec.attempt_id,spec.metadata_sha256,spec.cover.sha256]),file=path.join(folder,kind+'-'+key+'.json');
 const h=await fs.open(file,'wx',0o600);try{await h.writeFile(JSON.stringify({version:'short-native-control-intent-v1',kind,job_id:spec.job_id,attempt_id:spec.attempt_id,recovery_id:spec.recovery_id,metadata_sha256:spec.metadata_sha256,cover_sha256:spec.cover.sha256,created_at:new Date().toISOString(),new_video_file_selection:false,...extra}));await h.sync();}finally{await h.close();}
 const directory=await fs.open(folder,'r');try{await directory.sync();}finally{await directory.close();}return file;
}
async function hasIntent(spec,output,kind){
 const {root}=paths(spec,output);if(!SHA.test(spec.cover?.sha256||''))fail('NATIVE_DERIVED_COVER_REQUIRED');
 const key=digest([spec.attempt_id,spec.metadata_sha256,spec.cover.sha256]),file=path.join(root,'media',spec.job_id,'native-control-intents',kind+'-'+key+'.json');
 try{const s=await fs.lstat(file);if(!s.isFile()||s.isSymbolicLink())fail('NATIVE_INTENT_PATH_INVALID');return true;}catch(e){if(e.code==='ENOENT')return false;throw e;}
}
async function coverRegion(page){return page.evaluate(()=>{
 function visible(e){const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>1&&r.height>1&&s.display!=='none'&&s.visibility!=='hidden';}
 const areas=[...document.querySelectorAll('.cover-main .cover-module-main')].filter(visible);
 return {regions:areas.length,preview_present:areas.some(a=>[...a.querySelectorAll('img')].some(e=>visible(e)&&e.naturalWidth>0)||[...a.querySelectorAll('canvas')].some(e=>visible(e)&&e.width>0&&e.height>0)||[a,...a.querySelectorAll('*')].some(e=>visible(e)&&getComputedStyle(e).backgroundImage!=='none')),
  placeholder_present:areas.some(a=>[...a.querySelectorAll('*')].some(e=>visible(e)&&e.children.length===0&&(e.innerText||'').trim()==='添加封面'))};});}
function safeError(e){return typeof e?.code==='string'&&/^(NATIVE_|RECOVERY_)[A-Z_]+$/.test(e.code)?e.code:e?.code==='EEXIST'?'NATIVE_INTENT_ALREADY_EXISTS':'NATIVE_UI_OPERATION_FAILED';}
async function firstResult(output,name,value){
 let file;try{file=await fs.open(path.join(output,name),'wx',0o600);}catch(e){if(e.code==='EEXIST')return;throw e;}
 try{await file.writeFile(JSON.stringify(value,null,2));await file.sync();}finally{await file.close();}
}
export async function completeCover({page,spec,output,helpers}){
 let reserved=false,clicked=false;const base={version:'native-short-cover-complete-v1',job_id:spec.job_id,attempt_id:spec.attempt_id,recovery_id:spec.recovery_id,metadata_sha256:spec.metadata_sha256,submitted:false,submit_implemented:false,new_video_file_selection:false};
 try{
  if(await hasIntent(spec,output,'cover-complete'))return {...base,state:'cover_complete_reconcile_required',error_code:'NATIVE_INTENT_ALREADY_EXISTS',native_click_performed:false,repeat_forbidden:true};
  await guard(page,spec,helpers);const metadata=await metadataReadback(page,spec,output),cover=await coverProof(spec,output);
  const editor=await unique(page.locator('.cover-editor.bcc-dialog__wrap-mask:visible'));if(!(await editor.innerText()).includes('封面制作'))fail('NATIVE_COVER_EDITOR_CHANGED');
  const button=await unique(editor.locator('.cover-editor-button .button:visible').filter({hasText:/^\s*完成\s*$/}));
  if((await button.innerText()).trim()!=='完成'||!await button.isEnabled()||await button.getAttribute('aria-disabled')==='true')fail('NATIVE_COVER_COMPLETE_DISABLED');
  await reserveIntent(spec,output,'cover-complete',{public_submit:false});reserved=true;
  await button.click({timeout:5000});clicked=true;await helpers.write('native-cover-complete-clicked.json',{...base,clicked_at:new Date().toISOString(),native_click_performed:true});
  const end=Date.now()+8000;let region;do{if(!await helpers.checkProtection())fail('NATIVE_PROTECTION_REQUIRED');region=await coverRegion(page);if(await editor.count()===0&&region.regions===1&&region.preview_present&&!region.placeholder_present)break;await page.waitForTimeout(100);}while(Date.now()<end);
  if(await editor.count()!==0||region.regions!==1||!region.preview_present||region.placeholder_present)fail('NATIVE_COVER_APPLICATION_UNKNOWN');
  const result={...base,state:'cover_complete_applied',native_click_performed:true,metadata_readback:metadata,cover,visible_cover_region:region,remote_cover_bytes_verified:false};await firstResult(output,'native-cover-complete-result.json',result);await helpers.inventory('native-cover-complete-applied');return result;
 }catch(e){const consumed=reserved||await hasIntent(spec,output,'cover-complete').catch(()=>false);const result={...base,state:e?.code==='EEXIST'||consumed&&!reserved?'cover_complete_reconcile_required':reserved?'cover_complete_unknown':'blocked',error_code:safeError(e),native_click_performed:clicked?true:reserved?null:false,repeat_forbidden:consumed||e?.code==='EEXIST'};if(reserved)await firstResult(output,'native-cover-complete-result.json',result);else await helpers.write('native-cover-complete-validation.json',result);await helpers.inventory('native-cover-complete-blocked').catch(()=>{});return result;}
}
async function visibleSaveObservation(page){return page.evaluate(()=>{
 const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>1&&r.height>1&&s.display!=='none'&&s.visibility!=='hidden';};
 const messages=[...document.querySelectorAll('body *')].filter(e=>visible(e)&&e.children.length===0).map(e=>(e.innerText||'').trim()).filter(t=>/^(?:保存成功|保存草稿成功|草稿保存成功|已保存草稿|已保存至草稿箱|已存入草稿箱)$/.test(t));
 return {observed_at:new Date().toISOString(),url:new URL(location.href).origin+new URL(location.href).pathname,success_messages:[...new Set(messages)],visible_text:(document.body?.innerText||'').slice(0,20000)};
 });}
export async function savePrivateDraft({page,spec,output,helpers}){
 let reserved=false,clicked=false;const base={version:'native-short-private-draft-v1',job_id:spec.job_id,attempt_id:spec.attempt_id,recovery_id:spec.recovery_id,metadata_sha256:spec.metadata_sha256,cover_sha256:spec.cover?.sha256,
  draft_id:null,draft_id_verified:false,submitted:false,submit_implemented:false,new_video_file_selection:false};
 try{
  if(await hasIntent(spec,output,'draft-save'))return {...base,state:'private_draft_reconcile_required',native_click_performed:false,repeat_forbidden:true};
  await guard(page,spec,helpers);await metadataReadback(page,spec,output);await coverProof(spec,output);
  const completed=await readJSON(path.join(output,'native-cover-complete-result.json'));
  if(completed.state!=='cover_complete_applied'||completed.job_id!==spec.job_id||completed.attempt_id!==spec.attempt_id||completed.recovery_id!==spec.recovery_id||completed.metadata_sha256!==spec.metadata_sha256||completed.cover?.local_cover_sha256!==spec.cover.sha256)fail('NATIVE_COMPLETED_COVER_REQUIRED');
  if(await page.locator('.cover-editor.bcc-dialog__wrap-mask:visible').count())fail('NATIVE_COVER_EDITOR_STILL_OPEN');
  const region=await coverRegion(page);if(region.regions!==1||!region.preview_present||region.placeholder_present)fail('NATIVE_COMPLETED_COVER_REQUIRED');
  const button=await unique(page.locator('span.submit-draft:visible'));if((await button.innerText()).trim()!=='存草稿'||!await button.isEnabled()||await button.getAttribute('aria-disabled')==='true')fail('NATIVE_PRIVATE_SAVE_CONTROL_CHANGED');
  const before=await visibleSaveObservation(page);await reserveIntent(spec,output,'draft-save',{public_submit:false,before});reserved=true;
  await button.click({timeout:5000});clicked=true;await helpers.write('native-private-draft-clicked.json',{...base,native_click_performed:true,clicked_at:new Date().toISOString()});
  const end=Date.now()+8000;let observed,confirmed=false;do{if(!await helpers.checkProtection())fail('NATIVE_PROTECTION_REQUIRED');observed=await visibleSaveObservation(page);confirmed=observed.success_messages.some(x=>!before.success_messages.includes(x));if(confirmed)break;await page.waitForTimeout(100);}while(Date.now()<end);
  const result={...base,state:confirmed?'private_draft_saved':'save_unknown',native_click_performed:true,native_success_observed:confirmed,before,observed,repeat_forbidden:true};
  await firstResult(output,'native-private-draft-result.json',result);await helpers.inventory('native-private-draft-observed');return result;
 }catch(e){const consumed=reserved||await hasIntent(spec,output,'draft-save').catch(()=>false);const result={...base,state:consumed&&!reserved?'private_draft_reconcile_required':reserved?'save_unknown':'blocked',error_code:safeError(e),native_click_performed:clicked?true:reserved?null:false,repeat_forbidden:consumed};if(reserved)await firstResult(output,'native-private-draft-result.json',result);else await helpers.write('native-private-draft-validation.json',result);await helpers.inventory('native-private-draft-blocked').catch(()=>{});return result;}
}
export async function observePrivateDraft({page,spec,output,helpers}){
 const u=new URL(page.url());if(u.origin!=='https://member.bilibili.com'||!await helpers.checkProtection()||await helpers.accountIdentity()!==spec.target_site_uid)return {state:'blocked',error_code:'NATIVE_PRIVATE_LOOKUP_PROTECTION_REQUIRED',submitted:false,native_click_performed:false};
 let recorded=null;try{recorded=await readJSON(path.join(output,'native-private-draft-result.json'));}catch{}
 const bound=recorded&&recorded.job_id===spec.job_id&&recorded.attempt_id===spec.attempt_id&&recorded.recovery_id===spec.recovery_id&&recorded.metadata_sha256===spec.metadata_sha256&&recorded.cover_sha256===spec.cover?.sha256;
 return {state:bound&&recorded.state==='private_draft_saved'?'recorded_private_draft_saved':'private_draft_save_unknown',recorded:bound?recorded:null,...(recorded&&!bound?{error_code:'NATIVE_PRIVATE_RESULT_BINDING_MISMATCH'}:{}),observed:await visibleSaveObservation(page),
  draft_id:null,draft_id_verified:false,new_native_click:false,submitted:false,submit_implemented:false};
}
