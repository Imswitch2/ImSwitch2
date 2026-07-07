"""Phase 1 tests: canonical schema + LocalizationResult table result."""

from __future__ import annotations

import numpy as np
import pytest

from imswitch.improcess.model.localization_result import (
    LocalizationResult,
    _LazyHistogramPreview,
)
from imswitch.improcess.model.localization_schema import (
    LOCALIZATION_COLUMNS,
    LOCALIZATION_DTYPE,
    NAPARI_STORM_DTYPE,
    as_localizations,
    empty_localizations,
    localizations_from_columns,
    to_napari_storm_recarray,
    validate_localizations,
)


def _sample_locs(count: int = 4, *, with_z: bool = False) -> np.recarray:
    rng = np.random.default_rng(0)
    columns = {
        "frame": np.arange(count),
        "x_nm": rng.uniform(0, 1000, count),
        "y_nm": rng.uniform(0, 1000, count),
        "sigma_x_nm": np.full(count, 120.0),
        "sigma_y_nm": np.full(count, 120.0),
        "photons": rng.uniform(500, 1500, count),
    }
    if with_z:
        columns["z_nm"] = rng.uniform(-300, 300, count)
        columns["sigma_z_nm"] = np.full(count, 300.0)
    return localizations_from_columns(columns)


# -- schema -----------------------------------------------------------------


def test_empty_localizations_matches_schema():
    locs = empty_localizations(0)
    assert locs.dtype == LOCALIZATION_DTYPE
    assert len(locs) == 0


def test_localizations_from_columns_fills_defaults():
    locs = localizations_from_columns({"x_nm": [1.0, 2.0], "y_nm": [3.0, 4.0]})
    assert locs.dtype == LOCALIZATION_DTYPE
    np.testing.assert_array_equal(locs.z_nm, [0.0, 0.0])
    np.testing.assert_array_equal(locs.photons, [0.0, 0.0])
    np.testing.assert_array_equal(locs.frame, [0, 0])


def test_localizations_from_columns_requires_xy():
    with pytest.raises(KeyError):
        localizations_from_columns({"x_nm": [1.0]})


def test_localizations_from_columns_rejects_unknown_column():
    with pytest.raises(KeyError):
        localizations_from_columns({"x_nm": [1.0], "y_nm": [2.0], "bogus": [3.0]})


def test_localizations_from_columns_length_mismatch():
    with pytest.raises(ValueError):
        localizations_from_columns({"x_nm": [1.0, 2.0], "y_nm": [3.0]})


def test_as_localizations_reorders_and_drops_extra_fields():
    dtype = np.dtype(
        [("photons", "f4"), ("y_nm", "f4"), ("x_nm", "f4")]
        + [(c, "f4") for c in LOCALIZATION_COLUMNS if c not in ("photons", "x_nm", "y_nm")]
        + [("extra", "f4")]
    )
    raw = np.zeros(2, dtype=dtype)
    raw["x_nm"] = [10.0, 20.0]
    canonical = as_localizations(raw)
    assert canonical.dtype == LOCALIZATION_DTYPE
    np.testing.assert_array_equal(canonical.x_nm, [10.0, 20.0])


def test_as_localizations_rejects_plain_array():
    with pytest.raises(TypeError):
        as_localizations(np.zeros((3, 8)))


def test_validate_localizations_requires_exact_dtype():
    with pytest.raises(TypeError):
        validate_localizations(np.zeros(2, dtype=[("x_nm", "f8")]))
    validate_localizations(empty_localizations(2))  # exact dtype passes


def test_napari_storm_projection_divides_by_pixel_size():
    locs = localizations_from_columns(
        {"x_nm": [500.0], "y_nm": [1000.0], "sigma_x_nm": [100.0], "photons": [777.0]}
    )
    storm = to_napari_storm_recarray(locs, pixel_size_nm=100.0)
    assert storm.dtype == NAPARI_STORM_DTYPE
    assert storm.x_pos_pixels[0] == pytest.approx(5.0)
    assert storm.y_pos_pixels[0] == pytest.approx(10.0)
    assert storm.sigma_x_pixels[0] == pytest.approx(1.0)
    assert storm.photon_count[0] == pytest.approx(777.0)


# -- LocalizationResult -----------------------------------------------------


def test_result_construction_and_payload():
    locs = _sample_locs(5)
    result = LocalizationResult("blink", locs, pixel_size_nm=100.0)
    assert result.count == 5
    assert result.dims == "2D"
    # data must be viewable: 2D, has ndim, materializes to an image.
    assert result.data.ndim == 2
    image = np.asarray(result.data)
    assert image.ndim == 2
    assert image.sum() == pytest.approx(5.0)  # one count per localization


def test_result_auto_detects_3d_from_z():
    result = LocalizationResult("z", _sample_locs(3, with_z=True), pixel_size_nm=100.0)
    assert result.dims == "3D"
    assert "z_nm" in result.table_columns()


