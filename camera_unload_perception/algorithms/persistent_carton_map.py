"""World-space carton persistence, following odin_nx_persistent_box_mapping_spec.

The core consumes synchronized, vendor-independent observations. Sensor topic
names, detector execution, robot commands and UI concerns stay in the adapter.
"""
from copy import copy, deepcopy
import math
import uuid

import cv2
import numpy as np
from scipy.optimize import linear_sum_assignment
from scipy.spatial.transform import Rotation


DEFAULTS = {
    'max_center_distance_m': .20, 'min_iou_gate': .05, 'min_overlap_gate': .15,
    'max_depth_residual_m': .12, 'max_match_cost': .55,
    'weight_position': .30, 'weight_mask_iou': .30, 'weight_depth': .25, 'weight_size': .15,
    'mask_erode_px': 5, 'max_points_per_box': 1500, 'min_valid_depth_ratio': .40,
    'plane_min_inlier_ratio': .50, 'plane_residual_m': .025,
    'depth_support_cell_px': 8,
    'min_valid_pixels': 80, 'min_valid_ratio': .40, 'edge_margin_px': 5,
    'same_ratio_threshold': .55, 'occlusion_ratio_threshold': .50, 'clear_ratio_threshold': .55,
    'depth_consistency_m': .04, 'split_union_iou_threshold': .60, 'split_depth_delta_m': .05,
    'merge_overlap_threshold': .30, 'beta_alpha': 1., 'beta_beta': 1.,
    'matched_positive_weight': 1., 'geometry_present_weight': .30, 'clear_negative_weight': 1.50,
    'min_observations': 3, 'min_existence_probability': .70,
    'missing_candidate_after_clear_frames': 2, 'vacated_after_clear_frames': 4,
    'max_existence_probability': .25, 'center_alpha': .20, 'normal_alpha': .20, 'size_alpha': .10,
    'max_rgb_depth_dt_ms': 20., 'max_pose_dt_ms': 20.,
    'max_translation_speed_m_s': 1., 'max_rotation_speed_deg_s': 120.,
    'pose_translation_slack_m': .03, 'pose_rotation_slack_deg': 3.,
    'relocation_max_time_gap_sec': 3., 'relocation_max_distance_m': 1.,
    'relocation_weight_size': .35, 'relocation_weight_time': .20,
    'relocation_weight_spatial': .20, 'relocation_weight_motion': .15,
    'hypothesis_threshold': .60, 'likely_threshold': .80, 'min_support_frames': 2,
    'max_existence_evidence': 12.,
    'geometry_anchor_distance_m': .06, 'geometry_anchor_angle_deg': 12.,
    'geometry_conflict_iou': .55,
    'min_projected_pixels': 300,
    'replacement_min_layer_m': .18,
    'replacement_support_frames': 3, 'replacement_max_gap_s': 10.,
    'replacement_position_spread_m': .05,
    'epoch_frames': 3, 'epoch_min_support': 2, 'epoch_timeout_s': 20.,
    'epoch_translation_m': .01, 'epoch_rotation_deg': 2.,
    'epoch_position_spread_m': .05, 'epoch_clear_ratio': .60,
    'reacquire_support_frames': 3, 'reacquire_min_iou': .75,
}


def unit(vector):
    vector = np.asarray(vector, dtype=float)
    return vector / max(float(np.linalg.norm(vector)), 1e-9)


def cuboid(item):
    """Face center and outward normal anchor a cuboid extending behind the face."""
    n = unit(item['normal_world'])
    x = np.asarray(item['horizontal_world'], dtype=float)
    x = unit(x - n * np.dot(n, x))
    y = unit(np.cross(n, x))
    w, h, d = item['size_world']
    face = np.asarray(item['center_map'], dtype=float)
    front = np.array([face + sx*x*w/2 + sy*y*h/2 for sx, sy in [(-1,-1),(1,-1),(1,1),(-1,1)]])
    return np.vstack([front, front - n*d])


def project_cuboid(item, world_from_camera, intr, shape, edge_margin=5, surface_only=False):
    """Rasterize cuboid triangles with perspective-correct surface depth."""
    h, w = shape[:2]
    vertices = cuboid(item)[:4] if surface_only else cuboid(item)
    camera_from_world = np.linalg.inv(world_from_camera)
    vertices = vertices @ camera_from_world[:3,:3].T + camera_from_world[:3,3]
    zmap = np.full((h,w), np.inf, dtype=np.float32)
    # Conservative handling of the camera near plane: do not reason about an
    # object whose cuboid intersects it using a truncated projection.
    if not np.isfinite(vertices).all() or np.min(vertices[:,2]) <= .10:
        return {'mask': np.zeros((h,w), bool), 'depth': zmap, 'expected_visible': False, 'reason': 'near_plane'}
    uv = vertices[:,:2] / vertices[:,2,None]
    uv = uv * [intr['fx'], intr['fy']] + [intr['ppx'], intr['ppy']]
    faces = [(0,1,2,3)] if surface_only else [(0,1,2,3),(4,7,6,5),(0,4,5,1),(1,5,6,2),(2,6,7,3),(3,7,4,0)]
    for a,b,c,d in faces:
        for indices in [(a,b,c),(a,c,d)]:
            triangle = uv[list(indices)]
            left, top = np.maximum(np.floor(triangle.min(0)), [0,0]).astype(int)
            right, bottom = np.minimum(np.ceil(triangle.max(0)), [w-1,h-1]).astype(int)
            if right < left or bottom < top:
                continue
            (ax,ay),(bx,by),(cx,cy) = triangle
            denominator = (by-cy)*(ax-cx)+(cx-bx)*(ay-cy)
            if abs(denominator) < 1e-8:
                continue
            yy, xx = np.mgrid[top:bottom+1, left:right+1]
            wa = ((by-cy)*(xx-cx)+(cx-bx)*(yy-cy))/denominator
            wb = ((cy-ay)*(xx-cx)+(ax-cx)*(yy-cy))/denominator
            wc = 1-wa-wb
            inside = (wa >= 0) & (wb >= 0) & (wc >= 0)
            inverse_z = wa/vertices[indices[0],2]+wb/vertices[indices[1],2]+wc/vertices[indices[2],2]
            values = np.where(inside, 1/np.maximum(inverse_z,1e-9), np.inf)
            roi = zmap[top:bottom+1,left:right+1]
            np.minimum(roi, values, out=roi)
    mask = np.isfinite(zmap)
    ys,xs = np.where(mask)
    bbox = [int(xs.min()),int(ys.min()),int(xs.max()+1),int(ys.max()+1)] if len(xs) else [0,0,0,0]
    border = bool(np.any(uv[:,0]<edge_margin) or np.any(uv[:,0]>=w-edge_margin) or np.any(uv[:,1]<edge_margin) or np.any(uv[:,1]>=h-edge_margin))
    silhouette_area = cv2.contourArea(cv2.convexHull(uv.astype(np.float32)))
    reason = 'out_of_fov' if not mask.any() else 'partial_fov' if border else 'projected'
    return {'mask': mask, 'depth': zmap, 'bbox': bbox, 'expected_visible': bool(mask.any()) and not border,
            'inside_fov': bool(mask.any()), 'reason': reason,
            'inside_fraction': min(1., float(mask.sum()) / max(1., silhouette_area)),
            'visible_pixel_count': int(mask.sum())}


def measured_surface(obs):
    """Only a measured plane with measured extent may justify free space."""
    quality = obs.get('measurement_quality', {})
    return dict(source='MEASURED_FRONT_FACE',
                trusted=bool(obs.get('geometry_quality_ok', True) and obs.get('orientation_quality_ok', True) and
                             not quality.get('size_prior_fallback', False)),
                quality=deepcopy(quality),
                **{k: list(obs[k]) for k in ('center_map', 'normal_world', 'horizontal_world', 'size_world')})


