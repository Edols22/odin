# Odin Persistent Carton Dashboard

## Start

From `/home/nvidia/perception_domain_nx`:

```bash
./src/robot_perception/scripts/diagnostics/run_odin_perception_dashboard.sh --autostart
```

Open `http://127.0.0.1:8765`. `--autostart` starts the preview adapter; Start
enables detection. Without that option, Start also launches the adapter.
An existing Odin driver is reused. A second detector/server is rejected without
killing another process or trying to claim USB again. Ctrl+C stops processes
owned by this server, not an Odin connection started elsewhere.

For a browser on another computer, keep the default loopback binding and use:

```bash
ssh -L 8765:127.0.0.1:8765 nvidia@JETSON_IP
```

Then open the same localhost URL on that computer. `--port` selects another
unused port. The optional `--host` binding is for a trusted isolated network;
this diagnostic server has no authentication and must not be exposed publicly.

## Operator Controls

- Manual mode: each Space press or scan icon requests one computation.
  Keyboard input inside form fields does not trigger a computation.
- Realtime mode: computation continues while enabled.
- Epoch mode (稳定观察一批): Space/scan requests one batch, default three fresh
  synchronized RGB/cloud samples. Keep the camera still; the batch commits
  once. Pause, mode/parameter changes and reset cancel an unfinished batch.
  Progress and rejection reasons appear above the images. There is no 500 ms
  completion promise; the default timeout is 20 seconds.
- Pause: freezes inference/map updates; image and map-aligned live cloud remain live.
- Stop test: stops the owned detector. Restart creates a new object-ID session;
  use Pause to retain the current in-memory tracks.
- Reset: explicitly clears object tracks and the pose-anomaly latch, not the
  saved Odin environment map. Confirmation is required.
- Save: exports the current images, cloud, object state and parameters into
  `runtime/results/odin_browser/snapshots/`. It does not record a rosbag.
- The map has nearby-map and box-focus controls, orbit/zoom, top/front views,
  environment/live-cloud/trajectory/history toggles, point size and height clipping.
- Environment and live points farther than 5 m from the Odin camera's map
  position are hidden in the browser. The sphere follows the camera, not the
  orbit-view controls. Offline display uses the last stored camera position;
  without a position the points remain hidden. This affects rendering only.
- Saved map points are hidden by default; the 地图点云 checkbox can show them.
  Live points and box visualizations remain enabled by default.
- Selecting a box or table row focuses that world-space box and exposes its
  evidence, pose and covariance. VACATED objects are available through History.
- The policy tab shows the complete confirmed, not-yet-removed collection,
  including out-of-view and removal-candidate boxes. The policy-only checkbox
  filters both the 3D scene and table. New/unconfirmed candidates stay in the
  diagnostic map but are excluded from this policy collection.
- Parameters expose the mapper defaults and YOLO confidence/input size. P01
  confidence cannot be lowered below 0.70. Increasing input size costs inference time.

## Coordinates And Detection

Default environment: `runtime/odin1/maps/map_20260909_155803.bin`, containing
all 169 saved LiDAR keyframes, 338073 points, with the 5 m display filter applied.
This is a sparse localization map,
not a dense RGB reconstruction. See `../read_saved_map.py` for the locally
validated version-5 format and the coordinate-layout inference.

The configured Odin relocalization map must match the displayed map (mode 2).
Every observation uses `map <- cloud_frame` at the image timestamp, with <=20 ms
RGB/depth and RGB/odometry alignment. No odom-to-map fallback or latest-TF
substitution is used. The TF receiver has its own ROS node/executor. Preview
clouds independently select a recent point cloud for which map TF exists.

The detector uses `assets/box_seg.pt` and production P01 helpers
`_split_far_merged_instances` and `visible_measurement`, including the physical
size and visible-face gates. It does not call get_box_node services. Rejected
segmentation fragments may explain split/merge conflicts, but cannot create or
move a global object. The unobserved cuboid thickness is a 0.30 m prior, not a
measured rear surface. The reported center is the measured front-face center.

