"""Contract test for live-result display bridge to imcontrol viewer.

This test verifies that when the `live_display_in_imcontrol` config flag is
enabled, ImProcessMainController bridges sigResultProduced/sigLiveResultUpdated
to the cross-module sigLiveReconResult signal with a displayable 2D image.
"""

import numpy as np
import pytest
from types import SimpleNamespace
from unittest.mock import Mock, MagicMock

from imswitch.improcess.controller.ImProcessMainController import ImProcessMainController
from imswitch.improcess.controller.CommunicationChannel import CommunicationChannel
from imswitch.imcommon.controller.ModuleCommunicationChannel import ModuleCommunicationChannel
from imswitch.improcess.model.result import ProcessingResult


class _FakeProcessingResult(ProcessingResult):
    """Minimal ProcessingResult for testing."""
    def __init__(self, name, data, axis_labels=None, axis_scales=None):
        if axis_labels is None:
            axis_labels = ['Y', 'X'] if data.ndim == 2 else [f'D{i}' for i in range(data.ndim)]
        super().__init__(
            name=name,
            data=data,
            axis_labels=axis_labels,
            axis_scales=axis_scales,
        )
    
    def save(self, path, fmt):
        pass


def _create_bridge_stub(config=None, imcontrol_registered=True):
    """Create stub controller with just the bridge method."""
    stub = SimpleNamespace()
    stub._ImProcessMainController__processingConfig = config
    stub._ImProcessMainController__moduleCommChannel = Mock(spec=ModuleCommunicationChannel)
    stub._ImProcessMainController__moduleCommChannel.isModuleRegistered = Mock(return_value=imcontrol_registered)
    stub._ImProcessMainController__moduleCommChannel.sigLiveReconResult = Mock()
    stub._ImProcessMainController__moduleCommChannel.sigLiveReconResult.emit = Mock()
    
    # Bind the bridge and helper methods used by the bridge.
    stub._processingConfigValue = ImProcessMainController._processingConfigValue.__get__(stub)
    stub._displayImageForImcontrol = ImProcessMainController._displayImageForImcontrol
    bound_bridge = ImProcessMainController._bridgeResultToImcontrol.__get__(stub)
    return stub, bound_bridge


def test_bridge_enabled_fires_sigLiveReconResult():
    """With flag ON and imcontrol registered, sigResultProduced → sigLiveReconResult."""
    config = {'live_display_in_imcontrol': True}
    stub, bridge = _create_bridge_stub(config, imcontrol_registered=True)
    
    # Call bridge with a result
    result = _FakeProcessingResult('test-recon', np.ones((10, 20)), axis_scales=[1.5, 2.0])
    bridge(result, 'TestResult')
    
    # Should emit to module channel
    stub._ImProcessMainController__moduleCommChannel.sigLiveReconResult.emit.assert_called_once()
    call_args = stub._ImProcessMainController__moduleCommChannel.sigLiveReconResult.emit.call_args[0]
    name, image, scale = call_args
    assert name == 'TestResult'
    assert image.shape == (10, 20)
    assert scale == [1.5, 2.0]


def test_bridge_enabled_from_setup_like_config():
    """SetupInfo-like configs can expose the processing block via _catchAll."""
    config = SimpleNamespace(_catchAll={'processing': {'live_display_in_imcontrol': True}})
    stub, bridge = _create_bridge_stub(config, imcontrol_registered=True)

    result = _FakeProcessingResult('test-recon', np.ones((10, 20)))
    bridge(result, 'SetupConfig')

    stub._ImProcessMainController__moduleCommChannel.sigLiveReconResult.emit.assert_called_once()


def test_bridge_enabled_from_object_config_attribute():
    """Object-like configs can expose live_display_in_imcontrol as an attribute."""
    config = SimpleNamespace(live_display_in_imcontrol=True)
    stub, bridge = _create_bridge_stub(config, imcontrol_registered=True)

    result = _FakeProcessingResult('test-recon', np.ones((10, 20)))
    bridge(result, 'ObjectConfig')

    stub._ImProcessMainController__moduleCommChannel.sigLiveReconResult.emit.assert_called_once()


