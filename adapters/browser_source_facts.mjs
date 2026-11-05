// Machine policy evidence from the rendered article, never an API/human claim.
// Deliberately bounded: uncertain cards, media and conversation context stop v1.
export function inspectPlainTextSource(article) {
 const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;};
 const bodies=[...article.querySelectorAll('[data-testid="tweetText"],.whitespace-pre-wrap[dir="auto"]')].filter(visible);
 const body=bodies.length===1?bodies[0]:null;
 const text=body?.innerText||'';
 const urls=[];
 for(const a of body?.querySelectorAll('a[href]')||[]) {
  if(!visible(a))continue;
  const u=new URL(a.href,location.href);
  if(u.protocol!=='https:'||u.username||u.password)urls.push({visible:a.innerText,href:null});
  else urls.push({visible:a.innerText,href:u.href});
 }
 const images=[...article.querySelectorAll('img')].filter(visible).map(e=>({src:e.currentSrc||e.src,alt:e.alt,
  loaded:e.complete&&e.naturalWidth>0,inside_body:!!body?.contains(e)}));
 const foreignImages=images.filter(i=>{
  try{const u=new URL(i.src);return !(!i.inside_body&&u.hostname==='pbs.twimg.com'&&u.pathname.startsWith('/profile_images/'))&&
   !(u.hostname==='abs-0.twimg.com'&&u.pathname.includes('/emoji/'))&&!(u.hostname==='abs.twimg.com'&&u.pathname.includes('/emoji/'));}
  catch{return true;}
 });
 const statusLinks=[...article.querySelectorAll('a[href]')].filter(visible).map(e=>e.href)
  .filter(u=>/\/(?:status)\/\d+/.test(u));
 const statusIds=[...new Set(statusLinks.map(u=>/\/status\/(\d+)/.exec(u)?.[1]).filter(Boolean))];
 const extraLinks=[...article.querySelectorAll('a[href]')].filter(e=>visible(e)&&!body?.contains(e)).map(e=>e.href).filter(h=>{
  try{const u=new URL(h);return !['x.com','www.x.com','twitter.com','www.twitter.com'].includes(u.hostname);}catch{return true;}
 });
 const reply=article.ownerDocument.querySelector('article')!==article||/^(?:回复|Replying to)\s*@/mi.test(article.innerText);
 const showMore=[...article.querySelectorAll('button,a,[role="button"]')].filter(visible).some(e=>
  /^(?:显示更多|顯示更多|Show more|Read more)$/i.test(e.innerText.trim()));
 const clipped=!!body&&(body.scrollHeight>body.clientHeight+1||body.scrollWidth>body.clientWidth+1||
  !['none','0','auto'].includes(getComputedStyle(body).webkitLineClamp));
 // XDS cards need not retain legacy test IDs, image tags or external links.
 // Preserve a concrete inventory instead of treating any same-site card as UI.
 const specialCards=[];
 const ownId=statusIds.length===1?statusIds[0]:null;
 const primary=[...article.querySelectorAll('a[href]')].find(a=>{
  try{const u=new URL(a.href);return ['x.com','www.x.com','twitter.com','www.twitter.com'].includes(u.hostname)&&
   new RegExp(`^/[A-Za-z0-9_]+/status/${ownId}/?$`).test(u.pathname)&&(a.querySelector('time')||/[·•]/.test(a.innerText));}catch{return false;}
 });
 const ownHandle=primary?new URL(primary.href).pathname.split('/')[1]:null;
 const profileLinks=[...article.querySelectorAll('a[href]')].filter(a=>{
  try{const u=new URL(a.href);return ownHandle&&['x.com','www.x.com','twitter.com','www.twitter.com'].includes(u.hostname)&&
   u.pathname.replace(/\/$/,'').toLowerCase()==='/'+ownHandle.toLowerCase();}catch{return false;}
 });
 const inProfile=e=>profileLinks.some(a=>a.contains(e));
 const legacyIdentity=article.querySelector('[data-testid="User-Name"]');
 // For XDS, the common author/handle ancestor must stop before the body and
 // must not expand to the whole article. This bounds the blue-badge exemption.
 let identity=profileLinks.filter(a=>a.innerText.trim())[0]||legacyIdentity;
 const textProfiles=profileLinks.filter(a=>a.innerText.trim());
 if(textProfiles.length>=2){while(identity&&!identity.contains(textProfiles[1]))identity=identity.parentElement;
  if(identity===article||identity?.contains(body))identity=null;}
 const isIdentity=e=>!!legacyIdentity?.contains(e)||!!identity?.contains(e)||inProfile(e);
 const smallControl=e=>{const svg=e.getBoundingClientRect(),button=e.closest('button,[role="button"]');
  if(!button)return false;const b=button.getBoundingClientRect();return svg.width<=64&&svg.height<=64&&b.width<=200&&b.height<=80;};
 // Observed XDS UI (2026-11-01): these two small icons are not buttons.
 // Bind exceptions to their exact UI container/label and this source ID;
 // neither a general Grok drawing nor an arbitrary compose link is allowed.
 const observedUiIcon=e=>{
  if(body?.contains(e)||e.getAttribute('role')!=='img'||e.getAttribute('aria-hidden')!=='true')return false;
  const r=e.getBoundingClientRect(),parent=e.parentElement,group=parent?.parentElement;
  if(e.getAttribute('data-icon')==='icon-logo-grok'&&r.width<=20&&r.height<=20&&
    parent?.tagName==='SPAN'&&parent.classList.contains('text-secondary')&&group?.tagName==='DIV'&&
    group.classList.contains('flex')&&group.classList.contains('items-center')&&group.classList.contains('gap-1')&&
    !group.contains(body)&&['显示翻译','Show translation'].includes(group.innerText.trim())&&
    group.querySelectorAll('svg').length===1&&/^m9\.27(?:\s|[,.\d-])/.test(e.querySelector('path')?.getAttribute('d')||''))return true;
  const link=parent?.parentElement,use=e.querySelector('use');
  if(r.width>24||r.height>24||parent?.tagName!=='SPAN'||!parent.classList.contains('x-engagement-pill')||
    link?.tagName!=='A'||!link.classList.contains('x-link')||!link.classList.contains('x-engagement-button')||
    e.querySelectorAll('use').length!==1||use?.getAttribute('href')!=='#icon-reply'||
    e.querySelector('path,rect,text,image,foreignObject'))return false;
  try{
   const u=new URL(link.getAttribute('href'),primary?.href||article.ownerDocument.location.href);
   const accessible=/^\s*([\d.,万亿kKmM]+)\s+(?:回复[.。]?\s*回复|replies?\.\s*reply)\s*$/iu.exec(link.getAttribute('aria-label')||'');
   const count=parent.innerText.trim();
   // Observed on a public reply detail: native zero replies omit the printed
   // count while retaining the exact "0 回复. 回复" accessibility label. This
   // exception stays bound to this source's native compose action and icon.
   const countMatches=!!accessible&&(count===accessible[1]||count===''&&accessible[1]==='0');
   return ['x.com','www.x.com','twitter.com','www.twitter.com'].includes(u.hostname)&&u.protocol==='https:'&&
    !u.username&&!u.password&&u.pathname==='/compose/post'&&u.searchParams.get('in_reply_to')===ownId&&
    [...u.searchParams.keys()].length===1&&
    countMatches;
  }catch{return false;}
 };
 for(const a of article.querySelectorAll('a[href]')){
  if(!visible(a)||body?.contains(a))continue;
  try{const u=new URL(a.href);if(['x.com','www.x.com','twitter.com','www.twitter.com'].includes(u.hostname)&&
   /^\/i\/(?:article|spaces|broadcasts|communities)\//i.test(u.pathname))specialCards.push('internal_content_card');}catch{}
 }
 for(const e of article.querySelectorAll('[role="radiogroup"],[role="radio"],[data-testid*="poll" i],blockquote,[role="link"]:not(a)'))
  if(visible(e))specialCards.push(e.matches('[role="link"]')?'non_anchor_link_card':e.matches('blockquote')?'quote_block':'poll');
 let cssMedia=0,unclassifiedSvg=0;
 for(const e of [article,...article.querySelectorAll('*')]){
  if(!visible(e))continue;const style=getComputedStyle(e),r=e.getBoundingClientRect();
  if(style.backgroundImage!=='none'){
   const profileBackground=!body?.contains(e)&&isIdentity(e)&&r.width<=80&&r.height<=80&&
    /https:\/\/pbs\.twimg\.com\/profile_images\//.test(style.backgroundImage);
   if(!profileBackground&&!smallControl(e))cssMedia++;
  }
  if(e.matches('svg')){
   const smallBadge=isIdentity(e)&&!body?.contains(e)&&r.width<=48&&r.height<=48;
   if(e.querySelector('image,foreignObject')||(!smallControl(e)&&!smallBadge&&!observedUiIcon(e)))unclassifiedSvg++;
  }
 }
 const unsupported=article.querySelectorAll('video,audio,canvas,iframe,[data-testid="card.wrapper"],[data-testid="tweetPhoto"],[data-testid="videoPlayer"],[data-testid="quoteTweet"]').length+
  specialCards.length+cssMedia+unclassifiedSvg;
 const failures=[];
 if(!body||!text.trim())failures.push('BODY_AMBIGUOUS');
 if(reply)failures.push('REPLY_CONTEXT_REQUIRED');
 if(statusIds.length!==1)failures.push('QUOTE_OR_STATUS_CONTEXT_AMBIGUOUS');
 if(foreignImages.length||unsupported||extraLinks.length)failures.push('MEDIA_OR_CARD_UNSUPPORTED');
 if(images.some(i=>!i.loaded))failures.push('IMAGE_NOT_LOADED');
 // innerText does not include an <img>'s alt text. Do not silently lose an
 // emoji represented as a bitmap even if it comes from the platform emoji CDN.
 if(images.some(i=>i.inside_body))failures.push('BODY_IMAGE_TEXT_AMBIGUOUS');
 if(showMore||clipped)failures.push('TEXT_TRUNCATED');
 // A displayed ellipsis/link shorthand is not full original text. Later versions
 // may normalize it with an independently bound link inventory.
 if(urls.some(x=>!x.href||/…|\.\.\.$/.test(x.visible)))failures.push('LINK_DISPLAY_TRUNCATED');
 return {version:'x-visible-plain-text-v1',observed_text:text,body_count:bodies.length,status_ids:statusIds,
  ui_classification_version:'xds-source-ui-icons-v2',
  images,foreign_image_count:foreignImages.length,unsupported_elements:unsupported,extra_links:extraLinks,body_links:urls,
  special_card_kinds:specialCards,css_media_count:cssMedia,unclassified_svg_count:unclassifiedSvg,
  preceding_or_reply:reply,show_more:showMore,clipped,scope:'rendered_public_plain_text_without_media_cards_or_context',
  supported:failures.length===0,failures,human_review:false,official_api:false};
}
