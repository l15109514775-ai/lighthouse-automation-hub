// Independent static-media adapter. Not wired into machine-tweet-v1 or a sender.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import https from 'node:https';
import {createRequire} from 'node:module';
import {captureTweet} from './browser_tweet_capture.mjs';
import {locateTweetArticle,assertSourceVisible,ObservationError} from './browser_observation.mjs';
import {inspectPlainTextSource} from './browser_source_facts.mjs';
import {captureMachineLinkText,normalizeMachineLinkPayload,verifyMachineLinkNormalization} from './machine_link_text.mjs';
const hash=b=>crypto.createHash('sha256').update(b).digest('hex');
const sharp=createRequire(new URL('../compositor/package.json',import.meta.url))('sharp');
const canonical=x=>JSON.stringify(x,(_,v)=>v&&typeof v==='object'&&!Array.isArray(v)?Object.fromEntries(Object.keys(v).sort().map(k=>[k,v[k]])):v);

// This function runs only against rendered DOM. It never edits a URL to invent
// an original source. A name=orig URL must already be visible as an image src or
// an explicit "View original" link bound to the same platform media identifier.
export function inspectMachineMediaArticle(article,identity){
 const publicDimensions=identity.source_mode==='public_dom_full_dimensions';
 const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility==='visible'&&Number(s.opacity)>.01;};
 function mediaUrl(raw){try{const u=new URL(raw,article.ownerDocument.baseURI);
  if(u.protocol!=='https:'||u.hostname!=='pbs.twimg.com'||u.port||u.username||u.password||u.hash)return null;
  const p=/^\/media\/([A-Za-z0-9_-]+)(?:\.(jpg|jpeg|png|webp))?$/.exec(u.pathname);
  if(!p||[...u.searchParams.keys()].some(k=>!['name','format'].includes(k))||
   u.searchParams.getAll('name').length>1||u.searchParams.getAll('format').length>1)return null;
  const format=(u.searchParams.get('format')||p[2]||'').replace('jpeg','jpg');
  if(!['jpg','png','webp'].includes(format))return null;
  return {url:u.href,key:p[1],format,name:u.searchParams.get('name')};
 }catch{return null;}}
 const originalLinks=[...article.querySelectorAll('a[href]')].filter(visible).flatMap(a=>{
  const m=mediaUrl(a.href);return m?.name==='orig'&&/^(?:查看原图|打开原图|View original|Open original)$/i.test(a.innerText.trim())?[m]:[];
 });
 const failures=[],photos=[],usedImages=new Set();
 const anchors=[...article.querySelectorAll('a[href]')].filter(a=>visible(a)&&/\/photo\/\d+\/?(?:\?|$)/.test(a.href));
 for(const anchor of anchors){
  let match;try{const u=new URL(anchor.href);match=/^\/([A-Za-z0-9_]+)\/status\/(\d+)\/photo\/([1-4])\/?$/.exec(u.pathname);
   if(u.protocol!=='https:'||!['x.com','www.x.com','twitter.com','www.twitter.com'].includes(u.hostname)||u.port||u.username||u.password||u.search||u.hash||
    !match||match[1].toLowerCase()!==identity.handle.toLowerCase()||match[2]!==identity.postId){failures.push('MEDIA_POST_IDENTITY_MISMATCH');continue;}
  }catch{failures.push('MEDIA_POST_IDENTITY_MISMATCH');continue;}
  let imgs=[...anchor.querySelectorAll('img')].filter(visible),layout='anchor_descendant';
  if(!imgs.length){
   // Observed XDS tile: an inert image and a sibling transparent photo-link
   // overlay. This is the native target; do not force-click through it.
   const tile=anchor.parentElement,ar=anchor.getBoundingClientRect(),tr=tile?.getBoundingClientRect();
   if(!tile||getComputedStyle(tile).position!=='relative'||getComputedStyle(anchor).position!=='absolute'||
    !anchor.classList.contains('inset-0')||!['查看媒体','View media'].includes(anchor.getAttribute('aria-label'))||
    !tr||Math.max(Math.abs(ar.left-tr.left),Math.abs(ar.top-tr.top),Math.abs(ar.width-tr.width),Math.abs(ar.height-tr.height))>1){
    failures.push('MEDIA_OVERLAY_GEOMETRY_UNVERIFIED');continue;}
   imgs=[...tile.querySelectorAll('img')].filter(visible);layout='verified_sibling_photo_overlay';
   if(imgs.length!==1){failures.push('MEDIA_ITEM_AMBIGUOUS');continue;}
   const ir=imgs[0].getBoundingClientRect(),inert=imgs[0].closest('[inert]');
   if(!inert||!tile.contains(inert)||inert.contains(anchor)||imgs[0].closest('a')||
    Math.max(Math.abs(ar.left-ir.left),Math.abs(ar.top-ir.top),Math.abs(ar.width-ir.width),Math.abs(ar.height-ir.height))>1){
    failures.push('MEDIA_OVERLAY_GEOMETRY_UNVERIFIED');continue;}
  }
  if(imgs.length!==1){failures.push('MEDIA_ITEM_AMBIGUOUS');continue;}
  const image=imgs[0];usedImages.add(image);const preview=mediaUrl(image.currentSrc||image.src),s=getComputedStyle(image),r=image.getBoundingClientRect();
  if(!preview){failures.push('MEDIA_PREVIEW_SOURCE_INVALID');continue;}
  if(!image.complete||image.naturalWidth<1||image.naturalHeight<1){failures.push('MEDIA_PREVIEW_NOT_LOADED');continue;}
  let publicSource=null;
  if(publicDimensions){
   const rawSrc=image.getAttribute('src'),rawSet=image.getAttribute('srcset')||'',raw=mediaUrl(rawSrc);
   const declaredWidth=Number(image.getAttribute('width')),declaredHeight=Number(image.getAttribute('height'));
   if(!/^[1-9]\d*$/.test(image.getAttribute('width')||'')||!/^[1-9]\d*$/.test(image.getAttribute('height')||'')||
    !Number.isSafeInteger(declaredWidth)||!Number.isSafeInteger(declaredHeight)||declaredWidth*declaredHeight>40000000){
    failures.push('PUBLIC_MEDIA_DIMENSIONS_NOT_DECLARED');continue;}
   if(!raw||raw.key!==preview.key||raw.format!==preview.format){failures.push('PUBLIC_MEDIA_RAW_SOURCE_INVALID');continue;}
   const srcset=[];let badSet=false;
   for(const part of rawSet.split(',').map(s=>s.trim()).filter(Boolean)){
    const parsed=/^(\S+)\s+([1-9]\d*)w$/.exec(part),candidate=parsed?mediaUrl(parsed[1]):null;
    if(!candidate||candidate.key!==raw.key||candidate.format!==raw.format){badSet=true;break;}
    srcset.push({...candidate,descriptor_width:Number(parsed[2])});
   }
   if(badSet||new Set(srcset.map(x=>x.descriptor_width)).size!==srcset.length){failures.push('PUBLIC_MEDIA_SRCSET_AMBIGUOUS');continue;}
   srcset.sort((a,b)=>a.descriptor_width-b.descriptor_width);const largest=srcset.at(-1);
   const selected=largest||raw;
   publicSource={download_url:selected.url,source_url_basis:largest?'max_declared_public_srcset':'raw_public_src',raw_src_attribute:rawSrc,
    raw_src_url:raw.url,raw_srcset_attribute:rawSet,srcset_candidates:srcset,declared_width:declaredWidth,declared_height:declaredHeight,
    declared_width_attribute:image.getAttribute('width'),declared_height_attribute:image.getAttribute('height'),
    resource_url_rewritten:false,creator_original_upload_identity_verified:false};
  }
  const candidates=publicDimensions?[]:preview.name==='orig'?[preview]:originalLinks.filter(m=>m.key===preview.key&&m.format===preview.format);
  if(!publicDimensions&&candidates.length!==1){failures.push('VISIBLE_ORIGINAL_SOURCE_REQUIRED');continue;}
  let cropped=['cover','fill'].includes(s.objectFit)&&Math.abs(r.width/r.height-image.naturalWidth/image.naturalHeight)>.005;
  if(s.clipPath!=='none'||s.clip!=='auto')cropped=true;
  for(let p=image.parentElement;p&&p!==article;p=p.parentElement){const ps=getComputedStyle(p),b=p.getBoundingClientRect();
   if(['hidden','clip','scroll','auto'].includes(ps.overflowX)&&(r.left<b.left-1||r.right>b.right+1)||
      ['hidden','clip','scroll','auto'].includes(ps.overflowY)&&(r.top<b.top-1||r.bottom>b.bottom+1))cropped=true;}
  if(cropped&&!publicDimensions)failures.push('MEDIA_PREVIEW_CROPPED_OR_DISTORTED');
  const item={index:Number(match[3]),photo_url:anchor.href,preview_url:preview.url,
   media_key:preview.key,format:preview.format,preview_natural_width:image.naturalWidth,preview_natural_height:image.naturalHeight,
   alt_text:image.alt||'',display_layout:layout,photo_overlay_geometry_verified:layout==='verified_sibling_photo_overlay'};
  if(publicDimensions)Object.assign(item,publicSource,{thumbnail_cropped_by_layout:cropped});
  else Object.assign(item,{preview_cropped:cropped,original_url:candidates[0].url,
   original_basis:preview.name==='orig'?'visible_image_original_url':'visible_original_link_with_same_media_key',original_url_rewritten:false});
  photos.push(item);
 }
 if(!photos.length||photos.length>4)failures.push('STATIC_MEDIA_COUNT_UNVERIFIED');
 if(photos.some((p,i)=>p.index!==i+1)||new Set(photos.map(p=>p.media_key)).size!==photos.length)failures.push('MEDIA_ORDER_OR_IDENTITY_AMBIGUOUS');
 const foreignImages=[...article.querySelectorAll('img')].filter(e=>visible(e)&&!usedImages.has(e)).filter(e=>{
  try{const u=new URL(e.currentSrc||e.src);return !(u.hostname==='pbs.twimg.com'&&u.pathname.startsWith('/profile_images/'));}catch{return true;}
 });
 if(foreignImages.length)failures.push('UNBOUND_VISIBLE_MEDIA');
 if(article.querySelector('video,audio,canvas,iframe,[data-testid="videoPlayer"],[data-testid="quoteTweet"],[role="radiogroup"],[role="radio"]'))failures.push('NON_STATIC_OR_CONTEXT_MEDIA_UNSUPPORTED');
 return {version:publicDimensions?'machine-public-full-dimensions-dom-v1':'machine-static-media-dom-v1',source_url:`https://x.com/${identity.handle}/status/${identity.postId}`,
  photos,visible_media_count:photos.length,failures:[...new Set(failures)],supported:failures.length===0,
  human_review:false,official_api:false,image_text_translation_performed:false,
  scope:publicDimensions?'public_dom_full_dimensions':'visible_static_original_urls_only',limitations:[publicDimensions?
   'Public DOM dimensions only; not the creator original upload bytes or a hidden API attachment list.':
   'Original completeness is based on the visible first-party original URL and decoded complete file; no hidden API attachment list is claimed.']};
}

