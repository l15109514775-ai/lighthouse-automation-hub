// Explicit V5 delivery source, never an invented youtube-download-v2 envelope.
import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {canonicalDigest} from '../scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs';
const SHA=/^[a-f0-9]{64}$/,UUID=/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/;
const CUTOFF='2026-10-31T16:00:00Z';
function fail(code){throw Object.assign(new Error(code),{code});}
function numeric(value){return typeof value==='number'&&Number.isFinite(value);}
function descriptor(x){return x&&Number.isSafeInteger(x.index)&&x.index>=0&&typeof x.file==='string'&&path.isAbsolute(x.file)&&Number.isSafeInteger(x.bytes)&&x.bytes>0&&SHA.test(x.sha256||'')&&numeric(x.duration)&&x.duration>0&&SHA.test(x.part_idempotency_key||'');}
export function validatePartContract(s){
 const c=s?.source_contract;
 if(s?.version!=='replay-part-native-browser-manifest-v5'||!c||c.version!=='replay-part-source-contract-v5'||
  !UUID.test(s.parent_job_id||'')||s.parent_job_id===s.job_id||!Number.isSafeInteger(s.part_index)||!Number.isSafeInteger(s.part_count)||s.part_count<1||s.part_count>32||s.part_index<0||s.part_index>=s.part_count||
  !SHA.test(s.part_idempotency_key||'')||!SHA.test(s.whole_plan_sha256||'')||!SHA.test(s.parts_manifest_sha256||'')||c.download_job_id!==s.download_job_id||
  c.whole_plan_sha256!==s.whole_plan_sha256||!SHA.test(c.source_sha256||'')||!Number.isSafeInteger(c.source_bytes)||c.source_bytes<1||
  !numeric(c.source_duration_seconds)||c.source_duration_seconds<=0||c.source_duration_seconds!==s.source_duration_seconds||
  c.operating_not_before!==CUTOFF||!numeric(Date.parse(c.source_actual_start_time))||Date.parse(c.source_actual_start_time)<Date.parse(CUTOFF)||
  !['single_whole_passthrough_v5','lossless_segmented_v4'].includes(c.processing_model)||!descriptor(c.output)||
  c.output.index!==s.part_index||c.output.file!==s.file||c.output.bytes!==s.file_bytes||c.output.sha256!==s.artifact_sha256||c.output.duration!==s.duration_seconds||
  s.part_idempotency_key!==canonicalDigest(['bilibili','youtube',s.source_id,'site_uid:'+s.target_site_uid,s.parts_manifest_sha256,s.part_index,s.artifact_sha256,'video_website_replay_part_v5_p'+String(s.part_index+1).padStart(2,'0')])||
  !c.delivery_manifest||c.delivery_manifest.sha256!==s.parts_manifest_sha256||canonicalDigest(c.delivery_manifest)!==canonicalDigest(s.processing_manifest)||
  typeof s.account_name!=='string'||!s.account_name.trim())fail('REPLAY_PART_CONTRACT_INVALID');
 const fits=c.source_bytes<=14000000000&&c.source_duration_seconds<=36000;
 if((c.processing_model==='single_whole_passthrough_v5')!==fits||fits&&(s.part_count!==1||s.part_index!==0||s.file_bytes!==c.source_bytes||s.artifact_sha256!==c.source_sha256||s.duration_seconds!==c.source_duration_seconds)||
  !fits&&(s.file_bytes>14000000000||s.duration_seconds>36000))fail('REPLAY_PART_UNNECESSARY_SPLIT_OR_LIMIT');
 return c;
}
export function canonicalPartFile(root,s){const c=validatePartContract(s),area=path.join(root,'media',s.download_job_id,'youtube-whole-v4');return c.processing_model==='single_whole_passthrough_v5'?path.join(area,'original.mp4'):path.join(area,'parts-v4',`part-${String(s.part_index).padStart(3,'0')}.mp4`);}
export function validatePartReservation(ticket,s,binding,prepared){
 const now=Date.now()/1000,observed=Date.parse(prepared?.observed_at)/1000;
 if(!ticket||ticket.version!=='replay-part-native-reservation-v5'||Object.entries(binding).some(([k,v])=>ticket[k]!==v)||
  ticket.idempotency_key!==s.part_idempotency_key||!UUID.test(ticket.ticket_id||'')||ticket.upload_reference_sha256!==prepared.upload_reference_sha256||!SHA.test(ticket.machine_evidence_sha256||'')||ticket.can_click_submit!==true||ticket.state!=='dispatching'||
  !numeric(ticket.issued_at)||!numeric(ticket.expires_at)||ticket.issued_at>now||ticket.expires_at<=now||ticket.expires_at-ticket.issued_at>300||
  !numeric(observed)||now-observed<0||now-observed>300||prepared.version!=='replay-part-native-preparation-v5'||
  prepared.binding_sha256!==s.binding_sha256||prepared.private_draft_saved!==true||prepared.metadata_verified!==true||prepared.cover_applied!==true)fail('REPLAY_PART_RESERVATION_REQUIRED');
 return ticket;
}
async function safe(file){if(!path.isAbsolute(file)||path.resolve(file)!==file)fail('REPLAY_PART_PATH_INVALID');for(let p=file;p!==path.dirname(p);p=path.dirname(p))if((await fs.lstat(p)).isSymbolicLink())fail('REPLAY_PART_PATH_INVALID');if(await fs.realpath(file)!==file)fail('REPLAY_PART_PATH_INVALID');}
async function checkedDocument(reference,expected,max=2000000){
 if(!reference||Object.keys(reference).sort().join()!=='bytes,file,sha256'||reference.file!==expected||!Number.isSafeInteger(reference.bytes)||reference.bytes<1||reference.bytes>max||!SHA.test(reference.sha256||''))fail('REPLAY_PART_DOCUMENT_INVALID');
 await safe(expected);const h=await fs.open(expected,'r');let raw;
 try{const before=await h.stat();if(!before.isFile()||before.size!==reference.bytes)fail('REPLAY_PART_DOCUMENT_CHANGED');raw=await h.readFile();const after=await h.stat();if(raw.length!==before.size||['size','mtimeMs','ctimeMs','dev','ino'].some(k=>before[k]!==after[k])||createHash('sha256').update(raw).digest('hex')!==reference.sha256)fail('REPLAY_PART_DOCUMENT_CHANGED');}finally{await h.close();}
 try{return JSON.parse(raw);}catch{fail('REPLAY_PART_DOCUMENT_INVALID');}
}
function packetShape(x){return x&&Object.keys(x).sort().join()==='audio,video'&&['audio','video'].every(k=>x[k]&&Number.isSafeInteger(x[k].packets)&&x[k].packets>=0&&SHA.test(x[k].chain||''));}
export async function checkPartSourceFiles(root,s){
 const c=validatePartContract(s);root=path.resolve(root);const area=path.join(root,'media',s.download_job_id,'youtube-whole-v4');
 if(s.file!==canonicalPartFile(root,s))fail('REPLAY_PART_CANONICAL_FILE_CHANGED');
 const delivery=await checkedDocument(c.delivery_manifest,path.join(area,'delivery-v5','manifest.json'));
 const verified=await checkedDocument(c.download_receipt,path.join(area,'verified.json'));
 const original=delivery.source;
 if(delivery.version!=='replay-parts-delivery-manifest-v5'||delivery.processing_model!==c.processing_model||delivery.whole_plan_sha256!==s.whole_plan_sha256||
  original?.file!==path.join(area,'original.mp4')||original.bytes!==c.source_bytes||original.sha256!==c.source_sha256||original.duration!==c.source_duration_seconds||!packetShape(original.packet_evidence)||
  !Array.isArray(delivery.outputs)||delivery.outputs.length!==s.part_count||!Array.isArray(delivery.output_packet_coverage)||delivery.output_packet_coverage.length!==s.part_count||
  canonicalDigest(delivery.outputs[s.part_index])!==canonicalDigest(c.output)||verified.version!=='youtube-whole-replay-download-v4'||
  verified.source_asset?.file!==original.file||verified.source_asset.bytes!==original.bytes||verified.source_asset.sha256!==original.sha256||verified.source_asset.duration!==original.duration||
  canonicalDigest(verified.source_asset.packet_evidence)!==canonicalDigest(original.packet_evidence)||verified.receipt?.source_to_merged_packet_coverage_verified!==true||
  verified.human_review!==false||verified.publication_authorized!==false||verified.creator_original_bytes_verified!==false)fail('REPLAY_PART_DELIVERY_BINDING_CHANGED');
 let combined={video:{packets:0,chain:'0'.repeat(64)},audio:{packets:0,chain:'0'.repeat(64)}},duration=0;
 const files=new Set(),keys=new Set();
 for(let i=0;i<delivery.outputs.length;i++){
  const output=delivery.outputs[i],packet=delivery.output_packet_coverage[i];
  const expected=c.processing_model==='single_whole_passthrough_v5'?original.file:path.join(area,'parts-v4',`part-${String(i).padStart(3,'0')}.mp4`);
  if(!descriptor(output)||output.index!==i||output.file!==expected||files.has(output.file)||keys.has(output.part_idempotency_key)||
   packet?.index!==i||!packetShape(packet.packet_before)||!packetShape(packet.packet_after)||canonicalDigest(packet.packet_before)!==canonicalDigest(combined)||
   ['audio','video'].some(k=>packet.packet_after[k].packets<packet.packet_before[k].packets)||
   c.processing_model==='lossless_segmented_v4'&&(output.bytes>14000000000||output.duration>36000))fail('REPLAY_PART_PACKET_CHAIN_CHANGED');
  duration+=output.duration;combined=packet.packet_after;files.add(output.file);keys.add(output.part_idempotency_key);
 }
 if(canonicalDigest(combined)!==canonicalDigest(original.packet_evidence)||Math.abs(duration-c.source_duration_seconds)>2)fail('REPLAY_PART_WHOLE_COVERAGE_CHANGED');
 await safe(s.file);const st=await fs.stat(s.file);if(!st.isFile()||st.size!==s.file_bytes)fail('REPLAY_PART_FILE_SIZE_CHANGED');
 // The native driver streamCheckedFile and parent machine QA perform the
 // actual current selected-file SHA. This small-ref check never pretends its
 // declared source SHA was freshly re-read, nor repeatedly scans the whole.
 return {delivery_manifest:c.delivery_manifest,download_receipt:c.download_receipt,selected_output:c.output,source_file_rehashed:false};
}
