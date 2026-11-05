// Deterministic visible-link text normalization. Never dereferences a short URL.
import crypto from 'node:crypto';
import {locateTweetArticle,inspectArticleElement,ObservationError} from './browser_observation.mjs';
const canonical=x=>JSON.stringify(x,(_,v)=>v&&typeof v==='object'&&!Array.isArray(v)?Object.fromEntries(Object.keys(v).sort().map(k=>[k,v[k]])):v);
const hash=b=>crypto.createHash('sha256').update(b).digest('hex');

// Called in the page with one exact article body and its independently selected
// source identity. Positions are bound to the original innerText, not to hidden
// script state or a guessed expanded URL.
export function inspectMachineLinkText(body,identity){
 const original=body.innerText,anchors=[],failures=[];
 const visible=e=>{const r=e.getBoundingClientRect();if(r.width<=0||r.height<=0)return false;
  for(let p=e;p;p=p.parentElement){const s=getComputedStyle(p);if(s.display==='none'||s.visibility!=='visible'||Number(s.opacity)<.01)return false;}
  return true;};
 const article=body.closest('article'),style=getComputedStyle(body);
 if(!visible(body)||body.scrollHeight>body.clientHeight+1||body.scrollWidth>body.clientWidth+1||
  !['none','0','auto'].includes(style.webkitLineClamp))failures.push('LINK_BODY_NOT_COMPLETE_VISIBLE_TEXT');
 if(body.querySelector('img,svg,video,audio,canvas,iframe'))failures.push('LINK_BODY_NON_TEXT_CONTENT');
 if(article&&[...article.querySelectorAll('[data-testid="tweetText"],.whitespace-pre-wrap[dir="auto"]')].filter(visible).length!==1)
  failures.push('LINK_BODY_AMBIGUOUS');
 if([...(article||body).querySelectorAll('button,a,[role="button"]')].filter(visible).some(e=>
  /^(?:显示更多|顯示更多|Show more|Read more)$/i.test(e.innerText.trim())))failures.push('LINK_BODY_TRUNCATED');
 const points=[];let bodyTextVisible=true,node;const walker=body.ownerDocument.createTreeWalker(body,NodeFilter.SHOW_TEXT);
 while((node=walker.nextNode())){if(!node.textContent.trim()||node.parentElement.closest('script,style,noscript,template'))continue;
  if(!visible(node.parentElement)){bodyTextVisible=false;continue;}const range=body.ownerDocument.createRange();range.selectNodeContents(node);
  for(const r of range.getClientRects()){if(r.width<1||r.height<1)continue;
   if(r.left<0||r.top<0||r.right>innerWidth||r.bottom>innerHeight){bodyTextVisible=false;continue;}
   for(const f of [.05,.5,.95]){const x=r.left+r.width*f,y=r.top+r.height/2,top=body.ownerDocument.elementFromPoint(x,y);
    points.push({x,y});if(!(top===body||body.contains(top)))bodyTextVisible=false;}}
 }
 if([...body.ownerDocument.querySelectorAll('body *')].some(e=>{
  if(e===body||e.contains(body)||body.contains(e))return false;const s=getComputedStyle(e),r=e.getBoundingClientRect();
  return ['fixed','absolute','sticky'].includes(s.position)&&s.pointerEvents==='none'&&Number(s.zIndex)>0&&visible(e)&&
   (s.backgroundImage!=='none'||!['transparent','rgba(0, 0, 0, 0)'].includes(s.backgroundColor))&&
   points.some(p=>p.x>r.left&&p.x<r.right&&p.y>r.top&&p.y<r.bottom);
 }))bodyTextVisible=false;
 if(!bodyTextVisible)failures.push('LINK_BODY_NOT_COMPLETE_VISIBLE_TEXT');
 let previousEnd=-1;
 for(const [order,a]of [...body.querySelectorAll('a')].entries()){
  const label=a.innerText,raw=a.getAttribute('href'),rect=a.getBoundingClientRect();let href=null,kind='invalid';
  const itemFailures=[];
  if(!visible(a))itemFailures.push('LINK_ANCHOR_NOT_VISIBLE');
  if(a.querySelector('a,[role="link"],img,svg,video,canvas,iframe'))itemFailures.push('LINK_ANCHOR_NESTED_OR_NON_TEXT');
  if(!label||/[\r\n]/.test(label))itemFailures.push('LINK_LABEL_EMPTY_OR_MULTILINE');
  const start=label?original.indexOf(label):-1,end=start+label.length;
  if(start<0||original.indexOf(label,start+Math.max(1,label.length))!==-1)itemFailures.push('LINK_LABEL_AMBIGUOUS');
  if(start<previousEnd)itemFailures.push('LINK_ANCHOR_ORDER_AMBIGUOUS');
  previousEnd=end;
  try{
   if(!raw?.trim()||raw.trim()==='#')throw new Error();
   const u=new URL(a.href,body.ownerDocument.baseURI);
   if(u.protocol!=='https:'||!u.hostname||u.username||u.password)throw new Error();href=u.href;
   const site=['x.com','www.x.com','twitter.com','www.twitter.com'].includes(u.hostname);
   kind=site&&/^#[^\s]+$/u.test(label)?'preserve_hashtag':site&&/^@[A-Za-z0-9_]+$/.test(label)?'preserve_mention':'replace_with_actual_dom_href';
   if(kind==='preserve_mention'&&u.pathname.replace(/\/$/,'').toLowerCase()!=='/'+label.slice(1).toLowerCase())itemFailures.push('LINK_MENTION_TARGET_MISMATCH');
  }catch{itemFailures.push('LINK_HREF_PROTOCOL_OR_TARGET_INVALID');}
  // Link text clipped by an ancestor or covered by an overlay is not a visible
  // label. Checking every client rect also covers a wrapped inline anchor.
  const rects=[...a.getClientRects()].filter(r=>r.width>0&&r.height>0);
  if(!rects.length||rects.some(r=>r.left<0||r.top<0||r.right>innerWidth||r.bottom>innerHeight||
   [.05,.5,.95].some(f=>{const top=body.ownerDocument.elementFromPoint(r.left+r.width*f,r.top+r.height/2);return !(top===a||a.contains(top));})))
   itemFailures.push('LINK_ANCHOR_NOT_VISIBLE');
  anchors.push({order,label,href_attribute:raw,actual_href:href,kind,start_utf16:start,end_utf16:end,
   start_codepoint:start>=0?[...original.slice(0,start)].length:null,end_codepoint:start>=0?[...original.slice(0,end)].length:null,
   rect:{x:rect.x,y:rect.y,width:rect.width,height:rect.height},visible:itemFailures.length===0,failures:[...new Set(itemFailures)]});
  failures.push(...itemFailures);
 }
 return {version:'machine-link-dom-v1',source_url:identity.source_url,document_base_uri:body.ownerDocument.baseURI,post_id:identity.post_id,handle:identity.handle,
  original_inner_text:original,body_visible_text_verified:bodyTextVisible,anchors,failures:[...new Set(failures)],supported:failures.length===0,
  body_scope:'complete_visible_selected_body_only',human_review:false,official_api:false,
  url_targets_dereferenced:false,expanded_destination_inferred:false};
}

