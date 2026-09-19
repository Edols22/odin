import * as THREE from 'three';
import {OrbitControls} from 'three/examples/jsm/controls/OrbitControls.js';
import {createIcons, Play, Pause, Square, ScanLine, Save, RotateCcw, Maximize, Focus, Download, Check} from 'lucide';

const $=id=>document.getElementById(id);
const fmt=(n,d=2)=>Number.isFinite(n)?n.toFixed(d):'--';
const stateNames={CONFIRMED:'已确认',TENTATIVE:'待确认',MISSING_CANDIDATE:'待移除',VACATED:'已移除'};
const diagnosticNames={MATCHED:'匹配成功',DETECTOR_SPLIT:'分裂观测 · 保留原编号',DETECTOR_MERGE:'合并观测 · 保护原箱体',STRICT_REJECTED:'P01 未通过',LOW_GEOMETRY_QUALITY:'深度质量未通过',INVALID_OBSERVATION:'几何数据无效',TIME_SYNC_OR_POSE_INVALID:'时间同步或 map 位姿无效',NON_MONOTONIC_FRAME:'帧时间未递增',POSE_ANOMALY:'位姿跳变，地图更新已锁定',VALID:'有效',CLEAR:'原位置已空',GEOMETRY_PRESENT:'几何仍在',OCCLUDED:'被遮挡',UNKNOWN:'证据不足',OUT_OF_VIEW:'视野外',VISIBLE:'可见',insufficient_depth_points:'有效深度点不足',insufficient_spatial_coverage:'深度空间覆盖不足',plane_inlier_ratio_low:'平面内点率不足',median_depth_not_finite:'缺少有效深度',geometry_missing:'无法测量可见面',face_not_measured:'可见面测量失败',partial_face:'可见面不完整'};
const describe=value=>diagnosticNames[value]||({'projected_width_below_0.30m':'投影宽度小于 0.30 m','projected_width_above_0.75m':'投影宽度大于 0.75 m','projected_height_below_0.25m':'投影高度小于 0.25 m','projected_height_above_0.58m':'投影高度大于 0.58 m','projected_aspect_below_0.65':'宽高比小于 0.65','projected_aspect_above_1.90':'宽高比大于 1.90','visible_edge_below_0.35m':'可见边短于 0.35 m','visible_edge_above_0.80m':'可见边长于 0.80 m'})[value]||value||'--';
const pct=value=>Number.isFinite(value)?`${fmt(value*100,1)}%`:'--';
Object.assign(diagnosticNames,{IDENTITY_PENDING:'本帧已测得 · 编号待关联',GEOMETRY_CONFLICT:'旧版历史关联事件',actual_pose_plane_inliers_low:'实际姿态平面内点不足',actual_pose_plane_residual_high:'实际姿态平面误差过大',actual_pose_disagrees_with_support_plane:'姿态与支撑平面不一致'});
Object.assign(diagnosticNames,{OUT_OF_FOV:'视野外 · 保留',PARTIAL_FOV:'部分入镜 · 保留',
  EPOCH_INSUFFICIENT_SUPPORT:'本帧已测得 · 批次支持不足，暂不建档',
  STRICT_REJECTED:'已检出 · P01 定位未通过',LOW_GEOMETRY_QUALITY:'已检出 · 位置证据不足',position_unavailable:'位置未可靠测得',
  MATCHED_POSITION_ONLY:'位置匹配 · 姿态待核验',REDUNDANT_MERGE_OBSERVATION:'冗余合并框 · 使用独立箱框',
  MATCHED_VISIBILITY_ONLY:'已看见 · 保留上次定位',DETECTION_PRESENT:'图像/深度对应 · 本次不更新定位',
  MATCHED_REACQUIRED:'连续观测重新关联 · 保留原编号',IDENTITY_REACQUISITION:'正在连续观测核验原编号',
  RETURN_LOCATION_PENDING:'原位置重新出现 · 连续观测确认中',OBJECT_RETURNED_TO_LOCATION:'原位置恢复记录',
  REDUNDANT_OBJECT_OBSERVATION:'重复观测 · 不另建箱号',position_plane_support_low:'位置平面支撑不足',
  REPLACEMENT_PENDING:'前后排替换待核验',LAYER_REVEALED:'后排露出证据成立',
  EPOCH_COLLECTING:'正在收集批次，尚未提交',EPOCH_CAMERA_MOVED:'相机移动，本批未提交',
  EPOCH_CONTRADICTORY_EVIDENCE:'存在与清空证据矛盾，本批未提交',EPOCH_TIMING:'采样时间不合格，本批未提交',
  EPOCH_POSE_INVALID:'批次位姿无效',EPOCH_TIMEOUT:'批次超时，请重新观察',COMMITTED:'已提交',IDLE:'等待空格或单次按钮',CANCELLED:'批次已取消',
  epoch_insufficient_support:'批次支持帧不足',
  layer_separation_uncertain:'前后层间距不足以可靠区分',untrusted_new_surface:'新箱实测位置或法向不可靠',
  old_surface_clear:'旧箱自身投影已清空，后箱实测面连续确认中',
  old_surface_evaluation:'按旧箱自身投影判断存在状态',
  same_outline_depth_conflict:'箱面轮廓未变但深度突变，当前观测矛盾',
  MEASURED_FRONT_FACE:'历史实测面',POSITION_CONFIRMED_ESTIMATED_FACE:'已定位箱面估计范围（非精确实测边界）',
  old_measured_surface_not_clear:'旧实测表面尚无可靠清空证据',new_plane_ray_mismatch:'新平面与实测射线不一致'});
