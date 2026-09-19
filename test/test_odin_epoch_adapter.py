"""ROS adapter control/serialization tests with no Node, DDS or camera started."""
import copy
from collections import deque
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import numpy as np
from test_persistent_carton_map import observation, depth_for, INTR, SHAPE
from camera_unload_perception.algorithms.persistent_carton_map import PersistentCartonMap

module = None
if importlib.util.find_spec('rclpy'):
    source=Path(__file__).resolve().parents[1]/'tools/diagnostics/odin_persistent_box_test.py'
    spec=importlib.util.spec_from_file_location('odin_epoch_adapter_under_test',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


@unittest.skipIf(module is None, 'Source ROS Humble environment for adapter tests')
class EpochAdapterTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.node=object.__new__(module.OdinPersistentBoxTest)
        n=self.node
        n.mapper=PersistentCartonMap();n.session_id=n.mapper.session_id
        n.args=SimpleNamespace(mode='epoch',conf=.7,imgsz=640,localization_source='odin')
        n.world_frame='map';n.base_frame='base_link';n.base_from_camera=None;n.base_pose_error='no base TF'
        n.out=Path(self.temp.name);n.enabled=True;n.frame=0;n.sample_sequence=0
        n.epoch_samples=[];n.epoch_started_at=None;n.epoch_progress={}
        n.world_from_camera=np.eye(4);n.camera_world=[0.,0.,0.]
        n.map_tf_available=True;n.pose_error='';n.intr=INTR
        n.image=np.zeros((*SHAPE,3),np.uint8)
        n._read_cloud=lambda:np.empty((0,3),np.float32)
        n._resolve_world_pose=lambda:None
        n._nearest_odom=lambda stamp:(np.eye(4),stamp)
        n._save_geometry_evidence=Mock();n._atomic_image=Mock();n._publish_live_cloud=Mock()
        n.publish_markers=Mock();n.event_pub=Mock();n.map_pub=Mock();n.active_pub=Mock();n.delta_pub=Mock();n.measurement_pub=Mock()
        self.outputs={}
        n._atomic_json=lambda name,payload:self.outputs.update({name:copy.deepcopy(payload)})

    def tearDown(self):self.temp.cleanup()

    def sample(self, stamp):
        n=self.node;n.image_stamp=stamp;n.cloud_stamp=stamp
        obs=observation();obs['local_detection_id']=0
        obs.update(center_camera_optical=obs['center_map'],normal_camera_optical=obs['normal_world'],
            horizontal_camera_optical=obs['horizontal_world'],position_quality_ok=True,orientation_quality_ok=True)
        n._depth_image=lambda points:(depth_for([obs]),points)
        n._strict_observations=lambda *args:([obs],[],[],1)
        n.process()

    def test_batch_publishes_delta_only_once_after_complete_consensus(self):
        for stamp in (.2,.4):
            self.sample(stamp)
            self.assertEqual(self.node.mapper.revision,0)
            self.assertEqual(self.outputs['latest.json']['commit']['reason'],'EPOCH_COLLECTING')
            self.node.delta_pub.publish.assert_not_called()
        self.sample(.6)
        n=self.node
        self.assertEqual(n.mapper.revision,1);self.assertEqual(n.frame,1)
        self.assertFalse(n.epoch_samples)
        n.delta_pub.publish.assert_called_once()
        packet=json.loads(n.active_pub.publish.call_args.args[0].data)
        self.assertEqual(packet['session_id'],n.session_id)
        self.assertEqual(packet['boxes'][0]['global_id'],0)
        self.assertEqual(self.outputs['map_delta.json']['added'],[0])
        self.assertEqual(self.outputs['latest.json']['epoch']['state'],'COMMITTED')

    def test_empty_new_session_replaces_stale_delta_download_without_publishing_delta(self):
        self.outputs['map_delta.json']={'session_id':'old-session','removed':[99]}
        self.node._publish_policy(self.node.mapper.snapshot(),commit=False)
        self.assertEqual(self.outputs['map_delta.json']['session_id'],self.node.session_id)
        self.assertEqual(self.outputs['map_delta.json']['removed'],[])
        self.node.delta_pub.publish.assert_not_called()

    def test_manual_process_still_commits_one_frame_immediately(self):
        self.node.args.mode='manual';self.sample(.2)
        self.assertEqual(self.node.mapper.revision,1)
        self.assertEqual(self.node.mapper.objects[0]['state'],'TENTATIVE')
        self.assertEqual(self.node.mapper.epoch_id,0)

    def test_pause_cancels_partial_batch_and_queued_requests(self):
        self.sample(.2);n=self.node
        n.control_revision=0;n.request_id=0;n.reset_id=0;n.pending_requests=1
        control=dict(revision=1,mode='epoch',enabled=False,request_id=0,reset_id=0,config=n.mapper.config)
        (n.out/'control.json').write_text(json.dumps(control))
        n._read_control()
        self.assertFalse(n.epoch_samples);self.assertEqual(n.pending_requests,0)
        self.assertEqual(n.mapper.revision,0)
        self.assertEqual(n.epoch_progress['state'],'CANCELLED')

    def test_robot_tf_mode_does_not_require_odin_odometry(self):
        self.node.args.mode='manual';self.node.args.localization_source='tf'
        self.node._nearest_odom=lambda stamp:(None,None)
        self.sample(.2)
        self.assertTrue(self.outputs['latest.json']['commit']['commit'])

    def test_odin_exact_tf_is_not_invalidated_by_late_unused_odometry(self):
        self.node.args.mode='manual';self.node.args.localization_source='odin'
        self.node._nearest_odom=lambda stamp:(None,stamp-.10)
        self.sample(.2)
        sync=self.outputs['latest.json']['time_sync']
        self.assertTrue(self.outputs['latest.json']['commit']['commit'])
        self.assertAlmostEqual(sync['rgb_odom_dt_ms'],100.)
        self.assertEqual(sync['rgb_pose_dt_ms'],0.)
        self.assertEqual(sync['pose_source'],'RGB_TIMESTAMP_TF')

    def test_tick_runs_local_detection_without_odom_or_global_tf(self):
        n=self.node;n.args.mode='realtime';n.args.localization_source='tf'
        n._read_control=Mock();n.pending_requests=0;n.last_processed_stamp=None;n.last_processed_cloud_stamp=None
        n.image_buffer=deque([(.2,n.image)]);n.cloud_buffer=deque([(.2,None,'lidar')]);n.odom_buffer=deque()
        n.tf=Mock();n.tf.can_transform.return_value=False;n.process=Mock()
        n.tick()
        n.process.assert_called_once()
        self.assertEqual(n.tf.can_transform.call_args.args[:2],('map','lidar'))

    def test_measurements_survive_missing_world_and_base_tf(self):
        n=self.node;n.args.mode='manual';n.world_from_camera=None;n.map_tf_available=False
        self.sample(.2)
        packet=self.outputs['current_measurements.json'];box=packet['boxes'][0]
        self.assertTrue(box['position_quality_ok']);self.assertTrue(box['orientation_quality_ok'])
        self.assertIsNone(box['world']);self.assertIsNone(box['base'])
        self.assertEqual(box['camera_optical']['position'],[0.,0.,2.])
        self.assertFalse(self.outputs['latest.json']['commit']['commit'])

    def test_measurement_transforms_world_and_base_independently(self):
        n=self.node;n.args.mode='manual';n.world_frame='warehouse_map';n.base_frame='base'
        n.world_from_camera=np.eye(4);n.world_from_camera[:3,3]=[4.,5.,6.]
        n.base_from_camera=np.array([[0.,-1.,0.,1.],[1.,0.,0.,2.],[0.,0.,1.,3.],[0.,0.,0.,1.]])
        self.sample(.2)
        packet=self.outputs['current_measurements.json'];box=packet['boxes'][0]
        self.assertEqual(packet['world_frame'],'warehouse_map');self.assertEqual(packet['base_frame'],'base')
        np.testing.assert_allclose(box['world']['position'],[4.,5.,8.])
        np.testing.assert_allclose(box['base']['position'],[1.,2.,5.])
        np.testing.assert_allclose(box['base']['horizontal'],[0.,1.,0.])


if __name__=='__main__':unittest.main()
