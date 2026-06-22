import threading
import time
from unittest.mock import Mock, MagicMock, patch
import pytest


class FakeSetupInfo:
    """Minimal setup info with pyroServerInfo."""
    def __init__(self):
        self.pyroServerInfo = Mock()
        self.pyroServerInfo.name = "test_server"
        self.pyroServerInfo.host = "127.0.0.1"
        self.pyroServerInfo.port = 8001
        self.pyroServerInfo.active = True
        self.shortcuts = {}
        self.positioners = {}
        self.smartMicroscopyModes = None
        self.smartMicroscopyModePolicies = None


class TestImSwitchServerLifecycle:
    """Test ImSwitchServer lifecycle without real network/hardware."""
    
    @patch('imswitch.imcontrol.controller.server.ImSwitchServer.initLogger')
    @patch('imswitch.imcontrol.controller.server.ImSwitchServer.uvicorn.Server')
    @patch('imswitch.imcontrol.controller.server.ImSwitchServer.Pyro5.server.Daemon')
    def test_run_does_not_block(self, mock_daemon_class, mock_server_class, mock_logger):
        """Test that run() does not block the caller indefinitely."""
        from imswitch.imcontrol.controller.server.ImSwitchServer import ImSwitchServer
        
        # Mock logger
        mock_logger.return_value = MagicMock()
        
        # Mock uvicorn server
        mock_uvicorn_server = MagicMock()
        mock_server_class.return_value = mock_uvicorn_server
        
        # Mock Pyro daemon
        mock_pyro_daemon = MagicMock()
        mock_daemon_class.return_value = mock_pyro_daemon
        
        # Make requestLoop non-blocking for the test
        def fake_request_loop():
            time.sleep(0.1)
        mock_pyro_daemon.requestLoop = fake_request_loop
        
        api = Mock()
        api._asdict = Mock(return_value={})
        setupInfo = FakeSetupInfo()
        
        server = ImSwitchServer(api, setupInfo)
        
        # Run in a thread to test non-blocking behavior
        server_thread = threading.Thread(target=server.run, daemon=True)
        start_time = time.time()
        server_thread.start()
        
        # Give it a moment to start
        time.sleep(0.2)
        
        # If run() was blocking, we wouldn't get here quickly
        elapsed = time.time() - start_time
        assert elapsed < 1.0, "run() blocked for too long"
        
        # Cleanup
        server.stop()
        server_thread.join(timeout=1.0)
    
    @patch('imswitch.imcontrol.controller.server.ImSwitchServer.initLogger')
    @patch('imswitch.imcontrol.controller.server.ImSwitchServer.uvicorn.Server')
    @patch('imswitch.imcontrol.controller.server.ImSwitchServer.Pyro5.server.Daemon')
    def test_stop_shuts_down_both_servers(self, mock_daemon_class, mock_server_class, mock_logger):
        """Test that stop() shuts down both uvicorn and Pyro servers."""
        from imswitch.imcontrol.controller.server.ImSwitchServer import ImSwitchServer
        
        # Mock logger
        mock_logger.return_value = MagicMock()
        
        # Mock uvicorn server
        mock_uvicorn_server = MagicMock()
        mock_server_class.return_value = mock_uvicorn_server
        
        # Mock Pyro daemon
        mock_pyro_daemon = MagicMock()
        mock_daemon_class.return_value = mock_pyro_daemon
        
        # Make requestLoop non-blocking
        mock_pyro_daemon.requestLoop = lambda: time.sleep(0.05)
        
        api = Mock()
        api._asdict = Mock(return_value={})
        setupInfo = FakeSetupInfo()
        
        server = ImSwitchServer(api, setupInfo)
        
        # Start server in a thread
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()
        time.sleep(0.15)
        
        # Stop the server
        server.stop()
        server_thread.join(timeout=1.0)
        
        # Verify both servers were shut down
        assert mock_pyro_daemon.shutdown.called, "Pyro daemon shutdown not called"
        
        # Verify server handles are cleared
        assert server._uvicorn_server is None
        assert server._pyro_daemon is None
    
    @patch('imswitch.imcontrol.controller.server.ImSwitchServer.initLogger')
    @patch('imswitch.imcontrol.controller.server.ImSwitchServer.uvicorn.Server')
    @patch('imswitch.imcontrol.controller.server.ImSwitchServer.Pyro5.server.Daemon')
    def test_stop_is_idempotent(self, mock_daemon_class, mock_server_class, mock_logger):
        """Test that stop() is idempotent (safe to call multiple times)."""
        from imswitch.imcontrol.controller.server.ImSwitchServer import ImSwitchServer
        
        # Mock logger
        mock_logger.return_value = MagicMock()
        
        # Mock servers
        mock_uvicorn_server = MagicMock()
        mock_server_class.return_value = mock_uvicorn_server
        mock_pyro_daemon = MagicMock()
        mock_daemon_class.return_value = mock_pyro_daemon
        mock_pyro_daemon.requestLoop = lambda: time.sleep(0.05)
        
        api = Mock()
        api._asdict = Mock(return_value={})
        setupInfo = FakeSetupInfo()
        
        server = ImSwitchServer(api, setupInfo)
        
        # Start server
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()
        time.sleep(0.15)
        
        # Call stop multiple times - should not raise exceptions
        server.stop()
        server.stop()
        server.stop()
        
        server_thread.join(timeout=1.0)
        
        # Should still be None after multiple calls
        assert server._uvicorn_server is None
        assert server._pyro_daemon is None
    
    @patch('imswitch.imcontrol.controller.server.ImSwitchServer.initLogger')
    @patch('imswitch.imcontrol.controller.server.ImSwitchServer.uvicorn.Server')
    @patch('imswitch.imcontrol.controller.server.ImSwitchServer.Pyro5.server.Daemon')
    def test_stop_before_start(self, mock_daemon_class, mock_server_class, mock_logger):
        """Test that stop() can be called even if server was never started."""
        from imswitch.imcontrol.controller.server.ImSwitchServer import ImSwitchServer
        
        # Mock logger
        mock_logger.return_value = MagicMock()
        
        api = Mock()
        api._asdict = Mock(return_value={})
        setupInfo = FakeSetupInfo()
        
        server = ImSwitchServer(api, setupInfo)
        
        # Call stop without ever calling run - should not raise
        server.stop()
        
        # Verify handles remain None
        assert server._uvicorn_server is None
        assert server._pyro_daemon is None