function validateOriginalUrl(raw){const u=new URL(raw);
 if(u.protocol!=='https:'||u.hostname!=='pbs.twimg.com'||u.port||u.username||u.password||u.hash||
  !/^\/media\/[A-Za-z0-9_-]+(?:\.(?:jpg|jpeg|png|webp))?$/.test(u.pathname)||u.searchParams.get('name')!=='orig'||
  [...u.searchParams.keys()].some(k=>!['name','format'].includes(k))||u.searchParams.getAll('name').length!==1||u.searchParams.getAll('format').length>1)
  throw new ObservationError('MEDIA_ORIGINAL_URL_INVALID');
 return u;
}

export async function downloadMachineOriginal(url,{maxBytes=20000000,timeoutMs=30000}={}){
 const u=validateOriginalUrl(url);
 return new Promise((resolve,reject)=>{
  const request=https.get(u,{headers:{Accept:'image/jpeg,image/png,image/webp'},timeout:timeoutMs},response=>{
   const fail=code=>{response.destroy();reject(new ObservationError(code));};
   if(response.statusCode!==200){fail(response.statusCode===429?'MEDIA_RATE_LIMITED':response.statusCode===403?'MEDIA_ACCESS_DENIED':'MEDIA_ORIGINAL_HTTP_ERROR');return;}
   if(Number(response.headers['content-length']||0)>maxBytes){fail('MEDIA_ORIGINAL_TOO_LARGE');return;}
   const chunks=[];let count=0;
   response.on('data',chunk=>{count+=chunk.length;if(count>maxBytes){fail('MEDIA_ORIGINAL_TOO_LARGE');request.destroy();}else chunks.push(chunk);});
   response.on('aborted',()=>reject(new ObservationError('MEDIA_ORIGINAL_INCOMPLETE')));
   response.on('error',()=>reject(new ObservationError('MEDIA_ORIGINAL_NETWORK_ERROR')));
   response.on('end',()=>{if(!response.complete||response.headers['content-length']&&Number(response.headers['content-length'])!==count)
    reject(new ObservationError('MEDIA_ORIGINAL_INCOMPLETE'));
    else resolve({bytes:Buffer.concat(chunks),content_type:response.headers['content-type']||'',transport_mode:'default_tls',status:200});});
  });
  request.on('timeout',()=>{request.destroy();reject(new ObservationError('MEDIA_ORIGINAL_TIMEOUT'));});
  request.on('error',()=>reject(new ObservationError('MEDIA_ORIGINAL_NETWORK_ERROR')));
 });
}

