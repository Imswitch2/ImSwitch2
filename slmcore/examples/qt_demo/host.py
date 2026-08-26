"""Standalone composition root for the slmcore Qt demo."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from slmcore import SLMSetup,SLMStartupPreferences,SLMWorkspace
from slmcore.host import MockSLMDeviceProvider,SLMHostServices
from slmcore.qt import (
    PreviewContainer,
    PreviewPlacement,
    SLMPanelLayoutPolicy,
    SLMQtSession,
    SLMQtSessionFactory,
    SLMQtSessionGroup,
)

from .setup import load_demo_setups
from .window import DemoMainWindow


logger = logging.getLogger(__name__)


class DemoHost:
    """Assemble reusable slmcore sessions without any physical hardware."""

    def __init__(self,window: DemoMainWindow,*,data_dir: str | Path) -> None:
        self.window = window
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.workspace = SLMWorkspace(self.data_dir)
        self.factory = SLMQtSessionFactory(workspace=self.workspace)
        self.group = SLMQtSessionGroup(parent=window)
        self.sessions: dict[str,SLMQtSession] = {}
        self.devices: dict[str,MockSLMDeviceProvider] = {}
        self._disposed = False

        self.group.sigControlModeChanged.connect(window.set_control_mode)
        self.group.sigControlModeAvailabilityChanged.connect(
            window.set_control_mode_change_enabled,
        )
        window.sigControlModeRequested.connect(self.set_control_mode)

    def initialize(self) -> None:
        """Construct, mount and initialize every demo SLM."""
        for setup_file,setup,preferences in load_demo_setups(self.data_dir):
            self._initialize_slm(setup_file,setup,preferences)
        self.window.set_control_mode(self.group.control_mode)
        self.window.set_control_mode_change_enabled(
            self.group.can_change_control_mode,
        )

    def set_control_mode(self,mode: Any) -> None:
        if not self.group.set_control_mode(mode):
            self.window.set_control_mode(self.group.control_mode)

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        for slm_key,session in tuple(self.sessions.items()):
            self.group.remove_session(slm_key)
            self.window.remove_slm(slm_key)
            try:
                session.dispose()
            except Exception:
                logger.exception("Failed to dispose demo session %s",slm_key)
        self.sessions.clear()
        self.devices.clear()

    def _initialize_slm(
        self,
        setup_file: Path,
        setup: SLMSetup,
        preferences: SLMStartupPreferences,
    ) -> None:
        slm_key = setup.identity.key
        display_name = setup.identity.display_name or slm_key
        if slm_key in self.sessions:
            raise KeyError("SLM %r is already initialized" % slm_key)

        device = MockSLMDeviceProvider(requires_explicit_connection=True)
        session,panel = self.factory.create(
            setup=setup,
            startup_preferences=preferences,
            setup_file=setup_file,
            host_services=SLMHostServices(device=device),
            auto_upload_frame=True,
            layout_policy=SLMPanelLayoutPolicy(
                preview_placement=PreviewPlacement.LEFT,
                preview_container=PreviewContainer.PLAIN,
                preview_resizable=True,
                preview_initial_size=350,
                preview_min_size=150,
                highlight_active_section=True,
            ),
        )

        try:
            self.window.add_slm(
                slm_key=slm_key,
                display_name=display_name,
                panel=panel,
            )
            self.sessions[slm_key] = session
            self.devices[slm_key] = device
            self.group.add_session(session,key=slm_key)
            self._install_session_logging(slm_key,session,device)
        except Exception:
            self.group.remove_session(slm_key)
            self.window.remove_slm(slm_key)
            self.sessions.pop(slm_key,None)
            self.devices.pop(slm_key,None)
            try:
                session.dispose()
            finally:
                panel.deleteLater()
            raise

        result = session.initialize_device(show_error=False)
        if result is None or not result.connected:
            logger.warning("Mock device initialization failed for %s",slm_key)
        else:
            logger.info("Initialized %s with mock device",slm_key)

    @staticmethod
    def _install_session_logging(
        slm_key: str,
        session: SLMQtSession,
        device: MockSLMDeviceProvider,
    ) -> None:
        session.sigError.connect(
            lambda title,error,key=slm_key:
                logger.error("[%s] %s: %s",key,title,error)
        )
        session.sigWarning.connect(
            lambda title,message,key=slm_key:
                logger.warning("[%s] %s: %s",key,title,message)
        )
        session.sigInfo.connect(
            lambda title,message,key=slm_key:
                logger.info("[%s] %s: %s",key,title,message)
        )
        session.sigUploadFailed.connect(
            lambda error,key=slm_key:
                logger.error("[%s] frame upload failed: %s",key,error)
        )
        session.sigFrameChanged.connect(
            lambda _frame,key=slm_key,mock=device:
                logger.debug(
                    "[%s] frame published to mock device (upload #%d)",
                    key,mock.upload_count,
                )
        )
