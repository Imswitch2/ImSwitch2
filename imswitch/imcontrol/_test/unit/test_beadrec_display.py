"""The BeadRec image: levelled from its filled pixels, refitted on a new shape,
and given the dock's height rather than its own size hint.

Before: a running reconstruction was drawn with the levels left over from
whatever came before -- the histogram's initial (0, 1), which the greyclip LUT
shows as solid red, or the camera counts of the last ROI crop preview -- the
view kept an old zoom when the image changed size, and a word-wrapped status
label made the panel's scroll area keep the canvas at its ~450 px size hint.
"""
from types import SimpleNamespace

import numpy as np
import pytest
from qtpy import QtWidgets

from imswitch.imcontrol.view.widgets.basewidgets import WidgetFactory
from imswitch.imcontrol.view.widgets.BeadRecWidget import (
    BeadRecWidget, displayLevels,
)


pytestmark = pytest.mark.nohardware


def _beads(ny=60, nx=60, background=100.0, peak=60.0):
    yy, xx = np.mgrid[0:ny, 0:nx]
    im = np.full((ny, nx), background)
    for cy, cx in ((ny * 0.3, nx * 0.3), (ny * 0.7, nx * 0.6)):
        im += peak * np.exp(-((yy - cy) ** 2 + (xx - cx) ** 2) / 8.0)
    return im


def _partial(image, rows):
    partial = np.zeros_like(image)
    partial[:rows] = image[:rows]
    return partial


# -- the levels -----------------------------------------------------------------

def test_unfilled_pixels_do_not_pull_the_lower_level_to_zero():
    image = _beads()
    low, high = displayLevels(_partial(image, 10))
    assert low > 95          # the background of the filled rows, not 0
    assert high >= image[:10].max()


def test_a_full_image_spans_background_to_brightest_pixel_without_clipping_it():
    image = _beads()
    low, high = displayLevels(image)
    assert image.min() <= low < 101
    assert high > image.max()          # the peak is not drawn as clipped


def test_degenerate_images_get_usable_levels():
    assert displayLevels(np.zeros((4, 4))) == (0.0, 1.0)
    assert displayLevels(np.full((4, 4), np.nan)) == (0.0, 1.0)
    low, high = displayLevels(np.full((4, 4), 7.0))
    assert low < 7.0 < high
    with_nan = _beads()
    with_nan[0, 0] = np.nan
    assert np.all(np.isfinite(displayLevels(with_nan)))


# -- the widget -----------------------------------------------------------------

@pytest.fixture
def widget(qtbot):
    widget = WidgetFactory(SimpleNamespace()).createWidget(BeadRecWidget)
    qtbot.addWidget(widget)
    widget.resize(560, 420)
    widget.show()
    QtWidgets.QApplication.processEvents()
    return widget


def _levels(widget):
    return tuple(float(v) for v in widget.img.getLevels())


def test_a_reconstruction_update_is_levelled_from_its_filled_pixels(widget):
    image = _beads()
    widget.updateImage(_partial(image, 10))          # as BeadRecController.update does
    low, high = _levels(widget)
    assert low > 95 and high >= image[:10].max()


def test_reconstruction_levels_do_not_inherit_the_crop_preview_counts(widget):
    widget.updateImage(np.full((20, 20), 1500.0) + np.arange(400).reshape(20, 20),
                       autoLevels=True)               # live ROI crop preview
    widget.updateImage(_beads())
    low, high = _levels(widget)
    assert high < 200


def test_hold_levels_keeps_the_levels_and_false_always_does(widget):
    widget.updateImage(_beads())
    widget.img.setLevels((90.0, 120.0))               # the user's histogram
    widget.holdLevelsCheck.setChecked(True)
    widget.updateImage(_beads(peak=200.0))
    assert _levels(widget) == (90.0, 120.0)
    widget.holdLevelsCheck.setChecked(False)
    widget.updateImage(_beads(peak=200.0), autoLevels=False)
    assert _levels(widget) == (90.0, 120.0)
    widget.updateImage(_beads(peak=200.0))
    assert _levels(widget)[1] > 290


def test_a_new_shape_fits_the_view_again_after_a_zoom(widget):
    widget.updateImage(_beads(60, 60))
    widget.vb.setRange(xRange=(10, 20), yRange=(10, 20))   # a rectangle zoom
    widget.updateImage(_beads(10, 120))
    (x0, x1), (y0, y1) = widget.vb.viewRange()
    assert x0 <= 0 and x1 >= 119


def test_the_same_shape_keeps_the_users_zoom(widget):
    widget.updateImage(_beads(60, 60))
    widget.vb.setRange(xRange=(10, 20), yRange=(10, 20), padding=0)
    widget.updateImage(_beads(60, 60, peak=80.0))
    (x0, x1), _ = widget.vb.viewRange()
    assert x1 - x0 < 30


def test_the_canvas_follows_the_dock_height_instead_of_its_size_hint(widget, qtbot):
    content = widget._panelScrollContent
    assert not content.layout().hasHeightForWidth()
    widget.resize(560, 420)
    QtWidgets.QApplication.processEvents()
    shorter = widget.cwidget.height()
    widget.resize(560, 700)
    QtWidgets.QApplication.processEvents()
    assert widget.cwidget.height() > shorter + 200
    # ...and fits the panel: no vertical scrolling at a usable height.
    widget.resize(560, 420)
    QtWidgets.QApplication.processEvents()
    assert content.height() <= widget._panelScrollArea.viewport().height()


def test_a_very_short_dock_scrolls_but_keeps_a_usable_canvas(widget):
    widget.resize(560, 200)
    QtWidgets.QApplication.processEvents()
    assert widget.cwidget.height() >= BeadRecWidget.MINIMUM_CANVAS_HEIGHT


def test_long_status_text_still_wraps_and_shows_in_full(widget):
    text = ('BeadRec: 0 detector frames received during the scan - check camera '
            'triggering (e.g. external-trigger TTL never pulsed).')
    widget.setStatusText(text)
    QtWidgets.QApplication.processEvents()
    label = widget.statusLabel
    assert label.wordWrap()
    assert label.height() >= label.heightForWidth(label.width())
