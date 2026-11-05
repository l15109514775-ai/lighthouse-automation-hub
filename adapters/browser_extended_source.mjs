// Extended public DOM source packets. This module cannot approve or publish content.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {captureTweet} from './browser_tweet_capture.mjs';
import {captureReplyMachineV2} from './browser_reply_machine_v2.mjs';
import {inspectPlainTextSource} from './browser_source_facts.mjs';
import {inspectMachineMediaArticle,captureMachineMedia,verifyMachineMediaBundle} from './machine_media_capture.mjs';
import {captureMachineLinkText,normalizeMachineLinkPayload,verifyMachineLinkNormalization} from './machine_link_text.mjs';
import {locateTweetArticle,inspectArticleElement,assertSourceVisible,ObservationError} from './browser_observation.mjs';
import {publicXAccessError} from './browser_x_public.mjs';
import {positionReplyThread} from './browser_source_position.mjs';
const VERSION='x-web-extended-observation-v1',RULE='adjacent-list-avatar-connector-parent-day-reread-v2';
const hash=b=>crypto.createHash('sha256').update(b).digest('hex');
const canonical=x=>JSON.stringify(x,(_,v)=>v&&typeof v==='object'&&!Array.isArray(v)?Object.fromEntries(Object.keys(v).sort().map(k=>[k,v[k]])):v);
const check=(value,code)=>{if(!value)throw new ObservationError(code);};
async function descriptor(file){return {file,sha256:hash(await fs.readFile(file))};}
async function save(file,value){await fs.writeFile(file,JSON.stringify(value,null,2),{flag:'wx',mode:0o600});return descriptor(file);}

export function extendedSourceEnvelope(source,trigger='durable_job'){
 const result=structuredClone(source);
 result.capture={...result.capture,underlying_protocol:result.capture.protocol,protocol:VERSION,
  orchestration_trigger:trigger,scheduled_server_collection:trigger==='durable_scheduler'};
 return result;
}

function pairContext(parent){return {version:'reply-machine-source-v2',parent_source:structuredClone(parent),
 machine_relationship_rule:RULE,parent_time_basis:{value:parent.post.created_at,source:'independent_parent_permalink'},
 relationship_verified_by_human:false,human_review:false,official_api:false};}

// A reused thread/reread payload is never justified by the same ID alone.
// Compare the actual current native author, text and exact visible time basis.
function requireCurrentSource(raw,identity,facts){
 let published=identity.datetime,precision='dom_datetime';
 if(!published){
  const m=/^(\d{1,2}):(\d{2})\s*[·•]\s*(\d{4})年(\d{1,2})月(\d{1,2})日$/.exec(identity.visible_timestamp);
  check(m&&Number(m[1])<=23&&Number(m[2])<=59,'EXTENDED_CURRENT_SOURCE_TIMESTAMP_INVALID');
  published=`${m[3]}-${m[4].padStart(2,'0')}-${m[5].padStart(2,'0')}T${m[1].padStart(2,'0')}:${m[2]}:00+08:00`;
  precision='minute';
 }
 check(identity.author_verified&&identity.postId===raw.post.id&&identity.handle.toLowerCase()===raw.author.username.toLowerCase()&&
  identity.display_name===raw.author.name&&facts.observed_text===raw.post.text&&published===raw.post.created_at&&
  precision===raw.post.timestamp_precision,'EXTENDED_CURRENT_SOURCE_CHANGED');
}

