"""Behavioural regression tests for the independent expected-cloud project."""
import copy
from dataclasses import replace
import numpy as np
import pytest
from camera_unload_perception.active_perception.schema import CapturePacket, MeasuredSurface, BoxObservation, BoxRecord, unit
from camera_unload_perception.active_perception.ray_compare import compare_raw_returns, intersect_obb
from camera_unload_perception.active_perception.map_manager import ActivePerceptionMap
from camera_unload_perception.active_perception.scheduler import ActiveObservationScheduler


def packet(stamp=1.,z=2.,semantics='FIRST_OPAQUE_RETURN'):
    x,y=np.meshgrid(np.linspace(-.24,.24,32),np.linspace(-.24,.24,32))
    points=np.column_stack((x.ravel(),y.ravel(),np.full(x.size,2.)))*z/2
    return CapturePacket(str(stamp),stamp,stamp,points,np.arange(len(points)),np.eye(4),np.eye(4),
        dict(fx=400,fy=400,ppx=320,ppy=240),image=np.zeros((480,640,3),np.uint8),
        map_epoch='test_map',calibration_revision='calib1',stable_capture=True,pose_quality=True,return_semantics=semantics)


def obs(p,z=2.,x=0.,full=True):
    surface=MeasuredSurface([x,0,z],[0,0,-1],[[x-.25,-.25,z],[x+.25,-.25,z],[x+.25,.25,z],[x-.25,.25,z]],
        full,True,'SYNTHETIC',p.capture_id,p.rgb_stamp)
    return BoxObservation(0,p.capture_id,surface,[1,0,0],[.5,.3,.5],position_ok=True,orientation_ok=True)


def record(o,gid=0):return BoxRecord(gid,o.face,o.geometry(),o.dimensions,list(o.dimension_status))


def seed(manager,z=2.,start=1.):
    aa=[]
    for t in (start,start+1,start+2):
        p=packet(t,z);aa.append(manager.analyze(p,[obs(p,z)]))
    assert manager.commit(aa)['committed']


@pytest.mark.parametrize('z,kind',[(2,'PRESENT'),(1,'OCCLUDED'),(3,'CLEAR')])
def test_native_surface_evidence(z,kind):
    r=record(obs(packet()))
    assert compare_raw_returns(packet(z=z),[r]).evidence[0]['kind']==kind


def test_camera_origin_does_not_change_native_ranges():
    p=packet(z=3);p.t_world_camera=np.eye(4);p.t_world_camera[:3,3]=[.8,.4,.3]
    assert compare_raw_returns(p,[record(obs(packet()))]).evidence[0]['kind']=='CLEAR'


def test_same_return_clears_front_and_explains_rear_independent_of_order():
    front=record(obs(packet()),0);rear=record(obs(packet(),z=3),1)
    for records in ([front,rear],[rear,front]):
        result=compare_raw_returns(packet(z=3),records)
        assert result.evidence[0]['kind']=='CLEAR'
        assert result.evidence[1]['kind']=='PRESENT'
        assert np.count_nonzero(result.labels==2)>50


@pytest.mark.parametrize('semantics',['UNKNOWN','STRONGEST_RETURN','MULTIPLE_RETURN'])
def test_nonfirst_return_cannot_clear(semantics):
    r=compare_raw_returns(packet(z=3,semantics=semantics),[record(obs(packet()))])
    assert r.evidence[0]['kind']=='UNKNOWN'
    assert r.evidence[0]['reason']=='RETURN_SEMANTICS_UNVERIFIED'


def test_no_return_and_robot_not_free():
    p=packet();p.points_sensor[:]=0
    assert compare_raw_returns(p,[record(obs(packet()))]).evidence[0]['kind']=='UNKNOWN'
    p=packet(z=3);p.robot_mask=np.ones(len(p.points_sensor),bool)
    assert compare_raw_returns(p,[record(obs(packet()))]).evidence[0]['kind']=='UNKNOWN'


def test_partial_reference_cannot_clear_whole_object():
    r=record(obs(packet(),full=False))
    assert compare_raw_returns(packet(z=3),[r]).evidence[0]['kind']=='PATCH_CLEARED'
    assert r.geometry['body_center_map'].value is None


def test_uncertain_pose_has_no_clear_authority():
    p=packet(z=3);p.pose_error_bound_m=.2
    assert compare_raw_returns(p,[record(obs(packet()))]).evidence[0]['kind']=='UNKNOWN'


