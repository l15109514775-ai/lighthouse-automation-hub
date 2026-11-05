// One recovered editor, one root-issued coordinator reservation, one intent.
// This module has no browser, account credentials, network or native click.
import fs from 'node:fs/promises';
import {constants} from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';

const TICKETS='/run/short-video-submission';
const UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const SHA=/^[a-f0-9]{64}$/;
const FIELDS=['job_id','attempt_id','recovery_id','target_site_uid','source_url','source_id','download_job_id',
  'file','file_bytes','artifact_sha256','metadata_sha256','upload_metadata_sha256','binding_sha256',
  'metadata_revision_id','upload_reference_sha256'];
function fail(code){throw Object.assign(new Error(code),{code});}
function requireThat(value,code){if(!value)fail(code);}
function canonical(value){
  if(Array.isArray(value))return '['+value.map(canonical).join(',')+']';
  if(value!==null&&typeof value==='object')return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+canonical(value[k])).join(',')+'}';
  return JSON.stringify(value);
}
function hash(bytes){return createHash('sha256').update(bytes).digest('hex');}
function keys(value,expected){return value!==null&&typeof value==='object'&&!Array.isArray(value)&&canonical(Object.keys(value).sort())===canonical([...expected].sort());}
function text(value){return typeof value==='string'&&value.trim().length>0&&value.length<=2048;}

// Pure validation is exported for owned, offline fixtures. Production admission
// below always reads root-owned readonly tickets itself, without test overrides.
export function validateRecoveredTickets({spec,approval,reservation,approval_sha256,now=Math.floor(Date.now()/1000)}){
  requireThat(spec&&typeof spec==='object'&&!Array.isArray(spec),'RECOVERED_SUBMIT_SPEC_INVALID');
  for(const key of ['job_id','attempt_id','recovery_id','download_job_id','metadata_revision_id'])
    requireThat(typeof spec[key]==='string'&&UUID.test(spec[key]),'RECOVERED_SUBMIT_SPEC_INVALID');
  requireThat(spec.uploaded_attempt_id===spec.attempt_id&&typeof spec.target_site_uid==='string'&&/^\d{1,30}$/.test(spec.target_site_uid)&&
    typeof spec.source_id==='string'&&/^[A-Za-z0-9_-]{11}$/.test(spec.source_id)&&spec.source_url==='https://www.youtube.com/watch?v='+spec.source_id&&
    Number.isSafeInteger(spec.file_bytes)&&spec.file_bytes>0&&spec.file_bytes<=100000000&&
    typeof spec.file==='string'&&path.isAbsolute(spec.file),'RECOVERED_SUBMIT_SPEC_INVALID');
  for(const key of ['artifact_sha256','metadata_sha256','upload_metadata_sha256','binding_sha256','upload_reference_sha256'])
    requireThat(typeof spec[key]==='string'&&SHA.test(spec[key]),'RECOVERED_SUBMIT_SPEC_INVALID');
  requireThat(spec.metadata&&hash(canonical(spec.metadata))===spec.metadata_sha256&&spec.metadata.source===spec.source_url,
    'RECOVERED_SUBMIT_METADATA_MISMATCH');
  const binding=Object.fromEntries(FIELDS.map(key=>[key,spec[key]]));
  requireThat(keys(approval,['version','ticket_id','issued_at_unix','expires_at_unix','binding','owner_acceptance'])&&
    approval.version==='short-video-recovered-owner-approval-v1'&&UUID.test(approval.ticket_id||''),
    'RECOVERED_SUBMIT_APPROVAL_REQUIRED');
  requireThat(keys(reservation,['version','ticket_id','issued_at_unix','expires_at_unix','binding','approval_sha256','reservation','persisted_state'])&&
    reservation.version==='short-video-recovered-submit-reservation-v1','RECOVERED_SUBMIT_RESERVATION_REQUIRED');
  requireThat(canonical(approval.binding)===canonical(binding)&&canonical(reservation.binding)===canonical(binding)&&
    reservation.ticket_id===approval.ticket_id&&reservation.approval_sha256===approval_sha256&&SHA.test(approval_sha256||''),
    'RECOVERED_SUBMIT_TICKET_BINDING_MISMATCH');
  requireThat(Number.isSafeInteger(now)&&Number.isSafeInteger(approval.issued_at_unix)&&Number.isSafeInteger(approval.expires_at_unix)&&
    approval.issued_at_unix<=now&&now<approval.expires_at_unix&&approval.expires_at_unix-approval.issued_at_unix>0&&
    approval.expires_at_unix-approval.issued_at_unix<=900&&reservation.issued_at_unix===approval.issued_at_unix&&
    reservation.expires_at_unix===approval.expires_at_unix,'RECOVERED_SUBMIT_TICKET_EXPIRED');
  const owner=approval.owner_acceptance;
  requireThat(keys(owner,['version','binding_sha256','actual_owner_response','approval_reference','recorded_at',
    'cryptographically_signed_by_owner','human_review','automatic_other_video_publication_authorized'])&&
    owner.version==='operator-transcribed-short-video-owner-acceptance-v1'&&owner.binding_sha256===spec.binding_sha256&&
    text(owner.actual_owner_response)&&text(owner.approval_reference)&&typeof owner.recorded_at==='string'&&
    Number.isFinite(Date.parse(owner.recorded_at))&&Date.parse(owner.recorded_at)<=approval.issued_at_unix*1000+1000&&
    owner.cryptographically_signed_by_owner===false&&owner.human_review===false&&
    owner.automatic_other_video_publication_authorized===false,'RECOVERED_SUBMIT_OWNER_ACCEPTANCE_REQUIRED');
  const permit=reservation.reservation;
  const key=hash(canonical(['bilibili','youtube',spec.source_id,'site_uid:'+spec.target_site_uid,'video_website_short_v1']));
  requireThat(keys(permit,['can_click_submit','state','idempotency_key'])&&permit.can_click_submit===true&&
    permit.state==='dispatching'&&permit.idempotency_key===key,'RECOVERED_SUBMIT_COORDINATOR_RESERVATION_REQUIRED');
  requireThat(canonical(reservation.persisted_state)===canonical({job_state:'publish_unknown',pipeline_state:'submitting',
    publication_state:'dispatching',audit_action:'short_video_reserve_submit',principal:'operator:ssh-sudo'}),
    'RECOVERED_SUBMIT_COORDINATOR_RESERVATION_REQUIRED');
  return Object.freeze({ticket_id:approval.ticket_id,binding,expires_at_unix:approval.expires_at_unix});
}

