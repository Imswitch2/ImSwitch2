from __future__ import annotations

from dataclasses import replace
from typing import Mapping

from qtpy import QtCore

from ...application.configuration import (
    CalibrationMismatchPolicy,CorrectionMismatchPolicy,
)
from ...core.engine.section.geometry import SectionSplitLayout
from ...core.engine.section.presentation import SectionPresentation
from ...core.engine.state.topology import GroupTopology
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
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.section_host = section_host
        self._disposed = False
        self._connect()

    def _connect(self) -> None:
        self.controller.section_collection.sigSectionTopologyRequested.connect(
            self._on_topology_requested,
        )
        self.section_host.sigSettingsRequested.connect(self.open_settings)
        self.section_host.sigSectionTitleChanged.connect(self._on_title_changed)
        self.section_host.sigFocusedEditingRequested.connect(
            self._on_focused_editing_requested,
        )
        self._sync_focused_editing_controls()

    def _disconnect(self) -> None:
        pairs = (
            (self.controller.section_collection.sigSectionTopologyRequested,self._on_topology_requested),
            (self.section_host.sigSettingsRequested,self.open_settings),
            (self.section_host.sigSectionTitleChanged,self._on_title_changed),
            (self.section_host.sigFocusedEditingRequested,self._on_focused_editing_requested),
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
            section_layout_customizable=self.controller.section_layout_available,
            display_mode=self.section_host.display_mode,
            interaction_settings=self.controller.interaction_settings,
            focused_editing=self._focused_editing_preferences(),
            initial_section_key=self.section_host.current_section_key(),
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

        for section_key,selection in changes.focused_editing.items():
            self._apply_focused_editing_selection(section_key,selection)

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

    def _focused_editing_preferences(self):
        preferences = self.controller.startup_preferences
        if preferences is None:
            return {}
        return {
            key:preferences.focused_editing(key)
            for key in self.controller.section_collection.section_keys
        }

    def _sync_focused_editing_controls(self) -> None:
        preferences = self.controller.startup_preferences
        collection = self.controller.section_collection
        for section_key in collection.section_keys:
            selection = (
                None if preferences is None
                else preferences.focused_editing(section_key)
            )
            configured = bool(
                selection is not None and selection.configured
            )
            was_focused = self.section_host.focused_editing_state(section_key)
            self.section_host.set_focused_editing_available(
                section_key,configured,
            )
            collection.section_view(section_key).set_focused_editing(
                selection,enabled=was_focused and configured,
            )
            self.section_host.set_focused_editing_state(
                section_key,was_focused and configured,
            )

    @QtCore.Slot(str,bool)
    def _on_focused_editing_requested(
        self,section_key: str,enabled: bool,
    ) -> None:
        preferences = self.controller.startup_preferences
        selection = (
            None if preferences is None
            else preferences.focused_editing(section_key)
        )
        if selection is None or not selection.configured:
            self.section_host.set_focused_editing_state(section_key,False)
            return
        try:
            self.controller.section_collection.section_view(
                section_key
            ).set_focused_editing(selection,enabled=bool(enabled))
            self.section_host.set_focused_editing_state(
                section_key,bool(enabled),
            )
        except Exception as error:
            self.section_host.set_focused_editing_state(section_key,False)
            self.controller._emit_exception(
                "SLM focused editing failed",error,
            )

    def _apply_focused_editing_selection(
        self,section_key: str,selection,
    ) -> None:
        preferences = self.controller.startup_preferences
        if preferences is None:
            return
        was_focused = self.section_host.focused_editing_state(section_key)
        try:
            preferences.set_focused_editing(section_key,selection)
            saved = preferences.focused_editing(section_key)
            configured = saved.configured
            self.section_host.set_focused_editing_available(
                section_key,configured,
            )
            self.controller.section_collection.section_view(
                section_key
            ).set_focused_editing(
                saved,enabled=was_focused and configured,
            )
            self.section_host.set_focused_editing_state(
                section_key,was_focused and configured,
            )
        except Exception as error:
            self.controller._emit_exception(
                "SLM focused editing setting failed",error,
            )

    def _apply_display_mode(self,display_mode) -> None:
        mode = SectionsDisplayMode.normalize(display_mode)
        self.section_host.set_display_mode(mode)
        preferences = self.controller.startup_preferences
        if preferences is not None:
            try:
                preferences.set_section_display_mode(mode.value)
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
        self.controller.cancel_cgh(section_key)
        try:
            self.controller.flush_section(section_key,propagate=True)
            self.controller.apply_section_topology(
                section_key,topologies,
            )
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
        try:
            snapshot = self.controller.set_section_presentation(
                section_key,presentation,
            )
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
        try:
            prepared = self.controller.prepare_section_layout_change(layout)
            if not prepared.changed:
                return
        except Exception as error:
            self.controller._emit_exception("SLM section layout failed",error)
            return

        policy = None
        mismatches = prepared.calibration_mismatches
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
            policy = (
                CalibrationMismatchPolicy.CLEAR
                if decision is CalibrationMismatchDecision.CLEAR
                else CalibrationMismatchPolicy.KEEP
            )
        else:
            if not confirm_destructive_change(
                self.section_host,
                "Apply SLM section layout",
                "Changing the section layout will clear pending CGH work, measurements, "
                "localization feedback, and current CGH results. Continue?",
            ):
                return
            policy = CalibrationMismatchPolicy.KEEP

        correction_policy = CorrectionMismatchPolicy.REJECT
        if prepared.saved_correction_sections:
            names = ", ".join(prepared.saved_correction_sections)
            if not confirm_destructive_change(
                self.section_host,
                "Switch correction source",
                "The new section geometry is incompatible with saved correction "
                f"snapshots for: {names}. Continuing will switch those sections "
                "to the current workspace corrections. Continue?",
            ):
                return
            correction_policy = CorrectionMismatchPolicy.USE_CURRENT

        try:
            changed = self.controller.apply_prepared_section_layout_change(
                prepared,
                calibration_mismatch_policy=policy,
                correction_mismatch_policy=correction_policy,
                topologies_by_section=topologies_by_section,
                presentations=presentations,
            )
            if changed and self.controller.last_upload_error is None:
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