def test_body_front_and_top_centers_are_distinct():
    p=packet();o=obs(p);g=o.geometry()
    np.testing.assert_allclose(g['body_center_map'].value,[0,0,2.15])
    assert g['body_center_map'].status=='PRIOR_ESTIMATED'
    assert g['contact_center_map'].value is None
    o.face_type='TOP';g=o.geometry()
    np.testing.assert_allclose(g['body_center_map'].value,[0,0,2.25])
    assert g['axis_symmetry'].value=='ROTATION_PI_ABOUT_Z'
    assert np.linalg.det(g['body_orientation_map'].value)==pytest.approx(1)


def test_front_left_edge_sign_cannot_invert_world_up():
    o=obs(packet());o.face.normal=[-1,0,0];o.face.center=[2,0,0]
    o.face.polygon=[[2,-.25,-.25],[2,.25,-.25],[2,.25,.25],[2,-.25,.25]]
    o.horizontal=[0,1,0]
    rotation=np.asarray(o.geometry()['body_orientation_map'].value)
    assert rotation[2,2]>.99
    np.testing.assert_allclose(rotation[:,1],[1,0,0])


def test_readonly_replay_cannot_commit_even_if_caller_asks():
    m=ActivePerceptionMap();p=packet();p.stable_capture=False
    a=m.analyze(p,[obs(p)],read_only=True)
    assert not a.capture.stable_capture
    assert not m.commit([a],batch=False)['committed']


def test_wrist_target_and_base_transform_contract():
    from camera_unload_perception.active_perception.policy import wrist_verify
    m=ActivePerceptionMap();seed(m);p=packet(4);o=obs(p)
    transform=np.eye(4);transform[:3,3]=[1,2,3]
    result=wrist_verify(m,0,[o],transform,p.capture_id)
    assert result['status']=='VERIFIED';np.testing.assert_allclose(result['body_center_base'],[1,2,5.15])
    assert wrist_verify(m,99,[o],transform,p.capture_id)['status']=='TARGET_NOT_EXECUTABLE'
    assert wrist_verify(m,0,[],transform,p.capture_id)['status']=='TARGET_IDENTITY_AMBIGUOUS_OR_ABSENT'
    assert wrist_verify(m,0,[o],np.zeros((4,4)),p.capture_id)['status']=='INVALID_BASE_TRANSFORM'


def test_front_and_top_observations_share_body_id():
    m=ActivePerceptionMap();aa=[]
    for t in (1,2,3):
        p=packet(t);o=obs(p);o.face.center=[2,0,0];o.face.normal=[-1,0,0];o.horizontal=[0,1,0]
        o.face.polygon=[[2,-.25,-.25],[2,.25,-.25],[2,.25,.25],[2,-.25,.25]]
        aa.append(m.analyze(p,[o]))
    assert m.commit(aa)['committed']
    p=packet(4);top=obs(p);top.face_type='TOP';top.horizontal=[0,-1,0];top.face.center=[2.15,0,.25];top.face.normal=[0,0,1]
    top.face.polygon=[[2,-.25,.25],[2.3,-.25,.25],[2.3,.25,.25],[2,.25,.25]]
    a=m.analyze(p,[top]);assert a.assignments=={0:0}
    assert m.commit([a],batch=False)['committed'];assert m.next_id==1


def test_only_corner_rays_do_not_clear_full_face():
    p=packet(z=3);p.points_sensor[:,:2]=.34
    r=compare_raw_returns(p,[record(obs(packet()))]);assert r.evidence[0]['kind']=='UNKNOWN'
    assert r.evidence[0]['coverage']<.4


def test_edge_cells_never_make_reference_coverage_exceed_100_percent():
    p=packet(z=3);o=obs(packet());o.face.polygon=[[-.25,-.15,2],[-.15,-.25,2],[.15,-.25,2],[.25,-.15,2],
                                      [.25,.15,2],[.15,.25,2],[-.15,.25,2],[-.25,.15,2]]
    e=compare_raw_returns(p,[record(o)]).evidence[0]
    assert 0<e['coverage']<=1 and e['cells']<=e['reference_cells']


