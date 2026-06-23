"""Tests for live reconstruction folder discovery and sequential queue processing."""

import os
from collections import deque
from unittest.mock import MagicMock, patch

import pytest
from qtpy import QtCore
from qtpy.QtWidgets import QApplication

from imswitch.improcess.controller.LiveModeController import LiveModeController


class _FakeExtension:
    """Fake extension value object."""
    def __init__(self, value='zarr'):
        self._value = value
        self.sigValueChanged = MagicMock()
    
    def value(self):
        return self._value


class _FakeCommChannel:
    """Fake communication channel with extension attribute."""
    def __init__(self, extension='zarr'):
        self.extension = _FakeExtension(extension)
        self.sigResultProduced = MagicMock()
        self.sigLiveResultUpdated = MagicMock()


class _FakeWidget:
    """Fake watcher widget."""
    def __init__(self, path=None):
        self.path = path
        self.liveCheck = MagicMock()
        self.sigLiveChanged = MagicMock()


class _FakeReconstructor:
    """Fake reconstructor."""
    def __init__(self, name='test_recon'):
        self.name = name
        self.supports_streaming = False


class _FakeMainController:
    """Fake main controller."""
    def __init__(self, reconstructor=None):
        self._activeReconstructor = reconstructor or _FakeReconstructor()


class _FakeLiveReconstructionController(QtCore.QObject):
    """Fake LiveReconstructionController that records start() calls."""
    
    sigFinished = QtCore.Signal()
    
    def __init__(self, comm_channel):
        super().__init__()
        self._commChannel = comm_channel
        self.start_calls = []
        self.stop_calls = []
    
    def start(self, reconstructor, source, params, source_arg=None):
        """Record start call."""
        self.start_calls.append({
            'reconstructor': reconstructor,
            'source': source,
            'params': params,
            'source_arg': source_arg
        })
    
    def stop(self):
        """Record stop call."""
        self.stop_calls.append(True)


def _make_controller(folder_path='/tmp/test', extension='zarr'):
    """Create a LiveModeController with fakes using __new__ bypass."""
    controller = LiveModeController.__new__(LiveModeController)
    controller._widget = _FakeWidget(path=folder_path)
    controller._commChannel = _FakeCommChannel(extension=extension)
    controller._mainController = _FakeMainController()
    controller._logger = MagicMock()
    
    # Initialize instance variables
    controller._liveController = None
    controller._fileWatcher = None
    controller._storeQueue = deque()
    controller._currentlyProcessing = False
    controller._watchedFolder = None
    
    return controller


@patch('imswitch.improcess.controller.LiveModeController.FileWatcher')
@patch('imswitch.improcess.controller.LiveModeController.make_live_source')
def test_sequential_processing(mock_make_source, mock_file_watcher_class):
    """Test that stores are processed one at a time."""
    controller = _make_controller(folder_path='/tmp/test')
    
    # Setup mock FileWatcher
    mock_watcher = MagicMock()
    mock_watcher.filesInDirectory.return_value = ['store1.zarr', 'store2.zarr', 'store3.zarr']
    mock_file_watcher_class.return_value = mock_watcher
    
    # Setup mock make_live_source
    mock_make_source.return_value = MagicMock()
    
    # Replace the real LiveReconstructionController with our fake
    fake_live_controller = _FakeLiveReconstructionController(controller._commChannel)
    controller._liveController = fake_live_controller
    
    # Mock path validation
    with patch('os.path.exists', return_value=True), \
         patch('os.path.isdir', return_value=True), \
         patch('os.path.join', side_effect=lambda *args: '/'.join(args)):
        
        # Start live mode
        controller._startLive()
        
        # First store should start immediately
        assert len(fake_live_controller.start_calls) == 1
        assert fake_live_controller.start_calls[0]['source_arg'] == '/tmp/test/store1.zarr'
        
        # Second store should NOT start yet (we're still processing the first)
        assert len(fake_live_controller.start_calls) == 1
        
        # Simulate first store finishing by directly calling the slot
        controller._onStoreFinished()
        
        # Now second store should start
        assert len(fake_live_controller.start_calls) == 2
        assert fake_live_controller.start_calls[1]['source_arg'] == '/tmp/test/store2.zarr'
        
        # Simulate second store finishing
        controller._onStoreFinished()
        
        # Now third store should start
        assert len(fake_live_controller.start_calls) == 3
        assert fake_live_controller.start_calls[2]['source_arg'] == '/tmp/test/store3.zarr'


