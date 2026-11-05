import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {preflightExpiredTweet} from '../adapters/browser_expiry_preflight.mjs';
const output=process.argv[2];await fs.mkdir(output,{recursive:true});
const sharp=createRequire(new URL('../compositor/package.json',import.meta.url))('sharp');
const identity={handle:'example_handle_a',postId:'2116547209064071229'},cutoff=Date.parse('2026-10-31T18:00:00Z')/1000;
const base=`<!doctype html><style>body{margin:0;font:16px Arial}article{margin:80px 100px;width:600px}a{display:block;width:max-content;margin:8px}</style><article><a id="author-name" href="https://x.com/example_handle_a">示例主播A</a><a href="https://x.com/example_handle_a">@example_handle_a</a><div data-testid="tweetText">Folded quoted source…</div><a id="time" href="https://x.com/example_handle_a/status/${identity.postId}">23:05 · 2026年10月31日</a></article>`;
let externalRequests=0;const passed=[];
const browser=await chromium.launch({headless:true});
try{
 async function scenario(name,{source=base,expected='not_skipped',expectedCalls=6,mutating=false,beforeScreenshot,afterScreenshot,assertReceipt}={}){
  const context=await browser.newContext({viewport:{width:1280,height:1000},locale:'zh-CN',timezoneId:'Asia/Shanghai'});
  await context.route('**/*',r=>{externalRequests++;return r.abort();});
  const page=await context.newPage();await page.setContent(source);const original=await page.content();
  const actualScreenshot=page.screenshot.bind(page);let calls=0;
  page.screenshot=async options=>{
   calls++;assert.equal(options.fullPage,false);assert.equal(options.caret,'initial');assert.equal(options.animations,'allow');
   assert.ok(options.clip.width>0&&options.clip.height>0);assert.equal(options.mask,undefined);assert.equal(options.style,undefined);
   if(beforeScreenshot)await beforeScreenshot(page,calls);
   const bytes=await actualScreenshot(options);
   const decoded=await sharp(bytes).metadata();
   assert.equal(decoded.width,options.clip.width);assert.equal(decoded.height,options.clip.height);
   if(afterScreenshot)await afterScreenshot(page,calls);
   return bytes;
  };
  try{
   const receipt=await preflightExpiredTweet(page,identity,path.join(output,name),{notBefore:cutoff});
   assert.equal(receipt.decision,expected,`${name}: ${receipt.diagnostic}`);
   assert.equal(receipt.complete_source,false);assert.equal(receipt.publication_authorized,false);assert.equal(receipt.human_review,false);
   if(expected==='skipped_expired'){
    assert.equal(receipt.observations.length,2);assert.equal(calls,expectedCalls);
    for(const o of receipt.observations)for(const field of ['author_name','author_handle','timestamp']){
     assert.equal(o[field].screenshot_method,'page_viewport_clip-v1');assert.equal(o[field].visible,true);
    }
    if(!mutating)assert.equal(await page.content(),original,'preflight did not modify public DOM or CSS');
   }
   if(assertReceipt)await assertReceipt(receipt);
   passed.push(name);
  }finally{await context.close();}
 }
 // Reproduce the real failure mechanism: a normal visible node is continually
 // replaced with identical rendered content. Locator screenshots retain a
 // detached handle; viewport screenshots do not depend on that handle.
 const rerender=base+`<script>function replace(){let a=document.querySelector('#author-name');if(a)a.replaceWith(a.cloneNode(true));requestAnimationFrame(replace)}requestAnimationFrame(replace)</script>`;
 const control=await browser.newContext();const controlPage=await control.newPage();await controlPage.setContent(rerender);
 await assert.rejects(controlPage.locator('#author-name').screenshot({timeout:450}),/attached|stable|Timeout/);
 await control.close();passed.push('native-locator-detachment-reproduced');
 await scenario('identical-visible-rerender-uses-real-viewport-clips',{expected:'skipped_expired',
  beforeScreenshot:async page=>page.evaluate(()=>{for(const a of document.querySelectorAll('article a'))a.replaceWith(a.cloneNode(true));})});
 await scenario('moving-first-reading-is-rejected-then-fresh-strict-attempt-can-pass',{
  expected:'skipped_expired',expectedCalls:7,mutating:true,
  afterScreenshot:async(page,n)=>{if(n===1)await page.evaluate(()=>document.querySelector('#author-name').style.marginLeft='70px');},
  assertReceipt:r=>{assert.equal(r.attempts.length,2);assert.equal(r.attempts[0].reason,'EXPIRY_PREFLIGHT_TARGET_CHANGED');assert.equal(r.attempts[0].decision,'not_skipped');}});
 await scenario('cover-appears-during-screenshot-still-rejected',{
  afterScreenshot:async(page,n)=>{if(n===1)await page.evaluate(()=>{const d=document.createElement('div');d.style.cssText='position:fixed;inset:0;background:white;z-index:100';document.body.append(d);});},
  assertReceipt:r=>assert.equal(r.reason,'EXPIRY_PREFLIGHT_TARGET_CHANGED')});
 await scenario('time-changes-during-screenshot-still-rejected',{
  afterScreenshot:async(page,n)=>{if(n===3)await page.evaluate(()=>document.querySelector('#time').textContent='23:06 · 2026年10月31日');},
  assertReceipt:r=>assert.equal(r.reason,'EXPIRY_PREFLIGHT_TARGET_CHANGED')});
 await scenario('different-confirmation-geometry-is-rejected-before-fresh-strict-attempt',{
  expected:'skipped_expired',expectedCalls:12,mutating:true,
  afterScreenshot:async(page,n)=>{if(n===3)await page.evaluate(()=>document.querySelector('#author-name').style.marginLeft='70px');},
  assertReceipt:r=>{assert.equal(r.attempts.length,2);assert.equal(r.attempts[0].reason,'EXPIRY_PREFLIGHT_CONFIRMATIONS_DIFFER');assert.equal(r.attempts[0].decision,'not_skipped');}});
 assert.equal(externalRequests,0);console.log(JSON.stringify({passed,external_requests:externalRequests}));
}finally{await browser.close();}
