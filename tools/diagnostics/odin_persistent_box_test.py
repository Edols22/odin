#!/usr/bin/env python3
"""Odin carton persistence test using the strict get_box_node P-01 geometry.

This is deliberately a diagnostic executable, not the get_box service.  It
reuses the detector's P-01 implementation primitives (far-instance split,
physical gates and visible_measurement) and adds Odin world-space persistence
and a self-contained camera/map/cloud preview for the dashboard.
"""
import argparse, json, math, sys, time
import hashlib
import fcntl
import signal
from collections import deque
from pathlib import Path

# The diagnostics launcher is usable before the perception package is built.
# Make the checked-out implementation (including get_box_node's geometry)
# importable in that case.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import cv2
import numpy as np
import rclpy
import yaml
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy, DurabilityPolicy
from sensor_msgs.msg import Image, PointCloud2
from sensor_msgs_py import point_cloud2
from std_msgs.msg import String
from tf2_ros import Buffer, TransformListener
from ultralytics import YOLO

from camera_unload_perception.algorithms.carton_detector import (
    fit_plane_ransac,
    visible_measurement,
)
from camera_unload_perception.algorithms.persistent_carton_map import PersistentCartonMap, depth_support
from camera_unload_perception.core.action_guard import Operation
from camera_unload_perception.ros.camera_pipeline import (
    _intrinsic_corner_rejections,
    _neighbour_edges,
    _split_far_merged_instances,
)


def _q_matrix(q):
    x, y, z, w = float(q.x), float(q.y), float(q.z), float(q.w)
    return np.array([
        [1 - 2 * (y*y + z*z), 2 * (x*y - z*w), 2 * (x*z + y*w)],
        [2 * (x*y + z*w), 1 - 2 * (x*x + z*z), 2 * (y*z - x*w)],
        [2 * (x*z - y*w), 2 * (y*z + x*w), 1 - 2 * (x*x + y*y)],
    ], dtype=np.float64)


def _transform_matrix(value):
    q = value.transform.rotation
    t = value.transform.translation
    out = np.eye(4, dtype=np.float64)
    out[:3, :3] = _q_matrix(q)
    out[:3, 3] = [float(t.x), float(t.y), float(t.z)]
    return out


