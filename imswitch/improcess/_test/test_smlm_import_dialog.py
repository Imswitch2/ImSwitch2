"""Phase 3 tests: mapping an arbitrary CSV onto the canonical schema.

The guesswork is tested headlessly because it lives in the analysis layer;
the dialog is tested for the decisions it makes on top — chiefly which
selections it refuses to accept.
"""

from __future__ import annotations

import numpy as np
import pytest

from imswitch.improcess.analysis.smlm_import import (
    guess_column_mapping,
    guess_length_unit,
    read_csv_headers,
    read_generic_csv,
)

pytest.importorskip("qtpy.QtWidgets")

from imswitch.improcess.view.LocalizationImportDialog import (  # noqa: E402
    LocalizationImportDialog,
)


def _write(tmp_path, header, rows=("1,2,3,4",), name="custom.csv"):
    path = tmp_path / name
    path.write_text("\n".join([header, *rows]) + "\n")
    return path


# -- guessing (no Qt) -------------------------------------------------------


def test_headers_are_read_without_the_whole_table(tmp_path):
    path = _write(tmp_path, "a,b,c,d", [f"{i},{i},{i},{i}" for i in range(10000)])
    assert read_csv_headers(path) == ["a", "b", "c", "d"]


def test_guess_maps_obvious_names():
    mapping = guess_column_mapping(["frame", "x", "y", "intensity"])
    assert mapping["x_nm"] == "x"
    assert mapping["y_nm"] == "y"
    assert mapping["frame"] == "frame"
    assert mapping["photons"] == "intensity"


def test_guess_maps_alternative_spellings():
    mapping = guess_column_mapping(["xpos", "ypos", "n_photons"])
    assert mapping["x_nm"] == "xpos"
    assert mapping["y_nm"] == "ypos"
    assert mapping["photons"] == "n_photons"


def test_guess_shares_an_isotropic_width_across_both_axes():
    mapping = guess_column_mapping(["x", "y", "sigma"])
    assert mapping["sigma_x_nm"] == "sigma"
    assert mapping["sigma_y_nm"] == "sigma"


def test_guess_does_not_share_a_column_that_means_one_axis():
    """sx must not be pressed into service as the y width as well."""
    mapping = guess_column_mapping(["x", "y", "sx"])
    assert mapping["sigma_x_nm"] == "sx"
    assert "sigma_y_nm" not in mapping


def test_guess_keeps_width_and_precision_apart():
    mapping = guess_column_mapping(["x", "y", "sigma", "uncertainty"])
    assert mapping["sigma_x_nm"] == "sigma"
    assert mapping["lp_x_nm"] == "uncertainty"


def test_guess_leaves_unrecognised_columns_alone():
    """A blank is better than a confident wrong answer."""
    mapping = guess_column_mapping(["alpha", "beta", "gamma"])
    assert mapping == {}


def test_unit_is_taken_from_the_header_when_declared():
    assert guess_length_unit(["frame", "x [px]", "y [px]"]) == "px"
    assert guess_length_unit(["frame", "x [nm]", "y [nm]"]) == "nm"
    assert guess_length_unit(["frame", "x", "y"]) == "nm"


# -- the dialog -------------------------------------------------------------


def test_dialog_prefills_from_the_guess(qtbot, tmp_path):
    path = _write(tmp_path, "frame,x,y,intensity")
    dialog = LocalizationImportDialog(path)
    qtbot.addWidget(dialog)

    mapping = dialog.column_mapping()
    assert mapping["x_nm"] == "x"
    assert mapping["y_nm"] == "y"
    assert mapping["photons"] == "intensity"


def test_dialog_accepts_once_positions_are_mapped(qtbot, tmp_path):
    path = _write(tmp_path, "frame,x,y,intensity")
    dialog = LocalizationImportDialog(path)
    qtbot.addWidget(dialog)

    from qtpy import QtWidgets

    assert dialog._buttons.button(QtWidgets.QDialogButtonBox.Ok).isEnabled()


def test_dialog_refuses_without_positions(qtbot, tmp_path):
    """Nothing recognisable: OK stays disabled until the user maps x and y."""
    path = _write(tmp_path, "alpha,beta,gamma,delta")
    dialog = LocalizationImportDialog(path)
    qtbot.addWidget(dialog)

    from qtpy import QtWidgets

    assert not dialog._buttons.button(QtWidgets.QDialogButtonBox.Ok).isEnabled()
    assert "Choose a column" in dialog._warning.text()


def test_dialog_refuses_pixels_without_a_pixel_size(qtbot, tmp_path):
    """Reading pixels with no pixel size would rescale the whole dataset."""
    path = _write(tmp_path, "frame,x [px],y [px],intensity")
    dialog = LocalizationImportDialog(path)
    qtbot.addWidget(dialog)

    from qtpy import QtWidgets

    assert dialog.length_unit() == "px"
    ok = dialog._buttons.button(QtWidgets.QDialogButtonBox.Ok)
    assert not ok.isEnabled()
    assert "pixel size" in dialog._warning.text()

    dialog._pixelSize.setValue(117.0)
    assert ok.isEnabled()
    assert dialog.pixel_size_nm() == pytest.approx(117.0)


def test_dialog_warns_about_what_will_be_assumed(qtbot, tmp_path):
    path = _write(tmp_path, "frame,x,y")
    dialog = LocalizationImportDialog(path)
    qtbot.addWidget(dialog)

    text = dialog._warning.text()
    assert "pixel size" in text.lower()
    assert "precision" in text.lower()


def test_dialog_kwargs_drive_the_reader(qtbot, tmp_path):
    """The dialog's output must be exactly read_generic_csv's input."""
    path = _write(
        tmp_path, "frame,x,y,intensity", ["1,1.5,2.5,900", "2,3.5,4.5,700"]
    )
    dialog = LocalizationImportDialog(path)
    qtbot.addWidget(dialog)
    dialog._unit.setCurrentIndex(list(dialog._unit.itemText(i)
                                      for i in range(dialog._unit.count())).index("um"))
    dialog._frameBase.setValue(1)

    result = read_generic_csv(path, **dialog.import_kwargs())

    np.testing.assert_allclose(result.locs.x_nm, [1500.0, 3500.0], rtol=1e-5)
    np.testing.assert_array_equal(result.locs.frame, [0, 1])
    np.testing.assert_allclose(result.locs.photons, [900.0, 700.0], rtol=1e-5)


def test_unmapped_columns_are_omitted_not_guessed(qtbot, tmp_path):
    path = _write(tmp_path, "frame,x,y")
    dialog = LocalizationImportDialog(path)
    qtbot.addWidget(dialog)

    mapping = dialog.column_mapping()
    assert "photons" not in mapping
    assert "lp_x_nm" not in mapping
