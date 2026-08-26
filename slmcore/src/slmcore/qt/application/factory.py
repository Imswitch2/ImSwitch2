from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Callable

from qtpy import QtWidgets

from ...application.runtime_factory import SLMRuntimeFactory
from ...cgh.execution.executor import CGHExecutor
from ...engine.registry import DEFAULT_REGISTRIES,SLMRegistries
from ...host.services import SLMHostServices
from ...setup import (
    SLMSetup,
    SLMStartupPreferences,
    save_slm_startup_preferences,
)
from ...workspace import SLMWorkspace
from ..panel.panel import SLMPanel
from ..panel.policy import (
    DEFAULT_SLM_PANEL_LAYOUT_POLICY,
    SLMPanelLayoutPolicy,
)
from ..sections.display import SectionsDisplayMode
from ..sections.policy import DEFAULT_RENDER_POLICY,RenderPolicy
from .interaction import (
    DEFAULT_RUNTIME_VIEW_INTERACTION_SETTINGS,
    RuntimeViewInteractionSettings,
)
from .session import SLMQtSession
from .startup_preferences import _StartupPreferencesState


class SLMQtSessionFactory:
    """Construct the standard reusable Qt session and panel for one SLM.

    Normal hosts provide a canonical :class:`SLMSetup`, startup preferences and
    one :class:`SLMWorkspace`. The workspace owns config/correction/calibration
    locations. Hosts only provide physical/external capabilities and, when they
    own a larger setup file, an optional startup-preference persistence callback.
    """

    def __init__(
        self,
        *,
        registries: SLMRegistries | None=None,
        workspace: SLMWorkspace | None=None,
    ) -> None:
        registries = DEFAULT_REGISTRIES if registries is None else registries
        if not isinstance(registries,SLMRegistries):
            raise TypeError("registries must be SLMRegistries or None")
        if workspace is not None and not isinstance(workspace,SLMWorkspace):
            raise TypeError("workspace must be an SLMWorkspace or None")

        self.registries = registries
        self.workspace = workspace

    def create(
        self,
        *,
        setup: SLMSetup,
        startup_preferences: SLMStartupPreferences | None=None,
        setup_file: str | Path | None=None,
        on_startup_preferences_changed: (
            Callable[[SLMStartupPreferences],None] | None
        )=None,
        host_services: SLMHostServices | None=None,
        interaction_settings: RuntimeViewInteractionSettings=(
            DEFAULT_RUNTIME_VIEW_INTERACTION_SETTINGS
        ),
        layout_policy: SLMPanelLayoutPolicy=DEFAULT_SLM_PANEL_LAYOUT_POLICY,
        render_policy: RenderPolicy=DEFAULT_RENDER_POLICY,
        cgh_executor: CGHExecutor | None=None,
        auto_upload_frame: bool=True,
        parent: QtWidgets.QWidget | None=None,
    ) -> tuple[SLMQtSession, SLMPanel]:
        if not isinstance(setup,SLMSetup):
            raise TypeError("setup must be an SLMSetup")
        preferences = (
            SLMStartupPreferences()
            if startup_preferences is None
            else startup_preferences
        )
        if not isinstance(preferences,SLMStartupPreferences):
            raise TypeError(
                "startup_preferences must be an SLMStartupPreferences or None"
            )
        preference_callback = self._preference_callback(
            setup_file=setup_file,
            callback=on_startup_preferences_changed,
        )
        preference_state = _StartupPreferencesState(
            preferences,preference_callback,
        )
        services = host_services or SLMHostServices()
        if not isinstance(services,SLMHostServices):
            raise TypeError("host_services must be an SLMHostServices or None")

        workspace = self.workspace
        config_repository = (
            None if workspace is None
            else workspace.config_repository(setup,self.registries)
        )
        correction_store = (
            None if workspace is None else workspace.correction_store(setup)
        )
        calibration_store = (
            None if workspace is None else workspace.calibration_store
        )
        runtime_factory = SLMRuntimeFactory(
            setup=setup,
            registries=self.registries,
            correction_store=correction_store,
            config_repository=config_repository,
        )
        startup = runtime_factory.create_startup(
            preference_state.startup_config()
        )
        display_mode = self._display_mode(preference_state)
        display_name = setup.identity.display_name or setup.identity.key

        panel = None
        session = None
        try:
            panel = SLMPanel(
                section_snapshots=startup.runtime.get_section_snapshots(),
                initial_frame=startup.runtime.artifacts.eightbit,
                current_config_path=startup.config_path,
                section_display_mode=display_mode,
                render_policy=render_policy,
                layout_policy=layout_policy,
                parent=parent,
            )
            session = SLMQtSession(
                runtime=startup.runtime,
                panel=panel,
                host_services=services,
                startup_preferences=preference_state,
                interaction_settings=interaction_settings,
                cgh_executor=cgh_executor,
                auto_upload_frame=auto_upload_frame,
                display_name=display_name,
                calibration_store=calibration_store,
                apply_startup_calibration_defaults=(startup.config_path is None),
                runtime_factory=runtime_factory,
                config_repository=config_repository,
                current_config_path=startup.config_path,
                parent=panel,
            )
            if startup.warnings:
                panel.set_status("; ".join(startup.warnings),error=True)
            return session,panel
        except Exception:
            if session is not None:
                try:
                    session.dispose()
                except Exception:
                    pass
            if panel is not None:
                panel.deleteLater()
            raise

    @staticmethod
    def _preference_callback(
        *,
        setup_file: str | Path | None,
        callback: Callable[[SLMStartupPreferences],None] | None,
    ) -> Callable[[SLMStartupPreferences],None]:
        if callback is not None:
            if not callable(callback):
                raise TypeError("on_startup_preferences_changed must be callable or None")
            return callback
        if setup_file is None:
            raise ValueError(
                "setup_file is required when startup preference persistence "
                "is not provided by the host"
            )
        path = Path(setup_file).expanduser()
        return partial(save_slm_startup_preferences,path)

    @staticmethod
    def _display_mode(preferences: _StartupPreferencesState) -> SectionsDisplayMode:
        value = preferences.section_display_mode()
        try:
            return SectionsDisplayMode.normalize(value)
        except ValueError:
            return SectionsDisplayMode.TABS


__all__ = ["SLMQtSessionFactory"]
