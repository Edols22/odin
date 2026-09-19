#!/usr/bin/env bash
set -Ee pipefail
DOMAIN_ROOT="${PERCEPTION_DOMAIN_ROOT:-/home/nvidia/perception_domain_nx}"
START_ODIN=1; KEEP_ODIN=0; TIMEOUT_SEC=20
if [[ "${1:-}" == "--check-only" ]]; then START_ODIN=0; fi
source /opt/ros/humble/setup.bash
source "${DOMAIN_ROOT}/install/setup.bash"
source "${DOMAIN_ROOT}/odin_v013_ws/install/setup.bash"
set -u
odin_pid=""
cleanup() { if [[ "$KEEP_ODIN" -eq 0 && -n "$odin_pid" ]] && kill -0 "$odin_pid" 2>/dev/null; then kill -INT -- "-$odin_pid" 2>/dev/null || kill -INT "$odin_pid" 2>/dev/null || true; wait "$odin_pid" 2>/dev/null || true; fi; }
trap cleanup EXIT INT TERM
if [[ "$START_ODIN" -eq 1 ]] && ! pgrep -f '/odin_v013_ws/install/odin_ros_driver/.*/host_sdk_sample' >/dev/null; then
  echo "Starting Odin sensor only."
  setsid "${DOMAIN_ROOT}/tools/odin1/run_odin1.sh" & odin_pid=$!
fi
failure=0
for topic in /odin1/image/undistorted /odin1/cloud_slam /odin1/odometry /odin1/imu; do
  if timeout "$TIMEOUT_SEC" bash -c "until ros2 topic list 2>/dev/null | grep -Fxq '$topic'; do sleep 1; done"; then
    echo "PASS Odin topic $topic type=$(ros2 topic type "$topic")"
  else
    echo "FAIL missing Odin topic $topic" >&2; failure=1
  fi
done
services="$(ros2 service list 2>/dev/null | grep -E '/perception/(internal/)?(get_box|capture_wall|collect_carton_frames)' || true)"
if [[ -n "$services" ]]; then echo "PASS existing get_box services:"; printf '%s\n' "$services"; else echo "FAIL no existing get_box_node service is present" >&2; failure=1; fi
if ((failure)); then
  echo "Active-perception test NOT started: Odin adapter/persistent mapper are not in production launch." >&2
  echo "Existing get_box_node consumes the RealSense CameraFrame contract; Odin was not fed into it blindly." >&2
  exit 1
fi
echo "Prerequisites passed. No robot motion or grasp command was issued."
echo "Next: add Odin adapter for get_box_node CameraFrame, then attach persistent mapper."
