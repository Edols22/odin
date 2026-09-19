#!/usr/bin/env python3
"""MCAP support capture with actual raw-cloud-derived sparse depth and calibration."""
import argparse
from array import array
from collections import deque
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import Image, PointCloud2
from sensor_msgs_py import point_cloud2
import yaml

ROOT = Path('/home/nvidia/perception_domain_nx')
DEPTH = '/odin1/diagnostics/depth_registered'
TOPICS = ['/odin1/imu', '/odin1/odometry', '/odin1/cloud_raw',
          '/odin1/cloud_slam', '/odin1/image/compressed', DEPTH]


def stamp(msg):
    return msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9


class SparseDepth(Node):
    def __init__(self):
        super().__init__('odin_support_sparse_depth')
        self.images = deque(maxlen=20)
        self.clouds = deque(maxlen=30)
        self.last = -1.
        self.published = 0
        self.unpaired = 0
        self.errors = 0
        self.calib = None
        qos = QoSProfile(depth=30, reliability=ReliabilityPolicy.RELIABLE)
        self.pub = self.create_publisher(Image, DEPTH, qos)
        self.create_subscription(Image, '/odin1/image/undistorted', self.image, qos)
        self.create_subscription(PointCloud2, '/odin1/cloud_raw', self.clouds.append, qos)
        self.create_timer(.02, self.tick)

    def image(self, msg):
        # Retain only image metadata; no decoding or YOLO inference.
        self.images.append((stamp(msg), msg.width, msg.height))

    def tick(self):
        if not self.clouds or not self.images:
            return
        path = ROOT/'runtime/odin1/calibration/calib.yaml'
        if self.calib is None:
            if not path.exists():
                return
            self.calib = yaml.safe_load(path.read_text())
        while self.clouds:
            cloud = self.clouds[0]
            t = stamp(cloud)
            if t <= self.last:
                self.clouds.popleft()
                continue
            rgb_t, w, h = min(self.images, key=lambda item:abs(item[0]-t))
            if abs(rgb_t-t) > .020:
                if self.images[-1][0] < t+.020:
                    return
                self.clouds.popleft(); self.unpaired += 1
                continue
            self.clouds.popleft(); self.last = t
            try:
                raw = point_cloud2.read_points(cloud, field_names=('x','y','z'), skip_nans=True)
                points = np.column_stack([raw[key].reshape(-1) for key in ('x','y','z')])
                transform = np.array(self.calib['Tcl_0']).reshape(4,4)
                camera = points @ transform[:3,:3].T + transform[:3,3]
                valid = np.isfinite(camera).all(1) & (camera[:,2]>.20) & (camera[:,2]<8.)
                camera = camera[valid]; k = self.calib['cam_0']
                # Exactly the persistence diagnostic's _depth_image convention:
                # original 1600x1296 pinhole, rounded pixels, nearest z, no fill.
                u = np.rint(k['A11']*camera[:,0]/camera[:,2]+k['u0']).astype(np.int32)
                v = np.rint(k['A22']*camera[:,1]/camera[:,2]+k['v0']).astype(np.int32)
                keep = (u>=0)&(u<w)&(v>=0)&(v<h)
                depth = np.full(h*w, np.inf, np.float32)
                np.minimum.at(depth, v[keep].astype(np.int64)*w+u[keep], camera[keep,2])
                depth[~np.isfinite(depth)] = np.nan
                msg = Image(); msg.header.stamp = cloud.header.stamp
                msg.header.frame_id = 'odin_diagnostic_camera_optical'
                msg.height=h;msg.width=w;msg.encoding='32FC1';msg.is_bigendian=False;msg.step=w*4
                # ROS Image's setter otherwise validates 8M Python byte values
                # per frame. A typed byte array preserves bytes without that loop.
                msg.data=array('B',depth.tobytes()); self.pub.publish(msg);self.published+=1
            except Exception as exc:
                self.errors+=1; self.get_logger().error(f'Depth projection: {exc}')