function animatedContainer(bytes){
 if(bytes.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10]))){
  for(let p=8;p+12<=bytes.length;){const n=bytes.readUInt32BE(p),name=bytes.toString('ascii',p+4,p+8);
   if(name==='acTL')return true;if(n>bytes.length-p-12)break;p+=n+12;}
 }
 if(bytes.toString('ascii',0,4)==='RIFF'&&bytes.toString('ascii',8,12)==='WEBP'){
  for(let p=12;p+8<=bytes.length;){const n=bytes.readUInt32LE(p+4),name=bytes.toString('ascii',p,p+4);
   if(['ANIM','ANMF'].includes(name))return true;if(n>bytes.length-p-8)break;p+=n+8+n%2;}
 }
 return false;
}

export async function verifyMachineMediaBytes(bytes,item,{maxBytes=20000000,maxPixels=40000000}={}){
 if(!Buffer.isBuffer(bytes)||!bytes.length||bytes.length>maxBytes)throw new ObservationError('MEDIA_ORIGINAL_SIZE_INVALID');
 if(animatedContainer(bytes))throw new ObservationError('MEDIA_ANIMATED_OR_UNSUPPORTED');
 try{
  const image=sharp(bytes,{animated:true,limitInputPixels:maxPixels,failOn:'warning'}),meta=await image.metadata();
  const format=meta.format==='jpeg'?'jpg':meta.format;
  if(!['jpg','png','webp'].includes(format)||(meta.pages||1)!==1||format!==item.format)throw new ObservationError('MEDIA_ANIMATED_OR_UNSUPPORTED');
  if(!meta.width||!meta.height||meta.width*meta.height>maxPixels||meta.width<item.preview_natural_width||meta.height<item.preview_natural_height)
   throw new ObservationError('MEDIA_ORIGINAL_DIMENSIONS_UNVERIFIED');
  if(meta.orientation&&meta.orientation!==1)throw new ObservationError('MEDIA_ORIGINAL_ORIENTATION_UNSUPPORTED');
  // metadata alone can accept truncated files. Force complete pixel decoding.
  await image.raw().toBuffer();
  return {sha256:hash(bytes),bytes:bytes.length,width:meta.width,height:meta.height,format,decoded_complete:true,animated:false,
   operations:'original byte-for-byte copy; no crop, resize, orientation rewrite or image-text translation'};
 }catch(e){if(e instanceof ObservationError)throw e;throw new ObservationError('MEDIA_ORIGINAL_DECODE_FAILED');}
}