def project_measured_surfaces(item, pose, intr, shape, config):
    depth = np.full(shape, np.inf, np.float32)
    reasons = [];has_trusted_surface=False
    basis='MEASURED_FRONT_FACE'
    for surface in item.get('measured_surfaces', []):
        if (not surface.get('trusted') or
                surface.get('source') not in ('MEASURED_FRONT_FACE','MEASURED_OTHER_FACE')):
            continue
        has_trusted_surface=True
        to_camera = pose[:3, 3] - np.asarray(surface['center_map'])
        if np.dot(unit(to_camera), unit(surface['normal_world'])) <= .15:
            reasons.append('unmeasured_view')
            continue
        pred = project_cuboid(surface, pose, intr, shape, config['edge_margin_px'], surface_only=True)
        if not pred['expected_visible']:
            reasons.append(pred['reason'])
            continue
        np.minimum(depth, pred['depth'], out=depth)
    # A position-confirmed record must not become immortal merely because
    # its orientation failed the stricter grasp gate or its extent used a
    # size prior. Fall back to its ESTIMATED face for existence prediction,
    # retaining all normal ray, coverage, view and temporal checks. This is
    # not a measured extent and never upgrades its pose/geometry quality.
    position_supported=(item.get('observations',0)>0 and
        (item.get('geometry_quality',{}).get('position_usable',item.get('position_quality_ok',False))
         or any(s.get('quality',{}).get('position_usable',False) for s in item.get('measured_surfaces',[]))))
    if not has_trusted_surface and position_supported:
        basis='POSITION_CONFIRMED_ESTIMATED_FACE'
        to_camera=pose[:3,3]-np.asarray(item['center_map'])
        if np.dot(unit(to_camera),unit(item['normal_world']))>.15:
            pred=project_cuboid(item,pose,intr,shape,config['edge_margin_px'],surface_only=True)
            if pred['expected_visible']:np.minimum(depth,pred['depth'],out=depth)
            else:reasons.append(pred['reason'])
        else:reasons.append('unmeasured_view')
    mask = np.isfinite(depth)
    enough = int(mask.sum()) >= config['min_projected_pixels']
    reason = 'projected' if enough else ('small_projection' if mask.any() else
             next((r for r in reasons if r == 'partial_fov'), reasons[0] if reasons else 'untrusted_surface'))
    return dict(mask=mask, depth=depth, expected_visible=enough, reason=reason,
                visible_pixel_count=int(mask.sum()),geometry_basis=basis)


def mask_relation(a,b):
    intersection = int(np.count_nonzero(a & b))
    na,nb = int(np.count_nonzero(a)), int(np.count_nonzero(b))
    return intersection/max(1,na+nb-intersection), intersection/max(1,min(na,nb))


