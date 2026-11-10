// Read/navigation only: an already observed manager page and its exact title link.
import {inspectVideoProtection} from './bilibili_video_guard.mjs';
export const nativeControlCapabilities=Object.freeze({lookup:true,fill_metadata:false,submit:false,sync_metadata_revision:false});
export async function handleNativeControl({page,spec,output,control_action,helpers}){
 const base={version:'native-saved-draft-navigation-v3',job_id:spec.job_id,attempt_id:spec.attempt_id,recovery_id:spec.recovery_id,
  submitted:false,submit_implemented:false,metadata_modified:false,new_video_file_selection:false};
 if(control_action!=='lookup')return {...base,state:'unsupported'};
 let phase='manager_readiness';
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
   for(let i=0;i<texts.length;i++)if(texts[i].trim()===spec.metadata.title||texts[i].trim()===spec.metadata.title.replace(/ +/g,' '))matches.push(links.nth(i));
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
  if(matches.length===0||spec.require_saved_draft===true){
   phase='draft_tab';
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
  // The current native draft list is paginated. Check the complete bounded
  // inventory before choosing a title; never infer uniqueness from page one.
  const cards=page.locator('section.bcc-cmad-card.card:visible');
  if(await cards.count()){
   phase='draft_inventory';
   const tab=page.getByText(/^草稿\s+\d+$/,{exact:true}).filter({visible:true});
   if(await tab.count()!==1)throw new Error('draft-count');
   const total=Number((await tab.innerText()).match(/\d+/)?.[0]);
   if(!Number.isInteger(total)||total<1||total>60)throw new Error('draft-count');
   const selected=page.locator('li.bcc-pagination-item.selected > a:visible');
   const pageLink=n=>page.locator('li.bcc-pagination-item > a:visible').filter({hasText:new RegExp('^\\s*'+n+'\\s*$')});
   async function selectPage(n){
    const link=pageLink(n);if(await link.count()!==1)throw new Error('draft-pagination');
    const previous=await cards.allTextContents();
    await protectedIdentity();await link.click({timeout:5000});
    const end=Date.now()+10000;
    do{if(await selected.count()===1&&(await selected.innerText()).trim()===String(n)&&await cards.count()&&
     JSON.stringify(await cards.allTextContents())!==JSON.stringify(previous))return;
     await page.waitForTimeout(200);
    }while(Date.now()<end);throw new Error('draft-pagination-timeout');
   }
   if(await selected.count()===1&&(await selected.innerText()).trim()!=='1')await selectPage(1);
   const rows=[];let chosenPage=null,found=0;
   for(let n=1;n<=6;n++){
    await protectedIdentity();
    if(n>1||total>await cards.count())if(await selected.count()!==1||(await selected.innerText()).trim()!==String(n))throw new Error('draft-page');
    const current=await cards.evaluateAll(es=>es.map(e=>({title:e.querySelector('div.bcc-cmadc-title-time > p.title')?.getAttribute('title'),
     text:e.querySelector('div.bcc-cmadc-title-time > p.title a')?.innerText?.trim()})));
    // HTML rendering collapses repeated ASCII spaces in unrelated draft
    // titles too. The native title attribute and final editable title remain
    // exact; only their visible rendering receives this normalization.
    if(current.some(r=>!r.title||r.text!==r.title&&r.text!==r.title.replace(/ +/g,' ')))throw new Error('draft-card-title');
    rows.push(...current);for(const r of current)if(r.title===spec.metadata.title){found++;chosenPage=n;}
    if(rows.length===total)break;if(rows.length>total||n===6)throw new Error('draft-inventory');
    await selectPage(n+1);
   }
   if(rows.length!==total||found!==1)throw new Error('unique-title');
   if(await selected.count()===1&&(await selected.innerText()).trim()!==String(chosenPage))await selectPage(chosenPage);
   matches=await matchingTitleLinks();
  }
  if(matches.length!==1)throw new Error('unique-title');
  await protectedIdentity();matches=await matchingTitleLinks();if(matches.length!==1)throw new Error('unique-title');
  const href=await matches[0].getAttribute('href');
  phase='draft_navigation';
  if(href&&!['#','javascript:;','javascript:void(0)'].includes(href)){
   const target=new URL(href,page.url());if(target.origin!==origin||target.pathname!==editor||target.username||target.password)throw new Error('native-href');
   if(target.search){
    await page.goto(target.href,{waitUntil:'domcontentloaded',timeout:30000});
   }else{
    // Current cards expose a generic href; their native click handler supplies
    // the saved draft ID. Opening the href directly discards that selection.
    const opened=[];const onPopup=p=>opened.push(p);page.on('popup',onPopup);
    try{
     await matches[0].click({timeout:5000});const end=Date.now()+15000;let observed=null;
     do{
      if(opened.length>1)throw new Error('draft-popup-ambiguous');
      const active=opened[0]||page,current=new URL(active.url());
      if(current.href!=='about:blank'&&current.origin!==origin)throw new Error('draft-popup-origin');
      if(current.origin===origin&&current.pathname===editor&&current.searchParams.get('type')==='draft'&&/^\d+$/.test(current.searchParams.get('draftId')||'')){
       if((await inspectVideoProtection(active)).blocked)throw new Error('protection');
       const title=active.locator('input[placeholder="请输入稿件标题"]:visible');
       if(await title.count()===1&&await title.inputValue()===spec.metadata.title){observed=current.href;break;}
      }
      await page.waitForTimeout(200);
     }while(Date.now()<end);
     if(!observed)throw new Error('native-draft-not-confirmed');
     // Keep the caller's controlled page. This URL was observed after the
     // actual native click and matching title, never synthesized from a job.
     if(opened.length){await protectedIdentity();await page.goto(observed,{waitUntil:'domcontentloaded',timeout:30000});}
    }finally{page.off('popup',onPopup);for(const p of opened)await p.close().catch(()=>{});}
   }
  }else await matches[0].click({timeout:5000});
  phase='editor_identity';
  const end=Date.now()+15000;do{u=new URL(page.url());if(u.origin!==origin)throw new Error('origin');const nativeTitle=page.locator('input[placeholder="请输入稿件标题"]:visible');if(u.pathname===editor&&await nativeTitle.count()===1&&await nativeTitle.inputValue()===spec.metadata.title)break;await page.waitForTimeout(200);}while(Date.now()<end);
  if(u.pathname!==editor||!await helpers.checkProtection()||await helpers.accountIdentity()!==spec.target_site_uid)throw new Error('editor-not-confirmed');
  const title=page.locator('input[placeholder="请输入稿件标题"]:visible');if(await title.count()!==1||await title.inputValue()!==spec.metadata.title)throw new Error('editor-title');
  const result={...base,state:'saved_draft_editor_opened',native_draft_tab_clicked:draftTabClicked,source_identity:'same_account_unique_native_title_link_only_not_remote_file_hash',server_file_hash_verified:false};
  await helpers.write('native-saved-draft-navigation.json',result);return {...result,...await helpers.inventory('native-saved-draft-editor-opened')};
 }catch{
  const result={...base,state:'navigation_unknown',error_code:'NATIVE_SAVED_DRAFT_NAVIGATION_NOT_CONFIRMED',failure_phase:phase};
  await helpers.write('native-saved-draft-navigation.json',result);await helpers.inventory('native-saved-draft-navigation-unknown').catch(()=>{});return result;
 }
}