def test_measured_raw_patch_can_be_full_even_when_size_is_prior():
    from camera_unload_perception.active_perception.odin_adapter import project_depth,observation_from_odin
    p=packet();_,source,world=project_depth(p)
    mask=np.zeros(p.image.shape[:2],bool);mask[185:295,265:375]=True
    candidate=dict(_mask=mask,center_map=[0,0,2],normal_world=[0,0,-1],horizontal_world=[1,0,0],
        measurement_quality={'size_prior_fallback':True,'plane_rmse_m':.003},
        measurement={'size_prior_regularization':{'used':True},'partial_observation':{}},
        size_world=[.5,.5,.3],position_quality_ok=True,orientation_quality_ok=True)
    measured=observation_from_odin(candidate,p,source,world)
    assert measured.face.full_face_observed
    assert measured.face.source=='ODIN_CLOUD_RAW'
    assert measured.dimension_status==('PRIOR_ESTIMATED','PRIOR_ESTIMATED','PRIOR_ESTIMATED')
    candidate['measurement']['partial_observation']={'left':True}
    assert not observation_from_odin(candidate,p,source,world).face.full_face_observed


def test_operator_can_confirm_repeated_unknown_returns_without_forging_semantics():
    m=ActivePerceptionMap();seed(m)
    for start in (4,7):
        aa=[m.analyze(packet(t,3,'UNKNOWN'),[]) for t in (start,start+1,start+2)]
        assert m.commit(aa)['committed']
    assert m.records[0].life_state=='CONFIRMED' and m.records[0].clear_run==0
    assert m.records[0].review_clear_runs==2
    previous=m.revision
    result=m.confirm_removed(0,'physically removed from the stack',previous,m.last_stamp+.1)
    assert result['committed'] and result['delta']['removed']==[0]
    assert m.records[0].life_state=='VACATED' and m.records[0].positives>0
    assert m.records[0].last_evidence['operator_confirmation']['source']=='HUMAN_PHYSICAL_CHECK'
    assert m.snapshot()['inventory']==[] and m.snapshot()['history'][0]['box_id']==0


def test_operator_cannot_confirm_without_current_independent_review():
    m=ActivePerceptionMap();seed(m)
    with pytest.raises(ValueError):m.confirm_removed(0,'physically removed from the stack',m.revision,m.last_stamp)
    for start in (4,7):
        m.commit([m.analyze(packet(t,3,'UNKNOWN'),[]) for t in (start,start+1,start+2)])
    with pytest.raises(ValueError,match='map changed'):m.confirm_removed(0,'physically removed from the stack',m.revision-1,m.last_stamp)
    with pytest.raises(ValueError,match='stale'):m.confirm_removed(0,'physically removed from the stack',m.revision,m.last_stamp+20)
    p=packet(10,2,'UNKNOWN');m.process(p,[obs(p)])
    assert m.records[0].review_clear_runs==0
    with pytest.raises(ValueError):m.confirm_removed(0,'physically removed from the stack',m.revision,m.last_stamp)


def test_continuous_driver_resume_requires_matching_boxes_and_original_version(tmp_path):
    from types import SimpleNamespace
    from camera_unload_perception.active_perception.app import Project,DemoSource,ROOT
    m=ActivePerceptionMap();samples=[]
    for t in (1,2,3):
        p=packet(t);samples.append(m.analyze(p,[obs(p),obs(p,x=.65)]))
    assert m.commit(samples)['committed']
    m.save(tmp_path/'checkpoint.json')
    source=DemoSource();source.driver_identity=dict(start_wall=0,pid=111)
    args=SimpleNamespace(config=None,root=ROOT,output=tmp_path,mode='paused',live=True,resume_driver_continuity=True,start_driver=False)
    project=Project(args,source)
    incoming=packet(4);incoming.map_epoch='fresh';incoming.source='ODIN_CLOUD_RAW'
    project.restore_for_capture(incoming,[obs(incoming),obs(incoming,x=.65)])
    assert incoming.map_epoch=='test_map' and sorted(project.manager.records)==[0,1]
    assert project.manager.next_id==2
    project2=Project(args,source);incoming2=packet(4);incoming2.map_epoch='fresh';incoming2.source='ODIN_CLOUD_RAW'
    source.driver_identity=dict(start_wall=9,pid=112)
    with pytest.raises(ValueError):project2.restore_for_capture(incoming2,[obs(incoming2),obs(incoming2,x=.65)])


def test_error_reinstatement_is_audited_and_allocator_never_rewinds():
    m=ActivePerceptionMap();seed(m);m.records[0].life_state='VACATED';seed(m,start=4)
    m.reinstate(0,1,'operator reviewed false clear against recording')
    assert m.records[0].life_state=='CONFIRMED' and m.records[1].life_state=='VACATED'
    assert m.next_id==2 and m.events[-1]['type']=='REINSTATE'
    assert not m.execution_candidates()


