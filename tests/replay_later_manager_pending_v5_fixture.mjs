// Owned official-origin DOM derived from the two saved real manager captures.
// The video/identity are owned fixtures; no live account or publication claim.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {randomUUID} from 'node:crypto';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {canonicalDigest} from '../scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs';
import {observeLaterReplayResult} from '../adapters/replay_later_result_lookup_v2.mjs';

const [scenario,rootArg]=process.argv.slice(2),root=path.resolve(rootArg);
assert.ok(['space_public','space_pending','protocol_public','protocol_pending','pending_no_bv','known_conflict','cover_conflict','manager_loading_then_public','manager_loading_timeout'].includes(scenario));
const capture=JSON.parse(await fs.readFile(new URL('./fixtures/replay_manager_pending_v5.json',import.meta.url),'utf8'));
const actual=capture.published_row,bv=capture.candidate_bv,other='BV1234567890',title=actual.title_links[0].text+(scenario.startsWith('space_')?'  owned':'');
assert.equal(actual.title_links[0].raw_href,actual.covers[0].raw_href);
const job=randomUUID(),attempt=randomUUID(),base=path.join(root,'media',job,'replay-browser-part-v5',attempt);
await fs.mkdir(base,{recursive:true});
const file=path.join(root,'owned.mp4');
const generated=spawnSync('ffmpeg',['-v','error','-f','lavfi','-i','color=c=blue:s=128x72:r=10','-t','1','-c:v','libx264','-threads','1','-movflags','+faststart','-y',file],{encoding:'utf8',windowsHide:true});
assert.equal(generated.status,0);const video=await fs.readFile(file);
const source='https://www.youtube.com/watch?v=6wHqfDmNF0U',metadata={title,description:(scenario==='space_public'?'Owned  source description':'Owned source description')+'\n'+source};
const spec={version:'replay-part-native-browser-manifest-v5',job_id:job,attempt_id:attempt,target_site_uid:'123456',account_name:'Owned uploader',
 source_id:'6wHqfDmNF0U',source_url:source,metadata,metadata_sha256:canonicalDigest(metadata),binding_sha256:'b'.repeat(64),duration_seconds:1,
 parent_job_id:randomUUID(),part_index:0,part_count:1,part_idempotency_key:'c'.repeat(64),whole_plan_sha256:'d'.repeat(64),
 parts_manifest_sha256:'e'.repeat(64),artifact_sha256:'f'.repeat(64),file_bytes:video.length,policy_sha256:'a'.repeat(64)};
