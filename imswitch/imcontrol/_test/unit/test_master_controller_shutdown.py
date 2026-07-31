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
        result = master_controller.closeEvent()

        # Verify both working managers were finalized despite the failing one
        working_manager_1.finalize.assert_called_once()
        failing_manager.finalize.assert_called_once()
        working_manager_2.finalize.assert_called_once()
        assert result is False

    def test_false_finalizer_result_is_reported_but_cleanup_continues(
            self, master_controller):
        first = Mock(finalize=Mock(return_value=False))
        later = Mock(finalize=Mock())
        master_controller.lasersManager = first
        master_controller.positionersManager = later
        master_controller.recordingManager = Mock(endRecording=Mock())

        assert master_controller.closeEvent() is False

        first.finalize.assert_called_once()
        later.finalize.assert_called_once()

    def test_closeEvent_retry_skips_successes_and_retries_failures_by_identity(
            self, master_controller):
        stable = Mock(finalize=Mock())
        replacement = Mock(finalize=Mock())
        incomplete = Mock(finalize=Mock(side_effect=[False, None]))
        raising = Mock(
            finalize=Mock(
                side_effect=[RuntimeError("backend still stopping"), None]
            )
        )
        recording = Mock(finalize=Mock(), endRecording=Mock())
        master_controller.lasersManager = incomplete
        master_controller.positionersManager = stable
        master_controller.rotatorsManager = raising
        master_controller.recordingManager = recording

        assert master_controller.closeEvent() is False

        # A new object under an already-successful attribute is a new shutdown
        # target and must not be hidden by attribute-name bookkeeping.
        master_controller.positionersManager = replacement
        assert master_controller.closeEvent() is True

        stable.finalize.assert_called_once_with()
        replacement.finalize.assert_called_once_with()
        assert incomplete.finalize.call_count == 2
        assert raising.finalize.call_count == 2
        # Both pre-finalization drain and finalization are terminal for this
        # exact recording-manager object after the first successful pass.
        recording.endRecording.assert_called_once_with(
            emitSignal=False, wait=True
        )
        recording.finalize.assert_called_once_with()

    def test_multimanager_aggregates_submanager_shutdown_failures(self):
        from imswitch.imcontrol.model.managers.MultiManager import MultiManager

        class Group(MultiManager):
            def __init__(self):
                self._subManagers = {}

        successful = Mock(finalize=Mock())
        incomplete = Mock(finalize=Mock(return_value=False))
        failing = Mock(
            finalize=Mock(side_effect=RuntimeError("backend stuck"))
        )
        later = Mock(finalize=Mock())
        group = Group()
        group._subManagers = {
            "successful": successful,
            "incomplete": incomplete,
            "failing": failing,
            "later": later,
        }

        assert group.finalize() is False
        for manager in group._subManagers.values():
            manager.finalize.assert_called_once()

    def test_multimanager_retry_skips_successes_and_retries_failures(self):
        from imswitch.imcontrol.model.managers.MultiManager import MultiManager

        class Group(MultiManager):
            def __init__(self):
                self._subManagers = {}

        stable = Mock(finalize=Mock())
        incomplete = Mock(finalize=Mock(side_effect=[False, None]))
        raising = Mock(
            finalize=Mock(side_effect=[RuntimeError("still stopping"), None])
        )
        group = Group()
        group._subManagers = {
            "stable": stable,
            "incomplete": incomplete,
            "raising": raising,
        }

        assert group.finalize() is False
        assert group.finalize() is True

        stable.finalize.assert_called_once_with()
        assert incomplete.finalize.call_count == 2
        assert raising.finalize.call_count == 2

        replacement = Mock(finalize=Mock())
        group._subManagers["stable"] = replacement
        assert group.finalize() is True
        replacement.finalize.assert_called_once_with()
        stable.finalize.assert_called_once_with()

    def test_faulted_detector_gets_one_shutdown_retry_before_finalize(
            self, master_controller):
        detector_manager = Mock()
        detector_manager.activeAcquisitionLeases.return_value = ()
        detector_manager.faultedAcquisitionDetectors.side_effect = [
            ("camera",),
            (),
        ]
        detector_manager.finalize.return_value = None
        nidaq_manager = Mock(finalize=Mock())
        master_controller.detectorsManager = detector_manager
        master_controller.nidaqManager = nidaq_manager
        master_controller.recordingManager = Mock(endRecording=Mock())
        master_controller.scanExecutionCoordinator = Mock(
            activeToken=None, activeRunToken=None
        )

        assert master_controller.closeEvent() is True

        detector_manager.retryStop.assert_called_once_with("camera")
        detector_manager.finalize.assert_called_once()
        nidaq_manager.finalize.assert_called_once()

    def test_unresolved_detector_fault_blocks_acquisition_finalizers(
            self, master_controller):
        detector_manager = Mock(finalize=Mock())
        detector_manager.activeAcquisitionLeases.return_value = ()
        detector_manager.faultedAcquisitionDetectors.side_effect = [
            ("camera",),
            ("camera",),
        ]
        detector_manager.retryStop.side_effect = RuntimeError("still stuck")
        nidaq_manager = Mock(finalize=Mock())
        laser_manager = Mock(finalize=Mock())
        master_controller.detectorsManager = detector_manager
        master_controller.nidaqManager = nidaq_manager
        master_controller.lasersManager = laser_manager
        master_controller.recordingManager = Mock(
            endRecording=Mock(), finalize=Mock()
        )
        master_controller.scanExecutionCoordinator = Mock(
            activeToken=None, activeRunToken=None
        )

        assert master_controller.closeEvent() is False

        detector_manager.retryStop.assert_called_once_with("camera")
        detector_manager.finalize.assert_not_called()
        nidaq_manager.finalize.assert_not_called()
        laser_manager.finalize.assert_called_once()

    def test_recording_stop_failure_skips_acquisition_hardware_finalization(
            self, master_controller):
        detector_manager = Mock(finalize=Mock())
        laser_manager = Mock(finalize=Mock())
        recording_manager = Mock(
            endRecording=Mock(side_effect=RuntimeError('writer stuck')),
            finalize=Mock(),
        )
        master_controller.detectorsManager = detector_manager
        master_controller.lasersManager = laser_manager
        master_controller.recordingManager = recording_manager

        assert master_controller.closeEvent() is False

        detector_manager.finalize.assert_not_called()
        recording_manager.finalize.assert_not_called()
        # Independent safety cleanup, especially laser-off finalizers, still
        # runs even though acquisition backends cannot be torn down safely.
        laser_manager.finalize.assert_called_once()

    def test_live_recording_writer_skips_acquisition_hardware_finalization(
            self, master_controller):
        detector_manager = Mock(finalize=Mock())
        nidaq_manager = Mock(finalize=Mock())
        laser_manager = Mock(finalize=Mock())
        recording_manager = Mock(
            endRecording=Mock(),
            shutdownComplete=Mock(return_value=False),
            finalize=Mock(),
        )
        master_controller.detectorsManager = detector_manager
        master_controller.nidaqManager = nidaq_manager
        master_controller.lasersManager = laser_manager
        master_controller.recordingManager = recording_manager

        assert master_controller.closeEvent() is False

        detector_manager.finalize.assert_not_called()
        recording_manager.finalize.assert_not_called()
        nidaq_manager.finalize.assert_not_called()
        laser_manager.finalize.assert_called_once()

    def test_active_scan_coordinator_skips_acquisition_backends(
            self, master_controller):
        detector_manager = Mock(finalize=Mock())
        nidaq_manager = Mock(finalize=Mock())
        laser_manager = Mock(finalize=Mock())
        master_controller.detectorsManager = detector_manager
        master_controller.nidaqManager = nidaq_manager
        master_controller.lasersManager = laser_manager
        master_controller.recordingManager = Mock(endRecording=Mock())
        master_controller.scanExecutionCoordinator = Mock(
            activeToken=object(), activeRunToken=object()
        )

        assert master_controller.closeEvent() is False

        detector_manager.finalize.assert_not_called()
        nidaq_manager.finalize.assert_not_called()
        laser_manager.finalize.assert_called_once()

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
