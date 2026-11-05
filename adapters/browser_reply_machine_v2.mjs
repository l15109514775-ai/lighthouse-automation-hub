// XDS two-item avatar-connector rule. No fabricated label, thread time or review.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {captureTweet} from './browser_tweet_capture.mjs';
import {inspectReplyToLabel} from './browser_reply_machine.mjs';
import {inspectPlainTextSource} from './browser_source_facts.mjs';
import {publicXAccessError} from './browser_x_public.mjs';
import {dismissOrdinaryPrompts,locateTweetArticle,assertSourceVisible,inspectArticleElement,ObservationError} from './browser_observation.mjs';
import {positionReplyThread} from './browser_source_position.mjs';
const check=(v,c)=>{if(!v)throw new ObservationError(c);};
const stable=x=>JSON.stringify(x,(_,v)=>v&&typeof v==='object'&&!Array.isArray(v)?Object.fromEntries(Object.keys(v).sort().map(k=>[k,v[k]])):v);
const hash=b=>crypto.createHash('sha256').update(b).digest('hex');
async function artifact(file){return {file,sha256:hash(await fs.readFile(file))};}
async function write(file,value){await fs.writeFile(file,JSON.stringify(value,null,2),{mode:0o600});return artifact(file);}
async function navigate(page,url,dir){
 const response=await page.goto(url,{waitUntil:'domcontentloaded'}),access=publicXAccessError(page.url(),response?.status());
 if(access)throw new ObservationError(access);
 check(response?.status()===200,'WEB_REPLY_V2_PAGE_UNAVAILABLE');
 check(page.url().replace(/\/$/,'')===url,'WEB_REPLY_V2_SOURCE_URL_CHANGED');
 await dismissOrdinaryPrompts(page,path.join(dir,'prompts'));await page.locator('article').first().waitFor({state:'visible',timeout:15000});
 await page.waitForFunction(()=>[...document.querySelectorAll('article')].slice(0,2).every(a=>[...a.querySelectorAll('img')].every(i=>i.complete)),null,{timeout:10000});
}

