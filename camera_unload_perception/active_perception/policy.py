"""Target identity and local-frame contracts; no grasp execution or IK."""
import numpy as np
from .schema import transform_ok,jsonable
from .map_manager import geom_value


def advise(manager,robot_ready=True):
    if not robot_ready:return dict(action='WAIT_ROBOT_OBSERVATION_READY',target_ids=[])
    candidates=manager.execution_candidates()
    if candidates:return dict(action='WRIST_VERIFY',target_ids=candidates)
    missing=[i for i,r in manager.records.items() if r.life_state=='MISSING_CANDIDATE']
    if missing:return dict(action='LOCAL_CHECK',target_ids=missing)
    if manager.wall_batch:return dict(action='WALL_SCAN',target_ids=manager.wall_batch['member_ids'])
    return dict(action='OBSERVE',target_ids=[])


def wrist_verify(manager,target_id,observations,t_base_world,capture_id):
    """Only return the requested ID when unique measured geometry matches it."""
    if not transform_ok(t_base_world):return dict(status='INVALID_BASE_TRANSFORM')
    if target_id not in manager.execution_candidates():return dict(status='TARGET_NOT_EXECUTABLE')
    target=manager.records[target_id];matches=[]
    for o in observations:
        if o.capture_id!=capture_id or not o.position_ok or not o.orientation_ok:continue
        cost=manager._cost(target,o)
        if cost is None:continue
        competing=[manager._cost(r,o) for i,r in manager.records.items() if i!=target_id and r.life_state!='VACATED']
        if any(c is not None and c-cost<manager.config['ambiguity_margin'] for c in competing):continue
        matches.append(o)
    if len(matches)!=1:return dict(status='TARGET_IDENTITY_AMBIGUOUS_OR_ABSENT')
    geometry=matches[0].geometry();point=geom_value(geometry,'body_center_map');rotation=geom_value(geometry,'body_orientation_map')
    if point is None or rotation is None:return dict(status='LOCAL_GEOMETRY_UNAVAILABLE')
    t=np.asarray(t_base_world)
    return dict(status='VERIFIED',target_id=target_id,capture_id=capture_id,frame_id='base',
        body_center_base=jsonable(t[:3,:3]@point+t[:3,3]),body_orientation_base=jsonable(t[:3,:3]@rotation),
        contact_center_base=None,contact_status='UNAVAILABLE',requires_contact_planning=True)


def wrist_verify_batch(manager,target_id,samples,t_base_world,base_stamp):
    """Separate 2/2 identity gate; Odin can exercise this as a wrist proxy."""
    if len(samples)!=2:return dict(status='WRIST_REQUIRES_TWO_CAPTURES')
    captures=[p for p,_ in samples]
    if captures[0].capture_id==captures[1].capture_id or captures[0].cloud_stamp==captures[1].cloud_stamp:return dict(status='WRIST_DUPLICATE_CAPTURE')
    if not 0<captures[1].rgb_stamp-captures[0].rgb_stamp<=5:return dict(status='WRIST_TIME_WINDOW_INVALID')
    if not np.isfinite(base_stamp) or abs(base_stamp-captures[-1].rgb_stamp)>.020:return dict(status='BASE_TRANSFORM_STAMP_MISMATCH')
    matches=[]
    for p,observations in samples:
        try:p.validate()
        except ValueError:return dict(status='WRIST_CAPTURE_INVALID')
        if p.version!=tuple(manager.version or ()):return dict(status='WRIST_VERSION_MISMATCH')
        result=wrist_verify(manager,target_id,observations,t_base_world,p.capture_id)
        if result['status']!='VERIFIED':return result
        matches.append(result)
    if np.linalg.norm(np.asarray(matches[0]['body_center_base'])-matches[1]['body_center_base'])>manager.config['support_distance_m']:
        return dict(status='WRIST_GEOMETRY_DISAGREES')
    a=np.asarray(matches[0]['body_orientation_base']);b=np.asarray(matches[1]['body_orientation_base'])
    if np.degrees(np.arccos(np.clip((np.trace(a.T@b)-1)/2,-1,1)))>manager.config['support_angle_deg']:
        return dict(status='WRIST_ORIENTATION_DISAGREES')
    result=matches[-1];result['support_capture_ids']=[p.capture_id for p in captures];return result