def test_far_local_targets_get_separate_aim_requests():
    m=ActivePerceptionMap();seed(m);r=copy.deepcopy(m.records[0]);r.box_id=1;r.face.center=[2,0,2];m.records[1]=r
    s=ActiveObservationScheduler(m);s.start('LOCAL_CHECK',[0,1],robot_ready=True)
    assert len(s.session['views'])==2 and s.session['target_region']==[[0,0,2.],[2,0,2]]


def test_ambiguous_detection_does_not_create_or_steal_ids():
    m=ActivePerceptionMap();seed(m);r=copy.deepcopy(m.records[0]);r.box_id=1;r.face.center=[.08,0,2];m.records[1]=r;m.next_id=2
    p=packet(4);o=obs(p,x=.04);a=m.analyze(p,[o])
    assert a.statuses[0]=='IDENTITY_AMBIGUOUS' and not a.assignments
    m.commit([a],batch=False);assert m.next_id==2


def test_unusable_new_orientation_is_not_silently_old_grasp_pose():
    m=ActivePerceptionMap();seed(m);p=packet(4);o=obs(p);o.orientation_ok=False;o.face.trusted=False
    assert m.process(p,[o])['committed']
    assert m.records[0].geometry['body_orientation_map'].status=='RETAINED'
    assert not m.execution_candidates()


def test_replay_bad_frame_advances_and_reports_reason(tmp_path,monkeypatch):
    from camera_unload_perception.active_perception import app
    for name in ('a','b'):
        (tmp_path/(name+'.npz')).touch();(tmp_path/(name+'.jpg')).touch();(tmp_path/(name+'.json')).write_text('{"sensor_stamp":1}')
    def broken(path):raise ValueError('RECORDED_WORLD_POSE_UNAVAILABLE')
    monkeypatch.setattr(app,'load_saved_capture',broken);s=app.ReplaySource(tmp_path)
    assert s.next_capture() is None and s.index==1
    assert s.next_capture() is None and s.index==2
    assert len(s.health()['skipped'])==2


def test_recorded_zero_detection_masks_are_a_valid_capture(tmp_path):
    import cv2,json
    from camera_unload_perception.active_perception.odin_adapter import load_saved_capture
    path=tmp_path/'empty';raw=np.zeros(3,dtype=[('x','f4'),('y','f4'),('z','f4')]);raw['z']=2
    np.savez(path.with_suffix('.npz'),raw_cloud=raw,depth=np.ones((2,2)),masks_packed=np.array([],dtype=np.uint8))
    path.with_suffix('.json').write_text(json.dumps(dict(sensor_stamp=1,cloud_stamp=1,world_from_camera=np.eye(4).tolist(),tcl=np.eye(4).tolist(),intrinsics=packet().intrinsics)))
    cv2.imwrite(str(path.with_suffix('.jpg')),np.zeros((2,2,3),np.uint8))
    capture,meta,masks=load_saved_capture(path)
    assert masks.shape==(0,2,2) and len(capture.points_sensor)==3 and not capture.stable_capture


def test_wrist_two_independent_captures_and_timestamp_gate():
    from camera_unload_perception.active_perception.policy import wrist_verify_batch
    m=ActivePerceptionMap();seed(m);samples=[(packet(t),[obs(packet(t))]) for t in (4,5)]
    assert wrist_verify_batch(m,0,samples,np.eye(4),5)['status']=='VERIFIED'
    assert wrist_verify_batch(m,0,[samples[0],samples[0]],np.eye(4),4)['status']=='WRIST_DUPLICATE_CAPTURE'
    assert wrist_verify_batch(m,0,samples,np.eye(4),5.1)['status']=='BASE_TRANSFORM_STAMP_MISMATCH'


def test_robot_not_ready_immediately_revokes_execution_candidates(tmp_path):
    from types import SimpleNamespace
    from camera_unload_perception.active_perception.app import Project,DemoSource,ROOT
    project=Project(SimpleNamespace(config=None,root=ROOT,output=tmp_path,mode='manual'),DemoSource())
    seed(project.manager);assert project.manager.execution_candidates()==[0]
    project.control(dict(action='robot_ready',ready=False))
    assert project.manager.execution_candidates()==[]