async function topology(page,identity){
 const articles=await page.locator('article').all(),identities=[];
 for(const a of articles)identities.push(await a.evaluate(inspectArticleElement));
 const own=identities.flatMap((p,i)=>p?.handle.toLowerCase()===identity.handle.toLowerCase()&&p.postId===identity.postId?[i]:[]);
 check(own.length===1&&own[0]===1&&identities[0]?.author_verified&&identities[1]?.author_verified,'WEB_REPLY_V2_PARENT_OR_CHAIN_AMBIGUOUS');
 const parent=articles[0],reply=articles[1],pa=await parent.elementHandle(),ra=await reply.elementHandle();
 const value=await page.evaluate(({parent,reply,pid,rid,ph,rh})=>{
  const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return s.display!=='none'&&s.visibility==='visible'&&Number(s.opacity)>.01&&r.width>0&&r.height>0;};
  const rect=e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};};
  const pi=parent.closest('li'),ri=reply.closest('li'),list=pi?.parentElement;
  const ids=e=>[...e.querySelectorAll('article')].map(a=>a===parent?pid:a===reply?rid:null);
  if(!pi||!ri||!list)return {unsupported:true};
  const image=(article,handle)=>[...article.querySelectorAll('img')].filter(visible).find(e=>
   e.currentSrc.startsWith('https://pbs.twimg.com/profile_images/')&&e.alt==='@'+handle);
  const pimg=image(parent,ph),rimg=image(reply,rh);
  if(!pimg||!rimg)return {unsupported:true};
  const avatar=e=>({rect:rect(e),src:e.currentSrc,alt:e.alt,loaded:e.complete&&e.naturalWidth>0});
  const bodies=[...parent.querySelectorAll('[data-testid="tweetText"],.whitespace-pre-wrap[dir="auto"]')];
  const candidates=[...parent.querySelectorAll('span')].filter(e=>{
   if(!visible(e)||bodies.some(b=>b.contains(e)))return false;
   const r=e.getBoundingClientRect(),s=getComputedStyle(e),col=e.parentElement,cs=getComputedStyle(col);
   return r.width>0&&r.width<=4&&r.height>=10&&s.backgroundColor!=='rgba(0, 0, 0, 0)'&&s.backgroundColor!=='transparent'&&
    cs.display==='flex'&&cs.flexDirection==='column'&&col.contains(pimg);
  });
  let connector=null;
  if(candidates.length===1){const e=candidates[0],s=getComputedStyle(e),r=e.getBoundingClientRect(),col=e.parentElement,cs=getComputedStyle(col);
   connector={tag:e.tagName,class_name:e.className,rect:rect(e),background_color:s.backgroundColor,opacity:Number(s.opacity),
    inside_body:bodies.some(b=>b.contains(e)),owner_article_id:e.closest('article')===parent?pid:null,
    owner_item_article_id:e.closest('li')===pi?pid:null,avatar_column_contains_parent_avatar:col.contains(pimg),
    avatar_column_display:cs.display,avatar_column_flex_direction:cs.flexDirection,avatar_column_rect:rect(col),
    hit_points:[.2,.5,.8].map(f=>{const x=r.x+r.width/2,y=r.y+r.height*f,top=document.elementsFromPoint(x,y)[0];
     return {x,y,visible:!!top&&(top===e||e.contains(top))};})};
  }
  const bottom=reply.getBoundingClientRect().bottom;
  const collapsed=[...document.querySelectorAll('button,a,[role="button"]')].filter(e=>visible(e)&&e.getBoundingClientRect().top<=bottom&&
   !e.closest('[data-testid="tweetText"],.whitespace-pre-wrap[dir="auto"]')&&
   /显示.*(?:回复|对话|前文)|查看.*(?:回复|对话|前文)|Show (?:more )?(?:repl|thread)|View.*(?:repl|thread)|返信を表示|スレッドを表示/i.test(e.innerText.trim())).map(e=>e.innerText.trim());
  return {unsupported:false,list:{tag:list.tagName,rect:rect(list),child_count:list.children.length,article_ids:ids(list)},
   parent_item:{tag:pi.tagName,rect:rect(pi),article_ids:ids(pi)},reply_item:{tag:ri.tagName,rect:rect(ri),article_ids:ids(ri)},
   items_same_list:list===ri.parentElement,items_directly_adjacent:pi.nextElementSibling===ri,
   connector_candidate_count:candidates.length,connector,parent_avatar:avatar(pimg),reply_avatar:avatar(rimg),
   collapsed_controls:collapsed,viewport:{width:innerWidth,height:innerHeight}};
 },{parent:pa,reply:ra,pid:identities[0].postId,rid:identities[1].postId,ph:identities[0].handle,rh:identities[1].handle});
 check(!value.unsupported&&value.list.tag==='UL'&&value.list.child_count===2&&value.items_same_list&&value.items_directly_adjacent&&
  value.parent_item.article_ids.length===1&&value.reply_item.article_ids.length===1&&value.list.article_ids.length===2,'WEB_REPLY_V2_LIST_TOPOLOGY_UNSUPPORTED');
 check(value.collapsed_controls.length===0,'WEB_REPLY_V2_COLLAPSED_CONTEXT_UNSUPPORTED');
 check(value.connector_candidate_count===1&&value.connector,'WEB_REPLY_V2_CONNECTOR_MISSING_OR_AMBIGUOUS');
 const line=value.connector,lr=line.rect,p=value.parent_avatar.rect,r=value.reply_avatar.rect,pi=value.parent_item.rect,ri=value.reply_item.rect;
 const color=/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*([\d.]+))?\s*\)$/.exec(line.background_color);
 check(color,'WEB_REPLY_V2_CONNECTOR_PAINT_UNSUPPORTED');
 const rgb=color.slice(1,4).map(Number),alpha=Number(color[4]||1)*line.opacity,grey=rgb.reduce((a,b)=>a+b,0)/3*alpha+255*(1-alpha);
 check(Math.max(...rgb)-Math.min(...rgb)<=2&&grey>=80&&grey<=248&&alpha>=.05&&line.hit_points.every(p=>p.visible)&&
  Math.abs((p.x+p.width/2)-(r.x+r.width/2))<=1&&Math.abs((lr.x+lr.width/2)-(p.x+p.width/2))<=1&&
  lr.y-(p.y+p.height)>=0&&lr.y-(p.y+p.height)<=8&&Math.abs(lr.y+lr.height-(pi.y+pi.height))<=1&&
  r.y-(lr.y+lr.height)>=0&&r.y-(lr.y+lr.height)<=24&&Math.abs(pi.y+pi.height-ri.y)<=1,'WEB_REPLY_V2_CONNECTOR_GEOMETRY_UNSUPPORTED');
 const label=await reply.evaluate(inspectReplyToLabel);
 check(!label.ambiguous||label.candidate_count===0,'WEB_REPLY_V2_LABEL_AMBIGUOUS');
 if(!label.ambiguous)check(label.target_handles[0].toLowerCase()===identities[0].handle.toLowerCase(),'WEB_REPLY_V2_LABEL_CONTRADICTS_PARENT');
 const texts=[];for(const [i,a] of [parent,reply].entries()){
  const body=a.locator(identities[i].layout==='legacy-dom-v1'?'[data-testid="tweetText"]':'.whitespace-pre-wrap[dir="auto"]');
  check(await body.count()===1,'WEB_REPLY_V2_BODY_AMBIGUOUS');texts.push(await body.innerText());
 }
 return {value,identities:identities.slice(0,2),texts,label:label.ambiguous?null:label,parent,reply};
}
function semantic(t){return {ids:t.identities,texts:t.texts,topology:t.value,label:t.label};}

