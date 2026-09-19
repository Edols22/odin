'use strict';
const $=id=>document.getElementById(id);
const colors=['#65f1c4','#ffc46b','#bda3ff'];
const probes=[0,1,2].map(i=>({name:'ABC'[i],color:colors[i],mode:'aligned',x:[.38,.55,.68][i],y:.55,history:[],base:null,current:null}));
let frame=null,paused=false,busy=false,lastStamp=null,rgbBitmap=null,packetReceived=0;
const W=800,H=648,depthBuffer=new Float32Array(W*H),palette=[];
// Fixed color mapping, identical for every frame and both depth views.
for(let i=0;i<256;i++){const c=document.createElement('canvas');c.width=c.height=1;const ctx=c.getContext('2d');ctx.fillStyle=`hsl(${250*(1-i/255)} 90% 55%)`;ctx.fillRect(0,0,1,1);palette.push([...ctx.getImageData(0,0,1,1).data])}
function limits(){return {low:Number($('min').value),high:Number($('max').value),confidence:Number($('confidence').value),radius:Number($('radius').value)}}
function validLimits(){const c=limits();return Number.isFinite(c.low)&&Number.isFinite(c.high)&&c.low>=0&&c.high>c.low&&c.high<=20&&c.confidence>=0&&c.radius>=2&&c.radius<=60}
function colorAt(distance,c){return palette[Math.round(255*Math.max(0,Math.min(1,(distance-c.low)/(c.high-c.low))))]}
function putPixel(data,index,color){data[index*4]=color[0];data[index*4+1]=color[1];data[index*4+2]=color[2];data[index*4+3]=255}
function quantile(sorted,f){return sorted[Math.min(sorted.length-1,Math.floor((sorted.length-1)*f))]}
function drawProbes(canvas,mode){const ctx=canvas.getContext('2d'),c=limits();probes.forEach(p=>{if(p.mode!==mode)return;const x=p.x*canvas.width,y=p.y*canvas.height,r=mode==='aligned'?c.radius:Math.max(2,c.radius*canvas.width/W);ctx.strokeStyle=p.color;ctx.lineWidth=2;ctx.strokeRect(x-r,y-r,2*r,2*r);ctx.font=`bold ${mode==='aligned'?20:12}px sans-serif`;ctx.fillStyle='#102027';ctx.fillRect(x+r+2,y-17,22,21);ctx.fillStyle=p.color;ctx.fillText(p.name,x+r+5,y)})}
function calculate(append){
 if(!frame||!validLimits())return;
 const {info,points}=frame,c=limits(),T=info.tcl,k=info.intrinsics;
 const raw=$('raw');if(raw.width!==info.width||raw.height!==info.height){raw.width=info.width;raw.height=info.height}
 const rawctx=raw.getContext('2d'),rawImage=rawctx.createImageData(raw.width,raw.height),ctx=$('depth').getContext('2d'),image=ctx.createImageData(W,H);
 depthBuffer.fill(Infinity);const values=probes.map(()=>[]);let count=0;
 for(let i=0;i<info.points;i++){
  const x=points[i*4],y=points[i*4+1],z=points[i*4+2],confidence=points[i*4+3],range=Math.hypot(x,y,z);
  if(!Number.isFinite(range)||range<=.01||confidence<c.confidence)continue;
  count++;putPixel(rawImage.data,i,colorAt(range,c));
  const cx=T[0]*x+T[1]*y+T[2]*z+T[3],cy=T[4]*x+T[5]*y+T[6]*z+T[7],cz=T[8]*x+T[9]*y+T[10]*z+T[11];
  const u=(k.fx*cx/cz+k.skew*cy/cz+k.cx)*W/info.rgb_width,v=(k.fy*cy/cz+k.cy)*H/info.rgb_height;
  if(cz>.01&&Number.isFinite(u)&&Number.isFinite(v)&&u>=0&&u<W&&v>=0&&v<H){const pixel=Math.floor(v)*W+Math.floor(u);if(cz<depthBuffer[pixel])depthBuffer[pixel]=cz}
  for(let j=0;j<probes.length;j++){
   const p=probes[j];
   if(p.mode==='raw'){
    const r=Math.max(2,c.radius*raw.width/W);
    if(Math.abs(i%raw.width-p.x*raw.width)<=r&&Math.abs(Math.floor(i/raw.width)-p.y*raw.height)<=r)values[j].push(range);
   }
  }
 }
 // Only visible, real z-buffer samples enter RGB-aligned statistics.
 for(let i=0;i<depthBuffer.length;i++)if(Number.isFinite(depthBuffer[i]))putPixel(image.data,i,colorAt(depthBuffer[i],c));
 probes.forEach((p,j)=>{if(p.mode==='aligned'){
   for(let y=Math.max(0,Math.floor(p.y*H-c.radius));y<Math.min(H,p.y*H+c.radius);y++)for(let x=Math.max(0,Math.floor(p.x*W-c.radius));x<Math.min(W,p.x*W+c.radius);x++){
    const v=depthBuffer[y*W+x];if(Number.isFinite(v))values[j].push(v)
   }
  }
  const samples=values[j].sort((a,b)=>a-b);
  p.current=samples.length>=3?{value:quantile(samples,.5),p10:quantile(samples,.1),p90:quantile(samples,.9),count:samples.length}:null;
  if(append){if(p.base===null&&p.current)p.base=p.current.value;p.history.push({stamp:info.stamp,...(p.current||{value:null,count:samples.length})});p.history=p.history.filter(v=>info.stamp-v.stamp<=120).slice(-300)}
 });
 rawctx.putImageData(rawImage,0,0);ctx.putImageData(image,0,0);drawProbes(raw,'raw');drawProbes($('depth'),'aligned');
 const rgbctx=$('rgb').getContext('2d');rgbctx.fillStyle='#050a0c';rgbctx.fillRect(0,0,W,H);
 if(rgbBitmap&&info.rgb_valid)rgbctx.drawImage(rgbBitmap,0,0,W,H);else{rgbctx.fillStyle='#a6b9c1';rgbctx.font='22px sans-serif';rgbctx.fillText('等待时间匹配的 RGB',30,45)}
 drawProbes($('rgb'),'aligned');
 $('min-label').textContent=c.low.toFixed(1)+' m';$('max-label').textContent=c.high.toFixed(1)+' m';
 $('sync').textContent=info.rgb_valid?`RGB / 点云 Δt ${info.rgb_dt_ms.toFixed(2)} ms`:'RGB 未匹配；深度仍实时';
 $('stats').textContent=`${info.width} × ${info.height} 原始网格 · ${count.toLocaleString()} 有效点 · 点云时间 ${info.stamp.toFixed(3)} · 服务端处理 ${info.processing_ms.toFixed(1)} ms · 2 Hz 显示 · 不插值补洞`;
 renderReadings();drawChart();
}
function renderReadings(){const root=$('readings');root.replaceChildren();probes.forEach(p=>{const card=document.createElement('div');card.className='reading';card.style.setProperty('--color',p.color);const value=p.current?.value,delta=value!==undefined&&p.base!==null?(value-p.base)*1000:null;card.innerHTML=`<b>${p.name} · ${p.mode==='raw'?'原始 R':'对齐 Z'}</b><strong>${value!==undefined?value.toFixed(4)+' m':'无有效测距'}</strong><small>Δ ${delta===null?'--':(delta>=0?'+':'')+delta.toFixed(1)} mm</small><small>P10–P90 ${p.current?((p.current.p90-p.current.p10)*1000).toFixed(1)+' mm':'--'} · ${p.current?.count??0} 点</small>`;root.appendChild(card)})}
function drawChart(){
 const canvas=$('chart'),ctx=canvas.getContext('2d'),relative=$('relative').checked,c=limits(),w=canvas.width,h=canvas.height,pad=55;
 ctx.fillStyle='#172228';ctx.fillRect(0,0,w,h);ctx.font='12px sans-serif';
 const span=Number($('chart-range').value),low=relative?-span:c.low,high=relative?span:c.high,now=frame?.info.stamp??0;
 for(let i=0;i<=6;i++){const v=low+(high-low)*i/6,y=h-30-(h-50)*i/6;ctx.strokeStyle='#344750';ctx.beginPath();ctx.moveTo(pad,y);ctx.lineTo(w-12,y);ctx.stroke();ctx.fillStyle='#a6b9c1';ctx.fillText(v.toFixed(relative?0:2),4,y+4)}
 ctx.fillStyle='#a6b9c1';ctx.fillText(relative?'mm':'m',5,15);ctx.fillText('-120 s',pad,h-7);ctx.fillText('现在',w-44,h-7);
 ctx.save();ctx.beginPath();ctx.rect(pad,20,w-pad-12,h-50);ctx.clip();
 probes.forEach(p=>{ctx.strokeStyle=p.color;ctx.lineWidth=2;ctx.beginPath();let connected=false,prev=null;
  p.history.forEach(sample=>{if(sample.value===null||p.base===null){connected=false;return}const value=relative?(sample.value-p.base)*1000:sample.value,x=pad+(sample.stamp-now+120)/120*(w-pad-12),y=h-30-(value-low)/(high-low)*(h-50);if(!connected||prev!==null&&sample.stamp-prev>2)ctx.moveTo(x,y);else ctx.lineTo(x,y);connected=true;prev=sample.stamp});ctx.stroke();
 });ctx.restore();
}
function reset(){probes.forEach(p=>{p.history=[];p.base=null});if(frame)calculate(true)}
for(const id of ['min','max','confidence','radius'])$(id).addEventListener('change',()=>{if(!validLimits()){$('error').textContent='请输入有效量程、置信度和区域半径。';return}$('error').textContent='';if(id==='confidence'||id==='radius')reset();else calculate(false)});
$('relative').onchange=drawChart;$('chart-range').onchange=drawChart;$('reset').onclick=reset;
$('pause').onclick=()=>{paused=!paused;$('pause').textContent=paused?'继续实时':'暂停画面';$('status').textContent=paused?'已暂停 · 固定当前画面':'正在恢复实时数据'};
for(const id of ['rgb','depth','raw'])$(id).addEventListener('click',event=>{
 const canvas=$(id),rect=canvas.getBoundingClientRect(),aspect=canvas.width/canvas.height;
 let width=rect.width,height=rect.height,left=rect.left,top=rect.top;
 if(width/height>aspect){width=height*aspect;left+=(rect.width-width)/2}else{height=width/aspect;top+=(rect.height-height)/2}
 const x=(event.clientX-left)/width,y=(event.clientY-top)/height;if(x<0||x>1||y<0||y>1)return;
 const p=probes[Number($('probe').value)];Object.assign(p,{x,y,mode:id==='raw'?'raw':'aligned',base:null,history:[]});
 calculate(false);if(p.current){p.base=p.current.value;p.history.push({stamp:frame.info.stamp,...p.current})}renderReadings();drawChart();
});
document.querySelectorAll('[data-full]').forEach(b=>b.onclick=()=>$(b.dataset.full).requestFullscreen().catch(e=>$('error').textContent=e.message));
$('export').onclick=()=>{const rows=['probe,source,u_fraction,v_fraction,sensor_stamp_s,median_m,baseline_m,delta_mm,p10_m,p90_m,valid_points'];probes.forEach(p=>p.history.forEach(v=>rows.push([p.name,p.mode,p.x,p.y,v.stamp,v.value??'',p.base??'',v.value!==null&&p.base!==null?(v.value-p.base)*1000:'',v.p10??'',v.p90??'',v.count].join(','))));const url=URL.createObjectURL(new Blob(['\ufeff'+rows.join('\n')],{type:'text/csv;charset=utf-8'})),a=document.createElement('a');a.href=url;a.download='odin-depth-'+Date.now()+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
async function poll(){
 if(paused||busy)return;busy=true;
 try{
  const response=await fetch('/api/frame',{cache:'no-store',signal:AbortSignal.timeout(4000)});
  if(!response.ok)throw Error('等待原始点云；相机正在启动或尚未连接');
  const buffer=await response.arrayBuffer(),headerLength=new DataView(buffer).getUint32(0,true),offset=4+headerLength;
  const info=JSON.parse(new TextDecoder().decode(buffer.slice(4,offset)));
  if(buffer.byteLength!==offset+info.cloud_bytes+info.jpeg_bytes)throw Error('深度数据包不完整');
  if(Date.now()/1000-info.received_at>3){$('status').textContent='数据已停止 · 当前为最后一帧';throw Error('超过 3 秒未收到新的点云，不追加曲线数据')}
  if(info.stamp===lastStamp)return;
  const points=new Float32Array(buffer.slice(offset,offset+info.cloud_bytes));
  const bitmap=info.jpeg_bytes?await createImageBitmap(new Blob([buffer.slice(offset+info.cloud_bytes)],{type:'image/jpeg'})):null;
  if(paused){bitmap?.close();return}
  if(lastStamp!==null&&info.stamp<lastStamp)probes.forEach(p=>{p.history=[];p.base=null});
  rgbBitmap?.close();rgbBitmap=bitmap;frame={info,points};lastStamp=info.stamp;packetReceived=Date.now();
  $('status').textContent='实时深度 · 2 Hz';$('error').textContent='';calculate(true);
 }catch(error){if(!paused){$('error').textContent=error.message;$('status').textContent=frame?'连接中断 · 当前为最后一帧':'等待相机数据'}}finally{busy=false}
}
renderReadings();drawChart();poll();setInterval(poll,500);
window.depthView={get frame(){return frame},probes,get paused(){return paused},get lastStamp(){return lastStamp},get depthBuffer(){return depthBuffer}};