async function plainNode(page,identity,folder,role,{rawRecord=null,rawObservation=null,mediaBundle=null,trigger='durable_job'}={}){
 await fs.mkdir(folder,{recursive:true,mode:0o700});
 let raw=rawRecord,bundle=mediaBundle;
 if(!raw){
  const article=await locateTweetArticle(page,identity),inventory=await article.evaluate(inspectMachineMediaArticle,{...identity,source_mode:'public_dom_full_dimensions'});
  if(inventory.photos.length){
   bundle=await captureMachineMedia(page,identity,path.join(folder,'media'),{allowedRoot:folder,sourceMode:'public_dom_full_dimensions',normalizeLinks:true});
   raw=bundle.source;rawObservation=path.join(folder,'media/source/observation.json');
  }else{
   raw=await captureTweet(page,identity,path.join(folder,'source'),{machineSource:true,trigger:'extended_public_dom'});
   rawObservation=path.join(folder,'source/observation.json');
  }
 }
 const article=await locateTweetArticle(page,identity);
 if(rawRecord)await assertSourceVisible(page,article,path.join(folder,'reused-source-before'));
 const facts=await article.evaluate(inspectPlainTextSource),currentIdentity=await article.evaluate(inspectArticleElement);
 requireCurrentSource(raw.payload,currentIdentity,facts);
 const observation=await captureMachineLinkText(page,identity),normalization=normalizeMachineLinkPayload(raw.payload,observation);
 verifyMachineLinkNormalization(raw.payload,observation,normalization);
 if(rawRecord)await assertSourceVisible(page,article,path.join(folder,'reused-source-after'));
 const finalFacts=await article.evaluate(inspectPlainTextSource),finalLinks=await captureMachineLinkText(page,identity),finalIdentity=await article.evaluate(inspectArticleElement);
 requireCurrentSource(raw.payload,finalIdentity,finalFacts);
 check(canonical(currentIdentity)===canonical(finalIdentity)&&canonical(facts)===canonical(finalFacts)&&
  canonical(observation)===canonical(finalLinks),'EXTENDED_CURRENT_SOURCE_CHANGED');
 const linkFile=path.join(folder,'link-normalization.json'),link=await save(linkFile,{observation,...normalization});
 const source=extendedSourceEnvelope(normalization.derived_payload,trigger),screenshot=await descriptor(raw.screenshot_file);
 const provenance=[await descriptor(rawObservation),link];
 if(bundle)provenance.push(await descriptor(path.join(bundle.evidence_dir,'machine-media-manifest.json')));
 return {role,source,raw_source:raw.payload,raw_observation:await descriptor(rawObservation),screenshot,
  dom_facts:facts,link_normalization:{observation,...normalization},media_bundle:bundle,provenance};
}

export async function captureExtendedTweet(page,identity,evidenceDir,{trigger='durable_job'}={}){
 await fs.mkdir(evidenceDir,{recursive:true,mode:0o700});
 const disk=await fs.statfs(evidenceDir);check(Number(disk.bavail)*Number(disk.bsize)>=15_000_000_000+84_000_000,'DISK_LOW');
 const article=await locateTweetArticle(page,identity),facts=await article.evaluate(inspectPlainTextSource);
 let nodes,replyPacket=null;
 if(facts.preceding_or_reply){
  const pair=await captureReplyMachineV2(page,identity,path.join(evidenceDir,'reply-machine-v2'));
  replyPacket=pair.packet;
  const parentWrapper=JSON.parse(await fs.readFile(replyPacket.parent_reread.file,'utf8'));
  const parentRaw={payload:parentWrapper.payload,screenshot_file:parentWrapper.screenshot.file};
  const parentIdentity={handle:parentRaw.payload.author.username,postId:parentRaw.payload.post.id};
  const parent=await plainNode(page,parentIdentity,path.join(evidenceDir,'parent-node'),'parent',
   {rawRecord:parentRaw,rawObservation:replyPacket.parent_reread.file,trigger});
  const response=await page.goto(`https://x.com/${identity.handle}/status/${identity.postId}`,{waitUntil:'domcontentloaded'});
  const error=publicXAccessError(page.url(),response?.status());if(error)throw new ObservationError(error);
  await page.locator('article').first().waitFor({state:'visible'});
  await page.waitForFunction(()=>[...document.querySelectorAll('article')].slice(0,2).every(a=>[...a.querySelectorAll('img')].every(i=>i.complete)),null,{timeout:10000});
  await positionReplyThread(page,identity,path.join(evidenceDir,'reply-node-positioning'));
  const replyWrapper=JSON.parse(await fs.readFile(replyPacket.reply_in_thread.file,'utf8'));
  const post=await plainNode(page,identity,path.join(evidenceDir,'post-node'),'post',
   {rawRecord:{payload:replyWrapper.payload,screenshot_file:replyWrapper.screenshot.file},rawObservation:replyPacket.reply_in_thread.file,trigger});
  post.source.post.referenced_tweets=[{type:'replied_to',id:parent.source.post.id}];post.source.reply_context=pairContext(parent.source);
  nodes=[post,parent];
 }else nodes=[await plainNode(page,identity,path.join(evidenceDir,'post-node'),'post',{trigger})];
 const payload=nodes[0].source;
 const record={version:VERSION,payload,screenshot_file:nodes[0].screenshot.file,human_review:false,
  machine_extended_source:{version:'extended-dom-source-v1',nodes,reply_packet:replyPacket,collector_trigger:trigger,
   human_review:false,official_api:false,automatic_publication_eligible:false}};
 await verifyExtendedObservation(record,{allowedRoot:evidenceDir});
 await save(path.join(evidenceDir,'extended-observation.json'),record);
 return record;
}

