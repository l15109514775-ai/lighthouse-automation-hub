// Owner-operated ordinary GUI; no credential input, bypass or debug port.
import {chromium} from '../../compositor/node_modules/playwright/index.mjs';
const context=await chromium.launchPersistentContext('/opt/lighthouse-hub/data/browser-profiles/x',{
 headless:false,chromiumSandbox:true,channel:'chromium',viewport:{width:1280,height:1000},locale:'zh-CN',timezoneId:'Asia/Shanghai'});
let closing=false;const close=async()=>{if(!closing){closing=true;await context.close();process.exit(0);}};
process.on('SIGTERM',close);process.on('SIGINT',close);
const page=context.pages()[0]||await context.newPage();
await page.goto('https://x.com/i/flow/login',{waitUntil:'domcontentloaded',timeout:60000}).catch(()=>{});
console.log(JSON.stringify({login_window_open:true,owner_input_required:true}));
await new Promise(resolve=>context.on('close',resolve));
