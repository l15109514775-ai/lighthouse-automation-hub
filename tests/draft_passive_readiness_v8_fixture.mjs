// Owned Chromium DOM matches the root-observed 2026-11-03 native draft cards.
// The U+3000 is deliberate. No live account, original video or submission.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {canonicalDigest} from '../scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs';
import {observeLaterReplayResult,replayDraftLink} from '../adapters/replay_later_result_lookup_v2.mjs';
const [scenario,rootArg]=process.argv.slice(2),root=path.resolve(rootArg);
const cases=['sidebar_then_ready','values_while_blocked','body_timeout','real_challenge','real_dialog','real_auth'];
assert.ok(cases.includes(scenario));
const title='【示例主播B】原题◢\u3000直播回放 P1/1',source='https://www.youtube.com/watch?v=YsSXSIYtCoE';
const metadata={title,description:'Owned description\n'+source};
const spec={version:'replay-part-native-browser-manifest-v5',job_id:randomUUID(),attempt_id:randomUUID(),target_site_uid:'1000000000000000',
 source_id:'YsSXSIYtCoE',source_url:source,metadata,metadata_sha256:canonicalDigest(metadata),binding_sha256:'b'.repeat(64),duration_seconds:24232,
 parent_job_id:randomUUID(),part_index:0,part_count:1,part_idempotency_key:'c'.repeat(64),whole_plan_sha256:'d'.repeat(64),
 parts_manifest_sha256:'e'.repeat(64),artifact_sha256:'f'.repeat(64),file_bytes:123,policy_sha256:'a'.repeat(64)};
await fs.mkdir(path.join(root,'media',spec.job_id,'replay-browser-part-v5',spec.attempt_id),{recursive:true});
const href=id=>'/platform/upload/video/frame?type=draft&draftId='+id;
assert.equal(replayDraftLink(href('4069503')).draft_id,'4069503');
for(const bad of ['https://owned.invalid'+href('4069503'),href('4069503')+'&extra=1',href('4069503')+'#x',href('04069503'),href('4069503')+'&draftId=1'])assert.equal(replayDraftLink(bad),null);
const esc=s=>s.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const card=(id,t)=>'<section class="bcc-cmad-card card"><a class="bcc-cmadc-cover-box" href="'+esc(href(scenario==='cover_mismatch'?'999':id))+'"><img alt="'+esc(t)+'"></a>'+
 '<div class="bcc-cmadc-title-time"><p class="title" title="'+esc(t)+'"><a href="'+esc(href(id))+'">'+esc(t)+'</a></p><p class="time">2026年11月03日 05:26:15</p></div>'+
 '<a class="bcc-cmadc-btn edit" href="'+esc(href(id))+'">编辑</a><button onclick="ownedForbidden()">删除</button></section>';
