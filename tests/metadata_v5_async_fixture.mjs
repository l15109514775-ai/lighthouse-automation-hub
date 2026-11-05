// Actual Chromium, entirely owned native-shaped DOM. No live account or HTTP.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {inspectVideoProtection} from '../adapters/bilibili_video_guard.mjs';
import {canonicalDigest,handleNativeControl} from '../scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs';

const root=path.resolve(process.argv[2]),scenario=process.argv[3];
assert.ok(['async_animation','async_game','legacy_music','duplicate_option','option_timeout','duplicate_source','source_timeout','protected_wait','unexpected_category','duplicate_category','protected_source_blur','protected_tag_enter'].includes(scenario));
await fs.mkdir(root,{recursive:true,mode:0o700});
const metadata={title:'Owned async native form',description:'Owned full original title\nhttps://www.youtube.com/watch?v=abcdefghijk',
 source:'https://www.youtube.com/watch?v=abcdefghijk',copyright:2,category_label:'娱乐',tags:['Owned','回放']};
const spec={job_id:crypto.randomUUID(),attempt_id:crypto.randomUUID(),recovery_id:crypto.randomUUID(),
 source_url:metadata.source,target_site_uid:'123456',metadata_sha256:canonicalDigest(metadata)};
await fs.writeFile(path.join(root,'metadata.json'),JSON.stringify(metadata));
const local=Buffer.from('Owned local artifact; no platform file selection'),file=path.join(root,'owned.mp4');await fs.writeFile(file,local);
let outside=0,unowned=0,filechoosers=0,guards=0,hashes=0,identities=0;
const browser=await chromium.launch({headless:true,chromiumSandbox:true});
const context=await browser.newContext({viewport:{width:1280,height:960}}),page=await context.newPage();
const pageErrors=[];page.on('pageerror',error=>pageErrors.push(error.message));
page.on('filechooser',()=>filechoosers++);
context.on('response',response=>{if(response.url().startsWith('http')&&response.headers()['x-owned-fixture']!=='true')unowned++;});
const initial=scenario==='legacy_music'?'音乐':scenario==='unexpected_category'?'生活':scenario==='async_animation'?'动画':'游戏';
const config={scenario,initial,source:metadata.source,delay:scenario==='legacy_music'?0:180};
const html='<!doctype html><meta charset="utf-8"><body>'+
 '<input placeholder="请输入稿件标题" value="Owned async native form">'+
 '<div class="ql-editor" contenteditable="true"><p>Owned full original title</p><p>'+metadata.source+'</p></div>'+
 '<div class="ql-editor" contenteditable="true" style="display:none">Hidden duplicate description</div>'+
 '<div class="creation-statement-container"><div class="bcc-select"><input readonly placeholder="请选择符合您视频内容的创作声明"><ul class="statement-options" style="display:none"></ul></div></div>'+
 '<div id="source-area"></div><div class="video-human-type"><div class="select-container"><div class="select-controller">'+initial+'</div><ul class="category-options"></ul></div></div>'+
 '<div id="tags"><div class="label-item-v2-container"><p class="label-item-v2-content">MV</p><svg class="close" width="20" height="20" onclick="this.parentElement.remove()"><rect width="20" height="20"/></svg></div>'+
 '<div class="label-item-v2-container"><p class="label-item-v2-content">欧美音乐</p><svg class="close" width="20" height="20" onclick="this.parentElement.remove()"><rect width="20" height="20"/></svg></div>'+
 '<div class="label-item-v2-container"><p class="label-item-v2-content">剪辑</p><svg class="close" width="20" height="20" onclick="this.parentElement.remove()"><rect width="20" height="20"/></svg></div>'+
 '<div class="label-item-v2-container"><p class="label-item-v2-content">影视剪辑</p><svg class="close" width="20" height="20" onclick="this.parentElement.remove()"><rect width="20" height="20"/></svg></div>'+
 '<div class="label-item-v2-container"><p class="label-item-v2-content">短片</p><svg class="close" width="20" height="20" onclick="this.parentElement.remove()"><rect width="20" height="20"/></svg></div>'+
 '<div class="label-item-v2-container"><p class="label-item-v2-content">电视剧</p><svg class="close" width="20" height="20" onclick="this.parentElement.remove()"><rect width="20" height="20"/></svg></div></div>'+
 '<input class="owned-tag-input" placeholder="按回车键Enter创建标签"><input placeholder="按回车键Enter创建标签" style="display:none">'+
 '<script>const cfg='+JSON.stringify(config)+';window.events={dropdown:0,repost:0,category:0,sourceInput:0,sourceBlur:0,tagEnter:0};'+
 'function protect(){const c=document.createElement("div");c.className="geetest_challenge";c.style="width:100px;height:40px";c.textContent="Owned protection";document.body.append(c)}'+
 'const statement=document.querySelector(".creation-statement-container input");'+
 'statement.onclick=()=>{events.dropdown++;if(cfg.scenario==="protected_wait"){setTimeout(()=>{const c=document.createElement("div");c.className="geetest_challenge";c.style="width:100px;height:40px";c.textContent="Owned protection";document.body.append(c)},60);return}'+
 'if(cfg.scenario==="option_timeout")return;setTimeout(()=>{const ul=document.querySelector(".statement-options");'+
 'for(const label of ["内容为原创","内容为转载","自行拍摄","内容为授权","内容为合作","其他",...(cfg.scenario==="duplicate_option"?["内容为转载"]:[])]){'+
 'const li=document.createElement("li");li.className="bcc-option";const span=document.createElement("span");span.textContent=label;li.append(span);'+
 'li.onclick=()=>{if(label!=="内容为转载")throw new Error("Owned incorrect option");events.repost++;setTimeout(()=>{statement.value=label;ul.style.display="none";if(cfg.scenario==="source_timeout")return;'+
 'for(let i=0;i<(cfg.scenario==="duplicate_source"?2:1);i++){const s=document.createElement("input");s.placeholder="转载视频请注明来源、时间、地点(例：转自https://www.xxxx.com/yyyy)，注明来源会更快地通过审核哦";s.oninput=()=>{events.sourceInput++;if(cfg.scenario==="protected_source_blur")protect()};s.onblur=()=>events.sourceBlur++;document.querySelector("#source-area").append(s)}},cfg.delay)};ul.append(li)}'+
 'const hidden=document.createElement("li");hidden.className="bcc-option";hidden.style.display="none";hidden.textContent="内容为转载";ul.append(hidden);ul.style.display="block"},cfg.delay)};'+
 'document.querySelector(".select-controller").onclick=()=>{setTimeout(()=>{const ul=document.querySelector(".category-options");'+
 'for(let i=0;i<(cfg.scenario==="duplicate_category"?2:1);i++){const li=document.createElement("li");li.textContent="娱乐";li.onclick=()=>{events.category++;setTimeout(()=>{document.querySelector(".select-controller").textContent="娱乐";ul.replaceChildren()},cfg.delay)};ul.append(li)}},cfg.delay)};'+
 'document.querySelector(".owned-tag-input").oninput=()=>{if(cfg.scenario==="protected_tag_enter")setTimeout(protect,60)};'+
 'document.querySelector(".owned-tag-input").onkeydown=e=>{if(e.key==="Enter"){events.tagEnter++;const chip=document.createElement("div");chip.className="label-item-v2-container";const p=document.createElement("p");p.className="label-item-v2-content";p.textContent=e.target.value;chip.append(p);document.querySelector("#tags").append(chip);e.target.value=""}};'+
 '</script></body>';
