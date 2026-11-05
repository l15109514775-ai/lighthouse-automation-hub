// Real Chromium on fully owned/routed DOM. No account, download or publication.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash,randomUUID} from 'node:crypto';
import {deflateSync} from 'node:zlib';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';

const root=path.resolve(process.argv[2]),scenario=process.argv[3];
assert.ok(['cover_complete','cover_protection','cover_ambiguous','cover_unknown_once','private_saved','private_unknown_once'].includes(scenario));
const privateScenario=scenario.startsWith('private_');
process.env.HUB_ROOT=root;
const sha=bytes=>createHash('sha256').update(bytes).digest('hex');
function canonical(value){return Array.isArray(value)?'['+value.map(canonical).join(',')+']':
  value!==null&&typeof value==='object'?'{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+canonical(value[k])).join(',')+'}':JSON.stringify(value);}
const digest=value=>sha(canonical(value)),job=randomUUID(),attempt=randomUUID(),recovery=randomUUID(),download=randomUUID();
const sourceURL='https://www.youtube.com/watch?v=abcdefghijk';
const metadata={title:'Owned synthetic cover completion',description:'Owned line one\n'+sourceURL,
  source:sourceURL,tags:['owned','fixture'],category_label:'娱乐',copyright:2};
const file=path.join(root,'media',download,'youtube-download-v1/original.mp4');
await fs.mkdir(path.dirname(file),{recursive:true});
const artifact=Buffer.from('Owned synthetic fixture; never a real source or platform upload.');
await fs.writeFile(file,artifact);
const old=path.join(root,'media',job,'short-video-attempts',attempt);
const output=path.join(root,'media',job,'recovered-editors',recovery);
const assets=path.join(root,'media',job,privateScenario?'cover-assets-v4':'cover-assets-v3');
await fs.mkdir(old,{recursive:true});await fs.mkdir(output,{recursive:true});await fs.mkdir(assets,{recursive:true});
// Valid size headers are synthetic provenance fixtures. Actual FFmpeg rendering
// and decoded crop pixels are separately exercised by test_cover_normalizer.py.
const original=path.join(old,'cover-source-official-maxres.jpg');
const header=Buffer.from([255,216,255,192,0,17,8,2,208,5,0,3,1,17,0,2,17,1,3,17,1,255,217]);
await fs.writeFile(original,header);
function crc32(data){let value=0xffffffff;for(const byte of data){value^=byte;for(let n=0;n<8;n++)value=(value>>>1)^((value&1)?0xedb88320:0);}return (value^0xffffffff)>>>0;}
function chunk(name,body){const tag=Buffer.from(name),result=Buffer.alloc(body.length+12);result.writeUInt32BE(body.length,0);tag.copy(result,4);body.copy(result,8);result.writeUInt32BE(crc32(Buffer.concat([tag,body])),result.length-4);return result;}
function png(){const ihdr=Buffer.alloc(13);ihdr.writeUInt32BE(1280,0);ihdr.writeUInt32BE(960,4);ihdr[8]=8;ihdr[9]=2;
  const rgb=Buffer.alloc((1280*3+1)*960,96);for(let y=0;y<960;y++)rgb[y*(1280*3+1)]=0;
  return Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]),chunk('IHDR',ihdr),chunk('IDAT',deflateSync(rgb)),chunk('IEND',Buffer.alloc(0))]);}
const image=path.join(assets,privateScenario?'top-bottom-blur-cover.png':'blur-safe-cover.jpg'),
  imageBytes=privateScenario?png():Buffer.concat([header,Buffer.from('Owned derived header fixture')]);
await fs.writeFile(image,imageBytes);
const cover={file:image,bytes:imageBytes.length,sha256:sha(imageBytes),source_url:'https://i.ytimg.com/vi/abcdefghijk/maxresdefault.jpg'};
const filter=privateScenario?'[0:v]format=rgb24,split=2[bg0][fg];[bg0]scale=1280:960:force_original_aspect_ratio=increase:flags=lanczos,crop=1280:960,gblur=sigma=35:steps=3[bg];[bg][fg]overlay=0:120:format=rgb,format=rgb24[out]':'[0:v]split=2[bg0][fg0];[bg0]scale=1280:720:force_original_aspect_ratio=increase:flags=lanczos,crop=1280:720,gblur=sigma=35:steps=3[bg];[fg0]scale=896:504:force_original_aspect_ratio=decrease:flags=lanczos,pad=896:504:(ow-iw)/2:(oh-ih)/2:color=black[fg];[bg][fg]overlay=192:108:format=auto,setsar=1,format=yuvj420p[out]';
const transform=privateScenario?{filter_complex:filter,foreground_rect:{x:0,y:120,width:1280,height:720},
  safe_crop:{x:0,y:0,width:1280,height:960},background:{width:1280,height:960,blur_sigma:35},foreground_fit:'original_size',foreground_padding:'none'}:
  {filter_complex:filter,foreground_rect:{x:192,y:108,width:896,height:504},safe_crop:{x:160,y:0,width:960,height:720},
    background:{width:1280,height:720,blur_sigma:35},foreground_fit:'contain',foreground_padding:'black'};
