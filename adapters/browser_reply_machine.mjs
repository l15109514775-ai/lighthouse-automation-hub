// Independent bounded reply collector. No publisher, model, hidden state or v1 mutations.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {captureTweet} from './browser_tweet_capture.mjs';
import {dismissOrdinaryPrompts,locateTweetArticle,assertSourceVisible,inspectArticleElement,ObservationError} from './browser_observation.mjs';
const hash=b=>crypto.createHash('sha256').update(b).digest('hex');
const stable=x=>JSON.stringify(x,(_,v)=>v&&typeof v==='object'&&!Array.isArray(v)?Object.fromEntries(Object.keys(v).sort().map(k=>[k,v[k]])):v);
const check=(value,code)=>{if(!value)throw new ObservationError(code);};
const rect=r=>({x:r.x,y:r.y,width:r.width,height:r.height});

// Only actual visible Replying-to UI outside the source body can establish this
// target. A body mention, preceding post or caller-provided parent ID cannot.
export function inspectReplyToLabel(article){
 const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return s.display!=='none'&&s.visibility==='visible'&&Number(s.opacity)>.01&&r.width>0&&r.height>0;};
 const bodies=[...article.querySelectorAll('[data-testid="tweetText"],.whitespace-pre-wrap[dir="auto"]')];
 const candidates=[];
 for(const e of article.querySelectorAll('div,span,p,[data-testid="replyingTo"]')){
  if(!visible(e)||bodies.some(b=>b.contains(e)||e.contains(b))||e.closest('article')!==article)continue;
  const text=e.innerText.trim();if(text.length>256||!/^(?:回复|回覆|Replying to|返信先)\s*/i.test(text))continue;
  const handles=[...text.matchAll(/@([A-Za-z0-9_]+)/g)].map(m=>m[1]);if(handles.length!==1)continue;
  const links=[...e.querySelectorAll('a[href]')].filter(visible);if(links.length!==1)continue;
  let u;try{u=new URL(links[0].getAttribute('href'),location.href);}catch{continue;}
  if(u.protocol!=='https:'||!['x.com','www.x.com'].includes(u.hostname)||u.username||u.password||u.search||u.hash||
   !new RegExp(`^/${handles[0]}/?$`,'i').test(u.pathname))continue;
  candidates.push({element:e,text,handles,urls:[`https://x.com/${u.pathname.split('/')[1]}`]});
 }
 const leaves=candidates.filter(c=>!candidates.some(o=>o!==c&&c.element.contains(o.element)));
 if(leaves.length!==1)return {ambiguous:true,candidate_count:leaves.length};
 const c=leaves[0],r=c.element.getBoundingClientRect();
 return {ambiguous:false,text:c.text,target_handles:c.handles,target_profile_urls:c.urls,
  visible:true,rect:{x:r.x,y:r.y,width:r.width,height:r.height}};
}

async function artifact(file){return {file,sha256:hash(await fs.readFile(file))};}
async function write(file,value){await fs.writeFile(file,JSON.stringify(value,null,2),{mode:0o600});return artifact(file);}
async function navigate(page,url,dir){
 const response=await page.goto(url,{waitUntil:'domcontentloaded'});
 check(response?.status()===200,'WEB_REPLY_PAGE_UNAVAILABLE');
 if(new URL(page.url()).pathname.includes('/i/flow/login'))throw new ObservationError('AUTH_EXPIRED');
 check(page.url().replace(/\/$/,'')===url,'WEB_REPLY_SOURCE_URL_CHANGED');
 await dismissOrdinaryPrompts(page,path.join(dir,'prompts'));
 await page.locator('article').first().waitFor({state:'visible',timeout:15000});
}

