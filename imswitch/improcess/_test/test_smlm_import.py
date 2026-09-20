"""Phase 1 tests: reading localization tables from other SMLM software.

The interesting cases are the conventions that differ silently between tools —
frame base, units, and which column is a width versus a precision. Each gets a
known-answer fixture, because every one of them produces a plausible-looking
result when it is wrong.
"""

from __future__ import annotations

import numpy as np
import pytest

from imswitch.improcess.analysis.smlm_export import export_picasso_hdf5
from imswitch.improcess.analysis.smlm_import import (
    ASSUMED_PIXEL_SIZE_NM,
    LocalizationImportError,
    read_generic_csv,
    read_localizations,
    read_thunderstorm_csv,
    sniff_localization_format,
)
from imswitch.improcess.model.localization_result import LocalizationResult
from imswitch.improcess.model.localization_schema import localizations_from_columns

# A ThunderSTORM 2D export, headers verbatim including the quoting.
TS_2D_HEADER = (
    '"id","frame","x [nm]","y [nm]","sigma [nm]","intensity [photon]",'
    '"offset [photon]","bkgstd [photon]","uncertainty [nm]"'
)
TS_2D_ROWS = [
    "1,1,1000.5,2000.25,130.0,850.0,100.0,10.0,12.5",
    "2,1,3000.0,4000.0,145.0,1200.0,100.0,10.0,9.75",
    "3,2,5000.0,6000.0,138.0,640.0,100.0,10.0,15.0",
]


def _write(path, header, rows):
    path.write_text("\n".join([header, *rows]) + "\n")
    return path


@pytest.fixture
def ts_2d(tmp_path):
    return _write(tmp_path / "ts2d.csv", TS_2D_HEADER, TS_2D_ROWS)


# -- ThunderSTORM -----------------------------------------------------------


def test_thunderstorm_positions_and_photons(ts_2d):
    result = read_thunderstorm_csv(ts_2d)
    assert isinstance(result, LocalizationResult)
    assert len(result) == 3
    np.testing.assert_allclose(result.locs.x_nm, [1000.5, 3000.0, 5000.0], rtol=1e-5)
    np.testing.assert_allclose(result.locs.y_nm, [2000.25, 4000.0, 6000.0], rtol=1e-5)
    np.testing.assert_allclose(result.locs.photons, [850.0, 1200.0, 640.0], rtol=1e-5)
    assert result.dims == "2D"


def test_thunderstorm_frames_are_rebased_to_zero(ts_2d):
    """ThunderSTORM counts from 1; every other index here counts from 0."""
    result = read_thunderstorm_csv(ts_2d)
    np.testing.assert_array_equal(result.locs.frame, [0, 0, 1])


def test_thunderstorm_width_and_uncertainty_reach_different_columns(ts_2d):
    """`sigma` is the PSF width, `uncertainty` the localization precision."""
    result = read_thunderstorm_csv(ts_2d)
    np.testing.assert_allclose(result.locs.sigma_x_nm, [130.0, 145.0, 138.0], rtol=1e-5)
    np.testing.assert_allclose(result.locs.sigma_y_nm, [130.0, 145.0, 138.0], rtol=1e-5)
    np.testing.assert_allclose(result.locs.lp_x_nm, [12.5, 9.75, 15.0], rtol=1e-5)
    np.testing.assert_allclose(result.locs.lp_y_nm, [12.5, 9.75, 15.0], rtol=1e-5)


def test_thunderstorm_pixel_units_are_converted(tmp_path):
    header = '"id","frame","x [px]","y [px]","sigma [px]","intensity [photon]"'
    path = _write(tmp_path / "px.csv", header, ["1,1,10.0,20.0,1.3,850.0"])

    result = read_thunderstorm_csv(path, pixel_size_nm=100.0)
    np.testing.assert_allclose(result.locs.x_nm, [1000.0], rtol=1e-5)
    np.testing.assert_allclose(result.locs.sigma_x_nm, [130.0], rtol=1e-5)


