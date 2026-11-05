// Owned VM-linked filesystem fixtures: no live account, browser or network.
import fs from 'node:fs/promises';
import {constants} from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import {createHash,randomUUID} from 'node:crypto';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
const folder=path.resolve(process.argv[2]),scenario=process.argv[3];
await fs.mkdir(folder,{recursive:true});
const root=path.join(folder,'hub'),tickets=path.join(folder,'owned-root-tickets');
const job=randomUUID(),attempt=randomUUID(),recovery=randomUUID(),download=randomUUID();
const output=path.join(root,'media',job,'recovered-editors',recovery),jobdir=path.join(root,'media',job);
await fs.mkdir(output,{recursive:true});await fs.mkdir(tickets,{recursive:true});
const file=path.join(root,'media',download,'youtube-download-v1/original.mp4');
await fs.mkdir(path.dirname(file),{recursive:true});
const bytes=Buffer.from('Owned synthetic artifact, never a real source or upload.');
await fs.writeFile(file,bytes);
const sha=x=>createHash('sha256').update(x).digest('hex');
function canon(v){return Array.isArray(v)?'['+v.map(canon).join(',')+']':v!==null&&typeof v==='object'?'{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+canon(v[k])).join(',')+'}':JSON.stringify(v);}
const source='https://www.youtube.com/watch?v=abcdefghijk';
const metadata={title:'自有离线夹具',description:'Owned fixture '+source,tags:['owned'],category_label:'娱乐',copyright:2,source};
let spec={job_id:job,attempt_id:attempt,uploaded_attempt_id:attempt,recovery_id:recovery,download_job_id:download,
  source_id:'abcdefghijk',source_url:source,file,file_bytes:bytes.length,artifact_sha256:sha(bytes),metadata,
  target_site_uid:'123456',metadata_sha256:sha(canon(metadata)),upload_metadata_sha256:'1'.repeat(64),
  binding_sha256:'2'.repeat(64),metadata_revision_id:randomUUID(),upload_reference_sha256:'3'.repeat(64)};
const FIELDS=['job_id','attempt_id','recovery_id','target_site_uid','source_url','source_id','download_job_id',
  'file','file_bytes','artifact_sha256','metadata_sha256','upload_metadata_sha256','binding_sha256',
  'metadata_revision_id','upload_reference_sha256'];
const now=Math.floor(Date.now()/1000);
let approval,reservation;
function prepare(){
  const common={ticket_id:randomUUID(),issued_at_unix:now-1,expires_at_unix:now+600,
    binding:Object.fromEntries(FIELDS.map(k=>[k,spec[k]]))};
  approval={...structuredClone(common),version:'short-video-recovered-owner-approval-v1',owner_acceptance:{
    version:'operator-transcribed-short-video-owner-acceptance-v1',binding_sha256:spec.binding_sha256,
    actual_owner_response:'Owned fixture only, not a real owner acceptance',approval_reference:'owned fixture',
    recorded_at:new Date((now-2)*1000).toISOString(),cryptographically_signed_by_owner:false,human_review:false,
    automatic_other_video_publication_authorized:false}};
  reservation={...structuredClone(common),version:'short-video-recovered-submit-reservation-v1',
    approval_sha256:sha(JSON.stringify(approval)),reservation:{can_click_submit:true,state:'dispatching',
      idempotency_key:sha(canon(['bilibili','youtube',spec.source_id,'site_uid:'+spec.target_site_uid,'video_website_short_v1']))},
    persisted_state:{job_state:'publish_unknown',pipeline_state:'submitting',publication_state:'dispatching',
      audit_action:'short_video_reserve_submit',principal:'operator:ssh-sudo'}};
}
prepare();
const expected={
  expired:'RECOVERED_SUBMIT_TICKET_EXPIRED',missing_approval:'RECOVERED_SUBMIT_GATE_UNAVAILABLE',
  wrong_account:'RECOVERED_SUBMIT_TICKET_BINDING_MISMATCH',wrong_attempt:'RECOVERED_SUBMIT_TICKET_BINDING_MISMATCH',
  wrong_revision:'RECOVERED_SUBMIT_TICKET_BINDING_MISMATCH',wrong_metadata:'RECOVERED_SUBMIT_METADATA_MISMATCH',
  wrong_uploadref:'RECOVERED_SUBMIT_TICKET_BINDING_MISMATCH',reservation_denied:'RECOVERED_SUBMIT_COORDINATOR_RESERVATION_REQUIRED',
  stale_approval_hash:'RECOVERED_SUBMIT_TICKET_BINDING_MISMATCH',untrusted_owner:'RECOVERED_SUBMIT_TICKETS_NOT_ROOT_OWNED',
  writable_mount:'RECOVERED_SUBMIT_READONLY_TICKETS_REQUIRED',symlink_ticket:'RECOVERED_SUBMIT_PATH_INVALID',
  artifact_changed:'RECOVERED_SUBMIT_ARTIFACT_CHANGED',unsafe_output:'RECOVERED_SUBMIT_OUTPUT_INVALID',
  fsync_fail:'RECOVERED_SUBMIT_GATE_UNAVAILABLE'};
