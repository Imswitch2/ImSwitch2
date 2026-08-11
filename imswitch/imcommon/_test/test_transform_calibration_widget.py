"""Tests for the Step 0 transform calibration harness widget."""

import numpy as np
import pytest

pytest.importorskip("qtpy")

import importlib  # noqa: E402

from imswitch.imcommon.algorithms.transforms import AffineTransform  # noqa: E402
from imswitch.imcommon.view.widgets.TransformCalibrationWidget import (  # noqa: E402
    TransformCalibrationWidget,
    load_image_file,
)

from .test_transforms import foci_image, grid_positions, known_affine  # noqa: E402


# The package __init__ re-exports the class under the module's own name, so
# plain `import ... as m` yields the class. Go through importlib for the module.
widget_module = importlib.import_module(
    "imswitch.imcommon.view.widgets.TransformCalibrationWidget"
)


def _stub_file_dialog(monkeypatch, path):
    monkeypatch.setattr(
        widget_module.guitools, "askForFilePath", lambda *args, **kwargs: str(path)
    )


@pytest.fixture
def widget(qtbot):
    calibration_widget = TransformCalibrationWidget()
    qtbot.addWidget(calibration_widget)
    return calibration_widget


def _load_grid_pair(widget):
    source_points = grid_positions()
    truth = known_affine()
    widget.setSourceImage(foci_image(source_points), "moving.tif")
    widget.setTargetImage(foci_image(truth.map_points(source_points)), "reference.tif")
    widget._parameters.update({"n_rows": 4, "n_cols": 4, "min_distance": 15})
    return truth


def test_starts_with_nothing_calibrated(widget):
    assert widget.transform is None
    assert not widget.calibrateButton.isEnabled()
    assert not widget.saveTransformButton.isEnabled()
    assert not widget.visualizeButton.isEnabled()


def test_calibrate_enables_saving_and_reports_residuals(widget):
    truth = _load_grid_pair(widget)
    assert widget.calibrateButton.isEnabled()

    widget.calibrate()

    assert widget.transform is not None
    assert widget.transform.source_frame == "moving"
    assert widget.transform.target_frame == "reference"
    np.testing.assert_allclose(
        widget.transform.as_matrix(), truth.matrix, atol=1e-2
    )
    assert widget.saveTransformButton.isEnabled()
    assert widget.visualizeButton.isEnabled()
    assert "Residuals:" in widget.residualLabel.text()
    assert "n/a" not in widget.residualLabel.text()
    assert widget.matrixView.toPlainText()


def test_model_combo_offers_every_estimator(widget):
    from imswitch.imcommon.algorithms.transforms import estimator_specs

    offered = [
        widget.modelCombo.itemData(index)
        for index in range(widget.modelCombo.count())
    ]
    assert offered == [spec.name for spec in estimator_specs()]
    assert widget.selectedModel() == "affine"
    assert widget.modelHintLabel.text()


def test_choosing_a_model_updates_the_hint(widget):
    widget.setSelectedModel("rigid")
    assert widget.selectedModel() == "rigid"
    assert "no scaling" in widget.modelHintLabel.text()


def test_unknown_model_is_refused(widget):
    with pytest.raises(ValueError, match="unknown model"):
        widget.setSelectedModel("projective")


@pytest.mark.parametrize(
    "model, expected_kind",
    [
        ("affine", "affine"),
        ("similarity", "affine"),
        ("rigid", "affine"),
        ("translation", "affine"),
        ("rotation90", "rotation90"),
        ("identity", "identity"),
    ],
)
def test_every_model_choice_calibrates(widget, model, expected_kind):
    _load_grid_pair(widget)
    widget.setSelectedModel(model)

    widget.calibrate()

    assert widget.transform is not None
    assert widget.transform.kind == expected_kind
    assert widget.transform.provenance["estimator"] == model
    assert widget.matrixView.toPlainText()


def test_constrained_choice_is_named_in_the_status(widget):
    """'affine' is the stored kind for a rigid fit; the status must say rigid."""
    _load_grid_pair(widget)
    widget.setSelectedModel("rigid")
    widget.calibrate()
    assert "rigid -> affine" in widget.statusLabel.text()


def test_affine_choice_is_not_double_named(widget):
    _load_grid_pair(widget)
    widget.calibrate()
    assert "affine -> affine" not in widget.statusLabel.text()
    assert "[affine," in widget.statusLabel.text()


def test_rotation90_choice_survives_a_save_reload(widget, tmp_path, monkeypatch):
    """The non-affine kind must round-trip, matrix dataset and all."""
    _load_grid_pair(widget)
    widget.setSelectedModel("rotation90")
    widget.calibrate()

    path = tmp_path / "rotation.json"
    _stub_file_dialog(monkeypatch, path)
    widget.saveTransform()
    widget.clear()
    widget.loadTransform()

    assert widget.transform.kind == "rotation90"


def test_calibration_records_its_provenance_and_context(widget):
    _load_grid_pair(widget)
    widget.calibrate()

    transform = widget.transform
    assert transform.provenance["strategy"] == "foci_grid"
    assert transform.provenance["n_rows"] == 4
    assert transform.source_context.image_shape == (256, 256)
    assert transform.source_context.extra["file"] == "moving.tif"
    assert transform.target_context.extra["file"] == "reference.tif"