async function noSymlinks(file){
  for(let current=file;current!==path.dirname(current);current=path.dirname(current))
    requireThat(!(await fs.lstat(current)).isSymbolicLink(),'RECOVERED_SUBMIT_PATH_INVALID');
  requireThat(await fs.realpath(file)===file,'RECOVERED_SUBMIT_PATH_INVALID');
}
async function rootProtected(file){
  await noSymlinks(file);
  for(let current=file;;current=path.dirname(current)){
    const stat=await fs.lstat(current);
    requireThat(stat.uid===0&&!(stat.mode&0o022),'RECOVERED_SUBMIT_TICKETS_NOT_ROOT_OWNED');
    if(current===path.dirname(current))break;
  }
}
async function rootJson(file){
  await rootProtected(file);
  const fd=await fs.open(file,constants.O_RDONLY|(constants.O_NOFOLLOW||0));
  try{
    const before=await fd.stat();
    requireThat(before.isFile()&&before.uid===0&&!(before.mode&0o022)&&before.size>0&&before.size<=65536,
      'RECOVERED_SUBMIT_TICKET_FILE_INVALID');
    const raw=await fd.readFile(),after=await fd.stat();
    requireThat(raw.length===before.size&&['dev','ino','size','mtimeMs','ctimeMs'].every(k=>before[k]===after[k]),
      'RECOVERED_SUBMIT_TICKET_CHANGED');
    try{return {value:JSON.parse(raw.toString('utf8')),sha256:hash(raw)};}
    catch{fail('RECOVERED_SUBMIT_TICKET_JSON_INVALID');}
  }finally{await fd.close();}
}
async function artifact(spec,root){
  requireThat(spec.file===path.join(root,'media',spec.download_job_id,'youtube-download-v1/original.mp4'),
    'RECOVERED_SUBMIT_ARTIFACT_PATH_INVALID');
  await noSymlinks(spec.file);
  const fd=await fs.open(spec.file,constants.O_RDONLY|(constants.O_NOFOLLOW||0));
  try{
    const before=await fd.stat();
    requireThat(before.isFile()&&before.size===spec.file_bytes,'RECOVERED_SUBMIT_ARTIFACT_CHANGED');
    const digest=createHash('sha256'),buffer=Buffer.alloc(65536);let total=0;
    for(;;){const {bytesRead}=await fd.read(buffer,0,buffer.length,null);if(!bytesRead)break;
      total+=bytesRead;requireThat(total<=spec.file_bytes,'RECOVERED_SUBMIT_ARTIFACT_CHANGED');digest.update(buffer.subarray(0,bytesRead));}
    const after=await fd.stat();
    requireThat(total===spec.file_bytes&&digest.digest('hex')===spec.artifact_sha256&&
      ['dev','ino','size','mtimeMs','ctimeMs'].every(k=>before[k]===after[k]),'RECOVERED_SUBMIT_ARTIFACT_CHANGED');
  }finally{await fd.close();}
}

