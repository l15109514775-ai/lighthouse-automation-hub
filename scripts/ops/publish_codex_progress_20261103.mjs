// One explicitly owner-requested status announcement. Not a tweet policy route.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {chromium} from '/opt/lighthouse-hub/compositor/node_modules/playwright/index.mjs';
import {insertCaptionText} from '/opt/lighthouse-hub/adapters/browser_caption_input.mjs';
process.umask(0o077);
let raw='';for await(const b of process.stdin)raw+=b;const s=JSON.parse(raw);
const hash=x=>crypto.createHash('sha256').update(x).digest('hex');
const clean=x=>x.replace(/^\u200B/,'').trimEnd();
if(s.version!=='owner-requested-codex-status-v1'||s.uid!=='1000000000000000'||
 !['publish','lookup'].includes(s.mode)||s.caption.length>3000||hash(s.caption)!==s.caption_sha256||
 !s.caption.startsWith(s.heading)||!s.caption.endsWith('——Codex（AI助手，协助开发与部署）')||
 !/^\/opt\/lighthouse-hub\/media\/[a-f0-9-]{36}\/operator-announcement\/[a-f0-9-]{36}$/.test(s.folder))throw new Error('SPEC_INVALID');
await fs.mkdir(s.folder,{recursive:true,mode:0o700});let c,clicked=false,stage='launch';
const write=async(name,v)=>{const f=path.join(s.folder,name);await fs.writeFile(f+'.partial',JSON.stringify(v,null,2));await fs.rename(f+'.partial',f);};
async function verifyAccount(p){
 const a=p.locator('a.header-entry-mini');await a.waitFor({state:'visible'});
 const href=new URL(await a.getAttribute('href'),'https://t.bilibili.com').href;
 if(!new RegExp('^https://space\\.bilibili\\.com/'+s.uid+'/?$').test(href))throw new Error('TARGET_ACCOUNT_MISMATCH');
}
async function find(p){
 await p.locator('.dyn-card-opus__summary').first().waitFor({state:'visible'});
 const summary=p.locator('.bili-dyn-item__body').filter({hasText:s.heading});const n=await summary.count();
 if(n===0)return null;if(n!==1)throw new Error('DUPLICATE_RESULT_AMBIGUOUS');
 const card=summary.locator('..');
 if(await card.getByText(s.name,{exact:true}).count()!==1)throw new Error('RESULT_AUTHOR_UNVERIFIED');
 const expand=card.getByText(/^(展开|展开全文)$/);if(await expand.count()===1&&await expand.isVisible())await expand.click();
 const observed=clean(await summary.innerText());
 if(!observed.includes(s.caption))throw new Error('RESULT_FULL_TEXT_UNVERIFIED');
 const labels=await card.locator('[class]').evaluateAll(es=>[...new Set(es.filter(e=>
  /time/.test(typeof e.className==='string'?e.className:'')&&/^(?:刚刚|\d+分钟前|\d+小时前|\d{4}年|昨天|\d{1,2}月)/.test(e.innerText.trim())).map(e=>e.innerText.trim()))]);
 if(labels.length!==1)throw new Error('RESULT_LINK_UNVERIFIED');
 const popup=p.waitForEvent('popup',{timeout:15000});await card.getByText(labels[0],{exact:true}).click();
 const detail=await popup;await detail.waitForLoadState('domcontentloaded');
 await detail.waitForURL(u=>(u.hostname==='t.bilibili.com'&&/^\/\d+\/?$/.test(u.pathname))||
  (u.hostname==='www.bilibili.com'&&/^\/opus\/\d+\/?$/.test(u.pathname)),{timeout:20000});
 await detail.getByText(s.heading,{exact:false}).first().waitFor({state:'visible',timeout:30000});
 const text=await detail.locator('body').innerText();if(!text.includes(s.caption))throw new Error('DETAIL_FULL_TEXT_UNVERIFIED');
 const url=new URL(detail.url());url.search='';url.hash='';
 await detail.screenshot({path:path.join(s.folder,'published.png'),fullPage:false});
 return {url:url.href,site_uid:s.uid,caption_sha256:s.caption_sha256,full_approved_text_observed:true,
  observed_at:new Date().toISOString(),screenshot:path.join(s.folder,'published.png')};
}
try{
 c=await chromium.launchPersistentContext('/opt/lighthouse-hub/data/browser-profiles/bilibili',{
  headless:true,chromiumSandbox:true,channel:'chromium',viewport:{width:1280,height:1000},locale:'zh-CN',timezoneId:'Asia/Shanghai'});
 c.setDefaultTimeout(20000);c.setDefaultNavigationTimeout(45000);const p=c.pages()[0]||await c.newPage();
 stage='lookup_existing';await p.goto('https://space.bilibili.com/'+s.uid+'/dynamic',{waitUntil:'domcontentloaded'});await verifyAccount(p);
 let visible=await find(p);
 if(visible)await write('result.json',{state:'existing_visible',sent:false,evidence:visible});
 else if(s.mode==='lookup')await write('result.json',{state:'publish_unknown',sent:null,error_code:'NOT_FOUND_DO_NOT_RESEND'});
 else{
  stage='prepare';await p.goto('https://t.bilibili.com/',{waitUntil:'domcontentloaded'});await verifyAccount(p);
  const body=p.getByPlaceholder('有什么想和大家分享的？',{exact:true});await body.waitFor({state:'visible'});
  const before=clean(await body.innerText());
  if((before&&before!==s.caption)||await p.locator('.bili-pics-uploader__item').count())throw new Error('UNRELATED_DRAFT_REQUIRES_REVIEW');
  await p.getByPlaceholder('好的标题更容易获得支持，选填20字',{exact:true}).fill('');
  await insertCaptionText(p,body,s.caption,{clipboardOrigin:'https://t.bilibili.com'});
  await p.getByPlaceholder('好的标题更容易获得支持，选填20字',{exact:true}).focus();
  if(clean(await body.innerText())!==s.caption)throw new Error('DRAFT_CAPTION_CHANGED');
  await p.screenshot({path:path.join(s.folder,'prepared.png'),fullPage:false});
  await write('ready.json',{uid:s.uid,caption_sha256:s.caption_sha256,attempt_id:s.attempt_id});
  stage='await_durable_commit';let commit;const until=Date.now()+120000;
  while(Date.now()<until){commit=await fs.readFile(path.join(s.folder,'commit.json'),'utf8').then(JSON.parse).catch(()=>null);if(commit)break;await p.waitForTimeout(200);}
  if(commit?.attempt_id!==s.attempt_id||commit?.caption_sha256!==s.caption_sha256||commit?.action!=='publish')throw new Error('DURABLE_COMMIT_REQUIRED');
  if(clean(await body.innerText())!==s.caption)throw new Error('DRAFT_CAPTION_CHANGED');
  const send=p.locator('.bili-dyn-publishing__action').filter({hasText:/^\s*发布\s*$/});
  if(await send.count()!==1)throw new Error('PUBLISH_CONTROL_UNVERIFIED');
  if(/disabled/.test(await send.getAttribute('class')))throw new Error('PUBLISH_BUTTON_DISABLED');
  stage='send_once';clicked=true;await send.click();
  const terms=p.getByRole('button',{name:'确认并发送',exact:true});await p.waitForTimeout(1500);
  if(await terms.isVisible())throw new Error('TERMS_OWNER_CONFIRMATION_REQUIRED');
  stage='verify';await p.goto('https://space.bilibili.com/'+s.uid+'/dynamic',{waitUntil:'domcontentloaded'});await verifyAccount(p);
  const end=Date.now()+45000;while(Date.now()<end){visible=await find(p);if(visible)break;await p.waitForTimeout(1000);await p.reload({waitUntil:'domcontentloaded'});}
  if(!visible)throw new Error('RESULT_UNKNOWN');await write('result.json',{state:'published_visible',sent:true,evidence:visible});
 }
}catch(e){
 await write('result.json',{state:clicked?'publish_unknown':'blocked_before_send',sent:clicked?null:false,
  error_code:/^[A-Z_]+$/.test(e.message)?e.message:'UI_RUNTIME_ERROR',stage,publish_control_clicked:clicked});
 await c?.pages()[0]?.screenshot({path:path.join(s.folder,'error.png'),fullPage:false}).catch(()=>{});
}finally{await c?.close();}
