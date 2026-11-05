// Owned rendered HTML only. No URL dereference, model or publication.
import assert from 'node:assert/strict';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {captureMachineLinkText,normalizeMachineLinkPayload,verifyMachineLinkNormalization} from '../adapters/machine_link_text.mjs';
const identity={handle:'fixture_only',postId:'123'};
const header='<article style="width:650px;margin:40px;padding:10px"><div data-testid="User-Name"><span>Owned fixture</span><a href="https://x.com/fixture_only">@fixture_only</a></div><a href="https://x.com/fixture_only/status/123"><time datetime="2026-10-31T00:00:00Z">12:00</time></a>';
const body=html=>header+'<div data-testid="tweetText" style="white-space:pre-wrap">'+html+'</div></article>';
const browser=await chromium.launch({headless:true}),page=await browser.newPage({viewport:{width:1000,height:800}});
let calls=0;await page.route('**/*',r=>{calls++;return r.abort();});
const passed=[];
async function read(html){await page.setContent('<base href="https://x.com/">'+body(html));return captureMachineLinkText(page,identity);}
const source=observed=>({post:{id:'123',text:observed.original_inner_text,created_at:'2026-10-31T00:00:00Z',referenced_tweets:[]},
 author:{name:'Owned fixture',username:'fixture_only'},url:'https://x.com/fixture_only/status/123',source_api:'owned-test-fixture',
 capture:{human_review:false,source_screenshot_reviewed:false,full_source_completeness_verified:false},testFixture:true});
async function rejected(name,html,code){const observation=await read(html);assert.equal(observation.supported,false,name);
 assert(observation.failures.includes(code),JSON.stringify(observation));assert.throws(()=>normalizeMachineLinkPayload(source(observation),observation),{code:'MACHINE_LINK_NORMALIZATION_INVALID'});passed.push(name);}
