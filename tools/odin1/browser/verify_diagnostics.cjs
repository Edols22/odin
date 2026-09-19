// Read-only live acceptance: no injected boxes, API writes or camera restarts.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require('/home/nvidia/perception_domain_nx/runtime/odin1/map_viewer_deps/node_modules/playwright');
const out = '/home/nvidia/perception_domain_nx/runtime/results/odin_browser';

(async () => {
  const browser = await chromium.launch({headless:true,args:['--no-sandbox','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}}), errors=[], samples=[];
    page.on('pageerror', e=>errors.push(e.message));
    await page.goto('http://127.0.0.1:8765');
    await page.waitForFunction(()=>window.odinView?.boxes.some(b=>b.state==='CONFIRMED'),{},{timeout:60000});
    await page.waitForFunction(()=>document.getElementById('debug-image').naturalWidth===1600);
    assert(await page.locator('#rejected').isVisible());
    assert.equal(await page.locator('.diagnostic-pipeline>div').count(),4);
    assert(!(await page.locator('.diagnostic-raw').evaluate(el=>el.open)));
    for(let i=0;i<20;i++) {
      const sample=await page.evaluate(()=>{
        const {state,boxes,scene,renderer,render}=window.odinView,d=state.latest;
        const ids=boxes.map(b=>b.global_id), labels=[];scene.traverse(o=>{if(o.isSprite&&o.userData.id!==undefined)labels.push(o.userData.id)});
        render();const gl=renderer.getContext(),pixels=new Uint8Array(gl.drawingBufferWidth*gl.drawingBufferHeight*4);
        gl.readPixels(0,0,gl.drawingBufferWidth,gl.drawingBufferHeight,gl.RGBA,gl.UNSIGNED_BYTE,pixels);
        let colored=0;for(let p=0;p<pixels.length;p+=4)if(Math.max(pixels[p],pixels[p+1],pixels[p+2])>100)colored++;
        return {session:d.session_id,frame:d.frame,ids,confirmed:boxes.filter(b=>b.state==='CONFIRMED').map(b=>b.global_id),labels,
          rows:[...document.querySelectorAll('#boxes tr td:first-child')].map(e=>e.textContent),
          assigned:d.observations.filter(o=>o.global_id!==null&&o.global_id!==undefined).map(o=>o.global_id),
          summaries:[...document.querySelectorAll('.diagnostic-object summary>strong')].map(e=>e.textContent),
          coverage:d.observations.filter(o=>o.geometry_quality_ok).map(o=>o.depth_support.spatial_coverage),
          cameraWorld:d.camera_world,colored,error:state.heartbeat.error,commit:d.commit};
      });
      assert(sample.confirmed.length>0,'No real confirmed boxes');
      assert(sample.colored>500,'Blank map');assert(!sample.error);
      assert.deepEqual(sample.labels.sort(),sample.ids.slice().sort());
      assert.deepEqual(sample.rows,sample.ids.map(id=>'#'+id));
      for(const id of sample.assigned){assert(sample.ids.includes(id));assert(sample.summaries.some(s=>s.endsWith('→ #'+id)))}
      samples.push(sample);await page.waitForTimeout(1500);
    }
    assert(new Set(samples.map(s=>s.frame)).size>=5,'Inference is not progressing');
    assert.equal(new Set(samples.map(s=>s.session)).size,1,'Unexpected session reset');
    assert(samples.filter(s=>s.assigned.length>0).length>=5,'Insufficient real assigned detection frames');
    // This read-only UI check does not constrain physical camera/box motion.
    // Cross-frame identity stability is reported, not claimed as field acceptance.
    const unchangedIds=JSON.stringify(samples[0].ids)===JSON.stringify(samples.at(-1).ids);
    await page.locator('#boxes tr').first().click();
    assert((await page.locator('#selected-summary').innerText()).startsWith('#'));
    await page.locator('.diagnostic-object summary').first().click();
    await page.waitForTimeout(1100);
    assert(await page.locator('.diagnostic-object').first().evaluate(el=>el.open),'Expanded diagnostics collapsed on refresh');
    assert((await page.locator('.diagnostic-checks').first().innerText()).includes('有效深度点'));
    await page.screenshot({path:`${out}/diagnostics-desktop.png`,fullPage:true});
    await page.setViewportSize({width:390,height:844});await page.waitForTimeout(800);
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Mobile page overflow');
    const brief=await page.locator('.diagnostic-brief').first().boundingBox();assert(brief.width<=390);
    await page.screenshot({path:`${out}/diagnostics-mobile.png`,fullPage:true});
    assert.deepEqual(errors,[]);
    fs.writeFileSync(`${out}/diagnostics-verification.json`,JSON.stringify({fixtureOverlay:false,unchangedIds,samples,errors},null,2));
    console.log(JSON.stringify({realIds:samples.at(-1).ids,frames:[samples[0].frame,samples.at(-1).frame],samples:samples.length,unchangedIds,fixtureOverlay:false,errors}));
  } finally {await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
