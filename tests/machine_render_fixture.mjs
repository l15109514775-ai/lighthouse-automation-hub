// Rendering acceptance uses owned synthetic inputs and never invokes a sender.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {promisify} from 'node:util';
import {execFile} from 'node:child_process';
import crypto from 'node:crypto';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
const run=promisify(execFile),root=path.dirname(path.dirname(fileURLToPath(import.meta.url))),out=path.resolve(process.argv[2]);
await fs.mkdir(out,{recursive:true});
const browser=await chromium.launch({headless:true});
try{const page=await browser.newPage({viewport:{width:600,height:240}});await page.route('**/*',r=>r.abort());
 await page.setContent('<article style="background:#fff;color:#111;font-size:24px;padding:20px">Owned synthetic renderer fixture<br>独立机器渲染测试 23:40 🥳</article>');
 await page.screenshot({path:path.join(out,'source.png')});
 await page.screenshot({path:path.join(out,'avatar.png'),clip:{x:0,y:0,width:40,height:40}});
}finally{await browser.close();}
const digest=b=>crypto.createHash('sha256').update(b).digest('hex');
const input={schemaVersion:1,displayTimezone:'Asia/Shanghai',post:{id:'123',url:'https://x.com/fixture_only/status/123',
 author:{name:'Owned renderer fixture',handle:'fixture_only',avatar:path.join(out,'avatar.png'),verified:false},
 originalText:'Owned synthetic renderer fixture\nRendering only 23:40 🥳',translation:'项目自有的排版测试\n仅验证渲染 23:40 🥳',
 publishedAt:'2026-10-31T12:00:00+09:00',sourceTimezone:'Asia/Tokyo',
 sourceScreenshot:{file:path.join(out,'source.png'),capturedAt:'2026-10-31T12:30:00+09:00',reviewed:false},
 sourceMediaCount:0,sourceQuote:'none',quote:null,media:[],relationship:{type:'original'},
 review:{sourceConfirmed:false,translationConfirmed:false},notes:[]}};
const passed=[];
async function invoke(name,value,alterTicket=()=>{},envChange={}){
 const dir=path.join(out,name);await fs.mkdir(dir,{recursive:true});
 const file=path.join(dir,'input.json'),body=JSON.stringify(value,null,2);await fs.writeFile(file,body);
 const ticket={version:'machine-render-authorization-v1',policy:'machine-tweet-v1',input_sha256:digest(body),
  machine_source_verified:true,model_receipts_verified:true,human_review:false};alterTicket(ticket);
 const ticketFile=path.join(dir,'ticket.json');await fs.writeFile(ticketFile,JSON.stringify(ticket));
 let result;try{result=await run(process.execPath,[path.join(root,'adapters/render_machine.mjs'),file,dir,out,ticketFile],
  {cwd:path.join(root,'compositor'),env:{...process.env,TEMP:out,TMP:out,TMPDIR:out,...envChange},timeout:120000,maxBuffer:100000});}
 catch(e){result=e;}
 return {dir,file,body,result};
}
const good=await invoke('valid',input);assert.equal(good.result.code,undefined,good.result.stderr);
const report=JSON.parse(await fs.readFile(path.join(good.dir,'machine-render-result.json'),'utf8'));
assert.equal(report.machine_publishable,true);assert.equal(report.human_review,false);assert.equal(report.upstream_human_publishable,false);
assert.equal(report.mode,'machine-publish-v1');assert(Object.values(report.checks).every(x=>x===true));
assert.equal(await fs.readFile(good.file,'utf8'),good.body);
const saved=JSON.parse(good.body);assert.equal(saved.post.sourceScreenshot.reviewed,false);assert.deepEqual(saved.post.review,{sourceConfirmed:false,translationConfirmed:false});
const blocked=report.upstream_report;assert.equal(blocked.publishable,false);assert.equal(blocked.status,'blocked');assert.equal(blocked.mode,'preview');
assert.deepEqual([...new Set(blocked.issues.filter(i=>i.severity==='error').map(i=>i.code))].sort(),
 ['SCREENSHOT_UNREVIEWED','SOURCE_UNREVIEWED','TRANSLATION_UNREVIEWED']);
assert.equal(digest(await fs.readFile(report.png.file)),report.png.sha256);assert.equal(report.input_sha256,digest(good.body));
const lock=JSON.parse(await fs.readFile(path.join(root,'compositor/template-lock.json'),'utf8'));
assert.equal(report.template.sourceSha256,lock.sourceSha256);assert.equal(report.template.referenceSha256,lock.referenceSha256);
const fonts=JSON.parse(await fs.readFile(path.join(root,'compositor/assets/fonts/manifest.json'),'utf8'));
assert.equal(report.fonts.length,3);for(const f of report.fonts)assert.equal(f.sha256,fonts[f.name].sha256);
passed.push('real_machine_png_keeps_upstream_manual_report_blocked_and_input_untouched');
passed.push('original_locked_template_and_bundled_font_hashes_reused');
for(const [name,mutate,ticket] of [
 ['forged-human-flag',v=>v.post.review.sourceConfirmed=true,()=>{}],
 ['missing-source-attestation',()=>{},t=>t.machine_source_verified=false],
 ['missing-model-attestation',()=>{},t=>t.model_receipts_verified=false],
 ['changed-input-hash',()=>{},t=>t.input_sha256='0'.repeat(64)],
 ['fixture-tag-cannot-publish',v=>v.testFixture=true,()=>{}],
 ['media-outside-policy',v=>v.post.media.push({file:path.join(out,'avatar.png'),completeness:'unknown',reviewed:false}),()=>{}]]){
 const value=structuredClone(input);mutate(value);const r=await invoke(name,value,ticket);
 assert.notEqual(r.result.code,undefined,name);assert(String(r.result.stderr).includes('MACHINE_RENDER_AUTHORIZATION_INVALID'),name);
 await assert.rejects(fs.stat(path.join(r.dir,'machine-render-result.json')),{code:'ENOENT'});passed.push(name);
}
const invalid=structuredClone(input);invalid.post.author.avatar=path.join(out,'missing-avatar.png');
const invalidResult=await invoke('real-asset-error',invalid);
assert.notEqual(invalidResult.result.code,undefined);assert(String(invalidResult.result.stderr).includes('MACHINE_RENDER_UPSTREAM_TECHNICAL_CHECK_FAILED'));
passed.push('real_upstream_asset_failure_blocks_machine_png');
console.log(JSON.stringify({passed,human_review:false,external_calls:0,publish_requests:0,
 report_file:path.join(good.dir,'machine-render-result.json')}));