def depth_support(mask, depth, cell_px=1):
    """Measure spatial support without inventing depth at unsampled RGB pixels.

    Cell size 1 is the usual dense depth valid-pixel ratio. Registered sparse
    LiDAR uses sampling cells; evidence still uses only real measured rays.
    Count and plane checks remain independent of this coverage measure.
    """
    ys,xs=np.where(mask)
    valid=mask & np.isfinite(depth) & (depth>0)
    vy,vx=np.where(valid)
    cell_px=max(1,int(cell_px));columns=(mask.shape[1]+cell_px-1)//cell_px
    expected=np.unique((ys//cell_px)*columns+xs//cell_px)
    measured=np.unique((vy//cell_px)*columns+vx//cell_px)
    return dict(valid_pixels=len(vx),mask_pixels=len(xs),raw_pixel_ratio=len(vx)/max(1,len(xs)),
                valid_cells=len(measured),mask_cells=len(expected),cell_px=cell_px,
                spatial_coverage=len(measured)/max(1,len(expected)))


class PersistentCartonMap:
    def __init__(self, config=None):
        self.config = dict(DEFAULTS, **{key:value for key,value in (config or {}).items() if key in DEFAULTS})
        self.objects = {}
        self.next_id = 0
        self.hypotheses = {}
        self.events = []
        self.predictions = {}
        self.frame = 0
        self.last_pose = None
        self.last_stamp = None
        self.pose_anomaly_latched = False
        self.association_diagnostics = []
        self.session_id = uuid.uuid4().hex
        self.map_id = 'unspecified'
        self.frame_id = 'map'
        self.revision = 0
        self.epoch_id = 0
        self.replacements = {}
        self.reacquisitions = {}
        self.returns = {}
        self.delta = {}
        self.health = dict(valid=False, reason='NOT_OBSERVED')

    def add_existence(self, item, positive=0., negative=0.):
        # Keep lifetime hit counters, but bound the evidence memory so a box
        # observed for hours can still be retired after sustained clear rays.
        total = item['exist_positive'] + item['exist_negative']
        scale = min(1., self.config['max_existence_evidence'] / max(total, 1e-9))
        item['exist_positive'] = scale * item['exist_positive'] + positive
        item['exist_negative'] = scale * item['exist_negative'] + negative

    def event(self, name, gid=None, **detail):
        record = {'event': name, 'global_id': gid, 'stamp': self.stamp, 'frame': self.frame, 'detail': detail}
        self.events.append(record)
        if gid in self.objects:
            self.objects[gid]['events'] = (self.objects[gid].get('events',[]) + [record])[-100:]

    def probability(self,item):
        c=self.config
        return (c['beta_alpha']+item['exist_positive'])/(c['beta_alpha']+c['beta_beta']+item['exist_positive']+item['exist_negative'])

    def validity(self,pose,stamp,sync_ok):
        if not sync_ok or pose is None or not np.isfinite(pose).all():
            return False, 'TIME_SYNC_OR_POSE_INVALID'
        if self.last_pose is not None:
            dt=stamp-self.last_stamp
            if dt <= 0:
                return False,'NON_MONOTONIC_FRAME'
            delta=np.linalg.inv(self.last_pose)@pose
            translation=float(np.linalg.norm(delta[:3,3]))
            rotation=math.degrees(math.acos(np.clip((np.trace(delta[:3,:3])-1)/2,-1,1)))
            c=self.config
            if translation>c['pose_translation_slack_m']+c['max_translation_speed_m_s']*dt or rotation>c['pose_rotation_slack_deg']+c['max_rotation_speed_deg_s']*dt:
                self.pose_anomaly_latched=True
                self.event('POSE_ANOMALY',translation_m=translation,rotation_deg=rotation,dt_s=dt)
        if self.pose_anomaly_latched:
            return False,'POSE_ANOMALY'
        self.last_pose=pose.copy();self.last_stamp=stamp
        return True,'VALID'

    def visibility(self,pred,depth):
        c=self.config
        pred = pred.get('measured_prediction', pred)
        provenance=dict(geometry_basis=pred.get('geometry_basis','MEASURED_FRONT_FACE'))
        if not pred['expected_visible']:
            return 'UNKNOWN',{**provenance,'reason':pred['reason'], 'visibility': {
                'out_of_fov':'OUT_OF_FOV', 'partial_fov':'PARTIAL_FOV',
                'image_edge':'PARTIAL_FOV'}.get(pred['reason'], 'UNKNOWN')}
        kernel=np.ones((c['mask_erode_px'],)*2,np.uint8)
        mask=cv2.erode(pred['mask'].astype('uint8'),kernel).astype(bool)
        valid=mask & np.isfinite(depth) & (depth>0)
        support=depth_support(mask,depth,c['depth_support_cell_px'])
        if support['valid_pixels']<c['min_valid_pixels'] or support['spatial_coverage']<c['min_valid_ratio']:
            return 'UNKNOWN',{'reason':'insufficient_depth',**provenance,**support}
        residual=depth[valid]-pred['depth'][valid]
        tau=c['depth_consistency_m']
        ratios={'same':float(np.mean(abs(residual)<=tau)),'occluded':float(np.mean(residual < -tau)),
                'clear':float(np.mean(residual>tau)),**support,**provenance}
        for evidence,key,threshold in [('GEOMETRY_PRESENT','same','same_ratio_threshold'),('OCCLUDED','occluded','occlusion_ratio_threshold'),('CLEAR','clear','clear_ratio_threshold')]:
            if ratios[key]>=c[threshold]:return evidence,ratios
        return 'UNKNOWN',ratios

    def apply_evidence(self,item,evidence,detail):
        c=self.config;gid=item['global_id']
        item['last_evidence']=evidence;item['last_evidence_detail']=detail
        if evidence=='UNKNOWN':
            item['visibility_state']=detail.get('visibility','UNKNOWN')
            # Unknown pauses probability, but cannot bridge old clearance
            # evidence across an arbitrarily long unobserved interval.
            item['consecutive_clear_frames']=0
            return
        item['consecutive_matches']=0
        item['consecutive_missed_frames']+=1
        if evidence=='GEOMETRY_PRESENT':
            self.add_existence(item, positive=c['geometry_present_weight']);item['consecutive_clear_frames']=0
            item['visibility_state']='VISIBLE'
            if detail.get('reason')!='unique_rgb_depth_correspondence':
                self.event('DETECTOR_FALSE_NEGATIVE',gid,**detail)
            if item['state']=='MISSING_CANDIDATE' and item['ever_confirmed']:item['state']='CONFIRMED'
        elif evidence=='OCCLUDED':
            item['visibility_state']='OCCLUDED';item['consecutive_clear_frames']=0
            self.event('OBJECT_OCCLUDED',gid,**detail)
        elif evidence=='CLEAR':
            item['visibility_state']='CLEAR';self.add_existence(item, negative=c['clear_negative_weight'])
            item['consecutive_clear_frames']+=1;self.event('OBJECT_CLEAR_EVIDENCE',gid,**detail)
        item['existence_probability']=self.probability(item)

    def lifecycle(self):
        c=self.config
        for gid,item in self.objects.items():
            if item['state']=='VACATED':continue
            if item['last_evidence']=='CLEAR':
                if item['consecutive_clear_frames']>=c['missing_candidate_after_clear_frames'] and item['state']!='MISSING_CANDIDATE':
                    item['state']='MISSING_CANDIDATE';self.event('OBJECT_MISSING_CANDIDATE',gid)
                if item['consecutive_clear_frames']>=c['vacated_after_clear_frames'] and item['existence_probability']<c['max_existence_probability']:
                    item['state']='VACATED';item['vacated_at']=self.stamp
                    self.event('OBJECT_VACATED',gid,reason='repeated_clear',false_positive=not item['ever_confirmed'])
            elif item['last_evidence'] in ('MATCHED','DETECTOR_SPLIT'):
                if ((item['observations']>=c['min_observations'] and item['existence_probability']>=c['min_existence_probability'])
                        or item.get('epoch_confirmed',False)):
                    if item['state']!='CONFIRMED':self.event('OBJECT_CONFIRMED',gid)
                    item['state']='CONFIRMED';item['ever_confirmed']=True

    def matched(self,gid,obs,geometry=True,weight=None):
        c=self.config;item=self.objects[gid]
        if geometry:
            self.replacements.pop(gid, None)
            self.reacquisitions.pop(gid, None)
            residual=np.asarray(obs['center_map'])-np.asarray(item['center_map'])
            if item['observations']:
                fields = ['center_map']
                if obs.get('orientation_quality_ok', True):
                    fields += ['size_world','normal_world','horizontal_world']
                for key in fields:
                    # History determines identity, never corrects a current
                    # measurement towards a previously stored pose.
                    item[key]=list(obs[key])
                normal=unit(item['normal_world'])
                horizontal=np.asarray(item['horizontal_world'])
                item['normal_world']=normal.tolist()
                item['horizontal_world']=unit(horizontal-normal*np.dot(normal,horizontal)).tolist()
            covariance=np.asarray(item['covariance']);covariance[:3,:3]=.8*covariance[:3,:3]+.2*np.outer(residual,residual)
            item['covariance']=covariance.tolist();item['last_geometry_update']=self.stamp
            surface = measured_surface(obs)
            if surface['trusted']:
                item['measured_surfaces'] = [surface]
            item['geometry_quality'] = deepcopy(obs.get('measurement_quality', {}))
            item['position_quality_ok'] = bool(obs.get('position_quality_ok',obs.get('geometry_quality_ok',True)))
            item['orientation_observed_ok'] = bool(obs.get('orientation_quality_ok',True))
            if item['orientation_observed_ok']:
                if not item.get('orientation_trusted',True):
                    item['normal_world']=list(obs['normal_world'])
                    horizontal=np.asarray(obs['horizontal_world']);normal=unit(item['normal_world'])
                    item['normal_world']=normal.tolist()
                    item['horizontal_world']=unit(horizontal-normal*np.dot(normal,horizontal)).tolist()
                    item['geometry_anchor']['normal_world']=list(item['normal_world'])
                item['orientation_trusted'] = True
                item['last_orientation_update'] = self.stamp
            # A held orientation may be useful for display/association, but
            # moving its centre alone must not move a trusted measured face.
            if not item['orientation_observed_ok'] and item['observations']:
                item['geometry_quality']['orientation_action'] = 'HELD' if item.get('orientation_trusted') else 'UNVERIFIED'
        item['observations']+=1;item['consecutive_matches']+=1
        self.add_existence(item, positive=c['matched_positive_weight'] if weight is None else weight)
        item['existence_probability']=self.probability(item)
        item['last_seen']=self.stamp;item['last_seen_frame']=self.frame
        item['consecutive_clear_frames']=0;item['consecutive_missed_frames']=0
        item['visibility_state']='VISIBLE';item['last_evidence']='MATCHED' if geometry else 'DETECTOR_SPLIT'
        item['last_evidence_detail']=dict(reason='current_observation',local_detection_id=obs.get('local_detection_id'))
        item['confidence']=obs['confidence'];item['bbox']=obs['bbox'];item['depth_camera_m']=obs['depth_camera_m']
        item['epoch_confirmed'] = bool(obs.get('_epoch_confirmed', False))
        item['last_epoch_support'] = int(obs.get('_epoch_support', 0))
        item['cuboid_corners_world']=cuboid(item).tolist()
        obs['global_id']=gid;obs['association_status']=item['last_evidence']
        if geometry and not obs.get('orientation_quality_ok',True):
            obs['association_status']='MATCHED_POSITION_ONLY'
        if geometry:self.event('OBJECT_MATCHED',gid)

    def create(self,obs):
        gid=self.next_id;self.next_id+=1
        self.objects[gid]={'global_id':gid,'state':'TENTATIVE','ever_confirmed':False,'created_at':self.stamp,
            'center_map':list(obs['center_map']),'normal_world':list(obs['normal_world']),
            'horizontal_world':list(obs['horizontal_world']),'size_world':list(obs['size_world']),
            'covariance':(np.eye(6)*.001).tolist(),'observations':0,'consecutive_matches':0,
            'consecutive_clear_frames':0,'consecutive_missed_frames':0,'exist_positive':0.,'exist_negative':0.,
            'existence_probability':.5,'events':[]}
        self.objects[gid]['geometry_anchor'] = {key:list(obs[key]) for key in ('center_map','normal_world')}
        self.objects[gid]['orientation_trusted'] = bool(obs.get('orientation_quality_ok',True))
        self.objects[gid]['measured_surfaces'] = [measured_surface(obs)]
        self.event('OBJECT_CREATED_TENTATIVE',gid);self.matched(gid,obs)

    def relocation(self):
        c=self.config
        for old in self.objects.values():
            if not old['ever_confirmed'] or old['state'] not in ('VACATED','MISSING_CANDIDATE'):continue
            for new in self.objects.values():
                if new['global_id']==old['global_id'] or new['state']=='VACATED' or new.get('last_seen_frame')!=self.frame:continue
                gap=new['created_at']-old['last_seen']
                if not 0<=gap<=c['relocation_max_time_gap_sec']:continue
                distance=float(np.linalg.norm(np.asarray(old['center_map'])-new['center_map']))
                if distance>c['relocation_max_distance_m']:continue
                scores={'size':math.exp(-float(np.linalg.norm(np.asarray(old['size_world'])-new['size_world']))/.15),
                        'time':math.exp(-gap/c['relocation_max_time_gap_sec']),
                        'spatial':math.exp(-distance/c['relocation_max_distance_m']),
                        'motion':1.}  # No unjustified "falling only" assumption.
                weights={key:c['relocation_weight_'+key] for key in scores}
                score=sum(scores[k]*weights[k] for k in scores)/sum(weights.values())
                if score<c['hypothesis_threshold']:continue
                key=(old['global_id'],new['global_id']);previous=self.hypotheses.get(key,{})
                support=previous.get('supporting_frames',0)+(previous.get('last_frame')!=self.frame)
                hypothesis={'old_object_id':key[0],'new_candidate_id':key[1],'probability':score,
                            'supporting_frames':support,'last_frame':self.frame,'created_at':previous.get('created_at',self.stamp),
                            'last_updated':self.stamp,'scores':scores,'likely':score>=c['likely_threshold'] and support>=c['min_support_frames']}
                self.hypotheses[key]=hypothesis
                if support>=c['min_support_frames']:self.event('POSSIBLE_RELOCATION',key[1],**hypothesis)
        self.hypotheses={k:v for k,v in self.hypotheses.items() if self.stamp-v['last_updated']<=c['relocation_max_time_gap_sec']}

    def _trial(self):
        trial = copy(self)
        for key in ('objects', 'hypotheses', 'replacements', 'reacquisitions', 'returns', 'health'):
            setattr(trial, key, deepcopy(getattr(self, key)))
        trial.config = dict(self.config)
        trial.events = []; trial.predictions = {}; trial.association_diagnostics = []
        return trial

    def _validate_transaction(self):
        if self.objects and self.next_id <= max(self.objects):
            raise ValueError('Non-monotonic object allocator')
        for gid, obj in self.objects.items():
            if obj['global_id'] != gid:
                raise ValueError('Object identity mismatch')
            for key in ('center_map', 'normal_world', 'horizontal_world', 'size_world', 'covariance'):
                if not np.isfinite(obj[key]).all():
                    raise ValueError('Non-finite committed geometry')
            if min(obj['size_world']) <= 0 or not 0 <= obj['existence_probability'] <= 1:
                raise ValueError('Invalid object state')

    def _commit(self, trial):
        trial._validate_transaction()
        before = {gid for gid, o in self.objects.items() if o['ever_confirmed'] and o['state'] != 'VACATED'}
        after = {gid for gid, o in trial.objects.items() if o['ever_confirmed'] and o['state'] != 'VACATED'}
        updated = {e['global_id'] for e in trial.events if e['event'] == 'OBJECT_MATCHED'} & before & after
        trial.revision = self.revision + 1
        trial.health = dict(valid=True, reason='VALID', sensor_stamp=trial.stamp)
        trial.delta = dict(session_id=self.session_id, map_id=self.map_id, frame_id=self.frame_id,
            base_revision=self.revision, revision=trial.revision, epoch_id=trial.epoch_id,
            stamp=trial.stamp, added=sorted(after-before), removed=sorted(before-after),
            updated=sorted(updated), kept=sorted((before & after)-updated),
            conflicts=deepcopy([e for e in trial.events if e['event'] in
                ('IDENTITY_PENDING','REPLACEMENT_PENDING','LAYER_REVEALED')]))
        self.__dict__.update(trial.__dict__)

    def process(self, observations, depth, pose, intr, stamp, sync_ok=True):
        trial = self._trial()
        # Masks and depth are read-only; copy only the observation dictionaries.
        staged = [dict(o) for o in observations]
        try:
            result = trial._process(staged, depth, pose, intr, stamp, sync_ok)
            if result['commit']:
                self._commit(trial)
            else:
                self.pose_anomaly_latched = trial.pose_anomaly_latched
                self.reacquisitions = {}
                self.returns = {}
                self.events = trial.events
                self.predictions = {}
                self.health = dict(valid=False, reason=result['reason'], sensor_stamp=stamp)
            for original, updated in zip(observations, staged):
                original.clear(); original.update(updated)
            return dict(result, revision=self.revision)
        except Exception:
            self.health = dict(valid=False, reason='TRANSACTION_ABORTED', sensor_stamp=stamp)
            raise

    def _support_replacement(self,gid,obs,detail):
        c=self.config;previous=self.replacements.get(gid,{})
        consistent=(self.stamp-previous.get('stamp',-1e9)<=c['replacement_max_gap_s']
            and np.linalg.norm(np.asarray(previous.get('center',[1e9]*3))-obs['center_map'])<=c['replacement_position_spread_m']
            and np.dot(unit(previous.get('normal',obs['normal_world'])),unit(obs['normal_world']))
                >=math.cos(math.radians(c['geometry_anchor_angle_deg'])))
        count=previous.get('count',0)+1 if consistent else 1
        self.replacements[gid]=dict(count=count,stamp=self.stamp,center=list(obs['center_map']),normal=list(obs['normal_world']))
        return count>=c['replacement_support_frames'],dict(detail,support_frames=count)

    def _replacement_evidence(self, gid, obs, prediction, depth, pose, intr):
        """Associate a new layer using only the old/new object surfaces.

        Old-object existence is evaluated separately. Neither surrounding
        background nor other cartons may veto clearance or candidate birth.
        """
        c = self.config
        pred = prediction['measured_prediction']
        evidence, clear_detail = self.visibility(prediction, depth)
        if evidence != 'CLEAR':
            return False, dict(reason='old_measured_surface_not_clear')
        mask = cv2.erode(pred['mask'].astype('uint8'), np.ones((5,5),np.uint8)).astype(bool)
        shared = mask & obs['_mask'] & np.isfinite(depth)
        if shared.sum() < c['min_valid_pixels']:
            return False, dict(reason='insufficient_layer_rays')
        layer = float(np.median(depth[shared] - pred['depth'][shared]))
        # An unchanged RGB outline with a sudden large range change is
        # contradictory evidence about this SAME face, not a newly exposed
        # face. This uses the object silhouette only, never its surroundings.
        outline_iou=mask_relation(pred['mask'],obs['_mask'])[0]
        if layer>=c['replacement_min_layer_m'] and outline_iou>=.90:
            return False,dict(reason='same_outline_depth_conflict',layer_m=layer,outline_iou=outline_iou)
        if (obs.get('conflict_only') or not obs.get('geometry_quality_ok',True)
                or not obs.get('orientation_quality_ok',True)):
            return False, dict(reason='untrusted_new_surface')
        if layer < c['replacement_min_layer_m']:
            return False, dict(reason='layer_separation_uncertain', layer_m=layer)
        # Check the actual new face on shared object rays only.
        yy, xx = np.where(shared)
        rays = np.column_stack(((xx-intr['ppx'])/intr['fx'], (yy-intr['ppy'])/intr['fy'], np.ones(len(xx))))
        normal = pose[:3,:3].T @ unit(obs['normal_world'])
        center = pose[:3,:3].T @ (np.asarray(obs['center_map'])-pose[:3,3])
        denominator = rays @ normal
        z = np.full(len(xx), np.nan)
        usable = abs(denominator) > 1e-6
        z[usable] = np.dot(normal, center) / denominator[usable]
        plane = np.full(depth.shape, np.nan, np.float32); plane[yy,xx] = z
        fit = shared & np.isfinite(plane)
        if fit.sum() < c['min_valid_pixels'] or np.median(abs(depth[fit]-plane[fit])) > c['depth_consistency_m']:
            return False, dict(reason='new_plane_ray_mismatch')
        return self._support_replacement(gid,obs,dict(reason='old_surface_clear',
            layer_m=layer,old_surface_clear_ratio=clear_detail['clear'],
            old_surface_valid_pixels=clear_detail['valid_pixels'],new_plane_pixels=int(fit.sum())))

    def reacquire(self,gid,j,obs):
        """Recover a unique old identity after bounded measurement drift.

        This is an association-only three-frame alternative to the single-
        frame depth gate. It never changes measurement quality or permits a
        new depth layer beyond the replacement separation threshold.
        """
        c=self.config
        eligible=[d['global_id'] for d in self.association_diagnostics
            if d['observation']==j and d['rejection_reasons']==['depth_residual']
            and d['mask_iou']>=c['reacquire_min_iou']
            and d['shared_depth_points']>=c['min_valid_pixels']
            and d['depth_residual_m']<c['replacement_min_layer_m']]
        if (eligible!=[gid] or not measured_surface(obs)['trusted'] or
                self.objects[gid]['state']!='CONFIRMED'):
            return False
        # A second detection competing for this historical silhouette makes
        # reacquisition ambiguous, regardless of the first one's quality.
        if any(d['global_id']==gid and d['observation']!=j and
               d['mask_iou']>=c['geometry_conflict_iou'] for d in self.association_diagnostics):
            return False
        old=self.objects[gid]
        if np.max(abs(np.asarray(old['size_world'][:2])-obs['size_world'][:2]) /
                  np.asarray(old['size_world'][:2]))>.20:return False
        previous=self.reacquisitions.get(gid)
        stable=previous is not None and self.stamp-previous['stamp']<=c['replacement_max_gap_s']
        if stable:
            stable=(np.linalg.norm(np.asarray(previous['center'])-obs['center_map'])<=c['replacement_position_spread_m']
                and np.dot(unit(previous['normal']),unit(obs['normal_world']))>=math.cos(math.radians(c['geometry_anchor_angle_deg'])))
        count=previous['count']+1 if stable else 1
        self.reacquisitions[gid]=dict(center=list(obs['center_map']),normal=list(obs['normal_world']),
                                      count=count,stamp=self.stamp,frame=self.frame)
        obs['reacquisition_support']=count
        self.event('IDENTITY_REACQUISITION',gid,support_frames=count,required=c['reacquire_support_frames'])
        return count>=c['reacquire_support_frames']

    def returning_locations(self,observations,pose,intr,depth):
        """Reopen a vacated location after three unambiguous measurements.

        Geometric return is not proof of physical serial identity. Preserve
        that distinction explicitly; historical duplicate/relocation aliases
        cannot become independent live boxes through this path.
        """
        candidates={};c=self.config
        locations=[(gid,item) for gid,item in self.objects.items()
            if item['state']=='VACATED' and item.get('ever_confirmed')
            and item.get('invalidated_duplicate_of') is None and 'relocated_to' not in item]
        for j,obs in enumerate(observations):
            if obs.get('conflict_only') or not obs.get('geometry_quality_ok',True):continue
            # An occupied location already has an active identity.
            if any(o['state']!='VACATED' and np.linalg.norm(np.asarray(o['center_map'])-obs['center_map'])<=c['max_center_distance_m']
                   for o in self.objects.values()):continue
            for gid,item in locations:
                if np.linalg.norm(np.asarray(item['center_map'])-obs['center_map'])>c['max_center_distance_m']:continue
                if (not obs.get('measurement_quality',{}).get('size_prior_fallback') and
                        np.max(abs(np.asarray(item['size_world'][:2])-obs['size_world'][:2]) / np.asarray(item['size_world'][:2]))>.20):continue
                pred=project_cuboid(item,pose,intr,depth.shape,c['edge_margin_px'],surface_only=True)
                shared=pred['mask']&obs['_mask']&np.isfinite(depth)
                if (not pred['expected_visible'] or mask_relation(pred['mask'],obs['_mask'])[0]<c['reacquire_min_iou']
                        or shared.sum()<c['min_valid_pixels']
                        or np.median(abs(depth[shared]-pred['depth'][shared]))>=c['replacement_min_layer_m']):continue
                if gid not in candidates.setdefault(j,[]):candidates[j].append(gid)
        counts={gid:sum(gid in values for values in candidates.values()) for values in candidates.values() for gid in values}
        updated={}
        for j,ids in candidates.items():
            if len(ids)!=1 or counts[ids[0]]!=1:continue
            gid=ids[0];obs=observations[j];previous=self.returns.get(gid)
            stable=(previous is not None and self.stamp-previous['stamp']<=c['replacement_max_gap_s']
                and np.linalg.norm(np.asarray(previous['center'])-obs['center_map'])<=c['replacement_position_spread_m'])
            count=(previous['count']+1 if stable else 1) if obs.get('orientation_quality_ok',True) else 0
            if count<3:
                updated[gid]=dict(center=list(obs['center_map']),count=count,stamp=self.stamp)
                obs.update(_return_pending=True,association_status='RETURN_LOCATION_PENDING',
                           candidate_global_ids=[gid],return_support=count)
                continue
            item=self.objects[gid]
            item.update(state='CONFIRMED',exist_positive=2.,exist_negative=0.,consecutive_clear_frames=0,
                returned_at=self.stamp,identity_basis='KNOWN_LOCATION_RETURN',physical_identity_verified=False)
            # Initialize the restored location from this independent current
            # measurement. Standard one-to-one matching below updates it once.
            for key in ('center_map','normal_world','horizontal_world','size_world'):item[key]=list(obs[key])
            item['measured_surfaces']=[measured_surface(obs)]
            item['cuboid_corners_world']=cuboid(item).tolist()
            item.pop('vacated_at',None)
            self.event('OBJECT_RETURNED_TO_LOCATION',gid,support_frames=count,physical_identity_verified=False)
        self.returns=updated

    def _process(self,observations,depth,pose,intr,stamp,sync_ok=True, visibility_overrides=None):
        self.frame+=1;self.stamp=stamp;self.events=[];self.association_diagnostics=[]
        for obs in observations:
            obs['global_id']=None
            obs.pop('_return_pending',None)
        valid,reason=self.validity(pose,stamp,sync_ok)
        if not valid:
            self.predictions={}
            for obs in observations:obs['association_status']=reason
            return {'commit':False,'reason':reason}
        c=self.config
        usable=[]
        for obs in observations:
            vectors=[np.asarray(obs.get(key,[]),dtype=float) for key in ('center_map','normal_world','horizontal_world','size_world')]
            good=all(v.shape==(3,) and np.isfinite(v).all() for v in vectors)
            good=good and np.all(vectors[3]>0) and np.linalg.norm(np.cross(vectors[1],vectors[2]))>1e-6
            good=good and np.asarray(obs.get('_mask')).shape==depth.shape and np.isfinite(obs['depth_camera_m'])
            if good:usable.append(obs)
            else:obs['association_status']='INVALID_OBSERVATION'
        observations=usable
        self.returning_locations(observations,pose,intr,depth)
        active={gid:o for gid,o in self.objects.items() if o['state']!='VACATED'}
        predictions={gid:project_cuboid(o,pose,intr,depth.shape,c['edge_margin_px']) for gid,o in active.items()}
        for gid, pred in predictions.items():
            pred['measured_prediction'] = project_measured_surfaces(active[gid],pose,intr,depth.shape,c)
        # Object z-buffer is advisory, never a veto on measured free space:
        # an obsolete foreground record must not hide a removed background.
        global_z = np.full(depth.shape, np.inf, np.float32)
        for pred in predictions.values(): np.minimum(global_z, pred['depth'], out=global_z)
        for gid, pred in predictions.items():
            visible = pred['mask'] & (pred['depth'] <= global_z + c['depth_consistency_m'])
            active[gid]['visibility_prediction'] = dict(
                expected_visible=pred['measured_prediction']['expected_visible'],
                reason=pred['measured_prediction']['reason'],
                geometry_basis=pred['measured_prediction']['geometry_basis'],
                inside_fraction=pred.get('inside_fraction', 0.),
                predicted_visible_fraction=float(visible.sum())/max(1,int(pred['mask'].sum())))
        self.predictions=predictions
        gids=list(active);cost=np.full((len(gids),len(observations)),1e6)
        for r,gid in enumerate(gids):
            obj=active[gid];pred=predictions[gid]
            for j,obs in enumerate(observations):
                distance=float(np.linalg.norm(np.asarray(obj['center_map'])-obs['center_map']))
                iou,overlap=mask_relation(pred['mask'],obs['_mask'])
                shared=pred['mask']&obs['_mask']&np.isfinite(depth)
                count=int(shared.sum())
                residual=float(np.median(abs(depth[shared]-pred['depth'][shared]))) if count else None
                rejections=[]
                if obs.get('conflict_only') or not obs.get('geometry_quality_ok',True):rejections.append('observation_quality')
                if not obs.get('_epoch_admissible',True):rejections.append('epoch_insufficient_support')
                if obs.get('_return_pending'):rejections.append('return_confirmation_pending')
                if distance>c['max_center_distance_m']:rejections.append('center_distance')
                if iou<c['min_iou_gate'] and overlap<c['min_overlap_gate']:rejections.append('mask_overlap')
                if count<10:rejections.append('shared_depth_support')
                if residual is not None and residual>c['max_depth_residual_m']:rejections.append('depth_residual')
                self.association_diagnostics.append(dict(global_id=gid, observation=j,
                    center_distance_m=distance, mask_iou=iou, depth_residual_m=residual,
                    shared_depth_points=count, rejection_reasons=rejections))
                if rejections:continue
                size=np.mean(np.minimum(abs(np.asarray(obj['size_world'])-obs['size_world'])/(np.asarray(obj['size_world'])+1e-6),1))
                cost[r,j]=(c['weight_position']*distance/c['max_center_distance_m']+c['weight_mask_iou']*(1-iou)
                           +c['weight_depth']*min(residual/c['max_depth_residual_m'],1)+c['weight_size']*size)
        preliminary=[]
        if cost.size:
            rows,cols=linear_sum_assignment(cost)
            preliminary=[(gids[r],int(j)) for r,j in zip(rows,cols) if cost[r,j]<=c['max_match_cost']]
        claimed=set();protected=set();splits=[]
        for j,obs in enumerate(observations):
            if obs.get('_return_pending'):
                claimed.add(j)
            if not obs.get('_epoch_admissible',True):
                claimed.add(j)
                obs['association_status']='EPOCH_INSUFFICIENT_SUPPORT'
        # Resolve redundant segmentation BEFORE merge protection can consume
        # the IDs of two already well-explained individual detections.
        dedicated = {gid:j for gid,j in preliminary
            if mask_relation(predictions[gid]['mask'],observations[j]['_mask'])[0] >= c['geometry_conflict_iou']}
        for j,obs in enumerate(observations):
            parts = [(gid,k) for gid,k in dedicated.items() if k != j and
                np.count_nonzero(observations[k]['_mask'] & obs['_mask']) /
                    max(1,int(observations[k]['_mask'].sum())) >= .75]
            if len(parts) < 2: continue
            union = np.logical_or.reduce([observations[k]['_mask'] for _,k in parts])
            if mask_relation(union,obs['_mask'])[0] >= c['split_union_iou_threshold']:
                claimed.add(j)
                obs.update(association_status='REDUNDANT_MERGE_OBSERVATION',
                           candidate_global_ids=[gid for gid,_ in parts])
                self.event('REDUNDANT_MERGE_OBSERVATION', observation=j, global_ids=[gid for gid,_ in parts])
        # Splits require true mask-union coverage and consistent world geometry,
        # not the rectangle enclosing two neighbouring boxes.
        for gid,pred in predictions.items():
            fragments=[]
            for j,obs in enumerate(observations):
                if j in claimed:continue
                intersection=np.count_nonzero(pred['mask']&obs['_mask'])
                shared=pred['mask']&obs['_mask']&np.isfinite(depth)
                if shared.sum()<10 or np.median(abs(depth[shared]-pred['depth'][shared]))>c['max_depth_residual_m']:continue
                if intersection/max(1,obs['_mask'].sum())>=.75 and intersection/max(1,pred['mask'].sum())>=.15:
                    fragments.append(j)
            if len(fragments)<2:continue
            union=np.logical_or.reduce([observations[j]['_mask'] for j in fragments])
            depths=[observations[j]['depth_camera_m'] for j in fragments]
            centers=np.mean([observations[j]['center_map'] for j in fragments],axis=0)
            if mask_relation(union,pred['mask'])[0]>=c['split_union_iou_threshold'] and max(depths)-min(depths)<=c['split_depth_delta_m'] and np.linalg.norm(centers-active[gid]['center_map'])<=c['max_center_distance_m']:
                splits.append((gid,fragments));claimed.update(fragments);protected.add(gid)
        merges=[]
        for j,obs in enumerate(observations):
            if j in claimed:continue
            overlap_ids=[]
            for gid,pred in predictions.items():
                if gid in protected:continue
                shared=pred['mask']&obs['_mask']&np.isfinite(depth)
                if np.count_nonzero(pred['mask']&obs['_mask'])/max(1,pred['mask'].sum())>=c['merge_overlap_threshold'] and shared.sum()>=10 and np.median(abs(depth[shared]-pred['depth'][shared]))<=c['max_depth_residual_m']:
                    overlap_ids.append(gid)
            if len(overlap_ids)>=2:
                union=np.logical_or.reduce([predictions[gid]['mask'] for gid in overlap_ids])
                # A merged detector mask must explain DISTINCT image regions.
                # Two overlapping historical estimates of one face are not
                # evidence that the detector has merged two physical boxes.
                distinct=all(np.count_nonzero(predictions[gid]['mask'] & ~np.logical_or.reduce(
                    [predictions[other]['mask'] for other in overlap_ids if other!=gid])) /
                    max(1,int(predictions[gid]['mask'].sum())) >= .20 for gid in overlap_ids)
                if distinct and mask_relation(union,obs['_mask'])[0]>=c['split_union_iou_threshold']:
                    claimed.add(j);protected.update(overlap_ids);merges.append((j,overlap_ids))
        matched=set()
        for gid,fragments in splits:
            union_obs=dict(observations[fragments[0]])
            union_obs['_mask']=np.logical_or.reduce([observations[j]['_mask'] for j in fragments])
            self.matched(gid,union_obs,geometry=False,weight=c['geometry_present_weight'])
            for j in fragments:observations[j].update(global_id=gid,association_status='DETECTOR_SPLIT')
            self.event('DETECTOR_SPLIT',gid,observations=fragments);matched.add(gid)
        for j,ids in merges:
            observations[j].update(global_ids=ids,association_status='DETECTOR_MERGE')
            for gid in ids:
                self.apply_evidence(active[gid], 'UNKNOWN', dict(reason='detector_merge'))
                active[gid]['last_evidence'] = 'DETECTOR_MERGE'
            self.event('DETECTOR_MERGE',global_ids=ids,observation=j)
        # Association ambiguity cannot invalidate a current measurement.
        # No fixed historical position/normal anchor gates measurement quality.
        preliminary_pairs = set(preliminary)
        for j, obs in enumerate(observations):
            if j in claimed:
                continue
            # Do not let a stale alternative projection veto an already
            # valid one-to-one assignment to this observation.
            if any(index==j and gid not in protected for gid,index in preliminary):
                continue
            overlaps = [(mask_relation(pred['mask'], obs['_mask'])[0], gid)
                        for gid, pred in predictions.items() if gid not in protected and
                        (mask_relation(pred['mask'],obs['_mask'])[0]>=c['geometry_conflict_iou'] or
                         (np.count_nonzero(pred['mask']&obs['_mask'])/max(1,int(obs['_mask'].sum()))>=.60
                          and mask_relation(pred['mask'],obs['_mask'])[0]>=.30))]
            if not overlaps:
                continue
            iou, gid = max(overlaps)
            obj = active[gid]
            if self.reacquire(gid,j,obs):
                self.matched(gid,obs);matched.add(gid);claimed.add(j)
                obs['association_status']='MATCHED_REACQUIRED'
                continue
            # An unambiguous RGB/depth correspondence can establish presence
            # even when the fitted pose fails its separate quality check.
            # Preserve the last measured geometry; never call it a new pose.
            presence_candidates=[d['global_id'] for d in self.association_diagnostics
                if d['observation']==j and d['rejection_reasons']==['observation_quality']
                and d['shared_depth_points']>=c['min_valid_pixels']
                and d['mask_iou']>=c['geometry_conflict_iou']]
            if (not obs.get('conflict_only') and not obs.get('geometry_quality_ok',True)
                    and presence_candidates==[gid] and obj.get('ever_confirmed')):
                claimed.add(j);protected.add(gid)
                self.apply_evidence(obj,'GEOMETRY_PRESENT',dict(reason='unique_rgb_depth_correspondence',observation=j))
                obj.update(last_evidence='DETECTION_PRESENT',last_seen=self.stamp,last_seen_frame=self.frame,
                    orientation_observed_ok=False,position_quality_ok=False)
                obs.update(global_id=gid,association_status='MATCHED_VISIBILITY_ONLY')
                self.event('OBJECT_OBSERVED_WITHOUT_GEOMETRY',gid,observation=j)
                continue
            distance = float(np.linalg.norm(np.asarray(obj['center_map']) - obs['center_map']))
            angle = math.degrees(math.acos(np.clip(np.dot(unit(obj['normal_world']),
                                                         unit(obs['normal_world'])), -1., 1.)))
            if not obs.get('geometry_quality_ok', True) or (gid, j) not in preliminary_pairs:
                detail = dict(observation=j, local_detection_id=obs.get('local_detection_id',j),
                              mask_iou=iou, record_distance_m=distance,
                              record_angle_deg=angle, geometry_quality_ok=obs.get('geometry_quality_ok', True))
                revealed, reveal_detail = self._replacement_evidence(gid, obs, predictions[gid], depth, pose, intr)
                if revealed:
                    # Keep this observation available for its own existing
                    # rear ID, or create a new candidate. Old box receives
                    # actual CLEAR evidence below; no identity inheritance.
                    obs['revealed_behind'] = gid
                    self.event('LAYER_REVEALED', gid, **reveal_detail)
                    continue
                if reveal_detail['reason'] != 'old_surface_clear':
                    self.replacements.pop(gid, None)
                detail['replacement'] = reveal_detail
                if reveal_detail['reason'] == 'old_surface_clear':
                    self.event('REPLACEMENT_PENDING', gid, **reveal_detail)
                claimed.add(j); protected.add(gid)
                obs.update(association_status='STRICT_REJECTED' if obs.get('conflict_only') else
                           'LOW_GEOMETRY_QUALITY' if not obs.get('geometry_quality_ok',True) else 'IDENTITY_PENDING',
                           candidate_global_ids=[gid], replacement_evidence=reveal_detail)
                # Identity uncertainty belongs to this new observation.
                # It cannot turn valid evidence about the OLD surface into
                # UNKNOWN, or stop retirement until a rear box passes YOLO.
                evidence,existence_detail=self.visibility(predictions[gid],depth)
                if reveal_detail['reason'] in ('same_outline_depth_conflict','new_plane_ray_mismatch'):
                    evidence='UNKNOWN'
                    existence_detail=dict(reason=reveal_detail['reason'],replacement=reveal_detail)
                self.apply_evidence(obj,evidence,dict(existence_detail,
                    reason=existence_detail.get('reason','old_surface_evaluation'),pending_observation=j))
                self.event('IDENTITY_PENDING', gid, **detail)
        for gid,j in preliminary:
            if gid in protected or j in claimed:continue
            self.matched(gid,observations[j]);matched.add(gid);claimed.add(j)
        for gid,item in active.items():
            if gid not in matched and gid not in protected:
                evidence,detail=(visibility_overrides or {}).get(gid, self.visibility(predictions[gid],depth))
                self.apply_evidence(item,evidence,detail)
        created_observations=[]
        for j,obs in enumerate(observations):
            if j not in claimed:
                if obs.get('conflict_only') or not obs.get('geometry_quality_ok',True):
                    obs['association_status']='STRICT_REJECTED' if obs.get('conflict_only') else 'LOW_GEOMETRY_QUALITY'
                else:
                    # A merge/split or another matched detection may already
                    # account for this same measured surface. Do not allocate
                    # a second identity just because a branch claimed its ID.
                    duplicates=[]
                    for gid in matched | protected:
                        item=active[gid]
                        distance=np.linalg.norm(np.asarray(item['center_map'])-obs['center_map'])
                        iou=mask_relation(predictions[gid]['mask'],obs['_mask'])[0]
                        covered=np.count_nonzero(predictions[gid]['mask']&obs['_mask'])/max(1,int(obs['_mask'].sum()))
                        if ((distance<=c['geometry_anchor_distance_m'] and iou>=c['geometry_conflict_iou']) or
                                (covered>=.60 and iou>=.30)):
                            duplicates.append(gid)
                    for prior in created_observations:
                        if (np.linalg.norm(np.asarray(prior['center_map'])-obs['center_map'])<=c['geometry_anchor_distance_m']
                                and mask_relation(prior['_mask'],obs['_mask'])[0]>=.85):
                            duplicates.append(prior['global_id'])
                    if duplicates:
                        status='IDENTITY_PENDING' if any(
                            np.linalg.norm(np.asarray(self.objects[gid]['center_map'])-obs['center_map'])>
                            c['geometry_anchor_distance_m'] for gid in duplicates) else 'REDUNDANT_OBJECT_OBSERVATION'
                        obs.update(association_status=status,candidate_global_ids=duplicates)
                        self.event(status,observation=j,global_ids=duplicates)
                    else:
                        self.create(obs);created_observations.append(obs)
        self.relocation();self.lifecycle()
        self.reacquisitions={gid:value for gid,value in self.reacquisitions.items()
                             if value['frame']==self.frame and self.objects[gid]['state']!='VACATED'}
        self.replacements = {gid:entry for gid,entry in self.replacements.items()
            if self.objects[gid]['state'] != 'VACATED' and self.stamp-entry['stamp'] <= c['replacement_max_gap_s']}
        return {'commit':True,'reason':'VALID'}

    def process_epoch(self, samples):
        """Evaluate independent synchronized samples against one frozen map.

        Only last-frame detections supported by other samples may add/update
        geometry. Earlier reliable presence may KEEP an object absent in the
        last frame. Contradictory presence/clearance freezes the whole batch.
        Lifecycle and revision advance once, never once per sample.
        """
        c = self.config
        if not c['epoch_frames'] <= len(samples) <= 8:
            return dict(commit=False, reason='EPOCH_SAMPLE_COUNT', revision=self.revision)
        stamps = [s['stamp'] for s in samples]
        cloud_stamps = [s.get('cloud_stamp',s['stamp']) for s in samples]
        first_pose = samples[0]['pose']
        if (any(b <= a for a,b in zip(stamps, stamps[1:])) or len(set(cloud_stamps)) != len(samples)
                or stamps[-1]-stamps[0] > c['epoch_timeout_s']):
            return dict(commit=False, reason='EPOCH_TIMING', revision=self.revision)
        trials = []
        for sample in samples:
            pose = sample['pose']
            if pose is None or first_pose is None or not np.isfinite(pose).all():
                return dict(commit=False, reason='EPOCH_POSE_INVALID', revision=self.revision)
            delta = np.linalg.inv(first_pose) @ pose
            angle = math.degrees(math.acos(np.clip((np.trace(delta[:3,:3])-1)/2,-1,1)))
            if np.linalg.norm(delta[:3,3]) > c['epoch_translation_m'] or angle > c['epoch_rotation_deg']:
                return dict(commit=False, reason='EPOCH_CAMERA_MOVED', revision=self.revision)
            trial = self._trial()
            result = trial._process([dict(o) for o in sample['observations']], sample['depth'],
                                    pose, sample['intr'], sample['stamp'], sample.get('sync_ok',True))
            if not result['commit']:
                self.pose_anomaly_latched |= trial.pose_anomaly_latched
                self.health = dict(valid=False, reason=result['reason'], sensor_stamp=sample['stamp'])
                return dict(result, revision=self.revision)
            # Retain scalar/object evidence only. Keeping N full-resolution
            # projection sets would multiply batch memory by N (up to 8).
            trial.predictions = {}
            trial.association_diagnostics = []
            trials.append(trial)
        overrides = {}
        for gid, obj in self.objects.items():
            if obj['state'] == 'VACATED': continue
            evidence = [t.objects[gid]['last_evidence'] for t in trials]
            presence = sum(e in ('MATCHED','DETECTOR_SPLIT','GEOMETRY_PRESENT') for e in evidence)
            clearance = evidence.count('CLEAR')
            if presence and clearance:
                return dict(commit=False, reason='EPOCH_CONTRADICTORY_EVIDENCE', revision=self.revision)
            if presence >= c['epoch_min_support']:
                overrides[gid] = ('GEOMETRY_PRESENT', dict(reason='epoch_presence', support_frames=presence))
            elif clearance / len(samples) >= c['epoch_clear_ratio'] and 'OCCLUDED' not in evidence:
                overrides[gid] = ('CLEAR', dict(reason='epoch_clear', support_frames=clearance))
            elif 'OCCLUDED' in evidence:
                overrides[gid] = ('OCCLUDED', dict(reason='epoch_occlusion'))
            else:
                last = trials[-1].objects[gid]
                overrides[gid] = ('UNKNOWN', dict(reason='epoch_insufficient_support',
                    visibility=last['visibility_state'] if last['visibility_state'] in ('OUT_OF_FOV','PARTIAL_FOV') else 'UNKNOWN'))
        last = samples[-1]
        observations = [dict(o) for o in last['observations']]
        # A sample may support at most one latest observation. This prevents
        # one merged detection from confirming two separate new candidates.
        used = [set() for _ in samples]
        for obs in observations:
            support = []
            for index, sample in enumerate(samples):
                candidates = []
                for j, other in enumerate(sample['observations']):
                    if j in used[index] or other.get('conflict_only') or not other.get('geometry_quality_ok',True): continue
                    distance = np.linalg.norm(np.asarray(other['center_map'])-obs['center_map'])
                    angle = math.degrees(math.acos(np.clip(np.dot(unit(other['normal_world']),unit(obs['normal_world'])),-1,1)))
                    if distance <= c['epoch_position_spread_m'] and angle <= c['geometry_anchor_angle_deg']:
                        candidates.append((distance,j))
                if candidates:
                    _, j = min(candidates); used[index].add(j); support.append(sample['observations'][j])
            obs['_epoch_support'] = len(support)
            obs['_epoch_confirmed'] = len(support) >= c['epoch_min_support']
            if not obs['_epoch_confirmed']:
                obs['_epoch_admissible'] = False
        trial = self._trial(); trial.epoch_id += 1
        result = trial._process(observations,last['depth'],last['pose'],last['intr'],last['stamp'],
                                last.get('sync_ok',True), visibility_overrides=overrides)
        if result['commit']:
            self._commit(trial)
            for original, updated in zip(last['observations'], observations):
                original.clear(); original.update(updated)
        return dict(result, revision=self.revision, epoch_id=self.epoch_id, sample_count=len(samples))

    def active_map(self):
        boxes = []
        for item in self.objects.values():
            if not item['ever_confirmed'] or item['state'] == 'VACATED': continue
            normal=unit(item['normal_world']); horizontal=unit(item['horizontal_world'])
            orientation=Rotation.from_matrix(np.column_stack((horizontal,unit(np.cross(normal,horizontal)),normal))).as_quat().tolist()
            boxes.append(dict(global_id=item['global_id'], object_uid=f'{self.session_id}:{item["global_id"]}',
                lifecycle=item['state'], position_map=list(item['center_map']),
                position_reference='MEASURED_FRONT_FACE_CENTER',
                orientation_xyzw=orientation, orientation_axes='horizontal, vertical, outward_normal',
                normal_map=list(item['normal_world']), horizontal_map=list(item['horizontal_world']),
                size=list(item['size_world']), thickness_source='PRIOR',
                detection_confidence=item['confidence'], existence_probability=item['existence_probability'],
                geometry_quality=deepcopy(item.get('geometry_quality',{})),
                position_quality_ok=item.get('position_quality_ok',True),
                orientation_trusted=item.get('orientation_trusted',True),
                orientation_observed_ok=item.get('orientation_observed_ok',True),
                observation_count=item['observations'], last_seen=item['last_seen'],
                visibility=item['visibility_state'], currently_visible=item['visibility_state']=='VISIBLE',
                existence_geometry_basis=item.get('visibility_prediction',{}).get('geometry_basis'),
                needs_verification=item['state']=='MISSING_CANDIDATE' or item['last_evidence'] in ('UNKNOWN','CLEAR','DETECTOR_MERGE'),
                identity_basis=item.get('identity_basis','CONTINUOUS_ASSOCIATION'),
                physical_identity_verified=item.get('physical_identity_verified',False),
                geometry_usable=bool(self.health['valid'] and item['state']=='CONFIRMED' and
                    item['last_evidence']=='MATCHED' and item.get('orientation_observed_ok',True) and
                    any(s.get('trusted') for s in item.get('measured_surfaces',[])))))
        return dict(schema_version=1, frame_id=self.frame_id, map_id=self.map_id, session_id=self.session_id,
            revision=self.revision, epoch_id=self.epoch_id, stamp=self.last_stamp,
            localization=deepcopy(self.health), boxes=boxes)

    def snapshot(self):
        return {'map':[deepcopy(o) for o in self.objects.values() if o['state']!='VACATED'],
                'map_history':deepcopy(list(self.objects.values())), 'events':deepcopy(self.events),
                'association_diagnostics':deepcopy(self.association_diagnostics),
                'relocation_hypotheses':deepcopy(list(self.hypotheses.values())),
                'active_box_map':self.active_map(), 'map_delta':deepcopy(self.delta)}
