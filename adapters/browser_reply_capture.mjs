// A review draft of one visible two-post conversation. No relationship approval.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {captureTweet} from './browser_tweet_capture.mjs';
import {dismissOrdinaryPrompts,inspectArticleElement,ObservationError} from './browser_observation.mjs';
const hash=value=>crypto.createHash('sha256').update(value).digest('hex');

export async function captureReplyDraft(page,identity,evidenceDir){
 await fs.mkdir(evidenceDir,{recursive:true,mode:0o700});
 await dismissOrdinaryPrompts(page,path.join(evidenceDir,'conversation-prompts'));
 const articles=await page.locator('article').all();
 const identities=[];
 for(const article of articles)identities.push(await article.evaluate(inspectArticleElement));
 const own=identities.flatMap((p,i)=>p?.handle.toLowerCase()===identity.handle.toLowerCase()&&p.postId===identity.postId?[i]:[]);
 if(own.length!==1||own[0]!==1||!identities[0]?.author_verified)
  throw new ObservationError('WEB_REPLY_PARENT_OR_CHAIN_REQUIRES_REVIEW');
 const parentIdentity=identities[0];
 if(parentIdentity.postId===identity.postId)throw new ObservationError('WEB_REPLY_PARENT_OR_CHAIN_REQUIRES_REVIEW');
 const url=new URL(page.url());
 if(url.hostname!=='x.com'||url.pathname!==`/${identity.handle}/status/${identity.postId}`)
  throw new ObservationError('WEB_REPLY_SOURCE_URL_CHANGED');
 const conversationImage=path.join(evidenceDir,'conversation.png');
 await page.screenshot({path:conversationImage,fullPage:false});
 const reply=await captureTweet(page,identity,path.join(evidenceDir,'reply'),{pairedReplyDraft:true});
 const parentUrl=`https://x.com/${parentIdentity.handle}/status/${parentIdentity.postId}`;
 const response=await page.goto(parentUrl,{waitUntil:'domcontentloaded'});
 if(response?.status()!==200)throw new ObservationError('WEB_REPLY_PARENT_UNAVAILABLE');
 await page.locator('article').first().waitFor({state:'visible'});
 const parent=await captureTweet(page,{handle:parentIdentity.handle,postId:parentIdentity.postId},path.join(evidenceDir,'parent'));
 const relation={version:'reply-context-observation-v1',source_url:reply.payload.url,parent_url:parentUrl,
  parent_post_id:parentIdentity.postId,visible_article_order:identities.slice(0,2),
  basis:'one_preceding_article_in_visible_conversation',direct_parent_independently_verified:false,
  requires_human_relationship_review:true,human_review:false,official_api:false,
  conversation_screenshot:conversationImage,conversation_screenshot_sha256:hash(await fs.readFile(conversationImage)),
  reply_payload_sha256:hash(JSON.stringify(reply.payload)),parent_payload_sha256:hash(JSON.stringify(parent.payload))};
 const relationFile=path.join(evidenceDir,'relationship.json');
 await fs.writeFile(relationFile,JSON.stringify(relation,null,2),{mode:0o600});
 // Retain the distinction between an observed candidate and an approved fact.
 reply.payload.post.referenced_tweets=[{type:'replied_to',id:parentIdentity.postId}];
 reply.payload.reply_context={version:'reply-context-draft-v1',parent_source:parent.payload,
  relationship_evidence_file:relationFile,relationship_evidence_sha256:hash(await fs.readFile(relationFile)),
  relationship_verified_by_human:false,parent_candidate_only:true};
 const record={version:'reply-context-draft-v1',reply,parent,relationship:relation,
  automatic_publication_eligible:false,human_review:false};
 await fs.writeFile(path.join(evidenceDir,'reply-context.json'),JSON.stringify(record,null,2),{mode:0o600});
 return record;
}
