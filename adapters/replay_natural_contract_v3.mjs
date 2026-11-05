// Rebuild the finite highest-tier selection from its genuine frozen inventory.
import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {canonicalDigest} from '../scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs';
import {readBound,OPERATING_NOT_BEFORE} from './replay_highest_contract_v2.mjs';
const SHA=/^[a-f0-9]{64}$/,QUALITY='highest-height-fps-h264-aac-stream-copy-v1';
const BASIS='maximum_available_height_then_fps;highest_reported_h264_bitrate_at_that_tier;highest_reported_aac_bitrate';
function fail(){throw Object.assign(new Error('REPLAY_NATURAL_CONTRACT_REQUIRED'),{code:'REPLAY_NATURAL_CONTRACT_REQUIRED'});}
const digest=s=>createHash('sha256').update(s,'utf8').digest('hex');
const same=(a,b)=>canonicalDigest(a)===canonicalDigest(b);
const exact=(value,fields)=>value&&typeof value==='object'&&!Array.isArray(value)&&Object.keys(value).sort().join(',')===[...fields].sort().join(',');
const positive=v=>typeof v==='number'&&Number.isFinite(v)&&v>0;
function canonicalBytes(text,value,maximum=262144){if(typeof text!=='string'||Buffer.byteLength(text)>maximum||!same(JSON.parse(text),value))fail();return digest(text);}

export function validateNaturalContract(spec){
 try{
  const c=spec.highest_contract,b=c.download_binding,p=b.source_plan,payload=c.source_payload,raw=payload.raw;
  if(!exact(c,['version','download_policy_version','download_binding','download_binding_canonical','source_payload','source_payload_canonical',
   'source_inventory_canonical','official_metadata_canonical','download_receipt_sha256','native_upload_limits'])||
   c.version!=='replay-natural-source-contract-v3'||c.download_policy_version!=='youtube-highest-natural-download-v3'||
   b.version!=='youtube-replay-highest-natural-download-v3'||b.job_id!==spec.download_job_id||b.video_id!==spec.source_id||
   b.channel_id!==spec.channel_id||b.duration!==spec.duration_seconds||b.quality_profile!==QUALITY||!SHA.test(b.policy_sha256||'')||
   b.source_payload_sha256!==canonicalBytes(c.source_payload_canonical,payload)||
   !SHA.test(c.download_receipt_sha256||'')||payload.source_api!=='youtube-data-v3-official'||payload.video_id!==spec.source_id||
   payload.url!==spec.source_url||payload.channel_id!==spec.channel_id||raw.id!==spec.source_id||raw.snippet.channelId!==spec.channel_id||
   raw.status.privacyStatus!=='public'||!same(payload._natural_source_plan,p))fail();
  canonicalBytes(c.download_binding_canonical,b);
  const fields=['version','quality_profile','quality_basis','video_id','channel_id','duration','activated_at','observed_at',
   'operating_not_before','source_actual_start_time','source_actual_end_time','official_metadata_sha256','source_inventory','inventory_sha256',
   'selected_formats','highest_tier','source_bytes','planned_output_max_bytes','workspace_overhead_bytes','required_working_bytes',
   'minimum_range_bytes','media_requests_plan','metadata_request_allowance','redirect_resume_request_allowance','max_http_requests',
   'human_review','publication_authorized','plan_grants_admission'];
  const start=Date.parse(raw.liveStreamingDetails.actualStartTime),end=Date.parse(raw.liveStreamingDetails.actualEndTime),activation=Date.parse(p.activated_at),observed=Date.parse(p.observed_at);
  if(!exact(p,fields)||p.version!=='youtube-highest-natural-source-plan-v3'||p.quality_profile!==QUALITY||p.quality_basis!==BASIS||
   p.video_id!==spec.source_id||p.channel_id!==spec.channel_id||p.duration!==spec.duration_seconds||
   p.operating_not_before!==OPERATING_NOT_BEFORE||b.operating_not_before!==OPERATING_NOT_BEFORE||
   ![start,end,activation,observed].every(Number.isFinite)||start<Date.parse(OPERATING_NOT_BEFORE)||start>end||end<activation||end>observed+30000||observed>Date.now()+30000||
   Date.parse(p.source_actual_start_time)!==start||Date.parse(b.source_actual_start_time)!==start||Date.parse(p.source_actual_end_time)!==end||Date.parse(b.activated_at)!==activation||
   p.human_review!==false||p.publication_authorized!==false||p.plan_grants_admission!==false||
   p.official_metadata_sha256!==canonicalBytes(c.official_metadata_canonical,raw)||
   p.inventory_sha256!==canonicalBytes(c.source_inventory_canonical,p.source_inventory)||
   !Array.isArray(p.source_inventory)||!p.source_inventory.length||p.source_inventory.length>300||
   !Array.isArray(p.selected_formats)||p.selected_formats.length!==2)fail();
  const recordFields=['format_id','ext','vcodec','acodec','width','height','fps','filesize','bitrate_kbps','protocol','fragmented','has_drm','dynamic_range'];
  const inventory=p.source_inventory,ids=new Set();
  for(const r of inventory){
   if(!exact(r,recordFields)||typeof r.format_id!=='string'||!/^[A-Za-z0-9_-]{1,64}$/.test(r.format_id)||ids.has(r.format_id)||
    typeof r.vcodec!=='string'||typeof r.acodec!=='string'||typeof r.fragmented!=='boolean'||typeof r.has_drm!=='boolean'||
    r.vcodec!=='none'&&(!Number.isSafeInteger(r.width)||r.width<1||!Number.isSafeInteger(r.height)||r.height<1||!positive(r.fps))||
    r.bitrate_kbps!==null&&!positive(r.bitrate_kbps))fail();ids.add(r.format_id);
  }
  const videos=inventory.filter(r=>r.vcodec!=='none');if(!videos.length)fail();
  const height=Math.max(...videos.map(r=>r.height)),fps=Math.max(...videos.filter(r=>r.height===height).map(r=>r.fps));
  const top=videos.filter(r=>r.height===height&&r.fps===fps);
  if(height>1080||fps>60.1||top.some(r=>r.width>1920||![null,'SDR'].includes(r.dynamic_range))||!same(p.highest_tier,{height,fps}))fail();
  const chosen=[];
  for(const [candidates,ext]of [[top.filter(r=>r.vcodec==='h264'&&r.acodec==='none'),'mp4'],[inventory.filter(r=>r.vcodec==='none'&&r.acodec==='aac'),'m4a']]){
   if(!candidates.length||candidates.some(r=>!positive(r.bitrate_kbps)))fail();
   const best=Math.max(...candidates.map(r=>r.bitrate_kbps)),matches=candidates.filter(r=>r.bitrate_kbps===best);if(matches.length!==1)fail();
   const selected=matches[0];if(selected.ext!==ext||selected.protocol!=='https'||selected.fragmented||selected.has_drm||!Number.isSafeInteger(selected.filesize)||selected.filesize<1)fail();
   chosen.push(Object.fromEntries(['format_id','ext','vcodec','acodec','width','height','fps','filesize'].map(k=>[k,selected[k]])));
  }
  if(!same(chosen,p.selected_formats))fail();
  const total=chosen.reduce((n,r)=>n+r.filesize,0),media=chosen.reduce((n,r)=>n+Math.ceil(r.filesize/9961472),0),resume=Math.max(64,Math.ceil(media/4));
  const expected={source_bytes:total,planned_output_max_bytes:total+64000000,workspace_overhead_bytes:32000000,required_working_bytes:2*total+96000000,
   minimum_range_bytes:9961472,media_requests_plan:media,metadata_request_allowance:64,redirect_resume_request_allowance:resume,max_http_requests:media+64+resume};
  if(Object.entries(expected).some(([k,v])=>!Number.isSafeInteger(p[k])||p[k]!==v)||p.planned_output_max_bytes>14000000000||p.max_http_requests>2048||spec.file_bytes>p.planned_output_max_bytes)fail();
  const l=c.native_upload_limits;if(l.version!=='bilibili-highest-native-limit-binding-v2'||l.site_uid!==spec.target_site_uid||l.max_file_bytes!==16000000000||l.max_duration_seconds!==36000||
   l.server_file_hash_verified!==false||l.human_review!==false||l.fresh_native_uid_and_limit_check_required!==true||!SHA.test(l.binding?.sha256||''))fail();
  return c;
 }catch(e){if(e?.code==='REPLAY_NATURAL_CONTRACT_REQUIRED')throw e;fail();}
}

