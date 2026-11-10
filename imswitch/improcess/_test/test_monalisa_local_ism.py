"""Local Gaussian reassignment contract and regression tests.

These tests deliberately use camera-pixel samples, not interpolated detector
values. They verify that ISM consumes the existing Local extraction result.
"""

import numpy as np
import pytest

from imswitch.improcess.reconstructors.monalisa.calibration import PatternLocalization
from imswitch.improcess.reconstructors.monalisa.lattice import Lattice
from imswitch.improcess.reconstructors.monalisa.local_extraction import LocalExtractionOperator
from imswitch.improcess.reconstructors.monalisa.pipeline import (
    PipelineParams, _shift_factor_to_use, prepare_geometry, reconstruct_stack,
)
from imswitch.improcess.reconstructors.monalisa.placement import (
    OutputRaster, place_nearest, sample_positions,
)
from imswitch.improcess.reconstructors.monalisa.reassignment import reassign


@pytest.mark.parametrize("shape,size,radius", [
    ("rectangular", 5, 2.0), ("circular", 3, 2.5),
])
@pytest.mark.parametrize("background", ["none", "constant"])
def test_zero_shift_pixel_normal_equation(shape, size, radius, background):
    """Splatting Local's corrected pixels at alpha=0 recovers its fit."""
    x = np.array([2.3, 10.2, 18.5, 26.6])
    y = np.array([3.4, 10.9, 19.1, 26.2])
    operator = LocalExtractionOperator.build(
        x, y, 1.3, (30, 30), background=background,
        footprint_shape=shape, footprint_size=size,
        pinhole_radius_px=radius, sampling="camera_pixels",
    )
    frames = np.random.default_rng(12).normal(25, 3, (3, 30, 30))
    fit = operator.apply_device(frames, device="CPU")
    entries = operator.footprints
    values = operator.footprint_values(frames, extracted=fit)
    assert values.shape == (3, entries.focus.size)
    for focus in np.flatnonzero(operator.valid):
        pick = entries.focus == focus
        expected = np.sum(values[:, pick] * entries.weight[pick], axis=1) / np.sum(
            entries.weight[pick] ** 2
        )
        np.testing.assert_allclose(expected, fit.amplitude[:, focus], atol=1e-8, rtol=1e-10)
    assert np.all(entries.row == np.arange(entries.row.size))
    assert np.all(np.isfinite(entries.weight))


def test_zero_shift_reassignment_matches_direct_local_placement():
    x, y = np.array([7., 18., 7., 18.]), np.array([7., 7., 18., 18.])
    operator = LocalExtractionOperator.build(
        x, y, 1.3, (28, 28), footprint_shape="rectangular",
        footprint_size=5, sampling="camera_pixels", background="constant",
    )
    rng = np.random.default_rng(9)
    frames = rng.normal(30, 2, (3, 28, 28))
    offsets = np.array([[0., 0.], [1., 0.], [0., 1.]])
    extracted = operator.apply_device(frames, device="CPU")
    raster = OutputRaster.covering((28, 28), np.eye(2), (7., 7.))
    qx, qy = sample_positions(x, y, offsets[:, 0], offsets[:, 1])
    gx, gy = raster.coordinates(qx, qy)
    direct = place_nearest(
        gx[:, operator.valid], gy[:, operator.valid],
        extracted.amplitude[:, operator.valid], raster.shape,
    ).image
    ism = reassign(frames, operator, x, y, offsets, raster, alpha=0., extracted=extracted).image
    valid = np.isfinite(direct) & np.isfinite(ism)
    assert valid.sum() >= 8
    np.testing.assert_allclose(ism[valid], direct[valid], atol=1e-8, rtol=1e-10)


