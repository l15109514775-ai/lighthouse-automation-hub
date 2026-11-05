// Standalone operator IPC; original qualified driver owns prepare/submit.
// The only new browser behavior is bounded, passive manager-page readiness.
import fs from 'node:fs/promises';
import path from 'node:path';
import readline from 'node:readline';
import {pathToFileURL} from 'node:url';

const ORIGIN='https://member.bilibili.com',MANAGER='/platform/upload-manager/article';
function fail(code){throw Object.assign(new Error(code),{code});}
function loadingBodyOnly(p){
 const e=p?.evidence,c=e?.visible_challenge_controls;
 return p?.blocked===true&&p.error_code==='BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED'&&
  e?.version==='bilibili-video-protection-guard-v3'&&e.inspection_failed!==true&&e.truncated===false&&
  e.native_mp4_uploaders===0&&e.top_level_authentication===false&&
  e.known_document_container_excluded===false&&e.native_draft_manager_excluded===false&&
  Array.isArray(e.visible_dialogs)&&e.visible_dialogs.length===0&&Array.isArray(e.challenge_frames)&&e.challenge_frames.length===0&&
  Array.isArray(c)&&c.length===1&&c[0].tag==='BODY'&&c[0].role===''&&Array.isArray(c[0].classes)&&
  [...c[0].classes].sort().join(',')==='risk-captcha-adapt,risk-captcha-adapt-pc';
}
export async function queryManager({page,inspectProtection,accountIdentity,spec,write,chooserCount=()=>0}){
 const exact=label=>page.getByText(label,{exact:true}).filter({visible:true});
 const check=async()=>{
  const u=new URL(page.url());if(u.origin!==ORIGIN||u.pathname!==MANAGER||chooserCount())fail('RECOVERY_MANAGER_ORIGIN_OR_CHOOSER');
  const p=await inspectProtection(page);if(p.blocked)fail(p.error_code||'RECOVERY_MANAGER_PROTECTION');return p;
 };
 await page.goto(ORIGIN+MANAGER,{waitUntil:'domcontentloaded',timeout:45000});
 const end=Date.now()+20000,polls=[];let ready=false;
 do{
  const u=new URL(page.url());if(u.origin!==ORIGIN||u.pathname!==MANAGER||chooserCount())fail('RECOVERY_MANAGER_ORIGIN_OR_CHOOSER');
  const p=await inspectProtection(page);
  if(p.blocked&&!loadingBodyOnly(p))fail(p.error_code||'RECOVERY_MANAGER_PROTECTION');
  const all=await exact('全部稿件 0').count(),draft=await exact('草稿 2').count();
  if(all>1||draft>1)fail('RECOVERY_MANAGER_AMBIGUOUS');
  polls.push({all_zero_tabs:all,draft_two_tabs:draft,blocked:p.blocked,startup_body_only:loadingBodyOnly(p)});
  if(!p.blocked&&all===1&&draft===1){ready=true;break;}
  await page.waitForTimeout(Math.min(200,Math.max(0,end-Date.now())));
 }while(Date.now()<end);
 await write('recovery-manager-readiness.json',{version:'recovery-manager-passive-readiness-v1',ready,polls,maximum_wait_ms:20000});
 if(!ready)fail('RECOVERY_MANAGER_NOT_READY');
 await check();if(await accountIdentity()!==spec.target_site_uid)fail('TARGET_ACCOUNT_IDENTITY_MISMATCH');await check();
 const dom=await page.evaluate(()=>({url:location.origin+location.pathname,title:document.title,
  visible_text:(document.body?.innerText||'').slice(0,30000),links:[...document.querySelectorAll('a[href]')].filter(e=>{
   const r=e.getBoundingClientRect();return r.width>0&&r.height>0;}).map(e=>{
    let url=null;try{const u=new URL(e.getAttribute('href'),document.baseURI);url=u.origin+u.pathname;}catch{}
    return {text:(e.innerText||'').trim().slice(0,200),url};})}));
 const lines=dom.visible_text.split('\n').map(s=>s.trim());
 for(const line of ['视频管理','图文管理','互动视频管理','全部稿件 0','草稿 2','进行中 0','已发布 0','未通过 0','一个稿件都没有，请换个筛选条件'])
  if(!lines.includes(line))fail('RECOVERY_MANAGER_NOT_CONFIRMED_EMPTY');
 if(dom.visible_text.includes(spec.metadata.title)||dom.visible_text.includes(spec.source_url)||/BV[A-Za-z0-9]{10}/.test(dom.visible_text)||
  dom.links.some(l=>/\/video\/BV[A-Za-z0-9]{10}/.test(l.url||'')))fail('RECOVERY_MANAGER_PUBLICATION_CANDIDATE');
 await check();if(await exact('全部稿件 0').count()!==1||await exact('草稿 2').count()!==1)fail('RECOVERY_MANAGER_CHANGED');
 await write('lookup-inventory.json',{observed_at:new Date().toISOString(),visible_dom:dom});return {state:'unknown'};
}

