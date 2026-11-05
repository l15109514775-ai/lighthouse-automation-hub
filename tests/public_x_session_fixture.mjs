// Owned offline HTML/cookies only. No X login, external request or account.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {createPublicXSession,PUBLIC_X_AUTHENTICATION_MODE,publicXAccessError} from '../adapters/browser_x_public.mjs';
const root=path.resolve(process.argv[2]);await fs.mkdir(root,{recursive:true});
process.env.HUB_ROOT=root;
const originalRoot=path.join(root,'data/browser-profiles');
const originals=[['x/SingletonLock','owned stale lock; never remove'],['x/Cookies','owned synthetic persisted cookie bytes'],
 ['x/state.json',JSON.stringify({cookies:[{name:'owned_auth',value:'must-not-load',domain:'owned.example.test',path:'/'}]})],
 ['bilibili/sentinel','owned other-service profile; never touch']];
for(const [name,body]of originals){const p=path.join(originalRoot,name);await fs.mkdir(path.dirname(p),{recursive:true});await fs.writeFile(p,body);}
const digest=b=>crypto.createHash('sha256').update(b).digest('hex');
const before=await Promise.all(originals.map(async([name])=>{const p=path.join(originalRoot,name),s=await fs.stat(p);return {name,sha256:digest(await fs.readFile(p)),mtime:s.mtimeMs};}));
const passed=[];
const launch=chromium.launch;let calls=[];
try{
 const expected=new Error('owned synthetic launch failure');
 chromium.launch=async options=>{calls.push(options);throw expected;};
 await assert.rejects(()=>createPublicXSession(),e=>e===expected);
 assert.equal(calls.length,1);assert.deepEqual(calls[0],{headless:false,chromiumSandbox:true,channel:'chromium'});
 passed.push('production_defaults_headed_full_chromium_sandbox_and_no_retry_rotation');
 for(const options of [{storageState:'owned-state.json'},{userDataDir:originalRoot},{proxy:{server:'https://owned.invalid'}},{headless:'true'}])
  await assert.rejects(()=>createPublicXSession(options),/PUBLIC_X_BROWSER_OPTION_INVALID/);
 assert.equal(calls.length,1);passed.push('profile_storage_proxy_and_invalid_options_rejected_before_launch');
 let closed=0;chromium.launch=async()=>({newContext:async()=>{throw expected;},close:async()=>{closed++;}});
 await assert.rejects(()=>createPublicXSession(),e=>e===expected);assert.equal(closed,1);
 passed.push('failed_context_creation_closes_browser_without_fallback');
}finally{chromium.launch=launch;}
let first,second,external=0;
try{
 first=await createPublicXSession({headless:true});
 assert.equal(first.authentication_mode,PUBLIC_X_AUTHENTICATION_MODE);assert.equal(first.persistent_profile_used,false);
 assert.deepEqual(await first.context.cookies(),[]);
 await first.context.route('**/*',r=>r.fulfill({status:200,contentType:'text/html',body:'<main>Owned offline public fixture</main>'}));
 const page=await first.context.newPage();await page.goto('https://owned.example.test/public');
 assert.equal(await page.locator('main').innerText(),'Owned offline public fixture');
 await first.context.addCookies([{name:'owned_auth',value:'session-one-only',domain:'owned.example.test',path:'/'}]);
 await page.evaluate(()=>localStorage.setItem('owned_session','one'));
 assert.equal((await first.context.cookies()).find(c=>c.name==='owned_auth')?.value,'session-one-only');
 passed.push('owned_context_can_hold_its_own_synthetic_state');
 second=await createPublicXSession({headless:true});
 assert.notEqual(first.browser,second.browser);assert.notEqual(first.context,second.context);
 assert.deepEqual(await second.context.cookies(),[]);
 await second.context.route('**/*',r=>r.fulfill({status:200,contentType:'text/html',body:'<main>Owned offline public fixture</main>'}));
 const secondPage=await second.context.newPage();await secondPage.goto('https://owned.example.test/public');
 assert.equal(await secondPage.evaluate(()=>document.cookie),'');
 assert.equal(await secondPage.evaluate(()=>localStorage.getItem('owned_session')),null);
 assert.equal((await first.context.cookies()).find(c=>c.name==='owned_auth')?.value,'session-one-only');
 passed.push('two_new_sessions_share_neither_synthetic_auth_cookie_nor_local_storage');
 await secondPage.goto('https://x.com/i/flow/login');
 assert.equal(publicXAccessError(secondPage.url(),200),'X_WEB_LOGIN_REQUIRED_PUBLIC_MODE');
 assert.equal(publicXAccessError('https://x.com/login?redirect_after_login=%2Fexample_handle_a',200),'X_WEB_LOGIN_REQUIRED_PUBLIC_MODE');
 assert.equal(publicXAccessError('https://x.com/example_handle_a',403),'X_WEB_ACCESS_DENIED');
 assert.equal(publicXAccessError('https://x.com/example_handle_a',429),'X_WEB_RATE_LIMITED');
 assert.equal(publicXAccessError('https://x.com/example_handle_a',503),'X_WEB_UPSTREAM_UNAVAILABLE');
 assert.equal(publicXAccessError('https://x.com/example_handle_a',404),'X_WEB_PAGE_UNAVAILABLE');
 assert.equal(publicXAccessError('https://x.com/example_handle_a',200),null);
 passed.push('public_login_requirement_is_not_expired_auth_and_http_protections_keep_codes');
 const after=await Promise.all(originals.map(async([name])=>{const p=path.join(originalRoot,name),s=await fs.stat(p);return {name,sha256:digest(await fs.readFile(p)),mtime:s.mtimeMs};}));
 assert.deepEqual(after,before);passed.push('legacy_x_profile_lock_state_and_other_profile_bytes_mtimes_untouched');
 const browserA=first.browser,browserB=second.browser;
 await first.close();first=null;await second.close();second=null;
 assert.equal(browserA.isConnected(),false);assert.equal(browserB.isConnected(),false);
 passed.push('session_close_disconnects_browser_process');
}finally{await first?.close();await second?.close();}
console.log(JSON.stringify({passed,external_requests:external,authentication_mode:PUBLIC_X_AUTHENTICATION_MODE,persistent_profile_used:false,human_review:false}));
