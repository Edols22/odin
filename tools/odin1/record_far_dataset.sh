#!/usr/bin/env bash
set -Eeo pipefail

DOMAIN_ROOT="${PERCEPTION_DOMAIN_ROOT:-/home/nvidia/perception_domain_nx}"
OUTPUT_ROOT="${ODIN_DATASET_ROOT:-${DOMAIN_ROOT}/runtime/datasets/odin1_far}"
CALIB_DIR="${ODIN_CALIB_DIR:-${DOMAIN_ROOT}/runtime/odin1/calibration}"
SCENE="medicine_box"
DURATION=0
NOTE=""
INCLUDE_RAW_IMAGES=0
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: record_far_dataset.sh [options]

Record synchronized Odin1 far-view data into a timestamped ROS 2 bag.

Options:
  --scene NAME            Short scene identifier (default: medicine_box)
  --duration SECONDS      Stop cleanly after this duration; 0 means Ctrl-C
  --note TEXT             Free-form capture note stored in session.json
  --output-root DIR       Dataset root directory
  --include-raw-images    Also record decoded and undistorted Image topics
  --dry-run               Validate inputs and print the rosbag command only
  -h, --help              Show this help

Examples:
  ./record_far_dataset.sh --scene front_3m --duration 30 \
    --note "3 m, frontal, warehouse lights"
  ./record_far_dataset.sh --scene oblique_left --include-raw-images
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

while (($#)); do
  case "$1" in
    --scene) [[ $# -ge 2 ]] || die "--scene requires a value"; SCENE="$2"; shift 2 ;;
    --duration) [[ $# -ge 2 ]] || die "--duration requires a value"; DURATION="$2"; shift 2 ;;
    --note) [[ $# -ge 2 ]] || die "--note requires a value"; NOTE="$2"; shift 2 ;;
    --output-root) [[ $# -ge 2 ]] || die "--output-root requires a value"; OUTPUT_ROOT="$2"; shift 2 ;;
    --include-raw-images) INCLUDE_RAW_IMAGES=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[[ "$DURATION" =~ ^[0-9]+$ ]] || die "--duration must be a non-negative integer"
[[ "$SCENE" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || \
  die "--scene may contain only letters, digits, dot, underscore and hyphen"

source /opt/ros/humble/setup.bash
source "${DOMAIN_ROOT}/install/setup.bash"
source "${DOMAIN_ROOT}/odin_v013_ws/install/setup.bash"
set -u

command -v ros2 >/dev/null || die "ros2 is unavailable"
ros2 bag record --help >/dev/null 2>&1 || die "rosbag2 recorder is unavailable"
[[ -s "${CALIB_DIR}/calib.yaml" ]] || die "missing Odin calibration: ${CALIB_DIR}/calib.yaml"

topics=(
  /odin1/image/compressed
  /odin1/cloud_raw
  /odin1/cloud_slam
  /odin1/imu
  /odin1/odometry
  /odin1/odometry_highfreq
  /tf
  /tf_static
)
if ((INCLUDE_RAW_IMAGES)); then
  topics+=(/odin1/image /odin1/image/undistorted)
fi

available_topics="$(ros2 topic list)"
for topic in "${topics[@]}"; do
  if [[ "$topic" == "/tf_static" ]]; then
    continue
  fi
  grep -Fxq "$topic" <<<"$available_topics" || die "required topic is missing: $topic"
done

for topic in /odin1/image/compressed /odin1/cloud_raw /odin1/odometry; do
  timeout 6s ros2 topic echo --qos-reliability best_effort --once "$topic" >/dev/null 2>&1 || \
    die "topic exists but has no data: $topic"
done

timestamp="$(date +%Y%m%d_%H%M%S)"
session_dir="${OUTPUT_ROOT}/${timestamp}_${SCENE}"
bag_dir="${session_dir}/rosbag"

record_cmd=(
  ros2 bag record
  --storage sqlite3
  --max-bag-size 4294967296
  --max-cache-size 268435456
  --output "$bag_dir"
  "${topics[@]}"
)

if ((DRY_RUN)); then
  printf 'Validated Odin stream. Command:\n'
  printf ' %q' "${record_cmd[@]}"
  printf '\n'
  exit 0
fi

mkdir -p "$session_dir"
cp "${CALIB_DIR}/calib.yaml" "${session_dir}/odin_calib.yaml"

SESSION_DIR="$session_dir" SCENE="$SCENE" NOTE="$NOTE" DURATION="$DURATION" \
INCLUDE_RAW_IMAGES="$INCLUDE_RAW_IMAGES" python3 - <<'PY'
import json
import os
import platform
from datetime import datetime
from pathlib import Path

session = Path(os.environ["SESSION_DIR"])
metadata = {
    "schema_version": 1,
    "capture_type": "odin1_medicine_box_far_view",
    "scene": os.environ["SCENE"],
    "note": os.environ["NOTE"],
    "started_at_local": datetime.now().astimezone().isoformat(),
    "requested_duration_sec": int(os.environ["DURATION"]),
    "include_raw_images": bool(int(os.environ["INCLUDE_RAW_IMAGES"])),
    "host": platform.node(),
}
(session / "session.json").write_text(
    json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
PY

echo "Recording Odin dataset: $session_dir"
echo "Move/view the sensor as needed; press Ctrl-C once to stop cleanly."

recorder_pid=""
stop_recorder() {
  if [[ -n "$recorder_pid" ]] && kill -0 "$recorder_pid" 2>/dev/null; then
    kill -INT "$recorder_pid" 2>/dev/null || true
    wait "$recorder_pid" 2>/dev/null || true
  fi
}
trap stop_recorder INT TERM

"${record_cmd[@]}" &
recorder_pid=$!

if ((DURATION > 0)); then
  end_time=$((SECONDS + DURATION))
  while kill -0 "$recorder_pid" 2>/dev/null && ((SECONDS < end_time)); do
    sleep 1
  done
  stop_recorder
else
  wait "$recorder_pid" || status=$?
  status="${status:-0}"
  if [[ "$status" -ne 0 && "$status" -ne 130 ]]; then
    die "rosbag recorder exited with status $status"
  fi
fi
trap - INT TERM

[[ -s "${bag_dir}/metadata.yaml" ]] || die "recording ended without rosbag metadata"
ros2 bag info "$bag_dir" >"${session_dir}/bag_info.txt"
du -sh "$session_dir"
echo "Capture complete: $session_dir"
