#!/usr/bin/env bash
set -eo pipefail

DOMAIN_ROOT="${PERCEPTION_DOMAIN_ROOT:-/home/nvidia/perception_domain_nx}"
ODIN_WORKSPACE="${ODIN_WORKSPACE:-${DOMAIN_ROOT}/odin_v013_ws}"
CONFIG_FILE="${ODIN_CONFIG_FILE:-${ODIN_WORKSPACE}/src/odin_ros_driver/config/control_command.yaml}"
export ODIN_CALIB_DIR="${ODIN_CALIB_DIR:-${DOMAIN_ROOT}/runtime/odin1/calibration}"

mkdir -p "${ODIN_CALIB_DIR}" "${DOMAIN_ROOT}/runtime/odin1/maps"
exec 9>"${DOMAIN_ROOT}/runtime/odin1/driver.lock"
if ! flock -n 9; then
  echo 'Odin driver is already running; reuse the existing connection.' >&2
  exit 2
fi
if pgrep -f '/lib/odin_ros_driver/[h]ost_sdk_sample' >/dev/null; then
  echo 'An Odin driver already owns the camera; no second USB connection started.' >&2
  exit 2
fi
source /opt/ros/humble/setup.bash
source "${DOMAIN_ROOT}/install/setup.bash"
source "${ODIN_WORKSPACE}/install/setup.bash"
set -u
exec ros2 run odin_ros_driver host_sdk_sample --ros-args \
  -p config_file:="${CONFIG_FILE}"
