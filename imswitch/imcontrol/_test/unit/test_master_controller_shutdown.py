"""Unit tests for MasterController.closeEvent() finalization behavior.

Verifies that closeEvent() calls finalize() or close() on all manager attributes,
including non-MultiManager instances like pulseGeneratorManager and triggerScopeManager.
Ensures one failing finalize does not prevent the others from being called.
"""
import pytest
from unittest.mock import Mock, MagicMock, patch, PropertyMock


class TestMasterControllerShutdown:
    """Test suite for MasterController manager finalization."""

    @pytest.fixture
    def mock_setup_info(self):
        """Create a minimal SetupInfo mock."""
        setup = Mock()
        setup.rs232devices = {}
        setup.detectors = {}
        setup.lasers = {}
        setup.positioners = {}
        setup.rotators = {}
        setup.flipMirrors = {}
        setup.slms = {}
        setup.triggerScope = None
        setup.microscopeStand = None
        setup.scan = None
        setup.teensyPulse = None
        return setup

    @pytest.fixture
    def mock_comm_channel(self):
        """Create a mock communication channel."""
        channel = Mock()
        channel.sigAcquisitionStarted = Mock()
        channel.sigAcquisitionStopped = Mock()
        channel.sigDetectorSwitched = Mock()
        channel.sigUpdateImage = Mock()
        channel.sigNewFrame = Mock()
        channel.sigRecordingStarted = Mock()
        channel.sigRecordingEnded = Mock()
        channel.sigUpdateRecFrameNum = Mock()
        channel.sigUpdateRecTime = Mock()
        channel.sigMemorySnapAvailable = Mock()
        return channel

    @pytest.fixture
    def mock_module_comm_channel(self):
        """Create a mock module communication channel."""
        return Mock(memoryRecordings={})

    @pytest.fixture
    def master_controller(self, mock_setup_info, mock_comm_channel, mock_module_comm_channel):
        """Create a MasterController with all dependencies mocked."""
        with patch('imswitch.imcontrol.controller.MasterController.NidaqManager'), \
             patch('imswitch.imcontrol.controller.MasterController.RS232sManager'), \
             patch('imswitch.imcontrol.controller.MasterController.DetectorsManager'), \
             patch('imswitch.imcontrol.controller.MasterController.LasersManager'), \
             patch('imswitch.imcontrol.controller.MasterController.PositionersManager'), \
             patch('imswitch.imcontrol.controller.MasterController.RotatorsManager'), \
             patch('imswitch.imcontrol.controller.MasterController.FlipMirrorsManager'), \
             patch('imswitch.imcontrol.controller.MasterController.RecordingManager'), \
             patch('imswitch.imcontrol.controller.MasterController.SLMsManager'):

            from imswitch.imcontrol.controller.MasterController import MasterController
            mc = MasterController(mock_setup_info, mock_comm_channel, mock_module_comm_channel)
            return mc

    def test_closeEvent_finalizes_all_managers(self, master_controller):
        """Test that closeEvent calls finalize() on all manager attributes."""
        # Add mock finalize methods to all managers
        mock_managers = {
            'detectorsManager': Mock(finalize=Mock()),
            'lasersManager': Mock(finalize=Mock()),
            'positionersManager': Mock(finalize=Mock()),
            'rotatorsManager': Mock(finalize=Mock()),
            'flipMirrorsManager': Mock(finalize=Mock()),
            'recordingManager': Mock(finalize=Mock(), endRecording=Mock()),
            'slmsManager': Mock(finalize=Mock()),
            'nidaqManager': Mock(finalize=Mock()),
            'rs232sManager': Mock(finalize=Mock()),
        }

        # Set the mock managers on the controller
        for name, manager in mock_managers.items():
            setattr(master_controller, name, manager)

        # Call closeEvent
        master_controller.closeEvent()

        # Verify endRecording was called first
        mock_managers['recordingManager'].endRecording.assert_called_once_with(emitSignal=False, wait=True)

        # Verify finalize was called on all managers
        for name, manager in mock_managers.items():
            manager.finalize.assert_called_once(), f"{name}.finalize() was not called"

    def test_closeEvent_finalizes_pulse_generator_manager(self, master_controller):
        """Test that closeEvent finalizes pulseGeneratorManager (non-MultiManager)."""
        # Create a mock pulse generator manager with finalize
        mock_pulse_gen = Mock(finalize=Mock())
        master_controller.pulseGeneratorManager = mock_pulse_gen

        # Mock recordingManager.endRecording
        master_controller.recordingManager = Mock(endRecording=Mock())

        # Call closeEvent
        master_controller.closeEvent()

        # Verify finalize was called
        mock_pulse_gen.finalize.assert_called_once()

    def test_closeEvent_finalizes_trigger_scope_manager(self, master_controller):
        """Test that closeEvent finalizes triggerScopeManager (non-MultiManager)."""
        # Create a mock trigger scope manager with finalize
        mock_trigger_scope = Mock(finalize=Mock())
        master_controller.triggerScopeManager = mock_trigger_scope

        # Mock recordingManager.endRecording
        master_controller.recordingManager = Mock(endRecording=Mock())

        # Call closeEvent
        master_controller.closeEvent()

        # Verify finalize was called
        mock_trigger_scope.finalize.assert_called_once()

    def test_closeEvent_handles_one_manager_failing(self, master_controller):
        """Test that one manager failing to finalize doesn't prevent others."""
        # Create managers where one raises an exception
        failing_manager = Mock()
        failing_manager.finalize = Mock(side_effect=RuntimeError("Finalize failed"))

        working_manager_1 = Mock(finalize=Mock())
        working_manager_2 = Mock(finalize=Mock())

        master_controller.detectorsManager = working_manager_1
        master_controller.lasersManager = failing_manager
        master_controller.positionersManager = working_manager_2
        master_controller.recordingManager = Mock(endRecording=Mock())

        # Call closeEvent - should not raise
        master_controller.closeEvent()

        # Verify both working managers were finalized despite the failing one
        working_manager_1.finalize.assert_called_once()
        failing_manager.finalize.assert_called_once()
        working_manager_2.finalize.assert_called_once()

    def test_closeEvent_skips_none_managers(self, master_controller):
        """Test that closeEvent safely handles None manager attributes."""
        # Set some managers to None (as happens when optional hardware is not configured)
        master_controller.pulseGeneratorManager = None
        master_controller.triggerScopeManager = None
        master_controller.recordingManager = Mock(endRecording=Mock())

        # Call closeEvent - should not raise
        master_controller.closeEvent()

    def test_closeEvent_uses_close_fallback(self, master_controller):
        """Test that closeEvent falls back to close() if finalize() is not available."""
        # Create a manager with close() but not finalize()
        mock_manager = Mock(spec=['close'])
        mock_manager.close = Mock()

        master_controller.detectorsManager = mock_manager
        master_controller.recordingManager = Mock(endRecording=Mock())

        # Call closeEvent
        master_controller.closeEvent()

        # Verify close was called (since finalize is not available)
        mock_manager.close.assert_called_once()
