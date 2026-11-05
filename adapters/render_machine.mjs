// Explicit machine-render-v1 adapter. Upstream human renderer/schema unchanged.
import fs from 'node:fs/promises';
import path from 'node:path';
import {Renderer} from '../compositor/src/renderer.mjs';
import {loadTemplate,loadFonts,loadAssets,sha256,stable} from '../compositor/src/core.mjs';
import {buildDocument} from '../compositor/src/template.mjs';
const [inputFile,outputDir,allowedRoot,attestationFile]=process.argv.slice(2);
const input=JSON.parse(await fs.readFile(inputFile,'utf8')),ticket=JSON.parse(await fs.readFile(attestationFile,'utf8'));
const manualCodes=new Set(['TRANSLATION_UNREVIEWED','SOURCE_UNREVIEWED','SCREENSHOT_UNREVIEWED']);
const post=input.post;
if(ticket.version!=='machine-render-authorization-v1'||ticket.policy!=='machine-tweet-v1'||
 ticket.input_sha256!==sha256(await fs.readFile(inputFile))||ticket.machine_source_verified!==true||
 ticket.model_receipts_verified!==true||ticket.human_review!==false||input.testFixture||
 stable(post.review)!==stable({sourceConfirmed:false,translationConfirmed:false})||post.sourceScreenshot.reviewed!==false||
 post.media.length||post.quote||post.relationship.type!=='original'||post.sourceMediaCount!==0||post.sourceQuote!=='none')
 throw new Error('MACHINE_RENDER_AUTHORIZATION_INVALID');
class MachineRenderer extends Renderer {
 async start(){await super.start();if(!this.bound){const create=this.browser.newPage.bind(this.browser);
  this.browser.newPage=async options=>{const page=await create(options),set=page.setContent.bind(page);
   page.setContent=async(...args)=>{const value=await set(...args);await page.evaluate(async()=>{
    await Promise.all([document.fonts.load('40px MVPText','中文あ'),document.fonts.load('38px MVPText','中文あ'),document.fonts.load('40px MVPEmoji','😀')]);
   });return value;};return page;};this.bound=true;}return this;}
}
const renderer=new MachineRenderer();let page;
try{
 // Keep the genuine upstream report blocked by its three human requirements.
 const original=await renderer.render(input,{baseDir:path.dirname(inputFile),outputDir:path.join(outputDir,'upstream-human-check'),allowedRoot,mode:'preview'});
 const errors=original.report.issues.filter(i=>i.severity==='error');
 if(!original.report.png||errors.some(i=>!manualCodes.has(i.code))||
  original.report.checks.overflow?.length||!['translationExact','quoteComplete','imagesUnchanged','mediaAspectRatios','glyphCoverage'].every(k=>original.report.checks[k]===true))
  throw new Error('MACHINE_RENDER_UPSTREAM_TECHNICAL_CHECK_FAILED');
 const issues=[],assets=await loadAssets(input,path.dirname(inputFile),issues,allowedRoot),fonts=await loadFonts(),template=await loadTemplate();
 if(issues.some(i=>i.severity==='error'))throw new Error('MACHINE_RENDER_ASSET_INVALID');
 const doc=buildDocument(input,template,assets);
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
  images:[...document.querySelectorAll('img[data-asset]')].map(e=>({sha256:e.dataset.sha256,src:e.currentSrc})),
  fonts:document.fonts.check('40px MVPText')&&document.fonts.check('38px MVPText')&&document.fonts.check('40px MVPEmoji'),
  overflow:[...document.querySelectorAll('[data-check]')].filter(e=>e.scrollWidth>e.clientWidth+1||
   (e.scrollHeight>e.clientHeight+1&&getComputedStyle(e).overflowY!=='visible')).map(e=>e.dataset.check)}));
 const checks={translationExact:stable(layout.translations)===stable(doc.translations),quoteComplete:layout.quotes.length===0,
  imagesUnchanged:layout.images.length===doc.imageExpectations.length&&layout.images.every((x,i)=>
   sha256(Buffer.from(x.src.split(',')[1],'base64'))===doc.imageExpectations[i].sha256),
  fontsLoaded:layout.fonts,canvas:layout.width===template.width&&layout.height*template.scale<=30000&&
   layout.width*layout.height*template.scale**2<=100000000,overflow:layout.overflow.length===0};
 if(!Object.values(checks).every(x=>x===true))throw new Error('MACHINE_RENDER_FINAL_LAYOUT_FAILED');
 const png=path.join(outputDir,`${post.id}.machine-v1.png`),bytes=await page.screenshot({fullPage:true,type:'png',animations:'disabled',caret:'hide',scale:'device',timeout:60000});
 await fs.writeFile(png,bytes,{mode:0o600});
 const report={version:'machine-render-v1',mode:'machine-publish-v1',policy:'machine-tweet-v1',machine_publishable:true,
  human_review:false,upstream_human_publishable:original.report.publishable,input_sha256:sha256(await fs.readFile(inputFile)),
  upstream_report:original.report,checks,fonts:original.report.fonts,template:original.report.template,
  source_url:post.url,png:{file:png,sha256:sha256(bytes),bytes:bytes.length,width:template.width*template.scale,height:layout.height*template.scale},
  limitations:['Machine approval of rendered visible plain text, not human review or official API evidence.','Independent model QA can still miss semantic errors.']};
 await fs.writeFile(path.join(outputDir,'machine-render-result.json'),JSON.stringify(report,null,2),{mode:0o600});
}finally{await page?.close();await renderer.close();}
