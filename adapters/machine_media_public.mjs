// Public DOM full-dimensions variant, never a creator-original-upload claim.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import https from 'node:https';
import {captureTweet} from './browser_tweet_capture.mjs';
import {locateTweetArticle,assertSourceVisible,inspectArticleElement,ObservationError} from './browser_observation.mjs';
import {positionIndependentSource} from './browser_source_position.mjs';
import {inspectPlainTextSource} from './browser_source_facts.mjs';
import {inspectMachineMediaArticle,verifyMachineMediaBytes} from './machine_media_capture.mjs';
import {captureMachineLinkText,normalizeMachineLinkPayload,verifyMachineLinkNormalization} from './machine_link_text.mjs';
export const PUBLIC_MEDIA_MODE='public_dom_full_dimensions';
const hash=b=>crypto.createHash('sha256').update(b).digest('hex');
const canonical=x=>JSON.stringify(x,(_,v)=>v&&typeof v==='object'&&!Array.isArray(v)?Object.fromEntries(Object.keys(v).sort().map(k=>[k,v[k]])):v);
function publicUrl(raw,base){const u=new URL(raw,base),match=/^\/media\/([A-Za-z0-9_-]+)(?:\.(jpg|jpeg|png|webp))?$/.exec(u.pathname);
 const format=(u.searchParams.get('format')||match?.[2]||'').replace('jpeg','jpg');
 if(u.protocol!=='https:'||u.hostname!=='pbs.twimg.com'||u.port||u.username||u.password||u.hash||!match||
  !['jpg','png','webp'].includes(format)||[...u.searchParams.keys()].some(k=>!['name','format'].includes(k))||
  u.searchParams.getAll('name').length>1||u.searchParams.getAll('format').length>1)
  throw new ObservationError('PUBLIC_MEDIA_URL_INVALID');
 return {url:u.href,key:match[1],format,name:u.searchParams.get('name')};}