const provenance={version:privateScenario?'lighthouse-derived-cover-v4':'lighthouse-derived-cover-v3',
  policy_version:privateScenario?'top-bottom-blur-v1':'blur-safe-foreground-v1',
  input:{file:original,bytes:header.length,sha256:sha(header),source_url:cover.source_url,width:1280,height:720},
  output:{file:image,bytes:imageBytes.length,sha256:cover.sha256,width:1280,height:privateScenario?960:720,mime_type:privateScenario?'image/png':'image/jpeg'},
  transform,
  ffmpeg_version:'Owned synthetic proof fixture, not an actual transformation',created_at:new Date().toISOString(),
  image_edited:true,no_text_regenerated:true,official_original_bytes:false};
const provenanceFile=path.join(assets,'provenance.json'),raw=JSON.stringify(provenance);
await fs.writeFile(provenanceFile,raw);
const spec={job_id:job,attempt_id:attempt,uploaded_attempt_id:attempt,recovery_id:recovery,download_job_id:download,
  target_site_uid:'123456',source_id:'abcdefghijk',source_url:sourceURL,file,file_bytes:artifact.length,
  artifact_sha256:sha(artifact),metadata_sha256:digest(metadata),metadata,cover,
  cover_derivation:{version:privateScenario?'derived-cover-binding-v2':'derived-cover-binding-v1',provenance_file:provenanceFile,provenance_sha256:sha(raw),provenance}};
await fs.writeFile(path.join(output,'metadata.json'),JSON.stringify(metadata));
await fs.writeFile(path.join(output,'cover-selection-observation.json'),JSON.stringify({
  version:'bound-native-cover-selection-v1',cover,cover_derivation:spec.cover_derivation,
  files_assigned:true,server_file_hash_verified:false,new_video_file_selection:false,public_submit:false,
  owned_fixture_only:true}));

