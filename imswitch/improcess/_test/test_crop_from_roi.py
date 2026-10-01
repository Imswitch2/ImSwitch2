"""Cropping from an ROI drawn in the ROI manager.

The ranges in the dialog stay the single source of truth: an ROI *fills* them
rather than replacing them, so what is applied is always what is on screen.
"""

from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("qtpy")
from qtpy import QtWidgets  # noqa: E402

from imswitch.imcommon.algorithms.roi import ROIRecord  # noqa: E402
from imswitch.imcommon.algorithms.roi_geometry import roi_from_points  # noqa: E402
from imswitch.improcess.view.StackSubsetDialog import StackSubsetDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def _result(shape=(5, 64, 64), labels=("Z", "Y", "X")):
    return SimpleNamespace(data=np.zeros(shape), axis_labels=list(labels))


def _rois():
    return [
        ROIRecord("cell", "rectangle", (10, 30, 20, 45), uid="u1"),
        # The declared bounds agree with the vertices: roi_bounds() derives
        # the box from the geometry, which is the point — a declared box that
        # disagreed would be the less trustworthy of the two.
        ROIRecord(
            "blob", "polygon", (5, 15, 6, 16), uid="u2",
            vertices=((5.0, 6.0), (5.0, 16.0), (15.0, 16.0)),
        ),
    ]


def _dialog(qapp, **kwargs):
    return StackSubsetDialog(kwargs.pop("result", _result()), **kwargs)


# --------------------------------------------------------------------------
# choosing a source
# --------------------------------------------------------------------------

def test_manual_is_the_default_and_the_dialog_is_unchanged_without_rois(qapp):
    dialog = _dialog(qapp)
    try:
        assert dialog.roiCombo.currentText() == StackSubsetDialog.MANUAL
        assert not dialog.roiCombo.isEnabled()
        assert dialog.selected_params()["ranges"] == []
    finally:
        dialog.deleteLater()


def test_the_rois_are_offered_by_name_and_type(qapp):
    dialog = _dialog(qapp, rois=_rois())
    try:
        labels = [dialog.roiCombo.itemText(i) for i in range(dialog.roiCombo.count())]
        assert labels == ["Manual", "cell (rectangle)", "blob (polygon)"]
    finally:
        dialog.deleteLater()


def test_choosing_an_roi_fills_the_y_and_x_ranges(qapp):
    dialog = _dialog(qapp, rois=_rois())
    try:
        dialog.roiCombo.setCurrentIndex(1)          # "cell", bounds (10,30,20,45)
        ranges = {r["axis"]: r for r in dialog.selected_params()["ranges"]}

        assert ranges[1]["start"] == 10 and ranges[1]["stop"] == 30   # Y
        assert ranges[2]["start"] == 20 and ranges[2]["stop"] == 45   # X
        assert 0 not in ranges                                        # Z untouched
    finally:
        dialog.deleteLater()


def test_the_crop_records_which_roi_it_came_from(qapp):
    dialog = _dialog(qapp, rois=_rois())
    try:
        dialog.roiCombo.setCurrentIndex(1)
        params = dialog.selected_params()
        assert params["roi_uid"] == "u1"
        assert params["roi_name"] == "cell"
    finally:
        dialog.deleteLater()


def test_a_manual_crop_records_no_roi(qapp):
    """Absence means "typed by hand", not "unknown"."""
    dialog = _dialog(qapp, rois=_rois())
    try:
        assert "roi_uid" not in dialog.selected_params()
    finally:
        dialog.deleteLater()


# --------------------------------------------------------------------------
# the ranges stay the source of truth
# --------------------------------------------------------------------------

def test_editing_a_range_returns_the_source_to_manual(qapp):
    """What is applied is what is on screen; the ROI only filled it in."""
    dialog = _dialog(qapp, rois=_rois())
    try:
        dialog.roiCombo.setCurrentIndex(1)
        dialog._rows[1].firstSpin.setValue(12)

        assert dialog.roiCombo.currentText() == StackSubsetDialog.MANUAL
        assert "roi_uid" not in dialog.selected_params()
        # ...and the edit is what is applied.
        ranges = {r["axis"]: r for r in dialog.selected_params()["ranges"]}
        assert ranges[1]["start"] == 11
    finally:
        dialog.deleteLater()


