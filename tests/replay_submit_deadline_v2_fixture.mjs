// The new pure controller functions are extracted verbatim from the entry.
// Real owned Chromium observes clicks/close; no website, credential or QA claim.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import readline from 'node:readline';
import {PassThrough} from 'node:stream';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
const scenario=process.argv[2],source=await fs.readFile(new URL('../adapters/browser_replay_video.mjs',import.meta.url),'utf8');
assert.ok(['delayed_success','expires_before_click','already_expired','eof_during_launch'].includes(scenario));
function section(start,end){const a=source.indexOf(start),b=source.indexOf(end,a);assert.ok(a>=0&&b>a);return source.slice(a,b);}
const helper=section('export async function performWithSubmitDeadline','\nprocess.umask');
const functions=await import('data:text/javascript;base64,'+Buffer.from(helper).toString('base64'));
let clicks=0,external=0;
const browser=await chromium.launch({headless:true,chromiumSandbox:true});let context;
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function owned(){const c=await browser.newContext();await c.route('**/*',r=>{external++;return r.abort();});const p=await c.newPage();p.on('console',m=>{if(m.text()==='OWNED_CLICK')clicks++;});await p.setContent('<button onclick="console.log(\'OWNED_CLICK\')">Owned submit</button>');return {c,p};}
try{
 if(scenario==='eof_during_launch'){
  const closeCode=section('function close(){','\ncloseOnInputEnd(lines,close);');
  const mod=await import('data:text/javascript;base64,'+Buffer.from('let context=null,closed=false,closePromise=null,opening=null,lines=null;\n'+closeCode+'\nexport{close};export function setup(p,l){opening=p;lines=l;}export function status(){return closed;}').toString('base64'));
  const input=new PassThrough(),lines=readline.createInterface({input,crlfDelay:Infinity});
  const pending=(async()=>{await pause(120);const x=await owned();context=x.c;return x.c;})();
  mod.setup(pending,lines);functions.closeOnInputEnd(lines,mod.close);input.end();
  await pause(30);const one=mod.close(),two=mod.close();assert.equal(one,two);await one;
  assert.equal(mod.status(),true);assert.equal(context.pages().length,0);assert.equal(clicks,0);
 }else{
  const x=await owned();context=x.c;let started=0,error=null,result=null;
  const expires=Date.now()/1000+(scenario==='already_expired'?-1:scenario==='expires_before_click'?.12:2);
  try{result=await functions.performWithSubmitDeadline(async()=>{started++;await pause(scenario==='expires_before_click'?260:150);await x.p.getByRole('button',{name:'Owned submit',exact:true}).click({timeout:1000});await pause(150);return {state:'owned-result'};},{expires_at:expires},()=>context.close());}catch(e){error=e.code;}
  if(scenario==='delayed_success'){assert.deepEqual(result,{state:'owned-result'});assert.equal(error,null);assert.equal(clicks,1);await pause(Math.max(0,expires*1000-Date.now())+50);assert.equal(context.pages().length,1);}
  else{assert.equal(error,'REPLAY_BROWSER_RESERVATION_EXPIRED');assert.equal(clicks,0);assert.equal(started,scenario==='already_expired'?0:1);}
 }
 assert.equal(external,0);console.log(JSON.stringify({scenario,owned_clicks:clicks,real_publications:0,external_requests:external,live_tested:false}));
}finally{await context?.close();await browser.close();}
