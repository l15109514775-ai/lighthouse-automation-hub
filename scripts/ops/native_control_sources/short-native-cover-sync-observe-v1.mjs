export const nativeControlCapabilities=Object.freeze({fill_metadata:true,submit:false,lookup:false});
export async function handleNativeControl({page,spec,helpers,control_action}){
 if(control_action!=='fill_metadata')return {state:'unsupported',submitted:false};
 if(!await helpers.checkProtection()||await helpers.accountIdentity()!==spec.target_site_uid)throw new Error('ACCOUNT_REQUIRED');
 const visible=await page.evaluate(()=>{
  const seen=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>1&&r.height>1&&s.display!=='none'&&s.visibility!=='hidden';};
  const shape=e=>({tag:e.tagName,classes:[...e.classList],role:e.getAttribute('role'),text:(e.innerText||'').slice(0,1600)});
  return {text:document.body.innerText.slice(-7000),matches:[...document.querySelectorAll('body *')].filter(e=>seen(e)&&['确认同步','不同步，手动编辑'].includes((e.innerText||'').trim())).map(e=>({...shape(e),parent:shape(e.parentElement)}))};
 });
 const result={version:'native-cover-sync-observation-v1',state:'observed',visible_dom:visible,submitted:false,native_click_performed:false};await helpers.write('native-cover-sync-observation-v1.json',result);return result;
}
