// Inspect the control and open its native editor once. Never final-send/fill/upload.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';

const hash=b=>crypto.createHash('sha256').update(b).digest('hex');
const canonical=v=>Array.isArray(v)?v.map(canonical):v&&typeof v==='object'?Object.fromEntries(Object.keys(v).sort().map(k=>[k,canonical(v[k])])):v;
const digest=v=>hash(JSON.stringify(canonical(v)));
const norm=v=>String(v||'').replace(/^\u200B/,'').replace(/\s+/g,' ').trim();
const uid='1000000000000000';
const stamp=()=>new Date().toISOString();
function safeURL(value){try{const u=new URL(value);return u.origin+u.pathname;}catch{return '';}}
function dynamicURL(value,id){try{const u=new URL(value);return u.protocol==='https:'&&!u.username&&!u.password&&
 ((u.hostname==='t.bilibili.com'&&u.pathname.replace(/\/$/,'')==='/'+id)||(u.hostname==='www.bilibili.com'&&u.pathname.replace(/\/$/,'')==='/opus/'+id));}catch{return false;}}
function profileUID(value){try{const u=new URL(value);return u.origin==='https://space.bilibili.com'&&!u.username&&!u.password?
 /^\/(\d+)(?:\/dynamic)?\/?$/.exec(u.pathname)?.[1]||null:null;}catch{return null;}}

