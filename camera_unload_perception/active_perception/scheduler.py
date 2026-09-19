"""Event-driven observation sessions. No fake actuator or stability success."""
from enum import Enum
import copy
import time
import uuid
import numpy as np

class ObservationMode(str,Enum):
    REALTIME='realtime'
    MANUAL='manual'
    EPOCH='epoch'

class ActiveObservationScheduler:
    VIEWS=('UP','DOWN','LEFT','RIGHT')
    def __init__(self,manager,timeout_s=180.):
        self.map=manager;self.timeout_s=timeout_s;self.session=None;self.analyses=[];self.history=[]
        self.coverage_region=None;self.covered=set();self.view_poses={};self.clock=time.monotonic

    def configure_region(self,corners):
        p=np.asarray(corners,float)
        if p.shape!=(4,3) or not np.isfinite(p).all():raise ValueError('four fixed world-space region corners required')
        if np.linalg.norm(np.cross(p[1]-p[0],p[3]-p[0]))<.01 or np.linalg.norm(p[2]-(p[1]+p[3]-p[0]))>.01:
            raise ValueError('region must be a nondegenerate ordered parallelogram')
        self.coverage_region=p

    def start(self,kind,targets=(),robot_ready=False):
        if self.session and self.session['state'] not in ('COMPLETE','PARTIAL','CANCELLED','FAILED'):raise ValueError('session already active')
        if kind not in ('EPOCH','LOCAL_CHECK','WALL_SCAN'):raise ValueError('unknown session type')
        if not robot_ready:raise ValueError('observation-ready handshake required')
        if kind=='WALL_SCAN' and not targets and self.map.wall_batch:targets=self.map.wall_batch['member_ids']
        if any(i not in self.map.records for i in targets):raise ValueError('unknown target')
        if kind=='LOCAL_CHECK' and not targets:raise ValueError('LOCAL_CHECK needs old target IDs')
        target_points=[self.map.records[i].face.center for i in targets]
        # Separate finite aim requests for distant old target regions. This
        # conservatively limits a local view to targets within 0.8 m.
        groups=[]
        for gid,point in zip(targets,target_points):
            group=next((g for g in groups if np.linalg.norm(np.asarray(point)-self.map.records[g[0]].face.center)<.8),None)
            if group is None:groups.append([gid])
            else:group.append(gid)
        views=list(self.VIEWS) if kind=='WALL_SCAN' else [f'TARGET_GROUP_{i}' for i in range(len(groups))] if len(groups)>1 else ['TARGET_REGION']
        self.session=dict(id=uuid.uuid4().hex,kind=kind,targets=list(targets),state='WAIT_OPERATOR',
            views=views,target_groups=groups,view_index=0,collected=0,
            base_revision=self.map.revision,scan_actions_done=False,map_commit_ok=False,
            coverage_status='UNKNOWN',requested_scope_verified=False,wall_batch_verified_complete=False,
            started=self.clock(),target_region=copy.deepcopy(target_points),reason='MANUAL_ODIN_AIM_REQUIRED',
            completed_views=[],unresolved_regions=[])
        self.analyses=[];self.covered=set();self.view_poses={}
        return self.snapshot()

    def acknowledge(self,view):
        s=self.session
        if not s or s['state']!='WAIT_OPERATOR' or view!=s['views'][s['view_index']]:raise ValueError('unexpected view acknowledgement')
        s['state']='WAIT_STABLE';s['ack_time']=self.clock();s['reason']='WAIT_COMPLETE_STABLE_SCAN'

    def cancel(self):
        if self.session:self.session['state']='CANCELLED';self.session['reason']='OPERATOR_CANCELLED'
        self.analyses=[]

    def check_timeout(self):
        if self.session and self.session['state'] in ('WAIT_OPERATOR','WAIT_STABLE','COLLECTING') and self.clock()-self.session['started']>self.timeout_s:
            self.session.update(state='PARTIAL',reason='SESSION_TIMEOUT');self.analyses=[]

    def wants_capture(self):
        self.check_timeout()
        return bool(self.session and self.session['state'] in ('WAIT_STABLE','COLLECTING'))

    def _coverage(self,analysis):
        if self.coverage_region is None:return
        p=self.coverage_region;u=p[1]-p[0];v=p[3]-p[0];basis=np.column_stack((u,v));inv=np.linalg.pinv(basis)
        from .schema import MeasuredSurface,unit
        from .ray_compare import predict_surface_hits
        packet=analysis.capture;cloud,origins=packet.world_rays();vec=cloud-origins;ranges=np.linalg.norm(vec,axis=1)
        directions=np.divide(vec,ranges[:,None],out=np.zeros_like(vec),where=ranges[:,None]>1e-8)
        n=unit(np.cross(u,v))
        if (origins[0]-p[0])@n<0:n=-n
        surface=MeasuredSurface(p.mean(0),n,p,True,True)
        pred=predict_surface_hits(origins,directions,surface,dict(grid=10,boundary_m=0))
        t=np.linalg.inv(packet.t_world_camera);hit=pred['expected_points'];optical=hit@t[:3,:3].T+t[:3,3]
        intr=packet.intrinsics;z=np.maximum(optical[:,2],1e-8)
        x=(intr['fx']*optical[:,0]+intr.get('skew',0)*optical[:,1])/z+intr['ppx'];y=intr['fy']*optical[:,1]/z+intr['ppy']
        h,w=packet.image.shape[:2] if packet.image is not None else (0,0)
        # Fixed region is a coverage scope, never a geometric box reference. Only
        # observed/explained endpoints near that scope cover a cell. A closer arm,
        # missing return, or unsegmented point cluster leaves it unresolved.
        valid=pred['valid']&(optical[:,2]>.2)&(x>=0)&(x<w)&(y>=0)&(y<h)
        explained=analysis.rays.labels==2
        for j,o in enumerate(analysis.observations):
            if o.face.trusted and analysis.statuses[j] not in ('IDENTITY_AMBIGUOUS','DETECTOR_SPLIT','DETECTOR_MERGE','REDUNDANT_MERGE'):
                explained |= np.isin(packet.point_ids,o.face.support_point_ids)
        supported=explained&(abs(ranges-pred['range'])<.35)
        cleared=((analysis.rays.labels==4)|explained)&(ranges>pred['range']+.1)&(packet.return_semantics=='FIRST_OPAQUE_RETURN')
        valid &= supported|cleared
        for cell in np.unique(pred['cells'][valid]):
            if np.count_nonzero(valid&(pred['cells']==cell))>=3:self.covered.add((int(cell%10),int(cell//10)))

    def feed(self,analysis):
        if not self.wants_capture():return None
        s=self.session;p=analysis.capture
        if not p.stable_capture:return None
        if analysis.base_revision!=s['base_revision']:
            s.update(state='FAILED',reason='MAP_CHANGED_DURING_SESSION');self.analyses=[];return self.snapshot()
        view=s['views'][s['view_index']];p.view_id=view
        if any(a.capture.capture_id==p.capture_id for a in self.analyses):return None
        # Four names on identical poses are not four executed observation directions.
        pose=np.asarray(p.t_world_camera)
        if view not in self.view_poses and s['kind']=='WALL_SCAN':
            for old in self.view_poses.values():
                deg=np.degrees(np.arccos(np.clip((np.trace(old[:3,:3].T@pose[:3,:3])-1)/2,-1,1)))
                if deg<2 and np.linalg.norm(old[:3,3]-pose[:3,3])<.01:
                    s.update(state='WAIT_OPERATOR',reason='VIEW_NOT_CHANGED');return self.snapshot()
        self.view_poses.setdefault(view,pose);self.analyses.append(analysis);self._coverage(analysis)
        s['state']='COLLECTING';s['collected']=sum(a.capture.view_id==view for a in self.analyses)
        if s['collected']<self.map.config['epoch_size']:return self.snapshot()
        s['completed_views'].append(view)
        if s['view_index']+1<len(s['views']):
            s.update(view_index=s['view_index']+1,collected=0,state='WAIT_OPERATOR',reason='AIM_NEXT_VIEW');return self.snapshot()
        s['scan_actions_done']=True
        if s['kind']=='WALL_SCAN' and (self.coverage_region is None or len(self.covered)<80):
            s.update(state='PARTIAL',reason='COVERAGE_NOT_VERIFIED',coverage_status='UNKNOWN' if self.coverage_region is None else 'PARTIAL')
            s['unresolved_regions']=[list(cell) for cell in ((x,y) for x in range(10) for y in range(10)) if cell not in self.covered]
            self.analyses=[];return self.snapshot()
        result=self.map.commit(self.analyses,batch=True,wall=len(s['views'])>1,required_ids=s['targets'],allow_vacated_required=s['kind']=='WALL_SCAN')
        s['map_commit_ok']=result['committed'];s['reason']=result['reason'];s['requested_scope_verified']=result['committed']
        pending=any(i in self.map.records and self.map.records[i].life_state=='MISSING_CANDIDATE' for i in s['targets'])
        pending |= any(v in ('IDENTITY_AMBIGUOUS','LAYER_CHANGE_PENDING','DETECTOR_MERGE') for a in self.analyses for v in a.statuses.values())
        pending |= any(a.geometric_candidates for a in self.analyses)
        if pending and result['committed']:s['requested_scope_verified']=False;s['reason']='UNRESOLVED_CANDIDATES_OR_LIFECYCLE'
        s['state']='COMPLETE' if result['committed'] and not pending else 'PARTIAL'
        s['coverage_status']='COMPLETE' if s['kind']=='WALL_SCAN' and result['committed'] else s['coverage_status']
        if s['kind']=='WALL_SCAN' and result['committed'] and self.map.wall_batch:
            members=self.map.wall_batch['member_ids']
            missed=[gid for gid,r in self.map.records.items() if gid not in members and r.life_state=='CONFIRMED' and
                any(abs((np.asarray(r.face.center)-self.map.records[i].face.center)@np.asarray(self.map.records[i].face.normal))<.12 for i in members)]
            s['missed_current_face_ids']=missed
            s['wall_batch_verified_complete']=not missed and not pending and all(self.map.records[i].life_state=='VACATED' for i in members)
            s['policy_action']='RETURN_CURRENT_BATCH' if missed else 'SELECT_NEXT_BATCH' if s['wall_batch_verified_complete'] else 'REOBSERVE_CURRENT_BATCH'
        self.history=(self.history+[copy.deepcopy(s)])[-20:];self.analyses=[];return self.snapshot()

    def snapshot(self):return dict(session=copy.deepcopy(self.session),covered_cells=len(self.covered),coverage_region=None if self.coverage_region is None else self.coverage_region.tolist())