def test_invalid_calibration_cannot_enter_native_geometry():
    p=packet();p.intrinsics['fx']=0
    with pytest.raises(ValueError,match='intrinsics'):p.validate()


def test_wall_can_finish_vacated_fixed_batch_with_known_rear_once():
    m=ActivePerceptionMap();seed(m);m.set_wall_batch([0]);m.records[0].life_state='VACATED'
    rear=obs(packet(),3);rear.face.polygon=[[-.4,-.4,3],[.4,-.4,3],[.4,.4,3],[-.4,.4,3]]
    rear.dimensions=[.8,.3,.8];r=record(rear,1);r.life_state='CONFIRMED';r.ever_confirmed=True;m.records[1]=r;m.next_id=2
    s=ActiveObservationScheduler(m);s.configure_region([[-.25,-.25,2],[.25,-.25,2],[.25,.25,2],[-.25,.25,2]])
    s.start('WALL_SCAN',robot_ready=True);revision=m.revision
    for view_index,view in enumerate(s.VIEWS):
        s.acknowledge(view)
        for i in range(3):
            p=packet(4+3*view_index+i,3);theta=np.radians(view_index*5)
            rot=np.array([[np.cos(theta),-np.sin(theta),0],[np.sin(theta),np.cos(theta),0],[0,0,1]])
            p.points_sensor=p.points_sensor@rot;p.t_world_sensor[:3,:3]=rot;p.t_world_camera[:3,:3]=rot
            s.feed(m.analyze(p,[]))
    assert s.session['state']=='COMPLETE'
    assert s.session['wall_batch_verified_complete'] and s.session['coverage_status']=='COMPLETE'
    assert m.revision==revision+1 and m.next_id==2 and m.wall_batch['member_ids']==[0]


def test_independent_camera_can_change_view_while_lidar_stays_fixed():
    m=ActivePerceptionMap();s=ActiveObservationScheduler(m);s.start('WALL_SCAN',robot_ready=True);s.acknowledge('UP')
    for t in (1,2,3):s.feed(m.analyze(packet(t),[]))
    s.acknowledge('DOWN');p=packet(4);theta=np.radians(5)
    p.t_world_camera[:3,:3]=[[1,0,0],[0,np.cos(theta),-np.sin(theta)],[0,np.sin(theta),np.cos(theta)]]
    s.feed(m.analyze(p,[]));assert s.session['state']=='COLLECTING' and s.session['collected']==1


@pytest.mark.parametrize('fault',['duplicate_ids','unstable','bad_tf','bad_time','bad_origin'])
def test_capture_contract(fault):
    p=packet()
    if fault=='duplicate_ids':p.point_ids[:]=0
    if fault=='unstable':p.stable_capture=False
    if fault=='bad_tf':p.t_world_camera[0,0]=2
    if fault=='bad_time':p.cloud_stamp+=.030
    if fault=='bad_origin':p.origins_sensor=[np.nan,0,0]
    with pytest.raises(ValueError):p.validate()


def test_epoch_allocates_once_and_rejects_duplicate_commit():
    m=ActivePerceptionMap();seed(m)
    assert list(m.records)==[0] and m.records[0].ever_confirmed
    p=packet(4);a=m.analyze(p,[obs(p)])
    assert m.commit([a],batch=False)['committed']
    assert not m.commit([a],batch=False)['committed']
    assert m.next_id==1


def test_batch_present_clear_conflict_rolls_back_everything():
    m=ActivePerceptionMap();seed(m);before=m.snapshot()
    aa=[m.analyze(packet(4),[obs(packet(4))]),m.analyze(packet(5,3),[]),m.analyze(packet(6,3),[])]
    assert not m.commit(aa)['committed']
    assert m.snapshot()==before


def test_lifecycle_once_per_epoch_unknown_breaks_clear_run():
    m=ActivePerceptionMap();seed(m)
    aa=[m.analyze(packet(t,3),[]) for t in (4,5,6)]
    assert m.commit(aa)['committed'];assert m.records[0].clear_run==1
    p=packet(7);p.points_sensor[:]=0;m.process(p,[])
    assert m.records[0].clear_run==0