@patch('imswitch.improcess.controller.LiveModeController.FileWatcher')
@patch('imswitch.improcess.controller.LiveModeController.make_live_source')
def test_source_creation_failure_skips_to_next(mock_make_source, mock_file_watcher_class):
    """Test that make_live_source failure for one store skips to the next."""
    controller = _make_controller(folder_path='/tmp/test')
    
    # Setup mock FileWatcher
    mock_watcher = MagicMock()
    mock_watcher.filesInDirectory.return_value = ['bad.zarr', 'good.zarr']
    mock_file_watcher_class.return_value = mock_watcher
    
    # Setup mock make_live_source: first call fails, second succeeds
    mock_make_source.side_effect = [
        ValueError("Unsupported format"),
        MagicMock()
    ]
    
    # Replace with fake live controller
    fake_live_controller = _FakeLiveReconstructionController(controller._commChannel)
    controller._liveController = fake_live_controller
    
    # Mock path validation
    with patch('os.path.exists', return_value=True), \
         patch('os.path.isdir', return_value=True), \
         patch('os.path.join', side_effect=lambda *args: '/'.join(args)):
        
        # Start live mode
        controller._startLive()
        
        # First store should fail and skip to second
        assert len(fake_live_controller.start_calls) == 1
        # Only the good store should have been started
        assert fake_live_controller.start_calls[0]['source_arg'] == '/tmp/test/good.zarr'


@patch('imswitch.improcess.controller.LiveModeController.FileWatcher')
@patch('imswitch.improcess.controller.LiveModeController.make_live_source')
def test_new_stores_are_queued_and_processed(mock_make_source, mock_file_watcher_class):
    """Test that newly detected stores are queued and processed after current store finishes."""
    controller = _make_controller(folder_path='/tmp/test')
    
    # Setup mock FileWatcher
    mock_watcher = MagicMock()
    mock_watcher.filesInDirectory.return_value = ['store1.zarr']
    mock_file_watcher_class.return_value = mock_watcher
    
    # Setup mock make_live_source
    mock_make_source.return_value = MagicMock()
    
    # Replace with fake live controller
    fake_live_controller = _FakeLiveReconstructionController(controller._commChannel)
    controller._liveController = fake_live_controller
    
    # Mock path validation
    with patch('os.path.exists', return_value=True), \
         patch('os.path.isdir', return_value=True), \
         patch('os.path.join', side_effect=lambda *args: '/'.join(args)):
        
        # Start live mode
        controller._startLive()
        
        # First store should start
        assert len(fake_live_controller.start_calls) == 1
        assert fake_live_controller.start_calls[0]['source_arg'] == '/tmp/test/store1.zarr'
        
        # Simulate FileWatcher detecting a new store while processing first store
        controller._onNewStoresDetected(['store2.zarr'])
        
        # Second store should NOT start yet (still processing first)
        assert len(fake_live_controller.start_calls) == 1
        
        # Simulate first store finishing by directly calling the slot
        controller._onStoreFinished()
        
        # Now second store should start
        assert len(fake_live_controller.start_calls) == 2
        assert fake_live_controller.start_calls[1]['source_arg'] == '/tmp/test/store2.zarr'


