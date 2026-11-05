// Owned dynamic DOM regression only; no live account, source attestation or API.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {performance} from 'node:perf_hooks';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {locateTweetArticle,inspectArticleElement,assertSourceVisible} from '../adapters/browser_observation.mjs';
const output=process.argv[2];await fs.mkdir(output,{recursive:true});
const identity={handle:'fixture_only',postId:'123'};let externalCalls=0,ownedDocuments=0;
const browser=await chromium.launch({headless:true});
const context=await browser.newContext({viewport:{width:1280,height:1000}}),page=await context.newPage();
page.setDefaultTimeout(1000);let documentHtml='';
await context.route('**/*',r=>{
 if(r.request().url().startsWith('https://x.com/fixture_only/status/123')&&r.request().isNavigationRequest()){
  ownedDocuments++;return r.fulfill({contentType:'text/html',body:documentHtml});
 }
 externalCalls++;return r.abort();
});
const escape=value=>value.replaceAll('&','&amp;').replaceAll('"','&quot;').replaceAll('<','&lt;');
function article(id,{author='fixture_only',tag='source',href=`https://x.com/${author}/status/${id}`,extra='',time=true}={}){
 return `<article data-owned="${tag}"><a href="https://x.com/${author}">Owned ${author}</a><a href="https://x.com/${author}">@${author}</a><p data-testid="tweetText">Owned source ${id}</p><a href="${escape(href)}">${time?'<time datetime="2026-10-31T00:00:00Z">12:00</time>':'not a native date'}</a>${extra}</article>`;
}
async function load(body){
 documentHtml=`<!doctype html><style>body{margin:0;font:16px Arial}article{width:500px;margin:15px 100px;padding:8px;background:white}a{display:block}</style>${body}`;
 await page.goto('https://x.com/fixture_only/status/123',{waitUntil:'load'});
}
const passed=[];
try{
 await load([101,102,103,104].map(id=>article(id,{tag:'placeholder'})).join('')+article(123));
 const bound=await locateTweetArticle(page,identity);
 await page.evaluate(()=>document.querySelectorAll('[data-owned=placeholder]').forEach(e=>e.remove()));
 assert.equal((await bound.evaluate(inspectArticleElement)).postId,'123');
 await page.evaluate(()=>{const a=document.querySelector('[data-owned=source]');a.replaceWith(a.cloneNode(true));});
 assert.equal((await bound.evaluate(inspectArticleElement)).postId,'123');
 passed.push('removing_prior_four_articles_and_replacing_source_does_not_invalidate_binding');

 await load(article(456,{tag:'other'})+article(123));
 const second=await locateTweetArticle(page,identity);
 await page.evaluate(()=>document.body.insertBefore(document.querySelector('[data-owned=source]'),document.querySelector('[data-owned=other]')));
 assert.equal((await second.evaluate(inspectArticleElement)).postId,'123');
 passed.push('reordered_article_never_retargets_another_post');

 await load([101,102,103,104].map(id=>article(id,{tag:'placeholder'})).join('')+article(123));
 const actualLocator=page.locator.bind(page);let moved=false;
 page.locator=(selector,...args)=>{
  const locator=actualLocator(selector,...args);
  if(selector==='a[href]'){
   const actualEvaluateAll=locator.evaluateAll.bind(locator);
   locator.evaluateAll=async(...params)=>{const result=await actualEvaluateAll(...params);
    if(!moved){moved=true;await page.evaluate(()=>document.querySelectorAll('[data-owned=placeholder]').forEach(e=>e.remove()));}
    return result;
   };
  }
  return locator;
 };
 const hydrated=await locateTweetArticle(page,identity);page.locator=actualLocator;
 assert.equal(moved,true);assert.equal((await hydrated.evaluate(inspectArticleElement)).postId,'123');
 passed.push('hydration_between_href_snapshot_and_identity_lookup_keeps_source');

 const variants=[...['http','https'].flatMap(scheme=>['x.com','www.x.com','twitter.com','www.twitter.com'].map(host=>`${scheme}://${host}/fixture_only/status/123`)),
  '/fixture_only/status/123','/fixture_only/status/123/','./123','../../fixture_only/status/123',
  'https://x.com/FIXTURE_ONLY/status/123/?s=20#detail','/fixture_only/status/123?owned="quoted"&test=\\native#part'];
 for(const href of variants){
  await load(article(123,{href}));const found=await locateTweetArticle(page,identity);
  assert.equal((await found.evaluate(inspectArticleElement)).postId,'123');
 }
 passed.push('native_http_https_x_twitter_www_relative_tail_query_fragment_and_css_quotes');

 await load(article(123)+article(123));
 await assert.rejects(locateTweetArticle(page,identity),{code:'WEB_SOURCE_ARTICLE_AMBIGUOUS'});
 await load(article(999,{author:'foreign_only',extra:article(123)}));
 await assert.rejects(locateTweetArticle(page,identity),{code:'WEB_SOURCE_ARTICLE_AMBIGUOUS'});
 passed.push('duplicate_and_nested_same_id_are_ambiguous');

 await load(article(999,{author:'foreign_only',extra:'<blockquote><a href="https://x.com/fixture_only/status/123">quoted target</a></blockquote>'}));
 await assert.rejects(locateTweetArticle(page,identity),{code:'WEB_SOURCE_ARTICLE_MISSING'});
 await load(article(123,{time:false}));
 await assert.rejects(locateTweetArticle(page,identity),{code:'WEB_SOURCE_ARTICLE_MISSING'});
 passed.push('foreign_quote_and_non_date_permalink_do_not_become_primary');

 await load(article(123).replace('href="https://x.com/fixture_only">@fixture_only','href="https://x.com/other_only">@other_only'));
 await assert.rejects(locateTweetArticle(page,identity),{code:'WEB_SOURCE_AUTHOR_UNVERIFIED'});
 await load(article(123,{href:'https://x.com.evil.test/fixture_only/status/123'}));
 await assert.rejects(locateTweetArticle(page,identity),{code:'WEB_SOURCE_ARTICLE_MISSING'});
 passed.push('author_and_allowed_native_host_remain_required');

 await load(article(123));let removed=false;
 page.locator=(selector,...args)=>{
  const locator=actualLocator(selector,...args);
  if(selector==='article'){
   const actualFilter=locator.filter.bind(locator);
   locator.filter=options=>{const selected=actualFilter(options),actualCount=selected.count.bind(selected);
    selected.count=async()=>{const count=await actualCount();if(!removed){removed=true;await page.evaluate(()=>document.querySelector('[data-owned=source]').remove());}return count;};
    return selected;
   };
  }
  return locator;
 };
 const began=performance.now();await assert.rejects(locateTweetArticle(page,identity),{code:'WEB_SOURCE_ARTICLE_MISSING'});
 assert.ok(performance.now()-began<2000);page.locator=actualLocator;
 passed.push('identity_disappearing_during_lookup_is_missing_without_ten_second_nth_wait');

 await load(article(123));const visible=await locateTweetArticle(page,identity),before=await page.content();
 await assertSourceVisible(page,visible,path.join(output,'visible'));
 assert.equal(await page.content(),before);
 await load(article(123)+'<div style="position:fixed;inset:0;background:white;z-index:100">Owned opaque overlay</div>');
 const covered=await locateTweetArticle(page,identity),coveredHtml=await page.content();
 await assert.rejects(assertSourceVisible(page,covered,path.join(output,'covered')),{code:'WEB_SOURCE_OBSCURED'});
 assert.equal(await page.content(),coveredHtml);
 passed.push('visibility_checks_and_unmodified_public_dom_remain_enforced');

 assert.equal(externalCalls,0);
 console.log(JSON.stringify({passed,owned_documents:ownedDocuments,external_calls:externalCalls,
  human_review:false,source_approval:false,model_calls:0,publish_requests:0}));
}finally{await context.close();await browser.close();}