try{
 const observed=await read('😀配信はこちら\n<a href="https://t.co/16UDjexcs1">youtube.com/live/9T5tn1WGl…</a>\n<a href="/hashtag/OwnedFixture">#OwnedFixture</a> <a href="/creator_fixture">@creator_fixture</a>\n末尾。');
 const original=source(observed),before=JSON.stringify(original),observationBefore=JSON.stringify(observed),packet=normalizeMachineLinkPayload(original,observed);
 assert.equal(packet.derived_payload.post.text,'😀配信はこちら\nhttps://t.co/16UDjexcs1\n#OwnedFixture @creator_fixture\n末尾。');
 assert.equal(JSON.stringify(original),before);assert.equal(JSON.stringify(observed),observationBefore);assert.notEqual(packet.derived_payload,original);
 assert.equal(packet.evidence.original_inner_text,original.post.text);assert.equal(packet.evidence.normalized_visible_body_complete,true);
 assert.equal(packet.evidence.expanded_destination_inferred,false);assert.equal(packet.evidence.url_targets_dereferenced,false);
 assert.equal(packet.evidence.human_review,false);assert.equal(packet.derived_payload.capture.human_review,false);
 assert.equal(packet.derived_payload.capture.source_screenshot_reviewed,false);assert.equal(packet.derived_payload.capture.full_source_completeness_verified,false);
 assert.equal(verifyMachineLinkNormalization(original,observed,packet).integrity_verified,true);
 assert.equal(packet.evidence.operations[0].actual_href,'https://t.co/16UDjexcs1');
 assert.equal(packet.evidence.operations[0].replacement,'https://t.co/16UDjexcs1');assert.equal(observed.anchors[0].label,'youtube.com/live/9T5tn1WGl…');
 assert.equal(observed.anchors[0].start_utf16-observed.anchors[0].start_codepoint,1);
 passed.push('actual_tco_href_replaces_ellipsis_label_without_guessing_youtube_destination');
 passed.push('original_payload_observation_and_all_human_fields_unchanged');
 passed.push('mentions_hashtags_unicode_offsets_and_other_newlines_preserved');
 const tampered=structuredClone(packet);tampered.derived_payload.post.text=tampered.derived_payload.post.text.replace('https://t.co/16UDjexcs1','https://youtube.com/live/guessed');
 assert.throws(()=>verifyMachineLinkNormalization(original,observed,tampered),{code:'MACHINE_LINK_NORMALIZATION_INTEGRITY_INVALID'});passed.push('guessed_destination_cannot_pass_recomputed_evidence');
 const wrongSource=structuredClone(original);wrongSource.post.id='999';
 assert.throws(()=>normalizeMachineLinkPayload(wrongSource,observed),{code:'MACHINE_LINK_NORMALIZATION_INVALID'});passed.push('normalization_bound_to_exact_original_source_identity');
 const badEvidence=structuredClone(packet);badEvidence.evidence.unchanged_regions[0].sha256='0'.repeat(64);
 assert.throws(()=>verifyMachineLinkNormalization(original,observed,badEvidence),{code:'MACHINE_LINK_NORMALIZATION_INTEGRITY_INVALID'});passed.push('changed_unchanged_region_hash_rejected');
 const badObservation=structuredClone(observed);badObservation.anchors[0].href_attribute='https://t.co/another';
 assert.throws(()=>normalizeMachineLinkPayload(original,badObservation),{code:'MACHINE_LINK_NORMALIZATION_INVALID'});passed.push('label_href_mapping_recomputed_not_trusted');
 await rejected('duplicate_anchor_labels_rejected','<a href="https://t.co/a">same…</a> <a href="https://t.co/b">same…</a>','LINK_LABEL_AMBIGUOUS');
 await rejected('unlinked_duplicate_label_rejected','same… <a href="https://t.co/a">same…</a>','LINK_LABEL_AMBIGUOUS');
 await rejected('hidden_anchor_rejected','Visible text <a style="display:none" href="https://t.co/a">hidden</a>','LINK_ANCHOR_NOT_VISIBLE');
 await rejected('transparent_ancestor_anchor_rejected','Visible text <span style="opacity:0"><a href="https://t.co/a">invisible</a></span>','LINK_ANCHOR_NOT_VISIBLE');
 await rejected('javascript_link_rejected','<a href="javascript:void(0)">unsafe</a>','LINK_HREF_PROTOCOL_OR_TARGET_INVALID');
 await rejected('http_link_outside_https_contract_rejected','<a href="http://example.invalid/">plain http</a>','LINK_HREF_PROTOCOL_OR_TARGET_INVALID');
 await rejected('empty_href_rejected','<a href="">placeholder</a>','LINK_HREF_PROTOCOL_OR_TARGET_INVALID');
 await rejected('mismatched_mention_target_rejected','<a href="/other_person">@creator_fixture</a>','LINK_MENTION_TARGET_MISMATCH');
 await rejected('linebroken_label_rejected','<a href="https://t.co/a">one\ntwo</a>','LINK_LABEL_EMPTY_OR_MULTILINE');
 await rejected('css_clipped_body_not_claimed_complete','<span style="display:block;width:20px;height:10px;overflow:hidden">long original body <a href="https://t.co/a">long link…</a></span>','LINK_ANCHOR_NOT_VISIBLE');
 await page.setContent('<base href="https://x.com/">'+body('<a href="https://t.co/a">parent</a>'));
 await page.locator('[data-testid="tweetText"] a').evaluate(a=>{const child=document.createElement('a');child.href='https://t.co/b';child.textContent='nested';a.append(child);});
 const nested=await captureMachineLinkText(page,identity);assert(nested.failures.includes('LINK_ANCHOR_NESTED_OR_NON_TEXT'));passed.push('nested_anchor_structure_rejected');
 const relative=await read('<a href="//t.co/actual?x=1&amp;y=2"><span>display…</span></a>');
 assert.equal(normalizeMachineLinkPayload(source(relative),relative).derived_payload.post.text,'https://t.co/actual?x=1&y=2');passed.push('resolved_dom_href_and_inline_span_label_bound_without_url_fetch');
 const plain=await read('No anchors: author typed an ellipsis…\n😀原文');const plainPacket=normalizeMachineLinkPayload(source(plain),plain);
 assert.equal(plainPacket.derived_payload.post.text,plain.original_inner_text);assert.equal(plainPacket.evidence.operations.length,0);passed.push('unlinked_author_text_never_expanded_or_guessed');
 await read('Owned text <a href="https://t.co/a">covered</a>');await page.evaluate(()=>{const e=document.createElement('div');e.style='position:fixed;inset:0;background:white;z-index:100';document.body.append(e);});
 const covered=await captureMachineLinkText(page,identity);assert(covered.failures.includes('LINK_ANCHOR_NOT_VISIBLE'));passed.push('covered_link_is_not_attested_visible');
 const more=await read('Owned text<a href="https://t.co/a">visible</a><button>Show more</button>');assert(more.failures.includes('LINK_BODY_TRUNCATED'));passed.push('show_more_body_never_claimed_complete');
 assert.equal(calls,0);console.log(JSON.stringify({passed,external_calls:calls,model_calls:0,publish_requests:0,human_review:false,url_targets_dereferenced:false}));
}finally{await browser.close();}
