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


// Pure, versioned metadata lineage. No I/O, owner approval or browser actions.
export const PUBLIC_PERMISSION_LINE='转载许可编号：114514（字幕组提供的许可范围）。';
function canonical(v){return Array.isArray(v)?'['+v.map(canonical).join(',')+']':v!==null&&typeof v==='object'?'{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+canonical(v[k])).join(',')+'}':JSON.stringify(v);}
export const metadataChainDigest=v=>createHash('sha256').update(canonical(v)).digest('hex');
const equal=(a,b)=>canonical(a)===canonical(b),UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
function chainFail(){throw Object.assign(new Error('RECOVERY_METADATA_REVISION_INVALID'),{code:'RECOVERY_METADATA_REVISION_INVALID'});}
export function validateShortMetadataRevisionChain({spec,originalMetadata,originalSpec}){
 const hash=metadataChainDigest;
 if(hash(originalMetadata)!==spec.upload_metadata_sha256||hash(spec.metadata)!==spec.metadata_sha256)chainFail();
 let history;
 if(spec.metadata_revision_history!==undefined){if(!Array.isArray(spec.metadata_revision_history))chainFail();history=spec.metadata_revision_history;}
 else history=spec.metadata_revision?[spec.metadata_revision]:[];
 if(history.length>2||spec.metadata_revision&&(!history.length||!equal(spec.metadata_revision,history.at(-1))))chainFail();
 if(!history.length){if(!equal(spec.metadata,originalMetadata)||spec.metadata_sha256!==spec.upload_metadata_sha256)chainFail();return {mode:'unrevised',revision_ids:[]};}
 // Preserve the original, versioned single-category transport contract. New
 // explicit histories below require full bindings and cannot use this path.
 if(spec.metadata_revision_history===undefined&&history.length===1){
  const r=history[0];
  if(r.version!=='short-video-category-metadata-revision-v1'||r.revision_id!==spec.metadata_revision_id||r.previous_metadata_sha256!==spec.upload_metadata_sha256||
   r.new_metadata_sha256!==spec.metadata_sha256||hash(r.previous_binding?.metadata)!==spec.upload_metadata_sha256||r.new_binding_sha256!==spec.binding_sha256||
   !equal(r.changed_fields,['category_label'])||originalMetadata.category_label!=='虚拟UP主'||spec.metadata.category_label!=='娱乐'||
   hash({...spec.metadata,category_label:'虚拟UP主'})!==spec.upload_metadata_sha256)chainFail();
  return {mode:'legacy_category_single',revision_ids:[r.revision_id]};
 }
 let binding=history[0].previous_binding,metadata=originalMetadata;
 if(!binding||!equal(binding.metadata,originalMetadata)||binding.metadata_sha256!==spec.upload_metadata_sha256||
  binding.site_uid!==spec.target_site_uid||binding.source_url!==spec.source_url||binding.source_id!==spec.source_id||binding.download_job_id!==spec.download_job_id||
  binding.artifact_sha256!==spec.artifact_sha256||binding.file!==spec.file||binding.file_bytes!==spec.file_bytes||binding.human_review!==false||
  originalSpec&&originalSpec.metadata_sha256!==spec.upload_metadata_sha256)chainFail();
 for(let index=0;index<history.length;index++){
  const revision=history[index];
  if(!revision||!UUID.test(revision.revision_id||'')||revision.attempt_id!==spec.attempt_id||!equal(revision.previous_binding,binding)||
   revision.previous_binding_sha256!==hash(binding)||revision.previous_metadata_sha256!==hash(metadata)||revision.public_submit!==false||revision.remote_metadata_synchronized!==false)chainFail();
  let updated;
  if(index===0){
   if(revision.version!=='short-video-category-metadata-revision-v1'||!equal(revision.changed_fields,['category_label'])||metadata.category_label!=='虚拟UP主'||
    revision.previous_category_label!=='虚拟UP主'||revision.new_category_label!=='娱乐')chainFail();
   updated={...metadata,category_label:'娱乐'};
  }else{
   const suffix='\n'+PUBLIC_PERMISSION_LINE;
   if(revision.version!=='public-permission-line-removal-v1'||!equal(revision.changed_fields,['description'])||revision.previous_revision_id!==history[index-1].revision_id||
    revision.removed_last_line!==PUBLIC_PERMISSION_LINE||typeof metadata.description!=='string'||!metadata.description.endsWith(suffix)||metadata.description.endsWith('\r'+suffix)||
    revision.internal_rights_records_preserved!==true||revision.actual_owner_response!=='转载许可编号删掉'||
    !['owner_response_reference','actual_operator_reference'].every(k=>typeof revision[k]==='string'&&revision[k].trim().length>0&&revision[k].length<=2048))chainFail();
   updated={...metadata,description:metadata.description.slice(0,-suffix.length)};
  }
  const next={...binding,metadata:updated,metadata_sha256:hash(updated)};
  if(revision.new_metadata_sha256!==hash(updated)||revision.new_binding_sha256!==hash(next))chainFail();
  metadata=updated;binding=next;
 }
 if(spec.metadata_revision_id!==history.at(-1).revision_id||spec.binding_sha256!==hash(binding)||spec.metadata_sha256!==hash(metadata)||!equal(spec.metadata,metadata))chainFail();
 return {mode:history.length===2?'category_and_public_line_removal':'category_only',revision_ids:history.map(x=>x.revision_id),binding_sha256:hash(binding)};
}

