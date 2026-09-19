#!/usr/bin/env python3
"""Read-only time-window audit of live carton identities and visibility."""
import argparse
import json
from pathlib import Path
import time

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--seconds',type=float,default=90)
parser.add_argument('--expected-ids',required=True)
parser.add_argument('--expected-visible',help='Visible subset; defaults to all expected records')
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args();expected=set(map(int,args.expected_ids.split(',')))
expected_visible=set(map(int,args.expected_visible.split(','))) if args.expected_visible else expected
if not expected_visible<=expected:raise ValueError('Visible IDs must be a subset of records')
source=Path('/home/nvidia/perception_domain_nx/runtime/results/odin_browser/latest.json')
end=time.monotonic()+args.seconds;frames=[];seen=set()
seen.add(json.loads(source.read_text())['sensor_stamp'])
while time.monotonic()<end:
    data=json.loads(source.read_text());stamp=data['sensor_stamp']
    if stamp not in seen:
        seen.add(stamp)
        observations=data['observations']
        qualified=[o for o in observations if o.get('position_quality_ok') and not o.get('conflict_only')]
        frames.append(dict(frame=data['frame'],stamp=stamp,commit=data['commit'],sync=data['time_sync'],
            ids=[o['global_id'] for o in data['map']],
            visible=[o['global_id'] for o in data['map'] if o['visibility_state']=='VISIBLE'],
            qualified_count=len(qualified),qualified_ids=[o.get('global_id') for o in qualified],
            observations=[{k:o.get(k) for k in ('local_detection_id','global_id','association_status',
                'position_quality_ok','center_map','depth_camera_m')} for o in observations],events=data['events']))
        if len(frames)%10==0:print(json.dumps(dict(samples=len(frames),last=frames[-1]['ids'],visible=frames[-1]['visible'])),flush=True)
    time.sleep(.5)
eligible=[f for f in frames if f['commit']['commit'] and f['qualified_count']==len(expected_visible)]
failures=[f for f in frames if set(f['ids'])!=expected]
unassociated=[f for f in eligible if set(f['qualified_ids'])!=expected_visible]
summary=dict(samples=len(frames),duration_s=args.seconds,expected_ids=sorted(expected),
    id_failures=len(failures),eligible_frames=len(eligible),eligible_unassociated=len(unassociated),
    expected_visible=sorted(expected_visible),
    all_visible_frames=sum(set(f['visible'])==expected_visible for f in frames),
    failed_commits=sum(not f['commit']['commit'] for f in frames))
args.output.write_text(json.dumps(dict(summary=summary,frames=frames),allow_nan=False))
print(json.dumps(summary),flush=True)
if not eligible or failures or unassociated:raise SystemExit(1)
