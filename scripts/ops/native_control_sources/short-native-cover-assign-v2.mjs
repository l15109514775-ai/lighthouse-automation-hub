export const nativeControlCapabilities=Object.freeze({fill_metadata:true,submit:false,lookup:false});
export async function handleNativeControl({page,spec,helpers,control_action}) {
 if(control_action!=='fill_metadata')return {state:'unsupported',submitted:false,submit_implemented:false};
 let assigned=null,error=null;
 try {
  if(await helpers.accountIdentity()!==spec.target_site_uid||!await helpers.checkProtection())throw new Error('COVER_ACCOUNT_OR_PROTECTION');
  const editor=page.locator('.cover-editor.bcc-dialog__wrap-mask:visible');
  if(await editor.count()!==1||!(await editor.innerText()).includes('封面制作'))throw new Error('COVER_NATIVE_EDITOR_CHANGED');
  const upload=editor.locator('.cover-upload.cover-editor-panel-select-item:visible');
  if(await upload.count()!==1||!(await upload.innerText()).includes('上传封面'))throw new Error('COVER_NATIVE_UPLOAD_CHANGED');
  assigned=await helpers.boundCoverChooser(()=>upload.click({timeout:5000}),{select:true});
  await page.waitForTimeout(1200);
 }catch(e){error=typeof e?.code==='string'&&/^RECOVERY_[A-Z_]+$/.test(e.code)?e.code:'COVER_NATIVE_UI_CHANGED';}
 const visible=await page.evaluate(()=>{
  const seen=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>1&&r.height>1&&s.display!=='none'&&s.visibility!=='hidden';};
  const shape=e=>({tag:e.tagName,role:e.getAttribute('role'),classes:[...e.classList],text:(e.innerText||'').slice(0,500)});
  return {editor_text:(document.querySelector('.cover-editor')?.innerText||'').slice(0,10000),
   controls:[...document.querySelectorAll('.cover-editor span,.cover-editor button,.cover-editor [role="button"],.cover-editor [class*="button"],.cover-editor [class*="crop"]')].filter(seen).slice(0,100).map(shape),
   images:[...document.querySelectorAll('.cover-editor img')].filter(seen).map(e=>({classes:[...e.classList],alt:e.alt,width:e.naturalWidth,height:e.naturalHeight,source_kind:e.src.startsWith('blob:')?'blob':e.src.startsWith('data:')?'inline':'remote'})),
   canvases:[...document.querySelectorAll('.cover-editor canvas')].filter(seen).map(e=>({classes:[...e.classList],width:e.width,height:e.height}))};
 });
 const result={version:'native-bound-cover-assignment-v2',state:assigned?.files_assigned?'bound_cover_assigned_preview_pending':'cover_assignment_unconfirmed',
  observation:assigned,error_code:error,visible_dom:visible,cover_source:spec.cover,cover_verified:false,metadata_ready:false,new_video_file_selection:false,submitted:false,submit_implemented:false};
 await helpers.write('cover-image-assigned-v2.json',result);
 return {state:result.state,error_code:error,observation:assigned,cover_verified:false,metadata_ready:false,submitted:false,submit_implemented:false,
  ...await helpers.inventory('bound-cover-image-assigned-v2')};
}
