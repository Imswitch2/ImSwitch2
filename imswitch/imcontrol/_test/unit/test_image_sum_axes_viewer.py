"""The Sum panel in a real napari viewer (scan timing plan, S6): hidden
until a live frame has a dimension beyond its image, one box per such
dimension, and checking it puts the sum on the live layer -- the layer
rebuilt at the lower dimensionality and levelled anew.

The rest of the Sum tests drive the widget and the controller headless;
this one exists because their two halves once keyed the dimensions
differently (detector name against napari layer name) and every headless
test still passed.

**Deselected by default** (``glviewer``): a live GL context does not
survive alongside the rest of the suite in one process. Run on a real
display, not ``QT_QPA_PLATFORM=offscreen``::

    pytest -m glviewer imswitch/imcontrol/_test/unit/test_image_sum_axes_viewer.py
"""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pytest

pytest.importorskip("qtpy.QtWidgets")

pytestmark = pytest.mark.glviewer


@pytest.fixture
def live(qtbot):
    from imswitch.imcontrol.controller.controllers.ImageController import ImageController
    from imswitch.imcontrol.view.widgets.ImageWidget import ImageWidget

    widget = ImageWidget()
    widget.setLiveViewLayers(['APD'])
    master = Mock()
    master.detectorsManager.hasDevices.return_value = True
    master.detectorsManager.execOnCurrent.return_value = (50, 50)
    master.detectorsManager.getAllDeviceNames.return_value = ['APD']
    master.detectorsManager.__getitem__ = Mock(
        return_value=SimpleNamespace(displayAxisNames=('line steps', 'y', 'x')))
    with patch('imswitch.imcontrol.controller.controllers.ImageController.'
               'getWidgetStatePersistence', return_value=Mock()):
        controller = ImageController(Mock(), Mock(), master, widget=widget,
                                     factory=Mock(), moduleCommChannel=Mock())
    controller._setupInfo = SimpleNamespace(detectors={})
    yield widget, controller
    widget.napariViewer.close()


def test_the_sum_box_appears_with_line_steps_and_sums_the_live_layer(live, qtbot):
    widget, controller = live
    panel = widget.sumAxesWidget
    dock = panel._dock()
    assert dock is not None and dock.isHidden()                   # nothing to sum yet
    frame = np.random.default_rng(0).poisson(5, (3, 50, 50)).astype(np.uint16)
    controller.update('APD', frame, False, [1.0, 0.1, 0.1], True)
    assert not dock.isHidden()
    assert [box.text() for box in panel._boxes.values()] == ['Sum line steps']
    assert widget.imgLayers['APD'].data.shape == (3, 50, 50)

    panel._boxes['line steps'].setChecked(True)
    layer = widget.imgLayers['APD']
    np.testing.assert_array_equal(layer.data, frame.sum(axis=0))
    assert layer.contrast_limits[1] >= frame.sum(axis=0).max()   # levelled for the sum
    controller.update('APD', frame, True, [1.0, 0.1, 0.1], True)  # the next frame: summed too
    assert widget.imgLayers['APD'].data.shape == (50, 50)

    panel._boxes['line steps'].setChecked(False)
    assert widget.imgLayers['APD'].data.shape == (3, 50, 50)
    controller.update('APD', frame[0], True, [0.1, 0.1], True)    # a 2-D scan: panel gone
    assert dock.isHidden()
