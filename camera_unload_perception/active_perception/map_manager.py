"""One map writer; captures are analysed against an immutable map revision."""
import copy
import json
import time
import uuid
from dataclasses import dataclass,field
from pathlib import Path
import numpy as np
from scipy.optimize import linear_sum_assignment
from .schema import BoxRecord, MeasuredSurface, GeometryEstimate, unit, jsonable, transform_ok
from .ray_compare import compare_raw_returns, predict_surface_hits

DEFAULT_CONFIG=dict(center_gate_m=.20,face_gate_m=.12,ambiguity_margin=.08,
    layer_gap_m=.18,min_observations=3,confirm_probability=.70,
    evidence_limit=12.,clear_weight=1.5,missing_after=2,vacate_after=4,
    vacate_probability=.25,epoch_size=3,epoch_support=2,epoch_seconds=20.,
    epoch_translation_m=.01,epoch_rotation_deg=2.,support_distance_m=.05,
    support_angle_deg=12.,decisive_clear_ratio=.9,ray={})

def geom_value(g,key):
    x=g.get(key);return x.value if isinstance(x,GeometryEstimate) else x.get('value') if x else None

def angle(a,b):return float(np.degrees(np.arccos(np.clip(unit(a)@unit(b),-1,1))))

def relation(a,b):
    if a is None or b is None or a.shape!=b.shape:return 0.,0.,0.
    # Intersections on the minimal union ROI, not repeated full image arithmetic.
    ay,ax=np.nonzero(a);by,bx=np.nonzero(b)
    if not len(ax) or not len(bx):return 0.,0.,0.
    l=max(ax.min(),bx.min());r=min(ax.max(),bx.max())+1;t=max(ay.min(),by.min());d=min(ay.max(),by.max())+1
    inter=int(np.count_nonzero(a[t:d,l:r]&b[t:d,l:r])) if l<r and t<d else 0
    return inter/max(1,len(ax)+len(bx)-inter),inter/len(ax),inter/len(bx)

@dataclass
class Analysis:
    capture: object
    observations: list
    rays: object
    assignments: dict
    statuses: dict
    related: dict
    base_revision: int
    conflicts: list
    elapsed_ms: float
    read_only: bool=False
    geometric_candidates: list=field(default_factory=list)

