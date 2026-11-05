// Ordinary first-party pages only. No private API, proxy, stealth or challenge bypass.
import fs from 'node:fs/promises';
import path from 'node:path';
import {createPublicXSession,PUBLIC_X_AUTHENTICATION_MODE,publicXAccessError} from './browser_x_public.mjs';
import {dismissOrdinaryPrompts,inspectArticleElement,ObservationError} from './browser_observation.mjs';
import {captureTweet} from './browser_tweet_capture.mjs';
import {preflightExpiredTweet} from './browser_expiry_preflight.mjs';
process.umask(0o077);
const raw=await new Promise((resolve,reject)=>{let s='';process.stdin.setEncoding('utf8');
 process.stdin.on('data',b=>{s+=b;if(s.length>200000)reject(new Error('SPEC_TOO_LARGE'));});process.stdin.on('end',()=>resolve(s));});
const spec=JSON.parse(raw),root=process.env.HUB_ROOT||'/opt/lighthouse-hub';
if(spec.version!=='x-web-discovery-v1'||!['example_handle_a','example-channel-b'].includes(spec.handle)||
 !/^[-a-f0-9]{36}$/.test(spec.job_id)||!/^[-a-f0-9]{36}$/.test(spec.attempt_id)||
 !Array.isArray(spec.seen_ids)||spec.seen_ids.length>1000||spec.seen_ids.some(id=>!/^\d{1,30}$/.test(id))||
 !Number.isFinite(spec.not_before)||spec.not_before<0||spec.not_before>Date.now()/1000)
 throw new Error('X_WEB_SPEC_INVALID');
const dir=path.join(root,'media',spec.job_id,'x-web',spec.attempt_id);
await fs.mkdir(dir,{recursive:true,mode:0o700});
const result={version:'x-web-discovery-v1',handle:spec.handle,posts:[],errors:[],
 timeline_complete:false,coverage:'bounded_visible_timeline_snapshot',official_api:false,human_review:false,
 authentication_mode:PUBLIC_X_AUTHENTICATION_MODE,persistent_profile_used:false};
let session,context,page;
async function navigate(url){
 const response=await page.goto(url,{waitUntil:'domcontentloaded'});
 const status=response?.status();
 const accessError=publicXAccessError(page.url(),status);if(accessError)throw new ObservationError(accessError);
 await dismissOrdinaryPrompts(page,path.join(dir,'navigation-prompts'));
 try{await page.locator('article').first().waitFor({state:'visible',timeout:15000});}
 catch{
  const accessError=publicXAccessError(page.url());if(accessError)throw new ObservationError(accessError);
  throw new ObservationError('X_WEB_TIMELINE_UNAVAILABLE');
 }
 await dismissOrdinaryPrompts(page,path.join(dir,'loaded-prompts'));
}
try{
 session=await createPublicXSession();context=session.context;
 page=await context.newPage();
 await navigate(`https://x.com/${spec.handle}`);
 await page.screenshot({path:path.join(dir,'timeline.png'),fullPage:false});
 const links=[];
 for(const article of await page.locator('article').all()){
  const own=await article.evaluate(inspectArticleElement);
  if(own?.author_verified&&own.handle.toLowerCase()===spec.handle.toLowerCase())links.push(own.postId);
 }
 result.visible_post_ids=[...new Set(links)].sort((a,b)=>BigInt(a)>BigInt(b)?-1:BigInt(a)<BigInt(b)?1:0).slice(0,10);
 if(!result.visible_post_ids.length)throw new ObservationError('X_WEB_NO_VERIFIABLE_SOURCE_ARTICLES');
 for(const id of result.visible_post_ids){
  if(spec.seen_ids.includes(id))continue;
  try{
   await navigate(`https://x.com/${spec.handle}/status/${id}`);
   const expiry=await preflightExpiredTweet(page,{handle:spec.handle,postId:id},path.join(dir,id,'expiry-preflight'),{notBefore:spec.not_before});
   if(expiry.decision==='skipped_expired'){
    (result.skipped_before_monitor_start??=[]).push(id);
    (result.expiry_preflight_skips??=[]).push({post_id:id,evidence_file:expiry.evidence_file,
     version:expiry.version,purpose:expiry.purpose,complete_source:false});
    continue;
   }
   const observed=spec.machine_policy==='machine-tweet-v2'?
    await (await import('./browser_extended_source.mjs')).captureExtendedTweet(page,
     {handle:spec.handle,postId:id},path.join(dir,id),{trigger:spec.trigger}):
    await captureTweet(page,{handle:spec.handle,postId:id},path.join(dir,id),{trigger:spec.trigger,machineSource:spec.machine_source===true});
   if(Date.parse(observed.payload.post.created_at)/1000<spec.not_before){
    (result.skipped_before_monitor_start??=[]).push(id);continue;
   }
   result.posts.push(observed);
  }catch(e){
   const code=e.code==='AUTH_EXPIRED'?'X_WEB_LOGIN_REQUIRED_PUBLIC_MODE':
    e.code||(/^[A-Z_]+$/.test(e.message)?e.message:'X_WEB_CAPTURE_FAILED');
   result.errors.push({post_id:id,error_code:code});
   await page.screenshot({path:path.join(dir,`${id}-blocked.png`),timeout:5000}).catch(()=>{});
   if(['WEB_HUMAN_VERIFICATION_REQUIRED','X_WEB_LOGIN_REQUIRED_PUBLIC_MODE','X_WEB_ACCESS_DENIED'].includes(code))break;
  }
 }
 result.state=result.errors.length?'partial_observation':'observed';
}catch(e){
 result.state='blocked';result.error_code=e.code==='AUTH_EXPIRED'?'X_WEB_LOGIN_REQUIRED_PUBLIC_MODE':
  e.code||(e.name==='TimeoutError'?'X_WEB_NETWORK_TIMEOUT':'X_WEB_RUNTIME_FAILED');
 if(page){
  result.visited_url=page.url();
  await page.screenshot({path:path.join(dir,'blocked.png'),timeout:5000}).catch(()=>{});
 }
}finally{
 result.checked_at=new Date().toISOString();
 await fs.writeFile(path.join(dir,'result.json'),JSON.stringify(result,null,2),{mode:0o600});
 await session?.close();
 console.log(JSON.stringify({result_file:path.join(dir,'result.json')}));
}
