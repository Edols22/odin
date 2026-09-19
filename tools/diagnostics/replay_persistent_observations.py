#!/usr/bin/env python3
"""Replay saved exact masks/depth/poses, without YOLO, ROS or camera access.

Optional explicit seed corrections are reported separately from algorithm
behavior; they are never inferred as automatic identity merging.
"""
import argparse
import copy
import json
from pathlib import Path
import sys
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from camera_unload_perception.algorithms.persistent_carton_map import PersistentCartonMap, measured_surface, cuboid


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    parser.add_argument('--seed',type=Path)
    parser.add_argument('--duplicate',action='append',default=[])
    parser.add_argument('--remeasure',action='append',default=[])
    parser.add_argument('--return-source',action='append',default=[],metavar='MOVED:ORIGINAL')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();mapper=PersistentCartonMap()
    if args.seed:
        seed=json.loads(args.seed.read_text())
        mapper.objects={o['global_id']:copy.deepcopy(o) for o in seed['map_history']}
        mapper.next_id=max(mapper.objects)+1
        for value in args.return_source:
            moved,original=map(int,value.split(':'))
            mapper.objects[moved].update(state='VACATED',relocated_to=original)
        for value in args.duplicate:
            new,old=map(int,value.split(':'));target=mapper.objects[old];source=mapper.objects[new]
            for key in ('center_map','normal_world','horizontal_world','size_world','measured_surfaces'):
                target[key]=copy.deepcopy(source[key])
            source.update(state='VACATED',invalidated_duplicate_of=old)
        detections={o['local_detection_id']:o for o in seed['observations']}
        for value in args.remeasure:
            gid,local=map(int,value.split(':'));target=mapper.objects[gid];obs=detections[local]
            for key in ('center_map','normal_world','horizontal_world','size_world'):
                target[key]=copy.deepcopy(obs[key])
            target['measured_surfaces']=[measured_surface(obs)]
            target['cuboid_corners_world']=cuboid(target).tolist()
    frames=[]
    files=sorted(args.directory.glob('*.json'),key=lambda p:json.loads(p.read_text())['sensor_stamp'])
    for path in files:
        meta=json.loads(path.read_text())
        with np.load(path.with_suffix('.npz')) as data:
            depth=data['depth'];masks=np.unpackbits(data['masks_packed'],axis=-1)[:,:,:depth.shape[1]].astype(bool)
        observations=copy.deepcopy(meta['observations'])
        assert len(masks)==len(observations)
        for obs,mask in zip(observations,masks):
            obs['_mask']=mask
            for key in ('global_id','global_ids','association_status','candidate_global_ids','replacement_evidence'):
                obs.pop(key,None)
        pose=None if meta['world_from_camera'] is None else np.asarray(meta['world_from_camera'],dtype=float)
        result=mapper.process(observations,depth,pose,meta['intrinsics'],meta['sensor_stamp'],
            abs(meta['sensor_stamp']-meta['cloud_stamp'])<=.020)
        active=[o for o in mapper.objects.values() if o['state']!='VACATED']
        frames.append(dict(stamp=meta['sensor_stamp'],commit=result,ids=[o['global_id'] for o in active],
            visible=[o['global_id'] for o in active if o['visibility_state']=='VISIBLE'],
            statuses=[o.get('association_status') for o in observations],events=mapper.events))
    report=dict(seed=str(args.seed),explicit_seed_corrections=dict(duplicates=args.duplicate,remeasurements=args.remeasure,
        confirmed_return_sources=args.return_source),
        frames=frames,final=mapper.snapshot(),next_id=mapper.next_id)
    args.output.write_text(json.dumps(report,allow_nan=False))
    print(json.dumps(dict(frames=len(frames),commits=sum(f['commit']['commit'] for f in frames),
        id_sets=sorted({tuple(f['ids']) for f in frames}),visible_counts={str(n):sum(len(f['visible'])==n for f in frames) for n in range(8)},
        next_id=mapper.next_id,output=str(args.output))))


if __name__=='__main__':main()
