#!/usr/bin/env python3
"""Offline replay of recorded RGB/cloud/TF. No ROS publishers or USB access."""
import argparse
import json
from pathlib import Path
import time
from types import SimpleNamespace

import cv2
import numpy as np

from odin_persistent_box_test import OdinPersistentBoxTest, PersistentCartonMap, YOLO, _q_matrix


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--step', type=int, default=4)
    parser.add_argument('--output', type=Path, help='Write a separate report without replacing the original audit')
    args = parser.parse_args()
    root = Path('/home/nvidia/perception_domain_nx')
    # Only the pure observation functions are used; do not initialize Node.
    adapter = object.__new__(OdinPersistentBoxTest)
    adapter.args = SimpleNamespace(calib=root/'runtime/odin1/calibration/calib.yaml',
                                   conf=.7, imgsz=640, device='0')
    adapter.load_calib()
    adapter.mapper = PersistentCartonMap()
    adapter.strict_conf = .7
    adapter.box_length = .4
    adapter.mask_erode_px = 5
    adapter.max_points_per_box = 1500
    adapter.model = YOLO(str(root/'src/robot_perception/assets/box_seg.pt'))
    frames = []
    paths=sorted(args.directory.glob('*.npz'),key=lambda p: json.loads(p.with_suffix('.json').read_text()).get('sensor_stamp',
        json.loads(p.with_suffix('.json').read_text()).get('rgb_stamp',0)))
    for path in paths[::args.step]:
        start = time.monotonic()
        raw = np.load(path)['raw_cloud']
        points = np.column_stack([raw[key].ravel() for key in ('x', 'y', 'z')])
        meta = json.loads(path.with_suffix('.json').read_text())
        if 'world_from_camera' in meta:
            adapter.tcl=np.asarray(meta['tcl']);adapter.intr=meta['intrinsics']
            adapter.world_from_camera=np.asarray(meta['world_from_camera'])
            adapter.world_from_cloud=adapter.world_from_camera@adapter.tcl
        else:
            q = SimpleNamespace(**dict(zip('xyzw', meta['map_from_cloud_quaternion_xyzw'])))
            adapter.world_from_cloud = np.eye(4)
            adapter.world_from_cloud[:3, :3] = _q_matrix(q)
            adapter.world_from_cloud[:3, 3] = meta['map_from_cloud_translation']
            adapter.world_from_camera = adapter.world_from_cloud @ np.linalg.inv(adapter.tcl)
        adapter.image_stamp = meta.get('sensor_stamp',meta.get('rgb_stamp'))
        adapter.cloud_stamp = meta['cloud_stamp']
        adapter.image = cv2.imread(str(path.with_suffix('.jpg')))
        depth, _ = adapter._depth_image(points)
        obs, rejected, _, _ = adapter._strict_observations(adapter.image, depth, points)
        result = adapter.mapper.process(obs, depth, adapter.world_from_camera, adapter.intr,
                                        adapter.image_stamp, True)
        frames.append(adapter._json_safe(dict(source=path.stem, result=result,
            observations=[{k:v for k,v in o.items() if k!='measurement'} for o in obs],
            events=adapter.mapper.events, active_ids=[o['global_id'] for o in adapter.mapper.snapshot()['map']],
            next_id=adapter.mapper.next_id, processing_ms=(time.monotonic()-start)*1000)))
        if len(frames) % 10 == 0:
            print(f"replayed {len(frames)}: IDs {frames[-1]['active_ids']}, next_id {adapter.mapper.next_id}", flush=True)
    report = dict(frames=frames, final_map=adapter.mapper.snapshot())
    output = args.output or args.directory / 'replay_report.json'
    output.write_text(json.dumps(adapter._json_safe(report), allow_nan=False))
    print(f'Saved {output}; next_id={adapter.mapper.next_id}', flush=True)


if __name__ == '__main__':
    main()
