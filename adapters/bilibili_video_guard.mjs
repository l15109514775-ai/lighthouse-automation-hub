// Read-only protection inspection. Never clicks, closes, solves or changes DOM.
export const VIDEO_PROTECTION_GUARD_VERSION='bilibili-video-protection-guard-v3';

export async function inspectVideoProtection(page){
 if(!page||typeof page.evaluate!=='function'||typeof page.isClosed!=='function'||page.isClosed())return {
  blocked:true,error_code:'BILIBILI_VIDEO_PAGE_UNAVAILABLE',
  evidence:{version:VIDEO_PROTECTION_GUARD_VERSION,inspection_failed:true}};
 try{
  return await page.evaluate(version=>{
   const LIMIT=40;
   const evidence={version,known_document_container_excluded:false,native_mp4_uploaders:0,
    visible_dialogs:[],visible_challenge_controls:[],challenge_frames:[],top_level_authentication:false,truncated:false};
   const signals=[];
   // Match the browser's layout visibility without interpreting aria-hidden as
   // permission to ignore an actually rendered protection control.
   function visible(node){
    if(!(node instanceof Element)||!node.isConnected)return false;
    const rect=node.getBoundingClientRect();if(rect.width<=0||rect.height<=0)return false;
    for(let current=node;current;current=current.parentElement){
     const style=getComputedStyle(current);
     if(style.display==='none'||style.visibility==='hidden'||style.visibility==='collapse')return false;
    }
    return true;
   }
   function push(list,value){if(list.length<LIMIT)list.push(value);else evidence.truncated=true;}
   function structure(node){return {tag:node.tagName,role:(node.getAttribute('role')||'').slice(0,30),
    classes:[...node.classList].filter(x=>/^[A-Za-z0-9_-]{1,80}$/.test(x)).slice(0,12)};}
   function frameKind(raw){
    try{
     const url=new URL(raw,document.baseURI);const host=url.hostname.toLowerCase(),pathname=url.pathname.toLowerCase();
     if(host==='passport.bilibili.com'||host.endsWith('.passport.bilibili.com'))return 'login';
     if(/(?:^|[.-])(?:geetest|captcha)(?:[.-]|$)/.test(host)||/(?:^|\/)(?:geetest|captcha)(?:[\/_-]|$)/.test(pathname))return 'verification';
    }catch{}
    return null;
   }
   const topKind=frameKind(location.href);
   if(topKind){evidence.top_level_authentication=true;signals.push(topKind==='login'?'BILIBILI_VIDEO_AUTHENTICATION_REQUIRED':'BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED');}
   const uploaders=[...document.querySelectorAll('input[name="buploader"][type="file"]')].filter(node=>
    visible(node)&&(node.getAttribute('accept')||'').toLowerCase().split(',').map(s=>s.trim()).includes('.mp4'));
   evidence.native_mp4_uploaders=uploaders.length;
   const bodyClasses=document.body?[...document.body.classList].sort():[];
   // The native saved-draft manager uses the same document-only classes but
   // contains no uploader. Recognize that exact official route and visible
   // manager controls; child challenges, dialogs and frames remain checked.
   const draftLinks=[...document.querySelectorAll('a[href]')].filter(node=>{
    if(!visible(node))return false;
    try{const target=new URL(node.getAttribute('href'),document.baseURI);return target.origin==='https://member.bilibili.com'&&target.pathname==='/platform/upload/video/frame';}catch{return false;}
   });
   // The native default "all submissions" tab may be empty while a separate
   // draft tab contains the saved item. Its three observed canonical manager
   // navigation anchors identify this document before an edit link is shown.
   // This exception applies only to BODY; every child protection signal below
   // is still inspected normally.
   const managerNavigation=[
    ['/platform/upload-manager/article','视频管理'],
    ['/platform/upload-manager/opus','图文管理'],
    ['/platform/upload-manager/inactArticle','互动视频管理']
   ];
   const canonicalManagerNavigation=managerNavigation.every(([pathname,label])=>
    [...document.querySelectorAll('a[href]')].some(node=>{
     if(!visible(node)||(node.innerText||'').trim()!==label)return false;
     try{const target=new URL(node.getAttribute('href'),document.baseURI);return target.origin==='https://member.bilibili.com'&&target.pathname===pathname&&!target.search&&!target.hash&&!target.username&&!target.password;}catch{return false;}
    }));
   const managerText=(document.body?.innerText||'').slice(0,30000);
   const knownManager=location.origin==='https://member.bilibili.com'&&location.pathname==='/platform/upload-manager/article'&&
    ((draftLinks.length>=1&&draftLinks.length<=60)||canonicalManagerNavigation)&&managerText.includes('视频管理')&&managerText.includes('稿件管理')&&/草稿\s+\d+/.test(managerText);
   const knownBody=document.body&&bodyClasses.length===2&&bodyClasses[0]==='risk-captcha-adapt'&&
    bodyClasses[1]==='risk-captcha-adapt-pc'&&(uploaders.length>0||knownManager);
   evidence.known_document_container_excluded=Boolean(knownBody);
   evidence.native_draft_manager_excluded=Boolean(knownBody&&knownManager);
   for(const node of document.querySelectorAll('[role="dialog"],[role="alertdialog"],dialog[open],[aria-modal="true"]')){
    if(!visible(node))continue;
    push(evidence.visible_dialogs,structure(node));
   }
   if(evidence.visible_dialogs.length)signals.push('BILIBILI_VIDEO_VISIBLE_DIALOG_REQUIRES_REVIEW');
   // Body is a specifically observed document container, never a blanket
   // exception for a child dialog/control with the same protection classes.
   for(const node of document.querySelectorAll('[class*="captcha"],[id*="captcha"],[class*="geetest"],[id*="geetest"],[data-testid*="captcha"],[data-testid*="geetest"]')){
    if(node===document.body&&knownBody)continue;
    if(!visible(node))continue;
    push(evidence.visible_challenge_controls,structure(node));
   }
   if(evidence.visible_challenge_controls.length)signals.push('BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED');
   for(const node of document.querySelectorAll('iframe')){
    const kind=frameKind(node.getAttribute('src')||'');
    if(!kind||!visible(node))continue;
    // No signed URLs, query strings, fragments or contents are returned.
    push(evidence.challenge_frames,{tag:'IFRAME',kind});
    signals.push(kind==='login'?'BILIBILI_VIDEO_AUTHENTICATION_REQUIRED':'BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED');
   }
   const error_code=signals.includes('BILIBILI_VIDEO_AUTHENTICATION_REQUIRED')?'BILIBILI_VIDEO_AUTHENTICATION_REQUIRED':
    signals.includes('BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED')?'BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED':
    signals.includes('BILIBILI_VIDEO_VISIBLE_DIALOG_REQUIRES_REVIEW')?'BILIBILI_VIDEO_VISIBLE_DIALOG_REQUIRES_REVIEW':null;
   return {blocked:error_code!==null,error_code,evidence};
  },VIDEO_PROTECTION_GUARD_VERSION);
 }catch{
  // Do not stringify browser errors, which can contain URLs or account data.
  return {blocked:true,error_code:'BILIBILI_VIDEO_PROTECTION_INSPECTION_FAILED',
   evidence:{version:VIDEO_PROTECTION_GUARD_VERSION,inspection_failed:true}};
 }
}