class ActivePerceptionMap:
    def __init__(self,task_id='odin_validation',session_id=None,config=None):
        self.task_id=task_id;self.session_id=session_id or uuid.uuid4().hex
        self.config=copy.deepcopy(DEFAULT_CONFIG);self.config.update(config or {})
        c=self.config
        if not 2<=c['epoch_support']<=c['epoch_size']<=8:raise ValueError('epoch support/size')
        for key in ('center_gate_m','face_gate_m','layer_gap_m','evidence_limit','clear_weight','epoch_seconds','support_distance_m','support_angle_deg','epoch_translation_m','epoch_rotation_deg'):
            if not np.isfinite(c[key]) or c[key]<=0:raise ValueError('invalid '+key)
        if c['missing_after']<1 or c['vacate_after']<c['missing_after']:raise ValueError('invalid lifecycle thresholds')
        for key in ('confirm_probability','vacate_probability','ambiguity_margin','decisive_clear_ratio'):
            if not np.isfinite(c[key]) or not 0<c[key]<1:raise ValueError('invalid '+key)
        for key in ('epoch_size','epoch_support','min_observations','missing_after','vacate_after'):
            if type(c[key]) is not int or c[key]<1:raise ValueError('invalid integer '+key)
        if any(k not in DEFAULT_CONFIG for k in (config or {})):raise ValueError('unknown config field')
        from .ray_compare import DEFAULTS
        rc=dict(DEFAULTS,**c['ray'])
        if type(rc['grid']) is not int or type(rc['min_points']) is not int:raise ValueError('ray grid and point thresholds must be integers')
        if any(k not in DEFAULTS for k in c['ray']) or not 2<=rc['grid']<=32 or rc['min_points']<1 or not 0<rc['min_coverage']<=1:
            raise ValueError('invalid ray configuration')
        if not 0<rc['range_min']<rc['range_max'] or not 0<rc['min_incidence']<=1 or not 0<rc['range_error_m']<=rc['max_error_m']:raise ValueError('invalid range/error gates')
        for key in ('clear_ratio','same_ratio','nearer_ratio'):
            if not .5<rc[key]<=1 and not (key=='nearer_ratio' and rc[key]==.5):raise ValueError('invalid ratio '+key)
        self.records={};self.next_id=0;self.revision=0;self.version=None
        self.last_stamp=-float('inf');self.seen_captures=[];self.events=[];self.delta={}
        self.current_measurements=[];self.wall_batch=None;self.geometric_candidates=[]

    def _cost(self,record,obs):
        if not obs.position_ok:return None
        if record.face_type==obs.face_type:
            distance=np.linalg.norm(np.asarray(record.face.center)-obs.face.center)
            residual=abs((np.asarray(obs.face.center)-record.face.center)@unit(record.face.normal))
            if residual>self.config['face_gate_m']:return None
        else:
            a=geom_value(record.geometry,'body_center_map');b=geom_value(obs.geometry(),'body_center_map')
            if a is None or b is None:return None
            distance=np.linalg.norm(np.asarray(a)-b);residual=0.
        if distance>self.config['center_gate_m']:return None
        size=np.mean(np.minimum(abs(np.asarray(record.dimensions)-obs.dimensions)/np.asarray(record.dimensions),1))
        return .60*distance/self.config['center_gate_m']+.25*residual/self.config['face_gate_m']+.15*size

    def analyze(self,capture,observations,*,read_only=False):
        start=time.perf_counter();capture.validate(require_stability=not read_only)
        if capture.task_id!=self.task_id:raise ValueError('task mismatch')
        if self.version is not None and tuple(capture.version)!=tuple(self.version):raise ValueError('map/calibration/trajectory revision changed')
        for o in observations:
            if o.capture_id!=capture.capture_id:raise ValueError('observation source mismatch')
            o.geometry(capture.frame_id)
        rays=compare_raw_returns(capture,list(self.records.values()),self.config['ray'],require_stability=not read_only)
        gids=[i for i,r in self.records.items() if r.life_state!='VACATED']
        statuses={};assignments={};related={};conflicts=[]
        # Scan each full-size mask once; cache pairwise ROI intersections. The
        # previous prototype repeated nonzero over 1600x1296 for every pair.
        mask_index=[];overlap_cache={}
        for o in observations:
            if o.mask is None:mask_index.append(None);continue
            ys,xs=np.nonzero(o.mask)
            mask_index.append((int(xs.min()),int(ys.min()),int(xs.max())+1,int(ys.max())+1,len(xs)) if len(xs) else None)
        def overlap(j,k):
            if (j,k) in overlap_cache:return overlap_cache[j,k]
            a,b=mask_index[j],mask_index[k]
            if a is None or b is None:return (0.,0.,0.)
            l=max(a[0],b[0]);t=max(a[1],b[1]);r=min(a[2],b[2]);d=min(a[3],b[3])
            n=np.count_nonzero(observations[j].mask[t:d,l:r]&observations[k].mask[t:d,l:r]) if l<r and t<d else 0
            result=(n/max(1,a[4]+b[4]-n),n/a[4],n/b[4]);overlap_cache[j,k]=result;overlap_cache[k,j]=(result[0],result[2],result[1]);return result
        # An additional composite mask must not steal IDs from individual masks.
        for j,o in enumerate(observations):
            parts=[k for k,p in enumerate(observations) if k!=j and overlap(k,j)[1]>.85
                   and overlap(k,j)[2]<.65]
            if len(parts)>=2:statuses[j]='REDUNDANT_MERGE'
        costs=np.full((len(gids),len(observations)),1e6)
        for i,gid in enumerate(gids):
            record=self.records[gid]
            for j,o in enumerate(observations):
                if j in statuses:continue
                cost=self._cost(record,o)
                if cost is not None:costs[i,j]=cost
                # Preserve CHANGE_RELATED even when SAME_ID gating fails.
                origin=np.asarray(capture.t_world_sensor)[:3,3];vector=np.asarray(o.face.center)-origin
                distance=np.linalg.norm(vector)
                if distance<1e-6:continue
                pred=predict_surface_hits(origin[None],(vector/distance)[None],record.face,self.config['ray'])
                if pred['valid'][0] and distance-pred['range'][0]>self.config['layer_gap_m']:
                    related.setdefault(j,[]).append(gid)
        if costs.size:
            for j in range(costs.shape[1]):
                values=sorted(costs[:,j]);
                if len(values)>1 and values[1]<1 and values[1]-values[0]<self.config['ambiguity_margin']:
                    statuses[j]='IDENTITY_AMBIGUOUS';costs[:,j]=1e6
            rows,cols=linear_sum_assignment(costs)
            for i,j in zip(rows,cols):
                if costs[i,j]<1:
                    gid=gids[i];kind=rays.evidence.get(gid,{}).get('kind')
                    if kind=='CLEAR':conflicts.append(f'PRESENT_CLEAR:{gid}')
                    assignments[int(j)]=gid;statuses[int(j)]='MATCHED'
        # Explicit split and merge keep identities and cannot update geometry.
        for gid in gids:
            fragments=[j for j in range(len(observations)) if costs[gids.index(gid),j]<1 and statuses.get(j)!='REDUNDANT_MERGE']
            if len(fragments)>1:
                sizes=[np.prod(observations[j].dimensions[::2]) for j in fragments]
                if sum(sizes)<1.35*np.prod(np.asarray(self.records[gid].dimensions)[::2]):
                    for j in fragments:assignments[j]=gid;statuses[j]='DETECTOR_SPLIT'
        for j,o in enumerate(observations):
            if statuses.get(j)=='REDUNDANT_MERGE':continue
            covered=[gid for gid in gids if costs[gids.index(gid),j]<1]
            if len(covered)>1 and np.prod(o.dimensions[::2])>1.4*min(np.prod(np.asarray(self.records[g].dimensions)[::2]) for g in covered):
                assignments.pop(j,None);statuses[j]='DETECTOR_MERGE';related[j]=covered
        for j,o in enumerate(observations):
            if j in statuses:continue
            if not o.position_ok:statuses[j]='GEOMETRY_UNKNOWN';continue
            def front_unresolved(gid):
                evidence=rays.evidence.get(gid,{})
                if evidence.get('kind')=='CLEAR':return False
                if gid in assignments.values() or not (o.orientation_ok and o.face.trusted and o.face.full_face_observed):return True
                ratios=evidence.get('ratios',{})
                return not (evidence.get('reason')=='RETURN_SEMANTICS_UNVERIFIED'
                    and evidence.get('points',0)>=self.config['ray'].get('min_points',80)
                    and evidence.get('coverage',0)>=self.config['ray'].get('min_coverage',.40)
                    and ratios.get('clear',0)>=.8 and ratios.get('same',1)<=.1 and ratios.get('nearer',1)<=.1)
            statuses[j]='LAYER_CHANGE_PENDING' if any(front_unresolved(g) for g in related.get(j,[])) else 'NEW_CANDIDATE'
        return Analysis(capture,observations,rays,assignments,statuses,related,self.revision,conflicts,(time.perf_counter()-start)*1000,read_only)

    def _add(self,r,positive=0.,negative=0.):
        total=r.positives+r.negatives
        if total>self.config['evidence_limit']:
            factor=self.config['evidence_limit']/total;r.positives*=factor;r.negatives*=factor
        r.positives+=positive;r.negatives+=negative

    def _compatible(self,a,b):
        return (a.face_type==b.face_type and np.linalg.norm(np.asarray(a.face.center)-b.face.center)<=self.config['support_distance_m']
            and angle(a.face.normal,b.face.normal)<=self.config['support_angle_deg'])

    def commit(self,analyses,*,batch=True,wall=False,required_ids=(),allow_vacated_required=False):
        c=self.config
        def fail(reason):return dict(committed=False,reason=reason,revision=self.revision)
        if not analyses:return fail('NO_CAPTURE')
        if any(a.read_only or not a.capture.stable_capture for a in analyses):return fail('READ_ONLY_OR_UNSTABLE_CAPTURE')
        captures=[a.capture for a in analyses];ids=[p.capture_id for p in captures]
        if len(ids)!=len(set(ids)) or any(i in self.seen_captures for i in ids):return fail('DUPLICATE_CAPTURE')
        stamps=[p.rgb_stamp for p in captures];cloud_stamps=[p.cloud_stamp for p in captures]
        if stamps[0]<=self.last_stamp or any(b<=a for a,b in zip(stamps,stamps[1:])) or len(set(cloud_stamps))!=len(cloud_stamps):return fail('NONMONOTONIC_CAPTURE')
        if any(a.base_revision!=self.revision or a.capture.version!=captures[0].version for a in analyses):return fail('VERSION_CONFLICT')
        if any(a.conflicts for a in analyses):return fail('EVIDENCE_CONFLICT')
        if batch:
            views={v:[a for a in analyses if a.capture.view_id==v] for v in {p.view_id for p in captures}}
            for group in views.values():
                if len(group)<c['epoch_size']:return fail('INSUFFICIENT_BATCH')
                if group[-1].capture.rgb_stamp-group[0].capture.rgb_stamp>c['epoch_seconds']:return fail('BATCH_TIMEOUT')
                for pose_key in ('t_world_sensor','t_world_camera'):
                    first=np.asarray(getattr(group[0].capture,pose_key))
                    for a in group[1:]:
                        t=np.asarray(getattr(a.capture,pose_key))
                        rot=np.degrees(np.arccos(np.clip((np.trace(first[:3,:3].T@t[:3,:3])-1)/2,-1,1)))
                        if np.linalg.norm(first[:3,3]-t[:3,3])>c['epoch_translation_m'] or rot>c['epoch_rotation_deg']:return fail('BATCH_MOVED')
        # True contradictory evidence rejects the whole transaction, not majority voting.
        kinds={gid:[] for gid in self.records if self.records[gid].life_state!='VACATED'}
        matches={gid:[] for gid in kinds}
        for a in analyses:
            for gid in kinds:
                jj=[j for j,g in a.assignments.items() if g==gid]
                merged=any(s=='DETECTOR_MERGE' and gid in a.related.get(j,[]) for j,s in a.statuses.items())
                kind='PRESENT' if jj else 'UNKNOWN' if merged else a.rays.evidence.get(gid,{}).get('kind','UNKNOWN')
                kinds[gid].append(kind)
                if jj:matches[gid].append((a,a.observations[jj[0]],a.statuses[jj[0]]))
        for gid,kk in kinds.items():
            if 'PRESENT' in kk and 'CLEAR' in kk:return fail('PRESENT_CLEAR:'+str(gid))
        need=c['epoch_support'] if batch else 1
        for gid in required_ids:
            if allow_vacated_required and gid in self.records and self.records[gid].life_state=='VACATED':continue
            kk=kinds.get(gid,[])
            if max(kk.count('PRESENT'),kk.count('CLEAR'))<need:return fail('REQUIRED_SCOPE_UNRESOLVED:'+str(gid))
        trial=copy.deepcopy(self);events=[];now=stamps[-1];updated=set()
        for r in trial.records.values():r.current_capture_id='';r.current_pose_ok=False
        for gid,kk in kinds.items():
            r=trial.records[gid];mm=matches[gid]
            if mm and len(mm)>=need:
                r.review_clear_runs=0;r.unmeasured_run=0;r.decisive_clears=0
                # Last view seeing this ID wins only when supported in that view.
                a,o,status=mm[-1];support=[(b,p,s) for b,p,s in mm if self._compatible(o,p)]
                if len(support)<need:continue
                if not wall and gid not in analyses[-1].assignments.values():
                    r.clear_run=0;r.visibility='VISIBLE';trial._add(r,positive=.3);continue
                if status=='MATCHED':
                    geom=o.geometry(a.capture.frame_id)
                    if not o.orientation_ok:
                        held=copy.deepcopy(r.geometry['body_orientation_map']);held.status='RETAINED';geom['body_orientation_map']=held
                    r.geometry=geom
                    if o.face.trusted:r.face=copy.deepcopy(o.face)
                    r.dimensions=list(o.dimensions);r.dimension_status=list(o.dimension_status);r.horizontal=jsonable(o.horizontal);r.face_type=o.face_type
                    r.current_capture_id=o.capture_id;r.current_pose_ok=o.orientation_ok
                r.observations+=1;r.clear_run=0;r.visibility='VISIBLE';r.last_seen=now;trial._add(r,positive=1)
                if r.ever_confirmed or (batch and len(support)>=need) or (r.observations>=c['min_observations'] and r.probability>=c['confirm_probability']):
                    r.life_state='CONFIRMED';r.ever_confirmed=True
                updated.add(gid);events.append(dict(type=status,box_id=gid))
            elif kk.count('CLEAR')>=need and 'OCCLUDED' not in kk and (wall or kk.count('CLEAR')/len(kk)>=.60):
                r.review_clear_runs=0;r.unmeasured_run=0
                r.clear_run+=1;trial._add(r,negative=c['clear_weight']);r.visibility='UNKNOWN'
                if r.clear_run>=c['missing_after']:r.life_state='MISSING_CANDIDATE'
                # Full-coverage see-through is physically conclusive: an opaque box
                # cannot be seen through while present, so a sustained run of it
                # removes the box on run length alone. Otherwise a long-confirmed
                # box's capped positive mass makes the probability gate far too slow
                # (many frames to VACATE even for an unambiguous clear). Unlike
                # clear_run (one per commit), this counts individual captures so an
                # epoch batch of 3 clear frames advances it by 3 -- latency to
                # removal is then bounded by frames seen, not commits made.
                # "Decisive" is judged on the tested cells, not on coverage: rays
                # through a vacated face frequently get no valid return behind it
                # (out of range, low confidence), so 60-75 % coverage is normal in
                # real scenes. What is conclusive is that no tested cell still sees
                # the face (same ~ 0) while nearly all see through it.
                clears=[a.rays.evidence.get(gid,{}) for a in analyses if a.rays.evidence.get(gid,{}).get('kind')=='CLEAR']
                thr=c['decisive_clear_ratio']
                full=[e for e in clears if e.get('ratios',{}).get('clear',0)>=thr and e.get('ratios',{}).get('same',1)<=1-thr]
                r.decisive_clears=r.decisive_clears+len(full) if clears and len(full)==len(clears) else 0
                if (r.clear_run>=c['vacate_after'] and r.probability<c['vacate_probability']) or r.decisive_clears>=c['vacate_after']:r.life_state='VACATED'
                events.append(dict(type='OBJECT_VACATED' if r.life_state=='VACATED' else 'CLEAR_SUPPORT',box_id=gid))
            elif r.ever_confirmed and geom_value(r.geometry,'body_center_map') is None and 'PRESENT' not in kk and 'OCCLUDED' not in kk:
                # A confirmed box with no usable 3D geometry (untrusted / never
                # fully measured face) that is neither seen nor occluded cannot be
                # ray-tested for removal, so it would otherwise persist forever.
                # Decay it on the same lifecycle as CLEAR evidence. Pose-agnostic:
                # a genuinely measured box keeps a valid body_center and is immune.
                r.review_clear_runs=0;r.decisive_clears=0
                r.unmeasured_run+=1;trial._add(r,negative=c['clear_weight']);r.visibility='UNKNOWN'
                if r.unmeasured_run>=c['missing_after']:r.life_state='MISSING_CANDIDATE'
                if r.unmeasured_run>=c['vacate_after'] and r.probability<c['vacate_probability']:r.life_state='VACATED'
                events.append(dict(type='OBJECT_VACATED' if r.life_state=='VACATED' else 'UNMEASURED_DECAY',box_id=gid))
            else:
                r.clear_run=0;r.unmeasured_run=0;r.decisive_clears=0;r.visibility='OCCLUDED' if 'OCCLUDED' in kk else 'VISIBLE' if 'PRESENT' in kk else 'UNKNOWN'
                if 'PRESENT' in kk:trial._add(r,positive=.3)
                details=[a.rays.evidence.get(gid,{}) for a in analyses]
                review=sum(
                    e.get('reason') in ('RETURN_SEMANTICS_UNVERIFIED','PARTIAL_REFERENCE_ONLY')
                    and e.get('points',0)>=c['ray'].get('min_points',80)
                    and e.get('coverage',0)>=c['ray'].get('min_coverage',.40)
                    and e.get('ratios',{}).get('clear',0)>=.8
                    and e.get('ratios',{}).get('same',1)<=.1
                    and e.get('ratios',{}).get('nearer',1)<=.1
                    for e in details
                )
                r.review_clear_runs=r.review_clear_runs+1 if review>=need and not mm else 0
            r.last_evidence=dict(kinds=kk,capture_ids=ids,details=[a.rays.evidence.get(gid,{}) for a in analyses])
        groups=[]
        for a in analyses:
            for j,o in enumerate(a.observations):
                if a.statuses[j]!='NEW_CANDIDATE':continue
                candidates=[g for g in groups if self._compatible(g[-1][1],o)]
                if len(candidates)>1:return fail('NEW_ID_AMBIGUOUS')
                if candidates:candidates[0].append((a,o,a.related.get(j,[])))
                else:groups.append([(a,o,a.related.get(j,[]))])
        for group in groups:
            unique={o.capture_id for _,o,_ in group}
            if len(unique)<need:continue
            a,o,related=group[-1]
            if not wall and a is not analyses[-1]:continue
            # A new identity must rest on a genuinely measured face: reliable
            # position AND orientation AND a fully observed face (i.e. a valid
            # body_center). This is a measurement-quality gate, NOT a pose gate --
            # a well measured box in an odd pose still qualifies -- and it stops
            # degraded-frame or prior-only detections from minting phantom boxes.
            if geom_value(o.geometry(a.capture.frame_id),'body_center_map') is None:continue
            # A split/duplicate of an already accounted object is not a new identity.
            if any(trial._cost(r,o) is not None for r in trial.records.values() if r.life_state!='VACATED'):continue
            gid=trial.next_id;trial.next_id+=1
            reoccupied=[r.box_id for r in trial.records.values() if r.life_state=='VACATED' and self._cost(r,o) is not None]
            record=BoxRecord(gid,copy.deepcopy(o.face),o.geometry(a.capture.frame_id),list(o.dimensions),list(o.dimension_status),
                o.face_type,jsonable(o.horizontal),last_seen=now,current_capture_id=o.capture_id,current_pose_ok=o.orientation_ok,
                related_old_ids=related,identity_status='LOCATION_REOCCUPIED' if reoccupied else 'RESOLVED')
            if batch:record.life_state='CONFIRMED';record.ever_confirmed=True;record.positives=2.
            trial.records[gid]=record;events.append(dict(type='LAYER_REVEALED' if related else 'OBJECT_NEW',box_id=gid,old_ids=related,reoccupied_ids=reoccupied))
        trial.version=captures[0].version;trial.last_stamp=now;trial.seen_captures=(trial.seen_captures+ids)[-512:]
        before={i for i,r in self.records.items() if r.ever_confirmed and r.life_state!='VACATED'}
        after={i for i,r in trial.records.items() if r.ever_confirmed and r.life_state!='VACATED'}
        trial.revision+=1
        trial.delta=dict(base_revision=self.revision,revision=trial.revision,added=sorted(after-before),removed=sorted(before-after),
            updated=sorted(updated&before&after),kept=sorted((before&after)-updated))
        for e in events:e.update(revision=trial.revision,stamp=now,capture_ids=ids)
        trial.events=(trial.events+events)[-200:]
        trial.geometric_candidates=[jsonable(c) for a in analyses for c in a.geometric_candidates][-24:]
        trial.current_measurements=[dict(local_id=o.local_id,box_id=a.assignments.get(j,next((r.box_id for r in trial.records.values() if r.current_capture_id==o.capture_id and r.face.capture_id==o.capture_id and np.linalg.norm(np.asarray(r.face.center)-o.face.center)<1e-5),None)),
            capture_id=o.capture_id,position_ok=o.position_ok,orientation_ok=o.orientation_ok,geometry=jsonable(o.geometry(a.capture.frame_id))) for a in analyses[-1:] for j,o in enumerate(a.observations)]
        # Validate prior to the sole state swap.
        for r in trial.records.values():r.face.validate()
        json.dumps(trial.snapshot(),allow_nan=False)
        self.__dict__.update(trial.__dict__)
        return dict(committed=True,reason='COMMITTED',revision=self.revision,delta=self.delta)

    def process(self,capture,observations):return self.commit([self.analyze(capture,observations)],batch=False)

    def reanchor(self,transform,reason='LOCALIZATION_CORRECTION'):
        """Re-express every record in a corrected map frame (map_new <- map_old).
        Boxes are static in the world; when the localizer re-aligns its map
        frame the stored coordinates follow the correction instead of the
        session dying. Identities, evidence and history are preserved."""
        m=np.asarray(transform,dtype=float)
        if not transform_ok(m):raise ValueError('invalid reanchor transform')
        rot=m[:3,:3]
        def pt(v):return (rot@np.asarray(v,dtype=float)+m[:3,3]).tolist()
        def value(g):return g.value if isinstance(g,GeometryEstimate) else g.get('value') if g else None
        def assign(g,v):
            if isinstance(g,GeometryEstimate):g.value=v
            else:g['value']=v
        for r in self.records.values():
            f=r.face;f.center=pt(f.center);f.normal=(rot@unit(f.normal)).tolist();f.polygon=[pt(p) for p in f.polygon]
            if r.horizontal is not None:r.horizontal=(rot@np.asarray(r.horizontal,dtype=float)).tolist()
            for key in ('body_center_map','face_center_map','contact_center_map'):
                g=r.geometry.get(key)
                if g is not None and value(g) is not None:assign(g,pt(value(g)))
            g=r.geometry.get('body_orientation_map')
            if g is not None and value(g) is not None:assign(g,(rot@np.asarray(value(g),dtype=float)).tolist())
            f.validate()
        translation=float(np.linalg.norm(m[:3,3]));rotation=float(np.degrees(np.arccos(np.clip((np.trace(rot)-1)/2,-1,1))))
        self.revision+=1
        self.events=(self.events+[dict(type='MAP_REANCHORED',reason=reason,translation_m=round(translation,4),rotation_deg=round(rotation,3),revision=self.revision)])[-200:]

    def set_wall_batch(self,ids):
        if any(i not in self.records or not self.records[i].ever_confirmed for i in ids):raise ValueError('unknown batch member')
        self.wall_batch=dict(id=uuid.uuid4().hex,member_ids=sorted(set(ids)),created_revision=self.revision)

    def confirm_removed(self,box_id,reason,expected_revision,now_stamp):
        """Audited human observation; never promotes unknown returns to CLEAR."""
        if type(box_id) is not int or box_id not in self.records:raise ValueError('unknown box ID')
        if type(expected_revision) is not int or expected_revision!=self.revision:raise ValueError('map changed; inspect new evidence')
        if not isinstance(reason,str) or len(reason.strip())<8:raise ValueError('physical removal needs an audit reason')
        r=self.records[box_id]
        if r.life_state=='VACATED':raise ValueError('already vacated')
        if self.version is None or r.review_clear_runs<2 or r.current_capture_id or r.visibility!='UNKNOWN':
            raise ValueError('no consecutive unambiguous review evidence')
        if not np.isfinite(now_stamp) or now_stamp<self.last_stamp or now_stamp-self.last_stamp>12:
            raise ValueError('latest observation is stale')
        details=r.last_evidence.get('details',[])
        if len({e.get('capture_id') for e in details if e.get('reason') in ('RETURN_SEMANTICS_UNVERIFIED','PARTIAL_REFERENCE_ONLY')})<2:
            raise ValueError('insufficient independent raw captures')
        trial=copy.deepcopy(self);record=trial.records[box_id]
        record.life_state='VACATED';record.visibility='UNKNOWN';record.current_pose_ok=False;record.current_capture_id=''
        record.last_evidence=dict(record.last_evidence,operator_confirmation=dict(
            reason=reason.strip(),stamp=now_stamp,source='HUMAN_PHYSICAL_CHECK',
            raw_semantics='UNVERIFIED_OR_PARTIAL',review_clear_runs=record.review_clear_runs))
        trial.revision+=1;trial.delta=dict(base_revision=self.revision,revision=trial.revision,
            added=[],removed=[box_id],updated=[],kept=sorted(i for i,x in trial.records.items() if i!=box_id and x.ever_confirmed and x.life_state!='VACATED'))
        trial.events=(trial.events+[dict(type='OPERATOR_CONFIRMED_REMOVAL',box_id=box_id,
            reason=reason.strip(),revision=trial.revision,stamp=now_stamp,
            capture_ids=r.last_evidence.get('capture_ids',[]))])[-200:]
        json.dumps(trial.snapshot(),allow_nan=False)
        self.__dict__.update(trial.__dict__)
        return dict(committed=True,reason='OPERATOR_CONFIRMED_REMOVAL',revision=self.revision,delta=self.delta)

    def resolve_identity(self,box_id,reason):
        if not reason or box_id not in self.records:raise ValueError('identity resolution needs known ID and audit reason')
        r=self.records[box_id]
        if r.life_state=='VACATED':raise ValueError('use audited REINSTATE for an erroneous VACATED record')
        r.identity_status='RESOLVED';r.current_pose_ok=False;self.revision+=1
        self.events=(self.events+[dict(type='IDENTITY_RESOLVED',box_id=box_id,reason=reason,revision=self.revision)])[-200:]

    def reinstate(self,old_id,replacement_id,reason):
        """Explicit operator correction of a false VACATED, never automatic ID reuse."""
        if not reason or old_id==replacement_id or old_id not in self.records or replacement_id not in self.records:raise ValueError('known old/replacement IDs and audit reason required')
        old=self.records[old_id];new=self.records[replacement_id]
        if old.life_state!='VACATED' or new.life_state!='CONFIRMED' or self._cost(old,new_as_observation(new)) is None:
            raise ValueError('REINSTATE requires a compatible confirmed replacement at the falsely vacated location')
        restored=copy.deepcopy(new);restored.box_id=old_id;restored.identity_status='RESOLVED';restored.current_pose_ok=False
        restored.current_capture_id='';self.records[old_id]=restored;new.life_state='VACATED';new.identity_status='ALIASED_BY_AUDIT';new.current_pose_ok=False
        self.revision+=1;self.events=(self.events+[dict(type='REINSTATE',box_id=old_id,replacement_id=replacement_id,reason=reason,revision=self.revision)])[-200:]

    def execution_candidates(self):
        out=[]
        for i,r in self.records.items():
            if not (r.life_state=='CONFIRMED' and r.identity_status=='RESOLVED' and r.current_pose_ok and r.current_capture_id and geom_value(r.geometry,'body_center_map') is not None):continue
            if any(g in self.records and self.records[g].life_state!='VACATED' for g in r.related_old_ids):continue
            out.append(i)
        return out

    def snapshot(self):
        return dict(schema_version=2,task_id=self.task_id,session_id=self.session_id,version=self.version,revision=self.revision,
            next_id=self.next_id,last_stamp=jsonable(self.last_stamp),seen_captures=self.seen_captures,
            inventory=[r.to_dict() for r in self.records.values() if r.ever_confirmed and r.life_state!='VACATED'],
            history=[r.to_dict() for r in self.records.values()],current_measurements=self.current_measurements,
            execution_candidates=self.execution_candidates(),events=self.events,delta=self.delta,wall_batch=self.wall_batch,geometric_candidates=self.geometric_candidates)

    def save(self,path):
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        temp=path.with_suffix('.tmp');temp.write_text(json.dumps(self.snapshot(),allow_nan=False));temp.replace(path)

    def restore(self,path,expected_version):
        data=json.loads(Path(path).read_text())
        if data['schema_version']!=2 or tuple(data['version'])!=tuple(expected_version):raise ValueError('checkpoint provenance mismatch')
        trial=ActivePerceptionMap(data['task_id'],data['session_id'],self.config)
        for row in data['history']:
            row=dict(row);row.pop('existence_probability',None);row['face']=MeasuredSurface(**row['face'])
            row['geometry']={k:GeometryEstimate(**v) for k,v in row['geometry'].items()}
            r=BoxRecord(**row);r.face.validate();r.current_pose_ok=False;r.current_capture_id='';trial.records[r.box_id]=r
        if trial.records and data['next_id']<=max(trial.records):raise ValueError('reused ID allocator')
        for k in ('next_id','revision','last_stamp','seen_captures','events','delta','wall_batch'):setattr(trial,k,data[k])
        trial.version=tuple(expected_version);self.__dict__.update(trial.__dict__)


def new_as_observation(record):
    from .schema import BoxObservation
    return BoxObservation(0,record.face.capture_id,record.face,record.horizontal,record.dimensions,
        position_ok=True,orientation_ok=True,face_type=record.face_type,dimension_status=record.dimension_status)
