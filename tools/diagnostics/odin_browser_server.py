#!/usr/bin/env python3
"""Local operator UI; controls only its own detector, reuses the Odin driver."""
import argparse
import fcntl
import json
import math
import mimetypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import yaml
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'src/robot_perception'))
from camera_unload_perception.algorithms.persistent_carton_map import DEFAULTS


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def processes(fragment):
    result = []
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            args = (path / 'cmdline').read_bytes().split(b'\0')
            if any(fragment.encode() in arg for arg in args):
                result.append(int(path.name))
        except OSError:
            pass
    return result


class Application:
    def __init__(self, args):
        self.args = args
        self.out = ROOT / 'runtime/results/odin_browser'
        self.out.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.worker = self.driver = None
        self.worker_started_at=0.
        self.error = ''
        self.control = dict(revision=0, enabled=False, mode='manual', request_id=0,
                            reset_id=0, confidence=.70, inference_size=640, config=dict(DEFAULTS))
        saved = read_json(self.out / 'control.json')
        self.control.update(saved)
        self.control['config']=dict(DEFAULTS,**{key:value for key,value in saved.get('config',{}).items() if key in DEFAULTS})
        self.control['enabled'] = False
        self.write_control()

    def write_control(self):
        self.control['revision'] += 1
        temp = self.out / 'control.tmp'
        temp.write_text(json.dumps(self.control, allow_nan=False))
        temp.replace(self.out / 'control.json')

    def start(self):
        with self.lock:
            if self.worker and self.worker.poll() is None:
                return
            foreign = processes('/tools/diagnostics/odin_persistent_box_test.py')
            if foreign:
                raise ValueError(f'Existing detector PID {foreign}; stop that detector before starting this session')
            if self.args.localization_source == 'odin':
                metadata=read_json(self.args.map/'metadata.json')
                config=yaml.safe_load((ROOT/'odin_v013_ws/src/odin_ros_driver/config/control_command.yaml').read_text())['register_keys']
                if config.get('custom_map_mode')!=2 or config.get('relocalization_map_abs_path')!=metadata.get('source'):
                    raise ValueError('Displayed map must match the Odin relocalization map configuration (mode 2)')
            if self.args.localization_source == 'odin' and not processes('odin_ros_driver/lib/odin_ros_driver/host_sdk_sample'):
                log = open(self.out / 'odin.log', 'ab', buffering=0)
                self.driver = subprocess.Popen([str(ROOT/'tools/odin1/run_odin1.sh')], cwd=ROOT,
                                               stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                log.close()
            log = open(self.out / 'worker.log', 'ab', buffering=0)
            self.worker_started_at=time.time()
            log.write(f'\n--- detector session {self.worker_started_at} ---\n'.encode())
            self.worker = subprocess.Popen([sys.executable, str(Path(__file__).with_name('odin_persistent_box_test.py')),
                '--mode', self.control['mode'], '--output', str(self.out),
                '--world-frame',self.args.world_frame,'--base-frame',self.args.base_frame,
                '--localization-source',self.args.localization_source], cwd=ROOT,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            log.close()
            self.error = ''

    @staticmethod
    def stop_process(process):
        if process and process.poll() is None:
            # Only processes launched with our own session are signalled.
            os.killpg(process.pid, signal.SIGINT)
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)

    def close(self):
        self.stop_process(self.worker)
        self.stop_process(self.driver)

    def state(self):
        latest = read_json(self.out/'latest.json')
        heartbeat = read_json(self.out/'heartbeat.json')
        running = bool(self.worker and self.worker.poll() is None)
        if running and heartbeat.get('stamp',0)<self.worker_started_at:
            heartbeat={}
        if latest.get('session_id') != heartbeat.get('session_id'):
            latest = {}
        return dict(latest=latest, heartbeat=heartbeat, control=self.control,
            running=running, camera_live=running and time.time()-heartbeat.get('stamp',0)<3,
            driver_pids=processes('odin_ros_driver/lib/odin_ros_driver/host_sdk_sample'),
            worker_pid=self.worker.pid if running else None, error=self.error,
            worker_exit=self.worker.poll() if self.worker else None,
            map=read_json(self.args.map/'metadata.json'))

    def command(self, payload):
        with self.lock:
            action = payload.get('action')
            if action in ('start', 'once'):
                self.start()
                self.control['enabled'] = True
                if action == 'once':
                    self.control['request_id'] += 1
            elif action == 'pause':
                self.control['enabled'] = False
            elif action == 'stop':
                self.control['enabled']=False
                self.stop_process(self.worker)
                self.worker=None
            elif action == 'reset':
                if payload.get('confirm') is not True:
                    raise ValueError('Map reset requires confirmation')
                self.control['reset_id'] += 1
            elif action == 'configure':
                candidate=dict(self.control)
                if 'inference_size' in payload:
                    if payload['inference_size'] not in (640,960,1280,1600):raise ValueError('Invalid inference size')
                    candidate['inference_size']=payload['inference_size']
                if 'mode' in payload:
                    if payload['mode'] not in ('realtime','manual','epoch'):
                        raise ValueError('Invalid mode')
                    candidate['mode'] = payload['mode']
                if 'confidence' in payload:
                    value = float(payload['confidence'])
                    if not .70 <= value <= .99:
                        raise ValueError('P01 confidence must be 0.70..0.99')
                    candidate['confidence'] = value
                updated = dict(self.control['config'])
                for key,value in payload.get('config', {}).items():
                    if key not in DEFAULTS or isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:
                        raise ValueError(f'Invalid parameter: {key}')
                    if isinstance(DEFAULTS[key],int) and (int(value)!=value or value>100000):
                        raise ValueError(f'Expected bounded integer: {key}')
                    if ('ratio' in key or 'probability' in key or key in ('center_alpha','normal_alpha','size_alpha') or 'iou' in key or 'overlap' in key) and value>1:
                        raise ValueError(f'Expected (0,1]: {key}')
                    updated[key] = int(value) if isinstance(DEFAULTS[key],int) else float(value)
                if updated['mask_erode_px']>31 or updated['max_points_per_box']>1500:
                    raise ValueError('Mask erosion <=31; RANSAC sample <=1500')
                if not 1<=updated['depth_support_cell_px']<=16:raise ValueError('Depth support cell must be 1..16 pixels')
                if updated['min_observations']<3 or updated['max_points_per_box']<200:
                    raise ValueError('Confirmation needs >=3 observations; plane sample >=200')
                if not 3<=updated['epoch_frames']<=8 or not 2<=updated['epoch_min_support']<=updated['epoch_frames']:
                    raise ValueError('Epoch requires 3..8 frames and >=2 independent supporting frames')
                if not 1<=updated['epoch_timeout_s']<=60:
                    raise ValueError('Epoch timeout must be 1..60 seconds')
                if updated['replacement_support_frames']<3 or updated['replacement_min_layer_m']<=updated['max_depth_residual_m']:
                    raise ValueError('Replacement requires >=3 observations and layer separation beyond association depth gate')
                if updated['reacquire_support_frames']<3 or updated['reacquire_min_iou']<.75:
                    raise ValueError('Reacquisition requires >=3 observations and IoU >=0.75')
                candidate['config'] = updated
                self.control=candidate
            elif action == 'snapshot':
                target = self.out/'snapshots'/str(time.time_ns())
                target.mkdir(parents=True)
                import shutil
                for name in ('live.jpg','debug.jpg','latest.json','live_cloud.json','persistent_map.json','control.json','active_boxes.json','map_delta.json'):
                    source = self.out/name
                    if source.exists(): shutil.copy2(source,target/name)
                return {'saved':str(target)}
            else:
                raise ValueError('Unknown action')
            self.error=''
            self.write_control()
            return self.state()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--map',type=Path,default=ROOT/'runtime/odin1/maps/map_20260909_155803_view')
    parser.add_argument('--world-frame',default='map')
    parser.add_argument('--base-frame',default='base_link')
    parser.add_argument('--localization-source',choices=('odin','tf'),default='odin')
    parser.add_argument('--autostart',action='store_true')
    args=parser.parse_args()
    lock=open(ROOT/'runtime/odin1/browser_server.lock','a')
    try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError: raise SystemExit('Dashboard already running: http://127.0.0.1:8765')
    app=Application(args)
    assets=ROOT/'tools/odin1/browser'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass

        def send(self,body,kind='application/json',status=200):
            if not isinstance(body,bytes): body=json.dumps(body,ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type',kind)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers()
            try: self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError): pass

        def do_GET(self):
            path=urlsplit(self.path).path
            if path=='/api/state': return self.send(app.state())
            if args.localization_source=='tf' and path.startswith('/map/'):
                # Test-map points have no known transform into a deployed
                # robot's map frame. Do not silently draw them in that frame.
                if path=='/map/metadata.json':
                    return self.send(dict(bounds=[[-1,-1,-1],[1,1,1]],points=0,keyframes=0,
                        source='Robot TF: '+args.world_frame))
                if path in ('/map/points.f32','/map/trajectory.f32'):
                    return self.send(b'',kind='application/octet-stream')
            if path=='/api/log':
                try:
                    with open(app.out/'worker.log','rb') as file:
                        file.seek(max(0,os.fstat(file.fileno()).st_size-12000))
                        return self.send({'log':file.read().decode(errors='replace')})
                except OSError: return self.send({'log':''})
            routes={'/':assets/'index.html','/app.js':assets/'app.bundle.js','/app.css':assets/'app.css',
                '/map/points.f32':args.map/'points.f32','/map/trajectory.f32':args.map/'trajectory.f32',
                '/map/metadata.json':args.map/'metadata.json'}
            for name in ('live.jpg','debug.jpg','live_cloud.json','persistent_map.json','events.jsonl','active_boxes.json','map_delta.json','current_measurements.json'):
                routes['/data/'+name]=app.out/name
            target=routes.get(path)
            if not target or not target.is_file(): return self.send({'error':'Not ready'},status=404)
            self.send(target.read_bytes(),mimetypes.guess_type(str(target))[0] or 'application/octet-stream')

        def do_POST(self):
            if urlsplit(self.path).path!='/api/control': return self.send({'error':'Not found'},status=404)
            origin=self.headers.get('Origin')
            if origin and urlsplit(origin).netloc!=self.headers.get('Host'):
                return self.send({'error':'Foreign origin'},status=403)
            try:
                length=int(self.headers.get('Content-Length',0))
                if not 0<length<=65536: raise ValueError('Invalid request size')
                payload=json.loads(self.rfile.read(length))
                self.send(app.command(payload))
            except (ValueError,TypeError,OSError) as exc:
                app.error=str(exc);self.send({'error':str(exc)},status=400)

    server=ThreadingHTTPServer((args.host,args.port),Handler)
    print(f'Odin dashboard: http://{args.host}:{args.port}',flush=True)
    try:
        if args.autostart: app.start()
        server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close();app.close()


if __name__=='__main__': main()
