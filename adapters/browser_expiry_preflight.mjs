// This receipt can only skip a certainly expired post. It never attests full
// text, media, references, translation, human review or permission to publish.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {inspectArticleElement,locateTweetArticle} from './browser_observation.mjs';
const hash=b=>crypto.createHash('sha256').update(b).digest('hex');
const fail=(code,{retryEligible=false}={})=>{const e=new Error(code);e.code=code;e.retry_eligible=retryEligible;throw e;};
const RETRY_CODES=new Set(['EXPIRY_PREFLIGHT_TARGET_CHANGED','EXPIRY_PREFLIGHT_CONFIRMATIONS_DIFFER']);

export function expiryTimeInterval(value){
 let raw=value.kind==='native_time_datetime'?value.datetime:null;
 if(value.kind==='visible_year_minute'){
  const m=/^(\d{1,2}):(\d{2})\s*[·•]\s*(\d{4})年(\d{1,2})月(\d{1,2})日$/.exec(value.label);
  if(!m||value.browser_timezone!=='Asia/Shanghai')return null;
  raw=`${m[3]}-${m[4].padStart(2,'0')}-${m[5].padStart(2,'0')}T${m[1].padStart(2,'0')}:${m[2]}+08:00`;
 }
 const m=/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?(Z|[+-]\d{2}:\d{2})$/.exec(raw||'');
 if(!m||+m[1]<2000||+m[1]>2100||+m[4]>23||+m[5]>59||+(m[6]||0)>59)return null;
 const zone=m[8],hours=zone==='Z'?0:+zone.slice(1,3),minutes=zone==='Z'?0:+zone.slice(4,6);
 if(hours>14||minutes>59||(hours===14&&minutes!==0))return null;
 const offset=zone==='Z'?0:(zone[0]==='-'?-1:1)*(hours*60+minutes);
 const epoch=Date.parse(raw)/1000;if(!Number.isFinite(epoch))return null;
 const local=new Date((epoch+offset*60)*1000);
 if(local.getUTCFullYear()!==+m[1]||local.getUTCMonth()+1!==+m[2]||local.getUTCDate()!==+m[3]||
  local.getUTCHours()!==+m[4]||local.getUTCMinutes()!==+m[5]||local.getUTCSeconds()!==+(m[6]||0))return null;
 const uncertainty=m[7]?10**(-m[7].length):m[6]!==undefined?1:60;
 return {kind:value.kind,raw_value:raw,earliest_epoch:epoch,upper_bound_exclusive_epoch:epoch+uncertainty,
  precision_seconds:uncertainty};
}

