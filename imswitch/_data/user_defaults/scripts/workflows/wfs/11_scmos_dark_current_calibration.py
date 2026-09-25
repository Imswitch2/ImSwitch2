"""Photon-free sCMOS dark-current camera calibration example.

Acquires dark frame stacks at multiple exposure times and fits pixel-wise
linear models for:
  - baseline offset counts at 0 s exposure
  - dark current in counts/s
  - read-noise variance at 0 s exposure
  - thermal-noise variance in counts^2/s

Prerequisites:
  - Setup loaded: example_kiralux_teensy.json (or equivalent)
  - Camera covered or microscope shuttered so no light reaches the sensor
  - Lasers disabled by this script and also checked manually before running

Output:
  - <MEASUREMENTS_ROOT>/<YYYY_MM_DD>/camera_dark_calibration_<HHMMSS>/
  - Float32 TIFF calibration maps
  - exposure_summary.csv with whole-frame statistics per exposure
  - metadata.json with acquisition settings

This is an example acquisition and first-pass analysis script. Validate the
maps against your camera model and downstream correction algorithm before
using them for quantitative reconstruction.
"""
# ruff: noqa: F821
from __future__ import annotations

import csv
import json
import time
from pathlib import Path

import numpy as np
import tifffile as tf


MEASUREMENTS_ROOT = Path("D:/Measurements")  # Adapt to your measurement folder.
DETECTOR_NAME = "Kiralux"

LASER_ALIASES = {
    "488": "488 (EXC) sn27311",
    "405": "405 (ACT) sn26647",
}
ALL_LASERS = tuple(LASER_ALIASES)

EXPOSURE_TIMES_US = (1_000, 5_000, 10_000, 50_000, 100_000, 250_000, 500_000)
FRAMES_PER_EXPOSURE = 30
TARGET_FPS = 20.0
SETTLE_S = 0.2
SAVE_RAW_STACKS = False
NESTED_ACQUISITION = True


def output_folder() -> Path:
    now = time.localtime()
    folder = (
        MEASUREMENTS_ROOT.expanduser()
        / time.strftime("%Y_%m_%d", now)
        / f"camera_dark_calibration_{time.strftime('%H%M%S', now)}"
    )
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def acquire_single_dark_frame(facade, exposure_us: int) -> np.ndarray:
    facade.cam.expo = exposure_us
    time.sleep(SETTLE_S)

    frame_period_s = max(1.0 / TARGET_FPS, exposure_us * 1e-6)
    wait_s = frame_period_s + 0.1

    camera_started = False
    try:
        facade.cam.prepare_acquisition(1)
        facade.cam.start_acquisition()
        camera_started = True
        time.sleep(wait_s)
    finally:
        if camera_started:
            facade.cam.stop_acquisition()

    data = facade.cam.get_data()
    if data is None:
        raise RuntimeError(f"No camera frames returned at {exposure_us} us")

    stack = np.asarray(data)
    if stack.ndim == 2:
        return stack
    if stack.ndim != 3:
        raise RuntimeError(f"Expected a 2D frame or 3D frame stack, got {stack.shape}")
    return stack[0]


