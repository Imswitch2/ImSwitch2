"""The Profile panel can plot a named ROI, not only a freshly drawn shape.

The panel's own shapes are transient scratch. An ROI in the manager is named,
saved, and re-measurable across reconstructions -- which is what a profile is
usually wanted for -- so it is offered as a source alongside "Drawn".
"""

import numpy as np
import pytest

pytest.importorskip("qtpy")
from qtpy import QtWidgets  # noqa: E402

from imswitch.imcommon.algorithms.roi import ROIRecord  # noqa: E402
from imswitch.improcess.view.ProfileWidget import ProfileWidget  # noqa: E402

from .test_roi_manager_widget_p0 import _Viewer  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _image():
    """A field with a bright band down the middle rows, so a profile taken
    across it has an obvious shape and one taken along it does not."""
    image = np.zeros((32, 32), dtype=float)
    image[14:18, :] = 100.0
    return image


class _ROIPanel:
    def __init__(self, rois):
        self._rois = list(rois)

    def rois(self):
        return list(self._rois)


def _rois():
    return [
        ROIRecord("box", "rectangle", (10, 22, 4, 28), uid="u1"),
        ROIRecord(
            "cut", "line", (8, 24, 16, 16), uid="u2",
            vertices=((8.0, 16.0), (24.0, 16.0)),
        ),
        ROIRecord("blob", "ellipse", (10, 22, 4, 28), uid="u3",
                  vertices=((10.0, 4.0), (10.0, 28.0), (22.0, 28.0), (22.0, 4.0))),
    ]


@pytest.fixture
def panel(qapp):
    widget = ProfileWidget(_Viewer(_image()))
    widget._roiManagerWidget = _ROIPanel(_rois())
    widget.refreshProfileSources()
    yield widget
    widget.deleteLater()


def _choose(panel, name):
    for index in range(panel.sourceCombo.count()):
        if panel.sourceCombo.itemText(index).startswith(name):
            panel.sourceCombo.setCurrentIndex(index)
            return
    raise AssertionError(f"{name!r} is not offered")


# --------------------------------------------------------------------------
# choosing a source
# --------------------------------------------------------------------------

def test_drawn_is_the_default(panel):
    """The panel must behave exactly as before until a source is chosen."""
    assert panel.sourceCombo.currentText() == ProfileWidget.DRAWN
    assert panel.selectedROI() is None


def test_every_visible_roi_is_offered(panel):
    labels = [panel.sourceCombo.itemText(i) for i in range(panel.sourceCombo.count())]
    assert labels == ["Drawn", "box (rectangle)", "cut (line)", "blob (ellipse)"]


def test_a_hidden_roi_is_not_offered(qapp):
    hidden = ROIRecord("gone", "rectangle", (0, 4, 0, 4), uid="u9", visible=False)
    widget = ProfileWidget(_Viewer(_image()))
    try:
        widget._roiManagerWidget = _ROIPanel([hidden])
        widget.refreshProfileSources()
        assert widget.sourceCombo.count() == 1      # Drawn only
    finally:
        widget.deleteLater()


def test_the_panel_works_without_an_roi_manager(qapp):
    """The manager is wired in late and may never arrive."""
    widget = ProfileWidget(_Viewer(_image()))
    try:
        widget.refreshProfileSources()
        assert widget.sourceCombo.count() == 1
    finally:
        widget.deleteLater()


# --------------------------------------------------------------------------
# what each shape plots
# --------------------------------------------------------------------------

def test_a_line_roi_plots_its_profile(panel):
    _choose(panel, "cut")
    (name, x, values), = panel._last_payload

    assert name == "line"
    # The line runs down column 16 through the bright band: dark, bright, dark.
    assert values.max() == pytest.approx(100.0)
    assert values[0] == pytest.approx(0.0)
    assert x.size == values.size


def test_a_line_roi_offers_no_plot_choice(panel):
    """There is only one thing a line can be; a chooser would be a decision
    with one option."""
    _choose(panel, "cut")
    assert not panel.roiPlotCombo.isVisibleTo(panel)


def test_an_area_roi_offers_the_plot_choice(panel):
    _choose(panel, "box")
    assert panel.roiPlotCombo.isVisibleTo(panel)


def test_an_area_roi_plots_its_outline(panel):
    _choose(panel, "box")
    panel.roiPlotCombo.setCurrentIndex(0)           # Outline
    (name, _x, values), = panel._last_payload

    assert name == "outline"
    # The outline crosses the bright band on both vertical sides.
    assert values.max() == pytest.approx(100.0)
    assert values.min() == pytest.approx(0.0)


def test_an_area_roi_plots_the_mean_along_each_axis(panel):
    _choose(panel, "box")

    panel.roiPlotCombo.setCurrentIndex(1)           # Mean along X
    (name_x, _x, along_x), = panel._last_payload
    panel.roiPlotCombo.setCurrentIndex(2)           # Mean along Y
    (name_y, _y, along_y), = panel._last_payload

    assert (name_x, name_y) == ("mean x", "mean y")
    # Averaging over rows: every column crosses the band equally, so the
    # profile is flat. Averaging over columns: only the band rows are bright.
    assert along_x.std() == pytest.approx(0.0, abs=1e-9)
    assert along_y.max() == pytest.approx(100.0)
    assert along_y.min() == pytest.approx(0.0)


def test_the_mean_uses_the_rois_own_pixels_not_its_box(panel):
    """For anything but a rectangle those differ, and the box is the one
    nobody asked for. The signal is put in a corner of the box, which the
    inscribed ellipse does not reach."""
    corner = _image()
    corner[10:13, 4:9] = 500.0
    panel._viewer.layers[0].data = corner

    _choose(panel, "blob")
    panel.roiPlotCombo.setCurrentIndex(2)           # Mean along Y
    (_name, _x, ellipse), = panel._last_payload

    _choose(panel, "box")                            # same bounds, full box
    panel.roiPlotCombo.setCurrentIndex(2)
    (_name, _x, rectangle), = panel._last_payload

    assert ellipse.shape == rectangle.shape
    assert not np.allclose(ellipse, rectangle)


# --------------------------------------------------------------------------
# living alongside the drawn mode
# --------------------------------------------------------------------------

def test_going_back_to_drawn_stops_plotting_the_roi(panel):
    _choose(panel, "cut")
    assert panel._last_payload

    panel.sourceCombo.setCurrentIndex(0)

    assert panel.selectedROI() is None


def test_changing_the_result_re_plots_the_same_roi(panel):
    """An ROI is worth having as a source precisely because it can be measured
    against one reconstruction after another."""
    _choose(panel, "cut")
    first = panel._last_payload[0][2].copy()

    panel._viewer.layers[0].data = _image() * 2.0
    panel.setCurrentResult(object())

    assert panel._last_payload[0][2].max() == pytest.approx(2 * first.max())


def test_an_roi_off_the_image_reports_rather_than_raising(panel):
    """A saved ROI measured against a smaller result must not take the panel
    down."""
    panel._roiManagerWidget = _ROIPanel(
        [ROIRecord("far", "rectangle", (900, 950, 900, 950), uid="u8")]
    )
    panel.refreshProfileSources()
    _choose(panel, "far")

    assert panel._last_payload == []
    assert panel.fitSummary.text()
