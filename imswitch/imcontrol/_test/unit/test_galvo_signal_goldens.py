"""Golden byte-identity tests for GalvoScanDesigner.make_signal.

Phase A of docs/galvo-designer-single-axis-findings.md refactors the smooth
fast-axis generation (period slicing instead of the tiled middle). These
tests pin the designer's complete output — every emitted waveform plus the
load-bearing scanInfo fields — for the successful multi-axis baseline
fixtures the plan lists, so the refactor is provably behavior-preserving.

Baselines live in ``data/galvo_goldens/<case>.npz`` and were captured BEFORE
the refactor (see the capturing commit). Regenerate deliberately with::

    GALVO_GOLDEN_REGEN=1 QT_QPA_PLATFORM=offscreen \
        python -m pytest imswitch/imcontrol/_test/unit/test_galvo_signal_goldens.py

A regeneration is a statement that the output was MEANT to change — never
regenerate to silence a mismatch you cannot explain.
"""
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.model.signaldesigners.GalvoScanDesigner import (
    GalvoScanDesigner,
)

GOLDEN_DIR = Path(__file__).parent / "data" / "galvo_goldens"

# scanInfo fields pinned alongside the waveforms. tot_scan_time_s is included
# on purpose: it was made truthful (scan_samples_total * scan_time_step)
# immediately before capture, and must not drift again.
INFO_SCALARS = [
    "scan_samples_total", "scan_samples_d2_period", "n_pixels_fast",
    "samples_per_pixel", "dwell_time", "scan_time_step", "n_linesteps",
    "scan_throw_startzero", "scan_throw_settling", "scan_throw_startacc",
    "phase_delay", "tot_scan_time_s",
]
INFO_LISTS = [
    "img_dims", "pixel_sizes", "scan_samples", "scan_pads_initpos",
    "minmaxes", "smooth_axes", "axis_names", "img_axes_phys",
]


def _setup_sted_like():
    """example_sted-like device set: two galvos, a stiff piezo, two mocks."""
    def positioner(conv, vel, acc, minv, maxv):
        return SimpleNamespace(
            forScanning=True,
            managerProperties={
                "conversionFactor": conv, "minVolt": minv, "maxVolt": maxv,
                "vel_max": vel, "acc_max": acc,
            },
        )

    positioners = {
        "GalvoX": positioner(17.44, 0.1, 0.0001, -10, 10),
        "GalvoY": positioner(16.63, 0.1, 0.0001, -10, 10),
        "PiezoZ": positioner(1.0, 1000.0, 1000.0, 0, 10),
        "Mock-Repeat": positioner(1, 1000, 1000, 0, 200),
        "Mock-Timelapse": positioner(1, 1000, 1000, 0, 200),
    }
    return SimpleNamespace(
        scan=SimpleNamespace(sampleRate=100000, lineClockLine=None,
                             frameClockLine=None, frameStartClockLine=None,
                             frameEndClockLine=None),
        positioners=positioners,
    )


ALL_DEVICES = ["GalvoX", "GalvoY", "PiezoZ", "Mock-Repeat", "Mock-Timelapse"]


def _params(targets, lengths, steps, centers=None, n_linesteps=None):
    """Build a parameter dict the way the controllers do: the assigned scan
    dims first, then every remaining forScanning positioner as a 1-step dummy
    (length 1.0 / step 1.0), so len(target_device) == device_count."""
    targets = list(targets)
    lengths = list(lengths)
    steps = list(steps)
    centers = list(centers) if centers else [0.0] * len(targets)
    for dev in ALL_DEVICES:
        if dev not in targets:
            targets.append(dev)
            lengths.append(1.0)
            steps.append(1.0)
            centers.append(0.0)
    p = {
        "target_device": targets,
        "axis_length": lengths,
        "axis_step_size": steps,
        "axis_centerpos": centers,
        "axis_startpos": [[0]] * len(targets),
        "sequence_time": 2e-05,
        "phase_delay": 100.0,
        "d3step_delay": 0.0,
    }
    if n_linesteps is not None:
        p["n_linesteps"] = n_linesteps
    return p


