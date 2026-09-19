"""Odin DTOF adapter and P01 bridge; geometry core has no vendor dependency."""
import hashlib
from pathlib import Path
from types import SimpleNamespace
import cv2
import numpy as np
from .schema import CapturePacket,BoxObservation,MeasuredSurface,unit,transform_ok

def pointcloud2_arrays(message):
    from sensor_msgs_py import point_cloud2
    raw=point_cloud2.read_points(message,skip_nans=False)
    xyz=np.column_stack([raw[k].ravel() for k in ('x','y','z')])
    names=raw.dtype.names
    return xyz,np.arange(len(xyz)),raw['confidence'].ravel() if 'confidence' in names else None,raw['offset_time'].ravel() if 'offset_time' in names else None

def project_depth(capture):
    points,origins=capture.world_rays();t=np.linalg.inv(capture.t_world_camera)
    optical=points@t[:3,:3].T+t[:3,3];h,w=capture.image.shape[:2];intr=capture.intrinsics
    valid=np.isfinite(optical).all(1)&(optical[:,2]>.2)&(optical[:,2]<8)&(np.linalg.norm(capture.points_sensor,axis=1)>.2)
    if capture.confidence is not None:valid &= np.asarray(capture.confidence)>=35
    indices=np.flatnonzero(valid);p=optical[indices]
    u=np.rint((intr['fx']*p[:,0]+intr.get('skew',0)*p[:,1])/p[:,2]+intr['ppx']).astype(int)
    v=np.rint(intr['fy']*p[:,1]/p[:,2]+intr['ppy']).astype(int)
    valid=(u>=0)&(u<w)&(v>=0)&(v<h);indices=indices[valid];u=u[valid];v=v[valid];p=p[valid]
    order=np.argsort(p[:,2],kind='stable');pixels=v*w+u
    _,first=np.unique(pixels[order],return_index=True);chosen=order[first]
    depth=np.full((h,w),np.nan,np.float32);source=np.full((h,w),-1,np.int64)
    depth[v[chosen],u[chosen]]=p[chosen,2];source[v[chosen],u[chosen]]=indices[chosen]
    return depth,source,points

def observation_from_odin(obs,capture,source,points):
    mask=obs.get('_mask');center=obs.get('center_map');normal=obs.get('normal_world')
    if mask is None or center is None or normal is None:return None
    inner=cv2.erode(mask.astype('uint8'),np.ones((5,5),np.uint8)).astype(bool)
    indices=source[inner];indices=np.unique(indices[indices>=0]);n=unit(normal);c=np.asarray(center)
    if len(indices)<3:return None
    residual=(points[indices]-c)@n;indices=indices[abs(residual)<.02]
    if len(indices)<3:return None
    pts=points[indices];projected=pts-((pts-c)@n)[:,None]*n
    u=unit(np.asarray(obs['horizontal_world'])-n*np.dot(obs['horizontal_world'],n));v=unit(np.cross(n,u))
    uv=np.column_stack(((projected-c)@u,(projected-c)@v)).astype('float32')
    hull=cv2.convexHull(uv).reshape(-1,2)
    if len(hull)<3 or cv2.contourArea(hull)<1e-4:return None
    polygon=c+hull[:,0,None]*u+hull[:,1,None]*v
    q=obs.get('measurement_quality',{});m=obs.get('measurement') or {};size=obs.get('size_world',[.4,.4,.3])
    width,height=size[:2];coverage=cv2.contourArea(hull)/max(width*height,1e-8)
    prior=bool(q.get('size_prior_fallback')) or bool(m.get('size_prior_regularization',{}).get('used'))
    # Provenance is a hull of *actual supporting points*, never the constructed cuboid corners.
    # A prior used to stabilise width/height does not replace the independently
    # measured raw-point polygon. Occluded or clipped RGB faces remain partial.
    full=coverage>=.70 and not any(m.get('partial_observation',{}).values())
    position=bool(obs.get('position_quality_ok')) and not obs.get('conflict_only',False)
    orientation=bool(obs.get('orientation_quality_ok'))
    surface=MeasuredSurface(c.tolist(),n.tolist(),polygon.tolist(),full,
        position and orientation,'ODIN_CLOUD_RAW',capture.capture_id,capture.rgb_stamp,
        np.asarray(capture.point_ids)[indices].tolist(),max(.01,3*float(q.get('plane_rmse_m',.02))))
    measured='PRIOR_ESTIMATED' if prior else 'MEASURED'
    top=n[2]>.75
    return BoxObservation(int(obs.get('local_detection_id',0)),capture.capture_id,surface,u.tolist(),
        [width,height,.40] if top else [width,.30,height],list(obs.get('bbox',[])),mask,float(obs.get('confidence',0)),position,orientation,
        face_type='TOP' if top else 'FRONT',dimension_status=(measured,measured,'PRIOR_ESTIMATED') if top else (measured,'PRIOR_ESTIMATED',measured))

