// Owned contenteditable only. No account, publisher, profile or external page.
import assert from 'node:assert/strict';
import {chromium} from '../compositor/node_modules/playwright/index.mjs';
import {insertCaptionText} from '../adapters/browser_caption_input.mjs';

const cases=[
 ['single-line','自有草稿 https://x.com/example_handle_a/status/123'],
 ['source-and-media-note','原帖：https://x.com/example_handle_a/status/123\n附图来自官方API所列素材；图片内文字未翻译。'],
 ['several-lines','第一行\n第二行\n第三行'],
 ['interior-blank-lines','首行\n\n中间\n\n\n末行'],
 ['emoji-and-japanese','示例主播A 🥳🐉\nありがとう！\n1日の終わり 20:30'],
 ['urls-and-query','https://x.com/example-channel-b/status/123\nhttps://example.test/?a=1&b=2'],
 ['html-is-plain-text','<script>window.ownedInjected=true</script>\n<img src="owned" onerror="window.ownedInjected=true">'],
 ['markup-and-entities','<b>字面文本</b> &amp; &lt;\n"双引号" \'单引号\''],
];
let external=0;const passed=[],origin='https://t.bilibili.com',ownedURL=origin+'/__owned-caption-fixture';
const browser=await chromium.launch({headless:true});
try{
 const page=await browser.newPage({viewport:{width:1000,height:800}});
 const html='<meta charset="utf-8"><style>#editor{white-space:pre-wrap;min-height:180px;width:800px;border:1px solid #bbb}</style><input id="unrelated" value="Keep owned title"><div id="editor" contenteditable="true">Owned previous draft</div><script>window.ownedInjected=false;window.ownedInputEvents=[];window.ownedPasteEvents=[];const editor=document.querySelector("#editor");editor.addEventListener("input",e=>window.ownedInputEvents.push({trusted:e.isTrusted,type:e.inputType,data:e.data}));editor.addEventListener("paste",e=>window.ownedPasteEvents.push({trusted:e.isTrusted,text:e.clipboardData.getData("text/plain"),types:[...e.clipboardData.types]}));</script>';
 await page.route('**/*',route=>{
  if(route.request().url()===ownedURL)return route.fulfill({status:200,contentType:'text/html; charset=utf-8',body:html});
  external++;return route.abort();
 });
 await page.goto(ownedURL);
 const body=page.locator('#editor'),realClick=body.click.bind(body),realEvaluate=body.evaluate.bind(body),realPress=page.keyboard.press.bind(page.keyboard),
  context=page.context(),realGrant=context.grantPermissions.bind(context);
 let actions=[],selectionFault=null,selectionReads=0;
 context.grantPermissions=async(permissions,scope)=>{actions.push(['grant',permissions,scope]);return realGrant(permissions,scope);};
 body.click=async()=>{actions.push(['click']);return realClick();};
 body.evaluate=async(...args)=>{
  const value=await realEvaluate(...args);
  if(value&&typeof value==='object'&&Object.hasOwn(value,'anchor_inside')){
   selectionReads++;
   if(selectionFault?.read===selectionReads)return {...value,...selectionFault.fields};
  }
  return value;
 };
 page.keyboard.press=async value=>{actions.push(['press',value]);return realPress(value);};
 body.focus=body.fill=body.press=body.pressSequentially=page.keyboard.insertText=async()=>{throw new Error('OWNED_NON_PASTE_INPUT_FORBIDDEN');};
 for(const [name,caption]of cases){
  await page.locator('#unrelated').focus();
  actions=[];selectionReads=0;await insertCaptionText(page,body,caption,{clipboardOrigin:origin});
  assert.equal(selectionReads,3,name+' checked caret before select, after select and before paste');
  assert.deepEqual(actions,[['grant',['clipboard-read','clipboard-write'],{origin}],['click'],
   ['press','Control+A'],['press','Backspace'],['click'],['press','Control+V']]);
  assert.equal(await body.innerText(),caption,name);
  assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),caption,name+' plaintext clipboard');
  await page.locator('#unrelated').focus();
  assert.equal(await body.innerText(),caption,name+' after blur');
  const paste=await page.evaluate(()=>window.ownedPasteEvents.at(-1));
  assert.deepEqual(paste,{trusted:true,text:caption,types:['text/plain']},name+' native plaintext paste');
  assert.equal(await body.locator('script,img,b').count(),0);
  assert.equal(await page.evaluate(()=>window.ownedInjected),false);
  assert.equal(await page.locator('#unrelated').inputValue(),'Keep owned title');
  passed.push(name);
 }
 assert.equal((await page.evaluate(()=>window.ownedPasteEvents)).length,cases.length);
 assert.ok((await page.evaluate(()=>window.ownedInputEvents)).some(e=>e.trusted===true&&e.type==='insertFromPaste'));
 for(const [name,caption]of [['null',null],['number',12],['object',{}],['oversize','字'.repeat(10001)]]){
  actions=[];const before=await body.innerText();
  await assert.rejects(()=>insertCaptionText(page,body,caption,{clipboardOrigin:origin}),/SPEC_INVALID/);
  assert.deepEqual(actions,[]);assert.equal(await body.innerText(),before);passed.push('invalid-'+name);
 }
 // Bad or unscoped permission requests must fail before any UI/clipboard action.
 for(const options of [undefined,{clipboardOrigin:'https://example.test'},{clipboardOrigin:'https://t.bilibili.com.evil.test'}]){
  actions=[];await assert.rejects(()=>insertCaptionText(page,body,'owned',options),/SPEC_INVALID/);assert.deepEqual(actions,[]);
 }
 const guardNegativeCases=[];
 // Synthetic read faults exercise fail-closed guards after real UI clicks.
 // They never change the DOM, selection, trusted events or browser evidence.
 for(const [name,read,fields,stage]of [
  ['focus-not-active',1,{active:false},'focus'],
  ['selection-absent',1,{has_selection:false,anchor_inside:false,focus_inside:false},'focus'],
  ['selected-range-outside',2,{focus_inside:false},'select'],
 ]){
  await page.locator('#unrelated').focus();
  const before=await body.innerText();actions=[];selectionReads=0;selectionFault={read,fields};
  await assert.rejects(()=>insertCaptionText(page,body,'owned must not replace caption',{clipboardOrigin:origin}),
   error=>error.message==='DRAFT_CAPTION_CHANGED'&&error.captionInputStage===stage);
  assert.equal(actions.some(action=>action[0]==='press'&&['Backspace','Control+V'].includes(action[1])),false);
  assert.equal(await body.innerText(),before,name+' does not delete');
  selectionFault=null;guardNegativeCases.push(name);
 }
 assert.equal(external,0);
 console.log(JSON.stringify({passed,external_calls:external,model_calls:0,publish_requests:0,native_method:'scoped_clipboard_native_paste',
  clipboard_origin:origin,scoped_permission:true,guard_negative_cases:guardNegativeCases,
  dom_html_written:false,real_site_loaded:false}));
}finally{await browser.close();}
