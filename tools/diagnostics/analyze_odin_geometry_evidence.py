#!/usr/bin/env python3
"""Inspect fixed image patches, independent of YOLO masks and map fusion."""
import argparse
import json
from pathlib import Path

import cv2
import numpy as np
import yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    calibration = yaml.safe_load(Path('/home/nvidia/perception_domain_nx/runtime/odin1/calibration/calib.yaml').read_text())
    tcl = np.array(calibration['Tcl_0']).reshape(4, 4)
    k = calibration['cam_0']
    # Fixed interior patches of this captured four-carton scene, not detector output.
    patches = [[980, 500, 1120, 625], [980, 690, 1110, 815],
               [980, 880, 1110, 1010], [795, 855, 915, 975]]
    rows = []
    for path in sorted(args.directory.glob('*.npz')):
        raw = np.load(path)['raw_cloud']
        xyz = np.column_stack([raw[key].ravel() for key in ('x', 'y', 'z')])
        camera = xyz @ tcl[:3, :3].T + tcl[:3, 3]
        z = camera[:, 2]
        u = k['A11'] * camera[:, 0] / np.maximum(z, .001) + k['u0']
        v = k['A22'] * camera[:, 1] / np.maximum(z, .001) + k['v0']
        meta = json.loads(path.with_suffix('.json').read_text())
        row = dict(frame=path.stem, rgb_stamp=meta['rgb_stamp'],
                   sync_ms=1000 * abs(meta['rgb_stamp'] - meta['cloud_stamp']),
                   translation=meta['map_from_cloud_translation'],
                   quaternion=meta['map_from_cloud_quaternion_xyzw'], patches=[])
        valid = (z > .2) & (z < 8)
        row['right_near_points'] = int(np.sum(valid & (z < .8) & (u > 1200) & (u < 1600)))
        for x1, y1, x2, y2 in patches:
            mask = valid & (u > x1) & (u < x2) & (v > y1) & (v < y2)
            points = camera[mask]
            if len(points) < 20:
                row['patches'].append(None)
                continue
            center = points.mean(0)
            _, vectors = np.linalg.eigh((points - center).T @ (points - center))
            normal = vectors[:, 0]
            if normal[2] > 0:
                normal = -normal
            row['patches'].append(dict(depth=float(np.median(z[mask])), normal=normal.tolist(),
                                       confidence=float(np.median(raw['confidence'].ravel()[mask]))))
        rows.append(row)
        if int(path.stem) % 20 == 0:
            image = cv2.imread(str(path.with_suffix('.jpg')))
            for x, y, distance in zip(u[valid][::3], v[valid][::3], z[valid][::3]):
                if 0 <= x < image.shape[1] and 0 <= y < image.shape[0]:
                    color = (255, 0, 255) if distance < .8 else (0, 220, 0)
                    cv2.circle(image, (round(x), round(y)), 2, color, -1)
            cv2.imwrite(str(args.directory / (path.stem + '_projection.jpg')), image)
    (args.directory / 'fixed_patch_report.json').write_text(json.dumps(rows, indent=2))
    print(json.dumps(dict(frames=len(rows), sync_max_ms=max(r['sync_ms'] for r in rows),
                          output=str(args.directory / 'fixed_patch_report.json')), indent=2))


if __name__ == '__main__':
    main()
