// V5-only owner authentication assistance on the original protected page.
// No site API, CAPTCHA solving, automatic retry or public-send operation.
import fs from 'node:fs/promises';
import path from 'node:path';
import net from 'node:net';
import {spawn} from 'node:child_process';
import {randomBytes,randomUUID,createHash} from 'node:crypto';
import {inspectVideoProtection} from './bilibili_video_guard.mjs';
const VERSION='bilibili-video-owner-handoff-v5',DISPLAY=':91';
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
function fail(code){throw Object.assign(new Error(code),{code});}
function actualChallenge(p){return p?.blocked===true&&p.error_code==='BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED'&&
 (p.evidence?.visible_challenge_controls?.some(c=>c.tag!=='BODY')||p.evidence?.challenge_frames?.length>0);}
function exactBodyMarker(p){const e=p?.evidence,c=e?.visible_challenge_controls;return p?.blocked===true&&p.error_code==='BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED'&&
 e?.version==='bilibili-video-protection-guard-v3'&&e.inspection_failed!==true&&e.truncated===false&&e.top_level_authentication===false&&
 e.visible_dialogs?.length===0&&e.challenge_frames?.length===0&&e.native_mp4_uploaders===0&&c?.length===1&&c[0].tag==='BODY'&&c[0].role===''&&
 [...c[0].classes].sort().join(',')==='risk-captcha-adapt,risk-captcha-adapt-pc';}
