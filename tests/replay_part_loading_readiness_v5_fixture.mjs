// Owned DOM only: the actual manager sidebar is not a ready manager.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {inspectVideoProtection} from '../adapters/bilibili_video_guard.mjs';
import {settleEmptyBodyProtection} from '../adapters/replay_part_owner_handoff_v5.mjs';

const [scenario,directory]=process.argv.slice(2),output=path.resolve(directory);
await fs.mkdir(output,{recursive:true});
const url='https://member.bilibili.com/platform/upload-manager/article';
const sidebar='<aside>主站 投稿 首页 内容管理 稿件管理 申诉管理 字幕管理 数据中心 粉丝管理 互动管理 收益管理</aside>';
const manager=sidebar+'<main><a href="/platform/upload-manager/article">视频管理</a>'+
 '<a href="/platform/upload-manager/opus">图文管理</a><a href="/platform/upload-manager/inactArticle">互动视频管理</a>'+
 '<button>全部稿件 1</button><button>草稿 1</button></main>';
const browser=await chromium.launch({headless:true,chromiumSandbox:true}),context=await browser.newContext();
let outside=0;
await context.route('**/*',r=>{
 if(r.request().url()!==url){outside++;return r.abort();}
 return r.fulfill({status:200,contentType:'text/html',body:'<!doctype html><meta charset="utf-8"><style>body{min-height:100vh}a{display:inline-block;padding:4px}</style><body class="risk-captcha-adapt-pc risk-captcha-adapt">'+(scenario==='empty_then_sidebar_then_manager'?'':sidebar)+'</body>'});
});
try{
 const page=await context.newPage();await page.goto(url,{waitUntil:'domcontentloaded'});
 await page.evaluate(()=>{window.ownedActions={clicks:0,inputs:0,changes:0};for(const [event,key] of [['click','clicks'],['input','inputs'],['change','changes']])document.addEventListener(event,()=>window.ownedActions[key]++);});
 const protection=await inspectVideoProtection(page);assert.equal(protection.blocked,true);
 const spec={job_id:randomUUID(),attempt_id:randomUUID(),target_site_uid:'1000000000000000',source_id:'OwnedID1234',binding_sha256:'a'.repeat(64),metadata_sha256:'b'.repeat(64)};
 // These mutations model the page's own asynchronous rendering, not an
 // automation action. No storage, account, profile or external request exists.
 let scheduled;
 if(scenario==='empty_then_sidebar_then_manager')scheduled=page.evaluate(({sidebar,manager})=>{setTimeout(()=>document.body.innerHTML=sidebar,120);setTimeout(()=>document.body.innerHTML=manager,900);},{sidebar,manager});
 else if(scenario==='sidebar_then_manager')scheduled=page.evaluate(manager=>setTimeout(()=>document.body.innerHTML=manager,900),manager);
 else if(scenario==='challenge_during_loading')scheduled=page.evaluate(()=>setTimeout(()=>document.body.insertAdjacentHTML('beforeend','<div class="geetest_panel" style="width:300px;height:160px">Owned verification</div>'),500));
 else if(scenario==='authentication_during_loading')scheduled=page.evaluate(()=>setTimeout(()=>document.body.insertAdjacentHTML('beforeend','<iframe src="https://passport.bilibili.com/login" style="width:300px;height:160px"></iframe>'),500));
 else if(scenario==='ordinary_dialog_during_loading')scheduled=page.evaluate(()=>setTimeout(()=>document.body.insertAdjacentHTML('beforeend','<div role="dialog" style="width:300px;height:160px">Owned dialog</div>'),500));
 else assert.equal(scenario,'sidebar_timeout');
 await scheduled;
 const value=await settleEmptyBodyProtection({page,protection,output,spec});
 assert.ok(value.readiness_observation);const proof=JSON.parse(await fs.readFile(value.readiness_observation.file,'utf8'));
 assert.equal(proof.read_only,true);assert.deepEqual(proof.actions,{file_selections:0,submit_clicks:0,save_clicks:0});
 assert.ok(proof.before.screenshot.sha256);assert.ok(proof.after.screenshot.sha256);
 if(scenario.endsWith('then_manager')){
  assert.equal(value.protection.blocked,false);assert.equal(value.protection.evidence.native_draft_manager_excluded,true);assert.equal(proof.outcome,'guard_cleared');
  assert.ok(proof.after.visible_body_text.includes('草稿 1'));
 }else if(scenario==='sidebar_timeout'){
  assert.equal(value.protection.blocked,true);assert.equal(proof.outcome,'document_not_ready');assert.ok(proof.polls.at(-1).elapsed_ms>=5000);
  assert.equal(value.protection.evidence.native_draft_manager_excluded,false);
 }else{
  assert.equal(value.protection.blocked,true);assert.ok(proof.polls.at(-1).elapsed_ms<5000);
  if(scenario==='challenge_during_loading'){assert.equal(proof.outcome,'visible_challenge');assert.ok(value.protection.evidence.visible_challenge_controls.some(x=>x.tag==='DIV'));}
  else if(scenario==='authentication_during_loading')assert.equal(value.protection.error_code,'BILIBILI_VIDEO_AUTHENTICATION_REQUIRED');
  else assert.ok(value.protection.evidence.visible_dialogs.length===1);
 }
 assert.deepEqual(await page.evaluate(()=>window.ownedActions),{clicks:0,inputs:0,changes:0});
 // The login frame is intercepted and aborted; no request reaches a server.
 assert.equal(outside,scenario==='authentication_during_loading'?1:0);
 console.log(JSON.stringify({scenario,owned:true,actions:0,network_requests_sent:0,outcome:proof.outcome,blocked:value.protection.blocked}));
}finally{await context.close();await browser.close();}
