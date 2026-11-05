// Read rendered first-party DOM. Closing an ordinary dismissible prompt never
// marks human review complete, solves a challenge or alters a site's DOM/CSS.
import fs from 'node:fs/promises';
import path from 'node:path';

export class ObservationError extends Error {
 constructor(code){super(code);this.code=code;}
}
const security=/captcha|人机验证|安全验证|验证身份|verify (?:that )?you are human|unusual activity|请求过于频繁|机器人验证/i;
const closeNames=['关闭','Close','Dismiss','稍后再说','Not now','No thanks','不用了'];

async function screenshot(page,dir,name){
 await fs.mkdir(dir,{recursive:true,mode:0o700});
 const file=path.join(dir,name);await page.screenshot({path:file,fullPage:false,timeout:10000});
 await fs.chmod(file,0o600);return file;
}

export async function dismissOrdinaryPrompts(page,evidenceDir){
 const actions=[];
 for(let round=0;round<3;round++){
  const dialogs=await page.locator('[role="dialog"],[role="alertdialog"]').all();
  let changed=false;
  for(const dialog of dialogs){
   if(!await dialog.isVisible())continue;
   const text=await dialog.innerText({timeout:3000});
   await screenshot(page,evidenceDir,`prompt-${round}-before.png`);
   if(security.test(text))throw new ObservationError('WEB_HUMAN_VERIFICATION_REQUIRED');
   let button,selectedName;
   for(const name of closeNames){
    const candidate=dialog.getByRole('button',{name,exact:true});
    if(await candidate.count()===1&&await candidate.isVisible()){button=candidate;selectedName=name;break;}
   }
   if(!button)throw new ObservationError('WEB_NONDISMISSIBLE_PROMPT');
   await button.click();
   await dialog.waitFor({state:'hidden',timeout:5000});
   actions.push({action:'ordinary_ui_dismiss',button_name:selectedName});
   changed=true;break;
  }
  if(!changed)return {version:'ordinary-prompt-handling-v1',actions,human_review:false};
 }
 throw new ObservationError('WEB_REPEATING_PROMPT');
}

// Two observed public layouts: legacy test IDs and the current XDS rendered UI.
// This function only inspects visible links/text, never scripts or app state.
export function inspectArticleElement(e){
 const links=[...e.querySelectorAll('a[href]')].filter(a=>a.closest('article')===e);
 const parsed=links.flatMap(a=>{
  try{const u=new URL(a.getAttribute('href'),e.ownerDocument.location.href);
   return ['x.com','www.x.com','twitter.com','www.twitter.com'].includes(u.hostname)?[{a,u,text:a.innerText.trim()}]:[];
  }catch{return [];}
 });
 const date=/^(?:\d{1,2}:\d{2}\s*[·•]\s*\d{4}年\d{1,2}月\d{1,2}日|(?:\d{4}年)?\d{1,2}月\d{1,2}日|昨天|前天|\d+\s*(?:秒钟?|分钟?|小时|天)(?:前)?)$/;
 const primary=parsed.find(({a,u,text})=>/^\/[A-Za-z0-9_]+\/status\/\d+\/?$/.test(u.pathname)&&(a.querySelector('time')||date.test(text)));
 if(!primary)return null;
 const [,handle,postId]=/^\/([A-Za-z0-9_]+)\/status\/(\d+)\/?$/.exec(primary.u.pathname);
 const profiles=parsed.filter(({u,text})=>/^\/[A-Za-z0-9_]+\/?$/.test(u.pathname)&&text);
 const first=profiles[0],account=profiles.find(({text})=>/^@[A-Za-z0-9_]+$/.test(text));
 const authorVerified=!!first&&!!account&&first.u.pathname.replace(/\/$/,'').toLowerCase()==='/'+handle.toLowerCase()&&
  account.u.pathname.replace(/\/$/,'').toLowerCase()==='/'+handle.toLowerCase()&&account.text.toLowerCase()==='@'+handle.toLowerCase();
 const avatar=parsed.find(({a,u})=>a.querySelector('img')&&u.pathname.replace(/\/$/,'').toLowerCase()==='/'+handle.toLowerCase())?.a.querySelector('img')?.currentSrc;
 return {handle,postId,author_verified:authorVerified,display_name:first?.text,profile_image_url:avatar||null,
  datetime:primary.a.querySelector('time')?.getAttribute('datetime')||null,visible_timestamp:primary.text,
  layout:e.querySelector('[data-testid="tweetText"]')?'legacy-dom-v1':'xds-visible-dom-20261031-v1'};
}

