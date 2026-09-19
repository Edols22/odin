const {chromium}=require('/home/nvidia/perception_domain_nx/runtime/odin1/map_viewer_deps/node_modules/playwright');
const assert=require('node:assert/strict');
(async()=>{
  const browser=await chromium.launch({headless:true,args:['--no-sandbox','--enable-unsafe-swiftshader']});
  const page=await browser.newPage({viewport:{width:1600,height:1200}});const errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  for(let retry=0;retry<40;retry++){
    try{if((await page.request.get('http://127.0.0.1:8770/api/state')).ok())break;}catch(e){}
    if(retry===39)throw Error('project server did not become ready');
    await new Promise(resolve=>setTimeout(resolve,500));
  }
  await page.goto('http://127.0.0.1:8770');
  await page.waitForFunction(()=>{const rows=[...document.querySelectorAll('#inventory tr')];return rows.length===4&&rows.every(r=>r.children[1]?.textContent==='已确认')},null,{timeout:20000});
  await page.waitForFunction(()=>document.querySelector('#image').naturalWidth>0);
  assert.equal(await page.locator('#scene canvas').count(),1);
  assert.equal(await page.locator('#inventory tr').count(),4);
  assert.ok(await page.evaluate(async()=>(await(await fetch('/api/state')).json()).map.current_measurements.every(m=>m.box_id!==null)));
  await page.selectOption('#mode','manual');
  await page.waitForFunction(async()=>{const s=await(await fetch('/api/state')).json();return s.mode==='manual'});
  const before=await page.evaluate(async()=>(await(await fetch('/api/state')).json()).map.revision);
  await page.locator('h1').click();await page.keyboard.press('Space');
  await page.waitForFunction(async before=>(await(await fetch('/api/state')).json()).map.revision>before,before);
  await page.locator('#rawView').check();await page.locator('#showRaw').uncheck();await page.locator('#showRaw').check();
  await page.locator('#targets').fill('0');await page.locator('[data-session="LOCAL_CHECK"]').click();
  await page.waitForFunction(()=>document.querySelector('#session').innerText.includes('WAIT_OPERATOR'));
  await page.locator('#ack').click();
  await page.waitForFunction(async()=>(await(await fetch('/api/state')).json()).scheduler.session?.state==='COMPLETE',null,{timeout:20000});
  await page.screenshot({path:'/home/nvidia/perception_domain_nx/runtime/results/expected_cloud_demo_validation/browser.png',fullPage:true});
  assert.deepEqual(errors,[]);console.log(JSON.stringify({browser:'PASS',inventory:4,checks:['WebGL','RGB','persistent IDs','space step','manual local check','point toggles'],errors}));
  await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
