// Pure owned error objects. No browser, profile, account, environment or network.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import {classifyPublisherError} from '../adapters/browser_publish_errors.mjs';

const passed=[];
const error=(message,name='Error')=>({message,name});
const actualMarkers=[
 'browserType.launchPersistentContext: Target page, context or browser has been closed\n[ERROR] The profile appears to be in use by another Chromium process (120) on another computer (owned-old-host).',
 '[ERROR:process_singleton_posix.cc] Failed to create /owned/profile/SingletonLock: File exists (17)',
 'SingletonLock: already exists',
 'SingletonLock is already in use',
 'The profile is already in use by another process',
 'user data directory is already in use',
];
for(const [index,message]of actualMarkers.entries()){
 assert.equal(classifyPublisherError(error(message),'launch'),'WEB_BROWSER_PROFILE_LOCKED');
 passed.push(`launch-profile-in-use-marker-${index}`);
}
for(const stage of ['navigate','verify_account','lookup_existing','choose_image','click_publish','verify_visible_result']){
 assert.equal(classifyPublisherError(error(actualMarkers[0]),stage),'WEB_RUNTIME_ERROR');
 passed.push(`same-marker-after-${stage}-is-not-a-launch-lock`);
}
for(const [name,message]of [
 ['permission-denied','Failed to create /owned/profile/SingletonLock: Permission denied'],
 ['missing-lock','SingletonLock: No such file or directory'],
 ['generic-processsingleton','Failed to create a ProcessSingleton due to permission problem'],
 ['unrelated-file-exists','SingletonLock inspected; unrelated output: File exists'],
 ['another-computer-alone','Cannot connect to another computer'],
 ['browser-executable','Executable does not exist at owned/runtime/chromium'],
 ['unrecognized-uppercase-secret','OWNED_PRIVATE_TOKEN_VALUE'],
]){
 assert.equal(classifyPublisherError(error(message),'launch'),'WEB_RUNTIME_ERROR');passed.push(name);
}
for(const stage of ['launch','navigate']){
 assert.equal(classifyPublisherError(error(actualMarkers[0],'TimeoutError'),stage),'WEB_UI_TIMEOUT');
 passed.push(`timeout-${stage}-preserved-even-with-profile-wording`);
}
// The producer's existing explicit public symbols are the compatibility
// boundary. Reading its source does not execute the publisher or a browser.
const symbols=new Set();
for(const name of ['browser_publisher.mjs','publication_window.mjs']){
 const text=await fs.readFile(new URL('../adapters/'+name,import.meta.url),'utf8');
 for(const match of text.matchAll(/new Error\('([A-Z_]+)'\)/g))symbols.add(match[1]);
}
assert.ok(symbols.size>=26);
for(const symbol of symbols)for(const stage of ['launch','click_publish'])assert.equal(classifyPublisherError(error(symbol),stage),symbol);
passed.push('all-existing-public-publisher-and-window-codes-preserved');
const secret='sk-owned-test-key-12345',cookie='SESSDATA=owned-test-cookie',argv='--user-data-dir=/owned/private --owned-token='+secret;
const sensitive=actualMarkers[0]+'\nCookie: '+cookie+'\n'+argv;
for(const stage of ['launch','navigate']){
 const classified=classifyPublisherError(error(sensitive),stage);
 assert.ok(['WEB_BROWSER_PROFILE_LOCKED','WEB_RUNTIME_ERROR'].includes(classified));
 assert.equal(classified.includes(secret),false);assert.equal(classified.includes(cookie),false);assert.equal(classified.includes(argv),false);
}
passed.push('credentials-cookies-hostnames-and-argv-never-returned');
for(const value of [null,undefined,{},'Owned thrown string'])assert.equal(classifyPublisherError(value,'launch'),'WEB_RUNTIME_ERROR');
passed.push('non-error-values-never-stringify-unknown-data');
console.log(JSON.stringify({passed,public_codes_verified:symbols.size,browser_launches:0,profile_files_touched:0,external_calls:0}));