// Re-resolve the semantic field inside the exact article for each native
// scroll/read. Hydration can insert or hide identical links; a saved index or
// ElementHandle is not the identity of the visible source field.
function visibleTarget(article,{identity,displayName,field,scrollOnly=false}){
 const body=article.querySelector('[data-testid="tweetText"],.whitespace-pre-wrap[dir="auto"]');
 const parse=a=>{try{return new URL(a.getAttribute('href'),article.ownerDocument.location.href);}catch{return null;}};
 const cssVisible=e=>{
  const r=e.getBoundingClientRect();if(r.width<=0||r.height<=0)return false;
  for(let p=e;p;p=p.parentElement){const s=getComputedStyle(p);
   if(s.display==='none'||s.visibility!=='visible'||Number(s.opacity)<.01||s.clipPath!=='none'||
    (s.clip!=='auto'&&s.clip!=='rect(auto, auto, auto, auto)'))return false;}
  return true;
 };
 const own=[...article.querySelectorAll('a[href]')].map(a=>({a,u:parse(a)})).filter(({a,u})=>u&&
  a.closest('article')===article&&!body?.contains(a)&&!a.closest('blockquote,[data-testid="quoteTweet"]')&&
  ['http:','https:'].includes(u.protocol)&&!u.username&&!u.password&&
  ['x.com','www.x.com','twitter.com','www.twitter.com'].includes(u.hostname));
 const beforeBody=a=>!body||!a.contains(body)&&!!(a.compareDocumentPosition(body)&Node.DOCUMENT_POSITION_FOLLOWING);
 const matches=own.filter(({a,u})=>field==='timestamp'?
  u.pathname.replace(/\/$/,'').toLowerCase()===`/${identity.handle}/status/${identity.postId}`.toLowerCase()&&
   (a.querySelector('time[datetime]')||/^\d{1,2}:\d{2}\s*[·•]\s*\d{4}年\d{1,2}月\d{1,2}日$/.test(a.innerText.trim())):
  u.pathname.replace(/\/$/,'').toLowerCase()==='/'+identity.handle.toLowerCase()&&beforeBody(a)&&
   (field==='author_handle'?a.innerText.trim().toLowerCase()==='@'+identity.handle.toLowerCase():
    field==='author_name'&&a.innerText.trim()===displayName&&!/^@[A-Za-z0-9_]+$/.test(a.innerText.trim())));
 const candidates=matches.filter(({a})=>cssVisible(a));
 const binding={version:'native-expiry-field-binding-v1',field,handle:identity.handle,post_id:identity.postId,
  matching_anchors:matches.length,visible_candidates:candidates.length};
 const refuse=code=>({binding_error:code,binding,visible:false,
  candidate_diagnostics:matches.slice(0,8).map(({a})=>({href:a.href,text:a.innerText.trim(),css_visible:cssVisible(a),
   rect:{left:a.getBoundingClientRect().left,top:a.getBoundingClientRect().top,width:a.getBoundingClientRect().width,height:a.getBoundingClientRect().height}}))});
 if(candidates.length!==1)return refuse(candidates.length?'EXPIRY_PREFLIGHT_FIELD_AMBIGUOUS':'EXPIRY_PREFLIGHT_TARGET_NOT_VISIBLE');
 const [{a:anchor}]=candidates;let element=anchor;
 if(field==='timestamp'){
  const native=[...anchor.querySelectorAll('time[datetime]')];
  if(native.length>1)return refuse('EXPIRY_PREFLIGHT_NATIVE_TIME_AMBIGUOUS');
  if(native.length){[element]=native;if(!cssVisible(element))return refuse('EXPIRY_PREFLIGHT_TARGET_NOT_VISIBLE');}
 }
 binding.native_href=anchor.href;binding.native_datetime=element.tagName==='TIME';
 if(scrollOnly){element.scrollIntoView({block:'center',inline:'nearest',behavior:'instant'});return {binding,visible:true};}
 const root=element.closest('a')||element,rect=element.getBoundingClientRect(),points=[];
 let hidden=false,clipped=false;
 const clips=[];
 for(let p=element;p;p=p.parentElement){
  const s=getComputedStyle(p),b=p.getBoundingClientRect();
  if(s.display==='none'||s.visibility!=='visible'||Number(s.opacity)<.01||s.clipPath!=='none'||
   (s.clip!=='auto'&&s.clip!=='rect(auto, auto, auto, auto)'))hidden=true;
  if(['hidden','clip','scroll','auto'].includes(s.overflowX)||['hidden','clip','scroll','auto'].includes(s.overflowY))
   clips.push({left:b.left,top:b.top,right:b.right,bottom:b.bottom,x:s.overflowX!=='visible',y:s.overflowY!=='visible'});
 }
 const walker=element.ownerDocument.createTreeWalker(element,NodeFilter.SHOW_TEXT);let node,lines=0;
 while((node=walker.nextNode())){
  if(!node.textContent.trim())continue;
  const range=element.ownerDocument.createRange();range.selectNodeContents(node);
  for(const b of range.getClientRects()){
   if(b.width<1||b.height<1)continue;lines++;
   if(b.left<0||b.top<0||b.right>innerWidth||b.bottom>innerHeight||clips.some(c=>
    c.x&&(b.left<c.left-1||b.right>c.right+1)||c.y&&(b.top<c.top-1||b.bottom>c.bottom+1)))clipped=true;
   for(const fx of [.05,.5,.95]){
    const x=b.left+b.width*fx,y=b.top+b.height/2,hit=element.ownerDocument.elementFromPoint(x,y);
    points.push({x,y,visible:!!hit&&(hit===root||root.contains(hit))});
   }
  }
 }
 // Pointerless painted overlays still obscure the source although hit testing
 // passes through them. This also covers an ordinary native fixed/sticky bar.
 const painted_covers=[...element.ownerDocument.querySelectorAll('body *')].filter(n=>{
  if(n===root||n.contains(root)||root.contains(n))return false;
  const s=getComputedStyle(n),b=n.getBoundingClientRect();
  return ['fixed','sticky'].includes(s.position)&&s.display!=='none'&&s.visibility==='visible'&&Number(s.opacity)>.01&&
   s.pointerEvents==='none'&&(s.backgroundImage!=='none'||!['transparent','rgba(0, 0, 0, 0)'].includes(s.backgroundColor))&&
   points.some(p=>p.x>b.left&&p.x<b.right&&p.y>b.top&&p.y<b.bottom);
 }).map(n=>({tag:n.tagName,role:n.getAttribute('role')}));
 return {binding,text:element.innerText.trim(),href:root.href||null,datetime:element.getAttribute('datetime'),
  tag:element.tagName,rect:{left:rect.left,top:rect.top,width:rect.width,height:rect.height},
  hidden,clipped,text_line_count:lines,points,painted_covers,
  visible:!hidden&&!clipped&&!painted_covers.length&&rect.width>0&&rect.height>0&&points.length>0&&points.every(p=>p.visible),
  viewport:{width:innerWidth,height:innerHeight},
  scroll_x:scrollX,scroll_y:scrollY};
}