async function dayParent(page,identity,folder,navigationId){
 await fs.mkdir(folder,{recursive:true,mode:0o700});await dismissOrdinaryPrompts(page,path.join(folder,'prompts'));
 const article=await locateTweetArticle(page,identity);
 const read=async()=>({identity:await article.evaluate(inspectArticleElement),inventory:await article.evaluate(inspectPlainTextSource)});
 await assertSourceVisible(page,article,path.join(folder,'before'));const observed=await read(),v=observed.identity,i=observed.inventory;
 check(i.failures.length===0&&i.status_ids.length===1&&i.status_ids[0]===identity.postId,'WEB_REPLY_V2_PARENT_NOT_COMPLETE_PLAIN_TEXT');
 const date=/^(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日$/.exec(v.visible_timestamp);
 check(date&&!v.datetime,'WEB_REPLY_V2_PARENT_DAY_PRECISION_REQUIRED');
 const day={year:date[1]?Number(date[1]):null,month:Number(date[2]),day:Number(date[3]),precision:'day',visible_text:v.visible_timestamp};
 const screenshot=path.join(folder,'article.png');await article.screenshot({path:screenshot,timeout:10000});await fs.chmod(screenshot,0o600);
 await assertSourceVisible(page,article,path.join(folder,'after-screenshot'));
 check(stable(observed)===stable(await read()),'WEB_REPLY_V2_PARENT_CHANGED_DURING_CAPTURE');
 const payload={post:{id:v.postId,text:i.observed_text,published_day:day,timestamp_precision:'day',referenced_tweets:[]},
  author:{name:v.display_name,username:v.handle},url:`https://x.com/${v.handle}/status/${v.postId}`,source_api:'x-web-dom-day-v2',
  source_media_count:null,references_observed:false,capture:{protocol:'reply-thread-day-dom-v2',captured_at:new Date().toISOString(),
   screenshot_sha256:hash(await fs.readFile(screenshot)),visibility_passed:true,human_review:false,source_screenshot_reviewed:false,
   official_api_receipt:false,full_source_completeness_verified:false}};
 if(v.profile_image_url)payload.author.profile_image_url=v.profile_image_url;
 return write(path.join(folder,'day-parent-observation-v2.json'),{version:'reply-visible-post-day-v2',role:'parent_in_thread_day',
  human_review:false,official_api:false,payload,navigation_id:navigationId,page_url:page.url(),screenshot:await artifact(screenshot),
  visibility:await artifact(path.join(folder,'after-screenshot/visibility.json')),
  facts:{post_id:v.postId,handle:v.handle,display_name:v.display_name,observed_text:i.observed_text,observed_published_day:day,
   visible_timestamp:v.visible_timestamp,display_timezone:'Asia/Shanghai',body_count:i.body_count,media_count:i.foreign_image_count,
   quote_count:i.status_ids.filter(id=>id!==v.postId).length,card_count:i.unsupported_elements,show_more:i.show_more,clipped:i.clipped,body_links:i.body_links}});
}

async function fullPost(page,identity,folder,role,nav){
 const raw=await captureTweet(page,identity,folder,{pairedReplyDraft:role==='reply_in_thread',machineSource:true,trigger:'machine_reply_v2_prototype',
  sourcePositioning:role==='reply_in_thread'?'preserve_thread':'independent'});
 const article=await locateTweetArticle(page,identity),v=await article.evaluate(inspectArticleElement),i=raw.machine_source_facts;
 check(i.failures.every(f=>role==='reply_in_thread'&&f==='REPLY_CONTEXT_REQUIRED')&&i.unsupported_elements===0&&i.foreign_image_count===0&&
  i.body_count===1&&i.status_ids.length===1,'WEB_REPLY_V2_FULL_SOURCE_UNSUPPORTED');
 const facts={post_id:v.postId,handle:v.handle,display_name:v.display_name,observed_text:i.observed_text,
  observed_published_at:raw.payload.post.created_at,body_count:i.body_count,media_count:i.foreign_image_count,quote_count:0,card_count:i.unsupported_elements,
  show_more:i.show_more,clipped:i.clipped,body_links:i.body_links,visible_timestamp:v.visible_timestamp,display_timezone:'Asia/Shanghai'};
 if(v.datetime)facts.datetime_attribute=v.datetime;
 return write(path.join(folder,'reply-post-observation.json'),{version:'reply-visible-post-v1',role,human_review:false,official_api:false,
  payload:raw.payload,facts,navigation_id:nav,page_url:page.url(),screenshot:await artifact(raw.screenshot_file),
  visibility:await artifact(path.join(folder,'after-screenshot/visibility.json'))});
}

export async function captureReplyMachineV2(page,identity,evidenceRoot,{captureId=crypto.randomUUID()}={}){
 check(['example_handle_a','example-channel-b'].includes(identity?.handle)&&/^\d{1,30}$/.test(identity?.postId||''),'WEB_REPLY_V2_IDENTITY_INVALID');
 check(/^[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(captureId),'WEB_REPLY_V2_CAPTURE_ID_INVALID');
 check(await page.evaluate(()=>Intl.DateTimeFormat().resolvedOptions().timeZone)==='Asia/Shanghai','WEB_REPLY_V2_TIMEZONE_REQUIRED');
 await fs.mkdir(evidenceRoot,{recursive:true,mode:0o700});const dir=path.join(evidenceRoot,captureId);await fs.mkdir(dir,{mode:0o700});
 const nav=crypto.randomUUID(),replyUrl=`https://x.com/${identity.handle}/status/${identity.postId}`;
 await navigate(page,replyUrl,path.join(dir,'thread-navigation'));
 await positionReplyThread(page,identity,path.join(dir,'thread-positioning'));
 const initial=await topology(page,identity);
 const parentIdentity={handle:initial.identities[0].handle,postId:initial.identities[0].postId};
 const parent=await dayParent(page,parentIdentity,path.join(dir,'parent-thread-day'),nav);
 const reply=await fullPost(page,identity,path.join(dir,'reply-thread'),'reply_in_thread',nav);
 let final=await topology(page,identity);
 check(stable(semantic(initial))===stable(semantic(final)),'WEB_REPLY_V2_THREAD_CHANGED');
 await assertSourceVisible(page,final.parent,path.join(dir,'parent-final'));await assertSourceVisible(page,final.reply,path.join(dir,'reply-final'));
 final=await topology(page,identity);
 const screenshot=path.join(dir,'thread.png');await page.screenshot({path:screenshot,fullPage:false});await fs.chmod(screenshot,0o600);
 const after=await topology(page,identity);check(stable(semantic(final))===stable(semantic(after)),'WEB_REPLY_V2_THREAD_CHANGED');
 const parentObserved=JSON.parse(await fs.readFile(parent.file,'utf8'));
 const thread={page_url:page.url(),navigation_id:nav,context_before_reply_ids:[final.identities[0].postId],
  article_ids_through_reply:final.identities.map(p=>p.postId),collapsed_context_detected:final.value.collapsed_controls.length>0,
  show_more_context:final.value.collapsed_controls.length>0,parent_is_quote:parentObserved.facts.quote_count>0,
  reply_to_label:final.label,topology:final.value,viewport:final.value.viewport,screenshot:await artifact(screenshot)};
 await navigate(page,`https://x.com/${parentIdentity.handle}/status/${parentIdentity.postId}`,path.join(dir,'parent-navigation'));
 const full=await fullPost(page,parentIdentity,path.join(dir,'parent-reread'),'parent_independent_reread',crypto.randomUUID());
 const reread=JSON.parse(await fs.readFile(full.file,'utf8')),p=parentObserved.payload,f=reread.payload;
 check(p.post.id===f.post.id&&p.post.text===f.post.text&&p.author.name===f.author.name&&p.author.username===f.author.username&&p.url===f.url,
  'WEB_REPLY_V2_PARENT_REREAD_CHANGED');
 const pieces=Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'numeric',day:'numeric'})
  .formatToParts(new Date(f.post.created_at)).filter(x=>['year','month','day'].includes(x.type)).map(x=>[x.type,Number(x.value)]));
 const day=p.post.published_day;
 check(day.month===pieces.month&&day.day===pieces.day&&(day.year===null||day.year===pieces.year),'WEB_REPLY_V2_PARENT_DAY_MISMATCH');
 const packet={version:'reply-thread-machine-observation-v2',capture_id:captureId,rule:'adjacent-list-avatar-connector-parent-day-reread-v2',
  observer_kind:'server_rendered_dom',human_review:false,official_api:false,parent_in_thread_day:parent,reply_in_thread:reply,parent_reread:full,thread};
 const file=path.join(dir,'reply-machine-packet-v2.json');await write(file,packet);
 return {packet,packet_file:file,human_review:false,automatic_publication_eligible:false};
}
