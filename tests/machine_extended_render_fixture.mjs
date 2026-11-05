// Prototype-only owned source/model evidence. No real model or publisher calls.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {promisify} from 'node:util';
import {execFile} from 'node:child_process';
import {createRequire} from 'node:module';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {sha256,stable,walkPosts} from '../compositor/src/core.mjs';
const run=promisify(execFile),repo=path.dirname(path.dirname(fileURLToPath(import.meta.url))),out=path.resolve(process.argv[2]);
const sharp=createRequire(new URL('../compositor/package.json',import.meta.url))('sharp');
await fs.mkdir(out,{recursive:true});
const avatar=path.join(out,'owned-avatar.png'),photo=path.join(out,'owned-photo.png'),sourceImage=path.join(out,'owned-source.png');
const browser=await chromium.launch({headless:true});
try{const page=await browser.newPage({viewport:{width:600,height:200}});await page.route('**/*',r=>r.abort());
 await page.setContent('<article style="font-size:24px;background:white;padding:10px">Owned HTML source evidence<br>原推と返信、23:40 🥳<br>画像内テキストは翻訳していません</article>');
 await page.screenshot({path:sourceImage});await page.screenshot({path:avatar,clip:{x:0,y:0,width:40,height:40}});
}finally{await browser.close();}
await sharp({create:{width:120,height:80,channels:3,background:'#ccddee'}}).png().toFile(photo);
function post(id,name,handle,text,translation){return {id,url:`https://x.com/${handle}/status/${id}`,author:{name,handle,avatar,verified:false},
 originalText:text,translation,publishedAt:'2026-10-31T12:00:00+09:00',sourceTimezone:'Asia/Tokyo',
 sourceScreenshot:{file:sourceImage,capturedAt:'2026-10-31T12:30:00+09:00',reviewed:false},sourceMediaCount:1,sourceQuote:'none',
 media:[{file:photo,completeness:'complete',reviewed:false}],quote:null,relationship:{type:'original'},
 review:{sourceConfirmed:false,translationConfirmed:false},notes:[]};}
