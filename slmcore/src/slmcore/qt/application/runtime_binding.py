"""Transactional binding between :class:`SLMRuntime` and retained Qt views.

Standard and CGH-target drafts intentionally use independent debounce buckets.
This keeps the phase-1 runtime/view boundary intact while allowing fluid target
coalescing without visual locking or an edit-order scheduler.
"""

from __future__ import annotations

from dataclasses import dataclass,replace
from typing import Any,Callable,Mapping

from qtpy import QtCore

from ...core.cgh.feedback.model import base_cgh_recompute_would_discard_feedback
from ...core.cgh.targets.lattice import LatticeLockRequest
from ...core.engine.section.update import SectionUpdate
from ...core.engine.runtime import SLMRuntime
from ...core.engine.corrections import CorrectionSourceInvalidatedError
from ...application.configuration import CorrectionMismatchPolicy
from ...core.engine.state.base import ParamPath
from .interaction import (
    DEFAULT_RUNTIME_VIEW_INTERACTION_SETTINGS,
    ParameterEditKind,
    RuntimeViewInteractionSettings,
    classify_parameter_edit,
    is_target_selector_edit,
)
from ..sections.collection import SectionsCollectionView


DEFAULT_PATCH_DEBOUNCE_MS = (
    DEFAULT_RUNTIME_VIEW_INTERACTION_SETTINGS.standard_patch_debounce_ms
)
DEFAULT_TARGET_PATCH_DEBOUNCE_MS = (
    DEFAULT_RUNTIME_VIEW_INTERACTION_SETTINGS.target_patch_debounce_ms
)


@dataclass
class _PendingPatch:
    changes: dict[ParamPath, Any]
    edit_kind: ParameterEditKind
    lattice_lock_request: LatticeLockRequest | None = None


