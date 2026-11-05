// Ordinary first-party QR login. The account owner completes login on their phone.
// No cookies are imported from the operator's computer; no CAPTCHA is solved here.
import fs from 'node:fs/promises';
import path from 'node:path';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';

process.umask(0o077);
const root=process.env.HUB_ROOT||'/opt/lighthouse-hub';
const target=process.env.BILIBILI_EXPECTED_SITE_UID;
if(!/^\d{1,30}$/.test(target||''))throw new Error('EXPECTED_SITE_UID_REQUIRED');
const profile=path.join(root,'data/browser-profiles/bilibili');
const output=path.join(root,'media/server-browser-login-v1');
await fs.mkdir(profile,{recursive:true,mode:0o700});
await fs.mkdir(output,{recursive:true,mode:0o700});
const statusFile=path.join(output,'status.json');
async function status(state,extra={}){
 const value={version:'server-browser-login-v1',state,expected_site_uid:target,
  updated_at:new Date().toISOString(),local_cookies_imported:false,...extra};
 const temporary=statusFile+'.partial';await fs.writeFile(temporary,JSON.stringify(value,null,2),{mode:0o600});
 await fs.rename(temporary,statusFile);
}
let context;
process.on('SIGTERM',()=>{context?.close().finally(()=>process.exit(0));});
try{
 context=await chromium.launchPersistentContext(profile,{headless:true,chromiumSandbox:true,
  viewport:{width:1280,height:900},deviceScaleFactor:4,locale:'zh-CN',timezoneId:'Asia/Shanghai'});
 context.setDefaultTimeout(15000);
 context.setDefaultNavigationTimeout(45000);
 const page=context.pages()[0]||await context.newPage();
 const failedResponses=[];
 page.on('response',r=>{if(r.status()>=400){const u=new URL(r.url());failedResponses.push({origin:u.origin,path:u.pathname,status:r.status()});}});
 await page.goto('https://passport.bilibili.com/login',{waitUntil:'domcontentloaded',timeout:45000});
 await page.getByText('扫码登录',{exact:true}).waitFor({state:'visible',timeout:20000});
 await page.locator('img[alt="Scan me!"]').waitFor({state:'visible',timeout:20000});
 await page.locator('img[alt="Scan me!"]').screenshot({path:path.join(output,'qr.png'),scale:'device'});
 await page.screenshot({path:path.join(output,'login.png'),fullPage:false,scale:'css'});
 await status('awaiting_owner_qr_login',{screenshot:'qr.png'});
 const deadline=Date.now()+15*60*1000;
 while(Date.now()<deadline){
  const request=path.join(output,'refresh.request.json');
  const refresh=await fs.readFile(request,'utf8').then(JSON.parse).catch(()=>null);
  if(refresh?.action==='refresh_login_qr'){
   await page.reload({waitUntil:'domcontentloaded',timeout:45000});
   await page.locator('img[alt="Scan me!"]').waitFor({state:'visible',timeout:20000});
   await fs.rename(request,request+'.processed.'+Date.now());
   await status('awaiting_owner_qr_login',{screenshot:'qr.png'});
  }
  if(new URL(page.url()).hostname!=='passport.bilibili.com'||await page.getByText('登录成功',{exact:false}).count()){
   await page.goto('https://t.bilibili.com/',{waitUntil:'domcontentloaded',timeout:45000});
   const avatar=page.locator('a.header-entry-mini');
   await avatar.waitFor({state:'visible',timeout:20000});
   const href=await avatar.getAttribute('href');
   const actual=/^https:\/\/space\.bilibili\.com\/(\d+)\/?$/.exec(new URL(href,'https://t.bilibili.com').href)?.[1];
   if(actual!==target){await status('wrong_account',{actual_site_uid:actual||null});break;}
   await page.screenshot({path:path.join(output,'authenticated.png'),fullPage:false,scale:'css'});
   await status('authenticated',{actual_site_uid:actual,screenshot:'authenticated.png'});
   break;
  }
  const text=await page.locator('body').innerText();
  if(await page.locator('img[alt="Scan me!"]').isVisible())
   await page.locator('img[alt="Scan me!"]').screenshot({path:path.join(output,'qr.png'),scale:'device',timeout:5000}).catch(()=>{});
  await page.screenshot({path:path.join(output,'login.png'),fullPage:false,scale:'css',timeout:5000}).catch(()=>{});
  const observations=await page.locator('img').evaluateAll(es=>es.map(e=>({className:e.className,alt:e.alt,
   width:e.naturalWidth,height:e.naturalHeight,complete:e.complete,source_kind:e.src.startsWith('data:')?'inline_image':'remote_image'})));
  await fs.writeFile(path.join(output,'page-observation.json'),JSON.stringify({observed_at:new Date().toISOString(),
   images:observations,canvas_count:await page.locator('canvas').count(),text,failed_responses:failedResponses.slice(-15)},null,2),{mode:0o600});
  if(/安全验证|人机验证|请完成验证/.test(text)){
   await page.screenshot({path:path.join(output,'challenge.png'),fullPage:false});
   await status('human_verification_required',{screenshot:'challenge.png'});
  }
  // A normal UI poll; does not call private login endpoints or solve challenges.
  await page.waitForTimeout(3000);
 }
 const last=JSON.parse(await fs.readFile(statusFile,'utf8'));
 if(last.state==='awaiting_owner_qr_login')await status('login_window_expired');
}catch(e){
 await status('login_runtime_error',{error_code:e.name==='TimeoutError'?'LOGIN_PAGE_TIMEOUT':'LOGIN_BROWSER_ERROR'});
}finally{await context?.close();}
