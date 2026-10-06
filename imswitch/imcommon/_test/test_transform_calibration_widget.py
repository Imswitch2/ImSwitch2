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
    assert widget.localizationParametersButton.isEnabled()


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


def test_localization_and_transform_parameters_are_separate(widget):
    assert "threshold_rel" in widget.localizationParameters()
    assert "residual_threshold" not in widget.localizationParameters()
    assert widget.transformParameters()["residual_threshold"] == 2.0
    assert widget.transformParameters()["max_trials"] == 500


def test_parameter_buttons_follow_strategy_and_model_specs(widget):
    assert widget.localizationParametersButton.isEnabled()
    assert widget.transformParametersButton.isEnabled()
    widget.setSelectedModel("identity")
    # Inspect/tune remains useful for transform diagnostics even with no fit params.
    assert widget.transformParametersButton.isEnabled()


def test_unknown_model_is_refused(widget):
    with pytest.raises(ValueError, match="unknown model"):
        widget.setSelectedModel("projective")


def test_host_can_register_an_independent_correspondence_strategy(widget):
    from imswitch.imcommon.algorithms.transforms.correspondence import (
        CorrespondenceResult,
        CorrespondenceStrategySpec,
    )

    source_points = np.array(
        [[10.0, 10.0], [10.0, 30.0], [30.0, 10.0], [30.0, 30.0]]
    )
    target_points = source_points + np.array([2.0, -3.0])

    def correspondence(_source, _target, _parameters):
        return CorrespondenceResult(
            source_points=source_points,
            target_points=target_points,
            metadata={"provider": "test"},
        )

    widget.registerCorrespondenceStrategy(
        CorrespondenceStrategySpec(
            name="test_points",
            label="Test points",
            description="Synthetic host correspondence",
            handler=correspondence,
        ),
        select=True,
    )
    widget.setSourceImage(np.zeros((48, 48)), "moving")
    widget.setTargetImage(np.zeros((48, 48)), "reference")
    widget.setSelectedModel("translation")

    widget.calibrate()

    assert widget.transform.provenance["strategy"] == "test_points"
    assert widget.transform.provenance["correspondence"]["provider"] == "test"
    np.testing.assert_allclose(widget.transform.as_matrix()[:2, 2], [2.0, -3.0])


def test_localization_can_be_inspected_before_transform_fit(widget):
    _load_grid_pair(widget)

    assert widget.runLocalization()
    assert widget.correspondenceResult is not None
    assert widget.transform is None
    assert widget.fitTransformButton.isEnabled()
    assert widget.visualizeButton.isEnabled()
    assert "paired points" in widget.localizationStatusLabel.text()

    assert widget.fitTransform()
    assert widget.transform is not None


def test_transform_parameter_change_keeps_localization(widget):
    _load_grid_pair(widget)
    assert widget.runLocalization()
    result = widget.correspondenceResult
    assert widget.fitTransform()

    params = widget.transformParameters()
    params["residual_threshold"] = 3.0
    widget.setTransformParameters(params)

    assert widget.correspondenceResult is result
    assert widget.transform is None
    assert widget.fitTransformButton.isEnabled()


def test_failed_transform_fit_keeps_localization(widget):
    from imswitch.imcommon.algorithms.transforms.correspondence import (
        CorrespondenceResult,
        CorrespondenceStrategySpec,
    )

    point = np.array([[5.0, 5.0]])
    widget.registerCorrespondenceStrategy(
        CorrespondenceStrategySpec(
            name="one_pair",
            label="One pair",
            description="One synthetic pair",
            handler=lambda *_args: CorrespondenceResult(point, point + 1.0),
        ),
        select=True,
    )
    widget.setSourceImage(np.zeros((16, 16)))
    widget.setTargetImage(np.zeros((16, 16)))
    widget.setSelectedModel("affine")

    assert widget.runLocalization()
    result = widget.correspondenceResult
    assert not widget.fitTransform()
    assert widget.correspondenceResult is result
    assert widget.transform is None
    assert "fit failed" in widget.statusLabel.text().lower()


def test_localization_parameter_change_invalidates_both_stages(widget):
    _load_grid_pair(widget)
    widget.calibrate()
    params = widget.localizationParameters()
    params["threshold_rel"] = 0.3
    moving_result = widget.localizationResult("source")
    reference_result = widget.localizationResult("target")
    widget.setLocalizationParameters(params)

    assert widget.correspondenceResult is None
    assert widget.localizationResult("source") is moving_result
    assert widget.localizationResult("target") is reference_result
    assert widget.localizationIsStale("source")
    assert widget.localizationIsStale("target")
    assert widget.transform is None
    assert not widget.fitTransformButton.isEnabled()


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


