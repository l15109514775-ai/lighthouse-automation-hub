import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {validateHandoffConfig,validateVncVersion,guiArguments,retainProtectedPage,createPartOwnerHandoff,settleEmptyBodyProtection} from '../adapters/replay_part_owner_handoff_v5.mjs';
import {randomUUID} from 'node:crypto';
import {inspectVideoProtection} from '../adapters/bilibili_video_guard.mjs';
const [scenario,output]=process.argv.slice(2);
const config={version:'bilibili-video-owner-handoff-v5',enabled:true,ttl_seconds:900};
assert.deepEqual(validateHandoffConfig(config),config);
for(const bad of [{...config,ttl_seconds:1201},{...config,ttl_seconds:true},{...config,port:9999},{...config,command:'echo bad'}])assert.throws(()=>validateHandoffConfig(bad));
const args=guiArguments({auth:'/owned/Xauthority',passwordFile:'/owned/vnc-password.txt'});
assert.ok(args.xvfb.includes('-nolisten'));for(const flag of ['-norc','-noremote','-nocmds','-nosel','-passwdfile','-no6'])assert.ok(args.x11vnc.includes(flag));
validateVncVersion('x11vnc: 0.9.16 lastmod: 2019-01-05\n');
for(const version of ['x11vnc: 0.9.15 lastmod: 2019-01-05','x11vnc: 0.9.160 lastmod: 2019-01-05','unverified'])assert.throws(()=>validateVncVersion(version));
for(const flag of ['-ft','-tightfilexfer','-ultrafilexfer','-permitfiletransfer','-disablefiletransfer','-nofilexfer']){
 assert.ok(!args.x11vnc.includes(flag));
 assert.throws(()=>guiArguments({auth:'/owned/Xauthority',passwordFile:'/owned/vnc-password.txt',flags:[flag]}));
 assert.throws(()=>validateHandoffConfig({...config,x11vnc:[flag]}));
}
assert.deepEqual(args.websockify,['--web=/usr/share/novnc','127.0.0.1:6084','127.0.0.1:5904']);
assert.equal(args.x11vnc[args.x11vnc.indexOf('-listen')+1],'127.0.0.1');assert.ok(args.x11vnc.includes('-passwdfile'));
if(scenario==='config'){console.log(JSON.stringify({scenario,owned:true,platform_actions:0}));process.exit(0);}
if(scenario==='ticket'){
 const source=await fs.readFile(new URL('../adapters/browser_replay_part_v5.mjs',import.meta.url),'utf8');
 const start=source.indexOf('export async function performWithSubmitDeadline'),end=source.indexOf('\nexport function closeOnInputEnd',start);
 assert.ok(start>=0&&end>start);const run=new Function(source.slice(start,end).replace('export async function','return async function'))();
 let closes=0,clicks=0;await assert.rejects(()=>run(async()=>{await new Promise(r=>setTimeout(r,90));},{expires_at:Date.now()/1000+.04},()=>closes++,()=>true),e=>e.code==='REPLAY_BROWSER_RESERVATION_EXPIRED');
 assert.equal(closes,0);assert.equal(clicks,0);
 await assert.rejects(()=>run(async()=>{await new Promise(r=>setTimeout(r,90));},{expires_at:Date.now()/1000+.04},()=>closes++),e=>e.code==='REPLAY_BROWSER_RESERVATION_EXPIRED');assert.equal(closes,1);
 console.log(JSON.stringify({scenario,owned:true,platform_actions:0}));process.exit(0);
}
if(scenario==='close_hung'){
 const source=await fs.readFile(new URL('../adapters/browser_replay_part_v5.mjs',import.meta.url),'utf8');
 const start=source.indexOf('export async function closeOwnerContext'),end=source.indexOf('\n\nprocess.umask',start);
 assert.ok(start>=0&&end>start);const close=new Function(source.slice(start,end).replace('export async function','return async function'))();
 const sequence=[];let credentials=true;
 await assert.rejects(()=>close({close:()=>{sequence.push('context-close');return new Promise(()=>{});}},{prepareClose:async()=>{sequence.push('viewer-stop');credentials=false;},close:async()=>{sequence.push('display-stop');}},30),e=>e.code==='REPLAY_PART_CONTEXT_CLOSE_TIMEOUT');
 assert.deepEqual(sequence,['viewer-stop','context-close','display-stop']);assert.equal(credentials,false);
 console.log(JSON.stringify({scenario,owned:true,platform_actions:0}));process.exit(0);
}
if(scenario==='runtime_gui'){
 // Linux GUI-image smoke only. ROOT/config is a fixed root-owned RO fixture;
 // ROOT/media is an empty owned RW mount, never live application data.
 const root='/opt/lighthouse-hub',spec={version:'replay-part-native-browser-manifest-v5',job_id:randomUUID(),attempt_id:randomUUID(),target_site_uid:'1000000000000000',source_id:'OwnedID1234',binding_sha256:'a'.repeat(64),metadata_sha256:'b'.repeat(64)};
 let page,viewerTask,folder,shown=false;
 const handoff=await createPartOwnerHandoff({root,spec,notify:async event=>{
  const raw=JSON.parse(await fs.readFile(event.event_reference.file,'utf8'));folder=event.event_reference.file.replace(/\/detected.json$/,'');
  assert.equal(raw.state,'detected');assert.equal(raw.same_page,true);assert.ok(Date.parse(raw.expires_at)>Date.now());
  await fs.writeFile(folder+'/parent-ack.json',JSON.stringify({version:'replay-part-owner-handoff-parent-ack-v5',event_reference:event.event_reference,
   job_id:spec.job_id,attempt_id:spec.attempt_id,nonce:event.nonce,recorded_unknown_or_hold:true,automatic_retry:false,human_review:false,publication_authorized:false}),{flag:'wx',mode:0o600});
  viewerTask=(async()=>{const end=Date.now()+25000;let ready;
   while(Date.now()<end){try{ready=JSON.parse(await fs.readFile(folder+'/ready.json','utf8'));break;}catch(e){if(e.code!=='ENOENT')throw e;}await new Promise(r=>setTimeout(r,100));}
   assert.ok(ready);assert.equal(ready.web_host,'127.0.0.1');assert.equal(ready.web_port,6084);
   const password=await fs.readFile(ready.password_file,'utf8');assert.equal(password.trim().length,8);assert.equal((await fs.stat(ready.password_file)).mode&0o777,0o600);
   const rows=(await fs.readFile('/proc/net/tcp','utf8')).split('\n').slice(1).map(x=>x.trim().split(/\s+/));
   for(const port of [5904,6084])assert.ok(rows.some(r=>r[1]==='0100007F:'+port.toString(16).toUpperCase().padStart(4,'0')&&r[3]==='0A'));
   shown=true;await page.locator('.geetest_panel').evaluate(e=>e.remove());})();viewerTask.catch(()=>{});
 }});
 assert.equal(handoff.enabled,true);assert.equal(handoff.browserOptions.headless,false);
 const browser=await chromium.launch({...handoff.browserOptions,chromiumSandbox:true}),context=await browser.newContext();let outside=0;
 try{
  await context.route('**/*',r=>{if(r.request().url()!=='https://member.bilibili.com/platform/upload/video/frame'){outside++;return r.abort();}return r.fulfill({status:200,contentType:'text/html',body:'<input name=buploader type=file accept=.mp4><div class=geetest_panel style="width:300px;height:150px">Owned challenge</div>'});});
  page=await context.newPage();await page.goto('https://member.bilibili.com/platform/upload/video/frame');const protection=await inspectVideoProtection(page);
  await assert.rejects(()=>handoff.handleProtection({page,protection}),e=>e.code==='REPLAY_PART_OWNER_VERIFIED_RECONCILE_REQUIRED');await viewerTask;
  assert.equal(shown,true);assert.equal(page.isClosed(),false);assert.equal(outside,0);
 }finally{try{await handoff.prepareClose();}finally{try{await context.close();await browser.close();}finally{await handoff.close();}}}
 for(const name of ['Xauthority','vnc-password.txt'])await assert.rejects(()=>fs.lstat(folder+'/'+name),e=>e.code==='ENOENT');
 console.log(JSON.stringify({scenario,owned:true,real_xvfb_vnc_websockify:true,loopback_only:true,temporary_credentials_removed:true,platform_actions:0}));process.exit(0);
}
let outside=0,choices=0,clicks=0,started=0,stopped=0;const browser=await chromium.launch({headless:true,chromiumSandbox:true}),context=await browser.newContext();
await context.route('**/*',async r=>{if(r.request().url()!=='https://member.bilibili.com/platform/upload/video/frame'){outside++;return r.abort();}return r.fulfill({status:200,contentType:'text/html',headers:{'x-owned-fixture':'true'},body:['settles','body_unreadable'].includes(scenario)?'<body style="min-height:100vh" class="risk-captcha-adapt risk-captcha-adapt-pc"></body>':'<body class="risk-captcha-adapt risk-captcha-adapt-pc"><input type=file name=buploader accept=.mp4><button id=submit>立即投稿</button><div class=geetest_panel style="width:300px;height:150px">请完成验证</div></body>'});});
const page=await context.newPage();page.on('filechooser',()=>choices++);await page.goto('https://member.bilibili.com/platform/upload/video/frame');await page.exposeBinding('ownedClicked',()=>clicks++);if(await page.locator('#submit').count())await page.locator('#submit').evaluate(e=>e.onclick=()=>window.ownedClicked());
const protectedPage=await inspectVideoProtection(page);assert.equal(protectedPage.blocked,true);const events=[];let clock=0,notified=0;
try{
 if(['settles','body_unreadable'].includes(scenario)){
  await fs.mkdir(output,{recursive:true});const spec={job_id:randomUUID(),attempt_id:randomUUID(),target_site_uid:'1000000000000000',source_id:'OwnedID1234',binding_sha256:'a'.repeat(64),metadata_sha256:'b'.repeat(64)};
  if(scenario==='settles')setTimeout(()=>page.evaluate(()=>document.body.innerHTML='<input name=buploader type=file accept=.mp4><div>Owned editor readable</div>').catch(()=>{}),300);
  const settled=await settleEmptyBodyProtection({page,protection:protectedPage,output,spec});const proof=JSON.parse(await fs.readFile(settled.readiness_observation.file,'utf8'));
  assert.equal(proof.before.visible_body_text,'');assert.ok(proof.before.screenshot.sha256);assert.ok(proof.after.screenshot.sha256);assert.equal(proof.max_wait_ms,5000);assert.equal(proof.read_only,true);
  if(scenario==='settles'){assert.equal(settled.protection.blocked,false);assert.equal(proof.outcome,'guard_cleared');}
  else{
   assert.equal(settled.protection.blocked,true);assert.equal(proof.outcome,'still_unreadable_body_marker');assert.equal(proof.after.visible_body_text,'');assert.ok(proof.polls.at(-1).elapsed_ms>=5000);
   await assert.rejects(()=>retainProtectedPage({page,protection:settled.protection,seconds:60,reason:'unreadable_body_marker',persist:async state=>{events.push({state});return {};},notify:async()=>{},readAck:async()=>true,
    startGui:async()=>{setTimeout(()=>page.evaluate(()=>document.body.innerHTML='<input name=buploader type=file accept=.mp4><div>Owned readable</div>').catch(()=>{}),100);},stopGui:async()=>{}}),e=>e.code==='REPLAY_PART_OWNER_RECONCILE_REQUIRED');
   assert.deepEqual(events.map(e=>e.state),['detected','ready','page_readable']);
  }
  assert.equal(choices,0);assert.equal(clicks,0);assert.equal(outside,0);console.log(JSON.stringify({scenario,owned:true,platform_actions:0}));
 }else{
 const options={page,protection:protectedPage,seconds:60,persist:async(state,protection)=>{events.push({state,url:page.url(),protection});return {file:'/owned/'+state+'.json',bytes:100,sha256:'a'.repeat(64)};},
  notify:async()=>{notified++;},readAck:async()=>true,startGui:async()=>{started++;if(scenario==='verified')setTimeout(()=>page.locator('.geetest_panel').evaluate(e=>e.remove()).catch(()=>{}),120);},stopGui:async()=>{stopped++;}};
 if(scenario==='no_ack'){options.readAck=async()=>false;options.now=()=>clock;options.wait=async n=>{clock+=n;};}
 if(scenario==='expired'){options.now=()=>clock;options.wait=async()=>{clock+=60000;};}
 await assert.rejects(()=>retainProtectedPage(options),e=>e.code===({verified:'REPLAY_PART_OWNER_VERIFIED_RECONCILE_REQUIRED',no_ack:'REPLAY_PART_HANDOFF_PARENT_ACK_REQUIRED',expired:'REPLAY_PART_OWNER_HANDOFF_EXPIRED'}[scenario]));
 assert.equal(notified,1);assert.equal(stopped,1);assert.equal(started,scenario==='no_ack'?0:1);assert.equal(page.isClosed(),false);
 assert.deepEqual(events.map(e=>e.state),scenario==='verified'?['detected','ready','verification_cleared']:scenario==='expired'?['detected','ready','expired']:['detected']);
 assert.ok(events.every(e=>e.url==='https://member.bilibili.com/platform/upload/video/frame'));assert.equal(choices,0);assert.equal(clicks,0);assert.equal(outside,0);
 console.log(JSON.stringify({scenario,owned:true,platform_actions:0,retained_same_page:true}));
 }
}finally{await context.close();await browser.close();}
