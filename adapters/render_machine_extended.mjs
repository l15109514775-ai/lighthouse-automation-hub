// Independent v2 rendering prototype. No publisher, credential or policy enable.
import fs from 'node:fs/promises';
import path from 'node:path';
import {Renderer} from '../compositor/src/renderer.mjs';
import {loadTemplate,loadFonts,loadAssets,sha256,stable,walkPosts} from '../compositor/src/core.mjs';
import {buildDocument} from '../compositor/src/template.mjs';
import {verifyMachineMediaBytes} from './machine_media_capture.mjs';
const [inputFile,outputDir,allowedRoot,ticketFile]=process.argv.slice(2);
const manualCodes=new Set(['TRANSLATION_UNREVIEWED','SOURCE_UNREVIEWED','SCREENSHOT_UNREVIEWED','MEDIA_UNREVIEWED']);
const root=await fs.realpath(allowedRoot);
const fail=code=>{throw new Error(code);};
for(const filename of [inputFile,ticketFile]){const resolved=await fs.realpath(filename),relative=path.relative(root,resolved);
 if(!relative||relative.startsWith('..')||path.isAbsolute(relative))fail('MACHINE_EXTENDED_EVIDENCE_PATH_INVALID');}
const parseJson=bytes=>{try{return JSON.parse(bytes);}catch{fail('MACHINE_EXTENDED_EVIDENCE_JSON_INVALID');}};
const raw=await fs.readFile(inputFile),input=parseJson(raw),ticket=parseJson(await fs.readFile(ticketFile,'utf8'));
const outputParent=await fs.realpath(path.dirname(path.resolve(outputDir))),outputRelative=path.relative(root,outputParent);
if(outputRelative.startsWith('..')||path.isAbsolute(outputRelative))fail('MACHINE_EXTENDED_OUTPUT_PATH_INVALID');
// Attempt directories are immutable. A failed rerun cannot leave a stale result
// that looks like the product of the new input or overwrite an older report.
await fs.mkdir(outputDir,{recursive:false,mode:0o700});
async function boundFile(file,expected){
 if(typeof file!=='string'||!/^[a-f0-9]{64}$/.test(expected||''))fail('MACHINE_EXTENDED_EVIDENCE_BINDING_INVALID');
 const resolved=await fs.realpath(file),rel=path.relative(root,resolved);
 if(!rel||rel.startsWith('..')||path.isAbsolute(rel)||(await fs.lstat(file)).isSymbolicLink())fail('MACHINE_EXTENDED_EVIDENCE_PATH_INVALID');
 for(let p=path.dirname(path.resolve(file));p!==root&&p!==path.dirname(p);p=path.dirname(p))
  if((await fs.lstat(p)).isSymbolicLink())fail('MACHINE_EXTENDED_EVIDENCE_PATH_INVALID');
 const bytes=await fs.readFile(resolved);if(sha256(bytes)!==expected)fail('MACHINE_EXTENDED_EVIDENCE_HASH_CHANGED');return bytes;
}
const parsedFile=async ref=>{if(!ref||typeof ref!=='object')fail('MACHINE_EXTENDED_EVIDENCE_BINDING_INVALID');return parseJson(await boundFile(ref.file,ref.sha256));};
if(ticket.version!=='machine-render-authorization-v2'||ticket.policy!=='machine-tweet-v2'||ticket.purpose!=='render_only'||
 ticket.publication_authorized!==false||ticket.human_review!==false||ticket.input_sha256!==sha256(raw)||input.testFixture)
 fail('MACHINE_EXTENDED_TICKET_INVALID');
const posts=[];walkPosts(input.post,(post,field)=>posts.push({post,field}));
if(!Array.isArray(ticket.nodes)||ticket.nodes.length!==posts.length||new Set(posts.map(x=>x.post.id)).size!==posts.length||posts.length>4)
 fail('MACHINE_EXTENDED_NODE_SET_INVALID');
