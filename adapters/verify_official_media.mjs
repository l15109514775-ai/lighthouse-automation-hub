// Decode an already-downloaded local API photo. No HTTP, browser or model.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
const sharp=createRequire(new URL('../compositor/package.json',import.meta.url))('sharp');
try{
 const [fileArg,rootArg,widthArg,heightArg,format]=process.argv.slice(2),width=Number(widthArg),height=Number(heightArg);
 if(!fileArg||!rootArg||!Number.isSafeInteger(width)||!Number.isSafeInteger(height)||Math.min(width,height)<1||
  width*height>40000000||!['jpg','png','webp'].includes(format))throw new Error('X_MEDIA_ARGUMENT_INVALID');
 const root=await fs.realpath(rootArg),file=path.resolve(fileArg),rel=path.relative(root,file);
 if(!rel||rel.startsWith('..')||path.isAbsolute(rel)||(await fs.lstat(file)).isSymbolicLink()||await fs.realpath(file)!==file)
  throw new Error('X_MEDIA_FILE_INVALID');
 const info=await fs.stat(file);if(!info.isFile()||!info.size||info.size>20000000)throw new Error('X_MEDIA_SIZE_INVALID');
 const bytes=await fs.readFile(file);
 // APNG metadata support varies between libvips builds; do not silently accept
 // a container whose animation declaration the decoder happens not to expose.
 if(bytes.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10])))
  for(let at=8;at+12<=bytes.length;){const count=bytes.readUInt32BE(at),kind=bytes.toString('ascii',at+4,at+8);
   if(kind==='acTL')throw new Error('X_MEDIA_ANIMATED_UNSUPPORTED');if(count>bytes.length-at-12)break;at+=count+12;}
 const image=sharp(bytes,{animated:true,limitInputPixels:40000000,failOn:'warning'}),meta=await image.metadata();
 if((meta.pages||1)!==1)throw new Error('X_MEDIA_ANIMATED_UNSUPPORTED');
 const decodedFormat=meta.format==='jpeg'?'jpg':meta.format;
 if(decodedFormat!==format||meta.width!==width||meta.height!==height)throw new Error('X_MEDIA_DECLARED_DIMENSIONS_MISMATCH');
 if(meta.orientation&&meta.orientation!==1)throw new Error('X_MEDIA_ORIENTATION_UNSUPPORTED');
 await image.raw().toBuffer();
 console.log(JSON.stringify({version:'x-official-photo-validation-v1',sha256:crypto.createHash('sha256').update(bytes).digest('hex'),
  bytes:bytes.length,format:decodedFormat,width:meta.width,height:meta.height,decoded_complete:true,animated:false,
  human_review:false,creator_original_upload_identity_verified:false,operations:'exact API URL bytes; local decode only; no resize/crop/URL rewrite'}));
}catch(error){console.log(JSON.stringify({ok:false,error_code:/^[A-Z_]+$/.test(error.message)?error.message:'X_MEDIA_DECODE_FAILED'}));process.exitCode=1;}
