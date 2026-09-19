"""Audited record repair must not roll back unrelated live identities."""
import copy
import importlib.util
from pathlib import Path
import unittest

source=Path(__file__).resolve().parents[1]/'tools/diagnostics/reload_odin_mapper_checkpoint.py'
spec=importlib.util.spec_from_file_location('odin_identity_repair',source)
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class IdentityRepairTest(unittest.TestCase):
    def test_restore_front_rear_keeps_new_middle_record_and_retirement(self):
        def item(gid,state,x,**extra):
            return dict(global_id=gid,state=state,center_map=[x,0,0],events=[],**extra)
        latest=dict(session_id='same',sensor_stamp=20,frame=100,map_history=[
            item(0,'VACATED',1.6),item(1,'CONFIRMED',1.9,known_locations=[1]),
            item(7,'VACATED',1.9,relocated_to=1),item(8,'VACATED',1.9,relocated_to=1),
            item(9,'CONFIRMED',1.9)])
        checkpoint=dict(session_id='same',map_history=[item(1,'VACATED',1.6),item(7,'CONFIRMED',1.9)])
        before=copy.deepcopy(latest)
        module.restore_records(latest,checkpoint,[1,7],[(8,7)],'front/rear correction')
        objects={o['global_id']:o for o in latest['map_history']}
        self.assertEqual(objects[0],before['map_history'][0])
        self.assertEqual(objects[9],before['map_history'][4])
        self.assertEqual(objects[1]['state'],'VACATED')
        self.assertEqual(objects[1]['center_map'][0],1.6)
        self.assertEqual(objects[7]['state'],'CONFIRMED')
        self.assertNotIn('known_locations',objects[1])
        self.assertNotIn('relocated_to',objects[7])
        self.assertNotIn('relocated_to',objects[8])
        self.assertEqual(objects[8]['invalidated_duplicate_of'],7)
        self.assertEqual(checkpoint['map_history'][0]['events'],[])

    def test_other_or_unspecified_session_cannot_restore(self):
        for session in ('other',None):
            with self.subTest(session=session),self.assertRaises(ValueError):
                module.restore_records(dict(session_id='live'),dict(session_id=session),[],[],'reason')
