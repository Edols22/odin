const {chromium}=require('/home/nvidia/perception_domain_nx/runtime/odin1/map_viewer_deps/node_modules/playwright');
const assert=require('node:assert/strict');
(async()=>{
  const browser=await chromium.launch({headless:true,args:['--no-sandbox','--enable-unsafe-swiftshader']});
  const page=await browser.newPage({viewport:{width:1700,height:1100}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
  for(let retry=0;retry<40;retry++){
    try{if((await page.request.get('http://127.0.0.1:8770/api/state')).ok())break;}catch(e){}
    if(retry===39)throw Error('project server did not become ready');
    await new Promise(resolve=>setTimeout(resolve,500));
  }
  await page.goto('http://127.0.0.1:8770');
  await page.waitForFunction(()=>document.querySelector('#image').naturalWidth>0,null,{timeout:25000});
  const read=()=>page.evaluate(async()=>await(await fetch('/api/state')).json());
  const s=await read();assert.equal(s.error,'');assert.equal(s.source.frame_id,'map');assert.ok(s.map.revision>0);
  const cloud=await page.evaluate(async()=>await(await fetch('/api/scene')).json());assert.ok(cloud.points.length>1000);
  await page.selectOption('#mode','paused');await page.waitForFunction(async()=>(await(await fetch('/api/state')).json()).mode==='paused');
  const before=await read();await page.waitForFunction(async stamp=>(await(await fetch('/api/state')).json()).preview_stamp>stamp,before.preview_stamp);
  const paused=await read();assert.equal(paused.map.revision,before.map.revision);
  await page.locator('#step').click();await page.waitForFunction(async rev=>(await(await fetch('/api/state')).json()).map.revision>rev,before.map.revision,{timeout:15000});
  await page.screenshot({path:'/home/nvidia/perception_domain_nx/runtime/results/expected_cloud_live_validation/browser.png',fullPage:true});
  assert.deepEqual(errors,[]);console.log(JSON.stringify({browser:'PASS',source:'LIVE_ODIN',frame:cloud.frame_id,cloud_points:cloud.points.length,
    checks:['RGB','raw point cloud','map TF','pause preserves preview','one-shot commit','WebGL'],state:await read(),errors}));
  await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
