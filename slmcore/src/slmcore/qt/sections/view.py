"""Qt projection of one committed :class:`slmcore.SLMSectionSnapshot`."""

from __future__ import annotations

from typing import Any,Mapping

from qtpy import QtCore,QtWidgets

from ...core.config.loading import SectionConfigLoadResult
from ...core.calibration.geometry import calibration_geometry_matches
from ...core.engine.parameters.converters import METRIC_UNIT,SLM_UNIT
from ...core.engine.section.presentation import SectionPresentation
from ...core.engine.section.snapshot import SectionGroupSnapshot,SLMSectionSnapshot
from ...core.engine.section.update import SectionUpdate
from ...core.engine.state.base import ParamPath
from ...core.engine.state.groups import DynamicGroupState
from ...core.engine.transition import SectionStateTransition
from .group_factory import DEFAULT_GROUP_VIEW_FACTORY,GroupViewFactory
from .group_views import BaseGroupView,CghGroupView,GroupPresentationState
from .policy import DEFAULT_RENDER_POLICY,RenderPolicy
from .topology import SectionTopologyDialog


def _has_valid_calibration(calibration: Any) -> bool:
    if calibration is None:
        return False
    validator = getattr(calibration,"is_valid",None)
    return bool(validator()) if callable(validator) else True


def _calibration_data(calibration: Any):
    if calibration is None:
        return None
    serializer = getattr(calibration,"to_dict",None)
    return serializer() if callable(serializer) else calibration