def test_thunderstorm_pixel_units_without_a_pixel_size_is_refused(tmp_path):
    """Guessing here would silently scale someone's whole dataset."""
    header = '"id","frame","x [px]","y [px]"'
    path = _write(tmp_path / "px.csv", header, ["1,1,10.0,20.0"])

    with pytest.raises(LocalizationImportError, match="pixel"):
        read_thunderstorm_csv(path)


def test_thunderstorm_micron_units_are_converted(tmp_path):
    header = '"id","frame","x [um]","y [um]"'
    path = _write(tmp_path / "um.csv", header, ["1,1,1.5,2.5"])

    result = read_thunderstorm_csv(path)
    np.testing.assert_allclose(result.locs.x_nm, [1500.0], rtol=1e-5)


def test_thunderstorm_3d_with_elliptical_sigmas(tmp_path):
    header = (
        '"id","frame","x [nm]","y [nm]","z [nm]","sigma1 [nm]","sigma2 [nm]",'
        '"intensity [photon]","uncertainty_xy [nm]","uncertainty_z [nm]"'
    )
    path = _write(
        tmp_path / "ts3d.csv", header,
        ["1,1,1000.0,2000.0,-250.0,150.0,190.0,900.0,11.0,28.0"],
    )

    result = read_thunderstorm_csv(path)
    assert result.dims == "3D"
    np.testing.assert_allclose(result.locs.z_nm, [-250.0], rtol=1e-5)
    np.testing.assert_allclose(result.locs.sigma_x_nm, [150.0], rtol=1e-5)
    np.testing.assert_allclose(result.locs.sigma_y_nm, [190.0], rtol=1e-5)
    np.testing.assert_allclose(result.locs.lp_z_nm, [28.0], rtol=1e-5)
    # The sigma1/sigma2 -> x/y reading is an assumption, so it is recorded.
    assert result.metadata["sigma_source"] == "sigma1/sigma2"


def test_thunderstorm_records_an_assumed_pixel_size(ts_2d):
    """A nm-native file carries no pixel size; the guess must be visible."""
    result = read_thunderstorm_csv(ts_2d)
    assert result.pixel_size_nm == ASSUMED_PIXEL_SIZE_NM
    assert result.metadata["pixel_size_assumed"] is True


def test_thunderstorm_keeps_a_supplied_pixel_size(ts_2d):
    result = read_thunderstorm_csv(ts_2d, pixel_size_nm=160.0)
    assert result.pixel_size_nm == 160.0
    assert "pixel_size_assumed" not in result.metadata


def test_a_table_without_coordinates_is_refused(tmp_path):
    path = _write(tmp_path / "nope.csv", '"id","frame","intensity [photon]"', ["1,1,5.0"])
    with pytest.raises(LocalizationImportError, match="ThunderSTORM"):
        read_thunderstorm_csv(path)


def test_tab_separated_export_is_read(tmp_path):
    header = "id\tframe\tx [nm]\ty [nm]"
    path = _write(tmp_path / "tabs.tsv", header, ["1\t1\t1000.0\t2000.0"])

    result = read_thunderstorm_csv(path)
    np.testing.assert_allclose(result.locs.x_nm, [1000.0], rtol=1e-5)


def test_pandas_and_stdlib_readers_agree(ts_2d):
    """The fallback exists so a missing pandas degrades to slow, not wrong."""
    from imswitch.improcess.analysis import smlm_import

    headers, viapandas = smlm_import._read_csv_columns(ts_2d)
    _, viastdlib = smlm_import._read_csv_columns_stdlib(ts_2d, ",")

    assert headers == list(viastdlib)
    for header in headers:
        np.testing.assert_allclose(viapandas[header], viastdlib[header], rtol=1e-9)


# -- generic CSV ------------------------------------------------------------


