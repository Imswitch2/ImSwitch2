from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Callable

from qtpy import QtWidgets

from ...application.configuration import SLMConfigurationService,StartupRuntime
from ...application.runtime_factory import SLMRuntimeFactory
from ...application.session import SLMSession
from ...core.cgh.execution.executor import CGHExecutor
from ...core.engine.registry import DEFAULT_REGISTRIES,SLMRegistries
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
from .cgh_executor import QtCGHExecutor
from .interaction import (
    DEFAULT_RUNTIME_VIEW_INTERACTION_SETTINGS,
    RuntimeViewInteractionSettings,
)
from .session import SLMQtSession
from ...application.startup_preferences import StartupPreferencesState


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
        preference_state = StartupPreferencesState(
            preferences,preference_callback,
        )
        services = host_services or SLMHostServices()
        if not isinstance(services,SLMHostServices):
            raise TypeError("host_services must be an SLMHostServices or None")

        workspace = self.workspace
        config_store = (
            None if workspace is None
            else workspace.config_store(setup.identity,self.registries)
        )
        correction_store = (
            None if workspace is None else workspace.correction_store(setup.identity)
        )
        calibration_store = (
            None if workspace is None else workspace.calibration_store
        )
        runtime_factory = SLMRuntimeFactory(
            setup=setup,
            registries=self.registries,
            correction_provider=correction_store,
        )
        configuration_service = (
            None if config_store is None else SLMConfigurationService(
                store=config_store,runtime_factory=runtime_factory,
            )
        )
        startup_name = preference_state.startup_config()
        if configuration_service is not None:
            startup = configuration_service.create_startup(startup_name)
        elif startup_name:
            startup = StartupRuntime(
                runtime=runtime_factory.create_default(),
                config_path=None,
                warnings=(
                    "Startup config was requested but configuration storage "
                    "is not configured.",
                ),
            )
        else:
            startup = StartupRuntime(runtime_factory.create_default(),None)
        display_mode = self._display_mode(preference_state)
        display_name = setup.identity.display_name or setup.identity.key

        panel = None
        session = None
        application_session = None
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
            application_executor = cgh_executor
            owns_executor = False
            if application_executor is None:
                application_executor = QtCGHExecutor(
                    parent=panel if isinstance(panel,QtWidgets.QWidget) else None,
                )
                owns_executor = True
            application_session = SLMSession(
                runtime=startup.runtime,
                host_services=services,
                cgh_executor=application_executor,
                auto_upload_frame=auto_upload_frame,
                owns_cgh_executor=owns_executor,
                runtime_factory=runtime_factory,
                configuration_service=configuration_service,
                current_config_path=startup.config_path,
                calibration_store=calibration_store,
                startup_preferences=preference_state,
                display_name=display_name,
                apply_startup_calibration_defaults=False,
            )
            session = SLMQtSession(
                application_session=application_session,
                panel=panel,
                interaction_settings=interaction_settings,
                parent=panel,
            )
            if startup.config_path is None:
                application_session.calibration.apply_startup_defaults()
            if startup.warnings:
                panel.set_status("; ".join(startup.warnings),error=True)
            return session,panel
        except Exception:
            if session is not None:
                try:
                    session.dispose()
                except Exception:
                    pass
            elif application_session is not None:
                try:
                    application_session.dispose()
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
    def _display_mode(preferences: StartupPreferencesState) -> SectionsDisplayMode:
        value = preferences.section_display_mode()
        try:
            return SectionsDisplayMode.normalize(value)
        except ValueError:
            return SectionsDisplayMode.TABS


__all__ = ["SLMQtSessionFactory"]
