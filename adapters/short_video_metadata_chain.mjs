// Pure, versioned metadata lineage. No I/O, owner approval or browser actions.
import {createHash} from 'node:crypto';
export const PUBLIC_PERMISSION_LINE='转载许可编号：114514（字幕组提供的许可范围）。';
function canonical(v){return Array.isArray(v)?'['+v.map(canonical).join(',')+']':v!==null&&typeof v==='object'?'{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+canonical(v[k])).join(',')+'}':JSON.stringify(v);}
export const metadataChainDigest=v=>createHash('sha256').update(canonical(v)).digest('hex');
const equal=(a,b)=>canonical(a)===canonical(b),UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
function fail(){throw Object.assign(new Error('RECOVERY_METADATA_REVISION_INVALID'),{code:'RECOVERY_METADATA_REVISION_INVALID'});}
export function validateShortMetadataRevisionChain({spec,originalMetadata,originalSpec}){
 const hash=metadataChainDigest;
 if(hash(originalMetadata)!==spec.upload_metadata_sha256||hash(spec.metadata)!==spec.metadata_sha256)fail();
 let history;
 if(spec.metadata_revision_history!==undefined){if(!Array.isArray(spec.metadata_revision_history))fail();history=spec.metadata_revision_history;}
 else history=spec.metadata_revision?[spec.metadata_revision]:[];
 if(history.length>2||spec.metadata_revision&&(!history.length||!equal(spec.metadata_revision,history.at(-1))))fail();
 if(!history.length){if(!equal(spec.metadata,originalMetadata)||spec.metadata_sha256!==spec.upload_metadata_sha256)fail();return {mode:'unrevised',revision_ids:[]};}
 // Preserve the original, versioned single-category transport contract. New
 // explicit histories below require full bindings and cannot use this path.
 if(spec.metadata_revision_history===undefined&&history.length===1){
  const r=history[0];
  if(r.version!=='short-video-category-metadata-revision-v1'||r.revision_id!==spec.metadata_revision_id||r.previous_metadata_sha256!==spec.upload_metadata_sha256||
   r.new_metadata_sha256!==spec.metadata_sha256||hash(r.previous_binding?.metadata)!==spec.upload_metadata_sha256||r.new_binding_sha256!==spec.binding_sha256||
   !equal(r.changed_fields,['category_label'])||originalMetadata.category_label!=='虚拟UP主'||spec.metadata.category_label!=='娱乐'||
   hash({...spec.metadata,category_label:'虚拟UP主'})!==spec.upload_metadata_sha256)fail();
  return {mode:'legacy_category_single',revision_ids:[r.revision_id]};
 }
 let binding=history[0].previous_binding,metadata=originalMetadata;
 if(!binding||!equal(binding.metadata,originalMetadata)||binding.metadata_sha256!==spec.upload_metadata_sha256||
  binding.site_uid!==spec.target_site_uid||binding.source_url!==spec.source_url||binding.source_id!==spec.source_id||binding.download_job_id!==spec.download_job_id||
  binding.artifact_sha256!==spec.artifact_sha256||binding.file!==spec.file||binding.file_bytes!==spec.file_bytes||binding.human_review!==false||
  originalSpec&&originalSpec.metadata_sha256!==spec.upload_metadata_sha256)fail();
 for(let index=0;index<history.length;index++){
  const revision=history[index];
  if(!revision||!UUID.test(revision.revision_id||'')||revision.attempt_id!==spec.attempt_id||!equal(revision.previous_binding,binding)||
   revision.previous_binding_sha256!==hash(binding)||revision.previous_metadata_sha256!==hash(metadata)||revision.public_submit!==false||revision.remote_metadata_synchronized!==false)fail();
  let updated;
  if(index===0){
   if(revision.version!=='short-video-category-metadata-revision-v1'||!equal(revision.changed_fields,['category_label'])||metadata.category_label!=='虚拟UP主'||
    revision.previous_category_label!=='虚拟UP主'||revision.new_category_label!=='娱乐')fail();
   updated={...metadata,category_label:'娱乐'};
  }else{
   const suffix='\n'+PUBLIC_PERMISSION_LINE;
   if(revision.version!=='public-permission-line-removal-v1'||!equal(revision.changed_fields,['description'])||revision.previous_revision_id!==history[index-1].revision_id||
    revision.removed_last_line!==PUBLIC_PERMISSION_LINE||typeof metadata.description!=='string'||!metadata.description.endsWith(suffix)||metadata.description.endsWith('\r'+suffix)||
    revision.internal_rights_records_preserved!==true||revision.actual_owner_response!=='转载许可编号删掉'||
    !['owner_response_reference','actual_operator_reference'].every(k=>typeof revision[k]==='string'&&revision[k].trim().length>0&&revision[k].length<=2048))fail();
   updated={...metadata,description:metadata.description.slice(0,-suffix.length)};
  }
  const next={...binding,metadata:updated,metadata_sha256:hash(updated)};
  if(revision.new_metadata_sha256!==hash(updated)||revision.new_binding_sha256!==hash(next))fail();
  metadata=updated;binding=next;
 }
 if(spec.metadata_revision_id!==history.at(-1).revision_id||spec.binding_sha256!==hash(binding)||spec.metadata_sha256!==hash(metadata)||!equal(spec.metadata,metadata))fail();
 return {mode:history.length===2?'category_and_public_line_removal':'category_only',revision_ids:history.map(x=>x.revision_id),binding_sha256:hash(binding)};
}
