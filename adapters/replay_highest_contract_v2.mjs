// Highest-quality route only. Local public-source evidence; no platform API.
import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {canonicalDigest} from '../scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs';

const SHA=/^[a-f0-9]{64}$/;
export const OPERATING_NOT_BEFORE='2026-10-31T16:00:00Z';
const QUALITY='h264-1080p60-itag299-aac140-v1';
function fail(code='REPLAY_HIGHEST_CONTRACT_REQUIRED'){throw Object.assign(new Error(code),{code});}
function same(a,b){return canonicalDigest(a)===canonicalDigest(b);}
function exact(value,fields){return value&&typeof value==='object'&&!Array.isArray(value)&&Object.keys(value).sort().join(',')===[...fields].sort().join(',');}

export function validateHighestContract(spec){
 try{
  const c=spec.highest_contract,b=c.download_binding,p=b.source_plan,payload=c.source_payload,raw=payload.raw;
  const started=Date.parse(raw.liveStreamingDetails.actualStartTime),canonical=new Date(started).toISOString().replace('.000Z','Z');
  if(!Number.isFinite(started)||started<Date.parse(OPERATING_NOT_BEFORE)||started>Date.now()+30000||
   p.operating_not_before!==OPERATING_NOT_BEFORE||b.operating_not_before!==OPERATING_NOT_BEFORE||
   p.source_actual_start_time!==canonical||b.source_actual_start_time!==canonical)fail('REPLAY_HIGHEST_ACTUAL_START_REQUIRED');
  if(!exact(c,['version','download_policy_version','download_binding','download_binding_canonical','source_payload','source_payload_canonical','download_receipt_sha256','native_upload_limits'])||
   c.version!=='replay-highest-source-contract-v2'||c.download_policy_version!=='youtube-highest-download-v2'||
   b.version!=='youtube-replay-highest-download-v2'||b.job_id!==spec.download_job_id||b.video_id!==spec.source_id||
   spec.source_id!=='9T5tn1WGlJg'||b.channel_id!==spec.channel_id||b.duration!==spec.duration_seconds||
   !SHA.test(b.policy_sha256||'')||b.quality_profile!==QUALITY||
   typeof c.download_binding_canonical!=='string'||Buffer.byteLength(c.download_binding_canonical)>65536||!same(JSON.parse(c.download_binding_canonical),b)||
   typeof c.source_payload_canonical!=='string'||Buffer.byteLength(c.source_payload_canonical)>65536||!same(JSON.parse(c.source_payload_canonical),payload)||
   b.source_payload_sha256!==createHash('sha256').update(c.source_payload_canonical,'utf8').digest('hex')||
   payload.source_api!=='youtube-data-v3-official'||payload.video_id!==spec.source_id||payload.url!==spec.source_url||
   raw.id!==spec.source_id||raw.snippet.channelId!==spec.channel_id||payload.channel_id!==spec.channel_id||
   raw.status.privacyStatus!=='public'||!SHA.test(c.download_receipt_sha256||'')||
   !exact(p,['version','quality_profile','selected_formats','source_bytes','planned_output_max_bytes','workspace_overhead_bytes','required_working_bytes',
    'minimum_range_bytes','media_requests_plan','metadata_request_allowance','redirect_resume_request_allowance','max_http_requests',
    'operating_not_before','source_actual_start_time'])||p.version!=='youtube-highest-source-plan-v2'||p.quality_profile!==QUALITY||
   !Array.isArray(p.selected_formats)||p.selected_formats.length!==2)fail();
  const [v,a]=p.selected_formats,fields=['format_id','ext','vcodec','acodec','width','height','fps','filesize'];
  if(!exact(v,fields)||!exact(a,fields)||
   !same({...v,filesize:0},{format_id:'299',ext:'mp4',vcodec:'h264',acodec:'none',width:1920,height:1080,fps:60,filesize:0})||
   !same({...a,filesize:0},{format_id:'140',ext:'m4a',vcodec:'none',acodec:'aac',width:null,height:null,fps:null,filesize:0})||
   ![v.filesize,a.filesize].every(x=>Number.isSafeInteger(x)&&x>0&&x<=14_000_000_000))fail();
  const total=v.filesize+a.filesize,media=Math.ceil(v.filesize/9961472)+Math.ceil(a.filesize/9961472),overhead=Math.max(64,Math.ceil(media/4));
  const expected={source_bytes:total,planned_output_max_bytes:total+64_000_000,workspace_overhead_bytes:32_000_000,
   required_working_bytes:2*total+96_000_000,minimum_range_bytes:9961472,media_requests_plan:media,
   metadata_request_allowance:64,redirect_resume_request_allowance:overhead,max_http_requests:media+64+overhead};
  if(Object.entries(expected).some(([k,v])=>!Number.isSafeInteger(p[k])||p[k]!==v)||p.planned_output_max_bytes>14_000_000_000||
   p.max_http_requests>2048||spec.file_bytes>p.planned_output_max_bytes)fail();
  const limits=c.native_upload_limits;
  if(limits.version!=='bilibili-highest-native-limit-binding-v2'||limits.site_uid!==spec.target_site_uid||limits.max_file_bytes!==16_000_000_000||
   limits.max_duration_seconds!==36000||limits.server_file_hash_verified!==false||limits.human_review!==false||
   limits.fresh_native_uid_and_limit_check_required!==true||!SHA.test(limits.binding?.sha256||''))fail();
  return c;
 }catch(e){if(typeof e?.code==='string'&&e.code.startsWith('REPLAY_HIGHEST_'))throw e;fail();}
}

