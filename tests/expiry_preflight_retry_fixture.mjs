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
const old='23:05 · 2026年10月31日';
function html({time=old,native=false,overlay=''}={}){
 return `<!doctype html><html lang="zh"><style>body{margin:0;background:white;font:16px Arial;min-height:1800px}article{margin:80px 100px;width:600px}a{display:block;width:max-content;margin:8px}</style><body><article><a id="name" href="https://x.com/example_handle_a">示例主播A</a><a id="handle" href="https://x.com/example_handle_a">@example_handle_a</a><div data-testid="tweetText">Owned folded source…</div><a id="time" href="https://x.com/example_handle_a/status/${identity.postId}">${native?`<time datetime="2026-10-31T15:05:22Z">${time}</time>`:time}</a></article>${overlay}</body></html>`;
}
const cover=pointerless=>`<div role="dialog" style="position:fixed;inset:0;background:white;z-index:999;${pointerless?'pointer-events:none':''}">Owned obstruction</div>`;
const changeGeometry=page=>page.evaluate(()=>{const e=document.querySelector('#name');e.style.marginLeft=(parseInt(e.style.marginLeft||'0')+30)+'px';});
let externalRequests=0;const passed=[],summaries=[];
const browser=await chromium.launch({headless:true});
try{
 async function run(name,{source=html(),decision='not_skipped',attempts=1,screenshots=0,after,onRetry,check,immutable=false}={}){
  const dir=path.join(output,name),context=await browser.newContext({viewport:{width:1280,height:1000},locale:'zh-CN',timezoneId:'Asia/Shanghai'});
  await context.route('**/*',route=>{externalRequests++;return route.abort();});
  const page=await context.newPage();page.setDefaultTimeout(1500);await page.setContent(source);const originalHTML=await page.content();
  const actualScreenshot=page.screenshot.bind(page);let calls=0;
  page.screenshot=async options=>{
   calls++;assert.equal(options.fullPage,false);assert.equal(options.caret,'initial');assert.equal(options.animations,'allow');
   assert.equal(options.mask,undefined);assert.equal(options.style,undefined);
   const bytes=await actualScreenshot(options),meta=await sharp(bytes).metadata();
   assert.equal(meta.width,options.clip.width);assert.equal(meta.height,options.clip.height);
   if(after)await after(page,calls);return bytes;
  };
  const actualWait=page.waitForTimeout.bind(page);
  page.waitForTimeout=async milliseconds=>{
   if(milliseconds===75&&onRetry)await onRetry(page,calls);
   return actualWait(milliseconds);
  };
  try{
   const result=await preflightExpiredTweet(page,identity,dir,{notBefore:cutoff});
   assert.equal(result.version,'x-expiry-preflight-v1');assert.equal(result.purpose,'skip_expired_only');
   for(const key of ['complete_source','source_attested','publication_authorized','human_review','official_api'])assert.equal(result[key],false);
   assert.equal(result.decision,decision,`${name}: ${result.reason}`);
   assert.equal(result.attempts.length,attempts,name);assert.equal(calls,screenshots,name);
   assert.deepEqual(result.retry_policy,{version:'strict-expiry-bounded-retry-v1',max_attempts:3,wait_ms:75,
    eligible_error_codes:['EXPIRY_PREFLIGHT_TARGET_CHANGED','EXPIRY_PREFLIGHT_CONFIRMATIONS_DIFFER'],geometry_only:true});
   const final=result.attempts.at(-1);assert.deepEqual(result.observations,final.observations);
   assert.equal(result.reason,final.reason);assert.equal(result.attempt_index,attempts);
   assert.equal(result.evidence_file,path.join(dir,'expiry-preflight.json'));
   const imagePaths=new Set();let auditedImages=0;
   for(const [index,attempt]of result.attempts.entries()){
    const attemptDir=path.join(dir,`attempt-${index+1}`);
    assert.equal(attempt.attempt_index,index+1);assert.equal(attempt.evidence_file,path.join(attemptDir,'expiry-preflight.json'));
    const saved=JSON.parse(await fs.readFile(attempt.evidence_file,'utf8'));
    assert.equal(saved.decision,attempt.decision);assert.equal(saved.reason,attempt.reason);
    assert.deepEqual(saved.target_diagnostics,attempt.target_diagnostics);
    assert.equal(attempt.human_review,false);assert.equal(attempt.publication_authorized,false);
    for(const diagnostic of attempt.target_diagnostics){
     assert.ok(diagnostic.before,'failed targets retain their actual pre-read');
     assert.equal(typeof diagnostic.retry_eligible,'boolean');assert.ok(Array.isArray(diagnostic.mismatch_fields));
     if(diagnostic.screenshot_file){
      assert.equal(path.dirname(diagnostic.screenshot_file),attemptDir);
      assert.equal(imagePaths.has(diagnostic.screenshot_file),false,'attempts never overwrite each other');
      imagePaths.add(diagnostic.screenshot_file);auditedImages++;
      assert.equal(hash(await fs.readFile(diagnostic.screenshot_file)),diagnostic.screenshot_sha256);
      if(!diagnostic.after){
       assert.equal(diagnostic.retry_eligible,false);
       assert.equal(diagnostic.error_name,'TimeoutError');
       assert.equal(typeof diagnostic.error_message,'string');
      }
     }
    }
   }
   assert.equal(auditedImages,calls);
   if(decision==='skipped_expired'){
    assert.equal(result.observations.length,2);
    for(const reading of result.observations)for(const field of ['author_name','author_handle','timestamp']){
     assert.equal(reading[field].visible,true);assert.equal(path.dirname(reading[field].screenshot_file),path.join(dir,`attempt-${attempts}`));
    }
   }
   if(!after)assert.equal(await page.content(),originalHTML,'preflight must not rewrite DOM or CSS');
   if(check)await check(result);
   if(immutable){
    const files=[result.evidence_file,...result.attempts.map(a=>a.evidence_file),...imagePaths];
    const before=await Promise.all(files.map(async file=>hash(await fs.readFile(file))));
    await assert.rejects(preflightExpiredTweet(page,identity,dir,{notBefore:cutoff}),e=>e.code==='EXPIRY_PREFLIGHT_EVIDENCE_ALREADY_EXISTS');
    assert.deepEqual(await Promise.all(files.map(async file=>hash(await fs.readFile(file)))),before);
    assert.equal(calls,screenshots,'refused reuse performs no extra screenshots');
   }
   summaries.push({name,decision:result.decision,attempts:result.attempts.length,screenshots:calls});passed.push(name);
  }finally{await context.close();}
 }
 await run('stable-source-one-attempt-six-real-clips',{decision:'skipped_expired',screenshots:6,immutable:true});
 await run('geometry-change-first-target-then-six-fresh-clips',{decision:'skipped_expired',attempts:2,screenshots:7,
  after:async(page,n)=>{if(n===1)await changeGeometry(page);},check:r=>{
   const failed=r.attempts[0],d=failed.target_diagnostics[0];assert.equal(failed.decision,'not_skipped');
   assert.equal(failed.reason,'EXPIRY_PREFLIGHT_TARGET_CHANGED');assert.equal(failed.observations.length,0);
   assert.equal(d.retry_eligible,true);assert.deepEqual(d.mismatch_fields,['rect']);
   for(const field of ['visible','text','href','datetime'])assert.deepEqual(d.before[field],d.after[field]);
  },immutable:true});
 await run('moving-every-attempt-never-gets-fourth-attempt',{attempts:3,screenshots:3,
  after:changeGeometry,check:r=>{for(const a of r.attempts){assert.equal(a.decision,'not_skipped');assert.equal(a.retry_eligible,true);}}});
 await run('confirmation-geometry-change-demands-two-new-full-readings',{decision:'skipped_expired',attempts:2,screenshots:12,
  after:async(page,n)=>{if(n===3)await changeGeometry(page);},check:r=>{
   assert.equal(r.attempts[0].reason,'EXPIRY_PREFLIGHT_CONFIRMATIONS_DIFFER');
   assert.equal(r.attempts[0].confirmation_comparison.retry_eligible,true);assert.equal(r.attempts[0].observations.length,2);
  }});
 await run('viewport-only-change-can-retry-with-new-proofs',{decision:'skipped_expired',attempts:2,screenshots:7,
  after:async(page,n)=>{if(n===1)await page.setViewportSize({width:1290,height:1000});},
  check:r=>assert.ok(r.attempts[0].target_diagnostics[0].mismatch_fields.includes('viewport'))});
 await run('native-scroll-change-can-retry-with-new-proofs',{decision:'skipped_expired',attempts:2,screenshots:7,
  after:async(page,n)=>{if(n===1)await page.evaluate(()=>scrollBy(0,40));},
  check:r=>assert.ok(r.attempts[0].target_diagnostics[0].mismatch_fields.includes('scroll_y'))});
 await run('static-obstruction-never-retries',{source:html({overlay:cover(false)}),
  check:r=>{assert.equal(r.attempts[0].retry_eligible,false);assert.equal(r.reason,'EXPIRY_PREFLIGHT_TARGET_NOT_VISIBLE');}});
 await run('static-pointerless-painted-cover-never-retries',{source:html({overlay:cover(true)}),
  check:r=>assert.equal(r.attempts[0].retry_eligible,false)});
 for(const pointerless of [false,true])await run(`dynamic-${pointerless?'pointerless-':''}obstruction-never-retries`,{screenshots:1,
  after:async(page,n)=>{if(n===1)await page.evaluate(markup=>document.body.insertAdjacentHTML('beforeend',markup),cover(pointerless));},
  check:r=>{const d=r.attempts[0].target_diagnostics[0];assert.equal(d.before.visible,true);assert.equal(d.after.visible,false);assert.equal(d.retry_eligible,false);}});
 await run('recent-timestamp-cannot-skip-or-retry',{source:html({time:'03:05 · 2026年11月1日'}),screenshots:3,
  check:r=>{assert.equal(r.reason,'EXPIRY_PREFLIGHT_NOT_CERTAINLY_EXPIRED');assert.equal(r.attempts[0].retry_eligible,false);}});
 for(const kind of ['author','handle','time','datetime','id'])await run(`changed-${kind}-never-retries`,{
  source:html({native:kind==='datetime'}),screenshots:kind==='author'?1:kind==='handle'?2:3,
  after:async(page,n)=>{
   if(n!==(kind==='author'?1:kind==='handle'?2:3))return;
   await page.evaluate(kind=>{
    if(kind==='author')document.querySelector('#name').textContent='Owned different author';
    else if(kind==='handle'){document.querySelector('#handle').textContent='@other_owner';document.querySelector('#handle').href='https://x.com/other_owner';}
    else if(kind==='time')document.querySelector('#time').textContent='23:06 · 2026年10月31日';
    else if(kind==='datetime')document.querySelector('time').dateTime='2026-10-31T15:06:22Z';
    else document.querySelector('#time').href='https://x.com/example_handle_a/status/999999999';
   },kind);
  },check:r=>{assert.ok(['EXPIRY_PREFLIGHT_TARGET_CHANGED','EXPIRY_PREFLIGHT_UNREADABLE'].includes(r.reason));assert.equal(r.attempts[0].retry_eligible,false);}});
 for(const kind of ['time','id'])await run(`first-geometric-failure-does-not-reset-${kind}-identity`,{attempts:2,screenshots:1,
  after:async(page,n)=>{if(n===1)await changeGeometry(page);},
  onRetry:async page=>{await page.evaluate(kind=>{
   if(kind==='time')document.querySelector('#time').textContent='23:06 · 2026年10月31日';
   else document.querySelector('#time').href='https://x.com/example_handle_a/status/999999999';
  },kind);},check:r=>{
   assert.equal(r.attempts[0].retry_eligible,true);assert.equal(r.attempts[1].retry_eligible,false);
   assert.equal(r.attempts[1].target_diagnostics.length,0);assert.equal(r.observations.length,0);
   assert.ok(['EXPIRY_PREFLIGHT_IDENTITY_OR_TIME_CHANGED','WEB_SOURCE_ARTICLE_MISSING'].includes(r.reason));
  }});
 assert.equal(externalRequests,0);console.log(JSON.stringify({passed,summaries,external_requests:externalRequests}));
}finally{await browser.close();}
