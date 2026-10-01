"""Phase 0 tests: localization precision, and keeping it apart from PSF width.

The two quantities used to share the ``sigma_*_nm`` column depending on where
the data came from. These lock in that they no longer do — in the schema, in
the localizer, and across a Picasso round trip.
"""

from __future__ import annotations

import numpy as np
import pytest

from imswitch.improcess.analysis.smlm_export import (
    export_picasso_hdf5,
    read_picasso_hdf5,
)
from imswitch.improcess.model.localization_result import LocalizationResult
from imswitch.improcess.model.localization_schema import (
    LOCALIZATION_COLUMNS,
    LOCALIZATION_DTYPE,
    as_localizations,
    localizations_from_columns,
    napari_storm_table_kwargs,
)
from imswitch.improcess.reconstructors.smlm.precision import (
    localization_precision_nm,
)


# -- the estimator ----------------------------------------------------------


def test_precision_matches_thompson_closed_form_without_background():
    """sqrt((s^2 + a^2/12)/N), computed by hand."""
    precision = localization_precision_nm(100.0, 1000.0, pixel_size_nm=100.0)
    expected = np.sqrt((100.0 ** 2 + 100.0 ** 2 / 12.0) / 1000.0)
    assert float(precision) == pytest.approx(expected, rel=1e-6)
    assert float(precision) == pytest.approx(3.2914, abs=1e-3)


def test_precision_includes_the_background_term():
    """The 8*pi*s^4*b^2/(a^2 N^2) term, computed by hand."""
    precision = localization_precision_nm(
        100.0, 1000.0, pixel_size_nm=100.0, background=10.0
    )
    shot = (100.0 ** 2 + 100.0 ** 2 / 12.0) / 1000.0
    background = 8.0 * np.pi * 100.0 ** 4 * 10.0 / (100.0 ** 2 * 1000.0 ** 2)
    assert float(precision) == pytest.approx(np.sqrt(shot + background), rel=1e-6)


def test_precision_improves_as_the_inverse_root_of_photons():
    few = localization_precision_nm(100.0, 1000.0, pixel_size_nm=100.0)
    many = localization_precision_nm(100.0, 4000.0, pixel_size_nm=100.0)
    # Shot-noise limited: 4x the photons is 2x the precision.
    assert float(few) / float(many) == pytest.approx(2.0, rel=1e-6)


def test_precision_is_zero_where_it_cannot_be_measured():
    """Unusable rows report 0 -- 'not measured', not 'infinitely precise'."""
    precision = localization_precision_nm(
        np.array([100.0, 100.0, 0.0, 100.0]),
        np.array([1000.0, 0.0, 1000.0, np.nan]),
        pixel_size_nm=100.0,
    )
    np.testing.assert_array_equal(precision[1:] == 0, [True, True, True])
    assert precision[0] > 0


def test_precision_rejects_a_nonsense_pixel_size():
    with pytest.raises(ValueError, match="pixel_size_nm"):
        localization_precision_nm(100.0, 1000.0, pixel_size_nm=0.0)


# -- the schema -------------------------------------------------------------


def test_precision_columns_are_canonical_and_distinct_from_width():
    for column in ("lp_x_nm", "lp_y_nm", "lp_z_nm"):
        assert column in LOCALIZATION_COLUMNS
        assert column in LOCALIZATION_DTYPE.names
    assert LOCALIZATION_COLUMNS.index("sigma_x_nm") != LOCALIZATION_COLUMNS.index(
        "lp_x_nm"
    )


def test_tables_saved_before_precision_existed_still_load():
    """Old files lack lp_* entirely; they must zero-fill, not raise."""
    legacy_dtype = np.dtype(
        [(name, LOCALIZATION_DTYPE[name])
         for name in LOCALIZATION_COLUMNS
         if not name.startswith("lp_")]
    )
    legacy = np.zeros(3, dtype=legacy_dtype)
    legacy["x_nm"] = [1.0, 2.0, 3.0]

    canonical = as_localizations(legacy)
    assert canonical.dtype == LOCALIZATION_DTYPE
    np.testing.assert_array_equal(canonical.x_nm, [1.0, 2.0, 3.0])
    np.testing.assert_array_equal(canonical.lp_x_nm, [0.0, 0.0, 0.0])