Odin depth is a sparse LiDAR projection, not dense RGB-D. Spatial support is
the fraction of mask sampling cells containing a real depth return (8 px cells
for the current 1600 x 1296 image). The 40% support threshold, minimum 80 real
points and plane-inlier gate remain independent. Unsampled pixels stay NaN;
no interpolated depth is used for matching or CLEAR evidence. The original RGB
valid-pixel ratio is reported separately and is not compared to a dense-depth
40% threshold. Sampling-cell size needs validation for other resolutions/sensors;
1 px retains the dense-depth definition.

The result image labels global IDs as `#0`, matching map labels and table rows.
`D0` identifies a frame-local detection only. The unannotated live image remains
independent of inference. Detection diagnostics show pipeline counts, timestamp
gates, per-object confidence, spatial coverage and plane quality; expandable
rows expose thresholds and rejection reasons. Raw JSON is collapsed by default.

The vendor-independent core is
`camera_unload_perception/algorithms/persistent_carton_map.py`. The ROS adapter,
detector and core run in one diagnostic process, not four independently deployed
production nodes. It publishes `/box_map/events`, `/box_map/objects` (JSON String)
and `/odin1/persistent_box_markers`. Robot motion, NBV and grasp execution are
not launched. This dashboard is not a replacement for the production CameraFrame
and robot-task interface integration.

Existence uses Beta evidence with bounded history (historical weight 12 before
adding each update). Four CLEAR updates alone do not guarantee VACATED:
probability must also fall below 0.25. UNKNOWN/occlusion/segmentation conflict
break the CLEAR run without reducing existence. An implausible pose jump
latches writes off; inspect localization before explicitly resetting the session.

CLEAR now requires a trusted measured surface, not an inferred cuboid side or
back. A depth shift in a highly overlapping detection still holds the old box.
The replacement branch additionally requires a deeper consistent new plane,
real farther background outside its silhouette and three supporting updates.
Without that independent evidence it remains unresolved. Object z-buffer
occlusion is advisory and cannot permanently override measured free-space rays.

Both single frames and batches commit from isolated transactions. An exception
cannot partially move an object or consume an ID. Epoch lifecycle updates once,
with two supporting frames able to confirm a latest-frame detection; mixed
reliable presence/CLEAR rejects the batch. Epoch confirmation is separate from
the single-frame probability/hit-count confirmation rule.

Additional JSON String topics: `/box_map/active_boxes` (reliable/transient-local
full snapshot) and `/box_map/delta` (successful commits only). Downloads are
`active_boxes.json` and `map_delta.json`. Check map hash, session ID, revision
and timestamp before consuming data; resync from the full snapshot after a
lost delta. `object_uid` qualifies the numeric ID by session. This does not
restore physical identity across map changes or restarts. Positions remain
front-face centers; quaternion xyzw axes are horizontal, vertical, outward normal.
The policy collection is not a list of immediately graspable targets.

September 12 live correction: reliable individual detections now take priority
over an additional composite mask, and redundant observations cannot allocate
duplicate carton IDs. Position and orientation quality are separate: the core
plane fraction is >=0.50 for position admission and >=0.70 for orientation,
with point count, coverage, residual and diagnostic-plane agreement still
required. Position-only matches hold the previous orientation and measured
surface; an unverified new orientation is drawn dashed. Consumers must check
`orientation_trusted`, `orientation_observed_ok` and `geometry_usable` instead
of treating detection confidence or collection membership as grasp readiness.

Current algorithm and limits: [Chinese implementation summary](../../../src/robot_perception/docs/ODIN_ACTIVE_PERCEPTION_ALGORITHM_CURRENT.md).

## Verification

```bash
PYTHONPATH=src/robot_perception OPENBLAS_NUM_THREADS=1 \
  python3 -m unittest discover -s src/robot_perception/test \
  -p test_persistent_carton_map.py -v
PYTHONPATH=src/robot_perception OPENBLAS_NUM_THREADS=1 \
  python3 -m unittest discover -s src/robot_perception/test \
  -p test_odin_browser_server.py -v
node tools/odin1/browser/verify_browser.cjs
node tools/odin1/browser/verify_diagnostics.cjs
```

Camera-free verification of the new policy/epoch UI:

```bash
node tools/odin1/browser/verify_persistent_policy_offline.cjs
# Source the ROS environment for adapter serialization/control tests:
PYTHONPATH=src/robot_perception OPENBLAS_NUM_THREADS=1 \
  python3 -m unittest discover -s src/robot_perception/test \
  -p test_odin_epoch_adapter.py -v
```

The offline browser test intercepts all requests and labels screenshots as
synthetic; it neither starts a camera nor writes to the live object map.

The browser test controls the running diagnostic session, selects manual/realtime
modes, and leaves inference paused. It writes screenshots and
`runtime/results/odin_browser/browser-verification.json`.

The deterministic core cases cover static/viewpoint identity, false negatives,
1-2-frame false positives, occlusion, removal/history, relocation, split, merge,
pose jumps, missing pose/depth, stacked boxes, rejected fragments and bad geometry.
Four cases specifically check sparse coverage, clustered returns, real-ray
visibility and minimum point count. They are synthetic regression tests, not
physical acceptance tests or bag tuning. Six additional server tests check
transactional parameter validation, strict threshold bounds, Beta priors,
reset confirmation and sparse sampling-cell bounds.

Browser verification uses the real camera, real full map and real live cloud.
When there are no accepted live boxes, box-rendering tests use a browser-only
intercepted synthetic response, marked prominently in those screenshots. This
does not write any synthetic objects to the live mapper. `fixtureOverlay` in the
report records that distinction.

`verify_diagnostics.cjs` is a separate read-only test that requires real confirmed
boxes and advancing inference. It never injects fixtures or changes controls.
It compares detection IDs, table rows and 3D labels over 20 samples, checks
structured diagnostics and expanded-row persistence, reads canvas pixels, and
saves desktop/mobile screenshots plus `diagnostics-verification.json`.

Remaining physical acceptance: all nine prescribed scenes with annotated Odin
bags, ID consistency/false vacancy metrics, threshold calibration and sustained
5-10 Hz with real accepted boxes. The September 9 sparse-depth adaptation fix
produced four real confirmed records without lowering P01 gates. This stationary
check does not establish acceptance for moving-camera, stacking or removal scenes.
No remaining physical acceptance is claimed complete.

## Frontend Build

The checked-in bundle runs without npm at launch. Rebuild after editing app.js:

```bash
NODE_PATH="$PWD/runtime/odin1/map_viewer_deps/node_modules" \
  runtime/odin1/map_viewer_deps/node_modules/.bin/esbuild \
  tools/odin1/browser/app.js --bundle --minify --format=iife \
  --outfile=tools/odin1/browser/app.bundle.js
```

Local dependencies: three 0.160.1, lucide 0.468.0, esbuild 0.25.0,
playwright 1.51.1. Python/ROS dependencies are the existing Humble perception
environment. Logs and bounded latest-state files are under
`runtime/results/odin_browser/`; per-frame event history is appended to
`events.jsonl`. Explicit snapshots retain image history; the bounded 32-slot
geometry evidence ring also retains recent inference samples for diagnosis.
# 当前测量与部署 TF（2026-09-12）

识别/P01 质量只由当前 RGB 和点云评估。已撤销历史位置/姿态锚点否决和向旧几何平滑靠拢；历史仍用于编号、存在状态和移除管理。黄色虚线 `D… 本帧 · 编号待关联` 是本次测量，不是新的全局 ID。诊断中分开显示当前测量质量与编号关联结果。

每帧的 `latest.json.current_measurements` 和 ROS `/box_map/current_measurements` 输出光学相机、全局及 base 坐标。未提供 base TF 时返回 null，前端明确提示。当前测试默认 `map` / `base_link`；实际机器人可使用：

```bash
./src/robot_perception/scripts/diagnostics/run_odin_perception_dashboard.sh \
  --localization-source tf --world-frame map --base-frame base
```

此模式由机器人系统提供相机话题和历史 TF，界面不会启动测试定位驱动，也不会把旧测试环境点云画进机器人 map。坐标源/名称为启动参数，不能在运行会话中直接改名续用旧记录。仍需实际机器人 TF 联调，不代表抓取接口已接入。