class SLMRuntimeViewBinding(QtCore.QObject):
    """Bind one ``SLMRuntime`` to one retained section collection."""

    sigPatchApplied = QtCore.Signal(str,object)
    sigPatchFailed = QtCore.Signal(str,object)
    sigAutoComputeRequested = QtCore.Signal(str)

    def __init__(
        self,
        *,
        runtime: SLMRuntime,
        section_collection: SectionsCollectionView,
        application_session=None,
        interaction_settings: RuntimeViewInteractionSettings | None=None,
        debounce_ms: int | None=None,
        correction_source_switch_confirm: (
            Callable[[str,Exception],bool] | None
        )=None,
        parent: QtCore.QObject | None=None,
    ) -> None:
        super().__init__(parent)
        if tuple(runtime.section_keys) != tuple(section_collection.section_keys):
            raise ValueError(
                "Runtime and section collection must expose the same sections "
                "in the same order"
            )

        settings = (
            DEFAULT_RUNTIME_VIEW_INTERACTION_SETTINGS
            if interaction_settings is None else interaction_settings
        )
        if not isinstance(settings,RuntimeViewInteractionSettings):
            settings = RuntimeViewInteractionSettings(**dict(settings))
        if debounce_ms is not None:
            value = int(debounce_ms)
            if value < 0:
                raise ValueError("debounce_ms must be >= 0")
            # Compatibility mode intentionally preserves the old one-timer
            # timing, including values below the richer target UI minimum.
            settings = replace(
                settings,
                standard_patch_debounce_ms=value,
                target_patch_debounce_ms=value,
            )

        self.runtime = runtime
        self.application_session = application_session
        self.section_collection = section_collection
        self.interaction_settings = settings
        self._correction_source_switch_confirm = correction_source_switch_confirm
        self._pending_patches: dict[tuple[str, ParameterEditKind], _PendingPatch] = {}
        self._timers: dict[tuple[str, ParameterEditKind], QtCore.QTimer] = {}
        self._disposed = False
        self._writes_enabled = True

        self.section_collection.sigSectionPatchRequested.connect(
            self.request_patch,
        )
        self.section_collection.sigTargetLockRequested.connect(
            self.request_target_lock,
        )

    @property
    def debounce_ms(self) -> int:
        return self.interaction_settings.standard_patch_debounce_ms

    @property
    def has_pending_patches(self) -> bool:
        return bool(self._pending_patches)

    @property
    def pending_section_keys(self) -> tuple[str, ...]:
        seen = []
        for section_key,_kind in self._pending_patches:
            if section_key not in seen:
                seen.append(section_key)
        return tuple(seen)

    @property
    def writes_enabled(self) -> bool:
        return self._writes_enabled

    def set_writes_enabled(
        self,enabled: bool,*,restore_pending: bool=True,
    ) -> None:
        self._require_active()
        enabled = bool(enabled)
        if enabled == self._writes_enabled:
            return
        if not enabled:
            self.cancel_all(restore=restore_pending)
        self._writes_enabled = enabled

    def set_interaction_settings(
        self,settings: RuntimeViewInteractionSettings,
    ) -> None:
        self._require_active()
        if not isinstance(settings,RuntimeViewInteractionSettings):
            settings = RuntimeViewInteractionSettings(**dict(settings))
        if settings == self.interaction_settings:
            return
        self.interaction_settings = settings
        for token in tuple(self._pending_patches):
            self._timer(*token).start(self._debounce_for_kind(token[1]))

    @QtCore.Slot(str,object)
    def request_patch(
        self,section_key: str,changes: Mapping[ParamPath,Any],
    ) -> None:
        self._require_active()
        if not self._writes_enabled:
            return
        self._require_section(section_key)
        normalized = {
            tuple(path):value for path,value in dict(changes or {}).items()
        }
        if not normalized:
            return

        edit_kind = classify_parameter_edit(normalized)

        if edit_kind is ParameterEditKind.CGH_TARGET:
            if is_target_selector_edit(normalized):
                # Target replacement is a hard semantic boundary: discard the
                # old target draft completely rather than committing it first.
                self._cancel_kind(
                    section_key,ParameterEditKind.CGH_TARGET,
                    restore_paths=True,
                )
                update = self._apply_changes(
                    section_key,normalized,propagate=False,
                )
                if update is not None and update.target_definition_changed:
                    self._emit_auto_compute(section_key)
                return

            token = (section_key,ParameterEditKind.CGH_TARGET)
            pending = self._pending_patches.get(token)
            if pending is None:
                pending = _PendingPatch(
                    changes={},edit_kind=ParameterEditKind.CGH_TARGET,
                )
                self._pending_patches[token] = pending
            # Latest value per path wins across the entire target interaction
            # burst. Different target parameters intentionally coalesce.
            pending.changes.update(normalized)
            self._timer(*token).start(
                self.interaction_settings.target_patch_debounce_ms
            )
            return

        token = (section_key,ParameterEditKind.STANDARD)
        pending = self._pending_patches.get(token)
        if pending is None:
            pending = _PendingPatch(
                changes={},edit_kind=ParameterEditKind.STANDARD,
            )
            self._pending_patches[token] = pending
        pending.changes.update(normalized)
        self._timer(*token).start(
            self.interaction_settings.standard_patch_debounce_ms
        )

    @QtCore.Slot(str,str,object)
    def request_target_lock(
        self,section_key: str,target_key: str,kind: str | None,
    ) -> None:
        """Apply or join one raster-lattice lock request transactionally."""
        self._require_active()
        if not self._writes_enabled:
            return
        self._require_section(section_key)
        snapshot = self.runtime.get_section_snapshot(section_key)
        if snapshot.state.cgh.selected_target != target_key:
            self._restore_target_lock_presentation(section_key)
            return

        reference = self._draft_lock_reference(
            section_key,target_key,kind,
        )
        request = LatticeLockRequest(
            target_key=target_key,kind=kind,reference=reference,
        )
        token = (section_key,ParameterEditKind.CGH_TARGET)
        pending = self._pending_patches.get(token)
        if pending is None:
            # Lock-only changes alter persistent intent but not target geometry,
            # so they commit immediately and never trigger auto-compute.
            self._apply_changes(
                section_key,{},propagate=False,
                lattice_lock_request=request,
            )
            return

        pending.lattice_lock_request = request
        self._timer(*token).start(
            self.interaction_settings.target_patch_debounce_ms
        )

    def restore_current_cgh_target(
        self,section_key: str,*,propagate: bool=False,
    ) -> SectionUpdate | None:
        """Discard target drafts and restore the committed CGH target definition."""
        self._require_active()
        self._require_writes_enabled()
        self._require_section(section_key)

        # This action explicitly abandons the target draft. Standard pending
        # edits remain independent and keep their normal debounce lifecycle.
        self._cancel_kind(
            section_key,ParameterEditKind.CGH_TARGET,restore_paths=False,
        )
        try:
            target = self.application_session
            if target is None:
                update = self.runtime.restore_section_current_cgh_target(section_key)
            else:
                update = target.restore_section_current_cgh_target(section_key)
            if update is None:
                self.section_collection.restore_section(
                    section_key,self.runtime.get_section_snapshot(section_key),
                )
                self.section_collection.set_feedback_status(
                    section_key,
                    self.runtime.get_section_feedback_status(section_key),
                )
                return None
            self.section_collection.apply_section_update(section_key,update)
            self.section_collection.set_feedback_status(
                section_key,
                self.runtime.get_section_feedback_status(section_key),
            )
        except Exception as error:
            self._restore_authoritative_preserving_drafts(section_key)
            if propagate:
                raise
            self.sigPatchFailed.emit(section_key,error)
            return None

        self.sigPatchApplied.emit(section_key,update)
        return update

    def flush_section(
        self,section_key: str,*,propagate: bool=False,
    ) -> SectionUpdate | None:
        """Commit every pending draft for one section as one explicit barrier."""
        self._require_active()
        self._require_writes_enabled()
        self._require_section(section_key)
        batches = []
        for kind in (ParameterEditKind.STANDARD,ParameterEditKind.CGH_TARGET):
            token = (section_key,kind)
            timer = self._timers.get(token)
            if timer is not None:
                timer.stop()
            pending = self._pending_patches.pop(token,None)
            if pending is not None:
                batches.append(pending)
        if not batches:
            return None

        changes: dict[ParamPath, Any] = {}
        lock_request = None
        for batch in batches:
            changes.update(batch.changes)
            if (
                batch.edit_kind is ParameterEditKind.CGH_TARGET
                and batch.lattice_lock_request is not None
            ):
                lock_request = batch.lattice_lock_request

        # ``flush_*`` is an explicit host ordering barrier. It commits drafts
        # only; it must not recursively launch automatic CGH work (for example
        # while a manual Compute action is itself flushing target edits).
        return self._apply_changes(
            section_key,changes,propagate=bool(propagate),
            lattice_lock_request=lock_request,
        )

    def flush_all(self,*,propagate: bool=False) -> None:
        self._require_active()
        self._require_writes_enabled()
        for section_key in self.pending_section_keys:
            self.flush_section(section_key,propagate=propagate)

    def cancel_section(
        self,section_key: str,*,restore: bool=True,
    ) -> bool:
        self._require_active()
        self._require_section(section_key)
        affected = False
        for kind in (ParameterEditKind.STANDARD,ParameterEditKind.CGH_TARGET):
            affected = self._cancel_kind(
                section_key,kind,restore_paths=False,
            ) or affected
        if affected and restore:
            self.section_collection.restore_section(
                section_key,self.runtime.get_section_snapshot(section_key),
            )
        return affected

    def cancel_all(self,*,restore: bool=True) -> tuple[str, ...]:
        self._require_active()
        affected = self.pending_section_keys
        for timer in self._timers.values():
            timer.stop()
        self._pending_patches.clear()
        if restore:
            for section_key in affected:
                self.section_collection.restore_section(
                    section_key,self.runtime.get_section_snapshot(section_key),
                )
        return affected

    def dispose(self,*,restore: bool=False) -> None:
        if self._disposed:
            return
        self.cancel_all(restore=restore)
        try:
            self.section_collection.sigSectionPatchRequested.disconnect(
                self.request_patch,
            )
        except (RuntimeError,TypeError):
            pass
        try:
            self.section_collection.sigTargetLockRequested.disconnect(
                self.request_target_lock,
            )
        except (RuntimeError,TypeError):
            pass
        for timer in self._timers.values():
            timer.stop()
            timer.deleteLater()
        self._timers.clear()
        self._disposed = True

    def _flush_kind(
        self,section_key: str,edit_kind: ParameterEditKind,*,propagate=False,
    ) -> SectionUpdate | None:
        if not self._writes_enabled:
            self._cancel_kind(
                section_key,edit_kind,restore_paths=True,
            )
            return None
        token = (section_key,edit_kind)
        timer = self._timers.get(token)
        if timer is not None:
            timer.stop()
        pending = self._pending_patches.pop(token,None)
        if pending is None:
            return None
        update = self._apply_changes(
            section_key,pending.changes,propagate=bool(propagate),
            lattice_lock_request=pending.lattice_lock_request,
        )
        if (
            edit_kind is ParameterEditKind.CGH_TARGET
            and pending.changes
            and update is not None
            and update.target_definition_changed
        ):
            self._emit_auto_compute(section_key)
        return update

    def _cancel_kind(
        self,
        section_key: str,
        edit_kind: ParameterEditKind,
        *,
        restore_paths: bool,
    ) -> bool:
        token = (section_key,edit_kind)
        timer = self._timers.get(token)
        if timer is not None:
            timer.stop()
        pending = self._pending_patches.pop(token,None)
        if pending is None:
            return False
        if restore_paths:
            snapshot = self.runtime.get_section_snapshot(section_key)
            view = self.section_collection.section_view(section_key)
            for path in pending.changes:
                try:
                    view.set_parameter(path,snapshot.state.get_parameter(path))
                except Exception:
                    # A topology/selector boundary may make a stale draft path
                    # unavailable; the authoritative snapshot still wins.
                    pass
            if pending.lattice_lock_request is not None:
                self._restore_target_lock_presentation(section_key)
        return True

    def _debounce_for_kind(self,edit_kind: ParameterEditKind) -> int:
        if edit_kind is ParameterEditKind.CGH_TARGET:
            return self.interaction_settings.target_patch_debounce_ms
        return self.interaction_settings.standard_patch_debounce_ms

    def _timer(
        self,section_key: str,edit_kind: ParameterEditKind,
    ) -> QtCore.QTimer:
        token = (section_key,edit_kind)
        timer = self._timers.get(token)
        if timer is None:
            timer = QtCore.QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(
                lambda key=section_key,kind=edit_kind:
                    self._flush_kind(key,kind),
            )
            self._timers[token] = timer
        return timer

    def _draft_lock_reference(
        self,section_key: str,target_key: str,kind: str | None,
    ):
        if kind is None:
            return None
        snapshot = self.runtime.get_section_snapshot(section_key)
        target = snapshot.state.cgh.items[target_key]
        keys = (
            ("fov_x_px","fov_y_px")
            if kind == "fov" else
            ("n_foci_x","n_foci_y")
        )
        pending = self._pending_patches.get(
            (section_key,ParameterEditKind.CGH_TARGET)
        )
        values = []
        for key in keys:
            path = ("cgh",target_key,"params",key)
            if pending is not None and path in pending.changes:
                values.append(pending.changes[path])
            else:
                values.append(target.params.get_param_value(key))
        return tuple(values)

    def _restore_target_lock_presentation(self,section_key: str) -> None:
        snapshot = self.runtime.get_section_snapshot(section_key)
        view = self.section_collection.section_view(section_key)
        for group_view in view.groups.values():
            sync = getattr(group_view,"sync_target_lock_states",None)
            if callable(sync):
                sync(snapshot.state.cgh)

    def _emit_auto_compute(self,section_key: str) -> None:
        if self._disposed:
            return
        if (
            section_key,ParameterEditKind.CGH_TARGET
        ) in self._pending_patches:
            return
        cgh_status = self.runtime.get_section_cgh_status(section_key)
        if getattr(cgh_status,"target_type",None) is None:
            return
        if not self.section_collection.auto_recompute_enabled(section_key):
            return
        status = self.runtime.get_section_feedback_status(section_key)
        if base_cgh_recompute_would_discard_feedback(status):
            return
        self.sigAutoComputeRequested.emit(section_key)

    def _apply_changes(
        self,
        section_key: str,
        changes: Mapping[ParamPath,Any],
        *,
        propagate: bool,
        lattice_lock_request: LatticeLockRequest | None=None,
    ) -> SectionUpdate | None:
        self._require_writes_enabled()
        try:
            target = self.application_session
            if target is None:
                update = self.runtime.apply_section_patch(
                    section_key,changes,
                    lattice_lock_request=lattice_lock_request,
                )
            else:
                try:
                    update = target.apply_section_patch(
                        section_key,changes,
                        lattice_lock_request=lattice_lock_request,
                    )
                except CorrectionSourceInvalidatedError as error:
                    confirm = self._correction_source_switch_confirm
                    if confirm is None or not confirm(section_key,error):
                        raise
                    update = target.apply_section_patch(
                        section_key,changes,
                        lattice_lock_request=lattice_lock_request,
                        correction_mismatch_policy=(
                            CorrectionMismatchPolicy.USE_CURRENT
                        ),
                    )
            if update is None:
                self._restore_target_lock_presentation(section_key)
                return None
            self.section_collection.apply_section_update(section_key,update)
            self.section_collection.set_feedback_status(
                section_key,
                self.runtime.get_section_feedback_status(section_key),
            )
        except Exception as error:
            self._restore_authoritative_preserving_drafts(section_key)
            if propagate:
                raise
            self.sigPatchFailed.emit(section_key,error)
            return None

        self.sigPatchApplied.emit(section_key,update)
        return update

    def _restore_authoritative_preserving_drafts(self,section_key: str) -> None:
        """Recover from one failed transaction without dropping the other timer."""
        self.section_collection.restore_section(
            section_key,self.runtime.get_section_snapshot(section_key),
        )
        view = self.section_collection.section_view(section_key)
        for (key,_kind),pending in self._pending_patches.items():
            if key != section_key:
                continue
            for path,value in pending.changes.items():
                try:
                    view.set_parameter(path,value)
                except Exception:
                    pass

    def _require_section(self,section_key: str) -> None:
        if section_key not in self.runtime.section_keys:
            raise KeyError(f"Unknown SLM section '{section_key}'")

    def _require_active(self) -> None:
        if self._disposed:
            raise RuntimeError("SLMRuntimeViewBinding has been disposed")

    def _require_writes_enabled(self) -> None:
        if not self._writes_enabled:
            raise RuntimeError("SLM runtime editing is disabled")
