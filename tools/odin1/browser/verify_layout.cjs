// Read-only layout verification: never changes camera or mapper controls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require('/home/nvidia/perception_domain_nx/runtime/odin1/map_viewer_deps/node_modules/playwright');
const out = '/home/nvidia/perception_domain_nx/runtime/results/odin_browser';

(async () => {
  const browser = await chromium.launch({headless:true,args:['--no-sandbox','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
  try {
    const page = await browser.newPage();
    const errors = [], results = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.goto('http://127.0.0.1:8765');
    await page.waitForFunction(() => window.odinView?.scene.children.some(x => x.isPoints));
    await page.waitForFunction(() => document.getElementById('live-image').naturalWidth > 0);
    for (const [width,height] of [[1440,900],[1920,1080],[1024,768],[390,844]]) {
      await page.setViewportSize({width,height});
      await page.waitForTimeout(700);
      const result = await page.evaluate(() => {
        const rect = selector => {
          const r = document.querySelector(selector).getBoundingClientRect();
          return {x:r.x,y:r.y,width:r.width,height:r.height,bottom:r.bottom,right:r.right};
        };
        const {renderer,camera,render} = window.odinView;
        render();
        const gl = renderer.getContext(), pixels = new Uint8Array(gl.drawingBufferWidth*gl.drawingBufferHeight*4);
        gl.readPixels(0,0,gl.drawingBufferWidth,gl.drawingBufferHeight,gl.RGBA,gl.UNSIGNED_BYTE,pixels);
        let colored = 0;
        for (let i=0;i<pixels.length;i+=4) if (Math.max(pixels[i],pixels[i+1],pixels[i+2])>100) colored++;
        return {width:innerWidth,height:innerHeight,overflow:document.documentElement.scrollWidth>innerWidth,
          live:rect('#live-image'),debug:rect('#debug-image'),caption:rect('figure figcaption'),
          map:rect('.map-area'),camera:rect('aside'),scene:rect('#scene'),colored,
          projectionAspect:(camera.right-camera.left)/(camera.top-camera.bottom)};
      });
      assert(!result.overflow, `Page overflow at ${width}`);
      assert(result.live.y >= result.caption.bottom - 1, 'Caption overlaps image');
      assert(Math.abs(result.live.width/result.live.height-1600/1296)<.01, 'Image aspect changed');
      assert(result.map.y >= result.camera.bottom-1, 'Map overlaps camera band');
      assert(result.colored>500, 'Map canvas is blank');
      assert(Math.abs(result.projectionAspect-result.scene.width/result.scene.height)<.01, 'Map framing was not resized');
      if (width>720) {
        assert(Math.abs(result.live.y-result.debug.y)<1, 'Images are not side by side');
        assert(result.live.right<result.debug.x, 'Images overlap');
        assert(result.live.width>width*.47, 'Images still too narrow');
      } else {
        assert(result.debug.y>=result.live.bottom, 'Mobile images overlap');
        assert(result.live.width>=width-24, 'Mobile image is not full width');
      }
      await page.screenshot({path:`${out}/layout-${width}.png`,fullPage:true});
      results.push(result);
    }
    await page.setViewportSize({width:1440,height:900});
    await page.locator('#scene').scrollIntoViewIfNeeded();
    const before = await page.evaluate(() => window.odinView.camera.position.toArray());
    const r = await page.locator('#scene').boundingBox();
    await page.mouse.move(r.x+r.width*.5,r.y+r.height*.5);await page.mouse.down();
    await page.mouse.move(r.x+r.width*.65,r.y+r.height*.6,{steps:8});await page.mouse.up();
    assert.notDeepEqual(await page.evaluate(() => window.odinView.camera.position.toArray()),before);
    await page.locator('#live-full').click();
    await page.waitForFunction(() => document.fullscreenElement?.id==='live-image');
    await page.evaluate(() => document.exitFullscreen());
    assert.deepEqual(errors,[]);
    fs.writeFileSync(`${out}/layout-verification.json`,JSON.stringify({results,errors},null,2));
    console.log(results.map(r=>({viewport:r.width,imageWidth:Math.round(r.live.width),imageHeight:Math.round(r.live.height),mapPixels:r.colored})));
  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1});