def test_generic_csv_with_an_explicit_mapping(tmp_path):
    path = _write(
        tmp_path / "custom.csv", "n,xpos,ypos,counts",
        ["7,1.5,2.5,900", "8,3.5,4.5,700"],
    )

    result = read_generic_csv(
        path,
        {"frame": "n", "x_nm": "xpos", "y_nm": "ypos", "photons": "counts"},
        unit="um",
        frame_base=7,
    )
    np.testing.assert_allclose(result.locs.x_nm, [1500.0, 3500.0], rtol=1e-5)
    np.testing.assert_array_equal(result.locs.frame, [0, 1])
    np.testing.assert_allclose(result.locs.photons, [900.0, 700.0], rtol=1e-5)


def test_generic_csv_rejects_an_unknown_canonical_column(tmp_path):
    path = _write(tmp_path / "c.csv", "a,b", ["1,2"])
    with pytest.raises(LocalizationImportError, match="Unknown canonical"):
        read_generic_csv(path, {"x_nm": "a", "y_nm": "b", "bogus_nm": "b"})


def test_generic_csv_requires_lateral_coordinates(tmp_path):
    path = _write(tmp_path / "c.csv", "a,b", ["1,2"])
    with pytest.raises(LocalizationImportError, match="y_nm"):
        read_generic_csv(path, {"x_nm": "a"})


def test_generic_csv_reports_a_missing_source_column(tmp_path):
    path = _write(tmp_path / "c.csv", "a,b", ["1,2"])
    with pytest.raises(LocalizationImportError, match="absent"):
        read_generic_csv(path, {"x_nm": "a", "y_nm": "absent"})


# -- sniffing and dispatch --------------------------------------------------


def test_sniff_identifies_thunderstorm(ts_2d):
    assert sniff_localization_format(ts_2d) == "thunderstorm-csv"


def test_sniff_identifies_picasso(tmp_path):
    locs = localizations_from_columns(
        {"x_nm": [100.0, 200.0], "y_nm": [300.0, 400.0]}
    )
    result = LocalizationResult("p", locs, pixel_size_nm=100.0, dims="2D")
    path = export_picasso_hdf5(result, tmp_path / "p.hdf5")

    assert sniff_localization_format(path) == "picasso-hdf5"


def test_sniff_rejects_a_plain_hdf5(tmp_path):
    import h5py

    path = tmp_path / "image.hdf5"
    with h5py.File(str(path), "w") as h5:
        h5.create_dataset("data", data=np.zeros((4, 4)))

    assert sniff_localization_format(path) is None


def test_sniff_returns_none_for_a_missing_file(tmp_path):
    assert sniff_localization_format(tmp_path / "nothing.csv") is None


def test_read_localizations_dispatches_by_content(ts_2d):
    result = read_localizations(ts_2d)
    assert len(result) == 3


def test_read_localizations_round_trips_picasso(tmp_path):
    locs = localizations_from_columns(
        {
            "x_nm": [1000.0, 2000.0],
            "y_nm": [3000.0, 4000.0],
            "sigma_x_nm": [140.0, 150.0],
            "lp_x_nm": [12.0, 14.0],
            "photons": [900.0, 1100.0],
        }
    )
    original = LocalizationResult("rt", locs, pixel_size_nm=100.0, dims="2D")
    path = export_picasso_hdf5(original, tmp_path / "rt.hdf5")

    reloaded = read_localizations(path)
    np.testing.assert_allclose(reloaded.locs.x_nm, original.locs.x_nm, rtol=1e-4)
    np.testing.assert_allclose(reloaded.locs.sigma_x_nm, [140.0, 150.0], rtol=1e-4)
    np.testing.assert_allclose(reloaded.locs.lp_x_nm, [12.0, 14.0], rtol=1e-4)


def test_read_localizations_refuses_an_unmapped_table(tmp_path):
    path = _write(tmp_path / "other.csv", "alpha,beta", ["1,2"])
    with pytest.raises(LocalizationImportError, match="column mapping"):
        read_localizations(path)


def test_read_localizations_refuses_a_non_localization_file(tmp_path):
    path = tmp_path / "notes.rst"
    path.write_text("not a localization table\n")
    with pytest.raises(LocalizationImportError, match="not a localization file"):
        read_localizations(path)
