"""Cropping offers the same thing however it is reached.

Reported from the rig: the toolbar's Crop/Substack could take its rectangle
from an ROI and the panel could not. They were two implementations of one
feature -- a per-axis table in the dialog, a ``Z=0:10,T=0:5:2`` text field in
the panel -- so a feature added to one simply was not in the other. There is
one now, hosted by both.
"""

from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("qtpy")
from qtpy import QtWidgets  # noqa: E402

from imswitch.imcommon.algorithms.roi import ROIRecord  # noqa: E402
from imswitch.improcess.processors.stack_subset.processor import (  # noqa: E402
    StackSubsetProcessor,
)
from imswitch.improcess.view.StackSubsetDialog import (  # noqa: E402
    StackSubsetDialog,
    StackSubsetRangesWidget,
)


@pytest.fixture(scope="module")
def qapp():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _result(shape=(5, 64, 64), labels=("Z", "Y", "X")):
    return SimpleNamespace(data=np.zeros(shape), axis_labels=list(labels))


def _rois():
    return [
        ROIRecord("cell", "rectangle", (10, 30, 20, 45), uid="u1"),
        ROIRecord("spot", "ellipse", (2, 12, 4, 14), uid="u2"),
        # No extent: a crop is a rectangle, so these must not be offered.
        ROIRecord("edge", "line", (0, 1, 0, 5), uid="u3",
                  vertices=((0.0, 0.0), (0.0, 5.0))),
    ]


# --------------------------------------------------------------------------
# the panel is the dialog
# --------------------------------------------------------------------------

def test_the_processor_panel_uses_the_shared_range_controls(qapp):
    widget = StackSubsetProcessor().make_param_widget(None)
    try:
        assert isinstance(widget, StackSubsetRangesWidget)
        assert hasattr(widget, "roiCombo")
        assert callable(widget.get_values)
    finally:
        widget.deleteLater()


def test_both_hosts_produce_the_same_params(qapp):
    """The dialog and the panel widget, given the same input and the same ROI,
    must ask for the same crop."""
    dialog = StackSubsetDialog(_result(), rois=_rois())
    panel = StackSubsetRangesWidget(_result(), rois=_rois())
    try:
        dialog.roiCombo.setCurrentIndex(1)
        panel.roiCombo.setCurrentIndex(1)
        assert dialog.selected_params() == panel.get_values()
    finally:
        dialog.deleteLater()
        panel.deleteLater()


def test_the_dialog_answers_from_the_widget(qapp):
    """One implementation means the shell cannot disagree with its contents."""
    dialog = StackSubsetDialog(_result(), rois=_rois())
    try:
        assert dialog.roiCombo is dialog.ranges.roiCombo
        assert dialog.MANUAL == StackSubsetRangesWidget.MANUAL
    finally:
        dialog.deleteLater()


# --------------------------------------------------------------------------
# retargeting: the panel's input changes under it
# --------------------------------------------------------------------------

def test_the_table_follows_the_selected_result(qapp):
    widget = StackSubsetRangesWidget(_result((5, 64, 64), ("Z", "Y", "X")))
    try:
        assert [row.size for row in widget._rows] == [5, 64, 64]

        widget.setResult(_result((3, 8), ("T", "X")))

        assert [row.size for row in widget._rows] == [3, 8]
    finally:
        widget.deleteLater()


def test_ranges_typed_for_the_previous_result_do_not_survive(qapp):
    """Keeping them is how a crop silently applies to the wrong extent."""
    widget = StackSubsetRangesWidget(_result((5, 64, 64)))
    try:
        widget._rows[1].firstSpin.setValue(40)
        widget.setResult(_result((5, 16, 16)))

        assert widget.get_values()["ranges"] == []
    finally:
        widget.deleteLater()


def test_retargeting_re_offers_the_rois(qapp):
    widget = StackSubsetRangesWidget(_result())
    try:
        assert not widget.roiCombo.isEnabled()

        widget.setResult(_result(), _rois())

        assert widget.roiCombo.isEnabled()
        assert widget.roiCombo.count() == 3      # Manual + the two area ROIs
    finally:
        widget.deleteLater()


def test_a_result_of_none_empties_the_table(qapp):
    """The panel shows the processor before a result is chosen."""
    widget = StackSubsetRangesWidget(_result())
    try:
        widget.setResult(None)
        assert widget._rows == []
        assert widget.get_values()["ranges"] == []
    finally:
        widget.deleteLater()


# --------------------------------------------------------------------------
# only ROIs that can be cropped to
# --------------------------------------------------------------------------

def test_a_line_is_not_offered_as_a_crop_source(qapp):
    widget = StackSubsetRangesWidget(_result(), rois=_rois())
    try:
        offered = [widget.roiCombo.itemText(i) for i in range(widget.roiCombo.count())]
        assert offered == ["Manual", "cell (rectangle)", "spot (ellipse)"]
    finally:
        widget.deleteLater()


def test_an_ellipse_crops_to_its_box(qapp):
    """A crop is rectangular; the box is the honest answer for any shape."""
    widget = StackSubsetRangesWidget(_result(), rois=_rois())
    try:
        widget.roiCombo.setCurrentIndex(2)          # "spot", bounds (2,12,4,14)
        ranges = {r["axis"]: r for r in widget.get_values()["ranges"]}
        assert ranges[1]["start"] == 2 and ranges[1]["stop"] == 12
        assert ranges[2]["start"] == 4 and ranges[2]["stop"] == 14
    finally:
        widget.deleteLater()


def test_the_chosen_roi_is_recorded_for_the_footprint(qapp):
    widget = StackSubsetRangesWidget(_result(), rois=_rois())
    try:
        widget.roiCombo.setCurrentIndex(1)
        values = widget.get_values()
        assert values["roi_name"] == "cell"
        assert values["roi_uid"] == "u1"
    finally:
        widget.deleteLater()
