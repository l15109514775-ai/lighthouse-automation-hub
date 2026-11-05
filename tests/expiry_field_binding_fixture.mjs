// Owned HTML and real Chromium viewport clips only. DOM changes below are
// adversarial fixture events, never operations performed by the collector.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {preflightExpiredTweet} from '../adapters/browser_expiry_preflight.mjs';

const output=process.argv[2];await fs.mkdir(output,{recursive:true});
const sharp=createRequire(new URL('../compositor/package.json',import.meta.url))('sharp');
const hash=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
const identity={handle:'example_handle_a',postId:'2116547209064071229'};
const cutoff=Date.parse('2026-10-31T18:00:00Z')/1000;
const own='https://x.com/example_handle_a',permalink=`${own}/status/${identity.postId}`;
const nameLink=`<a href="${own}">示例主播A</a>`;
const handleLink=`<a href="${own}">@example_handle_a</a>`;
const dateLink=`<a href="${permalink}"><time datetime="2026-10-31T15:05:22Z">23:05 · 2026年10月31日</time></a>`;
function html({native=true,recent=false,before='',body='Owned complete source.',after='',hidden=false}={}){
 const label=recent?'03:05 · 2026年11月1日':'23:05 · 2026年10月31日';
 const timestamp=native?`<time datetime="${recent?'2026-10-31T19:05:22Z':'2026-10-31T15:05:22Z'}">${label}</time>`:label;
 return `<!doctype html><html lang="zh"><style>body{margin:0;background:white;font:16px Arial;min-height:1800px}article{margin:80px 100px;width:600px}a{display:block;width:max-content;margin:8px}blockquote{margin:20px}article article{margin:20px}footer{margin-top:20px}</style><body><article id="owned">${before}${hidden?`<div hidden>${nameLink}${handleLink}${dateLink}</div>`:''}<a id="name" href="${own}">示例主播A</a><a id="handle" href="${own}">@example_handle_a</a><div data-testid="tweetText">${body}</div><a id="time" href="${permalink}">${timestamp}</a>${after}</article></body></html>`;
}
const overlay=pointerless=>`<div role="dialog" style="position:fixed;inset:0;background:white;z-index:999;${pointerless?'pointer-events:none':''}">Owned obstruction</div>`;
async function insertHidden(page,id,style='display:none'){
 await page.evaluate(({id,style})=>{const current=document.getElementById(id),copy=current.cloneNode(true);copy.removeAttribute('id');copy.setAttribute('style',style);copy.setAttribute('data-owned-hidden-copy','');current.before(copy);},{id,style});
}
let externalRequests=0;const passed=[],summaries=[];
const browser=await chromium.launch({headless:true});
try{
 async function run(name,{source=html(),expected='skipped_expired',screenshots=6,after,check}={}){
  const context=await browser.newContext({viewport:{width:1280,height:1000},locale:'zh-CN',timezoneId:'Asia/Shanghai'});
  await context.route('**/*',route=>{externalRequests++;return route.abort();});
  const page=await context.newPage();page.setDefaultTimeout(700);await page.setContent(source);
  const original=await page.content(),dir=path.join(output,name),realScreenshot=page.screenshot.bind(page);let calls=0;
  page.screenshot=async options=>{
   calls++;assert.equal(options.fullPage,false);assert.equal(options.caret,'initial');assert.equal(options.animations,'allow');
   assert.equal(options.mask,undefined);assert.equal(options.style,undefined);
   const bytes=await realScreenshot(options),meta=await sharp(bytes).metadata();
   assert.equal(meta.width,options.clip.width);assert.equal(meta.height,options.clip.height);
   if(after)await after(page,calls);return bytes;
  };
  try{
   const result=await preflightExpiredTweet(page,identity,dir,{notBefore:cutoff});
   assert.equal(result.version,'x-expiry-preflight-v1');assert.equal(result.purpose,'skip_expired_only');
   for(const field of ['complete_source','source_attested','publication_authorized','human_review','official_api'])assert.equal(result[field],false);
   assert.equal(result.decision,expected,`${name}: ${result.reason} ${result.diagnostic||''}`);
   assert.equal(result.attempts.length,1,`${name}: a semantic field problem cannot earn a geometry retry`);
   assert.equal(result.attempts[0].retry_eligible,false);
   if(screenshots!==null)assert.equal(calls,screenshots,`${name}: screenshot count`);
   let provedScreenshots=0;
   const targetDiagnostics=result.attempts[0].target_diagnostics;
   for(const d of targetDiagnostics){
    if(!d.screenshot_file)continue;provedScreenshots++;
    assert.equal(path.dirname(d.screenshot_file),path.join(dir,'attempt-1'));
    assert.equal(hash(await fs.readFile(d.screenshot_file)),d.screenshot_sha256);
    assert.ok(d.before);assert.equal(d.before.visible,true);
    assert.equal(d.before.binding.version,'native-expiry-field-binding-v1');
    assert.equal(d.before.binding.visible_candidates,1);
    if(expected==='skipped_expired'){
     assert.ok(d.after);assert.equal(d.after.visible,true);assert.equal(d.after.binding.visible_candidates,1);
     for(const key of ['visible','text','href','datetime','tag','rect','viewport','scroll_x','scroll_y'])assert.deepEqual(d.after[key],d.before[key]);
    }
   }
   assert.equal(provedScreenshots,calls);
   if(expected==='skipped_expired'){
    assert.equal(result.observations.length,2);assert.equal(calls,6);
    for(const observation of result.observations){
     assert.equal(observation.display_name,'示例主播A');
     assert.equal(observation.author_name.text,'示例主播A');assert.equal(observation.author_handle.text,'@example_handle_a');
     assert.equal(observation.timestamp.href,permalink);assert.equal(observation.time.upper_bound_exclusive_epoch<cutoff,true);
    }
   }else assert.notEqual(result.reason,'entire_observed_time_interval_precedes_cutoff');
   const saved=JSON.parse(await fs.readFile(result.evidence_file,'utf8'));
   assert.deepEqual(saved.attempts,result.attempts);
   if(!after)assert.equal(await page.content(),original,'collector cannot rewrite fixture DOM or CSS');
   if(check)await check(result);
   passed.push(name);summaries.push({name,decision:result.decision,reason:result.reason,attempts:result.attempts.length,screenshots:calls});
  }finally{await context.close();}
 }

 await run('stable-native-full-post');
 await run('stable-visible-year-minute',{source:html({native:false})});
 await run('recent-native-time-never-skips',{source:html({recent:true}),expected:'not_skipped',screenshots:3,
  check:r=>assert.equal(r.reason,'EXPIRY_PREFLIGHT_NOT_CERTAINLY_EXPIRED')});
 await run('initial-hidden-own-author-handle-date-copies',{source:html({hidden:true})});
 for(const [id,n]of [['name',1],['handle',2],['time',3]])await run(`hidden-${id}-inserted-before-during-own-screenshot`,{
  after:async(page,calls)=>{if(calls===n)await insertHidden(page,id);},
  check:r=>{const d=r.attempts[0].target_diagnostics[n-1];assert.ok(d.after.binding.matching_anchors>d.before.binding.matching_anchors);}});
 await run('hidden-copies-inserted-before-every-field-read',{
  after:async(page,n)=>insertHidden(page,['name','handle','time'][(n-1)%3])});
 await run('hidden-copies-reordered-around-intact-visible-fields',{source:html({hidden:true}),
  after:async page=>page.evaluate(()=>{const group=document.querySelector('[hidden]');for(const a of [...group.children].reverse())group.append(a);document.querySelector('#owned').append(group);})});
 await run('same-visible-field-replaced-after-every-real-screenshot',{
  after:async(page,n)=>page.evaluate(id=>{const a=document.getElementById(id);a.replaceWith(a.cloneNode(true));},['name','handle','time'][(n-1)%3])});
 await run('visibility-hidden-copy-never-shadows-visible-name',{
  after:async(page,n)=>{if(n===1)await insertHidden(page,'name','position:absolute;visibility:hidden;left:0;top:0');}});
 await run('opacity-zero-copy-never-shadows-visible-handle',{
  after:async(page,n)=>{if(n===2)await insertHidden(page,'handle','position:absolute;opacity:0;left:0;top:0');}});
 await run('body-same-text-and-own-links-are-not-identity-or-date-fields',{
  source:html({body:`Owned body mentions ${nameLink}${handleLink}${dateLink}`})});
 await run('quote-same-text-and-own-links-are-not-own-fields',{
  source:html({after:`<blockquote>${nameLink}${handleLink}${dateLink}</blockquote>`})});
 await run('quote-testid-same-links-are-not-own-fields',{
  source:html({after:`<div data-testid="quoteTweet">${nameLink}${handleLink}${dateLink}</div>`})});
 await run('nested-foreign-post-does-not-supply-own-fields',{
  source:html({after:`<article>${nameLink}${handleLink}<a href="${own}/status/999999999"><time datetime="2026-10-30T15:05:22Z">23:05 · 2026年10月30日</time></a></article>`})});
 await run('foreign-host-lookalike-is-not-an-own-field',{
  source:html({before:`<a href="https://owned.test/example_handle_a">示例主播A</a><a href="https://owned.test/example_handle_a">@example_handle_a</a>`,after:`<a href="https://owned.test/example_handle_a/status/${identity.postId}"><time datetime="2026-10-31T15:05:22Z">23:05 · 2026年10月31日</time></a>`})});
 await run('footer-profile-links-are-not-leading-identity-fields',{
  source:html({after:`<footer>${nameLink}${handleLink}</footer>`})});
 for(const role of ['name','handle'])await run(`two-visible-leading-${role}-fields-refuse-ambiguity`,{
  source:role==='name'?html({before:nameLink}):html().replace('<a id="handle"',`${handleLink}<a id="handle"`),expected:'not_skipped',screenshots:null,
  check:r=>assert.equal(r.reason,'EXPIRY_PREFLIGHT_FIELD_AMBIGUOUS')});
 await run('second-visible-own-full-time-even-in-footer-is-ambiguous',{
  source:html({after:`<footer>${dateLink}</footer>`}),expected:'not_skipped',screenshots:2,
  check:r=>assert.equal(r.reason,'EXPIRY_PREFLIGHT_FIELD_AMBIGUOUS')});
 await run('two-native-time-nodes-in-one-own-anchor-are-ambiguous',{
  source:html().replace('</time></a>','</time><time datetime="2026-10-31T15:05:22Z">23:05 · 2026年10月31日</time></a>'),
  expected:'not_skipped',screenshots:2,check:r=>assert.equal(r.reason,'EXPIRY_PREFLIGHT_NATIVE_TIME_AMBIGUOUS')});
 for(const [id,n]of [['name',1],['handle',2],['time',3]])await run(`actual-${id}-hidden-during-screenshot-never-retries`,{
  expected:'not_skipped',screenshots:n,
  after:async(page,calls)=>{if(calls===n)await page.evaluate(id=>{document.getElementById(id).style.display='none';},id);}});
 for(const pointerless of [false,true])await run(`actual-${pointerless?'pointerless-':''}painted-cover-never-retries`,{
  expected:'not_skipped',screenshots:1,
  after:async(page,n)=>{if(n===1)await page.evaluate(markup=>document.body.insertAdjacentHTML('beforeend',markup),overlay(pointerless));},
  check:r=>assert.equal(r.attempts[0].target_diagnostics[0].after.visible,false)});
 for(const kind of ['author','handle','time','datetime','id']){
  const n=kind==='author'?1:kind==='handle'?2:3;
  await run(`actual-${kind}-change-never-retries`,{expected:'not_skipped',screenshots:n,
   after:async(page,calls)=>{if(calls===n)await page.evaluate(kind=>{
    if(kind==='author')document.getElementById('name').textContent='Different author';
    else if(kind==='handle'){const a=document.getElementById('handle');a.textContent='@other_owner';a.href='https://x.com/other_owner';}
    else if(kind==='time')document.querySelector('#time time').textContent='23:06 · 2026年10月31日';
    else if(kind==='datetime')document.querySelector('#time time').dateTime='2026-10-31T15:06:22Z';
    else document.getElementById('time').href='https://x.com/example_handle_a/status/999999999';
   },kind);}});
 }
 assert.equal(externalRequests,0);console.log(JSON.stringify({passed,summaries,external_requests:externalRequests}));
}finally{await browser.close();}
