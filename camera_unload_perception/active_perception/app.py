"""Standalone Odin architecture validation app, HTTP UI and ROS integration."""
import argparse
from collections import deque
import copy
import fcntl
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time
import traceback
from urllib.parse import urlsplit
import cv2
import numpy as np
from .schema import CapturePacket,BoxObservation,MeasuredSurface,jsonable
from .map_manager import ActivePerceptionMap
from .scheduler import ActiveObservationScheduler
from .odin_adapter import OdinFrontend,load_saved_capture
from .policy import advise,wrist_verify_batch

ROOT=Path(__file__).resolve().parents[4]
ASSETS=Path(__file__).parent/'web'


class DemoSource:
    """Synthetic contract fixture, conspicuously labelled, never a camera fallback."""
    def __init__(self):self.n=0;self.error='';self.hidden=set()
    def next_capture(self):
        self.n+=1;t=float(self.n);image=np.zeros((720,960,3),np.uint8);points=[];observations=[]
        p=CapturePacket(str(t),t,t,None,None,np.eye(4),np.eye(4),dict(fx=600,fy=600,ppx=480,ppy=360),
            image=image,map_epoch='SYNTHETIC',calibration_revision='SYNTHETIC',stable_capture=True,pose_quality=True,
            source='SYNTHETIC_NOT_ODIN',return_semantics='FIRST_OPAQUE_RETURN')
        for i,(x,y) in enumerate(((-.3,-.3),(.3,-.3),(-.3,.3),(.3,.3))):
            xx,yy=np.meshgrid(np.linspace(x-.245,x+.245,32),np.linspace(y-.245,y+.245,32))
            a=np.column_stack((xx.ravel(),yy.ravel(),np.full(xx.size,2.)))
            if i in self.hidden:points.append(a*1.5);continue
            points.append(a);l,r=int(480+300*(x-.25)),int(480+300*(x+.25));top,b=int(360+300*(y-.25)),int(360+300*(y+.25))
            cv2.rectangle(image,(l,top),(r,b),(80,140,185),-1)
            mask=np.zeros(image.shape[:2],bool);mask[top:b,l:r]=True
            face=MeasuredSurface([x,y,2],[0,0,-1],[[x-.25,y-.25,2],[x+.25,y-.25,2],[x+.25,y+.25,2],[x-.25,y+.25,2]],True,True,'SYNTHETIC',p.capture_id,t)
            observations.append(BoxObservation(i,p.capture_id,face,[1,0,0],[.5,.3,.5],[l,top,r,b],mask,.99,True,True))
        p.points_sensor=np.concatenate(points);p.point_ids=np.arange(len(p.points_sensor))
        cv2.putText(image,'SYNTHETIC TEST - NOT CAMERA',(30,45),cv2.FONT_HERSHEY_SIMPLEX,.8,(0,220,255),2)
        self.latest=p;self.observations=observations;return p
    def preview(self):return (self.latest.image,self.latest.rgb_stamp) if hasattr(self,'latest') else (None,None)
    def health(self):return dict(source='SYNTHETIC_NOT_ODIN',error='',return_semantics='FIRST_OPAQUE_RETURN',frame_id='map')
    def close(self):pass


class ReplaySource:
    def __init__(self,path):
        path=Path(path)
        self.paths=[path.with_suffix('')] if path.is_file() else [p.with_suffix('') for p in path.glob('*.npz') if p.with_suffix('.json').exists() and p.with_suffix('.jpg').exists()]
        self.paths.sort(key=lambda p:json.loads(p.with_suffix('.json').read_text()).get('sensor_stamp',0))
        if not self.paths:raise ValueError('no saved RGB/raw NPZ/JSON capture triples')
        self.index=0;self.latest=None;self.error='RECORDED_SCAN_STABILITY_UNVERIFIED';self.skipped=[]
    def next_capture(self):
        if self.index>=len(self.paths):self.error='REPLAY_EOF';return None
        path=self.paths[self.index];self.index+=1
        try:p,_,_=load_saved_capture(path)
        except (ValueError,KeyError,OSError) as exc:
            self.error=str(exc);self.skipped.append(dict(path=str(path),reason=str(exc)));return None
        self.latest=p;self.error='RECORDED_SCAN_STABILITY_UNVERIFIED';return p
    def preview(self):return (self.latest.image,self.latest.rgb_stamp) if self.latest is not None else (None,None)
    def health(self):return dict(source='RECORDED_ODIN_RAW',error=self.error,frames=self.index,total=len(self.paths),map_commit_enabled=False,skipped=self.skipped)
    def close(self):pass


