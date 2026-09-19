"""Vectorised native ray/finite-face tests, without RGB-origin substitution."""
import time
import numpy as np
from .schema import unit, RayComparison

LABELS=('NO_VALID_RETURN','UNMODELED_ENDPOINT','SURFACE_SAME','NEARER_RETURN',
        'THROUGH_OLD_SURFACE','ROBOT_OCCLUDER','MODEL_OR_POSE_UNRELIABLE')
DEFAULTS=dict(range_min=.2,range_max=8.,confidence_min=35.,range_error_m=.02,
              max_error_m=.10,grid=8,min_points=80,min_coverage=.40,clear_ratio=.55,
              same_ratio=.55,nearer_ratio=.50,boundary_m=.008,min_incidence=.20)

def intersect_obb(origins,directions,center,rotation,dimensions):
    """Slab broad phase only. Prior cuboid intersections never establish free space."""
    o=(origins-np.asarray(center))@np.asarray(rotation);d=directions@np.asarray(rotation)
    half=np.asarray(dimensions)/2;parallel=abs(d)<1e-10
    outside=np.any(parallel&(abs(o)>half),axis=1)
    a=np.divide(-half-o,d,out=np.full_like(o,-np.inf),where=~parallel)
    b=np.divide(half-o,d,out=np.full_like(o,np.inf),where=~parallel)
    near=np.maximum(np.min(np.stack((a,b)),axis=0).max(1),0)
    far=np.max(np.stack((a,b)),axis=0).min(1)
    return ~outside&(far>=near),near,far

def surface_basis(surface):
    p=np.asarray(surface.polygon); n=unit(surface.normal);u=unit(p[1]-p[0]);v=unit(np.cross(n,u))
    return p,n,u,v,np.column_stack(((p-p[0])@u,(p-p[0])@v))

def inside(uv,polygon,margin=0.):
    area=np.sum(polygon[:,0]*np.roll(polygon[:,1],-1)-polygon[:,1]*np.roll(polygon[:,0],-1))
    sign=1 if area>=0 else -1; valid=np.ones(len(uv),bool)
    for a,b in zip(polygon,np.roll(polygon,-1,axis=0)):
        e=b-a;valid &= sign*(e[0]*(uv[:,1]-a[1])-e[1]*(uv[:,0]-a[0]))>=margin*np.linalg.norm(e)-1e-8
    return valid

def predict_surface_hits(origins,directions,surface,config=None):
    c=dict(DEFAULTS,**(config or {}));surface.validate()
    p,n,u,v,polygon=surface_basis(surface)
    den=directions@n;side=(origins-p[0])@n
    safe=np.abs(den)>1e-8;s=np.divide(-side,den,out=np.full(len(den),np.inf),where=safe)
    # Ineligible rays never participate in arithmetic with infinity.
    hit=origins+np.where(np.isfinite(s),s,0)[:,None]*directions
    uv=np.column_stack(((hit-p[0])@u,(hit-p[0])@v))
    valid=(side>c['boundary_m'])&(den<=-c['min_incidence'])&(s>=c['range_min'])&(s<=c['range_max'])
    valid &= inside(uv,polygon,c['boundary_m'])
    g=int(c['grid']);lo=polygon.min(0);span=np.maximum(polygon.max(0)-lo,1e-8)
    ij=np.clip(np.floor((uv-lo)/span*g),0,g-1).astype(int);cells=ij[:,1]*g+ij[:,0]
    # A cell cut by the polygon edge is testable even when its centre is
    # outside. Sub-cell sampling keeps the reference and observed cell sets
    # in the same grid; the old centre-only denominator could exceed 100%.
    yy,xx=np.mgrid[:g,:g];offsets=np.array([.1,.3,.5,.7,.9]);oy,ox=np.meshgrid(offsets,offsets)
    samples=np.stack(((xx[...,None,None]+ox)/g,(yy[...,None,None]+oy)/g),axis=-1).reshape(-1,2)*span+lo
    testable=inside(samples,polygon,0).reshape(g*g,-1).any(axis=1)
    vertices=np.clip(np.floor((polygon-lo)/span*g),0,g-1).astype(int)
    testable[vertices[:,1]*g+vertices[:,0]]=True
    return dict(range=s,valid=valid,cells=cells,cell_count=int(testable.sum()),reference_cells=testable,
                expected_points=hit,incidence=np.maximum(-den,1e-6))

