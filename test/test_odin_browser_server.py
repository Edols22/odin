import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SOURCE=Path(__file__).resolve().parents[1]/'tools/diagnostics/odin_browser_server.py'
spec=importlib.util.spec_from_file_location('odin_browser_server_test',SOURCE)
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class BrowserControlTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.original_root=module.ROOT
        module.ROOT=Path(self.temp.name)
        self.app=module.Application(SimpleNamespace(map=module.ROOT/'map'))

    def tearDown(self):
        module.ROOT=self.original_root
        self.temp.cleanup()

    def test_config_transaction_rejects_partial_changes(self):
        before=dict(self.app.control)
        with self.assertRaises(ValueError):
            self.app.command({'action':'configure','mode':'realtime','confidence':.2})
        self.assertEqual(self.app.control,before)

    def test_beta_prior_is_not_a_fusion_ratio(self):
        self.app.command({'action':'configure','config':{'beta_alpha':2.}})
        self.assertEqual(self.app.control['config']['beta_alpha'],2.)

    def test_strict_confirmation_and_ransac_bounds(self):
        for config in ({'min_observations':1},{'max_points_per_box':100},{'center_alpha':2},
                       {'max_points_per_box':2000},{'mask_erode_px':100},{'max_center_distance_m':float('nan')}):
            with self.subTest(config=config),self.assertRaises(ValueError):
                self.app.command({'action':'configure','config':config})

    def test_reset_requires_confirmation(self):
        with self.assertRaises(ValueError):self.app.command({'action':'reset'})
        self.app.command({'action':'reset','confirm':True})
        self.assertEqual(self.app.control['reset_id'],1)

    def test_sparse_sampling_cell_bounds(self):
        for value in (0,17,8.5):
            with self.subTest(value=value),self.assertRaises(ValueError):
                self.app.command({'action':'configure','config':{'depth_support_cell_px':value}})
        self.app.command({'action':'configure','config':{'depth_support_cell_px':8}})
        self.assertEqual(self.app.control['config']['depth_support_cell_px'],8)

    def test_unknown_action_rejected(self):
        with self.assertRaises(ValueError):self.app.command({'action':'shell'})

    def test_robot_tf_start_needs_no_test_map_or_driver_launch(self):
        self.app.args.localization_source='tf'
        self.app.args.world_frame='warehouse_map';self.app.args.base_frame='base'
        with patch.object(module,'processes',return_value=[]),patch.object(module.subprocess,'Popen') as popen:
            self.app.start()
        popen.assert_called_once()
        command=popen.call_args.args[0]
        self.assertIn('warehouse_map',command);self.assertIn('base',command)
        self.assertIn('--localization-source',command);self.assertIn('tf',command)

    def test_epoch_mode_and_limits(self):
        self.app.command({'action':'configure','mode':'epoch','config':{'epoch_frames':5,'epoch_min_support':3}})
        self.assertEqual(self.app.control['mode'],'epoch')
        for config in ({'epoch_frames':2},{'epoch_frames':9},{'epoch_min_support':6},
                       {'epoch_timeout_s':61},{'replacement_support_frames':1},
                       {'replacement_min_layer_m':.10}):
            with self.subTest(config=config),self.assertRaises(ValueError):
                self.app.command({'action':'configure','config':config})


if __name__=='__main__':unittest.main()
