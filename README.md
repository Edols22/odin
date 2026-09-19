# odin — 奥丁之眼主动感知

机器人卸货视觉域中基于 Odin1(DTOF 面阵)的**主动感知**部分:用原生射线/箱面比对维护箱子生命周期地图(建号、跟踪、看穿移除、重定位重锚定),带 HTTP 界面、诊断工具和行为回归测试。

本仓库只收录主动感知这一块。它运行时依赖 `robot_perception` 主包里的三个模块(不在本仓库):
`camera_unload_perception.algorithms.carton_detector`、`camera_unload_perception.core.action_guard`、`camera_unload_perception.ros.camera_pipeline`
(经 `tools/diagnostics/odin_persistent_box_test.py` 的 YOLO 前端桥引用),以及 YOLO 权重 `assets/box_seg.pt`。

## 目录

| 路径 | 内容 |
|---|---|
| `camera_unload_perception/active_perception/` | 核心:`map_manager`(生命周期/移除/重锚定)、`ray_compare`(射线-面证据)、`live`(Odin ROS 输入)、`app`(HTTP UI)、`scheduler`、`policy`、`schema` |
| `camera_unload_perception/algorithms/persistent_carton_map.py` | 持久箱图 |
| `test/` | 行为回归测试(`test_active_perception_architecture.py` 等) |
| `tools/diagnostics/` | Odin 诊断/回放/录制工具 |
| `scripts/diagnostics/run_odin_*.sh` | 启动脚本 |
| `tools/odin1/` | 驱动启动(`run_odin1.sh`)、udev、录包、地图/深度查看器 |
| `odin_driver/` | 驱动配置 `control_command.yaml`(`dtof_fps: 145`)与本机标定 `calib.yaml` |
| `docs/` | 算法说明与事故记录 |

## 运行(Jetson Orin NX,ROS 2 Humble)

```bash
cd /home/nvidia/perception_domain_nx        # 工作区根,PYTHONPATH 含 src/robot_perception
bash tools/odin1/run_odin1.sh &             # 驱动
python3 -m camera_unload_perception.active_perception.app --live --host 0.0.0.0 \
  --output runtime/results/odin_expected_cloud --return-semantics FIRST_OPAQUE_RETURN
```
界面:`http://<NX>:8770`。`--return-semantics FIRST_OPAQUE_RETURN` 是在 Odin 测试阶段对回波语义的断言(Odin 驱动不输出回波类型),换 Mid-360 后应改用其真实回波信息。

## 测试

```bash
cd src/robot_perception
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest test/test_active_perception_architecture.py -q
```

## 与工作区同步

代码仍在 NX 工作区 `src/robot_perception` 里修改;发布时:
```bash
tools/sync_from_workspace.sh && git add -A && git commit && git push
```
