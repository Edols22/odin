# Odin 主动感知算法当前总结

更新日期：2026-09-18。

本文描述当前工作区已经实现的 Odin 纸箱持久感知算法。当前“主动感知”指在实时、单次或批次观测下维护全局箱体状态；主动选择视角、控制云台、机器人运动和抓取尚未实现。

详细变更记录见 [ODIN_ACTIVE_PERCEPTION_ALGORITHM_CURRENT.md](ODIN_ACTIVE_PERCEPTION_ALGORITHM_CURRENT.md)，原始设计约束见 [/home/nvidia/odin_nx_persistent_box_mapping_spec.md](/home/nvidia/odin_nx_persistent_box_mapping_spec.md)。

## 1. 目标和边界

系统维护跨帧、跨视角的纸箱对象地图。单帧检测只是观测：

```text
检测到 ≠ 新箱子
没有检测到 ≠ 箱子已经消失
```

当前目标：使用已有纸箱 YOLO 分割模型识别纸箱，计算可见前表面几何，转换到全局 `map`，保持稳定的 `global_id`，处理新箱、遮挡、漏检、清空、分裂、合并和可能搬移，并输出浏览器和 ROS 诊断信息。

当前未实现：Next-Best-View、主动探索、云台/机器人运动、抓取规划、抓槽精定位、D405 手眼相机、完整六面箱体模型和生产 CameraFrame/任务接口。

## 2. 总体流程

```text
Odin 去畸变 RGB + cloud_raw + TF
        ↓ 时间配对
cloud_raw 投影为稀疏相机深度
        ↓
YOLO 实例分割 → P01 箱面测量和质量检查
        ↓
当前观测转换到 map
        ↓
历史箱体投影到当前相机
        ↓
位置/掩码/深度一对一关联
        ↓
分裂、合并、冲突和替换处理
        ↓
存在证据、生命周期和搬移假设
        ↓
更新全局地图、事件和可视化
```

主要实现文件：

| 文件 | 职责 |
| --- | --- |
| `tools/diagnostics/odin_persistent_box_test.py` | Odin 适配、同步、raw 深度、YOLO/P01、输出 |
| `camera_unload_perception/algorithms/carton_detector.py` | 箱面几何测量 |
| `camera_unload_perception/algorithms/persistent_carton_map.py` | 全局对象、投影、关联、生命周期和冲突 |
| `tools/diagnostics/odin_browser_server.py`、`tools/odin1/browser/app.js` | 控制和浏览器显示 |

## 3. 传感器和坐标

当前输入为 `/odin1/image/undistorted`、`/odin1/cloud_raw`、`/tf`、`/tf_static`、`/odin1/odometry` 和 `calib.yaml`。纸箱局部几何使用 `/odin1/cloud_raw`，不是 `/odin1/cloud_slam`；后者是 Odin 处理后的 `odom` 坐标点云，主要用于地图或环境显示。SLAM 提供全局位姿，不能替代局部箱面深度测量。

坐标变换为：

```text
p_camera = Tcl × p_lidar
T_map_camera = T_map_lidar × inverse(Tcl)
p_map = T_map_camera × p_camera
```

`center_map` 是可见前表面中心，不是整个箱子的体积中心。厚度固定使用 0.30 m 先验。RGB、点云时间差默认不超过 20 ms；没有有效 TF 时不提交地图。当前尚未使用 raw 点的 `offset_time` 做逐点运动去畸变。

## 4. 单帧检测和几何测量

1. YOLO 实例分割，最低置信度 0.70；前端只能提高不能降低。
2. 使用 `retina_masks=True`，掩码保持原图坐标。
3. 使用 P01 远拍粘连实例拆分。
4. 掩码内缩 5 像素，计算真实深度中位数、MAD、有效点数和空间覆盖率。
5. 调用共享 `visible_measurement`，得到前表面中心、法向、水平轴、角点和可见宽高。
6. 将 project-camera 输出转换到 optical，再转换到 map。

这里复用了 get_box_node 的 P01 几何函数，但没有调用 get_box_node 服务接口；Odin 适配、质量门控和持久地图是本项目自己的处理。

质量分为 `detection_quality_ok`、`position_quality_ok` 和 `orientation_quality_ok`。主要门控包括：有效深度点至少 80 个、空间覆盖率至少 0.40、位置平面内点率至少 0.50、姿态平面内点率至少 0.70、平面 RMSE 不超过 0.02 m、独立平面法向夹角不超过 12°，以及投影尺寸和宽高比检查。

位置通过而姿态未通过时，可以确认“看见了该箱子”并保留历史姿态，但不会用当前不可靠姿态覆盖全局记录，也不会把它作为可靠抓取姿态。

## 5. 全局投影和关联

