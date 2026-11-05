// Generated root-RO native control entry. Core path and bytes are fixed.
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
export const nativeControlCapabilities=Object.freeze({"fill_metadata":true,"submit":false,"lookup":false});
const CORE_NAME="short_video_native_controls_core.b13d5a30188437e4fb6d739a455b1ecca4924e3d8c619f76de971ccd8d91a894.mjs",CORE_SHA="b13d5a30188437e4fb6d739a455b1ecca4924e3d8c619f76de971ccd8d91a894";
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
  return await module.completeCover(args);
 }catch(error){return {state:'blocked',error_code:typeof error?.code==='string'&&/^NATIVE_CORE_[A-Z_]+$/.test(error.code)?error.code:'NATIVE_CORE_LOAD_FAILED',submitted:false,native_click_performed:false};}
}
