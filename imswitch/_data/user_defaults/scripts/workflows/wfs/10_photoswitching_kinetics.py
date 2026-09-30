"""Photoswitching kinetics example for photophysical workflow development.

Records repeated dark, readout, switch, and recovery phases so users can
estimate fluorescence switching, bleaching, and recovery kinetics from the
saved time traces.

Prerequisites:
  - Setup loaded: example_kiralux_teensy.json (or equivalent)
  - Camera configured for continuous streaming
  - Lasers mapped below to the wavelengths used by the sample
  - Sample and powers validated manually before running long recordings

Output:
  - <MEASUREMENTS_ROOT>/<YYYY_MM_DD>/photoswitching_<HHMMSS>/*.tif
  - per_frame_summary.csv with raw and dark-corrected phase metrics
  - phase_plan.csv with the executed illumination program

Notes:
  - The defaults use the WFS 488/405 nm setup names. For red rsFP/RESOLFT
    assays, remap READOUT_LASER and SWITCH_LASER to the appropriate green,
    orange, or red lasers for your microscope.
  - The first dark phase is used as the per-pixel background for the
    dark-corrected mean in the CSV.
"""
# ruff: noqa: F821
from __future__ import annotations

import csv
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import tifffile as tf


MEASUREMENTS_ROOT = Path("D:/Measurements")  # Adapt to your measurement folder.
DETECTOR_NAME = "Kiralux"

LASER_ALIASES = {
    "488": "488 (EXC) sn27311",
    "405": "405 (ACT) sn26647",
}
READOUT_LASER = "488"
SWITCH_LASER = "405"
ALL_LASERS = tuple(LASER_ALIASES)

EXPOSURE_US = 50_000
TARGET_FPS = 20.0
N_SWITCHING_CYCLES = 3

READOUT_POWER_MW = 2.0
SWITCH_POWER_MW = 1.0

DARK_BASELINE_S = 1.0
READOUT_INITIAL_S = 2.0
SWITCH_PULSE_S = 0.5
READOUT_AFTER_SWITCH_S = 2.0
RECOVERY_DARK_S = 1.0


@dataclass(frozen=True)
class Phase:
    label: str
    lasers: tuple[str, ...]
    powers_mw: tuple[float, ...]
    duration_s: float


def build_phase_plan() -> list[Phase]:
    phases = [
        Phase("dark_baseline", (), (), DARK_BASELINE_S),
        Phase("readout_initial", (READOUT_LASER,), (READOUT_POWER_MW,), READOUT_INITIAL_S),
    ]
    for cycle in range(1, N_SWITCHING_CYCLES + 1):
        phases.extend(
            [
                Phase(
                    f"switch_pulse_{cycle}",
                    (SWITCH_LASER,),
                    (SWITCH_POWER_MW,),
                    SWITCH_PULSE_S,
                ),
                Phase(
                    f"readout_after_switch_{cycle}",
                    (READOUT_LASER,),
                    (READOUT_POWER_MW,),
                    READOUT_AFTER_SWITCH_S,
                ),
                Phase(f"recovery_dark_{cycle}", (), (), RECOVERY_DARK_S),
                Phase(
                    f"readout_after_recovery_{cycle}",
                    (READOUT_LASER,),
                    (READOUT_POWER_MW,),
                    READOUT_AFTER_SWITCH_S,
                ),
            ]
        )
    return phases


def output_folder() -> Path:
    now = time.localtime()
    folder = (
        MEASUREMENTS_ROOT.expanduser()
        / time.strftime("%Y_%m_%d", now)
        / f"photoswitching_{time.strftime('%H%M%S', now)}"
    )
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def write_phase_plan(folder: Path, phases: list[Phase]) -> None:
    with open(folder / "phase_plan.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["phase_index", "label", "lasers", "powers_mw", "duration_s"],
        )
        writer.writeheader()
        for index, phase in enumerate(phases):
            writer.writerow(
                {
                    "phase_index": index,
                    "label": phase.label,
                    "lasers": ";".join(phase.lasers),
                    "powers_mw": ";".join(str(power) for power in phase.powers_mw),
                    "duration_s": phase.duration_s,
                }
            )