def test_result_2d_table_hides_z_columns():
    result = LocalizationResult("flat", _sample_locs(3), pixel_size_nm=100.0)
    columns = result.table_columns()
    assert "z_nm" not in columns
    assert "sigma_z_nm" not in columns
    assert columns[:3] == ["frame", "x_nm", "y_nm"]


def test_result_table_records_shape_matches_columns():
    result = LocalizationResult("t", _sample_locs(4), pixel_size_nm=100.0)
    columns = result.table_columns()
    records = result.table_records()
    assert len(records) == 4
    for record in records:
        assert set(record) == set(columns)
        assert isinstance(record["frame"], int)
        assert isinstance(record["x_nm"], float)


def test_result_empty_table_is_viewable_and_has_no_plots():
    result = LocalizationResult("empty", empty_localizations(0), pixel_size_nm=100.0)
    assert result.count == 0
    assert np.asarray(result.data).shape == (1, 1)
    assert result.table_records() == []
    assert result.plot_payloads() == []


def test_result_preview_is_lazy_until_materialized():
    result = LocalizationResult("lazy", _sample_locs(6), pixel_size_nm=100.0)
    assert isinstance(result.data, _LazyHistogramPreview)
    assert result.data._cache is None
    np.asarray(result.data)
    assert result.data._cache is not None
    assert result.scale_unit == "nm"


def test_result_preview_pixel_size_override():
    result = LocalizationResult(
        "p", _sample_locs(4), pixel_size_nm=100.0, preview_pixel_size_nm=250.0
    )
    assert result.preview_pixel_size_nm == pytest.approx(250.0)
    assert result.axis_scales == [pytest.approx(250.0), pytest.approx(250.0)]


def test_result_metadata_and_recarray_roundtrip():
    locs = _sample_locs(3, with_z=True)
    result = LocalizationResult(
        "m",
        locs,
        pixel_size_nm=110.0,
        z_step_nm=50.0,
        source_name="stack.tif",
        metadata={"exposure_ms": 30},
    )
    out = result.to_recarray()
    assert out.dtype == LOCALIZATION_DTYPE
    np.testing.assert_allclose(out.x_nm, locs.x_nm)
    assert result.metadata["exposure_ms"] == 30
    # from_recarray reconstructs an equivalent result
    clone = LocalizationResult.from_recarray("m2", out, pixel_size_nm=110.0)
    np.testing.assert_allclose(clone.to_recarray().y_nm, locs.y_nm)


def test_result_to_dataframe_columns():
    result = LocalizationResult("df", _sample_locs(4), pixel_size_nm=100.0)
    frame = result.to_dataframe()
    assert list(frame.columns) == list(LOCALIZATION_COLUMNS)
    assert len(frame) == 4


def test_result_photon_plot_payload():
    result = LocalizationResult("ph", _sample_locs(20), pixel_size_nm=100.0)
    payloads = result.plot_payloads()
    assert len(payloads) == 1
    assert payloads[0].series[0].kind == "histogram"


def test_result_rejects_non_positive_pixel_size():
    with pytest.raises(ValueError):
        LocalizationResult("bad", _sample_locs(2), pixel_size_nm=0.0)


# -- persistence ------------------------------------------------------------


def test_result_hdf5_roundtrip(tmp_path):
    locs = _sample_locs(5, with_z=True)
    result = LocalizationResult(
        "save",
        locs,
        pixel_size_nm=120.0,
        z_step_nm=40.0,
        source_name="movie.h5",
        metadata={"gain": 2.0},
    )
    path = tmp_path / "locs.h5"
    result.save(path, "hdf5")

    loaded = LocalizationResult.load(path)
    assert loaded.pixel_size_nm == pytest.approx(120.0)
    assert loaded.z_step_nm == pytest.approx(40.0)
    assert loaded.dims == "3D"
    assert loaded.source_name == "movie.h5"
    np.testing.assert_allclose(loaded.to_recarray().x_nm, locs.x_nm, rtol=1e-5)


def test_result_csv_roundtrip(tmp_path):
    locs = _sample_locs(4)
    result = LocalizationResult("csv", locs, pixel_size_nm=100.0)
    path = tmp_path / "locs.csv"
    result.save(path, "csv")

    text = path.read_text().splitlines()
    assert text[0] == ",".join(LOCALIZATION_COLUMNS)
    reloaded = np.genfromtxt(path, delimiter=",", names=True)
    np.testing.assert_allclose(reloaded["x_nm"], locs.x_nm, rtol=1e-5)


def test_result_save_rejects_unknown_format(tmp_path):
    result = LocalizationResult("x", _sample_locs(2), pixel_size_nm=100.0)
    with pytest.raises(ValueError):
        result.save(tmp_path / "x.tiff", "tiff")
