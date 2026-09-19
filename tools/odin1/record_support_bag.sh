#!/usr/bin/env bash
set -Ee -o pipefail
ROOT="${PERCEPTION_DOMAIN_ROOT:-/home/nvidia/perception_domain_nx}"
record_args=("$@")
source /opt/ros/humble/setup.bash
source "${ROOT}/install/setup.bash"
source "${ROOT}/odin_v013_ws/install/setup.bash"
set -u
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
exec python3 "${ROOT}/src/robot_perception/tools/diagnostics/record_odin_support_bag.py" "${record_args[@]}"
