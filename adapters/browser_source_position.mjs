// One ordinary native scroll before source evidence is collected. This helper
// never decides visibility/completeness, dismisses protection or changes DOM/CSS.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {inspectArticleElement,locateTweetArticle,ObservationError} from './browser_observation.mjs';
const canonical=x=>JSON.stringify(x,(_,v)=>v&&typeof v==='object'&&!Array.isArray(v)?Object.fromEntries(Object.keys(v).sort().map(k=>[k,v[k]])):v);
const hash=b=>crypto.createHash('sha256').update(b).digest('hex');
const check=(value,code)=>{if(!value)throw new ObservationError(code);};

async function snapshot(page,articles,identities,range){
 const sources=[];
 for(let i=0;i<articles.length;i++){
  const article=articles[i],identity=await article.evaluate(inspectArticleElement),wanted=identities[i];
  check(identity?.author_verified&&identity.handle.toLowerCase()===wanted.handle.toLowerCase()&&identity.postId===wanted.postId,
   'WEB_SOURCE_AUTHOR_UNVERIFIED');
  const bodies=await article.evaluate(e=>[...e.querySelectorAll('[data-testid="tweetText"],.whitespace-pre-wrap[dir="auto"]')]
   .map(body=>({text:body.innerText,own_article:body.closest('article')===e})));
  sources.push({identity,bodies});
 }
 const geometry=await range.evaluate(e=>{const r=e.getBoundingClientRect();return {rect:{left:r.left,top:r.top,right:r.right,bottom:r.bottom,width:r.width,height:r.height},
  viewport:{width:innerWidth,height:innerHeight},scroll_x:scrollX,scroll_y:scrollY};});
 return {page_url:page.url(),sources,...geometry};
}

async function position(page,articles,identities,range,evidenceDir,scope,structure=null){
 check(await range.count()===1,'WEB_SOURCE_POSITION_RANGE_AMBIGUOUS');
 const before=await snapshot(page,articles,identities,range);
 const structureBefore=structure?await structure():null;
 // Exactly one native operation. No fallback, resizing, synthetic clicks,
 // geometric tolerance or retry after a failed visibility result is provided.
 await range.evaluate(e=>e.scrollIntoView({block:'center',inline:'nearest',behavior:'instant'}));
 await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
 let after=null,error=null,structureAfter=null;
 try{
  after=await snapshot(page,articles,identities,range);
  check(before.page_url===after.page_url&&canonical(before.sources)===canonical(after.sources)&&
   canonical(before.viewport)===canonical(after.viewport),'WEB_SOURCE_CHANGED_DURING_POSITIONING');
  structureAfter=structure?await structure():null;
  check(canonical(structureBefore)===canonical(structureAfter),'WEB_SOURCE_CHANGED_DURING_POSITIONING');
 }catch(e){error=e.code||'WEB_SOURCE_CHANGED_DURING_POSITIONING';}
 const value={version:'native-source-positioning-v1',scope,native_action:'scrollIntoView:center',native_action_count:1,
  before,after,structure_before:structureBefore,structure_after:structureAfter,error,
  visibility_verified_by_positioning:false,human_review:false,official_api:false};
 await fs.mkdir(evidenceDir,{recursive:true,mode:0o700});
 const file=path.join(evidenceDir,'positioning.json'),bytes=Buffer.from(JSON.stringify(value,null,2));
 await fs.writeFile(file,bytes,{flag:'wx',mode:0o600});
 if(error)throw new ObservationError(error==='WEB_SOURCE_AUTHOR_UNVERIFIED'?'WEB_SOURCE_CHANGED_DURING_POSITIONING':error);
 return {file,sha256:hash(bytes),version:value.version,scope,visibility_verified_by_positioning:false};
}

export async function positionIndependentSource(page,identity,article,evidenceDir){
 return position(page,[article],[identity],article,evidenceDir,'independent_article');
}

export async function positionReplyThread(page,identity,evidenceDir){
 const reply=await locateTweetArticle(page,identity),articles=await page.locator('article').all(),identities=[];
 for(const article of articles)identities.push(await article.evaluate(inspectArticleElement));
 const matches=identities.flatMap((value,index)=>value?.handle.toLowerCase()===identity.handle.toLowerCase()&&value.postId===identity.postId?[index]:[]);
 check(matches.length===1&&matches[0]===1&&identities[0]?.author_verified,'WEB_REPLY_V2_PARENT_OR_CHAIN_AMBIGUOUS');
 const parent=await locateTweetArticle(page,{handle:identities[0].handle,postId:identities[0].postId});
 const range=reply.locator('xpath=ancestor::li[1]/parent::ul');
 check(await range.count()===1,'WEB_REPLY_V2_LIST_TOPOLOGY_UNSUPPORTED');
 const structure=async()=>{
  const parentHandle=await parent.elementHandle(),replyHandle=await reply.elementHandle();
  try{const value=await range.evaluate((ul,{parent,reply})=>{
   const children=[...ul.children],articles=[...ul.querySelectorAll('article')];
   return {tag:ul.tagName,children:children.length,article_count:articles.length,
    items:children.map(li=>({tag:li.tagName,article_count:li.querySelectorAll('article').length})),
    adjacent:children[0]?.nextElementSibling===children[1],
    owns_exact_parent_then_reply:articles[0]===parent&&articles[1]===reply&&
     parent.closest('li')===children[0]&&reply.closest('li')===children[1]};
  },{parent:parentHandle,reply:replyHandle});
   check(value.tag==='UL'&&value.children===2&&value.article_count===2&&value.adjacent&&value.owns_exact_parent_then_reply&&
    value.items.every(item=>item.tag==='LI'&&item.article_count===1),'WEB_REPLY_V2_LIST_TOPOLOGY_UNSUPPORTED');
   return value;
  }
  finally{await parentHandle?.dispose();await replyHandle?.dispose();}
 };
 const before=await structure();
 check(before.tag==='UL'&&before.children===2&&before.article_count===2&&before.adjacent&&before.owns_exact_parent_then_reply&&
  before.items.every(item=>item.tag==='LI'&&item.article_count===1),'WEB_REPLY_V2_LIST_TOPOLOGY_UNSUPPORTED');
 return position(page,[parent,reply],[{handle:identities[0].handle,postId:identities[0].postId},identity],range,evidenceDir,'reply_parent_and_reply_union',structure);
}
