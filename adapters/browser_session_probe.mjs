// Tests a server-owned persistent session through the ordinary first-party UI.
import fs from 'node:fs/promises';
import path from 'node:path';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
process.umask(0o077);
const root=process.env.HUB_ROOT||'/opt/lighthouse-hub';
const target=process.env.BILIBILI_EXPECTED_SITE_UID;
if(!/^\d{1,30}$/.test(target||''))throw new Error('EXPECTED_SITE_UID_REQUIRED');
const output=path.join(root,'media/server-browser-login-v1');
const result={version:'server-browser-session-probe-v1',started_at:new Date().toISOString(),expected_site_uid:target,
 browser_process_reopened:true,local_cookies_imported:false};
let context;
try{
 context=await chromium.launchPersistentContext(path.join(root,'data/browser-profiles/bilibili'),{
  headless:true,chromiumSandbox:true,viewport:{width:1280,height:900},locale:'zh-CN',timezoneId:'Asia/Shanghai'});
 context.setDefaultTimeout(20000);context.setDefaultNavigationTimeout(45000);
 const page=context.pages()[0]||await context.newPage();
 const response=await page.goto('https://t.bilibili.com/',{waitUntil:'domcontentloaded'});
 result.http_status=response?.status();
 await page.locator('a.header-entry-mini').waitFor({state:'visible'});
 const href=await page.locator('a.header-entry-mini').getAttribute('href');
 result.actual_site_uid=/^https:\/\/space\.bilibili\.com\/(\d+)\/?$/.exec(new URL(href,'https://t.bilibili.com').href)?.[1]||null;
 result.state=result.actual_site_uid===target?'authenticated':'wrong_account';
 await page.screenshot({path:path.join(output,'session-restored.png'),fullPage:false});
 const own=page.getByText('示例主播A｜原帖中文翻译（非原图）',{exact:false});
 if(await own.count()===1){
  const structure=await own.evaluate(e=>{let a=e;const records=[];for(let i=0;a&&i<9;i++,a=a.parentElement)
   records.push({level:i,tag:a.tagName,className:a.className,
    attributes:[...a.attributes].filter(x=>x.name==='id'||x.name.startsWith('data-')).map(x=>({name:x.name,value:x.value})),
    children:[...a.children].slice(0,5).map(x=>({className:x.className,text:x.innerText.slice(0,100)}))});return records;});
  await fs.writeFile(path.join(output,'published-card-dom.json'),JSON.stringify(structure,null,2),{mode:0o600});
 }
}catch(e){
 result.state='session_probe_failed';result.error_code=e.name==='TimeoutError'?'LOGIN_UI_TIMEOUT':'LOGIN_UI_ERROR';
 const page=context?.pages()[0];
 if(page){
  result.url=page.url();
  await page.screenshot({path:path.join(output,'session-probe-failed.png'),timeout:5000}).catch(()=>{});
  await fs.writeFile(path.join(output,'session-probe-ui.txt'),await page.locator('body').innerText({timeout:5000}).catch(()=>''),{mode:0o600});
 }
}finally{await context?.close();}
result.finished_at=new Date().toISOString();
await fs.writeFile(path.join(output,'session-probe.json'),JSON.stringify(result,null,2),{mode:0o600});
console.log(JSON.stringify(result));
