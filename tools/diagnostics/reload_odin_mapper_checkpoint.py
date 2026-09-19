#!/usr/bin/env python3
"""Reload the diagnostic mapper without resetting its IDs or Odin driver.

Explicit --duplicate NEW:OLD entries are operator-reviewed identity repairs,
never automatic identity merging. Both pre-repair state and history survive.
"""
import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
import time
import urllib.request


def command(action):
    request = urllib.request.Request('http://127.0.0.1:8765/api/control',
        data=json.dumps(dict(action=action)).encode(), headers={'Content-Type':'application/json'})
    return json.load(urllib.request.urlopen(request, timeout=15))


def restore_records(latest, checkpoint, ids, aliases, reason):
    """Undo reviewed identity repairs without rolling back other records."""
    if not latest.get('session_id') or latest.get('session_id') != checkpoint.get('session_id'):
        raise ValueError('Record restore requires the same mapping session')
    objects={o['global_id']:o for o in latest['map_history']}
    saved={o['global_id']:o for o in checkpoint['map_history']}
    if not reason or any(gid not in objects or gid not in saved for gid in ids):
        raise ValueError('Restore requires existing IDs, a checkpoint, and an audit reason')
    for gid in ids:
        current=objects[gid];restored=copy.deepcopy(saved[gid])
        event=dict(event='OPERATOR_IDENTITY_REPAIR_REVERTED',global_id=gid,
            stamp=latest['sensor_stamp'],frame=latest['frame'],detail=dict(reason=reason,
                previous_center=current['center_map'],restored_center=restored['center_map']))
        restored['events']=(current.get('events',[])+[event])[-100:]
        objects[gid]=restored
    for source_id,target_id in aliases:
        source,target=objects[source_id],objects[target_id]
        if source_id==target_id or target['state']=='VACATED':
            raise ValueError('Alias correction requires a distinct active target')
        source.pop('relocated_to',None)
        source.pop('known_locations',None)
        source.update(state='VACATED',invalidated_duplicate_of=target_id,physical_identity_verified=False)
        source['events']=(source.get('events',[])+[dict(event='OPERATOR_DUPLICATE_CORRECTION',
            global_id=source_id,stamp=latest['sensor_stamp'],frame=latest['frame'],
            detail=dict(canonical_id=target_id,reason=reason))])[-100:]
    latest['map_history']=list(objects.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duplicate', action='append', default=[], metavar='NEW:OLD')
    parser.add_argument('--returned',action='append',default=[],metavar='MOVED:ORIGINAL',
                        help='Operator confirms the moved physical box has returned to its original record')
    parser.add_argument('--keep-canonical-geometry', action='store_true',
                        help='Invalidate reviewed duplicates without copying their measurements into the canonical record')
    parser.add_argument('--remeasure',action='append',default=[],metavar='GLOBAL:DETECTION',
                        help='Audited correction from an explicitly reviewed current physical box; never automatic association')
    parser.add_argument('--reason',help='Required audit explanation for explicit remeasurement')
    parser.add_argument('--restore-from',type=Path,help='Checkpoint before an incorrect identity repair')
    parser.add_argument('--restore-id',type=int,action='append',default=[])
    parser.add_argument('--invalidate-alias',action='append',default=[],metavar='ALIAS:ACTUAL',
                        help='Correct an already retired alias created by an erroneous repair')
    args = parser.parse_args()
    returns=[tuple(map(int,value.split(':'))) for value in args.returned]
    pairs = [tuple(map(int, value.split(':'))) for value in args.duplicate]+returns
    remeasure=[tuple(map(int,value.split(':'))) for value in args.remeasure]
    aliases=[tuple(map(int,value.split(':'))) for value in args.invalidate_alias]
    restore=json.loads(args.restore_from.read_text()) if args.restore_from else None
    if (args.restore_id or aliases) and (restore is None or not args.reason):
        raise ValueError('Identity repair reversal requires --restore-from and --reason')
    if (remeasure or returns) and not args.reason:raise ValueError('--remeasure/--returned requires --reason')
    if len({i for pair in pairs for i in pair}) != 2 * len(pairs):
        raise ValueError('Duplicate and canonical IDs must be distinct')
    state = json.load(urllib.request.urlopen('http://127.0.0.1:8765/api/state'))
    if not state['running'] or len(state['driver_pids']) != 1:
        raise ValueError('Requires one running driver and an owned running mapper')
    root = Path('/home/nvidia/perception_domain_nx')
    out = root / 'runtime/results/odin_browser'
    # Validate targets before interrupting any process.
    objects = {o['global_id']:o for o in state['latest']['map_history']}
    if restore is not None:
        restore_records(copy.deepcopy(state['latest']),restore,args.restore_id,aliases,args.reason)
    for new, old in pairs:
        if new not in objects or old not in objects or objects[new]['state'] == 'VACATED':
            raise ValueError(f'Invalid identity repair {new}:{old}')
    print('Backup:', command('snapshot')['saved'], flush=True)
    enabled = state['control']['enabled']
    command('pause')
    command('stop')
    latest = json.loads((out/'latest.json').read_text())
    # The stopped worker's final state is the authoritative checkpoint.
    backup = out / ('checkpoint_before_reload_' + str(time.time_ns()) + '.json')
    backup.write_text(json.dumps(latest))
    if restore is not None:
        restore_records(latest,restore,args.restore_id,aliases,args.reason)
    objects = {o['global_id']:o for o in latest['map_history']}
    for new, old in pairs:
        source, target = objects[new], objects[old]
        if source['state'] == 'VACATED':
            raise ValueError('State changed during reload; checkpoint preserved, no repair applied')
        returning=(new,old) in returns
        record = dict(event='OPERATOR_RETURN_CONFIRMED' if returning else 'OPERATOR_DUPLICATE_CORRECTION', global_id=old,
            stamp=time.time(), frame=latest['frame'],
            detail=dict(duplicate_id=new, canonical_id=old,
                        reason=args.reason if returning else 'Reviewed static-scene RGB and event history; same physical carton'))
        if not args.keep_canonical_geometry:
            for key in ('center_map','normal_world','horizontal_world','size_world','cuboid_corners_world',
                        'covariance','bbox','depth_camera_m','last_seen','last_seen_frame','last_geometry_update',
                        'measured_surfaces','geometry_anchor','geometry_quality','orientation_trusted',
                        'orientation_observed_ok','position_quality_ok','last_orientation_update'):
                if key in source:target[key] = copy.deepcopy(source[key])
        target.update(state='CONFIRMED', ever_confirmed=True, visibility_state='UNKNOWN',
                      last_evidence='UNKNOWN', consecutive_clear_frames=0, consecutive_missed_frames=0,
                      exist_positive=12., exist_negative=0., existence_probability=13/14)
        target['events'] = (target.get('events', []) + [record])[-100:]
        source.update(state='VACATED', vacated_at=time.time(),last_evidence=record['event'])
        if returning:
            source['relocated_to']=old
            target.update(identity_basis='OPERATOR_CONFIRMED_RETURN',physical_identity_verified=True)
            target.pop('vacated_at',None)
        else:source['invalidated_duplicate_of']=old
        source['events'] = (source.get('events', []) + [record])[-100:]
    if remeasure:
        sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
        import numpy as np
        from camera_unload_perception.algorithms.persistent_carton_map import cuboid,measured_surface,unit
        if not latest['time_sync']['pose_valid'] or latest['time_sync']['rgb_depth_dt_ms']>20:
            raise ValueError('Remeasurement requires synchronized RGB/cloud and timestamped global TF')
        detections={o['local_detection_id']:o for o in latest['observations']}
        for gid,local_id in remeasure:
            target=objects[gid];obs=detections[local_id]
            if target['state']=='VACATED' or not obs.get('position_quality_ok') or not obs.get('orientation_quality_ok'):
                raise ValueError('Remeasurement requires an active record and valid current pose measurement')
            record=dict(event='OPERATOR_MEASUREMENT_CORRECTION',global_id=gid,stamp=latest['sensor_stamp'],
                frame=latest['frame'],detail=dict(local_detection_id=local_id,reason=args.reason,
                    old_center=target['center_map'],new_center=obs['center_map']))
            for key in ('center_map','normal_world','horizontal_world','size_world','bbox','depth_camera_m','confidence'):
                target[key]=copy.deepcopy(obs[key])
            n=unit(target['normal_world']);x=np.asarray(target['horizontal_world'])
            target['normal_world']=n.tolist();target['horizontal_world']=unit(x-n*np.dot(x,n)).tolist()
            target['measured_surfaces']=[measured_surface(obs)]
            target['cuboid_corners_world']=cuboid(target).tolist()
            target['geometry_quality']=copy.deepcopy(obs['measurement_quality'])
            target.update(position_quality_ok=True,orientation_trusted=True,orientation_observed_ok=True,
                last_geometry_update=latest['sensor_stamp'],last_orientation_update=latest['sensor_stamp'],
                last_evidence='UNKNOWN',visibility_state='UNKNOWN',consecutive_clear_frames=0)
            target['events']=(target.get('events',[])+[record])[-100:]
    latest['map_history'] = list(objects.values())
    driver_pid = state['driver_pids'][0]
    latest.update(driver_pid=driver_pid,
        driver_start=(Path('/proc')/str(driver_pid)/'stat').read_text().split(') ')[1].split()[19],
        calib_sha256=hashlib.sha256((root/'runtime/odin1/calibration/calib.yaml').read_bytes()).hexdigest())
    checkpoint = out/'resume_checkpoint.json'
    if checkpoint.exists():
        raise ValueError('An unconsumed checkpoint already exists')
    checkpoint.write_text(json.dumps(latest))
    result = command('start')
    if not enabled:
        command('pause')
    print(json.dumps(dict(worker_pid=result['worker_pid'], driver_pid=driver_pid,
                          checkpoint_backup=str(backup), repairs=pairs,returns=returns,remeasurements=remeasure,
                          restored_ids=args.restore_id,corrected_aliases=aliases)), flush=True)


if __name__ == '__main__':
    main()
