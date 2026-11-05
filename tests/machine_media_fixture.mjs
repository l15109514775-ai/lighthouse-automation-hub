// Owned static-media fixtures. No external downloads, model or publishing calls.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {validateInput} from '../compositor/src/core.mjs';
import {inspectMachineMediaArticle,captureMachineMedia,verifyMachineMediaBytes,downloadMachineOriginal,verifyMachineMediaBundle,machineMediaCompositorInput} from '../adapters/machine_media_capture.mjs';
const root=path.resolve(process.argv[2]);await fs.mkdir(root,{recursive:true});
const sharp=createRequire(new URL('../compositor/package.json',import.meta.url))('sharp');
const hash=b=>crypto.createHash('sha256').update(b).digest('hex');
const preview=await sharp({create:{width:80,height:60,channels:3,background:'#ccddee'}}).png().toBuffer();
const original=await sharp({create:{width:160,height:120,channels:3,background:'#ccddee'}}).png().toBuffer();
const other=await sharp({create:{width:160,height:120,channels:3,background:'#eeddcc'}}).png().toBuffer();
const identity={handle:'fixture_only',postId:'123'};
const start='<article style="margin:50px 70px;padding:10px;width:600px;background:white"><div data-testid="User-Name"><span>Owned static-media fixture</span><a href="https://x.com/fixture_only">@fixture_only</a></div><a href="https://x.com/fixture_only/status/123"><time datetime="2026-10-31T00:00:00Z">12:00</time></a><div data-testid="tweetText">Owned static media, 23:40 🥳</div>';
const photo=(index,key,extra='')=>`<figure style="margin:8px 0"><a href="https://x.com/fixture_only/status/123/photo/${index}"><img style="display:block;width:80px;height:60px;object-fit:contain;${extra}" src="https://pbs.twimg.com/media/${key}?format=png&amp;name=small" alt="Owned fixture photo ${index}"></a><a href="https://pbs.twimg.com/media/${key}?format=png&amp;name=orig">View original</a></figure>`;
const overlayPhoto=(index,key)=>`<figure style="margin:8px 0"><div class="relative" style="position:relative;width:80px;height:60px"><div inert><img style="display:block;width:80px;height:60px;object-fit:contain" src="https://pbs.twimg.com/media/${key}?format=png&amp;name=small" alt="Owned fixture photo ${index}"></div><a aria-label="查看媒体" class="absolute inset-0 z-10" style="position:absolute;inset:0;z-index:10" href="/fixture_only/status/123/photo/${index}"></a></div><a href="https://pbs.twimg.com/media/${key}?format=png&amp;name=orig">View original</a></figure>`;
const full=start+photo(1,'OwnedOne')+photo(2,'OwnedTwo')+'</article>';
const browser=await chromium.launch({headless:true});const page=await browser.newPage({viewport:{width:1000,height:900}});
let external=0;
await page.route('**/*',route=>{if(route.request().url().startsWith('https://pbs.twimg.com/media/'))return route.fulfill({contentType:'image/png',body:preview});external++;return route.abort();});
const passed=[];
async function inventory(html){await page.setContent('<base href="https://x.com/">'+html);await page.evaluate(async()=>await Promise.all([...document.images].map(i=>i.decode().catch(()=>{}))));return page.locator('article').evaluate(inspectMachineMediaArticle,identity);}
async function blocked(name,html,code){const facts=await inventory(html);assert.equal(facts.supported,false,name);assert(facts.failures.includes(code),JSON.stringify(facts));passed.push(name);}
try{
 const facts=await inventory(full);assert.equal(facts.supported,true,JSON.stringify(facts));assert.deepEqual(facts.photos.map(x=>x.index),[1,2]);
 assert(facts.photos.every(x=>x.original_url_rewritten===false&&x.preview_cropped===false));passed.push('visible_original_links_and_contiguous_dom_order_bound');
 const requested=[];const downloader=async url=>{requested.push(url);return {bytes:url.includes('OwnedOne')?original:other,content_type:'image/png',status:200,transport_mode:'custom_transport'};};
 const bundle=await captureMachineMedia(page,identity,path.join(root,'valid-capture'),{allowedRoot:root,downloader});
 assert.equal(bundle.transport_mode,'custom_transport');assert.equal(bundle.live_transport_verified,false);assert.equal(bundle.production_queue_connected,false);
 assert.equal(bundle.automatic_publication_authorized_by_this_bundle,false);assert.equal(bundle.human_review,false);assert.equal(bundle.image_text_translation_performed,false);
 assert.equal(bundle.files.length,2);assert.equal(requested.length,2);
 for(const [i,item]of bundle.files.entries()){
  const bytes=await fs.readFile(item.file);assert.equal(hash(bytes),item.sha256);assert.equal(bytes.equals(i===0?original:other),true);
  assert.equal(item.decoded_complete,true);assert.equal(item.animated,false);assert.equal(item.width,160);assert.equal(item.height,120);
  assert.equal(hash(await fs.readFile(item.screenshot_file)),item.screenshot_sha256);assert.equal(item.human_review,false);
 }
 assert.equal(hash(await fs.readFile(bundle.source.screenshot_file)),bundle.source_screenshot_sha256);
 passed.push('complete_static_original_bytes_and_each_source_screenshot_hash_saved');
 passed.push('custom_test_transport_never_claims_live_access_human_review_or_image_translation');
 const integrity=await verifyMachineMediaBundle(bundle,{requireLive:false});assert.equal(integrity.integrity_verified,true);assert.equal(integrity.publication_authorized,false);
 await assert.rejects(()=>verifyMachineMediaBundle(bundle),{code:'MACHINE_MEDIA_BUNDLE_INTEGRITY_INVALID'});passed.push('default_gate_rejects_custom_transport_bundle_for_live_use');
 const baseInput={schemaVersion:1,displayTimezone:'Asia/Shanghai',post:{id:'123',url:bundle.source.payload.url,originalText:bundle.source.payload.post.text,
  translation:'自有图片测试，23:40 🥳',publishedAt:'2026-10-31T09:00:00+09:00',sourceTimezone:'Asia/Tokyo',sourceMediaCount:null,sourceQuote:'unknown',
  author:{name:'Owned fixture',handle:'fixture_only',avatar:bundle.source.screenshot_file,verified:false},review:{sourceConfirmed:false,translationConfirmed:false},
  sourceScreenshot:{file:bundle.source.screenshot_file,capturedAt:bundle.source.payload.capture.captured_at,reviewed:false},relationship:{type:'original'},quote:null,media:[]}};
 const renderInput=await machineMediaCompositorInput(baseInput,bundle,{requireLive:false});
 assert.equal(renderInput.post.sourceMediaCount,2);assert.equal(renderInput.post.sourceScreenshot.reviewed,false);
 assert(renderInput.post.media.every(x=>x.completeness==='complete'&&x.reviewed===false));assert.deepEqual(baseInput.post.media,[]);
 const schemaCheck=validateInput(renderInput);assert.equal(schemaCheck.schemaValid,true);
 assert(!schemaCheck.issues.some(x=>x.severity==='error'&&!['SOURCE_UNREVIEWED','TRANSLATION_UNREVIEWED','SCREENSHOT_UNREVIEWED','MEDIA_UNREVIEWED'].includes(x.code)),JSON.stringify(schemaCheck.issues));
 assert(!/\.\d+Z$/.test(renderInput.post.sourceScreenshot.capturedAt));assert.equal(bundle.source.payload.capture.captured_at.includes('.'),true);
 passed.push('derived_compositor_input_retains_real_byte_order_and_all_human_flags_false');
 await blocked('thumbnail_alone_is_not_original',full.replace(/<a href="https:\/\/pbs\.twimg\.com[^>]+>View original<\/a>/g,''),'VISIBLE_ORIGINAL_SOURCE_REQUIRED');
 await blocked('wrong_original_identifier_not_bound_to_thumbnail',full.replace('media/OwnedOne?format=png&amp;name=orig','media/OtherKey?format=png&amp;name=orig'),'VISIBLE_ORIGINAL_SOURCE_REQUIRED');
 await blocked('cropped_preview_rejected',start+photo(1,'OwnedOne','height:80px;object-fit:cover;')+'</article>','MEDIA_PREVIEW_CROPPED_OR_DISTORTED');
 await blocked('wrong_dom_order_rejected',start+photo(2,'OwnedTwo')+photo(1,'OwnedOne')+'</article>','MEDIA_ORDER_OR_IDENTITY_AMBIGUOUS');
 await blocked('foreign_post_media_rejected',full.replace('status/123/photo/1','status/999/photo/1'),'MEDIA_POST_IDENTITY_MISMATCH');
 await blocked('video_not_silently_sampled',full.replace('</article>','<video width="80" height="60"></video></article>'),'NON_STATIC_OR_CONTEXT_MEDIA_UNSUPPORTED');
 await blocked('unbound_visible_image_rejected',full.replace('</article>','<img width="40" height="30" src="https://pbs.twimg.com/media/Unbound?format=png&amp;name=orig"></article>'),'UNBOUND_VISIBLE_MEDIA');
 await blocked('duplicate_original_sources_ambiguous',full.replace('</article>','<a href="https://pbs.twimg.com/media/OwnedOne?format=png&amp;name=orig">View original</a></article>'),'VISIBLE_ORIGINAL_SOURCE_REQUIRED');
 await inventory(start+photo(1,'OwnedOne').replace('name=small','name=orig')+'</article>');
 assert.equal((await page.locator('article').evaluate(inspectMachineMediaArticle,identity)).photos[0].original_basis,'visible_image_original_url');passed.push('already_visible_original_src_needs_no_rewritten_url');
 const small=await sharp({create:{width:40,height:30,channels:3,background:'#112233'}}).png().toBuffer();
 const item={format:'png',preview_natural_width:80,preview_natural_height:60};
 await assert.rejects(()=>verifyMachineMediaBytes(small,item),{code:'MEDIA_ORIGINAL_DIMENSIONS_UNVERIFIED'});passed.push('smaller_than_preview_is_not_complete_original');
 await assert.rejects(()=>verifyMachineMediaBytes(original.subarray(0,original.length-20),item),{code:'MEDIA_ORIGINAL_DECODE_FAILED'});passed.push('truncated_static_file_fails_complete_pixel_decode');
 const singleGif=Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7','base64');
 const frame=singleGif.indexOf(Buffer.from([0x21,0xf9]));const gif=Buffer.concat([singleGif.subarray(0,frame),singleGif.subarray(frame,-1),singleGif.subarray(frame,-1),Buffer.from([0x3b])]);
 assert.equal((await sharp(gif,{animated:true}).metadata()).pages,2);
 await assert.rejects(()=>verifyMachineMediaBytes(gif,{format:'gif',preview_natural_width:1,preview_natural_height:1}),{code:'MEDIA_ANIMATED_OR_UNSUPPORTED'});passed.push('real_two_frame_gif_never_reduced_to_first_frame');
 await assert.rejects(()=>verifyMachineMediaBytes(original,item,{maxBytes:10}),{code:'MEDIA_ORIGINAL_SIZE_INVALID'});passed.push('oversized_bytes_stop_before_decode');
 await assert.rejects(()=>downloadMachineOriginal('https://127.0.0.1/private?name=orig'),{code:'MEDIA_ORIGINAL_URL_INVALID'});passed.push('non_platform_original_url_rejected_before_network');
 await inventory(full);
 await assert.rejects(()=>captureMachineMedia(page,identity,path.join(root,'bad-response'),{allowedRoot:root,downloader:async()=>({bytes:original,content_type:'text/html',status:200})}),{code:'MEDIA_ORIGINAL_RESPONSE_INVALID'});passed.push('html_response_cannot_be_saved_as_original_photo');
 await assert.rejects(()=>captureMachineMedia(page,identity,path.join(root,'valid-capture'),{allowedRoot:root,downloader}),{code:'EEXIST'});passed.push('existing_bundle_is_immutable_no_overwrite');
 await inventory(full.replace('</article>','<a href="https://x.com/i/article/999">Unrelated internal preview</a></article>'));
 await assert.rejects(()=>captureMachineMedia(page,identity,path.join(root,'card-context'),{allowedRoot:root,downloader}),{code:'MEDIA_BODY_OR_CONTEXT_UNSUPPORTED'});passed.push('static_photo_bundle_does_not_hide_extra_internal_card');
 const linked=full.replace('Owned static media, 23:40 🥳','Owned static media, 23:40 🥳 <a href="https://t.co/16UDjexcs1">youtube.com/live/9T5tn1WGl…</a>');
 await inventory(linked);
 await assert.rejects(()=>captureMachineMedia(page,identity,path.join(root,'link-default-off'),{allowedRoot:root,downloader}),{code:'MEDIA_BODY_OR_CONTEXT_UNSUPPORTED'});
 passed.push('link_normalization_default_off_preserves_original_strict_failure');
 const normalized=await captureMachineMedia(page,identity,path.join(root,'link-normalized'),{allowedRoot:root,downloader,normalizeLinks:true});
 assert(normalized.source.payload.post.text.includes('youtube.com/live/9T5tn1WGl…'));
 assert(normalized.link_normalization.derived_payload.post.text.includes('https://t.co/16UDjexcs1'));
 assert(normalized.text_facts.failures.includes('LINK_DISPLAY_TRUNCATED'));assert.equal(normalized.source.payload.capture.human_review,false);
 const normalizedBase=structuredClone(baseInput);normalizedBase.post.originalText=normalized.link_normalization.derived_payload.post.text;
 const normalizedInput=await machineMediaCompositorInput(normalizedBase,normalized,{requireLive:false,useNormalizedText:true});
 assert.equal(normalizedInput.post.originalText,normalized.link_normalization.derived_payload.post.text);assert.equal(normalizedInput.post.sourceScreenshot.reviewed,false);
 passed.push('opt_in_derived_payload_and_render_binding_keep_raw_source_and_failure_evidence');
 const changedPacket=structuredClone(normalized);changedPacket.link_normalization.evidence.normalized_text+=' invented';
 await assert.rejects(()=>verifyMachineMediaBundle(changedPacket,{requireLive:false}),{code:'MACHINE_MEDIA_BUNDLE_INTEGRITY_INVALID'});
 passed.push('changed_link_normalization_packet_fails_media_integrity');
 const overlayHtml=start+overlayPhoto(1,'OwnedOne')+photo(2,'OwnedTwo')+'</article>';
 const overlayFacts=await inventory(overlayHtml);assert.equal(overlayFacts.supported,true,JSON.stringify(overlayFacts));
 assert.equal(overlayFacts.photos[0].display_layout,'verified_sibling_photo_overlay');
 const overlayBundle=await captureMachineMedia(page,identity,path.join(root,'native-overlay'),{allowedRoot:root,downloader});
 assert.equal(overlayBundle.files[0].visibility.native_sibling_overlay_verified,true);
 assert.equal(overlayBundle.files[0].visibility.all_image_hit_points_verified,true);
 assert.equal((await verifyMachineMediaBundle(overlayBundle,{requireLive:false})).integrity_verified,true);
 passed.push('native_inert_image_and_same_tile_photo_overlay_bound_without_force_click');
 await blocked('wrong_overlay_post_id_rejected',overlayHtml.replace('/status/123/photo/1','/status/999/photo/1'),'MEDIA_POST_IDENTITY_MISMATCH');
 await blocked('overlay_bbox_mismatch_rejected',overlayHtml.replace('position:absolute;inset:0;z-index:10','position:absolute;inset:0;z-index:10;width:40px'),'MEDIA_OVERLAY_GEOMETRY_UNVERIFIED');
 await blocked('overlay_without_media_accessible_name_rejected',overlayHtml.replace('aria-label="查看媒体"','aria-label="Advertisement"'),'MEDIA_OVERLAY_GEOMETRY_UNVERIFIED');
 await blocked('multiple_visible_images_in_overlay_tile_rejected',overlayHtml.replace('</div><a aria-label="查看媒体"','<img style="width:80px;height:60px" src="https://pbs.twimg.com/media/SecondInsideTile?format=png&amp;name=small"></div><a aria-label="查看媒体"'),'MEDIA_ITEM_AMBIGUOUS');
 await inventory(overlayHtml.replace('</div><a href="https://pbs.twimg.com/media/OwnedOne?',
  '<span style="position:absolute;inset:0;z-index:20;background:white">Owned blocking overlay</span></div><a href="https://pbs.twimg.com/media/OwnedOne?'));
 await assert.rejects(()=>captureMachineMedia(page,identity,path.join(root,'obscured-native-overlay'),{allowedRoot:root,downloader}),{code:'MEDIA_SOURCE_OBSCURED'});
 passed.push('another_overlay_covering_native_media_link_still_blocks_capture');
 await fs.writeFile(bundle.files[0].file,Buffer.from('owned tamper fixture'));
 await assert.rejects(()=>verifyMachineMediaBundle(bundle,{requireLive:false}),{code:'MACHINE_MEDIA_BUNDLE_INTEGRITY_INVALID'});passed.push('changed_original_file_fails_bundle_integrity');
 assert.equal(external,0);console.log(JSON.stringify({passed,external_calls:external,publish_requests:0,model_calls:0,human_review:false,
  image_text_translation_performed:false,manifest:path.join(root,'valid-capture/machine-media-manifest.json')}));
}finally{await browser.close();}
