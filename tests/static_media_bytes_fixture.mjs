// Local owned pixels only. Compare legacy and API-only validation behavior.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
import {verifyMachineMediaBytes as legacy} from '../adapters/machine_media_capture.mjs';
import {verifyMachineMediaBytes as standalone} from '../adapters/static_media_bytes.mjs';
const sharp=createRequire(new URL('../compositor/package.json',import.meta.url))('sharp');
const hash=b=>crypto.createHash('sha256').update(b).digest('hex');
const pixels=()=>sharp({create:{width:24,height:16,channels:3,background:'#93b8d2'}});
const png=await pixels().png().toBuffer(),jpg=await pixels().jpeg().toBuffer(),webp=await pixels().webp().toBuffer();
const oriented=await pixels().withMetadata({orientation:6}).jpeg().toBuffer();
const apngMarker=Buffer.alloc(20);apngMarker.writeUInt32BE(8,0);apngMarker.write('acTL',4);
const markedPng=Buffer.concat([png.subarray(0,8),apngMarker,png.subarray(8)]);
const animChunk=Buffer.from('ANIM\x00\x00\x00\x00','binary');
const markedWebp=Buffer.concat([webp.subarray(0,12),animChunk,webp.subarray(12)]);
markedWebp.writeUInt32LE(markedWebp.length-8,4);
const cases=[
 ['complete-png',png,{format:'png',preview_natural_width:24,preview_natural_height:16},{},null],
 ['complete-jpeg',jpg,{format:'jpg',preview_natural_width:0,preview_natural_height:0},{},null],
 ['complete-webp',webp,{format:'webp',preview_natural_width:12,preview_natural_height:8},{},null],
 ['zero-byte-buffer',Buffer.alloc(0),{format:'png'},{},'MEDIA_ORIGINAL_SIZE_INVALID'],
 ['nonbuffer-input',new Uint8Array([1,2]),{format:'png'},{},'MEDIA_ORIGINAL_SIZE_INVALID'],
 ['byte-ceiling',png,{format:'png'},{maxBytes:png.length-1},'MEDIA_ORIGINAL_SIZE_INVALID'],
 ['declared-format-mismatch',png,{format:'jpg'},{},'MEDIA_ANIMATED_OR_UNSUPPORTED'],
 ['preview-width-larger-than-decoded',png,{format:'png',preview_natural_width:25,preview_natural_height:16},{},'MEDIA_ORIGINAL_DIMENSIONS_UNVERIFIED'],
 ['preview-height-larger-than-decoded',png,{format:'png',preview_natural_width:24,preview_natural_height:17},{},'MEDIA_ORIGINAL_DIMENSIONS_UNVERIFIED'],
 ['pixel-ceiling',png,{format:'png'},{maxPixels:100},'MEDIA_ORIGINAL_DECODE_FAILED'],
 ['exif-orientation',oriented,{format:'jpg'},{},'MEDIA_ORIGINAL_ORIENTATION_UNSUPPORTED'],
 ['owned-apng-declaration',markedPng,{format:'png'},{},'MEDIA_ANIMATED_OR_UNSUPPORTED'],
 ['owned-webp-animation-declaration',markedWebp,{format:'webp'},{},'MEDIA_ANIMATED_OR_UNSUPPORTED'],
 ['corrupt-static-container',Buffer.from('owned invalid image bytes'),{format:'png'},{},'MEDIA_ORIGINAL_DECODE_FAILED'],
 ['truncated-png-pixel-data',png.subarray(0,Math.floor(png.length*.65)),{format:'png'},{},'MEDIA_ORIGINAL_DECODE_FAILED'],
];
async function outcome(fn,bytes,item,options){
 try{return {result:await fn(bytes,item,options),error:null};}
 catch(error){return {result:null,error:{code:error.code,message:error.message,name:error.name}};}
}
const results=[];
for(const [name,bytes,item,options,expectedError]of cases){
 const before=Buffer.isBuffer(bytes)?hash(bytes):null;
 const first=await outcome(legacy,bytes,item,options),second=await outcome(standalone,bytes,item,options);
 assert.deepEqual(second,first,name+' changed validation result');
 assert.equal(second.error?.code??null,expectedError,name+' unexpected validation branch');
 if(!expectedError){
  assert.equal(second.result.width,24);assert.equal(second.result.height,16);
  assert.equal(second.result.sha256,before);assert.equal(second.result.decoded_complete,true);
  assert.equal(second.result.animated,false);assert.equal(second.result.bytes,bytes.length);
 }
 if(before)assert.equal(hash(bytes),before,name+' mutated caller bytes');
 results.push({name,passed:true,error_code:second.error?.code??null});
}
const pureSource=await fs.readFile(new URL('../adapters/static_media_bytes.mjs',import.meta.url),'utf8');
const imports=[...pureSource.matchAll(/^import .* from ['"]([^'"]+)['"]/gm)].map(m=>m[1]).sort();
assert.deepEqual(imports,['node:crypto','node:module']);
assert(!/\b(?:fetch|XMLHttpRequest|launchPersistentContext|newContext|page\.)/.test(pureSource));
const renderer=await fs.readFile(new URL('../adapters/render_machine_official.mjs',import.meta.url),'utf8');
assert(renderer.includes("from './static_media_bytes.mjs'"));
assert(!renderer.includes("from './machine_media_capture.mjs'"));
console.log(JSON.stringify({version:'owned-static-media-bytes-equivalence-v1',cases:results,
 direct_imports:imports,external_requests:0,browser_launches:0,model_calls:0,publish_requests:0}));
