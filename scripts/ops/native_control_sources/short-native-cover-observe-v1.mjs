export const nativeControlCapabilities=Object.freeze({fill_metadata:true,submit:false,lookup:false});
export async function handleNativeControl({page,spec,helpers,control_action}) {
 if(control_action!=='fill_metadata')return {state:'unsupported',submitted:false,submit_implemented:false};
 let observation=null,error=null;
 try {
  if(await helpers.accountIdentity()!==spec.target_site_uid||!await helpers.checkProtection())throw new Error('COVER_ACCOUNT_OR_PROTECTION');
  const add=page.locator('.cover-main .cover-module.cover-module-main .add-text:visible');
  if(await add.count()!==1||(await add.innerText()).trim()!=='添加封面')throw new Error('COVER_NATIVE_CONTROL_CHANGED');
  observation=await helpers.boundCoverChooser(()=>add.click({timeout:5000}),{select:false});
 }catch(e){error=typeof e?.code==='string'&&/^RECOVERY_[A-Z_]+$/.test(e.code)?e.code:'COVER_NATIVE_UI_CHANGED';}
 const visible=await page.evaluate(()=>{
  const seen=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>1&&r.height>1&&s.display!=='none'&&s.visibility!=='hidden';};
  const shape=e=>({tag:e.tagName,role:e.getAttribute('role'),classes:[...e.classList],text:(e.innerText||'').slice(0,4000),placeholder:e.getAttribute('placeholder')});
  return {text:(document.body.innerText||'').slice(-16000),dialogs:[...document.querySelectorAll('[role="dialog"],[role="alertdialog"],dialog[open],[aria-modal="true"]')].filter(seen).map(shape),
   cover_regions:[...document.querySelectorAll('[class*="cover"],[class*="Cover"]')].filter(seen).slice(-80).map(shape),
   buttons:[...document.querySelectorAll('button,[role="button"]')].filter(seen).map(shape),
   file_inputs:[...document.querySelectorAll('input[type="file"]')].map(e=>({tag:e.tagName,type:e.type,accept:e.getAttribute('accept'),classes:[...e.classList],name:e.name,visible:seen(e)}))};
 });
 const result={version:'short-native-cover-observation-v1',state:'native_cover_ui_observed',observation,error_code:error,visible_dom:visible,
  files_assigned:false,new_video_file_selection:false,cover_verified:false,metadata_ready:false,submitted:false,submit_implemented:false};
 await helpers.write('cover-ui-observation-v1.json',result);return {...result,...await helpers.inventory('native-cover-ui-observed-v1')};
}