async function visibleText(page){return (await page.locator('body').innerText()).slice(0,30000);}
export async function captureOwnerScreenshot(page,file,timeoutMs=15000){
 // The first headful compositor frame can lag document/fonts readiness.
 // Keep one absolute budget, the original page/viewport and every mask.
 if(!Number.isSafeInteger(timeoutMs)||timeoutMs<1||timeoutMs>15000)fail('REPLAY_PART_HANDOFF_SCREENSHOT_BUDGET_INVALID');
 const deadline=performance.now()+timeoutMs;
 const remaining=()=>{const ms=Math.floor(deadline-performance.now());if(ms<=0)fail('REPLAY_PART_HANDOFF_SCREENSHOT_NOT_READY');return ms;};
 for(let attempt=0;attempt<2;attempt++){
  let timer;
  try{
   await Promise.race([Promise.resolve().then(async()=>{await page.bringToFront();await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));}),
    new Promise((_,reject)=>{timer=setTimeout(()=>reject(Object.assign(new Error('REPLAY_PART_HANDOFF_SCREENSHOT_NOT_READY'),{code:'REPLAY_PART_HANDOFF_SCREENSHOT_NOT_READY'})),remaining());})]);
  }finally{clearTimeout(timer);}
  const mask=page.frames().map(frame=>frame.locator('input[type="password"],input[autocomplete="one-time-code"],input[type="tel"],input[inputmode="numeric"]'));
  try{return await page.screenshot({path:file,fullPage:false,timeout:remaining(),mask});}
  catch(error){
   if(attempt!==0||!String(error?.message||'').includes('Protocol error (Page.captureScreenshot): Unable to capture screenshot'))throw error;
   await delay(Math.min(100,remaining()));
  }
 }
}
async function imageRef(page,file){
 await captureOwnerScreenshot(page,file);await fs.chmod(file,0o600);const h=await fs.open(file,'r+');try{await h.sync();}finally{await h.close();}return ref(file,20000000);
}
export async function settleEmptyBodyProtection({page,protection,output,spec}){
 // A partially rendered native manager can already contain its sidebar.
 // Text alone is not readiness: only the unchanged guard may clear the BODY
 // marker once its already-observed native controls are actually present.
 if(!exactBodyMarker(protection))return {protection,readiness_observation:null};
 const url=new URL(page.url()),manager=url.origin==='https://member.bilibili.com'&&url.pathname==='/platform/upload-manager/article';
 if(!manager&&(await visibleText(page)).trim()!=='')return {protection,readiness_observation:null};
 const parent=path.join(output,'protection-readiness');await fs.mkdir(parent,{mode:0o700,recursive:true});await safe(parent);
 const folder=path.join(parent,randomUUID());await fs.mkdir(folder,{mode:0o700});
 const began=performance.now(),before={protection,visible_body_text:await visibleText(page),screenshot:await imageRef(page,path.join(folder,'before.png'))},polls=[];
 let current=protection,text=before.visible_body_text,outcome='still_unreadable_body_marker';
 do{
  current=await inspectVideoProtection(page);text=await visibleText(page);polls.push({elapsed_ms:performance.now()-began,protection:current,visible_body_empty:text.trim()===''});
  if(!current.blocked){outcome='guard_cleared';break;}
  if(actualChallenge(current)){outcome='visible_challenge';break;}
  if(!exactBodyMarker(current)||(!manager&&text.trim()!=='')){outcome='protection_changed';break;}
  if(performance.now()-began>=5000)break;await delay(100);
 }while(polls.length<60);
 if(manager&&exactBodyMarker(current)&&text.trim()!=='')outcome='document_not_ready';
 const u=new URL(page.url()),after={protection:current,visible_body_text:text,screenshot:await imageRef(page,path.join(folder,'after.png'))};
 const record={version:'replay-part-empty-body-readiness-v5',job_id:spec.job_id,attempt_id:spec.attempt_id,site_uid:spec.target_site_uid,source_id:spec.source_id,
  binding_sha256:spec.binding_sha256,metadata_sha256:spec.metadata_sha256,max_wait_ms:5000,before,after,polls,outcome,
  observed_at:new Date().toISOString(),page:{origin:u.origin,path:u.pathname,query_keys:[...new Set(u.searchParams.keys())].sort()},read_only:true,
  actions:{file_selections:0,submit_clicks:0,save_clicks:0}};
 return {protection:current,readiness_observation:await write(path.join(folder,'observation.json'),record)};
}
export function validateHandoffConfig(value){
 if(!value||Object.keys(value).sort().join()!=='enabled,ttl_seconds,version'||value.version!==VERSION||typeof value.enabled!=='boolean'||
  !Number.isSafeInteger(value.ttl_seconds)||value.ttl_seconds<60||value.ttl_seconds>1200)fail('REPLAY_PART_HANDOFF_CONFIG_INVALID');
 return Object.freeze({...value});
}
async function safe(file){
 if(!path.isAbsolute(file)||path.resolve(file)!==file)fail('REPLAY_PART_HANDOFF_PATH_INVALID');
 for(let p=file;p!==path.dirname(p);p=path.dirname(p))if((await fs.lstat(p)).isSymbolicLink())fail('REPLAY_PART_HANDOFF_PATH_INVALID');
 if(await fs.realpath(file)!==file)fail('REPLAY_PART_HANDOFF_PATH_INVALID');
}
async function ref(file,max=2000000){
 await safe(file);const h=await fs.open(file,'r');let raw;
 try{const a=await h.stat();if(!a.isFile()||a.size<1||a.size>max)fail('REPLAY_PART_HANDOFF_FILE_BOUND');raw=await h.readFile();const b=await h.stat();if(raw.length!==a.size||['size','dev','ino','mtimeMs','ctimeMs'].some(k=>a[k]!==b[k]))fail('REPLAY_PART_HANDOFF_FILE_CHANGED');}finally{await h.close();}
 return {file,bytes:raw.length,sha256:createHash('sha256').update(raw).digest('hex')};
}
async function write(file,value){
 const h=await fs.open(file,'wx',0o600);try{await h.writeFile(JSON.stringify(value,null,2)+'\n');await h.sync();}finally{await h.close();}
 if(process.platform!=='win32'){const d=await fs.open(path.dirname(file),'r');try{await d.sync();}finally{await d.close();}}return ref(file);
}
export function validateVncVersion(value){
 if(typeof value!=='string'||value.trim()!=='x11vnc: 0.9.16 lastmod: 2019-01-05')fail('REPLAY_PART_HANDOFF_VNC_VERSION_UNSUPPORTED');
}
export function guiArguments(value){
 if(!value||Object.keys(value).sort().join()!=='auth,passwordFile')fail('REPLAY_PART_HANDOFF_GUI_ARGUMENTS_INVALID');
 const {auth,passwordFile}=value;
 if(!path.isAbsolute(auth)||!path.isAbsolute(passwordFile))fail('REPLAY_PART_HANDOFF_PATH_INVALID');
 return {
  xvfb:[':91','-screen','0','1324x960x24','-nolisten','tcp','-auth',auth,'-noreset'],
  x11vnc:['-display',DISPLAY,'-auth',auth,'-passwdfile',passwordFile,'-rfbport','5904','-listen','127.0.0.1','-localhost','-no6',
   // The qualified 0.9.16 disables file transfer by default. Its opt-in
   // -tightfilexfer/-ultrafilexfer/-permitfiletransfer are never supplied.
   '-forever','-shared','-norc','-noremote','-nocmds','-nosel'],
  websockify:['--web=/usr/share/novnc','127.0.0.1:6084','127.0.0.1:5904']};
}
async function verifyVncVersion(){
 const p=spawn('x11vnc',['-version'],{stdio:['ignore','pipe','ignore']});let text='',tooLarge=false,timer;
 await new Promise((resolve,reject)=>{
  timer=setTimeout(()=>p.kill('SIGKILL'),5000);
  p.stdout.on('data',chunk=>{if(Buffer.byteLength(text)+chunk.length>1024){tooLarge=true;p.kill('SIGKILL');}else text+=chunk.toString('utf8');});
  p.once('error',()=>{clearTimeout(timer);reject(Object.assign(new Error('REPLAY_PART_HANDOFF_TOOL_UNAVAILABLE'),{code:'REPLAY_PART_HANDOFF_TOOL_UNAVAILABLE'}));});
  p.once('exit',code=>{clearTimeout(timer);try{if(code!==0||tooLarge)fail('REPLAY_PART_HANDOFF_VNC_VERSION_UNSUPPORTED');validateVncVersion(text);resolve();}catch(error){reject(error);}});
 });
}
function command(binary,args,input=null){
 const p=spawn(binary,args,{stdio:[input===null?'ignore':'pipe','ignore','ignore']});
 let timer;
 const finished=new Promise((resolve,reject)=>{p.once('error',()=>{clearTimeout(timer);reject(Object.assign(new Error('REPLAY_PART_HANDOFF_TOOL_UNAVAILABLE'),{code:'REPLAY_PART_HANDOFF_TOOL_UNAVAILABLE'}));});p.once('exit',code=>{clearTimeout(timer);code===0?resolve():reject(Object.assign(new Error('REPLAY_PART_HANDOFF_TOOL_FAILED'),{code:'REPLAY_PART_HANDOFF_TOOL_FAILED'}));});});
 if(input!==null)timer=setTimeout(()=>p.kill('SIGKILL'),10000);
 if(input!==null){p.stdin.on('error',()=>{});p.stdin.end(input);}
 return {p,finished};
}
async function stop(p){
 if(!p||p.exitCode!==null||p.signalCode!==null)return;
 p.kill('SIGTERM');await Promise.race([new Promise(r=>p.once('exit',r)),delay(3000)]);
 if(p.exitCode===null&&p.signalCode===null){p.kill('SIGKILL');await Promise.race([new Promise(r=>p.once('exit',r)),delay(3000)]);}
 if(p.exitCode===null&&p.signalCode===null)fail('REPLAY_PART_HANDOFF_TOOL_CLEANUP_REQUIRED');
}
async function portReady(port,child){
 const end=performance.now()+10000;
 while(performance.now()<end){
  if(child.exitCode!==null||child.signalCode!==null)fail('REPLAY_PART_HANDOFF_TOOL_FAILED');
  if(await new Promise(resolve=>{const s=net.createConnection({host:'127.0.0.1',port});s.once('connect',()=>{s.destroy();resolve(true);});s.once('error',()=>resolve(false));s.setTimeout(200,()=>{s.destroy();resolve(false);});}))return;
  await delay(100);
 }fail('REPLAY_PART_HANDOFF_TOOL_NOT_READY');
}
export async function retainProtectedPage({page,protection,seconds,persist,notify,readAck,startGui,stopGui,reason='visible_human_verification',now=()=>performance.now(),wait=delay}){
 // Trusted code callbacks only. None comes from a JSON manifest or policy.
 if((reason==='visible_human_verification'?!actualChallenge(protection):reason==='unreadable_body_marker'?!exactBodyMarker(protection)||Boolean((await visibleText(page)).trim()):true)||!Number.isSafeInteger(seconds)||seconds<60||seconds>1200)fail('REPLAY_PART_HANDOFF_PROTECTION_REQUIRED');
 const deadline=now()+seconds*1000;
 const timer=setTimeout(()=>{Promise.resolve().then(stopGui).catch(()=>{});},seconds*1000);
 try{
  const detected=await persist('detected',protection);await notify(detected);
  const ackEnd=Math.min(deadline,now()+15000);
  while(!await readAck(detected)){if(now()>=ackEnd)fail('REPLAY_PART_HANDOFF_PARENT_ACK_REQUIRED');await wait(100);}
  if(now()>=deadline)fail('REPLAY_PART_OWNER_HANDOFF_EXPIRED');
  await startGui(()=>now()<deadline);if(now()>=deadline)fail('REPLAY_PART_OWNER_HANDOFF_EXPIRED');let latest=await inspectVideoProtection(page);await persist('ready',latest);
  while(now()<deadline){
   if(page.isClosed())fail('REPLAY_PART_HANDOFF_PAGE_CLOSED');
   const current=await inspectVideoProtection(page);latest=current;
   if(current.evidence?.inspection_failed===true)fail('REPLAY_PART_HANDOFF_PAGE_UNREADABLE');
   if(!current.blocked){await persist(reason==='visible_human_verification'?'verification_cleared':'page_readable',current);fail(reason==='visible_human_verification'?'REPLAY_PART_OWNER_VERIFIED_RECONCILE_REQUIRED':'REPLAY_PART_OWNER_RECONCILE_REQUIRED');}
   if(current.error_code!=='BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED'){await persist('protection_changed',current);fail('REPLAY_PART_OWNER_RECONCILE_REQUIRED');}
   await wait(500);
  }
  await persist('expired',latest);fail('REPLAY_PART_OWNER_HANDOFF_EXPIRED');
 }finally{clearTimeout(timer);await stopGui();}
}
export async function createPartOwnerHandoff({root,spec,notify}){
 if(spec.version!=='replay-part-native-browser-manifest-v5'||typeof notify!=='function')fail('REPLAY_PART_HANDOFF_ROUTE_INVALID');
 const file=path.join(root,'config/bilibili-video-handoff-v5.json');let raw;
 try{
  await safe(file);for(let p=file;;p=path.dirname(p)){const st=await fs.stat(p);if(st.uid!==0||(st.mode&0o022)!==0)fail('REPLAY_PART_HANDOFF_CONFIG_NOT_ROOT_RO');if(p===path.dirname(p))break;}
  const evidence=await ref(file,4096);raw=JSON.parse(await fs.readFile(file,'utf8'));if((await ref(file,4096)).sha256!==evidence.sha256)fail('REPLAY_PART_HANDOFF_CONFIG_CHANGED');
 }catch(e){if(e.code==='ENOENT')return {enabled:false,browserOptions:{headless:true},isRetaining:()=>false,handleProtection:async()=>{},close:async()=>{}};throw e;}
 const config=validateHandoffConfig(raw);
 if(!config.enabled)return {enabled:false,browserOptions:{headless:true},isRetaining:()=>false,handleProtection:async()=>{},close:async()=>{}};
 const nonce=randomUUID(),base=path.join(root,'media',spec.job_id,'replay-browser-part-v5',spec.attempt_id);
 await fs.mkdir(base,{recursive:true,mode:0o700});await safe(base);
 const output=path.join(base,'owner-handoff');await fs.mkdir(output,{mode:0o700,recursive:true});await safe(output);
 const folder=path.join(output,nonce);await fs.mkdir(folder,{mode:0o700});
 const auth=path.join(folder,'Xauthority'),passwordFile=path.join(folder,'vnc-password.txt');
 const args=guiArguments({auth,passwordFile});let xvfb=null,vnc=null,web=null,retaining=false,consumed=false,activePage=null,expiresAt=null;
 const baseRecord={job_id:spec.job_id,attempt_id:spec.attempt_id,nonce,site_uid:spec.target_site_uid,source_id:spec.source_id,
  binding_sha256:spec.binding_sha256,metadata_sha256:spec.metadata_sha256,human_review:false,publication_authorized:false,
  authentication_assistance_only:true,automatic_resubmit:false,same_browser_context:true,same_page:true,ttl_seconds:config.ttl_seconds};
 async function persist(state,protection){
  const u=new URL(activePage.url());
  let screenshot=null;
  if(!activePage.isClosed()){
   screenshot=await imageRef(activePage,path.join(folder,state+'.png'));
  }
  return write(path.join(folder,state+'.json'),{version:'replay-part-owner-handoff-observation-v5',...baseRecord,state,
   observed_at:new Date().toISOString(),expires_at:expiresAt,page:{origin:u.origin,path:u.pathname,query_keys:[...new Set(u.searchParams.keys())].sort()},protection,screenshot,
   ...(state==='ready'?{web_host:'127.0.0.1',web_port:6084,vnc_host:'127.0.0.1',vnc_port:5904,password_file:passwordFile}:{}),
   handoff_actions:{automatic_public_submit_clicks:0,file_selections:0,private_save_clicks:0}});
 }
 async function stopGui(){const outcomes=await Promise.allSettled([stop(web),stop(vnc)]);web=null;vnc=null;await fs.unlink(passwordFile).catch(e=>{if(e.code!=='ENOENT')throw e;});if(outcomes.some(x=>x.status==='rejected'))fail('REPLAY_PART_HANDOFF_TOOL_CLEANUP_REQUIRED');}
 try{
  await verifyVncVersion();
  // Cookie travels through stdin, never through argv, a receipt or a log.
  await command('xauth',['-f',auth,'source','-'],'add '+DISPLAY+' MIT-MAGIC-COOKIE-1 '+randomBytes(16).toString('hex')+'\n').finished;
  await fs.chmod(auth,0o600);await safe(auth);
  for(const p of ['/tmp/.X91-lock','/tmp/.X11-unix/X91']){try{await fs.lstat(p);fail('REPLAY_PART_HANDOFF_DISPLAY_BUSY');}catch(e){if(e.code!=='ENOENT')throw e;}}
  const launched=command('Xvfb',args.xvfb);xvfb=launched.p;launched.finished.catch(()=>{});
  const end=performance.now()+10000;
  while(true){
   if(xvfb.exitCode!==null||xvfb.signalCode!==null)fail('REPLAY_PART_HANDOFF_DISPLAY_FAILED');
   try{if((await fs.stat('/tmp/.X11-unix/X91')).isSocket())break;}catch(e){if(e.code!=='ENOENT')throw e;}
   if(performance.now()>=end)fail('REPLAY_PART_HANDOFF_DISPLAY_NOT_READY');await delay(100);
  }
 }catch(e){await stop(xvfb);for(const file of [auth,passwordFile])await fs.unlink(file).catch(error=>{if(error.code!=='ENOENT')throw error;});throw e;}
 return {
  enabled:true,browserOptions:{headless:false,env:{...process.env,DISPLAY,XAUTHORITY:auth}},isRetaining:()=>retaining,
  async handleProtection({page,protection,readiness_observation=null}){
   const interactive=Boolean(actualChallenge(protection)),empty=(await visibleText(page)).trim()==='';
   if(!interactive&&(!exactBodyMarker(protection)||!empty||!readiness_observation))return;
   if(consumed)fail('REPLAY_PART_OWNER_HANDOFF_ALREADY_USED');consumed=true;retaining=true;activePage=page;expiresAt=new Date(Date.now()+config.ttl_seconds*1000).toISOString();
   Object.assign(baseRecord,{reason:interactive?'visible_human_verification':'unreadable_body_marker',interactive_verification_observed:interactive,
    visible_body_empty:empty,readiness_observation,authentication_assistance_only:interactive});
   try{
    await page.bringToFront();
    await retainProtectedPage({page,protection,seconds:config.ttl_seconds,persist,reason:baseRecord.reason,
     notify:event_reference=>notify({version:'replay-part-owner-handoff-event-v5',...baseRecord,expires_at:expiresAt,event_reference}),
     async readAck(event_reference){
      try{
       const ackFile=path.join(folder,'parent-ack.json');await ref(ackFile,8192);const a=JSON.parse(await fs.readFile(ackFile,'utf8'));
       if(Object.keys(a).sort().join()!=='attempt_id,automatic_retry,event_reference,human_review,job_id,nonce,publication_authorized,recorded_unknown_or_hold,version'||
        a.version!=='replay-part-owner-handoff-parent-ack-v5'||a.job_id!==spec.job_id||a.attempt_id!==spec.attempt_id||a.nonce!==nonce||
        !a.event_reference||Object.keys(a.event_reference).sort().join()!=='bytes,file,sha256'||Object.entries(event_reference).some(([k,v])=>a.event_reference[k]!==v)||
        a.recorded_unknown_or_hold!==true||a.automatic_retry!==false||a.human_review!==false||a.publication_authorized!==false)fail('REPLAY_PART_HANDOFF_PARENT_ACK_CHANGED');
       return true;
      }catch(e){if(e.code==='ENOENT')return false;throw e;}
     },async startGui(withinDeadline){
      if(!withinDeadline())fail('REPLAY_PART_OWNER_HANDOFF_EXPIRED');
      await fs.writeFile(passwordFile,randomBytes(6).toString('base64')+'\n',{flag:'wx',mode:0o600});
      const a=command('x11vnc',args.x11vnc);vnc=a.p;a.finished.catch(()=>{});await portReady(5904,vnc);
      if(!withinDeadline())fail('REPLAY_PART_OWNER_HANDOFF_EXPIRED');
      const b=command('websockify',args.websockify);web=b.p;b.finished.catch(()=>{});await portReady(6084,web);
     },stopGui});
   }finally{retaining=false;}
  },
  async prepareClose(){await stopGui();await fs.unlink(auth).catch(e=>{if(e.code!=='ENOENT')throw e;});},
  async close(){const results=await Promise.allSettled([stopGui(),stop(xvfb)]);await fs.unlink(auth).catch(e=>{if(e.code!=='ENOENT')throw e;});if(results.some(x=>x.status==='rejected'))fail('REPLAY_PART_HANDOFF_TOOL_CLEANUP_REQUIRED');}
 };
}
