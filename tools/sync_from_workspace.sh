#!/usr/bin/env bash
# Pull the Odin active-perception subset from the working tree into this repo.
# Source of truth for running on the NX is the workspace; this repo is the
# published copy. Usage: tools/sync_from_workspace.sh [&& git add -A && git commit]
set -euo pipefail
R="$(cd "$(dirname "$0")/.." && pwd)"
WS="${PERCEPTION_DOMAIN_ROOT:-/home/nvidia/perception_domain_nx}"
SRC="$WS/src/robot_perception"
rsync -a --files-from="$R/tools/package_files.txt" "$SRC/" "$R/"
rsync -a --delete --exclude='__pycache__' "$WS/tools/odin1/" "$R/tools/odin1/"
cp "$WS/odin_v013_ws/src/odin_ros_driver/config/control_command.yaml" "$R/odin_driver/control_command.yaml"
cp "$WS/runtime/odin1/calibration/calib.yaml" "$R/odin_driver/calibration/calib.yaml"
cd "$R" && git status --short
