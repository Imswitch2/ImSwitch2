"""The Transform Calibration widget's Manual point pairs method, driven with
synthetic mouse clicks on the point picker (offscreen, no napari)."""

import numpy as np
import pytest

pytest.importorskip("qtpy")

from qtpy import QtCore, QtWidgets  # noqa: E402
from qtpy.QtTest import QTest  # noqa: E402

from imswitch.imcommon._test import _bead_fields as bf  # noqa: E402
from imswitch.imcommon.algorithms.transforms import AcquisitionContext, AffineTransform  # noqa: E402
from imswitch.imcommon.view.widgets.TransformCalibrationWidget import (  # noqa: E402
    CalibrationSource,
    TransformCalibrationWidget,
)

TRUTH = AffineTransform.from_linear_offset(
    1.25 * np.array([[np.cos(0.3), -np.sin(0.3)], [np.sin(0.3), np.cos(0.3)]]), [20.0, -10.0])
SPOTS = np.array([[40.0, 50.0], [60.0, 150.0], [150.0, 70.0], [170.0, 160.0], [100.0, 110.0], [130.0, 40.0]])


def _images():
    rng = np.random.default_rng(0)
    moving = bf.render((220, 220), SPOTS, 1.6, [1200] * len(SPOTS), 40, rng)
    reference = bf.render((300, 300), TRUTH.map_points(SPOTS), 2.0, [1200] * len(SPOTS), 40, rng)
    return moving, reference


@pytest.fixture
def widget(qtbot):
    w = TransformCalibrationWidget()
    qtbot.addWidget(w)
    moving, reference = _images()
    w.setSourceImage(moving, "moving.tif")
    w.setTargetImage(reference, "reference.tif")
    w.setSelectedCorrespondence("manual")
    w.setSelectedModel("similarity")
    return w


def _click(pane, row, col):
    QtWidgets.QApplication.processEvents()
    pos = pane.widgetPositionOf(row, col)
    QTest.mouseClick(pane.plot.viewport(), QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, pos)
    QtWidgets.QApplication.processEvents()


@pytest.fixture
def picker(widget, qtbot):
    widget.openPointPicker()
    picker = widget._picker
    picker.resize(1200, 780)
    picker.show()
    qtbot.waitExposed(picker)
    for pane in (picker.moving, picker.reference):
        pane.plot.autoRange(padding=0)
    QtWidgets.QApplication.processEvents()
    return picker


def test_clicks_make_pairs_fit_and_show_each_pairs_residual(widget, picker):
    targets = TRUTH.map_points(SPOTS)
    assert widget.manualProblem.startswith("No pairs yet")
    for k in range(3):
        # A click 1.5 px off the spot: snapping (on by default) finds the centre.
        _click(picker.moving, SPOTS[k, 0] + 1.5, SPOTS[k, 1] - 1.0)
        assert widget.pairEditor.next_role() == "target"
        _click(picker.reference, targets[k, 0] - 1.0, targets[k, 1] + 1.5)
    assert len(widget.pairEditor) == 3 and widget.transform is not None
    assert widget.transform.provenance["strategy"] == "manual"
    np.testing.assert_allclose(widget.transform.map_points(SPOTS), targets, atol=0.3)
    assert picker.table.rowCount() == 3 and picker.table.item(0, 3).text().endswith("px")
    assert picker.table.item(0, 4).text()                   # leave-one-out with a spare pair
    # A bad click: obvious in the residuals, and singled out.
    _click(picker.moving, SPOTS[3, 0], SPOTS[3, 1])
    _click(picker.reference, targets[5, 0], targets[5, 1])  # the wrong spot
    _click(picker.moving, SPOTS[4, 0], SPOTS[4, 1])
    _click(picker.reference, targets[4, 0], targets[4, 1])
    pair_fit = widget.manualPairFit
    assert pair_fit.suspicious() == 3
    assert "disagrees" in widget.manualStatusLabel.text() and "pair 4" in widget.manualStatusLabel.text()
    assert picker.table.item(3, 0).foreground().color().name() == "#ff6666"
    # Delete it, then undo and redo the delete; reorder.
    picker.table.selectRow(3)
    picker.deleteButton.click()
    assert len(widget.pairEditor) == 4 and widget.manualPairFit.suspicious() is None
    picker.undoButton.click()
    assert len(widget.pairEditor) == 5
    picker.table.selectRow(3)
    picker.deleteButton.click()
    picker.table.selectRow(3)
    picker.upButton.click()
    assert widget.pairEditor.pairs[2].source[0] == pytest.approx(SPOTS[4, 0], abs=0.3)
    assert widget.transform.residuals.rms < 0.3
    picker.clearButton.click()
    assert len(widget.pairEditor) == 0 and widget.transform is None