def test_rear_gets_new_id_only_after_actual_front_clear():
    m=ActivePerceptionMap();seed(m)
    aa=[m.analyze(packet(t,3),[obs(packet(t,3),3)]) for t in (4,5,6)]
    assert aa[0].related[0]==[0] and aa[0].statuses[0]=='NEW_CANDIDATE'
    assert m.commit(aa)['committed'];assert list(m.records)==[0,1]
    assert 1 not in m.execution_candidates()
    for t in (7,10,13,16,19):
        aa=[m.analyze(packet(s,3),[obs(packet(s,3),3)]) for s in (t,t+1,t+2)]
        assert m.commit(aa)['committed']
    assert m.records[0].life_state=='VACATED'
    assert m.next_id==2


def test_unknown_return_allows_independent_rear_candidate_but_not_front_removal():
    m=ActivePerceptionMap();seed(m)
    p=packet(4,3,'UNKNOWN');a=m.analyze(p,[obs(p,3)])
    assert a.related[0]==[0] and a.statuses[0]=='NEW_CANDIDATE'
    assert m.commit([a],batch=False)['committed']
    assert m.records[0].life_state=='CONFIRMED' and m.records[0].review_clear_runs==1
    assert m.records[1].life_state=='TENTATIVE' and m.records[1].related_old_ids==[0]
    assert 1 not in [r['box_id'] for r in m.snapshot()['inventory']]
    for stamp in (5,6):
        p=packet(stamp,3,'UNKNOWN');assert m.process(p,[obs(p,3)])['committed']
    assert m.records[0].life_state=='CONFIRMED' and m.records[1].life_state=='CONFIRMED'
    assert 1 not in m.execution_candidates() and m.next_id==2


@pytest.mark.parametrize('blocker',['no_rays','partial_rear','front_still_detected'])
def test_unknown_return_does_not_approve_unverified_rear_without_independent_evidence(blocker):
    m=ActivePerceptionMap();seed(m)
    p=packet(4,3,'UNKNOWN');rear=obs(p,3)
    if blocker=='no_rays':p.points_sensor[:]=0
    if blocker=='partial_rear':rear.face.full_face_observed=False
    observations=[rear]
    if blocker=='front_still_detected':observations.insert(0,obs(p))
    a=m.analyze(p,observations)
    rear_index=len(observations)-1
    assert a.statuses[rear_index]=='LAYER_CHANGE_PENDING'
    assert m.commit([a],batch=False)['committed'] and m.next_id==1


def test_reoccupied_location_is_not_asserted_same_physical_object():
    m=ActivePerceptionMap();seed(m);m.records[0].life_state='VACATED'
    seed(m,start=4)
    assert m.next_id==2 and m.records[1].identity_status=='LOCATION_REOCCUPIED'
    assert 1 not in m.execution_candidates()


def test_last_capture_support_required_for_new_id():
    m=ActivePerceptionMap()
    aa=[m.analyze(packet(t),[obs(packet(t))] if t<3 else []) for t in (1,2,3)]
    assert m.commit(aa)['committed'];assert m.next_id==0


def test_batch_movement_is_atomic_failure():
    m=ActivePerceptionMap();aa=[]
    for t in (1,2,3):
        p=packet(t);p.t_world_sensor=p.t_world_sensor.copy();p.t_world_sensor[0,3]=t*.02
        aa.append(m.analyze(p,[obs(p)]))
    assert m.commit(aa)['reason']=='BATCH_MOVED';assert m.next_id==0


def test_checkpoint_revision_and_execution_freshness(tmp_path):
    m=ActivePerceptionMap();seed(m);path=tmp_path/'map.json';m.save(path)
    other=ActivePerceptionMap();other.restore(path,m.version)
    assert other.next_id==m.next_id and not other.execution_candidates()
    with pytest.raises(ValueError):other.restore(path,('wrong',))


def test_fixed_batch_does_not_append_rear():
    m=ActivePerceptionMap();seed(m);m.set_wall_batch([0])
    aa=[m.analyze(packet(t,3),[obs(packet(t,3),3)]) for t in (4,5,6)];m.commit(aa)
    assert m.wall_batch['member_ids']==[0]


def test_scan_requires_real_view_change_and_ack():
    m=ActivePerceptionMap();s=ActiveObservationScheduler(m)
    with pytest.raises(ValueError):s.start('WALL_SCAN')
    s.start('WALL_SCAN',robot_ready=True);assert not s.wants_capture()
    s.acknowledge('UP')
    for t in (1,2,3):s.feed(m.analyze(packet(t),[]))
    assert s.session['state']=='WAIT_OPERATOR' and s.session['view_index']==1
    s.acknowledge('DOWN');s.feed(m.analyze(packet(4),[]))
    assert s.session['reason']=='VIEW_NOT_CHANGED' and not s.session['scan_actions_done']


