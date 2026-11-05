// Reuse the real V5 owned browser fixture with explicit scenario mutations.
// These are synthetic website responses, never live Bilibili requests.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
const scenario=process.argv[5];
assert.ok(['redirect','delayed_complete','wrong_source','never_complete','challenge','manager_entry','already_draft_delayed','already_draft_never','foreign_entry'].includes(scenario));
let source=await fs.readFile(new URL('./replay_part_native_v5_fixture.mjs',import.meta.url),'utf8');
function replaceOnce(before,after){assert.equal(source.split(before).length,2,'fixture seam changed: '+before.slice(0,70));source=source.replace(before,after);}
for(const relative of ['../compositor/node_modules/playwright/index.mjs','../adapters/replay_native_driver.mjs','../scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs'])
 replaceOnce("from '"+relative+"'","from '"+new URL(relative,import.meta.url).href+"'");
replaceOnce('let selections=0,images=0,saves=0,submits=0,outside=0;',
 'let selections=0,images=0,saves=0,submits=0,outside=0;let savedForm="",redirectArmed=false,redirectDone=false;const redirectCase='+JSON.stringify(scenario)+';');
replaceOnce("context.exposeBinding('ownedSave',()=>saves++)","context.exposeBinding('ownedSave',()=>{saves++;redirectArmed=true;})");
replaceOnce("if(u.href==='https://t.bilibili.com/')html=", "if(u.href==='https://t.bilibili.com/'){if(redirectArmed&&!redirectDone){redirectDone=true;await page.goto(manager);}html=");
replaceOnce("spec.target_site_uid+'/\">Owned identity</a>';", "spec.target_site_uid+'/\">Owned identity</a>';}" );
replaceOnce('else if(u.href===editor){','else if(u.origin+u.pathname===editor){');
replaceOnce("  html='<input id=\"video\"", "  savedForm=form.replace('>添加封面</div>',' style=\"background-image:linear-gradient(white,white);width:300px;height:120px\"></div>');\n  html='<input id=\"video\"");
replaceOnce(" }else if(u.href===manager){", `
  if(saves&&submits===0){
   html=savedForm;
   if(redirectCase==='wrong_source')html=html.replace('value="'+spec.source_url+'"','value="https://www.youtube.com/watch?v=other123456"');
   if(['delayed_complete','never_complete','already_draft_delayed','already_draft_never'].includes(redirectCase))html=html.replace('<div>上传完成</div>','<div id="pending-upload">上传中</div>');
   if(['delayed_complete','already_draft_delayed'].includes(redirectCase))html+='<script>setTimeout(()=>document.querySelector("#pending-upload").textContent="上传完成",700)</script>';
   if(redirectCase==='challenge')html+='<div class="geetest_panel" style="width:200px;height:100px">请完成验证</div>';
  }
 }else if(u.href===manager){`);
replaceOnce(" }else if(u.origin==='https://www.bilibili.com'", `
  if(saves&&submits===0)html='<input name="buploader" type="file" accept=".mp4"><h1>稿件管理</h1><div>草稿 1</div><a href="'+editor+'?type=draft&draftId=777">'+esc(spec.metadata.title)+'</a>';
 }else if(u.origin==='https://www.bilibili.com'`);
replaceOnce("   await assert.rejects(()=>driver.perform('submit',{reservation:{...ticket,parent_job_id:", `
   if(redirectCase==='manager_entry')await page.goto(manager);
   if(redirectCase.startsWith('already_draft')){redirectDone=true;await page.goto(editor+'?type=draft&draftId=777');}
   if(redirectCase==='foreign_entry')await page.goto('about:blank');
   await assert.rejects(()=>driver.perform('submit',{reservation:{...ticket,parent_job_id:`);
replaceOnce("   }else{\n   const submitted=await driver.perform('submit',{reservation:ticket});", `
   }else if(['wrong_source','never_complete','challenge','already_draft_never','foreign_entry'].includes(redirectCase)){
    const expected={wrong_source:'REPLAY_BROWSER_DRAFT_IDENTITY_CHANGED',never_complete:'REPLAY_BROWSER_EDITOR_NOT_UPLOADED',already_draft_never:'REPLAY_BROWSER_EDITOR_NOT_UPLOADED',foreign_entry:'REPLAY_BROWSER_EDITOR_NOT_UPLOADED',challenge:'REPLAY_BROWSER_DRAFT_LOOKUP_REQUIRED'};
    await assert.rejects(()=>driver.perform('submit',{reservation:ticket}),error=>error.code===expected[redirectCase]);
    assert.equal(submits,0);await assert.rejects(()=>fs.stat(path.join(root,'media',spec.job_id,'replay-native-intents/public-submit.json')),error=>error.code==='ENOENT');
   }else{
   const submitted=await driver.perform('submit',{reservation:ticket});`);
replaceOnce(' assert.equal(outside,0);console.log(JSON.stringify({scenario,owned:true,selections,submits,images,saves,platform_actions:0,external_requests:outside}));', `
 const traceDirectory=path.join(driver.output,'editor-redirect-recovery');
 if(redirectCase==='foreign_entry'){await assert.rejects(()=>fs.stat(traceDirectory),error=>error.code==='ENOENT');}
 else if(redirectCase==='already_draft_delayed'){
  try{await fs.stat(traceDirectory);assert.fail('delayed completion should not require recovery trace');}
  catch(error){assert.equal(error.code,'ENOENT');}
  assert.equal(submits,1);
 }
 else{const entries=await fs.readdir(traceDirectory);
 assert.equal(entries.length,1);const trace=JSON.parse(await fs.readFile(path.join(traceDirectory,entries[0],'observation.json'),'utf8'));
 assert.equal(trace.ready,['redirect','delayed_complete','manager_entry','already_draft_delayed'].includes(redirectCase));assert.equal(trace.source_verified,trace.ready);
 assert.equal(trace.entry_url,redirectCase==='manager_entry'?manager:editor);assert.equal(trace.redirect_url,redirectCase.startsWith('already_draft')?editor:manager);assert.equal(trace.maximum_draft_navigations,1);
 assert.equal(trace.draft_navigations,redirectCase.startsWith('already_draft')?0:1);
 assert.equal(trace.save_clicks,0);assert.equal(trace.new_file_selections,0);assert.equal(trace.submit_clicks,0);
 if(trace.ready){assert.ok(trace.polls.length>=2);assert.equal(submits,1);}else assert.equal(submits,0);}
 assert.equal(selections,1);assert.equal(saves,1);assert.equal(images,1);
 assert.equal(outside,0);console.log(JSON.stringify({scenario,redirectCase,owned:true,selections,submits,images,saves,platform_actions:0,external_requests:outside}));`);
await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