export async function captureMachineMedia(page,identity,evidenceDir,{allowedRoot=path.dirname(evidenceDir),downloader=downloadMachineOriginal,maxBytes=20000000,maxPixels=40000000,normalizeLinks=false,sourceMode='orig_only',allowReplyContext=false}={}){
 if(sourceMode==='public_dom_full_dimensions'){
  const {capturePublicMachineMedia}=await import('./machine_media_public.mjs');
  return capturePublicMachineMedia(page,identity,evidenceDir,{allowedRoot,maxBytes,maxPixels,normalizeLinks,allowReplyContext,
   ...(downloader===downloadMachineOriginal?{}:{downloader})});
 }
 if(sourceMode!=='orig_only')throw new ObservationError('MEDIA_SOURCE_MODE_UNSUPPORTED');
 if(allowReplyContext)throw new ObservationError('MEDIA_REPLY_CONTEXT_UNSUPPORTED');
 const parent=await fs.realpath(path.dirname(path.resolve(evidenceDir))),dir=path.join(parent,path.basename(evidenceDir)),root=await fs.realpath(allowedRoot),relative=path.relative(root,dir);
 if(!relative||relative.startsWith('..')||path.isAbsolute(relative))throw new ObservationError('MEDIA_EVIDENCE_PATH_INVALID');
 await fs.mkdir(dir,{recursive:false,mode:0o700});
 const transport=downloader===downloadMachineOriginal?'default_tls':'custom_transport';
 const base=await captureTweet(page,identity,path.join(dir,'source'),{trigger:'machine_media_adapter'});
 const article=await locateTweetArticle(page,identity),inventory=await article.evaluate(inspectMachineMediaArticle,identity);
 if(!inventory.supported)throw new ObservationError(inventory.failures[0]||'MACHINE_MEDIA_SOURCE_UNSUPPORTED');
 let linkNormalization=null;
 if(normalizeLinks){const observation=await captureMachineLinkText(page,identity);
  const packet=normalizeMachineLinkPayload(base.payload,observation);verifyMachineLinkNormalization(base.payload,observation,packet);
  linkNormalization={observation,...packet};}
 const textFacts=await article.evaluate(inspectPlainTextSource),originals=new Set(inventory.photos.map(p=>p.original_url));
 if(textFacts.observed_text!==base.payload.post.text||textFacts.body_count!==1||textFacts.status_ids.length!==1||
  textFacts.status_ids[0]!==identity.postId||textFacts.preceding_or_reply||textFacts.show_more||textFacts.clipped||
  textFacts.special_card_kinds.length||textFacts.css_media_count||textFacts.unclassified_svg_count||
  textFacts.extra_links.some(link=>!originals.has(link))||textFacts.images.some(image=>image.inside_body)||
  textFacts.failures.some(code=>code!=='MEDIA_OR_CARD_UNSUPPORTED'&&!(linkNormalization&&code==='LINK_DISPLAY_TRUNCATED')))
  throw new ObservationError('MEDIA_BODY_OR_CONTEXT_UNSUPPORTED');
 const files=[];
 for(const item of inventory.photos){
  validateOriginalUrl(item.original_url);
  const response=await downloader(item.original_url,{maxBytes});
  if(response.status!==200||!/^image\/(?:jpeg|png|webp)(?:;|$)/i.test(response.content_type||''))throw new ObservationError('MEDIA_ORIGINAL_RESPONSE_INVALID');
  const verified=await verifyMachineMediaBytes(response.bytes,item,{maxBytes,maxPixels});
  const file=path.join(dir,`photo-${String(item.index).padStart(2,'0')}-${verified.sha256}.${verified.format}`);
  await fs.writeFile(file,response.bytes,{flag:'wx',mode:0o600});
  const photoAnchor=article.locator(`a[href*="/status/${identity.postId}/photo/${item.index}"]`);
  if(await photoAnchor.count()!==1)throw new ObservationError('MEDIA_ITEM_AMBIGUOUS');
  const image=item.display_layout==='verified_sibling_photo_overlay'?photoAnchor.locator('xpath=..').locator('img:visible'):photoAnchor.locator('img:visible');
  if(await image.count()!==1)throw new ObservationError('MEDIA_ITEM_AMBIGUOUS');await image.scrollIntoViewIfNeeded();
  const visible=await image.evaluate((e,item)=>{const r=e.getBoundingClientRect();let overlay=null;
   if(item.display_layout==='verified_sibling_photo_overlay'){
    const inert=e.closest('[inert]'),tile=inert?.parentElement;
    overlay=tile?[...tile.querySelectorAll('a[href]')].find(a=>a.href===item.photo_url&&!inert.contains(a)):null;
    if(!overlay)return false;const a=overlay.getBoundingClientRect();
    if(Math.max(Math.abs(a.left-r.left),Math.abs(a.top-r.top),Math.abs(a.width-r.width),Math.abs(a.height-r.height))>1)return false;
   }
   return r.top>=0&&r.left>=0&&r.bottom<=innerHeight&&r.right<=innerWidth&&
   [[.02,.02],[.98,.02],[.5,.5],[.02,.98],[.98,.98]].every(([x,y])=>{const top=e.ownerDocument.elementFromPoint(r.left+r.width*x,r.top+r.height*y);
    return top===e||e.contains(top)||!!overlay&&(top===overlay||overlay.contains(top));});},item);
  if(!visible)throw new ObservationError('MEDIA_SOURCE_OBSCURED');
  const screenshot=path.join(dir,`photo-${String(item.index).padStart(2,'0')}-visible.png`);
  await image.screenshot({path:screenshot,timeout:10000});await fs.chmod(screenshot,0o600);
  files.push({...item,...verified,file,screenshot_file:screenshot,screenshot_sha256:hash(await fs.readFile(screenshot)),
   visibility:{version:'machine-media-visible-v1',all_image_hit_points_verified:true,source_photo_url:item.photo_url,
    native_sibling_overlay_verified:item.display_layout==='verified_sibling_photo_overlay'},
   transport_mode:transport,human_review:false,image_text_translation_performed:false});
 }
 await assertSourceVisible(page,article,path.join(dir,'after-media'));
 if(canonical(inventory)!==canonical(await article.evaluate(inspectMachineMediaArticle,identity)))throw new ObservationError('MEDIA_SOURCE_CHANGED_DURING_CAPTURE');
 if(canonical(textFacts)!==canonical(await article.evaluate(inspectPlainTextSource)))throw new ObservationError('MEDIA_SOURCE_CHANGED_DURING_CAPTURE');
 if(linkNormalization&&canonical(linkNormalization.observation)!==canonical(await captureMachineLinkText(page,identity)))
  throw new ObservationError('MEDIA_SOURCE_CHANGED_DURING_CAPTURE');
 const manifest={version:'machine-static-media-bundle-v1',evidence_dir:dir,source:base,inventory,text_facts:textFacts,files,inventory_sha256:hash(canonical(inventory)),
  text_facts_sha256:hash(canonical(textFacts)),
  source_payload_sha256:hash(canonical(base.payload)),source_screenshot_sha256:base.payload.capture.screenshot_sha256,
  captured_at:new Date().toISOString(),transport_mode:transport,live_transport_verified:transport==='default_tls',
  human_review:false,official_api:false,image_text_translation_performed:false,
  production_queue_connected:false,automatic_publication_authorized_by_this_bundle:false};
 if(linkNormalization)manifest.link_normalization=linkNormalization;
 await fs.writeFile(path.join(dir,'machine-media-manifest.json'),JSON.stringify(manifest,null,2),{flag:'wx',mode:0o600});
 return manifest;
}