def acquire_phase(facade, phase: Phase) -> np.ndarray:
    n_frames = max(1, int(round(phase.duration_s * TARGET_FPS)))
    camera_started = False

    try:
        facade.cam.prepare_acquisition(n_frames)
        facade.cam.start_acquisition()
        camera_started = True

        if phase.lasers:
            facade.laser_con.set_constant_power(list(phase.lasers), list(phase.powers_mw))

        time.sleep(phase.duration_s)
    finally:
        if phase.lasers:
            facade.laser_con.laser_off(phase.lasers)
        if camera_started:
            facade.cam.stop_acquisition()

    data = facade.cam.get_data()
    if data is None:
        raise RuntimeError(f"No camera frames returned for phase {phase.label!r}")

    stack = np.asarray(data)
    if stack.ndim == 2:
        stack = stack[np.newaxis, :, :]
    if stack.ndim != 3:
        raise RuntimeError(f"Expected a 3D frame stack for {phase.label!r}, got {stack.shape}")
    return stack


def summarize_stack(
    phase_index: int,
    phase: Phase,
    stack: np.ndarray,
    phase_start_s: float,
    background_map: np.ndarray | None,
) -> list[dict]:
    rows = []
    for frame_index, frame in enumerate(stack):
        frame_f = frame.astype(np.float32, copy=False)
        corrected = frame_f - background_map if background_map is not None else frame_f
        rows.append(
            {
                "phase_index": phase_index,
                "phase": phase.label,
                "frame_index": frame_index,
                "time_s": phase_start_s + frame_index / TARGET_FPS,
                "lasers": ";".join(phase.lasers),
                "powers_mw": ";".join(str(power) for power in phase.powers_mw),
                "raw_mean_counts": float(np.mean(frame_f)),
                "raw_median_counts": float(np.median(frame_f)),
                "raw_p95_counts": float(np.percentile(frame_f, 95)),
                "dark_corrected_mean_counts": float(np.mean(corrected)),
                "dark_corrected_p95_counts": float(np.percentile(corrected, 95)),
            }
        )
    return rows


facade = api.imcontrol.buildWorkflowFacade(
    laser_aliases=LASER_ALIASES,
    detector_name=DETECTOR_NAME,
)

phases = build_phase_plan()
folder = output_folder()
write_phase_plan(folder, phases)

summary_rows: list[dict] = []
background_map = None
elapsed_s = 0.0
original_exposure_us = facade.cam.expo

try:
    facade.cam.expo = EXPOSURE_US
    facade.laser_con.laser_off(ALL_LASERS)
    facade.laser_con.set_modulation_mode(None)

    for phase_index, phase in enumerate(phases):
        print(f"Phase {phase_index + 1}/{len(phases)}: {phase.label}")
        stack = acquire_phase(facade, phase)
        tf.imwrite(folder / f"{phase_index:02d}_{phase.label}.tif", stack)

        summary_rows.extend(
            summarize_stack(phase_index, phase, stack, elapsed_s, background_map)
        )

        if phase.label == "dark_baseline":
            background_map = np.mean(stack.astype(np.float32), axis=0)

        elapsed_s += phase.duration_s
finally:
    facade.laser_con.laser_off(ALL_LASERS)
    facade.laser_con.set_modulation_mode(None)
    facade.cam.expo = original_exposure_us

with open(folder / "per_frame_summary.csv", "w", newline="", encoding="utf-8") as handle:
    writer = csv.DictWriter(handle, fieldnames=list(summary_rows[0].keys()))
    writer.writeheader()
    writer.writerows(summary_rows)

print(f"Photoswitching kinetics complete: {folder}")