def test_bridge_disabled_by_default():
    """With no config or flag OFF, sigResultProduced does not bridge."""
    stub, bridge = _create_bridge_stub(config=None, imcontrol_registered=True)
    
    result = _FakeProcessingResult('test-recon', np.ones((10, 20)))
    bridge(result, 'TestResult')
    
    # Should not emit
    stub._ImProcessMainController__moduleCommChannel.sigLiveReconResult.emit.assert_not_called()


def test_bridge_disabled_explicit_false():
    """With flag explicitly False, sigResultProduced does not bridge."""
    config = {'live_display_in_imcontrol': False}
    stub, bridge = _create_bridge_stub(config, imcontrol_registered=True)
    
    result = _FakeProcessingResult('test-recon', np.ones((10, 20)))
    bridge(result, 'TestResult')
    
    stub._ImProcessMainController__moduleCommChannel.sigLiveReconResult.emit.assert_not_called()


def test_bridge_not_fired_when_imcontrol_not_registered():
    """With flag ON but imcontrol not registered, does not emit."""
    config = {'live_display_in_imcontrol': True}
    stub, bridge = _create_bridge_stub(config, imcontrol_registered=False)
    
    result = _FakeProcessingResult('test-recon', np.ones((10, 20)))
    bridge(result, 'TestResult')
    
    stub._ImProcessMainController__moduleCommChannel.sigLiveReconResult.emit.assert_not_called()


def test_bridge_extracts_2d_slice_from_3d_data():
    """Multi-dimensional data is sliced to 2D for display."""
    config = {'live_display_in_imcontrol': True}
    stub, bridge = _create_bridge_stub(config, imcontrol_registered=True)
    
    # 3D result: take middle slice of first dimension
    data_3d = np.ones((5, 10, 20))
    result = _FakeProcessingResult('test-3d', data_3d, axis_scales=[1.0, 1.5, 2.0])
    bridge(result, 'Test3D')
    
    stub._ImProcessMainController__moduleCommChannel.sigLiveReconResult.emit.assert_called_once()
    call_args = stub._ImProcessMainController__moduleCommChannel.sigLiveReconResult.emit.call_args[0]
    name, image, scale = call_args
    assert image.shape == (10, 20)  # Middle slice: data_3d[2, :, :]
    assert scale == [1.5, 2.0]  # Last 2 scales


def test_bridge_extracts_latest_time_slice():
    """Time-labelled leading axes use the latest frame instead of the middle."""
    config = {'live_display_in_imcontrol': True}
    stub, bridge = _create_bridge_stub(config, imcontrol_registered=True)

    data_3d = np.arange(5 * 10 * 20).reshape(5, 10, 20)
    result = _FakeProcessingResult(
        'test-time',
        data_3d,
        axis_labels=['T', 'Y', 'X'],
        axis_scales=[1.0, 1.5, 2.0],
    )
    bridge(result, 'TestTime')

    call_args = stub._ImProcessMainController__moduleCommChannel.sigLiveReconResult.emit.call_args[0]
    name, image, scale = call_args
    np.testing.assert_array_equal(image, data_3d[-1])
    assert scale == [1.5, 2.0]


def test_bridge_extracts_2d_slice_from_4d_data():
    """4D data is sliced to 2D by taking middle slices."""
    config = {'live_display_in_imcontrol': True}
    stub, bridge = _create_bridge_stub(config, imcontrol_registered=True)
    
    # 4D result: (T, Z, Y, X)
    data_4d = np.arange(3 * 4 * 10 * 20).reshape(3, 4, 10, 20)
    result = _FakeProcessingResult('test-4d', data_4d, axis_scales=[1.0, 1.0, 1.5, 2.0])
    bridge(result, 'Test4D')
    
    stub._ImProcessMainController__moduleCommChannel.sigLiveReconResult.emit.assert_called_once()
    call_args = stub._ImProcessMainController__moduleCommChannel.sigLiveReconResult.emit.call_args[0]
    name, image, scale = call_args
    # Middle of first two dims: data_4d[1, 2, :, :]
    assert image.shape == (10, 20)
    assert scale == [1.5, 2.0]