await context.route('**/*',async route=>{
 const url=new URL(route.request().url());
 if(url.origin!=='https://member.bilibili.com'||url.pathname!=='/platform/upload/video/frame'){outside++;return route.abort();}
 return route.fulfill({contentType:'text/html',headers:{'x-owned-fixture':'true'},body:html});
});
try{
 await page.goto('https://member.bilibili.com/platform/upload/video/frame');
 assert.deepEqual(pageErrors,[]);
 const helpers={
  checkProtection:async()=>{guards++;return !(await inspectVideoProtection(page)).blocked;},
  accountIdentity:async()=>{identities++;return '123456';},
  checkedFile:async()=>{hashes++;const current=await fs.readFile(file);assert.equal(current.length,local.length);assert.equal(canonicalDigest([...current]),canonicalDigest([...local]));},
  write:async(name,value)=>{assert.equal(path.basename(name),name);await fs.writeFile(path.join(root,name),JSON.stringify(value));},
  inventory:async()=>({owned_inventory:true})};
 const result=await handleNativeControl({page,spec,output:root,control_action:'fill_metadata',helpers});
 const events=await page.evaluate(()=>window.events);
 assert.equal(result.submitted,false);assert.equal(result.submit_implemented,false);assert.equal(result.cover_modified,false);assert.equal(result.metadata_ready,false);
 if(['async_animation','async_game','legacy_music'].includes(scenario)){
  assert.equal(result.state,'text_metadata_verified_cover_pending',JSON.stringify({result,pageErrors,events}));assert.equal(result.text_metadata_verified,true);
  assert.equal(result.creation_statement,'内容为转载');assert.equal(result.source,metadata.source);assert.equal(result.category,'娱乐');assert.equal(result.description,metadata.description);
  assert.deepEqual(new Set(result.tags),new Set(metadata.tags));assert.equal(events.repost,1);assert.equal(events.category,1);
 }else{
  const expected={duplicate_option:'METADATA_NATIVE_CONTROL_AMBIGUOUS',option_timeout:'METADATA_NATIVE_CONTROL_TIMEOUT',duplicate_source:'METADATA_NATIVE_CONTROL_AMBIGUOUS',
   source_timeout:'METADATA_NATIVE_CONTROL_TIMEOUT',protected_wait:'METADATA_PROTECTION_REQUIRED',unexpected_category:'METADATA_CATEGORY_CHANGED',duplicate_category:'METADATA_CATEGORY_OPTION_AMBIGUOUS',protected_source_blur:'METADATA_PROTECTION_REQUIRED',protected_tag_enter:'METADATA_PROTECTION_REQUIRED'};
  assert.equal(result.state,'blocked');assert.equal(result.error_code,expected[scenario]);
  assert.equal(events.category,scenario==='protected_tag_enter'?1:0);if(['duplicate_option','option_timeout','protected_wait'].includes(scenario))assert.equal(events.repost,0);
  if(scenario==='protected_source_blur')assert.equal(events.sourceBlur,0);
  if(scenario==='protected_tag_enter')assert.equal(events.tagEnter,0);
 }
 assert.equal(events.dropdown,1);assert.ok(guards>3);assert.equal(hashes,1);assert.equal(identities,1);
 assert.equal(filechoosers,0);assert.equal(outside,0);assert.equal(unowned,0);assert.deepEqual(await fs.readFile(file),local);
 console.log(JSON.stringify({scenario,events,guards,file_selections:filechoosers,public_submissions:0,draft_saves:0,external_network_calls:outside,unowned_http_responses:unowned,live_tested:false}));
}finally{await context.close();await browser.close();}
