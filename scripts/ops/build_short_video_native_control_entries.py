"""Build tiny root-controller entries pinned to immutable sibling core bytes."""
from pathlib import Path
import hashlib,json
ROOT=Path(__file__).resolve().parents[2]
def main():
 core=ROOT/'adapters/short_video_native_controls_core.mjs';data=core.read_bytes();digest=hashlib.sha256(data).hexdigest()
 name='short_video_native_controls_core.'+digest+'.mjs';results={}
 for entry,function,capabilities in (
   ('short_video_cover_complete_control.mjs','completeCover',{'fill_metadata':True,'submit':False,'lookup':False}),
   ('short_video_private_draft_control.mjs','savePrivateDraft',{'fill_metadata':True,'submit':False,'lookup':True}),
   ('short_video_submit_control.mjs','submitNative',{'fill_metadata':False,'submit':True,'lookup':True})):
  if ('export async function '+function+'(').encode() not in data:continue
  dispatch="return await module.%s(args);"%function
  if function=='submitNative':dispatch="return args.control_action==='lookup'?await module.observeSubmission(args):await module.submitNative(args);"
  elif function=='savePrivateDraft':dispatch="return args.control_action==='lookup'?await module.observePrivateDraft(args):await module.savePrivateDraft(args);"
  source="""// Generated root-RO native control entry. Core path and bytes are fixed.
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
export const nativeControlCapabilities=Object.freeze(CAPABILITIES);
const CORE_NAME=CORE_NAME_VALUE,CORE_SHA=CORE_SHA_VALUE;
export async function handleNativeControl(args){
 try{
  if(!Object.hasOwn(nativeControlCapabilities,args.control_action)||nativeControlCapabilities[args.control_action]!==true)return {state:'unsupported',submitted:false,native_click_performed:false};
  const file=fileURLToPath(new URL('./'+CORE_NAME,import.meta.url));
  if(path.dirname(file)!=='/run/short-video-controls')throw Object.assign(new Error(),{code:'NATIVE_CORE_FIXED_PATH_REQUIRED'});
  const stat=await fs.lstat(file);if(!stat.isFile()||stat.isSymbolicLink()||stat.uid!==0||stat.mode&0o022||stat.size>131072)throw Object.assign(new Error(),{code:'NATIVE_CORE_ROOT_OWNERSHIP_REQUIRED'});
  const hash=bytes=>createHash('sha256').update(bytes).digest('hex');
  if(hash(await fs.readFile(file))!==CORE_SHA)throw Object.assign(new Error(),{code:'NATIVE_CORE_HASH_MISMATCH'});
  const url=pathToFileURL(file);url.searchParams.set('sha256',CORE_SHA);const module=await import(url.href);
  if(hash(await fs.readFile(file))!==CORE_SHA)throw Object.assign(new Error(),{code:'NATIVE_CORE_HASH_MISMATCH'});
  DISPATCH
 }catch(error){return {state:'blocked',error_code:typeof error?.code==='string'&&/^NATIVE_CORE_[A-Z_]+$/.test(error.code)?error.code:'NATIVE_CORE_LOAD_FAILED',submitted:false,native_click_performed:false};}
}
""".replace('CAPABILITIES',json.dumps(capabilities,separators=(',',':'))).replace('CORE_NAME_VALUE',json.dumps(name)).replace('CORE_SHA_VALUE',json.dumps(digest)).replace('DISPATCH',dispatch)
  target=ROOT/'adapters'/entry;target.write_text(source,encoding='utf-8',newline='\n');results[entry]=hashlib.sha256(target.read_bytes()).hexdigest()
 manifest={'version':'short-native-control-entry-manifest-v1','core_file':core.relative_to(ROOT).as_posix(),
  'core_sha256':digest,'installed_core_filename':name,'entries_sha256':results,'source_bytes_preserved':True}
 (ROOT/'scripts/ops/native_control_sources/native-controls-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
 print(json.dumps(manifest))
if __name__=='__main__':main()