const parent=post('122','Owned parent fixture','parent_fixture','原推の本文 23:40 🥳','原推正文 23:40 🥳');
const reply=post('123','Owned reply fixture','reply_fixture','返信の本文 23:40 🥳','回复正文 23:40 🥳');reply.quote=parent;reply.sourceQuote='present';
reply.relationship={type:'reply',authorName:parent.author.name,authorHandle:parent.author.handle,url:parent.url};
const baseline={schemaVersion:1,displayTimezone:'Asia/Shanghai',post:reply},passed=[];
const info=async file=>({file,sha256:sha256(await fs.readFile(file))});
async function json(file,value){await fs.writeFile(file,JSON.stringify(value,null,2));return info(file);}
async function invoke(name,mutate=()=>{},change=async()=>{},envChange={}){
 const dir=path.join(out,name);await fs.mkdir(dir,{recursive:true});const input=structuredClone(baseline);mutate(input);
 const inputFile=path.join(dir,'input.json'),ticketFile=path.join(dir,'ticket.json');await fs.writeFile(inputFile,JSON.stringify(input,null,2));
 const nodes=[];walkPosts(input.post,(p,field)=>nodes.push({p,field}));const entries=[];
 for(const [i,{p,field}]of nodes.entries()){
  const refs=p.quote?[{type:p.relationship.type==='reply'?'replied_to':p.relationship.type==='repost'?'retweeted':'quoted',id:p.quote.id}]:
   p.relationship.type==='reply'?[{type:'replied_to',id:'122'}]:[];
  const payload={post:{id:p.id,text:p.originalText,created_at:p.publishedAt,referenced_tweets:refs},
   author:{name:p.author.name,username:p.author.handle},url:p.url,source_api:'owned_html_fixture',human_review:false};
  const payloadRef=await json(path.join(dir,`source-payload-${i}.json`),payload);
  const media=await Promise.all(p.media.map(async(m,index)=>({...await info(m.file),index:index+1,format:'png',width:120,height:80,completeness:'complete'})));
  const source={version:'machine-render-source-evidence-v2',evidence_kind:'owned_fixture',human_review:false,post_id:p.id,source_url:p.url,
   original_text_sha256:sha256(p.originalText),source_payload:payloadRef,source_screenshot:await info(p.sourceScreenshot.file),media,provenance:[payloadRef]};
  const sourceRef=await json(path.join(dir,`source-proof-${i}.json`),source);
  const receipts=[];for(const phase of ['translation','quality'])receipts.push({phase,...await json(path.join(dir,`${phase}-${i}.json`),
   {version:'owned_model_receipt_fixture_v1',phase,source_sha256:sha256(stable(payload)),translation_sha256:sha256(p.translation),human_review:false})});
  const assessment={version:'model-receipt-v1',proof_integrity_passed:true,separate_requests_verified:true,qa_recommends_pass:true,human_review:false,
   source_sha256:sha256(stable(payload)),translation_sha256:sha256(p.translation),call_ids:[crypto.randomUUID(),crypto.randomUUID()],
   request_keys:[sha256(`${name}-${i}-translation`),sha256(`${name}-${i}-quality`)],live_transport_verified:false,transport_modes:['custom_transport','custom_transport']};
  const modelRef=await json(path.join(dir,`model-proof-${i}.json`),{version:'machine-render-model-evidence-v2',evidence_kind:'owned_fixture',human_review:false,
   post_id:p.id,source_evidence_sha256:sourceRef.sha256,translation_sha256:sha256(p.translation),assessment,receipts});
  entries.push({field,post_id:p.id,original_text_sha256:sha256(p.originalText),translation_sha256:sha256(p.translation),source_evidence:sourceRef,model_evidence:modelRef});
 }
 const ticket={version:'machine-render-authorization-v2',policy:'machine-tweet-v2',purpose:'render_only',publication_authorized:false,human_review:false,
  input_sha256:sha256(await fs.readFile(inputFile)),nodes:entries};await change({dir,input,ticket,entries});await fs.writeFile(ticketFile,JSON.stringify(ticket,null,2));
 let result;try{result=await run(process.execPath,[path.join(repo,'adapters/render_machine_extended.mjs'),inputFile,path.join(dir,'rendered'),out,ticketFile],
  {cwd:path.join(repo,'compositor'),env:{...process.env,TEMP:out,TMP:out,TMPDIR:out,...envChange},timeout:150000,maxBuffer:200000});}catch(e){result=e;}
 return {dir,input,inputFile,ticket,result,output:path.join(dir,'rendered/machine-render-v2-result.json')};
}
const good=await invoke('valid-reply-media');assert.equal(good.result.code,undefined,good.result.stderr);
const report=JSON.parse(await fs.readFile(good.output,'utf8'));
assert.equal(report.machine_renderable,true);assert.equal(report.machine_publishable,false);assert.equal(report.publication_authorized,false);assert.equal(report.human_review,false);
assert.equal(report.prototype,true);assert.equal(report.upstream_human_publishable,false);assert.equal(report.nodes.length,2);
assert(report.nodes.every(n=>n.model_live_transport_verified===false&&n.human_review===false&&n.evidence_kind==='owned_fixture'));
assert(Object.values(report.checks).every(x=>x===true));assert.deepEqual(report.expected_quotes,['post.quote']);
assert.deepEqual(report.layout.translations.map(x=>x.field),['post.translation','post.quote.translation']);
assert.equal(report.layout.images.filter(i=>i.role==='media-image').length,2);
assert.equal(sha256(await fs.readFile(report.png.file)),report.png.sha256);
assert.equal(report.upstream_report.publishable,false);assert.equal(report.upstream_report.status,'blocked');
const blockedCodes=new Set(report.upstream_report.issues.filter(i=>i.severity==='error').map(i=>i.code));
assert.deepEqual([...blockedCodes].sort(),['MEDIA_UNREVIEWED','SCREENSHOT_UNREVIEWED','SOURCE_UNREVIEWED','TRANSLATION_UNREVIEWED']);
walkPosts(JSON.parse(await fs.readFile(good.inputFile,'utf8')).post,p=>{assert.equal(p.sourceScreenshot.reviewed,false);assert(!Object.values(p.review).some(Boolean));assert(p.media.every(m=>m.reviewed===false));});
passed.push('parent_reply_and_static_media_render_exactly_without_human_claim_or_publish_permission');
passed.push('all_quote_levels_translations_image_bytes_and_aspect_ratios_verified');
passed.push('upstream_manual_report_stays_blocked_and_every_input_flag_unchanged');
async function negative(name,mutate,change,code){const r=await invoke(name,mutate,change);assert.notEqual(r.result.code,undefined,name);
 if(code)assert(String(r.result.stderr).includes(code),String(r.result.stderr));await assert.rejects(fs.stat(r.output),{code:'ENOENT'});passed.push(name);return r;}