class OdinFrontend:
    """Reuse only existing P01 observation building, not its persistent-map interface."""
    def __init__(self,root,device='0'):
        import sys
        root=Path(root);sys.path.insert(0,str(root/'src/robot_perception/tools/diagnostics'))
        from odin_persistent_box_test import OdinPersistentBoxTest,YOLO,PersistentCartonMap
        self.bridge=object.__new__(OdinPersistentBoxTest)
        a=self.bridge;a.model=YOLO(str(root/'src/robot_perception/assets/box_seg.pt'))
        a.args=SimpleNamespace(conf=.70,imgsz=640,device=device)
        a.mapper=PersistentCartonMap();a.strict_conf=.70;a.box_length=.40;a.mask_erode_px=5;a.max_points_per_box=1500

    def detect(self,capture):
        import time
        started=time.perf_counter();depth,source,points=project_depth(capture);projection_ms=(time.perf_counter()-started)*1000;a=self.bridge
        a.image=capture.image;a.image_stamp=capture.rgb_stamp;a.cloud_stamp=capture.cloud_stamp
        a.intr=capture.intrinsics;a.world_from_camera=capture.t_world_camera;a.world_from_cloud=capture.t_world_sensor
        a.tcl=np.linalg.inv(capture.t_world_camera)@capture.t_world_sensor
        t=time.perf_counter();accepted,rejected,splits,count=a._strict_observations(capture.image,depth,capture.points_sensor);measurement_ms=(time.perf_counter()-t)*1000
        obs=[observation_from_odin(o,capture,source,points) for o in accepted]
        return [o for o in obs if o is not None],dict(raw_count=count,rejected=a._json_safe(rejected),splits=splits,
            frontend_ms=dict(projection=projection_ms,yolo_p01=measurement_ms,measured_surface=(time.perf_counter()-t)*1000-measurement_ms)),depth

    def refine(self,capture,analysis,max_rois=2):
        """One bounded re-detection of unexplained geometric ROIs using the same YOLO/P01."""
        from dataclasses import replace
        from scipy.spatial import cKDTree
        points=analysis.rays.points;labels=analysis.rays.labels
        indices=np.flatnonzero((labels==1)|(labels==4))
        # Work only within RGB FOV and configured operating distance; sample into voxels.
        if len(indices)<80:return [],[],[]
        p=points[indices];vox=np.floor(p/.03).astype(int);_,unique=np.unique(vox,axis=0,return_index=True)
        indices=indices[unique]
        if len(indices)>4000:indices=indices[np.linspace(0,len(indices)-1,4000).astype(int)]
        p=points[indices];tree=cKDTree(p);seen=set();groups=[]
        for i in range(len(p)):
            if i in seen:continue
            stack=[i];seen.add(i);group=[]
            while stack:
                j=stack.pop();group.append(j)
                for k in tree.query_ball_point(p[j],.06):
                    if k not in seen:seen.add(k);stack.append(k)
            if len(group)>=30:groups.append((p[group],indices[group]))
        rois=[];observations=[];candidates=[];h,w=capture.image.shape[:2];t=np.linalg.inv(capture.t_world_camera);intr=capture.intrinsics
        for cloud,raw_indices in sorted(groups,key=lambda pair:len(pair[0]),reverse=True):
            if len(rois)>=max_rois:break
            centered=cloud-cloud.mean(0);_,_,v=np.linalg.svd(centered,full_matrices=False)
            if np.sqrt(np.mean((centered@v[-1])**2))>.03:continue
            optical=cloud@t[:3,:3].T+t[:3,3];optical=optical[optical[:,2]>.2]
            if not len(optical):continue
            uv=optical[:,:2]/optical[:,2,None]*[intr['fx'],intr['fy']]+[intr['ppx'],intr['ppy']]
            l,top=np.maximum(np.floor(uv.min(0))-20,[0,0]).astype(int);r,b=np.minimum(np.ceil(uv.max(0))+20,[w,h]).astype(int)
            if r-l<40 or b-top<40:continue
            if any(o.bbox and l>=o.bbox[0]-10 and top>=o.bbox[1]-10 and r<=o.bbox[2]+10 and b<=o.bbox[3]+10
                   and abs((cloud.mean(0)-o.face.center)@unit(o.face.normal))<.04 for o in analysis.observations):continue
            rois.append([int(l),int(top),int(r),int(b)])
            candidate=dict(kind='GEOMETRIC_CANDIDATE',capture_id=capture.capture_id,bbox=rois[-1],
                center_map=cloud.mean(0).tolist(),normal_world=v[-1].tolist(),sampled_points=len(cloud),
                source_point_ids=np.asarray(capture.point_ids)[raw_indices].tolist(),reason='NO_INDEPENDENT_RGB_INSTANCE',inventory_permission=False)
            crop=replace(capture,image=capture.image[top:b,l:r],intrinsics=dict(intr,ppx=intr['ppx']-l,ppy=intr['ppy']-top))
            new,_,_=self.detect(crop)
            for o in new:
                full=np.zeros((h,w),bool);full[top:b,l:r]=o.mask;o.mask=full
                o.bbox=[o.bbox[0]+l,o.bbox[1]+top,o.bbox[2]+l,o.bbox[3]+top]
                if any(np.linalg.norm(np.asarray(o.face.center)-p.face.center)<.08 for p in analysis.observations+observations):continue
                o.local_id=len(analysis.observations)+len(observations);o.proposal_source='GEOMETRY_ROI_YOLO';observations.append(o)
            if not any(o.position_ok and np.linalg.norm(np.asarray(o.face.center)-cloud.mean(0))<.25 for o in analysis.observations+observations):candidates.append(candidate)
        return observations,rois,candidates

