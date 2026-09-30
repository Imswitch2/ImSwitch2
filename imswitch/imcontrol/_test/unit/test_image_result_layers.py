"""Result layers in the main viewer: created once, then updated in place.

Real napari layers are not constructed here (see test_live_view_rendering in
ImProcess for why); the shipping ``ImageWidget`` methods are bound to a stub
whose viewer records what a napari viewer would be asked to do, and the
``ImageController`` relay runs against a mocked widget.
"""

from unittest.mock import Mock, patch

import numpy as np
import pytest
from qtpy import QtCore

from imswitch.imcontrol.controller.controllers.ImageController import ImageController
from imswitch.imcontrol.view.widgets.ImageWidget import ImageWidget


class _FakeLayer:
    def __init__(self, data, kwargs, layerType):
        self.data = data
        self.kwargs = dict(kwargs)
        self.layerType = layerType
        self.name = kwargs.get('name')
        self.scale = tuple(kwargs.get('scale') or (1.0,) * int(np.ndim(data)))
        self.protected = False
        self.dataWrites = 0

    @property
    def ndim(self):
        return int(np.ndim(self.data)) if self.layerType != 'points' else int(self.data.shape[1])

    def __setattr__(self, key, value):
        if key == 'data' and 'data' in self.__dict__:
            self.__dict__['dataWrites'] = self.__dict__.get('dataWrites', 0) + 1
        object.__setattr__(self, key, value)


class _FakeViewer:
    def __init__(self):
        self.layers = []
        self.added = []

    def _add(self, layerType):
        def add(data, **kwargs):
            layer = _FakeLayer(data, kwargs, layerType)
            self.layers.append(layer)
            self.added.append(layer)
            return layer
        return add

    def __getattr__(self, name):
        if name.startswith('add_'):
            return self._add(name[4:])
        raise AttributeError(name)


class _WidgetStub:
    setResultLayers = ImageWidget.setResultLayers
    removeResultLayers = ImageWidget.removeResultLayers
    resultLayerNames = ImageWidget.resultLayerNames
    _canUpdateInPlace = staticmethod(ImageWidget._canUpdateInPlace)
    _addResultLayer = ImageWidget._addResultLayer
    _removeResultLayer = ImageWidget._removeResultLayer
    _fitScale = staticmethod(ImageWidget._fitScale)
    _RESULT_LAYER_ADDERS = ImageWidget._RESULT_LAYER_ADDERS

    def __init__(self):
        self.napariViewer = _FakeViewer()
        self.resultLayers = {}


def _image(value, shape=(4, 5)):
    return np.full(shape, value, dtype=np.float32)


def test_first_update_creates_the_layer_and_later_ones_swap_its_data():
    widget = _WidgetStub()

    created = widget.setResultLayers(
        'job', [(_image(1), {'name': 'Recon: a', 'scale': (0.1, 0.2)}, 'image')]
    )
    assert created is True
    layer = widget.napariViewer.layers[0]
    assert layer.name == 'Recon: a'
    assert layer.kwargs['blending'] == 'additive'
    assert layer.scale == (0.1, 0.2)

    created = widget.setResultLayers(
        'job', [(_image(2), {'name': 'Recon: a', 'scale': (0.1, 0.2)}, 'image')]
    )

    assert created is False
    assert len(widget.napariViewer.added) == 1
    assert widget.napariViewer.layers == [layer]
    assert float(layer.data[0, 0]) == 2.0
    assert layer.dataWrites == 1
    assert widget.resultLayerNames('job') == ['Recon: a']


def test_a_changed_ndim_recreates_the_layer():
    widget = _WidgetStub()
    widget.setResultLayers('job', [(_image(1), {'name': 'a'}, 'image')])
    first = widget.napariViewer.layers[0]

    widget.setResultLayers('job', [(np.zeros((3, 4, 5)), {'name': 'a', 'scale': (2.0, 0.1, 0.2)}, 'image')])

    assert first not in widget.napariViewer.layers
    second = widget.napariViewer.layers[0]
    assert second is not first
    assert second.data.shape == (3, 4, 5)
    assert second.scale == (2.0, 0.1, 0.2)


def test_scale_is_fitted_to_the_layer_dims():
    widget = _WidgetStub()
    widget.setResultLayers('job', [(_image(1), {'name': 'a', 'scale': (9.0, 0.1, 0.2)}, 'image')])
    assert widget.napariViewer.layers[0].scale == (0.1, 0.2)

    widget.setResultLayers('job', [(_image(1), {'name': 'a', 'scale': (0.3,)}, 'image')])
    assert widget.napariViewer.layers[0].scale == (1.0, 0.3)


def test_layers_the_job_stopped_publishing_are_removed():
    widget = _WidgetStub()
    widget.setResultLayers('job', [
        (_image(1), {'name': 'signal'}, 'image'),
        (_image(2), {'name': 'background'}, 'image'),
    ])
    assert widget.resultLayerNames('job') == ['signal', 'background']

    widget.setResultLayers('job', [(_image(3), {'name': 'signal'}, 'image')])

    assert widget.resultLayerNames('job') == ['signal']
    assert [layer.name for layer in widget.napariViewer.layers] == ['signal']


