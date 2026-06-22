"""Unit tests for TriggerScopeManager.finalize() lifecycle method.

Verifies that finalize() properly stops and waits for the serial monitor thread,
is idempotent (safe to call multiple times), and doesn't crash if the monitor
was never started.
"""
import pytest
from unittest.mock import Mock, MagicMock, patch, PropertyMock


class TestTriggerScopeManagerFinalize:
    """Test suite for TriggerScopeManager finalization."""

    @pytest.fixture
    def mock_setup_info(self):
        """Create a minimal SetupInfo mock for TriggerScopeManager."""
        setup = Mock()
        trigger_scope_info = Mock()
        trigger_scope_info.rs232device = 'mock_device'
        setup.triggerScope = trigger_scope_info

        # Mock getAllDevices to return empty dict (no devices to register)
        setup.getAllDevices = Mock(return_value={})
        return setup

    @pytest.fixture
    def mock_rs232s_manager(self):
        """Create a mock RS232sManager."""
        rs232_mgr = Mock()
        mock_device = Mock()
        mock_device.setTimeout = Mock()
        mock_device.write = Mock()
        rs232_mgr.__getitem__ = Mock(return_value=mock_device)
        return rs232_mgr

    @pytest.fixture
    def trigger_scope_manager(self, mock_setup_info, mock_rs232s_manager):
        """Create a TriggerScopeManager with mocked dependencies."""
        # Mock the Thread and SerialMonitor to avoid starting actual threads
        with patch('imswitch.imcontrol.model.managers.TriggerScopeManager.Thread') as MockThread, \
             patch('imswitch.imcontrol.model.managers.TriggerScopeManager.SerialMonitor') as MockSerialMonitor:

            # Create mock thread with quit/wait methods
            mock_thread = Mock()
            mock_thread.quit = Mock()
            mock_thread.wait = Mock()
            mock_thread.start = Mock()
            mock_thread.started = Mock()
            mock_thread.started.connect = Mock()
            mock_thread.finished = Mock()
            mock_thread.finished.connect = Mock()
            MockThread.return_value = mock_thread

            # Create mock serial monitor
            mock_monitor = Mock()
            mock_monitor.sigScanDone = Mock()
            mock_monitor.sigScanDone.connect = Mock()
            mock_monitor.sigUnknownMessage = Mock()
            mock_monitor.sigUnknownMessage.connect = Mock()
            mock_monitor.moveToThread = Mock()
            MockSerialMonitor.return_value = mock_monitor

            from imswitch.imcontrol.model.managers.TriggerScopeManager import TriggerScopeManager
            tsm = TriggerScopeManager(mock_setup_info, mock_rs232s_manager)

            # Store mocks for verification
            tsm._mock_thread = mock_thread
            tsm._mock_monitor = mock_monitor

            return tsm

    def test_finalize_calls_quit_and_wait(self, trigger_scope_manager):
        """Test that finalize() calls both quit() and wait() on the thread."""
        # Call finalize
        trigger_scope_manager.finalize()

        # Verify quit and wait were called
        trigger_scope_manager._mock_thread.quit.assert_called_once()
        trigger_scope_manager._mock_thread.wait.assert_called_once()

        # Verify monitoring flag is set to False
        assert trigger_scope_manager._monitoring is False

    def test_finalize_is_idempotent(self, trigger_scope_manager):
        """Test that finalize() can be called multiple times safely."""
        # Call finalize multiple times
        trigger_scope_manager.finalize()
        trigger_scope_manager.finalize()
        trigger_scope_manager.finalize()

        # Should not raise an exception, and quit/wait should only be called once
        # (on the first call when _monitoring is True)
        assert trigger_scope_manager._mock_thread.quit.call_count == 1
        assert trigger_scope_manager._mock_thread.wait.call_count == 1

    def test_finalize_safe_when_monitoring_false(self, trigger_scope_manager):
        """Test that finalize() is safe to call when monitoring is already False."""
        # Manually set monitoring to False
        trigger_scope_manager._monitoring = False

        # Reset mock call counts
        trigger_scope_manager._mock_thread.quit.reset_mock()
        trigger_scope_manager._mock_thread.wait.reset_mock()

        # Call finalize
        trigger_scope_manager.finalize()

        # Should not call quit/wait since monitoring is already False
        trigger_scope_manager._mock_thread.quit.assert_not_called()
        trigger_scope_manager._mock_thread.wait.assert_not_called()

    def test_finalize_safe_when_thread_missing(self):
        """Test that finalize() is safe when _thread attribute is missing."""
        # Create a minimal TriggerScopeManager-like object without full initialization
        tsm = Mock()
        tsm._monitoring = True

        # Import and bind the finalize method
        from imswitch.imcontrol.model.managers.TriggerScopeManager import TriggerScopeManager
        tsm.finalize = TriggerScopeManager.finalize.__get__(tsm)

        # Call finalize - should not raise even though _thread doesn't exist
        tsm.finalize()

        # Monitoring should be set to False
        assert tsm._monitoring is False

    def test_closeMonitor_delegates_to_finalize(self, trigger_scope_manager):
        """Test that closeMonitor() now delegates to finalize()."""
        # Reset mock call counts
        trigger_scope_manager._mock_thread.quit.reset_mock()
        trigger_scope_manager._mock_thread.wait.reset_mock()

        # Ensure monitoring is True
        trigger_scope_manager._monitoring = True

        # Call closeMonitor
        trigger_scope_manager.closeMonitor()

        # Verify it called quit and wait (via finalize)
        trigger_scope_manager._mock_thread.quit.assert_called_once()
        trigger_scope_manager._mock_thread.wait.assert_called_once()
        assert trigger_scope_manager._monitoring is False

    def test_del_calls_finalize_defensively(self, trigger_scope_manager):
        """Test that __del__ calls finalize() defensively."""
        # Reset mock call counts
        trigger_scope_manager._mock_thread.quit.reset_mock()
        trigger_scope_manager._mock_thread.wait.reset_mock()

        # Ensure monitoring is True
        trigger_scope_manager._monitoring = True

        # Call __del__
        trigger_scope_manager.__del__()

        # Verify finalize was called (quit and wait should have been called)
        trigger_scope_manager._mock_thread.quit.assert_called_once()
        trigger_scope_manager._mock_thread.wait.assert_called_once()

    def test_del_handles_finalize_exception(self, trigger_scope_manager):
        """Test that __del__ doesn't raise if finalize() raises an exception."""
        # Make finalize raise an exception by deleting the thread
        del trigger_scope_manager._thread

        # Call __del__ - should not raise
        trigger_scope_manager.__del__()