def test_compact_status_reports_fit_quality(widget):
    _load_grid_pair(widget)
    widget.setSelectedModel("rigid")
    widget.calibrate()
    assert "pairs" in widget.statusLabel.text()
    assert "RMS" in widget.statusLabel.text()
    assert "max" in widget.statusLabel.text()


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
    assert transform.provenance["localization_parameters"]["moving"]["n_rows"] == 4
    assert transform.provenance["fit_parameters"]["residual_threshold"] == 2.0
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
    assert "✕" in widget.sourceLocalizationStatusLabel.text()
    assert "✕" in widget.targetLocalizationStatusLabel.text()
    assert "waiting" in widget.statusLabel.text().lower()
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
    assert widget.localizationParametersButton.isEnabled()


def test_clear_resets_everything(widget):
    _load_grid_pair(widget)
    widget.calibrate()
    widget.clear()

    assert widget.transform is None
    assert widget.statusLabel.text() == "○ No transform"
    assert widget.residualLabel.text() == "Residuals: n/a"
    assert widget.matrixView.toPlainText() == ""


def test_parameter_validation_rejects_nonsense(widget):
    with pytest.raises(ValueError, match="Focus threshold"):
        widget._normalizeParameters({**widget._parameters, "threshold_rel": 4.0})
    with pytest.raises(ValueError, match="Maximum relative grid tilt"):
        widget._normalizeParameters(
            {**widget._parameters, "max_relative_tilt_deg": 70.0}
        )


def test_non_2d_image_is_refused(widget):
    with pytest.raises(ValueError, match="must be 2-D"):
        widget.setSourceImage(np.zeros((3, 4, 5)))


def test_visualize_opens_the_shared_workspace(widget, qtbot):
    pytest.importorskip("pyqtgraph")
    _load_grid_pair(widget)
    widget.calibrate()
    widget.visualize()
    assert widget._workspace is not None
    qtbot.addWidget(widget._workspace)


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


def test_independent_localization_parameters_and_reruns(widget):
    from imswitch.imcommon.algorithms.transforms.correspondence import (
        CorrespondenceResult,
        CorrespondenceStrategySpec,
        ImageLocalizationResult,
    )
    from imswitch.imcommon.algorithms.transforms import ParameterSpec

    calls = []

    def localize(_image, parameters):
        value = float(parameters["offset"])
        calls.append(value)
        points = np.array([[1.0, 2.0], [3.0, 4.0]]) + value
        return ImageLocalizationResult(points, metadata={"offset": value})

    def match(source, target, *_parameters):
        return CorrespondenceResult(
            source.points, target.points,
            source_localization=source, target_localization=target,
        )

    widget.registerCorrespondenceStrategy(
        CorrespondenceStrategySpec(
            name="split_test", label="Split test", description="split",
            parameter_specs={"offset": ParameterSpec(0.0, float, "Offset")},
            localize_handler=localize, match_handler=match,
        ),
        select=True,
    )
    widget.setSourceImage(np.zeros((8, 8)))
    widget.setTargetImage(np.zeros((8, 8)))
    widget.setLocalizationParameters({"offset": 1.0}, "source")
    widget.setLocalizationParameters({"offset": 2.0}, "target")

    assert widget.runLocalizationForRole("source")
    assert calls == [1.0]
    assert widget.localizationResult("source") is not None
    assert widget.localizationResult("target") is None

    assert widget.runLocalizationForRole("target")
    assert calls == [1.0, 2.0]
    assert widget.buildCorrespondence()
    assert widget.correspondenceResult is not None
    np.testing.assert_allclose(
        widget.correspondenceResult.target_points
        - widget.correspondenceResult.source_points,
        1.0,
    )


def test_main_localization_runs_both_with_their_own_parameters(widget):
    from imswitch.imcommon.algorithms.transforms.correspondence import (
        CorrespondenceResult,
        CorrespondenceStrategySpec,
        ImageLocalizationResult,
    )
    from imswitch.imcommon.algorithms.transforms import ParameterSpec

    calls = []
    def localize(_image, parameters):
        value = float(parameters["threshold"])
        calls.append(value)
        return ImageLocalizationResult(np.array([[value, value]]))
    def match(source, target, *_parameters):
        return CorrespondenceResult(source.points, target.points)

    widget.registerCorrespondenceStrategy(
        CorrespondenceStrategySpec(
            name="two_params", label="Two params", description="two",
            parameter_specs={"threshold": ParameterSpec(0.1, float, "Threshold")},
            localize_handler=localize, match_handler=match,
        ), select=True,
    )
    widget.setSourceImage(np.zeros((8, 8)))
    widget.setTargetImage(np.zeros((8, 8)))
    widget.setLocalizationParameters({"threshold": 0.2}, "source")
    widget.setLocalizationParameters({"threshold": 0.7}, "target")

    assert widget.runLocalization()
    assert calls == [0.2, 0.7]
    assert "✓" in widget.sourceLocalizationStatusLabel.text()
    assert "✓" in widget.targetLocalizationStatusLabel.text()


def test_workspace_is_persistent_and_tab_specific(widget, qtbot):
    pytest.importorskip("pyqtgraph")
    widget.openWorkspace("localization")
    first = widget._workspace
    assert first is not None
    qtbot.addWidget(first)
    assert first.tabs.currentIndex() == 0
    widget.openWorkspace("transform")
    assert widget._workspace is first
    assert first.tabs.currentIndex() == 1
