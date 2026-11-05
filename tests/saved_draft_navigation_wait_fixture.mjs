// Owned real Chromium DOM and routed HTTP only. Never a platform account/action.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {inspectVideoProtection} from '../adapters/bilibili_video_guard.mjs';
import {handleNativeControl} from '../adapters/short_video_saved_draft_navigation_control.mjs';

const directory=path.resolve(process.argv[2]),scenario=process.argv[3];
assert.ok(['async_draft','async_title','duplicate_draft','real_challenge','timeout'].includes(scenario));
await fs.mkdir(directory,{recursive:true,mode:0o700});
const origin='https://member.bilibili.com',manager='/platform/upload-manager/article',editor='/platform/upload/video/frame';
const title='Owned async saved draft',uid='123456';
let external=0,unowned=0,managerNavigations=0,editorNavigations=0,tabClicks=0,choosers=0,uidChecks=0,guards=0;
const browser=await chromium.launch({headless:true,chromiumSandbox:true});
const context=await browser.newContext({viewport:{width:1280,height:960}}),page=await context.newPage();
const errors=[];page.on('pageerror',error=>errors.push(error.message));page.on('filechooser',()=>choosers++);
page.on('console',message=>{if(message.text()==='OWNED_DRAFT_TAB_CLICK')tabClicks++;});
context.on('response',response=>{if(response.url().startsWith('http')&&response.headers()['x-owned-fixture']!=='true')unowned++;});
const escape=s=>s.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const nav='<h1>稿件管理</h1><nav><a href="'+origin+manager+'">视频管理</a><a href="'+origin+'/platform/upload-manager/opus">图文管理</a><a href="'+origin+'/platform/upload-manager/inactArticle">互动视频管理</a></nav>';
const anchor='<a target="_blank" href="'+origin+editor+'?draft=owned-slot">'+escape(title)+'</a>';
const html='<!doctype html><meta charset="utf-8"><body style="min-height:100vh" class="risk-captcha-adapt risk-captcha-adapt-pc"><div id="mount"></div><script>'+ 
 'const scenario='+JSON.stringify(scenario)+',nav='+JSON.stringify(nav)+',anchor='+JSON.stringify(anchor)+';'+
 'if(scenario==="real_challenge")setTimeout(()=>{const d=document.createElement("div");d.className="geetest_challenge";d.style="width:120px;height:40px";d.textContent="Owned visible challenge";document.body.append(d)},80);'+
 'if(scenario!=="timeout")setTimeout(()=>{const root=document.querySelector("#mount");root.innerHTML=nav;'+
 'if(scenario==="async_title")root.insertAdjacentHTML("beforeend","<span>草稿 2</span>"+anchor);else for(let i=0;i<(scenario==="duplicate_draft"?2:1);i++){const button=document.createElement("button");button.textContent="草稿 2";button.onclick=()=>{console.log("OWNED_DRAFT_TAB_CLICK");setTimeout(()=>root.insertAdjacentHTML("beforeend",anchor),120)};root.append(button)}},360);'+
 '</script></body>';
await context.route('**/*',async route=>{
 const u=new URL(route.request().url());let body;
 if(u.origin==='https://t.bilibili.com'&&u.pathname==='/')body='<a class="header-entry-mini" href="https://space.bilibili.com/'+uid+'">Owned identity</a>';
 else if(u.origin===origin&&u.pathname===manager){managerNavigations++;body=html;}
 else if(u.origin===origin&&u.pathname===editor){editorNavigations++;body='<!doctype html><meta charset="utf-8"><input placeholder="请输入稿件标题" value="'+escape(title)+'">';}
 else{external++;return route.abort();}
 return route.fulfill({contentType:'text/html',headers:{'x-owned-fixture':'true'},body});
});
try{
 await page.goto(origin+manager,{waitUntil:'domcontentloaded'});
 const initial=await inspectVideoProtection(page);assert.equal(initial.blocked,true);assert.equal(initial.evidence.visible_challenge_controls.length,1);
 const saved={};
 const helpers={
  checkProtection:async()=>{guards++;return !(await inspectVideoProtection(page)).blocked;},
  accountIdentity:async()=>{uidChecks++;const identity=await context.newPage();try{await identity.goto('https://t.bilibili.com/');const avatar=identity.locator('a.header-entry-mini:visible');assert.equal(await avatar.count(),1);return /^\/(\d+)$/.exec(new URL(await avatar.getAttribute('href')).pathname)[1];}finally{await identity.close();}},
  write:async(name,value)=>{assert.equal(path.basename(name),name);saved[name]=value;await fs.writeFile(path.join(directory,name),JSON.stringify(value));},
  inventory:async()=>({owned_inventory:true})};
 const started=performance.now();
 const result=await handleNativeControl({page,spec:{job_id:'owned-job',attempt_id:'owned-attempt',recovery_id:'owned-recovery',target_site_uid:uid,metadata:{title}},output:directory,control_action:'lookup',helpers});
 const elapsed=performance.now()-started;
 assert.equal(result.submitted,false);assert.equal(result.submit_implemented,false);assert.equal(result.metadata_modified,false);assert.equal(result.new_video_file_selection,false);
 if(scenario==='async_draft'||scenario==='async_title'){
  assert.equal(result.state,'saved_draft_editor_opened',JSON.stringify({result,errors}));
  assert.equal(result.native_draft_tab_clicked,scenario==='async_draft');assert.equal(tabClicks,scenario==='async_draft'?1:0);assert.equal(editorNavigations,1);
  const readiness=saved['native-saved-draft-manager-readiness.json'];assert.equal(readiness.ready,true);assert.ok(readiness.polls.some(p=>p.startup_body_marker_only));
  assert.equal(readiness.polls.at(-1).guard_blocked,false);assert.ok(guards>=4&&uidChecks>=2);
 }else{
  assert.equal(result.state,'navigation_unknown');assert.equal(result.error_code,'NATIVE_SAVED_DRAFT_NAVIGATION_NOT_CONFIRMED');
  assert.equal(tabClicks,0);assert.equal(editorNavigations,0);assert.equal(uidChecks,0);
  if(scenario==='timeout'){assert.ok(elapsed>=19900&&elapsed<23500);assert.equal(saved['native-saved-draft-manager-readiness.json'].ready,false);}
  else assert.ok(elapsed<2500,'duplicate or real protection must stop before timeout');
 }
 assert.equal(managerNavigations,1);assert.equal(choosers,0);assert.equal(external,0);assert.equal(unowned,0);assert.deepEqual(errors,[]);
 console.log(JSON.stringify({scenario,elapsed_ms:Math.round(elapsed),manager_navigations:managerNavigations,tab_clicks:tabClicks,editor_navigations:editorNavigations,file_selections:0,draft_saves:0,public_submissions:0,external_network_calls:external,unowned_http_responses:unowned,live_tested:false}));
}finally{await context.close();await browser.close();}
