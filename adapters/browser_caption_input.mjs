// Native editor commands and plaintext clipboard only. Never write DOM/HTML.
const origin='https://t.bilibili.com';
const clean=value=>value.replace(/^\u200B/,'').trimEnd();
export async function insertCaptionText(page,body,caption,{clipboardOrigin}={}){
 if(typeof caption!=='string'||caption.length>10000||clipboardOrigin!==origin||
  new URL(page.url()).origin!==origin)throw new Error('SPEC_INVALID');
 await page.context().grantPermissions(['clipboard-read','clipboard-write'],{origin});
 const fail=captionInputStage=>{
  const error=new Error('DRAFT_CAPTION_CHANGED');error.captionInputStage=captionInputStage;throw error;
 };
 const requireSelection=async stage=>{
  const state=await body.evaluate(element=>{
   const selection=element.ownerDocument.getSelection();
   return {active:element.ownerDocument.activeElement===element,has_selection:!!selection&&selection.rangeCount>0,
    anchor_inside:!!selection?.anchorNode&&element.contains(selection.anchorNode),
    focus_inside:!!selection?.focusNode&&element.contains(selection.focusNode)};
  });
  if(!state.active||!state.has_selection||!state.anchor_inside||!state.focus_inside)fail(stage);
 };
 const focus=async stage=>{
  // A normal hit-tested click establishes a caret; focus() alone may retain
  // a selection from the title input. Never create or modify a DOM Range.
  await body.click();await requireSelection(stage);
 };
 await focus('focus');
 await page.keyboard.press('Control+A');
 await requireSelection('select');
 await page.keyboard.press('Backspace');
 await page.waitForTimeout(300);
 if(clean(await body.innerText())!=='')fail('clear');
 if(new URL(page.url()).origin!==origin)throw new Error('SPEC_INVALID');
 await page.evaluate(text=>navigator.clipboard.writeText(text),caption);
 await focus('paste_focus');
 await page.keyboard.press('Control+V');
 await page.waitForTimeout(1000);
 if(clean(await body.innerText())!==caption)fail('paste');
}
