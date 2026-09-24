"""Setting the frame range for new data must not navigate.

Review round 7: switching from a dataset shown at frame 350 to a 300-frame
one clamped the slider, and the clamp emitted ``valueChanged`` like a drag.
The controller then read plane 299 of the new data -- behind a mean preview
that had just decided to show nothing, so a non-lazy TIFF was decoded whole
-- and replaced the mean on screen with that plane.
"""
import pytest
from qtpy import QtWidgets

_APP = None


@pytest.fixture(scope="module")
def qapp():
    # Held at module level: dropping the last QApplication reference deletes
    # every Python-owned QObject (see the PyQt5 teardown note in the repo docs).
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _record(*signals):
    seen = []
    for signal in signals:
        signal.connect(lambda value, s=signal: seen.append(value))
    return seen


def test_the_current_data_panel_clamps_its_slider_without_navigating(qapp):
    from imswitch.improcess.view.DataFrame import DataFrame

    widget = DataFrame()
    widget.setNumFrames(400)
    widget.setCurrentFrame(350)
    seen = _record(widget.sigFrameSliderChanged, widget.sigFrameNumberChanged)

    widget.setNumFrames(300)                      # the new dataset

    assert seen == []                             # no plane was asked for
    assert widget.slider.value() == 299           # clamped, as Qt does
    assert widget.frameNum.text() == '299'        # and the field agrees
    assert widget.numFrames.text() == '300'

    widget.slider.setValue(10)                    # a real drag still navigates
    assert seen and seen[-1] == 10


def test_the_edit_window_clamps_its_slider_without_navigating(qapp):
    from imswitch.improcess.view.DataEditDialog import DataEditDialog

    dialog = DataEditDialog(None)
    dialog.updateDataProperties('a', 'CAM', 400)
    dialog.slider.setValue(350)
    seen = _record(dialog.sigImageSliceChanged)

    dialog.updateDataProperties('b', 'CAM', 300)

    assert seen == []
    assert dialog.slider.value() == 299
    assert dialog.frameNum.text() == '299'

    dialog.slider.setValue(10)
    assert seen and seen[-1] == 10
