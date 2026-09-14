"""Contract tests for the optional napari-storm viewer boundary.

These lock in what was agreed with the napari-storm side (see their
``docs/imswitch-integration.md``): a ``LocalizationResult``'s canonical
nanometre recarray is handed to a ``LocalizationTable`` *as it stands*, with
the column names declared rather than converted.

Everything here is planner-side, so it runs headlessly with no GL context and
no viewer — which is the whole reason the boundary was drawn where it was.
The tests skip entirely when the optional dependency is absent, exactly as the
adapter does at runtime.
"""

from __future__ import annotations

import subprocess
import sys

import numpy as np
import pytest

from imswitch.improcess.model.localization_result import LocalizationResult
from imswitch.improcess.model.localization_schema import (
    napari_storm_table_kwargs,
    localizations_from_columns,
    to_napari_storm_recarray,
)

storm_core = pytest.importorskip(
    "napari_storm.core",
    reason="optional 'storm' extra not installed",
)

PIXEL_SIZE_NM = 100.0


def _result(count=2000, *, with_z=True, dead_rows=0, pixel_size_nm=PIXEL_SIZE_NM):
    """A LocalizationResult with realistic fitted PSF widths, in nm."""
    rng = np.random.default_rng(11)
    columns = {
        "frame": np.arange(count),
        "x_nm": rng.uniform(0, 51200, count),
        "y_nm": rng.uniform(0, 51200, count),
        "sigma_x_nm": rng.uniform(110, 170, count),
        "sigma_y_nm": rng.uniform(110, 170, count),
        "photons": rng.uniform(500, 5000, count),
    }
    if with_z:
        columns["z_nm"] = rng.uniform(-400, 400, count)
        columns["sigma_z_nm"] = rng.uniform(250, 350, count)
    if dead_rows:
        # Failed fits: a real detector artefact, and the case that used to
        # take over the whole intensity range.
        for name in ("sigma_x_nm", "sigma_y_nm", "sigma_z_nm"):
            if name in columns:
                columns[name][:dead_rows] = 0.0
    locs = localizations_from_columns(columns)
    return LocalizationResult(
        "test", locs, pixel_size_nm=pixel_size_nm, dims="3D" if with_z else "2D"
    )


def _traits(result, **overrides):
    fields = {
        "zdim_present": result.dims == "3D",
        "sigma_present": True,
        "photon_count_present": True,
        "pixel_size_nm": result.pixel_size_nm,
    }
    fields.update(overrides)
    return storm_core.DatasetTraits(**fields)


def _plan_nm(result, settings, traits=None):
    """The display path: our own recarray, declared rather than converted."""
    table = storm_core.LocalizationTable(
        result.locs, copy=False, **napari_storm_table_kwargs(result.locs)
    )
    return storm_core.RenderPlanner().plan(
        table, settings, traits or _traits(result), name=result.name
    )


def _plan_via_pixels(result, settings, traits=None):
    """The old export-shaped path, kept only as a cross-check."""
    table = storm_core.LocalizationTable(
        to_napari_storm_recarray(result.locs, result.pixel_size_nm),
        position_columns={
            "x": "x_pos_pixels",
            "y": "y_pos_pixels",
            "z": "z_pos_pixels",
        },
        position_scale_nm=result.pixel_size_nm,
    )
    return storm_core.RenderPlanner().plan(
        table, settings, traits or _traits(result), name=result.name
    )


@pytest.mark.parametrize("mode", [0, 1])
@pytest.mark.parametrize("with_z", [False, True])
def test_canonical_recarray_plans_without_conversion(mode, with_z):
    """Our nm table renders in both Gaussian modes, 2D and 3D."""
    result = _result(with_z=with_z)
    request = _plan_nm(result, storm_core.GaussianSettings(mode=mode))

    assert len(request.coords) == len(result.locs)
    assert request.coords.shape[1] == 3
    assert np.isfinite(request.coords).all()
    assert np.isfinite(request.values).all()
    assert request.size > 0