@patch('imswitch.improcess.controller.LiveModeController.FileWatcher')
def test_stop_clears_queue_and_stops_watcher(mock_file_watcher_class):
    """Test that toggling off stops the watcher, controller, and clears the queue."""
    controller = _make_controller(folder_path='/tmp/test')
    
    # Setup mock FileWatcher
    mock_watcher = MagicMock()
    mock_watcher.filesInDirectory.return_value = []
    mock_file_watcher_class.return_value = mock_watcher
    
    # Replace with fake live controller
    fake_live_controller = _FakeLiveReconstructionController(controller._commChannel)
    controller._liveController = fake_live_controller
    
    # Mock path validation
    with patch('os.path.exists', return_value=True), \
         patch('os.path.isdir', return_value=True):
        
        # Start live mode
        controller._startLive()
        
        # Add some items to queue manually
        controller._storeQueue.append('/tmp/test/store1.zarr')
        controller._storeQueue.append('/tmp/test/store2.zarr')
        controller._currentlyProcessing = True
        
        # Stop live mode
        controller._stopLive()
        
        # Verify watcher was stopped
        assert mock_watcher.stop.called
        assert mock_watcher.quit.called
        
        # Verify controller was stopped
        assert len(fake_live_controller.stop_calls) == 1
        
        # Verify queue was cleared
        assert len(controller._storeQueue) == 0
        assert controller._currentlyProcessing is False
        assert controller._watchedFolder is None


@patch('imswitch.improcess.controller.LiveModeController.FileWatcher')
@patch('imswitch.improcess.controller.LiveModeController.make_live_source')
def test_uses_absolute_paths(mock_make_source, mock_file_watcher_class):
    """Test that start() receives absolute store paths."""
    controller = _make_controller(folder_path='/absolute/path/folder')
    
    # Setup mock FileWatcher
    mock_watcher = MagicMock()
    mock_watcher.filesInDirectory.return_value = ['store1.zarr']
    mock_file_watcher_class.return_value = mock_watcher
    
    # Setup mock make_live_source
    mock_make_source.return_value = MagicMock()
    
    # Replace with fake live controller
    fake_live_controller = _FakeLiveReconstructionController(controller._commChannel)
    controller._liveController = fake_live_controller
    
    # Mock path validation
    with patch('os.path.exists', return_value=True), \
         patch('os.path.isdir', return_value=True), \
         patch('os.path.join', side_effect=lambda *args: '/'.join(args)):
        
        # Start live mode
        controller._startLive()
        
        # Verify absolute path was passed
        assert len(fake_live_controller.start_calls) == 1
        assert fake_live_controller.start_calls[0]['source_arg'] == '/absolute/path/folder/store1.zarr'


@patch('imswitch.improcess.controller.LiveModeController.FileWatcher')
def test_folder_path_validation(mock_file_watcher_class):
    """Test that non-directory paths are rejected."""
    controller = _make_controller(folder_path='/tmp/file.zarr')
    
    # Mock path validation: exists but is not a directory
    with patch('os.path.exists', return_value=True), \
         patch('os.path.isdir', return_value=False):
        
        # Start live mode
        controller._startLive()
        
        # Verify it was rejected
        assert controller._widget.liveCheck.setChecked.called
        assert controller._fileWatcher is None


@patch('imswitch.improcess.controller.LiveModeController.FileWatcher')
@patch('imswitch.improcess.controller.LiveModeController.make_live_source')
def test_reconstructor_params_passed_to_start(mock_make_source, mock_file_watcher_class):
    """Test that reconstructor params are passed to LiveReconstructionController.start()."""
    controller = _make_controller(folder_path='/tmp/test')
    
    # Setup main controller with a widget that has params
    class _WidgetWithParams:
        def getReconstructionParams(self):
            return {'param1': 'value1', 'param2': 42}
    
    controller._mainController._widget = _WidgetWithParams()
    
    # Setup mock FileWatcher
    mock_watcher = MagicMock()
    mock_watcher.filesInDirectory.return_value = ['store1.zarr']
    mock_file_watcher_class.return_value = mock_watcher
    
    # Setup mock make_live_source
    mock_make_source.return_value = MagicMock()
    
    # Replace with fake live controller
    fake_live_controller = _FakeLiveReconstructionController(controller._commChannel)
    controller._liveController = fake_live_controller
    
    # Mock path validation
    with patch('os.path.exists', return_value=True), \
         patch('os.path.isdir', return_value=True), \
         patch('os.path.join', side_effect=lambda *args: '/'.join(args)):
        
        # Start live mode
        controller._startLive()
        
        # Verify params were passed
        assert len(fake_live_controller.start_calls) == 1
        assert fake_live_controller.start_calls[0]['params'] == {'param1': 'value1', 'param2': 42}
