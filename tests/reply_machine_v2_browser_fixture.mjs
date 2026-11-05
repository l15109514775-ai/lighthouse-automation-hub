// Owned rendered UL/LI/connector fixtures. Every network request is fulfilled locally.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {captureReplyMachineV2} from '../adapters/browser_reply_machine_v2.mjs';
import {captureExtendedTweet} from '../adapters/browser_extended_source.mjs';
const output=process.argv[2];await fs.mkdir(output,{recursive:true});
const browser=await chromium.launch({headless:true}),context=await browser.newContext({viewport:{width:1000,height:900},locale:'zh-CN',timezoneId:'Asia/Shanghai'});
const page=await context.newPage();let scenario='positive',external=0;
const pid='1000000000000000001',rid='1000000000000000002';
const css='<meta charset="utf-8"><style>body{margin:0;font:16px Arial;background:#fff}ul{list-style:none;padding:0;margin:0 40px;width:650px}li{margin:0;padding:0}.wrap{padding:12px 16px}article{display:flex;flex-direction:column;gap:4px}.row{display:flex;gap:8px}.avatar{position:relative;display:flex;width:40px;flex-shrink:0;flex-direction:column;align-items:center;align-self:stretch}.avatar a,.avatar img{display:block;width:40px;height:40px}.text{flex:1;min-width:0}a{color:#222;text-decoration:none}.head{display:flex;gap:10px;height:20px}p{white-space:pre-wrap;margin:8px 0;min-height:24px}.line{margin-top:4px;width:2px;flex:1;margin-bottom:-12px;background:rgba(0,0,0,.15);border-radius:100px 100px 0 0}.actions{height:20px}</style>';
function item(handle,id,text,stamp,connector=false){
 const isParent=handle==='parent_fixture',time=isParent?'12:00':'12:01';
 let line=connector?'<span class="line w-0.5 flex-1 rounded-t-full"></span>':'';
 if(scenario==='missing-line'||scenario==='body-mention-only')line='';
 if(scenario==='red-line')line=line.replace('class="','style="background:rgb(255,0,0)" class="');
 if(scenario==='shifted-line')line=line.replace('class="','style="transform:translateX(12px)" class="');
 return `<li><div class="wrap"><article><div class="row"><div class="avatar"><a href="https://x.com/${handle}"><img alt="@${handle}" src="https://pbs.twimg.com/profile_images/owned/${handle}.png"></a>${line}</div><div class="text"><div class="head"><a href="https://x.com/${handle}">Owned ${isParent?'parent':'reply'}</a><a href="https://x.com/${handle}">@${handle}</a>${isParent&&connector?`<a href="https://x.com/${handle}/status/${id}">${stamp}</a>`:''}</div><p data-testid="tweetText">${text}</p>${!connector?`<a href="https://x.com/${handle}/status/${id}"><time datetime="2026-10-31T${time}:00+08:00">${stamp}</time></a>`:''}<div class="actions"><button>1</button> <button>2</button></div></div></div></article></div></li>`;
}
await page.route('**/*',async route=>{
 const u=new URL(route.request().url());
 if(u.hostname==='pbs.twimg.com'){
  await route.fulfill({status:200,contentType:'image/png',body:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aV1cAAAAASUVORK5CYII=','base64')});return;
 }
 if(u.hostname!=='x.com'){external++;await route.abort();return;}
 if(u.pathname===`/example-channel-b/status/${rid}`){
  const stamp=scenario==='wrong-day'?'10月30日':'10月31日';
  let html=item('parent_fixture',pid,'架空原推20:00',stamp,true)+item('example-channel-b',rid,
   scenario==='body-mention-only'?'回复 @parent_fixture 架空回复21:00':'架空回复21:00','12:01 · 2026年10月31日');
  if(scenario==='multilevel')html=item('earlier_fixture','1000000000000000000','Earlier source','11:00 · 2026年10月31日')+html;
  if(scenario==='truncated')html=html.replace('架空回复21:00</p>','架空回复21:00</p><button data-testid="tweet-text-show-more-link">Show more</button>');
  const cover=scenario==='occluded'?'<div style="position:fixed;inset:0;background:white;z-index:999">Owned occlusion</div>':'';
  await route.fulfill({status:200,contentType:'text/html',body:css+'<ul>'+html+'</ul>'+cover});return;
 }
 if(u.pathname===`/parent_fixture/status/${pid}`){
  let text=scenario==='parent-edited'?'Edited parent20:00':'架空原推20:00';
  await route.fulfill({status:200,contentType:'text/html',body:css+'<ul>'+item('parent_fixture',pid,text,'12:00 · 2026年10月31日')+'</ul>'});return;
 }
 await route.fulfill({status:404,contentType:'text/html',body:'Owned unavailable route'});
});
const passed=[],failures={};
try{
 const positive=await captureReplyMachineV2(page,{handle:'example-channel-b',postId:rid},output);
 assert.equal(positive.packet.thread.reply_to_label,null);
 const day=JSON.parse(await fs.readFile(positive.packet.parent_in_thread_day.file,'utf8'));
 assert.equal(day.payload.post.timestamp_precision,'day');assert.equal('created_at' in day.payload.post,false);
 assert.equal(day.payload.post.published_day.year,null);passed.push('day_parent_and_real_connector_without_invented_label');
 for(const [name,code] of [
  ['missing-line','WEB_REPLY_V2_CONNECTOR_MISSING_OR_AMBIGUOUS'],['red-line','WEB_REPLY_V2_CONNECTOR_GEOMETRY_UNSUPPORTED'],
  ['shifted-line','WEB_REPLY_V2_CONNECTOR_GEOMETRY_UNSUPPORTED'],['multilevel','WEB_REPLY_V2_PARENT_OR_CHAIN_AMBIGUOUS'],
  ['body-mention-only','WEB_REPLY_V2_CONNECTOR_MISSING_OR_AMBIGUOUS'],['truncated','WEB_SOURCE_TEXT_TRUNCATED'],
  ['occluded','WEB_REPLY_V2_CONNECTOR_GEOMETRY_UNSUPPORTED'],['wrong-day','WEB_REPLY_V2_PARENT_DAY_MISMATCH'],
  ['parent-edited','WEB_REPLY_V2_PARENT_REREAD_CHANGED']]){
  scenario=name;
  await assert.rejects(()=>captureReplyMachineV2(page,{handle:'example-channel-b',postId:rid},output),e=>{failures[name]=e.code;
   if(e.code!==code)console.error(JSON.stringify({scenario:name,actual:e.code,expected:code}));return e.code===code;});
  passed.push(name+'_rejected');
 }
 assert.equal(external,0);
 scenario='positive';await page.goto(`https://x.com/example-channel-b/status/${rid}`);
 await captureExtendedTweet(page,{handle:'example-channel-b',postId:rid},path.join(output,'extended-reply'),{trigger:'durable_scheduler'});
 console.log(JSON.stringify({packet_file:positive.packet_file,extended_record_file:path.join(output,'extended-reply/extended-observation.json'),
  passed,failures,external_calls:external,human_review:false,automatic_publication_eligible:false}));
}finally{await context.close();await browser.close();}