def test_table_reads_our_array_in_place():
    """``copy=False`` means the viewer shares our buffer, not a duplicate."""
    result = _result(count=64)
    table = storm_core.LocalizationTable(
        result.locs, copy=False, **napari_storm_table_kwargs(result.locs)
    )
    assert table.records.base is result.locs or table.records is result.locs
    assert table.has_axis("z")
    assert table.has_sigma_axis("z")
    assert table.has_photons()
    # Declared as nanometres: no scaling on the way through.
    assert table.sigma_scale_nm == 1.0
    np.testing.assert_allclose(
        table.active_coordinate_nm("x"), result.locs.x_nm, rtol=0, atol=0
    )


@pytest.mark.parametrize("mode", [0, 1])
def test_nm_path_matches_pixel_roundtrip(mode):
    """Declaring columns must agree with converting them.

    The pixel round trip is what we did before napari-storm 1.0 made sigma and
    photon columns declarable. It stays correct; it is just lossy and a copy.
    """
    result = _result(dead_rows=5)
    settings = storm_core.GaussianSettings(mode=mode)
    direct, converted = _plan_nm(result, settings), _plan_via_pixels(result, settings)

    np.testing.assert_allclose(direct.sigmas, converted.sigmas, rtol=1e-5, atol=1e-6)
    np.testing.assert_allclose(direct.values, converted.values, rtol=1e-5, atol=1e-6)
    assert direct.size == pytest.approx(converted.size, rel=1e-5)
    # Positions agree to f4 precision at a 51 um field; the direct path is the
    # exact one, since it never divides by the pixel size and multiplies back.
    np.testing.assert_allclose(direct.coords, converted.coords, rtol=0, atol=5e-3)


def test_failed_fits_do_not_dominate_the_intensity_range():
    """A handful of zero-width rows must not set the scale for everything else.

    Regression guard on the defect we reported upstream: repairing an unusable
    width to the *floor* made every failed fit the brightest row in the image,
    because the weight is 1/product. Widths here are ~30x the floor, which is
    where that behaviour was worst.
    """
    result = _result(count=5000, dead_rows=5)
    request = _plan_nm(result, storm_core.GaussianSettings(mode=1))

    assert np.isfinite(request.values).all()
    real = request.values[5:]
    occupancy = (real.max() - real.min()) / np.ptp(request.values)
    assert occupancy > 0.5, (
        f"real localizations occupy only {occupancy:.2%} of the intensity "
        f"range; failed fits are dominating it again"
    )


def test_two_dimensional_table_without_z_sigma_column():
    """A 2D result must plan even though it declares no axial width."""
    result = _result(with_z=False)
    flat = result.locs[
        ["frame", "x_nm", "y_nm", "sigma_x_nm", "sigma_y_nm", "photons"]
    ].copy()
    kwargs = dict(napari_storm_table_kwargs(flat))
    kwargs["position_columns"] = {"x": "x_nm", "y": "y_nm"}
    kwargs["sigma_columns"] = {"x": "sigma_x_nm", "y": "sigma_y_nm"}

    table = storm_core.LocalizationTable(flat, copy=False, **kwargs)
    request = storm_core.RenderPlanner().plan(
        table,
        storm_core.GaussianSettings(mode=1),
        _traits(result, zdim_present=False),
        name="2d",
    )
    assert len(request.coords) == len(flat)
    assert np.isfinite(request.values).all()


def test_declaring_an_unfitted_axial_width_is_refused():
    """Claiming a z width we never fitted must fail loudly, not render wrong.

    Our localizer leaves ``sigma_z_nm`` zero-filled, so ``sigma_present`` may
    only be declared together with ``zdim_present`` once 3D fitting is real.
    """
    result = _result(with_z=True)
    result.locs.sigma_z_nm[:] = 0.0

    with pytest.raises(storm_core.InvalidLocalizationData, match="sigma_z_nm"):
        _plan_nm(result, storm_core.GaussianSettings(mode=1))


def test_planning_needs_neither_napari_nor_qt():
    """The import we rely on for headless CI must stay host-free.

    napari-storm guarantees this with its own test; we assert it too, because
    if it ever regressed our test suite would start requiring a GL context
    without anything saying why.
    """
    code = (
        "import sys; import napari_storm.core;"
        "loaded=[m for m in ('napari','qtpy','vispy') if m in sys.modules];"
        "print(','.join(loaded))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", f"napari_storm.core pulled in {proc.stdout!r}"