def test_failed_calibration_reports_and_clears(widget):
    """A wrong grid size must fail loudly and leave no half-built transform."""
    _load_grid_pair(widget)
    widget._parameters.update({"n_rows": 7, "n_cols": 7})

    widget.calibrate()

    assert widget.transform is None
    assert "failed" in widget.statusLabel.text().lower()
    assert not widget.saveTransformButton.isEnabled()


def test_save_and_reload_through_the_widget(widget, tmp_path, monkeypatch):
    _load_grid_pair(widget)
    widget.calibrate()
    original = widget.transform

    path = tmp_path / "calibration.json"
    _stub_file_dialog(monkeypatch, path)

    widget.saveTransform()
    assert path.is_file()

    widget.clear()
    assert widget.transform is None

    widget.loadTransform()
    assert widget.transform is not None
    np.testing.assert_allclose(widget.transform.as_matrix(), original.as_matrix())


def test_loading_a_transform_drops_stale_overlay_points(widget, tmp_path, monkeypatch):
    """Visualize must not draw points belonging to a different calibration."""
    _load_grid_pair(widget)
    widget.calibrate()

    path = tmp_path / "calibration.json"
    _stub_file_dialog(monkeypatch, path)
    widget.saveTransform()
    widget.loadTransform()

    assert widget.transform is not None
    assert not widget.visualizeButton.isEnabled()


def test_clear_resets_everything(widget):
    _load_grid_pair(widget)
    widget.calibrate()
    widget.clear()

    assert widget.transform is None
    assert widget.statusLabel.text() == "No transform"
    assert widget.residualLabel.text() == "Residuals: n/a"
    assert widget.matrixView.toPlainText() == ""


def test_parameter_validation_rejects_nonsense(widget):
    with pytest.raises(ValueError, match="threshold_rel"):
        widget._normalizeParameters({**widget._parameters, "threshold_rel": 4.0})
    with pytest.raises(ValueError, match="max_relative_tilt_deg"):
        widget._normalizeParameters(
            {**widget._parameters, "max_relative_tilt_deg": 70.0}
        )


def test_non_2d_image_is_refused(widget):
    with pytest.raises(ValueError, match="must be 2-D"):
        widget.setSourceImage(np.zeros((3, 4, 5)))


def test_visualize_builds_a_dialog(widget):
    _load_grid_pair(widget)
    widget.calibrate()
    dialog = widget._buildVisualization()
    assert dialog is not None


# ---------------------------------------------------------------------------
# File loading
# ---------------------------------------------------------------------------


def test_load_npy(tmp_path):
    path = tmp_path / "image.npy"
    np.save(path, np.arange(24, dtype=np.float32).reshape(4, 6))
    np.testing.assert_allclose(load_image_file(path), np.arange(24).reshape(4, 6))


def test_load_tiff(tmp_path):
    tifffile = pytest.importorskip("tifffile")
    path = tmp_path / "image.tif"
    picture = np.arange(24, dtype=np.uint16).reshape(4, 6)
    tifffile.imwrite(str(path), picture)
    np.testing.assert_array_equal(load_image_file(path), picture)


def test_load_h5_finds_the_first_image_dataset(tmp_path):
    h5py = pytest.importorskip("h5py")
    path = tmp_path / "recording.h5"
    picture = np.arange(24, dtype=np.uint16).reshape(4, 6)
    with h5py.File(path, "w") as handle:
        handle.create_dataset("scalars", data=np.arange(5))
        handle.create_group("acquisition").create_dataset("frame", data=picture)
    np.testing.assert_array_equal(load_image_file(path), picture)


def test_load_reduces_a_stack_to_one_plane(tmp_path):
    """A calibration is measured on a plane, not on a projection."""
    path = tmp_path / "stack.npy"
    stack = np.random.default_rng(0).random((5, 8, 9))
    np.save(path, stack)
    np.testing.assert_allclose(load_image_file(path), stack[2])


def test_load_rejects_a_non_image(tmp_path):
    path = tmp_path / "vector.npy"
    np.save(path, np.arange(7))
    with pytest.raises(ValueError, match="not an image"):
        load_image_file(path)


def test_load_h5_without_an_image_is_refused(tmp_path):
    h5py = pytest.importorskip("h5py")
    path = tmp_path / "empty.h5"
    with h5py.File(path, "w") as handle:
        handle.create_dataset("scalars", data=np.arange(5))
    with pytest.raises(ValueError, match="no dataset"):
        load_image_file(path)


def test_widget_calibration_matches_the_library_directly(widget):
    """The widget must add no arithmetic of its own on top of the core."""
    from imswitch.imcommon.algorithms.transforms import estimate_affine
    from imswitch.imcommon.algorithms.transforms.correspondence import (
        find_grid_correspondence,
    )

    _load_grid_pair(widget)
    widget.calibrate()

    source, target = find_grid_correspondence(
        widget._source_image,
        widget._target_image,
        n_rows=4,
        n_cols=4,
        min_distance=15,
    )
    expected = estimate_affine(source, target)

    assert isinstance(widget.transform.model, AffineTransform)
    np.testing.assert_allclose(
        widget.transform.as_matrix(), expected.transform.matrix
    )