await negative('wrong_input_hash_rejected',()=>{},async({ticket})=>ticket.input_sha256='0'.repeat(64),'MACHINE_EXTENDED_TICKET_INVALID');
await negative('prototype_ticket_cannot_claim_publish_authority',()=>{},async({ticket})=>ticket.publication_authorized=true,'MACHINE_EXTENDED_TICKET_INVALID');
await negative('boolean_only_evidence_rejected',()=>{},async({ticket})=>ticket.nodes[0].source_evidence=true,'MACHINE_EXTENDED_EVIDENCE_BINDING_INVALID');
await negative('forged_human_confirmation_rejected',v=>v.post.quote.review.sourceConfirmed=true,async()=>{},'MACHINE_EXTENDED_NODE_BINDING_INVALID');
await negative('unknown_media_completeness_rejected',v=>v.post.media[0].completeness='unknown',async()=>{},'MACHINE_EXTENDED_NODE_BINDING_INVALID');
await negative('cropped_media_rejected',v=>v.post.media[0].completeness='cropped',async()=>{},'MACHINE_EXTENDED_NODE_BINDING_INVALID');
await negative('missing_parent_layer_rejected',v=>{v.post.quote=null;v.post.sourceQuote='none';},async()=>{},'MACHINE_EXTENDED_RELATIONSHIP_BINDING_INVALID');
await negative('relationship_author_mismatch_rejected',v=>v.post.relationship.authorHandle='wrong_parent',async()=>{},'MACHINE_EXTENDED_RELATIONSHIP_BINDING_INVALID');
await negative('missing_node_evidence_rejected',()=>{},async({ticket})=>ticket.nodes.pop(),'MACHINE_EXTENDED_NODE_SET_INVALID');
await negative('failed_qa_not_overridden',()=>{},async({ticket,entries})=>{const ref=entries[0].model_evidence,m=JSON.parse(await fs.readFile(ref.file,'utf8'));m.assessment.qa_recommends_pass=false;
 ref.sha256=(await json(ref.file,m)).sha256;ticket.nodes[0].model_evidence=ref;},'MACHINE_EXTENDED_MODEL_BINDING_INVALID');
await negative('changed_model_receipt_bytes_rejected',()=>{},async({dir})=>fs.appendFile(path.join(dir,'translation-0.json'),' '),'MACHINE_EXTENDED_EVIDENCE_HASH_CHANGED');
await negative('consistent_hashes_do_not_replace_source_text_binding',()=>{},async({entries,ticket})=>{
 const sourceRef=entries[0].source_evidence,s=JSON.parse(await fs.readFile(sourceRef.file,'utf8'));
 const payload=JSON.parse(await fs.readFile(s.source_payload.file,'utf8'));payload.post.text='Different owned fixture source';
 s.source_payload=await json(s.source_payload.file,payload);s.provenance=[s.source_payload];sourceRef.sha256=(await json(sourceRef.file,s)).sha256;
 const modelRef=entries[0].model_evidence,m=JSON.parse(await fs.readFile(modelRef.file,'utf8'));m.source_evidence_sha256=sourceRef.sha256;
 modelRef.sha256=(await json(modelRef.file,m)).sha256;ticket.nodes[0].source_evidence=sourceRef;ticket.nodes[0].model_evidence=modelRef;
},'MACHINE_EXTENDED_SOURCE_BINDING_INVALID');
const assetError=await negative('real_missing_avatar_is_not_ignored',v=>v.post.author.avatar=path.join(out,'missing-avatar.png'),async()=>{},'MACHINE_EXTENDED_UPSTREAM_TECHNICAL_CHECK_FAILED');
const glyphError=await negative('missing_font_glyph_is_not_ignored',v=>v.post.translation='\u{10ffff}',async()=>{},'MACHINE_EXTENDED_UPSTREAM_TECHNICAL_CHECK_FAILED');
const canvasError=await negative('oversized_canvas_is_not_ignored',v=>v.post.translation='测'.repeat(8000),async()=>{},'MACHINE_EXTENDED_UPSTREAM_TECHNICAL_CHECK_FAILED');
async function upstreamHas(folder,code){for(const e of await fs.readdir(folder,{withFileTypes:true})){const file=path.join(folder,e.name);
 if(e.isDirectory()){if(await upstreamHas(file,code))return true;}else if(e.name.endsWith('.report.json')){
  const r=JSON.parse(await fs.readFile(file,'utf8'));if(r.issues?.some(i=>i.code===code))return true;}}return false;}
assert(await upstreamHas(assetError.dir,'ASSET_INVALID'));assert(await upstreamHas(glyphError.dir,'GLYPH_MISSING'));assert(await upstreamHas(canvasError.dir,'CANVAS_LIMIT'));
passed.push('real_upstream_failure_codes_remain_in_saved_technical_reports');
const outside=path.join(path.dirname(out),'outside-owned-sensitive-fixture.txt'),sentinel='owned-test-sentinel-not-a-real-secret';
await fs.writeFile(outside,sentinel);let outsideError;
try{await run(process.execPath,[path.join(repo,'adapters/render_machine_extended.mjs'),outside,path.join(out,'outside-attempt'),out,path.join(good.dir,'ticket.json')],
 {cwd:path.join(repo,'compositor'),env:process.env,timeout:10000,maxBuffer:100000});}catch(e){outsideError=e;}
assert(outsideError);assert(String(outsideError.stderr).includes('MACHINE_EXTENDED_EVIDENCE_PATH_INVALID'));
assert(!String(outsideError.stderr).includes(sentinel));passed.push('outside_root_input_rejected_before_reading_or_printing_its_content');
console.log(JSON.stringify({passed,external_calls:0,model_calls:0,publish_requests:0,human_review:false,publication_authorized:false,report_file:good.output}));