每个非 `VACATED` 历史对象根据 map 中的中心、法向、水平轴、宽高和厚度先验构造 cuboid，投影到当前相机，得到预测掩码、预测表面深度和可见性信息。

历史箱体和当前检测需要通过中心距离（默认不超过 0.20 m）、掩码 IoU/交集、有效深度和深度残差（默认不超过 0.12 m）门控。通过门控后使用匈牙利算法形成一对一初始匹配，之后还要处理分裂、合并和几何冲突。可靠匹配直接采用当前合格测量，不把当前几何强行拉回历史锚点。

环境地图点云只用于定位和显示，不用于判断箱体存在，也不用于修正箱体姿态。

## 6. 分裂、合并、替换和搬移

- **分裂**：一个历史箱体对应多个检测片段时，要求联合掩码、深度和位置解释一致，片段沿用一个旧 ID。
- **合并**：一个检测覆盖多个历史箱体时，保留多个旧 ID，标记检测合并，不融合身份，也不给旧箱直接清空证据。
- **前后排替换**：前表面清空、后方出现更深的新平面且连续支持时，标记 `LAYER_REVEALED`，前后箱体保持独立 ID。
- **可能搬移**：根据时间、空间、尺寸和观测支持建立 `POSSIBLE_RELOCATION`；这是启发式假设，不是物理身份证明。
- **回到原位**：位置、掩码、深度和尺寸连续稳定支持后，可恢复原位置编号；这表示位置恢复，不证明是同一个物理箱子。

## 7. 可见性和生命周期

没有匹配到旧箱时不立即删除。系统使用历史可信前表面或明确标记的历史定位估计表面判断可见性，记录 `VISIBLE`、`OCCLUDED`、`OUT_OF_FOV`、`PARTIAL_FOV`、`UNKNOWN` 等原因。遮挡、视野外、定位失效、分割冲突和几何矛盾不能当作清空证据，也会打断连续清空计数。

存在概率为有界证据模型：

```text
P(exists) = (1 + positive_evidence)
            / (2 + positive_evidence + negative_evidence)
```

正负历史权重上限为 12。默认生命周期为 `TENTATIVE → CONFIRMED → MISSING_CANDIDATE → VACATED`；`VACATED` 保留在历史中，但不进入当前策略对象集合。当前已知箱体集合不等于当前可抓取集合。

## 8. 运行模式

| 模式 | 行为 |
| --- | --- |
| `realtime` | 按实际处理能力持续处理新观测 |
| `manual` | 空格或按钮处理一次新观测 |
| `epoch` | 默认收集 3 帧、至少 2 帧支持后统一提交 |

批次模式使用冻结的旧地图逐帧分析，最后统一提交；时间不递增、位姿不稳定、支持不足或出现矛盾时整批不提交。

## 9. 输出和可视化

浏览器显示 RGB 检测图、帧内编号 `D0`、全局编号 `#0`、实时点云、全局箱体、生命周期、存在概率、姿态质量、关联诊断和事件。蓝色历史轮廓是旧地图投影，不等于当前新检测。

主要 ROS 输出：`/box_map/objects`、`/box_map/current_measurements`、`/box_map/events`、`/box_map/active_boxes`、`/box_map/delta` 和 `/odin1/persistent_box_markers`。

主要文件输出位于 `runtime/results/odin_browser/`：`latest.json`、`current_measurements.json`、`persistent_map.json`、`active_boxes.json`、`map_delta.json`、`events.jsonl`、`debug.jpg`、`live_cloud.json` 和轮转的 `geometry_evidence/`。

## 10. 当前限制和后续工作

- raw 点云已经观察到厘米级深度变化；当前保护逻辑只能减少异常观测污染，不能恢复传感器绝对测距精度。
- 点云逐点扫描时间尚未去畸变；移动相机时存在额外误差风险。
- 箱体姿态是可见前表面平面法向加重力上方向，不是完整六自由度物体姿态的充分测量。
- 箱体厚度是先验，尚未完成跨面统一身份和完整三维重建。
- 全局管理和取证仍有较大 CPU 开销，尚未达到目标处理频率。
- 搬移分数没有外观特征或真实运动模型；跨重启和换地图的物理身份恢复尚未完成。
- 主动观察、NBV、机器人运动和抓取执行尚未接入。

后续顺序：先用固定平面、独立距离和姿态真值核验 raw 测距、标定和逐点时间误差；再优化全局投影和取证线程；随后增加多帧姿态稳定性和不确定度；最后实现主动观察和抓取接口。

## 11. 启动入口

```bash
cd /home/nvidia/perception_domain_nx
./src/robot_perception/scripts/diagnostics/run_odin_perception_dashboard.sh --autostart
```

部署到机器人实际坐标系时使用真实 TF：

```bash
./src/robot_perception/scripts/diagnostics/run_odin_perception_dashboard.sh \
  --localization-source tf --world-frame map --base-frame base
```
