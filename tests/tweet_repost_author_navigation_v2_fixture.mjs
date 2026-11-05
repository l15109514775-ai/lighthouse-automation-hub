// Real Chromium, exact observed SPAN/sidebar structure; every HTTP is owned.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {observeRepostControl} from '../adapters/browser_tweet_repost_check_v1.mjs';
const [scenario,folder,requestFile]=process.argv.slice(2),rawRequest=await fs.readFile(requestFile),spec=JSON.parse(rawRequest);
const hash=x=>crypto.createHash('sha256').update(x).digest('hex');
const counts={author:0,repost:0,final:0};let profileRequests=0;
const browser=await chromium.launch({headless:true,chromiumSandbox:true});
try{
 const context=await browser.newContext({viewport:{width:1280,height:900}});
 await context.exposeFunction('ownedAction',name=>{counts[name]++;});
 await context.route('**/*',async route=>{
  const url=new URL(route.request().url());
  const isProfile=url.hostname==='space.bilibili.com';if(isProfile)profileRequests++;
  const esc=x=>x.replaceAll('&','&amp;').replaceAll('<','&lt;');
  let card='',sidebar='',script='';
  if(url.hostname==='t.bilibili.com'&&url.pathname==='/'+spec.external_id){
   const author='<span class="bili-dyn-title__text fs-large default">'+esc(spec.target_name)+'</span>';
   const caption=scenario==='return_changed'&&profileRequests?'Changed caption '+spec.source_url:spec.caption;
   card='<div class="bili-dyn-item"><div class="bili-dyn-item__main"><div class="bili-dyn-item__avatar"><div class="b-avatar"></div></div>'+
    '<div class="bili-dyn-item__header"><div class="bili-dyn-title">'+author+(scenario==='duplicate_author'?author:'')+'</div></div>'+
    '<div class="bili-dyn-item__body">'+esc(caption)+'</div></div></div>';
   const forward='<div class="side-toolbar__action forward"><svg></svg><div class="side-toolbar__action__text">0</div></div>';
   sidebar='<div class="side-toolbar"><div class="side-toolbar__box"><div class="side-toolbar__action like">0</div>'+forward+
    (scenario==='duplicate_sidebar'?forward:'')+'<div class="side-toolbar__action comment">0</div></div></div>';
   const profile='https://space.bilibili.com/'+(scenario==='wrong_author'?'10001':spec.target_site_uid)+'/dynamic?spm_id_from=owned';
   const navigate=scenario==='same_tab'?'location.href='+JSON.stringify(profile):'window.open('+JSON.stringify(profile)+',"_blank")';
   const response=scenario==='denied'?
    'document.body.insertAdjacentHTML("beforeend",\'<div role="alert">动态正在审核中，暂时无法转发</div>\')':
    'document.body.insertAdjacentHTML("beforeend",\'<div role="dialog"><h3>转发动态</h3><textarea placeholder="说点什么"></textarea><button id="final-send">转发</button></div>\');document.querySelector("#final-send").onclick=()=>window.ownedAction("final")';
   script='<script>for(const n of document.querySelectorAll(".bili-dyn-title__text"))n.onclick=()=>{window.ownedAction("author");'+navigate+';};'+
    'for(const n of document.querySelectorAll(".side-toolbar__action.forward"))n.onclick=()=>{window.ownedAction("repost");'+response+';};</script>';
  }
  const challenge=isProfile&&scenario==='profile_challenge'?'<div role="dialog" class="geetest_panel">请完成人机验证</div>':'';
  const body='<!doctype html><html><head><meta charset="utf-8"></head><body><a class="header-entry-mini" href="https://space.bilibili.com/'+spec.target_site_uid+'">Avatar</a>'+
   card+sidebar+challenge+script+'</body></html>';
  await route.fulfill({status:200,contentType:'text/html; charset=utf-8',body});
 });
 const page=await context.newPage();
 if(scenario==='cached_author'){await fs.mkdir(folder,{recursive:true});await fs.writeFile(path.join(folder,'author-navigation-intent.json'),'Owned retained author navigation intent');}
 const ref=await observeRepostControl(page,spec,folder,{readinessMs:1000,requestSHA:hash(rawRequest)});
 const body=JSON.parse(await fs.readFile(ref.file));assert.equal(hash(await fs.readFile(ref.file)),ref.sha256);
 const expected={popup:'dialog_opened',same_tab:'dialog_opened',wrong_author:'author_identity_mismatch',return_changed:'author_navigation_unconfirmed',
  profile_challenge:'protection_blocked',duplicate_sidebar:'ui_unrecognized',cached_author:'author_navigation_unconfirmed',denied:'native_denied',duplicate_author:'ui_unrecognized'};
 const author=['cached_author','duplicate_author'].includes(scenario)?0:1;
 const repost=['popup','same_tab','denied'].includes(scenario)?1:0;
 assert.equal(body.version,'tweet-native-repost-observation-v2');assert.equal(body.status,expected[scenario]);
 assert.deepEqual(counts,{author,repost,final:0});
 assert.deepEqual(body.actions,{clicks:author+repost,author_navigation_clicks:author,repost_dialog_clicks:repost,file_selections:0,fills:0,submits:0});
 if(repost){assert.equal(body.author_identity.profile_url,'https://space.bilibili.com/'+spec.target_site_uid+'/dynamic');assert.equal(body.author_identity.return_binding_verified,true);
  assert.equal(body.controls[0].surface,'detail_sidebar_forward_v2');assert.equal(body.author_identity.site_uid,spec.target_site_uid);}
 if(scenario==='cached_author')assert.equal(await fs.readFile(path.join(folder,'author-navigation-intent.json'),'utf8'),'Owned retained author navigation intent');
 console.log(JSON.stringify({scenario,owned:true,counts,result_reference:ref}));
}finally{await browser.close();}
