// New v4 cases only. Existing v1/v2/v3 fixtures and frozen archives stay intact.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {deflateSync} from 'node:zlib';
import {Readable} from 'node:stream';
import {registerHooks} from 'node:module';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
const root=path.resolve(process.argv[2]),scenario=process.argv[3];
assert.ok(['v4_image','v4_large_image','v4_source_changed','v4_output_changed','v4_wrong_output_sha',
  'v4_mixed_v3_binding','v4_wrong_dimensions','v4_wrong_source_url'].includes(scenario));
const hash=value=>crypto.createHash('sha256').update(value).digest('hex');
const canonical=value=>Array.isArray(value)?'['+value.map(canonical).join(',')+']':
  value!==null&&typeof value==='object'?'{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+canonical(value[k])).join(',')+'}':JSON.stringify(value);
const digest=value=>hash(canonical(value)),job=crypto.randomUUID(),attempt=crypto.randomUUID(),recovery=crypto.randomUUID(),download=crypto.randomUUID();
const oldMetadata={title:'Owned v4 PNG fixture',description:'Owned line one\nhttps://www.youtube.com/watch?v=abcdefghijk',
  source:'https://www.youtube.com/watch?v=abcdefghijk',category_label:'虚拟UP主',tags:['owned'],copyright:2};
const metadata={...oldMetadata,category_label:'娱乐'};
const file=path.join(root,'media',download,'youtube-download-v1/original.mp4');
await fs.mkdir(path.dirname(file),{recursive:true});const mediaBytes=Buffer.from('Owned synthetic artifact; never a real source or upload');
await fs.writeFile(file,mediaBytes);
const old=path.join(root,'media',job,'short-video-attempts',attempt),profile=path.join(root,'data/browser-profiles/bilibili-video');
await fs.mkdir(old,{recursive:true});await fs.mkdir(profile,{recursive:true,mode:0o700});
const original={job_id:job,attempt_id:attempt,target_site_uid:'123456',metadata_sha256:digest(oldMetadata),
  artifact_sha256:hash(mediaBytes),source_id:'abcdefghijk',source_url:metadata.source,download_job_id:download,file,file_bytes:mediaBytes.length};
const upload={version:'short-video-browser-upload-v1',attempt_id:attempt,site_uid:'123456',artifact_sha256:hash(mediaBytes),
  file_bytes:mediaBytes.length,upload_reference_sha256:'b'.repeat(64),same_attempt_observed:true,server_file_hash_verified:false};
for(const [name,value] of Object.entries({'spec.json':original,'metadata.json':oldMetadata,'upload-observation.json':upload}))
  await fs.writeFile(path.join(old,name),JSON.stringify(value));
const bindingSHA=digest({...original,metadata,metadata_sha256:digest(metadata)});
const revision={version:'short-video-category-metadata-revision-v1',revision_id:crypto.randomUUID(),
  previous_binding:{...original,metadata:oldMetadata},previous_metadata_sha256:digest(oldMetadata),
  new_metadata_sha256:digest(metadata),new_binding_sha256:bindingSHA,changed_fields:['category_label']};
const spec={...original,version:'short-video-recovered-editor-v1',uploaded_attempt_id:attempt,recovery_id:recovery,
  metadata,metadata_sha256:digest(metadata),upload_metadata_sha256:digest(oldMetadata),binding_sha256:bindingSHA,
  metadata_revision_id:revision.revision_id,metadata_revision:revision,upload_reference_sha256:upload.upload_reference_sha256,duration_seconds:297};
