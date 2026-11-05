// Owned Chromium public lookup only. Recorded metadata is never published.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {randomUUID} from 'node:crypto';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {observeLaterReplayResult} from '../adapters/replay_later_result_lookup_v2.mjs';
const [rootArg,scenario]=process.argv.slice(2),root=path.resolve(rootArg);
assert.ok(['recorded','changed_body','changed_space'].includes(scenario));
const recorded=JSON.parse(await fs.readFile(new URL('./fixtures/public_description_display_v2.json',import.meta.url),'utf8'));
const spec={version:'replay-part-native-browser-manifest-v5',job_id:randomUUID(),attempt_id:randomUUID(),
 target_site_uid:'1000000000000000',account_name:'Owned publisher',source_id:'YsSXSIYtCoE',source_url:recorded.source_url,
 metadata:recorded.expected_metadata,metadata_sha256:recorded.metadata_sha256,binding_sha256:'b'.repeat(64),duration_seconds:1,
 parent_job_id:randomUUID(),part_index:0,part_count:1,part_idempotency_key:'c'.repeat(64),whole_plan_sha256:'d'.repeat(64),
 parts_manifest_sha256:'e'.repeat(64),artifact_sha256:'f'.repeat(64),file_bytes:100,policy_sha256:'a'.repeat(64)};
const base=path.join(root,'media',spec.job_id,'replay-browser-part-v5',spec.attempt_id);await fs.mkdir(base,{recursive:true});
const mediaFile=path.join(root,'owned.mp4');assert.equal(spawnSync('ffmpeg',['-v','error','-f','lavfi','-i','color=blue:s=128x72:r=10','-t','1','-c:v','libx264','-threads','1','-y',mediaFile]).status,0);
const media=await fs.readFile(mediaFile),bv='BV15o4M6nEiC';let external=0,clicks=0;
let raw=recorded.observed_description;
if(scenario==='changed_body')raw=raw.replace('日语原声','日语假文');
if(scenario==='changed_space')raw=raw.replace('\u3000',' ');
const esc=s=>s.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const browser=await chromium.launch({headless:true,chromiumSandbox:true}),context=await browser.newContext();
await context.exposeBinding('forbiddenClick',()=>clicks++);
await context.route('**/*',async route=>{
 const url=new URL(route.request().url());let html;
 if(url.href==='https://t.bilibili.com/')html='<a class="header-entry-mini" href="https://space.bilibili.com/'+spec.target_site_uid+'/">Owned identity</a>';
 else if(url.href==='https://member.bilibili.com/platform/upload-manager/article'){
  html='<div>稿件管理</div><a href="/platform/upload-manager/article">视频管理</a><a href="/platform/upload-manager/opus">图文管理</a><a href="/platform/upload-manager/inactArticle">互动视频管理</a><div>全部稿件 1</div><div class="article-card clearfix v2"><a class="name ellipsis" href="//www.bilibili.com/video/'+bv+'/">'+esc(spec.metadata.title)+'</a><a class="cover-wrp" href="//www.bilibili.com/video/'+bv+'/"><span class="duration">00:00:01</span></a><div class="pubdate is-success"><span class="date">2026年11月03日 13:00:00</span></div></div>';
 }else if(url.origin==='https://www.bilibili.com'&&url.pathname==='/video/'+bv+'/'){
  html='<h1>'+esc(spec.metadata.title)+'</h1><div class="up-info-container"><a class="up-name" href="https://space.bilibili.com/'+spec.target_site_uid+'/">Owned publisher</a></div><div class="basic-desc-info" style="white-space:pre-wrap">'+esc(raw)+'</div><video muted autoplay controls width=320 height=180 src="/owned.mp4"></video>';
 }else if(url.href==='https://www.bilibili.com/owned.mp4'){
  const m=/^bytes=(\d+)-(\d*)$/.exec(route.request().headers().range||''),start=m?Number(m[1]):0,end=m&&m[2]?Math.min(Number(m[2]),media.length-1):media.length-1;
  await route.fulfill({status:m?206:200,contentType:'video/mp4',headers:{'content-length':String(end-start+1),'accept-ranges':'bytes',...(m?{'content-range':`bytes ${start}-${end}/${media.length}`}:{})},body:media.subarray(start,end+1)});return;
 }else{external++;await route.abort();return;}
 await route.fulfill({status:200,contentType:'text/html',body:'<!doctype html><meta charset=utf-8><style>a{display:inline-block}h1{font-size:18px}</style>'+html+'<script>document.addEventListener("click",()=>forbiddenClick())</script>'});
});
let result,error;
try{
 try{result=await observeLaterReplayResult({page:await context.newPage(),context,spec,root,publicTimeoutMs:1500});}catch(e){error=e.code;}
 const parent=path.join(base,'later-lookups-v2'),names=await fs.readdir(parent);assert.equal(names.length,1);const folder=path.join(parent,names[0]);
 if(scenario==='recorded'){
  assert.equal(error,undefined);assert.equal(result.state,'later_native_evidence_observed');
  const proof=JSON.parse(await fs.readFile(path.join(folder,'public-native-inventory.json'),'utf8'));
  assert.equal(proof.visible_dom.descriptions[0].text,raw);assert.equal(proof.binding_sha256,spec.binding_sha256);
  assert.equal(proof.description_display_evidence.version,'bilibili-public-description-display-v2');
  assert.equal(proof.description_display_evidence.candidate,'source_prefix_once_word_joiners_omitted');
  assert.deepEqual(proof.description_display_evidence.omitted_word_joiner_positions,[103]);
 }else{
  assert.equal(error,'REPLAY_LATER_PUBLIC_METADATA_CHANGED');
  const rejected=JSON.parse(await fs.readFile(path.join(folder,'public-native-metadata-rejected.json'),'utf8'));
  assert.equal(rejected.visible_dom.descriptions[0].text,raw);assert.equal(rejected.binding_sha256,spec.binding_sha256);
  assert.equal(rejected.reason,'description');assert.equal(rejected.submit_clicks,0);
 }
 assert.equal(clicks,0);assert.equal(external,0);
 console.log(JSON.stringify({scenario,owned:true,external_requests:external,submit_clicks:clicks}));
}finally{await context.close();await browser.close();}