export function validateRepostSpec(spec){
 const uu=/^[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;
 if(spec.version!=='tweet-native-repost-check-v1'||!['check_job_id','attempt_id','submission_job_id'].every(k=>uu.test(spec[k]||''))||
   spec.target_site_uid!==uid||!/^\d{1,30}$/.test(spec.external_id||'')||spec.url!==`https://t.bilibili.com/${spec.external_id}`||
   !/^https:\/\/x\.com\/(?:example_handle_a|example-channel-b)\/status\/\d{1,30}$/.test(spec.source_url||'')||
   !/^\d{1,30}$/.test(spec.source_id||'')||!spec.source_url.endsWith('/'+spec.source_id)||
   typeof spec.target_name!=='string'||!spec.target_name.trim()||spec.target_name.length>200||
   typeof spec.caption!=='string'||spec.caption.length>10000||!spec.caption.includes(spec.source_url)||
   spec.caption_sha256!==hash(spec.caption)||!Number.isFinite(spec.not_before)||Date.now()/1000<spec.not_before||
   spec.not_before!==spec.submission_recorded_at+300)throw new Error('TWEET_REPOST_SPEC_INVALID');
 return spec;
}

export function isOpenedRepostEditor(d,spec){
 if(d.kind==='bilibili_share_editor_v1'){
  return d.classes?.includes('bili-dyn-share-publishing')&&d.title==='分享到动态'&&d.editors===1&&
   d.editor_controls?.length===1&&d.editor_controls[0].tag==='DIV'&&d.editor_controls[0].contenteditable==='true'&&
   d.editor_controls[0].classes?.includes('bili-rich-textarea__inner')&&
   d.reference?.author_text==='@'+spec.target_name&&norm(d.reference.text||'').includes(norm(spec.caption))&&
   (d.reference.text||'').includes(spec.source_url)&&d.final_controls?.length===1&&
   d.final_controls[0].tag==='DIV'&&d.final_controls[0].text==='发布'&&
   d.final_controls[0].classes?.includes('bili-dyn-share-publishing__action')&&d.final_controls[0].classes?.includes('launcher');
 }
 return d.editors>0&&d.final_controls?.length>0&&/转发/.test(d.text);
}

export async function collectRepostResponseUI(page){
 return page.evaluate(()=>{
  const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';};
  const dialogs=[...document.querySelectorAll('[role="dialog"],.bili-dyn-forward,.bili-dyn-forward-popup,div.bili-dyn-share-publishing')].filter(visible).slice(0,10).map(e=>{
   const share=e.matches('div.bili-dyn-share-publishing');
   const editor_controls=share?[...e.querySelectorAll('.bili-dyn-share-publishing__input .bili-rich-textarea__inner[contenteditable="true"]')].filter(visible).map(a=>({tag:a.tagName,classes:[...a.classList],contenteditable:a.getAttribute('contenteditable')})):[];
   const finals=share?e.querySelectorAll('.bili-dyn-share-publishing__control div.bili-dyn-share-publishing__action.launcher'):e.querySelectorAll('button,[role="button"]');
   return {tag:e.tagName,role:e.getAttribute('role'),classes:[...e.classList],text:e.innerText.slice(0,10000),kind:share?'bilibili_share_editor_v1':'legacy_repost_dialog',
    title:share?e.querySelector('.bili-dyn-share-publishing__title')?.innerText.trim():null,
    editors:share?editor_controls.length:[...e.querySelectorAll('textarea,[contenteditable="true"],[role="textbox"]')].filter(visible).length,editor_controls,
    reference:share?{author_text:e.querySelector('.bili-dyn-share-publishing__reference__author__name')?.innerText.trim(),
     text:e.querySelector('.bili-dyn-share-publishing__reference .bili-rich-text__content')?.innerText||''}:null,
    final_controls:[...finals].filter(a=>visible(a)&&(share?a.innerText.trim()==='发布':/^(?:转发|发送)$/.test(a.innerText.trim()))).map(a=>({tag:a.tagName,text:a.innerText.trim(),disabled:Boolean(a.disabled)||a.hasAttribute('disabled')||a.getAttribute('aria-disabled')==='true',classes:[...a.classList]}))};
  });
  const messages=[...document.querySelectorAll('[role="alert"],.bili-toast,.bili-toast-content,.bili-message,.bili-dyn-toast')].filter(visible).map(e=>e.innerText.trim()).filter(Boolean).slice(0,20);
  return {dialogs,messages};
 });
}

async function protection(page){
 return page.evaluate(()=>{
  const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';};
  const markers=[...document.querySelectorAll('[role="dialog"],[role="alertdialog"],[class*="geetest"],[class*="captcha"]')]
   .filter(e=>e.tagName!=='BODY'&&visible(e)&&(/人机验证|安全验证|验证码|captcha|geetest/i.test(e.innerText+' '+e.className)))
   .slice(0,16).map(e=>({tag:e.tagName,role:e.getAttribute('role'),classes:[...e.classList].slice(0,12),text:e.innerText.slice(0,1000)}));
  const frames=[...document.querySelectorAll('iframe[src]')].filter(visible).flatMap(e=>{try{const u=new URL(e.src);return /captcha|geetest|verify/i.test(u.origin+u.pathname)?[{origin:u.origin,path:u.pathname}]:[];}catch{return [];}}).slice(0,16);
  return {visible_controls:markers,challenge_frames:frames,blocked:markers.length>0||frames.length>0};
 });
}

export async function observeRepostControl(page,spec,folder,{accountURL='https://t.bilibili.com/',readinessMs=20000,requestSHA=digest(spec),intentFile=path.join(folder,'dialog-open-intent.json')}={}){
 validateRepostSpec(spec);await fs.mkdir(folder,{recursive:true,mode:0o700});
 if(await fs.realpath(folder)!==path.resolve(folder))throw new Error('EVIDENCE_PATH_INVALID');
 let response,status='ui_unrecognized',account=null,control=[],binding={exact_dynamic:false,caption:false,source_url:false,author_uid:false,author_name:false},guards=null;
 let observedText='',httpStatus=null,clicks=0,authorClicks=0,authorIdentity=null,scrolled=false,beforeScreenshot=null;
 let probe={control_click_attempted:false,final_send_clicks:0,outcome:'not_attempted',dialogs:[],messages:[]};
 async function image(name,target=page){const bytes=await target.screenshot({fullPage:false,timeout:10000}),file=path.join(folder,name);const h=await fs.open(file,'wx',0o600);try{await h.writeFile(bytes);await h.sync();}finally{await h.close();}return {file,bytes:bytes.length,sha256:hash(bytes)};}
 async function finish(){
  const png=await page.screenshot({fullPage:false,timeout:10000});const file=path.join(folder,'observed.png');
  await fs.writeFile(file,png,{mode:0o600});
  const body={version:'tweet-native-repost-observation-v2',request_sha256:requestSHA,check_job_id:spec.check_job_id,attempt_id:spec.attempt_id,
   external_id:spec.external_id,target_site_uid:uid,requested_url:spec.url,final_url:safeURL(page.url()),observed_at:stamp(),
   account_uid:account,status,content_binding:binding,controls:control,protection:guards,http_status:httpStatus,visible_text:observedText,
   screenshot:{file,bytes:png.length,sha256:hash(png)},before_probe_screenshot:beforeScreenshot,readonly_scroll_performed:scrolled,functional_probe:probe,
   author_identity:authorIdentity,actions:{clicks:clicks+authorClicks,author_navigation_clicks:authorClicks,repost_dialog_clicks:clicks,
    file_selections:0,fills:0,submits:0},human_review:false,platform_cause_verified:false};
  const raw=Buffer.from(JSON.stringify(body,null,2));const output=path.join(folder,'observation.json');
  for(const [name,bytes] of [[file,png],[output,raw]]){const h=await fs.open(name,name===file?'r+':'wx',0o600);try{if(name!==file)await h.writeFile(bytes);await h.sync();}finally{await h.close();}}
  if(process.platform!=='win32'){const dir=await fs.open(folder,'r');try{await dir.sync();}finally{await dir.close();}}
  return {file:output,bytes:raw.length,sha256:hash(raw)};
 }
 async function ready(){
  const end=Date.now()+readinessMs;
  while(Date.now()<end){
   if(await page.locator('a.header-entry-mini:visible').count()||await page.locator('.bili-dyn-item:visible').count()||
     await page.getByText('登录',{exact:true}).first().isVisible().catch(()=>false))return;
   await page.waitForTimeout(150);
  }
 }
 response=await page.goto(accountURL,{waitUntil:'domcontentloaded',timeout:30000});await ready();guards=await protection(page);
 if(guards.blocked){status='protection_blocked';return finish();}
 if(page.url().includes('passport.bilibili.com')){status='auth_required';return finish();}
 const avatar=page.locator('a.header-entry-mini:visible');
 if(await avatar.count()!==1){status=await page.getByText('登录',{exact:true}).first().isVisible().catch(()=>false)?'auth_required':'ui_unrecognized';return finish();}
 const href=await avatar.getAttribute('href');try{const u=new URL(href,page.url());if(u.protocol==='https:'&&u.hostname==='space.bilibili.com')account=/^\/(\d+)\/?$/.exec(u.pathname)?.[1]||null;}catch{}
 if(account!==uid){status='wrong_account';return finish();}
 response=await page.goto(spec.url,{waitUntil:'domcontentloaded',timeout:30000});httpStatus=response?.status()??null;
 await ready();guards=await protection(page);
 if(guards.blocked){status='protection_blocked';return finish();}
 if(page.url().includes('passport.bilibili.com')||await page.getByText('登录',{exact:true}).first().isVisible().catch(()=>false)){status='auth_required';return finish();}
 if(httpStatus===401||httpStatus===403){status='http_denied';return finish();}
 binding.exact_dynamic=dynamicURL(page.url(),spec.external_id);
 const cards=page.locator('.bili-dyn-item:visible').filter({hasText:spec.source_url});
 try{await cards.first().waitFor({state:'visible',timeout:readinessMs});}catch{}
 if(await cards.count()!==1||!binding.exact_dynamic){status=await cards.count()>1?'ui_unrecognized':'dynamic_not_found';return finish();}
 const card=cards;observedText=(await card.innerText()).slice(0,16000);
 binding.caption=norm(observedText).includes(norm(spec.caption));binding.source_url=observedText.includes(spec.source_url);
 binding.author_name=(await Promise.all((await card.getByText(spec.target_name,{exact:true}).all()).map(e=>e.isVisible()))).filter(Boolean).length===1;
 if(!binding.caption||!binding.source_url||!binding.author_name){status='ui_unrecognized';return finish();}
 const named=(await card.getByText(spec.target_name,{exact:true}).all());const namedVisible=[];
 for(const e of named)if(await e.isVisible())namedVisible.push(e);
 if(namedVisible.length!==1){status='ui_unrecognized';return finish();}
 const namedHref=await namedVisible[0].evaluate(e=>{const a=e.closest('a[href]');if(!a)return null;const r=a.getBoundingClientRect();return r.width>0&&r.height>0?a.href:null;});
 if(namedHref&&profileUID(namedHref)===uid){
  binding.author_uid=true;authorIdentity={version:'tweet-native-author-identity-v2',method:'visible_named_author_anchor',
   site_uid:uid,profile_url:safeURL(namedHref),source_dynamic_url:safeURL(page.url()),observed_at:stamp()};
 }else{
  // The actual details page uses a JS SPAN, not an author href. A native
  // read-only profile navigation supplies UID evidence; the logged-in UID
  // and displayed nickname never stand in for the post author's identity.
  const author=card.locator('.bili-dyn-item__header .bili-dyn-title__text:visible').filter({hasText:spec.target_name});
  if(await author.count()!==1||(await author.innerText()).trim()!==spec.target_name){status='ui_unrecognized';return finish();}
  const authorTarget=await author.evaluate(e=>({tag:e.tagName,classes:[...e.classList],text:e.innerText.trim()}));
  authorIdentity={version:'tweet-native-author-identity-v2',method:'native_author_profile_navigation',
   source_dynamic_url:safeURL(page.url()),clicked_element:authorTarget,site_uid:null,profile_url:null,
   observed_at:null,return_binding_verified:false};
  const authorIntent=path.join(path.dirname(intentFile),'author-navigation-intent.json');
  await fs.mkdir(path.dirname(authorIntent),{recursive:true,mode:0o700});
  try{const h=await fs.open(authorIntent,'wx',0o600);try{await h.writeFile(JSON.stringify({version:'tweet-author-readonly-navigation-intent-v2',
   check_job_id:spec.check_job_id,attempt_id:spec.attempt_id,external_id:spec.external_id,request_sha256:requestSHA,
   created_at:stamp(),target:authorTarget,expected_site_uid:uid,final_send_authorized:false}));await h.sync();}finally{await h.close();}
   if(process.platform!=='win32'){const d=await fs.open(path.dirname(authorIntent),'r');try{await d.sync();}finally{await d.close();}}
  }catch(e){if(e.code!=='EEXIST')throw e;status='author_navigation_unconfirmed';return finish();}
  guards=await protection(page);if(guards.blocked){status='protection_blocked';return finish();}
  const beforePages=page.context().pages(),beforeURL=page.url();let profile;
  authorClicks=1;
  try{
   await author.click({timeout:8000});
   const end=Date.now()+20000;
   while(Date.now()<end&&!page.context().pages().some(p=>!beforePages.includes(p))&&page.url()===beforeURL)await page.waitForTimeout(100);
   const opened=page.context().pages().filter(p=>!beforePages.includes(p));
   if(opened.length>1||!opened.length&&page.url()===beforeURL){status='author_navigation_unconfirmed';return finish();}
   profile=opened[0]||page;await profile.waitForLoadState('domcontentloaded',{timeout:20000});
   authorIdentity.profile_url=safeURL(profile.url());authorIdentity.site_uid=profileUID(profile.url());authorIdentity.observed_at=stamp();
   authorIdentity.profile_screenshot=await image('author-profile.png',profile);
   authorIdentity.profile_protection=await protection(profile);
   if(authorIdentity.profile_protection.blocked){status='protection_blocked';return finish();}
   if(authorIdentity.site_uid!==uid){status='author_identity_mismatch';return finish();}
   if(profile!==page)await profile.close();
   await page.goto(spec.url,{waitUntil:'domcontentloaded',timeout:30000});await ready();
   try{await card.waitFor({state:'visible',timeout:readinessMs});}catch{}
   guards=await protection(page);if(guards.blocked){status='protection_blocked';return finish();}
   observedText=await card.innerText();
   const authorCount=(await Promise.all((await card.getByText(spec.target_name,{exact:true}).all()).map(e=>e.isVisible()))).filter(Boolean).length;
   if(await card.count()!==1||!dynamicURL(page.url(),spec.external_id)||!norm(observedText).includes(norm(spec.caption))||
     !observedText.includes(spec.source_url)||authorCount!==1){status='author_navigation_unconfirmed';return finish();}
   authorIdentity.returned_dynamic_url=safeURL(page.url());authorIdentity.return_binding_verified=true;binding.author_uid=true;
  }catch{status='author_navigation_unconfirmed';return finish();}
 }
 if(!Object.values(binding).every(v=>v===true)){status='ui_unrecognized';return finish();}
 // The known native dynamic-action class plus explicit accessible repost names.
 // Unrecognized/ambiguous layouts produce evidence, never a guessed decision.
 const inventory=await card.evaluate(root=>{
  const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';};
  const named=e=>[e.getAttribute('aria-label'),e.getAttribute('title'),e.innerText].some(t=>/^转发(?:\s*\d+(?:\.\d+)?[万亿]?)?$/.test((t||'').trim()));
  const all=[...root.querySelectorAll('.bili-dyn-action,button,[role="button"]')],candidates=all.filter(e=>visible(e)&&named(e));
  const selected=candidates.filter(e=>!candidates.some(p=>p!==e&&p.contains(e)));let scrolled=false;
  const bars=[...document.querySelectorAll('.side-toolbar')].filter(visible);
  let sidebar=null;
  if(bars.length){
   const box=bars.length===1?[...bars[0].children].filter(e=>e.matches('div.side-toolbar__box')):[];
   const part=box.length===1?[...box[0].children].filter(e=>e.matches('div.side-toolbar__action.forward')&&visible(e)):[];
   if(bars.length!==1||box.length!==1||part.length!==1||
      box[0].querySelectorAll(':scope > .side-toolbar__action.like').length!==1||
      box[0].querySelectorAll(':scope > .side-toolbar__action.comment').length!==1||selected.length)return {ambiguous:true,scrolled:false,controls:[]};
   sidebar=part[0];selected.push(sidebar);
  }
  if(selected.length===1){const e=selected[0],r=e.getBoundingClientRect();if(r.top<0||r.left<0||r.bottom>innerHeight||r.right>innerWidth){e.scrollIntoView({block:'center',inline:'nearest',behavior:'instant'});scrolled=true;}}
  return {scrolled,controls:selected.map(e=>{
   const limit=e===sidebar?bars[0]:root;
   const ancestors=[];let parent=e;for(let n=0;n<4&&parent&&limit.contains(parent);n++,parent=parent.parentElement)ancestors.push({tag:parent.tagName,role:parent.getAttribute('role'),classes:[...parent.classList],aria_disabled:parent.getAttribute('aria-disabled'),disabled_attribute:parent.hasAttribute('disabled'),pointer_events:getComputedStyle(parent).pointerEvents});
   const disabled=Boolean(e.disabled)||ancestors.some(a=>a.disabled_attribute||a.aria_disabled==='true'||a.pointer_events==='none'||a.classes.some(c=>/^(?:disabled|is-disabled|bili-dyn-action--disabled)$/.test(c)));
   const r=e.getBoundingClientRect();return {native_index:e===sidebar?0:all.indexOf(e),surface:e===sidebar?'detail_sidebar_forward_v2':'card_repost_action_v1',
    tag:e.tagName,role:e.getAttribute('role'),text:e.innerText.slice(0,1000),classes:[...e.classList],visible:true,disabled,
    native_post_action:(e===sidebar||e.matches('div.bili-dyn-action'))&&!e.closest('[role="dialog"],.bili-dyn-forward,.bili-dyn-forward-popup,form'),
    in_viewport:r.top>=0&&r.left>=0&&r.bottom<=innerHeight&&r.right<=innerWidth,attributes:ancestors};
  })};
 });
 control=inventory.controls;scrolled=inventory.scrolled;
 status=inventory.ambiguous?'ui_unrecognized':control.length===0?'not_found':control.length!==1?'ui_unrecognized':control[0].disabled?'disabled':'enabled_unverified';
 if(status!=='enabled_unverified'||!control[0].in_viewport||!control[0].native_post_action)return finish();
 async function responseUI(){return collectRepostResponseUI(page);}
 const before=await responseUI();probe.before_ui=before;
 if(before.dialogs.length){probe.outcome='preexisting_dialog_no_click';return finish();}
 beforeScreenshot=await image('before-probe.png');guards=await protection(page);
 if(guards.blocked){status='protection_blocked';return finish();}
 await fs.mkdir(path.dirname(intentFile),{recursive:true,mode:0o700});
 try{const h=await fs.open(intentFile,'wx',0o600);try{await h.writeFile(JSON.stringify({version:'tweet-repost-dialog-open-intent-v1',check_job_id:spec.check_job_id,attempt_id:spec.attempt_id,
  request_sha256:requestSHA,external_id:spec.external_id,created_at:stamp(),control:control[0],final_send_authorized:false,human_review:false}));await h.sync();}finally{await h.close();}
  if(process.platform!=='win32'){const d=await fs.open(path.dirname(intentFile),'r');try{await d.sync();}finally{await d.close();}}
 }catch(error){if(error.code!=='EEXIST')throw error;status='probe_unconfirmed';probe.outcome='previous_probe_intent_retained';return finish();}
 const target=control[0].surface==='detail_sidebar_forward_v2'?page.locator('.side-toolbar > .side-toolbar__box > div.side-toolbar__action.forward:visible'):
  card.locator('.bili-dyn-action,button,[role="button"]').nth(control[0].native_index);
 if(await target.count()!==1){status='probe_unconfirmed';return finish();}
 const same=await target.evaluate((e,old)=>(old.surface==='detail_sidebar_forward_v2'?
  e.matches('div.side-toolbar__action.forward')&&e.parentElement.matches('div.side-toolbar__box')&&e.parentElement.parentElement.matches('div.side-toolbar'):
  e.matches('div.bili-dyn-action'))&&!e.closest('[role="dialog"],.bili-dyn-forward,.bili-dyn-forward-popup,form')&&e.tagName===old.tag&&e.innerText.slice(0,1000)===old.text&&JSON.stringify([...e.classList])===JSON.stringify(old.classes)&&!e.disabled&&!e.hasAttribute('disabled')&&e.getAttribute('aria-disabled')!=='true',control[0]);
 guards=await protection(page);
 if(!same||!dynamicURL(page.url(),spec.external_id)||guards.blocked){status=guards.blocked?'protection_blocked':'probe_unconfirmed';return finish();}
 clicks=1;probe.control_click_attempted=true;
 try{await target.click({timeout:8000});}catch{status='probe_unconfirmed';probe.outcome='native_click_outcome_uncertain';return finish();}
 const end=Date.now()+5000;
 while(Date.now()<end){
  guards=await protection(page);const visible=await responseUI();probe.dialogs=visible.dialogs;probe.messages=visible.messages;
  if(guards.blocked){status='protection_blocked';probe.outcome='protection_blocked';return finish();}
  const denied=visible.messages.filter(t=>!before.messages.includes(t)&&/审核|无法转发|不能转发|不支持转发|暂不支持|转发受限|不可转发|仅自己可见|动态已失效/.test(t));
  if(denied.length){status='native_denied';probe.outcome='native_denial';probe.messages=denied;return finish();}
  const opened=visible.dialogs.filter(d=>isOpenedRepostEditor(d,spec)&&!before.dialogs.some(p=>JSON.stringify(p)===JSON.stringify(d)));
  if(opened.length===1){status='dialog_opened';probe.outcome='native_editor_opened_no_send';return finish();}
  await page.waitForTimeout(100);
 }
 status='probe_unconfirmed';probe.outcome='no_recognized_response';
 return finish();
}

async function main(){
 process.umask(0o077);let raw='';for await(const part of process.stdin){raw+=part;if(raw.length>40000)throw new Error('SPEC_TOO_LARGE');}
 const envelope=JSON.parse(raw);
 if(envelope.version!=='tweet-repost-native-request-v1'||typeof envelope.request_json!=='string'||hash(envelope.request_json)!==envelope.request_sha256)throw new Error('REQUEST_BYTES_INVALID');
 const spec=validateRepostSpec(JSON.parse(envelope.request_json)),root=await fs.realpath(process.env.HUB_ROOT||'/opt/lighthouse-hub');
 const folder=path.join(root,'media',spec.check_job_id,'tweet-repost-check-v1',spec.attempt_id);let context,closing;
 async function close(){if(closing)return closing;closing=(async()=>{if(context)await Promise.race([context.close(),new Promise(r=>setTimeout(r,5000))]);})();return closing;}
 process.on('SIGTERM',()=>{close().finally(()=>process.exit(0));});
 try{
  context=await chromium.launchPersistentContext(path.join(root,'data/browser-profiles/bilibili'),{headless:true,chromiumSandbox:true,
   channel:process.env.HUB_BROWSER_CHANNEL||undefined,viewport:{width:1280,height:900},locale:'zh-CN',timezoneId:'Asia/Shanghai'});
  const page=context.pages()[0]||await context.newPage();page.setDefaultTimeout(10000);
  const ref=await observeRepostControl(page,spec,folder,{requestSHA:envelope.request_sha256,intentFile:path.join(root,'media',spec.check_job_id,'tweet-repost-check-v1/dialog-open-intent.json')});process.stdout.write(JSON.stringify(ref));
 }finally{await close();}
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url))main().catch(()=>{process.exitCode=1;});
