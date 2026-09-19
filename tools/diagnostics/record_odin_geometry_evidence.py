#!/usr/bin/env python3
"""Bounded, passive RGB/raw-cloud/TF capture; never opens or controls USB."""
import argparse
from collections import deque
import json
from pathlib import Path
import time

import cv2
import numpy as np
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image, PointCloud2
from sensor_msgs_py import point_cloud2
from tf2_ros import Buffer, TransformListener


def stamp(msg):
    return msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9


class Recorder(Node):
    def __init__(self, out):
        super().__init__('odin_geometry_evidence')
        self.out = out
        self.images = deque(maxlen=12)
        self.clouds = deque(maxlen=12)
        self.tf = Buffer()
        self.listener = TransformListener(self.tf, self)
        self.create_subscription(Image, '/odin1/image/undistorted', self.images.append, 5)
        self.create_subscription(PointCloud2, '/odin1/cloud_raw', self.clouds.append, 5)
        self.last = 0.
        self.count = 0
        self.create_timer(.5, self.save)

    def save(self):
        for rgb in reversed(self.images):
            ts = stamp(rgb)
            if ts <= self.last or not self.clouds:
                continue
            cloud = min(self.clouds, key=lambda c: abs(stamp(c) - ts))
            if abs(stamp(cloud) - ts) > .020:
                continue
            at = rclpy.time.Time(nanoseconds=int(ts * 1e9))
            if not self.tf.can_transform('map', cloud.header.frame_id, at):
                continue
            transform = self.tf.lookup_transform('map', cloud.header.frame_id, at).transform
            q, t = transform.rotation, transform.translation
            xyz = point_cloud2.read_points(cloud, skip_nans=False)
            image = np.ndarray((rgb.height, rgb.width, 3), dtype=np.uint8,
                               buffer=bytes(rgb.data), strides=(rgb.step, 3, 1))
            if rgb.encoding == 'rgb8':
                image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
            name = f'{self.count:04d}'
            np.savez_compressed(self.out / (name + '.npz'), raw_cloud=xyz)
            cv2.imwrite(str(self.out / (name + '.jpg')), image, [cv2.IMWRITE_JPEG_QUALITY, 95])
            meta = dict(rgb_stamp=ts, cloud_stamp=stamp(cloud), cloud_frame=cloud.header.frame_id,
                        map_from_cloud_translation=[t.x, t.y, t.z],
                        map_from_cloud_quaternion_xyzw=[q.x, q.y, q.z, q.w])
            latest = self.out.parent.parent / 'odin_browser' / 'latest.json'
            if latest.exists():
                state = json.loads(latest.read_text())
                meta['dashboard'] = {k: state.get(k) for k in
                                     ('frame', 'sensor_stamp', 'observations', 'events', 'world_from_camera')}
            (self.out / (name + '.json')).write_text(json.dumps(meta))
            self.last = ts
            self.count += 1
            if self.count % 10 == 0:
                print(f'captured {self.count} synchronized frames: {self.out}', flush=True)
            break


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=120)
    parser.add_argument('--max-frames', type=int, default=180)
    parser.add_argument('--max-mb', type=int, default=256)
    args = parser.parse_args()
    root = Path('/home/nvidia/perception_domain_nx/runtime/results/geometry_evidence')
    out = root / str(time.time_ns())
    out.mkdir(parents=True)
    print(out, flush=True)
    rclpy.init()
    node = Recorder(out)
    deadline = time.monotonic() + args.seconds
    try:
        while time.monotonic() < deadline and node.count < args.max_frames:
            rclpy.spin_once(node, timeout_sec=.1)
            if sum(p.stat().st_size for p in out.iterdir()) >= args.max_mb * 1024**2:
                break
    finally:
        print(f'finished: {node.count} frames in {out}', flush=True)
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
