// Owned data only: exact policy folding and independently rehashed forgeries.
import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
import {validateShortMetadataRevisionChain as validate,metadataChainDigest as hash,PUBLIC_PERMISSION_LINE as line} from '../adapters/short_video_metadata_chain.mjs';
const originalMetadata={title:'Owned short replay',description:'Owned source\nhttps://www.youtube.com/watch?v=abcdefghijk\n'+line,source:'https://www.youtube.com/watch?v=abcdefghijk',copyright:2,category_label:'虚拟UP主',tags:['owned']};
function owned(){
 const attempt=randomUUID(),source=randomUUID();let binding={version:'owned',download_job_id:source,source_id:'abcdefghijk',source_url:originalMetadata.source,artifact_sha256:'a'.repeat(64),file:'/owned/original.mp4',file_bytes:123,site_uid:'123456',human_review:false,metadata:originalMetadata,metadata_sha256:hash(originalMetadata)};
 const category={version:'short-video-category-metadata-revision-v1',revision_id:randomUUID(),attempt_id:attempt,previous_binding:binding,previous_binding_sha256:hash(binding),previous_metadata_sha256:hash(originalMetadata),changed_fields:['category_label'],previous_category_label:'虚拟UP主',new_category_label:'娱乐',public_submit:false,remote_metadata_synchronized:false};
 const first={...binding,metadata:{...originalMetadata,category_label:'娱乐'}};first.metadata_sha256=hash(first.metadata);category.new_metadata_sha256=first.metadata_sha256;category.new_binding_sha256=hash(first);
 const updated={...first,metadata:{...first.metadata,description:first.metadata.description.slice(0,-('\n'+line).length)}};updated.metadata_sha256=hash(updated.metadata);
 const removal={version:'public-permission-line-removal-v1',revision_id:randomUUID(),previous_binding:first,previous_binding_sha256:hash(first),previous_metadata_sha256:first.metadata_sha256,new_metadata_sha256:updated.metadata_sha256,new_binding_sha256:hash(updated),changed_fields:['description'],removed_last_line:line,previous_revision_id:category.revision_id,attempt_id:attempt,actual_owner_response:'转载许可编号删掉',owner_response_reference:'Owned explicit fixture',actual_operator_reference:'Owned fixture',recorded_at:new Date().toISOString(),public_submit:false,remote_metadata_synchronized:false,internal_rights_records_preserved:true};
 const spec={...updated,metadata:updated.metadata,attempt_id:attempt,target_site_uid:binding.site_uid,upload_metadata_sha256:hash(originalMetadata),metadata_sha256:updated.metadata_sha256,binding_sha256:hash(updated),metadata_revision_history:[category,removal],metadata_revision:removal,metadata_revision_id:removal.revision_id};
 return structuredClone({spec,originalMetadata,originalSpec:{metadata_sha256:hash(originalMetadata)}});
}
function coherentMetadata(input,change){const r=input.spec.metadata_revision_history[1],meta={...input.spec.metadata,...change},binding={...r.previous_binding,metadata:meta,metadata_sha256:hash(meta)};r.new_metadata_sha256=hash(meta);r.new_binding_sha256=hash(binding);input.spec.metadata=meta;input.spec.metadata_sha256=hash(meta);input.spec.binding_sha256=hash(binding);}
const results=[];
for(const scenario of ['two_revisions','single_full','single_legacy_minimal','unrevised','coherent_title_change','coherent_extra_description','different_line','changed_first_history','wrong_previous_binding','missing_internal_rights','fake_public_approval','wrong_attempt','wrong_upload_hash','missing_history','reordered_history','crlf_suffix']){
 const input=owned();let success=['two_revisions','single_full','single_legacy_minimal','unrevised'].includes(scenario);
 if(scenario==='single_full'||scenario==='single_legacy_minimal'){
  const r=input.spec.metadata_revision_history[0];input.spec.metadata_revision_history=[r];input.spec.metadata_revision=r;input.spec.metadata_revision_id=r.revision_id;
  // Restore the authentic original binding before changing only its category.
  input.spec.metadata={...input.originalMetadata,category_label:'娱乐'};input.spec.metadata_sha256=hash(input.spec.metadata);input.spec.binding_sha256=r.new_binding_sha256;
  if(scenario==='single_legacy_minimal'){delete input.spec.metadata_revision_history;r.previous_binding={metadata:input.originalMetadata};}
 }else if(scenario==='unrevised'){input.spec.metadata=input.originalMetadata;input.spec.metadata_sha256=input.spec.upload_metadata_sha256;delete input.spec.metadata_revision_history;delete input.spec.metadata_revision;}
 else if(scenario==='coherent_title_change')coherentMetadata(input,{title:'Changed owned title'});
 else if(scenario==='coherent_extra_description')coherentMetadata(input,{description:input.spec.metadata.description+' altered'});
 else if(scenario==='different_line')input.spec.metadata_revision_history[1].removed_last_line='Wrong line';
 else if(scenario==='changed_first_history')input.spec.metadata_revision_history[0].new_category_label='音乐';
 else if(scenario==='wrong_previous_binding')input.spec.metadata_revision_history[1].previous_binding.source_id='other_owned';
 else if(scenario==='missing_internal_rights')input.spec.metadata_revision_history[1].internal_rights_records_preserved=false;
 else if(scenario==='fake_public_approval')input.spec.metadata_revision_history[1].public_submit=true;
 else if(scenario==='wrong_attempt')input.spec.metadata_revision_history[1].attempt_id=randomUUID();
 else if(scenario==='wrong_upload_hash')input.spec.upload_metadata_sha256='0'.repeat(64);
 else if(scenario==='missing_history')delete input.spec.metadata_revision_history;
 else if(scenario==='reordered_history')input.spec.metadata_revision_history.reverse();
 else if(scenario==='crlf_suffix'){
  const [first,second]=input.spec.metadata_revision_history;
  input.originalMetadata.description=input.originalMetadata.description.replace('\n'+line,'\r\n'+line);
  const base={...first.previous_binding,metadata:input.originalMetadata,metadata_sha256:hash(input.originalMetadata)};
  first.previous_binding=base;first.previous_binding_sha256=hash(base);first.previous_metadata_sha256=base.metadata_sha256;
  const middle={...base,metadata:{...input.originalMetadata,category_label:'娱乐'}};middle.metadata_sha256=hash(middle.metadata);
  first.new_metadata_sha256=middle.metadata_sha256;first.new_binding_sha256=hash(middle);
  second.previous_binding=middle;second.previous_binding_sha256=hash(middle);second.previous_metadata_sha256=middle.metadata_sha256;
  input.spec.upload_metadata_sha256=base.metadata_sha256;input.originalSpec.metadata_sha256=base.metadata_sha256;
  coherentMetadata(input,{description:middle.metadata.description.slice(0,-('\n'+line).length)});
 }
 const before=JSON.stringify(input);if(success)assert.ok(validate(input));else assert.throws(()=>validate(input));assert.equal(JSON.stringify(input),before);results.push(scenario);
}
console.log(JSON.stringify({passed:results,checks:results.length,network_calls:0,browser_actions:0,real_owner_approval:false}));