class OdinPersistentBoxTest(Node):
    def __init__(self, args):
        super().__init__('odin_persistent_box_test')
        self.args = args
        self.out = Path(args.output)
        self.out.mkdir(parents=True, exist_ok=True)
        self.instance_lock = open('/home/nvidia/perception_domain_nx/runtime/odin1/persistent_detector.lock', 'a')
        fcntl.flock(self.instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = yaml.safe_load(args.config.read_text()) if args.config else {}
        self.mapper = PersistentCartonMap(config)
        self.enabled = True
        self.control_revision = None
        self.request_id = self.reset_id = 0
        control_path=self.out/'control.json'
        if control_path.exists():
            baseline=json.loads(control_path.read_text())
            self.request_id=baseline.get('request_id',0)
            self.reset_id=baseline.get('reset_id',0)
        self.session_id = str(time.time_ns())
        self.mapper.session_id = self.session_id
        self.epoch_samples = []
        self.epoch_started_at = None
        self.epoch_progress = dict(state='IDLE', collected=0, required=self.mapper.config['epoch_frames'])
        self.sample_sequence = 0
        self.last_processed_cloud_stamp = None
        self.last_preview = self.last_cloud_preview = 0.
        self.last_error = ''
        self.pose_error = ''
        self.preview_pose_valid=False
        self.preview_stamp=0.
        self.world_from_camera = None
        self.model = YOLO(str(args.model))
        self.image = None
        self.image_stamp = None
        self.image_buffer = deque(maxlen=20)
        self.last_processed_stamp = None
        self.cloud = None
        self.cloud_stamp = None
        self.cloud_frame = ''
        self.cloud_buffer = deque(maxlen=30)
        self.odom_buffer = deque(maxlen=120)
        self.latest_odom_stamp = None
        self.odom_matrix = None
        self.world_from_cloud = None
        self.world_frame = args.world_frame
        self.base_frame = args.base_frame
        self.mapper.frame_id = self.world_frame
        self.base_from_camera = None
        self.base_pose_error = ''
        self.camera_world = None
        self.map_tf_available = False
        self.tcl = np.eye(4, dtype=np.float64)
        self.map_boxes = self.mapper.objects
        self.frame_events = []
        self.frame = 0
        self.request = False
        self.pending_requests = 0
        self.tf = Buffer()
        tf_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST, depth=100,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        tf_static_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST, depth=100,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        # Run TF subscription processing independently of the detector timer;
        # YOLO inference can otherwise starve the single-threaded ROS executor
        # and leave all world points unresolved.
        self.tf_node = Node('odin_persistent_tf_receiver')
        self.listener = TransformListener(
            self.tf, self.tf_node, spin_thread=True, qos=tf_qos, static_qos=tf_static_qos
        )
        self.marker_pub = self.create_publisher(
            __import__('visualization_msgs.msg', fromlist=['MarkerArray']).MarkerArray,
            '/odin1/persistent_box_markers', 10,
        )
        self.event_pub = self.create_publisher(String, '/box_map/events', 10)
        self.map_pub = self.create_publisher(String, '/box_map/objects', 10)
        self.measurement_pub = self.create_publisher(String, '/box_map/current_measurements', 10)
        policy_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                                durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.active_pub = self.create_publisher(String, '/box_map/active_boxes', policy_qos)
        self.delta_pub = self.create_publisher(String, '/box_map/delta', 10)
        reliable_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST, depth=5,
            reliability=ReliabilityPolicy.RELIABLE,
        )
        self.create_subscription(Image, '/odin1/image/undistorted', self.image_cb, reliable_qos)
        self.create_subscription(PointCloud2, '/odin1/cloud_raw', self.cloud_cb, reliable_qos)
        self.create_subscription(Odometry, '/odin1/odometry', self.odom_cb, reliable_qos)
        self.timer = self.create_timer(0.05, self.tick)
        self.load_calib()
        self._load_map_identity()
        self._resume_checkpoint()
        self.strict_conf = 0.70
        self.box_length = 0.40
        self.mapper.config['max_center_distance_m']=args.association_m
        self.mapper.config['min_observations']=max(3,args.confirm_hits)
        self.mask_erode_px = int(self.mapper.config['mask_erode_px'])
        self.max_points_per_box = int(self.mapper.config['max_points_per_box'])
        self.get_logger().info(
            f'mode={args.mode} strict=get_box_node.P01 conf>={self.strict_conf:.2f} '
            f'model={args.model} world=Odin SLAM map (timestamp-aligned)'
        )

    def load_calib(self):
        payload = yaml.safe_load(self.args.calib.read_text())
        cam = payload['cam_0']
        self.intr = {
            'fx': float(cam['A11']), 'fy': float(cam['A22']),
            'ppx': float(cam['u0']), 'ppy': float(cam['v0']),
            'A11': float(cam['A11']), 'A22': float(cam['A22']),
            'u0': float(cam['u0']), 'v0': float(cam['v0']),
        }
        self.tcl = np.asarray(payload['Tcl_0'], dtype=np.float64).reshape(4, 4)

    def _load_map_identity(self):
        if self.args.localization_source == 'tf':
            self.mapper.map_id = 'tf:' + self.world_frame
            return
        config_path = Path(__file__).resolve().parents[4] / 'odin_v013_ws/src/odin_ros_driver/config/control_command.yaml'
        keys = yaml.safe_load(config_path.read_text())['register_keys']
        map_path = Path(keys['relocalization_map_abs_path'])
        digest = hashlib.sha256()
        with map_path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024*1024), b''): digest.update(chunk)
        self.mapper.map_id = 'sha256:' + digest.hexdigest()

    @staticmethod
    def _stamp(msg):
        return float(msg.header.stamp.sec) + float(msg.header.stamp.nanosec) * 1e-9

    def image_cb(self, msg):
        try:
            channels = 1 if msg.encoding in ('mono8', '8UC1') else 3
            raw = np.frombuffer(bytes(msg.data), dtype=np.uint8)
            image = np.ndarray((int(msg.height), int(msg.width), channels), dtype=np.uint8,
                               buffer=raw, strides=(int(msg.step), channels, 1))
            if channels == 1:
                image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
            elif msg.encoding in ('rgb8', 'RGB8'):
                image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
            stamp = self._stamp(msg)
            self.image = image.copy(); self.image_stamp = stamp
            self.image_buffer.append((stamp, self.image.copy()))
            if time.monotonic() - self.last_preview >= .25:
                self.last_preview = time.monotonic()
                self._atomic_image('live.jpg', self.image)
                self._atomic_json('heartbeat.json', dict(stamp=time.time(), sensor_stamp=stamp,
                    mode=self.args.mode, enabled=self.enabled, frame=self.frame,
                    preview_pose_valid=self.preview_pose_valid,preview_stamp=self.preview_stamp,
                    error=self.last_error, session_id=self.session_id, request_pending=self.request,
                    epoch=self.epoch_progress))
        except Exception as exc:
            self.get_logger().warning(f'undistorted image decode failed: {exc}')

    def cloud_cb(self, msg):
        stamp = self._stamp(msg)
        self.cloud_buffer.append((stamp, msg, msg.header.frame_id))
        self.cloud, self.cloud_stamp, self.cloud_frame = msg, stamp, msg.header.frame_id

    def odom_cb(self, msg):
        self.latest_odom_stamp = self._stamp(msg)
        # Odom samples verify timing only. Global geometry always uses map TF.
        matrix = np.eye(4, dtype=np.float64)
        matrix[:3, :3] = _q_matrix(msg.pose.pose.orientation)
        matrix[:3, 3] = [float(msg.pose.pose.position.x),
                         float(msg.pose.pose.position.y),
                         float(msg.pose.pose.position.z)]
        self.odom_buffer.append((self.latest_odom_stamp, matrix.copy()))
        self.odom_matrix = matrix

    def tick(self):
        self._read_control()
        self.request = self.pending_requests>0
        if self.epoch_started_at is not None and time.monotonic()-self.epoch_started_at > self.mapper.config['epoch_timeout_s']:
            self._cancel_epoch('EPOCH_TIMEOUT')
            self.last_error = '批次观察超时，地图未更新；请重新执行一次'
        if self.args.mode in ('manual','epoch') and Path('/tmp/odin_persistent_box_once').exists():
            Path('/tmp/odin_persistent_box_once').unlink(missing_ok=True)
            self.pending_requests+=1;self.request=True
        if self.image is None or not self.image_buffer or not self.cloud_buffer:
            return
        if (not self.enabled or self.args.mode in ('manual','epoch')) and not self.request and not self.epoch_samples:
            if time.monotonic() - self.last_cloud_preview > .5:
                self.last_cloud_preview = time.monotonic()
                for stamp,msg,frame in reversed(self.cloud_buffer):
                    at=rclpy.time.Time(nanoseconds=int(stamp*1e9))
                    if self.tf.can_transform(self.world_frame,frame,at):
                        transform=_transform_matrix(self.tf.lookup_transform(self.world_frame,frame,at))
                        self._publish_live_cloud(self._cloud_points(msg),transform,stamp)
                        break
                else:
                    self.preview_pose_valid=False
                    self._atomic_json('live_cloud.json',{'frame':self.world_frame,'stamp':self.image_stamp,
                        'points':[],'world_from_camera':None,'pose_valid':False})
            return
        # Odin's RGB, cloud and odom publication paths have different latency.
        # Select the newest image for which both later streams already contain
        # a sample within the specification's 20 ms synchronization window.
        candidates=[]
        for stamp,image in reversed(self.image_buffer):
            if self.last_processed_stamp is not None and stamp<=self.last_processed_stamp:continue
            cloud=min(self.cloud_buffer,key=lambda sample:abs(sample[0]-stamp))
            if self.last_processed_cloud_stamp is not None and cloud[0] <= self.last_processed_cloud_stamp: continue
            if abs(cloud[0]-stamp)*1000>self.mapper.config['max_rgb_depth_dt_ms']:continue
            ready=self.tf.can_transform(self.world_frame,cloud[2],rclpy.time.Time(nanoseconds=int(stamp*1e9)))
            candidates.append((ready,stamp,image))
            if ready:break
        if not candidates:return
        ready,selected_stamp,selected_image=next((sample for sample in candidates if sample[0]),candidates[0])
        # Even without global localization, publish current optical/base
        # measurements. The mapper itself refuses a global commit without TF.
        self.image_stamp = selected_stamp; self.image = selected_image
        self.last_processed_stamp = selected_stamp
        while self.image_buffer and self.image_buffer[0][0] <= selected_stamp:
            self.image_buffer.popleft()
        if self.args.mode == 'realtime' or self.request or self.epoch_samples:
            if self.pending_requests and not self.epoch_samples:self.pending_requests-=1
            self.request = self.pending_requests>0
            try:
                self.process()
                self.last_error = ''
            except Exception as exc:
                # A late DDS shutdown must not leave an orphaned detector or
                # make the next dashboard start look like a camera failure.
                self.get_logger().error(f'processing frame failed: {exc}')
                self.last_error = str(exc)
                self._cancel_epoch('TRANSACTION_ABORTED')

    def _read_cloud(self):
        if self.image_stamp is not None and self.cloud_buffer:
            _, self.cloud, self.cloud_frame = min(self.cloud_buffer, key=lambda x: abs(x[0] - self.image_stamp))
            self.cloud_stamp = self._stamp(self.cloud)
        if self.cloud is None:
            return np.empty((0, 3), dtype=np.float32)
        return self._cloud_points(self.cloud)

    def _cloud_points(self, msg):
        try:
            raw = point_cloud2.read_points(
                msg, field_names=('x', 'y', 'z'), skip_nans=True
            )
            return np.column_stack((raw['x'], raw['y'], raw['z'])).astype(np.float32)
        except Exception as exc:
            self.get_logger().warning(f'cloud decode failed: {exc}')
            return np.empty((0, 3), dtype=np.float32)

    def _nearest_odom(self, stamp):
        if not self.odom_buffer:
            return self.odom_matrix, self.latest_odom_stamp
        sample_stamp, matrix = min(self.odom_buffer, key=lambda x: abs(x[0] - stamp))
        return matrix, sample_stamp

    @staticmethod
    def _depth_stats(mask, depth):
        """Robust depth statistics for one detector mask."""
        if mask is None or not np.any(mask):
            return float('nan'), float('nan'), 0.0, np.empty(0, dtype=np.float32)
        raw = depth[mask]
        valid = raw[np.isfinite(raw) & (raw > 0.20) & (raw < 8.0)]
        ratio = float(valid.size) / max(1, int(np.count_nonzero(mask)))
        if valid.size == 0:
            return float('nan'), float('nan'), ratio, valid
        median = float(np.median(valid))
        mad = float(np.median(np.abs(valid - median)))
        robust = valid[np.abs(valid - median) <= max(0.015, 3.0 * mad)]
        if robust.size == 0:
            robust = valid
        return float(np.median(robust)), mad, ratio, robust

    def _plane_stats(self, mask, depth):
        """Fit a small planar patch for geometry diagnostics."""
        ys, xs = np.where(mask & np.isfinite(depth) & (depth > .20) & (depth < 8.0))
        if len(xs) > self.max_points_per_box:
            choose = np.linspace(0, len(xs) - 1, self.max_points_per_box).astype(np.int64)
            xs, ys = xs[choose], ys[choose]
        if len(xs) < 30:
            return None, 0.0, float('nan')
        z = depth[ys, xs].astype(np.float64)
        x = (xs - self.intr['ppx']) * z / self.intr['fx']
        y = (ys - self.intr['ppy']) * z / self.intr['fy']
        pts = np.column_stack((x, y, z))
        result = fit_plane_ransac(pts, threshold_m=self.mapper.config['plane_residual_m'], trials=100)
        if result is None:
            return None, 0.0, float('nan')
        normal, offset, inlier = result
        residual = np.abs(pts @ normal + offset)
        rmse = float(np.sqrt(np.mean(residual[inlier] ** 2))) if np.any(inlier) else float('nan')
        return normal.tolist(), float(np.mean(inlier)), rmse

    def _depth_image(self, points):
        h, w = self.image.shape[:2]
        depth = np.full((h, w), np.nan, dtype=np.float32)
        if len(points) == 0:
            return depth, np.empty((0, 3), dtype=np.float32)
        cam = (self.tcl[:3, :3] @ points.T + self.tcl[:3, 3, None]).T
        valid = np.isfinite(cam).all(axis=1) & (cam[:, 2] > 0.20) & (cam[:, 2] < 8.0)
        cam = cam[valid]
        if len(cam) == 0:
            return depth, cam
        u = np.rint(self.intr['fx'] * cam[:, 0] / cam[:, 2] + self.intr['ppx']).astype(np.int32)
        v = np.rint(self.intr['fy'] * cam[:, 1] / cam[:, 2] + self.intr['ppy']).astype(np.int32)
        live = (u >= 0) & (u < w) & (v >= 0) & (v < h)
        u, v, cam = u[live], v[live], cam[live]
        if len(cam):
            flat = depth.reshape(-1)
            index = v.astype(np.int64) * w + u.astype(np.int64)
            # z-buffer: the nearest point owns a pixel.
            flat[:] = np.inf
            np.minimum.at(flat, index, cam[:, 2])
            flat[~np.isfinite(flat)] = np.nan
        return depth, cam

    def _world_point(self, point_cloud, stamp):
        if self.world_from_cloud is None:
            return None
        return (self.world_from_cloud[:3,:3] @ np.asarray(point_cloud) + self.world_from_cloud[:3,3]).tolist()

    def _strict_observations(self, image, depth, points):
        # get_box_node's minimum_confidence is 0.70.  The dashboard control
        # may raise this threshold, but never lower the production gate.
        confidence = max(self.strict_conf, float(self.args.conf))
        result = self.model.predict(image, conf=confidence, imgsz=self.args.imgsz, verbose=False,
                                    device=self.args.device, retina_masks=True)[0]
        detected = list(result.boxes or [])
        masks_raw = result.masks.data.cpu().numpy() if result.masks is not None else []
        masks, boxes, depths = [], [], []
        for index, box in enumerate(detected):
            bbox = np.asarray(box.xyxy[0].cpu().numpy(), dtype=float)
            x1, y1, x2, y2 = np.rint(bbox).astype(int)
            mask = (
                masks_raw[index] > 0.5
                if index < len(masks_raw) else np.zeros(image.shape[:2], dtype=bool)
            )
            if mask.shape != image.shape[:2]:
                raise ValueError('YOLO mask must be in original, unpadded image coordinates')
            if not mask.any():
                mask[max(0, y1):min(image.shape[0], y2), max(0, x1):min(image.shape[1], x2)] = True
            masks.append(mask); boxes.append(bbox)
            median, _, _, _ = self._depth_stats(mask, depth)
            depths.append(median)
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        detected, boxes, masks, depths, split_diagnostics = _split_far_merged_instances(
            Operation.P01, detected, boxes, masks, depths, gray, depth, self.intr
        )
        accepted, rejected = [], []
        for index, (box, bbox, mask, median_depth) in enumerate(zip(detected, boxes, masks, depths)):
            # Erode detector masks before RGB-D statistics to avoid the
            # registration error and neighbouring-box seams at the boundary.
            kernel = np.ones((self.mask_erode_px, self.mask_erode_px), np.uint8)
            inner_mask = cv2.erode(mask.astype(np.uint8), kernel, iterations=1).astype(bool)
            if np.count_nonzero(inner_mask) < 20:
                inner_mask = mask
            median_depth, depth_mad, valid_depth_ratio, _ = self._depth_stats(inner_mask, depth)
            support=depth_support(inner_mask,depth,self.mapper.config['depth_support_cell_px'])
            plane_normal, plane_inlier_ratio, plane_rmse = self._plane_stats(inner_mask, depth)
            x1, y1, x2, y2 = [int(v) for v in np.rint(bbox)]
            width_m = (x2 - x1) * median_depth / self.intr['fx']
            height_m = (y2 - y1) * median_depth / self.intr['fy']
            aspect = width_m / max(height_m, 1e-6)
            reasons = []
            if not np.isfinite(median_depth): reasons.append('median_depth_not_finite')
            if width_m < .30: reasons.append('projected_width_below_0.30m')
            if width_m > .75: reasons.append('projected_width_above_0.75m')
            if height_m < .25: reasons.append('projected_height_below_0.25m')
            if height_m > .58: reasons.append('projected_height_above_0.58m')
            if aspect < .65: reasons.append('projected_aspect_below_0.65')
            if aspect > 1.90: reasons.append('projected_aspect_above_1.90')
            measurement = None
            if not reasons:
                pad = max(12, int(.12 * max(x2 - x1, y2 - y1)))
                left, top, right, bottom = max(0, x1-pad), max(0, y1-pad), min(image.shape[1], x2+pad), min(image.shape[0], y2+pad)
                roi = np.s_[top:bottom, left:right]
                local_intr = dict(self.intr, ppx=self.intr['ppx']-left, ppy=self.intr['ppy']-top)
                local_bbox = (x1-left, y1-top, x2-left, y2-top)
                try:
                    measurement = visible_measurement(
                        mask[roi], depth[roi], local_intr, 1, local_bbox, gray[roi],
                        _neighbour_edges(boxes, index), None, None, .55,
                        self.box_length, self.box_length, .35,
                        self._camera_up_project(), True,
                    )
                except Exception as exc:
                    reasons.append(f'geometry_exception:{type(exc).__name__}')
            if measurement is None:
                reasons.append('geometry_missing')
            else:
                # These are exactly the P-01 acceptance gates in get_box_node.
                if measurement.get('status') != 'measured_visible_face': reasons.append('face_not_measured')
                if measurement.get('quality') == 'partial': reasons.append('partial_face')
                edge_h, edge_v = measurement.get('visible_horizontal_m'), measurement.get('visible_vertical_m')
                if edge_h is None or edge_v is None or min(edge_h, edge_v) < .35: reasons.append('visible_edge_below_0.35m')
                if edge_h is not None and edge_v is not None and max(edge_h, edge_v) > .80: reasons.append('visible_edge_above_0.80m')
                # Strict P-01 does not use P-02 corner-history vetoes, but keep
                # the diagnostic result available for the operator.
                measurement['strict_corner_rejections'] = _intrinsic_corner_rejections(measurement)
            candidate = {
                'frame_id':self.mapper.frame+1,'stamp':self.image_stamp,'class_id':0,
                'local_detection_id': index, 'confidence': float(box.conf[0]),
                'bbox': [x1, y1, x2, y2], 'depth_camera_m': median_depth,
                'depth_mad_m': depth_mad, 'valid_depth_ratio': valid_depth_ratio,
                'depth_support':support,
                'plane_normal_camera': plane_normal, 'plane_inlier_ratio': plane_inlier_ratio,
                'plane_rmse_m': plane_rmse,
                'measurement': measurement, 'rejection_reasons': reasons,
                'detection_quality_ok': True,
                'position_quality_ok': False, 'orientation_quality_ok': False,
                'geometry_quality_ok': False,
            }
            if reasons:
                candidate['position_rejection_reasons'] = list(reasons)
                candidate['orientation_rejection_reasons'] = ['position_unavailable']
                rejected.append(candidate)
                # Rejected fragments/merged masks can explain segmentation
                # conflicts, but can never create or move a global object.
                if np.isfinite(median_depth) and self.world_from_camera is not None and plane_normal is not None:
                    ys,xs=np.where(inner_mask & np.isfinite(depth))
                    z=depth[ys,xs]
                    optical=np.column_stack(((xs-self.intr['ppx'])*z/self.intr['fx'],
                                             (ys-self.intr['ppy'])*z/self.intr['fy'],z))
                    center=np.median(optical,axis=0)
                    candidate.update(_mask=mask, conflict_only=True,
                        center_map=(self.world_from_camera[:3,:3]@center+self.world_from_camera[:3,3]).tolist(),
                        size_world=[width_m,height_m,.30],
                        normal_world=(self.world_from_camera[:3,:3]@plane_normal).tolist(),
                        horizontal_world=self.world_from_camera[:3,0].tolist())
                    accepted.append(candidate)
                continue
            center = (measurement.get('front_face_center_camera_m') or {})
            optical = np.array([
                -float(center.get('y_left', np.nan)),
                -float(center.get('z_up', np.nan)),
                float(center.get('x_forward_depth', np.nan)),
            ])
            cloud_center = np.linalg.inv(self.tcl)[:3, :3] @ optical + np.linalg.inv(self.tcl)[:3, 3]
            world_point = self._world_point(cloud_center, self.cloud_stamp)
            candidate['center_map'] = world_point
            candidate['_mask'] = mask
            candidate['size_world'] = [measurement['visible_horizontal_m'], measurement['visible_vertical_m'], .30]
            rotation = self.world_from_camera[:3,:3] if self.world_from_camera is not None else np.eye(3)
            # get_box_node's rotation is box->project-camera. Convert that
            # complete pose once. Both corners and normals returned by the
            # helper use project-camera (forward, left, up), not optical axes.
            project_to_optical = np.array([[0., -1., 0.],
                                           [0., 0., -1.],
                                           [1., 0., 0.]])
            pose_project = np.asarray(
                measurement.get('front_face_rotation_matrix_box_to_camera', []),
                dtype=float,
            )
            if pose_project.shape == (3, 3) and np.isfinite(pose_project).all():
                pose_optical = project_to_optical @ pose_project
                toward_camera_optical = -pose_optical[:, 0]
                horizontal_optical = pose_optical[:, 1]
            else:
                normal = np.asarray(measurement['front_face_normal_toward_camera'], dtype=float)
                toward_camera_optical = project_to_optical @ normal
                corners = measurement['front_face_corners_camera_m']
                horizontal_optical = project_to_optical @ (np.asarray(corners[1])-np.asarray(corners[0]))
            candidate['normal_world'] = (rotation @ toward_camera_optical).tolist()
            candidate['horizontal_world'] = (rotation @ horizontal_optical).tolist()
            candidate['normal_camera_optical'] = toward_camera_optical.tolist()
            candidate['horizontal_camera_optical'] = horizontal_optical.tolist()
            quality_reasons=[]
            if support['valid_pixels']<self.mapper.config['min_valid_pixels']:quality_reasons.append('insufficient_depth_points')
            if support['spatial_coverage']<self.mapper.config['min_valid_depth_ratio']:quality_reasons.append('insufficient_spatial_coverage')
            if plane_inlier_ratio<self.mapper.config['plane_min_inlier_ratio']:quality_reasons.append('plane_inlier_ratio_low')
            actual_ratio = float(measurement.get('central_core_inlier_ratio', 0.))
            actual_rmse = float(measurement.get('plane_rmse_m', float('inf')))
            agreement = math.degrees(math.acos(np.clip(
                np.dot(toward_camera_optical, plane_normal), -1., 1.))) if plane_normal is not None else 180.
            candidate['measurement_quality'] = dict(core_inlier_ratio=actual_ratio,
                plane_rmse_m=actual_rmse, diagnostic_plane_angle_deg=agreement,
                size_prior_fallback=bool(measurement.get('far_size_prior_fallback_used')))
            # Identity/position and precision orientation have separate gates.
            # A well-supported carton must not disappear merely because its
            # central-plane inlier fraction fluctuates around 70 percent.
            position_reasons=list(quality_reasons)
            if actual_ratio < .50: position_reasons.append('position_plane_support_low')
            if not np.isfinite(actual_rmse) or actual_rmse > .02: position_reasons.append('actual_pose_plane_residual_high')
            if agreement > 12.: position_reasons.append('actual_pose_disagrees_with_support_plane')
            if actual_ratio < .7: quality_reasons.append('actual_pose_plane_inliers_low')
            if not np.isfinite(actual_rmse) or actual_rmse > .02: quality_reasons.append('actual_pose_plane_residual_high')
            if agreement > 12.: quality_reasons.append('actual_pose_disagrees_with_support_plane')
            candidate['geometry_rejection_reasons']=quality_reasons
            candidate['detection_quality_ok'] = True
            candidate['position_quality_ok'] = not position_reasons
            candidate['orientation_quality_ok'] = not quality_reasons
            candidate['position_rejection_reasons'] = position_reasons
            candidate['orientation_rejection_reasons'] = list(quality_reasons)
            # Compatibility: the mapper's admission gate means a usable
            # position, not a promise of a precise six-DoF grasp pose.
            candidate['geometry_quality_ok'] = candidate['position_quality_ok']
            candidate['measurement_quality'].update(position_min_core_ratio=.50,orientation_min_core_ratio=.70,
                position_usable=candidate['position_quality_ok'],orientation_usable=candidate['orientation_quality_ok'])
            candidate['center_camera_optical'] = optical.tolist()
            candidate['pixel_center'] = [float((x1+x2)/2), float((y1+y2)/2)]
            accepted.append(candidate)
        return accepted, rejected, split_diagnostics, len(detected)

    def _camera_up_project(self):
        if self.world_from_camera is None:
            return None
        optical_up = self.world_from_camera[:3, :3].T @ np.array([0., 0., 1.])
        return np.array([optical_up[2], -optical_up[0], -optical_up[1]])

    def _resume_checkpoint(self):
        path = self.out / 'resume_checkpoint.json'
        if not path.exists():
            return
        saved = json.loads(path.read_text())
        driver = Path('/proc') / str(saved['driver_pid'])
        if (not driver.exists() or saved['driver_start'] != driver.joinpath('stat').read_text().split(') ')[1].split()[19]
                or b'host_sdk_sample' not in driver.joinpath('cmdline').read_bytes()
                or saved['calib_sha256'] != hashlib.sha256(self.args.calib.read_bytes()).hexdigest()):
            raise RuntimeError('Cannot resume box map across driver/calibration changes')
        self.mapper.objects = {int(o['global_id']):o for o in saved['map_history']}
        for obj in self.mapper.objects.values():
            obj.setdefault('geometry_anchor', {key:list(obj[key]) for key in ('center_map', 'normal_world')})
            # Legacy records were admitted by the former full-pose gate.
            # Keep that provenance explicit during a same-driver reload.
            obj.setdefault('orientation_trusted', True)
            obj.setdefault('orientation_observed_ok', False)
            obj.setdefault('position_quality_ok', True)
        self.mapper.next_id = max(self.mapper.objects, default=-1) + 1
        self.mapper.frame = saved['frame']
        self.mapper.last_pose = np.array(saved['world_from_camera'])
        self.mapper.last_stamp = saved['sensor_stamp']
        self.mapper.pose_anomaly_latched = bool(saved.get('time_sync', {}).get('pose_anomaly', False))
        self.mapper.hypotheses = {(h['old_object_id'], h['new_candidate_id']):h
                                  for h in saved.get('relocation_hypotheses', [])}
        self.session_id = saved['session_id']
        self.mapper.session_id = self.session_id
        previous = saved.get('active_box_map', {})
        if previous.get('frame_id','map') != self.world_frame:
            raise RuntimeError('Cannot resume records in a different world frame')
        if previous.get('map_id', self.mapper.map_id) != self.mapper.map_id:
            raise RuntimeError('Cannot resume across environment map changes')
        self.mapper.revision = previous.get('revision', 0)
        self.mapper.epoch_id = previous.get('epoch_id', 0)
        self.map_boxes = self.mapper.objects
        self.frame = self.mapper.frame
        path.rename(self.out / ('resumed_' + str(time.time_ns()) + '.json'))
        self.get_logger().info('Resumed box IDs and history on unchanged Odin driver')

    def _save_geometry_evidence(self, image, depth, observations):
        # 32 bounded slots retain exact sparse depth, masks and raw points for
        # replay. No filled depth, unbounded bag recording or second USB client.
        folder = self.out / 'geometry_evidence'
        folder.mkdir(exist_ok=True)
        stem = str(self.sample_sequence % 32).zfill(2)
        raw = point_cloud2.read_points(self.cloud, skip_nans=False)
        masks = np.array([o['_mask'] for o in observations if '_mask' in o], dtype=np.uint8)
        temp = folder / (stem + '.tmp.npz')
        np.savez_compressed(temp, raw_cloud=raw, depth=depth,
                            masks_packed=np.packbits(masks, axis=-1), image_shape=image.shape[:2])
        temp.replace(folder / (stem + '.npz'))
        cv2.imwrite(str(folder / (stem + '.jpg')), image, [cv2.IMWRITE_JPEG_QUALITY, 95])
        metadata = dict(frame=self.frame, sample_sequence=self.sample_sequence, sensor_stamp=self.image_stamp, cloud_stamp=self.cloud_stamp,
            world_from_camera=self.world_from_camera, tcl=self.tcl, intrinsics=self.intr,
            observations=[{k:v for k,v in o.items() if k!='measurement'} for o in observations],
            events=self.mapper.events, association_diagnostics=self.mapper.association_diagnostics)
        (folder / (stem + '.json')).write_text(json.dumps(self._json_safe(metadata), allow_nan=False))

    def _cancel_epoch(self, reason):
        self.epoch_samples = []
        self.epoch_started_at = None
        self.epoch_progress = dict(state=reason, collected=0, required=self.mapper.config['epoch_frames'])

    def _read_control(self):
        path = self.out / 'control.json'
        if not path.exists():
            return
        try:
            control = json.loads(path.read_text())
            if control.get('revision') == self.control_revision:
                return
            self.control_revision = control.get('revision')
            if (control.get('mode', self.args.mode) != self.args.mode or not control.get('enabled',True)
                    or control.get('config',self.mapper.config) != self.mapper.config
                    or control.get('confidence',self.args.conf) != self.args.conf
                    or control.get('inference_size',self.args.imgsz) != self.args.imgsz):
                self._cancel_epoch('CANCELLED')
                self.pending_requests = 0
            self.args.mode = control.get('mode', self.args.mode)
            self.enabled = bool(control.get('enabled', True))
            if control.get('request_id', 0) > self.request_id:
                self.pending_requests+=control['request_id']-self.request_id
                self.request_id = control['request_id']; self.request = True
            if control.get('reset_id', 0) > self.reset_id:
                self.reset_id = control['reset_id']
                self.session_id=str(time.time_ns())
                map_id=self.mapper.map_id
                self.mapper = PersistentCartonMap(self.mapper.config)
                self.mapper.frame_id=self.world_frame
                self.mapper.map_id=map_id;self.mapper.session_id=self.session_id
                self._cancel_epoch('IDLE');self.pending_requests=0;self.frame=0
                self.map_boxes = self.mapper.objects
                snapshot=self.mapper.snapshot()
                self._atomic_json('latest.json',dict(snapshot,frame=0,stamp=time.time(),session_id=self.session_id,
                    mode=self.args.mode,enabled=self.enabled,map_frame=self.world_frame,config=self.mapper.config))
                self._atomic_json('persistent_map.json',dict(snapshot,map_frame=self.world_frame,session_id=self.session_id))
                self._publish_policy(snapshot, commit=False)
                self.publish_markers()
            if 'config' in control:
                self.mapper.config.update({key:value for key,value in control['config'].items() if key in self.mapper.config})
                self.mask_erode_px = int(self.mapper.config['mask_erode_px'])
                self.max_points_per_box = int(self.mapper.config['max_points_per_box'])
            self.args.conf = max(.70, float(control.get('confidence', self.args.conf)))
            self.args.imgsz = int(control.get('inference_size',self.args.imgsz))
        except (ValueError, OSError) as exc:
            self.get_logger().warning(f'control read failed: {exc}')

    def _atomic_json(self, name, payload):
        temp = self.out / (name + '.tmp')
        temp.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False))
        temp.replace(self.out / name)

    def _atomic_image(self, name, image):
        temp = self.out / ('tmp_' + name)
        cv2.imwrite(str(temp), image, [cv2.IMWRITE_JPEG_QUALITY, 85])
        temp.replace(self.out / name)

    @staticmethod
    def _json_safe(value):
        if isinstance(value, np.ndarray): return OdinPersistentBoxTest._json_safe(value.tolist())
        if isinstance(value, np.generic): return OdinPersistentBoxTest._json_safe(value.item())
        if isinstance(value, float) and not math.isfinite(value): return None
        if isinstance(value, dict): return {k: OdinPersistentBoxTest._json_safe(v) for k,v in value.items() if not k.startswith('_')}
        if isinstance(value, (list,tuple)): return [OdinPersistentBoxTest._json_safe(v) for v in value]
        return value

    def _resolve_world_pose(self):
        self.world_from_cloud = None
        self.world_from_camera = None
        self.map_tf_available = False
        try:
            stamp = rclpy.time.Time(nanoseconds=int(self.image_stamp * 1e9))
            value = self.tf.lookup_transform(self.world_frame, self.cloud_frame, stamp,
                                            rclpy.duration.Duration(seconds=.015))
            self.world_from_cloud = _transform_matrix(value)
            self.world_from_camera = self.world_from_cloud @ np.linalg.inv(self.tcl)
            self.camera_world = self.world_from_camera[:3,3].tolist()
            self.map_tf_available = True
            self.pose_error = ''
        except Exception as exc:
            self.pose_error = str(exc)
            self.camera_world = None
        # Resolve base independently: a missing global localization must not
        # destroy a usable local measurement or fabricate a base transform.
        self.base_from_camera = None
        self.base_pose_error = ''
        try:
            value = self.tf.lookup_transform(self.base_frame, self.cloud_frame, stamp,
                                            rclpy.duration.Duration(seconds=.015))
            self.base_from_camera = _transform_matrix(value) @ np.linalg.inv(self.tcl)
        except Exception as exc:
            self.base_pose_error = str(exc)

    def _current_measurements(self, observations, sync_ok):
        boxes=[]
        for obs in observations:
            if obs.get('conflict_only') or 'center_camera_optical' not in obs:
                continue
            center=np.asarray(obs['center_camera_optical'])
            normal=np.asarray(obs['normal_camera_optical'])
            horizontal=np.asarray(obs['horizontal_camera_optical'])
            def geometry(transform):
                if transform is None: return None
                return dict(position=(transform[:3,:3]@center+transform[:3,3]).tolist(),
                    normal=(transform[:3,:3]@normal).tolist(),
                    horizontal=(transform[:3,:3]@horizontal).tolist())
            boxes.append(dict(local_detection_id=obs['local_detection_id'],global_id=obs.get('global_id'),
                association_status=obs.get('association_status'),
                position_quality_ok=obs['position_quality_ok'],orientation_quality_ok=obs['orientation_quality_ok'],
                quality_source='CURRENT_RGB_AND_CLOUD_P01',measurement_quality=obs.get('measurement_quality',{}),
                size=obs['size_world'],thickness_source='PRIOR',
                camera_optical=geometry(np.eye(4)),world=geometry(self.world_from_camera),
                base=geometry(self.base_from_camera)))
        return dict(sensor_stamp=self.image_stamp,world_frame=self.world_frame,base_frame=self.base_frame,
            camera_coordinates='calib.cam_0 optical: right, down, forward',
            synchronized=sync_ok,world_pose_valid=self.map_tf_available,
            base_pose_valid=self.base_from_camera is not None,base_pose_error=self.base_pose_error,boxes=boxes)

    def process(self):
        started = time.monotonic()
        image = self.image.copy()
        points = self._read_cloud()
        self.last_processed_cloud_stamp = self.cloud_stamp
        self.sample_sequence += 1
        self._resolve_world_pose()
        depth, camera_points = self._depth_image(points)
        accepted, rejected, split_diagnostics, raw_count = self._strict_observations(image, depth, points)
        rgb_depth_dt = abs(self.image_stamp - self.cloud_stamp)
        _, odom_stamp = self._nearest_odom(self.image_stamp)
        rgb_odom_dt = abs(self.image_stamp - odom_stamp) if odom_stamp is not None else float('inf')
        c = self.mapper.config
        # Both localization modes use the exact RGB-time TF, never the
        # nearest odometry message. Publication latency on that unused stream
        # must not invalidate an available timestamped transform.
        rgb_pose_dt = 0. if self.map_tf_available else float('inf')
        sync_ok = (rgb_depth_dt * 1000 <= c['max_rgb_depth_dt_ms']
                   and rgb_pose_dt * 1000 <= c['max_pose_dt_ms'])
        if self.args.mode == 'epoch':
            if not self.epoch_samples: self.epoch_started_at = time.monotonic()
            self.epoch_samples.append(dict(observations=accepted, depth=depth,
                pose=None if self.world_from_camera is None else self.world_from_camera.copy(),
                intr=dict(self.intr), stamp=self.image_stamp, cloud_stamp=self.cloud_stamp, sync_ok=sync_ok))
            count=len(self.epoch_samples)
            if count < c['epoch_frames']:
                result=dict(commit=False,reason='EPOCH_COLLECTING',revision=self.mapper.revision)
            else:
                result=self.mapper.process_epoch(self.epoch_samples)
                self.epoch_samples=[];self.epoch_started_at=None
            self.epoch_progress=dict(state='COMMITTED' if result['commit'] else result['reason'],
                collected=count,required=c['epoch_frames'],revision=self.mapper.revision)
            if not result['commit']:
                for obs in accepted:obs.update(global_id=None,association_status=result['reason'])
        else:
            result = self.mapper.process(accepted, depth, self.world_from_camera, self.intr, self.image_stamp, sync_ok)
        self.frame = self.mapper.frame
        self._save_geometry_evidence(image, depth, accepted)
        self.map_boxes = self.mapper.objects
        snapshot = self.mapper.snapshot()
        self.frame_events = snapshot['events'] if result['commit'] else []
        snapshot['events'] = self.frame_events
        for event in self.frame_events:
            msg=String(); msg.data=json.dumps(event); self.event_pub.publish(msg)
        preview = image.copy()
        matched_gids = {obs.get('global_id') for obs in accepted
                        if obs.get('global_id') is not None}
        color_by_state = {'TENTATIVE':(0,220,255), 'CONFIRMED':(255,255,0),
                          'MISSING_CANDIDATE':(0,140,255), 'VACATED':(0,0,255)}
        for gid,prediction in self.mapper.predictions.items():
            if gid in matched_gids:
                continue
            contours,_ = cv2.findContours(prediction['mask'].astype('uint8'), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            color=(180,90,20)
            if gid in self.map_boxes:
                item=self.map_boxes[gid]
                if item['last_evidence']=='CLEAR' or item['state']=='VACATED':color=(0,0,255)
                elif item['state']=='MISSING_CANDIDATE':color=(0,140,255)
            cv2.drawContours(preview,contours,-1,color,2)
            if contours:
                x, y, _, _ = cv2.boundingRect(max(contours, key=cv2.contourArea))
                evidence = self.map_boxes.get(gid, {}).get('last_evidence', 'UNKNOWN')
                cv2.putText(preview, f'#{gid} MAP {evidence}', (x, max(22, y+22)),
                            cv2.FONT_HERSHEY_SIMPLEX, .55, color, 1, cv2.LINE_AA)
        for obs in accepted:
            gid=obs.get('global_id');status=obs.get('association_status','')
            color=(0,220,0)
            if gid is not None:color=color_by_state.get(self.map_boxes[gid]['state'],color)
            if status=='DETECTOR_SPLIT':color=(0,255,255)
            if status=='DETECTOR_MERGE':color=(255,0,255)
            if status=='IDENTITY_PENDING':color=(0,165,255)
            contours,_=cv2.findContours(obs['_mask'].astype('uint8'),cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
            cv2.drawContours(preview,contours,-1,color,2)
            x1,y1,x2,y2=obs['bbox']
            label=f'D{obs["local_detection_id"]} {status}'
            if gid is not None:
                item=self.map_boxes[gid]
                label=f'#{gid} P={item["existence_probability"]:.2f}'
                if status=='MATCHED_VISIBILITY_ONLY':label=f'#{gid} SEEN / LAST POSE'
                if not obs.get('orientation_quality_ok',True):label+=' POSE HOLD' if item.get('orientation_trusted') else ' POSE ?'
            elif status=='DETECTOR_MERGE':label='MERGE '+str(obs.get('global_ids'))
            elif status=='IDENTITY_PENDING':label=f'D{obs["local_detection_id"]} MEASURED / ID ?'
            elif status=='RETURN_LOCATION_PENDING':label=f'RETURN #{obs["candidate_global_ids"][0]} {obs.get("return_support",0)}/3'
            elif status in ('STRICT_REJECTED','LOW_GEOMETRY_QUALITY'):
                label=f'D{obs["local_detection_id"]} BOX / POSITION ?'
            elif status.startswith('REDUNDANT_'):
                # Composite duplicates stay in diagnostics without covering
                # the valid individual carton labels in the result image.
                continue
            scale=.78
            (tw,th),baseline=cv2.getTextSize(label,cv2.FONT_HERSHEY_SIMPLEX,scale,2)
            lx=max(0,min(x1,preview.shape[1]-tw-12));ly=max(th+12,y1-8)
            cv2.rectangle(preview,(lx,ly-th-8),(lx+tw+10,ly+baseline+4),(20,27,28),-1)
            cv2.putText(preview,label,(lx+5,ly),cv2.FONT_HERSHEY_SIMPLEX,scale,color,2,cv2.LINE_AA)
        for obs in rejected:
            x1,y1,x2,y2=obs['bbox']
            cv2.rectangle(preview,(x1,y1),(x2,y2),(0,0,255),1)
            if all(obs is not candidate for candidate in accepted):
                cv2.putText(preview,f'D{obs["local_detection_id"]} BOX / POSITION ?',(max(0,x1),max(22,y1-8)),0,.65,(0,100,255),2)
        self._publish_live_cloud(points)
        current=self._json_safe(self._current_measurements(accepted,sync_ok))
        self._atomic_json('current_measurements.json',current)
        msg=String();msg.data=json.dumps(current,allow_nan=False);self.measurement_pub.publish(msg)
        payload=dict(snapshot, frame=self.frame, stamp=time.time(), sensor_stamp=self.image_stamp,
            sample_sequence=self.sample_sequence,epoch=self.epoch_progress,
            mode=self.args.mode, enabled=self.enabled, commit=result, session_id=self.session_id,
            strict_get_box_node={'operation':'P01','minimum_confidence':self.args.conf,'raw_yolo':raw_count,
                                 'inference_size':self.args.imgsz,
                                 'accepted':sum(not o.get('conflict_only') for o in accepted),'rejected':len(rejected),'splits':split_diagnostics},
            observations=[{k:v for k,v in obs.items() if k!='measurement'} for obs in accepted], rejected=rejected,
            map_frame=self.world_frame,base_frame=self.base_frame,current_measurements=current,
            map_source=self.args.localization_source+' timestamped TF' if self.map_tf_available else 'WORLD TF UNAVAILABLE',
            global_localization=self.map_tf_available, camera_world=self.camera_world,
            world_from_camera=self.world_from_camera, config=self.mapper.config,
            time_sync={'ok':sync_ok,'rgb_depth_dt_ms':rgb_depth_dt*1000,'rgb_pose_dt_ms':rgb_pose_dt*1000,
                       'rgb_odom_dt_ms':rgb_odom_dt*1000,'pose_source':'RGB_TIMESTAMP_TF',
                       'pose_valid':self.map_tf_available,'pose_error':self.pose_error,'pose_anomaly':self.mapper.pose_anomaly_latched},
            processing_ms=(time.monotonic()-started)*1000)
        payload=self._json_safe(payload)
        self._atomic_image('debug.jpg',preview)
        self._atomic_json('latest.json',payload)
        self._atomic_json('persistent_map.json',{'map_frame':self.world_frame,'session_id':self.session_id,**snapshot})
        map_msg=String();map_msg.data=json.dumps(self._json_safe({'map_frame':self.world_frame,**snapshot}))
        self.map_pub.publish(map_msg)
        self._publish_policy(snapshot, commit=result['commit'])
        # Keep an append-only audit without writing a full-resolution JPEG on
        # every inference. The operator explicitly saves review snapshots.
        with (self.out/'events.jsonl').open('a') as file:
            for event in self.frame_events:file.write(json.dumps(event)+'\n')
        self.publish_markers()

    def _publish_policy(self, snapshot, commit):
        active=self._json_safe(snapshot['active_box_map'])
        self._atomic_json('active_boxes.json',active)
        msg=String();msg.data=json.dumps(active,allow_nan=False);self.active_pub.publish(msg)
        if commit:
            delta=self._json_safe(snapshot['map_delta'])
            self._atomic_json('map_delta.json',delta)
            msg=String();msg.data=json.dumps(delta,allow_nan=False);self.delta_pub.publish(msg)
        elif not snapshot['map_delta']:
            # A new/reset session must not offer the previous session's last
            # delta as its download. This marker is not a committed ROS delta.
            self._atomic_json('map_delta.json',dict(session_id=active['session_id'],
                map_id=active['map_id'],revision=active['revision'],reason='NO_COMMIT',
                added=[],removed=[],updated=[],kept=[]))

    def _publish_live_cloud(self, points, transform=None, stamp=None):
        cloud_map=[]
        transform=self.world_from_cloud if transform is None else transform
        stamp=self.image_stamp if stamp is None else stamp
        if transform is not None and len(points):
            selected=points[::max(1,math.ceil(len(points)/10000))]
            cloud_map=(selected@transform[:3,:3].T+transform[:3,3]).tolist()
        camera_pose=transform@np.linalg.inv(self.tcl) if transform is not None else None
        self.preview_pose_valid=transform is not None
        self.preview_stamp=stamp
        self._atomic_json('live_cloud.json',{'frame':self.world_frame,'stamp':stamp,'pose_valid':transform is not None,
            'points':cloud_map, 'world_from_camera':self._json_safe(camera_pose)})

    def publish_markers(self):
        from visualization_msgs.msg import Marker, MarkerArray
        arr=MarkerArray(); clear=Marker();clear.action=Marker.DELETEALL;arr.markers.append(clear)
        for gid,item in self.map_boxes.items():
            if item['state']=='VACATED':continue
            marker=Marker();marker.header.frame_id=self.world_frame;marker.ns='persistent_boxes';marker.id=gid
            marker.type=Marker.LINE_LIST;marker.action=Marker.ADD;marker.pose.orientation.w=1.
            marker.scale.x=.012;marker.color.r=.2;marker.color.g=1.;marker.color.b=.4;marker.color.a=1.
            from geometry_msgs.msg import Point
            corners=item['cuboid_corners_world']
            for a,b in [(0,1),(1,2),(2,3),(3,0),(4,5),(5,6),(6,7),(7,4),(0,4),(1,5),(2,6),(3,7)]:
                for i in (a,b):
                    p=Point();p.x,p.y,p.z=map(float,corners[i]);marker.points.append(p)
            arr.markers.append(marker)
        self.marker_pub.publish(arr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('realtime','manual','epoch'), default='manual')
    parser.add_argument('--world-frame',default='map')
    parser.add_argument('--base-frame',default='base_link')
    parser.add_argument('--localization-source',choices=('odin','tf'),default='odin',
                        help='odin: test localization map; tf: robot TF, no test map file dependency')
    parser.add_argument('--model', type=Path, default=Path('/home/nvidia/perception_domain_nx/src/robot_perception/assets/box_seg.pt'))
    parser.add_argument('--calib', type=Path, default=Path('/home/nvidia/perception_domain_nx/runtime/odin1/calibration/calib.yaml'))
    parser.add_argument('--output', type=Path, default=Path('/home/nvidia/perception_domain_nx/runtime/results/odin_persistent_box_test'))
    parser.add_argument('--conf', type=float, default=.70)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--imgsz', type=int, default=640, choices=(640,960,1280,1600))
    parser.add_argument('--device', default='0')
    parser.add_argument('--association-m', type=float, default=.20, dest='association_m')
    parser.add_argument('--confirm-hits', type=int, default=3, dest='confirm_hits')
    from rclpy.signals import SignalHandlerOptions
    stop_requested=False
    def request_stop(signum,frame):
        nonlocal stop_requested
        stop_requested=True
    signal.signal(signal.SIGINT,request_stop)
    signal.signal(signal.SIGTERM,request_stop)
    args = parser.parse_args(); rclpy.init(signal_handler_options=SignalHandlerOptions.NO); node = OdinPersistentBoxTest(args)
    executor=rclpy.executors.SingleThreadedExecutor();executor.add_node(node)
    try:
        while not stop_requested and rclpy.ok():executor.spin_once(timeout_sec=.2)
    except KeyboardInterrupt: pass
    finally:
        executor.shutdown();executor.remove_node(node)
        node.listener.executor.shutdown()
        node.listener.dedicated_listener_thread.join()
        node.listener.unregister()
        node.tf_node.destroy_node()
        node.destroy_node()
        if rclpy.ok(): rclpy.shutdown()


if __name__ == '__main__':
    main()