def test_a_layer_the_user_deleted_comes_back_on_the_next_update():
    widget = _WidgetStub()
    widget.setResultLayers('job', [(_image(1), {'name': 'a'}, 'image')])
    widget.napariViewer.layers.clear()          # deleted from the layer list

    widget.setResultLayers('job', [(_image(2), {'name': 'a'}, 'image')])

    assert len(widget.napariViewer.layers) == 1
    assert float(widget.napariViewer.layers[0].data[0, 0]) == 2.0


def test_jobs_are_independent_and_removable():
    widget = _WidgetStub()
    widget.setResultLayers('one', [(_image(1), {'name': 'a'}, 'image')])
    widget.setResultLayers('two', [(_image(2), {'name': 'a'}, 'image')])
    assert len(widget.napariViewer.layers) == 2

    widget.removeResultLayers('one')

    assert widget.resultLayerNames('one') == []
    assert widget.resultLayerNames('two') == ['a']
    assert len(widget.napariViewer.layers) == 1
    widget.removeResultLayers('never')          # unknown job: no error


def test_points_layers_update_in_place_when_the_dimensionality_matches():
    widget = _WidgetStub()
    coords = np.array([[1.0, 2.0], [3.0, 4.0]])
    widget.setResultLayers('job', [(coords, {'name': 'locs', 'scale': (0.1, 0.1)}, 'points')])
    layer = widget.napariViewer.layers[0]

    widget.setResultLayers('job', [(np.array([[5.0, 6.0]]), {'name': 'locs'}, 'points')])
    assert widget.napariViewer.layers == [layer]
    assert layer.data.shape == (1, 2)

    widget.setResultLayers('job', [(np.array([[5.0, 6.0, 7.0]]), {'name': 'locs'}, 'points')])
    assert widget.napariViewer.layers[0] is not layer


def _controller_with_mock_widget():
    widget = Mock()
    commChannel = Mock()
    master = Mock()
    master.detectorsManager.hasDevices.return_value = False
    with patch(
        'imswitch.imcontrol.controller.controllers.ImageController.getWidgetStatePersistence',
        return_value=Mock(),
    ):
        controller = ImageController(
            Mock(), commChannel, master, widget=widget, factory=Mock(), moduleCommChannel=Mock(),
        )
    return controller, widget


def test_controller_coalesces_result_layer_updates_per_job(qtbot):
    controller, widget = _controller_with_mock_widget()
    first = [(_image(1), {'name': 'a'}, 'image')]
    second = [(_image(2), {'name': 'a'}, 'image')]
    other = [(_image(3), {'name': 'b'}, 'image')]

    controller.resultLayersUpdated('job', first)
    controller.resultLayersUpdated('job', second)
    controller.resultLayersUpdated('other', other)
    assert not widget.setResultLayers.called

    qtbot.waitUntil(lambda: widget.setResultLayers.call_count == 2, timeout=2000)
    calls = {call.args[0]: call.args[1] for call in widget.setResultLayers.call_args_list}
    assert float(calls['job'][0][0][0, 0]) == 2.0
    assert float(calls['other'][0][0][0, 0]) == 3.0


def test_an_empty_viewer_fits_itself_to_the_first_result_layer(qtbot):
    controller, widget = _controller_with_mock_widget()
    widget.setResultLayers.return_value = True
    controller._shouldResetView = True

    controller.resultLayersUpdated('job', [(_image(1, shape=(6, 8)), {'name': 'a'}, 'image')])
    qtbot.waitUntil(lambda: widget.setResultLayers.called, timeout=2000)

    widget.resetView.assert_called_once()
    assert controller._shouldResetView is False

    widget.setResultLayers.return_value = False
    controller.resultLayersUpdated('job', [(_image(2, shape=(6, 8)), {'name': 'a'}, 'image')])
    qtbot.waitUntil(lambda: widget.setResultLayers.call_count == 2, timeout=2000)
    widget.resetView.assert_called_once()


def test_controller_drops_pending_updates_of_a_removed_job(qtbot):
    controller, widget = _controller_with_mock_widget()
    controller.resultLayersUpdated('job', [(_image(1), {'name': 'a'}, 'image')])

    controller.resultLayersRemoved('job')
    widget.removeResultLayers.assert_called_once_with('job')
    qtbot.wait(50)

    assert not widget.setResultLayers.called


def test_improcess_live_results_replace_one_layer_per_name(qtbot):
    controller, widget = _controller_with_mock_widget()

    controller.liveReconResultAvailable('recon', _image(1), [0.1, 0.2])
    controller.liveReconResultAvailable('recon', _image(2), [0.1, 0.2])
    qtbot.waitUntil(lambda: widget.setResultLayers.called, timeout=2000)

    assert widget.setResultLayers.call_count == 1
    jobName, layers = widget.setResultLayers.call_args.args
    assert jobName == 'ImProcess: recon'
    data, kwargs, kind = layers[0]
    assert kind == 'image'
    assert float(data[0, 0]) == 2.0
    assert kwargs == {'name': 'recon', 'scale': (0.1, 0.2)}
    assert not widget.addStaticLayer.called