def test_nonzero_reassignment_reuses_existing_fit(monkeypatch):
    x, y = np.array([7., 18.]), np.array([7., 18.])
    op = LocalExtractionOperator.build(
        x, y, 1.3, (28, 28), footprint_shape="circular",
        pinhole_radius_px=2.5, sampling="camera_pixels",
    )
    rng = np.random.default_rng(43)
    frames = rng.normal(30, 2, (3, 28, 28))
    fitted = op.apply_device(frames, device="CPU")
    def forbidden(*_args, **_kwargs):
        raise AssertionError("unexpected second Local Gaussian solve")
    monkeypatch.setattr(op, "apply_device", forbidden)
    offsets = np.array([[0., 0.], [1., 0.], [0., 1.]])
    raster = OutputRaster.covering((28, 28), np.eye(2), (7., 7.))
    zero = reassign(frames, op, x, y, offsets, raster, 0., extracted=fitted).image
    shifted = reassign(frames, op, x, y, offsets, raster, 0.4, extracted=fitted).image
    assert np.isfinite(shifted).any()
    assert np.nanmax(np.abs(np.nan_to_num(shifted) - np.nan_to_num(zero))) > 1e-3


def test_local_ism_pipeline_and_scan_axis_mask():
    lattice = Lattice.rectangular(10., 10., xo=5., yo=5.)
    localization = PatternLocalization.from_lattice(lattice)
    index = np.column_stack([np.zeros(5), np.arange(5)])  # YZ: Y scanned only
    params = PipelineParams(
        estimator="local", local_sampling="camera_pixels", background="constant",
        footprint_shape="square", footprint_mode="fixed", footprint_size_px=5.,
        spot_sigma_px=1.2, reassignment=0.35, frame_gain="off",
        cell_offsets="off", variance_weighting=False, device="CPU",
    )
    geometry = prepare_geometry(
        np.ones((44, 44)) * 5., np.diag([10., 2.]), index, params,
        localization=localization,
    )
    assert isinstance(geometry.operator, LocalExtractionOperator)
    assert _shift_factor_to_use(geometry, index) == (0., 0.35)
    frames = np.random.default_rng(3).normal(10, 1, (len(index), 44, 44))
    result = reconstruct_stack(frames, geometry, index)
    assert result.amplitude.method == "reassigned"
    assert np.isfinite(result.amplitude.image).any()


def test_bilinear_sampling_rejected_for_local_ism():
    lattice = Lattice.rectangular(10., 10., xo=5., yo=5.)
    params = PipelineParams(
        estimator="local", local_sampling="bilinear",
        spot_sigma_px=1.2, reassignment=0.35,
        frame_gain="off", cell_offsets="off", variance_weighting=False,
    )
    with pytest.raises(ValueError, match="original camera-pixel sampling"):
        prepare_geometry(
            np.ones((44, 44)), np.eye(2), np.array([[0, 0], [1, 0]]),
            params, localization=PatternLocalization.from_lattice(lattice),
        )


def test_lattice_unit_cells_use_nonoverlapping_camera_pixels():
    lattice = Lattice.rectangular(8., 8., xo=4., yo=4.)
    centers = np.array([4., 12., 20.])
    x, y = np.meshgrid(centers, centers, indexing="xy")
    operator = LocalExtractionOperator.build(
        x.ravel(), y.ravel(), 1.4, (25, 25),
        background="constant", footprint_shape="lattice",
        sampling="camera_pixels", lattice=lattice,
    )
    entries = operator.footprints
    assert entries.focus.size > 0
    assert np.unique(operator._ism_pixel_index).size == entries.focus.size
    rng = np.random.default_rng(11)
    frames = rng.normal(15, 2, (2, 25, 25))
    extracted = operator.apply_device(frames, device="CPU")
    values = operator.footprint_values(frames, extracted=extracted)
    for i in np.flatnonzero(operator.valid):
        selected = entries.focus == i
        expected = (values[:, selected] @ entries.weight[selected]) / np.sum(
            entries.weight[selected] ** 2
        )
        np.testing.assert_allclose(expected, extracted.amplitude[:, i], atol=1e-8)


def test_integrated_local_ism_preserves_amplitude_units():
    operator = LocalExtractionOperator.build(
        np.array([12.2]), np.array([12.4]), 1.5, (25, 25),
        background="constant", footprint_shape="rectangular", footprint_size=7,
        sampling="camera_pixels", integrated=True,
    )
    frames = np.random.default_rng(2).normal(20, 1, (3, 25, 25))
    extracted = operator.apply_device(frames, device="CPU")
    entries = operator.footprints
    values = operator.footprint_values(frames, extracted=extracted)
    recovered = (values @ entries.weight) / np.sum(entries.weight**2)
    np.testing.assert_allclose(recovered, extracted.amplitude[:, 0], atol=1e-8)