def load_saved_capture(path,task_id='odin_validation'):
    import json
    path=Path(path);meta=json.loads(path.with_suffix('.json').read_text())
    if not transform_ok(meta.get('world_from_camera')):raise ValueError('RECORDED_WORLD_POSE_UNAVAILABLE')
    if not transform_ok(meta.get('tcl')):raise ValueError('RECORDED_CALIBRATION_UNAVAILABLE')
    with np.load(path.with_suffix('.npz')) as data:
        raw=data['raw_cloud'];points=np.column_stack([raw[k].ravel() for k in ('x','y','z')])
        depth=data['depth'];packed=data['masks_packed']
        if not packed.size:masks=np.zeros((0,*depth.shape),bool)
        elif packed.ndim==3:masks=np.unpackbits(packed,axis=-1)[:,:,:depth.shape[1]].astype(bool)
        else:raise ValueError('RECORDED_MASK_LAYOUT_INVALID')
    tcam=np.asarray(meta['world_from_camera']);tcl=np.asarray(meta['tcl'])
    image=cv2.imread(str(path.with_suffix('.jpg')))
    if image is None:raise ValueError('RECORDED_IMAGE_UNREADABLE')
    capture=CapturePacket(str(meta.get('sensor_stamp')),meta['sensor_stamp'],meta['cloud_stamp'],points,np.arange(len(points)),
        tcam@tcl,tcam,meta['intrinsics'],image,
        raw['confidence'].ravel() if 'confidence' in raw.dtype.names else None,
        raw['offset_time'].ravel() if 'offset_time' in raw.dtype.names else None,
        task_id=task_id,map_epoch='recorded:'+str(path.parent),calibration_revision=hashlib.sha256(tcl.tobytes()).hexdigest(),
        pose_quality=True,stable_capture=False,source='ODIN_RECORDED_RAW')
    # A single recorded pose is not a full scan-stability proof. Caller must check the sequence.
    return capture,meta,masks