export async function main(){
 process.umask(0o077);
 const root=path.resolve(process.env.HUB_ROOT||'/opt/lighthouse-hub');
 const {chromium}=await import(pathToFileURL(root+'/compositor/node_modules/playwright/index.mjs').href);
 const {createReplayNativeDriver,validateReplayManifest}=await import(pathToFileURL(root+'/adapters/replay_native_driver.mjs').href);
 const {inspectVideoProtection}=await import(pathToFileURL(root+'/adapters/bilibili_video_guard.mjs').href);
 const lines=readline.createInterface({input:process.stdin,crlfDelay:Infinity}),iterator=lines[Symbol.asyncIterator]();
 let context=null,closed=false,choosers=0;
 const reply=value=>process.stdout.write(JSON.stringify(value)+'\n');
 async function close(){if(closed)return;closed=true;await context?.close();lines.close();}
 for(const sig of ['SIGTERM','SIGINT'])process.on(sig,()=>close().finally(()=>process.exit(0)));
 const deadline=setTimeout(()=>close().finally(()=>process.exit(2)),1200000);deadline.unref();
 try{
  const first=await iterator.next();if(first.done||Buffer.byteLength(first.value)>131072)fail('REPLAY_BROWSER_MANIFEST_INVALID');
  const spec=validateReplayManifest(JSON.parse(first.value));
  if(process.getuid()!==10001||process.getgid()!==10001||spec.job_id!=='594aed06-3aa1-45f4-90f0-1db49a602395'||
   spec.attempt_id!=='150c1074-4aed-4f5d-b87a-83cf2840af24'||spec.target_site_uid!==process.env.BILIBILI_EXPECTED_SITE_UID)fail('RECOVERY_OPERATOR_IDENTITY');
  const profile=root+'/data/browser-profiles/bilibili-video';
  for(let p=profile;p!==path.dirname(p);p=path.dirname(p))if((await fs.lstat(p)).isSymbolicLink())fail('RECOVERY_PROFILE_INVALID');
  const st=await fs.stat(profile);if(await fs.realpath(profile)!==profile||!st.isDirectory()||st.uid!==10001||st.mode&0o077)fail('RECOVERY_PROFILE_INVALID');
  context=await chromium.launchPersistentContext(profile,{headless:true,channel:'chromium',chromiumSandbox:true,viewport:{width:1280,height:960},acceptDownloads:false});
  const page=context.pages()[0]||await context.newPage();page.on('filechooser',()=>choosers++);
  const driver=await createReplayNativeDriver({page,context,spec,root});
  async function write(name,value){
   if(path.basename(name)!==name)fail('RECOVERY_EVIDENCE_NAME');
   const file=path.join(driver.output,name),tmp=file+'.partial',h=await fs.open(tmp,'wx',0o600);
   try{await h.writeFile(JSON.stringify(value,null,2)+'\n');await h.sync();}finally{await h.close();}
   await fs.rename(tmp,file);const d=await fs.open(driver.output,'r');try{await d.sync();}finally{await d.close();}
  }
  async function accountIdentity(){
   const identity=await context.newPage();try{
    await identity.goto('https://t.bilibili.com/',{waitUntil:'domcontentloaded',timeout:45000});
    const avatar=identity.locator('a.header-entry-mini:visible');await avatar.waitFor({state:'visible',timeout:30000});
    if(await avatar.count()!==1)fail('REPLAY_BROWSER_UID_AMBIGUOUS');
    const link=new URL(await avatar.getAttribute('href'),'https://t.bilibili.com/');
    const uid=link.origin==='https://space.bilibili.com'&&!link.username&&!link.password&&/^\/(\d+)\/?$/.exec(link.pathname)?.[1];
    if(uid!==spec.target_site_uid)fail('TARGET_ACCOUNT_IDENTITY_MISMATCH');return uid;
   }finally{await identity.close();}
  }
  reply({version:'replay-native-browser-response-v1',state:'ready',job_id:spec.job_id,attempt_id:spec.attempt_id,output:driver.output});
  const seen=new Set();
  while(!closed){
   const next=await iterator.next();if(next.done)break;let request;
   try{
    if(Buffer.byteLength(next.value)>16384)fail('REPLAY_BROWSER_REQUEST_INVALID');request=JSON.parse(next.value);
    if(Object.keys(request).sort().join(',')!=='arguments,operation,request_id,version'||request.version!=='replay-native-browser-request-v1'||
     !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(request.request_id||'')||seen.has(request.request_id)||
     !['prepare','submit','lookup'].includes(request.operation)||!request.arguments||typeof request.arguments!=='object'||Array.isArray(request.arguments))fail('REPLAY_BROWSER_REQUEST_INVALID');
    const keys=Object.keys(request.arguments).sort().join(',');if(request.operation==='submit'?keys!=='reservation':keys!=='')fail('REPLAY_BROWSER_REQUEST_INVALID');
    seen.add(request.request_id);if(choosers)fail('RECOVERY_UNEXPECTED_CHOOSER');
    const result=request.operation==='lookup'?await queryManager({page,inspectProtection:inspectVideoProtection,accountIdentity,spec,write,chooserCount:()=>choosers}):await driver.perform(request.operation,request.arguments);
    if(choosers)fail('RECOVERY_UNEXPECTED_CHOOSER');
    reply({version:'replay-native-browser-response-v1',request_id:request.request_id,operation:request.operation,ok:true,result});
   }catch(e){reply({version:'replay-native-browser-response-v1',request_id:request?.request_id||null,operation:request?.operation||null,ok:false,error_code:/^[A-Z][A-Z0-9_]{1,100}$/.test(e?.code||'')?e.code:'RECOVERY_OPERATOR_STOPPED'});}
  }
 }catch(e){reply({version:'replay-native-browser-response-v1',state:'unavailable',ok:false,error_code:/^[A-Z][A-Z0-9_]{1,100}$/.test(e?.code||'')?e.code:'RECOVERY_OPERATOR_STOPPED'});process.exitCode=2;}
 finally{clearTimeout(deadline);await close();}
}
if(process.argv[1]===undefined||process.argv[1]===new URL(import.meta.url).pathname)await main();
