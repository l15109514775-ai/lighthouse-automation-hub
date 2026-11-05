// Owned HTML with all network intercepted. No account, upload, click or solver.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {inspectVideoProtection,VIDEO_PROTECTION_GUARD_VERSION} from '../adapters/bilibili_video_guard.mjs';

const out=path.resolve(process.argv[2]);await fs.mkdir(out,{recursive:true});
let networkCalls=0,routedRequests=0;const passed=[];
const browser=await chromium.launch({headless:true});const context=await browser.newContext();
await context.route('**/*',async route=>{
 routedRequests++;
 // Route supplies every document, including test iframe URLs, from owned bytes.
 // No route.continue or external transport exists in this fixture.
 await route.fulfill({contentType:'text/html',body:'<!doctype html><title>Owned frame</title><body>Owned fixture</body>'});
});
const page=await context.newPage();
async function load({classes='risk-captcha-adapt-pc risk-captcha-adapt',input=true,extra='',origin='https://upload.owned.test/page'}={}){
 await page.goto(origin,{waitUntil:'domcontentloaded'});
 await page.setContent(`<!doctype html><style>body{margin:8px;font:16px Arial}input{display:block;width:200px;height:35px}iframe{width:100px;height:50px}.box{width:200px;height:100px}dialog[open]{width:250px;height:100px}</style>
  <body class="${classes}">${input?'<input name="buploader" type="file" accept=".mp4,.flv,.mkv" multiple>':''}
  <input name="buploader" type="file" accept=".txt"><input type="file" accept=".mp4" hidden>
  <footer><a href="https://www.bilibili.com/blackboard/protocal/">投稿规范</a><a href="https://passport.bilibili.com/login">登录</a><span>验证码 captcha security 用户协议</span></footer>
  ${extra}<script>window.ownedCounts={clicked:0,input:0,changed:0};document.addEventListener('click',()=>ownedCounts.clicked++);document.addEventListener('input',()=>ownedCounts.input++);document.addEventListener('change',()=>ownedCounts.changed++);</script></body>`);
}
async function inspect(label,expectedCode=null){
 const before=await page.content();const value=await inspectVideoProtection(page);const after=await page.content();
 assert.equal(before,after,'inspection must not mutate or hide DOM');
 assert.equal(value.evidence.version,VIDEO_PROTECTION_GUARD_VERSION);
 assert.equal(value.blocked,expectedCode!==null,label);assert.equal(value.error_code,expectedCode,label);
 assert.deepEqual(await page.evaluate(()=>window.ownedCounts),{clicked:0,input:0,changed:0});
 assert.equal(JSON.stringify(value).includes('owned-private-query-value'),false);
 passed.push(label);return value;
}
try{
 await load();let value=await inspect('known_body_container_and_visible_native_multiple_mp4_are_not_challenge');
 assert.equal(value.evidence.known_document_container_excluded,true);assert.equal(value.evidence.native_mp4_uploaders,1);
 await load({classes:'risk-captcha-adapt risk-captcha-adapt-pc'});await inspect('known_body_class_order_independent');
 await load({classes:''});await inspect('normal_document_footer_words_and_links_do_not_block');
 await load({extra:'<iframe src="https://message.bilibili.com/pages/nav/index_new_pc_sync"></iframe><iframe src="https://s1.hdslb.com/bfs/short/cols/iframe.html"></iframe>'});
 await inspect('observed_message_and_columns_frames_are_not_challenges');
 await load({input:false});await inspect('txt_and_unnamed_hidden_mp4_do_not_supply_known_native_uploader','BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED');
 await load({classes:'risk-captcha-adapt-pc risk-captcha-adapt unexpected-class'});await inspect('unobserved_body_wrapper_is_not_broadly_ignored','BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED');
 await load({extra:'<div class="risk-captcha-adapt risk-captcha-adapt-pc box">Owned real challenge</div>'});
 await inspect('same_protection_classes_on_child_div_still_block','BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED');
 for(const role of ['dialog','alertdialog']){
  await load({extra:`<div role="${role}" class="box">Owned ${role}</div>`});
  await inspect('visible_'+role+'_stops_without_interpreting_text','BILIBILI_VIDEO_VISIBLE_DIALOG_REQUIRES_REVIEW');
 }
 await load({extra:'<dialog open>Owned native dialog</dialog>'});await inspect('native_open_dialog_stops','BILIBILI_VIDEO_VISIBLE_DIALOG_REQUIRES_REVIEW');
 await load({extra:'<div aria-modal="true" class="box">Owned modal</div>'});await inspect('visible_aria_modal_stops','BILIBILI_VIDEO_VISIBLE_DIALOG_REQUIRES_REVIEW');
 for(const selector of ['class="geetest_panel"','id="captcha-box"','data-testid="captcha-control"']){
  await load({extra:`<div ${selector} style="width:150px;height:80px">Owned verification</div>`});
  await inspect('visible_verification_control_'+passed.length,'BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED');
 }
 await load({extra:'<div role="dialog" hidden>Owned hidden dialog</div><div class="geetest_panel box" style="display:none">Hidden fixture</div><div id="captcha-box" class="box" style="visibility:hidden">Hidden fixture</div>'});
 await inspect('unrendered_dialog_and_verification_placeholders_do_not_block');
 await load({extra:'<div style="display:none"><div role="alertdialog" class="box">Hidden by ancestor</div></div>'});await inspect('hidden_ancestor_is_not_a_visible_dialog');
 for(const src of ['https://passport.bilibili.com/login?next=owned-private-query-value','https://gcaptcha4.geetest.com/load?token=owned-private-query-value','https://verify.owned.test/captcha/frame?token=owned-private-query-value']){
  await load({extra:`<iframe src="${src}"></iframe>`});
  await inspect('visible_challenge_iframe_'+passed.length,src.includes('passport')?'BILIBILI_VIDEO_AUTHENTICATION_REQUIRED':'BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED');
 }
 await load({extra:'<iframe src="https://passport.bilibili.com/login" style="display:none"></iframe>'});await inspect('unrendered_login_frame_is_not_an_active_visible_prompt');
 await load({origin:'https://passport.bilibili.com/login?token=owned-private-query-value'});await inspect('top_level_login_page_stops_even_without_a_dialog','BILIBILI_VIDEO_AUTHENTICATION_REQUIRED');
 await load();await inspect('before_dynamic_challenge_is_clear');
 await page.evaluate(()=>{const node=document.createElement('div');node.className='geetest_panel';node.style.cssText='width:100px;height:70px';node.textContent='Owned dynamic challenge';document.body.append(node);});
 await inspect('next_inspection_stops_for_dynamically_appearing_verification','BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED');
 await load({extra:'<div id="ordinary" class="box">Owned plain element</div>'});
 await page.evaluate(()=>Object.defineProperty(window,'localStorage',{get(){throw new Error('storage-must-not-be-read');}}));
 await page.evaluate(()=>Object.defineProperty(document,'cookie',{get(){throw new Error('cookies-must-not-be-read');}}));
 await inspect('inspection_never_reads_storage_or_cookies');
 await load({extra:Array.from({length:80},(_,i)=>`<div class="captcha-fixture box">Owned bounded ${i}</div>`).join('')});
 value=await inspect('many_protection_controls_have_bounded_evidence','BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED');
 assert.equal(value.evidence.visible_challenge_controls.length,40);assert.equal(value.evidence.truncated,true);
 const manager='<nav>稿件管理 视频管理 草稿 1</nav><a href="https://member.bilibili.com/platform/upload/video/frame?draft=owned-private-query-value">Owned saved draft</a>';
 await load({input:false,origin:'https://member.bilibili.com/platform/upload-manager/article',extra:manager});
 value=await inspect('official_saved_draft_manager_document_is_not_itself_a_challenge');assert.equal(value.evidence.native_draft_manager_excluded,true);
 for(const extra of ['<div class="captcha-box box">Owned verification</div>','<div role="dialog" class="box">Owned modal</div>','<iframe src="https://passport.bilibili.com/login?token=owned-private-query-value"></iframe>']){
  await load({input:false,origin:'https://member.bilibili.com/platform/upload-manager/article',extra:manager+extra});
  await inspect('manager_still_blocks_real_protection_'+passed.length,extra.includes('iframe')?'BILIBILI_VIDEO_AUTHENTICATION_REQUIRED':extra.includes('role=')?'BILIBILI_VIDEO_VISIBLE_DIALOG_REQUIRES_REVIEW':'BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED');
 }
 for(const origin of ['https://manager.owned.test/platform/upload-manager/article','https://member.bilibili.com/platform/not-the-manager']){
  await load({input:false,origin,extra:manager});await inspect('manager_wrapper_exception_requires_exact_official_origin_and_path_'+passed.length,'BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED');
 }
 await load({input:false,origin:'https://member.bilibili.com/platform/upload-manager/article',extra:'<nav>稿件管理 视频管理 草稿 1</nav>'});await inspect('manager_exception_requires_visible_native_edit_link','BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED');
 const emptyManager='<h1>稿件管理</h1><nav><a href="https://member.bilibili.com/platform/upload-manager/article">视频管理</a>'+
  '<a href="https://member.bilibili.com/platform/upload-manager/opus">图文管理</a>'+
  '<a href="https://member.bilibili.com/platform/upload-manager/inactArticle">互动视频管理</a></nav><button>草稿 1</button>';
 await load({input:false,origin:'https://member.bilibili.com/platform/upload-manager/article',extra:emptyManager});
 value=await inspect('empty_official_manager_with_three_visible_canonical_links_is_document_only');
 assert.equal(value.evidence.known_document_container_excluded,true);assert.equal(value.evidence.native_draft_manager_excluded,true);
 await load({input:false,origin:'https://member.bilibili.com/platform/upload-manager/article',extra:emptyManager+'<div class="captcha-box box">Owned child challenge remains blocking</div>'});
 value=await inspect('empty_official_manager_still_blocks_child_verification','BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED');
 assert.equal(value.evidence.native_draft_manager_excluded,true);assert.ok(value.evidence.visible_challenge_controls.some(x=>x.tag==='DIV'));
 const fail=await inspectVideoProtection({isClosed:()=>false,evaluate:async()=>{throw new Error('owned-private-query-value');}});
 assert.equal(fail.blocked,true);assert.equal(fail.error_code,'BILIBILI_VIDEO_PROTECTION_INSPECTION_FAILED');assert.equal(JSON.stringify(fail).includes('owned-private-query-value'),false);passed.push('inspection_exception_fails_closed_without_private_error_string');
 await page.close();const closed=await inspectVideoProtection(page);assert.equal(closed.blocked,true);assert.equal(closed.error_code,'BILIBILI_VIDEO_PAGE_UNAVAILABLE');passed.push('closed_page_fails_closed');
 const receipt={version:'bilibili-video-guard-owned-fixture-v1',passed,external_network_calls:networkCalls,routed_requests:routedRequests,
  real_uploads:0,clicks:0,storage_reads:0,cookie_reads:0,live_tested:false,human_review:false,publication_authorized:false};
 await fs.writeFile(path.join(out,'result.json'),JSON.stringify(receipt,null,2));process.stdout.write(JSON.stringify(receipt));
}finally{await context.close();await browser.close();}