class Project:
    def __init__(self,args,source):
        self.args=args;self.source=source;self.root=Path(args.root);self.out=Path(args.output);self.out.mkdir(parents=True,exist_ok=True)
        config=json.loads(Path(args.config).read_text()) if args.config else None
        self.manager=ActivePerceptionMap(config=config);self.scheduler=ActiveObservationScheduler(self.manager)
        self.frontend=None;self.stop=threading.Event();self.commands=queue.Queue(maxsize=32);self.lock=threading.Lock()
        self.mode=args.mode;self.request=False;self.robot_ready=True;self.last_analysis=None;self.samples=[];self.stage={};self.resume_failure=''
        self.state={};self.state_bytes=b'{}';self.scene_bytes=b'{}';self.jpeg=None;self.raw_jpeg=None;self.depth_jpeg=None
        self.preview_stamp=None;self.latest_error='';self.last_commit={};self.command_results=deque(maxlen=20);self.timings=deque(maxlen=100)
        self.recent_analyses=deque(maxlen=2)
        self.start_mono=time.monotonic();self.cpu_start=time.process_time();self.cpu_last=(self.start_mono,self.cpu_start)
        self.worker=threading.Thread(target=self.run,name='perception-worker',daemon=True)
        self.refresh_state()

    def refresh_state(self):
        snapshot=self.manager.snapshot()
        # HTTP status is compact; full raw evidence IDs remain in checkpoint/export.
        for r in snapshot['history']+snapshot['inventory']:
            r['face'].pop('support_point_ids',None)
            for e in r['last_evidence'].get('details',[]):e.pop('clear_point_ids',None)
        now=time.monotonic();cpu=time.process_time();dt=now-self.cpu_last[0]
        if dt>.5:self.cpu_pct=(cpu-self.cpu_last[1])/dt*100;self.cpu_last=(now,cpu)
        self.scheduler.check_timeout()
        state=dict(project='Odin Expected Cloud',source=self.source.health(),mode=self.mode,robot_ready=self.robot_ready,
            map=snapshot,scheduler=self.scheduler.snapshot(),error=self.latest_error,commit=self.last_commit,
            diagnostics=getattr(self,'diagnostics',{}),timing_ms=self.stage,process_cpu_percent=round(getattr(self,'cpu_pct',0),1),
            samples_collected=len(self.samples),policy=advise(self.manager,self.robot_ready),command_results=list(self.command_results),
            preview_stamp=self.preview_stamp,uptime_s=round(now-self.start_mono,1),config=self.manager.config,
            performance=dict(count=len(self.timings),p50_ms=float(np.percentile(self.timings,50)) if self.timings else None,p95_ms=float(np.percentile(self.timings,95)) if self.timings else None))
        with self.lock:self.state=state;self.state_bytes=json.dumps(jsonable(state),ensure_ascii=False,allow_nan=False).encode()

    def control(self,cmd):
        action=cmd.get('action')
        if action=='mode':
            if cmd.get('mode') not in ('paused','manual','realtime','epoch'):raise ValueError('invalid mode')
            if self.scheduler.session and self.scheduler.session['state'] in ('WAIT_OPERATOR','WAIT_STABLE','COLLECTING'):raise ValueError('cancel active session first')
            self.mode=cmd['mode'];self.samples=[];self.request=False
        elif action=='step':self.request=True
        elif action=='robot_ready':
            if not isinstance(cmd.get('ready'),bool):raise ValueError('ready must be boolean')
            self.robot_ready=cmd['ready']
            if not self.robot_ready:
                self.samples=[];self.scheduler.cancel()
                for r in self.manager.records.values():r.current_pose_ok=False
        elif action=='session':
            self.samples=[];self.scheduler.start(cmd['kind'],cmd.get('target_ids',[]),self.robot_ready);self.mode='paused'
        elif action=='ack':self.scheduler.acknowledge(cmd['view'])
        elif action=='cancel':self.scheduler.cancel();self.samples=[]
        elif action=='region':self.scheduler.configure_region(cmd['corners'])
        elif action=='wall_batch':self.manager.set_wall_batch(cmd['target_ids'])
        elif action=='config':
            config=dict(self.manager.config,**cmd['values']);checked=ActivePerceptionMap(config=config)
            self.manager.config=checked.config;self.manager.revision+=1;self.samples=[];self.scheduler.cancel()
        elif action=='resolve_identity':self.manager.resolve_identity(int(cmd['box_id']),cmd['reason']);self.samples=[];self.scheduler.cancel()
        elif action=='reinstate':self.manager.reinstate(int(cmd['old_id']),int(cmd['replacement_id']),cmd['reason']);self.samples=[];self.scheduler.cancel()
        elif action=='confirm_removed':
            if not self.args.live or not self.robot_ready:raise ValueError('live, observation-ready Odin required')
            box_id=int(cmd['box_id']);reason=cmd.get('reason','')
            if cmd.get('confirmation')!=f'移除 ID {box_id}':raise ValueError('physical removal confirmation does not match target ID')
            self.manager.save(self.out/f'before_operator_removal_{time.time_ns()}.json')
            result=self.manager.confirm_removed(box_id,reason,cmd.get('revision'),time.time())
            self.last_commit=result;self.samples=[];self.scheduler.cancel()
            self.manager.save(self.out/'checkpoint.json')
            if hasattr(self.source,'publish'):self.source.publish(self.manager.snapshot(),self.manager.delta)
            return dict(ok=True,**result)
        elif action=='save':self.manager.save(self.out/'checkpoint.json')
        elif action=='new_session':
            if self.manager.version is None and (self.out/'checkpoint.json').exists():
                import shutil
                shutil.copyfile(self.out/'checkpoint.json',self.out/f'previous_checkpoint_{time.time_ns()}.json')
            self.manager.save(self.out/f'archive_{time.time_ns()}.json')
            if hasattr(self.source,'reset_epoch'):self.source.reset_epoch()
            self.manager=ActivePerceptionMap(config=self.manager.config);self.scheduler=ActiveObservationScheduler(self.manager)
            self.last_analysis=None;self.recent_analyses.clear();self.samples=[];self.mode='paused';self.last_commit={};self.args.resume=False;self.resume_failure=''
        elif action=='restore':
            if self.last_analysis is None:raise ValueError('capture first to verify current provenance')
            self.manager.restore(self.out/'checkpoint.json',self.last_analysis.capture.version);self.samples=[];self.scheduler.cancel()
        elif action=='demo_hide' and isinstance(self.source,DemoSource):self.source.hidden=set(cmd.get('ids',[]))
        elif action=='wrist_verify':
            if self.last_analysis is None:raise ValueError('no current measurement')
            if time.monotonic()-self.last_analysis_at>2:raise ValueError('measurement stale')
            result=wrist_verify_batch(self.manager,int(cmd['target_id']),[(a.capture,a.observations) for a in self.recent_analyses],np.asarray(cmd['t_base_world']),float(cmd['base_stamp']))
            result['source_role']='ODIN_PROXY_NOT_HAND_EYE';return result
        else:raise ValueError('unknown action')
        return dict(ok=True,action=action)

    def encode_preview(self,image,stamp):
        if image is None:return
        view=image
        if image.shape[1]>1280:view=cv2.resize(image,(1280,int(image.shape[0]*1280/image.shape[1])))
        ok,data=cv2.imencode('.jpg',view,[cv2.IMWRITE_JPEG_QUALITY,90])
        if ok:
            with self.lock:self.raw_jpeg=data.tobytes();self.preview_stamp=stamp

    def render(self,a,depth):
        image=a.capture.image.copy();mapped={row['local_id']:row['box_id'] for row in self.manager.current_measurements if row['capture_id']==a.capture.capture_id}
        for j,o in enumerate(a.observations):
            color=(60,220,100) if o.position_ok else (0,180,255)
            if o.mask is not None:
                contours,_=cv2.findContours(o.mask.astype(np.uint8),cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE);cv2.drawContours(image,contours,-1,color,2)
            gid=mapped.get(o.local_id,a.assignments.get(j));label=f'ID {gid}' if gid is not None else f'local {o.local_id} {a.statuses.get(j,"")}'
            if o.bbox:cv2.putText(image,label,(int(o.bbox[0]),max(25,int(o.bbox[1])-8)),cv2.FONT_HERSHEY_SIMPLEX,.7,color,2)
        if image.shape[1]>1280:image=cv2.resize(image,(1280,int(image.shape[0]*1280/image.shape[1])))
        ok,data=cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,90])
        if ok:
            with self.lock:self.jpeg=data.tobytes()
        if depth is not None:
            small=cv2.resize(depth,(640,int(depth.shape[0]*640/depth.shape[1])),interpolation=cv2.INTER_NEAREST)
            valid=np.isfinite(small);grey=np.clip(np.nan_to_num(small,nan=0)/5*255,0,255).astype(np.uint8)
            colored=cv2.applyColorMap(grey,cv2.COLORMAP_TURBO);colored[~valid]=0
            _,encoded=cv2.imencode('.jpg',colored)
            with self.lock:self.depth_jpeg=encoded.tobytes()
        valid=np.isfinite(a.rays.points).all(1)&(a.rays.labels>0);indices=np.flatnonzero(valid)
        if len(indices)>8000:indices=indices[np.linspace(0,len(indices)-1,8000).astype(int)]
        expected={str(gid):pred['expected_points'][np.flatnonzero(pred['valid'])[::8]].round(4).tolist() for gid,pred in a.rays.predictions.items()}
        scene=dict(capture_id=a.capture.capture_id,stamp=a.capture.rgb_stamp,frame_id=a.capture.frame_id,
            points=a.rays.points[indices].round(4).tolist(),labels=a.rays.labels[indices].tolist(),expected=expected,
            world_from_camera=jsonable(a.capture.t_world_camera),revision=self.manager.revision)
        with self.lock:self.scene_bytes=json.dumps(scene,allow_nan=False).encode()

    def process(self,p):
        start=time.perf_counter();stage={}
        if self.resume_failure:raise ValueError(self.resume_failure)
        if isinstance(self.source,DemoSource):observations=self.source.observations;diagnostics=dict(source='SYNTHETIC',raw_count=len(observations));depth=None
        else:
            if self.frontend is None:self.frontend=OdinFrontend(self.root,self.args.device)
            t=time.perf_counter();observations,diagnostics,depth=self.frontend.detect(p);stage['detection_geometry']=(time.perf_counter()-t)*1000
        if self.args.resume and self.manager.version is None and (self.out/'checkpoint.json').exists():
            self.args.resume=False
            try:self.restore_for_capture(p,observations)
            except ValueError as exc:
                self.resume_failure=f'RESUME_REJECTED_NEW_SESSION_REQUIRED: {exc}';self.mode='paused';raise ValueError(self.resume_failure) from exc
        correction=getattr(self.source,'pending_reanchor',None)
        if correction is not None:self.manager.reanchor(correction);self.source.pending_reanchor=None
        # Saved NPZ captures lack scan-window TF history. They can test geometry
        # and rendering but cannot acquire live map mutation authority.
        readonly=not p.stable_capture
        t=time.perf_counter();a=self.manager.analyze(p,observations,read_only=readonly);stage['ray_and_association']=(time.perf_counter()-t)*1000
        if self.args.refine and self.frontend is not None and not readonly:
            t=time.perf_counter();extra,rois,candidates=self.frontend.refine(p,a);stage['roi_refinement']=(time.perf_counter()-t)*1000
            diagnostics['refinement_rois']=rois
            if extra:a=self.manager.analyze(p,observations+extra)
            a.geometric_candidates=candidates;diagnostics['geometric_candidates']=candidates
        self.last_analysis=a;self.recent_analyses.append(a);self.last_analysis_at=time.monotonic();t=time.perf_counter()
        if readonly:self.last_commit=dict(committed=False,reason='RECORDED_SCAN_STABILITY_UNVERIFIED')
        elif not self.robot_ready:self.last_commit=dict(committed=False,reason='ROBOT_NOT_READY')
        elif self.scheduler.wants_capture():
            self.scheduler.feed(a);self.last_commit=dict(committed=self.scheduler.session['map_commit_ok'],reason=self.scheduler.session['reason'])
        elif self.mode=='epoch':
            self.samples.append(a)
            if len(self.samples)>=self.manager.config['epoch_size']:
                self.last_commit=self.manager.commit(self.samples);self.samples=[]
        elif self.mode in ('realtime','manual') or self.request:self.last_commit=self.manager.commit([a],batch=False)
        else:self.last_commit=dict(committed=False,reason='PAUSED')
        self.request=False;stage['commit']=(time.perf_counter()-t)*1000
        self.diagnostics=dict(diagnostics,observations=[dict(local_id=o.local_id,status=a.statuses[j],position_ok=o.position_ok,orientation_ok=o.orientation_ok,
            full_measured_face=o.face.full_face_observed,body_center_status=o.geometry()['body_center_map'].status) for j,o in enumerate(a.observations)],
            ray_evidence={str(i):{k:v for k,v in e.items() if k!='clear_point_ids'} for i,e in a.rays.evidence.items()},conflicts=a.conflicts)
        t=time.perf_counter();self.render(a,depth);stage['render']=(time.perf_counter()-t)*1000
        stage['total']=(time.perf_counter()-start)*1000;self.stage=stage;self.timings.append(stage['total'])
        if self.last_commit.get('committed'):
            self.manager.save(self.out/'checkpoint.json')
            if hasattr(self.source,'publish'):self.source.publish(self.manager.snapshot(),self.manager.delta)
        self.refresh_state()
        report=dict(stage=stage,diagnostics=self.diagnostics,commit=self.last_commit,capture_id=p.capture_id,source=p.source,stable_capture=p.stable_capture)
        (self.out/'last_validation.json').write_text(json.dumps(jsonable(report),ensure_ascii=False,allow_nan=False))
        with (self.out/'metrics.jsonl').open('a') as f:
            f.write(json.dumps(jsonable(dict(capture_id=p.capture_id,stage=stage,raw_count=diagnostics.get('raw_count'),
                accepted_positions=sum(o.position_ok for o in observations),accepted_orientations=sum(o.orientation_ok for o in observations),
                commit=self.last_commit,inventory_count=len(self.manager.snapshot()['inventory']))),allow_nan=False)+'\n')

    def restore_for_capture(self,packet,observations):
        checkpoint=self.out/'checkpoint.json';data=json.loads(checkpoint.read_text());saved=tuple(data['version'])
        if packet.version!=saved:
            identity=getattr(self.source,'driver_identity',None)
            if not (self.args.live and self.args.resume_driver_continuity and not self.args.start_driver
                    and packet.source=='ODIN_CLOUD_RAW' and identity is not None
                    and packet.version[:1]+packet.version[2:]==saved[:1]+saved[2:]
                    and identity['start_wall']<=data['last_stamp']+1.
                    and 0<packet.rgb_stamp-data['last_stamp']<120.):
                raise ValueError('checkpoint version or driver continuity mismatch')
            previous=ActivePerceptionMap(config=self.manager.config);previous.restore(checkpoint,saved)
            matches=set()
            for o in observations:
                if not o.position_ok or not o.orientation_ok or not o.face.trusted:continue
                eligible=[(previous._cost(r,o),gid) for gid,r in previous.records.items()
                          if r.life_state=='CONFIRMED']
                eligible=[(cost,gid) for cost,gid in eligible if cost is not None and cost<.65]
                if len(eligible)==1:matches.add(eligible[0][1])
            if len(matches)<2:raise ValueError('cannot verify at least two unchanged global boxes')
            packet.map_epoch=saved[1];self.source.map_epoch=saved[1]
        self.manager.restore(checkpoint,packet.version)

    def preview_loop(self):
        # Raw preview must not share the worker thread: process() blocks it for
        # ~0.7 s per capture, which froze the preview for most of every second.
        while not self.stop.is_set():
            try:
                image,stamp=self.source.preview();self.encode_preview(image,stamp)
            except Exception as exc:self.latest_error=f'{type(exc).__name__}: {exc}'
            self.stop.wait(1/15)

    def run(self):
        last_process=0.;last_state=0.
        threading.Thread(target=self.preview_loop,name='preview',daemon=True).start()
        while not self.stop.is_set():
            try:
                while not self.commands.empty():
                    cmd=self.commands.get_nowait()
                    try:result=self.control(cmd)
                    except Exception as exc:result=dict(ok=False,error=str(exc),action=cmd.get('action'))
                    result['request_id']=cmd['request_id'];self.command_results.append(result)
                now=time.monotonic()
                wanted=self.mode in ('realtime','epoch') or self.request or self.scheduler.wants_capture()
                if wanted and now-last_process>=1/self.args.hz:
                    p=self.source.next_capture()
                    # Clock the cadence from the start of processing so --hz is a
                    # true rate cap; timing from the end added the full ~0.7 s
                    # process() cost on top of 1/hz every frame.
                    if p is not None:last_process=now;self.process(p);self.latest_error=''
                # Expired outputs never remain executable just because processing stopped.
                if self.last_analysis is not None and now-self.last_analysis_at>2:
                    for r in self.manager.records.values():r.current_pose_ok=False
                if now-last_state>.25:self.refresh_state();last_state=now
            except Exception as exc:
                self.latest_error=f'{type(exc).__name__}: {exc}';self.samples=[]
                traceback.print_exc();self.refresh_state();last_process=time.monotonic()
            self.stop.wait(.02)