if(scenario==='expired'){approval.issued_at_unix=now-100;approval.expires_at_unix=now-1;
  reservation.issued_at_unix=approval.issued_at_unix;reservation.expires_at_unix=approval.expires_at_unix;}
if(scenario==='wrong_account')approval.binding.target_site_uid='999';
if(scenario==='wrong_attempt')reservation.binding.attempt_id=randomUUID();
if(scenario==='wrong_revision')approval.binding.metadata_revision_id=randomUUID();
if(scenario==='wrong_metadata')spec.metadata.title='Changed text';
if(scenario==='wrong_uploadref')reservation.binding.upload_reference_sha256='0'.repeat(64);
if(scenario==='reservation_denied')reservation.reservation.can_click_submit=false;
if(scenario!=='stale_approval_hash')reservation.approval_sha256=sha(JSON.stringify(approval));
else reservation.approval_sha256='0'.repeat(64);
if(scenario==='artifact_changed')await fs.writeFile(file,Buffer.alloc(bytes.length,9));
async function persist(){
  await fs.writeFile(path.join(tickets,'approval.json'),JSON.stringify(approval));
  await fs.writeFile(path.join(tickets,'reservation.json'),JSON.stringify(reservation));
}
await persist();
if(scenario==='missing_approval')await fs.unlink(path.join(tickets,'approval.json'));
const events=[];
const normalized=p=>String(p).replaceAll('\\','/');
const isTicket=p=>normalized(p).startsWith('/run/short-video-submission');
const ticketPath=p=>path.join(tickets,path.basename(p));
function statView(s,{uid=10001,mode=0o700,symlink=false}={}){
  return {...s,uid,mode,isFile:()=>s.isFile(),isDirectory:()=>s.isDirectory(),isSymbolicLink:()=>symlink||s.isSymbolicLink()};
}
async function fakeStat(p){
  const normalizedPath=normalized(p);
  if(normalizedPath==='/'||normalizedPath==='/run'||normalizedPath==='/run/short-video-submission')
    return {uid:0,mode:0o755,isFile:()=>false,isDirectory:()=>true,isSymbolicLink:()=>false};
  if(isTicket(p))return statView(await fs.lstat(ticketPath(p)),{uid:scenario==='untrusted_owner'?10001:0,mode:0o640,
    symlink:scenario==='symlink_ticket'&&path.basename(p)==='approval.json'});
  return statView(await fs.lstat(p));
}
const mockfs={
  lstat:fakeStat,
  realpath:async p=>isTicket(p)||normalized(p)==='/run'||normalized(p)==='/'?p:fs.realpath(p),
  readFile:async(p,...args)=>p==='/proc/self/mountinfo'?
    '42 21 0:1 / /run/short-video-submission '+(scenario==='writable_mount'?'rw':'ro')+',relatime - tmpfs tmpfs ro\n':
    fs.readFile(isTicket(p)?ticketPath(p):p,...args),
  open:async(p,flags,mode)=>{
    if(p===jobdir)return {sync:async()=>{events.push('parent-sync');if(scenario==='fsync_fail')throw new Error('Owned fsync failure');},
      close:async()=>{}};
    const actual=isTicket(p)?ticketPath(p):p,h=await fs.open(actual,flags,mode);
    return {stat:async()=>statView(await h.stat(),{uid:isTicket(p)?0:10001,mode:isTicket(p)?0o640:0o600}),
      readFile:(...a)=>h.readFile(...a),read:(...a)=>h.read(...a),
      writeFile:async(...a)=>{events.push('intent-write');return h.writeFile(...a);},
      sync:async()=>{events.push('file-sync');return h.sync();},close:()=>h.close()};
  }};