def stop(process):
    if process is not None and process.poll() is None:
        os.killpg(process.pid, signal.SIGINT)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            print(f'PID {process.pid} still flushing; not force-killing data writer.', flush=True)
            raise RuntimeError('Process did not finish its graceful shutdown')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds',type=float,default=0,help='0: record until Ctrl+C')
    args=parser.parse_args()
    from rosbag2_py import get_registered_writers
    if 'mcap' not in get_registered_writers():
        raise SystemExit('Missing MCAP plugin: sudo apt install ros-humble-rosbag2-storage-mcap')
    lock=open(ROOT/'runtime/odin1/support_record.lock','a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise SystemExit('A support recording is already running')
    out=ROOT/'runtime/datasets'/('odin_support_'+datetime.now().strftime('%Y%m%d_%H%M%S'))
    out.mkdir(parents=True,exist_ok=False)
    print(f'数据目录：{out}',flush=True)
    shutil.copy2(ROOT/'tools/odin1/support_bag_qos.yaml',out/'qos.yaml')
    shutil.copy2(ROOT/'tools/odin1/support_bag_mcap.yaml',out/'mcap_storage.yaml')
    shutil.copy2(ROOT/'odin_v013_ws/src/odin_ros_driver/config/control_command.yaml',out/'control_command.yaml')
    shutil.copy2(Path(__file__),out/'record_odin_support_bag.py')
    driver=bag=None
    rclpy.init();node=SparseDepth()
    status='preparing'; started=None
    # Ctrl+C must allow bag indexes and metadata to be written before exit.
    interrupted=False
    def interrupt(*_):
        nonlocal interrupted
        interrupted=True
    signal.signal(signal.SIGINT,interrupt);signal.signal(signal.SIGTERM,interrupt)
    try:
        from odin_depth_viewer import driver_pids
        if not driver_pids():
            with open(out/'driver.log','ab',buffering=0) as log:
                driver=subprocess.Popen([str(ROOT/'tools/odin1/run_odin1.sh')],cwd=ROOT,
                    stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        deadline=time.monotonic()+90
        while not interrupted:
            rclpy.spin_once(node,timeout_sec=.05)
            missing=[topic for topic in TOPICS if not node.count_publishers(topic)]
            if not missing and node.published:
                break
            if time.monotonic()>deadline:
                raise RuntimeError(f'Topics not ready: {missing}; depth frames={node.published}')
        if interrupted:
            return
        shutil.copy2(ROOT/'runtime/odin1/calibration/calib.yaml',out/'calib.yaml')
        text='''Odin support reproduction dataset\n
Storage: ROS2 MCAP with explicit reliable QoS; IMU subscriber depth 4000.
Original requested topics are recorded, plus /tf and /tf_static for diagnosis.
Our perception code did NOT subscribe to a vendor depth image topic. It formed
a sparse registered depth image from /odin1/cloud_raw in memory. For this bag
the same projection is published as /odin1/diagnostics/depth_registered.
This is a derived diagnostic Image (32FC1, metres, camera optical Z), not an
independent sensor measurement or vendor depth-completion output. Invalid pixels
are NaN. Intrinsics/Tcl are in calib.yaml; no hole filling or depth interpolation.
Header stamp equals source cloud stamp; RGB metadata matched within 20 ms.
The optical frame is defined by Tcl_0 applied to lidar. It is not map coordinates.
The optional depth viewer renders at half resolution and includes skew, whereas
this diagnostic bag reproduces the full-resolution persistent mapper projection.
No YOLO/persistent mapper or browser is required during recording.
Raw compressed RGB is distorted; rectify with calib.yaml before comparison.
'''
        (out/'README.txt').write_text(text)
        command=['ros2','bag','record','-s','mcap','--max-cache-size','268435456',
                 '--storage-config-file',str(out/'mcap_storage.yaml'),
                 '--qos-profile-overrides-path',str(out/'qos.yaml'),
                 '-o',str(out/'bag'),*TOPICS,'/tf','/tf_static']
        (out/'record_command.json').write_text(json.dumps(command,indent=2))
        with open(out/'recorder.log','ab',buffering=0) as log:
            bag=subprocess.Popen(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        started=time.monotonic();status='recording'
        print('正在录制 MCAP。保持箱子和相机不动，反复将手臂放入右侧再移开。按 Ctrl+C 结束并保存。',flush=True)
        while not interrupted and (args.seconds<=0 or time.monotonic()-started<args.seconds):
            rclpy.spin_once(node,timeout_sec=.05)
            if bag.poll() is not None:
                raise RuntimeError(f'Recorder exited: {bag.returncode}; see {out}/recorder.log')
            if shutil.disk_usage(out).free < 5*1024**3:
                raise RuntimeError('Free disk below 5 GiB; stopping cleanly')
        status='finished'
    finally:
        stop(bag)
        summary=dict(status=status,elapsed_seconds=time.monotonic()-started if started else 0,
                     depth_published=node.published,unpaired_depth=node.unpaired,projection_errors=node.errors)
        (out/'capture_status.json').write_text(json.dumps(summary,indent=2))
        if (out/'bag/metadata.yaml').exists():
            metadata=yaml.safe_load((out/'bag/metadata.yaml').read_text())['rosbag2_bagfile_information']
            counts={entry['topic_metadata']['name']:entry['message_count'] for entry in metadata['topics_with_message_count']}
            (out/'topic_counts.json').write_text(json.dumps(counts,indent=2))
            print('录制条数：'+json.dumps(counts,ensure_ascii=False),flush=True)
            absent=[topic for topic in TOPICS if counts.get(topic,0)==0]
            if absent:print('警告：以下必要话题没有消息：'+str(absent),flush=True)
            subprocess.run(['ros2','bag','info',str(out/'bag')],check=False)
        node.destroy_node()
        if rclpy.ok():rclpy.shutdown()
        stop(driver)
        hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [out/'calib.yaml',out/'qos.yaml'] if p.exists()}
        (out/'checksums.json').write_text(json.dumps(hashes,indent=2))
        print(f'保存完成：{out}（交给客服整个文件夹）',flush=True)


if __name__=='__main__':main()