export async function checkNaturalSourceFiles(root,spec){
 const c=validateNaturalContract(spec),file=path.join(root,'media',spec.download_job_id,'youtube-highest-v3/verified.json'),size=(await fs.stat(file)).size;
 const verified=await readBound(file,{file,bytes:size,sha256:c.download_receipt_sha256},262144),a=verified.source_asset,b=c.download_binding;
 if(verified.version!=='youtube-replay-highest-natural-download-v3'||verified.binding_sha256!==digest(c.download_binding_canonical)||
  verified.human_review!==false||verified.publication_authorized!==false||verified.creator_original_bytes_verified!==false||
  a?.file!==spec.file||a.bytes!==spec.file_bytes||a.sha256!==spec.artifact_sha256||!Number.isFinite(a.duration)||Math.abs(a.duration-spec.duration_seconds)>2||
  !same(a.source_plan,b.source_plan)||!same(a.selected_formats,b.source_plan.selected_formats)||
  !same(verified.receipt?.source_plan,b.source_plan)||!same(verified.receipt?.selected_formats,b.source_plan.selected_formats)||
  verified.receipt?.source_to_merged_packet_coverage_verified!==true)fail();
 const l=c.native_upload_limits,limitFile=path.join(root,'config/replay-highest-native-limits-v2/binding.json'),limit=await readBound(limitFile,l.binding,65536);
 if(limit.site_uid!==spec.target_site_uid||limit.version!==l.version||limit.max_file_bytes!==16000000000||limit.max_duration_seconds!==36000||limit.server_file_hash_verified!==false||limit.human_review!==false)fail();
 return {version:'replay-natural-source-files-rechecked-v3',source_actual_start_time:b.source_actual_start_time,operating_not_before:OPERATING_NOT_BEFORE,
  inventory_sha256:b.source_plan.inventory_sha256,download_receipt_sha256:c.download_receipt_sha256,server_file_hash_verified:false,human_review:false};
}