export async function admitRecoveredSubmit({root,spec,output,command_id}){
  try{
    requireThat(process.platform==='linux'&&process.getuid?.()===10001,'RECOVERED_SUBMIT_APP_IDENTITY_REQUIRED');
    requireThat(typeof root==='string'&&path.isAbsolute(root)&&path.resolve(root)===root&&UUID.test(command_id||''),
      'RECOVERED_SUBMIT_SPEC_INVALID');
    await rootProtected(TICKETS);
    const mounts=await fs.readFile('/proc/self/mountinfo','utf8');
    requireThat(mounts.split('\n').some(line=>{const parts=line.split(' ');return parts[4]===TICKETS&&parts[5]?.split(',').includes('ro');}),
      'RECOVERED_SUBMIT_READONLY_TICKETS_REQUIRED');
    const approval=await rootJson(path.join(TICKETS,'approval.json'));
    const reservation=await rootJson(path.join(TICKETS,'reservation.json'));
    const verified=validateRecoveredTickets({spec,approval:approval.value,reservation:reservation.value,approval_sha256:approval.sha256});
    const job=path.join(root,'media',spec.job_id);
    requireThat(output===path.join(job,'recovered-editors',spec.recovery_id),'RECOVERED_SUBMIT_OUTPUT_INVALID');
    await noSymlinks(output);
    const st=await fs.lstat(job),out=await fs.lstat(output);
    requireThat(st.isDirectory()&&out.isDirectory()&&st.uid===10001&&out.uid===10001&&!(st.mode&0o077)&&!(out.mode&0o077),
      'RECOVERED_SUBMIT_OUTPUT_INVALID');
    await artifact(spec,root);
    const intent=path.join(job,'recovered-submit-intent.json');
    const value={version:'recovered-short-video-submit-intent-v1',state:'submit_reserved_result_unknown',
      ticket_id:verified.ticket_id,command_id,binding:verified.binding,created_at:new Date().toISOString(),
      approval_sha256:approval.sha256,reservation_sha256:reservation.sha256,reconcile_before_retry:true,
      native_click_performed:'unknown',new_file_selection:false,human_review:false};
    let fd;
    try{fd=await fs.open(intent,'wx',0o600);}
    catch(error){if(error.code==='EEXIST')fail('RECOVERED_SUBMIT_ALREADY_CONSUMED');throw error;}
    try{await fd.writeFile(JSON.stringify(value,null,2));await fd.sync();}finally{await fd.close();}
    const directory=await fs.open(job,constants.O_RDONLY|(constants.O_DIRECTORY||0));
    try{await directory.sync();}finally{await directory.close();}
    // No failure path removes an intent, even if no click was reached.
    return Object.freeze({allowed:true,intent_file:intent,ticket_id:verified.ticket_id,reconcile_before_retry:true});
  }catch(error){
    if(typeof error?.code==='string'&&error.code.startsWith('RECOVERED_SUBMIT_'))throw error;
    fail('RECOVERED_SUBMIT_GATE_UNAVAILABLE');
  }
}
