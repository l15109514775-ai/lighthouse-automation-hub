// First-party browser UI transport. No private API, stealth, proxy or CAPTCHA solver.
import fs from 'node:fs/promises';
import {assertPublicationWindow} from './publication_window.mjs';
import {classifyPublisherError} from './browser_publish_errors.mjs';
import {insertCaptionText} from './browser_caption_input.mjs';
import path from 'node:path';
import crypto from 'node:crypto';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
process.umask(0o077);
const root=process.env.HUB_ROOT||'/opt/lighthouse-hub';
const raw=await new Promise((resolve,reject)=>{let s='';process.stdin.setEncoding('utf8');
 process.stdin.on('data',b=>{s+=b;if(s.length>30000)reject(new Error('SPEC_TOO_LARGE'));});process.stdin.on('end',()=>resolve(s));});
const spec=JSON.parse(raw);
const hash=b=>crypto.createHash('sha256').update(b).digest('hex');
if(spec.version!=='bilibili-ui-transport-v1'||!/^[-a-f0-9]{36}$/.test(spec.job_id)||
 !/^[-a-f0-9]{36}$/.test(spec.attempt_id)||!/^\d{1,30}$/.test(spec.target_site_uid)||
 spec.target_site_uid!==process.env.BILIBILI_EXPECTED_SITE_UID||typeof spec.caption!=='string'||spec.caption.length>10000||
 !/^https:\/\/x\.com\/(?:example_handle_a|example_handle_b)\/status\/\d+$/.test(spec.source_url)||
 !spec.caption.includes(spec.source_url)||!['publish','lookup','upload_probe'].includes(spec.mode))throw new Error('SPEC_INVALID');
const jobRoot=await fs.realpath(path.join(root,'media',spec.job_id));
const attempt=path.join(jobRoot,'website-attempts',spec.attempt_id);
await fs.mkdir(attempt,{recursive:true,mode:0o700});
const file=await fs.realpath(spec.file);
if(!file.startsWith(jobRoot+path.sep)||hash(await fs.readFile(file))!==spec.artifact_sha256)throw new Error('ARTIFACT_INVALID');
const artifactStat=await fs.stat(file);
let artifactHeight=null;
try{const fd=await fs.open(file,'r');const head=Buffer.alloc(24);await fd.read(head,0,24,0);await fd.close();
 if(head.slice(1,4).toString()==='PNG')artifactHeight=head.readUInt32BE(20);}catch{}
const MAX_IMAGE_BYTES=Number(process.env.HUB_MAX_DYNAMIC_IMAGE_BYTES||3145728);
const MAX_IMAGE_HEIGHT=Number(process.env.HUB_MAX_DYNAMIC_IMAGE_HEIGHT||4096);
if(artifactStat.size>MAX_IMAGE_BYTES||(artifactHeight&&artifactHeight>MAX_IMAGE_HEIGHT))throw new Error('IMAGE_TOO_LARGE_FOR_UPLOAD');
async function write(name,value){const f=path.join(attempt,name);await fs.writeFile(f+'.partial',JSON.stringify(value,null,2),{mode:0o600});await fs.rename(f+'.partial',f);}
const clean=s=>s.replace(/^\u200B/,'').trimEnd();
 let context,clicked=false,stage='launch';
