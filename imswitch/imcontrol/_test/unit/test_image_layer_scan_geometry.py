"""The live layer files a frame's scan geometry with its pixels (plan D1).

``ImageController.update`` reads the geometry a ``ScanFrame`` carries before
applying the detector's display transform, and ``ImageWidget.setImage``
stores it, with that transform and the raw shape, in the layer's metadata in
the same call that sets the layer's pixels -- including when the layer has to
be recreated for a new number of dimensions, which drops its metadata. A
frame without a geometry clears it. A plain array (every frame outside the
SimplePointScan panel) reaches napari exactly as before.
"""
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from imswitch.imcontrol.controller.controllers.ImageController import ImageController
from imswitch.imcontrol.controller.display_transform import DisplayTransform
from imswitch.imcontrol.model.scan_frame import (
    AxisGeometry,
    DisplayedScanGeometry,
    FrameGeometry,
    SCAN_GEOMETRY_METADATA_KEY,
    ScanFrame,
    with_frame_geometry,
)
from imswitch.imcontrol.view.widgets.ImageWidget import ImageWidget

GEOMETRY = FrameGeometry(
    axes=(AxisGeometry('X', 0.5, 4, -1.0), AxisGeometry('Y', 0.5, 3, 2.0)),
    run=1, iteration=4,
)


class _Layer:
    def __init__(self, data):
        self.data = data
        self.scale = (1.0,) * data.ndim
        self.metadata = {}


def _widget(ndisplay=2, data=None):
    widget = ImageWidget.__new__(ImageWidget)
    widget.__dict__.update(
        imgLayers={'APD': _Layer(np.zeros((3, 4)) if data is None else data)},
        napariViewer=SimpleNamespace(dims=SimpleNamespace(ndisplay=ndisplay)),
    )
    return widget


def test_a_scan_frame_files_its_geometry_and_napari_gets_a_plain_view():
    widget = _widget()
    frame = with_frame_geometry(np.arange(12.0).reshape(3, 4), GEOMETRY)
    shown = DisplayedScanGeometry(GEOMETRY, DisplayTransform(), (3, 4))

    widget.setImage('APD', frame, [0.5, 0.5], scanGeometry=shown)

    layer = widget.imgLayers['APD']
    assert layer.metadata[SCAN_GEOMETRY_METADATA_KEY] is shown
    assert widget.getScanGeometry('APD') is shown
    assert not isinstance(layer.data, ScanFrame)
    assert np.shares_memory(layer.data, frame)


def test_a_frame_without_geometry_clears_it():
    widget = _widget()
    shown = DisplayedScanGeometry(GEOMETRY, DisplayTransform(), (3, 4))
    widget.setImage('APD', np.zeros((3, 4)), [0.5, 0.5], scanGeometry=shown)

    widget.setImage('APD', np.ones((3, 4)), [0.5, 0.5])

    assert SCAN_GEOMETRY_METADATA_KEY not in widget.imgLayers['APD'].metadata
    assert widget.getScanGeometry('APD') is None


def test_a_plain_array_reaches_the_layer_as_before():
    """Advanced as today: the same object, no metadata touched."""
    widget = _widget()
    image = np.ones((3, 4))

    widget.setImage('APD', image, [0.5, 0.5])

    layer = widget.imgLayers['APD']
    assert layer.data is image
    assert layer.metadata == {}


def test_the_geometry_survives_a_layer_recreated_for_new_dimensions():
    """_recreateLiveLayer builds a new layer and does not copy metadata."""
    widget = _widget()

    def recreate(name, im, scale):
        widget.imgLayers[name] = _Layer(im)

    widget._recreateLiveLayer = recreate
    volume = with_frame_geometry(np.zeros((2, 3, 4)), GEOMETRY)
    shown = DisplayedScanGeometry(GEOMETRY, DisplayTransform(), (2, 3, 4))

    widget.setImage('APD', volume, [1.0, 0.5, 0.5], scanGeometry=shown)

    assert widget.imgLayers['APD'].metadata[SCAN_GEOMETRY_METADATA_KEY] is shown


def _controller(widget, managerProperties):
    controller = ImageController.__new__(ImageController)
    controller.__dict__.update(
        _widget=widget,
        _setupInfo=SimpleNamespace(detectors={
            'APD': SimpleNamespace(managerProperties=managerProperties),
        }),
        _shouldResetView=False,
        _shownScanGeometry={},
        _commChannel=Mock(),
    )
    controller.autoLevels = Mock()
    controller.adjustFrame = Mock()
    return controller


@pytest.mark.parametrize('properties, transform', [
    ({}, DisplayTransform()),
    ({'displayRotation': 90, 'displayFlipX': True},
     DisplayTransform(rotation=90, flip_x=True)),
])
def test_the_controller_reads_the_geometry_before_the_display_transform(
        properties, transform):
    widget = Mock()
    controller = _controller(widget, properties)
    frame = with_frame_geometry(np.arange(12.0).reshape(3, 4), GEOMETRY)

    controller.update('APD', frame, True, [0.5, 0.5], False)

    _, kwargs = widget.setImage.call_args
    assert kwargs['scanGeometry'] == DisplayedScanGeometry(GEOMETRY, transform, (3, 4))


def test_the_controller_passes_no_geometry_for_a_plain_array():
    widget = Mock()
    controller = _controller(widget, {})
    image = np.arange(12.0).reshape(3, 4)

    controller.update('APD', image, True, [0.5, 0.5], False)

    args, kwargs = widget.setImage.call_args
    assert args[1] is image
    assert kwargs['scanGeometry'] is None


def test_the_shown_geometry_is_announced_once_per_change_after_the_layer_has_it():
    """What a panel mapping viewer points to the scanner re-projects on."""
    widget = Mock()
    controller = _controller(widget, {'displayRotation': 90})
    announce = controller._commChannel.sigScanGeometryShown.emit
    order = []
    widget.setImage.side_effect = lambda *a, **k: order.append('shown')
    announce.side_effect = lambda *a: order.append('announced')
    frame = with_frame_geometry(np.arange(12.0).reshape(3, 4), GEOMETRY)

    for _ in range(3):
        controller.update('APD', frame, True, [0.5, 0.5], False)

    shown = DisplayedScanGeometry(GEOMETRY, DisplayTransform(rotation=90), (3, 4))
    announce.assert_called_once_with('APD', shown)
    assert order[:2] == ['shown', 'announced']

    other = FrameGeometry(axes=GEOMETRY.axes, run=2, iteration=0)
    controller.update('APD', with_frame_geometry(np.zeros((3, 4)), other), True,
                      [0.5, 0.5], False)
    controller.update('APD', np.zeros((3, 4)), True, [0.5, 0.5], False)
    assert [c.args[1] for c in announce.call_args_list[1:]] == [
        DisplayedScanGeometry(other, DisplayTransform(rotation=90), (3, 4)), None,
    ]


def test_frames_without_geometry_announce_nothing():
    """A camera at video rate: no signal per frame."""
    widget = Mock()
    controller = _controller(widget, {})
    for _ in range(5):
        controller.update('APD', np.zeros((3, 4)), True, [0.5, 0.5], False)
    controller._commChannel.sigScanGeometryShown.emit.assert_not_called()


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
