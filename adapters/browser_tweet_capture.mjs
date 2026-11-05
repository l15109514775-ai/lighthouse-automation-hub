// Evidence from rendered public DOM; never a human or official-API attestation.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {dismissOrdinaryPrompts,locateTweetArticle,assertSourceVisible,inspectArticleElement,ObservationError} from './browser_observation.mjs';
import {inspectPlainTextSource} from './browser_source_facts.mjs';
import {positionIndependentSource} from './browser_source_position.mjs';
const hash=value=>crypto.createHash('sha256').update(value).digest('hex');
const canonical=x=>JSON.stringify(x,(_,v)=>v&&typeof v==='object'&&!Array.isArray(v)?Object.fromEntries(Object.keys(v).sort().map(k=>[k,v[k]])):v);

export async function captureTweet(page,identity,evidenceDir,{trigger='server_operator_probe',pairedReplyDraft=false,machineSource=false,
 sourcePositioning=pairedReplyDraft?'preserve_thread':'independent'}={}){
 if(!['independent','preserve_thread'].includes(sourcePositioning))throw new ObservationError('WEB_SOURCE_POSITION_MODE_INVALID');
 const prompts=await dismissOrdinaryPrompts(page,path.join(evidenceDir,'prompts'));
 const article=await locateTweetArticle(page,identity);
 const preceding=await article.evaluate(e=>[...e.ownerDocument.querySelectorAll('article')].indexOf(e));
 if(!pairedReplyDraft&&(preceding>0||/^(?:回复|Replying to)\s*@/mi.test(await article.innerText())))
  throw new ObservationError('WEB_REPLY_CONTEXT_REQUIRED');
 const positioning=sourcePositioning==='independent'?await positionIndependentSource(page,identity,article,path.join(evidenceDir,'native-positioning')):null;
 await assertSourceVisible(page,article,path.join(evidenceDir,'before'));
 const read=async()=>{
  const identityRead=await article.evaluate(inspectArticleElement);
  if(!identityRead?.author_verified||identityRead.handle.toLowerCase()!==identity.handle.toLowerCase()||identityRead.postId!==identity.postId)
   throw new ObservationError('WEB_SOURCE_AUTHOR_UNVERIFIED');
  const text=article.locator(identityRead.layout==='legacy-dom-v1'?'[data-testid="tweetText"]':'.whitespace-pre-wrap[dir="auto"]');
  if(await text.count()!==1)throw new ObservationError('WEB_SOURCE_TEXT_OR_QUOTE_REQUIRES_REVIEW');
  if(await article.locator('[data-testid="tweet-text-show-more-link"]').isVisible())
   throw new ObservationError('WEB_SOURCE_TEXT_TRUNCATED');
  const display=identityRead.display_name;
  let published=identityRead.datetime,precision='dom_datetime';
  if(!published){
   const m=/^(\d{1,2}):(\d{2})\s*[·•]\s*(\d{4})年(\d{1,2})月(\d{1,2})日$/.exec(identityRead.visible_timestamp);
   if(!m||Number(m[1])>23||Number(m[2])>59)throw new ObservationError('WEB_SOURCE_TIMESTAMP_NOT_VISIBLE');
   // The browser uses Asia/Shanghai. Preserve the UI's minute precision; do
   // not recover hidden timestamp data from script tags or invent seconds.
   const iso=`${m[3]}-${m[4].padStart(2,'0')}-${m[5].padStart(2,'0')}T${m[1].padStart(2,'0')}:${m[2]}:00+08:00`;
   if(!Number.isFinite(Date.parse(iso)))throw new ObservationError('WEB_SOURCE_TIMESTAMP_NOT_VISIBLE');
   published=iso;precision='minute';
  }
  const value=await text.innerText();
  if(!display||!value.trim()||!published||!Number.isFinite(Date.parse(published))||
    !/(?:Z|[+-]\d{2}:\d{2})$/.test(published)||Date.parse(published)>Date.now()+300000)
   throw new ObservationError('WEB_SOURCE_TEXT_OR_TIMESTAMP_INVALID');
  return {identity:identityRead,text:value,name:display,published_at:published,timestamp_precision:precision,layout:identityRead.layout,
   profile_image_url:identityRead.profile_image_url};
 };
 const observed=await read();
 const facts=machineSource?await article.evaluate(inspectPlainTextSource):null;
 await dismissOrdinaryPrompts(page,path.join(evidenceDir,'final-prompts'));
 await assertSourceVisible(page,article,path.join(evidenceDir,'after'));
 if(JSON.stringify(observed)!==JSON.stringify(await read()))throw new ObservationError('WEB_SOURCE_CHANGED_DURING_CAPTURE');
 if(facts&&canonical(facts)!==canonical(await article.evaluate(inspectPlainTextSource)))throw new ObservationError('WEB_SOURCE_CHANGED_DURING_CAPTURE');
 const screenshot=path.join(evidenceDir,'article.png');
 await article.screenshot({path:screenshot,timeout:10000});await fs.chmod(screenshot,0o600);
 await assertSourceVisible(page,article,path.join(evidenceDir,'after-screenshot'));
 if(JSON.stringify(observed)!==JSON.stringify(await read()))throw new ObservationError('WEB_SOURCE_CHANGED_DURING_CAPTURE');
 if(facts&&canonical(facts)!==canonical(await article.evaluate(inspectPlainTextSource)))throw new ObservationError('WEB_SOURCE_CHANGED_DURING_CAPTURE');
 const capturedAt=new Date().toISOString();
 const payload={post:{id:identity.postId,text:observed.text,created_at:observed.published_at,
   timestamp_precision:observed.timestamp_precision,referenced_tweets:[]},
  author:{name:observed.name,username:identity.handle},
  url:`https://x.com/${identity.handle}/status/${identity.postId}`,source_api:'x-web-dom-v1',
  // Visible text is observed. Hidden attachments and relationships remain unknown.
  source_media_count:null,references_observed:false,
  capture:{protocol:'x-web-dom-observation-v1',captured_at:capturedAt,collector_trigger:trigger,
   scheduled_server_collection:trigger==='durable_scheduler',
   source_screenshot_reviewed:false,screenshot_sha256:hash(await fs.readFile(screenshot)),
   visibility_version:'tweet-visibility-v2',dom_layout:observed.layout,visibility_passed:true,used_ocr:false,
   human_review:false,official_api_receipt:false,full_source_completeness_verified:false},
  intake_claim:'Server read rendered public X DOM; original media, references and translation require genuine review.'};
 if(observed.profile_image_url){
  const avatar=new URL(observed.profile_image_url);
  if(avatar.protocol==='https:'&&avatar.hostname==='pbs.twimg.com')payload.author.profile_image_url=avatar.href;
 }
 if(facts){payload.capture.protocol='x-web-dom-observation-v2';payload.capture.machine_facts_sha256=hash(canonical(facts));
  payload.intake_claim='Rendered public DOM observed; machine policy supports only complete visible plain text. Human and official API review are not claimed.';}
 const record={version:'x-web-dom-observation-v1',payload,screenshot_file:screenshot,
  payload_sha256:hash(JSON.stringify(payload)),prompt_actions:prompts.actions,human_review:false,
  observed_source_identity:observed.identity,native_positioning:positioning};
 if(facts){record.version='x-web-dom-observation-v2';record.machine_source_facts=facts;}
 await fs.writeFile(path.join(evidenceDir,'observation.json'),JSON.stringify(record,null,2),{mode:0o600});
 return record;
}
