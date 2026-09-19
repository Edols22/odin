// Browser-only synthetic contract test. Never contacts a camera or live mapper.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {chromium}=require('/home/nvidia/perception_domain_nx/runtime/odin1/map_viewer_deps/node_modules/playwright');
const root=path.resolve(__dirname,'../../..');
const assets=__dirname;
const out=fs.mkdtempSync(path.join(os.tmpdir(),'odin-policy-browser-'));
const commands=[];
const box=(id,state,visibility)=>({global_id:id,state,visibility_state:visibility,center_map:[id*.5,1.,.6],
  size_world:[.4,.4,.3],normal_world:[0,-1,0],horizontal_world:[1,0,0],existence_probability:.8,
  observations:4,exist_positive:4,exist_negative:0,consecutive_clear_frames:0,last_evidence:'UNKNOWN',last_seen:1,
  cuboid_corners_world:[[-1,-1],[1,-1],[1,1],[-1,1]].map(([a,b])=>[id*.5+a*.2,1.,.6+b*.2]).concat(
    [[-1,-1],[1,-1],[1,1],[-1,1]].map(([a,b])=>[id*.5+a*.2,1.3,.6+b*.2]))});
const objects=[box(0,'CONFIRMED','OUT_OF_FOV'),box(1,'MISSING_CANDIDATE','CLEAR'),box(2,'TENTATIVE','VISIBLE'),box(3,'VACATED','CLEAR')];
const fixture={running:true,camera_live:false,control:{mode:'manual',confidence:.7,inference_size:640,config:{epoch_frames:3,replacement_support_frames:3}},
  heartbeat:{session_id:'offline-fixture',mode:'manual',enabled:true,epoch:{state:'EPOCH_COLLECTING',collected:2,required:3}},
  latest:{session_id:'offline-fixture',frame:12,sample_sequence:13,stamp:1,map:objects.slice(0,3),map_history:objects,
    commit:{commit:false,reason:'EPOCH_COLLECTING'},events:[],observations:[{local_detection_id:7,
      association_status:'IDENTITY_PENDING',position_quality_ok:true,orientation_quality_ok:true,
      center_camera_optical:[0,0,2],center_map:[.1,1,.6],candidate_global_ids:[0]}],time_sync:{pose_valid:true},
    current_measurements:{world_frame:'warehouse_map',base_frame:'base',world_pose_valid:true,base_pose_valid:false,
      synchronized:true,boxes:[{local_detection_id:7,global_id:null,position_quality_ok:true,orientation_quality_ok:true,
        size:[.4,.4,.3],world:{position:[.1,1,.6],normal:[0,-1,0],horizontal:[1,0,0]}}]},
    active_box_map:{revision:12,stamp:1,localization:{valid:true},boxes:[{global_id:0},{global_id:1,needs_verification:true}]},
    map_delta:{added:[1],removed:[3],updated:[],kept:[0]}}};
(async()=>{
  const browser=await chromium.launch({headless:true,args:['--no-sandbox','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.route('**/*',async route=>{
      const url=new URL(route.request().url());
      if(url.pathname==='/api/state')return route.fulfill({json:fixture});
      if(url.pathname==='/api/control'){
        const command=route.request().postDataJSON();commands.push(command);
        if(command.mode){fixture.control.mode=command.mode;fixture.heartbeat.mode=command.mode}
        return route.fulfill({json:fixture});
      }
      if(url.pathname==='/map/metadata.json')return route.fulfill({json:{bounds:[[-1,0,0],[3,3,2]],points:4,keyframes:1,source:'OFFLINE SYNTHETIC TEST'}});
      if(url.pathname.endsWith('.f32'))return route.fulfill({body:Buffer.from(new Float32Array([0,0,0,2,0,1,0,2,1,2,2,2]).buffer)});
      if(url.pathname==='/data/debug.jpg')return route.fulfill({body:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9mQAAAAASUVORK5CYII=','base64'),contentType:'image/png'});
      const names={'/':'index.html','/app.js':'app.bundle.js','/app.css':'app.css'};
      const file=names[url.pathname];
      if(file)return route.fulfill({body:fs.readFileSync(path.join(assets,file)),contentType:file.endsWith('.css')?'text/css':file.endsWith('.js')?'application/javascript':'text/html'});
      return route.fulfill({status:404,body:'offline route not supplied'});
    });
    await page.goto('http://odin-map-test.invalid/');
    await page.waitForFunction(()=>window.odinView?.boxes.length===3);
    assert((await page.locator('#rejected').textContent()).includes('编号待关联'));
    assert((await page.locator('#rejected').textContent()).includes('warehouse_map'));
    assert((await page.locator('#rejected').textContent()).includes('未提供 TF'));
    assert(!(await page.locator('#rejected').textContent()).includes('几何锚点'));
    await page.locator('#mode').selectOption('epoch');
    await page.waitForFunction(()=>document.querySelector('#epoch-status').textContent.includes('2/3'));
    await page.locator('h1').click();await page.keyboard.press('Space');
    await page.waitForFunction(()=>document.querySelector('#mode').value==='epoch');
    await page.locator('[data-tab="policy"]').click();
    await page.waitForFunction(()=>document.querySelector('#policy').textContent.includes('完整已知集合 2 个'));
    await page.locator('#policy-only').check();
    await page.waitForFunction(()=>window.odinView.boxes.length===2);
    assert.deepEqual(await page.locator('#boxes tr td:first-child').allTextContents(),['#0','#1']);
    assert((await page.locator('#boxes').innerText()).includes('视野外'));
    assert((await page.locator('#policy').innerText()).includes('移除：#3'));
    await page.locator('#history').check();
    assert.equal(await page.locator('#boxes tr').count(),2,'history must not add removed objects to policy collection');
    await page.locator('#policy-only').uncheck();
    await page.waitForFunction(()=>window.odinView.boxes.length===4);
    await page.evaluate(()=>{const banner=document.createElement('div');banner.textContent='离线合成 UI 测试 — 非相机直播';banner.style='position:fixed;bottom:0;left:0;right:0;background:#ffd44b;color:black;z-index:9999;padding:8px;text-align:center';document.body.append(banner)});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.screenshot({path:path.join(out,'desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.waitForTimeout(300);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.screenshot({path:path.join(out,'mobile.png'),fullPage:true});
    assert(commands.some(c=>c.action==='configure'&&c.mode==='epoch'));
    assert(commands.some(c=>c.action==='once'));
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({passed:true,synthetic:true,cameraStarted:false,commands,artifacts:out}));
  }finally{await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