def test_a_degenerate_set_is_refused_with_a_reason(widget, picker):
    widget.setSelectedModel("affine")
    line = np.array([[40.0, 40.0], [80.0, 80.0], [120.0, 120.0]])
    image = bf.render((220, 220), line, 1.6, [1200] * 3, 40, np.random.default_rng(1))
    widget.setSourceImage(image, "line.tif")
    widget.setTargetImage(image, "line.tif")
    for point in line:
        widget.addManualPoint("moving", point)
        widget.addManualPoint("reference", point)
    assert widget.transform is None
    assert "lie on a line" in widget.manualProblem
    assert "lie on a line" in widget.manualStatusLabel.text()
    widget.setSelectedModel("similarity")
    widget.calibrate()
    assert widget.transform is not None      # two points are enough for a similarity


def test_the_minimum_per_model_is_stated(widget):
    widget.setSelectedModel("affine")
    widget.addManualPoint("moving", SPOTS[0])
    widget.addManualPoint("reference", TRUTH.map_points(SPOTS[:1])[0])
    assert "affine needs at least 3" in widget.manualProblem
    assert widget.transform is None


def test_snapping_can_be_turned_off(widget, picker):
    picker.snapCheck.setChecked(False)
    _click(picker.moving, SPOTS[0, 0] + 2.0, SPOTS[0, 1] + 2.0)
    clicked = widget.pairEditor.pairs[0].source
    assert abs(clicked[0] - (SPOTS[0, 0] + 2.0)) < 0.6 and abs(clicked[1] - (SPOTS[0, 1] + 2.0)) < 0.6


def test_pairs_into_world_coordinates_and_saved_under_an_id(widget, tmp_path):
    """A Reference with a pixel -> world mapping (as a scan recording has):
    the clicks are in Reference pixels, the transform in world units."""
    moving, reference = _images()
    world = AffineTransform.from_linear_offset(np.diag([0.1, 0.1]), [-5.0, 3.0])
    source = CalibrationSource(reference, "scan.h5", AcquisitionContext(detector="APD"), pixel_to_world=world,
                               world_axes=("Y", "X"), world_units="um", world_frame="scanner:galvo",
                               detector="APD", kind="scan recording")
    widget.setSource("target", source)
    widget.setSource("source", CalibrationSource(moving, "cam.h5", AcquisitionContext(detector="Camera"),
                                                 detector="Camera", kind="camera recording"))
    assert widget.selectedCorrespondence() == "manual"        # a user's method is kept
    targets = TRUTH.map_points(SPOTS)
    for k in range(4):
        widget.addManualPoint("moving", SPOTS[k])
        widget.addManualPoint("reference", targets[k])
    transform = widget.transform
    assert transform.units == "um" and transform.target_frame == "scanner:galvo"
    assert transform.source_frame == "detector:Camera"
    assert transform.target_context.extra["axes"] == ["Y", "X"]
    np.testing.assert_allclose(transform.map_points(SPOTS), world.map_points(targets), atol=0.03)
    widget.predict(*SPOTS[0])
    assert "Y = " in widget.predictResultLabel.text() and "µm" in widget.predictResultLabel.text()
    saved = {}

    class Store:
        folder = tmp_path

        def exists(self, calibration_id):
            return calibration_id in saved

        def archive(self, calibration_id):
            saved[calibration_id + "_old"] = saved.pop(calibration_id)
            return calibration_id + "_old"

        def save(self, transform, calibration_id, *, overwrite, report, figure):
            saved[calibration_id] = transform
            return {"record": tmp_path / f"{calibration_id}.json", "figure": figure(tmp_path / "qc.png"),
                    "notes": []}

    widget.setCalibrationStore(Store())
    assert widget.idEdit.text() == "cam_to_scan"
    paths = widget.saveToStore()
    assert saved["cam_to_scan"] is transform and paths["figure"].exists()
    widget.askOverwrite = lambda *args: "keep"
    assert widget.saveToStore() is not None and "cam_to_scan_old" in saved
    assert "kept as" in widget.matrixView.toPlainText()


def test_changing_the_model_or_the_limit_refits_the_clicked_pairs(widget):
    targets = TRUTH.map_points(SPOTS)
    for k in range(4):
        widget.addManualPoint("moving", SPOTS[k])
        widget.addManualPoint("reference", targets[k])
    assert widget.transform.provenance["estimator"] == "similarity"
    widget.setSelectedModel("affine")
    assert widget.transform is not None and widget.transform.provenance["estimator"] == "affine"
    widget.residualLimitSpin.setValue(0.001)         # px: every real fit is above it
    assert widget.transform is None and widget.verdictLabel.text().startswith("Refused")
    widget.residualLimitSpin.setValue(0)
    assert widget.transform is not None
