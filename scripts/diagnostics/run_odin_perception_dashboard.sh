#!/usr/bin/env bash
set -Ee -o pipefail
ROOT="${PERCEPTION_DOMAIN_ROOT:-/home/nvidia/perception_domain_nx}"
dashboard_args=("$@")
source /opt/ros/humble/setup.bash
source "${ROOT}/install/setup.bash"
source "${ROOT}/odin_v013_ws/install/setup.bash"
set -u
export PYTHONPATH="${ROOT}/src/robot_perception:${PYTHONPATH:-}"
# Do not forward the shell's positional parameters: ROS setup scripts and the
# desktop launcher can leave an implementation detail such as "pipefail" in
# $@, which the Qt application would then mistake for a command-line option.
export OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=1
exec python3 "${ROOT}/src/robot_perception/tools/diagnostics/odin_browser_server.py" "${dashboard_args[@]}"