def make_handler(project):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send(self,data,kind='application/json',status=200):
            self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(data)))
            self.send_header('Cache-Control','no-store');self.end_headers()
            try:self.wfile.write(data)
            except (BrokenPipeError,ConnectionResetError):pass
        def do_GET(self):
            path=urlsplit(self.path).path
            with project.lock:
                data={'/api/state':project.state_bytes,'/api/scene':project.scene_bytes,
                    '/image.jpg':project.jpeg,'/raw.jpg':project.raw_jpeg,'/depth.jpg':project.depth_jpeg}.get(path)
            if path in ('/image.jpg','/raw.jpg','/depth.jpg'):
                return self.send(data or b'', 'image/jpeg',200 if data else 204)
            if data is not None:return self.send(data)
            files={'/':ASSETS/'index.html','/app.js':ASSETS/'app.js','/style.css':ASSETS/'style.css',
                '/vendor/three.module.js':project.root/'runtime/odin1/map_viewer_deps/node_modules/three/build/three.module.js',
                '/vendor/OrbitControls.js':project.root/'runtime/odin1/map_viewer_deps/node_modules/three/examples/jsm/controls/OrbitControls.js'}
            if path=='/api/export':return self.send(json.dumps(project.state['map'],ensure_ascii=False,allow_nan=False).encode())
            file=files.get(path)
            if not file or not file.exists():return self.send(b'{"error":"not found"}',status=404)
            kind='text/html; charset=utf-8' if file.suffix=='.html' else 'text/css' if file.suffix=='.css' else 'text/javascript'
            self.send(file.read_bytes(),kind)
        def do_POST(self):
            if urlsplit(self.path).path!='/api/control':return self.send(b'{}',status=404)
            # Same-origin browser controls; no shell command or arbitrary path API.
            origin=self.headers.get('Origin')
            if origin and urlsplit(origin).netloc!=self.headers.get('Host'):return self.send(b'{"error":"origin mismatch"}',status=403)
            try:
                length=int(self.headers.get('Content-Length','0'))
                if length<=0 or length>16384:raise ValueError('invalid request size')
                cmd=json.loads(self.rfile.read(length))
                if not isinstance(cmd,dict):raise ValueError('object required')
                if cmd.get('action')=='confirm_removed' and self.client_address[0] not in ('127.0.0.1','::1'):
                    return self.send(b'{"error":"physical removal confirmation is local-only"}',status=403)
                cmd['request_id']=str(time.time_ns());project.commands.put_nowait(cmd)
                self.send(json.dumps(dict(queued=True,request_id=cmd['request_id'])).encode(),status=202)
            except (ValueError,queue.Full) as exc:self.send(json.dumps(dict(error=str(exc))).encode(),status=400)
    return Handler


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group(required=True);group.add_argument('--live',action='store_true');group.add_argument('--replay');group.add_argument('--demo',action='store_true')
    parser.add_argument('--root',default=str(ROOT));parser.add_argument('--output',default=str(ROOT/'runtime/results/odin_expected_cloud'))
    parser.add_argument('--port',type=int,default=8770);parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--mode',choices=('paused','manual','realtime','epoch'),default='epoch');parser.add_argument('--hz',type=float,default=5.)
    parser.add_argument('--world-frame',default='map');parser.add_argument('--device',default='0');parser.add_argument('--refine',action=argparse.BooleanOptionalAction,default=True)
    parser.add_argument('--config');parser.add_argument('--start-driver',action='store_true');parser.add_argument('--duration',type=float,default=0);parser.add_argument('--resume',action='store_true');parser.add_argument('--resume-driver-continuity',action='store_true')
    parser.add_argument('--return-semantics',choices=('UNKNOWN','FIRST_OPAQUE_RETURN','STRONGEST_RETURN','MULTIPLE_RETURN'),default='UNKNOWN')
    args=parser.parse_args()
    if not .1<=args.hz<=5:parser.error('hz must be 0.1..5')
    if args.start_driver and not args.live:parser.error('--start-driver requires --live')
    if args.resume_driver_continuity and not (args.live and args.resume and not args.start_driver):parser.error('continuity requires --live --resume and reuse of the already running driver')
    cv2.setNumThreads(2);out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    lock=open(Path(args.root)/'runtime/odin1/expected_cloud.lock','a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:parser.exit(2,'Expected-cloud project is already running. Open its browser; do not start a second instance.\n')
    driver=None;log=None;source=None;server=None
    try:
        if args.start_driver:
            owners=subprocess.run(['pgrep','-f','/lib/odin_ros_driver/[h]ost_sdk_sample'],capture_output=True,text=True)
            if owners.returncode!=0:
                log=open(out/'driver.log','a');driver=subprocess.Popen([str(Path(args.root)/'tools/odin1/run_odin1.sh')],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        if args.live:
            from .live import OdinLiveSource
            source=OdinLiveSource(args.root,args.world_frame,args.return_semantics)
        else:source=ReplaySource(args.replay) if args.replay else DemoSource()
        project=Project(args,source);server=ThreadingHTTPServer((args.host,args.port),make_handler(project));server.timeout=.5
        project.worker.start()
        def stop(*_):project.stop.set()
        signal.signal(signal.SIGINT,stop);signal.signal(signal.SIGTERM,stop)
        print(f'Expected Cloud UI: http://{args.host}:{args.port} | {"LIVE ODIN" if args.live else "RECORDED GEOMETRY ONLY" if args.replay else "SYNTHETIC TEST"}',flush=True)
        deadline=time.monotonic()+args.duration if args.duration else float('inf')
        while not project.stop.is_set() and time.monotonic()<deadline:server.handle_request()
        project.stop.set();project.worker.join(timeout=30);project.manager.save(out/'shutdown_checkpoint.json')
        (out/'validation_summary.json').write_text(json.dumps(jsonable(project.state),ensure_ascii=False,allow_nan=False))
    finally:
        if server:server.server_close()
        if source:source.close()
        if driver and driver.poll() is None:
            os.killpg(driver.pid,signal.SIGINT)
            try:driver.wait(timeout=8)
            except subprocess.TimeoutExpired:os.killpg(driver.pid,signal.SIGTERM);driver.wait(timeout=5)
        if log:log.close()
        lock.close()


if __name__=='__main__':main()
