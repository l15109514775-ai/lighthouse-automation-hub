// Fully owned routed DOM; no account, platform request or public action.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {inspectVideoProtection} from '../adapters/bilibili_video_guard.mjs';
import {queryManager} from '../scripts/ops/replay_unconsumed_manager_query_v1.mjs';
const output=path.resolve(process.argv[2]),scenario=process.argv[3];
assert.ok(['delayed_ready','real_challenge','public_candidate'].includes(scenario));await fs.mkdir(output,{recursive:true});
let clicks=0,choosers=0,external=0,unowned=0,identity=0;const records={};
const browser=await chromium.launch({headless:true,chromiumSandbox:true});
const context=await browser.newContext(),page=await context.newPage();
page.on('filechooser',()=>choosers++);page.on('console',m=>{if(m.text()==='OWNED_CLICK')clicks++;});
context.on('response',r=>{if(r.url().startsWith('http')&&r.headers()['x-owned-fixture']!=='true')unowned++;});
const manager='https://member.bilibili.com/platform/upload-manager/article';
const full='<h1>稿件管理</h1><a href="/platform/upload-manager/article">视频管理</a><a href="/platform/upload-manager/opus">图文管理</a><a href="/platform/upload-manager/inactArticle">互动视频管理</a>'+['全部稿件 0','草稿 2','进行中 0','已发布 0','未通过 0','一个稿件都没有，请换个筛选条件'].map(s=>'<p>'+s+'</p>').join('')+(scenario==='public_candidate'?'<a href="https://www.bilibili.com/video/BV1234567890">Owned candidate</a>':'');
await context.route('**/*',async r=>{
 if(r.request().url()!==manager){external++;return r.abort();}
 const html='<!doctype html><meta charset="utf-8"><style>a{display:block}body{min-height:100vh}</style><body class="risk-captcha-adapt risk-captcha-adapt-pc"><div id="mount"></div><script>document.addEventListener("click",()=>console.log("OWNED_CLICK"));'+
  'setTimeout(()=>document.querySelector("#mount").innerHTML='+JSON.stringify(full)+',4000);'+
  (scenario==='real_challenge'?'setTimeout(()=>{const c=document.createElement("div");c.className="geetest_challenge";c.textContent="Owned challenge";c.style="width:120px;height:40px";document.body.append(c)},100);':'')+'</script></body>';
 return r.fulfill({contentType:'text/html',headers:{'x-owned-fixture':'true'},body:html});
});
try{
 let result=null,error=null;const started=performance.now();
 try{result=await queryManager({page,inspectProtection:inspectVideoProtection,accountIdentity:async()=>{identity++;return 'owned-uid';},spec:{target_site_uid:'owned-uid',metadata:{title:'Owned exact replay'},source_url:'https://www.youtube.com/watch?v=ownedvideo1'},chooserCount:()=>choosers,write:async(name,value)=>{records[name]=value;await fs.writeFile(path.join(output,name),JSON.stringify(value));}});}catch(e){error=e.code;}
 const elapsed=performance.now()-started;
 if(scenario==='delayed_ready'){
  assert.deepEqual(result,{state:'unknown'});assert.equal(error,null);assert.ok(elapsed>=3900&&elapsed<9000);
  const proof=records['recovery-manager-readiness.json'];assert.equal(proof.ready,true);assert.ok(proof.polls.some(p=>p.startup_body_only));assert.equal(identity,1);
 }else if(scenario==='real_challenge'){
  assert.equal(error,'BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED');assert.ok(elapsed<2500);assert.equal(identity,0);
 }else{assert.equal(error,'RECOVERY_MANAGER_PUBLICATION_CANDIDATE');assert.ok(elapsed>=3900);}
 assert.equal(clicks,0);assert.equal(choosers,0);assert.equal(external,0);assert.equal(unowned,0);
 console.log(JSON.stringify({scenario,native_clicks:clicks,file_selections:choosers,external_requests:external,live_tested:false}));
}finally{await context.close();await browser.close();}
