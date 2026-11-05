// Pure local byte validation for the API renderer. No DOM/browser/network import.
// The legacy validator remains unchanged for old evidence verification.
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
const sharp=createRequire(new URL('../compositor/package.json',import.meta.url))('sharp');
const hash=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
export class StaticMediaBytesError extends Error {
 constructor(code){super(code);this.code=code;}
}
// Keep the original validator's error-code and complete-decode semantics while
// avoiding a dependency on the X rendered-DOM observation module.
const ObservationError=StaticMediaBytesError;

function animatedContainer(bytes){
 if(bytes.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10]))){
  for(let p=8;p+12<=bytes.length;){const n=bytes.readUInt32BE(p),name=bytes.toString('ascii',p+4,p+8);
   if(name==='acTL')return true;if(n>bytes.length-p-12)break;p+=n+12;}
 }
 if(bytes.toString('ascii',0,4)==='RIFF'&&bytes.toString('ascii',8,12)==='WEBP'){
  for(let p=12;p+8<=bytes.length;){const n=bytes.readUInt32LE(p+4),name=bytes.toString('ascii',p,p+4);
   if(['ANIM','ANMF'].includes(name))return true;if(n>bytes.length-p-8)break;p+=n+8+n%2;}
 }
 return false;
}

export async function verifyMachineMediaBytes(bytes,item,{maxBytes=20000000,maxPixels=40000000}={}){
 if(!Buffer.isBuffer(bytes)||!bytes.length||bytes.length>maxBytes)throw new ObservationError('MEDIA_ORIGINAL_SIZE_INVALID');
 if(animatedContainer(bytes))throw new ObservationError('MEDIA_ANIMATED_OR_UNSUPPORTED');
 try{
  const image=sharp(bytes,{animated:true,limitInputPixels:maxPixels,failOn:'warning'}),meta=await image.metadata();
  const format=meta.format==='jpeg'?'jpg':meta.format;
  if(!['jpg','png','webp'].includes(format)||(meta.pages||1)!==1||format!==item.format)throw new ObservationError('MEDIA_ANIMATED_OR_UNSUPPORTED');
  if(!meta.width||!meta.height||meta.width*meta.height>maxPixels||meta.width<item.preview_natural_width||meta.height<item.preview_natural_height)
   throw new ObservationError('MEDIA_ORIGINAL_DIMENSIONS_UNVERIFIED');
  if(meta.orientation&&meta.orientation!==1)throw new ObservationError('MEDIA_ORIGINAL_ORIENTATION_UNSUPPORTED');
  // metadata alone can accept truncated files. Force complete pixel decoding.
  await image.raw().toBuffer();
  return {sha256:hash(bytes),bytes:bytes.length,width:meta.width,height:meta.height,format,decoded_complete:true,animated:false,
   operations:'original byte-for-byte copy; no crop, resize, orientation rewrite or image-text translation'};
 }catch(e){if(e instanceof ObservationError)throw e;throw new ObservationError('MEDIA_ORIGINAL_DECODE_FAILED');}
}

