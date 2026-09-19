"""Deterministic world-space replay cases; these are not physical acceptance tests."""
import copy
import unittest
from unittest.mock import patch

import numpy as np
from camera_unload_perception.algorithms.persistent_carton_map import PersistentCartonMap, project_cuboid, depth_support

INTR = dict(fx=240.,fy=240.,ppx=160.,ppy=120.)
SHAPE = (240,320)


def observation(center=(0.,0.,2.), pose=None):
    pose = np.eye(4) if pose is None else pose
    item = dict(center_map=list(center), normal_world=[0.,0.,-1.],horizontal_world=[1.,0.,0.],
                size_world=[.4,.4,.3],confidence=.9)
    prediction = project_cuboid(item,pose,INTR,SHAPE)
    item.update(_mask=prediction['mask'],bbox=prediction['bbox'],depth_camera_m=float(center[2]-pose[2,3]))
    return item


def depth_for(observations, pose=None):
    depth=np.full(SHAPE,4.,np.float32)
    for obs in observations:
        pred=project_cuboid(obs,np.eye(4) if pose is None else pose,INTR,SHAPE)
        np.minimum(depth,pred['depth'],out=depth)
    return depth


class PersistentMapTest(unittest.TestCase):
    def setUp(self): self.mapper=PersistentCartonMap();self.stamp=0.

    def frame(self, observations, depth=None, pose=None, sync=True):
        self.stamp+=.2
        return self.mapper.process(observations,depth_for(observations,pose) if depth is None else depth,
            np.eye(4) if pose is None else pose,INTR,self.stamp,sync)

    def confirmed(self, centers=((0.,0.,2.),)):
        for _ in range(3):self.frame([observation(c) for c in centers])
        self.assertTrue(all(o['state']=='CONFIRMED' for o in self.mapper.objects.values()))

    def test_static_viewpoint_keeps_id(self):
        self.confirmed()
        for x in np.linspace(0,.4,10):
            pose=np.eye(4);pose[0,3]=x
            obs=observation(pose=pose);self.frame([obs],pose=pose)
            self.assertEqual(obs['global_id'],0)
        self.assertEqual(self.mapper.next_id,1)

    def test_false_negative_is_positive_geometry_evidence(self):
        self.confirmed();old=copy.deepcopy(self.mapper.objects[0])
        self.frame([],depth_for([observation()]))
        item=self.mapper.objects[0]
        self.assertEqual(item['last_evidence'],'GEOMETRY_PRESENT')
        self.assertEqual(item['center_map'],old['center_map'])
        self.assertGreater(item['existence_probability'],old['existence_probability'])

    def test_false_positive_never_confirms(self):
        self.frame([observation()]);self.frame([observation()])
        for _ in range(10):self.frame([],np.full(SHAPE,4.,np.float32))
        self.assertEqual(self.mapper.objects[0]['state'],'VACATED')
        self.assertFalse(self.mapper.objects[0]['ever_confirmed'])

    def test_occlusion_does_not_delete(self):
        self.confirmed();probability=self.mapper.objects[0]['existence_probability']
        for _ in range(12):self.frame([],np.full(SHAPE,1.,np.float32))
        self.assertEqual(self.mapper.objects[0]['last_evidence'],'OCCLUDED')
        self.assertEqual(self.mapper.objects[0]['existence_probability'],probability)
        self.assertEqual(self.mapper.objects[0]['state'],'CONFIRMED')

    def test_removal_requires_repeated_clear_and_keeps_history(self):
        self.confirmed();self.frame([])
        self.assertEqual(self.mapper.objects[0]['state'],'CONFIRMED')
        self.frame([]);self.assertEqual(self.mapper.objects[0]['state'],'MISSING_CANDIDATE')
        for _ in range(10):self.frame([])
        self.assertEqual(self.mapper.objects[0]['state'],'VACATED')
        self.assertEqual(len(self.mapper.snapshot()['map']),0)
        self.assertEqual(len(self.mapper.snapshot()['map_history']),1)

    def test_relocation_does_not_force_id_reuse(self):
        self.confirmed()
        for _ in range(4):self.frame([observation((.6,0.,2.))])
        self.assertEqual(self.mapper.next_id,2)
        hypotheses=self.mapper.snapshot()['relocation_hypotheses']
        self.assertTrue(hypotheses)
        self.assertGreaterEqual(hypotheses[0]['supporting_frames'],2)
        self.assertEqual(hypotheses[0]['old_object_id'],0)
        self.assertEqual(hypotheses[0]['new_candidate_id'],1)

    def test_split_union_preserves_geometry(self):
        self.confirmed();original=copy.deepcopy(self.mapper.objects[0])
        left,right=observation(),observation()
        left['_mask'][:,160:]=False;right['_mask'][:,:160]=False
        left['center_map'][0]=-.1;right['center_map'][0]=.1
        self.frame([left,right],depth_for([observation()]))
        self.assertEqual(left['association_status'],'DETECTOR_SPLIT')
        self.assertEqual((left['global_id'],right['global_id']),(0,0))
        self.assertEqual(self.mapper.next_id,1)
        self.assertEqual(self.mapper.objects[0]['center_map'],original['center_map'])
        self.assertEqual(self.mapper.objects[0]['size_world'],original['size_world'])

    def test_merge_protects_both_ids_without_geometry_update(self):
        centers=((-0.22,0.,2.),(.22,0.,2.));self.confirmed(centers)
        before=copy.deepcopy(self.mapper.objects)
        merged=observation();merged['_mask']=observation(centers[0])['_mask']|observation(centers[1])['_mask']
        self.frame([merged],depth_for([observation(c) for c in centers]))
        self.assertEqual(merged['association_status'],'DETECTOR_MERGE')
        self.assertEqual(merged['global_ids'],[0,1])
        self.assertEqual(self.mapper.next_id,2)
        for gid in (0,1):
            self.assertEqual(self.mapper.objects[gid]['center_map'],before[gid]['center_map'])
            self.assertEqual(self.mapper.objects[gid]['exist_negative'],0)

    def test_pose_jump_and_sync_failure_freeze_map(self):
        self.confirmed();before=copy.deepcopy(self.mapper.objects)
        pose=np.eye(4);pose[0,3]=2
        self.assertFalse(self.frame([observation()],pose=pose)['commit'])
        self.assertEqual(self.mapper.objects,before)
        self.assertTrue(self.mapper.pose_anomaly_latched)

    def test_missing_pose_or_depth_is_unknown_not_vacated(self):
        self.confirmed();before=copy.deepcopy(self.mapper.objects)
        self.assertFalse(self.mapper.process([],np.full(SHAPE,np.nan),None,INTR,1.,True)['commit'])
        self.assertEqual(self.mapper.objects,before)
        for _ in range(20):self.frame([],np.full(SHAPE,np.nan,np.float32))
        self.assertEqual(self.mapper.objects[0]['state'],'CONFIRMED')
        self.assertEqual(self.mapper.objects[0]['existence_probability'],before[0]['existence_probability'])

    def test_stacked_new_box_cannot_share_id(self):
        self.confirmed()
        old,new=observation(),observation((0.,-.45,2.))
        self.frame([old,new])
        self.assertEqual(old['global_id'],0)
        self.assertEqual(new['global_id'],1)

    def test_rejected_fragment_cannot_create_or_move_object(self):
        obs=observation();obs['conflict_only']=True
        self.frame([obs]);self.assertEqual(self.mapper.next_id,0)
        self.confirmed()
        fragment=observation();fragment['conflict_only']=True
        self.frame([fragment]);self.assertEqual(self.mapper.next_id,1)
        self.assertEqual(fragment['association_status'],'STRICT_REJECTED')

    def test_bad_geometry_cannot_confirm(self):
        for _ in range(5):
            obs=observation();obs['geometry_quality_ok']=False;self.frame([obs])
        self.assertEqual(self.mapper.next_id,0)

    def test_invalid_center_is_not_inserted(self):
        obs=observation();obs['center_map'][0]=float('nan')
        self.frame([obs],np.full(SHAPE,2.,np.float32))
        self.assertEqual(self.mapper.next_id,0)
        self.assertEqual(obs['association_status'],'INVALID_OBSERVATION')

    def test_sparse_lidar_coverage_does_not_require_dense_rgb(self):
        mask=np.ones((160,160),bool);depth=np.full(mask.shape,np.nan,np.float32)
        depth[::7,::7]=2.;original=depth.copy()
        support=depth_support(mask,depth,8)
        self.assertLess(support['raw_pixel_ratio'],.03)
        self.assertGreater(support['spatial_coverage'],.9)
        self.assertGreater(support['valid_pixels'],200)
        np.testing.assert_array_equal(depth,original)

    def test_clustered_points_are_not_full_surface_support(self):
        mask=np.ones((160,160),bool);depth=np.full(mask.shape,np.nan,np.float32)
        depth[:30,:30]=2.
        support=depth_support(mask,depth,8)
        self.assertGreater(support['valid_pixels'],200)
        self.assertLess(support['spatial_coverage'],.1)

    def test_sparse_visibility_preserves_real_ray_evidence(self):
        self.confirmed()
        for value,evidence in [(2.,'GEOMETRY_PRESENT'),(1.,'OCCLUDED'),(4.,'CLEAR')]:
            depth=np.full(SHAPE,np.nan,np.float32);depth[::4,::4]=value
            self.frame([],depth)
            self.assertEqual(self.mapper.objects[0]['last_evidence'],evidence)

    def test_sparse_coverage_does_not_bypass_minimum_point_count(self):
        self.confirmed()
        depth=np.full(SHAPE,np.nan,np.float32);depth[::16,::16]=4.
        self.frame([],depth)
        self.assertEqual(self.mapper.objects[0]['last_evidence'],'UNKNOWN')
        self.assertEqual(self.mapper.objects[0]['exist_negative'],0)

    def test_long_observed_box_can_still_be_vacated(self):
        self.confirmed()
        self.mapper.objects[0]['exist_positive'] = 10000.
        for _ in range(20): self.frame([])
        self.assertEqual(self.mapper.objects[0]['state'], 'VACATED')
        self.assertEqual(len(self.mapper.snapshot()['map_history']), 1)

    def test_same_face_bad_depth_is_conflict_not_new_box_or_clear(self):
        self.confirmed()
        before = copy.deepcopy(self.mapper.objects[0])
        for _ in range(8):
            obs = observation((0., 0., 2.25))
            obs['_mask'] = observation()['_mask']
            self.frame([obs], np.full(SHAPE, 2.25, np.float32))
            self.assertEqual(obs['association_status'], 'IDENTITY_PENDING')
        self.assertEqual(self.mapper.next_id, 1)
        self.assertEqual(self.mapper.objects[0]['center_map'], before['center_map'])
        self.assertEqual(self.mapper.objects[0]['exist_negative'], 0)
        recovered = observation(); self.frame([recovered])
        self.assertEqual(recovered['global_id'], 0)

    def test_valid_measurement_is_not_clamped_to_initial_history(self):
        self.confirmed()
        for shift in np.linspace(.005, .15, 20):
            obs = observation((0., 0., 2. + shift))
            obs['_mask'] = observation()['_mask']
            self.frame([obs], np.full(SHAPE, 2. + shift, np.float32))
        self.assertEqual(self.mapper.next_id, 1)
        self.assertAlmostEqual(self.mapper.objects[0]['center_map'][2], 2.15)

    def test_current_quality_passed_normal_is_not_vetoed_by_history(self):
        self.confirmed()
        obs = observation(); obs['normal_world'] = [0., .5, -.8660254]
        self.frame([obs], depth_for([observation()]))
        self.assertEqual(obs['association_status'], 'MATCHED')
        np.testing.assert_allclose(self.mapper.objects[0]['normal_world'],obs['normal_world'],atol=1e-7)

    def test_identity_pending_preserves_current_measurement_quality(self):
        self.confirmed()
        obs=observation((0.,0.,2.25));obs['_mask']=observation()['_mask']
        obs.update(position_quality_ok=True,orientation_quality_ok=True,geometry_quality_ok=True)
        original=copy.deepcopy(obs)
        self.frame([obs],np.full(SHAPE,2.25,np.float32))
        self.assertEqual(obs['association_status'],'IDENTITY_PENDING')
        for key in ('center_map','normal_world','position_quality_ok','orientation_quality_ok','geometry_quality_ok'):
            self.assertEqual(obs[key],original[key])
        self.assertEqual(self.mapper.next_id,1)

    def test_overlapping_history_does_not_fake_detector_merge(self):
        self.confirmed()
        duplicate=copy.deepcopy(self.mapper.objects[0]);duplicate['global_id']=1
        duplicate['center_map'][2]+=.04
        self.mapper.objects[1]=duplicate;self.mapper.next_id=2
        obs=observation();self.frame([obs])
        self.assertEqual(obs['association_status'],'MATCHED')
        self.assertEqual(obs['global_id'],0)
        self.assertFalse(any(e['event']=='DETECTOR_MERGE' for e in self.mapper.events))

    def test_contained_face_with_low_iou_cannot_create_duplicate(self):
        self.confirmed()
        # A distorted historical silhouette is larger than the measured face.
        # The old .55 IoU-only guard missed this fully contained observation.
        item=self.mapper.objects[0];item['size_world']=[.6,.6,.3]
        obs=observation((0.,0.,1.75));obs['_mask']=observation()['_mask']
        self.frame([obs],np.full(SHAPE,1.75,np.float32))
        self.assertEqual(self.mapper.next_id,1)
        self.assertEqual(obs['association_status'],'IDENTITY_PENDING')

    def test_global_frame_labels_are_not_hardcoded(self):
        self.mapper.frame_id='warehouse_map'
        self.confirmed()
        self.assertEqual(self.mapper.active_map()['frame_id'],'warehouse_map')
        self.assertEqual(self.mapper.delta['frame_id'],'warehouse_map')

    def test_rejected_pose_does_not_clear_corresponding_old_box(self):
        self.confirmed()
        obs = observation(); obs['geometry_quality_ok'] = False
        self.frame([obs], np.full(SHAPE, 2.2, np.float32))
        self.assertEqual(self.mapper.objects[0]['last_evidence'], 'UNKNOWN')
        self.assertEqual(self.mapper.objects[0]['exist_negative'], 0)

    def test_pose_quality_failure_does_not_erase_observed_visibility(self):
        self.confirmed();before=copy.deepcopy(self.mapper.objects[0])
        obs=observation();obs.update(geometry_quality_ok=False,position_quality_ok=False,orientation_quality_ok=False)
        self.frame([obs],depth_for([observation()]))
        item=self.mapper.objects[0]
        self.assertEqual(item['visibility_state'],'VISIBLE')
        self.assertEqual(obs['association_status'],'MATCHED_VISIBILITY_ONLY')
        self.assertEqual(obs['global_id'],0)
        self.assertEqual(item['center_map'],before['center_map'])
        self.assertEqual(item['normal_world'],before['normal_world'])
        self.assertFalse(self.mapper.active_map()['boxes'][0]['geometry_usable'])
        self.assertFalse(obs['position_quality_ok'])

    def test_unique_depth_drift_reacquires_only_after_three_measurements(self):
        self.confirmed()
        for index in range(3):
            obs=observation((0.,0.,1.87));obs['_mask']=observation()['_mask']
            self.frame([obs],np.full(SHAPE,1.87,np.float32))
            self.assertEqual(self.mapper.next_id,1)
            self.assertEqual(obs['association_status'],'MATCHED_REACQUIRED' if index==2 else 'IDENTITY_PENDING')
        self.assertEqual(obs['global_id'],0)
        self.assertAlmostEqual(self.mapper.objects[0]['center_map'][2],1.87)

    def test_reacquisition_does_not_bridge_missing_observation_or_new_layer(self):
        self.confirmed()
        for _ in range(3):
            obs=observation((0.,0.,1.87));obs['_mask']=observation()['_mask']
            self.frame([obs],np.full(SHAPE,1.87,np.float32))
            self.frame([],np.full(SHAPE,np.nan,np.float32))
            self.assertEqual(self.mapper.objects[0]['center_map'],[0.,0.,2.])
        for _ in range(5):
            obs=observation((0.,0.,1.80));obs['_mask']=observation()['_mask']
            self.frame([obs],np.full(SHAPE,1.80,np.float32))
        self.assertEqual(self.mapper.objects[0]['center_map'],[0.,0.,2.])

    def test_box_return_to_vacated_location_restores_location_id(self):
        self.confirmed()
        for _ in range(15):self.frame([])
        self.assertEqual(self.mapper.objects[0]['state'],'VACATED')
        for index in range(3):
            obs=observation();self.frame([obs])
            self.assertEqual(self.mapper.next_id,1)
            if index<2:self.assertEqual(obs['association_status'],'RETURN_LOCATION_PENDING')
        self.assertEqual(obs['global_id'],0)
        self.assertEqual(self.mapper.objects[0]['state'],'CONFIRMED')
        self.assertEqual(self.mapper.active_map()['boxes'][0]['identity_basis'],'KNOWN_LOCATION_RETURN')
        self.assertFalse(self.mapper.active_map()['boxes'][0]['physical_identity_verified'])

    def test_return_does_not_resurrect_an_invalidated_duplicate(self):
        self.confirmed()
        self.mapper.objects[0].update(state='VACATED',invalidated_duplicate_of=5)
        obs=observation();self.frame([obs])
        self.assertEqual(obs['global_id'],1)
        self.assertEqual(self.mapper.objects[0]['state'],'VACATED')

    def test_return_in_front_of_displaced_record_is_not_blocked_forever(self):
        self.confirmed()
        for _ in range(15):self.frame([])
        self.frame([observation((0.,0.,2.3))])
        self.assertEqual(self.mapper.next_id,2)
        for _ in range(3):
            obs=observation();self.frame([obs])
        self.assertEqual(obs['global_id'],0)
        self.assertEqual(self.mapper.objects[0]['state'],'CONFIRMED')
        self.assertEqual(self.mapper.next_id,2)
        # Without a physical-motion confirmation, an occluded record is not
        # silently declared removed or the same physical carton.
        self.assertNotEqual(self.mapper.objects[1]['state'],'VACATED')

    def test_prior_extent_frame_cannot_break_return_reservation_and_create_new_id(self):
        self.confirmed()
        for _ in range(15):self.frame([])
        for index in range(3):
            obs=observation((0.,0.,1.87));obs['_mask']=observation()['_mask']
            obs['measurement_quality']={'size_prior_fallback':index==1}
            self.frame([obs],np.full(SHAPE,1.87,np.float32))
            self.assertEqual(self.mapper.next_id,1)
        self.assertEqual(obs['global_id'],0)

    def test_repeated_front_removal_and_return_preserves_independent_rear_id(self):
        self.confirmed()
        for cycle in range(3):
            for _ in range(18):
                rear=observation((0.,0.,2.3));self.frame([rear])
            self.assertEqual(self.mapper.objects[0]['state'],'VACATED')
            self.assertEqual(rear['global_id'],1)
            self.assertEqual(self.mapper.next_id,2)
            for _ in range(3):
                front=observation();self.frame([front])
            self.assertEqual(front['global_id'],0)
            self.assertEqual(self.mapper.objects[1]['visibility_state'],'OCCLUDED')
            self.assertEqual(self.mapper.objects[1]['state'],'CONFIRMED')
            self.assertEqual(self.mapper.next_id,2)

    def test_reveal_with_near_neighbours_in_apron_and_prior_extent(self):
        self.confirmed()
        for _ in range(18):
            rear=observation((0.,0.,2.3))
            rear['measurement_quality']={'size_prior_fallback':True}
            depth=depth_for([rear])
            apron=observation()['_mask'] & ~rear['_mask']
            yy,xx=np.indices(SHAPE)
            depth[apron & (xx<160)]=1.7
            self.frame([rear],depth)
        self.assertEqual(rear['global_id'],1)
        self.assertEqual(self.mapper.objects[0]['state'],'VACATED')
        self.assertEqual(self.mapper.next_id,2)

    def test_unrelated_near_depth_does_not_change_identity(self):
        self.confirmed()
        for _ in range(10):
            obs = observation(); depth = depth_for([obs]); depth[:, 270:] = .35
            self.frame([obs], depth)
            self.assertEqual(obs['global_id'], 0)
        self.assertEqual(self.mapper.next_id, 1)

    def test_fused_pose_axes_remain_orthonormal(self):
        self.confirmed()
        obs=observation();obs['normal_world']=[.08,.04,-.99599]
        obs['horizontal_world']=[.99,.1,.08353]
        self.frame([obs],depth_for([observation()]))
        item=self.mapper.objects[0]
        self.assertAlmostEqual(np.dot(item['normal_world'],item['horizontal_world']),0.,places=10)
        self.assertAlmostEqual(np.linalg.norm(item['horizontal_world']),1.,places=10)

    def test_rear_reveal_exits_conflict_without_reusing_front_id(self):
        self.confirmed()
        for _ in range(16):
            rear=observation((0.,0.,2.3));self.frame([rear])
        self.assertEqual(self.mapper.objects[0]['state'],'VACATED')
        self.assertEqual(rear['global_id'],1)
        self.assertEqual(self.mapper.objects[1]['state'],'CONFIRMED')
        self.assertEqual([o['global_id'] for o in self.mapper.active_map()['boxes']],[1])
        self.assertEqual(self.mapper.next_id,2)

    def test_one_reveal_frame_cannot_release_old_box(self):
        self.confirmed();rear=observation((0.,0.,2.3));self.frame([rear])
        self.assertEqual(self.mapper.next_id,1)
        self.assertEqual(self.mapper.objects[0]['exist_negative'],1.5)
        self.assertEqual(self.mapper.objects[0]['state'],'CONFIRMED')
        self.assertTrue(any(e['event']=='REPLACEMENT_PENDING' for e in self.mapper.events))
        old=observation();self.frame([old]);self.assertEqual(old['global_id'],0)

    def test_rear_reveal_needs_no_separate_background_or_other_boxes(self):
        self.confirmed()
        for _ in range(12):
            rear=observation((0.,0.,2.3));self.frame([rear],np.full(SHAPE,2.3,np.float32))
        self.assertEqual(self.mapper.next_id,2)
        self.assertEqual(rear['global_id'],1)
        self.assertEqual(self.mapper.objects[0]['state'],'VACATED')

    def test_foreground_apron_and_far_outliers_do_not_veto_valid_rear_reveal(self):
        self.confirmed()
        for _ in range(12):
            rear=observation((0.,0.,2.3))
            depth=depth_for([rear]);apron=observation()['_mask'] & ~rear['_mask']
            depth[apron]=1.7
            yy,xx=np.where(apron);depth[yy[:15],xx[:15]]=4.
            self.frame([rear],depth)
        self.assertEqual(self.mapper.next_id,2)
        self.assertEqual(rear['global_id'],1)
        self.assertEqual(self.mapper.objects[0]['state'],'VACATED')

    def test_each_of_four_front_boxes_can_reveal_rear_without_third_background(self):
        centers=[(-.55,-.45,2.),(.55,-.45,2.),(-.55,.45,2.),(.55,.45,2.)]
        for removed in range(4):
            with self.subTest(removed=removed):
                self.mapper=PersistentCartonMap();self.stamp=0.;self.confirmed(centers)
                rear_center=(*centers[removed][:2],2.3)
                for cycle in range(2):
                    for _ in range(18):
                        observations=[observation(rear_center if i==removed else center) for i,center in enumerate(centers)]
                        depth=depth_for(observations)
                        # A rear wall is coplanar with the exposed rear face:
                        # no still-farther apron can ever be observed.
                        depth[depth>2.3]=2.3
                        self.frame(observations,depth)
                    self.assertEqual(observations[removed]['global_id'],4)
                    self.assertEqual(self.mapper.objects[removed]['state'],'VACATED')
                    self.assertEqual(self.mapper.next_id,5)
                    for _ in range(3):
                        observations=[observation(center) for center in centers]
                        self.frame(observations)
                    self.assertEqual(observations[removed]['global_id'],removed)
                    self.assertEqual(self.mapper.objects[4]['visibility_state'],'OCCLUDED')
                    self.assertEqual(self.mapper.next_id,5)

    def test_multiple_local_reveals_need_no_unchanged_other_box(self):
        centers=[(-.55,-.45,2.),(.55,-.45,2.),(-.55,.45,2.),(.55,.45,2.)]
        self.confirmed(centers)
        for _ in range(18):
            observations=[observation((*center[:2],2.3)) for center in centers]
            self.frame(observations,np.full(SHAPE,2.3,np.float32))
        self.assertEqual(self.mapper.next_id,8)
        self.assertTrue(all(self.mapper.objects[i]['state']=='VACATED' for i in range(4)))
        self.assertTrue(all(self.mapper.objects[i]['state']=='CONFIRMED' for i in range(4,8)))

    def test_unrelated_box_orientation_quality_does_not_veto_layer_change(self):
        centers=[(-.55,-.45,2.),(.55,-.45,2.),(0.,.45,2.)]
        self.confirmed(centers)
        for _ in range(12):
            observations=[observation((*centers[0][:2],2.3)),observation(centers[1]),observation(centers[2])]
            observations[2]['orientation_quality_ok']=False
            depth=depth_for(observations);depth[depth>2.3]=2.3
            self.frame(observations,depth)
        self.assertEqual(self.mapper.next_id,4)
        self.assertEqual(observations[0]['global_id'],3)
        self.assertEqual(self.mapper.objects[0]['state'],'VACATED')

    def test_new_plane_ray_mismatch_remains_object_local_conflict(self):
        centers=[(-.55,-.45,2.),(.55,-.45,2.),(0.,.45,2.)]
        self.confirmed(centers)
        for _ in range(12):
            observations=[observation((*centers[0][:2],2.3)),observation(centers[1]),observation(centers[2])]
            depth=depth_for(observations);depth[depth>2.3]=2.3
            depth[observations[0]['_mask']]=2.6
            self.frame(observations,depth)
        self.assertEqual(self.mapper.next_id,3)
        self.assertEqual(self.mapper.objects[0]['state'],'CONFIRMED')

    def test_depth_outside_target_surfaces_cannot_change_reveal_decision(self):
        self.confirmed();baseline=copy.deepcopy(self.mapper)
        rng=np.random.default_rng(42)
        for _ in range(18):
            rear=observation((0.,0.,2.3));plain=depth_for([rear])
            target=observation()['_mask']|rear['_mask']
            noisy=plain.copy();noisy[~target]=rng.choice([np.nan,.2,2.3,8.],size=(~target).sum())
            observed=copy.deepcopy(rear);self.frame([observed],noisy)
            expected=copy.deepcopy(rear)
            baseline.process([expected],plain,np.eye(4),INTR,self.stamp)
            self.assertEqual(observed['global_id'],expected['global_id'])
            self.assertEqual(self.mapper.objects,baseline.objects)

    def test_new_detection_quality_cannot_freeze_valid_old_surface_clearance(self):
        self.confirmed()
        for _ in range(18):
            rear=observation((0.,0.,2.3));rear.update(geometry_quality_ok=False,orientation_quality_ok=False)
            self.frame([rear])
        self.assertEqual(self.mapper.objects[0]['state'],'VACATED')
        self.assertEqual(self.mapper.next_id,1)
        for _ in range(3):
            rear=observation((0.,0.,2.3));self.frame([rear])
        self.assertEqual(rear['global_id'],1)
        self.assertEqual(self.mapper.objects[1]['state'],'CONFIRMED')

    def test_obsolete_background_threshold_is_not_loaded(self):
        mapper=PersistentCartonMap({'replacement_background_margin_m':100.})
        self.assertNotIn('replacement_background_margin_m',mapper.config)

    def test_out_of_fov_keeps_confirmed_policy_object(self):
        self.confirmed()
        for x in np.linspace(.1,4.,40):
            pose=np.eye(4);pose[0,3]=x
            self.frame([],depth_for([observation(pose=pose)],pose),pose)
        for _ in range(30):self.frame([],np.full(SHAPE,4.,np.float32),pose)
        obj=self.mapper.objects[0]
        self.assertEqual(obj['visibility_state'],'OUT_OF_FOV')
        self.assertEqual(obj['exist_negative'],0)
        active=self.mapper.active_map()['boxes']
        self.assertEqual(len(active),1);self.assertFalse(active[0]['currently_visible'])

    def test_unmeasured_back_face_cannot_clear(self):
        self.confirmed()
        pose=np.eye(4);pose[:3,:3]=np.diag([-1.,1.,-1.]);pose[2,3]=4.
        # A valid new viewing direction, independent of the jump guard.
        self.mapper.last_pose=pose.copy()
        for _ in range(12):self.frame([],np.full(SHAPE,4.,np.float32),pose)
        self.assertEqual(self.mapper.objects[0]['exist_negative'],0)
        self.assertEqual(self.mapper.objects[0]['last_evidence'],'UNKNOWN')

    def test_prior_extent_record_can_retire_without_promoting_extent_to_measured(self):
        for _ in range(3):
            obs=observation();obs['measurement_quality']={'size_prior_fallback':True};self.frame([obs])
        for _ in range(12):self.frame([])
        self.assertEqual(self.mapper.objects[0]['state'],'VACATED')
        self.assertFalse(self.mapper.objects[0]['measured_surfaces'][0]['trusted'])
        self.assertEqual(self.mapper.objects[0]['last_evidence_detail']['geometry_basis'],
                         'POSITION_CONFIRMED_ESTIMATED_FACE')

    def test_position_only_record_retirement_is_separate_from_grasp_pose(self):
        for _ in range(3):
            obs=observation();obs.update(orientation_quality_ok=False,position_quality_ok=True)
            obs['measurement_quality']={'position_usable':True,'orientation_usable':False,'size_prior_fallback':True}
            self.frame([obs])
        item=self.mapper.objects[0];before=copy.deepcopy(item)
        # The most recent visibility-only frame may mark CURRENT position
        # unavailable; that does not erase the earlier accepted location.
        item['position_quality_ok']=False
        for _ in range(12):self.frame([])
        self.assertEqual(self.mapper.objects[0]['state'],'VACATED')
        self.assertFalse(self.mapper.objects[0]['orientation_trusted'])
        self.assertEqual(self.mapper.objects[0]['center_map'],before['center_map'])
        self.assertEqual(self.mapper.objects[0]['measured_surfaces'],before['measured_surfaces'])

    def test_estimated_face_cannot_retire_an_occluded_or_unobserved_record(self):
        for _ in range(3):
            obs=observation();obs['orientation_quality_ok']=False;self.frame([obs])
        for depth in (np.full(SHAPE,1.,np.float32),np.full(SHAPE,np.nan,np.float32)):
            for _ in range(12):self.frame([],depth)
        self.assertEqual(self.mapper.objects[0]['state'],'CONFIRMED')
        self.assertEqual(self.mapper.objects[0]['exist_negative'],0)

    def test_estimated_tentative_record_also_has_a_retirement_path(self):
        obs=observation();obs['orientation_quality_ok']=False;self.frame([obs])
        self.assertEqual(self.mapper.objects[0]['state'],'TENTATIVE')
        for _ in range(12):self.frame([])
        self.assertEqual(self.mapper.objects[0]['state'],'VACATED')
        self.assertFalse(self.mapper.objects[0]['ever_confirmed'])

    def test_policy_keeps_missing_candidate_until_confirmed_removal(self):
        self.frame([observation()]);self.assertFalse(self.mapper.active_map()['boxes'])
        self.frame([observation()]);self.frame([observation()])
        self.assertEqual(self.mapper.delta['added'],[0])
        self.frame([]);self.frame([])
        active=self.mapper.active_map()['boxes']
        self.assertEqual(active[0]['lifecycle'],'MISSING_CANDIDATE')
        self.assertTrue(active[0]['needs_verification'])
        self.assertFalse(self.mapper.delta['removed'])

    def test_transaction_exception_cannot_partially_move_or_allocate(self):
        self.confirmed();before=copy.deepcopy(self.mapper.snapshot());revision=self.mapper.revision
        observed=observation((.01,0.,2.))
        with patch.object(PersistentCartonMap,'lifecycle',side_effect=RuntimeError('injected commit failure')):
            with self.assertRaises(RuntimeError):self.frame([observed,observation((.6,0.,2.))])
        self.assertEqual(self.mapper.snapshot()['map_history'],before['map_history'])
        self.assertEqual(self.mapper.revision,revision);self.assertEqual(self.mapper.next_id,1)
        self.assertNotIn('global_id',observed)

    def epoch_samples(self, groups):
        samples=[]
        for group in groups:
            self.stamp+=.2
            samples.append(dict(observations=group, depth=depth_for(group), pose=np.eye(4),
                                intr=INTR,stamp=self.stamp,sync_ok=True))
        return samples

    def test_epoch_confirms_with_one_transaction_and_two_of_three_support(self):
        samples=self.epoch_samples([[],[observation()],[observation()]])
        result=self.mapper.process_epoch(samples)
        self.assertTrue(result['commit']);self.assertEqual(self.mapper.revision,1)
        self.assertEqual(self.mapper.objects[0]['observations'],1)
        self.assertEqual(self.mapper.objects[0]['state'],'CONFIRMED')
        self.assertEqual(self.mapper.delta['added'],[0])

    def test_epoch_one_frame_false_positive_cannot_create(self):
        result=self.mapper.process_epoch(self.epoch_samples([[],[],[observation()]]))
        self.assertTrue(result['commit']);self.assertEqual(self.mapper.next_id,0)

    def test_epoch_rejects_contradictory_clearance_without_partial_updates(self):
        self.confirmed();before=copy.deepcopy(self.mapper.objects);revision=self.mapper.revision
        result=self.mapper.process_epoch(self.epoch_samples([[observation()],[],[observation()]]))
        self.assertEqual(result['reason'],'EPOCH_CONTRADICTORY_EVIDENCE')
        self.assertEqual(self.mapper.objects,before);self.assertEqual(self.mapper.revision,revision)

    def test_epoch_clearance_is_one_evidence_update(self):
        self.confirmed()
        result=self.mapper.process_epoch(self.epoch_samples([[],[],[]]))
        self.assertTrue(result['commit'])
        self.assertEqual(self.mapper.objects[0]['exist_negative'],1.5)
        self.assertEqual(self.mapper.objects[0]['consecutive_clear_frames'],1)

    def test_epoch_moving_camera_or_repeated_stamps_cannot_commit(self):
        samples=self.epoch_samples([[observation()] for _ in range(3)])
        samples[1]['pose'][0,3]=.02
        self.assertEqual(self.mapper.process_epoch(samples)['reason'],'EPOCH_CAMERA_MOVED')
        samples[1]['pose']=np.eye(4);samples[1]['stamp']=samples[0]['stamp']
        self.assertEqual(self.mapper.process_epoch(samples)['reason'],'EPOCH_TIMING')
        self.assertEqual(self.mapper.revision,0)

    def test_unknown_breaks_clear_run_without_existence_decay(self):
        self.confirmed();self.frame([]);negative=self.mapper.objects[0]['exist_negative']
        self.frame([],np.full(SHAPE,np.nan,np.float32))
        self.assertEqual(self.mapper.objects[0]['consecutive_clear_frames'],0)
        self.assertEqual(self.mapper.objects[0]['exist_negative'],negative)

    def test_session_qualified_identity_does_not_collide_after_restart(self):
        self.confirmed();first=self.mapper.active_map()['boxes'][0]['object_uid']
        self.mapper=PersistentCartonMap();self.confirmed()
        self.assertNotEqual(self.mapper.active_map()['boxes'][0]['object_uid'],first)

    def test_inferred_source_cannot_impersonate_a_measured_face(self):
        self.confirmed()
        self.mapper.objects[0]['measured_surfaces'][0]['source']='INFERRED_BACK_FACE'
        for _ in range(10):self.frame([])
        self.assertEqual(self.mapper.objects[0]['last_evidence_detail']['geometry_basis'],
                         'POSITION_CONFIRMED_ESTIMATED_FACE')

    def test_predicted_occluder_cannot_permanently_veto_real_clear_rays(self):
        # Seed two known historical surfaces; both are now physically gone.
        self.mapper.stamp=0.;self.mapper.frame=0
        for z in (2.,2.3):
            self.mapper.create(observation((0.,0.,z)))
        for obj in self.mapper.objects.values():obj.update(state='CONFIRMED',ever_confirmed=True)
        self.frame([])
        self.assertLess(self.mapper.objects[1]['visibility_prediction']['predicted_visible_fraction'],.1)
        self.assertEqual(self.mapper.objects[1]['last_evidence'],'CLEAR')
        for _ in range(12):self.frame([])
        self.assertEqual(self.mapper.active_map()['boxes'],[])

    def test_epoch_rear_reveal_can_complete_without_reusing_old_id(self):
        self.confirmed()
        for _ in range(14):
            result=self.mapper.process_epoch(self.epoch_samples([[observation((0.,0.,2.3))] for _ in range(3)]))
            self.assertTrue(result['commit'])
        self.assertEqual(self.mapper.objects[0]['state'],'VACATED')
        self.assertEqual(self.mapper.objects[1]['state'],'CONFIRMED')
        self.assertEqual(self.mapper.next_id,2)

    def test_epoch_duplicate_cloud_is_not_independent_support(self):
        samples=self.epoch_samples([[observation()] for _ in range(3)])
        for sample in samples:sample['cloud_stamp']=.2
        self.assertEqual(self.mapper.process_epoch(samples)['reason'],'EPOCH_TIMING')
        self.assertEqual(self.mapper.revision,0)

    def test_individuals_plus_composite_cannot_steal_ids_and_create_duplicates(self):
        centers=((-0.22,0.,2.),(.22,0.,2.));self.confirmed(centers)
        for _ in range(5):
            left,right=[observation(c) for c in centers]
            composite=observation();composite['_mask']=left['_mask']|right['_mask']
            composite['conflict_only']=True
            self.frame([composite,right,left],depth_for([left,right]))
            self.assertEqual((left['global_id'],right['global_id']),(0,1))
            self.assertEqual(composite['association_status'],'REDUNDANT_MERGE_OBSERVATION')
            self.assertEqual(self.mapper.next_id,2)

    def test_merge_plus_one_individual_cannot_allocate_duplicate(self):
        centers=((-0.22,0.,2.),(.22,0.,2.));self.confirmed(centers)
        left,right=[observation(c) for c in centers]
        composite=observation();composite['_mask']=left['_mask']|right['_mask']
        self.frame([left,composite],depth_for([left,right]))
        self.assertEqual(self.mapper.next_id,2)
        self.assertEqual(left['association_status'],'REDUNDANT_OBJECT_OBSERVATION')

    def test_position_only_can_keep_identity_without_changing_orientation(self):
        self.confirmed();before=copy.deepcopy(self.mapper.objects[0])
        for _ in range(5):
            obs=observation((.01,0.,2.));obs['orientation_quality_ok']=False
            obs['position_quality_ok']=True;obs['normal_world']=[0.,.5,-.8660254]
            self.frame([obs],depth_for([observation()]))
            self.assertEqual(obs['global_id'],0)
            self.assertEqual(obs['association_status'],'MATCHED_POSITION_ONLY')
        item=self.mapper.objects[0]
        self.assertEqual(item['normal_world'],before['normal_world'])
        self.assertEqual(item['measured_surfaces'],before['measured_surfaces'])
        self.assertFalse(self.mapper.active_map()['boxes'][0]['geometry_usable'])

    def test_position_only_new_box_is_explicitly_untrusted_pose_until_measured(self):
        for _ in range(3):
            obs=observation();obs['orientation_quality_ok']=False
            self.frame([obs])
        item=self.mapper.objects[0]
        self.assertEqual(item['state'],'CONFIRMED')
        self.assertFalse(item['orientation_trusted'])
        self.assertFalse(item['measured_surfaces'][0]['trusted'])
        self.assertEqual(len(self.mapper.active_map()['boxes']),1)
        reliable=observation();self.frame([reliable])
        self.assertTrue(self.mapper.objects[0]['orientation_trusted'])
        self.assertTrue(self.mapper.objects[0]['measured_surfaces'][0]['trusted'])

    def test_position_only_does_not_disable_large_depth_conflict_guard(self):
        self.confirmed()
        for _ in range(8):
            obs=observation((0.,0.,2.25));obs['_mask']=observation()['_mask']
            obs['orientation_quality_ok']=False
            self.frame([obs],np.full(SHAPE,2.25,np.float32))
        self.assertEqual(self.mapper.next_id,1)
        self.assertEqual(self.mapper.objects[0]['exist_negative'],0)

    def test_same_frame_duplicate_new_detection_gets_only_one_candidate(self):
        a,b=observation(),observation((.005,0.,2.))
        self.frame([a,b],depth_for([a]))
        self.assertEqual(self.mapper.next_id,1)
        self.assertEqual(b['association_status'],'REDUNDANT_OBJECT_OBSERVATION')


if __name__=='__main__': unittest.main()
