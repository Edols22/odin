// Read-only live check; never sends controls, resets IDs, or starts a camera.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {chromium}=require('/home/nvidia/perception_domain_nx/runtime/odin1/map_viewer_deps/node_modules/playwright');
(async()=>{
  const out=fs.mkdtempSync(path.join(os.tmpdir(),'odin-current-live-'));
  const browser=await chromium.launch({headless:true,args:['--no-sandbox','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
  try{
    const page=await browser.newPage({viewport:{width:1600,height:1100}}),errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto('http://127.0.0.1:8765/');
    await page.waitForFunction(()=>window.odinView?.state.latest?.current_measurements?.boxes.length>0);
    const first=await page.evaluate(()=>window.odinView.state.latest.sensor_stamp);
    await page.waitForFunction(stamp=>window.odinView.state.latest.sensor_stamp>stamp&&window.odinView.state.latest.commit.commit,first,{timeout:20000});
    await page.waitForFunction(()=>['live-image','debug-image'].every(id=>document.getElementById(id).naturalWidth===1600));
    const state=await page.evaluate(()=>window.odinView.state),d=state.latest;
    assert(state.camera_live);assert(!state.heartbeat.error);assert(d.current_measurements.world_pose_valid);
    for(const b of d.current_measurements.boxes){
      const observation=d.observations.find(o=>o.local_detection_id===b.local_detection_id);
      assert.equal(b.position_quality_ok,observation.position_quality_ok);
      assert.equal(b.orientation_quality_ok,observation.orientation_quality_ok);
      const p=b.camera_optical.position,m=d.world_from_camera;
      const expected=m.slice(0,3).map(row=>row[0]*p[0]+row[1]*p[1]+row[2]*p[2]+row[3]);
      expected.forEach((value,i)=>assert(Math.abs(value-b.world.position[i])<1e-8));
      if(!d.current_measurements.base_pose_valid)assert.equal(b.base,null);
    }
    assert(!(await page.locator('#rejected').textContent()).includes('几何锚点'));
    assert.deepEqual(errors,[]);
    await page.screenshot({path:path.join(out,'live.png'),fullPage:true});
    fs.writeFileSync(path.join(out,'state.json'),JSON.stringify(state));
    console.log(JSON.stringify({passed:true,synthetic:false,readOnly:true,out,frame:d.frame,
      driver:state.driver_pids,session:d.session_id,records:d.map.map(b=>b.global_id),
      measurements:d.current_measurements.boxes.map(b=>({d:b.local_detection_id,id:b.global_id,
        position:b.position_quality_ok,orientation:b.orientation_quality_ok,status:b.association_status})),
      baseReady:d.current_measurements.base_pose_valid}));
  }finally{await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