process.on('SIGTERM',()=>{context?.close().finally(()=>process.exit(0));});
async function findResult(page){
 const matches=page.locator('.dyn-card-opus__summary').filter({hasText:spec.source_url});
 const count=await matches.count();if(count===0)return null;
 if(count!==1)throw new Error('DUPLICATE_RESULT_AMBIGUOUS');
 const summary=matches;
 if(clean(await summary.innerText())!==spec.caption)throw new Error('EXISTING_SOURCE_DIFFERENT_CAPTION');
 const card=summary.locator('xpath=ancestor::div[contains(@class,"bili-dyn-item__body")]/..');
 if(await card.getByText(spec.target_name,{exact:true}).count()!==1)throw new Error('RESULT_AUTHOR_UNVERIFIED');
 const labels=await card.locator('[class]').evaluateAll(es=>[...new Set(es.filter(e=>
  /time/.test(typeof e.className==='string'?e.className:'')&&/^(?:刚刚|\d+分钟前|\d+小时前|\d{4}年|昨天|\d{1,2}月)/.test(e.innerText.trim()))
  .map(e=>e.innerText.trim()))]);
 if(labels.length!==1)throw new Error('RESULT_TIME_LINK_UNVERIFIED');
 const popup=page.waitForEvent('popup',{timeout:15000});
 await card.getByText(labels[0],{exact:true}).click();
 const detail=await popup;await detail.waitForLoadState('domcontentloaded');
 await detail.waitForURL(u=>(u.hostname==='t.bilibili.com'&&/^\/\d+\/?$/.test(u.pathname))||
  (u.hostname==='www.bilibili.com'&&/^\/opus\/\d+\/?$/.test(u.pathname)),{timeout:20000});
 const u=new URL(detail.url());u.search='';u.hash='';
 if(!((u.hostname==='t.bilibili.com'&&/^\/\d+\/?$/.test(u.pathname))||
  (u.hostname==='www.bilibili.com'&&/^\/opus\/\d+\/?$/.test(u.pathname))))throw new Error('RESULT_URL_UNVERIFIED');
 await write('candidate-result.json',{url:u.href,source_url:spec.source_url,attempt_id:spec.attempt_id,
  state:'detail_not_yet_verified',publish_requested:spec.mode==='publish'});
 // DOMContentLoaded precedes the client-rendered post. A zero immediate count
 // used to turn a successful send into an unknown result during reconciliation.
 try{await detail.getByText(spec.source_url,{exact:false}).first().waitFor({state:'visible',timeout:30000});}
 catch{await detail.screenshot({path:path.join(attempt,'candidate-unverified.png'),timeout:5000}).catch(()=>{});
  throw new Error('RESULT_SOURCE_UNVERIFIED');}
 try{await detail.waitForFunction(()=>[...document.images].some(e=>/\.hdslb\.com\//.test(e.currentSrc)&&
  /\/bfs\/(new_dyn|album)\//.test(e.currentSrc)&&e.complete&&e.naturalWidth>200&&e.getBoundingClientRect().width>100),null,{timeout:30000});}
 catch{throw new Error('RESULT_MEDIA_NOT_LOADED');}
 const screenshot=path.join(attempt,'published.png');await detail.screenshot({path:screenshot,fullPage:false,timeout:15000});
 return {version:'operator-browser-publication-observation-v1',observer:'server_browser_ui',
  observed_at:new Date().toISOString(),url:u.href,site_uid:spec.target_site_uid,account_name:spec.target_name,
  caption:spec.caption,observed_caption:clean(await summary.innerText()),artifact_sha256:spec.artifact_sha256,
  screenshot_file:screenshot,screenshot_sha256:hash(await fs.readFile(screenshot)),visible_in_owner_browser:true,
  media_rendered:true,moderation_status:'not_exposed_by_page',attempt_id:spec.attempt_id};
}
try{
 context=await chromium.launchPersistentContext(path.join(root,'data/browser-profiles/bilibili'),{
  headless:true,chromiumSandbox:true,channel:process.env.HUB_BROWSER_CHANNEL||undefined,
  viewport:{width:1280,height:900},locale:'zh-CN',timezoneId:'Asia/Shanghai'});
 context.setDefaultTimeout(20000);context.setDefaultNavigationTimeout(45000);
 const page=context.pages()[0]||await context.newPage();
 const failedRequests=[];
 page.on('response',r=>{if(r.status()>=400){const u=new URL(r.url());failedRequests.push({origin:u.origin,path:u.pathname,status:r.status()});}});
 page.on('requestfailed',r=>{const u=new URL(r.url());failedRequests.push({origin:u.origin,path:u.pathname,failure:r.failure()?.errorText});});
 stage='navigate';
 await page.goto(spec.mode==='lookup'?`https://space.bilibili.com/${spec.target_site_uid}/dynamic`:'https://t.bilibili.com/',{waitUntil:'domcontentloaded'});
 stage='verify_account';
 try{await page.locator('a.header-entry-mini').waitFor({state:'visible'});}
 catch{
  if(page.url().includes('passport.bilibili.com')||await page.getByText('登录',{exact:true}).first().isVisible())throw new Error('AUTH_EXPIRED');
  throw new Error('ACCOUNT_UI_NOT_AVAILABLE');
 }
 const href=await page.locator('a.header-entry-mini').getAttribute('href');
 const actual=/^https:\/\/space\.bilibili\.com\/(\d+)\/?$/.exec(new URL(href,'https://t.bilibili.com').href)?.[1];
 if(actual!==spec.target_site_uid)throw new Error('TARGET_ACCOUNT_MISMATCH');
 await page.locator('.dyn-card-opus__summary').first().waitFor({state:'visible'});
 stage='lookup_existing';const existing=await findResult(page);
 await page.screenshot({path:path.join(attempt,'lookup.png'),fullPage:false});
 await write('lookup.json',{mode:spec.mode,source_url:spec.source_url,visited_url:page.url(),found:!!existing,
  visible_source_matches:await page.locator('.dyn-card-opus__summary').filter({hasText:spec.source_url}).count(),
  actual_site_uid:actual,checked_at:new Date().toISOString()});
 if(existing){await write('result.json',{state:'existing_visible',evidence:existing});}
 else if(spec.mode==='lookup'){await write('result.json',{state:'not_found_result_unknown',sent:false});}
 else{
  stage='inspect_existing_draft';
  const body=page.getByPlaceholder('有什么想和大家分享的？',{exact:true});
  const previousText=clean(await body.innerText());
  const existingImages=page.locator('.bili-pics-uploader__item');
  const imageCount=await existingImages.count();
  if((previousText&&!previousText.includes(spec.source_url))||(imageCount&&!previousText.includes(spec.source_url)))
   throw new Error('UNRELATED_DRAFT_REQUIRES_REVIEW');
  if(imageCount){
   if(imageCount!==1)throw new Error('EXISTING_DRAFT_IMAGES_REQUIRE_REVIEW');
   await page.screenshot({path:path.join(attempt,'owned-stale-draft.png'),fullPage:false});
   await write('owned-stale-draft.json',{source_url:spec.source_url,text:previousText,image_count:imageCount,
    action:'replace_own_unfinished_upload',local_original_preserved:true,artifact_sha256:spec.artifact_sha256});
   await page.locator('.bili-pics-uploader__item__remove').click();
   await existingImages.waitFor({state:'detached'});
  }
  stage='type_caption';
  const title=page.getByPlaceholder('好的标题更容易获得支持，选填20字',{exact:true});
  await title.fill('');
  // Native plaintext paste was checked after blur and image upload on the live
  // editor. Keep all later exact visible-caption checks intact.
  await insertCaptionText(page,body,spec.caption,{clipboardOrigin:'https://t.bilibili.com'});
  stage='choose_image';
  const chooser=page.waitForEvent('filechooser');
  const add=page.locator('.bili-pics-uploader__add');
  if(await add.isVisible())await add.click();
  else await page.locator('.bili-dyn-publishing__tools__item.pic').click();
  await (await chooser).setFiles([file]);
  stage='wait_image_preview';
  await fs.writeFile(path.join(attempt,'uploader-dom.html'),await page.locator('.bili-dyn-publishing__image-upload').evaluate(e=>e.outerHTML),{mode:0o600});
  await page.waitForFunction(()=>{
   const box=document.querySelector('.bili-dyn-publishing__image-upload');
   if(!box||getComputedStyle(box).display==='none')return false;
   return [...box.querySelectorAll('img')].some(e=>e.complete&&e.naturalWidth>0)||
    [...box.querySelectorAll('[style]')].some(e=>getComputedStyle(e).backgroundImage.startsWith('url('));
  },null,{timeout:30000});
  // A thumbnail is shown before the remote upload finishes. Wait for the real
  // loading state to end; never accept the site's "publish without image" prompt.
  stage='wait_upload_completion';
  {
   const deadline=Date.now()+120000;let uploadState='loading';
   while(Date.now()<deadline){
    uploadState=await page.evaluate(()=>{
     const box=document.querySelector('.bili-dyn-publishing__image-upload');
     if(!box||getComputedStyle(box).display==='none')return 'hidden';
     const items=[...document.querySelectorAll('.bili-pics-uploader__item')];
     if(items.some(e=>e.classList.contains('error')))return 'error';
     if(items.length===1&&!items[0].classList.contains('loading'))return 'ready';
     return 'loading';
    });
    if(uploadState==='ready'||uploadState==='error')break;
    await page.waitForTimeout(400);
   }
   if(uploadState==='error'){
    await page.locator('.bili-pics-uploader-item-error__retry').click().catch(()=>{});
    await page.waitForTimeout(20000);
    uploadState=await page.evaluate(()=>{
     const items=[...document.querySelectorAll('.bili-pics-uploader__item')];
     return items.some(e=>e.classList.contains('error'))?'error':'ready';
    });
   }
   if(uploadState!=='ready'){
    await write('upload-network-failures.json',{failures:failedRequests});
    await fs.writeFile(path.join(attempt,'uploader-final-dom.html'),await page.locator('.bili-dyn-publishing__image-upload').evaluate(e=>e.outerHTML),{mode:0o600});
    throw new Error('IMAGE_UPLOAD_NOT_COMPLETED');
   }
  }
  stage='verify_draft';
  if(clean(await body.innerText())!==spec.caption)throw new Error('DRAFT_CAPTION_CHANGED');
  await page.screenshot({path:path.join(attempt,'prepared.png'),fullPage:false});
  await write('ready.json',{state:'prepared',site_uid:actual,artifact_sha256:spec.artifact_sha256,
   caption_sha256:hash(spec.caption),attempt_id:spec.attempt_id});
  if(spec.mode==='upload_probe'){
   await write('result.json',{state:'upload_verified_not_published',sent:false,artifact_sha256:spec.artifact_sha256});
   await context.close();context=null;
   process.exit(0);
  }
  const deadline=Date.now()+180000;let commit;
  while(Date.now()<deadline){
   commit=await fs.readFile(path.join(attempt,'commit.json'),'utf8').then(JSON.parse).catch(()=>null);
   if(commit)break;await page.waitForTimeout(250);
  }
  if(commit?.action!=='publish'||commit?.attempt_id!==spec.attempt_id||commit?.artifact_sha256!==spec.artifact_sha256)
   throw new Error('DISPATCH_COMMIT_REQUIRED');
  if(hash(await fs.readFile(file))!==spec.artifact_sha256||clean(await body.innerText())!==spec.caption)
   throw new Error('ARTIFACT_CHANGED_BEFORE_SEND');
  const send=page.locator('.bili-dyn-publishing__action');
  if(/disabled/.test(await send.getAttribute('class')))throw new Error('PUBLISH_BUTTON_DISABLED');
  assertPublicationWindow(spec);
  stage='click_publish';await send.click();clicked=true;
  // First use/changed terms require real owner confirmation at this moment.
  const termsButton=page.getByRole('button',{name:'确认并发送',exact:true});
  const signalUntil=Date.now()+10000;
  while(Date.now()<signalUntil){
   if(await termsButton.isVisible()||!clean(await body.innerText())||await page.getByText('图片上传中',{exact:true}).isVisible())break;
   await page.waitForTimeout(250);
  }
  if(await page.getByText('图片上传中',{exact:true}).isVisible()){
   await page.screenshot({path:path.join(attempt,'upload-blocking-dialog.png'),fullPage:false});
   await write('submission-blocked.json',{version:'first-party-ui-nonacceptance-v1',
    state:'awaiting_confirmation_to_publish_without_image',confirmation_clicked:false,source_url:spec.source_url,
    attempt_id:spec.attempt_id,observed_text:await page.locator('body').innerText(),observed_at:new Date().toISOString()});
   throw new Error('IMAGE_UPLOAD_BLOCKED_SUBMISSION');
  }
  if(await termsButton.isVisible()){
   stage='await_terms_owner_confirmation';
   let frame;
   const frameUntil=Date.now()+20000;
   while(Date.now()<frameUntil){
    frame=page.frames().find(f=>f.url().includes('/h5/dynamic/specification'));
    if(frame&&await frame.getByText('哔哩哔哩动态使用规范',{exact:false}).first().isVisible())break;
    frame=null;await page.waitForTimeout(250);
   }
   if(!frame)throw new Error('TERMS_TEXT_NOT_LOADED');
   const text=await frame.locator('body').innerText();
   if(text.length<500||!text.includes('动态使用规范'))throw new Error('TERMS_TEXT_NOT_LOADED');
   await fs.writeFile(path.join(attempt,'terms-text.txt'),text,{mode:0o600});
   const termsHash=hash(text);await page.screenshot({path:path.join(attempt,'terms.png'),fullPage:false});
   await write('terms-required.json',{state:'terms_acceptance_required',terms_sha256:termsHash,hash_scope:'loaded_official_terms_text',
    attempt_id:spec.attempt_id,job_id:spec.job_id,target_site_uid:actual});
   const until=Date.now()+15*60*1000;let decision;
   while(Date.now()<until){
    decision=await fs.readFile(path.join(attempt,'terms-accept.json'),'utf8').then(JSON.parse).catch(()=>null);
    if(decision)break;await page.waitForTimeout(500);
   }
   if(decision?.accept!==true||decision?.terms_sha256!==termsHash||decision?.attempt_id!==spec.attempt_id)
    throw new Error('TERMS_OWNER_CONFIRMATION_REQUIRED');
   assertPublicationWindow(spec);
   await termsButton.click();
  }
  stage='verify_visible_result';
  // The main feed announces new posts without refreshing its cards. Read the
  // owner's freshly loaded public timeline; this never clicks send again.
  await page.goto(`https://space.bilibili.com/${spec.target_site_uid}/dynamic`,{waitUntil:'domcontentloaded'});
  await page.locator('.dyn-card-opus__summary').first().waitFor({state:'visible'});
  const until=Date.now()+60000;let evidence;
  while(Date.now()<until){evidence=await findResult(page);if(evidence)break;await page.waitForTimeout(1000);}
  if(!evidence)throw new Error('PUBLISH_RESULT_UNKNOWN');
  await write('result.json',{state:'published_visible',evidence});
 }
}catch(e){
 const code=classifyPublisherError(e,stage);
 const captionInputStage=code==='DRAFT_CAPTION_CHANGED'&&
  ['focus','select','clear','paste_focus','paste'].includes(e?.captionInputStage)?e.captionInputStage:null;
 const page=context?.pages()[0];
 if(page){
  const body=page.getByPlaceholder('有什么想和大家分享的？',{exact:true});
  if(await body.count()===1)await write('draft-observed.json',{expected_caption:spec.caption,
   caption_input_stage:captionInputStage,
   observed_text:await body.innerText({timeout:3000}).catch(()=>null),
   observed_html:await body.evaluate(e=>e.innerHTML).catch(()=>null)});
  if(code==='IMAGE_UPLOAD_NOT_COMPLETED'&&await body.count()===1){
   const observed=clean(await body.innerText({timeout:3000}).catch(()=>null)||'');
   if(observed===spec.caption){
    await body.click().catch(()=>{});
    await page.keyboard.press('Control+A').catch(()=>{});
    await page.keyboard.press('Backspace').catch(()=>{});
    await page.waitForTimeout(600);
    const remaining=await page.locator('.bili-pics-uploader__item.error').count();
    for(let i=0;i<remaining;i+=1)await page.locator('.bili-pics-uploader__item.error .bili-pics-uploader__item__remove').first().click().catch(()=>{});
    const cleared=clean(await body.innerText({timeout:3000}).catch(()=>'')||'');
    await write('own-draft-cleared.json',{cleared:cleared==='',text_sha256:hash(Buffer.from(observed)),remaining_error_images:remaining});
   }
  }
  await page.screenshot({path:path.join(attempt,'error.png'),timeout:5000}).catch(()=>{});
 }
 await write('result.json',{state:clicked?'publish_unknown':'blocked_before_send',error_code:code,
  stage,caption_input_stage:captionInputStage,publish_control_clicked:clicked,sent:clicked?null:false});
}finally{await context?.close();}
