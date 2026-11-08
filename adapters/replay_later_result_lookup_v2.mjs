// Native read/navigation; V5 additionally observes an ordinary player seek.
// Never chooses a file, edits a contribution, saves or submits.
import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash,randomUUID} from 'node:crypto';
import {inspectVideoProtection} from './bilibili_video_guard.mjs';
import {canonicalDigest} from '../scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs';
import {settleEmptyBodyProtection} from './replay_part_owner_handoff_v5.mjs';

const MEMBER='https://member.bilibili.com',MANAGER='/platform/upload-manager/article',BV=/^BV[A-Za-z0-9]{10}$/;
const ROUTES={'replay-highest-native-browser-manifest-v2':'replay-browser-highest-v2','replay-natural-native-browser-manifest-v3':'replay-browser-natural-v3','replay-part-native-browser-manifest-v5':'replay-browser-part-v5'};
const UUID=/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/,SHA=/^[a-f0-9]{64}$/;
function fail(code){throw Object.assign(new Error(code),{code});}
export function titleDisplayMatches(raw,expected){return typeof raw==='string'&&typeof expected==='string'&&(raw===expected||raw===expected.replace(/ +/g,' '));}
export function publicDescriptionDisplayEvidence(raw,expected,source,metadata_sha256,allowAsciiSpaceCollapse=false){
 if(typeof raw!=='string'||typeof expected!=='string'||raw.length>8000||expected.length>2000||
  !/^https:\/\/www\.youtube\.com\/watch\?v=[A-Za-z0-9_-]{11}$/.test(source||'')||!SHA.test(metadata_sha256||''))return null;
 const prefix=source+'\n',display=expected.replaceAll('\u2060',''),positions=Array.from(expected).flatMap((c,i)=>c==='\u2060'?[i]:[]);
 const variants=[['exact',expected,false,false],['source_prefix_once',prefix+expected,true,false]];
 if(display!==expected)variants.push(['word_joiners_omitted',display,false,true],['source_prefix_once_word_joiners_omitted',prefix+display,true,true]);
 if(allowAsciiSpaceCollapse)for(const [name,text,prefixed,omitted] of [...variants]){const collapsed=text.replace(/ +/g,' ');if(collapsed!==text)variants.push([name+'_ascii_spaces_collapsed',collapsed,prefixed,omitted,true]);}
 const match=variants.find(([,text])=>text===raw);if(!match)return null;
 const sha=text=>createHash('sha256').update(text,'utf8').digest('hex');
 return {version:match[4]?'bilibili-public-description-display-v3':'bilibili-public-description-display-v2',candidate:match[0],metadata_sha256,
  expected_description_sha256:sha(expected),observed_description_sha256:sha(raw),
  expected_codepoints:Array.from(expected).length,observed_codepoints:Array.from(raw).length,
  source_prefix_removed:match[2],omitted_word_joiner_positions:match[3]?positions:[],...(match[4]?{ascii_spaces_collapsed:true}:{})};
}
function bodyOnly(p){
 const e=p?.evidence,c=e?.visible_challenge_controls;
 return p?.blocked===true&&p.error_code==='BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED'&&e?.version==='bilibili-video-protection-guard-v3'&&
  e.inspection_failed!==true&&e.truncated===false&&e.native_mp4_uploaders===0&&e.top_level_authentication===false&&
  e.visible_dialogs?.length===0&&e.challenge_frames?.length===0&&c?.length===1&&c[0].tag==='BODY'&&c[0].role===''&&
  [...c[0].classes].sort().join(',')==='risk-captcha-adapt,risk-captcha-adapt-pc';
}
async function safe(file){
 for(let p=file;p!==path.dirname(p);p=path.dirname(p))if((await fs.lstat(p)).isSymbolicLink())fail('REPLAY_LATER_LOOKUP_PATH');
 if(await fs.realpath(file)!==file)fail('REPLAY_LATER_LOOKUP_PATH');
}
async function write(file,value){
 const raw=Buffer.from(JSON.stringify(value,null,2)+'\n'),h=await fs.open(file,'wx',0o600);
 try{await h.writeFile(raw);await h.sync();}finally{await h.close();}
 if(process.platform!=='win32'){const d=await fs.open(path.dirname(file),'r');try{await d.sync();}finally{await d.close();}}
 return {file,bytes:raw.length,sha256:createHash('sha256').update(raw).digest('hex')};
}
async function screenshot(page,file){
 await page.screenshot({path:file,fullPage:false,timeout:20000});const h=await fs.open(file,'r+');
 try{await h.sync();}finally{await h.close();}
 if(process.platform!=='win32'){const d=await fs.open(path.dirname(file),'r');try{await d.sync();}finally{await d.close();}}
 const raw=await fs.readFile(file);return {file,bytes:raw.length,sha256:createHash('sha256').update(raw).digest('hex')};
}
function videoId(raw){try{const u=new URL(raw);return u.origin==='https://www.bilibili.com'&&!u.search&&!u.hash&&!u.username&&!u.password&&/^\/video\/(BV[A-Za-z0-9]{10})\/?$/.exec(u.pathname)?.[1];}catch{return null;}}
// The actual V5 manager uses protocol-relative href attributes. Keep those
// original attributes as evidence; resolve only this exact official form.
function managerVideoLink(raw){
 const match=typeof raw==='string'&&/^(?:https:)?\/\/www\.bilibili\.com\/video\/(BV[A-Za-z0-9]{10})\/?$/.exec(raw);
 return match&&match[0]===raw?{id:match[1],canonical_url:'https://www.bilibili.com/video/'+match[1]+'/'}:null;
}

