// Owned original-post link fixture, no external content, models or sends.
import fs from 'node:fs/promises';
import path from 'node:path';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {captureExtendedTweet} from '../adapters/browser_extended_source.mjs';
const output=process.argv[2];await fs.mkdir(output,{recursive:true});
const browser=await chromium.launch({headless:true}),context=await browser.newContext({viewport:{width:1000,height:900},locale:'zh-CN',timezoneId:'Asia/Shanghai'});
const page=await context.newPage();let external=0;
await page.route('**/*',route=>{const u=new URL(route.request().url());if(u.hostname!=='x.com'){external++;return route.abort();}
 return route.fulfill({status:200,contentType:'text/html',body:'<meta charset="utf-8"><article style="width:650px;margin:50px;background:white"><a href="https://x.com/example_handle_b">Owned extended fixture</a><a href="https://x.com/example_handle_b">@example_handle_b</a><p data-testid="tweetText" style="white-space:pre-wrap">配信 <a href="https://www.youtube.com/watch?v=owned_fixture">youtube.com/watch…</a>\n20:00</p><a href="https://x.com/example_handle_b/status/1000000000000000003"><time datetime="2026-10-31T12:01:00+08:00">12:01 · 2026年10月31日</time></a></article>'});});
try{
 await page.goto('https://x.com/example_handle_b/status/1000000000000000003');
 const record=await captureExtendedTweet(page,{handle:'example_handle_b',postId:'1000000000000000003'},path.join(output,'original'),
  {trigger:process.argv[3]||'durable_job'});
 console.log(JSON.stringify({record_file:path.join(output,'original/extended-observation.json'),external_calls:external,
  raw_text:record.machine_extended_source.nodes[0].raw_source.post.text,normalized_text:record.payload.post.text,human_review:false}));
}finally{await context.close();await browser.close();}
