"""Bead FWHM: beads found by threshold, fitted with 2D Gaussians, FWHM and SNR per plane.

Synthetic beads are Gaussians of known sigma on a constant background with
Poisson noise, so the expected FWHM is ``2.3548 * sigma`` and a through-focus
stack has its narrowest, brightest beads in the focal plane.
"""

from __future__ import annotations

import math
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.analysis.bead_fwhm import (  # noqa: E402
    STATUS_CROWDED,
    STATUS_EDGE,
    STATUS_TOO_NARROW,
    BeadParams,
    analyse_plane,
    robust_noise,
)
from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402
from imswitch.improcess.processors import BeadFwhmProcessor  # noqa: E402
from imswitch.improcess.processors.bead_fwhm.result import fwhm_unit, plane_label  # noqa: E402

FWHM = 2.0 * math.sqrt(2.0 * math.log(2.0))
GRID = [(y, x) for y in (16.3, 36.0, 55.7) for x in (15.6, 35.2, 54.9)]


def _beads(shape=(72, 72), centres=GRID, sigma=1.5, amplitude=1000.0, background=100.0,
           seed=0, noise=True):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]]
    image = np.full(shape, background, dtype=np.float64)
    for cy, cx in centres:
        image += amplitude * np.exp(-((yy - cy) ** 2 + (xx - cx) ** 2) / (2.0 * sigma ** 2))
    return rng.poisson(image).astype(np.float64) if noise else image


def _run(data, labels, scales=None, unit="px", **params):
    result = ArrayProcessingResult("beads", data, labels, axis_scales=scales, scale_unit=unit)
    return BeadFwhmProcessor().apply(result, params)


# ---------------------------------------------------------------- one plane
def test_known_beads_give_their_fwhm():
    result = _run(_beads().astype(np.uint16), ["Y", "X"])

    assert result.count == 9
    fwhm = result.properties["fwhm_px"]
    np.testing.assert_allclose(fwhm, 1.5 * FWHM, rtol=0.04)
    np.testing.assert_allclose(result.properties["fwhm_x_px"], 1.5 * FWHM, rtol=0.06)
    np.testing.assert_allclose(result.properties["fwhm_y_px"], 1.5 * FWHM, rtol=0.06)
    # Fitted centres are sub-pixel, not the detection pixel.
    found = sorted(map(tuple, np.round(result.coordinates, 1)))
    expected = sorted(GRID)
    np.testing.assert_allclose(found, expected, atol=0.15)
    # Peak 1000 over Poisson noise of ~sqrt(100..1100): SNR in the tens.
    assert np.all(result.properties["snr"] > 20)
    assert np.all(result.properties["r2"] > 0.95)
    assert result.summary()["beads"] == 9


def test_a_single_plane_has_a_table_and_the_image_but_no_graphs():
    result = _run(_beads(), ["Y", "X"])

    assert result.kind == BeadFwhmProcessor.output_kind == "table"
    assert result.plot_payloads() == []
    layers = result.display_layers()
    assert [layer.component for layer in layers] == ["image", "beads"]
    assert layers[0].role == "context"
    assert layers[1].layer_kwargs["symbol"] == "square"
    assert layers[1].layer_kwargs["size"] == 11.0
    rows = result.table_records()
    assert len(rows) == 9 and {"Y", "X", "plane", "fwhm_px", "snr", "r2"} <= set(rows[0])


def test_calibrated_micrometres_are_reported_in_nanometres():
    result = _run(_beads(), ["Y", "X"], scales=[0.05, 0.05], unit="um")

    assert result.fwhm_unit == "nm"
    np.testing.assert_allclose(result.properties["fwhm_nm"], 1.5 * FWHM * 50.0, rtol=0.04)
    assert result.summary()["fwhm_unit"] == "nm"


def test_isolation_edges_and_hot_pixels_are_rejected_with_their_reason():
    image = _beads(centres=[(36.0, 36.0), (14.0, 14.0), (14.0, 18.0), (36.0, 2.0)])
    image[60, 60] += 3000.0  # one hot pixel
    params = BeadParams()
    fits, rejected, _level = analyse_plane(image, params)

    assert [(round(fit.y), round(fit.x)) for fit in fits] == [(36, 36)]

    def reason_near(y, x):
        near = [item.reason for item in rejected if math.hypot(item.y - y, item.x - x) <= 1.5]
        assert len(near) == 1, (y, x, rejected)
        return near[0]

    # Each of the pair is found (as its own maximum) and both are refused.
    assert reason_near(14, 14) == reason_near(14, 18) == STATUS_CROWDED
    assert reason_near(36, 2) == STATUS_EDGE
    assert reason_near(60, 60) == STATUS_TOO_NARROW

    result = _run(image, ["Y", "X"])
    rejected_layer = [layer for layer in result.display_layers() if layer.component == "rejected"]
    assert len(rejected_layer) == 1 and len(rejected_layer[0].data) == 4
    assert result.summary()["rejected"] == {STATUS_EDGE: 1, STATUS_CROWDED: 2, STATUS_TOO_NARROW: 1}


