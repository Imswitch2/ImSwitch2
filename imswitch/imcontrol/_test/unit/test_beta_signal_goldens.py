"""Golden tests for BetaScanDesigner.make_signal, and for the Advanced TTL
designer on the scans it builds.

The scan-program plan (docs/design/plans/scan-engines-and-expert-scan.md,
M0) moves stepped scans onto a reference interpreter. It must reproduce what
the Beta designer plays today, or the designer stays a compile path. These
tests pin today's output first -- every emitted waveform plus the scanInfo
fields consumers read -- over the designer's parameter space: centred and
start-anchored ROIs, the legacy absolute centres without a position snapshot,
two and three axes, line steps, a zero return time, dwells and returns that
are not whole samples (rounded up), and move/settle other than the defaults.
The TTL cases pin AdvancedScanTTLCycleDesigner on the same Beta scan
structure, which stays a compile path for Advanced-shaped programs.

Baselines live in ``data/beta_goldens/<case>.npz`` and were captured before
any change to either designer (see the capturing commit). Regenerate
deliberately with::

    BETA_GOLDEN_REGEN=1 QT_QPA_PLATFORM=offscreen \\
        python -m pytest imswitch/imcontrol/_test/unit/test_beta_signal_goldens.py

A regeneration is a statement that the output was MEANT to change -- never
regenerate to silence a mismatch you cannot explain. Comparison is the Galvo
goldens' (exact for counts, names, shapes and dtypes; ULP-tolerant for
computed doubles, for the reason given there).
"""
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.model.signaldesigners.AdvancedScanTTLCycleDesigner import (
    AdvancedScanTTLCycleDesigner,
)
from imswitch.imcontrol.model.signaldesigners.BetaScanDesigner import (
    BetaScanDesigner,
)
from .test_galvo_signal_goldens import _assert_matches_golden

GOLDEN_DIR = Path(__file__).parent / "data" / "beta_goldens"
SAMPLE_RATE = 100000

INFO_SCALARS = [
    "scan_samples_total", "scan_samples_d2_period", "n_pixels_fast",
    "samples_per_pixel", "dwell_time", "scan_time_step", "n_linesteps",
    "return_time", "tot_scan_time_s",
]
INFO_LISTS = [
    "img_dims", "pixel_sizes", "scan_samples", "minmaxes", "smooth_axes",
    "axis_names", "img_axes_phys", "positions",
]


def _setup():
    """A sample stage: three axes with their own conversion factors."""
    def positioner(conv):
        return SimpleNamespace(
            forScanning=True,
            managerProperties={"conversionFactor": conv, "minVolt": -10, "maxVolt": 10},
        )

    return SimpleNamespace(
        scan=SimpleNamespace(sampleRate=SAMPLE_RATE, lineClockLine=None,
                             frameClockLine=None, frameStartClockLine=None,
                             frameEndClockLine=None),
        positioners={"StageX": positioner(10.0), "StageY": positioner(5.0),
                     "StageZ": positioner(2.0)},
    )


def _params(lengths, steps, centers=(0.0, 0.0, 0.0), *, snapshot=(50.0, 40.0, 30.0),
            dwell=5e-3, return_time=10e-3, n_linesteps=None, **extra):
    """Analog parameters the way the Advanced controller hands them over:
    fast, middle, slow, with the position snapshot it takes before a scan."""
    p = {
        "target_device": ["StageX", "StageY", "StageZ"],
        "axis_length": list(lengths),
        "axis_step_size": list(steps),
        "axis_centerpos": list(centers),
        "axis_startpos": [[0.0], [0.0], [0.0]],
        "sequence_time": dwell,
        "return_time": return_time,
        "phase_delay": 0.0,
        "d3step_delay": 0.0,
    }
    if snapshot is not None:
        p["axis_position_before_scan"] = [[value] for value in snapshot]
    if n_linesteps is not None:
        p["n_linesteps"] = n_linesteps
    p.update(extra)
    return p


CASES = {
    # 2D (slow length 0: Z is not emitted), centred on the snapshot
    "xy_centred": _params([20.0, 10.0, 0.0], [5.0, 5.0, 1.0]),
    # three axes
    "xyz_centred": _params([20.0, 10.0, 10.0], [5.0, 5.0, 5.0]),
    # line steps: the middle axis holds through the repeats
    "xy_linesteps": _params([20.0, 10.0, 0.0], [5.0, 5.0, 1.0], n_linesteps=3),
    # the first pixel at snapshot + Center
    "xy_start_anchor": _params([20.0, 10.0, 0.0], [5.0, 5.0, 1.0], centers=(2.0, -3.0, 0.0),
                               position_anchor="start"),
    # no snapshot: the legacy absolute Center
    "xy_absolute": _params([20.0, 10.0, 0.0], [5.0, 5.0, 1.0], centers=(50.0, 40.0, 0.0),
                           snapshot=None),
    # dwell and return that are not whole samples: both rounded up
    "xy_fractional_samples": _params([20.0, 10.0, 0.0], [5.0, 5.0, 1.0],
                                     dwell=4.025e-3, return_time=10.005e-3),
    # move and settle other than the 2 ms defaults
    "xy_short_motion": _params([20.0, 10.0, 0.0], [5.0, 5.0, 1.0], dwell=2e-3,
                               move_time=0.5e-3, settle_time=0.5e-3),
    # a single line
    "x_single_line": _params([20.0, 0.0, 0.0], [5.0, 1.0, 1.0]),
    # no return time: no return ramps at all
    "xy_no_return": _params([20.0, 10.0, 0.0], [5.0, 5.0, 1.0], return_time=0.0),
    # negative centres, a length that is not a whole number of steps
    "xyz_offsets": _params([21.0, 10.0, 6.0], [5.0, 2.5, 3.0], centers=(-4.0, 3.5, -1.0),
                           n_linesteps=2),
}