def test_unexplained_background_cannot_cover_wall():
    m=ActivePerceptionMap();s=ActiveObservationScheduler(m)
    s.configure_region([[-.25,-.25,2],[.25,-.25,2],[.25,.25,2],[-.25,.25,2]])
    s._coverage(m.analyze(packet(),[]));assert not s.covered


def test_obb_parallel_outside_rays():
    valid,near,far=intersect_obb(np.array([[0.,0.,0.],[2,0,0]]),np.array([[0.,0,1],[0.,0,1]]),[0,0,2],np.eye(3),[1,1,1])
    assert valid.tolist()==[True,False];assert near[0]==pytest.approx(1.5)


@pytest.mark.parametrize('config',[{'epoch_support':1},{'vacate_after':0},{'ray':{'min_coverage':0}},{'typo':1}])
def test_configuration_rejects_invalid_values(config):
    with pytest.raises(ValueError):ActivePerceptionMap(config=config)


def test_well_measured_odd_pose_box_is_still_created():
    """A strongly tilted face is fine as long as it is reliably, fully measured:
    the creation gate checks measurement quality, never pose plausibility."""
    m=ActivePerceptionMap();aa=[]
    n=unit([.4,.3,-.87]);c=np.array([0.,0.,2.]);u=unit(np.cross(n,[0,1.,0]));v=unit(np.cross(n,u))
    poly=[(c+.2*(su*u+sv*v)).tolist() for su,sv in [(-1,-1),(1,-1),(1,1),(-1,1)]]
    for t in (1,2,3):
        p=packet(t)
        surface=MeasuredSurface(c.tolist(),n.tolist(),poly,True,True,'SYNTHETIC',p.capture_id,p.rgb_stamp)
        o=BoxObservation(0,p.capture_id,surface,u.tolist(),[.3,.3,.3],position_ok=True,orientation_ok=True)
        assert o.geometry()['body_center_map'].value is not None
        aa.append(m.analyze(p,[o]))
    assert m.commit(aa)['committed']
    assert list(m.records)==[0] and m.records[0].life_state=='CONFIRMED'


@pytest.mark.parametrize('degrade',['partial_face','unreliable_orientation'])
def test_untrusted_candidate_is_not_confirmed(degrade):
    """Position-only / prior-only detections (no valid body_center) must never
    mint a confirmed identity, even when repeated across a full epoch."""
    m=ActivePerceptionMap();aa=[]
    for t in (1,2,3):
        p=packet(t);o=obs(p)
        if degrade=='partial_face':o.face.full_face_observed=False
        if degrade=='unreliable_orientation':o.orientation_ok=False;o.face.trusted=False
        assert o.geometry()['body_center_map'].value is None
        aa.append(m.analyze(p,[o]))
    assert m.commit(aa)['committed']
    assert m.next_id==0 and not m.records


def test_confirmed_box_without_measurable_geometry_decays_out():
    """A phantom already in the map (confirmed, high confidence, but no usable
    geometry and an untrusted face) is un-removable by the ray/CLEAR path, so it
    must decay autonomously; a genuinely measured neighbour is left untouched."""
    m=ActivePerceptionMap();seed(m)
    ph=obs(packet(4));ph.face.center=[.9,0,2];ph.face.polygon=[[.65,-.25,2],[1.15,-.25,2],[1.15,.25,2],[.65,.25,2]]
    r=record(ph,gid=1);r.life_state='CONFIRMED';r.ever_confirmed=True;r.positives=2.
    r.geometry['body_center_map'].value=None;r.geometry['body_center_map'].status='UNAVAILABLE';r.face.trusted=False
    m.records[1]=r;m.next_id=2
    assert compare_raw_returns(packet(2),[m.records[1]]).evidence[1]['reason']=='REFERENCE_NOT_MEASURED'
    vacated=False
    for start in (7,10,13,16,19,22,25,28):
        aa=[m.analyze(packet(t,2,'UNKNOWN'),[obs(packet(t,2))]) for t in (start,start+1,start+2)]
        assert m.commit(aa)['committed']
        if m.records[1].life_state=='VACATED':vacated=True;break
    assert vacated
    assert m.records[0].life_state=='CONFIRMED'
    assert 1 not in [b['box_id'] for b in m.snapshot()['inventory']]