const verifiedNodes=[];
for(const [index,{post:p,field}]of posts.entries()){
 const t=ticket.nodes[index];
 if(t.field!==field||t.post_id!==p.id||t.original_text_sha256!==sha256(p.originalText)||t.translation_sha256!==sha256(p.translation)||
  stable(p.review)!==stable({sourceConfirmed:false,translationConfirmed:false})||p.sourceScreenshot?.reviewed!==false||
  p.sourceMediaCount!==p.media.length||p.media.some(m=>m.reviewed!==false||m.completeness!=='complete')||
  p.sourceQuote!==(p.quote?'present':'none'))fail('MACHINE_EXTENDED_NODE_BINDING_INVALID');
 const s=await parsedFile(t.source_evidence),m=await parsedFile(t.model_evidence);
 const source=await parsedFile(s.source_payload);
 if(s.version!=='machine-render-source-evidence-v2'||!['owned_fixture','server_receipt'].includes(s.evidence_kind)||s.human_review!==false||s.post_id!==p.id||s.source_url!==p.url||
  s.original_text_sha256!==sha256(p.originalText)||source.post?.id!==p.id||source.post?.text!==p.originalText||source.url!==p.url||
  source.author?.username!==p.author.handle.replace(/^@/,'')||source.author?.name!==p.author.name||
  !Number.isFinite(Date.parse(source.post.created_at))||Date.parse(source.post.created_at)!==Date.parse(p.publishedAt)||
  !Array.isArray(s.provenance)||!s.provenance.length||!Array.isArray(s.media)||s.media.length!==p.media.length)
  fail('MACHINE_EXTENDED_SOURCE_BINDING_INVALID');
 for(const proof of s.provenance)await boundFile(proof.file,proof.sha256);
 if(path.resolve(s.source_screenshot.file)!==path.resolve(p.sourceScreenshot.file))fail('MACHINE_EXTENDED_SCREENSHOT_BINDING_INVALID');
 await boundFile(s.source_screenshot.file,s.source_screenshot.sha256);
 for(const [i,media]of p.media.entries()){
  const record=s.media[i];if(record.index!==i+1||path.resolve(record.file)!==path.resolve(media.file)||record.completeness!=='complete')
   fail('MACHINE_EXTENDED_MEDIA_BINDING_INVALID');
  const bytes=await boundFile(record.file,record.sha256);
  await verifyMachineMediaBytes(bytes,{format:record.format,preview_natural_width:record.width,preview_natural_height:record.height});
 }
 const relation=p.relationship,refs=source.post.referenced_tweets||[];
 if(relation.type!=='original'){
  if(!p.quote||relation.url!==p.quote.url||relation.authorName!==p.quote.author.name||
   relation.authorHandle.replace(/^@/,'')!==p.quote.author.handle.replace(/^@/,'')||
   !refs.some(r=>r.type===(relation.type==='reply'?'replied_to':'retweeted')&&r.id===p.quote.id))
   fail('MACHINE_EXTENDED_RELATIONSHIP_BINDING_INVALID');
 }else if(p.quote&&!refs.some(r=>r.type==='quoted'&&r.id===p.quote.id))fail('MACHINE_EXTENDED_RELATIONSHIP_BINDING_INVALID');
 if(!p.quote&&refs.some(r=>['replied_to','retweeted','quoted'].includes(r.type)))fail('MACHINE_EXTENDED_PARENT_MISSING');
 const assessment=m.assessment;
 if(m.version!=='machine-render-model-evidence-v2'||m.evidence_kind!==s.evidence_kind||m.human_review!==false||m.post_id!==p.id||
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
  fail('MACHINE_EXTENDED_MODEL_BINDING_INVALID');
 for(const proof of m.receipts)await boundFile(proof.file,proof.sha256);
 verifiedNodes.push({field,post_id:p.id,source_evidence_sha256:t.source_evidence.sha256,model_evidence_sha256:t.model_evidence.sha256,
  evidence_kind:s.evidence_kind,model_live_transport_verified:assessment.live_transport_verified===true,human_review:false});
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
  original.report.checks.overflow?.length)fail('MACHINE_EXTENDED_UPSTREAM_TECHNICAL_CHECK_FAILED');
 const issues=[],assets=await loadAssets(input,path.dirname(inputFile),issues,root),fonts=await loadFonts(),template=await loadTemplate();
 if(issues.some(i=>i.severity==='error'))fail('MACHINE_EXTENDED_ASSET_INVALID');
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
 if(!Object.values(checks).every(x=>x===true))fail('MACHINE_EXTENDED_FINAL_LAYOUT_FAILED');
 const bytes=await page.screenshot({fullPage:true,type:'png',animations:'disabled',caret:'hide',scale:'device',timeout:60000});
 const png=path.join(outputDir,`${input.post.id}.machine-v2.png`);await fs.writeFile(png,bytes,{mode:0o600});
 const report={version:'machine-render-v2',policy:'machine-tweet-v2',mode:'machine-render-only-v2',prototype:true,machine_renderable:true,
  machine_publishable:false,publication_authorized:false,human_review:false,input_sha256:sha256(raw),ticket_sha256:sha256(await fs.readFile(ticketFile)),
  nodes:verifiedNodes,upstream_human_publishable:original.report.publishable,upstream_report:original.report,checks,
  template:original.report.template,fonts:original.report.fonts,expected_quotes:expectedQuotes,
  layout:{...layout,images:layout.images.map(({src,...rest})=>rest)},
  png:{file:png,sha256:sha256(bytes),bytes:bytes.length,width:layout.width*template.scale,height:layout.height*template.scale},
  limitations:['Evidence hashes and rendering are bound; source/model validity and rights still require the independent policy verifier.',
   'This prototype render ticket grants no publication authorization and performs no image-text translation.']};
 await fs.writeFile(path.join(outputDir,'machine-render-v2-result.json'),JSON.stringify(report,null,2),{mode:0o600});
}finally{await page?.close();await renderer.close();}
