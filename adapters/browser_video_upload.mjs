// Disabled-by-default page driver prototype. No launcher, credentials or live layout.
import {createReadStream} from 'node:fs';
import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash} from 'node:crypto';

export const VIDEO_UPLOAD_LAYOUT_VERSION='website-video-layout-v1';
export const VIDEO_UPLOAD_DRIVER_VERSION='website-video-page-driver-v1';
const RECEIPT='website-video-upload-receipt-v1';
const SHA=/^[a-f0-9]{64}$/;
const BV=/^BV[A-Za-z0-9]{10}$/;
const receiptKeys=['version','key','artifact_sha256','file_bytes','site_uid','upload_reference','evidence_kind','server_file_hash_verified'];
function fail(code){throw Object.assign(new Error(code),{code});}
function text(value){return typeof value==='string'&&value.trim().length>0;}
function freeze(value){if(value&&typeof value==='object'){Object.values(value).forEach(freeze);Object.freeze(value);}return value;}
function origin(value){try{const u=new URL(value);if(u.protocol!=='https:'||u.username||u.password||u.origin!==value)fail('WEB_VIDEO_LAYOUT_INVALID');return u.origin;}catch{fail('WEB_VIDEO_LAYOUT_INVALID');}}
function checkLayout(value){
 if(!value||value.version!==VIDEO_UPLOAD_LAYOUT_VERSION||value.validation!=='owned_fixture_only_unverified_live')fail('WEB_VIDEO_LAYOUT_UNVERIFIED_REQUIRED');
 origin(value.origin);origin(value.account?.origin);
 if(!new URL(value.origin).hostname.endsWith('.test')||!new URL(value.account.origin).hostname.endsWith('.test'))fail('WEB_VIDEO_OWNED_TEST_ORIGIN_REQUIRED');
 if(!text(value.account?.linkSelector)||!text(value.account?.nameSelector)||!path.isAbsolute(value.artifactRoot??''))fail('WEB_VIDEO_LAYOUT_INVALID');
 for(const name of ['fileInput','uploadReference','submitButton','resultLink','status'])if(!text(value.selectors?.[name]))fail('WEB_VIDEO_LAYOUT_INVALID');
 if(!value.metadataFields||!Object.keys(value.metadataFields).length||Object.values(value.metadataFields).some(x=>!text(x)))fail('WEB_VIDEO_LAYOUT_INVALID');
 for(const kind of ['captcha','login','terms','security'])if(!Array.isArray(value.challenges?.[kind])||!value.challenges[kind].length||value.challenges[kind].some(x=>!text(x)))fail('WEB_VIDEO_LAYOUT_INVALID');
 const statuses=[];
 for(const state of ['awaiting_moderation','completed','failed']){
  const names=value.statusTexts?.[state];if(!Array.isArray(names)||!names.length||names.some(x=>!text(x)||x!==x.trim()))fail('WEB_VIDEO_LAYOUT_INVALID');statuses.push(...names);
 }
 if(new Set(statuses).size!==statuses.length)fail('WEB_VIDEO_LAYOUT_INVALID');
 const timeout=value.timeoutMs??5000;if(!Number.isInteger(timeout)||timeout<100||timeout>30000)fail('WEB_VIDEO_LAYOUT_INVALID');
 return freeze(structuredClone({...value,timeoutMs:timeout}));
}