def test_reset_clears_the_roi_as_well_as_the_ranges(qapp):
    dialog = _dialog(qapp, rois=_rois())
    try:
        dialog.roiCombo.setCurrentIndex(1)
        dialog.resetRanges()

        assert dialog.roiCombo.currentText() == StackSubsetDialog.MANUAL
        assert dialog.selected_params()["ranges"] == []
    finally:
        dialog.deleteLater()


def test_switching_between_two_rois_replaces_the_ranges(qapp):
    dialog = _dialog(qapp, rois=_rois())
    try:
        dialog.roiCombo.setCurrentIndex(1)
        dialog.roiCombo.setCurrentIndex(2)          # "blob", bounds (5,15,6,16)
        ranges = {r["axis"]: r for r in dialog.selected_params()["ranges"]}
        assert ranges[1]["start"] == 5 and ranges[1]["stop"] == 15
        assert dialog.selected_params()["roi_name"] == "blob"
    finally:
        dialog.deleteLater()


# --------------------------------------------------------------------------
# the awkward cases
# --------------------------------------------------------------------------

def test_the_axes_are_found_by_label_not_by_position(qapp):
    """A YZ view's ranges must not be filled from an ROI drawn on YX."""
    dialog = _dialog(
        qapp, result=_result((64, 5, 64), ("Y", "Z", "X")), rois=_rois()
    )
    try:
        dialog.roiCombo.setCurrentIndex(1)
        ranges = {r["axis"]: r for r in dialog.selected_params()["ranges"]}
        assert ranges[0]["start"] == 10      # Y is axis 0 here
        assert ranges[2]["start"] == 20      # X is axis 2
        assert 1 not in ranges               # Z untouched
    finally:
        dialog.deleteLater()


def test_an_roi_larger_than_the_image_is_clipped_not_refused(qapp):
    dialog = _dialog(
        qapp,
        result=_result((5, 16, 16)),
        rois=[ROIRecord("big", "rectangle", (2, 900, 3, 900), uid="u3")],
    )
    try:
        dialog.roiCombo.setCurrentIndex(1)
        ranges = {r["axis"]: r for r in dialog.selected_params()["ranges"]}
        assert ranges[1]["start"] == 2 and ranges[1]["stop"] == 16
        assert ranges[2]["start"] == 3 and ranges[2]["stop"] == 16
    finally:
        dialog.deleteLater()


def test_a_polygon_crops_to_its_box_which_is_what_a_crop_is(qapp):
    """A crop is rectangular; taking the box is honest, pretending the shape
    was applied is not."""
    dialog = _dialog(qapp, rois=_rois())
    try:
        dialog.roiCombo.setCurrentIndex(2)
        ranges = {r["axis"]: r for r in dialog.selected_params()["ranges"]}
        assert (ranges[1]["stop"] - ranges[1]["start"]) == 10
        assert (ranges[2]["stop"] - ranges[2]["start"]) == 10
    finally:
        dialog.deleteLater()


def test_a_result_with_no_plane_axes_is_left_alone(qapp):
    dialog = _dialog(
        qapp, result=_result((10, 4), ("T", "C")), rois=_rois()
    )
    try:
        assert dialog.applyROI(_rois()[0]) is False
        assert dialog.selected_params()["ranges"] == []
    finally:
        dialog.deleteLater()


# --------------------------------------------------------------------------
# what the toolbar offers
# --------------------------------------------------------------------------

def test_only_ROIs_with_an_extent_are_offered_for_cropping():
    """A line or a point has no rectangle to crop to."""
    from imswitch.imcommon.algorithms.roi_geometry import roi_capabilities

    candidates = [
        ROIRecord("box", "rectangle", (0, 4, 0, 4)),
        ROIRecord("line", "line", (0, 1, 0, 5), vertices=((0.0, 0.0), (0.0, 5.0))),
        roi_from_points([[2.0, 3.0]], name="dot"),
    ]
    offered = [roi for roi in candidates if roi_capabilities(roi.roi_type).is_area]
    assert [roi.name for roi in offered] == ["box"]
