#!/usr/bin/env bash
set -Ee -o pipefail
PROJECT_ROOT="${PERCEPTION_SOURCE_ROOT:-/home/nvidia/perception_domain_nx/src/robot_perception}"
DOMAIN_ROOT="${PERCEPTION_DOMAIN_ROOT:-/home/nvidia/perception_domain_nx}"
MODE=manual
while (($#)); do case "$1" in --mode) MODE="$2"; shift 2;; --help|-h) echo "Usage: $0 [--mode realtime|manual|epoch]"; exit 0;; *) break;; esac; done
detector_args=("$@")
source /opt/ros/humble/setup.bash
source "${DOMAIN_ROOT}/install/setup.bash"
source "${DOMAIN_ROOT}/odin_v013_ws/install/setup.bash"
set -u
export OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=1
exec python3 "${PROJECT_ROOT}/tools/diagnostics/odin_persistent_box_test.py" --mode "$MODE" "${detector_args[@]}"