// Only native draft links actually present on the account's manager page.
export function replayDraftLink(raw){
 try{
  if(typeof raw!=='string'||!raw||raw.length>2048||raw.trim()!==raw)return null;
  const u=new URL(raw,MEMBER);
  if(u.origin!==MEMBER||u.username||u.password||u.hash||u.pathname!=='/platform/upload/video/frame'||
   [...u.searchParams.keys()].sort().join(',')!=='draftId,type'||u.searchParams.get('type')!=='draft')return null;
  const id=u.searchParams.get('draftId');if(!/^[1-9][0-9]{0,17}$/.test(id||''))return null;
  return {draft_id:id,draft_url:u.href};
 }catch{return null;}
}

async function observeReplayDraft({page,spec,common,output,managerRef,protection,account,budget}){
 const managerPath=()=>{const u=new URL(page.url());if(u.origin!==MEMBER||u.pathname!==MANAGER)fail('REPLAY_DRAFT_MANAGER_ORIGIN');};
 managerPath();await protection(page,MEMBER,MANAGER);
 const tab=page.getByText(/^草稿\s+[0-9]+$/,{exact:true}).filter({visible:true});
 if(await tab.count()!==1)fail('REPLAY_DRAFT_TAB_AMBIGUOUS');
 const count=Number((await tab.innerText()).trim().match(/^草稿\s+([0-9]+)$/)?.[1]);
 if(!Number.isSafeInteger(count)||count<0||count>60)fail('REPLAY_DRAFT_PAGINATION_NOT_COMPLETE');
 const identity=await account({record:true});await protection(page,MEMBER,MANAGER);
 await tab.click({timeout:budget(5000)});
 const section=page.locator('section.bcc-content-manager-article-draft:visible');
 if(count>0)await section.waitFor({state:'visible',timeout:budget(15000)});
 managerPath();await protection(page,MEMBER,MANAGER);
 const raw=await page.evaluate(()=>{
  const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';};
  const sections=[...document.querySelectorAll('section.bcc-content-manager-article-draft')].filter(visible);
  const cards=sections.flatMap(s=>[...s.querySelectorAll('section.bcc-cmad-card.card')].filter(visible));
  const link=e=>({text:e.innerText,href:e.getAttribute('href')});
  const text=document.body?.innerText||'';
  return {url:location.href,section_count:sections.length,visible_text:text.slice(0,30000),truncated:text.length>30000||cards.length>60,
   pagination:[...document.querySelectorAll('div.bcc-pagination-total')].filter(visible).map(e=>e.innerText),
   selected_pages:[...document.querySelectorAll('li.bcc-pagination-item.selected > a')].filter(visible).map(e=>e.innerText),
   rows:cards.slice(0,60).map(e=>({titles:[...e.querySelectorAll('div.bcc-cmadc-title-time > p.title')].filter(visible).map(n=>({title:n.getAttribute('title'),links:[...n.querySelectorAll('a[href]')].filter(visible).map(link)})),
    covers:[...e.querySelectorAll('a.bcc-cmadc-cover-box[href]')].filter(visible).map(n=>({href:n.getAttribute('href'),alts:[...n.querySelectorAll('img')].map(i=>i.getAttribute('alt'))})),
    saved_times:[...e.querySelectorAll('div.bcc-cmadc-title-time > p.time')].filter(visible).map(n=>n.innerText)}))};
 });
 const shot=await screenshot(page,path.join(output,'draft-manager.png'));
 const draftManagerRef=await write(path.join(output,'draft-manager.json'),{version:'replay-part-native-draft-manager-v5',...common,
  observed_at:new Date().toISOString(),account_observation:identity,declared_draft_count:count,visible_dom:raw,screenshot:shot,read_only:true});
 if((count>0?raw.section_count!==1:raw.section_count>1)||raw.truncated||raw.rows.length!==count||raw.rows.length>60)fail('REPLAY_DRAFT_PAGINATION_NOT_COMPLETE');
 if(count>0&&(raw.pagination.length!==1||!/^\s*共\s*1\s*页\s*\/\s*[0-9]+\s*个\s*[，,]?\s*$/.test(raw.pagination[0])||
  Number(raw.pagination[0].match(/\/\s*([0-9]+)/)?.[1])!==count||raw.selected_pages.length!==1||raw.selected_pages[0].trim()!=='1'))fail('REPLAY_DRAFT_PAGINATION_NOT_COMPLETE');
 const normalized=raw.rows.map(row=>{
  if(row.titles.length!==1||row.titles[0].links.length!==1||row.covers.length!==1||row.covers[0].alts.length!==1||row.saved_times.length!==1)fail('REPLAY_DRAFT_CARD_AMBIGUOUS');
  const title=row.titles[0],link=title.links[0],a=replayDraftLink(link.href),b=replayDraftLink(row.covers[0].href);
  if(!a||!b||a.draft_id!==b.draft_id||a.draft_url!==b.draft_url||title.title!==link.text||row.covers[0].alts[0]!==link.text)fail('REPLAY_DRAFT_CARD_IDENTITY_CHANGED');
  return {...a,title:link.text,saved_at_text:row.saved_times[0],row};
 });
 if(new Set(normalized.map(x=>x.draft_id)).size!==normalized.length)fail('REPLAY_DRAFT_CARD_AMBIGUOUS');
 const matches=normalized.filter(x=>x.title===spec.metadata.title);
 if(matches.length>1)fail('REPLAY_DRAFT_CARD_AMBIGUOUS');
 if(!matches.length)return {state:'draft_not_observed',manager_observation:managerRef,draft_manager_observation:draftManagerRef,reconcile_before_retry:true};
 const selected=matches[0];await protection(page,MEMBER,MANAGER);
 // Navigate only the exact native href; never click edit/delete/save/submit.
 await page.goto(selected.draft_url,{waitUntil:'domcontentloaded',timeout:budget(30000)});
 const start=performance.now(),end=start+15000,polls=[];let editor=null,stable=0,ready=false,readinessError=null,lastProtection=null;
 try{
  do{
   budget(1);const current=replayDraftLink(page.url());
   if(!current||current.draft_id!==selected.draft_id||current.draft_url!==selected.draft_url)fail('REPLAY_DRAFT_EDITOR_IDENTITY_CHANGED');
   // An editor skeleton may have sidebar text plus only the BODY marker.
   // This passive wait grants no interaction or readiness while still blocked.
   lastProtection=await protection(page,MEMBER,'/platform/upload/video/frame',{passive:true});
   const values=await page.evaluate(()=>{
    const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';};
    const titles=[...document.querySelectorAll('input[placeholder="请输入稿件标题"]')].filter(visible);
    const sources=[...document.querySelectorAll('input[placeholder="转载视频请注明来源、时间、地点(例：转自https://www.xxxx.com/yyyy)，注明来源会更快地通过审核哦"]')].filter(visible);
    return {url:location.href,title_count:titles.length,source_count:sources.length,
     title:titles.length===1?titles[0].value.slice(0,4096):null,source_url:sources.length===1?sources[0].value.slice(0,4096):null};
   });
   const actual=replayDraftLink(values.url);
   const matches=!lastProtection.blocked&&values.title_count===1&&values.source_count===1&&values.title===spec.metadata.title&&values.source_url===spec.source_url;
   stable=matches?stable+1:0;polls.push({...values,elapsed_ms:Math.round(performance.now()-start),guard_blocked:lastProtection.blocked,
    body_marker_only:bodyOnly(lastProtection),exact_values_match:matches,consecutive_matches:stable});
   if(!actual||actual.draft_id!==selected.draft_id||actual.draft_url!==selected.draft_url||values.title_count>1||values.source_count>1)fail('REPLAY_DRAFT_EDITOR_IDENTITY_CHANGED');
   if(stable>=2&&performance.now()<=end){editor={url:values.url,title:values.title,source_url:values.source_url};ready=true;break;}
   const left=end-performance.now();if(left<=0)break;await page.waitForTimeout(Math.min(200,left));
  }while(performance.now()<end);
  if(!ready)fail(lastProtection?.blocked&&bodyOnly(lastProtection)?'REPLAY_DRAFT_EDITOR_NOT_READY':'REPLAY_DRAFT_EDITOR_IDENTITY_CHANGED');
 }catch(error){readinessError=error;}
 // Preserve actual visible values even when hydration never becomes ready.
 // Diagnostic failure must not hide a genuine protection/identity failure.
 let readinessRef=null;
 try{
  const image=await screenshot(page,path.join(output,'draft-editor-readiness.png'));
  readinessRef=await write(path.join(output,'draft-editor-readiness.json'),{version:'replay-part-draft-editor-readiness-v1',...common,
   observed_at:new Date().toISOString(),draft_id:selected.draft_id,draft_url:selected.draft_url,maximum_wait_ms:15000,required_consecutive_matches:2,
   elapsed_ms:Math.round(performance.now()-start),actual_url:page.url(),ready,polls,error_code:readinessError?.code||null,screenshot:image,read_only:true});
 }catch(error){if(!readinessError)throw error;}
 if(readinessError)throw readinessError;
 const afterIdentity=await account({record:true});await protection(page,MEMBER,'/platform/upload/video/frame');
 const editorShot=await screenshot(page,path.join(output,'draft-editor.png'));
 const editorRef=await write(path.join(output,'draft-editor.json'),{version:'replay-part-native-draft-editor-v5',...common,observed_at:new Date().toISOString(),
  account_observation:afterIdentity,visible_dom:editor,screenshot:editorShot,readiness_observation:readinessRef,read_only:true});
 const observation={version:'replay-part-native-draft-observation-v5',...common,state:'draft_found',observed_at:new Date().toISOString(),
  draft_id:selected.draft_id,draft_url:selected.draft_url,saved_at_text:selected.saved_at_text,manager_observation:managerRef,
  draft_manager_observation:draftManagerRef,draft_editor_observation:editorRef,public_accessible:false,server_file_hash_verified:false,
  human_review:false,read_only:true,navigation_actions:{draft_tab_clicks:1,draft_editor_navigations:1},publication_authorized:false};
 return {state:'draft_found',draft_observation:observation,draft_reference:await write(path.join(output,'draft-observation.json'),observation),reconcile_before_retry:true};
}

