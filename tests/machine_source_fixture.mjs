// Browser fixtures owned by this project; no accounts, external calls or sends.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {inspectPlainTextSource} from '../adapters/browser_source_facts.mjs';
import {captureTweet} from '../adapters/browser_tweet_capture.mjs';
const out=process.argv[2];await fs.mkdir(out,{recursive:true});
const browser=await chromium.launch({headless:true});
const page=await browser.newPage({viewport:{width:1000,height:800}});
const pixel=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j6XcAAAAASUVORK5CYII=','base64');
let external=0;
await page.route('**/*',async route=>{if(route.request().url().startsWith('https://pbs.twimg.com/')||route.request().url().startsWith('https://abs.twimg.com/'))
 await route.fulfill({contentType:'image/png',body:pixel});else{external++;await route.abort();}});
const source='<article style="width:600px;min-height:220px;margin:80px 80px;background:white">'+
 '<div data-testid="User-Name"><span>Owned fixture</span><a href="https://x.com/fixture_only">@fixture_only</a></div>'+
 '<a href="https://x.com/fixture_only/status/123"><time datetime="2026-10-31T00:00:00Z">12:00</time></a>'+
 '<p data-testid="tweetText" style="white-space:pre-wrap">Owned synthetic text\n第二行 23:40 🥳</p></article>';