export async function readBound(file,reference,maximum){
 if(reference?.file!==file||!SHA.test(reference?.sha256||'')||!Number.isSafeInteger(reference?.bytes)||reference.bytes<1||reference.bytes>maximum)fail();
 for(let p=file;p!==path.dirname(p);p=path.dirname(p))if((await fs.lstat(p)).isSymbolicLink())fail();
 if(await fs.realpath(file)!==file)fail();
 const handle=await fs.open(file,'r');try{
  const before=await handle.stat();if(!before.isFile()||before.size!==reference.bytes)fail();
  const raw=Buffer.alloc(reference.bytes+1);let count=0;
  while(count<raw.length){const read=await handle.read(raw,count,raw.length-count,null);if(!read.bytesRead)break;count+=read.bytesRead;}
  const after=await handle.stat();
  if(count!==reference.bytes||before.ino!==after.ino||before.dev!==after.dev||before.mtimeMs!==after.mtimeMs||before.ctimeMs!==after.ctimeMs||
   createHash('sha256').update(raw.subarray(0,count)).digest('hex')!==reference.sha256)fail();
  return JSON.parse(raw.subarray(0,count).toString('utf8'));
 }finally{await handle.close();}
}

export async function checkHighestSourceFiles(root,spec){
 const c=validateHighestContract(spec),file=path.join(root,'media',spec.download_job_id,'youtube-highest-v2/verified.json');
 const st=await fs.stat(file),verified=await readBound(file,{file,bytes:st.size,sha256:c.download_receipt_sha256},262144);
 const b=c.download_binding,a=verified.source_asset;
 if(verified.version!=='youtube-replay-highest-download-v2'||verified.binding_sha256!==createHash('sha256').update(c.download_binding_canonical,'utf8').digest('hex')||
  verified.human_review!==false||verified.publication_authorized!==false||verified.creator_original_bytes_verified!==false||
  a?.file!==spec.file||a.bytes!==spec.file_bytes||a.sha256!==spec.artifact_sha256||!Number.isFinite(a.duration)||Math.abs(a.duration-spec.duration_seconds)>2||
  !same(a.source_plan,b.source_plan)||!same(a.selected_formats,b.source_plan.selected_formats)||
  !same(verified.receipt?.source_plan,b.source_plan)||!same(verified.receipt?.selected_formats,b.source_plan.selected_formats)||
  verified.receipt?.source_to_merged_packet_coverage_verified!==true)fail();
 const limits=c.native_upload_limits,limitFile=path.join(root,'config/replay-highest-native-limits-v2/binding.json');
 const observed=await readBound(limitFile,limits.binding,65536);
 if(observed.version!==limits.version||observed.site_uid!==spec.target_site_uid||observed.max_file_bytes!==16_000_000_000||
  observed.max_duration_seconds!==36000||observed.server_file_hash_verified!==false||observed.human_review!==false)fail();
 return {version:'replay-highest-source-files-rechecked-v2',source_actual_start_time:b.source_actual_start_time,
  operating_not_before:OPERATING_NOT_BEFORE,download_receipt_sha256:c.download_receipt_sha256,native_limit_binding_sha256:limits.binding.sha256,
  server_file_hash_verified:false,human_review:false};
}
