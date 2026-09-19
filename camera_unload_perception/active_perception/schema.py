"""Sensor-independent contracts. All geometry is metres in the task world frame."""
from dataclasses import dataclass, field, asdict
from enum import Enum
import math
import numpy as np


class GeometryStatus(str, Enum):
    MEASURED='MEASURED'
    PRIOR_ESTIMATED='PRIOR_ESTIMATED'
    RETAINED='RETAINED'
    UNAVAILABLE='UNAVAILABLE'

class FaceType(str, Enum):
    FRONT='FRONT'
    TOP='TOP'
    UNKNOWN='UNKNOWN'

class LifeState(str, Enum):
    TENTATIVE='TENTATIVE'
    CONFIRMED='CONFIRMED'
    MISSING_CANDIDATE='MISSING_CANDIDATE'
    VACATED='VACATED'

class VisibilityState(str, Enum):
    VISIBLE='VISIBLE'
    OCCLUDED='OCCLUDED'
    UNKNOWN='UNKNOWN'

class RayLabel(str, Enum):
    SURFACE_SAME='SURFACE_SAME'
    NEARER_RETURN='NEARER_RETURN'
    THROUGH_OLD_SURFACE='THROUGH_OLD_SURFACE'
    UNMODELED_ENDPOINT='UNMODELED_ENDPOINT'
    NO_VALID_RETURN='NO_VALID_RETURN'
    MODEL_OR_POSE_UNRELIABLE='MODEL_OR_POSE_UNRELIABLE'
    ROBOT_OCCLUDER='ROBOT_OCCLUDER'

def jsonable(x):
    if hasattr(x,'__dataclass_fields__'): return jsonable(asdict(x))
    if isinstance(x,Enum): return x.value
    if isinstance(x,np.ndarray): return jsonable(x.tolist())
    if isinstance(x,np.generic): return jsonable(x.item())
    if isinstance(x,dict): return {str(k):jsonable(v) for k,v in x.items() if not str(k).startswith('_')}
    if isinstance(x,(list,tuple)): return [jsonable(v) for v in x]
    if isinstance(x,float) and not math.isfinite(x): return None
    return x

def unit(v):
    a=np.asarray(v,dtype=float); n=np.linalg.norm(a)
    if a.shape!=(3,) or not np.isfinite(a).all() or n<1e-9: raise ValueError('invalid direction')
    return a/n

def transform_ok(t):
    if t is None: return False
    a=np.asarray(t)
    return (a.shape==(4,4) and np.isfinite(a).all() and np.allclose(a[3],[0,0,0,1])
            and np.allclose(a[:3,:3].T@a[:3,:3],np.eye(3),atol=2e-4)
            and abs(np.linalg.det(a[:3,:3])-1)<2e-4)

@dataclass
class GeometryEstimate:
    value: object=None
    status: str='UNAVAILABLE'
    frame_id: str='map'
    measured_at: float=0.
    source_capture_ids: list=field(default_factory=list)
    uncertainty_or_bound: object=None

@dataclass
class MeasuredSurface:
    center: object
    normal: object
    polygon: object
    full_face_observed: bool=False
    trusted: bool=False
    source: str='UNKNOWN'
    capture_id: str=''
    stamp: float=0.
    support_point_ids: list=field(default_factory=list)
    error_bound_m: float=.02

    def validate(self):
        p=np.asarray(self.polygon,dtype=float);c=np.asarray(self.center,dtype=float);n=unit(self.normal)
        if c.shape!=(3,) or not np.isfinite(c).all(): raise ValueError('invalid surface center')
        if p.ndim!=2 or p.shape[1]!=3 or len(p)<3 or not np.isfinite(p).all(): raise ValueError('invalid polygon')
        if np.max(abs((p-c)@n))>.003: raise ValueError('polygon off plane')
        area=np.linalg.norm(np.sum(np.cross(p-c,np.roll(p,-1,axis=0)-c),axis=0))/2
        if area<1e-5: raise ValueError('empty polygon')
        edges=np.roll(p,-1,axis=0)-p
        turns=np.cross(edges,np.roll(edges,-1,axis=0))@n
        if np.any(np.linalg.norm(edges,axis=1)<1e-8) or (np.any(turns>1e-8) and np.any(turns< -1e-8)):
            raise ValueError('surface polygon must be convex and ordered')
        if not math.isfinite(self.error_bound_m) or self.error_bound_m<0: raise ValueError('invalid surface error')

