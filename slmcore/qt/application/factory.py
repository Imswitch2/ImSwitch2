from __future__ import annotations

from pathlib import Path

from qtpy import QtWidgets

from ...application.runtime_factory import SLMRuntimeFactory
from ...cgh.execution.executor import CGHExecutor
from ...corrections import SLMCorrectionStore
from ...host.services import SLMHostServices
from ...engine.registry import DEFAULT_REGISTRIES,SLMRegistries
from ...setup import SLMSetup
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


class SLMQtSessionFactory:
    """Construct the standard reusable Qt session and panel for one SLM.

    The factory owns generic setup resolution, runtime/startup construction,
    persistence wiring, panel construction and session assembly. A workspace
    supplies standard slmcore persistence; explicit host capabilities override
    the workspace defaults. Physical device initialization remains a host-side
    lifecycle decision after the returned session/panel are mounted.
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
        host_services: SLMHostServices | None=None,
        display_name: str="",
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

        workspace = self.workspace
        defaults = None if workspace is None else workspace.default_host_services(setup)
        services = (host_services or SLMHostServices()).with_fallbacks(defaults)
        config_repository = (
            None if workspace is None
            else workspace.config_repository(setup,self.registries)
        )
        correction_store = (
            self._preferred_correction_store(setup)
            if workspace is None
            else workspace.correction_store(setup)
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
        startup_name = None
        if services.configuration_preferences is not None:
            startup_name = services.configuration_preferences.get()
        startup = runtime_factory.create_startup(startup_name)
        display_mode = self._display_mode(services)

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
    def _preferred_correction_store(setup: SLMSetup) -> SLMCorrectionStore | None:
        correction_setup = setup.corrections
        if correction_setup is None or not correction_setup.preferred_directory:
            return None
        directory = Path(correction_setup.preferred_directory).expanduser()
        if not directory.is_dir():
            return None
        return SLMCorrectionStore(
            identity=setup.identity,
            directory=directory,
            wavelength_table_file=correction_setup.wavelength_table_file,
        )

    @staticmethod
    def _display_mode(host_services: SLMHostServices) -> SectionsDisplayMode:
        preferences = host_services.section_view_preferences
        if preferences is None:
            return SectionsDisplayMode.TABS
        value = preferences.get()
        if not value:
            return SectionsDisplayMode.TABS
        try:
            return SectionsDisplayMode.normalize(value)
        except ValueError:
            return SectionsDisplayMode.TABS


__all__ = ["SLMQtSessionFactory"]
