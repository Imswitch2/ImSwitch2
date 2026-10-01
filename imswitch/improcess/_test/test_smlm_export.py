"""Phase 4 tests: napari-storm (Picasso) HDF5 export contract."""

from __future__ import annotations

import numpy as np
import pytest

from imswitch.improcess.analysis.smlm_export import (
    export_picasso_hdf5,
    read_picasso_hdf5,
    to_picasso_recarray,
)
from imswitch.improcess.model.localization_result import LocalizationResult
from imswitch.improcess.model.localization_schema import localizations_from_columns


def _result(count=6, with_z=False, pixel_size_nm=100.0):
    rng = np.random.default_rng(3)
    columns = {
        "frame": np.arange(count),
        "x_nm": rng.uniform(0, 5000, count),
        "y_nm": rng.uniform(0, 5000, count),
        "sigma_x_nm": np.full(count, 130.0),
        "sigma_y_nm": np.full(count, 140.0),
        "photons": rng.uniform(500, 1500, count),
    }
    if with_z:
        columns["z_nm"] = rng.uniform(-400, 400, count)
        columns["sigma_z_nm"] = np.full(count, 300.0)
    locs = localizations_from_columns(columns)
    return LocalizationResult(
        "storm", locs, pixel_size_nm=pixel_size_nm,
        dims="3D" if with_z else "2D", source_shape=(64, 64),
    )


def test_picasso_recarray_2d_fields_and_units():
    result = _result(pixel_size_nm=100.0)
    recs = to_picasso_recarray(result)
    # napari-storm reads these field names by attribute.
    assert set(recs.dtype.names) == {
        "frame", "x", "y", "photons", "sx", "sy", "lpx", "lpy",
    }
    # x/y are pixels = nm / pixel_size.
    np.testing.assert_allclose(recs.x, result.locs.x_nm / 100.0, rtol=1e-5)
    # Picasso keeps PSF width and localization precision apart, and so do we:
    # sx is the width, lpx the precision. Writing the width into lpx (as this
    # once did) makes every consumer read a spot size as a position error.
    np.testing.assert_allclose(recs.sx, result.locs.sigma_x_nm / 100.0, rtol=1e-5)
    np.testing.assert_allclose(recs.lpx, result.locs.lp_x_nm / 100.0, rtol=1e-5)


def test_picasso_recarray_3d_has_z_in_nm():
    result = _result(with_z=True, pixel_size_nm=100.0)
    recs = to_picasso_recarray(result)
    assert "z" in recs.dtype.names
    assert "lpz" in recs.dtype.names
    # z is stored in nm (napari-storm divides by pixel size on read).
    np.testing.assert_allclose(recs.z, result.locs.z_nm, rtol=1e-5)


def test_export_writes_locs_dataset_and_yaml(tmp_path):
    result = _result()
    path = export_picasso_hdf5(result, tmp_path / "out.hdf5")
    assert path.exists()
    assert path.with_suffix(".yaml").exists()

    import h5py

    with h5py.File(str(path), "r") as h5:
        assert "locs" in h5
        assert h5["locs"].attrs["pixelsize_nm"] == pytest.approx(100.0)
        assert bool(h5["locs"].attrs["zdim_present"]) is False


def test_export_forces_hdf5_suffix(tmp_path):
    result = _result()
    path = export_picasso_hdf5(result, tmp_path / "noext")
    assert path.suffix == ".hdf5"


def test_export_roundtrip_2d(tmp_path):
    result = _result(pixel_size_nm=110.0)
    path = export_picasso_hdf5(result, tmp_path / "rt.hdf5")

    reloaded = read_picasso_hdf5(path)
    assert reloaded.dims == "2D"
    assert reloaded.pixel_size_nm == pytest.approx(110.0)
    np.testing.assert_allclose(reloaded.locs.x_nm, result.locs.x_nm, rtol=1e-4)
    np.testing.assert_allclose(reloaded.locs.y_nm, result.locs.y_nm, rtol=1e-4)
    np.testing.assert_allclose(reloaded.locs.sigma_x_nm, result.locs.sigma_x_nm, rtol=1e-4)
    np.testing.assert_allclose(reloaded.locs.photons, result.locs.photons, rtol=1e-4)


def test_export_roundtrip_3d(tmp_path):
    result = _result(with_z=True, pixel_size_nm=120.0)
    path = export_picasso_hdf5(result, tmp_path / "rt3d.hdf5")

    reloaded = read_picasso_hdf5(path)
    assert reloaded.dims == "3D"
    np.testing.assert_allclose(reloaded.locs.z_nm, result.locs.z_nm, rtol=1e-4)


def test_roundtrip_reads_pixelsize_from_yaml_when_attr_absent(tmp_path):
    result = _result(pixel_size_nm=95.0)
    path = export_picasso_hdf5(result, tmp_path / "yaml.hdf5")

    # Strip the attr so only the yaml sidecar carries the pixel size.
    import h5py

    with h5py.File(str(path), "a") as h5:
        del h5["locs"].attrs["pixelsize_nm"]

    reloaded = read_picasso_hdf5(path)
    assert reloaded.pixel_size_nm == pytest.approx(95.0)


def test_localization_result_save_picasso_format(tmp_path):
    result = _result()
    path = tmp_path / "viasave.hdf5"
    result.save(path, "napari-storm")
    reloaded = read_picasso_hdf5(path)
    np.testing.assert_allclose(reloaded.locs.x_nm, result.locs.x_nm, rtol=1e-4)


def test_read_requires_pixel_size_when_none_available(tmp_path):
    result = _result(pixel_size_nm=100.0)
    path = export_picasso_hdf5(result, tmp_path / "np.hdf5")
    import h5py

    with h5py.File(str(path), "a") as h5:
        del h5["locs"].attrs["pixelsize_nm"]
    path.with_suffix(".yaml").unlink()

    with pytest.raises(ValueError):
        read_picasso_hdf5(path)
