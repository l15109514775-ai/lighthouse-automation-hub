// Owned bytes/DOM only. Synthetic model/API evidence never claims live access.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {promisify} from 'node:util';
import {execFile} from 'node:child_process';
import {createRequire} from 'node:module';
import {sha256,stable} from '../compositor/src/core.mjs';
const run=promisify(execFile),repo=path.dirname(path.dirname(fileURLToPath(import.meta.url))),out=path.resolve(process.argv[2]);
const sharp=createRequire(new URL('../compositor/package.json',import.meta.url))('sharp');
await fs.mkdir(out,{recursive:true});
const avatar=path.join(out,'owned-avatar.png'),photo=path.join(out,'owned-photo.png');
await sharp({create:{width:40,height:40,channels:3,background:'#aabbcc'}}).png().toFile(avatar);
await sharp({create:{width:120,height:80,channels:3,background:'#ccddee'}}).png().toFile(photo);
const info=async file=>({file,sha256:sha256(await fs.readFile(file))});
const json=async(file,value)=>{await fs.writeFile(file,JSON.stringify(value,null,2));return info(file);};
async function invoke(name,mixed,change=()=>{}){
 const dir=path.join(out,name);await fs.mkdir(dir);const roles=['post',...(mixed?['parent']:[]),'quote'];
 const fields={post:'post',parent:'post.parent',quote:'post.quoted'},ids={post:'9103',parent:'9101',quote:'9102'};
 const posts={},sources={},media={};
 for(const [index,role]of roles.entries()){
  const handle=role==='post'?'owned_post':'owned_'+role,id=ids[role],original='本文 '+role+' 23:40',translation='完整正文 '+role+' 23:40';
  posts[role]={id,url:`https://x.com/${handle}/status/${id}`,author:{name:'Owned '+role,handle,avatar,verified:false},
   originalText:original,translation,publishedAt:'2026-11-01T12:00:00+09:00',sourceTimezone:'Asia/Tokyo',
   sourceScreenshot:{file:null,capturedAt:null,reviewed:false},sourceMediaCount:1,sourceQuote:role==='post'?'present':'none',
   media:[{file:photo,completeness:'complete',reviewed:false}],quote:null,relationship:{type:role==='post'?(mixed?'reply_quote':'quote'):'original'},
   review:{sourceConfirmed:false,translationConfirmed:false},notes:[]};
  media[role]=[{...await info(photo),index:1,format:'png',width:120,height:80,completeness:'complete',media_key:'owned-'+role,
   source_url:'https://pbs.twimg.com/media/Owned_'+role+'.png',transport_mode:'custom_transport'}];
  // Match the two actual acceptance shapes: the main post has no own photo;
  // only each directly replied-to/quoted target carries one complete photo.
  if(role==='post'){posts[role].media=[];posts[role].sourceMediaCount=0;media[role]=[];}
  const author={id:String(8100+index),name:posts[role].author.name,username:handle};
  const refs=role!=='post'?[]:[{type:'quoted',id:ids.quote},...(mixed?[{type:'replied_to',id:ids.parent}]:[])];
  const post={id,author_id:author.id,text:original,created_at:posts[role].publishedAt,referenced_tweets:refs,
   attachments:{media_keys:media[role].map(m=>m.media_key)},edit_history_tweet_ids:[id]};
  if(role==='post')delete post.attachments;
  sources[role]={post,author,url:posts[role].url,source_api:'x-v2-official',human_review:false,
   media:Object.fromEntries(media[role].map(m=>[m.media_key,{media_key:m.media_key,type:'photo',url:m.source_url,width:120,height:80}])),
   capture:{human_review:false,official_api_receipt:false},official_source:{local_media:media[role]}};
 }
 const context={version:'x-official-reference-context-v2',relationship_type:mixed?'reply_quote':'quote',
  relations:roles.filter(r=>r!=='post').map(role=>({type:role==='parent'?'replied_to':'quoted',post_id:ids[role],role,source:sources[role]})),
  expansion:{max_depth:1,complete:true,unexpanded_relations:[]},human_review:false};
 sources.post.reference_context=context;
 const input={machineSchemaVersion:'official-reference-cards-v2',displayTimezone:'Asia/Shanghai',post:posts.post,
  contextPosts:Object.fromEntries(roles.filter(r=>r!=='post').map(r=>[r,posts[r]])),reference_context:context};
 const entries=[];
 for(const role of roles){
  const p=posts[role],source=sources[role];
  const sourcePayload=await json(path.join(dir,role+'-payload.json'),source);
  const apiBody={data:source.post,includes:{users:[source.author],media:Object.values(source.media)}};
  const response=await json(path.join(dir,role+'-response.json'),apiBody);
  const authority=await json(path.join(dir,role+'-authority.json'),{version:'x-official-render-authority-v1',source_sha256:sha256(stable(source)),
   post_id:p.id,author_id:source.author.id,human_review:false,publication_authorized:false,api_calls:[{call_id:crypto.randomUUID(),
    method:'GET',host:'api.x.com',path:'/2/tweets/'+p.id,params:{'tweet.fields':'author_id,created_at'},http_status_class:'2xx',
    captured_at:Date.parse(p.publishedAt)/1000+300,transport_mode:'custom_transport',response,canonical_response_sha256:sha256(stable(apiBody))}]});
  const sourceRef=await json(path.join(dir,role+'-source-proof.json'),{version:'machine-render-source-evidence-api-v2',evidence_kind:'owned_fixture',
   human_review:false,post_id:p.id,source_url:p.url,source_authority:'x-v2-official',original_text_sha256:sha256(p.originalText),
   source_payload:sourcePayload,source_screenshot:null,media:media[role],provenance:[response,authority,...media[role].map(m=>({file:m.file,sha256:m.sha256}))]});
  const receipts=[];
  for(const phase of ['translation','quality'])receipts.push({phase,...await json(path.join(dir,role+'-'+phase+'.json'),{owned:true,phase,human_review:false})});
  const modelRef=await json(path.join(dir,role+'-model-proof.json'),{version:'machine-render-model-evidence-api-v2',contract_version:'model-text-contract-v7',
   evidence_kind:'owned_fixture',human_review:false,post_id:p.id,source_evidence_sha256:sourceRef.sha256,translation_sha256:sha256(p.translation),receipts,
   assessment:{version:'model-receipt-v1',proof_integrity_passed:true,separate_requests_verified:true,qa_recommends_pass:true,human_review:false,
    source_sha256:sha256(stable(source)),translation_sha256:sha256(p.translation),call_ids:[crypto.randomUUID(),crypto.randomUUID()],
    request_keys:[sha256(role+'-translation'),sha256(role+'-qa')],live_transport_verified:false,transport_modes:['custom_transport','custom_transport']}});
  entries.push({field:fields[role],post_id:p.id,original_text_sha256:sha256(p.originalText),translation_sha256:sha256(p.translation),
   source_evidence:sourceRef,model_evidence:modelRef});
 }
 const inputFile=path.join(dir,'input.json'),ticketFile=path.join(dir,'ticket.json');
 const ticket={version:'machine-render-authorization-api-v2',policy:'machine-tweet-v4-api-relations',purpose:'render_only',human_review:false,
  publication_authorized:false,nodes:entries};
 await change({input,ticket,entries,dir});await fs.writeFile(inputFile,JSON.stringify(input,null,2));
 ticket.input_sha256=sha256(await fs.readFile(inputFile));await fs.writeFile(ticketFile,JSON.stringify(ticket,null,2));
 let result;try{result=await run(process.execPath,[path.join(repo,'adapters/render_machine_official_relations.mjs'),inputFile,path.join(dir,'rendered'),out,ticketFile],
  {cwd:repo,env:{...process.env,TEMP:out,TMP:out,TMPDIR:out},timeout:150000,maxBuffer:200000});}catch(e){result=e;}
 return {result,output:path.join(dir,'rendered/machine-render-api-v2-result.json'),roles,input};
}
const reports=[];
for(const mixed of [false,true]){
 const good=await invoke(mixed?'mixed':'quote',mixed);assert.equal(good.result.code,undefined,good.result.stderr);
 const report=JSON.parse(await fs.readFile(good.output));reports.push(good.output);
 assert.equal(report.version,'machine-render-api-v2');assert.equal(report.human_review,false);assert.equal(report.publication_authorized,false);
 assert.equal(report.machine_publishable,false);assert(Object.values(report.checks).every(x=>x===true));
 assert.deepEqual(report.layout.cards,good.roles);assert.equal(report.layout.translations.length,good.roles.length);
 assert.equal(report.nodes.length,good.roles.length);assert(report.nodes.every(n=>n.human_review===false&&!n.model_live_transport_verified&&!n.source_api_live_transport_verified));
 assert.equal(report.layout.images.filter(i=>i.role==='media-image').length,good.roles.length-1);
 assert.equal(sha256(await fs.readFile(report.png.file)),report.png.sha256);
 for(const original of report.upstream_reports)assert.equal(original.publishable,false);
}
for(const [name,change,code]of [
 ['wrong-relation',async({input})=>{input.post.relationship={type:'reply'};},'MACHINE_OFFICIAL_REFERENCE_GRAPH_INVALID'],
 ['missing-parent',async({input})=>{delete input.contextPosts.parent;},'MACHINE_OFFICIAL_NODE_SET_INVALID'],
 ['failed-quote-qa',async({entries})=>{const ref=entries.at(-1).model_evidence,value=JSON.parse(await fs.readFile(ref.file));value.assessment.qa_recommends_pass=false;ref.sha256=(await json(ref.file,value)).sha256;},'MACHINE_OFFICIAL_MODEL_BINDING_INVALID']]){
 const bad=await invoke(name,true,change);assert.notEqual(bad.result.code,undefined,name);
 assert(String(bad.result.stderr).includes(code),bad.result.stderr);await assert.rejects(fs.stat(bad.output),{code:'ENOENT'});
}
console.log(JSON.stringify({reports,passed:5,external_calls:0,model_calls:0,publish_requests:0,human_review:false}));
