#!/usr/bin/env python3
"""Independent, bounded live viewer of Odin raw XYZ and RGB, without detection/TF."""
import argparse
from collections import deque
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import signal
import struct
import subprocess
import threading
import time

import cv2
import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image, PointCloud2
from sensor_msgs_py import point_cloud2
import yaml

ROOT = Path('/home/nvidia/perception_domain_nx')
ASSETS = ROOT / 'tools/odin1/depth_viewer'


def stamp(message):
    return message.header.stamp.sec + message.header.stamp.nanosec * 1e-9


def driver_pids():
    result = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if b'/lib/odin_ros_driver/host_sdk_sample\x00' in (proc/'cmdline').read_bytes():
                result.append(int(proc.name))
        except (OSError, PermissionError):
            pass
    return result


class DepthViewer(Node):
    def __init__(self):
        super().__init__('odin_raw_depth_viewer')
        self.images = deque(maxlen=20)
        self.cloud = None
        self.last_stamp = None
        self.lock = threading.Lock()
        self.packet = b''
        self.rgb = b''
        self.status = dict(ready=False, error='', received_at=0.)
        self.calib_mtime = None
        self.calib = None
        self.create_subscription(Image, '/odin1/image/undistorted', self.images.append, qos_profile_sensor_data)
        self.create_subscription(PointCloud2, '/odin1/cloud_raw', self.on_cloud, qos_profile_sensor_data)
        self.create_timer(.5, self.update)

    def on_cloud(self, msg):
        self.cloud = msg

    def update(self):
        cloud = self.cloud
        if cloud is None or stamp(cloud) == self.last_stamp:
            return
        started = time.monotonic()
        try:
            path = ROOT/'runtime/odin1/calibration/calib.yaml'
            mtime = path.stat().st_mtime_ns
            if mtime != self.calib_mtime:
                self.calib = yaml.safe_load(path.read_text())
                self.calib_mtime = mtime
            data = point_cloud2.read_points(cloud, skip_nans=False)
            points = np.column_stack([data[key].reshape(-1) for key in ('x', 'y', 'z', 'confidence')]).astype('<f4')
            if len(points) != cloud.width * cloud.height:
                raise ValueError('Unexpected organized cloud dimensions')
            # PointCloud2Modifier.resize flattens the vendor ROS message to
            # 49152 x 1. The SDK still writes row-major 256-column DTOF data
            # (see publish_raw_cloud's i / 256 row and six-row timing groups).
            width, height = cloud.width, cloud.height
            if height == 1 and len(points) == 256 * 192:
                width, height = 256, 192
            rgb = min(self.images, key=lambda msg: abs(stamp(msg)-stamp(cloud))) if self.images else None
            rgb_dt = abs(stamp(rgb)-stamp(cloud)) if rgb is not None else None
            # A stale RGB overlay is misleading. Native depth remains usable
            # even while a synchronized image has not arrived yet.
            rgb_valid = rgb is not None and rgb_dt <= .020
            encoded = b''
            if rgb_valid:
                image = np.ndarray((rgb.height, rgb.width, 3), dtype=np.uint8,
                    buffer=bytes(rgb.data), strides=(rgb.step, 3, 1))
                if rgb.encoding == 'rgb8':
                    image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
                image = cv2.resize(image, (800, round(rgb.height*800/rgb.width)), interpolation=cv2.INTER_AREA)
                ok, jpg = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 85])
                if not ok:
                    raise ValueError('JPEG encode failed')
                encoded = jpg.tobytes()
            cam = self.calib['cam_0']
            info = dict(ready=True, error='', stamp=stamp(cloud), received_at=time.time(),
                width=width, height=height, ros_width=cloud.width, ros_height=cloud.height,
                frame=cloud.header.frame_id,
                rgb_valid=rgb_valid, rgb_dt_ms=rgb_dt*1000 if rgb_dt is not None else None,
                rgb_width=int(rgb.width) if rgb is not None else 1600,
                rgb_height=int(rgb.height) if rgb is not None else 1296,
                intrinsics=dict(fx=cam['A11'], fy=cam['A22'], cx=cam['u0'], cy=cam['v0'], skew=cam.get('A12', 0)),
                tcl=self.calib['Tcl_0'], points=len(points),
                processing_ms=(time.monotonic()-started)*1000,
                # Format: u32 header length, JSON, N*(x,y,z,confidence) f32 LE,
                # then the exact paired JPEG. One HTTP response = one frame.
                cloud_bytes=points.nbytes, jpeg_bytes=len(encoded))
            header = json.dumps(info, allow_nan=False).encode()
            packet = struct.pack('<I', len(header)) + header + points.tobytes() + encoded
            with self.lock:
                self.packet, self.status = packet, info
            self.last_stamp = stamp(cloud)
        except Exception as exc:
            with self.lock:
                self.status = dict(self.status, error=str(exc))
            self.get_logger().warning(str(exc))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--start-camera', action='store_true')
    args = parser.parse_args()
    rclpy.init()
    node = DepthViewer()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            path = self.path.split('?')[0]
            if path == '/api/frame':
                with node.lock:
                    body = node.packet
                kind = 'application/octet-stream'
                status = 200 if body else 503
            elif path == '/api/status':
                with node.lock:
                    body = json.dumps(dict(node.status, driver_pids=driver_pids())).encode()
                kind, status = 'application/json', 200
            else:
                target = {'/':'index.html', '/app.js':'app.js', '/app.css':'app.css'}.get(path)
                if not target:
                    self.send_error(404)
                    return
                body = (ASSETS/target).read_bytes()
                kind = {'index.html':'text/html; charset=utf-8', 'app.js':'text/javascript', 'app.css':'text/css'}[target]
                status = 200
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    driver = None
    try:
        if args.start_camera and not driver_pids():
            out = ROOT/'runtime/results/odin_depth_viewer'
            out.mkdir(parents=True, exist_ok=True)
            with open(out/'driver.log', 'ab', buffering=0) as log:
                driver = subprocess.Popen([str(ROOT/'tools/odin1/run_odin1.sh')], cwd=ROOT,
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        print(f'Odin depth viewer: http://127.0.0.1:{args.port}', flush=True)
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        # Only stop a driver this viewer started; never stop a shared driver.
        if driver is not None and driver.poll() is None:
            import os
            os.killpg(driver.pid, signal.SIGINT)
            try:
                driver.wait(timeout=8)
            except subprocess.TimeoutExpired:
                print('Driver still shutting down; no forced USB reset.', flush=True)


if __name__ == '__main__':
    main()
