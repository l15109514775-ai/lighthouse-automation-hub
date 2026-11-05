// Owned public-DOM media fixtures; no X account, network, model or publication.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {captureMachineMedia,inspectMachineMediaArticle,verifyMachineMediaBundle,machineMediaCompositorInput} from '../adapters/machine_media_capture.mjs';
import {capturePublicMachineMedia,verifyPublicMediaBytes,downloadPublicDOMMedia} from '../adapters/machine_media_public.mjs';
const root=path.resolve(process.argv[2]);await fs.mkdir(root,{recursive:true});
const sharp=createRequire(new URL('../compositor/package.json',import.meta.url))('sharp');
const small=await sharp({create:{width:80,height:60,channels:3,background:'#aabbcc'}}).webp().toBuffer();
const full=await sharp({create:{width:160,height:120,channels:3,background:'#aabbcc'}}).webp().toBuffer();
const big=await sharp({create:{width:320,height:240,channels:3,background:'#aabbcc'}}).webp().toBuffer();
const identity={handle:'fixture_only',postId:'123'},spec={...identity,source_mode:'public_dom_full_dimensions'},passed=[];
const browser=await chromium.launch({headless:true}),page=await browser.newPage({viewport:{width:1000,height:800}});let external=0;
await page.route('**/*',route=>{if(route.request().url().startsWith('https://pbs.twimg.com/media/'))return route.fulfill({contentType:'image/webp',body:small});external++;return route.abort();});
const header='<article style="width:650px;margin:40px;background:white"><div data-testid="User-Name"><span>Owned public dimensions</span><a href="https://x.com/fixture_only">@fixture_only</a></div><a href="https://x.com/fixture_only/status/123"><time datetime="2026-10-31T00:00:00Z">12:00</time></a><div data-testid="tweetText">自有公开页面图片测试 <a href="https://t.co/OwnedFixture">youtube.com/live/owned…</a></div>';
const image='<img width="160" height="120" style="display:block;width:80px;height:80px;object-fit:cover" src="https://pbs.twimg.com/media/OwnedMedia?format=webp&amp;name=large" srcset="https://pbs.twimg.com/media/OwnedMedia?format=webp&amp;name=small 80w, https://pbs.twimg.com/media/OwnedMedia?format=webp&amp;name=large 320w" sizes="80px">';
const html=header+'<div class="relative" style="position:relative;width:80px;height:80px"><div inert>'+image+'</div><a class="absolute inset-0 z-10" aria-label="查看媒体" style="position:absolute;inset:0;z-index:10" href="/fixture_only/status/123/photo/1"></a></div></article>';
async function load(markup=html){await page.setContent('<base href="https://x.com/">'+markup);await page.evaluate(async()=>await Promise.all([...document.images].map(i=>i.decode().catch(()=>{}))));return page.locator('article').last().evaluate(inspectMachineMediaArticle,spec);}
async function blocked(name,markup,code){const facts=await load(markup);assert.equal(facts.supported,false,name);assert(facts.failures.includes(code),JSON.stringify(facts));passed.push(name);}
try{
 const inv=await load();assert.equal(inv.supported,true,JSON.stringify(inv));assert.equal(inv.version,'machine-public-full-dimensions-dom-v1');
 assert.equal(inv.photos[0].download_url,'https://pbs.twimg.com/media/OwnedMedia?format=webp&name=large');
 assert.equal(inv.photos[0].thumbnail_cropped_by_layout,true);assert.equal(inv.photos[0].creator_original_upload_identity_verified,false);
 assert.equal(inv.photos[0].resource_url_rewritten,false);assert.equal(inv.photos[0].declared_width,160);passed.push('declared_dimensions_and_exact_public_srcset_url_selected_without_orig_rewrite');
 const requested=[];const downloader=async url=>{requested.push(url);return {bytes:full,status:200,content_type:'image/webp'};};
 const bundle=await captureMachineMedia(page,identity,path.join(root,'public-valid'),{allowedRoot:root,sourceMode:'public_dom_full_dimensions',normalizeLinks:true,downloader});
 assert.equal(bundle.version,'machine-public-full-dimensions-bundle-v1');assert.equal(bundle.creator_original_upload_identity_verified,false);
 assert.equal(bundle.public_dimensions_verified,true);assert.equal(bundle.files[0].width,160);assert.equal(bundle.files[0].height,120);
 assert.equal(bundle.files[0].thumbnail_cropped_by_layout,true);assert.equal(bundle.files[0].decoded_complete,true);assert.equal(bundle.live_transport_verified,false);
 assert.equal(requested[0],inv.photos[0].download_url);assert(!requested[0].includes('name=orig'));
 assert.equal((await verifyMachineMediaBundle(bundle,{requireLive:false})).integrity_verified,true);
 assert(bundle.source.payload.post.text.includes('youtube.com/live/owned…'));assert(bundle.link_normalization.derived_payload.post.text.includes('https://t.co/OwnedFixture'));
 passed.push('layout_cropped_thumbnail_allowed_only_with_exact_full_public_pixel_match');
 passed.push('raw_body_and_link_normalization_proof_preserved_without_original_upload_claim');
 await assert.rejects(()=>verifyMachineMediaBundle(bundle),{code:'PUBLIC_MEDIA_BUNDLE_INTEGRITY_INVALID'});passed.push('test_transport_cannot_masquerade_as_live');
 const base={post:{id:'123',url:bundle.source.payload.url,originalText:bundle.link_normalization.derived_payload.post.text,author:{handle:'fixture_only'},
  review:{sourceConfirmed:false,translationConfirmed:false},sourceScreenshot:{reviewed:false},relationship:{type:'original'},quote:null}};
 const input=await machineMediaCompositorInput(base,bundle,{requireLive:false,useNormalizedText:true});
 assert.equal(input.post.media[0].completeness,'complete');assert.equal(input.post.media[0].reviewed,false);assert.equal(input.post.sourceScreenshot.reviewed,false);
 passed.push('existing_v2_renderer_media_factory_uses_verified_file_and_keeps_human_flags_false');
 await assert.rejects(()=>verifyPublicMediaBytes(small,inv.photos[0]),{code:'PUBLIC_MEDIA_DECLARED_DIMENSIONS_MISMATCH'});passed.push('smaller_than_declared_file_rejected');
 await assert.rejects(()=>verifyPublicMediaBytes(big,inv.photos[0]),{code:'PUBLIC_MEDIA_DECLARED_DIMENSIONS_MISMATCH'});passed.push('larger_than_declared_file_also_rejected');
 const origFacts=await page.locator('article').evaluate(inspectMachineMediaArticle,identity);assert.equal(origFacts.supported,false);assert(origFacts.failures.includes('VISIBLE_ORIGINAL_SOURCE_REQUIRED'));
 passed.push('orig_only_mode_meaning_not_expanded_by_new_variant');
 await blocked('missing_declared_width_rejected',html.replace('width="160" ',''),'PUBLIC_MEDIA_DIMENSIONS_NOT_DECLARED');
 await blocked('crop_transform_in_public_url_rejected',html.replaceAll('name=large','name=large&amp;rect=0,0,80,80'),'PUBLIC_MEDIA_RAW_SOURCE_INVALID');
 await blocked('another_media_identifier_in_srcset_rejected',html.replace('media/OwnedMedia?format=webp&amp;name=large 320w','media/OtherImage?format=webp&amp;name=large 320w'),'PUBLIC_MEDIA_SRCSET_AMBIGUOUS');
 await blocked('duplicate_srcset_descriptors_rejected',html.replace('name=large 320w','name=large 80w'),'PUBLIC_MEDIA_SRCSET_AMBIGUOUS');
 await blocked('wrong_photo_source_rejected',html.replace('status/123/photo/1','status/999/photo/1'),'MEDIA_POST_IDENTITY_MISMATCH');
 await blocked('extra_visible_photo_rejected',html.replace('</article>','<img width="20" height="20" src="https://pbs.twimg.com/media/Extra?format=webp&amp;name=small"></article>'),'UNBOUND_VISIBLE_MEDIA');
 await load(html.replace(/ srcset="[^"]+"/,''));const noSet=await page.locator('article').evaluate(inspectMachineMediaArticle,spec);
 assert.equal(noSet.photos[0].source_url_basis,'raw_public_src');assert.equal(noSet.photos[0].download_url,'https://pbs.twimg.com/media/OwnedMedia?format=webp&name=large');passed.push('raw_public_src_valid_when_no_srcset_exists');
 await assert.rejects(()=>downloadPublicDOMMedia('https://127.0.0.1/private?format=webp&name=large'),{code:'PUBLIC_MEDIA_URL_INVALID'});passed.push('non_platform_url_blocked_before_network');
 const changed=structuredClone(bundle);changed.inventory.photos[0].download_url+='&name=orig';
 await assert.rejects(()=>verifyMachineMediaBundle(changed,{requireLive:false}),{code:'PUBLIC_MEDIA_BUNDLE_INTEGRITY_INVALID'});passed.push('invented_orig_candidate_cannot_pass_bundle_integrity');
 await load();await assert.rejects(()=>capturePublicMachineMedia(page,identity,path.join(root,'actual-small-bytes'),{allowedRoot:root,normalizeLinks:true,
  downloader:async()=>({bytes:small,status:200,content_type:'image/webp'})}),{code:'PUBLIC_MEDIA_DECLARED_DIMENSIONS_MISMATCH'});passed.push('capture_itself_rejects_insufficient_pixels_not_only_verifier');
 const parent=header.replaceAll('fixture_only','parent_fixture').replace('status/123','status/122').replace(/<div data-testid="tweetText">.*?<\/div>/,'<div data-testid="tweetText">Owned parent body</div>')+'</article>';
 await load(parent+html);
 await assert.rejects(()=>capturePublicMachineMedia(page,identity,path.join(root,'reply-default-blocked'),{allowedRoot:root,normalizeLinks:true,downloader}),{code:'WEB_REPLY_CONTEXT_REQUIRED'});
 const replyBundle=await capturePublicMachineMedia(page,identity,path.join(root,'reply-external-context'),{allowedRoot:root,normalizeLinks:true,downloader,allowReplyContext:true});
 assert.equal(replyBundle.requires_external_reply_evidence,true);assert.equal(replyBundle.relationship_verified_by_adapter,false);
 assert(replyBundle.text_facts.failures.includes('REPLY_CONTEXT_REQUIRED'));assert.equal(replyBundle.automatic_publication_authorized_by_this_bundle,false);
 assert.equal((await verifyMachineMediaBundle(replyBundle,{requireLive:false})).integrity_verified,true);passed.push('reply_media_requires_separate_relationship_evidence_and_preserves_raw_context_failure');
 assert.equal(external,0);console.log(JSON.stringify({passed,external_calls:external,model_calls:0,publish_requests:0,human_review:false,creator_original_upload_identity_verified:false}));
}finally{await browser.close();}