export async function captureMachineLinkText(page,identity){
 const article=await locateTweetArticle(page,identity),observed=await article.evaluate(inspectArticleElement);
 const body=article.locator(observed.layout==='legacy-dom-v1'?'[data-testid="tweetText"]':'.whitespace-pre-wrap[dir="auto"]');
 if(await body.count()!==1)throw new ObservationError('LINK_BODY_AMBIGUOUS');
 return body.evaluate(inspectMachineLinkText,{source_url:`https://x.com/${identity.handle}/status/${identity.postId}`,
  post_id:identity.postId,handle:identity.handle});
}

export function normalizeMachineLinkPayload(source,observation){
 try{
  const original=source.post.text;
  if(observation.version!=='machine-link-dom-v1'||observation.supported!==true||observation.body_visible_text_verified!==true||observation.failures.length||
   observation.human_review!==false||observation.official_api!==false||observation.url_targets_dereferenced!==false||
   observation.expanded_destination_inferred!==false||observation.source_url!==source.url||observation.post_id!==source.post.id||
   observation.handle!==source.author.username||observation.original_inner_text!==original||source.text_normalization||
   source.url!==`https://x.com/${source.author.username}/status/${source.post.id}`||!/^\d{1,30}$/.test(source.post.id))throw new Error();
  let result='',cursor=0;const operations=[],unchanged=[];
  for(const [order,item]of observation.anchors.entries()){
   const {start_utf16:start,end_utf16:end,label,actual_href:href,kind}=item;
   if(item.order!==order||item.visible!==true||item.failures.length||typeof label!=='string'||!label||/[\r\n]/.test(label)||
    !Number.isInteger(start)||!Number.isInteger(end)||start<cursor||end!==start+label.length||original.slice(start,end)!==label||
    original.indexOf(label)!==start||original.indexOf(label,end)!==-1||
    item.start_codepoint!==[...original.slice(0,start)].length||item.end_codepoint!==[...original.slice(0,end)].length)throw new Error();
   const u=new URL(href);if(u.protocol!=='https:'||!u.hostname||u.username||u.password||u.href!==href)throw new Error();
   const actual=new URL(item.href_attribute,observation.document_base_uri).href;if(actual!==href)throw new Error();
   const site=['x.com','www.x.com','twitter.com','www.twitter.com'].includes(u.hostname);
   const expected=site&&/^#[^\s]+$/u.test(label)?'preserve_hashtag':site&&/^@[A-Za-z0-9_]+$/.test(label)?'preserve_mention':'replace_with_actual_dom_href';
   if(kind!==expected||kind==='preserve_mention'&&u.pathname.replace(/\/$/,'').toLowerCase()!=='/'+label.slice(1).toLowerCase())throw new Error();
   const before=original.slice(cursor,start);result+=before;
   unchanged.push({start_utf16:cursor,end_utf16:start,sha256:hash(before)});
   const replacement=kind==='replace_with_actual_dom_href'?href:label;result+=replacement;
   operations.push({order,kind,start_utf16:start,end_utf16:end,label,actual_href:href,replacement});cursor=end;
  }
  const tail=original.slice(cursor);result+=tail;unchanged.push({start_utf16:cursor,end_utf16:original.length,sha256:hash(tail)});
  if(result.split('\n').length!==original.split('\n').length)throw new Error();
  const sourceHash=hash(canonical(source)),observationHash=hash(canonical(observation)),originalHash=hash(original);
  const derived=structuredClone(source);derived.post.text=result;
  derived.text_normalization={version:'machine-link-text-v1',source_payload_sha256:sourceHash,
   observation_sha256:observationHash,original_inner_text_sha256:originalHash,
   rule:'replace_only_unique_visible_external_anchor_labels_with_their_actual_dom_href'};
  const evidence={version:'machine-link-text-v1',source_url:source.url,source_payload_sha256:sourceHash,
   observation_sha256:observationHash,original_inner_text:original,original_inner_text_sha256:originalHash,
   normalized_text:result,normalized_text_sha256:hash(result),derived_payload_sha256:hash(canonical(derived)),
   operations,unchanged_regions:unchanged,newlines_preserved:true,normalized_visible_body_complete:true,
   unrendered_or_hidden_source_verified:false,human_review:false,official_api:false,url_targets_dereferenced:false,
   expanded_destination_inferred:false,canonicalization:'node-json-sorted-keys-v1'};
  return {derived_payload:derived,evidence};
 }catch{throw new ObservationError('MACHINE_LINK_NORMALIZATION_INVALID');}
}

export function verifyMachineLinkNormalization(source,observation,packet){
 try{const expected=normalizeMachineLinkPayload(source,observation);
  if(canonical(expected)!==canonical(packet))throw new Error();
  return {version:'machine-link-integrity-v1',integrity_verified:true,derived_payload_sha256:expected.evidence.derived_payload_sha256,
   normalized_visible_body_complete:true,human_review:false,expanded_destination_inferred:false};
 }catch{throw new ObservationError('MACHINE_LINK_NORMALIZATION_INTEGRITY_INVALID');}
}