const intentFolder=path.join(root,'media',job,'native-control-intents');
const intent=path.join(intentFolder,'cover-complete-'+digest([attempt,spec.metadata_sha256,cover.sha256])+'.json');
const draftIntent=path.join(intentFolder,'draft-save-'+digest([attempt,spec.metadata_sha256,cover.sha256])+'.json');
let fileSynced=false,directorySynced=false;
const open=fs.open.bind(fs);
fs.open=async(file,flags,...args)=>{
  if(String(file)===intentFolder&&flags==='r'){
    // Windows cannot fsync directories. This single owned boundary is simulated;
    // Linux performs the actual directory fsync with the production function.
    const handle=process.platform==='win32'?null:await open(file,flags,...args);
    return {sync:async()=>{await handle?.sync();directorySynced=true;},close:async()=>{await handle?.close();}};
  }
  const handle=await open(file,flags,...args);
  if([intent,draftIntent].includes(String(file))&&flags==='wx')return {
    writeFile:(...a)=>handle.writeFile(...a),sync:async()=>{await handle.sync();fileSynced=true;},close:()=>handle.close()};
  return handle;
};
const {completeCover,savePrivateDraft,observePrivateDraft}=await import('../adapters/short_video_native_controls_core.mjs');
let context,receipt,externalRequests=0,privateRequests=0,fileSelections=0;
const escape=value=>value.replaceAll('&','&amp;').replaceAll('"','&quot;').replaceAll('<','&lt;');
try{
  const allowed=['PATH','HOME','LANG','LC_ALL','TMP','TEMP','TMPDIR','PLAYWRIGHT_BROWSERS_PATH',
    'XDG_CONFIG_HOME','XDG_CACHE_HOME','SystemRoot','WINDIR','USERPROFILE'];
  const env=Object.fromEntries(allowed.filter(key=>process.env[key]!==undefined).map(key=>[key,process.env[key]]));
  assert.equal(Object.keys(env).some(key=>/KEY|TOKEN|COOKIE|SECRET/i.test(key)),false);
  context=await chromium.launch({headless:true,channel:'chromium',chromiumSandbox:true,env});
  const page=await context.newPage({viewport:{width:1360,height:1000}});
  page.setDefaultTimeout(5000);
  await page.exposeFunction('ownedBeforeComplete',async()=>{
    assert.equal(fileSynced,true);assert.equal(directorySynced,true);
    const record=JSON.parse(await fs.readFile(intent,'utf8'));
    assert.equal(record.job_id,job);assert.equal(record.attempt_id,attempt);
    assert.equal(record.metadata_sha256,spec.metadata_sha256);assert.equal(record.cover_sha256,cover.sha256);
    assert.equal(record.public_submit,false);assert.equal(record.new_video_file_selection,false);
  });
  await page.exposeFunction('ownedBeforeDraft',async()=>{
    assert.equal(fileSynced,true);assert.equal(directorySynced,true);
    const record=JSON.parse(await fs.readFile(draftIntent,'utf8'));
    assert.equal(record.kind,'draft-save');assert.equal(record.job_id,job);
    assert.equal(record.metadata_sha256,spec.metadata_sha256);assert.equal(record.cover_sha256,cover.sha256);
    assert.equal(record.public_submit,false);assert.equal(record.new_video_file_selection,false);
  });
  await page.route('**/*',async route=>{
    const url=new URL(route.request().url());
    if(url.origin!=='https://member.bilibili.com'){externalRequests++;return route.abort();}
    if(url.pathname!=='/favicon.ico'&&(url.pathname!=='/platform/upload/video/frame'||
      route.request().method()!=='GET'||route.request().resourceType()!=='document')){privateRequests++;return route.abort();}
    const html='<!doctype html><meta charset="utf-8"><style>'+
      'body{font:16px Arial}input{display:block;width:600px;height:28px}.ql-editor{width:600px;min-height:50px}'+
      '.cover-module-main{width:320px;height:180px;background:#ddd}.cover-editor{position:fixed;left:660px;top:380px;width:500px;height:230px;background:white;border:2px solid black}'+
      '.button{display:inline-block;width:100px;height:40px;margin:20px;cursor:pointer}.owned-protection{position:fixed;inset:0;background:white;z-index:99}</style>'+
      '<input placeholder="请输入稿件标题" value="'+escape(metadata.title)+'">'+
      '<div class="ql-editor" contenteditable="true"><p>Owned line one</p><p>'+sourceURL+'</p></div>'+
      '<input placeholder="转载视频请注明来源、时间、地点" value="'+sourceURL+'">'+
      '<input readonly placeholder="请选择符合您视频内容的创作声明" value="内容为转载">'+
      '<div class="video-human-type"><div class="select-controller">娱乐</div></div>'+
      metadata.tags.map(tag=>'<div class="label-item-v2-container"><p class="label-item-v2-content">'+tag+'</p></div>').join('')+
      '<div id="owned-identity" data-site-uid="123456">Owned identity fixture</div>'+
      '<div class="cover-main"><div class="cover-module-main"><span>添加封面</span></div></div>'+
      '<input type="file" hidden><span class="submit-draft">存草稿</span><span class="submit-add">立即投稿</span>'+
      '<div class="cover-editor bcc-dialog__wrap-mask"><h2>封面制作</h2><div class="cover-editor-button">'+
      '<div class="button" id="complete">完成</div>'+
      (scenario==='cover_ambiguous'?'<div class="button">完成</div>':'')+
      '<div class="button" id="cancel">取消</div></div></div>'+
      (scenario==='cover_protection'?'<div class="owned-protection">Owned verification required</div>':'')+
      '<script>window.owned={complete:0,cancel:0,draft_save:0,public_submit:0,file_selections:0,storage_accesses:0};'+
      'for(const key of ["getItem","setItem","removeItem","clear"]){Storage.prototype[key]=function(){owned.storage_accesses++;throw new Error("Owned storage access forbidden");};}'+
      'document.querySelector("input[type=file]").addEventListener("change",()=>owned.file_selections++);'+
      'document.querySelector("#complete").addEventListener("click",async()=>{await ownedBeforeComplete();owned.complete++;document.querySelector(".cover-editor").remove();'+
      (scenario==='cover_unknown_once'?'':'document.querySelector(".cover-module-main").innerHTML="<canvas width=1280 height='+(privateScenario?960:720)+' style=\\"width:320px;height:180px\\"></canvas>";')+
      '});document.querySelector("#cancel").addEventListener("click",()=>owned.cancel++);'+
      'document.querySelector(".submit-draft").addEventListener("click",async()=>{await ownedBeforeDraft();owned.draft_save++;'+
      (scenario==='private_saved'?'const toast=document.createElement("div");toast.textContent="草稿保存成功";document.body.append(toast);':
        scenario==='private_unknown_once'?'document.querySelector(".submit-draft").remove();':'')+
      '});'+
      'document.querySelector(".submit-add").addEventListener("click",()=>owned.public_submit++);</script>';
    return route.fulfill({contentType:'text/html',body:html});
  });
  page.on('filechooser',()=>{fileSelections++;});
  await page.goto('https://member.bilibili.com/platform/upload/video/frame',{waitUntil:'domcontentloaded'});
  const helpers={
    checkProtection:async()=>await page.locator('.owned-protection:visible').count()===0,
    accountIdentity:async()=>page.locator('#owned-identity').getAttribute('data-site-uid'),
    checkedFile:async()=>{assert.equal(sha(await fs.readFile(file)),spec.artifact_sha256);},
    write:async(name,value)=>{await fs.writeFile(path.join(output,name),JSON.stringify(value,null,2));},
    inventory:async label=>{
      const screenshot=path.join(output,label+'.png');await page.screenshot({path:screenshot});
      return {screenshot_file:screenshot,owned_fixture_only:true};
    }};
  const result=await completeCover({page,spec,output,helpers});
  if(privateScenario){
    assert.equal(result.state,'cover_complete_applied');
    fileSynced=false;directorySynced=false;
    const draft=await savePrivateDraft({page,spec,output,helpers}),expected=scenario==='private_saved'?'private_draft_saved':'save_unknown';
    assert.equal(draft.state,expected);assert.equal(draft.draft_id,null);assert.equal(draft.draft_id_verified,false);
    assert.equal(draft.submitted,false);assert.equal(draft.native_success_observed,scenario==='private_saved');
    const retry=await savePrivateDraft({page,spec,output,helpers});
    assert.equal(retry.state,'private_draft_reconcile_required');assert.equal(retry.repeat_forbidden,true);
    const observed=await observePrivateDraft({page,spec,output,helpers});
    assert.equal(observed.state,scenario==='private_saved'?'recorded_private_draft_saved':'private_draft_save_unknown');
    assert.equal(observed.new_native_click,false);assert.equal(observed.draft_id,null);assert.equal(observed.draft_id_verified,false);
    const preserved=JSON.parse(await fs.readFile(path.join(output,'native-private-draft-result.json'),'utf8'));
    assert.equal(preserved.state,expected);
  }else if(scenario==='cover_complete'){
    assert.equal(result.state,'cover_complete_applied');assert.equal(result.native_click_performed,true);
    assert.equal(await page.locator('.cover-editor:visible').count(),0);
    assert.equal(result.visible_cover_region.preview_present,true);assert.equal(result.remote_cover_bytes_verified,false);
  }else if(scenario==='cover_unknown_once'){
    assert.equal(result.state,'cover_complete_unknown');assert.equal(result.repeat_forbidden,true);
    const retry=await completeCover({page,spec,output,helpers});
    assert.equal(retry.state,'cover_complete_reconcile_required');assert.equal(retry.repeat_forbidden,true);
    const preserved=JSON.parse(await fs.readFile(path.join(output,'native-cover-complete-result.json'),'utf8'));
    assert.equal(preserved.state,'cover_complete_unknown');assert.equal(preserved.repeat_forbidden,true);
  }else{
    assert.equal(result.state,'blocked');assert.equal(result.native_click_performed,false);
    assert.equal(result.error_code,scenario==='cover_protection'?'NATIVE_PROTECTION_REQUIRED':'NATIVE_CONTROL_AMBIGUOUS');
    await assert.rejects(()=>fs.stat(intent),error=>error.code==='ENOENT');
  }
  const counts=await page.evaluate(()=>window.owned);
  receipt={scenario,complete_clicks:counts.complete,draft_save_clicks:counts.draft_save,cancel_clicks:counts.cancel,public_submit:counts.public_submit,
    external_network_calls:externalRequests,private_api_requests:privateRequests,file_selections:fileSelections+counts.file_selections,
    storage_accesses:counts.storage_accesses,live_tested:false,real_owner_approval:false,
    source_or_cover_normalizer_executed:false};
  assert.equal(receipt.complete_clicks,privateScenario||['cover_complete','cover_unknown_once'].includes(scenario)?1:0);
  assert.equal(receipt.draft_save_clicks,privateScenario?1:0);
  assert.equal(receipt.cancel_clicks,0);assert.equal(receipt.public_submit,0);
}finally{
  fs.open=open;
  await context?.close();
}
console.log('FIXTURE_RESULT:'+JSON.stringify(receipt));