async function readTarget(page,article,fieldSpec,file,trace){
 const diagnostic={field:path.basename(file),before:null,after:null,mismatch_fields:[],retry_eligible:false};
 trace.target_diagnostics.push(diagnostic);
 try{
 // Ordinary native scrolling only. A sticky bar or prompt which still covers
 // the native target makes the preflight inconclusive, never an authorization.
 const scrolled=await article.evaluate(visibleTarget,{...fieldSpec,scrollOnly:true});
 diagnostic.scroll_binding=scrolled.binding;
 if(scrolled.binding_error){diagnostic.before=scrolled;fail(scrolled.binding_error);}
 const value=await article.evaluate(visibleTarget,fieldSpec);
 diagnostic.before=value;
 if(value.binding_error)fail(value.binding_error);
 if(!value.visible)fail('EXPIRY_PREFLIGHT_TARGET_NOT_VISIBLE');
 // Locator screenshots perform their own actionability wait/scroll and retain
 // an ElementHandle, which a normal public DOM rerender can detach. Capture
 // the actual proved viewport region without another scroll or DOM change.
 const x=Math.max(0,Math.floor(value.rect.left)),y=Math.max(0,Math.floor(value.rect.top));
 const clip={x,y,width:Math.min(value.viewport.width,Math.ceil(value.rect.left+value.rect.width))-x,
  height:Math.min(value.viewport.height,Math.ceil(value.rect.top+value.rect.height))-y};
 if(clip.width<=0||clip.height<=0)fail('EXPIRY_PREFLIGHT_TARGET_NOT_VISIBLE');
 await page.screenshot({path:file,clip,fullPage:false,caret:'initial',animations:'allow',timeout:5000});
 await fs.chmod(file,0o600);
 diagnostic.screenshot_file=file;diagnostic.screenshot_sha256=hash(await fs.readFile(file));diagnostic.screenshot_clip=clip;
 const after=await article.evaluate(visibleTarget,fieldSpec);
 diagnostic.after=after;
 if(after.binding_error){diagnostic.mismatch_fields=['binding'];fail('EXPIRY_PREFLIGHT_TARGET_CHANGED');}
 diagnostic.mismatch_fields=['visible','text','href','datetime','tag','rect','viewport','scroll_x','scroll_y'].filter(field=>
  JSON.stringify(after[field])!==JSON.stringify(value[field]));
 if(!after.visible||diagnostic.mismatch_fields.length){
  diagnostic.retry_eligible=value.visible&&after.visible&&diagnostic.mismatch_fields.length>0&&
   diagnostic.mismatch_fields.every(field=>['rect','viewport','scroll_x','scroll_y'].includes(field));
  fail('EXPIRY_PREFLIGHT_TARGET_CHANGED',{retryEligible:diagnostic.retry_eligible});
 }
 return {...value,screenshot_method:'page_viewport_clip-v1',screenshot_clip:clip,
  screenshot_file:file,screenshot_sha256:diagnostic.screenshot_sha256};
 }catch(e){diagnostic.error_code=e.code||'EXPIRY_PREFLIGHT_UNREADABLE';diagnostic.error_name=e.name||'Error';
  diagnostic.error_message=String(e.message||diagnostic.error_code).slice(0,700);throw e;}
}

