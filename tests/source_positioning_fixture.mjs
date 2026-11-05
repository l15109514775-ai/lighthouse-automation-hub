// Owned pages only: real native scrolling, screenshots and source adapters.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {captureTweet} from '../adapters/browser_tweet_capture.mjs';
import {locateTweetArticle,assertSourceVisible} from '../adapters/browser_observation.mjs';
import {capturePublicMachineMedia} from '../adapters/machine_media_public.mjs';
import {verifyMachineMediaBundle} from '../adapters/machine_media_capture.mjs';
import {captureReplyMachineV2} from '../adapters/browser_reply_machine_v2.mjs';
import {createPublicXSession} from '../adapters/browser_x_public.mjs';
import {captureExtendedTweet} from '../adapters/browser_extended_source.mjs';

const output=path.resolve(process.argv[2]);await fs.mkdir(output,{recursive:true});
const sharp=createRequire(new URL('../compositor/package.json',import.meta.url))('sharp');
const hash=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
const identity={handle:'example_handle_a',postId:'2116547209064071229'};
const ownURL=`https://x.com/${identity.handle}/status/${identity.postId}`;
const parentId='1000000000000000001',replyId='1000000000000000002';
const replyIdentity={handle:'example-channel-b',postId:replyId};
const replyURL=`https://x.com/example-channel-b/status/${replyId}`,parentURL=`https://x.com/parent_fixture/status/${parentId}`;
const mediaURL='https://pbs.twimg.com/media/OwnedPositioning?format=webp&name=large';
const full=await sharp({create:{width:160,height:120,channels:3,background:'#c8d9ea'}}).webp().toBuffer();
const small=await sharp({create:{width:80,height:60,channels:3,background:'#c8d9ea'}}).webp().toBuffer();
const avatar=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aV1cAAAAASUVORK5CYII=','base64');
const bodyText=Array.from({length:20},(_,i)=>`Owned complete source line ${i}.`).join('\n');
const header='<div class="native-header" style="position:fixed;left:0;right:0;top:0;height:53px;background:#eee;z-index:500">Owned native sticky header</div>';
function singleHTML({height=990,gap=500,cover='',media=false,crop=false,owner=identity,name='Owned source',text=bodyText}={}){
 const image=media?`<a id="photo" href="/${owner.handle}/status/${owner.postId}/photo/1" style="position:absolute;bottom:48px;left:0"><img width="160" height="120" style="display:block;width:160px;height:120px" src="${mediaURL}${crop?'&rect=0,0,80,60':''}"></a>`:'';
 const covering=cover==='absolute'?`<div style="position:absolute;left:270px;top:${gap}px;width:600px;height:${height}px;background:white;z-index:999">Owned absolute cover</div>`:
  cover?`<div style="position:fixed;inset:0;background:white;z-index:999;${cover==='pointerless'?'pointer-events:none':''}">Owned full cover</div>`:'';
 return `<!doctype html><meta charset="utf-8"><style>body{margin:0;min-height:5500px;font:16px/20px Arial;background:white}article{position:relative;margin-left:270px;width:600px;height:${height}px;background:white}article a{display:block;width:max-content;color:#222}p{white-space:pre-wrap;margin:12px 0;line-height:24px}</style>${header}<div style="height:${gap}px"></div><article><a id="name" href="https://x.com/${owner.handle}">${name}</a><a id="handle" href="https://x.com/${owner.handle}">@${owner.handle}</a><p id="body" data-testid="tweetText">${text}</p>${image}<a id="time" style="position:absolute;bottom:10px;left:0" href="https://x.com/${owner.handle}/status/${owner.postId}"><time datetime="2026-10-31T12:00:00+08:00">12:00 · 2026年10月31日</time></a></article>${covering}`;
}
function threadHTML({parentHeight=400,replyHeight=500,cover=false}={}){
 const css=`<meta charset="utf-8"><style>body{margin:0;font:16px Arial;background:white;min-height:5500px}ul{list-style:none;padding:0;margin:500px 0 0 270px;width:650px}li{margin:0;padding:0}.wrap{padding:12px 16px}article{display:flex;flex-direction:column;gap:4px}.row{height:100%;display:flex;gap:8px}.avatar{position:relative;display:flex;width:40px;flex-shrink:0;flex-direction:column;align-items:center;align-self:stretch}.avatar a,.avatar img{display:block;width:40px;height:40px}.text{flex:1;min-width:0}a{color:#222;text-decoration:none}.head{display:flex;gap:10px;height:20px}p{white-space:pre-wrap;margin:8px 0;min-height:24px}.line{margin-top:4px;width:2px;flex:1;margin-bottom:-12px;background:rgba(0,0,0,.15);border-radius:100px 100px 0 0}.actions{height:20px}</style>`;
 const item=(handle,id,text,height,parent)=>`<li><div class="wrap"><article style="height:${height}px"><div class="row"><div class="avatar"><a href="https://x.com/${handle}"><img alt="@${handle}" src="https://pbs.twimg.com/profile_images/owned/${handle}.png"></a>${parent?'<span class="line w-0.5 flex-1 rounded-t-full"></span>':''}</div><div class="text"><div class="head"><a href="https://x.com/${handle}">Owned ${parent?'parent':'reply'}</a><a href="https://x.com/${handle}">@${handle}</a>${parent?`<a href="https://x.com/${handle}/status/${id}">10月31日</a>`:''}</div><p data-testid="tweetText">${text}</p>${parent?'':`<a href="https://x.com/${handle}/status/${id}"><time datetime="2026-10-31T12:01:00+08:00">12:01 · 2026年10月31日</time></a>`}<div class="actions"><button>1</button><button>2</button></div></div></div></article></div></li>`;
 return css+header+'<ul>'+item('parent_fixture',parentId,'Owned parent source20:00',parentHeight,true)+item('example-channel-b',replyId,'Owned reply source21:00',replyHeight,false)+'</ul>'+
  (cover?'<div style="position:fixed;inset:0;background:white;z-index:999;pointer-events:none">Owned pointerless thread cover</div>':'');
}

