from __future__ import annotations

from dataclasses import replace
from typing import Mapping

from qtpy import QtCore

from ...setup import SLMSetup
from ...application.runtime_factory import SLMRuntimeFactory
from ...calibration.geometry import calibration_geometry_mismatches
from ...engine.section.geometry import (
    SectionSplitLayout,create_split_section_geometries,split_layout_signature,
)
from ...engine.section.presentation import SectionPresentation
from ...engine.state.topology import GroupTopology
from ..calibration.geometry_dialogs import (
    CalibrationMismatchDecision,calibration_mismatch_decision,
    confirm_destructive_change,
)
from ..sections.display import SectionsDisplayMode
from ..sections.settings import SLMSectionsSettingsDialog


class SectionSettingsManager(QtCore.QObject):
    """Reusable topology/presentation/layout workflow for one SLM Qt session."""

    def __init__(
        self,
        controller,
        *,
        section_host,
        setup: SLMSetup | None,
        runtime_factory: SLMRuntimeFactory | None,
        view_preferences=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.section_host = section_host
        self.setup = setup
        self.runtime_factory = runtime_factory
        self.view_preferences = view_preferences
        self._disposed = False
        self._connect()

    def _connect(self) -> None:
        self.controller.section_collection.sigSectionTopologyRequested.connect(
            self._on_topology_requested,
        )
        self.section_host.sigSettingsRequested.connect(self.open_settings)
        self.section_host.sigSectionTitleChanged.connect(self._on_title_changed)

    def _disconnect(self) -> None:
        pairs = (
            (self.controller.section_collection.sigSectionTopologyRequested,self._on_topology_requested),
            (self.section_host.sigSettingsRequested,self.open_settings),
            (self.section_host.sigSectionTitleChanged,self._on_title_changed),
        )
        for signal,slot in pairs:
            try: signal.disconnect(slot)
            except (RuntimeError,TypeError): pass

    def open_settings(self) -> None:
        if (
            not self.controller.editor_writes_allowed
            or self.controller.automatic_operation_active
        ):
            return
        collection = self.controller.section_collection
        changes = SLMSectionsSettingsDialog.get_changes(
            section_snapshots=collection.get_section_snapshots(),
            section_titles=self.section_host.section_titles(),
            section_layout_customizable=bool(
                self.setup is not None and self.setup.sections.customizable
            ),
            display_mode=self.section_host.display_mode,
            interaction_settings=self.controller.interaction_settings,
            title="%s settings" % self.controller.display_name,
            parent=self.section_host,
        )
        if not changes:
            return

        if changes.interaction_settings is not None:
            self.controller.set_interaction_settings(changes.interaction_settings)
            self.controller.sigInteractionSettingsChanged.emit(changes.interaction_settings)

        if changes.display_mode is not None:
            self._apply_display_mode(changes.display_mode)

        if changes.layout is not None:
            self._apply_layout(
                changes.layout,
                topologies_by_section=changes.topologies,
                presentations=changes.presentations,
            )
            return

        for section_key,topologies in changes.topologies.items():
            self._apply_topology(section_key,topologies)
        for section_key,presentation in changes.presentations.items():
            self._apply_presentation(section_key,presentation)

    def _apply_display_mode(self,display_mode) -> None:
        mode = SectionsDisplayMode.normalize(display_mode)
        self.section_host.set_display_mode(mode)
        if self.view_preferences is not None:
            try:
                self.view_preferences.set(mode.value)
            except Exception as error:
                self.controller._emit_exception("SLM section view setting failed",error)

    @QtCore.Slot(str,object)
    def _on_topology_requested(self,section_key: str,topologies: object) -> None:
        if (
            not self.controller.editor_writes_allowed
            or self.controller.automatic_operation_active
        ):
            return
        self._apply_topology(section_key,dict(topologies or {}))

    def _apply_topology(
        self,section_key: str,topologies: Mapping[str,GroupTopology],
    ) -> None:
        if not self.controller.editor_writes_allowed:
            return
        runtime = self.controller.runtime
        self.controller.cancel_cgh(section_key)
        try:
            self.controller.flush_section(section_key,propagate=True)
            transition = runtime.apply_section_topology(section_key,topologies)
            if transition is not None:
                self.controller.apply_transition(section_key,transition)
        except Exception as error:
            self.controller._restore_section(section_key)
            self.controller._emit_exception("SLM section settings failed",error)

    @QtCore.Slot(str,str)
    def _on_title_changed(self,section_key: str,title: str) -> None:
        if not self.controller.editor_writes_allowed:
            return
        snapshot = self.controller.runtime.get_section_snapshot(section_key)
        presentation = replace(
            snapshot.presentation,title=str(title or "").strip() or None,
        )
        self._apply_presentation(section_key,presentation)

    def _apply_presentation(
        self,section_key: str,presentation: SectionPresentation,
    ) -> None:
        if not self.controller.editor_writes_allowed:
            return
        runtime = self.controller.runtime
        try:
            snapshot = runtime.set_section_presentation(section_key,presentation)
            if snapshot is None:
                return
            self.controller.section_collection.apply_section_presentation(
                section_key,snapshot,
            )
            self.section_host.set_section_title(
                section_key,getattr(snapshot.presentation,"title",None),
            )
        except Exception as error:
            self.controller._restore_section(section_key)
            self.controller._emit_exception(
                "SLM section interface settings failed",error,
            )

    def _apply_layout(
        self,
        layout: SectionSplitLayout,
        *,
        topologies_by_section,
        presentations,
    ) -> None:
        if not self.controller.editor_writes_allowed:
            return
        setup = self.setup
        factory = self.runtime_factory
        runtime = self.controller.runtime
        try:
            if setup is None or factory is None:
                raise RuntimeError("Section layout editing is not configured")
            if not setup.sections.customizable:
                raise ValueError("This setup does not allow section layout editing.")
            if not isinstance(layout,SectionSplitLayout):
                raise TypeError("layout must be a SectionSplitLayout")
            if layout.n_sections != setup.section_count:
                raise ValueError("Changing section count is not supported")
            section_geometries = create_split_section_geometries(
                runtime.geometry,layout,
            )
            setup.validate_layout(runtime.geometry,section_geometries)
            current_signature = split_layout_signature(
                runtime.geometry,
                {key:runtime.get_section_geometry(key) for key in runtime.section_keys},
            )
            requested_signature = split_layout_signature(
                runtime.geometry,section_geometries,
            )
            if requested_signature == current_signature:
                return
        except Exception as error:
            self.controller._emit_exception("SLM section layout failed",error)
            return

        mismatches = calibration_geometry_mismatches(
            (
                key,section_geometries[key],runtime.get_section_calibration_copy(key)
            )
            for key in runtime.section_keys
        )
        clear_sections = ()
        if mismatches:
            decision = calibration_mismatch_decision(
                self.section_host,
                title="Apply SLM section layout",
                message=(
                    "Changing the section layout clears pending CGH work, measurements, "
                    "localization feedback and current CGH results. Some current calibrations "
                    "were measured with a different section geometry."
                ),
                mismatches=mismatches,
                allow_clear=True,
            )
            if decision is CalibrationMismatchDecision.CANCEL:
                return
            if decision is CalibrationMismatchDecision.CLEAR:
                clear_sections = tuple(item.section_key for item in mismatches)
        elif not confirm_destructive_change(
            self.section_host,
            "Apply SLM section layout",
            "Changing the section layout will clear pending CGH work, measurements, "
            "localization feedback, and current CGH results. Continue?",
        ):
            return

        try:
            replacement = factory.create_layout_replacement(
                runtime,section_geometries,
                clear_calibration_sections=clear_sections,
                topologies_by_section=topologies_by_section,
                presentations=presentations,
            )
            self.controller.replace_runtime(replacement)
            if self.controller.last_upload_error is None:
                self.controller.sigStatusChanged.emit(
                    "Section layout updated.",False,
                )
        except Exception as error:
            self.controller._emit_exception("SLM section layout failed",error)

    def prepare_runtime_replacement(self) -> None:
        self._disconnect()

    def runtime_replaced(self) -> None:
        self._connect()

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disconnect()
        self._disposed = True
