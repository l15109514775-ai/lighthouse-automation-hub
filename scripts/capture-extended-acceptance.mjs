// Source-only helper for an explicit real-content recovery acceptance job.
// This process has no model/publisher credentials and grants no publication.
import fs from 'node:fs/promises';
import path from 'node:path';
import {createPublicXSession,publicXAccessError} from '../adapters/browser_x_public.mjs';
import {captureExtendedTweet} from '../adapters/browser_extended_source.mjs';
import {dismissOrdinaryPrompts} from '../adapters/browser_observation.mjs';
const [root,jobId,handle,postId]=process.argv.slice(2);
if(root!=='/opt/lighthouse-hub'||!/^[-a-f0-9]{36}$/.test(jobId)||
 !['example_handle_a','example-channel-b'].includes(handle)||!/^\d{1,30}$/.test(postId))throw new Error('ACCEPTANCE_SPEC_INVALID');
const folder=path.join(root,'media',jobId,'recovery-source');
let session;
try{
 session=await createPublicXSession();const page=await session.context.newPage();
 const response=await page.goto(`https://x.com/${handle}/status/${postId}`,{waitUntil:'domcontentloaded'});
 const code=publicXAccessError(page.url(),response?.status());if(code)throw new Error(code);
 await page.locator('article').first().waitFor({state:'visible',timeout:15000});
 await dismissOrdinaryPrompts(page,path.join(root,'media',jobId,'acceptance-prompts'));
 await captureExtendedTweet(page,{handle,postId},folder,{trigger:'durable_job'});
 console.log(JSON.stringify({record_file:path.join(folder,'extended-observation.json'),source_only:true,
  model_calls:0,publish_requests:0,authentication_mode:session.authentication_mode,persistent_profile_used:false}));
}finally{await session?.close();}