export async function observeLaterReplayResult({page,context,spec,root,known_external_id=null,publicTimeoutMs=30000,onProtection=null}){
 if(!spec||!ROUTES[spec.version]||![spec.job_id,spec.attempt_id].every(x=>UUID.test(x||''))||
  typeof spec.target_site_uid!=='string'||!/^\d{1,30}$/.test(spec.target_site_uid)||!/^[\w-]{11}$/.test(spec.source_id||'')||
  spec.source_url!=='https://www.youtube.com/watch?v='+spec.source_id||![spec.metadata_sha256,spec.binding_sha256].every(x=>SHA.test(x||''))||
  !spec.metadata||canonicalDigest(spec.metadata)!==spec.metadata_sha256||typeof spec.metadata.title!=='string'||!spec.metadata.title||
  typeof spec.metadata.description!=='string'||!spec.metadata.description.includes(spec.source_url)||
  !Number.isFinite(spec.duration_seconds)||spec.duration_seconds<=0||spec.duration_seconds>36000||
  known_external_id!==null&&!BV.test(known_external_id))fail('REPLAY_LATER_LOOKUP_ROUTE_UNSUPPORTED');
 if(!Number.isSafeInteger(publicTimeoutMs)||publicTimeoutMs<1||publicTimeoutMs>30000)fail('REPLAY_LATER_LOOKUP_TIMEOUT');
 root=path.resolve(root);const standalone=spec.version==='replay-part-native-browser-manifest-v5';
 const deadline=performance.now()+90000;
 const budget=maximum=>{const left=deadline-performance.now();if(left<=0)fail('REPLAY_LATER_LOOKUP_TIMEOUT');return Math.max(1,Math.min(maximum,left));};
 const base=path.join(root,'media',spec.job_id,ROUTES[spec.version],spec.attempt_id);await safe(base);
 const parent=path.join(base,'later-lookups-v2');await fs.mkdir(parent,{mode:0o700,recursive:true});await safe(parent);
 const output=path.join(parent,randomUUID());await fs.mkdir(output,{mode:0o700});let choosers=0;
 const onChooser=()=>{choosers++;};page.on('filechooser',onChooser);
 const common={job_id:spec.job_id,attempt_id:spec.attempt_id,site_uid:spec.target_site_uid,source_id:spec.source_id,
  source_url:spec.source_url,metadata_sha256:spec.metadata_sha256,binding_sha256:spec.binding_sha256,
  ...(spec.version==='replay-part-native-browser-manifest-v5'?{parent_job_id:spec.parent_job_id,part_index:spec.part_index,part_count:spec.part_count,
   part_idempotency_key:spec.part_idempotency_key,whole_plan_sha256:spec.whole_plan_sha256,parts_manifest_sha256:spec.parts_manifest_sha256,
   artifact_sha256:spec.artifact_sha256,file_bytes:spec.file_bytes,policy_sha256:spec.policy_sha256}:{}),
  file_selections:0,save_clicks:0,submit_clicks:0,player_seeks:0,source_or_media_modified:false};
 async function account({record=false}={}){
  const p=await context.newPage();try{
   await p.goto('https://t.bilibili.com/',{waitUntil:'domcontentloaded',timeout:budget(45000)});
   const a=p.locator('a.header-entry-mini:visible');await a.waitFor({state:'visible',timeout:budget(30000)});
   if(await a.count()!==1)fail('REPLAY_BROWSER_UID_AMBIGUOUS');
   const href=await a.getAttribute('href'),u=new URL(href,'https://t.bilibili.com/');
   const uid=u.origin==='https://space.bilibili.com'&&!u.username&&!u.password&&/^\/(\d+)\/?$/.exec(u.pathname)?.[1];
   if(uid!==spec.target_site_uid)fail('TARGET_ACCOUNT_IDENTITY_MISMATCH');return record?{site_uid:uid,profile_href:href,url:p.url(),observed_at:new Date().toISOString()}:uid;
  }finally{await p.close();}
 }
 async function protection(target,origin,pathname,{passive=false,publicIdentity=false}={}){
  const u=new URL(target.url());if(u.origin!==origin||u.pathname!==pathname&&u.pathname!==pathname+'/'||choosers)fail('REPLAY_LATER_LOOKUP_ORIGIN_OR_CHOOSER');
  let p=await inspectVideoProtection(target),readiness_observation=null;
  if(standalone&&p.blocked){const settled=await settleEmptyBodyProtection({page:target,protection:p,output:base,spec});p=settled.protection;readiness_observation=settled.readiness_observation;
   const current=new URL(target.url());if(current.origin!==origin||current.pathname!==pathname&&current.pathname!==pathname+'/'||choosers)fail('REPLAY_LATER_LOOKUP_ORIGIN_OR_CHOOSER');
   // A BODY-only loading timeout is not a visible verification challenge.
   // Let the existing bounded passive readiness loop continue. Any real
   // challenge/auth/dialog, or any later interaction, still fails closed.
   if(p.blocked&&readiness_observation&&!(passive&&bodyOnly(p))){if(onProtection)await onProtection({page:target,protection:p,readiness_observation});fail(p.error_code||'REPLAY_LATER_LOOKUP_PROTECTION');}}
  if(p.blocked&&!(bodyOnly(p)&&(passive||publicIdentity))){if(standalone&&onProtection)await onProtection({page:target,protection:p,readiness_observation});fail(p.error_code||'REPLAY_LATER_LOOKUP_PROTECTION');}
  return p;
 }
 try{
  await page.goto(MEMBER+MANAGER,{waitUntil:'domcontentloaded',timeout:budget(45000)});
  const end=Date.now()+20000,polls=[];let ready=false;
  do{
   budget(1);
   const p=await protection(page,MEMBER,MANAGER,{passive:true});
   const tabs=await page.getByText(/^全部稿件\s+\d+$/,{exact:true}).filter({visible:true}).count();
   if(tabs>1)fail('REPLAY_LATER_MANAGER_AMBIGUOUS');
   polls.push({all_tabs:tabs,blocked:p.blocked,body_marker_only:bodyOnly(p)});
   if(!p.blocked&&tabs===1){ready=true;break;}
   await page.waitForTimeout(Math.min(200,Math.max(0,end-Date.now())));
  }while(Date.now()<end);
  if(!ready)fail('REPLAY_LATER_MANAGER_NOT_READY');
  await protection(page,MEMBER,MANAGER);await account();await protection(page,MEMBER,MANAGER);
  const manager=await page.evaluate(v5=>{
   const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';};
   const cards=[...document.querySelectorAll('.article-card.clearfix.v2')].filter(visible);
   const link=e=>({tag:e.tagName,class:e.className,text:e.innerText.trim(),href:e.getAttribute('href'),visible:visible(e)});
   return {url:location.origin+location.pathname,visible_text:(document.body?.innerText||'').slice(0,30000),truncated:cards.length>60,
    rows:cards.slice(0,60).map(e=>({class:e.className,...(v5?{row_text:e.innerText.slice(0,6000),row_text_truncated:e.innerText.length>6000}:{}),title_links:[...e.querySelectorAll('a.name.ellipsis')].filter(visible).map(link),
     covers:[...e.querySelectorAll('a.cover-wrp')].filter(visible).map(link),
     dates:[...e.querySelectorAll('.pubdate.is-success .date')].filter(visible).map(n=>({text:n.innerText.trim(),class:n.className,parent_class:n.parentElement.className})),
     durations:[...e.querySelectorAll('a.cover-wrp .duration')].filter(visible).map(n=>n.innerText.trim())}))};
  },standalone);
  if(standalone)for(const r of manager.rows)for(const a of [...r.title_links,...r.covers])a.canonical_url=managerVideoLink(a.href)?.canonical_url||null;
  const allLabels=await page.getByText(/^全部稿件\s+[0-9]+$/,{exact:true}).filter({visible:true}).allTextContents();
  manager.declared_all_count=allLabels.length===1?Number(allLabels[0].trim().match(/^全部稿件\s+([0-9]+)$/)?.[1]):null;
  const titleMatches=raw=>standalone?titleDisplayMatches(raw,spec.metadata.title):raw===spec.metadata.title;
  const matches=manager.rows.filter(r=>r.title_links.some(a=>titleMatches(a.text)));
  let candidate=known_external_id,row=matches.length===1?matches[0]:null;
  // Persist the untouched href and the bounded native card before interpreting
  // it, including a true conflict or a still-transcoding/no-public-link card.
  const managerRef=await write(path.join(output,'manager-row.json'),{version:'replay-native-manager-row-v2',...common,
   observed_at:new Date().toISOString(),visible_dom:manager,selected_row:row,known_external_id,manager_readiness_polls:polls});
  if(manager.truncated||standalone&&manager.rows.some(r=>r.row_text_truncated))fail('REPLAY_LATER_MANAGER_TRUNCATED');
  if(matches.length>1)fail('REPLAY_LATER_MANAGER_AMBIGUOUS');
  if(standalone&&!known_external_id&&matches.length===0){
   if(!Number.isSafeInteger(manager.declared_all_count)||manager.declared_all_count<0||manager.declared_all_count!==manager.rows.length)
    return {state:'public_inventory_incomplete',error_code:'REPLAY_DRAFT_PUBLIC_INVENTORY_INCOMPLETE',manager_observation:managerRef,reconcile_before_retry:true};
   return await observeReplayDraft({page,spec,common,output,managerRef,protection,account,budget});
  }
  function pending(){
   const lines=row.row_text.split(/\r?\n/).map(x=>x.trim()),labels=['转码中','审核中'].filter(x=>lines.includes(x));
   return labels.length?{state:'awaiting_moderation',candidate_external_id:candidate,manager_observation:managerRef,reconcile_before_retry:true,
    result_observation:{version:'replay-part-native-pending-observation-v5',...common,state:'awaiting_moderation',candidate_external_id:candidate,
     observed_at:new Date().toISOString(),native_status_labels:labels,manager_observation:managerRef,public_accessible:false,
     human_review:false,server_file_hash_verified:false}}:null;
  }
  if(matches.length===1){
   if(row.title_links.length!==1||!titleMatches(row.title_links[0].text)||row.covers.length!==1)fail('REPLAY_LATER_MANAGER_AMBIGUOUS');
   const found=standalone?managerVideoLink(row.title_links[0].href)?.id:videoId(row.title_links[0].href),
    cover=standalone?managerVideoLink(row.covers[0].href)?.id:videoId(row.covers[0].href);
   if(candidate&&(!found||cover!==found||candidate!==found)||found&&cover&&cover!==found)fail('REPLAY_LATER_MANAGER_BV_CHANGED');
   if(!found||!cover){
    if(!standalone)fail('REPLAY_LATER_MANAGER_BV_CHANGED');
    const waiting=pending();if(waiting)return waiting;
    return {state:'publication_not_observed',candidate_external_id:null,manager_observation:managerRef,reconcile_before_retry:true};
   }
   candidate=found;
   if(standalone){const waiting=pending();if(waiting)return waiting;}
  }
  if(!candidate||!known_external_id&&(!row||row.dates.length!==1||row.durations.length!==1))
   return {state:'publication_not_observed',candidate_external_id:candidate,manager_observation:managerRef,reconcile_before_retry:true};
  const publicPage=await context.newPage();
  publicPage.on('filechooser',onChooser);
  try{
   await publicPage.goto('https://www.bilibili.com/video/'+candidate+'/',{waitUntil:'domcontentloaded',timeout:budget(45000)});
   async function read(){return publicPage.evaluate(active=>{
    const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';};
    const url=e=>{try{const u=new URL(e.getAttribute('href'),document.baseURI);return u.origin+u.pathname;}catch{return null;}};
    return {url:location.origin+location.pathname,titles:[...document.querySelectorAll('h1')].filter(visible).map(e=>({text:e.innerText.trim(),title:e.getAttribute('title'),class:e.className})),
     owner_candidates:[...document.querySelectorAll('a.up-name')].map(e=>({text:e.innerText.trim(),url:url(e),class:e.className,visible:visible(e),card_text:(e.closest('.up-panel-container,.up-info-container')?.innerText||'').slice(0,1500)})),
     descriptions:[...document.querySelectorAll('.basic-desc-info')].map(e=>({class:e.className,text:e.innerText.slice(0,8000),visible:visible(e)})),
     videos:[...document.querySelectorAll('video')].map(v=>{const ranges=[];for(let i=0;i<Math.min(100,v.seekable.length);i++)ranges.push({start:v.seekable.start(i),end:v.seekable.end(i)});return {visible:visible(v),duration_seconds:Number.isFinite(v.duration)?v.duration:null,current_time:v.currentTime,ready_state:v.readyState,paused:v.paused,video_width:v.videoWidth,video_height:v.videoHeight,error_code:v.error?.code||null,seekable_ranges:ranges,...(active?{seeking:v.seeking,ended:v.ended}:{})};})};
   },standalone);}
   const until=Date.now()+publicTimeoutMs;let dom,matched=false,guard;
   do{
    budget(1);
    guard=await protection(publicPage,'https://www.bilibili.com','/video/'+candidate,{passive:true});dom=await read();
    const owners=dom.owner_candidates.filter(x=>x.visible),desc=dom.descriptions.filter(x=>x.visible),videos=dom.videos.filter(x=>x.visible);
    const descriptionMatches=desc.length===1&&(standalone?Boolean(publicDescriptionDisplayEvidence(desc[0].text,spec.metadata.description,spec.source_url,spec.metadata_sha256,true)):
     [spec.metadata.description,spec.source_url+'\n'+spec.metadata.description].includes(desc[0].text));
    async function rejectedMetadata(reason){
     await write(path.join(output,'public-native-metadata-rejected.json'),{version:'replay-public-metadata-rejected-v2',...common,
      observed_at:new Date().toISOString(),external_id:candidate,expected_title:spec.metadata.title,reason,visible_dom:dom});
    }
    if(dom.titles.length===1&&dom.titles[0].text&&!titleMatches(dom.titles[0].text)){await rejectedMetadata('title');fail('REPLAY_LATER_PUBLIC_IDENTITY_CHANGED');}
    if(desc.length===1&&desc[0].text&&!descriptionMatches){await rejectedMetadata('description');fail('REPLAY_LATER_PUBLIC_METADATA_CHANGED');}
    matched=dom.titles.length===1&&titleMatches(dom.titles[0].text)&&owners.length===1&&Boolean(owners[0].text)&&
     (spec.version!=='replay-part-native-browser-manifest-v5'||owners[0].text===spec.account_name)&&
     owners[0].url?.replace(/\/$/,'')==='https://space.bilibili.com/'+spec.target_site_uid&&owners[0].card_text.includes(owners[0].text)&&
     descriptionMatches&&
     videos.length===1&&Number.isFinite(videos[0].duration_seconds)&&Math.abs(videos[0].duration_seconds-spec.duration_seconds)<=2&&
     videos[0].ready_state>=2&&videos[0].error_code===null&&videos[0].seekable_ranges.some(r=>r.end>r.start);
    if(matched)break;await publicPage.waitForTimeout(500);
   }while(Date.now()<until);
   await protection(publicPage,'https://www.bilibili.com','/video/'+candidate,{passive:!matched,publicIdentity:matched});
   let playback=null;
   if(standalone&&matched){
    // The whole original P must be identified before interacting with its
    // public HTMLVideoElement. A successful seek alone does not prove play.
    await protection(publicPage,'https://www.bilibili.com','/video/'+candidate,{publicIdentity:true});
    await publicPage.evaluate(()=>{const items=[...document.querySelectorAll('video')].filter(v=>{const r=v.getBoundingClientRect();return r.width>0&&r.height>0;});
     if(items.length!==1)throw new Error('REPLAY_PART_PLAYER_AMBIGUOUS');const v=items[0];v.pause();v.currentTime=Math.max(0,v.duration-5);});
    common.player_seeks=1;
    const seekEnd=Date.now()+budget(30000);let before=null;
    do{
     await protection(publicPage,'https://www.bilibili.com','/video/'+candidate,{publicIdentity:true});dom=await read();
     const videos=dom.videos.filter(v=>v.visible);
     if(videos.length!==1)fail('REPLAY_PART_PLAYER_AMBIGUOUS');const v=videos[0];
     if(v.error_code!==null)fail('REPLAY_PART_PLAYER_ERROR');
     if(!v.seeking&&v.ready_state>=2&&Math.abs(v.duration_seconds-spec.duration_seconds)<=2&&v.current_time>=Math.max(0,v.duration_seconds-5)-0.05){before=v.current_time;break;}
     await publicPage.waitForTimeout(100);
    }while(Date.now()<seekEnd);
    if(before===null){
     const publication=await write(path.join(output,'publication-observed.json'),{
      version:'replay-part-publication-observed-v5',...common,observed_at:new Date().toISOString(),external_id:candidate,
      manager_observation:managerRef,reason:'REPLAY_PART_NEAR_END_SEEK_UNKNOWN',visible_dom:dom,
      player:{duration_seconds:spec.duration_seconds,expected_near_end:Math.max(0,spec.duration_seconds-5)}});
     common.publication_observation=publication;
     fail('REPLAY_PART_NEAR_END_SEEK_UNKNOWN');
    }
    await protection(publicPage,'https://www.bilibili.com','/video/'+candidate,{publicIdentity:true});
    // Bounded play promise; neither the browser's clock nor an assignment to
    // currentTime is substituted for observed decoded playback progress.
    const played=await publicPage.evaluate(async()=>{const videos=[...document.querySelectorAll('video')].filter(v=>{const r=v.getBoundingClientRect();return r.width>0&&r.height>0;});
     if(videos.length!==1)return false;let timer;try{return await Promise.race([videos[0].play().then(()=>true,()=>false),new Promise(resolve=>{timer=setTimeout(()=>resolve(false),5000);})]);}finally{clearTimeout(timer);}});
    if(!played)fail('REPLAY_PART_NEAR_END_PLAY_BLOCKED');
    const playEnd=Date.now()+budget(30000);let progressed=false;
    do{
     await protection(publicPage,'https://www.bilibili.com','/video/'+candidate,{publicIdentity:true});dom=await read();
     const videos=dom.videos.filter(v=>v.visible);if(videos.length!==1)fail('REPLAY_PART_PLAYER_AMBIGUOUS');const v=videos[0];
     if(v.error_code!==null)fail('REPLAY_PART_PLAYER_ERROR');
     playback={seek_performed:true,seeking:v.seeking,before,after:v.current_time,ended:v.ended};
     progressed=!v.seeking&&v.ready_state>=2&&v.video_width>0&&v.video_height>0&&Math.abs(v.duration_seconds-spec.duration_seconds)<=2&&
      v.current_time>=Math.max(0,v.duration_seconds-5)&&(v.current_time-before>0.1||v.ended===true)&&v.seekable_ranges.some(r=>r.end>r.start);
     if(progressed)break;await publicPage.waitForTimeout(100);
    }while(Date.now()<playEnd);
    if(!progressed){
     const publication=await write(path.join(output,'publication-observed.json'),{
      version:'replay-part-publication-observed-v5',...common,observed_at:new Date().toISOString(),external_id:candidate,
      manager_observation:managerRef,reason:'REPLAY_PART_NEAR_END_PLAY_UNKNOWN',visible_dom:dom,
      player:{duration_seconds:spec.duration_seconds,before,after:playback?.after??null}});
     common.publication_observation=publication;
     fail('REPLAY_PART_NEAR_END_PLAY_UNKNOWN');
    }
   }
   const finalDescriptions=dom.descriptions.filter(x=>x.visible);
   const descriptionDisplay=standalone&&finalDescriptions.length===1?publicDescriptionDisplayEvidence(finalDescriptions[0].text,spec.metadata.description,spec.source_url,spec.metadata_sha256,true):null;
   if(standalone&&matched&&!descriptionDisplay){
    await write(path.join(output,'public-native-metadata-rejected.json'),{version:'replay-public-metadata-rejected-v2',...common,
     observed_at:new Date().toISOString(),external_id:candidate,expected_title:spec.metadata.title,reason:'description_after_player',visible_dom:dom});
    fail('REPLAY_LATER_PUBLIC_METADATA_CHANGED');
   }
   const publicRef=await write(path.join(output,'public-native-inventory.json'),{version:standalone?'replay-part-public-player-observation-v5':'known-bv-public-passive-inventory-v1',...common,
    observed_at:new Date().toISOString(),external_id:candidate,expected_title:spec.metadata.title,visible_dom:dom,
    ...(standalone?{near_end_playback:playback,description_display_evidence:descriptionDisplay}:{})});
   const imageRef=await screenshot(publicPage,path.join(output,'public-page.png'));
   if(!matched)return {state:'public_result_not_ready',candidate_external_id:candidate,manager_observation:managerRef,public_observation:publicRef,reconcile_before_retry:true};
   const record=await write(path.join(output,'reconciliation.json'),{version:'replay-later-native-result-observation-v2',...common,
    external_id:candidate,observed_at:new Date().toISOString(),manager_observation:managerRef,public_observation:publicRef,public_screenshot:imageRef});
   return {state:'later_native_evidence_observed',candidate_external_id:candidate,reconciliation_observation:record,
    same_submit_attempt_observed:false,reconcile_before_retry:true};
  }finally{publicPage.off('filechooser',onChooser);await publicPage.close();}
 }finally{page.off('filechooser',onChooser);}
}
