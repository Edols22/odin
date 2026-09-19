"""Bounded Odin ROS input. TF runs separately from inference and HTTP."""
from collections import deque
import hashlib
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid
import numpy as np
import yaml
from scipy.spatial.transform import Rotation
from .schema import CapturePacket, transform_ok
from .odin_adapter import pointcloud2_arrays


def pose_matrix(msg):
    q=msg.transform.rotation;t=msg.transform.translation
    a=np.eye(4);a[:3,:3]=Rotation.from_quat([q.x,q.y,q.z,q.w]).as_matrix();a[:3,3]=[t.x,t.y,t.z]
    if not transform_ok(a):raise ValueError('invalid TF')
    return a


def stable_poses(poses,translation=.01,rotation_deg=2.):
    if len(poses)<2:return False
    first=poses[0]
    for p in poses[1:]:
        if not transform_ok(p) or np.linalg.norm(p[:3,3]-first[:3,3])>translation:return False
        if np.degrees(np.arccos(np.clip((np.trace(first[:3,:3].T@p[:3,:3])-1)/2,-1,1)))>rotation_deg:return False
    return True


def driver_identity():
    """Use the actual executable, not the ros2 wrapper command line."""
    found=subprocess.run(['pgrep','-f','/lib/odin_ros_driver/[h]ost_sdk_sample'],capture_output=True,text=True)
    matches=[]
    for value in found.stdout.split():
        try:
            pid=int(value);exe=(Path('/proc')/str(pid)/'exe').resolve()
            if exe.name!='host_sdk_sample':continue
            fields=(Path('/proc')/str(pid)/'stat').read_text().rsplit(')',1)[1].split()
            start_ticks=int(fields[19]);matches.append((pid,start_ticks))
        except (OSError,ValueError,IndexError):continue
    if len(matches)!=1:return None
    pid,start_ticks=matches[0];boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    key=f'{boot}:{pid}:{start_ticks}'
    start_wall=time.time()-time.monotonic()+start_ticks/os.sysconf('SC_CLK_TCK')
    return dict(epoch='odin:'+hashlib.sha256(key.encode()).hexdigest()[:24],start_wall=start_wall,pid=pid)


