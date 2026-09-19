#!/usr/bin/env bash
set -eo pipefail

DOMAIN_ROOT="${PERCEPTION_DOMAIN_ROOT:-/home/nvidia/perception_domain_nx}"
export ODIN_CALIB_DIR="${ODIN_CALIB_DIR:-${DOMAIN_ROOT}/runtime/odin1/calibration}"
source /opt/ros/humble/setup.bash
source "${DOMAIN_ROOT}/install/setup.bash"
source "${DOMAIN_ROOT}/odin_v013_ws/install/setup.bash"
set -u

required_topics=(
  /odin1/image
  /odin1/image/compressed
  /odin1/image/undistorted
  /odin1/imu
  /odin1/cloud_raw
  /odin1/cloud_slam
  /odin1/odometry
  /odin1/odometry_highfreq
  /tf
)

required_services=(
  /odin1/get_ae
  /odin1/get_awb
  /odin1/set_ae
  /odin1/set_awb
)

failure=0
topics="$(ros2 topic list)"
services="$(ros2 service list)"
for name in "${required_topics[@]}"; do
  if grep -Fxq "${name}" <<<"${topics}"; then
    echo "PASS topic ${name} type=$(ros2 topic type "${name}")"
  else
    echo "FAIL missing topic ${name}" >&2
    failure=1
  fi
done
for name in "${required_services[@]}"; do
  if grep -Fxq "${name}" <<<"${services}"; then
    echo "PASS service ${name} type=$(ros2 service type "${name}")"
  else
    echo "FAIL missing service ${name}" >&2
    failure=1
  fi
done

if [[ -s "${ODIN_CALIB_DIR}/calib.yaml" ]]; then
  echo "PASS device calibration ${ODIN_CALIB_DIR}/calib.yaml"
else
  echo "FAIL device calibration was not received" >&2
  failure=1
fi

for name in /odin1/image /odin1/imu /odin1/cloud_raw /odin1/cloud_slam /odin1/odometry /odin1/odometry_highfreq; do
  if timeout 8s ros2 topic echo --qos-reliability best_effort --once "${name}" >/dev/null 2>&1; then
    echo "PASS data ${name}"
  else
    echo "FAIL no data on ${name}" >&2
    failure=1
  fi
done

exit "${failure}"
