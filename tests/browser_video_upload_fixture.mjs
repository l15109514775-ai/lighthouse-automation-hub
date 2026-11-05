// Owned .test HTML only. No real account, platform API, cookie or live upload.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {createVideoUploadPageDriver} from '../adapters/browser_video_upload.mjs';

const out=path.resolve(process.argv[2]);await fs.mkdir(out,{recursive:true});
const file=path.join(out,'owned-fixture.mp4'),content=Buffer.from('Owned fixture bytes, not a real media upload.');await fs.writeFile(file,content);
const sha=createHash('sha256').update(content).digest('hex'),key='a'.repeat(64),bv='BV1234567890';
const metadata={title:'Owned title',tid:1,tag:'owned',desc:'Owned description',copyright:2,source:'https://www.youtube.com/watch?v=owned123456',no_reprint:1};
const selectors=Object.fromEntries(Object.keys(metadata).map(k=>[k,'#meta-'+k]));
const layout={version:'website-video-layout-v1',validation:'owned_fixture_only_unverified_live',
 origin:'https://video.owned.test',artifactRoot:out,account:{linkSelector:'#account',nameSelector:'#account-name',origin:'https://account.owned.test'},
 selectors:{fileInput:'#file',uploadReference:'.upload-ref',submitButton:'#submit',resultLink:'.native-result',status:'.status'},
 metadataFields:selectors,challenges:{captcha:['.captcha'],login:['.login'],terms:['.terms'],security:['.security']},
 statusTexts:{awaiting_moderation:['Owned pending'],completed:['Owned accepted'],failed:['Owned rejected']},timeoutMs:250};
