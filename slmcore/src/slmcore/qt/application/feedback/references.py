"""Qt-side position-reference presentation and dialogs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any,Mapping,TYPE_CHECKING

from qtpy import QtWidgets

if TYPE_CHECKING:
    from ....application.feedback import SLMFeedbackService
    from ...cgh.session_window import CGHSessionWindow


_UNSET = object()


class PositionReferenceCoordinator:
    """Synchronize and edit position references for feedback UI windows."""

    def __init__(
        self,
        feedback_service: "SLMFeedbackService",
        *,
        window_for: Callable[[str],"CGHSessionWindow | None"],
        flush_section: Callable[[str],None],
        synchronize_section: Callable[[str],None],
        report_error: Callable[[str,Exception],None],
    ) -> None:
        self._feedback = feedback_service
        self._window_for = window_for
        self._flush_section = flush_section
        self._synchronize_section = synchronize_section
        self._report_error = report_error

    def ui_context(self,section_key: str) -> dict[str,Any]:
        """Adapt the application reference context to the window mapping contract."""
        return self._ui_context_from_context(
            self._feedback.position_reference_context(section_key)
        )

    def synchronize(
        self,section_key: str,window: "CGHSessionWindow",*,lightweight: bool=False,
    ) -> None:
        context,preview=self._feedback.position_reference_presentation(section_key)
        window.set_position_reference_state(
            self._ui_context_from_context(context),preview,
            lightweight=bool(lightweight),
        )

    def _ui_context_from_context(self,context: Any) -> dict[str,Any]:
        """Adapt an already-resolved application context without resolving it again."""
        selection = context.selection
        return {
            "mode":selection.mode.value,
            "reference_name":selection.reference_name,
            "references":tuple(
                {
                    "name":record.reference.name,
                    "lifetime":record.lifetime.value,
                    "created_at":record.reference.created_at,
                    "detector_name":record.reference.detector_name,
                }
                for record in context.references
            ),
            "persistence_available":context.persistence_available,
            "editable_geometry":(
                None if selection.editable_geometry is None
                else selection.editable_geometry.to_dict()
            ),
            "editable_locks":{
                name:(name in selection.editable_locks)
                for name in (
                    "period_x_px","period_y_px",
                    "rotation_deg","lattice_angle_deg",
                )
            },
            "fit_geometry":(
                None if context.fit_geometry is None
                else context.fit_geometry.to_dict()
            ),
            "correspondence":selection.correspondence.value,
            "correction_mode":selection.correction_mode,
            "polynomial_degree":selection.polynomial_degree,
            "affine_transform":(
                None
                if selection.affine_transform is None
                else tuple(tuple(row) for row in selection.affine_transform)
            ),
            "manual_transform":(
                None
                if selection.manual_transform is None
                else selection.manual_transform.to_dict()
            ),
            "correspondence_options":context.correspondence_options,
            "correspondence_resolved":context.correspondence_resolved,
            "correspondence_diagnostics":(
                None
                if context.correspondence_diagnostics is None
                else dict(context.correspondence_diagnostics)
            ),
            "observation_plane_name":context.observation_plane_name,
            "compatible":context.compatible,
            "compatibility_error":context.compatibility_error,
            "change_allowed":context.change_allowed,
            "change_unavailable_reason":context.change_unavailable_reason,
            "create_allowed":context.create_allowed,
            "create_unavailable_reason":context.create_unavailable_reason,
        }

    def set_position_reference(
        self,
        section_key: str,
        *,
        mode: str,
        reference_name: str | None,
        editable_geometry: Mapping[str,Any] | None=None,
        editable_locks: Mapping[str,Any] | None=None,
        correspondence: str | None=None,
        correction_mode: str | None=None,
        polynomial_degree: int | None=None,
        affine_transform: Any=_UNSET,
        manual_transform: Any=_UNSET,
        lightweight: bool=False,
    ) -> None:
        """Flush editor changes before applying a reference selection mapping."""
        try:
            if not lightweight:
                self._flush_section(section_key)
            kwargs = {
                "mode":mode,
                "reference_name":reference_name,
                "editable_geometry":editable_geometry,
                "editable_locks":editable_locks,
                "correspondence":correspondence,
                "correction_mode":correction_mode,
                "polynomial_degree":polynomial_degree,
            }
            if affine_transform is not _UNSET:
                kwargs["affine_transform"] = affine_transform
            if manual_transform is not _UNSET:
                kwargs["manual_transform"] = manual_transform
            self._feedback.set_position_reference(
                section_key,**kwargs,notify=not bool(lightweight),
            )
            if lightweight:
                window=self._window_for(section_key)
                if window is not None:
                    context,preview=self._feedback.position_reference_presentation(
                        section_key
                    )
                    window.set_position_reference_state(
                        self._ui_context_from_context(context),preview,
                        lightweight=True,
                    )
        except Exception as error:
            self._report_error("Changing position reference failed",error)
            self._synchronize_section(section_key)

    def fit_position_reference_center(self,section_key: str) -> None:
        try:
            self._flush_section(section_key)
            self._feedback.fit_position_reference_center(section_key)
        except Exception as error:
            self._report_error("Fitting center reference failed",error)
            self._synchronize_section(section_key)

    def add_position_reference(self,section_key: str,name: str) -> None:
        """Create a session reference, asking before replacing an existing name."""
        if not name:
            return
        try:
            self._feedback.add_position_reference(section_key,name)
        except FileExistsError:
            parent = self._window_for(section_key)
            answer = QtWidgets.QMessageBox.question(
                parent,
                "Replace position reference",
                'Position reference "%s" already exists for this plane. Replace it?'
                % name,
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if answer != QtWidgets.QMessageBox.Yes:
                return
            try:
                self._feedback.add_position_reference(
                    section_key,name,overwrite=True,
                )
            except Exception as error:
                self._report_error("Adding position reference failed",error)
        except Exception as error:
            self._report_error("Adding position reference failed",error)

    def persist_position_reference(self,section_key: str,name: str) -> None:
        """Promote a session reference to persistent storage."""
        if not name:
            return
        try:
            self._feedback.persist_position_reference(section_key,name)
        except Exception as error:
            self._report_error("Saving position reference permanently failed",error)

    def delete_position_reference(self,section_key: str,name: str) -> None:
        """Confirm persistent deletion; session references are removed directly."""
        if not name:
            return
        context = self._feedback.position_reference_context(section_key)
        record = next(
            (item for item in context.references if item.name == name),None,
        )
        if record is not None and record.lifetime.value == "persistent":
            parent = self._window_for(section_key)
            answer = QtWidgets.QMessageBox.question(
                parent,
                "Delete position reference",
                'Delete persistent position reference "%s"?' % name,
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if answer != QtWidgets.QMessageBox.Yes:
                return
        try:
            self._feedback.delete_position_reference(section_key,name)
        except Exception as error:
            self._report_error("Deleting position reference failed",error)