const passed=[];
async function facts(html){await page.setContent('<base href="https://x.com/">'+html);await page.evaluate(async()=>await Promise.all([...document.images].map(i=>i.decode().catch(()=>{}))));return page.locator('article').last().evaluate(inspectPlainTextSource);}
async function rejected(name,html,code){const r=await facts(html);assert.equal(r.supported,false,name);assert(r.failures.includes(code),JSON.stringify(r));passed.push(name);}
try{
 const good=await facts(source);assert.equal(good.supported,true);assert.equal(good.human_review,false);assert.equal(good.official_api,false);
 assert.equal(good.observed_text,'Owned synthetic text\n第二行 23:40 🥳');assert.deepEqual(good.status_ids,['123']);passed.push('native_emoji_linebreaks_and_full_text_kept');
 const withAvatar=await facts(source.replace('<div data-testid="User-Name">','<img width="24" height="24" src="https://pbs.twimg.com/profile_images/123/avatar.png"><div data-testid="User-Name">'));
 assert.equal(withAvatar.supported,true);passed.push('loaded_profile_avatar_is_not_media');
 await rejected('attached_photo_rejected',source.replace('</article>','<img width="80" height="60" src="https://pbs.twimg.com/media/fixture.png"></article>'),'MEDIA_OR_CARD_UNSUPPORTED');
 await rejected('body_profile_path_photo_cannot_impersonate_avatar',source.replace('</p>','<img width="80" height="60" src="https://pbs.twimg.com/profile_images/123/attachment.png"></p>'),'MEDIA_OR_CARD_UNSUPPORTED');
 await rejected('bitmap_emoji_not_silently_omitted_from_innertext',source.replace('</p>','<img alt="🥳" width="18" height="18" src="https://abs.twimg.com/emoji/fixture.png"></p>'),'BODY_IMAGE_TEXT_AMBIGUOUS');
 await rejected('video_rejected',source.replace('</article>','<video width="80" height="60"></video></article>'),'MEDIA_OR_CARD_UNSUPPORTED');
 await rejected('external_preview_card_rejected',source.replace('</article>','<a href="https://example.invalid/fixture">Owned preview card</a></article>'),'MEDIA_OR_CARD_UNSUPPORTED');
 await rejected('nested_quote_rejected',source.replace('</article>','<a href="https://x.com/parent/status/122">Parent quote</a></article>'),'QUOTE_OR_STATUS_CONTEXT_AMBIGUOUS');
 await rejected('preceding_context_blocks_single_post_mode',source.replaceAll('status/123','status/122')+source,'REPLY_CONTEXT_REQUIRED');
 await rejected('reply_label_blocks_single_post_mode',source.replace('<p data-testid="tweetText"','<p>回复 @parent</p><p data-testid="tweetText"'),'REPLY_CONTEXT_REQUIRED');
 await rejected('show_more_blocks_full_text_claim',source.replace('</article>','<button>Show more</button></article>'),'TEXT_TRUNCATED');
 await rejected('css_clamped_text_blocks_full_text_claim',source.replace('white-space:pre-wrap','white-space:pre-wrap;display:-webkit-box;-webkit-line-clamp:1;-webkit-box-orient:vertical;overflow:hidden'),'TEXT_TRUNCATED');
 await rejected('ellipsis_link_blocks_full_text_claim',source.replace('</p>',' <a href="https://example.invalid/full">example.invalid/…</a></p>'),'LINK_DISPLAY_TRUNCATED');
 await rejected('duplicate_body_blocks_source_claim',source.replace('</article>','<p data-testid="tweetText">Another text body</p></article>'),'BODY_AMBIGUOUS');
 await rejected('poll_without_legacy_testid_rejected',source.replace('</article>','<div role="radiogroup"><button role="radio">Option A</button><button role="radio">Option B</button><p>Poll results 60% / 40%</p></div></article>'),'MEDIA_OR_CARD_UNSUPPORTED');
 await rejected('internal_article_preview_rejected',source.replace('</article>','<a href="https://x.com/i/article/456">Long form article preview title</a></article>'),'MEDIA_OR_CARD_UNSUPPORTED');
 await rejected('non_anchor_role_link_quote_rejected',source.replace('</article>','<div role="link" tabindex="0"><span>Quoted author</span><p>Visible quoted original content</p></div></article>'),'MEDIA_OR_CARD_UNSUPPORTED');
 await rejected('css_background_media_rejected',source.replace('</article>','<div style="width:80px;height:60px;background-image:linear-gradient(red,blue)"></div></article>'),'MEDIA_OR_CARD_UNSUPPORTED');
 await rejected('non_ui_svg_media_rejected',source.replace('</article>','<svg width="80" height="60"><rect width="80" height="60" fill="blue"/></svg></article>'),'MEDIA_OR_CARD_UNSUPPORTED');
 await rejected('internal_space_card_rejected',source.replace('</article>','<a href="https://x.com/i/spaces/ownedfixture">Live Space title</a></article>'),'MEDIA_OR_CARD_UNSUPPORTED');
 const icons=await facts(source.replace('Owned fixture</span>','Owned fixture<svg width="20" height="20" aria-label="Verified"><path d="M0 0L10 10"/></svg></span>').replace('</article>',
  '<button aria-label="Like"><svg width="18" height="18"><path d="M0 0L10 10"/></svg></button></article>'));
 assert.equal(icons.supported,true,JSON.stringify(icons));passed.push('small_identity_badge_and_action_svg_remain_supported');
 const translateUi='<div class="flex items-center gap-1"><span class="text-secondary [&amp;&gt;svg]:size-4"><svg width="16" height="16" role="img" aria-hidden="true" data-icon="icon-logo-grok"><path d="m9.27 0 1 1"/></svg></span>显示翻译</div>';
 const replyUi='<a class="x-link x-engagement-button" aria-label="8 回复. 回复" href="/compose/post?in_reply_to=123"><span class="x-engagement-pill"><svg width="20" height="20" role="img" aria-hidden="true"><use href="#icon-reply"/></svg>8</span></a>';
 const observedUi=await facts(source.replace('</article>',translateUi+replyUi+'</article>'));
 assert.equal(observedUi.supported,true,JSON.stringify(observedUi));assert.equal(observedUi.unclassified_svg_count,0);
 passed.push('observed_xds_translation_logo_and_exact_reply_compose_icon_supported');
 const zeroUi=replyUi.replace('8 回复. 回复','0 回复. 回复').replace('</svg>8','</svg>');
 const zero=await facts(source.replace('</article>',zeroUi+'</article>'));
 assert.equal(zero.supported,true,JSON.stringify(zero));assert.equal(zero.ui_classification_version,'xds-source-ui-icons-v2');
 passed.push('native_zero_reply_count_can_be_visually_empty_with_exact_zero_accessible_count');
 await rejected('empty_nonzero_reply_count_is_not_allowed',source.replace('</article>',zeroUi.replace('0 回复. 回复','8 回复. 回复')+'</article>'),'MEDIA_OR_CARD_UNSUPPORTED');
 await rejected('empty_zero_reply_for_foreign_source_is_not_allowed',source.replace('</article>',zeroUi.replace('in_reply_to=123','in_reply_to=999')+'</article>'),'MEDIA_OR_CARD_UNSUPPORTED');
 await rejected('visible_reply_count_mismatch_is_not_allowed',source.replace('</article>',replyUi.replace('</svg>8','</svg>9')+'</article>'),'MEDIA_OR_CARD_UNSUPPORTED');
 await rejected('grok_artwork_without_exact_translation_label_rejected',source.replace('</article>',translateUi.replace('显示翻译','Grok drawing')+'</article>'),'MEDIA_OR_CARD_UNSUPPORTED');
 await rejected('translation_ui_with_additional_visible_text_rejected',source.replace('</article>',translateUi.replace('显示翻译','显示翻译 额外图形内容')+'</article>'),'MEDIA_OR_CARD_UNSUPPORTED');
 await rejected('reply_ui_for_another_source_rejected',source.replace('</article>',replyUi.replace('in_reply_to=123','in_reply_to=999')+'</article>'),'MEDIA_OR_CARD_UNSUPPORTED');
 await rejected('reply_ui_without_matching_accessible_label_rejected',source.replace('</article>',replyUi.replace('8 回复. 回复','Open artwork')+'</article>'),'MEDIA_OR_CARD_UNSUPPORTED');
 await rejected('grok_logo_in_body_not_treated_as_ui',source.replace('<p data-testid','<div data-testid').replace('</p>',translateUi+'</div>'),'MEDIA_OR_CARD_UNSUPPORTED');
 const dialog='<div role="dialog" style="position:fixed;inset:0;z-index:100;background:white">Owned ad<button onclick="this.parentElement.remove()">关闭</button></div>';
 await page.setContent(source+dialog);
 const captured=await captureTweet(page,{handle:'fixture_only',postId:'123'},path.join(out,'dismissed-ad'),{machineSource:true,trigger:'owned_fixture'});
 assert.equal(captured.payload.capture.protocol,'x-web-dom-observation-v2');assert.equal(captured.machine_source_facts.supported,true);
 assert.equal(captured.payload.capture.human_review,false);assert.equal(captured.payload.capture.source_screenshot_reviewed,false);
 assert.equal(captured.payload.capture.full_source_completeness_verified,false);assert.equal(captured.payload.references_observed,false);
 const canonical=x=>JSON.stringify(x,(_,v)=>v&&typeof v==='object'&&!Array.isArray(v)?Object.fromEntries(Object.keys(v).sort().map(k=>[k,v[k]])):v);
 assert.equal(captured.payload.capture.machine_facts_sha256,crypto.createHash('sha256').update(canonical(captured.machine_source_facts)).digest('hex'));
 passed.push('ordinary_popup_removed_before_hash_bound_capture_without_human_claim');
 await page.setContent(source+'<div style="position:fixed;inset:0;z-index:100;background:white">No close action</div>');
 await assert.rejects(()=>captureTweet(page,{handle:'fixture_only',postId:'123'},path.join(out,'blocked-ad'),{machineSource:true}),{code:'WEB_SOURCE_OBSCURED'});
 passed.push('unknown_popup_blocks_machine_capture');
 await page.setContent(source+dialog.replace('Owned ad','请完成人机验证'));
 await assert.rejects(()=>captureTweet(page,{handle:'fixture_only',postId:'123'},path.join(out,'challenge'),{machineSource:true}),{code:'WEB_HUMAN_VERIFICATION_REQUIRED'});
 passed.push('machine_mode_never_bypasses_human_verification');
 console.log(JSON.stringify({passed,human_review:false,external_calls:external}));
}finally{await browser.close();}