async function readThread(page,identity){
 const articles=await page.locator('article').all(),identities=[];
 for(const article of articles)identities.push(await article.evaluate(inspectArticleElement));
 const own=identities.flatMap((p,i)=>p?.handle.toLowerCase()===identity.handle.toLowerCase()&&p.postId===identity.postId?[i]:[]);
 check(own.length===1&&own[0]===1&&identities[0]?.author_verified&&identities[1]?.author_verified,
  'WEB_REPLY_PARENT_OR_CHAIN_AMBIGUOUS');
 const parent=articles[0],reply=articles[1];
 check(await parent.locator('article').count()===0&&await reply.locator('article').count()===0,'WEB_REPLY_NESTED_ARTICLE_UNSUPPORTED');
 const label=await reply.evaluate(inspectReplyToLabel);
 check(!label.ambiguous&&label.target_handles[0].toLowerCase()===identities[0].handle.toLowerCase(),
  'WEB_REPLY_VISIBLE_TARGET_MISSING_OR_MISMATCHED');
 const controls=await page.evaluate(()=>{
  const ownArticles=[...document.querySelectorAll('article')],bottom=ownArticles[1]?.getBoundingClientRect().bottom||0;
  return [...document.querySelectorAll('button,a,[role="button"]')].filter(e=>{
   const r=e.getBoundingClientRect(),s=getComputedStyle(e);if(r.width<=0||r.height<=0||r.top>bottom||s.display==='none'||s.visibility!=='visible')return false;
   if(e.closest('[data-testid="tweetText"],.whitespace-pre-wrap[dir="auto"]'))return false;
   return /显示.*(?:回复|对话|前文)|查看.*(?:回复|对话|前文)|Show (?:more )?(?:repl|thread)|View.*(?:repl|thread)|返信を表示|スレッドを表示/i.test(e.innerText.trim());
  }).map(e=>e.innerText.trim());
 });
 check(controls.length===0,'WEB_REPLY_COLLAPSED_CONTEXT_UNSUPPORTED');
 const viewport=await page.evaluate(()=>({width:innerWidth,height:innerHeight}));
 const sourceTexts=[];
 for(const [i,article] of [parent,reply].entries()){
  const body=article.locator(identities[i].layout==='legacy-dom-v1'?'[data-testid="tweetText"]':'.whitespace-pre-wrap[dir="auto"]');
  check(await body.count()===1,'WEB_REPLY_SOURCE_TEXT_AMBIGUOUS');sourceTexts.push(await body.innerText());
 }
 const p=await parent.boundingBox(),r=await reply.boundingBox();
 check(p&&r,'WEB_REPLY_THREAD_NOT_VISIBLE');
 return {articles:{parent,reply},identities:identities.slice(0,2),source_texts:sourceTexts,label,controls,viewport,parent_rect:rect(p),reply_rect:rect(r)};
}

function semanticThread(t){return {identities:t.identities,source_texts:t.source_texts,label:{text:t.label.text,target_handles:t.label.target_handles,
 target_profile_urls:t.label.target_profile_urls},controls:t.controls};}
function requireGeometry(t){
 for(const r of [t.parent_rect,t.reply_rect,t.label.rect])check(r.x>=0&&r.y>=0&&r.width>0&&r.height>0&&
  r.x+r.width<=t.viewport.width&&r.y+r.height<=t.viewport.height,'WEB_REPLY_THREAD_NOT_FULLY_VISIBLE');
 check(t.parent_rect.y+t.parent_rect.height<=t.reply_rect.y+1,'WEB_REPLY_THREAD_LAYOUT_AMBIGUOUS');
}

async function observe(page,identity,folder,role,navigationId){
 const raw=await captureTweet(page,identity,folder,{pairedReplyDraft:role!=='parent_independent_reread',machineSource:true,
  trigger:'machine_reply_prototype'});
 const article=await locateTweetArticle(page,identity),visible=await article.evaluate(inspectArticleElement),inventory=raw.machine_source_facts;
 const permitted=role==='reply_in_thread'?['REPLY_CONTEXT_REQUIRED']:[];
 check(inventory.failures.every(f=>permitted.includes(f)),'WEB_REPLY_SOURCE_NOT_COMPLETE_PLAIN_TEXT');
 check(inventory.status_ids.length===1&&inventory.status_ids[0]===identity.postId&&inventory.foreign_image_count===0&&
  inventory.unsupported_elements===0&&inventory.body_count===1,'WEB_REPLY_SOURCE_NOT_COMPLETE_PLAIN_TEXT');
 const facts={post_id:visible.postId,handle:visible.handle,display_name:visible.display_name,observed_text:inventory.observed_text,
  observed_published_at:raw.payload.post.created_at,body_count:inventory.body_count,show_more:inventory.show_more,clipped:inventory.clipped,
  media_count:inventory.foreign_image_count,quote_count:inventory.status_ids.filter(id=>id!==identity.postId).length,
  card_count:inventory.unsupported_elements,body_links:inventory.body_links,visible_timestamp:visible.visible_timestamp,
  display_timezone:await page.evaluate(()=>Intl.DateTimeFormat().resolvedOptions().timeZone)};
 if(visible.datetime)facts.datetime_attribute=visible.datetime;
 return write(path.join(folder,'reply-post-observation.json'),{version:'reply-visible-post-v1',role,human_review:false,official_api:false,
  payload:raw.payload,facts,screenshot:await artifact(raw.screenshot_file),
  visibility:await artifact(path.join(folder,'after-screenshot/visibility.json')),navigation_id:navigationId,page_url:page.url()});
}