// Synthetic original header is provenance-test data, not an acquired source.
const input=path.join(old,'cover-source-official-maxres.jpg');
const jpeg=Buffer.from([255,216,255,192,0,17,8,2,208,5,0,3,1,17,0,2,17,1,3,17,1,255,217]);
await fs.writeFile(input,jpeg);
function crc32(data){let value=0xffffffff;for(const byte of data){value^=byte;for(let n=0;n<8;n++)value=(value>>>1)^((value&1)?0xedb88320:0);}return (value^0xffffffff)>>>0;}
function chunk(name,body){const tag=Buffer.from(name),result=Buffer.alloc(body.length+12);result.writeUInt32BE(body.length,0);tag.copy(result,4);body.copy(result,8);result.writeUInt32BE(crc32(Buffer.concat([tag,body])),result.length-4);return result;}
function ownedPNG(width,height){
  const ihdr=Buffer.alloc(13);ihdr.writeUInt32BE(width,0);ihdr.writeUInt32BE(height,4);ihdr[8]=8;ihdr[9]=2;
  // Actual decodable RGB PNG, generated locally without network or a model.
  const scan=Buffer.alloc((width*3+1)*height,96);if(scenario==='v4_large_image')crypto.randomFillSync(scan);
  for(let y=0;y<height;y++)scan[y*(width*3+1)]=0;
  return Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]),chunk('IHDR',ihdr),chunk('IDAT',deflateSync(scan)),chunk('IEND',Buffer.alloc(0))]);
}
const assets=path.join(root,'media',job,'cover-assets-v4');await fs.mkdir(assets,{recursive:true});
const png=ownedPNG(1280,scenario==='v4_wrong_dimensions'?720:960),image=path.join(assets,'top-bottom-blur-cover.png');
await fs.writeFile(image,png);
if(scenario==='v4_large_image')assert.ok(png.length>1000000&&png.length<=5000000);
const sourceURL='https://i.ytimg.com/vi/abcdefghijk/maxresdefault.jpg';
spec.cover={file:image,bytes:png.length,sha256:hash(png),source_url:scenario==='v4_wrong_source_url'?sourceURL.replace('maxresdefault','hqdefault'):sourceURL};
const provenance={version:'lighthouse-derived-cover-v4',policy_version:'top-bottom-blur-v1',
  input:{file:input,bytes:jpeg.length,sha256:hash(jpeg),source_url:sourceURL,width:1280,height:720},
  output:{file:image,bytes:png.length,sha256:scenario==='v4_wrong_output_sha'?'0'.repeat(64):hash(png),width:1280,height:960,mime_type:'image/png'},
  transform:{filter_complex:'[0:v]format=rgb24,split=2[bg0][fg];[bg0]scale=1280:960:force_original_aspect_ratio=increase:flags=lanczos,crop=1280:960,gblur=sigma=35:steps=3[bg];[bg][fg]overlay=0:120:format=rgb,format=rgb24[out]',
    foreground_rect:{x:0,y:120,width:1280,height:720},safe_crop:{x:0,y:0,width:1280,height:960},
    background:{width:1280,height:960,blur_sigma:35},foreground_fit:'original_size',foreground_padding:'none'},
  ffmpeg_version:'Owned provenance fixture, not a real FFmpeg transformation',created_at:new Date().toISOString(),
  image_edited:true,no_text_regenerated:true,official_original_bytes:false};
const raw=JSON.stringify(provenance),document=path.join(assets,'provenance.json');await fs.writeFile(document,raw);
spec.cover_derivation={version:scenario==='v4_mixed_v3_binding'?'derived-cover-binding-v1':'derived-cover-binding-v2',
  provenance_file:document,provenance_sha256:hash(raw),provenance};
if(scenario==='v4_source_changed')await fs.writeFile(input,Buffer.concat([jpeg,Buffer.from('Owned changed original')]));
if(scenario==='v4_output_changed')await fs.writeFile(image,Buffer.concat([png,Buffer.from('Owned changed output')]));
const output=path.join(root,'media',job,'recovered-editors',recovery);
process.env.HUB_ROOT=root;process.env.BILIBILI_EXPECTED_SITE_UID='123456';
Object.defineProperty(process,'stdin',{value:Readable.from([JSON.stringify(spec)]),configurable:true});
const control='/run/short-video-controls/controls.mjs';
const controlSource='export const nativeControlCapabilities={submit:false};export async function handleNativeControl({page,helpers}){const result=await helpers.boundCoverChooser(()=>page.locator("#owned-cover").click(),{select:true});return {...result,submitted:false,native_click_performed:false};}';
const controlSHA=hash(controlSource),controlURL='owned-v4-control:'+controlSHA;
const lstat=fs.lstat.bind(fs),read=fs.readFile.bind(fs),open=fs.open.bind(fs);
fs.lstat=async(file,...args)=>{
  if(String(file)===control)return {isFile:()=>true,isSymbolicLink:()=>false,uid:0,mode:0o100644,size:Buffer.byteLength(controlSource)};
  if(String(file)===path.dirname(control))return {isSymbolicLink:()=>false,uid:0,mode:0o40755};
  const value=await lstat(file,...args);
  return process.platform==='win32'&&String(file)===profile?Object.assign(value,{mode:value.mode&~0o077}):value;
};
fs.readFile=async(file,...args)=>String(file)===control?Buffer.from(controlSource):String(file)==='/proc/self/mountinfo'?
  '999 1 0:1 / /run/short-video-controls ro - tmpfs owned-fixture ro\n':read(file,...args);
if(process.platform==='win32')fs.open=async(file,flags,...args)=>String(file)===output&&flags==='r'?{sync:async()=>{},close:async()=>{}}:open(file,flags,...args);
const hook=registerHooks({resolve(s,c,next){if(s.startsWith('file:')&&s.includes('/run/short-video-controls/controls.mjs?'))return {url:controlURL,shortCircuit:true};return next(s,c);},
  load(url,c,next){if(url===controlURL)return {format:'module',source:controlSource,shortCircuit:true};return next(url,c);}});