const passed=[];let externalCalls=0,ownedDocuments=0;const browser=await chromium.launch({headless:true});
const context=await browser.newContext(),page=await context.newPage();page.setDefaultTimeout(1000);let document='';
await context.route('**/*',route=>{
 if(route.request().isNavigationRequest()&&route.request().url().startsWith('https://video.owned.test/')){ownedDocuments++;return route.fulfill({contentType:'text/html',body:document});}
 externalCalls++;return route.abort();
});
async function load(options={}){
 document=`<!doctype html><style>body{font:16px Arial}input,button,a,span{display:block;margin:5px}.challenge{padding:30px}</style>
 <a id="account" href="https://account.owned.test/123456">Account</a><span id="account-name">Owned account</span>
 ${Object.keys(metadata).map(k=>`<input id="meta-${k}" type="text">`).join('')}
 <input id="file" type="file"><div id="refs"></div><button id="submit">Owned submit</button><div id="results"></div>
 ${['captcha','login','terms','security'].map(k=>`<div class="${k} challenge" ${options.challenge===k?'':'hidden'}>Owned ${k}</div>`).join('')}
 <script>window.counts={selected:0,submitted:0,filled:0};window.fileHash=null;
 document.querySelectorAll('[id^=meta-]').forEach(e=>e.addEventListener('input',()=>counts.filled++));
 document.querySelector('#file').addEventListener('change',async e=>{
  counts.selected++;const f=e.target.files[0];const bytes=await crypto.subtle.digest('SHA-256',await f.arrayBuffer());
  window.fileHash=Array.from(new Uint8Array(bytes),x=>x.toString(16).padStart(2,'0')).join('');
  document.querySelector('#refs').innerHTML=${JSON.stringify(options.noRef?'':options.duplicateRef?'<span class="upload-ref">owned-upload-1</span><span class="upload-ref">owned-upload-2</span>':'<span class="upload-ref">owned-upload-1</span>')};
 });
 document.querySelector('#submit').addEventListener('click',()=>{counts.submitted++;document.querySelector('#results').innerHTML=${JSON.stringify(options.unknown?'':`<a class="native-result" href="https://www.bilibili.com/video/${bv}">Owned native BV</a><span class="status">Owned pending</span>`)};});</script>`;
 await page.goto('https://video.owned.test/upload',{waitUntil:'load'});
}
const input=()=>({key,file,artifact_sha256:sha,file_bytes:content.length,metadata:structuredClone(metadata)});
const make=(options={})=>createVideoUploadPageDriver(page,layout,{beforeUpload:async()=>{},beforeSubmit:async()=>{},...options});
async function counts(){return page.evaluate(()=>window.counts);}
const rejects=async(fn,code)=>assert.rejects(fn,error=>error.code===code);
try{
 await load();let uploads=0,submits=0;
 const driver=make({beforeUpload:async()=>{uploads++;},beforeSubmit:async()=>{submits++;}});
 assert.deepEqual(await driver.accountIdentity(),{site_uid:'123456',name:'Owned account'});
 const receipt=await driver.prepareUpload(input());
 assert.deepEqual(Object.keys(receipt).sort(),['version','key','artifact_sha256','file_bytes','site_uid','upload_reference','evidence_kind','server_file_hash_verified'].sort());
 assert.equal(receipt.artifact_sha256,sha);assert.equal(receipt.file_bytes,content.length);assert.equal(receipt.server_file_hash_verified,false);
 assert.equal(await page.evaluate(()=>window.fileHash),sha);assert.equal(uploads,Object.keys(metadata).length+1);
 assert.deepEqual(await driver.submit({key,upload_receipt:receipt}),{state:'awaiting_moderation',external_id:bv,url:'https://www.bilibili.com/video/'+bv});
 assert.equal(submits,1);assert.deepEqual(await counts(),{selected:1,submitted:1,filled:7});
 await rejects(()=>driver.submit({key,upload_receipt:receipt}),'WEB_VIDEO_SUBMIT_ALREADY_CONSUMED');assert.equal((await counts()).submitted,1);
 await rejects(()=>driver.prepareUpload(input()),'WEB_VIDEO_PAGE_ATTEMPT_ALREADY_BOUND');
 passed.push('native_file_name_size_browser_bytes_sha_and_single_submit');

 for(const challenge of ['captcha','login','terms','security']){
  await load({challenge});await rejects(()=>make().prepareUpload(input()),'WEB_VIDEO_'+challenge.toUpperCase()+'_REQUIRED');
  assert.deepEqual(await counts(),{selected:0,submitted:0,filled:0});passed.push('stop_'+challenge+'_without_closing_or_bypassing');
 }
 await load();await rejects(()=>make({beforeUpload:undefined}).prepareUpload(input()),'WEB_VIDEO_UPLOAD_CALLBACK_REQUIRED');
 assert.equal((await counts()).selected,0);passed.push('missing_upload_callback_prevents_native_writes');
 await load();let d=make({beforeSubmit:undefined}),r=await d.prepareUpload(input());
 await rejects(()=>d.submit({key,upload_receipt:r}),'WEB_VIDEO_SUBMIT_CALLBACK_REQUIRED');assert.equal((await counts()).submitted,0);
 passed.push('missing_submit_callback_prevents_click');
 await load();await rejects(()=>make({beforeUpload:async()=>false}).prepareUpload(input()),'WEB_VIDEO_UPLOAD_CALLBACK_REQUIRED');
 assert.deepEqual(await counts(),{selected:0,submitted:0,filled:0});passed.push('rejected_callback_never_writes');
 await load();await rejects(()=>make().prepareUpload({...input(),artifact_sha256:'0'.repeat(64)}),'WEB_VIDEO_ARTIFACT_HASH_MISMATCH');
 assert.deepEqual(await counts(),{selected:0,submitted:0,filled:0});passed.push('complete_file_hash_mismatch_before_writes');
 await load();await rejects(()=>make().prepareUpload({...input(),file_bytes:content.length+1}),'WEB_VIDEO_ARTIFACT_SIZE_MISMATCH');
 assert.equal((await counts()).selected,0);passed.push('file_size_mismatch_before_selection');
 await load();d=make({beforeUpload:async c=>{if(c.boundary==='file_selection')await fs.writeFile(file,'changed owned bytes');}});
 await rejects(()=>d.prepareUpload(input()),'WEB_VIDEO_ARTIFACT_CHANGED');assert.equal((await counts()).selected,0);await fs.writeFile(file,content);
 passed.push('revalidate_file_after_authorization_callback');

 await load({duplicateRef:true});await rejects(()=>make().prepareUpload(input()),'WEB_VIDEO_UPLOAD_REFERENCE_AMBIGUOUS');passed.push('duplicate_native_upload_reference_rejected');
 await load({noRef:true});await rejects(()=>make().prepareUpload(input()),'WEB_VIDEO_UPLOAD_RESULT_UNKNOWN');passed.push('missing_native_reference_is_unknown');
 await load();d=make();r=await d.prepareUpload(input());await page.locator('.upload-ref').evaluate(el=>{el.textContent='other-upload';});
 await rejects(()=>d.submit({key,upload_receipt:r}),'WEB_VIDEO_UPLOAD_REFERENCE_CHANGED');assert.equal((await counts()).submitted,0);passed.push('reference_mutation_before_submit_rejected');
 await load();d=make();r=await d.prepareUpload(input());await page.locator('#meta-title').fill('Changed title');
 await rejects(()=>d.submit({key,upload_receipt:r}),'WEB_VIDEO_METADATA_CHANGED');assert.equal((await counts()).submitted,0);passed.push('metadata_mutation_before_submit_rejected');
 await load();await page.locator('#results').evaluate((el,id)=>el.innerHTML=`<a class="native-result" href="https://www.bilibili.com/video/${id}">Old unrelated result</a>`,bv);
 await rejects(()=>make().prepareUpload(input()),'WEB_VIDEO_RESULT_ALREADY_PRESENT');assert.deepEqual(await counts(),{selected:0,submitted:0,filled:0});passed.push('preexisting_bv_cannot_be_bound_to_a_new_upload');
 await load();d=make();r=await d.prepareUpload(input());await page.locator('#results').evaluate((el,id)=>el.innerHTML=`<a class="native-result" href="https://www.bilibili.com/video/${id}">Old unrelated result</a>`,bv);
 await rejects(()=>d.submit({key,upload_receipt:r}),'WEB_VIDEO_RESULT_ALREADY_PRESENT');assert.equal((await counts()).submitted,0);passed.push('result_appearing_before_click_is_not_claimed');
 await load();d=make({beforeSubmit:async()=>{await page.locator('.terms').evaluate(el=>{el.hidden=false;});}});r=await d.prepareUpload(input());
 await rejects(()=>d.submit({key,upload_receipt:r}),'WEB_VIDEO_TERMS_REQUIRED');assert.equal((await counts()).submitted,0);passed.push('challenge_after_submit_callback_still_stops');
 await load();d=make();r=await d.prepareUpload(input());await page.locator('#account').evaluate(el=>el.href='https://account.owned.test/999');
 await rejects(()=>d.submit({key,upload_receipt:r}),'WEB_VIDEO_ACCOUNT_CHANGED');assert.equal((await counts()).submitted,0);passed.push('changed_account_stops_submit');

 await load({unknown:true});d=make();r=await d.prepareUpload(input());assert.deepEqual(await d.submit({key,upload_receipt:r}),{state:'unknown'});
 await rejects(()=>d.submit({key,upload_receipt:r}),'WEB_VIDEO_SUBMIT_ALREADY_CONSUMED');assert.equal((await counts()).submitted,1);passed.push('submit_unknown_never_reclicks');
 await load();d=make({beforeUpload:undefined,beforeSubmit:undefined});assert.deepEqual(await d.lookup({key}),{state:'unknown'});assert.deepEqual(await counts(),{selected:0,submitted:0,filled:0});passed.push('unknown_lookup_has_zero_writes_and_does_not_use_title');
 await load();d=make();r=await d.prepareUpload(input());const badHash={...r,upload_reference_sha256:'0'.repeat(64)};delete badHash.upload_reference;
 await rejects(()=>d.submit({key,upload_receipt:badHash}),'WEB_VIDEO_UPLOAD_RECEIPT_MISMATCH');assert.equal((await counts()).submitted,0);passed.push('forged_reference_hash_prevents_submit');
 await load();d=make();r=await d.prepareUpload(input());const hashed={...r,upload_reference_sha256:createHash('sha256').update(r.upload_reference).digest('hex')};delete hashed.upload_reference;
 await rejects(()=>make().submit({key,upload_receipt:hashed}),'WEB_VIDEO_UPLOAD_ATTEMPT_NOT_BOUND');passed.push('fresh_driver_cannot_claim_an_old_browser_upload_attempt');
 assert.equal((await d.submit({key,upload_receipt:hashed})).external_id,bv);passed.push('hashed_reference_receipt_supported_without_persisting_raw_reference');
 for(const [label,state]of [['Owned pending','awaiting_moderation'],['Owned accepted','completed'],['Owned rejected','failed']]){
  await page.locator('.status').evaluate((el,x)=>el.textContent=x,label);
  const value=await d.lookup({key,external_id:bv});assert.equal(value.state,state);if(state==='failed')assert.equal(value.error_code,'BILIBILI_MODERATION_REJECTED');
 }
 assert.equal((await counts()).submitted,1);passed.push('known_bv_read_only_moderation_states');
 await rejects(()=>d.lookup({key,external_id:'BVabcdefghij'}),'WEB_VIDEO_BV_BINDING_MISMATCH');passed.push('wrong_known_bv_is_not_claimed');
 await page.locator('#results').evaluate((el,id)=>el.insertAdjacentHTML('beforeend',`<a class="native-result" href="https://www.bilibili.com/video/${id}">duplicate</a>`),bv);
 await rejects(()=>d.lookup({key,external_id:bv}),'WEB_VIDEO_RESULT_AMBIGUOUS');passed.push('duplicate_native_bv_rejected');
 assert.throws(()=>createVideoUploadPageDriver(page,{}),error=>error.code==='WEB_VIDEO_LAYOUT_UNVERIFIED_REQUIRED');passed.push('no_default_real_layout');
 assert.throws(()=>createVideoUploadPageDriver(page,{...layout,origin:'https://member.bilibili.com'}),error=>error.code==='WEB_VIDEO_OWNED_TEST_ORIGIN_REQUIRED');passed.push('owned_only_profile_cannot_target_a_live_platform_origin');
 const wrong={...layout,origin:'https://other.owned.test'};await rejects(()=>createVideoUploadPageDriver(page,wrong).accountIdentity(),'WEB_VIDEO_ORIGIN_MISMATCH');passed.push('explicit_origin_is_enforced');
 process.stdout.write(JSON.stringify({passed,external_calls:externalCalls,owned_documents:ownedDocuments,live_tested:false,human_review:false,publication_authorized:false,real_uploads:0}));
}finally{await context.close();await browser.close();}