def test_an_absolute_threshold_keeps_only_beads_brighter_than_it():
    bright = _beads(centres=GRID[:4], amplitude=2000.0, noise=False)
    dim = _beads(centres=GRID[4:], amplitude=300.0, background=0.0, noise=False)
    image = np.random.default_rng(1).poisson(bright + dim).astype(np.float64)

    assert _run(image, ["Y", "X"], threshold=10.0).count == 9
    assert _run(image, ["Y", "X"], threshold=1200.0, threshold_mode="absolute").count == 4


def test_noise_alone_finds_nothing_and_shows_only_the_image():
    image = np.random.default_rng(2).poisson(100.0, (64, 64)).astype(np.float64)
    result = _run(image, ["Y", "X"])

    assert result.count == 0
    assert [layer.component for layer in result.display_layers()] == ["image"]
    assert result.table_records() == []


def test_a_background_of_zeros_still_has_a_noise_estimate():
    values = np.zeros(1000)
    values[:20] = 5.0
    median, sigma = robust_noise(values)
    assert median == 0.0 and sigma > 0


# -------------------------------------------------------------------- stacks
def _through_focus(n=7, focus=3, sigma0=1.2, step=0.35):
    """A Z stack whose beads blur away from ``focus``, conserving their photons."""
    planes, sigmas = [], []
    for z in range(n):
        sigma = sigma0 + step * abs(z - focus)
        amplitude = 1500.0 * (sigma0 / sigma) ** 2
        planes.append(_beads(sigma=sigma, amplitude=amplitude, seed=z))
        sigmas.append(sigma)
    return np.stack(planes), np.array(sigmas)


def test_a_z_stack_gives_mean_fwhm_and_snr_per_slice():
    stack, sigmas = _through_focus()
    result = _run(stack, ["Z", "Y", "X"], window=15)

    payloads = result.plot_payloads()
    assert [payload.title for payload in payloads] == [
        "Mean bead FWHM vs Z slice", "Mean bead SNR vs Z slice",
    ]
    fwhm, snr = (payload.series[0] for payload in payloads)
    np.testing.assert_array_equal(fwhm.x, np.arange(7))
    assert payloads[0].x_label == "Z slice" and payloads[0].y_label == "FWHM (px)"
    np.testing.assert_allclose(fwhm.y, sigmas * FWHM, rtol=0.05)
    assert int(np.argmin(fwhm.y)) == 3 and int(np.argmax(snr.y)) == 3
    assert np.all(np.diff(snr.y[3:]) < 0) and np.all(np.diff(snr.y[:4]) > 0)

    # Every bead is a row on its own slice, at its own Z.
    assert result.axis_names == ["Z", "Y", "X"]
    np.testing.assert_array_equal(result.coordinates[:, 0], result.properties["plane"])
    assert np.bincount(result.properties["plane"]).tolist() == [9] * 7


def test_a_slice_without_beads_is_a_gap_in_the_lines():
    stack, _sigmas = _through_focus(n=3, focus=1)
    stack[2] = np.random.default_rng(5).poisson(100.0, stack[2].shape)
    result = _run(stack, ["Z", "Y", "X"], window=15)

    fwhm = result.plot_payloads()[0].series[0].y
    assert np.isfinite(fwhm[:2]).all() and np.isnan(fwhm[2])
    assert result.plot_payloads()[0].metadata["planes_with_beads"] == 2


def test_every_leading_axis_is_a_plane():
    stack, _sigmas = _through_focus(n=3, focus=1)
    data = np.stack([stack, stack])  # T, Z, Y, X
    result = _run(data, ["T", "Z", "Y", "X"], window=15)

    assert result.plot_payloads()[0].x_label == "Plane (T × Z)"
    assert result.analysis.n_planes == 6
    assert result.axis_names == ["T", "Z", "Y", "X"]
    last = result.coordinates[result.properties["plane"] == 5]
    assert np.all(last[:, 0] == 1) and np.all(last[:, 1] == 2)


@pytest.mark.parametrize(
    "unit, scales, expected",
    [("px", (1.0, 1.0), ("px", 1.0, 1.0)), ("um", (0.1, 0.05), ("nm", 100.0, 50.0)),
     ("nm", (20.0, 20.0), ("nm", 20.0, 20.0)), ("", (2.0, 2.0), ("px", 1.0, 1.0))],
)
def test_fwhm_unit(unit, scales, expected):
    assert fwhm_unit(unit, *scales) == expected


def test_plane_labels():
    assert plane_label([]) == "Plane"
    assert plane_label(["T"]) == "T slice"
    assert plane_label(["T", "Z"]) == "Plane (T × Z)"


@pytest.mark.parametrize(
    "params, message",
    [({"window": 3}, "at least 5"), ({"threshold_mode": "fraction"}, "threshold_mode"),
     ({"min_r2": 1.5}, "between 0 and 1"), ({"sigma": -1}, "negative")],
)
def test_bad_settings_are_refused(params, message):
    with pytest.raises(ValueError, match=message):
        BeadParams.from_params(params)
