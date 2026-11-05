// Ordinary public-page collection in a fresh context for each bounded run.
// Chromium's own temporary runtime profile is discarded with the browser. No
// application profile, stored state or account credentials are supplied.
import {chromium} from '../compositor/node_modules/playwright/index.mjs';

export const PUBLIC_X_AUTHENTICATION_MODE='public_anonymous_ephemeral';

export function publicXAccessError(url,status){
 if(status===403)return 'X_WEB_ACCESS_DENIED';
 if(status===429)return 'X_WEB_RATE_LIMITED';
 if(status>=500)return 'X_WEB_UPSTREAM_UNAVAILABLE';
 if(status>=400)return 'X_WEB_PAGE_UNAVAILABLE';
 try{const u=new URL(url);if(['x.com','www.x.com','twitter.com','www.twitter.com'].includes(u.hostname)&&
  /^(?:\/i\/flow)?\/(?:login|signup)(?:\/|$)/.test(u.pathname))return 'X_WEB_LOGIN_REQUIRED_PUBLIC_MODE';}catch{}
 return null;
}

export async function createPublicXSession(options={}){
 if(!options||typeof options!=='object'||Array.isArray(options)||
  Object.keys(options).some(k=>k!=='headless')||
  (options.headless!==undefined&&typeof options.headless!=='boolean'))
  throw new TypeError('PUBLIC_X_BROWSER_OPTION_INVALID');
 // The headless override exists only for offline owned-page tests. Production
 // never passes options and always uses the ordinary headed full Chromium.
 const browser=await chromium.launch({headless:options.headless??false,chromiumSandbox:true,channel:'chromium'});
 let context;
 try{
  context=await browser.newContext({viewport:{width:1280,height:1600},locale:'zh-CN',timezoneId:'Asia/Shanghai'});
  context.setDefaultTimeout(10000);context.setDefaultNavigationTimeout(45000);
 }catch(error){await browser.close();throw error;}
 return {browser,context,authentication_mode:PUBLIC_X_AUTHENTICATION_MODE,persistent_profile_used:false,
  async close(){try{await context.close();}finally{await browser.close();}}};
}