// A future machine-media policy must verify this bundle and bind it to the DB.
// This helper never authorizes publishing or sets any human confirmation flag.
export async function verifyMachineMediaBundle(bundle,{requireLive=true}={}){
 if(bundle.version==='machine-public-full-dimensions-bundle-v1'){
  const {verifyPublicMachineMediaBundle}=await import('./machine_media_public.mjs');return verifyPublicMachineMediaBundle(bundle,{requireLive});
 }
 try{
  const root=await fs.realpath(bundle.evidence_dir);
  const file=async(p,expected)=>{if((await fs.lstat(p)).isSymbolicLink())throw new Error();const real=await fs.realpath(p),relative=path.relative(root,real);
   if(!relative||relative.startsWith('..')||path.isAbsolute(relative)||hash(await fs.readFile(real))!==expected)throw new Error();return real;};
  if(bundle.version!=='machine-static-media-bundle-v1'||bundle.human_review!==false||bundle.image_text_translation_performed!==false||
   requireLive&&(bundle.live_transport_verified!==true||bundle.transport_mode!=='default_tls')||
   hash(canonical(bundle.inventory))!==bundle.inventory_sha256||hash(canonical(bundle.text_facts))!==bundle.text_facts_sha256||
   hash(canonical(bundle.source.payload))!==bundle.source_payload_sha256||!bundle.inventory.supported||bundle.inventory.failures.length||
   !bundle.files.length||bundle.files.length!==bundle.inventory.photos.length||bundle.source.payload.capture.human_review!==false||
   bundle.source.payload.capture.source_screenshot_reviewed!==false)throw new Error();
  await file(bundle.source.screenshot_file,bundle.source_screenshot_sha256);
  if(bundle.link_normalization){const {observation,derived_payload,evidence}=bundle.link_normalization;
   verifyMachineLinkNormalization(bundle.source.payload,observation,{derived_payload,evidence});}
  for(const [i,item]of bundle.files.entries()){
   const observed=bundle.inventory.photos[i];
   if(item.index!==i+1||item.human_review!==false||item.image_text_translation_performed!==false||item.animated!==false||item.decoded_complete!==true||
    item.transport_mode!==bundle.transport_mode||item.original_url!==observed.original_url||item.media_key!==observed.media_key||
    item.preview_cropped!==false||item.original_url_rewritten!==false||item.format!==observed.format||
    item.display_layout!==observed.display_layout||item.visibility?.all_image_hit_points_verified!==true||
    item.visibility.source_photo_url!==observed.photo_url||
    item.visibility.native_sibling_overlay_verified!==(observed.display_layout==='verified_sibling_photo_overlay'))throw new Error();
   await file(item.file,item.sha256);await file(item.screenshot_file,item.screenshot_sha256);
  }
  return {version:'machine-media-integrity-v1',integrity_verified:true,live_transport_verified:bundle.live_transport_verified,
   inventory_sha256:bundle.inventory_sha256,text_facts_sha256:bundle.text_facts_sha256,files:bundle.files.map(f=>({index:f.index,file:f.file,sha256:f.sha256})),
   human_review:false,image_text_translation_performed:false,publication_authorized:false};
 }catch{throw new ObservationError('MACHINE_MEDIA_BUNDLE_INTEGRITY_INVALID');}
}