def compare_raw_returns(capture,records,config=None,*,require_stability=True):
    c=dict(DEFAULTS,**(config or {}));start=time.perf_counter();capture.validate(require_stability=require_stability)
    points,origins=capture.world_rays();vec=points-origins;ranges=np.linalg.norm(vec,axis=1)
    valid=np.isfinite(vec).all(1)&(ranges>=c['range_min'])&(ranges<=c['range_max'])
    if capture.confidence is not None:valid &= np.isfinite(capture.confidence)&(np.asarray(capture.confidence)>=c['confidence_min'])
    directions=np.divide(vec,ranges[:,None],out=np.zeros_like(vec),where=ranges[:,None]>1e-8)
    directions[~valid]=0
    robot=np.zeros(len(points),bool) if capture.robot_mask is None else np.asarray(capture.robot_mask,bool)
    labels=np.where(valid,1,0).astype(np.uint8);labels[robot&valid]=5
    evidence={};predictions={};nearest=np.full(len(points),np.inf);explained=np.zeros(len(points),bool)
    for record in records:
        if record.life_state=='VACATED':continue
        s=record.face
        if not s.trusted:
            evidence[record.box_id]=dict(kind='UNKNOWN',reason='REFERENCE_NOT_MEASURED',points=0);continue
        pred=predict_surface_hits(origins,directions,s,c);predictions[record.box_id]=pred
        use=pred['valid']&valid&~robot
        tau=c['range_error_m']+(capture.pose_error_bound_m+s.error_bound_m)/pred['incidence']
        use &= tau<=c['max_error_m']
        residual=ranges-pred['range'];idx=np.flatnonzero(use)
        point_labels=np.where(abs(residual)<=tau,2,np.where(residual<0,3,4))
        closest=use&(pred['range']<nearest);labels[closest]=point_labels[closest];nearest[closest]=pred['range'][closest]
        # Endpoint may explain a known rear surface even if it crosses an old front.
        explained |= use&(point_labels==2)
        cells=pred['cells'][idx];unique=np.unique(cells)
        unique=unique[pred['reference_cells'][unique]];vote=[]
        for cell in unique:
            # One residual per physical face cell: dense returns do not buy votes.
            members=idx[cells==cell];r=float(np.median(residual[members]));t=float(np.median(tau[members]))
            vote.append('same' if abs(r)<=t else 'nearer' if r<0 else 'clear')
        count=max(1,len(unique));coverage=len(unique)/max(1,pred['cell_count'])
        ratios={k:vote.count(k)/count for k in ('same','nearer','clear')}
        kind='UNKNOWN';reason='INSUFFICIENT_COVERAGE'
        if len(idx)>=c['min_points'] and coverage>=c['min_coverage']:
            if ratios['same']>=c['same_ratio']:kind='PRESENT';reason='MEASURED_FACE_SUPPORTED'
            elif ratios['nearer']>=c['nearer_ratio']:kind='OCCLUDED';reason='NEARER_RETURN'
            elif ratios['clear']>=c['clear_ratio']:
                if capture.return_semantics!='FIRST_OPAQUE_RETURN':reason='RETURN_SEMANTICS_UNVERIFIED'
                elif not s.full_face_observed:kind='PATCH_CLEARED';reason='PARTIAL_REFERENCE_ONLY'
                elif vote.count('clear')/max(1,pred['cell_count'])<c['min_coverage']:reason='CLEAR_AREA_TOO_SMALL'
                else:kind='CLEAR';reason='NATIVE_RAYS_THROUGH_MEASURED_FACE'
        evidence[record.box_id]=dict(kind=kind,reason=reason,points=len(idx),cells=len(unique),
            reference_cells=pred['cell_count'],coverage=coverage,ratios=ratios,
            median_residual_m=float(np.median(residual[idx])) if len(idx) else None,
            capture_id=capture.capture_id,clear_point_ids=np.asarray(capture.point_ids)[use&(point_labels==4)].tolist())
    labels[explained]=2
    return RayComparison(points,labels,predictions,evidence,(time.perf_counter()-start)*1000)