class SectionView(QtWidgets.QWidget):
    """Retained Qt representation of one backend SLM section.

    The view never owns or mutates an ``SLMSectionRuntime``. It emits requested
    canonical patches and applies committed snapshots, updates and config-load
    results supplied by the application/controller.
    """

    sigPatchRequested = QtCore.Signal(str,object)  # section_key, changes
    sigTopologyRequested = QtCore.Signal(str,object)  # section_key, topologies
    sigActivePlaneRequested = QtCore.Signal(str,str)  # section_key, plane
    sigAddPlaneRequested = QtCore.Signal(str)  # section_key
    sigDeletePlaneRequested = QtCore.Signal(str,str)  # section_key, plane
    sigCalibrationRequested = QtCore.Signal(str)  # section_key
    sigCghActionRequested = QtCore.Signal(str,str,object)
    sigTargetLockRequested = QtCore.Signal(str,str,object)

    def __init__(
        self,
        *,
        section_key: str,
        snapshot: SLMSectionSnapshot,
        render_policy: RenderPolicy=DEFAULT_RENDER_POLICY,
        group_factory: GroupViewFactory | None=None,
        parent: QtWidgets.QWidget | None=None,
    ) -> None:
        super().__init__(parent)

        self.section_key = section_key
        self.render_policy = render_policy
        self.group_factory = (
            DEFAULT_GROUP_VIEW_FACTORY
            if group_factory is None else group_factory
        )
        self.groups: dict[str, BaseGroupView] = {}
        self.state_key_to_group: dict[str, str] = {}
        self.group_to_state_key: dict[str, str] = {}
        self.unit_mode = SLM_UNIT
        self.calibration = snapshot.calibration
        self.presentation = snapshot.presentation
        self._snapshot = snapshot
        self._revision = snapshot.revision
        self._cgh_computing = False
        self._feedback_status: Any = None

        self.content_layout = QtWidgets.QVBoxLayout(self)
        self.content_layout.setContentsMargins(4,4,4,4)
        self.content_layout.setSpacing(6)

        self._controls_row: QtWidgets.QHBoxLayout | None = None
        self.calibration_interface_widget: QtWidgets.QWidget | None = None
        self.unit_label: QtWidgets.QLabel | None = None
        self.slm_unit_button: QtWidgets.QPushButton | None = None
        self.metric_unit_button: QtWidgets.QPushButton | None = None
        self.calibration_label: QtWidgets.QLabel | None = None
        self.active_plane_combo: QtWidgets.QComboBox | None = None
        self.calibration_button: QtWidgets.QPushButton | None = None
        self.plane_more_button: QtWidgets.QToolButton | None = None
        self.add_plane_action: QtWidgets.QAction | None = None
        self.delete_plane_action: QtWidgets.QAction | None = None
        self.topology_settings_button: QtWidgets.QPushButton | None = None
        if (
            self.render_policy.show_unit_controls
            or self.render_policy.show_calibration_controls
            or self.render_policy.show_topology_settings
        ):
            self._build_section_controls()

        self._reindex_snapshot(snapshot)
        for entry in snapshot.group_entries():
            if not entry.state.enabled:
                continue
            group_view = self._create_group_view(entry)
            self.groups[entry.group_key] = group_view
            self.content_layout.addWidget(group_view.widget)

        self.content_layout.addStretch(1)
        self._refresh_unit_controls()
        self._refresh_calibration_controls()
        self._refresh_presentation_controls()
        self._refresh_topology_controls()
        self.apply_cgh_status(snapshot.cgh_status)

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def snapshot(self) -> SLMSectionSnapshot:
        """Return the detached view-side snapshot used for structure/state."""
        return self._snapshot

    def _build_section_controls(self) -> None:
        row = QtWidgets.QHBoxLayout()
        self._controls_row = row

        has_calibration_interface = (
            self.render_policy.show_unit_controls
            or self.render_policy.show_calibration_controls
        )
        if has_calibration_interface:
            interface = QtWidgets.QWidget(self)
            interface_layout = QtWidgets.QHBoxLayout(interface)
            interface_layout.setContentsMargins(0,0,0,0)
            interface_layout.setSpacing(4)
            self.calibration_interface_widget = interface

        else:
            interface_layout = None
            row.addStretch(1)

        if self.render_policy.show_unit_controls and interface_layout is not None:
            self.slm_unit_button = QtWidgets.QPushButton("SLM")
            self.metric_unit_button = QtWidgets.QPushButton("Metric")
            for button in (self.slm_unit_button,self.metric_unit_button):
                button.setCheckable(True)
                button.setFixedHeight(22)

            group = QtWidgets.QButtonGroup(self)
            group.setExclusive(True)
            group.addButton(self.slm_unit_button)
            group.addButton(self.metric_unit_button)
            self._unit_button_group = group
            self.slm_unit_button.setChecked(True)

            self.slm_unit_button.toggled.connect(
                lambda checked:checked and self.set_unit_mode(SLM_UNIT),
            )
            self.metric_unit_button.toggled.connect(
                lambda checked:checked and self.set_unit_mode(METRIC_UNIT),
            )

            self.unit_label = QtWidgets.QLabel("Units:")
            interface_layout.addWidget(self.unit_label)
            interface_layout.addWidget(self.slm_unit_button)
            interface_layout.addWidget(self.metric_unit_button)

        if interface_layout is not None:
            interface_layout.addStretch(1)

        if (
            self.render_policy.show_unit_controls
            or self.render_policy.show_calibration_controls
        ) and interface_layout is not None:
            self.calibration_label = QtWidgets.QLabel()
            self.calibration_label.setStyleSheet("color: #888;")
            interface_layout.addWidget(self.calibration_label)

        if (
            self.render_policy.show_calibration_controls
            and interface_layout is not None
        ):
            interface_layout.addWidget(QtWidgets.QLabel("Plane:"))

            combo = QtWidgets.QComboBox()
            combo.setMinimumWidth(130)
            combo.currentTextChanged.connect(
                self._on_active_plane_changed,
            )
            self.active_plane_combo = combo
            interface_layout.addWidget(combo)

            calibrate = QtWidgets.QPushButton("Calibrate")
            calibrate.setFixedHeight(22)
            calibrate.clicked.connect(
                lambda _checked=False:self.sigCalibrationRequested.emit(
                    self.section_key,
                )
            )
            self.calibration_button = calibrate
            interface_layout.addWidget(calibrate)

            more = QtWidgets.QToolButton()
            more.setText("More")
            more.setFixedHeight(22)
            more.setPopupMode(QtWidgets.QToolButton.InstantPopup)
            menu = QtWidgets.QMenu(more)
            self.add_plane_action = menu.addAction(
                "Add plane...",
                lambda:self.sigAddPlaneRequested.emit(self.section_key),
            )
            self.delete_plane_action = menu.addAction(
                "Delete plane...",
                self._request_delete_plane,
            )
            more.setMenu(menu)
            self.plane_more_button = more
            interface_layout.addWidget(more)

        if self.calibration_interface_widget is not None:
            row.addWidget(self.calibration_interface_widget,1)

        if self.render_policy.show_topology_settings:
            button = QtWidgets.QPushButton("Settings...")
            button.setFixedHeight(22)
            button.setToolTip(
                "Add or remove registry-backed groups and items for this section"
            )
            button.clicked.connect(self.open_topology_settings)
            self.topology_settings_button = button
            row.addWidget(button)

        self.content_layout.addLayout(row)

    def _on_active_plane_changed(self,plane_name: str) -> None:
        self._refresh_calibration_controls()
        self.sigActivePlaneRequested.emit(
            self.section_key,str(plane_name or ""),
        )

    def _request_delete_plane(self) -> None:
        plane_name = self.get_active_plane()
        if plane_name:
            self.sigDeletePlaneRequested.emit(
                self.section_key,plane_name,
            )

    @property
    def has_editable_topology(self) -> bool:
        """Whether this section exposes registry-backed topology choices."""
        return any(
            isinstance(entry.state,DynamicGroupState)
            for entry in self._snapshot.group_entries()
        )

    def open_topology_settings(self) -> None:
        """Open the reusable topology editor for this retained section."""
        if not self.has_editable_topology:
            return
        changes = SectionTopologyDialog.get_changes(
            snapshot=self._snapshot,
            parent=self,
        )
        if changes:
            self.sigTopologyRequested.emit(
                self.section_key,dict(changes),
            )

    def _refresh_topology_controls(self) -> None:
        button = self.topology_settings_button
        if button is None:
            return
        editable = self.has_editable_topology
        button.setVisible(editable)
        button.setEnabled(editable)

    def _create_group_view(
        self,entry: SectionGroupSnapshot,
    ) -> BaseGroupView:
        group_view = self.group_factory.create(
            entry=entry,
            conversion_context=lambda:self.calibration,
            on_edit=self._on_group_edit,
            render_policy=self.render_policy,
        )
        group_view.set_unit_mode(self.unit_mode)
        group_view.set_action_handler(self._on_group_action)
        if isinstance(group_view,CghGroupView):
            group_view.set_target_lock_handler(
                lambda target_key,kind:self.sigTargetLockRequested.emit(
                    self.section_key,target_key,kind,
                )
            )
            group_view.set_computing(self._cgh_computing)
            if self._feedback_status is not None:
                group_view.apply_feedback_status(self._feedback_status)
        return group_view

    def _on_group_action(
        self,action: str,options: Any,
    ) -> None:
        self.sigCghActionRequested.emit(
            self.section_key,str(action),dict(options or {}),
        )

    def _on_group_edit(
        self,state_key: str,changes: Mapping[ParamPath,Any],
    ) -> None:
        full_changes = {
            (state_key,) + tuple(relative_path):value
            for relative_path,value in changes.items()
        }
        self.sigPatchRequested.emit(
            self.section_key,full_changes,
        )

    def auto_recompute_enabled(self) -> bool:
        for group_view in self.groups.values():
            if isinstance(group_view,CghGroupView):
                return group_view.auto_recompute_enabled()
        return False

    def set_auto_recompute_enabled(self,enabled: bool) -> None:
        for group_view in self.groups.values():
            if isinstance(group_view,CghGroupView):
                group_view.set_auto_recompute_enabled(enabled)
                return

    def apply_update(self,update: SectionUpdate) -> None:
        """Apply a committed update against the expected snapshot revision."""
        snapshot = self._snapshot.apply_update(update)
        for path,value in update.applied_values.items():
            self.set_parameter(path,value)
        for group_view in self.groups.values():
            if isinstance(group_view,CghGroupView):
                group_view.sync_target_lock_states(snapshot.state.cgh)
        self.apply_cgh_status(snapshot.cgh_status)
        self.set_snapshot(snapshot)

    def apply_transition(
        self,
        transition: SectionStateTransition,
        *,
        apply_cgh_status: bool=True,
    ) -> None:
        """Reconcile one generic committed section-state transition."""
        if transition.base_revision != self._revision:
            raise RuntimeError(
                "Section transition revision mismatch: "
                f"view={self._revision}, "
                f"transition base={transition.base_revision}"
            )

        snapshot = transition.snapshot
        for entry in snapshot.group_entries():
            delta = transition.group_deltas.get(entry.group_key)
            if delta is None:
                continue
            if delta.topology_changed:
                self.replace_group(snapshot,entry.group_key)
                continue
            for path,value in delta.changed_values.items():
                self.set_parameter(path,value)

        if transition.calibration_changed:
            self.apply_calibration(snapshot.calibration)
        if apply_cgh_status:
            self.apply_cgh_status(snapshot.cgh_status)
        self.set_snapshot(snapshot)

    def apply_config_result(
        self,result: SectionConfigLoadResult,
    ) -> None:
        """Reconcile config loading through the generic transition path."""
        self.apply_transition(
            result.transition,
            apply_cgh_status=result.cgh_session_restored,
        )

    def restore_snapshot(self,snapshot: SLMSectionSnapshot) -> None:
        """Fully restore this retained view from an authoritative snapshot."""
        group_presentation_states = {
            key:view.capture_presentation_state()
            for key,view in self.groups.items()
        }

        for group_view in tuple(self.groups.values()):
            self.content_layout.removeWidget(group_view.widget)
            group_view.dispose()
        self.groups.clear()

        self.calibration = snapshot.calibration
        self.presentation = snapshot.presentation
        if (
            self.unit_mode == METRIC_UNIT
            and (
                not self.presentation.show_calibration_interface
                or not _has_valid_calibration(self.calibration)
            )
        ):
            self.unit_mode = SLM_UNIT

        self._snapshot = snapshot
        self._revision = snapshot.revision
        self._reindex_snapshot(snapshot)

        for entry in snapshot.group_entries():
            if not entry.state.enabled:
                continue
            group_view = self._create_group_view(entry)
            state = group_presentation_states.get(entry.group_key)
            if state is not None:
                group_view.restore_presentation_state(state)
            self.groups[entry.group_key] = group_view
            self.content_layout.insertWidget(
                max(0,self.content_layout.count() - 1),
                group_view.widget,
            )

        self._sync_unit_buttons(self.unit_mode)
        self._sync_plane_from_calibration()
        self._refresh_unit_controls()
        self._refresh_calibration_controls()
        self._refresh_presentation_controls()
        self._refresh_topology_controls()
        self.apply_cgh_status(snapshot.cgh_status)

    def set_parameter(self,path: ParamPath,value: Any) -> bool:
        path = tuple(path)
        if not path:
            return False
        group_key = self.state_key_to_group.get(path[0])
        group_view = self.groups.get(group_key) if group_key else None
        if group_view is None:
            return False
        return group_view.set_parameter(path[1:],value)

    def apply_group_state(self,entry: SectionGroupSnapshot) -> None:
        group_view = self.groups.get(entry.group_key)
        if group_view is None:
            raise KeyError(
                f"Group '{entry.group_key}' does not exist in "
                f"section '{self.section_key}'"
            )
        group_view.apply_state(entry.state)
        self._index_group_entry(entry)

    def replace_group(
        self,snapshot: SLMSectionSnapshot,group_key: str,
    ) -> None:
        """Atomically replace one group from a committed snapshot."""
        entry = snapshot.group_entry(group_key)
        old = self.groups.get(group_key)
        group_presentation_state = (
            old.capture_presentation_state()
            if old is not None else GroupPresentationState()
        )

        if not entry.state.enabled:
            if old is not None:
                self.content_layout.removeWidget(old.widget)
                self.groups.pop(group_key,None)
                old.dispose()
            self._index_group_entry(entry)
            self._refresh_unit_controls()
            self._refresh_presentation_controls()
            return

        replacement = self._create_group_view(entry)
        replacement.restore_presentation_state(group_presentation_state)

        if old is not None:
            self.content_layout.replaceWidget(old.widget,replacement.widget)
            self.groups[group_key] = replacement
            old.dispose()
        else:
            position = self._group_layout_position(snapshot,group_key)
            self.content_layout.insertWidget(position,replacement.widget)
            self.groups[group_key] = replacement

        self._index_group_entry(entry)
        self._refresh_unit_controls()
        self._refresh_presentation_controls()

    def _group_layout_position(
        self,snapshot: SLMSectionSnapshot,group_key: str,
    ) -> int:
        position = 1 if self._controls_row is not None else 0
        for entry in snapshot.group_entries():
            if entry.group_key == group_key:
                break
            if entry.state.enabled:
                position += 1
        return min(position,max(0,self.content_layout.count() - 1))

    def _reindex_snapshot(self,snapshot: SLMSectionSnapshot) -> None:
        self.state_key_to_group.clear()
        self.group_to_state_key.clear()
        for entry in snapshot.group_entries():
            self._index_group_entry(entry)

    def _index_group_entry(self,entry: SectionGroupSnapshot) -> None:
        previous = self.group_to_state_key.get(entry.group_key)
        if previous is not None:
            self.state_key_to_group.pop(previous,None)
        self.group_to_state_key[entry.group_key] = entry.state_key
        self.state_key_to_group[entry.state_key] = entry.group_key

    def apply_calibration(self,calibration: Any) -> None:
        self.calibration = calibration
        if self.unit_mode == METRIC_UNIT and not _has_valid_calibration(calibration):
            self.set_unit_mode(SLM_UNIT)
        for group_view in self.groups.values():
            group_view.refresh_conversions()
        self._sync_plane_from_calibration()
        self._refresh_unit_controls()
        self._refresh_calibration_controls()
        self._refresh_presentation_controls()

    def apply_presentation(self,presentation: SectionPresentation) -> None:
        if not isinstance(presentation,SectionPresentation):
            raise TypeError(
                "presentation must be a SectionPresentation"
            )

        if (
            not presentation.show_calibration_interface
            and self.unit_mode == METRIC_UNIT
        ):
            self.set_unit_mode(SLM_UNIT)

        self.presentation = presentation.copy()
        self._refresh_unit_controls()
        self._refresh_calibration_controls()
        self._refresh_presentation_controls()

    def set_available_planes(
        self,plane_names,active_plane: str | None=None,
    ) -> None:
        """Set host-provided planes without emitting a selection request."""
        combo = self.active_plane_combo
        if combo is None:
            return

        names = [str(name) for name in (plane_names or ())]
        blocker = QtCore.QSignalBlocker(combo)
        try:
            combo.clear()
            combo.addItems(names)
            index = combo.findText(str(active_plane)) if active_plane else -1
            combo.setCurrentIndex(index)
        finally:
            del blocker
        self._refresh_calibration_controls()

    def get_active_plane(self) -> str | None:
        combo = self.active_plane_combo
        if combo is None or combo.currentIndex() < 0:
            return None
        return combo.currentText().strip() or None

    def _sync_plane_from_calibration(self) -> None:
        combo = self.active_plane_combo
        if combo is None:
            return
        plane = getattr(self.calibration,"plane",None)
        index = combo.findText(str(plane)) if plane else -1
        blocker = QtCore.QSignalBlocker(combo)
        try:
            combo.setCurrentIndex(index)
        finally:
            del blocker

    def set_unit_mode(self,mode: str) -> bool:
        if mode not in (SLM_UNIT,METRIC_UNIT):
            raise ValueError(f"Unknown SLM unit mode '{mode}'")
        if (
            mode == METRIC_UNIT
            and not self.presentation.show_calibration_interface
        ):
            self._sync_unit_buttons(SLM_UNIT)
            return False
        if mode == METRIC_UNIT and not _has_valid_calibration(self.calibration):
            self._sync_unit_buttons(SLM_UNIT)
            return False

        for group_view in self.groups.values():
            group_view.set_unit_mode(mode)
        self.unit_mode = mode
        self._sync_unit_buttons(mode)
        return True

    def _sync_unit_buttons(self,mode: str) -> None:
        if self.slm_unit_button is None or self.metric_unit_button is None:
            return
        slm_blocker = QtCore.QSignalBlocker(self.slm_unit_button)
        metric_blocker = QtCore.QSignalBlocker(self.metric_unit_button)
        try:
            self.slm_unit_button.setChecked(mode == SLM_UNIT)
            self.metric_unit_button.setChecked(mode == METRIC_UNIT)
        finally:
            del slm_blocker
            del metric_blocker

    def _refresh_unit_controls(self) -> None:
        if self.slm_unit_button is None or self.metric_unit_button is None:
            return

        has_converters = any(
            group_view.binding.has_converters
            for group_view in self.groups.values()
        )
        valid = _has_valid_calibration(self.calibration)
        if self.unit_label is not None:
            self.unit_label.setVisible(has_converters)
        self.slm_unit_button.setVisible(has_converters)
        self.metric_unit_button.setVisible(has_converters)
        self.metric_unit_button.setEnabled(valid)

        if (
            self.unit_mode == METRIC_UNIT
            and (
                not valid
                or not self.presentation.show_calibration_interface
            )
        ):
            self.set_unit_mode(SLM_UNIT)

    def _refresh_presentation_controls(self) -> None:
        widget = self.calibration_interface_widget
        if widget is None:
            return
        if (
            not self.presentation.show_calibration_interface
            and self.unit_mode == METRIC_UNIT
        ):
            self.set_unit_mode(SLM_UNIT)
        widget.setVisible(self.presentation.show_calibration_interface)

    def _refresh_calibration_controls(self) -> None:
        label = self.calibration_label
        valid = _has_valid_calibration(self.calibration)
        plane = self.get_active_plane()

        if label is not None:
            if valid:
                kx = float(getattr(self.calibration,"kx_per_um",0.0))
                ky = float(getattr(self.calibration,"ky_per_um",0.0))
                matches = calibration_geometry_matches(
                    self.calibration,self._snapshot.geometry,
                )
                prefix = "Calibration" if matches else "⚠ Calibration"
                suffix = "" if matches else " (section geometry differs)"
                label.setText(
                    f"{prefix}: kx={kx:.6g}, ky={ky:.6g} 1/px/um{suffix}"
                )
                label.setStyleSheet(
                    "color: #286b2d;" if matches else "color: #a66;"
                )
                if matches:
                    label.setToolTip("")
                else:
                    label.setToolTip(
                        "This calibration was measured with a different section geometry."
                    )
            elif plane:
                label.setText("Calibration: not calibrated")
                label.setStyleSheet("color: #888;")
            else:
                label.setText("Calibration: no plane selected")
                label.setStyleSheet("color: #888;")

        if self.calibration_button is not None:
            self.calibration_button.setEnabled(bool(plane))
        if self.delete_plane_action is not None:
            self.delete_plane_action.setEnabled(bool(plane))

    def set_cgh_target_presentation(
        self,summary: Mapping[str,Any],
    ) -> None:
        for group_view in self.groups.values():
            if isinstance(group_view,CghGroupView):
                group_view.set_target_presentation(summary)

    def set_cgh_computing(self,computing: bool) -> None:
        self._cgh_computing = bool(computing)
        for group_view in self.groups.values():
            if isinstance(group_view,CghGroupView):
                group_view.set_computing(self._cgh_computing)

    def apply_cgh_status(self,status: Any) -> None:
        for group_view in self.groups.values():
            if isinstance(group_view,CghGroupView):
                group_view.apply_status(status)

    def apply_feedback_status(self,status: Any) -> None:
        self._feedback_status = status
        for group_view in self.groups.values():
            if isinstance(group_view,CghGroupView):
                group_view.apply_feedback_status(status)

    def set_snapshot(self,snapshot: SLMSectionSnapshot) -> None:
        calibration_changed = (
            _calibration_data(self.calibration)
            != _calibration_data(snapshot.calibration)
        )
        presentation_changed = self.presentation != snapshot.presentation
        self._snapshot = snapshot
        self._revision = snapshot.revision
        self._reindex_snapshot(snapshot)
        self._refresh_topology_controls()
        if calibration_changed:
            self.apply_calibration(snapshot.calibration)
        if presentation_changed:
            self.apply_presentation(snapshot.presentation)

    def dispose(self) -> None:
        self.deleteLater()
