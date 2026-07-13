"""Regression tests for the scan pixel-count convention.

``pixels = axis_length / axis_step_size`` must use ONE rounding rule
(``round``, min 1) everywhere: the widget "Pixels (#)" display, the digital
``Nx``/``Ny``, ``getDimsScan()`` (recorded OME dims), and the scan signal
designers that decide how many lines actually get scanned.

Historically these drifted apart -- the widget used ``round``, ``getDimsScan``
and ``GalvoScanDesigner.img_dims`` used ``int`` (truncate), while
``BetaScanDesigner`` and ``GalvoScanDesigner``'s returned ``positions`` used
``ceil``. For a non-divisible ratio (e.g. 10 um / 3 um) the GUI showed 3, the
Beta scan ran 4 lines, and the recording was labeled 3. See
``docs`` / memory ``scan-pixelcount-convention-bug``.
"""
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.model.scan_parameters import (
    AdvancedScanParameterSerializer,
    axis_pixel_positions,
    pixels_for_length_step,
)
from imswitch.imcontrol.model.signaldesigners.BetaScanDesigner import (
    BetaScanDesigner,
)
from imswitch.imcontrol.model.signaldesigners.GalvoScanDesigner import (
    GalvoScanDesigner,
)


@pytest.mark.parametrize(
    "length, step, expected",
    [
        (10, 3, 3),     # 3.33 -> round down (was ceil=4)
        (100, 3, 33),   # 33.33 -> round down (was ceil=34)
        (10, 4, 2),     # 2.5 -> banker's round to 2
        (9.9, 1.0, 10), # 9.9 -> round up (was truncate=9)
        (30, 0.5, 60),  # exact -> unchanged
        (0, 1, 1),      # zero-length axis -> min 1
        (5, 0, 1),      # zero step (inactive) -> min 1
        (0.4, 1.0, 1),  # rounds to 0 -> clamped to min 1
    ],
)
def test_pixels_for_length_step(length, step, expected):
    assert pixels_for_length_step(length, step) == expected


def _beta_setup(sample_rate=1000, conv=1.0):
    positioners = {
        axis: SimpleNamespace(
            forScanning=True,
            managerProperties={"conversionFactor": conv},
        )
        for axis in ("X", "Y", "Z")
    }
    return SimpleNamespace(
        scan=SimpleNamespace(sampleRate=sample_rate),
        positioners=positioners,
    )


def test_beta_designer_uses_round_and_matches_display():
    """Non-divisible ratio: Beta positions == img_dims == GUI count == round."""
    length, step = 10, 3
    params = {
        "target_device": ["X", "Y", "Z"],
        "axis_length": [length, 1, 0],
        "axis_step_size": [step, 1, 1],
        "axis_startpos": [[0], [0], [0]],
        "axis_centerpos": [0, 0, 0],
        "return_time": 0.001,
        "sequence_time": 0.002,
        "n_linesteps": 1,
    }
    _signals, positions, scan_info = BetaScanDesigner().make_signal(
        params, _beta_setup()
    )

    expected = pixels_for_length_step(length, step)  # 3, not ceil=4
    assert expected == 3
    # real scanned lines (designer) agree with the canonical count...
    assert positions[0] == expected
    assert scan_info["img_dims"][0] == expected
    # ...and with the widget "Pixels (#)" / Nx-Ny display path.
    serializer = AdvancedScanParameterSerializer()
    analog = {
        "target_device": ["X", "Y", "Z"],
        "axis_length": [length, 1, 0],
        "axis_step_size": [step, 1, 1],
    }
    assert serializer.pixels_for_scan_device(analog, "X") == expected


def test_galvo_active_axis_indices_use_round():
    """GalvoScanDesigner axis activation uses round(len/step) > 1, not ceil.

    ``_active_axis_indices`` decides which axes count as scan axes and feeds
    both ``n_steps_dx`` (img_dims / waveform) and the returned ``positions`` --
    all now share ``pixels_for_length_step`` (round). Under the old ceil rule an
    axis with 1 < len/step <= 1.5 was wrongly treated as a 2-pixel scan axis.
    """
    # X: 10/0.6 = 16.67 -> 17 (active). Y: 1.2/1.0 -> round 1 (inactive; was
    # ceil 2 = active). Z: 0.4/1.0 -> round 0 -> clamp 1 (inactive).
    active = GalvoScanDesigner._active_axis_indices(
        [10, 1.2, 0.4], [0.6, 1.0, 1.0], 3
    )
    assert active == [0]