def test_full_coverage_clear_vacates_within_five_frames_despite_sticky_confidence():
    """A long-confirmed box has capped positive mass, so the probability gate alone
    takes ~14 frames to VACATE even for a perfect see-through. A sustained run of
    full-coverage clears must remove it on run length alone, within 5 frames."""
    m=ActivePerceptionMap();seed(m)
    m.records[0].positives=12.;m.records[0].negatives=0.   # heavily confirmed / sticky
    assert m.records[0].probability>.9
    frames=0
    for t in range(4,4+8):
        assert m.process(packet(t,3),[])['committed'];frames+=1   # z=3 -> full-coverage CLEAR of the z=2 face
        assert m.records[0].last_evidence['details'][0]['coverage']==pytest.approx(1.,abs=.05)
        if m.records[0].life_state=='VACATED':break
    assert m.records[0].life_state=='VACATED' and frames<=5


def test_epoch_mode_decisive_clear_counts_captures_not_commits():
    """In epoch mode one commit covers 3 captures. Removal latency must be bounded
    by captures seen (vacate_after=4), i.e. the 2nd clear epoch, not the 4th."""
    m=ActivePerceptionMap();seed(m);m.records[0].positives=12.
    aa=[m.analyze(packet(t,3),[]) for t in (4,5,6)];assert m.commit(aa)['committed']
    assert m.records[0].clear_run==1 and m.records[0].decisive_clears==3 and m.records[0].life_state!='VACATED'
    aa=[m.analyze(packet(t,3),[]) for t in (7,8,9)];assert m.commit(aa)['committed']
    assert m.records[0].life_state=='VACATED' and m.records[0].decisive_clears==6


def test_partial_coverage_but_unanimous_clear_is_decisive():
    """Real Odin scene: only ~70 % of a vacated face gets valid returns behind it,
    yet every tested cell sees through. That must take the fast path (2 epochs),
    not the ~14-commit probability decay that a coverage gate forced."""
    m=ActivePerceptionMap();seed(m);m.records[0].positives=12.
    def partial(t):
        p=packet(t,3);keep=p.points_sensor[:,0]<.1
        p.points_sensor=p.points_sensor[keep];p.point_ids=np.arange(int(keep.sum()));return p
    for start in (4,7):
        aa=[m.analyze(partial(t),[]) for t in (start,start+1,start+2)];assert m.commit(aa)['committed']
    e=m.records[0].last_evidence['details'][0]
    assert e['kind']=='CLEAR' and .5<e['coverage']<.85 and e['ratios']['clear']==1.
    assert m.records[0].life_state=='VACATED' and m.records[0].clear_run==2


def test_localization_correction_reanchors_map_instead_of_dying():
    """A relocalization jump moves the map frame, not the boxes. Records must
    follow the correction so the same physical box keeps its ID; the shift used
    here (27 cm) exceeds the association gate, so without re-anchoring the box
    would have been minted as a new identity."""
    theta=np.radians(3);M=np.eye(4)
    M[:3,:3]=[[np.cos(theta),-np.sin(theta),0],[np.sin(theta),np.cos(theta),0],[0,0,1]];M[:3,3]=[.25,-.1,.02]
    def shifted(p):
        o=obs(p);R=M[:3,:3];o.face.center=(R@np.array(o.face.center)+M[:3,3]).tolist();o.face.normal=(R@np.array(o.face.normal)).tolist()
        o.face.polygon=[(R@np.array(q)+M[:3,3]).tolist() for q in o.face.polygon];o.horizontal=(R@np.array([1.,0,0])).tolist();return o
    stale=ActivePerceptionMap();seed(stale);p=packet(4)
    assert stale.analyze(p,[shifted(p)]).statuses[0]=='NEW_CANDIDATE'
    m=ActivePerceptionMap();seed(m);before=np.asarray(m.records[0].face.center);m.reanchor(M)
    np.testing.assert_allclose(m.records[0].face.center,M[:3,:3]@before+M[:3,3])
    assert m.events[-1]['type']=='MAP_REANCHORED' and m.events[-1]['translation_m']==pytest.approx(.27,abs=.01)
    a=m.analyze(p,[shifted(p)]);assert a.assignments=={0:0}
    assert m.process(p,[shifted(p)])['committed'] and m.next_id==1
    with pytest.raises(ValueError):m.reanchor(np.zeros((4,4)))