export async function machineMediaCompositorInput(baseInput,bundle,options={}){
 const {useNormalizedText=false,...verifyOptions}=options;await verifyMachineMediaBundle(bundle,verifyOptions);
 const value=structuredClone(baseInput),p=value.post,source=useNormalizedText?bundle.link_normalization?.derived_payload:bundle.source.payload;
 if(!source)throw new ObservationError('MACHINE_MEDIA_NORMALIZED_SOURCE_REQUIRED');
 if(p?.id!==source.post.id||p.url!==source.url||p.originalText!==source.post.text||p.author?.handle!==source.author.username||
  p.review?.sourceConfirmed!==false||p.review?.translationConfirmed!==false||p.sourceScreenshot?.reviewed!==false||
  p.relationship?.type!=='original'||p.quote)throw new ObservationError('MACHINE_MEDIA_COMPOSITOR_BINDING_INVALID');
 const captured=source.capture.captured_at;
 if(typeof captured!=='string'||!Number.isFinite(Date.parse(captured)))throw new ObservationError('MACHINE_MEDIA_CAPTURE_TIME_INVALID');
 // Original schema supports whole seconds. Keep the original observation's
 // millisecond timestamp untouched; normalize only this derived render field.
 p.sourceScreenshot={file:bundle.source.screenshot_file,capturedAt:captured.replace(/\.\d+(?=Z$|[+-]\d{2}:\d{2}$)/,''),reviewed:false};
 p.sourceMediaCount=bundle.files.length;p.sourceQuote='none';
 p.media=bundle.files.map(item=>({file:item.file,completeness:'complete',reviewed:false}));
 return value;
}
