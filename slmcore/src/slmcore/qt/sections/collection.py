"""Tab-agnostic collection of retained SLM section views."""

from __future__ import annotations

from typing import Mapping

from qtpy import QtCore

from ...core.config.loading import SLMConfigLoadReport
from ...core.engine.section.snapshot import SLMSectionSnapshot
from ...core.engine.section.update import SectionUpdate
from ...core.engine.transition import SectionStateTransition
from .group_factory import DEFAULT_GROUP_VIEW_FACTORY,GroupViewFactory
from .policy import DEFAULT_RENDER_POLICY,RenderPolicy
from .view import SectionView


class SectionsCollectionView(QtCore.QObject):
    """Own the canonical collection of retained ``SectionView`` objects.

    The collection mirrors backend section ownership and synchronization, but
    deliberately makes no layout decision. Hosts may place the returned section
    widgets in tabs, splitters, columns, separate windows, or any other layout.
    """

    sigSectionPatchRequested = QtCore.Signal(str,object)
    sigSectionTopologyRequested = QtCore.Signal(str,object)
    sigActivePlaneRequested = QtCore.Signal(str,str)
    sigAddPlaneRequested = QtCore.Signal(str)
    sigDeletePlaneRequested = QtCore.Signal(str,str)
    sigCalibrationRequested = QtCore.Signal(str)
    sigCghActionRequested = QtCore.Signal(str,str,object)
    sigTargetLockRequested = QtCore.Signal(str,str,object)

    def __init__(
        self,
        *,
        section_snapshots: Mapping[str,SLMSectionSnapshot],
        render_policy: RenderPolicy=DEFAULT_RENDER_POLICY,
        group_factory: GroupViewFactory | None=None,
        parent: QtCore.QObject | None=None,
    ) -> None:
        super().__init__(parent)

        self.render_policy = render_policy
        self.group_factory = (
            DEFAULT_GROUP_VIEW_FACTORY
            if group_factory is None else group_factory
        )
        self.sections: dict[str, SectionView] = {}
        self.section_order: list[str] = []

        for section_key,snapshot in section_snapshots.items():
            self.add_section(section_key,snapshot)

    def add_section(
        self,section_key: str,snapshot: SLMSectionSnapshot,
    ) -> SectionView:
        """Add one section in authoritative display order."""
        if section_key in self.sections:
            raise KeyError(f"Section '{section_key}' is already registered")

        view = self._create_section_view(section_key,snapshot)
        self.sections[section_key] = view
        self.section_order.append(section_key)
        return view

    def section_view(self,section_key: str) -> SectionView:
        try:
            return self.sections[section_key]
        except KeyError as error:
            raise KeyError(f"Unknown SLM section '{section_key}'") from error

    @property
    def section_keys(self) -> tuple[str, ...]:
        return tuple(self.section_order)

    def get_section_snapshot(self,section_key: str) -> SLMSectionSnapshot:
        return self.section_view(section_key).snapshot

    def get_section_snapshots(self) -> Mapping[str,SLMSectionSnapshot]:
        """Return current detached snapshots in section display order."""
        return {
            key:self.sections[key].snapshot
            for key in self.section_order
        }

    def apply_section_update(
        self,section_key: str,update: SectionUpdate,
    ) -> None:
        self.section_view(section_key).apply_update(update)

    def apply_section_transition(
        self,section_key: str,transition: SectionStateTransition,
    ) -> None:
        self.section_view(section_key).apply_transition(transition)

    def apply_section_presentation(
        self,section_key: str,snapshot: SLMSectionSnapshot,
    ) -> None:
        """Synchronize a same-revision presentation-only snapshot."""
        self.section_view(section_key).set_snapshot(snapshot)

    def apply_config_report(
        self,
        report: SLMConfigLoadReport,
        *,
        failed_section_snapshots: Mapping[str, SLMSectionSnapshot] | None=None,
    ) -> dict[str, Exception]:
        """Synchronize every section and return only unrecovered UI failures.

        Successful backend sections first use their semantic config deltas. If
        incremental reconciliation fails, the complete committed snapshot is
        restored in place. Backend-failed sections are restored from the
        authoritative runtime snapshots supplied by the host.
        """
        ui_failures: dict[str, Exception] = {}

        for section_key,result in report.section_results.items():
            try:
                view = self.section_view(section_key)
                try:
                    view.apply_config_result(result)
                except Exception:
                    view.restore_snapshot(result.snapshot)
            except Exception as error:
                ui_failures[section_key] = error

        snapshots = dict(failed_section_snapshots or {})
        for section_key in report.failed_sections:
            try:
                snapshot = snapshots[section_key]
                self.section_view(section_key).restore_snapshot(snapshot)
            except Exception as error:
                ui_failures[section_key] = error

        return ui_failures

    def restore_section(
        self,section_key: str,snapshot: SLMSectionSnapshot,
    ) -> None:
        """Restore one retained section from an authoritative snapshot."""
        self.section_view(section_key).restore_snapshot(snapshot)

    def set_available_planes(
        self,section_key: str,plane_names,active_plane: str | None=None,
    ) -> None:
        self.section_view(section_key).set_available_planes(
            plane_names,active_plane,
        )

    def get_active_plane(self,section_key: str) -> str | None:
        return self.section_view(section_key).get_active_plane()

    def set_cgh_target_presentation(
        self,section_key: str,summary: Mapping[str,Any],
    ) -> None:
        self.section_view(section_key).set_cgh_target_presentation(summary)

    def set_cgh_computing(
        self,section_key: str,computing: bool,
    ) -> None:
        self.section_view(section_key).set_cgh_computing(computing)

    def set_feedback_status(self,section_key: str,status) -> None:
        self.section_view(section_key).apply_feedback_status(status)

    def auto_recompute_enabled(self,section_key: str) -> bool:
        return self.section_view(section_key).auto_recompute_enabled()

    def set_auto_recompute_enabled(
        self,section_key: str,enabled: bool,
    ) -> None:
        self.section_view(section_key).set_auto_recompute_enabled(enabled)

    def auto_recompute_preferences(self) -> Mapping[str,bool]:
        return {
            key:self.section_view(key).auto_recompute_enabled()
            for key in self.section_order
        }

    def apply_auto_recompute_preferences(
        self,preferences: Mapping[str,bool],
    ) -> None:
        for key,value in dict(preferences or {}).items():
            if key in self.sections:
                self.set_auto_recompute_enabled(key,bool(value))

    def _create_section_view(
        self,section_key: str,snapshot: SLMSectionSnapshot,
    ) -> SectionView:
        view = SectionView(
            section_key=section_key,
            snapshot=snapshot,
            render_policy=self.render_policy,
            group_factory=self.group_factory,
        )
        view.sigPatchRequested.connect(
            self.sigSectionPatchRequested.emit,
        )
        view.sigTopologyRequested.connect(
            self.sigSectionTopologyRequested.emit,
        )
        view.sigActivePlaneRequested.connect(
            self.sigActivePlaneRequested.emit,
        )
        view.sigAddPlaneRequested.connect(
            self.sigAddPlaneRequested.emit,
        )
        view.sigDeletePlaneRequested.connect(
            self.sigDeletePlaneRequested.emit,
        )
        view.sigCalibrationRequested.connect(
            self.sigCalibrationRequested.emit,
        )
        view.sigCghActionRequested.connect(
            self.sigCghActionRequested.emit,
        )
        view.sigTargetLockRequested.connect(
            self.sigTargetLockRequested.emit,
        )
        return view