@dataclass
class CapturePacket:
    capture_id: str
    rgb_stamp: float
    cloud_stamp: float
    points_sensor: object
    point_ids: object
    t_world_sensor: object
    t_world_camera: object
    intrinsics: dict
    image: object=None
    confidence: object=None
    offset_time: object=None
    origins_sensor: object=None
    robot_mask: object=None
    task_id: str='odin_validation'
    view_id: str='CURRENT'
    map_epoch: str=''
    calibration_revision: str=''
    trajectory_revision: str=''
    frame_id: str='map'
    stable_capture: bool=False
    pose_quality: bool=False
    sensor_quality: bool=True
    source: str='UNKNOWN'
    # Raw topic selection does not establish the device's return semantics.
    return_semantics: str='UNKNOWN'
    pose_error_bound_m: float=.01

    def validate(self,require_stability=True):
        p=np.asarray(self.points_sensor); ids=np.asarray(self.point_ids)
        if p.ndim!=2 or p.shape[1]!=3 or ids.shape!=(len(p),): raise ValueError('point/ID shape')
        if ids.dtype.kind not in 'iu' or np.any(ids<0):raise ValueError('raw point IDs must be nonnegative integers')
        if len(np.unique(ids))!=len(ids): raise ValueError('duplicate raw point IDs')
        if not self.capture_id or not self.map_epoch or not self.calibration_revision: raise ValueError('missing provenance')
        if not transform_ok(self.t_world_sensor) or not transform_ok(self.t_world_camera): raise ValueError('invalid transform')
        if not self.pose_quality or not self.sensor_quality: raise ValueError('input quality unavailable')
        values=[self.intrinsics.get(k,float('nan')) for k in ('fx','fy','ppx','ppy')]
        if not np.isfinite(values).all() or min(values[:2])<=0 or not np.isfinite(self.intrinsics.get('skew',0)):raise ValueError('invalid intrinsics')
        if not np.isfinite([self.rgb_stamp,self.cloud_stamp]).all() or abs(self.rgb_stamp-self.cloud_stamp)>.020: raise ValueError('unsynchronized capture')
        if require_stability and not self.stable_capture: raise ValueError('scan not fully inside stable window')
        if self.return_semantics not in ('UNKNOWN','FIRST_OPAQUE_RETURN','STRONGEST_RETURN','MULTIPLE_RETURN'):
            raise ValueError('invalid return semantics')
        if self.origins_sensor is not None:
            origins=np.asarray(self.origins_sensor)
            if origins.shape not in ((3,),p.shape) or not np.isfinite(origins).all():raise ValueError('invalid ray origins')
        if not math.isfinite(self.pose_error_bound_m) or self.pose_error_bound_m<0: raise ValueError('invalid pose error')
        for x in (self.confidence,self.offset_time,self.robot_mask):
            if x is not None and np.asarray(x).shape!=(len(p),): raise ValueError('point metadata shape')
        if self.offset_time is not None and not np.isfinite(self.offset_time).all():raise ValueError('invalid point times')

    @property
    def version(self): return (self.task_id,self.map_epoch,self.calibration_revision,self.trajectory_revision,self.frame_id)

    def world_rays(self):
        t=np.asarray(self.t_world_sensor); p=np.asarray(self.points_sensor)
        o=np.zeros_like(p) if self.origins_sensor is None else np.broadcast_to(self.origins_sensor,p.shape)
        return p@t[:3,:3].T+t[:3,3],o@t[:3,:3].T+t[:3,3]

@dataclass
class BoxObservation:
    local_id: int
    capture_id: str
    face: MeasuredSurface
    horizontal: object
    dimensions: object                 # width, depth, height
    bbox: list=field(default_factory=list)
    mask: object=None
    confidence: float=.0
    position_ok: bool=False
    orientation_ok: bool=False
    face_type: str='FRONT'
    dimension_status: tuple=('MEASURED','PRIOR_ESTIMATED','MEASURED')
    proposal_source: str='YOLO'

    def geometry(self,frame_id='map'):
        self.face.validate(); n=unit(self.face.normal); sizes=np.asarray(self.dimensions,dtype=float)
        if sizes.shape!=(3,) or not np.isfinite(sizes).all() or np.any(sizes<=0): raise ValueError('invalid dimensions')
        if self.face_type=='FRONT':
            y=-n;x=unit(np.asarray(self.horizontal)-y*np.dot(self.horizontal,y));z=unit(np.cross(x,y));axis=1
            # P01's visible horizontal edge may point left. Resolve its sign,
            # without changing the measured plane, so body Z is world-up.
            if z[2]<0:x=-x;z=-z
        elif self.face_type=='TOP':
            z=n;x=unit(np.asarray(self.horizontal)-z*np.dot(self.horizontal,z));y=unit(np.cross(z,x));axis=2
        else: raise ValueError('MULTI_FACE_UNSUPPORTED')
        def est(v,status):return GeometryEstimate(jsonable(v),status,frame_id,self.face.stamp,[self.capture_id])
        center=np.asarray(self.face.center)-n*sizes[axis]/2
        body_valid=self.position_ok and self.orientation_ok and self.face.full_face_observed
        return dict(body_center_map=est(center if body_valid else None,
                ('MEASURED' if self.dimension_status[axis]=='MEASURED' else 'PRIOR_ESTIMATED') if body_valid else 'UNAVAILABLE'),
            face_center_map=est(self.face.center if self.position_ok and self.face.full_face_observed else None,
                'MEASURED' if self.position_ok and self.face.full_face_observed else 'UNAVAILABLE'),
            body_orientation_map=est(np.column_stack((x,y,z)) if self.orientation_ok else None,'MEASURED' if self.orientation_ok else 'UNAVAILABLE'),
            axis_symmetry=est('ROTATION_PI_ABOUT_Z' if self.face_type=='TOP' else 'NONE','PRIOR_ESTIMATED'),
            contact_center_map=est(None,'UNAVAILABLE'))

@dataclass
class BoxRecord:
    box_id: int
    face: MeasuredSurface
    geometry: dict
    dimensions: list
    dimension_status: list
    face_type: str='FRONT'
    horizontal: object=None
    life_state: str='TENTATIVE'
    visibility: str='UNKNOWN'
    identity_status: str='RESOLVED'
    positives: float=1.
    negatives: float=0.
    observations: int=1
    clear_run: int=0
    ever_confirmed: bool=False
    last_seen: float=0.
    last_evidence: dict=field(default_factory=dict)
    current_capture_id: str=''
    current_pose_ok: bool=False
    related_old_ids: list=field(default_factory=list)
    review_clear_runs: int=0
    unmeasured_run: int=0
    decisive_clears: int=0

    @property
    def probability(self):return (1+self.positives)/(2+self.positives+self.negatives)
    def to_dict(self):return dict(jsonable(self),existence_probability=self.probability)

@dataclass
class RayComparison:
    points: object
    labels: object
    predictions: dict
    evidence: dict
    elapsed_ms: float
