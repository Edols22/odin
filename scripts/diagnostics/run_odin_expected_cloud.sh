#!/usr/bin/env bash
set -Ee -o pipefail
ROOT="${PERCEPTION_DOMAIN_ROOT:-/home/nvidia/perception_domain_nx}"
project_args=("$@")
source /opt/ros/humble/setup.bash
source "${ROOT}/install/setup.bash"
source "${ROOT}/odin_v013_ws/install/setup.bash"
set -u
export PYTHONPATH="${ROOT}/src/robot_perception:${PYTHONPATH:-}"
export OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=1
exec python3 -m camera_unload_perception.active_perception.app "${project_args[@]}"
