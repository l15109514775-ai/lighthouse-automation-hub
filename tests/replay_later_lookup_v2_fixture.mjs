// Owned official-origin HTML and actual local FFmpeg/HTMLVideoElement playback.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {randomUUID} from 'node:crypto';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {canonicalDigest} from '../scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs';
import {observeLaterReplayResult} from '../adapters/replay_later_result_lookup_v2.mjs';
const scenario=process.argv[2],root=path.resolve(process.argv[3]);assert.ok(['nonempty','duplicate','wrong_source','viewer_not_owner'].includes(scenario));
const job=randomUUID(),attempt=randomUUID(),base=path.join(root,'media',job,'replay-browser-highest-v2',attempt);await fs.mkdir(base,{recursive:true});
const file=path.join(root,'owned.mp4'),run=spawnSync('ffmpeg',['-v','error','-f','lavfi','-i','color=c=blue:s=128x72:r=10','-t','1','-c:v','libx264','-threads','1','-movflags','+faststart','-y',file],{encoding:'utf8',windowsHide:true});assert.equal(run.status,0);const video=await fs.readFile(file);
const source='https://www.youtube.com/watch?v=9T5tn1WGlJg',metadata={title:'Owned complete replay',description:'Owned original title\n'+source};
const spec={version:'replay-highest-native-browser-manifest-v2',job_id:job,attempt_id:attempt,target_site_uid:'123456',source_id:'9T5tn1WGlJg',source_url:source,
 metadata,metadata_sha256:canonicalDigest(metadata),binding_sha256:'b'.repeat(64),duration_seconds:1};
const bv='BV1234567890',url='https://www.bilibili.com/video/'+bv+'/';let external=0,interactions=0;
const browser=await chromium.launch({headless:true,chromiumSandbox:true}),context=await browser.newContext();
await context.route('**/*',async r=>{
 const u=new URL(r.request().url());let body;
 if(u.href==='https://t.bilibili.com/')body='<a class="header-entry-mini" href="https://space.bilibili.com/123456/">Owned viewer</a>';
 else if(u.href==='https://member.bilibili.com/platform/upload-manager/article'){
  const card=(title,id)=>`<div class="article-card clearfix v2"><div class="meta-title"><a class="name ellipsis" href="https://www.bilibili.com/video/${id}/">${title}</a></div><a class="cover-wrp" href="https://www.bilibili.com/video/${id}/"><span class="duration">00:00:01</span></a><div class="pubdate is-success"><span class="date">2026年11月02日 10:13:11</span></div></div>`;
  body='<body class="risk-captcha-adapt risk-captcha-adapt-pc"><div>稿件管理</div><a href="/platform/upload-manager/article">视频管理</a><a href="/platform/upload-manager/opus">图文管理</a><a href="/platform/upload-manager/inactArticle">互动视频管理</a><div>全部稿件 42</div><div>草稿 2</div>'+card('Unrelated historical replay','BVabcdefghij')+card(metadata.title,bv)+(scenario==='duplicate'?card(metadata.title,bv):'');
 }else if(u.href===url)body='<h1>'+metadata.title+'</h1><a class="header-entry-mini" href="https://space.bilibili.com/123456/">Viewer header</a><div class="up-info-container"><a class="up-name" href="https://space.bilibili.com/'+(scenario==='viewer_not_owner'?'999999':'123456')+'/">Owned uploader</a></div><div class="basic-desc-info">'+(scenario==='wrong_source'?'Other source':source+'\n'+metadata.description)+'</div><video muted autoplay controls src="/owned.mp4" style="width:640px;height:360px"></video>';
 else if(u.href==='https://www.bilibili.com/owned.mp4'){
  const match=/^bytes=(\d+)-(\d*)$/.exec(r.request().headers().range||'');
  const start=match?Number(match[1]):0,end=match&&match[2]?Math.min(Number(match[2]),video.length-1):video.length-1;
  await r.fulfill({status:match?206:200,contentType:'video/mp4',headers:{'accept-ranges':'bytes','content-length':String(end-start+1),...(match?{'content-range':`bytes ${start}-${end}/${video.length}`}:{})},body:video.subarray(start,end+1)});return;
 }
 else{external++;await r.abort();return;}
 await r.fulfill({status:200,contentType:'text/html',headers:{'x-owned-fixture':'true'},body:'<!doctype html><meta charset="utf-8"><style>.basic-desc-info{white-space:pre-wrap}.article-card{padding:10px}a{display:inline-block;min-height:20px}</style>'+body+'<script>document.addEventListener("click",()=>console.log("OWNED_INTERACTION"));</script>'});
});
context.on('page',p=>p.on('console',m=>{if(m.text()==='OWNED_INTERACTION')interactions++;}));
const page=await context.newPage();let result,error;
try{
 try{result=await observeLaterReplayResult({page,context,spec,root,publicTimeoutMs:1500});}catch(e){error=e.code;}
 if(scenario==='duplicate')assert.equal(error,'REPLAY_LATER_MANAGER_AMBIGUOUS');
 else if(scenario==='wrong_source')assert.equal(error,'REPLAY_LATER_PUBLIC_METADATA_CHANGED');
 else if(scenario==='viewer_not_owner')assert.equal(result.state,'public_result_not_ready');
 else{
  assert.equal(error,undefined);assert.equal(result.state,'later_native_evidence_observed');assert.equal(result.same_submit_attempt_observed,false);
  const composite=JSON.parse(await fs.readFile(result.reconciliation_observation.file,'utf8'));
  const manager=JSON.parse(await fs.readFile(composite.manager_observation.file,'utf8')),pub=JSON.parse(await fs.readFile(composite.public_observation.file,'utf8'));
  assert.equal(manager.visible_dom.rows.length,2);assert.equal(manager.selected_row.title_links.length,1);assert.equal(pub.visible_dom.videos[0].duration_seconds,1);
  assert.equal(pub.visible_dom.videos[0].ready_state>=2,true);assert.equal(pub.visible_dom.videos[0].seekable_ranges.length>0,true);
 }
 assert.equal(external,0);assert.equal(interactions,0);console.log(JSON.stringify({scenario,owned:true,real_video:true,external_requests:external,native_clicks:interactions,publications:0}));
}finally{await context.close();await browser.close();}
