#!/usr/bin/env python3
"""Read the validated MAPV0001/version-5 LiDAR keyframe section, read-only.

This layout was verified against the local Odin 0.13.3 saved maps. It is not
an SDK promise: reject other layouts rather than interpreting arbitrary bytes.
The visual descriptor section is deliberately not interpreted.
"""
import argparse
import json
import struct
from pathlib import Path

import numpy as np


def read_map(path):
    data = Path(path).read_bytes()
    if len(data) < 36 or data[:8] != b'MAPV0001':
        raise ValueError('Not an Odin MAPV0001 map')
    version, visual_size, lidar_size, tail_size = struct.unpack_from('<IQQQ', data, 8)
    if version != 5 or 36 + visual_size + lidar_size + tail_size != len(data):
        raise ValueError('Unsupported version or invalid section sizes')
    section = memoryview(data)[36 + visual_size:36 + visual_size + lidar_size]
    offset = 0

    def uint():
        nonlocal offset
        value = struct.unpack_from('<I', section, offset)[0]
        offset += 4
        return value

    count, descriptor_count = uint(), uint()
    if not 0 < count < 100000 or count != descriptor_count:
        raise ValueError('Invalid keyframe count')
    for _ in range(count):
        rows, cols = uint(), uint()
        if (rows, cols) != (20, 60):
            raise ValueError('Unsupported scan-context shape')
        offset += rows * cols * 8
    if uint() != count:
        raise ValueError('Ring-key count mismatch')
    for _ in range(count):
        if uint() != 20:
            raise ValueError('Ring-key shape mismatch')
        offset += 20 * 4
    poses, clouds = [], []
    for index in range(count):
        if uint() != index:
            raise ValueError('Unexpected keyframe index')
        pose = np.frombuffer(section, dtype='<f8', count=16, offset=offset).reshape(4, 4, order='F').copy()
        offset += 128
        if not np.isfinite(pose).all() or not np.allclose(pose[3], [0, 0, 0, 1]):
            raise ValueError('Invalid homogeneous pose')
        rotation = pose[:3, :3]
        if not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-4) or not np.isclose(np.linalg.det(rotation), 1, atol=1e-4):
            raise ValueError('Non-rigid keyframe pose')
        points = uint()
        if not 0 < points <= 1000000 or offset + points * 12 + 4 > len(section):
            raise ValueError('Invalid point count')
        cloud = np.frombuffer(section, dtype='<f4', count=points * 3, offset=offset).reshape(-1, 3).copy()
        offset += points * 12
        if not np.isfinite(cloud).all() or np.max(np.abs(cloud)) > 100000:
            raise ValueError('Invalid point coordinates')
        if uint() != 0:
            raise ValueError('Unsupported keyframe extension')
        poses.append(pose)
        clouds.append(cloud)
    if offset != len(section):
        raise ValueError('Unconsumed LiDAR section bytes')
    return np.array(poses), clouds


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('map', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    poses, clouds = read_map(args.map)
    # These saved scan-context clouds already have world-aligned axes, but
    # retain the keyframe's local origin. Applying R a second time rotates
    # horizontal floors/ceilings incorrectly. Only restore the saved origin.
    world = np.concatenate([cloud + pose[:3, 3] for pose, cloud in zip(poses, clouds)]).astype('<f4')
    args.output.mkdir(parents=True, exist_ok=True)
    world.tofile(args.output / 'points.f32')
    poses[:, :3, 3].astype('<f4').tofile(args.output / 'trajectory.f32')
    metadata = {'source': str(args.map.resolve()), 'format': 'MAPV0001', 'version': 5,
                'keyframes': len(poses), 'points': len(world), 'bounds': [world.min(0).tolist(), world.max(0).tolist()],
                'geometry': 'All saved LiDAR keyframes; world-aligned axes with stored keyframe origins restored',
                'coordinate_validation': 'Local version-5 layout: rigid poses and complete section length checked; axes inferred from horizontal plane alignment',
                'representation': 'Sparse localization keyframes, not a dense RGB reconstruction'}
    (args.output / 'metadata.json').write_text(json.dumps(metadata, indent=2))
    with (args.output / (args.map.stem + '.ply')).open('wb') as file:
        file.write(('ply\nformat binary_little_endian 1.0\nelement vertex %d\nproperty float x\nproperty float y\nproperty float z\nend_header\n' % len(world)).encode('ascii'))
        file.write(world.tobytes())
    print(json.dumps(metadata, indent=2))


if __name__ == '__main__':
    main()