class TestImConMainControllerServerLifecycle:
    """Test that ImConMainController properly manages server lifecycle."""
    
    @patch('imswitch.imcontrol.controller.ImConMainController.MasterController')
    @patch('imswitch.imcontrol.controller.ImConMainController.ImConWidgetControllerFactory')
    @patch('imswitch.imcontrol.controller.ImConMainController.Thread')
    @patch('imswitch.imcontrol.controller.ImConMainController.ImSwitchServer')
    def test_close_event_stops_and_joins_server_thread(
        self, mock_server_class, mock_thread_class, mock_factory_class, mock_master_class
    ):
        """Test that closeEvent() stops the server and joins the thread."""
        from imswitch.imcontrol.controller.ImConMainController import ImConMainController
        
        # Setup mocks
        mock_server = MagicMock()
        mock_server_class.return_value = mock_server
        
        mock_thread = MagicMock()
        mock_thread.wait = MagicMock(return_value=True)  # Thread stops within timeout
        mock_thread_class.return_value = mock_thread
        
        mock_factory = MagicMock()
        mock_factory_class.return_value = mock_factory
        
        mock_master = MagicMock()
        mock_master_class.return_value = mock_master
        
        # Create minimal mocks for ImConMainController initialization
        options = Mock()
        setupInfo = FakeSetupInfo()
        mainView = MagicMock()
        mainView.widgets = {}
        moduleCommChannel = Mock()
        
        with patch('imswitch.imcontrol.controller.ImConMainController.CommunicationChannel'), \
             patch('imswitch.imcontrol.controller.ImConMainController.generateAPI'), \
             patch('imswitch.imcontrol.controller.ImConMainController.generateShortcuts'), \
             patch('imswitch.imcontrol.controller.ImConMainController.ShortcutManager'), \
             patch('imswitch.imcontrol.controller.ImConMainController.getWidgetStatePersistence'), \
             patch('imswitch.imcontrol.controller.ImConMainController.SetupModeController'), \
             patch('imswitch.imcontrol.controller.ImConMainController.SmartMicroscopyModeService'), \
             patch('imswitch.imcontrol.controller.ImConMainController.PickSetupController'), \
             patch('imswitch.imcontrol.controller.ImConMainController.PickDatasetsController'):
            
            controller = ImConMainController(options, setupInfo, mainView, moduleCommChannel)
            
            # Verify server thread was started
            assert hasattr(controller, '_serverWorker')
            assert hasattr(controller, '_thread')
            assert mock_thread.start.called
            
            # Call closeEvent
            controller.closeEvent()
            
            # Verify server was stopped and thread was joined
            assert mock_server.stop.called, "Server stop() not called"
            assert mock_thread.quit.called, "Thread quit() not called"
            assert mock_thread.wait.called, "Thread wait() not called"
            
            # Verify master controller closeEvent was called
            assert mock_master.closeEvent.called


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
