// Worker-only JSON-lines IPC. No network listener, arbitrary command or JS.
import fs from 'node:fs/promises';
import path from 'node:path';
import readline from 'node:readline';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {createReplayNativeDriver,validateReplayManifest} from './replay_native_driver.mjs';

// The existing native driver still performs every source/file/guard check.
// Waiting for its result is separate from permission to click before expiry.
export async function performWithSubmitDeadline(run,reservation,close){
 const expires=reservation?.expires_at,remaining=typeof expires==='number'&&Number.isFinite(expires)?expires*1000-Date.now():NaN;
 const refuse=()=>{throw Object.assign(new Error('REPLAY_BROWSER_RESERVATION_EXPIRED'),{code:'REPLAY_BROWSER_RESERVATION_EXPIRED'});};
 if(!Number.isFinite(remaining)||remaining<=0||remaining>900000)refuse();
 let expired=false;
 const timer=setTimeout(()=>{expired=true;Promise.resolve().then(close).catch(()=>{});},remaining);
 try{const result=await run();if(expired)refuse();return result;}
 catch(error){if(expired)refuse();throw error;}finally{clearTimeout(timer);}
}
export function closeOnInputEnd(input,close){
 input.once('close',()=>{Promise.resolve().then(close).catch(()=>{});});
}

process.umask(0o077);
const UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const root=path.resolve(process.env.HUB_ROOT||'/opt/lighthouse-hub');
const lines=readline.createInterface({input:process.stdin,crlfDelay:Infinity});
let context=null,closed=false,closePromise=null,opening=null;
function fail(code){throw Object.assign(new Error(code),{code});}
function reply(value){process.stdout.write(JSON.stringify(value)+'\n');}
function close(){
 if(closePromise)return closePromise;closed=true;
 const pending=opening;
 closePromise=(async()=>{const active=context||(pending?await pending.catch(()=>null):null);await active?.close();lines.close();})();return closePromise;
}
closeOnInputEnd(lines,close);
process.on('SIGTERM',()=>close().finally(()=>process.exit(0)));
process.on('SIGINT',()=>close().finally(()=>process.exit(0)));
const deadline=setTimeout(()=>close().finally(()=>process.exit(2)),24*3600*1000);
deadline.unref();
try{
 const iterator=lines[Symbol.asyncIterator](),first=await iterator.next();
 if(first.done||Buffer.byteLength(first.value)>1048576)fail('REPLAY_BROWSER_MANIFEST_INVALID');
 const spec=validateReplayManifest(JSON.parse(first.value));
 if(spec.version!=='replay-natural-native-browser-manifest-v3'&&Buffer.byteLength(first.value)>131072)fail('REPLAY_BROWSER_MANIFEST_INVALID');
 if(typeof process.getuid!=='function'||process.getuid()!==10001||process.getgid()!==10001||process.env.HUB_BROWSER_CHANNEL&&process.env.HUB_BROWSER_CHANNEL!=='chromium'||spec.target_site_uid!==process.env.BILIBILI_EXPECTED_SITE_UID)fail('REPLAY_BROWSER_RUNTIME_IDENTITY_INVALID');
 const profile=path.join(root,'data/browser-profiles/bilibili-video');
 for(let p=profile;p!==path.dirname(p);p=path.dirname(p))if((await fs.lstat(p)).isSymbolicLink())fail('REPLAY_BROWSER_PROFILE_INVALID');
 const st=await fs.stat(profile);if(await fs.realpath(profile)!==profile||!st.isDirectory()||st.uid!==10001||st.mode&0o077)fail('REPLAY_BROWSER_PROFILE_INVALID');
 if(closed)fail('REPLAY_BROWSER_INPUT_CLOSED');
 opening=chromium.launchPersistentContext(profile,{headless:true,channel:'chromium',chromiumSandbox:true,viewport:{width:1280,height:960},acceptDownloads:false});
 context=await opening;if(closed){await close();fail('REPLAY_BROWSER_INPUT_CLOSED');}
 const page=context.pages()[0]||await context.newPage();
 const driver=await createReplayNativeDriver({page,context,spec,root});
 reply({version:'replay-native-browser-response-v1',state:'ready',job_id:spec.job_id,attempt_id:spec.attempt_id,output:driver.output});
 const seen=new Set();
 while(!closed){
  const next=await iterator.next();if(next.done)break;
  let request=null;
  try{
   if(Buffer.byteLength(next.value)>16384)fail('REPLAY_BROWSER_REQUEST_INVALID');request=JSON.parse(next.value);
   if(!request||Object.keys(request).sort().join(',')!=='arguments,operation,request_id,version'||request.version!=='replay-native-browser-request-v1'||!UUID.test(request.request_id||'')||seen.has(request.request_id)||!['upload','prepare','submit','lookup'].includes(request.operation)||!request.arguments||typeof request.arguments!=='object'||Array.isArray(request.arguments))fail('REPLAY_BROWSER_REQUEST_INVALID');
   const keys=Object.keys(request.arguments).sort().join(',');
   if(request.operation==='submit'?keys!=='reservation':request.operation==='lookup'?keys!==''&&keys!=='external_id':keys!=='')fail('REPLAY_BROWSER_REQUEST_INVALID');
   seen.add(request.request_id);
   const result=request.operation==='submit'?
    await performWithSubmitDeadline(()=>driver.perform(request.operation,request.arguments),request.arguments.reservation,close):
    await driver.perform(request.operation,request.arguments);
   reply({version:'replay-native-browser-response-v1',request_id:request.request_id,operation:request.operation,ok:true,result});
  }catch(e){reply({version:'replay-native-browser-response-v1',request_id:request?.request_id||null,operation:request?.operation||null,ok:false,error_code:/^[A-Z][A-Z0-9_]{1,100}$/.test(e?.code||'')?e.code:'REPLAY_BROWSER_REQUEST_FAILED'});}
 }
}catch(e){reply({version:'replay-native-browser-response-v1',state:'unavailable',ok:false,error_code:/^[A-Z][A-Z0-9_]{1,100}$/.test(e?.code||'')?e.code:'REPLAY_BROWSER_RUNTIME_FAILED'});process.exitCode=2;}
finally{clearTimeout(deadline);await close();}
