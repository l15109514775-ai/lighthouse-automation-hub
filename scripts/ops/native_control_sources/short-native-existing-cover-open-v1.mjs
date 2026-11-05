export const nativeControlCapabilities=Object.freeze({fill_metadata:true,submit:false,lookup:false});
export async function handleNativeControl({page,spec,helpers,control_action}){
 if(control_action!=='fill_metadata')return {state:'unsupported',submitted:false};
 const u=new URL(page.url());
 if(u.origin!=='https://member.bilibili.com'||u.pathname!=='/platform/upload/video/frame'||
  !await helpers.checkProtection()||await helpers.accountIdentity()!==spec.target_site_uid)throw new Error('NATIVE_ACCOUNT_REQUIRED');
 await helpers.checkedFile();
 const title=page.locator('input[placeholder="请输入稿件标题"]:visible');
 if(await title.count()!==1||await title.inputValue()!==spec.metadata.title)throw new Error('NATIVE_TITLE_CHANGED');
 const existing=page.locator('.cover-main .cover-module.cover-module-main:visible');
 if(await existing.count()!==1||await existing.locator('.cover-img:visible').count()!==1||await page.locator('.cover-editor.bcc-dialog__wrap-mask:visible').count())throw new Error('EXISTING_COVER_REQUIRED');
 const observation=await helpers.boundCoverChooser(()=>existing.click({timeout:5000}),{select:false});
 const editor=page.locator('.cover-editor.bcc-dialog__wrap-mask:visible');
 if(await editor.count()!==1||!(await editor.innerText()).includes('封面制作'))throw new Error('NATIVE_COVER_EDITOR_NOT_OBSERVED');
 const result={version:'native-existing-cover-open-v1',state:'existing_cover_editor_opened',observation,new_video_file_selection:false,cover_verified:false,submitted:false,metadata_modified:false};
 await helpers.write('native-existing-cover-open-v1.json',result);return {...result,...await helpers.inventory('existing-cover-editor-opened-v1')};
}