# --- realized pixel spacing == requested step (convention A) ---------------


def test_axis_pixel_positions_spacing_equals_step():
    # start-anchored: first pixel at start, spacing exactly step
    pos = axis_pixel_positions(4, 3.0, start=2.0)
    assert list(pos) == [2.0, 5.0, 8.0, 11.0]
    # centered: symmetric about center, spacing exactly step, span (n-1)*step
    posc = axis_pixel_positions(5, 2.0, center=10.0)
    assert list(posc) == [6.0, 8.0, 10.0, 12.0, 14.0]
    # single pixel degenerates to the anchor
    assert list(axis_pixel_positions(1, 3.0, start=7.0)) == [7.0]
    assert list(axis_pixel_positions(1, 3.0, center=7.0)) == [7.0]


def _beta_fast_axis_spacing(length, step, seq_time=0.01, sample_rate=10000):
    # dwell (spp) must exceed Beta's fixed 0.002 s end-of-pixel smoothing window
    # so the plateau at the start of each dwell is the clean held pixel position.
    params = {
        "target_device": ["X", "Y", "Z"],
        "axis_length": [length, 1, 0],
        "axis_step_size": [step, 1, 1],
        "axis_startpos": [[0], [0], [0]],
        "axis_centerpos": [0, 0, 0],
        "return_time": 0.001,
        "sequence_time": seq_time,
        "n_linesteps": 1,
    }
    signals, positions, _info = BetaScanDesigner().make_signal(params, _beta_setup(sample_rate))
    N = positions[0]
    spp = int(np.ceil(seq_time * sample_rate))
    fast = np.asarray(signals["X"], dtype=float)
    held = np.array([fast[s * spp] for s in range(N)])  # start of each dwell
    return N, np.diff(held)


@pytest.mark.parametrize("length, step", [(10, 1), (10, 3), (20, 0.7), (9, 2)])
def test_beta_realized_spacing_equals_requested_step(length, step):
    """The Beta scan must visit pixels exactly `step` apart (convFactor 1), so
    the reported pixel_sizes / OME PhysicalSize are truthful. Previously the
    ramp spanned the full ROI -> pitch length/(N-1) (up to 100% wrong)."""
    N, diffs = _beta_fast_axis_spacing(length, step)
    assert N == pixels_for_length_step(length, step)
    assert np.allclose(diffs, step, atol=1e-9)


# --- Center moves a Beta (stage) scan (was inert; see scan-beta-center-ignored) ---


def _beta_x_range(center, *, length=10, step=2, conv=1.0):
    params = {
        "target_device": ["X", "Y", "Z"],
        "axis_length": [length, 1, 0], "axis_step_size": [step, 1, 1],
        "axis_startpos": [[center], [center], [center]],
        "axis_centerpos": [center, center, center],
        "return_time": 0.001, "sequence_time": 0.002, "n_linesteps": 1,
    }
    sig, positions, _info = BetaScanDesigner().make_signal(params, _beta_setup(conv=conv))
    x = np.asarray(sig["X"], dtype=float)
    return positions[0], float(x.min()), float(x.max())


def test_beta_scan_is_centered_on_center():
    """Center now moves the scan: N=5 pixels pitch 2 um span (N-1)*2=8 um,
    centered on center. center=0 -> [-4, 4]; center=20 -> [16, 24]."""
    N, lo0, hi0 = _beta_x_range(0.0)
    assert N == 5
    assert np.isclose(lo0, -4.0) and np.isclose(hi0, 4.0)
    _N, lo20, hi20 = _beta_x_range(20.0)
    assert np.isclose(lo20, 16.0) and np.isclose(hi20, 24.0)


def test_beta_centered_scan_stays_in_voltage_range():
    """MoNaLISA-like piezo (conv 1.75, 0-10 V = 0-17.5 um): a 5 um scan centered
    mid-travel keeps the waveform within [minVolt, maxVolt] and centered near
    center/conv V -- confirming the Center fix is physically representable."""
    conv, min_v, max_v, center_um = 1.75, 0.0, 10.0, 8.75
    _N, lo, hi = _beta_x_range(center_um, length=5, step=0.5, conv=conv)
    assert min_v <= lo and hi <= max_v
    assert np.isclose((lo + hi) / 2, center_um / conv, atol=0.1)