# The plan's explicit baseline list: all of these SUCCEED at capture time.
CASES = {
    # smooth d1 + step d2 (the classic XY raster)
    "xy_galvo_galvo": _params(
        ["GalvoX", "GalvoY", "PiezoZ"], [0.7, 0.7, 1.0], [0.1, 0.1, 1.0]),
    # the rig's standard XZ: galvo d1, piezo staircase d2
    "xz_galvo_piezo": _params(
        ["GalvoX", "PiezoZ", "GalvoY"], [5.0, 10.0, 1.0], [0.1, 0.5, 1.0]),
    # 3-axis: d3 staircase on the piezo
    "xyz_3axis": _params(
        ["GalvoX", "GalvoY", "PiezoZ"], [0.7, 0.7, 2.0], [0.1, 0.1, 1.0]),
    # linestep expansion of the d2 blocks
    "xy_linesteps": _params(
        ["GalvoX", "GalvoY", "PiezoZ"], [0.7, 0.7, 1.0], [0.1, 0.1, 1.0],
        n_linesteps=3),
    # non-smooth (mock) d1 + smooth d2
    "mock_d1": _params(
        ["Mock-Repeat", "GalvoY", "GalvoX"], [3.0, 0.5, 1.0], [1.0, 0.1, 1.0]),
    # mock BETWEEN active smooth axes (d1 smooth, d2 mock, d3 smooth)
    "mock_between": _params(
        ["GalvoY", "Mock-Repeat", "GalvoX"], [0.5, 3.0, 0.5], [0.1, 1.0, 0.1]),
    # the degenerate-XZ rig workaround (2-step d1)
    "degenerate_xz": _params(
        ["GalvoX", "PiezoZ", "GalvoY"], [0.2, 10.0, 1.0], [0.1, 0.5, 1.0]),
}


def _run_case(params):
    sig, positions, info = GalvoScanDesigner().make_signal(
        params, _setup_sted_like())
    return sig, positions, info


def _flatten(sig, positions, info):
    """One flat {key: ndarray} dict for npz storage/comparison."""
    out = {}
    out["__devices__"] = np.array(list(sig.keys()))
    for dev, s in sig.items():
        out[f"sig::{dev}"] = np.asarray(s)
    out["positions"] = np.asarray(positions)
    for key in INFO_SCALARS:
        out[f"info::{key}"] = np.asarray(info[key])
    for key in INFO_LISTS:
        out[f"info::{key}"] = np.asarray(info[key])
    return out


@pytest.mark.parametrize("case", sorted(CASES))
def test_galvo_golden(case):
    flat = _flatten(*_run_case(CASES[case]))
    path = GOLDEN_DIR / f"{case}.npz"

    if os.environ.get("GALVO_GOLDEN_REGEN") == "1":
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(path, **flat)
        pytest.skip(f"regenerated golden {path.name}")

    assert path.exists(), (
        f"golden baseline {path} missing — run with GALVO_GOLDEN_REGEN=1 "
        f"on the commit the baseline should represent"
    )
    with np.load(path, allow_pickle=False) as stored:
        stored_keys = set(stored.files)
        assert stored_keys == set(flat), (
            f"{case}: key set changed: only-stored="
            f"{sorted(stored_keys - set(flat))} "
            f"only-new={sorted(set(flat) - stored_keys)}"
        )
        for key in sorted(flat):
            got, want = np.asarray(flat[key]), stored[key]
            assert got.shape == want.shape, (
                f"{case}/{key}: shape {got.shape} != golden {want.shape}")
            if got.dtype.kind in "US" or want.dtype.kind in "US":
                assert list(got.astype(str)) == list(want.astype(str)), (
                    f"{case}/{key} differs")
            else:
                assert got.dtype == want.dtype, (
                    f"{case}/{key}: dtype {got.dtype} != golden {want.dtype}")
                assert np.array_equal(got, want, equal_nan=True), (
                    f"{case}/{key}: values differ from golden "
                    f"(max |diff| = "
                    f"{np.max(np.abs(got.astype(float) - want.astype(float)))})"
                )