let active,captured={cover:0,video:0,submit:0,selected_name:null,selected_type:null},outside=0,first,phase=0,pollBusy=false,error,result;
const launch=chromium.launchPersistentContext.bind(chromium);
chromium.launchPersistentContext=async(name,options)=>{
  const context=await launch(name,options);active=context;
  await context.route('**/*',route=>{
    const url=new URL(route.request().url());let html;
    if(url.hostname==='t.bilibili.com')html='<a class="header-entry-mini" href="https://space.bilibili.com/123456">Owned fixture identity</a>';
    else if(url.hostname==='member.bilibili.com')html='<input type="file" id="video" accept=".mp4" hidden>'+
      '<input placeholder="请输入稿件标题" value="'+metadata.title+'">'+
      '<div class="ql-editor" contenteditable="true"><p>Owned line one</p><p>'+metadata.source+'</p></div>'+
      '<input placeholder="转载视频请注明来源、时间、地点" value="'+metadata.source+'"><span>上传完成</span>'+
      '<button id="owned-cover">Owned cover upload</button><input id="cover" type="file" accept="image/*,.png" hidden>'+
      '<span class="submit-add">立即投稿</span><script>window.owned={cover:0,video:0,submit:0,selected_name:null,selected_type:null};'+
      'document.querySelector("#owned-cover").onclick=()=>document.querySelector("#cover").click();'+
      'document.querySelector("#cover").onchange=e=>{owned.cover++;owned.selected_name=e.target.files[0].name;owned.selected_type=e.target.files[0].type;};'+
      'document.querySelector("#video").onchange=()=>owned.video++;document.querySelector(".submit-add").onclick=()=>owned.submit++;</script>';
    else {outside++;return route.abort();}
    return route.fulfill({contentType:'text/html',body:'<!doctype html><meta charset="utf-8"><style>body{font:16px Arial}input{display:block;width:600px;height:30px}.ql-editor{min-height:80px}button{height:40px}</style>'+html});
  });
  const close=context.close.bind(context);context.close=async(...args)=>{
    for(const page of context.pages())if(page.url().startsWith('https://member.bilibili.com/'))captured=await page.evaluate(()=>window.owned).catch(()=>captured);
    return close(...args);
  };
  return context;
};
async function exists(file){try{return JSON.parse(await read(file,'utf8'));}catch(e){if(e.code==='ENOENT')return null;throw e;}}
async function command(action,extra={}){
  const command_id=crypto.randomUUID(),now=Date.now(),value={version:'short-video-recovered-editor-command-v1',command_id,
    recovery_id:recovery,action,created_at:new Date(now-1000).toISOString(),expires_at:new Date(now+60000).toISOString(),...extra};
  const file=path.join(output,'command.json');await fs.writeFile(file+'.owned',JSON.stringify(value));await fs.rename(file+'.owned',file);return command_id;
}
const watchdog=setTimeout(()=>{error??=new Error('Owned v4 watchdog');active?.close().catch(()=>{});},45000);
const poll=setInterval(async()=>{
  if(pollBusy)return;pollBusy=true;
  try{
    const status=await exists(path.join(output,'status.json'));if(!status)return;
    if(phase===0&&status.state==='recovered_editor_waiting_for_operator'){
      phase=1;first=await command('native_control',{control_sha256:controlSHA,control_action:'fill_metadata'});
    }else if(phase===1&&first){
      const observed=await exists(path.join(output,'command-'+first+'-result.json'));
      if(observed){result=observed.result;phase=2;await command('close');}
    }
  }catch(e){error??=e;await active?.close().catch(()=>{});}
  finally{pollBusy=false;}
},30);
try{await import('../adapters/browser_recovered_short_video.mjs?owned-v4='+crypto.randomUUID());}
finally{clearTimeout(watchdog);clearInterval(poll);hook.deregister();fs.open=open;fs.lstat=lstat;fs.readFile=read;}
assert.ifError(error);assert.equal(phase,2);assert.equal(captured.video,0);assert.equal(captured.submit,0);assert.equal(outside,0);
if(['v4_image','v4_large_image'].includes(scenario)){
  assert.equal(result.state,'native_bound_cover_assigned');assert.equal(captured.cover,1);
  assert.equal(captured.selected_name,'top-bottom-blur-cover.png');assert.equal(captured.selected_type,'image/png');
  const receipt=JSON.parse(await read(path.join(output,'cover-selection-observation.json'),'utf8'));
  assert.equal(receipt.cover.sha256,spec.cover.sha256);assert.equal(receipt.cover_derivation.version,'derived-cover-binding-v2');
  assert.equal(receipt.server_file_hash_verified,false);assert.equal(receipt.public_submit,false);
}else{
  assert.equal(result.state,'blocked');assert.equal(captured.cover,0);
  await assert.rejects(()=>fs.stat(path.join(output,'cover-selection-intent.json')),e=>e.code==='ENOENT');
}
console.log('FIXTURE_RESULT:'+JSON.stringify({scenario,cover_selections:captured.cover,video_selections:captured.video,
  public_submit:captured.submit,external_network_calls:outside,live_tested:false,real_normalizer_executed:false,real_owner_approval:false}));
