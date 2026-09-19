// Live acceptance: no synthetic sensor frames, USB control or detector startup.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {chromium}=require('/home/nvidia/perception_domain_nx/runtime/odin1/map_viewer_deps/node_modules/playwright');
const out='/home/nvidia/perception_domain_nx/runtime/results/odin_depth_viewer';
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 try{
  const page=await browser.newPage({viewport:{width:1600,height:1100},acceptDownloads:true}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://127.0.0.1:8766');
  await page.waitForFunction(()=>window.depthView?.frame?.info.rgb_valid,{},{timeout:60000});
  const first=await page.evaluate(()=>depthView.lastStamp);
  await page.waitForFunction(t=>depthView.lastStamp>t,first,{timeout:10000});
  await page.locator('#pause').click();
  const frozen=await page.evaluate(()=>({stamp:depthView.lastStamp,lengths:depthView.probes.map(p=>p.history.length)}));
  await page.waitForTimeout(1400);
  assert.deepEqual(await page.evaluate(()=>({stamp:depthView.lastStamp,lengths:depthView.probes.map(p=>p.history.length)})),frozen);
  const occupied=await page.evaluate(()=>{
   const w=800,h=648,b=depthView.depthBuffer;
   for(let y=120;y<h-80;y+=20)for(let x=120;x<w-80;x+=20){let n=0;for(let yy=y-12;yy<y+12;yy++)for(let xx=x-12;xx<x+12;xx++)if(Number.isFinite(b[yy*w+xx]))n++;
    if(n>=15)return {x,y};}
   throw Error('No populated registered depth patch');
  });
  const canvas=page.locator('#depth');await canvas.scrollIntoViewIfNeeded();
  let box=await canvas.boundingBox();await page.mouse.click(box.x+occupied.x/800*box.width,box.y+occupied.y/648*box.height);
  const aligned=await page.evaluate(()=>{
   const p=depthView.probes[0],v=[],r=12;
   for(let y=Math.max(0,Math.floor(p.y*648-r));y<Math.min(648,p.y*648+r);y++)for(let x=Math.max(0,Math.floor(p.x*800-r));x<Math.min(800,p.x*800+r);x++){const n=depthView.depthBuffer[y*800+x];if(Number.isFinite(n))v.push(n)}
   v.sort((a,b)=>a-b);return {actual:p.current.value,expected:v[Math.floor((v.length-1)*.5)],points:v.length};
  });
  assert.equal(aligned.actual,aligned.expected);assert(aligned.points>=3);
  await page.selectOption('#probe','1');await page.locator('#raw').scrollIntoViewIfNeeded();box=await page.locator('#raw').boundingBox();
  await page.mouse.click(box.x+box.width/2,box.y+box.height/2);
  const raw=await page.evaluate(()=>{
   const p=depthView.probes[1],{info,points}=depthView.frame,v=[],r=Math.max(2,12*info.width/800);
   for(let i=0;i<info.points;i++)if(Math.abs(i%info.width-p.x*info.width)<=r&&Math.abs(Math.floor(i/info.width)-p.y*info.height)<=r){const d=Math.hypot(points[4*i],points[4*i+1],points[4*i+2]);if(Number.isFinite(d)&&d>.01&&points[4*i+3]>=35)v.push(d)}
   v.sort((a,b)=>a-b);return {mode:p.mode,actual:p.current?.value,expected:v[Math.floor((v.length-1)*.5)],width:info.width,height:info.height};
  });
  assert.equal(raw.mode,'raw');assert.equal(raw.width,256);assert.equal(raw.height,192);assert.equal(raw.actual,raw.expected);assert(raw.actual>0);
  await page.locator('#min').fill('0.5');await page.locator('#max').fill('4.0');await page.locator('#max').dispatchEvent('change');
  assert.equal(await page.locator('#min-label').textContent(),'0.5 m');assert.equal(await page.locator('#max-label').textContent(),'4.0 m');
  await page.locator('#pause').click();await page.waitForFunction(t=>depthView.lastStamp>t,frozen.stamp,{timeout:10000});
  await page.waitForTimeout(1600);
  const pending=page.waitForEvent('download');await page.locator('#export').click();const download=await pending;await download.saveAs(out+'/verification.csv');
  const csv=fs.readFileSync(out+'/verification.csv','utf8');assert(csv.includes('sensor_stamp_s'));assert(csv.includes(',aligned,'));assert(csv.includes(',raw,'));
  const pixels=await page.evaluate(()=>['rgb','depth','raw'].map(id=>{const c=document.getElementById(id),a=c.getContext('2d').getImageData(0,0,c.width,c.height).data;let n=0;for(let i=0;i<a.length;i+=4)if(Math.max(a[i],a[i+1],a[i+2])>80)n++;return [id,n]}));
  for(const [id,n] of pixels)assert(n>500,id+' is blank');
  await page.evaluate(()=>scrollTo(0,0));await page.screenshot({path:out+'/depth-desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});await page.waitForTimeout(500);
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Mobile overflow');
  await page.screenshot({path:out+'/depth-mobile.png',fullPage:true});
  // Browser-side disconnect only: the real camera continues untouched.
  await page.route('**/api/frame',route=>route.abort());
  await page.waitForFunction(()=>document.getElementById('status').textContent.includes('连接中断'));
  const stopped=await page.evaluate(()=>depthView.probes.map(p=>p.history.length));
  await page.waitForTimeout(1100);assert.deepEqual(await page.evaluate(()=>depthView.probes.map(p=>p.history.length)),stopped);
  await page.unroute('**/api/frame');await page.waitForFunction(()=>document.getElementById('status').textContent.includes('实时深度'));
  assert.deepEqual(errors,[]);
  const report={aligned,raw,pixels,pausePassed:true,exportPassed:true,disconnectPassed:true,errors};fs.writeFileSync(out+'/verification.json',JSON.stringify(report,null,2));console.log(JSON.stringify(report));
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
