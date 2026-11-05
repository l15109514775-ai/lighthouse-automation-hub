// Independent official-API rendering. No source screenshot is invented. No publisher, credential or policy enable.
import fs from 'node:fs/promises';
import path from 'node:path';
import {Renderer} from '../compositor/src/renderer.mjs';
import {loadTemplate,loadFonts,loadAssets,sha256,stable,walkPosts} from '../compositor/src/core.mjs';
import {buildDocument} from '../compositor/src/template.mjs';
import {verifyMachineMediaBytes} from './static_media_bytes.mjs';
const [inputFile,outputDir,allowedRoot,ticketFile]=process.argv.slice(2);
const manualCodes=new Set(['TRANSLATION_UNREVIEWED','SOURCE_UNREVIEWED','SCREENSHOT_UNREVIEWED','MEDIA_UNREVIEWED','SCREENSHOT_MISSING','CAPTURE_TIME']);
const root=await fs.realpath(allowedRoot);
const fail=code=>{throw new Error(code);};
for(const filename of [inputFile,ticketFile]){const resolved=await fs.realpath(filename),relative=path.relative(root,resolved);
 if(!relative||relative.startsWith('..')||path.isAbsolute(relative))fail('MACHINE_OFFICIAL_EVIDENCE_PATH_INVALID');}