export async function captureReplyMachine(page,identity,evidenceRoot,{captureId=crypto.randomUUID()}={}){
 check(['example_handle_a','example-channel-b'].includes(identity?.handle)&&/^\d{1,30}$/.test(identity?.postId||''),'WEB_REPLY_IDENTITY_INVALID');
 check(/^[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(captureId),'WEB_REPLY_CAPTURE_ID_INVALID');
 check(await page.evaluate(()=>Intl.DateTimeFormat().resolvedOptions().timeZone)==='Asia/Shanghai','WEB_REPLY_BROWSER_TIMEZONE_REQUIRED');
 await fs.mkdir(evidenceRoot,{recursive:true,mode:0o700});
 const dir=path.join(evidenceRoot,captureId);
 try{await fs.mkdir(dir,{mode:0o700});}catch(e){if(e.code==='EEXIST')throw new ObservationError('WEB_REPLY_CAPTURE_ALREADY_EXISTS');throw e;}
 const replyUrl=`https://x.com/${identity.handle}/status/${identity.postId}`,threadNavigation=crypto.randomUUID();
 await navigate(page,replyUrl,path.join(dir,'thread-navigation'));
 const initial=await readThread(page,identity),parentIdentity={handle:initial.identities[0].handle,postId:initial.identities[0].postId};
 const parent=await observe(page,parentIdentity,path.join(dir,'parent-thread'),'parent_in_thread',threadNavigation);
 const reply=await observe(page,identity,path.join(dir,'reply-thread'),'reply_in_thread',threadNavigation);
 let final=await readThread(page,identity);
 check(stable(semanticThread(initial))===stable(semanticThread(final)),'WEB_REPLY_THREAD_CHANGED_DURING_CAPTURE');
 await assertSourceVisible(page,final.articles.parent,path.join(dir,'thread-parent-final'));
 await assertSourceVisible(page,final.articles.reply,path.join(dir,'thread-reply-final'));
 final=await readThread(page,identity);requireGeometry(final);
 const conversation=path.join(dir,'conversation.png');await page.screenshot({path:conversation,fullPage:false});await fs.chmod(conversation,0o600);
 const after=await readThread(page,identity);requireGeometry(after);
 check(stable(semanticThread(final))===stable(semanticThread(after)),'WEB_REPLY_THREAD_CHANGED_DURING_CAPTURE');
 const parentObserved=JSON.parse(await fs.readFile(parent.file,'utf8'));
 const thread={page_url:page.url(),navigation_id:threadNavigation,context_before_reply_ids:[final.identities[0].postId],
  article_ids_through_reply:final.identities.map(x=>x.postId),parent_index:0,reply_index:1,
  collapsed_context_detected:final.controls.length>0,show_more_context:final.controls.length>0,parent_is_quote:parentObserved.facts.quote_count>0,
  reply_to_label:{article_id:final.identities[1].postId,visible:final.label.visible,text:final.label.text,
   target_handles:final.label.target_handles,target_profile_urls:final.label.target_profile_urls},screenshot:await artifact(conversation),
  viewport:final.viewport,parent_rect:final.parent_rect,reply_rect:final.reply_rect,reply_to_label_rect:final.label.rect};
 const parentUrl=`https://x.com/${parentIdentity.handle}/status/${parentIdentity.postId}`;
 await navigate(page,parentUrl,path.join(dir,'parent-navigation'));
 const reread=await observe(page,parentIdentity,path.join(dir,'parent-reread'),'parent_independent_reread',crypto.randomUUID());
 const semantic=value=>({id:value.payload.post.id,handle:value.payload.author.username,name:value.payload.author.name,
  text:value.payload.post.text,published_at:Date.parse(value.payload.post.created_at),precision:value.payload.post.timestamp_precision,url:value.payload.url});
 check(stable(semantic(JSON.parse(await fs.readFile(parent.file,'utf8'))))===stable(semantic(JSON.parse(await fs.readFile(reread.file,'utf8')))),
  'WEB_REPLY_PARENT_REREAD_CHANGED');
 const packet={version:'reply-thread-machine-observation-v1',capture_id:captureId,rule:'visible-reply-to-and-adjacent-parent-reread-v1',
  observer_kind:'server_rendered_dom',human_review:false,official_api:false,parent_in_thread:parent,reply_in_thread:reply,parent_reread:reread,thread};
 const packetFile=path.join(dir,'reply-machine-packet.json');await write(packetFile,packet);
 return {packet,packet_file:packetFile,human_review:false,automatic_publication_eligible:false};
}