export const nativeControlCapabilities=Object.freeze({sync_metadata_revision:true,fill_metadata:false,submit:false,lookup:false});
export async function handleNativeControl({page,spec,output,control_action,helpers}){
 const base={version:'native-public-permission-line-sync-v1',job_id:spec.job_id,attempt_id:spec.attempt_id,recovery_id:spec.recovery_id,metadata_sha256:spec.metadata_sha256,
  submitted:false,submit_implemented:false,new_video_file_selection:false,saved:false};
 if(control_action!=='sync_metadata_revision')return {...base,state:'unsupported',native_modified:false};
 try{
  const history=spec.metadata_revision_history;if(!Array.isArray(history)||history.length!==2)fail('METADATA_SYNC_TWO_REVISIONS_REQUIRED');
  const checked=validateShortMetadataRevisionChain({spec,originalMetadata:history[0].previous_binding.metadata,originalSpec:{metadata_sha256:spec.upload_metadata_sha256}});
  if(checked.mode!=='category_and_public_line_removal')fail('METADATA_SYNC_SCOPE_REQUIRED');
  const f=path.join(output,'metadata.json'),st=await fs.lstat(f);if(!st.isFile()||st.isSymbolicLink()||st.size>20000)fail('METADATA_SYNC_BOUND_FILE_REQUIRED');
  const metadata=JSON.parse(await fs.readFile(f,'utf8'));if(canonicalDigest(metadata)!==spec.metadata_sha256||metadata.description!==spec.metadata.description)fail('METADATA_SYNC_BOUND_FILE_CHANGED');
  const u=new URL(page.url());if(u.origin!=='https://member.bilibili.com'||u.pathname!=='/platform/upload/video/frame')fail('METADATA_SYNC_ORIGIN_CHANGED');
  if(!await helpers.checkProtection()||await helpers.accountIdentity()!==spec.target_site_uid)fail('METADATA_SYNC_ACCOUNT_OR_PROTECTION');await helpers.checkedFile();
  const title=page.locator('input[placeholder="请输入稿件标题"]:visible'),description=page.locator('.ql-editor[contenteditable="true"]:visible');
  if(await title.count()!==1||await description.count()!==1||await title.inputValue()!==metadata.title)fail('METADATA_SYNC_NATIVE_TITLE_CHANGED');
  const before=await readNativeDescription(description);if(!before.supported)fail('METADATA_SYNC_NATIVE_DESCRIPTION_UNSUPPORTED');
  if(before.text===metadata.description)return {...base,state:'metadata_already_current',native_modified:false};
  const previous=history[1].previous_binding.metadata;
  if(before.text!==previous.description||!before.text.endsWith('\n'+PUBLIC_PERMISSION_LINE)||before.text.endsWith('\r\n'+PUBLIC_PERMISSION_LINE)||metadata.description!==before.text.slice(0,-('\n'+PUBLIC_PERMISSION_LINE).length))fail('METADATA_SYNC_PREVIOUS_DESCRIPTION_CHANGED');
  await helpers.write('native-metadata-description-sync-before.json',{...base,previous_metadata_sha256:history[1].previous_metadata_sha256,description:before.text,native_modified:false});
  await description.fill(metadata.description,{timeout:5000});await description.blur();
  const after=await readNativeDescription(description);
  if(!after.supported||after.text!==metadata.description||await title.inputValue()!==metadata.title)fail('METADATA_SYNC_READBACK_FAILED');
  const result={...base,state:'metadata_revision_applied_pending_broker_verification',native_modified:true,description:after.text,previous_metadata_sha256:history[1].previous_metadata_sha256};
  await helpers.write('native-metadata-description-sync-result.json',result);return result;
 }catch(error){const result={...base,state:'blocked',error_code:typeof error?.code==='string'&&/^(METADATA_SYNC_|RECOVERY_)[A-Z_]+$/.test(error.code)?error.code:'METADATA_SYNC_NATIVE_FAILED',native_modified:null};await helpers.write('native-metadata-description-sync-blocked.json',result);return result;}
}
