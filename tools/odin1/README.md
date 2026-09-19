# Odin1 operator tools

## Far-view medicine-box dataset capture

Start the Odin driver first, then record one clearly identified scene per bag:

```bash
/home/nvidia/perception_domain_nx/tools/odin1/record_far_dataset.sh \
  --scene front_3m \
  --duration 30 \
  --note "3 m, frontal, warehouse lights"
```

The default capture contains the original compressed RGB stream, raw and SLAM
point clouds, IMU, odometry, and TF. It also copies the per-device calibration
into the session directory. Output is stored under
`runtime/datasets/odin1_far/<timestamp>_<scene>/` and must not be committed.

Use `--include-raw-images` only for short geometry/debug captures. Those topics
are decoded images and substantially increase disk bandwidth and dataset size;
they do not contain more source detail than the device's original JPEG stream.

Recommended scene names encode one controlled variation, for example:
`front_2m`, `front_4m`, `yaw_left_30`, `partial_occlusion`, `dim_light`, and
`stacked_mixed`. Put measured distance, medicine-box model, lighting, and any
occlusion details in `--note`.

