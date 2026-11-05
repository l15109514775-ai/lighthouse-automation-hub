// Owned HTML served by route.fulfill only; no real X/model/account traffic.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {captureReplyMachine} from '../adapters/browser_reply_machine.mjs';
const output=process.argv[2];await fs.mkdir(output,{recursive:true});
const browser=await chromium.launch({headless:true});
const context=await browser.newContext({viewport:{width:1000,height:900},locale:'zh-CN',timezoneId:'Asia/Shanghai'});
const page=await context.newPage();let scenario='positive';
const style='<meta charset="utf-8"><style>body{margin:0;background:#eee;font:16px Arial}article{box-sizing:border-box;width:650px;min-height:200px;margin:0 40px;background:white;border-bottom:1px solid #bbb;padding:18px}a{display:inline-block;margin-right:12px}p{white-space:pre-wrap} .target{margin:0 0 12px}</style>';
const pid='1000000000000000001',rid='1000000000000000002';
function article(handle,id,name,text,time,label=''){
 return `<article><div><a href="https://x.com/${handle}">${name}</a><a href="https://x.com/${handle}">@${handle}</a></div>${label}<p data-testid="tweetText">${text}</p><a href="https://x.com/${handle}/status/${id}"><time datetime="2026-10-31T${time}:00+08:00">${time} · 2026年10月31日</time></a></article>`;
}
const parent=()=>article('parent_fixture',pid,'Owned parent fixture','架空原推20:00','12:00');
const reply=label=>article('example-channel-b',rid,'Owned reply fixture','架空回复21:00','12:01',label);
const label=target=>`<div class="target">回复 <a href="https://x.com/${target}">@${target}</a></div>`;
let external=0;
await page.route('**/*',async route=>{
 const u=new URL(route.request().url());
 if(u.hostname!=='x.com'){external++;await route.abort();return;}
 if(u.pathname===`/example-channel-b/status/${rid}`){
  let html=parent()+reply(label(scenario==='wrong-target'?'other_fixture':'parent_fixture'));
  if(scenario==='multilevel')html=article('earlier_fixture','1000000000000000000','Earlier fixture','Earlier text','11:00')+html;
  if(scenario==='truncated')html=html.replace('架空回复21:00</p>','架空回复21:00</p><button data-testid="tweet-text-show-more-link">Show more</button>');
  if(scenario==='body-mention-only')html=parent()+reply('').replace('架空回复21:00','回复 @parent_fixture 架空回复21:00');
  if(scenario==='collapsed')html=parent()+'<button>查看之前的回复</button>'+reply(label('parent_fixture'));
  if(scenario==='occluded')html+='<div style="position:fixed;inset:0;background:white;z-index:100">Owned occlusion fixture</div>';
  await route.fulfill({status:200,contentType:'text/html',body:style+html});return;
 }
 if(u.pathname===`/parent_fixture/status/${pid}`){
  let html=parent();
  if(scenario==='wrong-parent')html=article('other_fixture','1000000000000000009','Wrong fixture','Wrong source','12:00');
  if(scenario==='parent-edited')html=html.replace('架空原推20:00','Edited reread20:00');
  await route.fulfill({status:200,contentType:'text/html',body:style+html});return;
 }
 await route.fulfill({status:404,contentType:'text/html',body:'Owned route not found'});
});
const passed=[],failures={};
try{
 const positive=await captureReplyMachine(page,{handle:'example-channel-b',postId:rid},output);
 assert.equal(positive.packet.human_review,false);assert.equal(positive.packet.official_api,false);
 assert.equal(positive.packet.thread.reply_to_label.text,'回复 @parent_fixture');
 assert.deepEqual(positive.packet.thread.article_ids_through_reply,[pid,rid]);
 passed.push('real_dom_packet_contains_parent_reply_label_and_independent_reread');
 for(const [name,expected] of [
  ['wrong-target','WEB_REPLY_VISIBLE_TARGET_MISSING_OR_MISMATCHED'],
  ['multilevel','WEB_REPLY_PARENT_OR_CHAIN_AMBIGUOUS'],
  ['truncated','WEB_SOURCE_TEXT_TRUNCATED'],
  ['occluded','WEB_SOURCE_OBSCURED'],
  ['body-mention-only','WEB_REPLY_VISIBLE_TARGET_MISSING_OR_MISMATCHED'],
  ['collapsed','WEB_REPLY_COLLAPSED_CONTEXT_UNSUPPORTED'],
  ['wrong-parent','WEB_SOURCE_ARTICLE_MISSING'],
  ['parent-edited','WEB_REPLY_PARENT_REREAD_CHANGED'],
 ]){
  scenario=name;
  await assert.rejects(()=>captureReplyMachine(page,{handle:'example-channel-b',postId:rid},output),error=>{
   failures[name]=error.code;return error.code===expected;
  });passed.push(name+'_rejected');
 }
 assert.equal(external,0);
 console.log(JSON.stringify({packet_file:positive.packet_file,passed,failures,external_calls:external,human_review:false,
  automatic_publication_eligible:false}));
}finally{await context.close();await browser.close();}
