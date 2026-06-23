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


def test_imagecontroller_slot_calls_addStaticLayer():
    """ImageController.liveReconResultAvailable calls widget.addStaticLayer."""
    from imswitch.imcontrol.controller.controllers.ImageController import ImageController
    
    # Create minimal stub
    class _FakeWidget:
        def __init__(self):
            self.added_layers = []
        
        def addStaticLayer(self, name, image, scale=None):
            self.added_layers.append((name, image, scale))
    
    stub_widget = _FakeWidget()
    
    # Bind the method to a fake controller
    stub = SimpleNamespace(_widget=stub_widget, _shouldResetView=False)
    bound_method = ImageController.liveReconResultAvailable.__get__(stub)
    
    # Call it
    test_image = np.ones((10, 20))
    test_scale = [1.5, 2.0]
    bound_method('test-layer', test_image, test_scale)
    
    # Verify
    assert len(stub_widget.added_layers) == 1
    name, image, scale = stub_widget.added_layers[0]
    assert name == 'test-layer'
    assert np.array_equal(image, test_image)
    assert scale == test_scale
