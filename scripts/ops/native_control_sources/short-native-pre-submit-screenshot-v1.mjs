import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {pathToFileURL} from 'node:url';
export const nativeControlCapabilities=Object.freeze({fill_metadata:true,submit:false,lookup:false});
const CORE_SHA='b13d5a30188437e4fb6d739a455b1ecca4924e3d8c619f76de971ccd8d91a894';
const hash=b=>createHash('sha256').update(b).digest('hex');
export async function handleNativeControl({page,spec,output,helpers,control_action}){
 if(control_action!=='fill_metadata')return {state:'unsupported',submitted:false,native_click_performed:false};
 const coreFile='/run/short-video-controls/short_video_native_controls_core.'+CORE_SHA+'.mjs';
 const st=await fs.lstat(coreFile);if(st.uid!==0||!st.isFile()||st.isSymbolicLink()||st.mode&0o022||hash(await fs.readFile(coreFile))!==CORE_SHA)throw new Error('PINNED_CORE_REQUIRED');
 const core=await import(pathToFileURL(coreFile).href);
 if(hash(await fs.readFile(coreFile))!==CORE_SHA||await helpers.accountIdentity()!==spec.target_site_uid||!await helpers.checkProtection())throw new Error('ACCOUNT_OR_PROTECTION_CHANGED');
 await helpers.checkedFile();
 const completed=JSON.parse(await fs.readFile(path.join(output,'native-cover-complete-result.json'),'utf8'));
 if(completed.state!=='cover_complete_applied'||completed.recovery_id!==spec.recovery_id||completed.metadata_sha256!==spec.metadata_sha256||completed.cover?.local_cover_sha256!==spec.cover.sha256)throw new Error('COMPLETED_COVER_REQUIRED');
 if(await page.locator('.cover-editor.bcc-dialog__wrap-mask:visible').count())throw new Error('COVER_EDITOR_STILL_OPEN');
 const meta=spec.metadata;if(core.canonicalDigest(meta)!==spec.metadata_sha256||hash(await fs.readFile(spec.cover.file))!==spec.cover.sha256)throw new Error('ARTIFACT_CHANGED');
 const one=async s=>{const x=page.locator(s);if(await x.count()!==1)throw new Error('AMBIGUOUS_FORM');return x;};
 const title=await (await one('input[placeholder="请输入稿件标题"]:visible')).inputValue();
 const description=await core.readNativeDescription(await one('.ql-editor[contenteditable="true"]:visible'));
 const source=await (await one('input[placeholder^="转载视频请注明来源"]:visible')).inputValue();
 const category=(await (await one('.video-human-type .select-controller:visible')).innerText()).trim();
 const statement=await (await one('input[placeholder="请选择符合您视频内容的创作声明"]:visible')).inputValue();
 const tags=(await page.locator('.label-item-v2-container:visible p.label-item-v2-content').allTextContents()).map(x=>x.trim());
 if(title!==meta.title||!description.supported||description.text!==meta.description||source!==meta.source||category!==meta.category_label||statement.trim()!=='内容为转载'||core.canonicalDigest([...tags].sort())!==core.canonicalDigest([...meta.tags].sort()))throw new Error('FORM_READBACK_MISMATCH');
 const dimensions=await page.evaluate(()=>({document_width:document.documentElement.scrollWidth,document_height:document.documentElement.scrollHeight,viewport_width:innerWidth,viewport_height:innerHeight,scrollable_regions:[...document.querySelectorAll('body *')].filter(e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>200&&r.height>100&&e.scrollHeight>e.clientHeight+40&&['auto','scroll'].includes(s.overflowY);}).map(e=>({tag:e.tagName,classes:[...e.classList],scroll_height:e.scrollHeight,client_height:e.clientHeight})).slice(0,20)}));
 if(dimensions.document_height>16000)throw new Error('SCREENSHOT_SIZE_LIMIT');
 const file=path.join(output,'pre-submit-full-form-v1.png');try{await fs.lstat(file);throw new Error('SCREENSHOT_ALREADY_EXISTS');}catch(e){if(e.code!=='ENOENT')throw e;}
 await page.screenshot({path:file,fullPage:true,timeout:20000});
 const result={version:'actual-native-pre-submit-full-form-v1',state:'prepared_not_submitted',captured_at:new Date().toISOString(),job_id:spec.job_id,attempt_id:spec.attempt_id,recovery_id:spec.recovery_id,target_site_uid:spec.target_site_uid,metadata_sha256:spec.metadata_sha256,cover_sha256:spec.cover.sha256,title,description:description.text,source,category,statement,tags,dimensions,screenshot_file:file,screenshot_sha256:hash(await fs.readFile(file)),submitted:false,native_click_performed:false,full_page:true};
 await helpers.write('pre-submit-full-form-v1.json',result);return result;
}