const assigned=obs=>obs.global_id!==null&&obs.global_id!==undefined;
function diagnosticObservations(data){const rows=new Map();for(const obs of [...(data.rejected||[]),...(data.observations||[])])rows.set(obs.local_detection_id,obs);return [...rows.values()].sort((a,b)=>a.local_detection_id-b.local_detection_id)}
function emptyMapReason(data){
  if(!data.frame)return '尚未执行识别';
  if(!data.commit?.commit)return `本帧未更新地图：${describe(data.commit?.reason)}`;
  if(!data.strict_get_box_node?.raw_yolo)return '本帧没有达到置信度门槛的纸箱检测';
  if(!data.strict_get_box_node?.accepted)return '本帧检测未通过 P01 检查';
  if(!(data.observations||[]).some(o=>!o.conflict_only&&o.geometry_quality_ok))return 'P01 已通过，但深度质量未通过';
  return '当前无有效箱体记录，请查看本帧关联结果';
}
function element(tag,text,className){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(className)node.className=className;return node}
function updateDiagnostics(data){
  const panel=$('rejected'),expanded=new Set([...panel.querySelectorAll('details[open]')].map(x=>x.dataset.key));panel.replaceChildren();
  if(!data.frame&&!data.sample_sequence){panel.append(element('p','尚未执行识别','diagnostic-message'));return}
  const obs=diagnosticObservations(data),config=data.config||{},strict=data.strict_get_box_node||{};
  const mapped=new Set((data.observations||[]).filter(assigned).map(o=>o.global_id));
  const summary=element('div',undefined,'diagnostic-pipeline');
  for(const [name,value] of [['YOLO 检出',strict.raw_yolo??0],['P01 通过',strict.accepted??0],['位置可用',obs.filter(o=>!o.conflict_only&&(o.position_quality_ok??o.geometry_quality_ok)).length],['本帧关联',mapped.size]]){const cell=element('div');cell.append(element('span',name),element('strong',String(value)));summary.append(cell)}panel.append(summary);
  const ok=data.commit?.commit,pose=data.time_sync?.pose_valid;
  const conflicts=obs.filter(o=>o.association_status==='IDENTITY_PENDING');
  if(conflicts.length)panel.append(element('p',`${conflicts.length} 个本帧测量等待编号关联，当前位置与姿态单独显示；旧箱按自身存在证据独立更新。`,'diagnostic-message'));
  const current=data.current_measurements;
  if(current)panel.append(element('p',`定位坐标系 ${current.world_frame}：${current.world_pose_valid?'有效':'未就绪'} · 机器人坐标系 ${current.base_frame}：${current.base_pose_valid?'有效':'未提供 TF'} · 测量质量只依据当前 RGB / 点云`,'diagnostic-timing'));
  const held=obs.filter(o=>o.position_quality_ok&&!o.orientation_quality_ok);
  if(held.length)panel.append(element('p',`${held.length} 个箱子位置证据可用、姿态待核验：可以维持箱体记录，本次姿态不用于精确抓取。`,'diagnostic-message'));
  panel.append(element('p',ok?`map 位姿有效 · 本帧可更新 · ${fmt(data.processing_ms,0)} ms`:`本帧未更新：${describe(data.commit?.reason)}${data.time_sync?.pose_error?' · '+data.time_sync.pose_error:''}`,`diagnostic-message ${ok?'good':'bad'}`));
  panel.append(element('p',`RGB / 深度 ${fmt(data.time_sync?.rgb_depth_dt_ms,1)} / ${config.max_rgb_depth_dt_ms??'--'} ms 上限 · RGB / 位姿 ${fmt(data.time_sync?.rgb_pose_dt_ms,1)} / ${config.max_pose_dt_ms??'--'} ms 上限 · TF ${pose?'有效':'无效'}`,'diagnostic-timing'));
  if(!obs.length)panel.append(element('p',emptyMapReason(data),'diagnostic-message'));
  for(const o of obs){
    const details=element('details',undefined,'diagnostic-object');details.dataset.key=`D${o.local_detection_id}`;details.open=expanded.has(details.dataset.key);
    const heading=element('summary'),strictOk=!(o.rejection_reasons||[]).length&&!o.conflict_only;
    const identity=assigned(o)?`D${o.local_detection_id} → #${o.global_id}`:`D${o.local_detection_id} → ${o.global_ids?.length?o.global_ids.map(id=>'#'+id).join(', '):'未分配'}`;
    const status=['MATCHED_POSITION_ONLY','MATCHED_VISIBILITY_ONLY'].includes(o.association_status)?describe(o.association_status):assigned(o)?stateNames[(data.map||[]).find(b=>b.global_id===o.global_id)?.state]||describe(o.association_status):describe(o.association_status||'STRICT_REJECTED');
    heading.append(element('strong',identity),element('span',status,assigned(o)?'good':'bad'));
    const support=o.depth_support||{},brief=element('div',undefined,'diagnostic-brief');
    for(const [name,value] of [['置信度',pct(o.confidence)],['深度',`${fmt(o.depth_camera_m,3)} m`],['空间覆盖',pct(support.spatial_coverage)],['平面内点',pct(o.plane_inlier_ratio)]]){const cell=element('span');cell.append(element('small',name),element('b',value));brief.append(cell)}heading.append(brief);details.append(heading);
    const checks=element('dl',undefined,'diagnostic-checks');
    const check=(name,value,pass)=>{checks.append(element('dt',name),element('dd',value,pass===undefined?'':pass?'good':'bad'))};
    check('图像检测',o.detection_quality_ok===false?'未通过':'已检出纸箱候选',o.detection_quality_ok!==false);
    check('P01 定位',strictOk?'通过':(o.rejection_reasons||[]).map(describe).join('；')||'未通过',strictOk);
    if(o.position_quality_ok!==undefined)check('位置证据',o.position_quality_ok?'本帧位置质量通过':(o.position_rejection_reasons||[]).map(describe).join('；'),o.position_quality_ok);
    if(o.orientation_quality_ok!==undefined)check('本次姿态',o.orientation_quality_ok?'通过质量检查':'待核验 · 不更新姿态',o.orientation_quality_ok);
    check('有效深度点',`${support.valid_pixels??'--'} / 至少 ${config.min_valid_pixels??'--'} 点`,support.valid_pixels>=config.min_valid_pixels);
    check('空间覆盖',`${pct(support.spatial_coverage)} / 至少 ${pct(config.min_valid_depth_ratio)} · ${support.valid_cells??'--'} / ${support.mask_cells??'--'} 个 ${support.cell_px??'--'} px 采样格`,support.spatial_coverage>=config.min_valid_depth_ratio);
    check('平面内点率',`${pct(o.plane_inlier_ratio)} / 至少 ${pct(config.plane_min_inlier_ratio)}`,o.plane_inlier_ratio>=config.plane_min_inlier_ratio);
    check('平面 RMSE',`${fmt(o.plane_rmse_m*1000,1)} mm`);
    if(o.measurement_quality){check('中心平面内点',`${pct(o.measurement_quality.core_inlier_ratio)} · 位置门槛 ${pct(o.measurement_quality.position_min_core_ratio??.5)} / 姿态门槛 ${pct(o.measurement_quality.orientation_min_core_ratio??.7)}`);check('实际姿态平面 RMSE',`${fmt(o.measurement_quality.plane_rmse_m*1000,1)} mm`);check('两平面法向差',`${fmt(o.measurement_quality.diagnostic_plane_angle_deg,1)}°`)}
    if(o.candidate_global_ids?.length)check('待关联历史编号',o.candidate_global_ids.map(id=>'#'+id).join(', '));
    if(o.return_support)check('放回原位置',`${o.return_support}/3 次独立观测`);
    if(o.replacement_evidence){const r=o.replacement_evidence;check('前后排替换',`${describe(r.reason)}${r.support_frames?` · ${r.support_frames}/${config.replacement_support_frames} 次支持`:''}`);if(r.layer_m!==undefined)check('层间距',`${fmt(r.layer_m,3)} m`);if(r.old_surface_clear_ratio!==undefined)check('旧箱投影清空',`${pct(r.old_surface_clear_ratio)} · ${r.old_surface_valid_pixels} 个有效点`);if(r.new_plane_pixels!==undefined)check('后箱实测面',`${r.new_plane_pixels} 个对应点`)}
    check('RGB 像素占比',pct(support.raw_pixel_ratio??o.valid_depth_ratio));
    check('关联结果',describe(o.association_status));
    if(o.geometry_rejection_reasons?.length)check('姿态限制原因',o.geometry_rejection_reasons.map(describe).join('；'),false);
    if(o.center_camera_optical)check('本帧相机光学坐标',o.center_camera_optical.map(v=>fmt(v,3)).join(' / ')+' m');
    if(o.center_map)check(`本帧 ${data.map_frame||'map'} 坐标`,o.center_map.map(v=>fmt(v,3)).join(' / ')+' m');
    const measured=current?.boxes?.find(b=>b.local_detection_id===o.local_detection_id);
    if(measured?.base)check(`本帧 ${current.base_frame} 坐标`,measured.base.position.map(v=>fmt(v,3)).join(' / ')+' m');
    details.append(checks);panel.append(details);
  }
  const raw=element('details',undefined,'diagnostic-raw');raw.dataset.key='raw';raw.open=expanded.has('raw');raw.append(element('summary','原始数据'),element('pre',JSON.stringify({strict:data.strict_get_box_node,time_sync:data.time_sync,observations:data.observations,rejected:data.rejected},null,2)));panel.append(raw);
}
const colors={CONFIRMED:0x36eda4,TENTATIVE:0xf2db3f,MISSING_CANDIDATE:0xff943d,VACATED:0xff5d64};
let current={},selected=null,view='3d',mapBounds,focusedBounds,boxSignature='',lastFrame='',lastImage='',eventKeys=new Set(),events=[];
let savedCloud,path,liveCloud,boxItems=[],focusPending=true,configBuilt=false;
const cloudRangeMeters=5;
let cloudRangeCenter=null;
let activeSession='';
const host=$('scene'),renderer=new THREE.WebGLRenderer({antialias:true,preserveDrawingBuffer:true});
renderer.setPixelRatio(Math.min(devicePixelRatio,1.5));renderer.setClearColor(0x171e20);renderer.localClippingEnabled=true;host.appendChild(renderer.domElement);
const scene=new THREE.Scene(),camera=new THREE.OrthographicCamera(-10,10,10,-10,.01,1000);
camera.up.set(0,0,1);camera.position.set(10,-10,10);
const controls=new OrbitControls(camera,renderer.domElement);controls.enableDamping=false;
function limitCloudRange(cloud){
  if(!cloud)return;
  const geometry=cloud.geometry,positions=geometry.getAttribute('position');
  let index=geometry.getIndex();
  if(!index){index=new THREE.BufferAttribute(new Uint32Array(positions.count),1);index.setUsage(THREE.DynamicDrawUsage);geometry.setIndex(index)}
  let count=0;
  if(cloudRangeCenter){
    const p=positions.array,[cx,cy,cz]=cloudRangeCenter,r2=cloudRangeMeters*cloudRangeMeters;
    for(let i=0;i<positions.count;i++){const j=i*3,dx=p[j]-cx,dy=p[j+1]-cy,dz=p[j+2]-cz;if(dx*dx+dy*dy+dz*dz<=r2)index.array[count++]=i}
  }
  geometry.setDrawRange(0,count);index.needsUpdate=true;
  cloud.userData.rangeFilter={radius:cloudRangeMeters,center:cloudRangeCenter?.slice()||null,shown:count,total:positions.count};
}
function nearbyMapBounds(){
  if(!cloudRangeCenter)return mapBounds;
  const center=new THREE.Vector3(...cloudRangeCenter),offset=new THREE.Vector3().setScalar(cloudRangeMeters);
  const bounds=new THREE.Box3(center.clone().sub(offset),center.clone().add(offset));
  if(mapBounds){const intersection=bounds.clone().intersect(mapBounds);if(!intersection.isEmpty())return intersection}
  return bounds;
}
function updateCloudRangeCenter(matrix,lastKnown=false){
  const next=matrix?.slice(0,3).map(row=>row[3]);
  const valid=next?.length===3&&next.every(Number.isFinite),hadCenter=cloudRangeCenter!==null;
  const changed=valid?(!cloudRangeCenter||next.some((v,i)=>v!==cloudRangeCenter[i])):hadCenter;
  cloudRangeCenter=valid?next:null;
  $('cloud-range').textContent=valid?`${lastKnown?'上次相机位置':'相机周围'} ${cloudRangeMeters} m 内`:'等待相机位置 · 点云暂隐藏';
  if(changed){limitCloudRange(savedCloud);limitCloudRange(liveCloud);if(!hadCenter&&valid&&mapBounds)fit(nearbyMapBounds())}
}
const clip=new THREE.Plane(new THREE.Vector3(0,0,-1),100),boxGroup=new THREE.Group();scene.add(boxGroup);
const cameraMarker=new THREE.Group();cameraMarker.add(new THREE.AxesHelper(.35));
const frustumGeom=new THREE.BufferGeometry().setFromPoints([
  ...[[0,0,0],[-.2,-.13,.35],[.2,-.13,.35],[0,0,0],[.2,.13,.35],[-.2,.13,.35],[0,0,0],[-.2,-.13,.35],[-.2,.13,.35],[.2,.13,.35],[.2,-.13,.35]].map(v=>new THREE.Vector3(...v))]);
