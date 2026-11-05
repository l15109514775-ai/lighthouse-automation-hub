import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash} from 'node:crypto';
export const nativeControlCapabilities=Object.freeze({fill_metadata:true,submit:false,lookup:false});
const hash=b=>createHash('sha256').update(b).digest('hex');
function fail(code){throw Object.assign(new Error(code),{code:'RECOVERY_'+code});}
export async function handleNativeControl({page,spec,output,helpers,control_action}){
 if(control_action!=='fill_metadata')return {state:'unsupported',submitted:false};
 const read=async n=>JSON.parse(await fs.readFile(path.join(output,n),'utf8'));
 const prior=await read('native-cover-complete-result.json'),assigned=await read('cover-image-assigned-v2.json');
 if(prior.state!=='cover_complete_unknown'||prior.native_click_performed!==true||prior.metadata_sha256!==spec.metadata_sha256||prior.recovery_id!==spec.recovery_id||assigned.state!=='bound_cover_assigned_preview_pending'||assigned.cover_source?.sha256!==spec.cover.sha256)fail('BOUND_PRIOR_COVER_CLICK_REQUIRED');
 const u=new URL(page.url());if(u.origin!=='https://member.bilibili.com'||u.pathname!=='/platform/upload/video/frame'||!await helpers.checkProtection()||await helpers.accountIdentity()!==spec.target_site_uid)fail('ACCOUNT_REQUIRED');
 await helpers.checkedFile();if(hash(await fs.readFile(spec.cover.file))!==spec.cover.sha256)fail('COVER_CHANGED');
 const body=await page.locator('body').innerText();if(body.includes('检测到个人空间封面（16:9）未修改')||!body.includes('封面制作'))fail('POST_SYNC_EDITOR_REQUIRED');
 const syncIntentDir=path.join(path.dirname(path.dirname(output)),'native-control-intents'),syncKey=hash(Buffer.from(JSON.stringify([spec.attempt_id,spec.metadata_sha256,spec.cover.sha256])));
 const syncIntent=JSON.parse(await fs.readFile(path.join(syncIntentDir,'cover-confirm-sync-'+syncKey+'.json'),'utf8'));
 if(syncIntent.job_id!==spec.job_id||syncIntent.recovery_id!==spec.recovery_id||syncIntent.metadata_sha256!==spec.metadata_sha256||syncIntent.cover_sha256!==spec.cover.sha256)fail('PRIOR_SYNC_INTENT_REQUIRED');
 const button=page.locator('.cover-editor.bcc-dialog__wrap-mask:visible .cover-editor-button .button:visible').filter({hasText:/^\s*完成\s*$/});
 if(await button.count()!==1||!await button.isEnabled())fail('UNIQUE_COVER_SYNC_BUTTON_REQUIRED');
 const root=path.dirname(path.dirname(output)),intents=path.join(root,'native-control-intents');
 const file=path.join(intents,'cover-complete-after-sync-'+hash(Buffer.from(JSON.stringify([spec.attempt_id,spec.metadata_sha256,spec.cover.sha256])))+'.json');
 await fs.mkdir(intents,{recursive:true,mode:0o700});const fd=await fs.open(file,'wx',0o600);
 try{await fd.writeFile(JSON.stringify({version:'cover-complete-after-sync-intent-v1',job_id:spec.job_id,recovery_id:spec.recovery_id,metadata_sha256:spec.metadata_sha256,cover_sha256:spec.cover.sha256,created_at:new Date().toISOString(),public_submit:false}));await fd.sync();}finally{await fd.close();}
 const directory=await fs.open(intents,'r');try{await directory.sync();}finally{await directory.close();}
 await button.click({timeout:5000});
 const end=Date.now()+15000;let region=null;
 do{
  if(!await helpers.checkProtection())fail('PROTECTION_CHANGED');
  region=await page.evaluate(()=>{const a=document.querySelector('.cover-main .cover-module-main');if(!a)return null;return {preview_present:[a,...a.querySelectorAll('*')].some(e=>getComputedStyle(e).backgroundImage!=='none')||[...a.querySelectorAll('img')].some(e=>e.naturalWidth>0),placeholder_present:(a.innerText||'').includes('添加封面')};});
  if(await page.locator('.cover-editor.bcc-dialog__wrap-mask:visible').count()===0&&await button.count()===0&&region?.preview_present&&!region.placeholder_present)break;
  await page.waitForTimeout(200);
 }while(Date.now()<end);
 if(await page.locator('.cover-editor.bcc-dialog__wrap-mask:visible').count()||await button.count()||!region?.preview_present||region.placeholder_present||await helpers.accountIdentity()!==spec.target_site_uid)fail('COVER_SYNC_RESULT_UNKNOWN');
 const result={version:'native-cover-sync-reconciliation-v1',state:'cover_sync_confirmed',job_id:spec.job_id,attempt_id:spec.attempt_id,recovery_id:spec.recovery_id,metadata_sha256:spec.metadata_sha256,cover_sha256:spec.cover.sha256,observed_at:new Date().toISOString(),native_cover_sync_clicked:false,native_post_sync_complete_clicked:true,prior_complete_unknown_preserved:true,remote_cover_bytes_verified:false,visible_cover_region:region,submitted:false,new_video_file_selection:false};
 await helpers.write('native-cover-sync-reconciliation-v1.json',result);return {...result,...await helpers.inventory('native-cover-sync-reconciled-v1')};
}