async function readObservation(page,identity,dir,round,trace,baseline){
 const article=await locateTweetArticle(page,identity),before=await article.evaluate(inspectArticleElement);
 trace.identity_diagnostics.push({round,stage:'before',value:before});
 if(!before?.author_verified||before.handle.toLowerCase()!==identity.handle.toLowerCase()||before.postId!==identity.postId)
  fail('EXPIRY_PREFLIGHT_IDENTITY_UNCONFIRMED');
 // An unsuccessful geometry reading cannot reset the identity/time guard on
 // the next attempt. This is negative evidence only, never permission to skip.
 if(baseline.value===null)baseline.value=before;
 else if(JSON.stringify(baseline.value)!==JSON.stringify(before))fail('EXPIRY_PREFLIGHT_IDENTITY_OR_TIME_CHANGED');
 const descriptor={identity,displayName:before.display_name};
 const name=await readTarget(page,article,{...descriptor,field:'author_name'},path.join(dir,`reading-${round}-author-name.png`),trace);
 const account=await readTarget(page,article,{...descriptor,field:'author_handle'},path.join(dir,`reading-${round}-author-handle.png`),trace);
 if(name.text!==before.display_name||account.text.toLowerCase()!=='@'+identity.handle.toLowerCase())
  fail('EXPIRY_PREFLIGHT_IDENTITY_CHANGED');
 const timestamp=await readTarget(page,article,{...descriptor,field:'timestamp'},path.join(dir,`reading-${round}-timestamp.png`),trace);
 const u=new URL(timestamp.href);if(u.pathname.replace(/\/$/,'').toLowerCase()!==
  `/${identity.handle}/status/${identity.postId}`.toLowerCase())fail('EXPIRY_PREFLIGHT_PERMALINK_CHANGED');
 const timezone=await page.evaluate(()=>Intl.DateTimeFormat().resolvedOptions().timeZone);
 const time=expiryTimeInterval({kind:timestamp.tag==='TIME'?'native_time_datetime':'visible_year_minute',
  datetime:timestamp.datetime,label:timestamp.text,browser_timezone:timezone});
 if(!time)fail('EXPIRY_PREFLIGHT_FULL_TIME_UNREADABLE');
 const after=await article.evaluate(inspectArticleElement);
 trace.identity_diagnostics.push({round,stage:'after',value:after});
 if(JSON.stringify(before)!==JSON.stringify(after))fail('EXPIRY_PREFLIGHT_IDENTITY_OR_TIME_CHANGED');
 return {post_id:identity.postId,handle:identity.handle,display_name:name.text,time,browser_timezone:timezone,
  author_name:name,author_handle:account,timestamp,observed_at:new Date().toISOString()};
}