const passed=[],summaries=[];let unexpectedRequests=0,ownedRequests=0;
for(const option of [{viewport:{width:1280,height:1000}},{screen:{width:1280,height:1000}},{deviceScaleFactor:2}])
 await assert.rejects(()=>createPublicXSession({headless:true,...option}),/PUBLIC_X_BROWSER_OPTION_INVALID/);
const factorySession=await createPublicXSession({headless:true});
try{
 await factorySession.context.route('**/*',route=>{unexpectedRequests++;return route.abort();});
 const page=await factorySession.context.newPage();
 assert.deepEqual(page.viewportSize(),{width:1280,height:1600});
 assert.deepEqual(await page.evaluate(()=>({width:innerWidth,height:innerHeight})),{width:1280,height:1600});
 assert.equal(factorySession.authentication_mode,'public_anonymous_ephemeral');assert.equal(factorySession.persistent_profile_used,false);
 passed.push('anonymous-factory-fixed-viewport-and-no-arbitrary-overrides');
 summaries.push({name:passed.at(-1),result:'fixed',viewport:{width:1280,height:1600}});
}finally{await factorySession.close();}
const browser=await chromium.launch({headless:true});
try{
 async function scenario(name,{viewport={width:1280,height:1600},source=singleHTML(),thread=false,parentEdited=false,initial=true,onArticleScreenshot,onPhotoScreenshot,onPageScreenshot}={},work){
  const context=await browser.newContext({viewport,locale:'zh-CN',timezoneId:'Asia/Shanghai'}),page=await context.newPage();
  page.setDefaultTimeout(1000);const dir=path.join(output,name);await fs.mkdir(dir,{recursive:true});
  let parentNavigations=0,replyNavigations=0,articleScreenshots=0,photoScreenshots=0;
  await context.route('**/*',async route=>{
   const u=new URL(route.request().url());
   if(u.hostname==='pbs.twimg.com'&&u.pathname.startsWith('/profile_images/')){ownedRequests++;return route.fulfill({contentType:'image/png',body:avatar});}
   if(u.hostname==='pbs.twimg.com'&&u.pathname.startsWith('/media/OwnedPositioning')){ownedRequests++;return route.fulfill({contentType:'image/webp',body:full});}
   if(u.hostname==='x.com'){
    ownedRequests++;
    if(thread&&u.pathname===new URL(parentURL).pathname){parentNavigations++;return route.fulfill({contentType:'text/html',body:singleHTML({owner:{handle:'parent_fixture',postId:parentId},name:'Owned parent',text:parentEdited?'Changed independent parent':'Owned parent source20:00'})});}
    if(u.pathname===new URL(thread?replyURL:ownURL).pathname){if(thread)replyNavigations++;return route.fulfill({contentType:'text/html',body:source});}
   }
   unexpectedRequests++;return route.abort();
  });
  const prototype=Object.getPrototypeOf(page.locator('article')),realScreenshot=prototype.screenshot;
  prototype.screenshot=async function(options){
   const bytes=await realScreenshot.call(this,options);
   if(options?.path&&path.basename(options.path)==='article.png'){
    articleScreenshots++;if(onArticleScreenshot)await onArticleScreenshot(page,articleScreenshots,options);
   }
   if(options?.path&&/^photo-\d+-visible\.png$/.test(path.basename(options.path))){
    photoScreenshots++;if(onPhotoScreenshot)await onPhotoScreenshot(page,photoScreenshots,options);
   }
   return bytes;
  };
  const realPageScreenshot=page.screenshot.bind(page);
  page.screenshot=async options=>{
   const bytes=await realPageScreenshot(options);
   if(onPageScreenshot)await onPageScreenshot(page,options);
   return bytes;
  };
  try{
   if(initial){
    await page.goto(thread?replyURL:ownURL);await page.evaluate(async()=>await Promise.all([...document.images].map(i=>i.decode().catch(()=>{}))));
    if(!thread)await page.evaluate(()=>{const a=document.querySelector('article');scrollTo(0,a.getBoundingClientRect().top+scrollY-10);});
    await page.waitForTimeout(60);
   }
   const detail=await work({page,dir,context,parentNavigations:()=>parentNavigations,replyNavigations:()=>replyNavigations,articleScreenshots:()=>articleScreenshots,photoScreenshots:()=>photoScreenshots});
   passed.push(name);summaries.push({name,...detail});
  }finally{prototype.screenshot=realScreenshot;await context.close();}
 }
 const reject=async(operation,{allowMissingSourceTimeout=false}={})=>{
  let code;await assert.rejects(operation,e=>{
   if(typeof e.code==='string'){code=e.code;return true;}
   // When the actual permalink disappears after a screenshot, the original
   // source-bound locator can time out before another semantic read. Retain
   // that true rejection, rather than inventing a structured source error.
   if(allowMissingSourceTimeout&&e.name==='TimeoutError'&&/locator\.(?:scrollIntoViewIfNeeded|evaluate)/.test(e.message)){
    code=e.name;return true;
   }
   return false;
  });return code;
 };
 const safeRect=page=>page.locator('article').evaluate(e=>{const r=e.getBoundingClientRect();return {top:r.top,bottom:r.bottom,height:r.height,viewport:innerHeight,scroll:scrollY};});
 const fullCapture=(page,dir)=>captureTweet(page,identity,dir,{machineSource:true,trigger:'owned_source_positioning_test'});
 async function verifyCapture(record){
  assert.equal(record.payload.capture.human_review,false);assert.equal(record.payload.capture.source_screenshot_reviewed,false);
  assert.equal(record.payload.capture.visibility_passed,true);assert.equal(hash(await fs.readFile(record.screenshot_file)),record.payload.capture.screenshot_sha256);
  const meta=await sharp(await fs.readFile(record.screenshot_file)).metadata();assert.ok(meta.width>0&&meta.height>0);
 }

 await scenario('unpositioned-original-strict-guard-still-rejects-sticky',{},async({page,dir})=>{
  const before=await safeRect(page);assert.equal(before.top,10);assert.equal(before.height,990);
  const article=await locateTweetArticle(page,identity),code=await reject(()=>assertSourceVisible(page,article,dir));
  assert.equal(code,'WEB_SOURCE_OBSCURED');return {result:'rejected',code,before,after:await safeRect(page)};
 });
 await scenario('independent-capture-centers-then-original-strict-passes',{},async({page,dir})=>{
  const record=await fullCapture(page,dir);await verifyCapture(record);const rect=await safeRect(page);
  assert.ok(rect.top>=53&&rect.bottom<=1600);assert.equal(record.payload.post.text,bodyText);
  return {result:'captured',rect};
 });
 for(const [name,options]of [
  ['990px-article-cannot-fit-1000px-minus-header',{viewport:{width:1280,height:1000}}],
  ['article-exceeds-1600px-safe-band',{source:singleHTML({height:1590})}],
  ['document-top-scroll-clamp-stays-obscured',{source:singleHTML({gap:10})}],
  ['full-painted-cover-stays-rejected',{source:singleHTML({cover:'full'})}],
  ['absolute-painted-cover-stays-rejected',{source:singleHTML({cover:'absolute'})}],
  ['pointerless-painted-cover-stays-rejected',{source:singleHTML({cover:'pointerless'})}],
 ])await scenario(name,options,async({page,dir})=>({result:'rejected',code:await reject(()=>fullCapture(page,dir))}));

 for(const kind of ['author','id','text','time'])await scenario(`source-${kind}-changes-during-native-positioning`,{},async({page,dir})=>{
  await page.evaluate(kind=>{window.ownedScrollMutationCount=0;addEventListener('scroll',()=>{
   window.ownedScrollMutationCount++;
   if(kind==='author')document.querySelector('#name').textContent='Changed source author';
   else if(kind==='id')document.querySelector('#time').href='https://x.com/example_handle_a/status/999999999';
   else if(kind==='text')document.querySelector('#body').textContent='Changed source text';
   else document.querySelector('#time time').dateTime='2026-10-31T12:02:00+08:00';
  },{once:true});},kind);
  const code=await reject(()=>fullCapture(page,dir));assert.equal(await page.evaluate(()=>window.ownedScrollMutationCount),1);
  return {result:'rejected',code};
 });
 for(const kind of ['author','id','text','time'])await scenario(`source-${kind}-changes-during-real-article-screenshot`,{
  onArticleScreenshot:async(page,n)=>{if(n===1)await page.evaluate(kind=>{
   if(kind==='author')document.querySelector('#name').textContent='Changed source author';
   else if(kind==='id')document.querySelector('#time').href='https://x.com/example_handle_a/status/999999999';
   else if(kind==='text')document.querySelector('#body').textContent='Changed source text';
   else document.querySelector('#time time').dateTime='2026-10-31T12:02:00+08:00';
  },kind);}},async({page,dir,articleScreenshots})=>{
   const code=await reject(()=>fullCapture(page,dir),{allowMissingSourceTimeout:kind==='id'});assert.equal(articleScreenshots(),1);return {result:'rejected',code};
  });

 await scenario('public-media-native-image-scroll-requires-source-recoordination',{source:singleHTML({media:true}),
  onPhotoScreenshot:async page=>page.evaluate(()=>{document.querySelector('#photo img').scrollIntoView({block:'center',behavior:'instant'});window.ownedMediaScroll={top:document.querySelector('article').getBoundingClientRect().top,scroll:scrollY};})
 },async({page,dir,photoScreenshots})=>{
  const requested=[];const bundle=await capturePublicMachineMedia(page,identity,path.join(dir,'public-media'),{allowedRoot:output,normalizeLinks:true,
   downloader:async url=>{requested.push(url);return {bytes:full,status:200,content_type:'image/webp'};}});
  assert.equal(photoScreenshots(),1);assert.deepEqual(requested,[mediaURL]);
  assert.ok((await page.evaluate(()=>window.ownedMediaScroll)).top<53);
  assert.equal((await verifyMachineMediaBundle(bundle,{requireLive:false})).integrity_verified,true);
  assert.equal(bundle.files[0].width,160);assert.equal(bundle.files[0].height,120);assert.equal(bundle.human_review,false);
  const rect=await safeRect(page);assert.ok(rect.top>=53&&rect.bottom<=1600);
  return {result:'captured',rect,media_scroll:await page.evaluate(()=>window.ownedMediaScroll)};
 });
 for(const [name,source,bytes,expected]of [
  ['positioning-cannot-authorize-wrong-public-pixels',singleHTML({media:true}),small,'PUBLIC_MEDIA_DECLARED_DIMENSIONS_MISMATCH'],
  // With no srcset, the transformed URL is also currentSrc and is rejected
  // first by preview validation, before the separate raw-src-only guard.
  ['positioning-cannot-authorize-transformed-public-url',singleHTML({media:true,crop:true}),full,'MEDIA_PREVIEW_SOURCE_INVALID'],
 ])await scenario(name,{source},async({page,dir})=>{
  const code=await reject(()=>capturePublicMachineMedia(page,identity,path.join(dir,'public-media'),{allowedRoot:output,normalizeLinks:true,
   downloader:async()=>({bytes,status:200,content_type:'image/webp'})}));assert.equal(code,expected);return {result:'rejected',code};
 });

 await scenario('reply-joint-position-plus-real-independent-parent-reread',{thread:true,source:threadHTML(),initial:false},async({page,dir,parentNavigations})=>{
  const result=await captureReplyMachineV2(page,replyIdentity,dir),packet=result.packet;
  assert.equal(parentNavigations(),1);assert.equal(packet.human_review,false);assert.equal(result.automatic_publication_eligible,false);
  assert.equal(packet.thread.topology.viewport.height,1600);
  const union={top:packet.thread.topology.parent_item.rect.y,bottom:packet.thread.topology.reply_item.rect.y+packet.thread.topology.reply_item.rect.height};
  assert.ok(union.top>=53&&union.bottom<=1600);
  const day=JSON.parse(await fs.readFile(packet.parent_in_thread_day.file,'utf8')),fullParent=JSON.parse(await fs.readFile(packet.parent_reread.file,'utf8'));
  assert.equal(day.payload.post.timestamp_precision,'day');assert.equal('created_at' in day.payload.post,false);
  assert.equal(fullParent.payload.post.created_at,'2026-10-31T12:00:00+08:00');
  assert.equal(fullParent.role,'parent_independent_reread');assert.notEqual(day.navigation_id,fullParent.navigation_id);
  assert.equal(day.payload.post.text,fullParent.payload.post.text);
  assert.equal(hash(await fs.readFile(packet.thread.screenshot.file)),packet.thread.screenshot.sha256);
  return {result:'captured',union,parent_navigations:parentNavigations()};
 });
 for(const [name,source,parentEdited]of [
  ['reply-each-node-fits-but-joint-range-too-tall',threadHTML({parentHeight:800,replyHeight:800}),false],
  ['reply-pointerless-cover-stays-rejected',threadHTML({cover:true}),false],
  ['reply-independent-parent-reread-changed',threadHTML(),true],
 ])await scenario(name,{thread:true,source,parentEdited,initial:false},async({page,dir,parentNavigations})=>{
  const code=await reject(()=>captureReplyMachineV2(page,replyIdentity,dir));
  if(parentEdited){assert.equal(parentNavigations(),1);assert.equal(code,'WEB_REPLY_V2_PARENT_REREAD_CHANGED');}
  else assert.equal(parentNavigations(),0);
  return {result:'rejected',code,parent_navigations:parentNavigations()};
 });
 await scenario('complete-extended-reply-orchestration-keeps-joint-capture-and-parent-reread',{
  thread:true,source:threadHTML(),initial:true,
 },async({page,dir,parentNavigations,replyNavigations})=>{
  const record=await captureExtendedTweet(page,replyIdentity,dir,{trigger:'durable_scheduler'}),ext=record.machine_extended_source;
  assert.deepEqual(ext.nodes.map(n=>n.role),['post','parent']);assert.equal(parentNavigations(),1);assert.equal(replyNavigations(),3);
  assert.equal(ext.human_review,false);assert.equal(ext.automatic_publication_eligible,false);assert.equal(record.human_review,false);
  assert.equal(ext.nodes[0].raw_source.post.text,'Owned reply source21:00');assert.equal(ext.nodes[1].raw_source.post.text,'Owned parent source20:00');
  assert.deepEqual(record.payload.reply_context.parent_source,ext.nodes[1].source);
  assert.equal(ext.nodes[1].raw_observation.file,ext.reply_packet.parent_reread.file);
  assert.equal(hash(await fs.readFile(ext.nodes[0].screenshot.file)),ext.nodes[0].screenshot.sha256);
  return {result:'captured',parent_navigations:parentNavigations(),reply_navigations:replyNavigations()};
 });
 await scenario('extended-final-reused-source-screenshot-cannot-hide-body-change',{
  thread:true,source:threadHTML(),initial:true,
  onPageScreenshot:async(page,options)=>{
   const normalized=String(options?.path||'').replaceAll('\\','/');
   if(!normalized.endsWith('/post-node/reused-source-after/source-visible.png'))return;
   await page.evaluate(id=>{
    const reply=[...document.querySelectorAll('article')].find(a=>a.querySelector(`a[href="https://x.com/example-channel-b/status/${id}"]`));
    if(!reply)throw new Error('OWNED_REPLY_MISSING');
    reply.querySelector('[data-testid="tweetText"]').textContent+=' Owned after-screenshot edit.';
    window.ownedExtendedBodyMutation=true;
   },replyId);
  },
 },async({page,dir,parentNavigations,replyNavigations})=>{
  const code=await reject(()=>captureExtendedTweet(page,replyIdentity,dir,{trigger:'durable_scheduler'}));
  assert.equal(await page.evaluate(()=>window.ownedExtendedBodyMutation),true);
  assert.ok(['EXTENDED_CURRENT_SOURCE_CHANGED','WEB_SOURCE_CHANGED_DURING_CAPTURE'].includes(code));
  assert.equal(parentNavigations(),1);assert.equal(replyNavigations(),3);
  return {result:'rejected',code,parent_navigations:parentNavigations(),reply_navigations:replyNavigations()};
 });
 assert.equal(unexpectedRequests,0);
 console.log(JSON.stringify({passed,summaries,external_requests:unexpectedRequests,owned_fulfilled_requests:ownedRequests,model_calls:0,publish_requests:0}));
}finally{await browser.close();}
