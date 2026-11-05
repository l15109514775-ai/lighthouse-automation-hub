// Native UI controls v3: select exact bound repost statement; inspect category/tags/cover only.
import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash} from 'node:crypto';

export const nativeControlCapabilities=Object.freeze({fill_metadata:true,submit:false,lookup:false});
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

// Appended to the previously verified native description serializer.
export async function handleNativeControl({page,spec,output,control_action,helpers}) {
 const resultBase={version:'native-short-metadata-v5',job_id:spec.job_id,attempt_id:spec.attempt_id,
  recovery_id:spec.recovery_id,metadata_sha256:spec.metadata_sha256,submitted:false,submit_implemented:false,cover_modified:false,metadata_ready:false};
 if(control_action!=='fill_metadata')return {...resultBase,state:'unsupported'};
 const check=async()=>{
  const u=new URL(page.url());if(u.origin!=='https://member.bilibili.com'||u.pathname!=='/platform/upload/video/frame')fail('METADATA_ORIGIN_CHANGED');
  if(!await helpers.checkProtection())fail('METADATA_PROTECTION_REQUIRED');
 };
 const unique=async(locator,code='METADATA_NATIVE_CONTROL_AMBIGUOUS')=>{
  const deadline=Date.now()+3000;
  do{
   await check();const count=await locator.count();
   if(count>1)fail(code);
   if(count===1&&await locator.isVisible())return locator;
   if(Date.now()>=deadline)fail(code.replace(/_AMBIGUOUS$/,'_TIMEOUT'));
   await page.waitForTimeout(50);
  }while(true);
 };
 const waitValue=async(locator,expected,code,read)=>{
  const deadline=Date.now()+3000;
  do{
   const control=await unique(locator);await check();
   if((await read(control)).trim()===expected)return;
   if(Date.now()>=deadline)fail(code);
   await page.waitForTimeout(50);
  }while(true);
 };
 const tagValues=async()=>page.locator('.label-item-v2-container:visible p.label-item-v2-content').allTextContents();
 try {
  const f=path.join(output,'metadata.json'),st=await fs.lstat(f);
  if(!st.isFile()||st.isSymbolicLink()||st.size>20000)fail('METADATA_BINDING_INVALID');
  const meta=JSON.parse(await fs.readFile(f,'utf8'));
  if(canonicalDigest(meta)!==spec.metadata_sha256||meta.copyright!==2||meta.source!==spec.source_url||meta.category_label!=='娱乐')fail('METADATA_BINDING_INVALID');
  await check();if(await helpers.accountIdentity()!==spec.target_site_uid)fail('METADATA_ACCOUNT_CHANGED');await helpers.checkedFile();
  const title=await unique(page.locator('input[placeholder="请输入稿件标题"]:visible'));
  const description=await unique(page.locator('.ql-editor:visible'));
  const desc=await readNativeDescription(description);
  if(await title.inputValue()!==meta.title||!desc.supported||desc.text!==meta.description)fail('METADATA_RESTORED_CONTENT_CHANGED');
  const statement=await unique(page.locator('input[placeholder="请选择符合您视频内容的创作声明"]:visible'));
  if(!await statement.evaluate(e=>e.readOnly))fail('METADATA_DECLARATION_CONTROL_CHANGED');
  if((await statement.inputValue()).trim()!=='内容为转载'){
   await check();await statement.click();
   const option=await unique(page.locator('li.bcc-option:visible').filter({hasText:/^\s*内容为转载\s*$/}));
   await check();await option.click();
  }
  await waitValue(statement,'内容为转载','METADATA_DECLARATION_READBACK_FAILED',control=>control.inputValue());
  const source=await unique(page.locator('input[placeholder^="转载视频请注明来源"]:visible'));
  if(await source.inputValue()!==meta.source){await check();await source.fill(meta.source);await check();await source.blur();}
  if(await source.inputValue()!==meta.source)fail('METADATA_SOURCE_READBACK_FAILED');
  const category=await unique(page.locator('.video-human-type .select-controller:visible'));
  if((await category.innerText()).trim()!==meta.category_label){
   if(!['音乐','游戏','动画'].includes((await category.innerText()).trim()))fail('METADATA_CATEGORY_CHANGED');
   await check();await category.click();
   const candidates=page.locator('.video-human-type .select-container:visible').getByText(meta.category_label,{exact:true}).filter({visible:true});
   const selected=await unique(candidates,'METADATA_CATEGORY_OPTION_AMBIGUOUS');
   await check();await selected.click();
  }
  await waitValue(category,meta.category_label,'METADATA_CATEGORY_READBACK_FAILED',control=>control.innerText());
  let tags=(await tagValues()).map(s=>s.trim());
  for(const unwanted of tags.filter(s=>!meta.tags.includes(s))){
   // Bilibili seeds a small, category-dependent set of temporary tags on a
   // fresh upload. Remove only this explicit allowlist; any unrecognised tag
   // still stops for review rather than being silently discarded.
   if(!['MV','欧美音乐','剪辑','影视剪辑','短片','电视剧'].includes(unwanted))fail('METADATA_UNEXPECTED_EXISTING_TAG');
   const chips=page.locator('.label-item-v2-container:visible');const matches=[];
   for(let i=0;i<await chips.count();i++)if((await chips.nth(i).locator('p.label-item-v2-content').innerText()).trim()===unwanted)matches.push(chips.nth(i));
   if(matches.length!==1)fail('METADATA_TAG_AMBIGUOUS');
   const close=await unique(matches[0].locator('svg.close'));await check();await close.click();
   if((await tagValues()).map(s=>s.trim()).includes(unwanted))fail('METADATA_TAG_REMOVE_FAILED');
  }
  const input=await unique(page.locator('input[placeholder="按回车键Enter创建标签"]:visible'));
  for(const tag of meta.tags){
   tags=(await tagValues()).map(s=>s.trim());if(tags.includes(tag))continue;
   await check();await input.fill(tag);await page.waitForTimeout(500);await check();await input.press('Enter');
   const limit=Date.now()+5000;let matched=false;
   while(Date.now()<limit){
    await check();
    if((await tagValues()).map(s=>s.trim()).filter(s=>s===tag).length===1){matched=true;break;}
    await page.waitForTimeout(100);
   }
   if(!matched)fail('METADATA_TAG_READBACK_FAILED');
  }
  tags=(await tagValues()).map(s=>s.trim());
  if(tags.length!==meta.tags.length||new Set(tags).size!==tags.length||meta.tags.some(t=>!tags.includes(t)))fail('METADATA_FINAL_TAGS_MISMATCH');
  await check();
  const verified={...resultBase,state:'text_metadata_verified_cover_pending',title:await title.inputValue(),
   description:desc.text,creation_statement:await statement.inputValue(),source:await source.inputValue(),
   category:(await category.innerText()).trim(),tags,text_metadata_verified:true,cover_verified:false};
  await helpers.write('metadata-v5-readback.json',verified);
  return {...verified,...await helpers.inventory('text-metadata-v5-verified')};
 }catch(e){
  const result={...resultBase,state:'blocked',error_code:typeof e?.code==='string'&&/^METADATA_[A-Z_]+$/.test(e.code)?e.code:'METADATA_NATIVE_UI_ERROR'};
  await helpers.write('metadata-v5-blocked.json',result);await helpers.inventory('text-metadata-v5-blocked').catch(()=>{});return result;
 }
}