def _run_beta(params):
    return BetaScanDesigner().make_signal(params, _setup())


def _flatten_beta(sig, positions, info):
    out = {"__devices__": np.array(list(sig.keys()))}
    for device, signal in sig.items():
        out[f"sig::{device}"] = np.asarray(signal)
    out["positions"] = np.asarray(positions)
    for key in INFO_SCALARS + INFO_LISTS:
        out[f"info::{key}"] = np.asarray(info[key])
    return out


#: TTL programs on Beta scans: whole-pixel gating, intra-pixel windows (on and
#: off the sample grid), and line steps with their own enables and windows.
TTL_CASES = {
    "ttl_whole_pixel": ("xy_centred", {
        "target_device": ["L488", "Camera"], "n_linesteps": 1,
        "linestep_enable": {"L488": [True], "Camera": [True]},
        "pulse_starts_s": {"L488": [[]], "Camera": [[]]},
        "pulse_ends_s": {"L488": [[]], "Camera": [[]]},
        "advanced_mode": False,
    }),
    "ttl_windows": ("xyz_centred", {
        "target_device": ["L488", "Camera"], "n_linesteps": 1,
        "linestep_enable": {"L488": [True], "Camera": [True]},
        "pulse_starts_s": {"L488": [[0.5e-3]], "Camera": [[0.2e-3]]},
        "pulse_ends_s": {"L488": [[0.9e-3]], "Camera": [[0.95e-3]]},
        "advanced_mode": True,
    }),
    "ttl_off_grid_windows": ("xy_centred", {
        "target_device": ["L488"], "n_linesteps": 1,
        "linestep_enable": {"L488": [True]},
        "pulse_starts_s": {"L488": [[0.123456e-3, 1.5e-3]]},
        "pulse_ends_s": {"L488": [[0.987654e-3, 2.25e-3]]},
        "advanced_mode": True,
    }),
    "ttl_linesteps": ("xy_linesteps", {
        "target_device": ["L405", "L488", "Camera"], "n_linesteps": 3,
        "linestep_enable": {"L405": [True, False, True], "L488": [False, True, True],
                            "Camera": [True, True, True]},
        "pulse_starts_s": {"L405": [[0.1e-3], [], [0.3e-3]], "L488": [[], [0.2e-3], [0.4e-3]],
                           "Camera": [[0.0], [0.0], [0.0]]},
        "pulse_ends_s": {"L405": [[0.6e-3], [], [0.8e-3]], "L488": [[], [0.7e-3], [0.9e-3]],
                         "Camera": [[1.0e-3], [1.0e-3], [1.0e-3]]},
        "advanced_mode": True,
    }),
}


def _run_ttl(case):
    scanCase, digital = TTL_CASES[case]
    params = CASES[scanCase]
    _, _, info = _run_beta(params)
    digital = dict(digital, sequence_time=params["sequence_time"])
    signals, _ = AdvancedScanTTLCycleDesigner().make_signal(digital, _setup(), info)
    return {"__devices__": np.array(sorted(signals)),
            **{f"ttl::{name}": np.asarray(signals[name]) for name in sorted(signals)}}


def _check(flat, name):
    path = GOLDEN_DIR / f"{name}.npz"
    if os.environ.get("BETA_GOLDEN_REGEN") == "1":
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(path, **flat)
        pytest.skip(f"regenerated golden {path.name}")
    assert path.exists(), (
        f"golden baseline {path} missing -- run with BETA_GOLDEN_REGEN=1 "
        f"on the commit the baseline should represent"
    )
    with np.load(path, allow_pickle=False) as stored:
        assert set(stored.files) == set(flat), (
            f"{name}: key set changed: only-stored={sorted(set(stored.files) - set(flat))} "
            f"only-new={sorted(set(flat) - set(stored.files))}"
        )
        for key in sorted(flat):
            _assert_matches_golden(f"{name}/{key}", flat[key], stored[key])


@pytest.mark.parametrize("case", sorted(CASES))
def test_beta_golden(case):
    _check(_flatten_beta(*_run_beta(CASES[case])), case)


@pytest.mark.parametrize("case", sorted(TTL_CASES))
def test_advanced_ttl_golden_on_beta_scans(case):
    _check(_run_ttl(case), case)


def test_the_cases_cover_what_the_plan_names():
    """The parameter space M0 must reproduce (plan §2.7): centred and
    absolute, 1 to 3 axes, return times, move and settle."""
    names = set(CASES)
    assert {"xy_absolute", "xy_centred", "xyz_centred", "x_single_line",
            "xy_no_return", "xy_short_motion", "xy_fractional_samples",
            "xy_linesteps", "xy_start_anchor"} <= names


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