export async function locateTweetArticle(page,{handle,postId}){
 if(!/^[A-Za-z0-9_]{1,30}$/.test(handle)||!/^\d{1,30}$/.test(postId))throw new ObservationError('WEB_SOURCE_ID_INVALID');
 // Read native hrefs once, then bind every subsequent locator resolution to
 // those exact source links. all()/nth(i) preserves a position, not an article:
 // ordinary hydration can remove placeholders or reorder an intact source.
 const hrefs=await page.locator('a[href]').evaluateAll((anchors,identity)=>{
  const wanted=`/${identity.handle}/status/${identity.postId}`.toLowerCase(),found=[];
  for(const a of anchors){
   const raw=a.getAttribute('href');
   try{
    const u=new URL(raw,a.ownerDocument.location.href);
    if(['http:','https:'].includes(u.protocol)&&!u.username&&!u.password&&
     ['x.com','www.x.com','twitter.com','www.twitter.com'].includes(u.hostname)&&
     u.pathname.replace(/\/$/,'').toLowerCase()===wanted)found.push(raw);
   }catch{}
  }
  return [...new Set(found)];
 },{handle,postId});
 if(!hrefs.length)throw new ObservationError('WEB_SOURCE_ARTICLE_MISSING');
 // Escape observed attribute values as CSS strings, including native queries
 // and fragments. No attributes or styles are added to the public document.
 const quoted=value=>'"'+value.replace(/[\u0000-\u001f\u007f"\\]/g,c=>'\\'+c.charCodeAt(0).toString(16)+' ')+'"';
 const article=page.locator('article').filter({has:page.locator(hrefs.map(h=>'a[href='+quoted(h)+']').join(','))});
 const unique=async()=>{
  const count=await article.count();
  if(count!==1)throw new ObservationError(count?'WEB_SOURCE_ARTICLE_AMBIGUOUS':'WEB_SOURCE_ARTICLE_MISSING');
 };
 await unique();
 let observed;
 try{observed=await article.evaluate(inspectArticleElement,undefined,{timeout:500});}
 catch(e){
  // A disappearing identity is missing, not a ten-second wait on a stale nth.
  await unique();
  if(e.name==='TimeoutError')throw new ObservationError('WEB_SOURCE_ARTICLE_UNSTABLE');
  throw e;
 }
 await unique();
 if(!observed||observed.handle.toLowerCase()!==handle.toLowerCase()||observed.postId!==postId)
  throw new ObservationError('WEB_SOURCE_ARTICLE_MISSING');
 if(!observed.author_verified)throw new ObservationError('WEB_SOURCE_AUTHOR_UNVERIFIED');
 return article;
}

export async function assertSourceVisible(page,article,evidenceDir){
 await article.scrollIntoViewIfNeeded();
 const result=await article.evaluate(e=>{
  const r=e.getBoundingClientRect();const points=[];
  for(const fx of [.02,.25,.5,.75,.98])for(const fy of [.02,.25,.5,.75,.98]){
   const x=r.left+r.width*fx,y=r.top+r.height*fy;
   if(x<0||y<0||x>=innerWidth||y>=innerHeight)continue;
   const top=e.ownerDocument.elementsFromPoint(x,y)[0];
   points.push({x,y,visible:!!top&&(top===e||e.contains(top))});
  }
  // Also inspect every rendered text line. Nine coarse points can miss a small
  // popup covering the last line or the source timestamp.
  const walker=e.ownerDocument.createTreeWalker(e,NodeFilter.SHOW_TEXT);let node;
  let clipped_text=false,text_line_count=0;
  while((node=walker.nextNode())){
   if(!node.textContent.trim())continue;
   const parent=node.parentElement,s=getComputedStyle(parent);
   if(parent.closest('script,style,noscript,template'))continue;
   if(s.display==='none'||s.visibility!=='visible'||Number(s.opacity)<.01)continue;
   const range=e.ownerDocument.createRange();range.selectNodeContents(node);
   for(const b of range.getClientRects()){
    if(b.width<1||b.height<1)continue;text_line_count++;
    if(b.left<0||b.top<0||b.right>innerWidth||b.bottom>innerHeight){clipped_text=true;continue;}
    for(const fx of [.05,.5,.95]){
     const x=b.left+b.width*fx,y=b.top+b.height/2,top=e.ownerDocument.elementsFromPoint(x,y)[0];
     points.push({x,y,visible:!!top&&(top===e||e.contains(top))});
    }
   }
  }
  const fixed=[...e.ownerDocument.querySelectorAll('body *')].filter(n=>{
   if(n===e||n.contains(e)||e.contains(n))return false;
   const s=getComputedStyle(n),b=n.getBoundingClientRect();
   return ['fixed','sticky'].includes(s.position)&&s.display!=='none'&&s.visibility==='visible'&&Number(s.opacity)>.01&&
    Number(s.zIndex)>0&&b.width>20&&b.height>20&&b.left<r.right&&b.right>r.left&&b.top<r.bottom&&b.bottom>r.top;
  }).map(n=>{
   const s=getComputedStyle(n),b=n.getBoundingClientRect();
   const painted=s.backgroundColor!=='rgba(0, 0, 0, 0)'&&s.backgroundColor!=='transparent'||s.backgroundImage!=='none';
   return {tag:n.tagName,role:n.getAttribute('role'),class_name:typeof n.className==='string'?n.className.slice(0,200):'',
    rect:{left:b.left,top:b.top,right:b.right,bottom:b.bottom},
    pointerless_painted_cover:s.pointerEvents==='none'&&painted&&points.some(p=>p.x>b.left&&p.x<b.right&&p.y>b.top&&p.y<b.bottom)};
  });
  return {points,text_line_count,clipped_text,fixed_intersections:fixed,
   // A transparent sticky layout box can overlap the article's bounding box
   // while covering no content. Use hit tests, and separately retain painted
   // pointer-events:none overlays which ordinary hit tests cannot see.
   occluded:points.length===0||points.some(p=>!p.visible)||fixed.some(p=>p.pointerless_painted_cover)||clipped_text};
 });
 const record={version:'tweet-visibility-v2',...result,human_review:false,used_ocr:false};
 await fs.mkdir(evidenceDir,{recursive:true,mode:0o700});
 await fs.writeFile(path.join(evidenceDir,'visibility.json'),JSON.stringify(record,null,2),{mode:0o600});
 await screenshot(page,evidenceDir,result.occluded?'source-obscured.png':'source-visible.png');
 if(result.occluded)throw new ObservationError('WEB_SOURCE_OBSCURED');
 return record;
}
