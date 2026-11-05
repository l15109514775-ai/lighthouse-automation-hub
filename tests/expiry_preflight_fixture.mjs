import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {preflightExpiredTweet,expiryTimeInterval} from '../adapters/browser_expiry_preflight.mjs';
const output=process.argv[2];await fs.mkdir(output,{recursive:true});
const identity={handle:'example_handle_a',postId:'2116547209064071229'};
const cutoff=Date.parse('2026-10-31T18:00:00Z')/1000;
const old='23:05 · 2026年10月31日';
let requests=0;const passed=[];
const browser=await chromium.launch({headless:true});
const context=await browser.newContext({viewport:{width:1280,height:1000},locale:'zh-CN',timezoneId:'Asia/Shanghai'});
await context.route('**/*',route=>{requests++;return route.abort();});
const page=await context.newPage();
function html({time=old,body='Owned quoted source <blockquote>Truncated quoted text…</blockquote>',after='',style='',prefix='',native=null}={}){
 return `<!doctype html><html lang="zh"><style>body{margin:0;background:white;color:black;font:16px Arial;}article{margin:80px 100px 80px;width:600px;padding:10px;}a{display:block;color:blue;width:max-content;max-width:100%;margin:8px;}[data-testid=tweetText]{margin:20px;} ${style}</style><body>${prefix}<article><a href="https://x.com/example_handle_a">示例主播A</a><a href="https://x.com/example_handle_a">@example_handle_a</a><div data-testid="tweetText">${body}</div><a id="own-time" href="https://x.com/example_handle_a/status/${identity.postId}">${native===null?time:`<time datetime="${native}">${time}</time>`}</a></article>${after}</body></html>`;
}
async function run(name,source,expected,options={}){
 await page.setContent(source);const original=await page.content();
 const result=await preflightExpiredTweet(page,options.identity||identity,path.join(output,name),{notBefore:options.cutoff??cutoff});
 assert.equal(result.decision,expected,`${name}: ${result.reason}`);
 assert.equal(result.version,'x-expiry-preflight-v1');assert.equal(result.purpose,'skip_expired_only');
 for(const field of ['complete_source','source_attested','publication_authorized','human_review','official_api'])assert.equal(result[field],false);
 assert.equal(result.payload,undefined);assert.equal((await fs.stat(result.evidence_file)).isFile(),true);
 if(!options.mutating)assert.equal(await page.content(),original,'preflight must not rewrite DOM/CSS');
 if(expected==='not_skipped'){
  assert.equal(typeof result.error_name,'string');assert.equal(typeof result.diagnostic,'string');
  assert.ok(result.diagnostic.length>0&&result.diagnostic.length<=700);
 }
 if(expected==='skipped_expired'){
  assert.equal(result.observations.length,2);assert.equal(result.observations[0].post_id,identity.postId);
  for(const reading of result.observations)for(const field of ['author_name','author_handle','timestamp']){
   assert.equal(reading[field].visible,true);assert.match(reading[field].screenshot_sha256,/^[a-f0-9]{64}$/);
  }
 }
 passed.push(name);return result;
}
try{
 await run('expired-folded-quote-never-attests-full-source',html(),'skipped_expired');
 await run('recent-full-date',html({time:'03:05 · 2026年11月1日'}),'not_skipped');
 await run('minute-partially-overlaps-cutoff',html(),'not_skipped',{cutoff:Date.parse('2026-10-31T15:05:30Z')/1000});
 await run('minute-completely-before-cutoff',html(),'skipped_expired',{cutoff:Date.parse('2026-10-31T15:06:00Z')/1000});
 await run('missing-year-not-inferred',html({time:'10月31日'}),'not_skipped');
 await run('visible-native-datetime',html({time:'3小时',native:'2026-10-31T15:05:22.000Z'}),'skipped_expired');
 await run('hidden-native-time',html({time:'3小时',native:'2026-10-31T15:05:22.000Z',style:'time{visibility:hidden}'}),'not_skipped');
 await run('full-dialog-no-dismiss-or-bypass',html({after:'<div role="dialog" style="position:fixed;inset:0;background:white;z-index:100">人机验证<button>Close</button></div>'}),'not_skipped');
 await run('pointerless-painted-cover',html({after:'<div style="position:fixed;inset:0;background:white;pointer-events:none;z-index:100"></div>'}),'not_skipped');
 await run('timestamp-behind-fixedbar',html({style:'body{height:2000px} #own-time{position:fixed;top:0}',after:'<div style="position:fixed;top:0;left:0;width:100%;height:53px;background:white;z-index:100">Native bar</div>'}),'not_skipped');
 await run('wrong-post-id',html(),'not_skipped',{identity:{...identity,postId:'999'}});
 await run('body-mention-cannot-supply-author',html().replace('<a href="https://x.com/example_handle_a">@example_handle_a</a>','').replace('Owned quoted source','<a href="https://x.com/example_handle_a">@example_handle_a</a>Owned source'),'not_skipped');
 await run('body-self-link-cannot-supply-time',html({time:'10月31日',body:`<a href="https://x.com/example_handle_a/status/${identity.postId}">${old}</a>`}),'not_skipped');
 await run('calendar-invalid',html({time:'23:05 · 2026年2月30日'}),'not_skipped');
 await run('clipped-author',html({style:'article>a:first-child{width:20px;overflow:hidden;white-space:nowrap}'}),'not_skipped');
 await run('long-post-separate-native-scroll-readings',html({body:'<div style="height:1800px">Long incomplete source never attested</div>',prefix:'<div style="position:fixed;top:0;left:0;height:53px;width:100%;background:white;z-index:3">Native bar</div>'}),'skipped_expired');
 await run('native-time-changes-between-confirmations',html({body:'<div style="height:1800px">Long source</div>',after:`<script>let readings=0;const ordinaryScroll=Element.prototype.scrollIntoView;Element.prototype.scrollIntoView=function(...args){if(++readings===4)document.querySelector('#own-time').textContent='23:06 · 2026年10月31日';return ordinaryScroll.apply(this,args);};</script>`}),'not_skipped',{mutating:true});
 assert.equal(expiryTimeInterval({kind:'visible_year_minute',label:old,browser_timezone:'UTC'}),null);
 assert.equal(expiryTimeInterval({kind:'native_time_datetime',datetime:'2026-10-31T15:05:00+14:01'}),null);
 passed.push('invalid-timezone-and-offset-rejected');
 assert.equal(requests,0);console.log(JSON.stringify({passed,external_requests:requests}));
}finally{await context.close();await browser.close();}