const pending=scenario==='space_pending'||scenario==='protocol_pending'||scenario==='pending_no_bv';let outside=0,clicks=0,choosers=0,publicNavigations=0;
const esc=s=>s.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const browser=await chromium.launch({headless:true,chromiumSandbox:true}),context=await browser.newContext();
await context.exposeBinding('ownedClick',()=>clicks++);
context.on('page',p=>p.on('filechooser',()=>choosers++));
await context.route('**/*',async route=>{
 const u=new URL(route.request().url());let body;
 if(u.href==='https://t.bilibili.com/')body='<a class="header-entry-mini" href="https://space.bilibili.com/123456/">Owned viewer</a>';
 else if(u.href==='https://member.bilibili.com/platform/upload-manager/article'){
  const raw=scenario==='pending_no_bv'?'javascript:void(0)':actual.title_links[0].raw_href;
  const cover=scenario==='cover_conflict'?'//www.bilibili.com/video/'+other+'/':raw;
  const status=pending?'<div>转码中</div><div>审核中</div>':'';
  const date=pending?'<div class="pubdate"><span class="date">'+capture.pending_date+'</span></div>':
   '<div class="pubdate is-success"><span class="date">'+actual.dates[0].text+'</span></div>';
  // The 00:00:00 pending duration is actual. Published duration is explicitly
  // replaced with the owned one-second movie, never claimed as a 6h playback.
  const card='<div class="article-card clearfix v2"><div class="meta-title"><a class="name ellipsis" href="'+esc(raw)+'">'+esc(title)+'</a></div>'+
   '<a class="cover-wrp" href="'+esc(cover)+'"><span class="duration">'+(pending?'00:00:00':'00:00:01')+'</span></a>'+date+status+'</div>';
  body='<body class="risk-captcha-adapt risk-captcha-adapt-pc"><div>稿件管理</div><a href="/platform/upload-manager/article">视频管理</a>'+
   '<a href="/platform/upload-manager/opus">图文管理</a><a href="/platform/upload-manager/inactArticle">互动视频管理</a><div>全部稿件 2</div><div>草稿 1</div>'+
   '<div class="article-card clearfix v2"><a class="name ellipsis" href="https://www.bilibili.com/video/'+other+'/">Unrelated pending title</a><div>审核中</div></div>'+card;
  if(scenario.startsWith('manager_loading_')){
   const full=body.replace(/^<body[^>]*>/,'');
   body='<body style="min-height:100vh" class="risk-captcha-adapt risk-captcha-adapt-pc"><aside>主站 投稿 首页 内容管理 稿件管理 申诉管理 字幕管理 数据中心</aside>'+
    (scenario==='manager_loading_then_public'?'<script>setTimeout(()=>document.body.innerHTML='+JSON.stringify(full)+',6200)</script>':'');
  }
 }else if(u.href==='https://www.bilibili.com/video/'+bv+'/'){
  publicNavigations++;body='<h1>'+esc(title)+'</h1><div class="up-info-container"><a class="up-name" href="https://space.bilibili.com/123456/">Owned uploader</a></div>'+
   '<div class="basic-desc-info">'+esc(source+'\n'+metadata.description)+'</div><video autoplay muted controls src="/owned.mp4" style="width:640px;height:360px"></video>';
 }else if(u.href==='https://www.bilibili.com/owned.mp4'){
  const range=/^bytes=(\d+)-(\d*)$/.exec(route.request().headers().range||'');
  const start=range?Number(range[1]):0,end=range&&range[2]?Math.min(Number(range[2]),video.length-1):video.length-1;
  await route.fulfill({status:range?206:200,contentType:'video/mp4',headers:{'accept-ranges':'bytes','content-length':String(end-start+1),
   ...(range?{'content-range':`bytes ${start}-${end}/${video.length}`}:{})},body:video.subarray(start,end+1)});return;
 }else{outside++;await route.abort();return;}
 await route.fulfill({status:200,contentType:'text/html',headers:{'x-owned-fixture':'true'},body:'<!doctype html><meta charset="utf-8">'+
  '<style>.basic-desc-info{white-space:'+(scenario==='space_public'?'pre-line':'pre-wrap')+'}.article-card{padding:10px}a{display:inline-block;min-height:20px}</style>'+body+
  '<script>document.addEventListener("click",()=>ownedClick())</script>'});
});
const page=await context.newPage();let result,error;
try{
 try{result=await observeLaterReplayResult({page,context,spec,root,known_external_id:scenario==='known_conflict'?other:null,publicTimeoutMs:1500});}
 catch(e){error=e.code;}
 if(scenario==='manager_loading_timeout'){
  assert.equal(error,'REPLAY_LATER_MANAGER_NOT_READY');assert.equal(publicNavigations,0);
  assert.equal(outside,0);assert.equal(clicks,0);assert.equal(choosers,0);
  console.log(JSON.stringify({scenario,owned:true,captured_href_shape:true,external_requests:outside,clicks,choosers,public_navigations:publicNavigations,error}));
 }else{
 const folders=await fs.readdir(path.join(base,'later-lookups-v2'));assert.equal(folders.length,1);
 const manager=JSON.parse(await fs.readFile(path.join(base,'later-lookups-v2',folders[0],'manager-row.json'),'utf8'));
 assert.equal(manager.selected_row.title_links[0].href,scenario==='pending_no_bv'?'javascript:void(0)':actual.title_links[0].raw_href);
 assert.ok(manager.selected_row.row_text.includes(title.replace(/ +/g,' ')));
 if(['known_conflict','cover_conflict'].includes(scenario)){
  assert.equal(error,'REPLAY_LATER_MANAGER_BV_CHANGED');assert.equal(publicNavigations,0);
 }else if(pending){
  assert.equal(error,undefined);assert.equal(result.state,'awaiting_moderation');
  assert.equal(result.result_observation.state,'awaiting_moderation');assert.equal(result.result_observation.public_accessible,false);
  assert.equal(result.candidate_external_id,scenario==='pending_no_bv'?null:bv);assert.equal(publicNavigations,0);
  assert.deepEqual(result.result_observation.native_status_labels,['转码中','审核中']);
 }else{
  assert.equal(error,undefined);assert.equal(result.state,'later_native_evidence_observed');assert.equal(result.candidate_external_id,bv);
  assert.equal(manager.selected_row.title_links[0].canonical_url,'https://www.bilibili.com/video/'+bv+'/');
  assert.equal(publicNavigations,1);
  const reconciliation=JSON.parse(await fs.readFile(result.reconciliation_observation.file,'utf8'));
  const pub=JSON.parse(await fs.readFile(reconciliation.public_observation.file,'utf8'));
  assert.equal(pub.near_end_playback.seek_performed,true);assert.ok(pub.near_end_playback.after-pub.near_end_playback.before>.1||pub.near_end_playback.ended);
 }
 assert.equal(outside,0);assert.equal(clicks,0);assert.equal(choosers,0);
 console.log(JSON.stringify({scenario,owned:true,captured_href_shape:true,external_requests:outside,clicks,choosers,public_navigations:publicNavigations}));
 }
}finally{await context.close();await browser.close();}
