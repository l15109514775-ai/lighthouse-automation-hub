// Read/navigation only: an already observed manager page and its exact title link.
import {inspectVideoProtection} from './bilibili_video_guard.mjs';
export const nativeControlCapabilities=Object.freeze({lookup:true,fill_metadata:false,submit:false,sync_metadata_revision:false});
export async function handleNativeControl({page,spec,output,control_action,helpers}){
 const base={version:'native-saved-draft-navigation-v3',job_id:spec.job_id,attempt_id:spec.attempt_id,recovery_id:spec.recovery_id,
  submitted:false,submit_implemented:false,metadata_modified:false,new_video_file_selection:false};
 if(control_action!=='lookup')return {...base,state:'unsupported'};
 try{
  const origin='https://member.bilibili.com',manager='/platform/upload-manager/article',editor='/platform/upload/video/frame';
  let u=new URL(page.url());if(u.origin!==origin)throw new Error('origin');
  async function protectedIdentity(){
   if(!await helpers.checkProtection()||await helpers.accountIdentity()!==spec.target_site_uid||!await helpers.checkProtection())throw new Error('protection');
  }
  if(u.pathname!==manager){await protectedIdentity();await page.goto(origin+manager,{waitUntil:'domcontentloaded',timeout:30000});}
  async function matchingTitleLinks(){
   const links=page.locator('a:visible'),matches=[];
   const texts=await links.allTextContents();
   for(let i=0;i<texts.length;i++)if(texts[i].trim()===spec.metadata.title)matches.push(links.nth(i));
   return matches;
  }
  function loadingBodyOnly(protection){
   const e=protection?.evidence,c=e?.visible_challenge_controls;
   return protection?.blocked===true&&protection.error_code==='BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED'&&
    e?.version==='bilibili-video-protection-guard-v3'&&e.inspection_failed!==true&&e.truncated===false&&
    e.native_mp4_uploaders===0&&e.top_level_authentication===false&&
    e.known_document_container_excluded===false&&e.native_draft_manager_excluded===false&&
    Array.isArray(e.visible_dialogs)&&e.visible_dialogs.length===0&&Array.isArray(e.challenge_frames)&&e.challenge_frames.length===0&&
    Array.isArray(c)&&c.length===1&&c[0].tag==='BODY'&&c[0].role===''&&Array.isArray(c[0].classes)&&
    [...c[0].classes].sort().join(',')==='risk-captcha-adapt,risk-captcha-adapt-pc';
  }
  const readyStart=performance.now(),readyEnd=readyStart+20000,polls=[];
  let matches=[],draftTabClicked=false,ready=false;
  do{
   const current=new URL(page.url());if(current.origin!==origin||current.pathname!==manager)throw new Error('manager-origin');
   const protection=await inspectVideoProtection(page);
   if(protection.blocked&&!loadingBodyOnly(protection))throw new Error('protection');
   matches=await matchingTitleLinks();
   const draftCount=await page.getByText(/^草稿\s+\d+$/,{exact:true}).filter({visible:true}).count();
   polls.push({elapsed_ms:Math.round(performance.now()-readyStart),title_count:matches.length,draft_tab_count:draftCount,
    guard_blocked:protection.blocked,startup_body_marker_only:loadingBodyOnly(protection)});
   if(matches.length>1||draftCount>1)throw new Error('manager-ambiguous');
   if(!protection.blocked&&(matches.length===1||draftCount===1)){ready=true;break;}
   const remaining=readyEnd-performance.now();if(remaining<=0)break;await page.waitForTimeout(Math.min(200,remaining));
  }while(performance.now()<=readyEnd);
  await helpers.write('native-saved-draft-manager-readiness.json',{version:'native-saved-draft-manager-readiness-v1',
   ready,polls,elapsed_ms:Math.round(performance.now()-readyStart),maximum_wait_ms:20000,passive_wait_only:true});
  if(!ready)throw new Error('manager-timeout');
  // Only passive reads above may observe the precisely identified startup BODY.
  // Every subsequent native navigation still requires the original guard and UID.
  await protectedIdentity();
  matches=await matchingTitleLinks();if(matches.length>1)throw new Error('unique-title');
  if(matches.length===0){
   // A saved draft is on the native draft tab, not necessarily the default
   // empty "all submissions" list. This is navigation only: do not create,
   // delete, edit, save or submit any content here.
   const draftTab=page.getByText(/^草稿\s+\d+$/,{exact:true}).filter({visible:true});
   if(await draftTab.count()!==1)throw new Error('unique-draft-tab');
   await protectedIdentity();
   if(await draftTab.count()!==1)throw new Error('unique-draft-tab');
   await draftTab.click({timeout:5000});draftTabClicked=true;
   const tabEnd=Date.now()+15000;
   do{
    const current=new URL(page.url());if(current.origin!==origin||current.pathname!==manager)throw new Error('manager-origin');
    if(!await helpers.checkProtection())throw new Error('protection');
    matches=await matchingTitleLinks();if(matches.length>0)break;
    await page.waitForTimeout(200);
   }while(Date.now()<tabEnd);
   await protectedIdentity();
  }
  if(matches.length!==1)throw new Error('unique-title');
  await protectedIdentity();matches=await matchingTitleLinks();if(matches.length!==1)throw new Error('unique-title');
  const href=await matches[0].getAttribute('href');
  if(href&&!['#','javascript:;','javascript:void(0)'].includes(href)){
   const target=new URL(href,page.url());if(target.origin!==origin||target.pathname!==editor||target.username||target.password)throw new Error('native-href');
   // Navigate to the actual DOM URL in the same controlled tab, even if the
   // native anchor uses target=_blank. No hidden endpoint or guessed draft ID.
   await page.goto(target.href,{waitUntil:'domcontentloaded',timeout:30000});
  }else await matches[0].click({timeout:5000});
  const end=Date.now()+15000;do{u=new URL(page.url());if(u.origin!==origin)throw new Error('origin');const nativeTitle=page.locator('input[placeholder="请输入稿件标题"]:visible');if(u.pathname===editor&&await nativeTitle.count()===1&&await nativeTitle.inputValue()===spec.metadata.title)break;await page.waitForTimeout(200);}while(Date.now()<end);
  if(u.pathname!==editor||!await helpers.checkProtection()||await helpers.accountIdentity()!==spec.target_site_uid)throw new Error('editor-not-confirmed');
  const title=page.locator('input[placeholder="请输入稿件标题"]:visible');if(await title.count()!==1||await title.inputValue()!==spec.metadata.title)throw new Error('editor-title');
  const result={...base,state:'saved_draft_editor_opened',native_draft_tab_clicked:draftTabClicked,source_identity:'same_account_unique_native_title_link_only_not_remote_file_hash',server_file_hash_verified:false};
  await helpers.write('native-saved-draft-navigation.json',result);return {...result,...await helpers.inventory('native-saved-draft-editor-opened')};
 }catch{
  const result={...base,state:'navigation_unknown',error_code:'NATIVE_SAVED_DRAFT_NAVIGATION_NOT_CONFIRMED'};
  await helpers.write('native-saved-draft-navigation.json',result);await helpers.inventory('native-saved-draft-navigation-unknown').catch(()=>{});return result;
 }
}
