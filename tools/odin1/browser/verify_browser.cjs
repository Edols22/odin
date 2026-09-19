const assert=require('node:assert/strict');
const fs=require('node:fs');
const {chromium}=require('/home/nvidia/perception_domain_nx/runtime/odin1/map_viewer_deps/node_modules/playwright');
const base='http://127.0.0.1:8765';
const out='/home/nvidia/perception_domain_nx/runtime/results/odin_browser';
async function state(){return (await fetch(base+'/api/state')).json()}
async function command(payload){const response=await fetch(base+'/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});assert(response.ok,await response.clone().text());return response.json()}
async function until(fn,timeout=20000){const end=Date.now()+timeout;while(Date.now()<end){if(await fn())return;await new Promise(r=>setTimeout(r,300))}throw Error('Timed out')}
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
 try{
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));await page.goto(base);await page.waitForFunction(()=>window.odinView?.scene.children.some(x=>x.isPoints));
 await command({action:'configure',mode:'manual'});await command({action:'start'});
 await until(async()=>{const s=await state();return s.camera_live&&s.heartbeat.mode==='manual'&&s.heartbeat.enabled&&!s.heartbeat.request_pending});
 const before=(await state()).latest.frame||0;
 await page.locator('h1').click();await page.keyboard.press('Space');
 await until(async()=>((await state()).latest.frame||0)>before);
 const first=await state();assert(first.latest.commit.commit,JSON.stringify(first.latest.time_sync));
 await new Promise(r=>setTimeout(r,2000));const idle=await state();
 assert.equal(idle.latest.frame,first.latest.frame,'manual mode committed without a trigger');
 assert(idle.heartbeat.sensor_stamp>first.heartbeat.sensor_stamp,'manual RGB preview froze');
 await command({action:'once'});await command({action:'once'});
 await until(async()=>((await state()).latest.frame||0)>=idle.latest.frame+2);
 await new Promise(r=>setTimeout(r,1000));assert.equal((await state()).latest.frame,idle.latest.frame+2,'two triggers must produce exactly two calculations');
 await command({action:'configure',mode:'realtime'});
 await until(async()=>((await state()).latest.frame||0)>=first.latest.frame+8,30000);
 await command({action:'pause'});await new Promise(r=>setTimeout(r,700));
 await page.waitForTimeout(1000);
 const check=await page.evaluate(()=>({boxes:window.odinView.boxes.map(b=>({id:b.global_id,state:b.state,center:b.center_map})),mapPoints:window.odinView.scene.children.filter(x=>x.isPoints).map(p=>p.geometry.attributes.position.count),overflow:document.documentElement.scrollWidth>innerWidth,images:[...document.querySelectorAll('figure img')].map(x=>({complete:x.complete,width:x.naturalWidth,height:x.naturalHeight}))}));
 assert(!check.overflow);assert(check.mapPoints.includes(338073));assert(check.mapPoints.some(n=>n>1000&&n<=10000));assert(check.images.every(x=>x.width>0));
 const poseBefore=await page.evaluate(()=>window.odinView.camera.position.toArray());
 const rect=await page.locator('#scene').boundingBox();await page.mouse.move(rect.x+rect.width*.5,rect.y+rect.height*.5);await page.mouse.down();await page.mouse.move(rect.x+rect.width*.6,rect.y+rect.height*.6,{steps:10});await page.mouse.up();
 const poseAfter=await page.evaluate(()=>window.odinView.camera.position.toArray());assert.notDeepEqual(poseBefore,poseAfter);
 await page.screenshot({path:out+'/browser-desktop.png',fullPage:true});
 await page.locator('#fit').click();await page.screenshot({path:out+'/browser-full-map.png',fullPage:true});
 let fixtureOverlay=false;
 if(!check.boxes.length){
  fixtureOverlay=true;
  // A browser-only intercepted response exercises rendering without changing
  // the running map. Screenshots explicitly identify these as synthetic boxes.
  const fixtures=[0,1].map(i=>{const x=i*.5,y=1.5,z=.5,w=.4,h=.4,d=.3;return {global_id:900000+i,state:'CONFIRMED',center_map:[x,y,z],size_world:[w,h,d],normal_world:[0,-1,0],horizontal_world:[1,0,0],existence_probability:.9,observations:9,exist_positive:9,exist_negative:0,consecutive_clear_frames:0,visibility_state:'VISIBLE',last_evidence:'MATCHED',last_seen:0,covariance:Array.from({length:6},()=>Array(6).fill(0)),cuboid_corners_world:[[-1,-1],[1,-1],[1,1],[-1,1]].map(([a,b])=>[x+a*w/2,y,z+b*h/2]).concat([[-1,-1],[1,-1],[1,1],[-1,1]].map(([a,b])=>[x+a*w/2,y+d,z+b*h/2]))}});
  await page.route('**/api/state',async route=>{const data=await state();data.latest.map=fixtures;data.latest.map_history=fixtures;await route.fulfill({json:data})});
  await until(async()=>await page.evaluate(()=>window.odinView.boxes.length===2));
  await page.evaluate(()=>{const banner=document.createElement('div');banner.textContent='SYNTHETIC OVERLAY TEST - NOT LIVE DETECTIONS';banner.style='position:fixed;top:0;left:0;right:0;background:#ffd44b;color:#000;z-index:9999;text-align:center;padding:8px';document.body.appendChild(banner)});
 }
 await page.locator('#boxes tr').first().click();await page.screenshot({path:out+'/browser-box-focus.png',fullPage:true});
 const pixels=await page.evaluate(()=>{const {renderer}=window.odinView;const gl=renderer.getContext(),bytes=new Uint8Array(gl.drawingBufferWidth*gl.drawingBufferHeight*4);gl.readPixels(0,0,gl.drawingBufferWidth,gl.drawingBufferHeight,gl.RGBA,gl.UNSIGNED_BYTE,bytes);let bright=0;for(let i=0;i<bytes.length;i+=4)if(Math.max(bytes[i],bytes[i+1],bytes[i+2])>100)bright++;return bright});assert(pixels>500);
 await page.setViewportSize({width:390,height:844});await page.waitForTimeout(800);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);await page.screenshot({path:out+'/browser-mobile.png',fullPage:true});
 const mobilePixels=await page.evaluate(()=>{const gl=window.odinView.renderer.getContext(),bytes=new Uint8Array(gl.drawingBufferWidth*gl.drawingBufferHeight*4);gl.readPixels(0,0,gl.drawingBufferWidth,gl.drawingBufferHeight,gl.RGBA,gl.UNSIGNED_BYTE,bytes);let n=0;for(let i=0;i<bytes.length;i+=4)if(Math.max(bytes[i],bytes[i+1],bytes[i+2])>100)n++;return n});assert(mobilePixels>500);
 assert.deepEqual(errors,[]);const report={...check,fixtureOverlay,pixels,mobilePixels,manualFrames:[first.latest.frame,idle.latest.frame],liveStamps:[first.heartbeat.sensor_stamp,idle.heartbeat.sensor_stamp],errors};fs.writeFileSync(out+'/browser-verification.json',JSON.stringify(report,null,2));console.log(report);
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