const context=vm.createContext({Buffer,console,process:{platform:'linux',getuid:()=>10001}});
const gatefile=path.join(path.dirname(fileURLToPath(import.meta.url)),'../adapters/recovered_submit_gate.mjs');
const module=new vm.SourceTextModule(await fs.readFile(gatefile,'utf8'),{context,identifier:gatefile});
await module.link(async name=>{
  const values=name==='node:fs/promises'?{default:mockfs}:name==='node:fs'?{constants}:
    name==='node:path'?{default:path}:name==='node:crypto'?{createHash}:null;
  assert.ok(values,'Unexpected gate dependency');
  return new vm.SyntheticModule(Object.keys(values),function(){for(const [k,v] of Object.entries(values))this.setExport(k,v);},{context});
});
await module.evaluate();
const admit=module.namespace.admitRecoveredSubmit,intent=path.join(jobdir,'recovered-submit-intent.json');
const args=()=>({root,spec,output:scenario==='unsafe_output'?jobdir:
  path.join(jobdir,'recovered-editors',spec.recovery_id),command_id:randomUUID()});
if(scenario==='concurrent_once'){
  const results=await Promise.allSettled([admit(args()),admit(args())]);
  assert.equal(results.filter(x=>x.status==='fulfilled').length,1);
  assert.equal(results.find(x=>x.status==='rejected').reason.code,'RECOVERED_SUBMIT_ALREADY_CONSUMED');
}else if(scenario==='normal_once'||scenario==='replay_new_recovery'){
  assert.equal((await admit(args())).allowed,true);
  if(scenario==='replay_new_recovery'){
    spec={...spec,recovery_id:randomUUID()};
    await fs.mkdir(path.join(jobdir,'recovered-editors',spec.recovery_id));
    prepare();await persist();
  }
  await assert.rejects(()=>admit(args()),e=>e.code==='RECOVERED_SUBMIT_ALREADY_CONSUMED');
}else{
  assert.ok(expected[scenario],'Unknown fixture scenario');
  await assert.rejects(()=>admit(args()),e=>e.code===expected[scenario]);
  if(scenario==='fsync_fail')await assert.rejects(()=>admit(args()),e=>e.code==='RECOVERED_SUBMIT_ALREADY_CONSUMED');
}
const exists=await fs.stat(intent).then(()=>true,()=>false);
const consumed=['normal_once','concurrent_once','replay_new_recovery','fsync_fail'].includes(scenario);
assert.equal(exists,consumed);
if(consumed){
  const body=JSON.parse(await fs.readFile(intent,'utf8'));
  assert.equal(body.binding.job_id,job);assert.equal(body.binding.attempt_id,attempt);
  assert.equal(body.state,'submit_reserved_result_unknown');assert.equal(body.reconcile_before_retry,true);
  assert.equal(events.filter(x=>x==='intent-write').length,1);
  assert.ok(events.indexOf('file-sync')<events.indexOf('parent-sync'));
}
console.log(JSON.stringify({scenario,passed:true,real_network_calls:0,native_clicks:0,intent_consumed:consumed}));