def fit_line(exposure_s: np.ndarray, maps: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    x = exposure_s.astype(np.float32)
    y = maps.astype(np.float32, copy=False)
    x_centered = x - float(np.mean(x))
    y_mean = np.mean(y, axis=0)
    denom = float(np.sum(x_centered * x_centered))
    if denom <= 0:
        raise RuntimeError("At least two distinct exposure times are required")
    slope = np.tensordot(x_centered, y - y_mean, axes=(0, 0)) / denom
    intercept = y_mean - slope * float(np.mean(x))
    return intercept.astype(np.float32), slope.astype(np.float32)


def write_float_map(folder: Path, name: str, data: np.ndarray) -> None:
    tf.imwrite(folder / f"{name}.tif", np.asarray(data, dtype=np.float32))


facade = api.imcontrol.buildWorkflowFacade(
    laser_aliases=LASER_ALIASES,
    detector_name=DETECTOR_NAME,
)

folder = output_folder()
original_exposure_us = facade.cam.expo
mean_maps = []
variance_maps = []
summary_rows = []
frames_by_exposure = {exposure_us: [] for exposure_us in EXPOSURE_TIMES_US}

try:
    facade.laser_con.laser_off(ALL_LASERS)
    facade.laser_con.set_modulation_mode(None)

    if NESTED_ACQUISITION:
        for repeat_index in range(FRAMES_PER_EXPOSURE):
            print(f"Nested dark-frame repeat {repeat_index + 1}/{FRAMES_PER_EXPOSURE}")
            for exposure_us in EXPOSURE_TIMES_US:
                frame = acquire_single_dark_frame(facade, exposure_us)
                frames_by_exposure[exposure_us].append(frame)
    else:
        for exposure_us in EXPOSURE_TIMES_US:
            print(f"Dark stack: {exposure_us} us x {FRAMES_PER_EXPOSURE} frames")
            for _ in range(FRAMES_PER_EXPOSURE):
                frame = acquire_single_dark_frame(facade, exposure_us)
                frames_by_exposure[exposure_us].append(frame)
finally:
    facade.laser_con.laser_off(ALL_LASERS)
    facade.laser_con.set_modulation_mode(None)
    facade.cam.expo = original_exposure_us

for exposure_us in EXPOSURE_TIMES_US:
    stack = np.stack(frames_by_exposure[exposure_us], axis=0)
    if stack.shape[0] < 2:
        raise RuntimeError("At least two frames are required for variance estimates")

    stack_f = stack.astype(np.float32, copy=False)
    mean_map = np.mean(stack_f, axis=0)
    variance_map = np.var(stack_f, axis=0, ddof=1)
    mean_maps.append(mean_map.astype(np.float32))
    variance_maps.append(variance_map.astype(np.float32))

    if SAVE_RAW_STACKS:
        tf.imwrite(folder / f"dark_{exposure_us}us.tif", stack)

    summary_rows.append(
        {
            "exposure_us": exposure_us,
            "frames": int(stack.shape[0]),
            "median_mean_counts": float(np.median(mean_map)),
            "median_variance_counts2": float(np.median(variance_map)),
            "median_noise_counts": float(np.sqrt(max(np.median(variance_map), 0.0))),
            "p99_mean_counts": float(np.percentile(mean_map, 99)),
            "p99_variance_counts2": float(np.percentile(variance_map, 99)),
        }
    )

exposure_s = np.asarray(EXPOSURE_TIMES_US, dtype=np.float32) * 1e-6
mean_maps_arr = np.stack(mean_maps, axis=0)
variance_maps_arr = np.stack(variance_maps, axis=0)

baseline_counts, dark_current_counts_per_s = fit_line(exposure_s, mean_maps_arr)
read_noise_variance_counts2, thermal_variance_counts2_per_s = fit_line(
    exposure_s,
    variance_maps_arr,
)

read_noise_variance_counts2 = np.maximum(read_noise_variance_counts2, 0.0)
thermal_variance_counts2_per_s = np.maximum(thermal_variance_counts2_per_s, 0.0)

write_float_map(folder, "baseline_offset_counts", baseline_counts)
write_float_map(folder, "dark_current_counts_per_s", dark_current_counts_per_s)
write_float_map(folder, "read_noise_variance_counts2", read_noise_variance_counts2)
write_float_map(folder, "read_noise_counts", np.sqrt(read_noise_variance_counts2))
write_float_map(folder, "thermal_variance_counts2_per_s", thermal_variance_counts2_per_s)
write_float_map(folder, "thermal_noise_counts_per_sqrt_s", np.sqrt(thermal_variance_counts2_per_s))

with open(folder / "exposure_summary.csv", "w", newline="", encoding="utf-8") as handle:
    writer = csv.DictWriter(handle, fieldnames=list(summary_rows[0].keys()))
    writer.writeheader()
    writer.writerows(summary_rows)

metadata = {
    "detector_name": DETECTOR_NAME,
    "exposure_times_us": list(EXPOSURE_TIMES_US),
    "frames_per_exposure": FRAMES_PER_EXPOSURE,
    "target_fps": TARGET_FPS,
    "nested_acquisition": NESTED_ACQUISITION,
    "save_raw_stacks": SAVE_RAW_STACKS,
    "map_units": {
        "baseline_offset_counts": "counts",
        "dark_current_counts_per_s": "counts/s",
        "read_noise_variance_counts2": "counts^2",
        "read_noise_counts": "counts",
        "thermal_variance_counts2_per_s": "counts^2/s",
        "thermal_noise_counts_per_sqrt_s": "counts/sqrt(s)",
    },
}
with open(folder / "metadata.json", "w", encoding="utf-8") as handle:
    json.dump(metadata, handle, indent=2)

print(f"Camera dark-current calibration complete: {folder}")