cameraMarker.add(new THREE.Line(frustumGeom,new THREE.LineBasicMaterial({color:0xff65b1,depthTest:false})));cameraMarker.visible=false;scene.add(cameraMarker);
function render(){
  const occupied=[],units=(camera.top-camera.bottom)/(camera.zoom*Math.max(1,host.clientHeight));
  for(const sprite of boxGroup.children.filter(child=>child.isSprite)){
    const anchor=sprite.userData.anchor,point=anchor.clone().project(camera),width=sprite.userData.pixelWidth,height=24;
    sprite.scale.set(width*units,height*units,1);
    const x=(point.x+1)*host.clientWidth/2;let y=(1-point.y)*host.clientHeight/2;
    for(let tries=0;tries<12;tries++){
      if(!occupied.some(r=>Math.abs(r.x-x)<(r.width+width)/2+3&&Math.abs(r.y-y)<height+3))break;
      y-=height+4;
    }
    occupied.push({x,y,width});point.y=1-2*y/host.clientHeight;sprite.position.copy(point.unproject(camera));
  }
  renderer.render(scene,camera)
}controls.addEventListener('change',render);
function fit(bounds=focusedBounds||mapBounds){
  if(!bounds)return;focusedBounds=bounds.clone();
  const center=bounds.getCenter(new THREE.Vector3()),extent=bounds.getSize(new THREE.Vector3());
  const aspect=host.clientWidth/host.clientHeight,radius=Math.max(.8,extent.length()/2),height=radius*1.15/Math.min(1,aspect);
  camera.left=-height*aspect;camera.right=height*aspect;camera.top=height;camera.bottom=-height;camera.zoom=1;
  const direction=view==='top'?new THREE.Vector3(0,0,1):view==='front'?new THREE.Vector3(0,-1,.0001):new THREE.Vector3(1,-1.4,1.3).normalize();
  camera.position.copy(center).addScaledVector(direction,radius*3+10);camera.up.set(0,view==='top'?1:0,view==='top'?0:1);
  controls.target.copy(center);camera.updateProjectionMatrix();controls.update();render();
}
new ResizeObserver(()=>{renderer.setSize(host.clientWidth,host.clientHeight);fit()}).observe(host);
document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>{view=b.dataset.view;document.querySelectorAll('[data-view]').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));fit()});
$('fit').onclick=()=>fit(nearbyMapBounds());
function boxBounds(items){const bounds=new THREE.Box3();items.forEach(item=>item.cuboid_corners_world.forEach(p=>bounds.expandByPoint(new THREE.Vector3(...p))));return bounds.isEmpty()?null:bounds.expandByScalar(.5)}
$('focus').onclick=()=>{const items=selected===null?boxItems:boxItems.filter(b=>b.global_id===selected);const bounds=boxBounds(items);if(bounds)fit(bounds)};
$('saved-cloud').onchange=()=>{if(savedCloud)savedCloud.visible=$('saved-cloud').checked;render()};
$('live-cloud').onchange=()=>{if(liveCloud)liveCloud.visible=$('live-cloud').checked;render()};
$('trajectory').onchange=()=>{if(path)path.visible=$('trajectory').checked;render()};
$('show-boxes').onchange=()=>{boxGroup.visible=$('show-boxes').checked;render()};
$('history').onchange=()=>{boxSignature='';updateBoxes(current.latest||{})};
$('policy-only').onchange=()=>{boxSignature='';updateBoxes(current.latest||{})};
$('point-size').oninput=()=>{if(savedCloud)savedCloud.material.size=+$('point-size').value;render()};
$('ceiling').oninput=()=>{clip.constant=+$('ceiling').value;$('ceiling-value').value=fmt(clip.constant)+' m';render()};
function dispose(object){object.traverse(child=>{child.geometry?.dispose();if(child.material){child.material.map?.dispose();child.material.dispose()}})}
function label(text,color){const canvas=document.createElement('canvas');canvas.width=320;canvas.height=88;const ctx=canvas.getContext('2d');ctx.fillStyle='#142127ee';ctx.fillRect(0,0,320,88);ctx.fillStyle=color;ctx.font='bold 38px sans-serif';const width=ctx.measureText(text).width;if(width>300)ctx.font=`bold ${Math.floor(38*300/width)}px sans-serif`;ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText(text,160,44);const texture=new THREE.CanvasTexture(canvas);const sprite=new THREE.Sprite(new THREE.SpriteMaterial({map:texture,depthTest:false}));sprite.scale.set(.64,.176,1);sprite.renderOrder=10;return sprite}
function select(id){selected=id;boxSignature='';updateBoxes(current.latest||{});const item=boxItems.find(x=>x.global_id===id);if(item)fit(boxBounds([item]))}
function updateBoxes(data){
  boxItems=($('history').checked?data.map_history:data.map)||[];
  if($('policy-only').checked){const ids=new Set((data.active_box_map?.boxes||[]).map(b=>b.global_id));boxItems=boxItems.filter(b=>ids.has(b.global_id))}
  const signature=JSON.stringify([boxItems,data.current_measurements,selected,boxItems.length?'':emptyMapReason(data)]);if(signature===boxSignature)return;boxSignature=signature;
  for(const child of [...boxGroup.children]){boxGroup.remove(child);dispose(child)}
  const edges=[[0,1],[1,2],[2,3],[3,0],[4,5],[5,6],[6,7],[7,4],[0,4],[1,5],[2,6],[3,7]];
  for(const item of boxItems){
    if(!item.cuboid_corners_world)continue;const corners=item.cuboid_corners_world;
    const color=item.global_id===selected?0xffffff:colors[item.state]||0xffffff;
    const geom=new THREE.BufferGeometry().setFromPoints(edges.flatMap(([a,b])=>[new THREE.Vector3(...corners[a]),new THREE.Vector3(...corners[b])]));
    const uncertain=item.orientation_trusted===false;
    const material=uncertain?new THREE.LineDashedMaterial({color,depthTest:false,transparent:true,opacity:.55,dashSize:.04,gapSize:.03}):new THREE.LineBasicMaterial({color,depthTest:false,transparent:true,opacity:item.state==='VACATED'?.45:1});
    const line=new THREE.LineSegments(geom,material);if(uncertain)line.computeLineDistances();line.userData.id=item.global_id;line.renderOrder=5;boxGroup.add(line);
    const center=new THREE.Vector3(...item.center_map),top=Math.max(...corners.map(p=>p[2]));const text=`#${item.global_id} ${uncertain?'姿态待核验':item.orientation_observed_ok===false?'沿用姿态':fmt(item.existence_probability)}`;const sprite=label(text,new THREE.Color(color).getStyle());sprite.position.set(center.x,center.y,top+.19);sprite.userData.id=item.global_id;sprite.userData.anchor=sprite.position.clone();sprite.userData.pixelWidth=Math.max(78,text.length*8);boxGroup.add(sprite);
  }
  // Current unassociated measurements remain visible even when history
  // cannot establish an identity. D labels are frame-local, never global IDs.
  for(const b of data.current_measurements?.boxes||[]){
    if(b.global_id!==null&&b.global_id!==undefined||!b.position_quality_ok||!b.world||!data.current_measurements.synchronized)continue;
    const center=new THREE.Vector3(...b.world.position),n=new THREE.Vector3(...b.world.normal).normalize();
    const x=new THREE.Vector3(...b.world.horizontal);x.addScaledVector(n,-x.dot(n)).normalize();
    const y=new THREE.Vector3().crossVectors(n,x).normalize();
    const front=[[-1,-1],[1,-1],[1,1],[-1,1]].map(([sx,sy])=>center.clone().addScaledVector(x,sx*b.size[0]/2).addScaledVector(y,sy*b.size[1]/2));
    const corners=[...front,...front.map(p=>p.clone().addScaledVector(n,-b.size[2]))];
    const geom=new THREE.BufferGeometry().setFromPoints(edges.flatMap(([a,c])=>[corners[a],corners[c]]));
    const line=new THREE.LineSegments(geom,new THREE.LineDashedMaterial({color:0xffbf47,depthTest:false,dashSize:.025,gapSize:.02,transparent:true,opacity:.8}));
    line.computeLineDistances();line.renderOrder=6;boxGroup.add(line);
    const sprite=label(`D${b.local_detection_id} 本帧 · 编号待关联${b.orientation_quality_ok?'':' · 姿态待核验'}`,'#ffbf47');
    sprite.position.copy(center).add(new THREE.Vector3(0,0,b.size[1]/2+.1));sprite.userData.anchor=sprite.position.clone();sprite.userData.pixelWidth=210;boxGroup.add(sprite);
  }
  const visible=(data.map||[]).filter(b=>b.visibility_state==='VISIBLE').length;
  const occluded=(data.map||[]).filter(b=>b.visibility_state==='OCCLUDED').length;
  $('box-count').textContent=`${visible} 可见 / ${occluded} 遮挡 / ${(data.map||[]).length} 未退场记录`;
  $('boxes').replaceChildren();
  for(const item of boxItems){const row=document.createElement('tr');if(item.global_id===selected)row.className='selected';
    ['#'+item.global_id,stateNames[item.state]||item.state,describe(item.visibility_state),item.orientation_trusted===false?'待核验':item.orientation_observed_ok===false?'沿用历史':'本次通过',fmt(item.existence_probability),item.center_map.map(v=>fmt(v,3)).join(' / '),item.size_world.map(v=>fmt(v)).join(' × '),item.observations,describe(item.last_evidence)].forEach(value=>{const cell=document.createElement('td');cell.textContent=value;row.appendChild(cell)});row.onclick=()=>select(item.global_id);$('boxes').appendChild(row)}
  if(!boxItems.length){const row=element('tr',undefined,'empty-records'),cell=element('td',$('policy-only').checked?'没有已确认且尚未移除的策略记录':emptyMapReason(data));cell.colSpan=9;row.append(cell);$('boxes').append(row)}
  const item=boxItems.find(x=>x.global_id===selected);
  $('selected-summary').textContent=item?`#${item.global_id} · ${stateNames[item.state]} · ${item.center_map.map(v=>fmt(v,3)).join(', ')} m`:'未选中箱体';
  $('detail').textContent=item?`#${item.global_id} | ${describe(item.visibility_state)} | 正证据 ${fmt(item.exist_positive)} / 负证据 ${fmt(item.exist_negative)} | 连续 CLEAR ${item.consecutive_clear_frames} | 存在性范围：${describe(item.visibility_prediction?.geometry_basis||item.last_evidence_detail?.geometry_basis||'UNKNOWN')} | 当前可检查 ${item.visibility_prediction?.expected_visible?'是':'否'} | 预测可见 ${pct(item.visibility_prediction?.predicted_visible_fraction)} | 法向 ${item.normal_world.map(v=>fmt(v,3)).join(', ')} | 最后观测 ${item.last_seen}`:'';
  if(focusPending&&boxItems.length){fit(boxBounds(boxItems));focusPending=false}render();
}
let down;
renderer.domElement.addEventListener('pointerdown',e=>{down=[e.clientX,e.clientY]});
renderer.domElement.addEventListener('pointerup',e=>{if(!down||Math.hypot(e.clientX-down[0],e.clientY-down[1])>5)return;const rect=renderer.domElement.getBoundingClientRect(),ray=new THREE.Raycaster();ray.params.Line.threshold=.06;ray.setFromCamera(new THREE.Vector2((e.clientX-rect.left)/rect.width*2-1,-(e.clientY-rect.top)/rect.height*2+1),camera);const hit=ray.intersectObjects(boxGroup.children).find(h=>h.object.userData.id!==undefined);if(hit)select(hit.object.userData.id)});
async function json(url){const response=await fetch(url);if(!response.ok)throw Error(`${response.status}: ${url}`);return response.json()}
async function command(action,detail={}){try{const response=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,...detail})});const data=await response.json();if(!response.ok)throw Error(data.error);$('notice').textContent=data.saved?`已保存：${data.saved}`:'';await refresh();return data}catch(e){$('notice').textContent=e.message;throw e}}
function action(id,fn){$(id).onclick=()=>fn().catch(()=>{})}
action('start',()=>command('start'));action('pause',()=>command('pause'));action('once',()=>command('once'));action('save',()=>command('snapshot'));
action('stop',async()=>{if(confirm('停止测试进程？当前结果会保留在磁盘；重新开始将建立新的箱体会话。'))await command('stop')});
action('reset',async()=>{if(confirm('重置当前箱体地图、ID 和位姿异常锁定？环境点云不变。')){await command('reset',{confirm:true});focusPending=true;selected=null}});
$('mode').onchange=()=>command('configure',{mode:$('mode').value}).catch(()=>{});
for(const name of ['live','debug'])$(name+'-full').onclick=()=>$(name+'-image').requestFullscreen().catch(e=>$('notice').textContent=e.message);
document.addEventListener('keydown',e=>{if(e.code==='Space'&&!e.repeat&&!['INPUT','TEXTAREA','SELECT','BUTTON'].includes(document.activeElement.tagName)){e.preventDefault();command('once').catch(()=>{})}});
$('config-form').onsubmit=e=>{e.preventDefault();const config={};document.querySelectorAll('[data-config]').forEach(input=>config[input.dataset.config]=Number(input.value));command('configure',{confidence:Number($('confidence').value),inference_size:Number($('inference-size').value),config}).catch(()=>{})};
document.querySelectorAll('[data-tab]').forEach(button=>button.onclick=()=>{document.querySelectorAll('[data-tab]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));document.querySelectorAll('.tab-content').forEach(panel=>panel.hidden=panel.id!==button.dataset.tab);if(button.dataset.tab==='logs')json('/api/log').then(d=>$('logs').textContent=d.log).catch(()=>{})});
async function refresh(){
  const data=await json('/api/state');current=data;const d=data.latest||{},h=data.heartbeat||{};
  if(!data.camera_live)updateCloudRangeCenter(d.world_from_camera,true);
  if(h.session_id&&h.session_id!==activeSession){activeSession=h.session_id;events=[];eventKeys=new Set();selected=null;focusPending=true;lastFrame='';lastImage=''}
  $('status').textContent=!data.running?'识别进程未启动':!data.camera_live?'等待相机数据':h.error?'计算错误':h.enabled?(h.mode==='realtime'?'实时计算中':h.mode==='epoch'?'批次观察模式':'单帧模式'):'预览中 · 识别暂停';
  const epoch=h.epoch||d.epoch||{};
  $('epoch-status').textContent=data.control.mode==='epoch'?`保持相机稳定，空格或单次按钮观察一批 · ${describe(epoch.state||'IDLE')} · ${epoch.collected||0}/${epoch.required||data.control.config.epoch_frames} 帧；整批完成后更新一次地图`:'';
  $('camera-state').textContent=data.camera_live?'在线':'无实时图像';
  if(h.error||data.error)$('notice').textContent=h.error||data.error;
  if(document.activeElement!==$('mode'))$('mode').value=data.control.mode;
  if(!configBuilt){$('inference-size').value=data.control.inference_size;$('confidence').value=data.control.confidence;for(const [key,value] of Object.entries(data.control.config)){if(['center_alpha','normal_alpha','size_alpha','replacement_background_margin_m'].includes(key))continue;const label=document.createElement('label');label.textContent=({geometry_anchor_distance_m:'重复观测位置距离 (m)',geometry_anchor_angle_deg:'替换 / 批次观测法向一致性 (°)'})[key]||key;const input=document.createElement('input');input.type='number';input.step=Number.isInteger(value)?'1':'0.01';input.min='0.001';input.value=value;input.dataset.config=key;label.appendChild(input);$('config-fields').appendChild(label)}configBuilt=true}
  if(data.camera_live&&lastImage!==h.sensor_stamp){lastImage=h.sensor_stamp;$('live-image').src=`/data/live.jpg?t=${h.stamp}`;$('live-empty').hidden=true}
  if((d.frame||d.sample_sequence)&&lastFrame!==`${d.session_id}/${d.sample_sequence??d.frame}`){lastFrame=`${d.session_id}/${d.sample_sequence??d.frame}`;$('debug-image').src=`/data/debug.jpg?t=${d.stamp}`;$('debug-empty').hidden=true}
  if(!d.frame&&!d.sample_sequence){$('debug-image').removeAttribute('src');$('debug-empty').hidden=false}
  $('frame-count').textContent=d.frame?`#${d.frame}`:'';
  const mapPose=d.global_localization&&d.time_sync?.pose_valid;
  const values=[['实时位姿',mapPose?'map 已对齐':(h.preview_pose_valid?'map 预览可用':'map 未就绪')],['全局记录',`${(d.map||[]).filter(b=>b.visibility_state==='VISIBLE').length} 可见 / ${(d.map||[]).length} 未退场`],['计算耗时',`${fmt(d.processing_ms,0)} ms`],['RGB / 深度',`${fmt(d.time_sync?.rgb_depth_dt_ms,1)} ms`],['RGB / 位姿',`${fmt(d.time_sync?.rgb_pose_dt_ms,1)} ms`],['P01 通过 / 原始',`${d.strict_get_box_node?.accepted??0} / ${d.strict_get_box_node?.raw_yolo??0}`]];
  $('metrics').replaceChildren();values.forEach(([name,value])=>{const cell=document.createElement('div'),span=document.createElement('span'),strong=document.createElement('strong');span.textContent=name;strong.textContent=value;cell.append(span,strong);$('metrics').appendChild(cell)});
  updateBoxes(d);
  const active=d.active_box_map||{},delta=d.map_delta||{},policy=$('policy');policy.replaceChildren();
  policy.append(element('p',`完整已知集合 ${(active.boxes||[]).length} 个 · 待核验 ${(active.boxes||[]).filter(b=>b.needs_verification).length} 个 · 版本 ${active.revision??'--'}`,'diagnostic-message'));
  policy.append(element('p','包含已确认、尚未确认移除的箱子；视野外和待移除箱仍保留。该集合不等于可立即抓取的目标清单。'));
  for(const [key,name] of [['added','新增'],['removed','移除'],['updated','定位更新'],['kept','保留']])policy.append(element('p',`${name}：${(delta[key]||[]).map(id=>'#'+id).join('、')||'无'}`));
  policy.append(element('p',`map 定位：${active.localization?.valid?'有效':'未就绪'} · 集合时间 ${active.stamp??'--'}`));
  for(const [file,name] of [['active_boxes.json','下载完整策略集合'],['map_delta.json','下载最近变更']]){const link=element('a',name+' ');link.href='/data/'+file;link.download=file;policy.append(link)}
  for(const event of d.events||[]){const key=JSON.stringify([d.session_id,event]);if(!eventKeys.has(key)){eventKeys.add(key);events.unshift(event)}}events=events.slice(0,100);if(eventKeys.size>1000)eventKeys=new Set();
  $('events').replaceChildren();events.forEach(event=>{const row=document.createElement('div');row.textContent=`${event.event} ${event.global_id===null?'':`#${event.global_id}`}`;const detail=document.createElement('small');detail.textContent=`帧 ${event.frame} · ${JSON.stringify(event.detail)}`;row.appendChild(detail);$('events').appendChild(row)});
  $('relocation').textContent=JSON.stringify(d.relocation_hypotheses||[],null,2);
  updateDiagnostics(d);
}
async function updateCloud(){
  const data=await json('/data/live_cloud.json');if(liveCloud){scene.remove(liveCloud);dispose(liveCloud)}
  liveCloud=null;updateCloudRangeCenter(data.world_from_camera);
  const geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.Float32BufferAttribute(data.points.flat(),3));
  liveCloud=new THREE.Points(geometry,new THREE.PointsMaterial({color:0xb3f0ed,size:2,sizeAttenuation:false}));liveCloud.name='live-cloud';limitCloudRange(liveCloud);liveCloud.visible=$('live-cloud').checked;scene.add(liveCloud);
  cameraMarker.visible=!!data.world_from_camera;if(cameraMarker.visible){const matrix=new THREE.Matrix4().set(...data.world_from_camera.flat());cameraMarker.matrixAutoUpdate=false;cameraMarker.matrix.copy(matrix)}render();
}
async function loadMap(){
  const meta=await json('/map/metadata.json'),response=await fetch('/map/points.f32'),positions=new Float32Array(await response.arrayBuffer());
  mapBounds=new THREE.Box3(new THREE.Vector3(...meta.bounds[0]),new THREE.Vector3(...meta.bounds[1]));
  const geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.BufferAttribute(positions,3));const pointColors=new Float32Array(positions.length),c=new THREE.Color();
  for(let i=0;i<positions.length;i+=3){const h=(positions[i+2]-meta.bounds[0][2])/(meta.bounds[1][2]-meta.bounds[0][2]);c.setHSL(.55-.48*h,.5,.40);pointColors.set([c.r,c.g,c.b],i)}geometry.setAttribute('color',new THREE.BufferAttribute(pointColors,3));
  savedCloud=new THREE.Points(geometry,new THREE.PointsMaterial({vertexColors:true,size:1.5,sizeAttenuation:false,clippingPlanes:[clip]}));savedCloud.name='environment-cloud';savedCloud.visible=$('saved-cloud').checked;limitCloudRange(savedCloud);scene.add(savedCloud);
  const trajectory=await fetch('/map/trajectory.f32');const pathGeometry=new THREE.BufferGeometry();pathGeometry.setAttribute('position',new THREE.BufferAttribute(new Float32Array(await trajectory.arrayBuffer()),3));path=new THREE.Line(pathGeometry,new THREE.LineBasicMaterial({color:0xc777ae,depthTest:false}));path.visible=false;scene.add(path);
  const grid=new THREE.GridHelper(40,40,0x475252,0x293638);grid.rotation.x=Math.PI/2;grid.position.z=meta.bounds[0][2]-.02;scene.add(grid);scene.add(new THREE.AxesHelper(1));
  $('ceiling').min=meta.bounds[0][2];$('ceiling').max=meta.bounds[1][2];$('ceiling').value=meta.bounds[1][2];clip.constant=meta.bounds[1][2];$('ceiling-value').value=fmt(clip.constant)+' m';
  $('map-info').textContent=`地图共 ${meta.points.toLocaleString()} 点 · ${meta.keyframes} 关键帧 · ${meta.source.split('/').pop()}`;fit(nearbyMapBounds());
}
createIcons({icons:{Play,Pause,Square,ScanLine,Save,RotateCcw,Maximize,Focus,Download,Check}});
window.odinView={scene,camera,controls,renderer,render,get boxes(){return boxItems},get state(){return current}};
loadMap().catch(e=>$('notice').textContent=`地图加载失败：${e.message}`);
async function poll(){try{await refresh();if(current.camera_live)await updateCloud()}catch(e){$('status').textContent=`连接异常：${e.message}`}setTimeout(poll,500)}poll();