async function strictAttempt(page,identity,evidenceDir,notBefore,index,baseline){
 const result={version:'x-expiry-preflight-v1',purpose:'skip_expired_only',complete_source:false,
  source_attested:false,publication_authorized:false,human_review:false,official_api:false,
  post_id:identity.postId,handle:identity.handle,not_before:notBefore,decision:'not_skipped',observations:[],
  attempt_index:index,target_diagnostics:[],identity_diagnostics:[],retry_eligible:false};
 try{
  if(!Number.isFinite(notBefore)||notBefore<0)fail('EXPIRY_PREFLIGHT_INVALID_CUTOFF');
  if(!notBefore)fail('EXPIRY_PREFLIGHT_CUTOFF_DISABLED');
  const first=await readObservation(page,identity,evidenceDir,1,result,baseline);result.observations.push(first);
  if(first.time.upper_bound_exclusive_epoch>notBefore)fail('EXPIRY_PREFLIGHT_NOT_CERTAINLY_EXPIRED');
  await page.waitForTimeout(50);
  const second=await readObservation(page,identity,evidenceDir,2,result,baseline);result.observations.push(second);
  const identityChanged=first.post_id!==second.post_id||first.handle!==second.handle||first.display_name!==second.display_name||
   first.time.kind!==second.time.kind||first.time.raw_value!==second.time.raw_value||
   first.time.upper_bound_exclusive_epoch!==second.time.upper_bound_exclusive_epoch;
  const geometryChanged=['author_name','author_handle','timestamp'].some(field=>
    JSON.stringify(first[field].rect)!==JSON.stringify(second[field].rect)||
    JSON.stringify(first[field].viewport)!==JSON.stringify(second[field].viewport)||
    first[field].scroll_x!==second[field].scroll_x||first[field].scroll_y!==second[field].scroll_y);
  if(identityChanged||geometryChanged){
   result.confirmation_comparison={identity_or_time_changed:identityChanged,geometry_changed:geometryChanged,
    retry_eligible:!identityChanged&&geometryChanged};
   fail('EXPIRY_PREFLIGHT_CONFIRMATIONS_DIFFER',{retryEligible:!identityChanged&&geometryChanged});
  }
  if(second.time.upper_bound_exclusive_epoch>notBefore)fail('EXPIRY_PREFLIGHT_NOT_CERTAINLY_EXPIRED');
  result.decision='skipped_expired';result.reason='entire_observed_time_interval_precedes_cutoff';
 }catch(e){
  result.reason=e.code||'EXPIRY_PREFLIGHT_UNREADABLE';
  result.error_name=e.name||'Error';
  // Public DOM/locator diagnostics only; no page storage, application state or
  // credentials are read. Keep this bounded so inconclusive probes are usable.
  result.diagnostic=String(e.message||result.reason).slice(0,700);
  result.retry_eligible=RETRY_CODES.has(result.reason)&&e.retry_eligible===true;
 }
 result.checked_at=new Date().toISOString();const file=path.join(evidenceDir,'expiry-preflight.json');
 await fs.writeFile(file,JSON.stringify(result,null,2),{flag:'wx',mode:0o600});
 return {...result,evidence_file:file};
}

export async function preflightExpiredTweet(page,identity,evidenceDir,{notBefore}={}){
 await fs.mkdir(evidenceDir,{recursive:true,mode:0o700});
 const file=path.join(evidenceDir,'expiry-preflight.json');
 try{await fs.lstat(file);fail('EXPIRY_PREFLIGHT_EVIDENCE_ALREADY_EXISTS');}
 catch(e){if(e.code!=='ENOENT')throw e;}
 const attempts=[],baseline={value:null};
 for(let index=1;index<=3;index++){
  const dir=path.join(evidenceDir,`attempt-${index}`);await fs.mkdir(dir,{mode:0o700});
  const result=await strictAttempt(page,identity,dir,notBefore,index,baseline);attempts.push(result);
  if(result.decision==='skipped_expired'||!result.retry_eligible||index===3)break;
  // Native page rendering may settle. No navigation, script/style mutation,
  // dismissal, hidden data, media download or changed source is introduced.
  await page.waitForTimeout(75);
 }
 const result={...attempts.at(-1),attempts,retry_policy:{version:'strict-expiry-bounded-retry-v1',
  max_attempts:3,wait_ms:75,eligible_error_codes:[...RETRY_CODES],geometry_only:true},evidence_file:file};
 await fs.writeFile(file,JSON.stringify(result,null,2),{flag:'wx',mode:0o600});return result;
}