const count=scenario==='empty'?0:scenario==='not_found'?1:2;
const targetTitle=scenario==='unicode_mismatch'?title.replace('\u3000',' '):title;
const cards=scenario==='empty'?'':(scenario==='not_found'?'':card('4069503',targetTitle))+card('4069103',scenario==='duplicate_title'?title:'Another streamer-a draft');
let outside=0,tabClicks=0,editorVisits=0,forbidden=0,choosers=0,protectionCalls=0;
const browser=await chromium.launch({headless:true,chromiumSandbox:true}),context=await browser.newContext();
await context.exposeBinding('ownedTab',()=>tabClicks++);await context.exposeBinding('ownedForbidden',()=>forbidden++);
context.on('page',p=>p.on('filechooser',()=>choosers++));
await context.route('**/*',async route=>{
 const u=new URL(route.request().url());let body;
 if(u.href==='https://t.bilibili.com/')body='<a class="header-entry-mini" href="https://space.bilibili.com/'+(scenario==='wrong_uid'?'999':'1000000000000000')+'/">Owned viewer</a>';
 else if(u.href==='https://member.bilibili.com/platform/upload-manager/article'){
  const drafts='<section class="bcc-content-manager-article-draft"><div class="bcc-cmad-content-box">'+cards+'</div></section>'+
   '<div class="bcc-pagination-total"> 共'+(scenario==='multiple_pages'?2:1)+'页 / '+count+'个， </div><li class="bcc-pagination-item selected"><a>1</a></li>'+
   (scenario==='challenge'?'<div class="geetest_panel" style="width:300px;height:150px">请完成验证</div>':'');
  body='<div>稿件管理</div><a href="/platform/upload-manager/article">视频管理</a><a href="/platform/upload-manager/opus">图文管理</a>'+
   '<a href="/platform/upload-manager/inactArticle">互动视频管理</a><div>全部稿件 2</div><button id="draft">草稿 '+count+'</button><div id="listing">'+
   '<div class="article-card clearfix v2"><a class="name ellipsis" href="//www.bilibili.com/video/BV1gv4A6cEdK/">Another published streamer-a</a></div>'+
   (scenario==='public_inventory_incomplete'?'':'<div class="article-card clearfix v2"><a class="name ellipsis" href="//www.bilibili.com/video/BV1JExh6QELr/">Older published streamer-a</a></div>')+'</div>'+
   '<script>document.querySelector("#draft").onclick=()=>{ownedTab();history.pushState({},"","?group=draft&page=1");document.querySelector("#listing").innerHTML='+JSON.stringify(drafts)+'};</script>';
 }else if(u.origin==='https://member.bilibili.com'&&replayDraftLink(u.href)){
  editorVisits++;body='<input placeholder="请输入稿件标题" value="'+esc(title)+'"><input placeholder="转载视频请注明来源、时间、地点(例：转自https://www.xxxx.com/yyyy)，注明来源会更快地通过审核哦" value="'+esc(scenario==='wrong_source'?'https://www.youtube.com/watch?v=6wHqfDmNF0U':source)+'">'+
   '<div>上传完成</div><button onclick="ownedForbidden()">立即投稿</button><button onclick="ownedForbidden()">存草稿</button><input type=file name=buploader accept=.mp4>';

  if(scenario==='delayed_empty'||scenario==='never_ready')body+='<script>document.querySelectorAll("input").forEach(x=>x.value="");'+(scenario==='delayed_empty'?'setTimeout(()=>{document.querySelector("input[placeholder=请输入稿件标题]").value='+JSON.stringify(title)+';document.querySelectorAll("input")[1].value='+JSON.stringify(source)+'},500)':'')+'</script>';
  if(scenario==='delayed_title')body+='<script>document.querySelector("input[placeholder=请输入稿件标题]").value="Old stale title";setTimeout(()=>document.querySelector("input[placeholder=请输入稿件标题]").value='+JSON.stringify(title)+',500)</script>';
  if(scenario==='delayed_source')body+='<script>{const x=document.querySelectorAll("input")[1];x.remove();setTimeout(()=>document.body.append(x),500)}</script>';
  if(scenario==='wrong_url')body+='<script>history.replaceState({},"","?type=draft&draftId=4069103")</script>';
  if(scenario==='duplicate_control')body+='<input placeholder="请输入稿件标题" value="Duplicate">';
  if(scenario==='protection')body+='<div class="geetest_panel" style="width:300px;height:150px">请完成验证</div>';

  body+='<script>{const full=document.body.innerHTML;document.querySelectorAll("input[type=file]").forEach(x=>x.remove());document.body.className="risk-captcha-adapt risk-captcha-adapt-pc";'+
   (scenario==='sidebar_then_ready'?'document.body.innerHTML="<aside>主站 投稿 首页 内容管理</aside><div>正在加载中...</div>";setTimeout(()=>{document.body.innerHTML=full;document.body.className="";},700);':
    scenario==='values_while_blocked'?'setTimeout(()=>document.body.className="",700);':
    scenario==='real_challenge'?'document.body.innerHTML="<aside>主站 投稿 首页</aside><div>正在加载中...</div>";setTimeout(()=>document.body.insertAdjacentHTML("beforeend","<div class=geetest_panel style=width:300px;height:150px>请完成验证</div>"),500);':
    scenario==='real_dialog'?'document.body.innerHTML="<aside>主站 投稿 首页</aside><div>正在加载中...</div>";setTimeout(()=>{document.body.className="";document.body.insertAdjacentHTML("beforeend","<div role=dialog style=width:300px;height:150px>原生确认</div>")},500);':
    scenario==='real_auth'?'document.body.innerHTML="<aside>主站 投稿 首页</aside><div>正在加载中...</div>";setTimeout(()=>document.body.insertAdjacentHTML("beforeend","<iframe src=https://passport.bilibili.com/login style=width:300px;height:150px></iframe>"),500);':'')+'}</script>';
 }else if(u.origin==='https://passport.bilibili.com'){body='<p>Owned login fixture</p>';
 }else{outside++;await route.abort();return;}
 await route.fulfill({status:200,contentType:'text/html',body:'<!doctype html><meta charset="utf-8"><style>a{display:inline-block;min-height:20px}.bcc-cmad-card{padding:8px}input{display:block;width:600px}</style>'+body});
});


let result,error;const started=Date.now();try{
 try{result=await observeLaterReplayResult({page:await context.newPage(),context,spec,root,onProtection:async()=>{protectionCalls++;}});}catch(e){error=e.code;}
 const positive=['sidebar_then_ready','values_while_blocked'].includes(scenario);
 const codes={body_timeout:'REPLAY_DRAFT_EDITOR_NOT_READY',real_challenge:'BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED',real_dialog:'BILIBILI_VIDEO_VISIBLE_DIALOG_REQUIRES_REVIEW',real_auth:'BILIBILI_VIDEO_AUTHENTICATION_REQUIRED'};
 if(positive){assert.equal(error,undefined);assert.equal(result.state,'draft_found');}
 else assert.equal(error,codes[scenario]);
 const folders=await fs.readdir(path.join(root,'media',spec.job_id,'replay-browser-part-v5',spec.attempt_id,'later-lookups-v2'));assert.equal(folders.length,1);
 const raw=JSON.parse(await fs.readFile(path.join(root,'media',spec.job_id,'replay-browser-part-v5',spec.attempt_id,'later-lookups-v2',folders[0],'draft-editor-readiness.json'),'utf8'));
 assert.equal(raw.ready,positive);assert.ok(raw.polls.some(x=>x.guard_blocked&&x.body_marker_only));
 assert.ok(raw.polls.filter(x=>x.guard_blocked).every(x=>!x.exact_values_match&&x.consecutive_matches===0));
 assert.ok((await fs.stat(raw.screenshot.file)).size>0);
 if(positive){assert.ok(raw.polls.slice(-2).every(x=>!x.guard_blocked&&x.exact_values_match));assert.equal(protectionCalls,0);}
 else if(scenario==='body_timeout'){assert.ok(raw.elapsed_ms>=14900);assert.equal(protectionCalls,0);}
 else{assert.ok(raw.elapsed_ms<5000);assert.equal(protectionCalls,1);}
 if(scenario==='values_while_blocked')assert.ok(raw.polls.some(x=>x.guard_blocked&&x.title===title&&x.source_url===source));
 assert.equal(outside,0);assert.equal(forbidden,0);assert.equal(choosers,0);
 console.log(JSON.stringify({scenario,owned:true,ready:raw.ready,protection_calls:protectionCalls,external_requests:outside,public_submits:forbidden,choosers,elapsed_ms:Date.now()-started}));
}finally{await context.close();await browser.close();}
