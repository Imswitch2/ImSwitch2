from __future__ import annotations

from typing import Any

from qtpy import QtCore,QtWidgets

from ...application.addressing import SLMSectionAddress
from ...application.control_mode import SLMControlMode
from ...workspace import PositionReferenceLifetime,SLMWorkspace
from ..calibration.management_dialogs import CalibrationManagementDialog
from .address_presentation import (
    section_display_label,section_physical_detail,slm_display_label,
    slm_physical_detail,
)
from .session_group import SLMQtSessionGroup


class SLMWorkspaceCoordinator(QtCore.QObject):
    """Own one shared multi-SLM calibration/characterization workspace.

    The coordinator owns navigation only.  Each physical SLM keeps its own
    :class:`CalibrationManager`, which is attached to the shared dialog only
    while that SLM is selected.  Mutating operations therefore continue to be
    handled by the explicitly selected physical session rather than a global
    mutable "current SLM" application state.
    """

    def __init__(
        self,
        group: SLMQtSessionGroup,
        workspace: SLMWorkspace,
        *,
        parent_widget: QtWidgets.QWidget | None=None,
        parent: QtCore.QObject | None=None,
    ) -> None:
        super().__init__(parent if parent is not None else parent_widget)
        if not isinstance(group,SLMQtSessionGroup):
            raise TypeError("group must be an SLMQtSessionGroup")
        if not isinstance(workspace,SLMWorkspace):
            raise TypeError("workspace must be an SLMWorkspace")
        self.group=group
        self.workspace=workspace
        self.parent_widget=parent_widget
        self._dialog: CalibrationManagementDialog | None=None
        self._active_slm_key: str | None=None
        self._active_manager=None
        self._managers: dict[str,Any]={}
        self._section_host_connections: dict[str,tuple[Any,Any,Any]]={}
        self._section_titles_refresh_pending: set[str]=set()
        self._disposed=False

        self.group.sigSessionAdded.connect(self._session_added)
        self.group.sigSessionRemoved.connect(self._session_removed)
        self.group.sigControlModeChanged.connect(self._shared_edit_state_changed)
        self.group.sigControlModeAvailabilityChanged.connect(self._shared_edit_state_changed)
        self.workspace.position_reference_store.add_listener(
            self._refresh_reference_catalog,
        )
        for key,session in self.group.session_items():
            self._install_session(key,session)

    @property
    def dialog(self) -> CalibrationManagementDialog | None:
        return self._dialog

    @property
    def active_slm_key(self) -> str | None:
        return self._active_slm_key

    @property
    def active_address(self) -> SLMSectionAddress | None:
        dialog=self._dialog
        key=self._active_slm_key
        if dialog is None or not key or not dialog.section_key:
            return None
        return SLMSectionAddress(key,dialog.section_key)

    def open(
        self,address: SLMSectionAddress | None=None,*,page: str | None=None,
    ) -> bool:
        if self._disposed:
            raise RuntimeError("SLMWorkspaceCoordinator has been disposed")
        if address is not None:
            if not isinstance(address,SLMSectionAddress):
                raise TypeError("address must be an SLMSectionAddress or None")
            self.group.resolve(address)
            target_key=address.slm_key
            target_section=address.section_key
        else:
            items=self.group.session_items()
            if not items:
                return False
            target_key=(
                self._active_slm_key
                if self._active_slm_key is not None and self.group.contains(self._active_slm_key)
                else items[0][0]
            )
            target_section=None

        if self._active_manager is not None and self._active_manager.management_workflow_active:
            current=self.active_address
            if address is not None and current != address:
                self._warn_locked(current)
                self._show_dialog()
                return False
            if target_key != self._active_slm_key:
                self._warn_locked(current)
                self._show_dialog()
                return False

        if not self._activate_slm(target_key,section_key=target_section):
            self._show_dialog()
            return False
        if page is not None:
            page_key=str(page).strip().casefold()
            if page_key != "mapping":
                raise ValueError("Unknown SLM Workspace page %r" % page)
            if not self._active_manager.management_workflow_active:
                self._dialog.select_mapping_tab()
        self._show_dialog()
        return True

    def open_mapping(self,address: SLMSectionAddress) -> bool:
        """Open an exact physical section directly on Mapping management."""
        return self.open(address,page="mapping")

    def close(self) -> None:
        if self._dialog is not None:
            self._dialog.close()

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed=True
        try:
            self.group.sigSessionAdded.disconnect(self._session_added)
        except (RuntimeError,TypeError):
            pass
        try:
            self.group.sigSessionRemoved.disconnect(self._session_removed)
        except (RuntimeError,TypeError):
            pass
        try:
            self.group.sigControlModeChanged.disconnect(self._shared_edit_state_changed)
        except (RuntimeError,TypeError):
            pass
        try:
            self.group.sigControlModeAvailabilityChanged.disconnect(self._shared_edit_state_changed)
        except (RuntimeError,TypeError):
            pass
        self.workspace.position_reference_store.remove_listener(
            self._refresh_reference_catalog,
        )
        if self._active_manager is not None:
            self._disconnect_active_manager()
            self._active_manager.detach_management_view()
        for key in tuple(self._managers):
            self._uninstall_session(key)
        dialog=self._dialog
        self._dialog=None
        self._active_slm_key=None
        self._active_manager=None
        self._section_titles_refresh_pending.clear()
        if dialog is not None:
            # Disposal is authoritative and must not be blocked by the normal
            # active-workflow close guard.
            dialog.set_context_switching_enabled(True)
            dialog.close()
            dialog.deleteLater()

    def _ensure_dialog(self,current_slm: str) -> CalibrationManagementDialog:
        dialog=self._dialog
        if dialog is None:
            dialog=CalibrationManagementDialog(
                sections=(),mappings=(),
                slms=self._slm_catalog(),current_slm=current_slm,
                parent=self.parent_widget,
            )
            self._dialog=dialog
            dialog.sigSlmChanged.connect(self._slm_changed)
            dialog.reference_view.sigPersistRequested.connect(
                self._persist_reference_requested,
            )
            dialog.reference_view.sigDeleteRequested.connect(
                self._delete_reference_requested,
            )
            dialog.destroyed.connect(self._dialog_destroyed)
            self._refresh_reference_catalog()
            self._refresh_reference_mutation_state()
        return dialog

    def _activate_slm(self,slm_key: str,*,section_key: str | None=None) -> bool:
        key=str(slm_key).strip()
        session=self.group.session(key)
        manager=session.calibration_manager
        if (
            self._active_manager is not None
            and self._active_manager is not manager
            and self._active_manager.management_workflow_active
        ):
            self._warn_locked(self.active_address)
            return False

        dialog=self._ensure_dialog(key)
        dialog.set_slm_catalog(self._slm_catalog(),current_slm=key)

        if self._active_manager is not manager:
            if self._active_manager is not None:
                self._disconnect_active_manager()
                self._active_manager.detach_management_view()
            self._active_manager=manager
            self._active_slm_key=key
            manager.sigManagementWorkflowActiveChanged.connect(
                self._workflow_active_changed,
            )
            manager.attach_management_view(dialog,section_key=section_key)
        else:
            self._active_slm_key=key
            manager.populate_management_view(section_key=section_key)

        self._decorate_context_identity()
        self._workflow_active_changed(manager.management_workflow_active)
        return True

    @QtCore.Slot(str)
    def _slm_changed(self,slm_key: str) -> None:
        key=str(slm_key).strip()
        if not key or key == self._active_slm_key:
            return
        if not self._activate_slm(key):
            dialog=self._dialog
            if dialog is not None and self._active_slm_key is not None:
                dialog.set_slm_catalog(
                    self._slm_catalog(),current_slm=self._active_slm_key,
                )

    @QtCore.Slot(bool)
    def _workflow_active_changed(self,active: bool) -> None:
        dialog=self._dialog
        if dialog is None:
            return
        reason=""
        if active:
            address=self.active_address
            if address is not None:
                reason=(
                    "Finish or cancel the active calibration workflow for "
                    "%s before switching SLM or section."
                    % self._address_context_text(address)
                )
            else:
                reason="Finish or cancel the active calibration workflow before switching context."
        dialog.set_context_switching_enabled(not active,reason)
        if not active:
            key=self._active_slm_key
            if key and key in self._section_titles_refresh_pending and self._active_manager is not None:
                self._section_titles_refresh_pending.discard(key)
                preferred=dialog.section_key
                self._active_manager.populate_management_view(section_key=preferred)
                self._decorate_context_identity()
        self._refresh_reference_mutation_state()

    @QtCore.Slot(str,object)
    def _session_added(self,slm_key: str,session) -> None:
        key=str(slm_key).strip()
        self._install_session(key,session)
        dialog=self._dialog
        if dialog is None:
            return
        if self._active_slm_key is None:
            # A workspace that was left open with no live SLM should become
            # usable again as soon as a session appears.
            self._activate_slm(key)
        else:
            dialog.set_slm_catalog(
                self._slm_catalog(),current_slm=self._active_slm_key,
            )
            self._decorate_context_identity()
        self._refresh_reference_catalog()

    @QtCore.Slot(str)
    def _session_removed(self,slm_key: str) -> None:
        key=str(slm_key).strip()
        was_active=key == self._active_slm_key
        if was_active and self._active_manager is not None:
            self._disconnect_active_manager()
            self._active_manager.detach_management_view()
            self._active_manager=None
            self._active_slm_key=None
        self._section_titles_refresh_pending.discard(key)
        self._uninstall_session(key)

        dialog=self._dialog
        if was_active and dialog is not None:
            # The manager is disconnected before detach so its final
            # workflow-active=False signal cannot reach us.  Explicitly clear
            # the shared workspace lock before selecting a replacement SLM or
            # entering the empty state.
            dialog.set_context_switching_enabled(True)
        if dialog is None:
            return
        items=self.group.session_items()
        if was_active and items:
            self._activate_slm(items[0][0])
        elif not items:
            dialog.clear_slm_context()
        else:
            dialog.set_slm_catalog(
                self._slm_catalog(),current_slm=self._active_slm_key,
            )
            self._decorate_context_identity()
        self._refresh_reference_catalog()

    def _shared_edit_state_changed(self,*_args: Any) -> None:
        self._refresh_reference_mutation_state()

    def _refresh_reference_mutation_state(self) -> None:
        dialog=self._dialog
        if dialog is None:
            return
        enabled=(
            self.group.control_mode is SLMControlMode.EDITOR
            and self.group.can_change_control_mode
        )
        reason=""
        if not enabled:
            if self.group.control_mode is not SLMControlMode.EDITOR:
                reason="Reference management is unavailable in Fast Config mode."
            else:
                reason="Reference management is unavailable while an automatic or calibration workflow is active."
        dialog.set_reference_mutations_enabled(enabled,reason)

    def _refresh_reference_catalog(self) -> None:
        dialog=self._dialog
        if dialog is None:
            return
        dialog.set_reference_records(self._reference_rows())
        self._refresh_reference_mutation_state()

    def _reference_rows(self) -> tuple[dict[str,Any],...]:
        store=self.workspace.position_reference_store
        return tuple(
            self._reference_row(record,store.persistence_available)
            for record in store.list_all()
        )

    def _reference_row(self,record,persistence_available: bool) -> dict[str,Any]:
        reference=record.reference
        metadata=dict(reference.metadata or {})
        source_slm=str(metadata.get("source_slm_key") or "").strip()
        source_serial=str(metadata.get("source_slm_serial") or "").strip()
        source_section=str(metadata.get("source_section") or "").strip()

        physical_parts=[]
        if source_slm and source_section:
            physical_parts.append("%s / %s" % (source_slm,source_section))
        elif source_slm:
            physical_parts.append(source_slm)
        elif source_section:
            physical_parts.append(source_section)
        if source_serial:
            physical_parts.append(source_serial)
        source_detail=" · ".join(physical_parts) or "Unknown source"
        source_label=source_detail
        if source_slm and self.group.contains(source_slm):
            session=self.group.session(source_slm)
            live_serial=str(session.runtime.identity.serial_number or "").strip()
            serial_matches=(not source_serial or source_serial==live_serial)
            if serial_matches:
                if source_section:
                    source_label=section_display_label(
                        session,SLMSectionAddress(source_slm,source_section),
                    )
                else:
                    source_label=slm_display_label(session)
            elif live_serial:
                source_detail += " · currently loaded serial=%s" % live_serial

        total=int(reference.positions_px.shape[1])
        matched=metadata.get("matched_count")
        inferred=metadata.get("inferred_count")
        if matched is None and inferred is None:
            sites=str(total)
        else:
            matched=int(matched or 0); inferred=int(inferred or 0)
            sites=(
                "%d matched · %d inferred · %d total"
                % (matched,inferred,total)
            )

        pixel=metadata.get("source_pixel_size_um")
        if pixel is None:
            pixel_text="—"
        else:
            try: pixel_text="%g µm/px" % float(pixel)
            except (TypeError,ValueError): pixel_text=str(pixel)
        target_type=str(metadata.get("source_target_type") or "—")
        config_name=str(metadata.get("source_config_name") or "—")
        orientation=str(metadata.get("feedback_orientation") or "—")
        lattice=metadata.get("lattice_representation")
        lattice_summary="—" if lattice is None else self._lattice_summary(lattice)
        frame=metadata.get("source_observation_frame")
        frame_summary="—" if frame is None else self._frame_summary(frame)
        created=str(reference.created_at or "—")
        details="\n".join((
            "Name: %s" % reference.name,
            "Lifetime: %s" % record.lifetime.value,
            "Observation plane: %s" % reference.observation_plane_name,
            "Created: %s" % created,
            "",
            "Source SLM key: %s" % (source_slm or "—"),
            "Source SLM serial: %s" % (source_serial or "—"),
            "Source section: %s" % (source_section or "—"),
            "Source config: %s" % config_name,
            "Source target type: %s" % target_type,
            "",
            "Detector: %s" % (reference.detector_name or "—"),
            "Image shape: %s × %s" % tuple(reference.image_shape),
            "Pixel size: %s" % pixel_text,
            "Feedback orientation: %s" % orientation,
            "",
            "Sites: %s" % sites,
            "Observation frame: %s" % frame_summary,
            "Lattice: %s" % lattice_summary,
            "Physical source: %s" % source_detail,
        ))
        return {
            "name":reference.name,
            "observation_plane":reference.observation_plane_name,
            "lifetime":record.lifetime.value,
            "source_label":source_label,
            "source_detail":source_detail,
            "detector":reference.detector_name or "—",
            "sites":sites,
            "created":created,
            "details":details,
            "persistence_available":bool(persistence_available),
        }

    @staticmethod
    def _frame_summary(value: Any) -> str:
        if not isinstance(value,dict):
            return str(value)
        parts=[]
        for key in ("slm_serial","section_key","observation_plane_name","detector_name"):
            item=value.get(key)
            if item not in (None,""):
                parts.append("%s=%s" % (key,item))
        shape=value.get("image_shape")
        if shape not in (None,""):
            parts.append("image_shape=%s" % (shape,))
        return ", ".join(parts) or "—"

    @staticmethod
    def _lattice_summary(value: Any) -> str:
        if not isinstance(value,dict):
            return str(value)

        # Current references persist the canonical
        # LatticeRepresentation.to_dict() shape.  Summarize that directly
        # rather than looking for older target-specific top-level fields.
        parts=[]
        diagnostics=value.get("diagnostics")
        diagnostics=diagnostics if isinstance(diagnostics,dict) else {}

        family=diagnostics.get("family") or diagnostics.get("lattice_family")
        if family not in (None,""):
            parts.append(str(family))

        lattice_indices=value.get("lattice_indices")
        if (
            isinstance(lattice_indices,(list,tuple))
            and len(lattice_indices)==2
            and all(isinstance(axis,(list,tuple)) for axis in lattice_indices)
            and len(lattice_indices[0])==len(lattice_indices[1])
        ):
            parts.append("%d sites" % len(lattice_indices[0]))

        basis_offsets=value.get("basis_offsets")
        if (
            isinstance(basis_offsets,(list,tuple))
            and len(basis_offsets)==2
            and all(isinstance(axis,(list,tuple)) for axis in basis_offsets)
            and len(basis_offsets[0])==len(basis_offsets[1])
        ):
            parts.append("%d-point basis" % len(basis_offsets[0]))

        stagger=diagnostics.get("stagger")
        if stagger not in (None,""):
            parts.append("stagger=%s" % stagger)

        return " · ".join(parts) or "stored topology"

    def _reference_mutation_allowed(self) -> bool:
        return bool(
            self.group.control_mode is SLMControlMode.EDITOR
            and self.group.can_change_control_mode
        )

    @QtCore.Slot(str,str)
    def _persist_reference_requested(self,plane: str,name: str) -> None:
        if not self._reference_mutation_allowed():
            self._refresh_reference_mutation_state()
            return
        try:
            self.workspace.position_reference_store.persist(plane,name)
        except Exception as error:
            QtWidgets.QMessageBox.warning(
                self._dialog,
                "Keep Position Reference",
                str(error),
            )

    @QtCore.Slot(str,str)
    def _delete_reference_requested(self,plane: str,name: str) -> None:
        if not self._reference_mutation_allowed():
            self._refresh_reference_mutation_state()
            return
        store=self.workspace.position_reference_store
        try:
            record=store.get_record(plane,name)
        except Exception as error:
            QtWidgets.QMessageBox.warning(self._dialog,"Delete Position Reference",str(error))
            return
        if record.lifetime is PositionReferenceLifetime.PERSISTENT:
            answer=QtWidgets.QMessageBox.question(
                self._dialog,
                "Delete Position Reference",
                'Delete persistent position reference "%s" from observation plane "%s"?' % (name,plane),
                QtWidgets.QMessageBox.Yes|QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if answer != QtWidgets.QMessageBox.Yes:
                return
        try:
            store.delete(plane,name)
        except Exception as error:
            QtWidgets.QMessageBox.warning(self._dialog,"Delete Position Reference",str(error))

    def _install_session(self,slm_key: str,session) -> None:
        key=str(slm_key).strip()
        manager=session.calibration_manager
        self._managers[key]=manager
        manager.set_shared_workspace_opener(
            lambda section,key=key:self.open_mapping(SLMSectionAddress(key,section))
        )
        host=session.section_host
        sections_slot=lambda key=key:self._sections_changed(key)
        titles_slot=lambda key=key:self._section_titles_changed(key)
        host.sigSectionsChanged.connect(sections_slot)
        host.sigSectionTitlesChanged.connect(titles_slot)
        self._section_host_connections[key]=(host,sections_slot,titles_slot)

    def _uninstall_session(self,slm_key: str) -> None:
        key=str(slm_key).strip()
        connection=self._section_host_connections.pop(key,None)
        if connection is not None:
            host,sections_slot,titles_slot=connection
            for signal,slot in (
                (host.sigSectionsChanged,sections_slot),
                (host.sigSectionTitlesChanged,titles_slot),
            ):
                try:
                    signal.disconnect(slot)
                except (RuntimeError,TypeError):
                    pass
        manager=self._managers.pop(key,None)
        if manager is not None:
            manager.set_shared_workspace_opener(None)

    def _sections_changed(self,slm_key: str) -> None:
        key=str(slm_key).strip()
        if key != self._active_slm_key or self._active_manager is None:
            self._refresh_reference_catalog()
            return
        dialog=self._dialog
        preferred=None if dialog is None else dialog.section_key
        cancelled=self._active_manager.reconcile_management_section_topology(
            preferred_section=preferred,
        )
        self._decorate_context_identity()
        self._refresh_reference_catalog()
        if cancelled:
            self._active_manager.controller.sigWarning.emit(
                "SLM Workspace",
                "The active calibration workflow was cancelled because the SLM section topology changed.",
            )

    def _section_titles_changed(self,slm_key: str) -> None:
        key=str(slm_key).strip()
        if key == self._active_slm_key and self._active_manager is not None:
            if self._active_manager.management_workflow_active:
                # Do not repopulate underneath an embedded calibration.  Defer
                # the presentation refresh until the workflow ends.
                self._section_titles_refresh_pending.add(key)
            else:
                dialog=self._dialog
                preferred=None if dialog is None else dialog.section_key
                self._active_manager.populate_management_view(section_key=preferred)
                self._section_titles_refresh_pending.discard(key)
        self._decorate_context_identity()
        self._refresh_reference_catalog()

    def _disconnect_active_manager(self) -> None:
        manager=self._active_manager
        if manager is None:
            return
        try:
            manager.sigManagementWorkflowActiveChanged.disconnect(
                self._workflow_active_changed,
            )
        except (RuntimeError,TypeError):
            pass

    def _slm_catalog(self) -> tuple[tuple[str,str],...]:
        return tuple(
            (key,slm_display_label(session))
            for key,session in self.group.session_items()
        )

    def _decorate_context_identity(self) -> None:
        dialog=self._dialog
        if dialog is None:
            return
        for index in range(dialog.slm_combo.count()):
            key=str(dialog.slm_combo.itemData(index) or "").strip()
            if not key or not self.group.contains(key):
                continue
            session=self.group.session(key)
            dialog.slm_combo.setItemData(
                index,slm_physical_detail(session,slm_key=key),
                QtCore.Qt.ToolTipRole,
            )
        key=self._active_slm_key
        if not key or not self.group.contains(key):
            return
        session=self.group.session(key)
        for index in range(dialog.section_combo.count()):
            section=str(dialog.section_combo.itemData(index) or "").strip()
            if not section:
                continue
            address=SLMSectionAddress(key,section)
            dialog.section_combo.setItemData(
                index,section_physical_detail(session,address),
                QtCore.Qt.ToolTipRole,
            )

    def _address_context_text(self,address: SLMSectionAddress) -> str:
        if self.group.contains(address.slm_key):
            session=self.group.session(address.slm_key)
            friendly=section_display_label(session,address)
            return "%s [%s / %s]" % (
                friendly,address.slm_key,address.section_key,
            )
        return "%s / %s" % (address.slm_key,address.section_key)

    def _warn_locked(self,address: SLMSectionAddress | None) -> None:
        manager=self._active_manager
        if manager is None:
            return
        target=(
            "the current SLM/section"
            if address is None else self._address_context_text(address)
        )
        manager.controller.sigWarning.emit(
            "SLM Workspace",
            "A calibration workflow is active for %s. Finish or cancel it before switching workspace context."
            % target,
        )

    def _show_dialog(self) -> None:
        dialog=self._dialog
        if dialog is None:
            return
        dialog.show(); dialog.raise_(); dialog.activateWindow()

    @QtCore.Slot(QtCore.QObject)
    def _dialog_destroyed(self,_object: Any=None) -> None:
        if self._active_manager is not None:
            self._disconnect_active_manager()
            self._active_manager.management_view_destroyed(self._dialog)
        self._dialog=None
        self._active_manager=None
        self._active_slm_key=None


__all__=["SLMWorkspaceCoordinator"]
