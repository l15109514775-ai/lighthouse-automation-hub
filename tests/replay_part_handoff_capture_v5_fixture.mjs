import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {captureOwnerScreenshot} from '../adapters/replay_part_owner_handoff_v5.mjs';
const [scenario,output]=process.argv.slice(2);
assert.ok(['retry','persistent_failure','other_error'].includes(scenario));
await fs.mkdir(output,{recursive:true});
const browser=await chromium.launch({headless:true,chromiumSandbox:true}),context=await browser.newContext();
let outside=0,clicks=0,choices=0,calls=0;
try{
 await context.route('**/*',r=>{
  if(r.request().url()!=='https://owned.invalid/capture'){outside++;return r.abort();}
  return r.fulfill({status:200,contentType:'text/html',body:'<body><h1>Owned capture only</h1><input type=password value=owned-only><div style="height:150px;width:300px">Owned visible fixture</div></body>'});
 });
 const page=await context.newPage();page.on('filechooser',()=>choices++);await page.goto('https://owned.invalid/capture');
 await page.exposeBinding('ownedClick',()=>clicks++);await page.evaluate(()=>document.addEventListener('click',()=>window.ownedClick()));
 const original=page.screenshot.bind(page),file=path.join(output,'capture.png');
 page.screenshot=async options=>{
  calls++;assert.equal(options.path,file);assert.equal(options.fullPage,false);assert.ok(options.timeout>0&&options.timeout<=15000);
  assert.equal(options.mask.length,page.frames().length);assert.ok(options.mask.every(m=>m.toString().includes('input[type="password"]')));
  if(scenario==='other_error')throw new Error('Owned unrelated rendering failure');
  if(scenario==='persistent_failure'||calls===1)throw new Error('page.screenshot: Protocol error (Page.captureScreenshot): Unable to capture screenshot');
  return original(options);
 };
 if(scenario==='retry'){
  await captureOwnerScreenshot(page,file);assert.equal(calls,2);
  const image=await fs.readFile(file);assert.equal(image.subarray(0,8).toString('hex'),'89504e470d0a1a0a');
  assert.equal(image.readUInt32BE(16),1280);assert.equal(image.readUInt32BE(20),720);
 }else{
  await assert.rejects(()=>captureOwnerScreenshot(page,file),e=>e.message.includes(scenario==='persistent_failure'?'Unable to capture screenshot':'unrelated rendering failure'));
  assert.equal(calls,scenario==='persistent_failure'?2:1);await assert.rejects(()=>fs.stat(file),e=>e.code==='ENOENT');
  if(scenario==='persistent_failure'){
   calls=0;await page.evaluate(()=>window.requestAnimationFrame=()=>0);
   const before=performance.now();await assert.rejects(()=>captureOwnerScreenshot(page,file,80),e=>e.code==='REPLAY_PART_HANDOFF_SCREENSHOT_NOT_READY');
   assert.ok(performance.now()-before<2000);assert.equal(calls,0);await assert.rejects(()=>fs.stat(file),e=>e.code==='ENOENT');
  }
 }
 assert.equal(outside,0);assert.equal(clicks,0);assert.equal(choices,0);assert.equal(page.url(),'https://owned.invalid/capture');
 console.log(JSON.stringify({scenario,owned:true,real_png:scenario==='retry',mask_preserved:true,bounded:true,platform_actions:0}));
}finally{await context.close();await browser.close();}