const parseJson=bytes=>{try{return JSON.parse(bytes);}catch{fail('MACHINE_OFFICIAL_EVIDENCE_JSON_INVALID');}};
const raw=await fs.readFile(inputFile),input=parseJson(raw),ticket=parseJson(await fs.readFile(ticketFile,'utf8'));
const outputParent=await fs.realpath(path.dirname(path.resolve(outputDir))),outputRelative=path.relative(root,outputParent);
if(outputRelative.startsWith('..')||path.isAbsolute(outputRelative))fail('MACHINE_OFFICIAL_OUTPUT_PATH_INVALID');
// Attempt directories are immutable. A failed rerun cannot leave a stale result
// that looks like the product of the new input or overwrite an older report.
await fs.mkdir(outputDir,{recursive:false,mode:0o700});
async function boundFile(file,expected){
 if(typeof file!=='string'||!/^[a-f0-9]{64}$/.test(expected||''))fail('MACHINE_OFFICIAL_EVIDENCE_BINDING_INVALID');
 const resolved=await fs.realpath(file),rel=path.relative(root,resolved);
 if(!rel||rel.startsWith('..')||path.isAbsolute(rel)||(await fs.lstat(file)).isSymbolicLink())fail('MACHINE_OFFICIAL_EVIDENCE_PATH_INVALID');
 for(let p=path.dirname(path.resolve(file));p!==root&&p!==path.dirname(p);p=path.dirname(p))
  if((await fs.lstat(p)).isSymbolicLink())fail('MACHINE_OFFICIAL_EVIDENCE_PATH_INVALID');
 const bytes=await fs.readFile(resolved);if(sha256(bytes)!==expected)fail('MACHINE_OFFICIAL_EVIDENCE_HASH_CHANGED');return bytes;
}
const parsedFile=async ref=>{if(!ref||typeof ref!=='object')fail('MACHINE_OFFICIAL_EVIDENCE_BINDING_INVALID');return parseJson(await boundFile(ref.file,ref.sha256));};
function verifyPhotoRetrieval(item,sourceURL){
 const has=key=>Object.prototype.hasOwnProperty.call(item,key);
 if(!has('download_url')&&!has('retrieval_rule'))return; // Immutable legacy exact-API-URL semantics.
 const invalid=()=>fail('MACHINE_OFFICIAL_MEDIA_RETRIEVAL_INVALID');
 if(!has('download_url')||!has('retrieval_rule')||item.retrieval_rule!=='x-pbs-api-photo-orig-v1'||
  typeof sourceURL!=='string'||sourceURL!==sourceURL.trim()||/[\x00-\x20\x7f]/.test(sourceURL)||/[?#]$/.test(sourceURL))invalid();
 // Parse the raw path, not URL.pathname, so native dot-segment normalization
 // cannot hide a changed media identity. Only this one rendition is derived.
 const parsed=/^https:\/\/pbs\.twimg\.com(\/[^?#]*)(?:\?([^#]*))?$/.exec(sourceURL);
 const media=parsed&&/^\/media\/([A-Za-z0-9_-]+)(?:\.(jpg|jpeg|png|webp))?$/.exec(parsed[1]);
 if(!media)invalid();
 const query=parsed[2];
 if(query!==undefined&&query.split('&').some(part=>!part||!part.includes('=')))invalid();
 const values={};
 for(const [key,value]of new URLSearchParams(query||'')){
  if(!['format','name'].includes(key)||!value||Object.prototype.hasOwnProperty.call(values,key))invalid();
  values[key]=value;
 }
 const extension=media[2]?.replaceAll('jpeg','jpg'),requested=values.format?.replaceAll('jpeg','jpg');
 if(requested!==undefined&&(!['jpg','png','webp'].includes(requested)||extension&&requested!==extension))invalid();
 const format=requested||extension;
 if(!['jpg','png','webp'].includes(format)||item.format!==format||
  item.download_url!==`https://pbs.twimg.com/media/${media[1]}?format=${format}&name=orig`)invalid();
}
async function verifyOfficialAuthority(evidence,source,post){
 const candidates=[];
 for(const descriptor of evidence.provenance){
  const bytes=await boundFile(descriptor.file,descriptor.sha256);let document;
  try{document=JSON.parse(bytes.toString('utf8'));}catch{continue;}
  if(document?.version==='x-official-render-authority-v1')candidates.push({ref:descriptor,authority:document});
 }
 if(candidates.length!==1)fail('MACHINE_OFFICIAL_API_AUTHORITY_INVALID');
 const [{ref,authority}]=candidates;
 const contains=descriptor=>evidence.provenance.some(p=>p&&p.sha256===descriptor.sha256&&
  typeof p.file==='string'&&path.resolve(p.file)===path.resolve(descriptor.file));
 if(authority.version!=='x-official-render-authority-v1'||authority.source_sha256!==sha256(stable(source))||
  authority.post_id!==post.id||authority.author_id!==source.author?.id||source.post?.author_id!==authority.author_id||
  evidence.source_authority!=='x-v2-official'||source.capture?.human_review!==false||
  source.capture?.official_api_receipt!==(evidence.evidence_kind==='server_receipt')||
  authority.human_review!==false||authority.publication_authorized!==false||!contains(ref)||
  !Array.isArray(authority.api_calls)||!authority.api_calls.length||
  new Set(authority.api_calls.map(c=>c.call_id)).size!==authority.api_calls.length)
  fail('MACHINE_OFFICIAL_API_AUTHORITY_INVALID');
 const tweets=[],users=[],media=[];let postCaptureVerified=false;
 for(const call of authority.api_calls){
  if(!/^[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(call.call_id||'')||
   call.method!=='GET'||call.host!=='api.x.com'||call.http_status_class!=='2xx'||
   !/^\/2\/(?:users(?:\/by\/username\/[A-Za-z0-9_]{1,15}|\/[0-9]{1,30}(?:\/tweets)?)?|tweets(?:\/[0-9]{1,30})?)$/.test(call.path||'')||
   !call.params||typeof call.params!=='object'||Array.isArray(call.params)||
   !Number.isFinite(call.captured_at)||call.captured_at<0||
   call.transport_mode!==(evidence.evidence_kind==='server_receipt'?'default_tls':'custom_transport')||
   !call.response||!contains(call.response))fail('MACHINE_OFFICIAL_API_RECEIPT_INVALID');
  const body=await parsedFile(call.response);
  if(!body||typeof body!=='object'||Array.isArray(body)||sha256(stable(body))!==call.canonical_response_sha256||
   ('errors' in body&&(!Array.isArray(body.errors)||body.errors.length)))
   fail('MACHINE_OFFICIAL_API_RESPONSE_INVALID');
  const data=Array.isArray(body.data)?body.data:body.data&&typeof body.data==='object'?[body.data]:[];
  const included=body.includes||{};
  if(!Array.isArray(included.tweets||[])||!Array.isArray(included.users||[])||!Array.isArray(included.media||[]))
   fail('MACHINE_OFFICIAL_API_RESPONSE_INVALID');
  for(const item of [...data,...(included.tweets||[])])if(item&&typeof item==='object'&&'author_id' in item){
   tweets.push(item);
   if(item.id===source.post.id&&call.captured_at+300>=Date.parse(source.post.created_at)/1000)postCaptureVerified=true;
  }
  for(const item of [...data,...(included.users||[])])if(item&&typeof item==='object'&&'username' in item)users.push(item);
  media.push(...(included.media||[]));
 }
 const text=p=>p.note_tweet===undefined?p.text:p.note_tweet?.text;
 const samePost=p=>p.id===source.post.id&&p.author_id===source.post.author_id&&text(p)===source.post.text&&
  p.created_at===source.post.created_at&&stable(p.referenced_tweets||[])===stable(source.post.referenced_tweets||[])&&
  stable(p.attachments?.media_keys||[])===stable(source.post.attachments?.media_keys||[])&&
  stable(p.edit_history_tweet_ids)===stable(source.post.edit_history_tweet_ids);
 const matchingPosts=tweets.filter(p=>p.id===post.id);
 if(!postCaptureVerified||!matchingPosts.length||matchingPosts.some(p=>!samePost(p))||
  !Array.isArray(source.post.edit_history_tweet_ids)||!source.post.edit_history_tweet_ids.includes(post.id)||
  !users.some(u=>u.id===source.author.id&&u.name===source.author.name&&u.username===source.author.username&&
   (!source.author.profile_image_url||u.profile_image_url===source.author.profile_image_url)))
  fail('MACHINE_OFFICIAL_API_SOURCE_MISMATCH');
 const keys=source.post.attachments?.media_keys||[];
 if(!Array.isArray(keys)||new Set(keys).size!==keys.length||keys.length!==evidence.media.length||
  stable(source.official_source?.local_media)!==stable(evidence.media))
  fail('MACHINE_OFFICIAL_API_MEDIA_MISMATCH');
 for(const [index,key]of keys.entries()){
  const item=evidence.media[index],metadata=source.media?.[key];
  if(!metadata||metadata.media_key!==key||metadata.type!=='photo'||item.media_key!==key||item.source_url!==metadata.url||
   item.transport_mode!==(evidence.evidence_kind==='server_receipt'?'default_tls':'custom_transport')||
   !/^https:\/\/pbs\.twimg\.com\/media\//.test(metadata.url||'')||
   !Number.isSafeInteger(metadata.width)||!Number.isSafeInteger(metadata.height)||metadata.width<=0||metadata.height<=0||
   item.width!==metadata.width||item.height!==metadata.height||
   !media.some(m=>m.media_key===key&&m.type==='photo'&&m.url===metadata.url&&m.width===metadata.width&&m.height===metadata.height))
   fail('MACHINE_OFFICIAL_API_MEDIA_MISMATCH');
  verifyPhotoRetrieval(item,metadata.url);
 }
 return {api_authority_sha256:ref.sha256,api_response_count:authority.api_calls.length,
  source_api_live_transport_verified:evidence.evidence_kind==='server_receipt'};
}
if(ticket.version!=='machine-render-authorization-api-v1'||ticket.policy!=='machine-tweet-v3-api'||ticket.purpose!=='render_only'||
 ticket.publication_authorized!==false||ticket.human_review!==false||ticket.input_sha256!==sha256(raw)||input.testFixture)
 fail('MACHINE_OFFICIAL_TICKET_INVALID');
const posts=[];walkPosts(input.post,(post,field)=>posts.push({post,field}));
if(!Array.isArray(ticket.nodes)||ticket.nodes.length!==posts.length||new Set(posts.map(x=>x.post.id)).size!==posts.length||posts.length>2)
 fail('MACHINE_OFFICIAL_NODE_SET_INVALID');
const verifiedNodes=[];
for(const [index,{post:p,field}]of posts.entries()){
 const t=ticket.nodes[index];
 if(t.field!==field||t.post_id!==p.id||t.original_text_sha256!==sha256(p.originalText)||t.translation_sha256!==sha256(p.translation)||
  stable(p.review)!==stable({sourceConfirmed:false,translationConfirmed:false})||stable(p.sourceScreenshot)!==stable({file:null,capturedAt:null,reviewed:false})||
  p.sourceMediaCount!==p.media.length||p.media.some(m=>m.reviewed!==false||m.completeness!=='complete')||
  p.sourceQuote!==(p.quote?'present':'none'))fail('MACHINE_OFFICIAL_NODE_BINDING_INVALID');
 const s=await parsedFile(t.source_evidence),m=await parsedFile(t.model_evidence);
 const source=await parsedFile(s.source_payload);
 if(s.version!=='machine-render-source-evidence-api-v1'||!['owned_fixture','server_receipt'].includes(s.evidence_kind)||s.human_review!==false||s.post_id!==p.id||s.source_url!==p.url||
  s.original_text_sha256!==sha256(p.originalText)||source.post?.id!==p.id||source.post?.text!==p.originalText||source.url!==p.url||
  source.author?.username!==p.author.handle.replace(/^@/,'')||source.author?.name!==p.author.name||source.source_api!=='x-v2-official'||
  !Number.isFinite(Date.parse(source.post.created_at))||Date.parse(source.post.created_at)!==Date.parse(p.publishedAt)||
  !Array.isArray(s.provenance)||!s.provenance.length||!Array.isArray(s.media)||s.media.length!==p.media.length)
  fail('MACHINE_OFFICIAL_SOURCE_BINDING_INVALID');
 for(const proof of s.provenance)await boundFile(proof.file,proof.sha256);
 if(s.source_screenshot!==null)fail('MACHINE_OFFICIAL_SCREENSHOT_BINDING_INVALID');
 const apiProof=await verifyOfficialAuthority(s,source,p);
 for(const [i,media]of p.media.entries()){
  const record=s.media[i];if(record.index!==i+1||path.resolve(record.file)!==path.resolve(media.file)||record.completeness!=='complete')
   fail('MACHINE_OFFICIAL_MEDIA_BINDING_INVALID');
  const bytes=await boundFile(record.file,record.sha256);
  const decoded=await verifyMachineMediaBytes(bytes,{format:record.format,preview_natural_width:record.width,preview_natural_height:record.height});
  if(decoded.width!==record.width||decoded.height!==record.height)fail('MACHINE_OFFICIAL_MEDIA_PIXELS_MISMATCH');
 }
 const relation=p.relationship,refs=source.post.referenced_tweets||[];
 if(!['original','reply'].includes(relation.type))fail('MACHINE_OFFICIAL_RELATIONSHIP_BINDING_INVALID');
 if(relation.type!=='original'){
  if(!p.quote||relation.url!==p.quote.url||relation.authorName!==p.quote.author.name||
   relation.authorHandle.replace(/^@/,'')!==p.quote.author.handle.replace(/^@/,'')||
   !refs.some(r=>r.type===(relation.type==='reply'?'replied_to':'retweeted')&&r.id===p.quote.id))
   fail('MACHINE_OFFICIAL_RELATIONSHIP_BINDING_INVALID');
 }else if(p.quote&&!refs.some(r=>r.type==='quoted'&&r.id===p.quote.id))fail('MACHINE_OFFICIAL_RELATIONSHIP_BINDING_INVALID');
 if(!p.quote&&refs.some(r=>['replied_to','retweeted','quoted'].includes(r.type)))fail('MACHINE_OFFICIAL_PARENT_MISSING');
 const assessment=m.assessment;
 if(m.version!=='machine-render-model-evidence-api-v1'||m.evidence_kind!==s.evidence_kind||m.human_review!==false||m.post_id!==p.id||
  m.source_evidence_sha256!==t.source_evidence.sha256||m.translation_sha256!==sha256(p.translation)||
  assessment?.version!=='model-receipt-v1'||assessment.human_review!==false||assessment.proof_integrity_passed!==true||
  assessment.separate_requests_verified!==true||assessment.qa_recommends_pass!==true||
  assessment.source_sha256!==sha256(stable(source))||assessment.translation_sha256!==sha256(p.translation)||
  !Array.isArray(assessment.call_ids)||assessment.call_ids.length!==2||new Set(assessment.call_ids).size!==2||
  !Array.isArray(assessment.request_keys)||assessment.request_keys.length!==2||new Set(assessment.request_keys).size!==2||
  assessment.request_keys.some(k=>!/^[a-f0-9]{64}$/.test(k))||!Array.isArray(m.receipts)||m.receipts.length!==2||
  m.receipts[0].phase!=='translation'||m.receipts[1].phase!=='quality'||
  !Array.isArray(assessment.transport_modes)||assessment.transport_modes.length!==2||
  (s.evidence_kind==='owned_fixture'&&(assessment.live_transport_verified!==false||assessment.transport_modes.some(t=>t!=='custom_transport')))||
  (s.evidence_kind==='server_receipt'&&(assessment.live_transport_verified!==true||assessment.transport_modes.some(t=>t!=='default_tls'))))
  fail('MACHINE_OFFICIAL_MODEL_BINDING_INVALID');
 for(const proof of m.receipts)await boundFile(proof.file,proof.sha256);
 verifiedNodes.push({field,post_id:p.id,source_evidence_sha256:t.source_evidence.sha256,model_evidence_sha256:t.model_evidence.sha256,
  evidence_kind:s.evidence_kind,...apiProof,model_live_transport_verified:assessment.live_transport_verified===true,human_review:false});
}
class ExtendedRenderer extends Renderer{
 async start(){await super.start();if(!this.bound){const create=this.browser.newPage.bind(this.browser);
  this.browser.newPage=async options=>{const page=await create(options),set=page.setContent.bind(page);
   page.setContent=async(...args)=>{const value=await set(...args);await page.evaluate(async()=>await Promise.all([
    document.fonts.load('40px MVPText','中文あ'),document.fonts.load('38px MVPText','中文あ'),document.fonts.load('40px MVPEmoji','😀')]));return value;};return page;};this.bound=true;}return this;}
}
const renderer=new ExtendedRenderer();let page;
try{
 const original=await renderer.render(input,{baseDir:path.dirname(inputFile),outputDir:path.join(outputDir,'upstream-human-check'),allowedRoot:root,mode:'preview'});
 if(!original.report.png||original.report.issues.some(i=>i.severity==='error'&&!manualCodes.has(i.code))||
  !['translationExact','quoteComplete','imagesUnchanged','mediaAspectRatios','glyphCoverage'].every(k=>original.report.checks[k]===true)||
  original.report.checks.overflow?.length)fail('MACHINE_OFFICIAL_UPSTREAM_TECHNICAL_CHECK_FAILED');
 const issues=[],assets=await loadAssets(input,path.dirname(inputFile),issues,root),fonts=await loadFonts(),template=await loadTemplate();
 if(issues.some(i=>i.severity==='error'))fail('MACHINE_OFFICIAL_ASSET_INVALID');
 const doc=buildDocument(input,template,assets),expectedQuotes=posts.filter(p=>p.field!=='post').map(p=>p.field);
 page=await renderer.browser.newPage({viewport:{width:template.width,height:1},deviceScaleFactor:template.scale,
  locale:'zh-CN',timezoneId:input.displayTimezone,colorScheme:'light',reducedMotion:'reduce',serviceWorkers:'block'});
 await page.route('**/*',async route=>{const f=fonts.find(f=>route.request().url()===`https://renderer.invalid/fonts/${f.name}`);
  if(f)await route.fulfill({status:200,contentType:f.name.endsWith('.otf')?'font/otf':'font/ttf',body:f.bytes,headers:{'Access-Control-Allow-Origin':'*'}});
  else await route.abort('blockedbyclient');});
 await page.setContent(doc.html,{waitUntil:'load',timeout:60000});
 await page.evaluate(async()=>{await document.fonts.ready;await Promise.all([...document.images].map(i=>i.decode()));});
 const layout=await page.evaluate(()=>({width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight,
  translations:[...document.querySelectorAll('[data-translation]')].map(e=>({field:e.dataset.translation,text:e.textContent})),
  quotes:[...document.querySelectorAll('[data-quote]')].map(e=>e.dataset.quote),
  images:[...document.querySelectorAll('img[data-asset]')].map(e=>({field:e.dataset.asset,sha256:e.dataset.sha256,role:e.className,
   src:e.currentSrc,naturalWidth:e.naturalWidth,naturalHeight:e.naturalHeight,width:e.getBoundingClientRect().width,height:e.getBoundingClientRect().height})),
  fonts:document.fonts.check('40px MVPText')&&document.fonts.check('38px MVPText')&&document.fonts.check('40px MVPEmoji'),
  noWarnings:!document.querySelector('.preview-warning,.crop-warning,.missing'),
  overflow:[...document.querySelectorAll('[data-check]')].filter(e=>e.scrollWidth>e.clientWidth+1||
   (e.scrollHeight>e.clientHeight+1&&getComputedStyle(e).overflowY!=='visible')).map(e=>e.dataset.check)}));
 const checks={translationExact:stable(layout.translations)===stable(doc.translations),quoteComplete:stable(layout.quotes)===stable(expectedQuotes),
  imagesUnchanged:layout.images.length===doc.imageExpectations.length&&layout.images.every((x,i)=>x.field===doc.imageExpectations[i].field&&
   sha256(Buffer.from(x.src.split(',')[1],'base64'))===doc.imageExpectations[i].sha256),
  mediaAspectRatios:layout.images.filter(i=>i.role==='media-image').every(i=>Math.abs(i.width/i.height-i.naturalWidth/i.naturalHeight)<.005),
  fontsLoaded:layout.fonts,glyphCoverage:original.report.checks.glyphCoverage,warningFree:layout.noWarnings,
  canvas:layout.width===template.width&&layout.height*template.scale<=30000&&layout.width*layout.height*template.scale**2<=100000000,overflow:layout.overflow.length===0};
 if(!Object.values(checks).every(x=>x===true))fail('MACHINE_OFFICIAL_FINAL_LAYOUT_FAILED');
 const bytes=await page.screenshot({fullPage:true,type:'png',animations:'disabled',caret:'hide',scale:'device',timeout:60000});
 const png=path.join(outputDir,`${input.post.id}.machine-api.png`);await fs.writeFile(png,bytes,{mode:0o600});
 const report={version:'machine-render-api-v1',policy:'machine-tweet-v3-api',mode:'machine-render-only-api-v1',prototype:true,source_authority:'x-v2-official',source_screenshot:null,machine_renderable:true,
  machine_publishable:false,publication_authorized:false,human_review:false,input_sha256:sha256(raw),ticket_sha256:sha256(await fs.readFile(ticketFile)),
  nodes:verifiedNodes,upstream_human_publishable:original.report.publishable,upstream_report:original.report,checks,
  template:original.report.template,fonts:original.report.fonts,expected_quotes:expectedQuotes,
  layout:{...layout,images:layout.images.map(({src,...rest})=>rest)},
  png:{file:png,sha256:sha256(bytes),bytes:bytes.length,width:layout.width*template.scale,height:layout.height*template.scale},
  limitations:['Official API response hashes and rendering are bound; the independent Python source/policy verifier still decides source validity and publication.',
   'No original-post screenshot exists or is fabricated. All human fields stay false; this render-only ticket grants no publication and performs no image-text translation.']};
 await fs.writeFile(path.join(outputDir,'machine-render-api-result.json'),JSON.stringify(report,null,2),{mode:0o600});
}finally{await page?.close();await renderer.close();}