function verifySelection(item,sourceUrl){
 const raw=publicUrl(item.raw_src_attribute,sourceUrl),preview=publicUrl(item.preview_url,sourceUrl),set=[];
 for(const part of item.raw_srcset_attribute.split(',').map(x=>x.trim()).filter(Boolean)){
  const match=/^(\S+)\s+([1-9]\d*)w$/.exec(part);if(!match)throw new Error();
  set.push({...publicUrl(match[1],sourceUrl),descriptor_width:Number(match[2])});}
 set.sort((a,b)=>a.descriptor_width-b.descriptor_width);const selected=set.at(-1)||raw;
 if(raw.key!==preview.key||raw.format!==preview.format||item.media_key!==raw.key||item.format!==raw.format||
  set.some(x=>x.key!==raw.key||x.format!==raw.format)||new Set(set.map(x=>x.descriptor_width)).size!==set.length||
  canonical(set)!==canonical(item.srcset_candidates)||item.raw_src_url!==raw.url||item.download_url!==selected.url||
  item.resource_url_rewritten!==false||item.creator_original_upload_identity_verified!==false||
  !/^[1-9]\d*$/.test(item.declared_width_attribute)||!/^[1-9]\d*$/.test(item.declared_height_attribute)||
  item.declared_width!==Number(item.declared_width_attribute)||item.declared_height!==Number(item.declared_height_attribute)||
  !Number.isSafeInteger(item.declared_width)||!Number.isSafeInteger(item.declared_height)||item.declared_width*item.declared_height>40000000)throw new Error();
 return selected.url;
}
export async function downloadPublicDOMMedia(url,{maxBytes=20000000,timeoutMs=30000}={}){
 const target=publicUrl(url).url;
 return new Promise((resolve,reject)=>{const request=https.get(target,{headers:{Accept:'image/jpeg,image/png,image/webp'},timeout:timeoutMs},response=>{
  const fail=code=>{response.destroy();reject(new ObservationError(code));};
  if(response.statusCode!==200){fail(response.statusCode===429?'PUBLIC_MEDIA_RATE_LIMITED':response.statusCode===403?'PUBLIC_MEDIA_ACCESS_DENIED':'PUBLIC_MEDIA_HTTP_ERROR');return;}
  if(Number(response.headers['content-length']||0)>maxBytes){fail('PUBLIC_MEDIA_TOO_LARGE');return;}
  const chunks=[];let count=0;
  response.on('data',chunk=>{count+=chunk.length;if(count>maxBytes){fail('PUBLIC_MEDIA_TOO_LARGE');request.destroy();}else chunks.push(chunk);});
  response.on('aborted',()=>reject(new ObservationError('PUBLIC_MEDIA_DOWNLOAD_INCOMPLETE')));
  response.on('error',()=>reject(new ObservationError('PUBLIC_MEDIA_NETWORK_ERROR')));
  response.on('end',()=>{if(!response.complete||response.headers['content-length']&&Number(response.headers['content-length'])!==count)
   reject(new ObservationError('PUBLIC_MEDIA_DOWNLOAD_INCOMPLETE'));else resolve({bytes:Buffer.concat(chunks),content_type:response.headers['content-type']||'',status:200});});
 });request.on('timeout',()=>{request.destroy();reject(new ObservationError('PUBLIC_MEDIA_TIMEOUT'));});
 request.on('error',()=>reject(new ObservationError('PUBLIC_MEDIA_NETWORK_ERROR')));});
}
export async function verifyPublicMediaBytes(bytes,item,options={}){
 const result=await verifyMachineMediaBytes(bytes,{format:item.format,preview_natural_width:0,preview_natural_height:0},options);
 if(result.width!==item.declared_width||result.height!==item.declared_height)throw new ObservationError('PUBLIC_MEDIA_DECLARED_DIMENSIONS_MISMATCH');
 return {...result,operations:'public DOM resource byte copy; no URL rewriting, resize, crop or image-text translation',
  public_dimensions_verified:true,creator_original_upload_identity_verified:false};
}
export async function capturePublicMachineMedia(page,identity,evidenceDir,{allowedRoot=path.dirname(evidenceDir),downloader=downloadPublicDOMMedia,maxBytes=20000000,maxPixels=40000000,normalizeLinks=false,allowReplyContext=false}={}){
 const parent=await fs.realpath(path.dirname(path.resolve(evidenceDir))),dir=path.join(parent,path.basename(evidenceDir)),root=await fs.realpath(allowedRoot),rel=path.relative(root,dir);
 if(!rel||rel.startsWith('..')||path.isAbsolute(rel))throw new ObservationError('PUBLIC_MEDIA_EVIDENCE_PATH_INVALID');
 await fs.mkdir(dir,{recursive:false,mode:0o700});const transport=downloader===downloadPublicDOMMedia?'default_tls':'custom_transport';
 const source=await captureTweet(page,identity,path.join(dir,'source'),{trigger:'public_media_dimensions_adapter',pairedReplyDraft:allowReplyContext});
 const article=await locateTweetArticle(page,identity),spec={...identity,source_mode:PUBLIC_MEDIA_MODE};
 const inventory=await article.evaluate(inspectMachineMediaArticle,spec);
 if(!inventory.supported)throw new ObservationError(inventory.failures[0]||'PUBLIC_MEDIA_SOURCE_UNSUPPORTED');
 const textFacts=await article.evaluate(inspectPlainTextSource);let normalized=null;
 if(normalizeLinks){const observation=await captureMachineLinkText(page,identity),packet=normalizeMachineLinkPayload(source.payload,observation);
  verifyMachineLinkNormalization(source.payload,observation,packet);normalized={observation,...packet};}
 if(textFacts.observed_text!==source.payload.post.text||textFacts.body_count!==1||textFacts.status_ids.length!==1||textFacts.status_ids[0]!==identity.postId||
  !allowReplyContext&&textFacts.preceding_or_reply||textFacts.show_more||textFacts.clipped||textFacts.special_card_kinds.length||textFacts.css_media_count||textFacts.unclassified_svg_count||
  textFacts.extra_links.length||textFacts.images.some(x=>x.inside_body)||textFacts.failures.some(code=>code!=='MEDIA_OR_CARD_UNSUPPORTED'&&
   !(normalized&&code==='LINK_DISPLAY_TRUNCATED')&&!(allowReplyContext&&code==='REPLY_CONTEXT_REQUIRED')))
  throw new ObservationError('PUBLIC_MEDIA_BODY_OR_CONTEXT_UNSUPPORTED');
 const files=[];
 for(const item of inventory.photos){
  verifySelection(item,source.payload.url);const response=await downloader(item.download_url,{maxBytes});
  if(response.status!==200||!/^image\/(?:jpeg|png|webp)(?:;|$)/i.test(response.content_type||''))throw new ObservationError('PUBLIC_MEDIA_RESPONSE_INVALID');
  const verified=await verifyPublicMediaBytes(response.bytes,item,{maxBytes,maxPixels}),file=path.join(dir,`photo-${item.index}-${verified.sha256}.${verified.format}`);
  await fs.writeFile(file,response.bytes,{flag:'wx',mode:0o600});
  const anchor=article.locator(`a[href*="/status/${identity.postId}/photo/${item.index}"]`);
  if(await anchor.count()!==1)throw new ObservationError('PUBLIC_MEDIA_ITEM_AMBIGUOUS');
  const image=item.display_layout==='verified_sibling_photo_overlay'?anchor.locator('xpath=..').locator('img:visible'):anchor.locator('img:visible');
  if(await image.count()!==1)throw new ObservationError('PUBLIC_MEDIA_ITEM_AMBIGUOUS');await image.scrollIntoViewIfNeeded();
  const visible=await image.evaluate((e,item)=>{const r=e.getBoundingClientRect();let overlay=null;
   if(item.display_layout==='verified_sibling_photo_overlay'){const inert=e.closest('[inert]'),tile=inert?.parentElement;
    overlay=tile?[...tile.querySelectorAll('a[href]')].find(a=>a.href===item.photo_url&&!inert.contains(a)):null;
    if(!overlay)return false;const b=overlay.getBoundingClientRect();if(Math.max(Math.abs(b.left-r.left),Math.abs(b.top-r.top),Math.abs(b.width-r.width),Math.abs(b.height-r.height))>1)return false;}
   return r.left>=0&&r.top>=0&&r.right<=innerWidth&&r.bottom<=innerHeight&&[[.02,.02],[.98,.02],[.5,.5],[.02,.98],[.98,.98]].every(([x,y])=>{
    const top=e.ownerDocument.elementFromPoint(r.left+r.width*x,r.top+r.height*y);return top===e||e.contains(top)||!!overlay&&(top===overlay||overlay.contains(top));});},item);
  if(!visible)throw new ObservationError('PUBLIC_MEDIA_SOURCE_OBSCURED');
  const screenshot=path.join(dir,`photo-${item.index}-visible.png`);await image.screenshot({path:screenshot});await fs.chmod(screenshot,0o600);
  files.push({...item,...verified,file,screenshot_file:screenshot,screenshot_sha256:hash(await fs.readFile(screenshot)),transport_mode:transport,
   visibility:{all_image_hit_points_verified:true,source_photo_url:item.photo_url},human_review:false,image_text_translation_performed:false});
 }
 if(!allowReplyContext)await positionIndependentSource(page,identity,article,path.join(dir,'after-media-positioning'));
 await assertSourceVisible(page,article,path.join(dir,'after-media'));
 if(canonical(source.observed_source_identity)!==canonical(await article.evaluate(inspectArticleElement)))throw new ObservationError('PUBLIC_MEDIA_SOURCE_CHANGED');
 if(canonical(inventory)!==canonical(await article.evaluate(inspectMachineMediaArticle,spec))||canonical(textFacts)!==canonical(await article.evaluate(inspectPlainTextSource))||
  normalized&&canonical(normalized.observation)!==canonical(await captureMachineLinkText(page,identity)))throw new ObservationError('PUBLIC_MEDIA_SOURCE_CHANGED');
 const manifest={version:'machine-public-full-dimensions-bundle-v1',source_mode:PUBLIC_MEDIA_MODE,evidence_dir:dir,source,inventory,text_facts:textFacts,files,
  inventory_sha256:hash(canonical(inventory)),text_facts_sha256:hash(canonical(textFacts)),source_payload_sha256:hash(canonical(source.payload)),
  source_screenshot_sha256:source.payload.capture.screenshot_sha256,captured_at:new Date().toISOString(),transport_mode:transport,live_transport_verified:transport==='default_tls',
  creator_original_upload_identity_verified:false,public_dimensions_verified:true,human_review:false,official_api:false,image_text_translation_performed:false,
  requires_external_reply_evidence:allowReplyContext,relationship_verified_by_adapter:false,
  production_queue_connected:false,automatic_publication_authorized_by_this_bundle:false};
 if(normalized)manifest.link_normalization=normalized;
 await fs.writeFile(path.join(dir,'machine-media-manifest.json'),JSON.stringify(manifest,null,2),{flag:'wx',mode:0o600});return manifest;
}
export async function verifyPublicMachineMediaBundle(bundle,{requireLive=true}={}){
 try{
  const root=await fs.realpath(bundle.evidence_dir),bound=async(file,expected)=>{
   const real=await fs.realpath(file),rel=path.relative(root,real);if(!rel||rel.startsWith('..')||path.isAbsolute(rel)||(await fs.lstat(file)).isSymbolicLink())throw new Error();
   const bytes=await fs.readFile(real);if(hash(bytes)!==expected)throw new Error();return bytes;};
  if(bundle.version!=='machine-public-full-dimensions-bundle-v1'||bundle.source_mode!==PUBLIC_MEDIA_MODE||bundle.creator_original_upload_identity_verified!==false||
   bundle.public_dimensions_verified!==true||bundle.human_review!==false||bundle.image_text_translation_performed!==false||
   typeof bundle.requires_external_reply_evidence!=='boolean'||bundle.relationship_verified_by_adapter!==false||
   requireLive&&(bundle.live_transport_verified!==true||bundle.transport_mode!=='default_tls')||
   hash(canonical(bundle.inventory))!==bundle.inventory_sha256||hash(canonical(bundle.text_facts))!==bundle.text_facts_sha256||
   hash(canonical(bundle.source.payload))!==bundle.source_payload_sha256||!bundle.inventory.supported||bundle.inventory.failures.length||
   !bundle.files.length||bundle.files.length!==bundle.inventory.photos.length||bundle.source.payload.capture.human_review!==false||
   bundle.source.payload.capture.source_screenshot_reviewed!==false)throw new Error();
  await bound(bundle.source.screenshot_file,bundle.source_screenshot_sha256);
  if(bundle.link_normalization){const {observation,derived_payload,evidence}=bundle.link_normalization;verifyMachineLinkNormalization(bundle.source.payload,observation,{derived_payload,evidence});}
  for(const [i,item]of bundle.files.entries()){
   const observed=bundle.inventory.photos[i];verifySelection(observed,bundle.source.payload.url);
   if(item.index!==i+1||item.download_url!==observed.download_url||item.media_key!==observed.media_key||item.transport_mode!==bundle.transport_mode||
    item.resource_url_rewritten!==false||item.creator_original_upload_identity_verified!==false||item.human_review!==false||item.image_text_translation_performed!==false||
    item.visibility?.all_image_hit_points_verified!==true||item.visibility.source_photo_url!==observed.photo_url||
    item.declared_width!==observed.declared_width||item.declared_height!==observed.declared_height)throw new Error();
   const bytes=await bound(item.file,item.sha256),verified=await verifyPublicMediaBytes(bytes,observed);
   if(verified.width!==item.width||verified.height!==item.height||verified.sha256!==item.sha256)throw new Error();
   await bound(item.screenshot_file,item.screenshot_sha256);
  }
  return {version:'machine-public-dimensions-integrity-v1',integrity_verified:true,public_dimensions_verified:true,creator_original_upload_identity_verified:false,
   live_transport_verified:bundle.live_transport_verified,human_review:false,image_text_translation_performed:false,publication_authorized:false,
   files:bundle.files.map(f=>({index:f.index,file:f.file,sha256:f.sha256}))};
 }catch{throw new ObservationError('PUBLIC_MEDIA_BUNDLE_INTEGRITY_INVALID');}
}