export async function verifyExtendedObservation(record,{allowedRoot,requireLive=true}={}){
 const root=await fs.realpath(allowedRoot);
 async function bound(item){
  const real=await fs.realpath(item.file),relative=path.relative(root,real);
  check(relative&&!relative.startsWith('..')&&!path.isAbsolute(relative)&&!(await fs.lstat(item.file)).isSymbolicLink(),'EXTENDED_ARTIFACT_PATH_INVALID');
  const bytes=await fs.readFile(real);check(hash(bytes)===item.sha256,'EXTENDED_ARTIFACT_HASH_CHANGED');return bytes;
 }
 const ext=record.machine_extended_source;
 check(record.version===VERSION&&ext?.version==='extended-dom-source-v1'&&ext.human_review===false&&ext.official_api===false&&
  canonical(ext.nodes.map(n=>n.role))===canonical(ext.reply_packet?['post','parent']:['post']),'EXTENDED_PACKET_SHAPE_INVALID');
 const proofs=[];
 for(const node of ext.nodes){
  const raw=node.raw_source,facts=node.dom_facts;
  check(raw?.capture?.human_review===false&&raw.capture.source_screenshot_reviewed===false&&raw.capture.official_api_receipt===false&&
   raw.capture.screenshot_sha256===node.screenshot.sha256&&raw.post.id===facts.status_ids[0]&&facts.status_ids.length===1&&
   facts.observed_text===raw.post.text&&facts.body_count===1&&!facts.show_more&&!facts.clipped&&facts.human_review===false&&facts.official_api===false&&
   facts.special_card_kinds.length===0&&facts.css_media_count===0&&facts.unclassified_svg_count===0&&facts.extra_links.length===0&&
   !facts.images.some(i=>i.inside_body||!i.loaded),'EXTENDED_SOURCE_DOM_UNSUPPORTED');
  const rawObservation=JSON.parse((await bound(node.raw_observation)).toString('utf8'));
  check(canonical(rawObservation.payload)===canonical(raw),'EXTENDED_RAW_SOURCE_CHANGED');
  await bound(node.screenshot);for(const item of node.provenance)await bound(item);
  const link=node.link_normalization;verifyMachineLinkNormalization(raw,link.observation,{derived_payload:link.derived_payload,evidence:link.evidence});
  let expected=extendedSourceEnvelope(link.derived_payload,ext.collector_trigger);
  if(ext.reply_packet&&node.role==='post'){
   const parent=ext.nodes.find(n=>n.role==='parent');expected.post.referenced_tweets=[{type:'replied_to',id:parent.source.post.id}];
   expected.reply_context=pairContext(parent.source);
  }
  check(canonical(expected)===canonical(node.source)&&node.source.capture.full_source_completeness_verified===false,'EXTENDED_CANONICAL_SOURCE_CHANGED');
  let mediaProof=null;
  if(node.media_bundle){
   check(canonical(node.media_bundle.source.payload)===canonical(raw)&&canonical(node.media_bundle.text_facts)===canonical(facts),
    'EXTENDED_MEDIA_SOURCE_CHANGED');
   mediaProof=await verifyMachineMediaBundle(node.media_bundle,{requireLive});
  }else check(facts.foreign_image_count===0&&facts.unsupported_elements===0&&
   raw.capture.machine_facts_sha256===hash(canonical(facts)),'EXTENDED_UNBOUND_MEDIA');
  const permitted=new Set(['LINK_DISPLAY_TRUNCATED',...(ext.reply_packet?['REPLY_CONTEXT_REQUIRED']:[]),...(node.media_bundle?['MEDIA_OR_CARD_UNSUPPORTED']:[])]);
  check(facts.failures.every(f=>permitted.has(f))&&(!facts.preceding_or_reply||!!ext.reply_packet),'EXTENDED_SOURCE_CONTEXT_UNBOUND');
  proofs.push({role:node.role,raw_source_sha256:hash(canonical(raw)),source_sha256:hash(canonical(node.source)),
   screenshot_sha256:node.screenshot.sha256,dom_facts_sha256:hash(canonical(facts)),link_evidence_sha256:hash(canonical(link.evidence)),
   media_proof:mediaProof,human_review:false,official_api:false});
 }
 check(canonical(ext.nodes[0].source)===canonical(record.payload)&&record.screenshot_file===ext.nodes[0].screenshot.file,'EXTENDED_POST_SOURCE_CHANGED');
 return {version:'extended-node-integrity-v1',integrity_verified:true,nodes:proofs,human_review:false,official_api:false,
  automatic_publication_eligible:false};
}

if(process.argv[2]==='--verify'){
 try{
  const record=JSON.parse(await fs.readFile(process.argv[3],'utf8'));
  console.log(JSON.stringify(await verifyExtendedObservation(record,{allowedRoot:process.argv[4]})));
 }catch(e){console.log(JSON.stringify({error_code:e.code||'EXTENDED_NODE_VALIDATION_FAILED'}));process.exitCode=1;}
}
