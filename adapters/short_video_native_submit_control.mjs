// Fixed recovered-short controller. Broker admission MUST run before submit.
// No API, credentials, storage, hidden app state, upload or agreement acceptance.
import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash} from 'node:crypto';
export const nativeControlCapabilities=Object.freeze({submit:true,lookup:true,fill_metadata:false,sync_metadata_revision:false});
const SHA=/^[a-f0-9]{64}$/,UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/,BV=/^BV[A-Za-z0-9]{10}$/;
const FIELDS=['job_id','attempt_id','recovery_id','target_site_uid','source_url','source_id','download_job_id','file','file_bytes','artifact_sha256','metadata_sha256','upload_metadata_sha256','binding_sha256','metadata_revision_id','upload_reference_sha256'];
function fail(code){throw Object.assign(new Error(code),{code});}
function canonical(v){return Array.isArray(v)?'['+v.map(canonical).join(',')+']':v!==null&&typeof v==='object'?'{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+canonical(v[k])).join(',')+'}':JSON.stringify(v);}
const hash=b=>createHash('sha256').update(b).digest('hex'),digest=v=>hash(canonical(v));
async function safeFile(file,max){
 if(!path.isAbsolute(file)||await fs.realpath(file)!==file)fail('SHORT_SUBMIT_PATH_INVALID');
 for(let p=file;p!==path.dirname(p);p=path.dirname(p))if((await fs.lstat(p)).isSymbolicLink())fail('SHORT_SUBMIT_PATH_INVALID');
 const st=await fs.lstat(file);if(!st.isFile()||st.size<1||st.size>max)fail('SHORT_SUBMIT_BOUND_FILE_INVALID');return fs.readFile(file);
}
async function readJSON(file,max=65536){return JSON.parse((await safeFile(file,max)).toString('utf8'));}
async function unique(locator){if(await locator.count()!==1)fail('SHORT_SUBMIT_NATIVE_CONTROL_AMBIGUOUS');return locator;}
async function description(locator){return locator.evaluate(e=>{
 const inlineTags=new Set(['SPAN','A','STRONG','B','EM','I','U','S','DEL','CODE']);
 function text(n){if(n.nodeType===Node.TEXT_NODE)return n.nodeValue.replace(/\r\n/g,'\n');if(n.nodeType===Node.COMMENT_NODE)return '';if(n.nodeType!==Node.ELEMENT_NODE)throw 0;if(n.tagName==='BR')return '\n';if(!inlineTags.has(n.tagName)||n.getAttribute('contenteditable')==='false'||n.classList.contains('ql-cursor'))throw 0;const s=getComputedStyle(n);if(s.display==='none'||s.visibility==='hidden')throw 0;return [...n.childNodes].map(text).join('');}
 try{const nodes=[...e.childNodes].filter(n=>n.nodeType!==Node.COMMENT_NODE&&!(n.nodeType===Node.TEXT_NODE&&n.nodeValue===''));return {supported:true,text:nodes.some(n=>n.nodeType===Node.ELEMENT_NODE&&['P','DIV'].includes(n.tagName))?nodes.map(n=>{if(n.nodeType!==Node.ELEMENT_NODE||!['P','DIV'].includes(n.tagName))throw 0;return n.childNodes.length===1&&n.firstChild.nodeName==='BR'?'':[...n.childNodes].map(text).join('');}).join('\n'):nodes.map(text).join('')};}catch{return {supported:false};}
 });}