export function createVideoUploadPageDriver(page,layout,{beforeUpload,beforeSubmit}={}){
 const spec=checkLayout(layout);let attempt=null,busy=false;
 async function visible(selector){const list=page.locator(selector),out=[];for(let i=0;i<await list.count();i++)if(await list.nth(i).isVisible())out.push(list.nth(i));return out;}
 async function unique(selector,code){const list=await visible(selector);if(list.length!==1)fail(list.length?code+'_AMBIGUOUS':code+'_MISSING');return list[0];}
 async function guard(){
  if(page.isClosed())fail('WEB_VIDEO_PAGE_CLOSED');
  let actual;try{actual=new URL(page.url()).origin;}catch{fail('WEB_VIDEO_ORIGIN_MISMATCH');}
  if(actual!==spec.origin)fail('WEB_VIDEO_ORIGIN_MISMATCH');
  for(const kind of ['captcha','login','terms','security'])for(const selector of spec.challenges[kind])if((await visible(selector)).length)fail('WEB_VIDEO_'+kind.toUpperCase()+'_REQUIRED');
 }
 async function accountIdentity(){
  await guard();const link=await unique(spec.account.linkSelector,'WEB_VIDEO_ACCOUNT');
  const name=(await (await unique(spec.account.nameSelector,'WEB_VIDEO_ACCOUNT_NAME')).innerText()).trim();
  let u;try{u=new URL(await link.getAttribute('href'),page.url());}catch{fail('WEB_VIDEO_ACCOUNT_INVALID');}
  const match=u.pathname.match(/^\/([1-9][0-9]{0,19})\/?$/);
  if(u.origin!==spec.account.origin||u.username||u.password||u.search||u.hash||!match||!name)fail('WEB_VIDEO_ACCOUNT_INVALID');
  return {site_uid:match[1],name};
 }
 async function sameAccount(account){const now=await accountIdentity();if(now.site_uid!==account.site_uid||now.name!==account.name)fail('WEB_VIDEO_ACCOUNT_CHANGED');}
 async function checkedFile(file,sha,size){
  if(!path.isAbsolute(file??'')||!SHA.test(sha??'')||!Number.isSafeInteger(size)||size<=0||size>4_000_000_000)fail('WEB_VIDEO_ARTIFACT_INVALID');
  const base=await fs.realpath(spec.artifactRoot),absolute=path.resolve(file),real=await fs.realpath(absolute);
  if(real!==absolute||!real.startsWith(base+path.sep)||(await fs.lstat(absolute)).isSymbolicLink())fail('WEB_VIDEO_ARTIFACT_PATH_INVALID');
  const before=await fs.stat(real);if(!before.isFile()||before.size!==size)fail('WEB_VIDEO_ARTIFACT_SIZE_MISMATCH');
  const hash=createHash('sha256');for await(const chunk of createReadStream(real))hash.update(chunk);
  const after=await fs.stat(real);
  if(after.size!==before.size||after.mtimeMs!==before.mtimeMs||after.ctimeMs!==before.ctimeMs||await fs.realpath(absolute)!==real)fail('WEB_VIDEO_ARTIFACT_CHANGED');
  if(hash.digest('hex')!==sha)fail('WEB_VIDEO_ARTIFACT_HASH_MISMATCH');
  return real;
 }
 async function fileStamp(file){const stat=await fs.stat(file);return [stat.size,stat.mtimeMs,stat.ctimeMs,stat.ino,stat.dev].join(':');}
 async function callback(fn,context,code){if(typeof fn!=='function')fail(code);if(await fn(freeze(structuredClone(context)))===false)fail(code);}
 async function inputSelection(expected){
  const locator=page.locator(spec.selectors.fileInput);if(await locator.count()!==1)fail('WEB_VIDEO_FILE_INPUT_AMBIGUOUS');
  const files=await locator.evaluate(el=>Array.from(el.files??[],f=>({name:f.name,size:f.size})));
  if(files.length!==1||files[0].name!==path.basename(expected.file)||files[0].size!==expected.file_bytes)fail('WEB_VIDEO_BROWSER_FILE_MISMATCH');
 }
 async function metadataValue(selector){
  const node=await unique(selector,'WEB_VIDEO_METADATA_FIELD');
  const type=await node.evaluate(el=>({tag:el.tagName,type:el.type}));
  if(type.tag!=='TEXTAREA'&&(type.tag!=='INPUT'||!['text','number','url','search','email','tel'].includes(type.type)))fail('WEB_VIDEO_METADATA_CONTROL_UNSUPPORTED');
  return {node,value:await node.inputValue()};
 }
 async function reference(){return (await (await unique(spec.selectors.uploadReference,'WEB_VIDEO_UPLOAD_REFERENCE')).innerText()).trim();}
 async function waitReference(){
  const end=Date.now()+spec.timeoutMs;
  do{await guard();const list=await visible(spec.selectors.uploadReference);if(list.length>1)fail('WEB_VIDEO_UPLOAD_REFERENCE_AMBIGUOUS');
   if(list.length){const value=(await list[0].innerText()).trim();if(!value||value.length>512)fail('WEB_VIDEO_UPLOAD_REFERENCE_INVALID');return value;}
   await page.waitForTimeout(40);
  }while(Date.now()<end);fail('WEB_VIDEO_UPLOAD_RESULT_UNKNOWN');
 }
 async function result(known=null,{wait=false}={}){
  const end=Date.now()+(wait?spec.timeoutMs:0);
  do{
   await guard();const links=await visible(spec.selectors.resultLink);
   if(links.length>1)fail('WEB_VIDEO_RESULT_AMBIGUOUS');
   if(links.length){
    let url;try{url=new URL(await links[0].getAttribute('href'),page.url());}catch{fail('WEB_VIDEO_RESULT_INVALID');}
    const match=url.pathname.match(/^\/video\/(BV[A-Za-z0-9]{10})\/?$/);
    if(url.origin!=='https://www.bilibili.com'||url.username||url.password||!match)fail('WEB_VIDEO_RESULT_INVALID');
    const external_id=match[1],canonical='https://www.bilibili.com/video/'+external_id;
    if(known&&external_id!==known)fail('WEB_VIDEO_BV_BINDING_MISMATCH');
    const statuses=await visible(spec.selectors.status);if(statuses.length>1)fail('WEB_VIDEO_STATUS_AMBIGUOUS');
    if(statuses.length){const status=(await statuses[0].innerText()).trim();
     for(const [state,names]of Object.entries(spec.statusTexts))if(names.includes(status))return {state,external_id,url:canonical,...(state==='failed'?{error_code:'BILIBILI_MODERATION_REJECTED'}:{})};
    }
    return {state:'unknown',external_id,url:canonical};
   }
   if(!wait||Date.now()>=end)return {state:'unknown'};
   await page.waitForTimeout(40);
  }while(true);
 }
 async function exclusive(fn){if(busy)fail('WEB_VIDEO_PAGE_BUSY');busy=true;try{return await fn();}finally{busy=false;}}
 async function prepareUpload(input){return exclusive(async()=>{
  if(!input||!SHA.test(input.key??'')||!input.metadata||typeof input.metadata!=='object'||Array.isArray(input.metadata))fail('WEB_VIDEO_UPLOAD_INPUT_INVALID');
  if(attempt)fail('WEB_VIDEO_PAGE_ATTEMPT_ALREADY_BOUND');
  if(typeof beforeUpload!=='function')fail('WEB_VIDEO_UPLOAD_CALLBACK_REQUIRED');
  await guard();const account=await accountIdentity();
  if((await visible(spec.selectors.uploadReference)).length)fail('WEB_VIDEO_UPLOAD_REFERENCE_ALREADY_PRESENT');
  if((await visible(spec.selectors.resultLink)).length)fail('WEB_VIDEO_RESULT_ALREADY_PRESENT');
  const keys=Object.keys(spec.metadataFields).sort();if(JSON.stringify(Object.keys(input.metadata).sort())!==JSON.stringify(keys))fail('WEB_VIDEO_METADATA_NOT_BOUND');
  const values={};for(const key of keys){const value=input.metadata[key];if(typeof value!=='string'&&!Number.isSafeInteger(value))fail('WEB_VIDEO_METADATA_INVALID');values[key]=String(value);await metadataValue(spec.metadataFields[key]);}
  const file=await checkedFile(input.file,input.artifact_sha256,input.file_bytes);
  const fileInput=page.locator(spec.selectors.fileInput);if(await fileInput.count()!==1||await fileInput.evaluate(el=>el.tagName!=='INPUT'||el.type!=='file'||el.multiple))fail('WEB_VIDEO_FILE_INPUT_INVALID');
  const binding=freeze({key:input.key,file,artifact_sha256:input.artifact_sha256,file_bytes:input.file_bytes,metadata:structuredClone(input.metadata),site_uid:account.site_uid});
  attempt={binding,account,state:'preparing'};
  try{
   for(const key of keys){
    await callback(beforeUpload,{...binding,boundary:'metadata_fill',metadata_field:key},'WEB_VIDEO_UPLOAD_CALLBACK_REQUIRED');
    await guard();await sameAccount(account);const field=await metadataValue(spec.metadataFields[key]);await field.node.fill(values[key],{timeout:spec.timeoutMs});
   }
   await guard();await sameAccount(account);await checkedFile(file,binding.artifact_sha256,binding.file_bytes);
   const stamp=await fileStamp(file);
   await callback(beforeUpload,{...binding,boundary:'file_selection'},'WEB_VIDEO_UPLOAD_CALLBACK_REQUIRED');
   await guard();await sameAccount(account);if(await fileStamp(file)!==stamp)fail('WEB_VIDEO_ARTIFACT_CHANGED');
   attempt.state='uploading';await fileInput.setInputFiles(file,{timeout:spec.timeoutMs});await inputSelection(binding);
   const upload_reference=await waitReference();await sameAccount(account);
   const receipt={version:RECEIPT,key:binding.key,artifact_sha256:binding.artifact_sha256,file_bytes:binding.file_bytes,
    site_uid:account.site_uid,upload_reference,evidence_kind:'same_browser_attempt_observation',server_file_hash_verified:false};
   attempt={...attempt,state:'uploaded',receipt:freeze(receipt),values};return structuredClone(receipt);
  }catch(error){attempt.state='upload_unknown';throw error;}
 });}
 async function submit(input){return exclusive(async()=>{
  if(!input||!SHA.test(input.key??'')||!attempt||attempt.binding.key!==input.key)fail('WEB_VIDEO_UPLOAD_ATTEMPT_NOT_BOUND');
  if(attempt.state!=='uploaded')fail('WEB_VIDEO_SUBMIT_ALREADY_CONSUMED');
  const receipt=input.upload_receipt;
  const hashKeys=receiptKeys.map(k=>k==='upload_reference'?'upload_reference_sha256':k);
  const hashed=receipt&&JSON.stringify(Object.keys(receipt).sort())===JSON.stringify(hashKeys.sort());
  const raw=receipt&&JSON.stringify(Object.keys(receipt).sort())===JSON.stringify([...receiptKeys].sort());
  const expectedHash=createHash('sha256').update(attempt.receipt.upload_reference).digest('hex');
  if((!raw&&!hashed)||receiptKeys.filter(k=>k!=='upload_reference').some(k=>receipt[k]!==attempt.receipt[k])||
    (hashed?receipt.upload_reference_sha256!==expectedHash:receipt.upload_reference!==attempt.receipt.upload_reference))fail('WEB_VIDEO_UPLOAD_RECEIPT_MISMATCH');
  if(typeof beforeSubmit!=='function')fail('WEB_VIDEO_SUBMIT_CALLBACK_REQUIRED');
  await guard();await sameAccount(attempt.account);await checkedFile(attempt.binding.file,receipt.artifact_sha256,receipt.file_bytes);await inputSelection(attempt.binding);
  const stamp=await fileStamp(attempt.binding.file);
  await callback(beforeSubmit,{...attempt.binding,boundary:'public_submit',upload_receipt:receipt},'WEB_VIDEO_SUBMIT_CALLBACK_REQUIRED');
  await guard();await sameAccount(attempt.account);if(await fileStamp(attempt.binding.file)!==stamp)fail('WEB_VIDEO_ARTIFACT_CHANGED');await inputSelection(attempt.binding);
  if(createHash('sha256').update(await reference()).digest('hex')!==expectedHash)fail('WEB_VIDEO_UPLOAD_REFERENCE_CHANGED');
  for(const [key,value]of Object.entries(attempt.values))if((await metadataValue(spec.metadataFields[key])).value!==value)fail('WEB_VIDEO_METADATA_CHANGED');
  if((await visible(spec.selectors.resultLink)).length)fail('WEB_VIDEO_RESULT_ALREADY_PRESENT');
  const button=await unique(spec.selectors.submitButton,'WEB_VIDEO_SUBMIT');if(!await button.isEnabled())fail('WEB_VIDEO_SUBMIT_DISABLED');
  attempt.state='submitting';
  try{await button.click({timeout:spec.timeoutMs});const value=await result(null,{wait:true});attempt.state=value.state==='unknown'?'submit_unknown':'submitted';return value;}
  catch(error){attempt.state='submit_unknown';throw error;}
 });}
 async function lookup({key,external_id}={}){
  if(!SHA.test(key??''))fail('INVALID_PUBLICATION_KEY');
  if(!external_id)return {state:'unknown'};
  if(!BV.test(external_id))fail('WEB_VIDEO_BV_INVALID');
  await guard();await accountIdentity();return result(external_id);
 }
 return Object.freeze({accountIdentity,prepareUpload,submit,lookup});
}