def test_a_missing_required_column_still_raises():
    """Relaxing lp_* must not have relaxed everything else."""
    partial = np.zeros(2, dtype=np.dtype([("x_nm", "f4"), ("lp_x_nm", "f4")]))
    with pytest.raises(KeyError, match="y_nm"):
        as_localizations(partial)


# -- what the viewer is told ------------------------------------------------


def _locs(count=8, *, precision):
    columns = {
        "x_nm": np.linspace(0, 1000, count),
        "y_nm": np.linspace(0, 1000, count),
        "sigma_x_nm": np.full(count, 140.0),
        "sigma_y_nm": np.full(count, 140.0),
        "photons": np.full(count, 1000.0),
    }
    if precision:
        columns["lp_x_nm"] = np.full(count, 12.0)
        columns["lp_y_nm"] = np.full(count, 12.0)
    return localizations_from_columns(columns)


def test_viewer_is_given_precision_when_it_exists():
    kwargs = napari_storm_table_kwargs(_locs(precision=True))
    assert kwargs["sigma_columns"]["x"] == "lp_x_nm"


def test_viewer_falls_back_to_psf_width_when_precision_is_absent():
    """A zero-filled precision column reads as 'not measured'."""
    kwargs = napari_storm_table_kwargs(_locs(precision=False))
    assert kwargs["sigma_columns"]["x"] == "sigma_x_nm"


# -- round trip -------------------------------------------------------------


def test_picasso_round_trip_keeps_width_and_precision_apart(tmp_path):
    """The defect this phase fixes: lpx used to be written from the width."""
    locs = _locs(count=16, precision=True)
    locs.sigma_x_nm[:] = 140.0
    locs.lp_x_nm[:] = 12.0
    result = LocalizationResult("rt", locs, pixel_size_nm=100.0, dims="2D")

    path = export_picasso_hdf5(result, tmp_path / "rt.hdf5")
    reloaded = read_picasso_hdf5(path)

    np.testing.assert_allclose(reloaded.locs.sigma_x_nm, 140.0, rtol=1e-4)
    np.testing.assert_allclose(reloaded.locs.lp_x_nm, 12.0, rtol=1e-4)
    assert reloaded.locs.sigma_x_nm[0] != pytest.approx(reloaded.locs.lp_x_nm[0])


# -- end to end -------------------------------------------------------------


def test_localizer_fills_precision_for_every_fit():
    from imswitch.improcess.reconstructors.smlm.localizer import localize_stack

    rng = np.random.default_rng(5)
    frame = rng.normal(100, 2, (48, 48)).astype(np.float32)
    yy, xx = np.mgrid[0:48, 0:48]
    for cy, cx in ((12, 12), (30, 20), (20, 34)):
        frame += 800 * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * 1.4 ** 2))

    locs = localize_stack(frame, threshold=200.0, roi=7, pixel_size_nm=100.0)

    assert len(locs) > 0
    assert np.all(locs.lp_x_nm > 0), "every usable fit should carry a precision"
    # Precision is a position error; it must be far below the spot width.
    assert np.all(locs.lp_x_nm < locs.sigma_x_nm)


@pytest.mark.parametrize("method", ["gausslq", "mle"])
def test_reported_photons_are_integrated_for_both_methods(method):
    """The MLE branch used to report peak amplitude, the other the integral.

    They differ by 2*pi*sigma^2, so anything reading photons changed meaning
    with the fit method. Both are integrals now, so a single bright spot must
    give comparable counts either way.
    """
    from imswitch.improcess.reconstructors.smlm.fitting import fit_spot

    yy, xx = np.mgrid[0:21, 0:21]
    frame = 50 + 1000 * np.exp(-((xx - 10) ** 2 + (yy - 10) ** 2) / (2 * 1.5 ** 2))

    fit = fit_spot(frame, 10, 10, roi=9, method=method)
    integrated = 1000 * 2 * np.pi * 1.5 ** 2

    assert fit["intensity"] == pytest.approx(integrated, rel=0.35)
    assert "background" in fit