async function syncDirectory(dir){if(process.platform==='win32')return;const h=await fs.open(dir,'r');try{await h.sync();}finally{await h.close();}}
async function durableExclusive(file,value){const h=await fs.open(file,'wx',0o600);try{await h.writeFile(JSON.stringify(value,null,2)+'\n');await h.sync();}finally{await h.close();}await syncDirectory(path.dirname(file));}
async function bindingAdmission(root,spec){
 const i=await readJSON(path.join(root,'media',spec.job_id,'recovered-submit-intent.json'));
 const bound=Object.fromEntries(FIELDS.map(k=>[k,spec[k]]));
 if(i.version!=='recovered-short-video-submit-intent-v1'||i.state!=='submit_reserved_result_unknown'||digest(i.binding)!==digest(bound)||!UUID.test(i.ticket_id||'')||!UUID.test(i.command_id||'')||![i.approval_sha256,i.reservation_sha256].every(s=>SHA.test(s||''))||i.reconcile_before_retry!==true||i.native_click_performed!=='unknown'||i.new_file_selection!==false||i.human_review!==false||!Number.isFinite(Date.parse(i.created_at))||Date.now()-Date.parse(i.created_at)<0||Date.now()-Date.parse(i.created_at)>900000)fail('SHORT_SUBMIT_ACTUAL_GATE_INTENT_REQUIRED');
 return i;
}
async function localCover(root,spec){
 const c=spec.cover,d=spec.cover_derivation,pfile=path.join(root,'media',spec.job_id,'cover-assets-v4/provenance.json');
 if(c?.file!==path.join(root,'media',spec.job_id,'cover-assets-v4/top-bottom-blur-cover.png')||!SHA.test(c?.sha256||'')||!Number.isInteger(c.bytes)||c.bytes<33||c.bytes>5_000_000||c.source_url!=='https://i.ytimg.com/vi/'+spec.source_id+'/maxresdefault.jpg'||d?.version!=='derived-cover-binding-v2'||d.provenance_file!==pfile||!SHA.test(d.provenance_sha256||''))fail('SHORT_SUBMIT_COVER_BINDING_INVALID');
 const bytes=await safeFile(c.file,5_000_000),raw=await safeFile(pfile,32768),p=JSON.parse(raw);
 if(bytes.length!==c.bytes||hash(bytes)!==c.sha256||!bytes.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10]))||bytes.readUInt32BE(8)!==13||bytes.toString('ascii',12,16)!=='IHDR'||bytes.readUInt32BE(16)!==1280||bytes.readUInt32BE(20)!==960||hash(raw)!==d.provenance_sha256||digest(p)!==digest(d.provenance)||p.version!=='lighthouse-derived-cover-v4'||p.policy_version!=='top-bottom-blur-v1'||p.image_edited!==true||p.no_text_regenerated!==true||p.official_original_bytes!==false||
  p.output?.file!==c.file||p.output?.bytes!==c.bytes||p.output?.sha256!==c.sha256||p.output?.width!==1280||p.output?.height!==960||p.output?.mime_type!=='image/png'||
  p.input?.file!==path.join(root,'media',spec.job_id,'short-video-attempts',spec.attempt_id,'cover-source-official-maxres.jpg')||p.input?.source_url!==c.source_url||!SHA.test(p.input?.sha256||'')||p.input?.width!==1280||p.input?.height!==720||!Number.isInteger(p.input?.bytes)||p.input.bytes<1||p.input.bytes>1_000_000)fail('SHORT_SUBMIT_COVER_CHANGED');
 const original=await safeFile(p.input.file,1_000_000);if(original.length!==p.input.bytes||hash(original)!==p.input.sha256||original[0]!==255||original[1]!==216)fail('SHORT_SUBMIT_COVER_SOURCE_CHANGED');
 if(digest(p.transform?.foreground_rect)!==digest({x:0,y:120,width:1280,height:720})||digest(p.transform?.safe_crop)!==digest({x:0,y:0,width:1280,height:960})||p.transform?.foreground_fit!=='original_size'||p.transform?.foreground_padding!=='none'||digest(p.transform?.background)!==digest({width:1280,height:960,blur_sigma:35}))fail('SHORT_SUBMIT_COVER_POLICY_CHANGED');
 return {cover_sha256:c.sha256,cover_provenance_sha256:d.provenance_sha256,server_image_hash_verified:false};
}
async function result(page,spec,{sameAttempt=false}={}){
 const links=page.locator('a[href]:visible'),ids=[];
 for(let i=0;i<await links.count();i++){let u;try{u=new URL(await links.nth(i).getAttribute('href'),page.url());}catch{continue;}const m=/^\/video\/(BV[A-Za-z0-9]{10})\/?$/.exec(u.pathname);if(u.origin==='https://www.bilibili.com'&&!u.username&&!u.password&&m)ids.push(m[1]);}
 const unique=[...new Set(ids)];if(unique.length!==1)return {state:'submit_unknown',external_id_candidates:unique.slice(0,5),submitted:null};
 const body=await page.locator('body').innerText();let state=null;
 if(/审核不通过|审核未通过|已退回/.test(body))state='failed';else if(/审核中|投稿成功|投递成功|已通过/.test(body))state='awaiting_moderation';
 if(!state)return {state:'submit_unknown',external_id_candidates:unique,submitted:null};
 const observation={version:'short-video-browser-result-v1',attempt_id:spec.attempt_id,site_uid:spec.target_site_uid,source_url:spec.source_url,metadata_sha256:spec.metadata_sha256,external_id:unique[0],url:'https://www.bilibili.com/video/'+unique[0],state,...(sameAttempt?{same_submit_attempt_observed:true}:{}),...(state==='failed'?{error_code:'BILIBILI_MODERATION_REJECTED'}:{})};
 return {state,submitted:true,result_observation:observation,public_accessible_verified:false};
}
export async function handleNativeControl({page,spec,output,control_action,helpers}){
 const base={version:'short-native-submit-controller-v1',job_id:spec.job_id,attempt_id:spec.attempt_id,recovery_id:spec.recovery_id,metadata_sha256:spec.metadata_sha256,submit_implemented:true,new_video_file_selection:false};
 if(!['submit','lookup'].includes(control_action))return {...base,state:'unsupported',submitted:false,native_click_performed:false};
 const root=path.resolve(process.env.HUB_ROOT||'/opt/lighthouse-hub'),intent=path.join(output,'native-short-submit-click-intent.json');let clicked=false;
 try{
  if(![spec.job_id,spec.attempt_id,spec.recovery_id].every(v=>UUID.test(v||''))||output!==path.join(root,'media',spec.job_id,'recovered-editors',spec.recovery_id))fail('SHORT_SUBMIT_OUTPUT_INVALID');
  if(control_action==='submit'){
   try{await fs.lstat(intent);return {...base,state:'submit_reconcile_required',submitted:null,native_click_performed:false,reconcile_before_retry:true};}catch(e){if(e.code!=='ENOENT')throw e;}
  }
  if(!await helpers.checkProtection()||await helpers.accountIdentity()!==spec.target_site_uid)fail('SHORT_SUBMIT_ACCOUNT_OR_PROTECTION_CHANGED');
  if(control_action==='lookup'){const observed=await result(page,spec);await helpers.write('short-native-submit-lookup.json',{...base,...observed,native_click_performed:false});return {...base,...observed,native_click_performed:false};}
  const u=new URL(page.url());if(u.origin!=='https://member.bilibili.com'||u.pathname!=='/platform/upload/video/frame')fail('SHORT_SUBMIT_ORIGIN_CHANGED');
  const admission=await bindingAdmission(root,spec);await helpers.checkedFile();
  const meta=await readJSON(path.join(output,'metadata.json'),20000);
  if(digest(meta)!==spec.metadata_sha256||digest(meta)!==digest(spec.metadata)||meta.copyright!==2||meta.source!==spec.source_url||meta.category_label!=='娱乐')fail('SHORT_SUBMIT_METADATA_BINDING_CHANGED');
  const title=await (await unique(page.locator('input[placeholder="请输入稿件标题"]:visible'))).inputValue(),desc=await description(await unique(page.locator('.ql-editor[contenteditable="true"]:visible')));
  const declaration=await (await unique(page.locator('input[placeholder="请选择符合您视频内容的创作声明"]:visible'))).inputValue();
  const source=await (await unique(page.locator('input[placeholder^="转载视频请注明来源"]:visible'))).inputValue();
  const category=(await (await unique(page.locator('.video-human-type .select-controller:visible'))).innerText()).trim();
  const tags=(await page.locator('.label-item-v2-container:visible p.label-item-v2-content').allTextContents()).map(s=>s.trim());
  if(title!==meta.title||!desc.supported||desc.text!==meta.description||declaration!=='内容为转载'||source!==meta.source||category!==meta.category_label||tags.length!==meta.tags.length||new Set(tags).size!==tags.length||meta.tags.some(t=>!tags.includes(t)))fail('SHORT_SUBMIT_NATIVE_METADATA_CHANGED');
  if(await page.locator('.cover-editor:visible').count()||await page.locator('.modal-footer button:visible').count())fail('SHORT_SUBMIT_NATIVE_MODAL_STILL_OPEN');
  const cover=await unique(page.locator('.cover-main .cover-module-main:visible'));
  if(await cover.getByText('添加封面',{exact:true}).count()||!await cover.evaluate(e=>Boolean(e.querySelector('img,canvas')||[e,...e.querySelectorAll('*')].some(n=>getComputedStyle(n).backgroundImage!=='none'))))fail('SHORT_SUBMIT_NATIVE_COVER_REQUIRED');
  const coverEvidence=await localCover(root,spec);
  const uncheckedAgreement=await page.evaluate(()=>[...document.querySelectorAll('input[type="checkbox"]')].some(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&!e.checked&&/同意|阅读|协议/.test((e.closest('label')||e.parentElement)?.innerText||'');}));
  if(uncheckedAgreement)fail('SHORT_SUBMIT_NEW_AGREEMENT_REQUIRED');
  const existing=await result(page,spec);if(existing.external_id_candidates?.length||existing.result_observation)fail('SHORT_SUBMIT_PREEXISTING_BV_REQUIRES_LOOKUP');
  const button=await unique(page.locator('span.submit-add:visible').filter({hasText:/^\s*立即投稿\s*$/}));
  if(!await button.isEnabled())fail('SHORT_SUBMIT_NATIVE_BUTTON_DISABLED');
  await page.screenshot({path:path.join(output,'short-immediately-before-submit.png'),fullPage:true,timeout:20000});
  const evidence={...base,ticket_id:admission.ticket_id,command_id:admission.command_id,cover:coverEvidence,title,description:desc.text,source,category,tags,created_at:new Date().toISOString(),state:'native_click_reserved_result_unknown',reconcile_before_retry:true};
  await durableExclusive(intent,evidence);
  if(!await helpers.checkProtection()||await helpers.accountIdentity()!==spec.target_site_uid)fail('SHORT_SUBMIT_ACCOUNT_OR_PROTECTION_CHANGED');
  clicked=true;await button.click({timeout:5000});
  const deadline=Date.now()+15000;
  do{
   if(!await helpers.checkProtection())fail('SHORT_SUBMIT_POST_CLICK_PROTECTION_REQUIRED');
   const observed=await result(page,spec,{sameAttempt:true});
   if(observed.result_observation){const response={...base,...observed,native_click_performed:true,cover:coverEvidence};await helpers.write('short-native-submit-result.json',response);await helpers.inventory('short-native-submit-result').catch(()=>{});return response;}
   await page.waitForTimeout(250);
  }while(Date.now()<deadline);
  const unknown={...base,state:'submit_unknown',submitted:null,native_click_performed:true,reconcile_before_retry:true};await helpers.write('short-native-submit-result.json',unknown);await helpers.inventory('short-native-submit-unknown').catch(()=>{});return unknown;
 }catch(e){
  const response={...base,state:clicked?'submit_unknown':'blocked',submitted:clicked?null:false,native_click_performed:clicked,error_code:typeof e?.code==='string'&&/^SHORT_SUBMIT_[A-Z_]+$/.test(e.code)?e.code:e.code==='EEXIST'?'SHORT_SUBMIT_ALREADY_CONSUMED':'SHORT_SUBMIT_NATIVE_OPERATION_FAILED',reconcile_before_retry:true};
  await helpers.write('short-native-submit-error-'+Date.now()+'.json',response);await helpers.inventory('short-native-submit-error').catch(()=>{});return response;
 }
}