def test_bridge_ignores_results_without_data():
    """Results without .data attribute are silently ignored."""
    config = {'live_display_in_imcontrol': True}
    stub, bridge = _create_bridge_stub(config, imcontrol_registered=True)
    
    # Result without data
    bad_result = SimpleNamespace(name='no-data')
    bridge(bad_result, 'BadResult')
    
    stub._ImProcessMainController__moduleCommChannel.sigLiveReconResult.emit.assert_not_called()


def test_bridge_ignores_1d_data():
    """1D data is not displayable and is silently ignored."""
    config = {'live_display_in_imcontrol': True}
    stub, bridge = _create_bridge_stub(config, imcontrol_registered=True)
    
    result = _FakeProcessingResult('1d-data', np.ones(10))
    bridge(result, '1D')
    
    stub._ImProcessMainController__moduleCommChannel.sigLiveReconResult.emit.assert_not_called()


def test_imagecontroller_slot_keeps_one_layer_per_live_result():
    """ImageController.liveReconResultAvailable routes through the result-layer
    path: the first update creates the layer, later ones update it in place."""
    from unittest.mock import patch

    from imswitch.imcontrol.controller.controllers.ImageController import ImageController
    from qtpy import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    widget = Mock()
    master = Mock()
    master.detectorsManager.hasDevices.return_value = False
    with patch(
        'imswitch.imcontrol.controller.controllers.ImageController.getWidgetStatePersistence',
        return_value=Mock(),
    ):
        controller = ImageController(
            Mock(), Mock(), master, widget=widget, factory=Mock(), moduleCommChannel=Mock(),
        )

    controller.liveReconResultAvailable('test-layer', np.ones((10, 20)), [1.5, 2.0])
    controller.liveReconResultAvailable('test-layer', np.full((10, 20), 2.0), [1.5, 2.0])
    deadline = QtCore.QDeadlineTimer(2000)
    while not widget.setResultLayers.called and not deadline.hasExpired():
        app.processEvents()

    assert widget.setResultLayers.call_count == 1
    jobName, layers = widget.setResultLayers.call_args.args
    assert jobName == 'ImProcess: test-layer'
    data, kwargs, kind = layers[0]
    assert kind == 'image'
    assert np.array_equal(data, np.full((10, 20), 2.0))
    assert kwargs == {'name': 'test-layer', 'scale': (1.5, 2.0)}
    assert not widget.addStaticLayer.called


def test_bridge_converts_the_scale_to_micrometres():
    """The ImControl viewer is in µm; a nm-calibrated result must not be sent
    with its raw scale (it was drawn a thousand times too large)."""
    config = {'live_display_in_imcontrol': True}
    stub, bridge = _create_bridge_stub(config, imcontrol_registered=True)

    result = _FakeProcessingResult('nm-recon', np.ones((4, 6)), axis_scales=[100.0, 50.0])
    result.scale_unit = 'nm'
    bridge(result, 'NmResult')

    _, _, scale = stub._ImProcessMainController__moduleCommChannel.sigLiveReconResult.emit.call_args[0]
    assert scale == pytest.approx([0.1, 0.05])

    stub, bridge = _create_bridge_stub(config, imcontrol_registered=True)
    result = _FakeProcessingResult('um-recon', np.ones((4, 6)), axis_scales=[0.2, 0.3])
    result.scale_unit = 'um'
    bridge(result, 'UmResult')
    _, _, scale = stub._ImProcessMainController__moduleCommChannel.sigLiveReconResult.emit.call_args[0]
    assert scale == pytest.approx([0.2, 0.3])