class OdinLiveSource:
    def __init__(self,root,world_frame='map',semantics='UNKNOWN'):
        import rclpy
        from rclpy.node import Node
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.qos import qos_profile_sensor_data
        from rclpy.duration import Duration
        from sensor_msgs.msg import Image,PointCloud2
        from std_msgs.msg import String
        from tf2_ros import Buffer,TransformListener
        self.rclpy=rclpy;rclpy.init();self.node=Node('odin_expected_cloud_adapter')
        self.lock=threading.Lock();self.images=deque(maxlen=12);self.clouds=deque(maxlen=12)
        self.image_received=0.;self.cloud_received=0.;self.last_cloud='';self.frame=world_frame;self.semantics=semantics
        self.error='WAIT_RGB_RAW_TF';self.sequence=0;self.map_epoch='odin:'+uuid.uuid4().hex;self.map_anchor=None;self.pending_reanchor=None;self.revision_fault=False
        self.driver_identity=None
        path=Path(root)/'runtime/odin1/calibration/calib.yaml';calib_bytes=path.read_bytes();calib=yaml.safe_load(calib_bytes)
        c=calib['cam_0'];self.tcl=np.array(calib['Tcl_0']).reshape(4,4)
        self.intr=dict(fx=c['A11'],fy=c['A22'],ppx=c['u0'],ppy=c['v0'],skew=c.get('A12',0))
        self.calib_revision=hashlib.sha256(calib_bytes).hexdigest();self.calib_path=path;self.calib_mtime=path.stat().st_mtime_ns;self.epoch_established=False
        self.tf=Buffer(cache_time=Duration(seconds=8));self.listener=TransformListener(self.tf,self.node,spin_thread=True)
        self.node.create_subscription(Image,'/odin1/image/undistorted',self._image,qos_profile_sensor_data)
        self.node.create_subscription(PointCloud2,'/odin1/cloud_raw',self._cloud,qos_profile_sensor_data)
        self.state_pub=self.node.create_publisher(String,'/global_vision/state',1)
        self.delta_pub=self.node.create_publisher(String,'/global_vision/map_delta',1)
        # TransformListener owns the executor for this node, including the cheap
        # input callbacks. Never spin this node in a second executor.

    @staticmethod
    def stamp(msg):return msg.header.stamp.sec+msg.header.stamp.nanosec*1e-9

    def _image(self,msg):
        with self.lock:self.images.append(msg);self.image_received=time.monotonic()

    def _cloud(self,msg):
        with self.lock:self.clouds.append(msg);self.cloud_received=time.monotonic()

    def decode_image(self,msg):
        channels=3 if msg.encoding in ('bgr8','rgb8') else 0
        if not channels:raise ValueError('expected undistorted bgr8/rgb8 image')
        arr=np.frombuffer(msg.data,np.uint8).reshape(msg.height,msg.step)[:,:msg.width*3].reshape(msg.height,msg.width,3)
        return arr.copy() if msg.encoding=='bgr8' else arr[:,:,::-1].copy()

    def preview(self):
        with self.lock:msg=self.images[-1] if self.images else None
        return (self.decode_image(msg),self.stamp(msg)) if msg is not None else (None,None)

    def transform(self,target,source,stamp):
        from rclpy.time import Time
        return pose_matrix(self.tf.lookup_transform(target,source,Time(nanoseconds=int(stamp*1e9))))

    def next_capture(self):
        with self.lock:images=list(self.images);clouds=list(self.clouds)
        if not images or not clouds:self.error='WAIT_RGB_RAW';return None
        if time.monotonic()-min(self.image_received,self.cloud_received)>2:self.error='INPUT_STALE';return None
        if self.revision_fault:self.error='LOCALIZATION_REVISION_REQUIRES_NEW_SESSION';return None
        # Leave 0.35 s for TF to cover the complete scan, not just its header.
        # The stability check below looks up to stamp+0.15 s, and device TF
        # arrives ~170-180 ms after its stamp at 14.5 fps; 0.2 s left ~50 ms of
        # slack and dropped whole frames with "extrapolation into the future".
        latest=min(self.stamp(images[-1]),self.stamp(clouds[-1]))
        clouds=[m for m in clouds if self.stamp(m)<=latest-.35 and self.stamp(m)>self.sequence]
        if not clouds:return None
        cloud=clouds[-1];stamp=self.stamp(cloud);im=min(images,key=lambda m:abs(self.stamp(m)-stamp))
        if abs(self.stamp(im)-stamp)>.020:self.error='RGB_RAW_PAIR_EXCEEDS_20MS';self.sequence=stamp;return None
        try:
            mtime=self.calib_path.stat().st_mtime_ns
            if mtime!=self.calib_mtime:
                if hashlib.sha256(self.calib_path.read_bytes()).hexdigest()!=self.calib_revision:
                    self.revision_fault=True;raise ValueError('CALIBRATION_CHANGED_RESTART_ADAPTER')
                self.calib_mtime=mtime
            points,ids,confidence,offsets=pointcloud2_arrays(cloud)
            # Driver offsets are seconds. A conservative symmetric 0.15 s
            # window covers both header conventions plus the full 10 Hz scan.
            duration=float(np.nanmax(offsets)-np.nanmin(offsets)) if offsets is not None and len(offsets) else .1
            if not np.isfinite(duration) or duration>.15:raise ValueError('RAW_SCAN_DURATION_UNSUPPORTED')
            poses=[self.transform(self.frame,cloud.header.frame_id,t) for t in np.linspace(stamp-.15,stamp+.15,16)]
            if not stable_poses(poses):self.error='WAIT_STABLE_FULL_SCAN';self.sequence=stamp;return None
            world_sensor=self.transform(self.frame,cloud.header.frame_id,stamp)
            if not self.epoch_established:
                self.driver_identity=driver_identity()
                if self.driver_identity is not None:self.map_epoch=self.driver_identity['epoch']
                self.epoch_established=True
            if self.frame=='map':
                anchor=self.transform('map','odom',stamp)
                if self.map_anchor is None:self.map_anchor=anchor
                elif not stable_poses([self.map_anchor,anchor],.03,2.):
                    # The localizer re-aligned its map frame (relocalization /
                    # loop closure). The boxes did not move, so hand the
                    # correction to the map instead of freezing until an
                    # operator opens a new session. Only an implausibly large
                    # jump is treated as a fault.
                    if not stable_poses([self.map_anchor,anchor],.5,20.):
                        self.revision_fault=True;raise ValueError('LOCALIZATION_REVISION_REQUIRES_NEW_SESSION')
                    delta=anchor@np.linalg.inv(self.map_anchor)
                    self.pending_reanchor=delta if self.pending_reanchor is None else delta@self.pending_reanchor
                    self.map_anchor=anchor
            self.sequence=stamp
            packet=CapturePacket(f'{self.map_epoch}:{stamp:.9f}',self.stamp(im),stamp,points,ids,world_sensor,
                world_sensor@np.linalg.inv(self.tcl),self.intr,self.decode_image(im),confidence,offsets,
                map_epoch=self.map_epoch,calibration_revision=self.calib_revision,trajectory_revision='live_stable_v1',
                frame_id=self.frame,stable_capture=True,pose_quality=True,source='ODIN_CLOUD_RAW',return_semantics=self.semantics)
            packet.validate();self.error='';return packet
        except Exception as exc:self.error=str(exc);return None

    def reset_epoch(self):
        self.map_epoch='odin:'+uuid.uuid4().hex;self.map_anchor=None;self.pending_reanchor=None;self.revision_fault=False;self.epoch_established=True

    def publish(self,snapshot,delta=None):
        from std_msgs.msg import String
        import json
        self.state_pub.publish(String(data=json.dumps(snapshot,allow_nan=False)))
        if delta:self.delta_pub.publish(String(data=json.dumps(delta,allow_nan=False)))

    def health(self):
        return dict(error=self.error,image_age_s=round(time.monotonic()-self.image_received,2) if self.image_received else None,
            cloud_age_s=round(time.monotonic()-self.cloud_received,2) if self.cloud_received else None,
            frame_id=self.frame,map_epoch=self.map_epoch,return_semantics=self.semantics,
            stable_window_s=.30,pose_error_bound_m=.01,pose_bound_source='CONFIGURED_NOT_CERTIFIED')

    def close(self):
        self.listener.executor.shutdown();self.listener.dedicated_listener_thread.join(timeout=5)
        self.listener.unregister();self.node.destroy_node();self.rclpy.shutdown()
