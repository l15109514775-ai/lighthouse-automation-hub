import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {observeRepostControl,validateRepostSpec} from '../adapters/browser_tweet_repost_check_v1.mjs';
const [scenario,folder,requestFile]=process.argv.slice(2);
const sha=b=>crypto.createHash('sha256').update(b).digest('hex');
let spec={version:'tweet-native-repost-check-v1',check_job_id:'00000000-0000-4000-8000-000000000001',attempt_id:'00000000-0000-4000-8000-000000000002',
 submission_job_id:'00000000-0000-4000-8000-000000000003',target_site_uid:'1000000000000000',target_name:'Owned native account',
 external_id:'1266077623570661433',url:'https://t.bilibili.com/1266077623570661433',source_id:'2117189314744590552',
 source_url:'https://x.com/example-channel-b/status/2117189314744590552',caption:'Owned native translation https://x.com/example-channel-b/status/2117189314744590552',
 submission_recorded_at:Date.now()/1000-600};spec.not_before=spec.submission_recorded_at+300;spec.caption_sha256=sha(spec.caption);
let requestSHA;
if(requestFile){const raw=await fs.readFile(requestFile);spec=JSON.parse(raw);requestSHA=sha(raw);}
validateRepostSpec(spec);assert.throws(()=>validateRepostSpec({...spec,url:'https://owned.invalid/another'}));
const capturedModal=scenario==='captured_share_editor'?JSON.parse(await fs.readFile(new URL('./fixtures/tweet_share_modal_captured_20261103.json',import.meta.url),'utf8')):null;
function shareMarkup(){
 if(capturedModal){assert.equal(spec.caption,capturedModal.expectation.expected_caption);assert.equal(spec.source_url,capturedModal.expectation.source_url);assert.equal(spec.target_name,capturedModal.expectation.target_name);return capturedModal.outerHTML;}
 const text=scenario==='share_wrong_reference'?spec.caption.replace(spec.source_url,'https://x.com/example-channel-b/status/10001'):spec.caption;
 const esc=x=>x.replaceAll('&','&amp;').replaceAll('<','&lt;');
 return '<div class="'+(scenario==='share_nonmodal'?'owned-non-modal':'bili-dyn-share-publishing')+'">'+
  '<div class="bili-dyn-share-publishing__title"><span>分享到动态</span></div>'+
  (scenario==='share_without_editor'?'':'<div class="bili-dyn-share-publishing__input"><div class="bili-rich-textarea__inner empty" contenteditable="true">&#8203;</div></div>')+
  '<div class="bili-dyn-share-publishing__reference"><div class="bili-dyn-share-publishing__reference__author__name">@'+esc(spec.target_name)+'</div>'+
  '<div class="bili-rich-text__content">'+esc(text)+'</div></div>'+
  '<div class="bili-dyn-share-publishing__control"><div class="bili-dyn-share-publishing__action launcher" id="final-send">发布</div></div></div>';
}
let clicks=0;const browser=await chromium.launch({headless:true,chromiumSandbox:true});
try{
 const context=await browser.newContext({viewport:{width:1280,height:900}});const page=await context.newPage();
 await page.exposeFunction('ownedClick',()=>clicks++);
 await context.addInitScript(()=>{window.finalSendCount=0;document.addEventListener('click',e=>{window.ownedClick();if(e.target.id==='final-send')window.finalSendCount++;},true);});
 const esc=t=>t.replaceAll('&','&amp;').replaceAll('<','&lt;');
 const header=scenario==='auth'?'<button>登录</button>':`<a class="header-entry-mini" href="https://space.bilibili.com/${scenario==='wrong_uid'?'10001':spec.target_site_uid}">Avatar</a>`;
 const control=scenario==='not_found'?'':scenario==='final_only'?'<textarea></textarea><button id="final-send">转发</button>':`${scenario==='offscreen'?'<div style="height:1400px">Owned long image space</div>':''}<div class="bili-dyn-action ${scenario==='disabled'?'is-disabled':''}" role="button" ${scenario==='disabled'?'aria-disabled="true"':''}>转发</div>`;
 await context.route('**/*',route=>{
  const url=route.request().url();let card='';
  if(url.includes(spec.external_id))card=`<div class="bili-dyn-item"><a href="https://space.bilibili.com/${spec.target_site_uid}">${spec.target_name}</a><p>${esc(scenario==='wrong_caption'?'Wrong caption '+spec.source_url:spec.caption)}</p>${control}</div>`;
  const challenge=scenario==='challenge'?'<div role="dialog" class="geetest_panel">请完成人机验证</div>':'';
  const shareResponse=(scenario==='captured_share_editor'||scenario.startsWith('share_'))?'document.body.insertAdjacentHTML(\"beforeend\",'+JSON.stringify(shareMarkup())+')':null;
  const action=`<script>document.querySelector('.bili-dyn-action')?.addEventListener('click',()=>{${shareResponse?shareResponse:scenario==='denied'?`document.body.insertAdjacentHTML('beforeend','<div role="alert">动态正在审核中，暂时无法转发</div>')`:scenario==='after_challenge'?`document.body.insertAdjacentHTML('beforeend','<div role="dialog" class="geetest_panel">请完成人机验证</div>')`:`document.body.insertAdjacentHTML('beforeend','<div role="dialog"><h3>转发动态</h3><textarea placeholder="说点什么"></textarea><button id="final-send">转发</button></div>')`}});</script>`;
  return route.fulfill({status:200,contentType:'text/html; charset=utf-8',body:`<!doctype html><html><head><meta charset="utf-8"></head><body>${header}${card}${challenge}${action}</body></html>`});
 });
 if(scenario==='cached_probe'){await fs.mkdir(folder,{recursive:true});await fs.writeFile(path.join(folder,'dialog-open-intent.json'),'Owned retained earlier intent');}
 const ref=await observeRepostControl(page,spec,folder,{readinessMs:300,...(requestSHA?{requestSHA}:{})});const raw=await fs.readFile(ref.file);assert.equal(sha(raw),ref.sha256);
 const body=JSON.parse(raw);const expected={available:'dialog_opened',disabled:'disabled',not_found:'not_found',auth:'auth_required',wrong_uid:'wrong_account',challenge:'protection_blocked',wrong_caption:'ui_unrecognized',denied:'native_denied',offscreen:'dialog_opened',cached_probe:'probe_unconfirmed',after_challenge:'protection_blocked',final_only:'enabled_unverified',captured_share_editor:'dialog_opened',share_editor:'dialog_opened',share_wrong_reference:'probe_unconfirmed',share_nonmodal:'probe_unconfirmed',share_without_editor:'probe_unconfirmed'};
 const expectedClicks=['available','denied','offscreen','after_challenge','captured_share_editor','share_editor','share_wrong_reference','share_nonmodal','share_without_editor'].includes(scenario)?1:0;
 assert.equal(body.status,expected[scenario]);assert.deepEqual(body.actions,{clicks:expectedClicks,author_navigation_clicks:0,repost_dialog_clicks:expectedClicks,file_selections:0,fills:0,submits:0});assert.equal(clicks,expectedClicks);assert.equal(await page.evaluate(()=>window.finalSendCount),0);
 assert.equal(body.platform_cause_verified,false);assert.equal(body.human_review,false);assert.equal((await fs.readFile(body.screenshot.file)).subarray(0,8).toString('hex'),'89504e470d0a1a0a');
 if(['available','disabled','not_found','denied','offscreen','cached_probe'].includes(scenario))assert.ok(Object.values(body.content_binding).every(v=>v===true));
 if(scenario==='offscreen'){assert.equal(body.readonly_scroll_performed,true);assert.equal(body.controls[0].in_viewport,true);}
 if(scenario==='cached_probe')assert.equal(await fs.readFile(path.join(folder,'dialog-open-intent.json'),'utf8'),'Owned retained earlier intent');
 console.log(JSON.stringify({scenario,owned:true,clicks,final_send_clicks:0,status:body.status,result_reference:ref}));
}finally{await browser.close();}
