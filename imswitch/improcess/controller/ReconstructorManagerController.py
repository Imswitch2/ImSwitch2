import copy
import json
import os
import uuid

from qtpy import QtCore

from imswitch.improcess.execution.result_assembly import ResultAssemblyPlan
from imswitch.improcess.execution.result_publication import PublicationHooks, ResultPublication

from .reconstruction_worker import (
    ReconstructionWorker,
    ReconstructionWorkerFailure,
    ReconstructionWorkerJob,
    ReconstructionWorkerOutcome,
)
from .localization_worker import (
    LocalizationWorker,
    LocalizationWorkerFailure,
    LocalizationWorkerJob,
    LocalizationWorkerOutcome,
)
from .basecontrollers import ImProcessWidgetController


class ReconstructorManagerController(ImProcessWidgetController):
    """Manages reconstructor selection, parameter installation, and dispatch
    to the active reconstructor plugin. The coordinator retains shared state
    (_activeReconstructor, _currentDataObj); the manager mutates/reads them
    through self._main.

    Extracted from ``ImProcessMainViewController`` following the pattern of
    ``FileIOController`` and ``WidefieldStarssBatchController``.
    """

    def __init__(self, *args, mainController=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._main = mainController
        self._reconstructionThread = None
        self._reconstructionWorker = None
        self._reconstructionWorkerReconstructor = None
        self._reconstructionJobLabels = []
        self._multiPublication = None
        self._multiPublicationError = None
        self._multiPublicationCount = 0
        self._publishWorkerItemsSeparately = False
        self._latestWorkerResult = None
        self._localizationThread = None
        self._localizationWorker = None
        self._localizationActiveJob = None
        self._localizationPendingJob = None
        self._localizationRequestCounter = 0
        self._localizationExpectedRequestId = None
        current_result_changed = getattr(self._commChannel, "sigCurrentResultChanged", None)
        if current_result_changed is not None:
            current_result_changed.connect(lambda _result: self.refreshUpdateReconstructionAvailability())

    def initActiveReconstructor(self):
        """Select, install, and publish the default reconstructor. Called once
        by the coordinator after all subsidiary controllers exist."""
        self._main._activeReconstructor = self._select_reconstructor()
        if self._main._activeReconstructor is not None:
            self._install_reconstructor_params(self._main._activeReconstructor)
        self._publishReconstructorChoices()

    def isReconstructionRunning(self) -> bool:
        """Whether a worker-thread reconstruction job is in flight."""
        return self._reconstructionThread is not None

    def pluginsReloaded(self):
        """Re-sync the active reconstructor and the picker after drop-in
        reconstructors were re-registered.

        Three cases. The active one is untouched (a built-in, or a plugin
        whose file did not change): only the picker is refreshed. The same id
        is now a fresh instance (an edited plugin): it is swapped in and its
        parameter widget rebuilt, so the edit is live at once. The active one
        is gone (its file was removed): the first registered reconstructor
        takes over, as at startup.
        """
        from imswitch.improcess.reconstructors.registry import get_registry

        registry = get_registry()
        active = self._main._activeReconstructor
        fresh = (
            registry.get_reconstructor(active.id, raise_on_missing=False)
            if active is not None
            else None
        )
        if active is not None and fresh is active:
            pass
        elif fresh is not None:
            self._logger.info(
                f"Reconstructor {fresh.id!r} was reloaded; using its new code"
            )
            self._main._activeReconstructor = fresh
            self._install_reconstructor_params(fresh)
        else:
            if active is not None:
                self._logger.info(
                    f"Active reconstructor {active.id!r} is no longer available; "
                    "selecting another"
                )
            self._main._activeReconstructor = self._select_reconstructor()
            if self._main._activeReconstructor is not None:
                self._install_reconstructor_params(self._main._activeReconstructor)
        self._publishReconstructorChoices()

    def reconstructorLoaded(self, plugin_id: str) -> bool:
        """A reconstructor was registered at runtime: offer it in the picker
        and make it active.

        Returns whether it became the active one. It does not when it cannot
        take the current source and the user's selection cannot be reopened
        for it; the picker then does not offer it until a file it accepts is
        open, and the caller says so.
        """
        self._publishReconstructorChoices()
        self._on_user_changed_reconstructor(plugin_id)
        active = self._main._activeReconstructor
        return active is not None and active.id == plugin_id

    def _select_reconstructor(self):
        from imswitch.improcess.reconstructors.registry import get_registry

        reconstructors = get_registry().reconstructors()
        if not reconstructors:
            self._logger.warning("No ImProcess reconstructors registered")
            return None
        reconstructor = reconstructors[0]
        self._logger.info(
            f"Using active reconstructor: {reconstructor.id} ({reconstructor.name})"
        )
        return reconstructor

    def _publishReconstructorChoices(self):
        """Send the current registered-reconstructor list to the Parameters
        dock picker. Best-effort: silently no-ops on view builds that don't
        expose the picker yet."""
        try:
            from imswitch.improcess.reconstructors.registry import get_registry

            choices = [
                (r.id, r.name)
                for r in get_registry().reconstructors()
                if self._offerable(r)
            ]
            current = self._main._activeReconstructor.id if self._main._activeReconstructor else None
            self._widget.setReconstructorChoices(choices, current)
        except AttributeError:
            pass
        except Exception as exc:
            self._logger.debug(
                f"Could not publish reconstructor choices to view: {exc}"
            )

    def _accepts_current_source(self, reconstructor) -> bool:
        data_obj = getattr(self._main, '_currentDataObj', None)
        source_kind = getattr(data_obj, 'sourceKind', 'image')
        accepted = tuple(
            getattr(reconstructor, 'accepted_source_kinds', ('image',))
        )
        return source_kind in accepted

    def _offerable(self, reconstructor) -> bool:
        """Whether the picker should list ``reconstructor`` at all.

        Filtering strictly on the *resolved* source kind is what made this a
        dead end: opening any file inside a tiling run resolves to that run's
        manifest, only the tiling reconstructor accepts a manifest, so the
        picker collapsed to a single entry and nothing in that folder could
        ever restore an image source. Offering what the user's own selection
        could still be opened as is the way out -- and choosing it re-resolves
        that path, so the offer is real rather than an entry that errors.
        """
        return (
            self._accepts_current_source(reconstructor)
            or self._reopen_path_for(reconstructor) is not None
        )

    def _reopen_path_for(self, reconstructor):
        """The user's selected path, if this reconstructor would accept it.

        Returns None when there is nothing to reopen -- no selection recorded,
        the selection is what is already loaded, or it resolves to a kind this
        reconstructor does not take.
        """
        from imswitch.improcess.model.dataset_sources import (
            resolve_dataset_source,
            source_kind_for,
            specs_for_reconstructor,
        )

        data_obj = getattr(self._main, '_currentDataObj', None)
        selected = getattr(data_obj, 'sourceOriginalPath', None)
        if not selected:
            return None
        accepted = tuple(
            getattr(reconstructor, 'accepted_source_kinds', ('image',))
        )
        try:
            resolved = resolve_dataset_source(
                selected, allowed_specs=specs_for_reconstructor(reconstructor)
            )
        except Exception:
            return None
        kind = source_kind_for(resolved.format_id)
        if kind not in accepted:
            return None
        # Already loaded means the same path *as the same kind of source*. A
        # time lapse is opened at the very file the user picked, so comparing
        # paths alone would call that file "already loaded" for a reconstructor
        # that wants it as an image, and strand the picker on the lapse.
        if (
            kind == getattr(data_obj, 'sourceKind', 'image')
            and str(resolved.path) == str(getattr(data_obj, 'dataPath', None))
        ):
            return None
        return selected

    def currentDataChanged(self, data_obj) -> None:
        """Select a compatible plugin and pass it a metadata-only inspection."""
        from imswitch.improcess.reconstructors.registry import get_registry

        # Any in-flight localization belongs to the previous current source.
        # It is allowed to finish in its worker thread, but its result is no
        # longer eligible to mutate the UI/session state.
        self._invalidate_localization_requests()

        if data_obj is None:
            # Clearing a deleted current source must not make a None object look
            # like a generic image source and silently switch reconstructors.
            self._publishReconstructorChoices()
            return

        active = self._main._activeReconstructor
        if active is None or not self._accepts_current_source(active):
            compatible = [
                candidate for candidate in get_registry().reconstructors()
                if self._accepts_current_source(candidate)
            ]
            if not compatible:
                self._logger.warning(
                    f"No reconstructor accepts source kind "
                    f"{getattr(data_obj, 'sourceKind', 'image')!r}"
                )
                self._main._activeReconstructor = None
                self._publishReconstructorChoices()
                return
            self._main._activeReconstructor = compatible[0]
            self._install_reconstructor_params(compatible[0])

        self._publishReconstructorChoices()
        self._inspect_current_source()
        self._defer_auto_localization(data_obj)

    def _inspect_current_source(self) -> None:
        reconstructor = self._main._activeReconstructor
        data_obj = getattr(self._main, '_currentDataObj', None)
        if reconstructor is None or data_obj is None:
            return
        try:
            inspection = reconstructor.inspect_source(data_obj)
            widget = getattr(self._widget, 'parTree', None)
            setter = getattr(widget, 'set_source_inspection', None)
            if callable(setter):
                setter(inspection)
            # Shown for every reconstructor, not only the one plugin whose own
            # widget implements the hook above. Without this the inspection
            # that knows a scan stopped at four of six positions was assembled
            # and thrown away, and the recording opened looking ordinary.
            self._showSourceInspection(inspection)
            self._logSourceInspection(data_obj, reconstructor, inspection)
        except Exception as exc:
            self._logger.warning(
                f"Could not inspect {getattr(data_obj, 'name', 'source')} "
                f"for {reconstructor.id}: {exc}"
            )

    def _confirm_reconstructor_change(self) -> bool:
        """Ask before a reconstructor change ends a running watch.

        ``True`` means go ahead -- either nothing was running, or the user
        accepted. On acceptance the watch is stopped here rather than left
        to :meth:`setDirectoryWatcherAvailable`, which only stops one when
        the panel is going away: switching between two *streaming* plugins
        keeps the panel, and would otherwise keep the old run alive.

        On refusal the picker is put back, so the combo keeps showing what
        is actually active. ``setActiveReconstructorName`` blocks signals,
        so reverting does not re-enter this handler.
        """
        widget = self._widget
        try:
            if widget.confirmDirectoryWatcherInterruption():
                widget.stopDirectoryWatcher()
                return True
        except AttributeError:
            # A view build without the directory-watcher panel has nothing
            # to interrupt.
            return True

        active = getattr(self._main, '_activeReconstructor', None)
        name = getattr(active, 'name', None)
        if name:
            try:
                widget.setActiveReconstructorName(name)
            except Exception:
                self._logger.debug(
                    'Could not restore the reconstructor picker', exc_info=True
                )
        return False

    def _showSourceInspection(self, inspection) -> None:
        """Put the inspection's warnings in the Parameters dock."""
        show = getattr(self._widget, 'setSourceInspection', None)
        if not callable(show):
            return
        lines = []
        severity = 'warning'
        if inspection is not None:
            issues = getattr(inspection, 'issues', ()) or ()
            for issue in issues:
                lines.append(str(issue.message))
                if getattr(issue, 'severity', 'warning') == 'error':
                    severity = 'error'
            if not issues:
                # ``warning`` is derived from the issues when there are any, so
                # it is only its own message when there are none.
                warning = getattr(inspection, 'warning', None)
                if warning:
                    lines.append(str(warning))
        try:
            show("\n".join(lines), severity)
        except Exception as exc:
            self._logger.debug(f"Could not display the source inspection: {exc}")

    def _logSourceInspection(self, data_obj, reconstructor, inspection) -> None:
        """Report what the source inspection found, when nothing displays it."""
        if inspection is None:
            return
        name = getattr(data_obj, 'name', 'source')
        warning = getattr(inspection, 'warning', None)
        if warning:
            self._logger.warning(f"{name}: {warning}")
        for issue in getattr(inspection, 'issues', ()) or ():
            message = f"{name}: [{issue.code}] {issue.message}"
            if getattr(issue, 'severity', 'warning') == 'error':
                self._logger.error(message)
            else:
                self._logger.warning(message)

    def _on_user_changed_reconstructor(self, plugin_id: str):
        """Slot for view-side picker: swap the active reconstructor and
        re-install its parameter widget. If the new reconstructor is
        pass-through and a current DataObj is already loaded, also kick the
        auto-route so the viewer reflects the change immediately."""
        if not plugin_id:
            return

        # A watch in flight belongs to the reconstructor that started it, and
        # the next job off the queue would use the new one -- one session
        # producing results from two plugins. So the watch ends here, and the
        # user is told before it does rather than after.
        if not self._confirm_reconstructor_change():
            return

        from imswitch.improcess.reconstructors.registry import get_registry

        for candidate in get_registry().reconstructors():
            if candidate.id == plugin_id:
                if not self._accepts_current_source(candidate):
                    # The picker only offers a reconstructor that cannot take
                    # the loaded source when the user's own selection could be
                    # reopened for it. Make the active choice first: reopening
                    # resolves the path under whichever reconstructor is
                    # active, so doing it the other way round would land on the
                    # same source again.
                    reopen = self._reopen_path_for(candidate)
                    if reopen is None:
                        self._logger.warning(
                            f"Reconstructor {candidate.id!r} does not accept "
                            "the current source kind"
                        )
                        self._publishReconstructorChoices()
                        return
                    self._main._activeReconstructor = candidate
                    self._install_reconstructor_params(candidate)
                    if not self._reopen_current_source(reopen):
                        self._publishReconstructorChoices()
                    return
                if self._main._activeReconstructor is candidate:
                    return
                self._main._activeReconstructor = candidate
                self._install_reconstructor_params(candidate)
                self._inspect_current_source()
                if (
                    getattr(candidate, 'is_pass_through', False)
                    and self._main._currentDataObj is not None
                ):
                    try:
                        self.reconstruct([self._main._currentDataObj], consolidate=False)
                    except Exception as exc:
                        self._logger.warning(
                            f"Pass-through auto-route on reconstructor switch failed: {exc}"
                        )
                return
        self._logger.warning(
            f"Reconstructor {plugin_id!r} requested by view picker is not registered"
        )

    def _reopen_current_source(self, path) -> bool:
        """Reload ``path`` now that a different reconstructor is active."""
        fileIO = getattr(self._main, 'fileIOController', None)
        loader = getattr(fileIO, 'loadFromPath', None) or getattr(
            fileIO, '_loadFromPath', None
        )
        if not callable(loader):
            self._logger.warning(
                'Cannot reopen the selected source: no file loader available'
            )
            return False
        try:
            # Promote it: the user asked to view this source, not to add it to
            # the multi-data list and leave the manifest current.
            loader(str(path), prefer_as_current=True)
        except Exception as exc:
            self._logger.warning(f"Could not reopen {path}: {exc}")
            return False
        return True

    def _install_reconstructor_params(self, reconstructor):
        # Multi-data relocalization is orchestration state, not a MoNaLISA
        # reconstruction parameter. Keep it beside the list it governs and
        # expose it only for the unified MoNaLISA plugin.
        try:
            multi = self._widget.multiDataFrame
            is_monalisa = reconstructor.id == "monalisa"
            multi.setRelocalizeEachDataItemVisible(is_monalisa)
            multi.setRelocalizeEachDataItemEnabled(is_monalisa)
        except Exception:
            pass

        # Always reflect the active reconstructor in the Parameters dock so
        # the user can tell at a glance which plugin's parameters they are
        # editing — even for plugins that keep the legacy parameter tree.
        try:
            self._widget.setActiveReconstructorName(reconstructor.name)
        except Exception:
            pass

        # Switching reconstructors must not leave a stale detection-preview
        # overlay on the raw-data viewer. The newly installed widget decides
        # whether its preview should start visible.
        if getattr(self, '_previewWidget', None) is not None:
            self._invalidate_localization_requests()
            self._previewWidget = None
            try:
                import numpy as np
                self._commChannel.sigDetectionPreviewUpdated.emit(
                    np.array([]), np.array([])
                )
                self._commChannel.sigDetectionPreviewVisibilityChanged.emit(False)
            except Exception:
                pass

        # Gate the modality-specific DATA-footer actions:
        # - 'Reconstruct current' is ceremonial for pass-through plugins
        #   (process() is a no-op wrap), so hide it; currentDataChanged
        #   auto-routes the data to the viewer in that case.
        # - Update reconstruction is now owned by unified MoNaLISA's Scanning
        #   controls; the compatibility flag below no longer creates a footer
        #   button.
        # - The multidata button stays visible for every plugin. Pass-through
        #   plugins get it relabeled as a batch 'load to viewer' action with
        #   the consolidate entry hidden; plugins without consolidation
        #   support keep the entry visible but disabled, so a multidata run
        #   never silently degrades to individual processing.
        # NOTE: Special-case by ID retained because update_reconstruction is a
        # MoNaLISA-specific UI action that cannot be expressed through the
        # plugin registry's current API.
        is_pass_through = bool(getattr(reconstructor, 'is_pass_through', False))
        supports_consolidation = bool(
            getattr(reconstructor, 'supports_consolidation', False)
        )
        try:
            self._widget.setReconstructionActionsVisible(
                reconstruct_current=not is_pass_through,
                update_reconstruction=(reconstructor.id in ('monalisa', 'monalisa-legacy')),
                reconstruct_multidata=True,
                multidata_consolidate=(
                    'hidden' if is_pass_through
                    else ('enabled' if supports_consolidation else 'disabled')
                ),
                multidata_labels=(
                    ('Load multidata', 'Load data items to viewer')
                    if is_pass_through else None
                ),
            )
        except Exception:
            pass

        # The Directory watcher drives a live streaming run, so it is only
        # meaningful for a reconstructor that can make a session. Declared
        # capability, like is_pass_through above -- not another id check.
        try:
            streaming = bool(getattr(reconstructor, 'supports_streaming', False))
            derived = str(getattr(reconstructor, 'result_role', 'derived')) != 'inspection'
            # Auto after explicit finite readiness works for offline-only
            # reconstructors too. Only true acquisition-time following needs
            # a StreamingSession.
            session_controller = getattr(self._main, 'processingSessionController', None)
            auto_available = bool(
                derived and session_controller is not None
                and getattr(session_controller, 'session', None) is not None
                and getattr(session_controller, 'sessionMode', 'review') == 'follow'
            )
            self._widget.setDirectoryWatcherAvailable(streaming)
            self._widget.processingSessionFrame.setProcessingAvailable(derived)
            self._widget.setBatchProcessingAvailable(derived)
            self._widget.setSessionAutoProcessingAvailable(auto_available)
            # An unavailable Auto control must not leave a hidden policy armed.
            # Re-enabling Auto is an explicit user action for the new reconstructor.
            if not auto_available:
                queue_controller = getattr(self._main, 'processingQueueController', None)
                if queue_controller is not None and queue_controller.autoPolicy.enabled:
                    queue_controller.setAutoEnabled(False)
        except Exception:
            # Logged, not swallowed silently: a failure here leaves the panel
            # offering a run the plugin cannot perform, which looks like the
            # feature simply not working.
            self._logger.debug(
                'Could not update Directory watcher availability', exc_info=True
            )

        # The classic SignalExtractor path remains on the legacy parameter tree
        # until its standalone UI is completed.  The unified Gaussian MoNaLISA
        # plugin now uses the normal plugin-owned fixed parameter widget.
        if reconstructor.id == "monalisa-legacy":
            try:
                self._widget.setLegacyMonalisaParameterWidget()
            except Exception:
                pass
            return
        widget = reconstructor.make_param_widget(self._widget)
        self._widget.setParameterWidget(widget)
        # The widget exists now, on the GUI thread: the one safe place to
        # compare it with the plugin's headless declaration. A mismatch is
        # remembered on the class, so this reconstructor's results record
        # why they cannot be replayed.
        from imswitch.improcess.model.plugin_contract import warn_contract_problems

        warn_contract_problems(self._logger, reconstructor, widget)
        if getattr(self._main, '_currentDataObj', None) is not None:
            self._inspect_current_source()
        # NOTE: Special-case by ID retained because widefield-starss batch signals
        # are plugin-specific and cannot be generically wired through the registry.
        if reconstructor.id == "widefield-starss" and hasattr(widget, "sigRunBatchRequested"):
            try:
                widget.sigRunBatchRequested.connect(self._main.wfsBatchController.runBatch)
                widget.sigCancelBatchRequested.connect(self._main.wfsBatchController.cancelBatch)
                if hasattr(widget, "sigPlotMetricRequested"):
                    widget.sigPlotMetricRequested.connect(self._main.wfsBatchController.plotMetric)
                if hasattr(widget, "sigTablePlotRequested"):
                    widget.sigTablePlotRequested.connect(self._main.wfsBatchController.plotTable)
            except Exception:
                pass

        if reconstructor.id == "monalisa":
            try:
                if hasattr(widget, "sigModeChanged"):
                    widget.sigModeChanged.connect(self._set_monalisa_update_enabled)
                if hasattr(widget, "sigUpdateRequested"):
                    widget.sigUpdateRequested.connect(self.updateReconstruction)
                if hasattr(widget, "sigUpdateEligibilityChanged"):
                    widget.sigUpdateEligibilityChanged.connect(
                        self.refreshUpdateReconstructionAvailability
                    )
                self.refreshUpdateReconstructionAvailability()
            except Exception:
                pass

        # Live detection preview wiring, for any plugin whose widget has the
        # preview checkbox (the SMLM localizer, the MoNaLISA lattice
        # reconstructor): what is drawn comes from the plugin's
        # ``detection_preview``. The installed widget replaces
        # self._widget.parTree, so keep our own reference for the preview
        # computation; the frame-changed connection is made once and stays —
        # _updatePreview no-ops when no preview widget is installed.
        if hasattr(widget, "sigPreviewToggled"):
            try:
                widget.sigPreviewToggled.connect(self._handlePreviewToggled)
                if hasattr(widget, "sigLocalizeRequested"):
                    widget.sigLocalizeRequested.connect(self._localizeCurrent)
                if hasattr(widget, "sigDetectionParamsChanged"):
                    widget.sigDetectionParamsChanged.connect(self._updatePreview)
                if not getattr(self, '_previewFrameSignalConnected', False):
                    self._commChannel.sigDisplayedFrameChanged.connect(
                        self._updatePreview
                    )
                    self._previewFrameSignalConnected = True
                self._previewWidget = widget
                preview_checkbox = getattr(widget, "previewCheckbox", None)
                if preview_checkbox is not None and preview_checkbox.isChecked():
                    self._handlePreviewToggled(True)
                if hasattr(widget, "sigLocalizationModeChanged"):
                    widget.sigLocalizationModeChanged.connect(
                        self._set_monalisa_relocalize_enabled
                    )
                    try:
                        mode = widget.get_values().get("localization_mode", "Auto")
                        self._set_monalisa_relocalize_enabled(mode)
                    except Exception:
                        pass
            except Exception:
                pass

        if reconstructor.id == "monalisa":
            self._defer_auto_localization(
                getattr(self._main, "_currentDataObj", None)
            )

    @QtCore.Slot(str)
    def _set_monalisa_update_enabled(self, _mode):
        self.refreshUpdateReconstructionAvailability()

    @QtCore.Slot()
    def refreshUpdateReconstructionAvailability(self):
        """Keep the MoNaLISA Scan/Orientation update action exactly gated."""
        reconstructor = getattr(self._main, "_activeReconstructor", None)
        widget = getattr(self._widget, "parTree", None)
        setter = getattr(widget, "setUpdateReconstructionEnabled", None)
        if reconstructor is None or reconstructor.id != "monalisa" or not callable(setter):
            return
        if self.isReconstructionRunning():
            setter(False, "Wait for the current reconstruction to finish.")
            return
        if self._shared_processing_queue_busy():
            setter(False, "Pause or finish queued processing before updating reconstruction.")
            return
        result = None
        reconstruction_controller = getattr(self._main, "reconstructionController", None)
        if reconstruction_controller is not None:
            result = reconstruction_controller.getActiveResult()
        try:
            params = widget.get_values() if hasattr(widget, "get_values") else {}
            checker = getattr(reconstructor, "can_update_reconstruction", None)
            if callable(checker):
                enabled, reason = checker(result, params)
            else:
                enabled, reason = False, "Update reconstruction is unavailable."
        except Exception as exc:
            enabled, reason = False, str(exc)
        setter(bool(enabled), str(reason or ""))

    @QtCore.Slot(str)
    def _set_monalisa_relocalize_enabled(self, mode):
        auto = str(mode) == "Auto"
        try:
            self._widget.multiDataFrame.setRelocalizeEachDataItemEnabled(auto)
        except Exception:
            pass
        if not auto:
            self._invalidate_localization_requests()
            return
        self._defer_auto_localization(
            getattr(self._main, "_currentDataObj", None)
        )

    def _monalisa_relocalize_each_data_item(self) -> bool:
        try:
            return bool(self._widget.multiDataFrame.relocalizeEachDataItem())
        except Exception:
            return True  # Independent datasets require independent Auto calibration.

    def reuseMonalisaCalibration(
        self, snapshot: dict, *, mode: str = "both", source_label: str = "reconstruction"
    ) -> str | None:
        """Import a reconstruction calibration into the active MoNaLISA registry/UI."""
        active = getattr(self._main, "_activeReconstructor", None)
        if active is None or active.id != "monalisa":
            self._on_user_changed_reconstructor("monalisa")
            active = getattr(self._main, "_activeReconstructor", None)
        if active is None or active.id != "monalisa":
            raise ValueError("Select the MoNaLISA reconstructor before reusing its calibration")

        widget = getattr(self, "_previewWidget", None)
        apply_snapshot = getattr(widget, "reuse_calibration_snapshot", None)
        if not callable(apply_snapshot):
            raise RuntimeError("The active MoNaLISA parameter widget cannot import calibration")
        return apply_snapshot(
            dict(snapshot or {}), mode=str(mode), source_label=str(source_label or "reconstruction")
        )

    def _shared_processing_queue_busy(self) -> bool:
        controller = getattr(self._main, 'processingQueueController', None)
        if controller is None:
            return False
        queue = getattr(controller, 'queue', None)
        if queue is None:
            return False
        if getattr(queue, 'current', None) is not None:
            return True
        return bool(getattr(queue, 'waiting', ())) and not bool(getattr(queue, 'paused', False))

    def updateReconstruction(self):
        """Refresh the active MoNaLISA result without repeating extraction."""
        if self._shared_processing_queue_busy():
            self._set_reconstruction_job_state(
                False, status="Reconstruction queue is active; pause it before updating."
            )
            return
        reconstructor = self._main._activeReconstructor
        if reconstructor is None:
            return
        if reconstructor.id == "monalisa-legacy":
            self._main.monalisaController.updateScanParams(applyOnCurrentRecon=True)
            return
        updater = getattr(reconstructor, "update_reconstruction", None)
        if not callable(updater):
            return
        result = self._main.reconstructionController.getActiveResult()
        if result is None:
            self._set_reconstruction_job_state(
                False, status="No reconstruction is selected to update."
            )
            return
        try:
            params = self._params_for_data_obj(reconstructor)
            updated = updater(result, params)
        except Exception as exc:
            self._logger.warning(f"Could not update reconstruction: {exc}")
            self._set_reconstruction_job_state(False, status=str(exc))
            return
        self._commChannel.sigResultReplaced.emit(
            result, updated, getattr(updated, "name", "reconstruction")
        )
        widget = getattr(self, "_previewWidget", None)
        sync = getattr(widget, "sync_reconstruction_results", None)
        if callable(sync):
            try:
                sync([updated])
            except Exception:
                self._logger.debug(
                    "Could not refresh MoNaLISA controls after update", exc_info=True
                )
        self._set_reconstruction_job_state(
            False, status="Reconstruction placement updated from retained Fast extraction."
        )

    def reconstructCurrent(self):
        if self._shared_processing_queue_busy():
            self._set_reconstruction_job_state(
                False, status="Reconstruction queue is active; pause it before reconstructing current data."
            )
            return
        data_controller = getattr(self._main, 'dataFrameController', None)
        ready = getattr(data_controller, 'currentDataReadyForProcessing', None)
        if callable(ready) and not ready():
            self._set_reconstruction_job_state(
                False, status='Current data is still loading.'
            )
            return
        if self._main._currentDataObj is None:
            return

        self.reconstruct([self._main._currentDataObj], consolidate=False)

    def openInspection(self, plugin_id: str, data_obj) -> str:
        """Publish one inspection result without changing the active plugin.

        Session's ``Open in viewer`` is a navigation/inspection action, not a
        processing choice.  It therefore resolves the inspection plugin
        directly from the registry and runs it with its own defaults instead
        of installing it in the Parameters panel.
        """
        from imswitch.improcess.reconstructors import (
            available_reconstructor_specs,
            register_reconstructor_by_id,
        )
        from imswitch.improcess.reconstructors.registry import get_registry

        registry = get_registry()
        reconstructor = registry.get_reconstructor(plugin_id, raise_on_missing=False)
        registered_for_viewer = False
        if reconstructor is None:
            try:
                reconstructor = register_reconstructor_by_id(registry, plugin_id)
            except Exception as exc:
                viewer_name = (
                    "Time Lapse" if plugin_id == "time-lapse" else "image"
                )
                raise RuntimeError(
                    f"Could not load the {viewer_name} viewer; see the log for details"
                ) from exc
            registered_for_viewer = True

        if (
            str(getattr(reconstructor, "result_role", "derived") or "derived")
            != "inspection"
        ):
            # ``openInspection`` is deliberately restricted to inspection
            # plugins. If an unexpected id was auto-registered above, undo it
            # rather than silently changing the runtime plugin set.
            if registered_for_viewer:
                registry.unregister_reconstructor(plugin_id)
            raise ValueError(
                f"Reconstructor {plugin_id!r} is not an inspection plugin"
            )

        if registered_for_viewer:
            self._logger.info(
                f"Auto-loaded inspection reconstructor {plugin_id!r} for viewer"
            )
            # Registration must not activate the plugin, but it should update
            # the ordinary plugin UI so Tools -> Load reconstructor does not
            # continue to advertise it as unloaded.
            self._publishReconstructorChoices()
            setter = getattr(self._widget, "setAvailableReconstructors", None)
            if callable(setter):
                try:
                    loaded = {plugin.id for plugin in registry.reconstructors()}
                    setter(
                        [
                            (known_id, name, description)
                            for known_id, name, description
                            in available_reconstructor_specs()
                            if known_id not in loaded
                        ]
                    )
                except Exception as exc:
                    # Menu refresh is cosmetic; a working viewer must not fail
                    # just because the optional plugin menu could not refresh.
                    self._logger.debug(
                        f"Could not refresh reconstructor load menu: {exc}"
                    )

        params = copy.deepcopy(reconstructor.default_params())
        validator = getattr(reconstructor, "validate_source", None)
        if callable(validator):
            validator(data_obj)

        if getattr(reconstructor, "execution_policy", "inline") == "worker":
            started = self._start_worker_reconstruction(
                reconstructor, [data_obj], False, params_override=params
            )
            if not started:
                raise RuntimeError("The viewer could not be started")
            return "started"

        from imswitch.improcess.reconstructors.run import run_reconstruction

        result = run_reconstruction(reconstructor, data_obj, params).result
        # View-only results can point directly at a lazy source handle. Keep
        # its DataObj alive for exactly as long as the result remains loaded;
        # ReconstructionViewController already owns this resource contract.
        result._session_backing_data_obj = data_obj
        self._publishPluginResult(
            result, getattr(result, "name", "viewer"), reconstructor=reconstructor
        )
        return "ready"

    def reconstructMulti(self, consolidate):
        if self._shared_processing_queue_busy():
            self._set_reconstruction_job_state(
                False, status="Reconstruction queue is active; pause it before reconstructing Multi-data."
            )
            return
        self.reconstruct(self._widget.getMultiDatas(), consolidate)

    def reconstruct(self, dataObjs, consolidate):
        if self._main._activeReconstructor is None:
            return
        # The classic SignalExtractor is already registered as its own plugin,
        # but its standalone parameter widget/controller cleanup belongs to the
        # Legacy consolidation pass.  Preserve the proven legacy execution path
        # while allowing ``monalisa`` itself to use the normal plugin API now.
        if self._main._activeReconstructor.id == "monalisa-legacy":
            self._main.monalisaController.runLegacyReconstruct(dataObjs, consolidate)
            return
        self._reconstruct_with_plugin(dataObjs, consolidate)

    def _reconstruct_with_plugin(self, dataObjs, consolidate):
        reconstructor = self._main._activeReconstructor
        if reconstructor is None:
            return
        if consolidate and not getattr(reconstructor, 'supports_consolidation', False):
            # The UI disables the consolidate action for these plugins; this
            # guard keeps scripted callers on the individual path instead of
            # failing.
            self._logger.warning(
                f"{reconstructor.name} does not support consolidated "
                "multi-data reconstruction; processing items individually."
            )
            consolidate = False

        if getattr(reconstructor, 'execution_policy', 'inline') == 'worker':
            self._start_worker_reconstruction(
                reconstructor, list(dataObjs), consolidate
            )
            return

        from imswitch.improcess.reconstructors.run import (
            run_consolidation,
            run_reconstruction,
        )

        collected = []
        for dataObj in dataObjs:
            params = self._params_for_data_obj(reconstructor)
            validator = getattr(reconstructor, "validate_source", None)
            if callable(validator):
                try:
                    validator(dataObj)
                except Exception as exc:
                    self._logger.error(f"Reconstruction preflight failed: {exc}")
                    return
            self._logger.info(
                f"Running {reconstructor.id} reconstruction for {dataObj.name}"
            )
            result = run_reconstruction(reconstructor, dataObj, params).result
            if consolidate:
                collected.append(result)
            else:
                self._publishPluginResult(result, result.name)

        if not consolidate or not collected:
            return
        try:
            merged = run_consolidation(reconstructor, collected)
        except Exception:
            # Keep the per-file work: publish the individual results so a
            # failed merge (e.g. mismatched scan geometry) loses nothing.
            self._logger.exception(
                "Consolidation failed; publishing the individual results instead"
            )
            for result in collected:
                self._publishPluginResult(result, result.name)
            return
        self._publishPluginResult(merged, f'{merged.name}_multi')

    def snapshotProcessingParams(self, reconstructor=None):
        """Return an immutable-by-convention queue-time parameter snapshot.

        Queued work must not silently change when the user edits the parameter
        widget later.  The queue stores the returned dict and hands a fresh copy
        to each processing unit.
        """
        reconstructor = reconstructor or self._main._activeReconstructor
        if reconstructor is None:
            return {}
        params = copy.deepcopy(self._widget.getReconstructionParams())
        if reconstructor.id == "monalisa":
            params = dict(params)
            params['scan_params'] = copy.deepcopy(
                self._main.monalisaController._scanParDict
            )
        return params

    def _params_for_data_obj(self, reconstructor):
        return self.snapshotProcessingParams(reconstructor)

    def publishQueuedResult(self, result, displayName, reconstructor=None):
        """Publish one queue-produced result through the normal ImProcess path."""
        self._publishPluginResult(result, displayName, reconstructor=reconstructor)

    @staticmethod
    def _same_data_source(left, right) -> bool:
        """Whether two DataObj-like values identify the same concrete source.

        Auto-localization belongs to the Current-data source.  Detector identity
        alone is not sufficient for Multi-data reuse: two acquisitions from the
        same camera can require different localization.
        """
        if left is right and left is not None:
            return True
        if left is None or right is None:
            return False

        if str(getattr(left, "sourceKind", "image") or "image") != str(
            getattr(right, "sourceKind", "image") or "image"
        ):
            return False

        def normalized_path(obj):
            path = getattr(obj, "dataPath", None)
            if not path:
                file_obj = getattr(obj, "_file", None)
                path = getattr(file_obj, "filename", None)
            if not path:
                return None
            try:
                return os.path.normcase(os.path.abspath(os.fspath(path)))
            except (TypeError, ValueError):
                return str(path)

        left_path = normalized_path(left)
        right_path = normalized_path(right)
        if left_path is None or right_path is None or left_path != right_path:
            return False

        return str(getattr(left, "datasetName", "") or "") == str(
            getattr(right, "datasetName", "") or ""
        )

    @staticmethod
    def _default_memory_budget_bytes() -> int:
        """Reserve headroom for Qt, readers and one materialized input tile."""
        try:
            import psutil

            available = int(psutil.virtual_memory().available)
        except Exception:
            available = 2 * 1024 ** 3
        return max(256 * 1024 ** 2, int(available * 0.6))

    def _start_worker_reconstruction(
        self, reconstructor, data_objs, consolidate, *, params_override=None
    ):
        if self._reconstructionThread is not None:
            self._logger.warning("A reconstruction job is already running")
            self._set_reconstruction_job_state(
                True, status="A reconstruction is already running."
            )
            return False
        if not data_objs:
            return False

        def params_for(data_obj):
            if params_override is not None:
                return copy.deepcopy(params_override)
            return self._params_for_data_obj(reconstructor)

        budget = self._default_memory_budget_bytes()
        shared_auto_localizations = {}
        relocalize_each = False
        if reconstructor.id == "monalisa" and len(data_objs) > 1:
            first_params = params_for(data_objs[0])
            if str(first_params.get("localization_mode", "Auto")) == "Auto":
                relocalize_each = self._monalisa_relocalize_each_data_item()
                if not relocalize_each:
                    from imswitch.improcess.reconstructors.monalisa.session_registry import (
                        detector_identity,
                    )

                    # Reuse once per detector, never across detectors.  A
                    # Current-data auto-localization is source-owned, though:
                    # detector identity alone must not let localization from a
                    # different acquisition seed this batch.
                    first_by_detector = {}
                    for item in data_objs:
                        first_by_detector.setdefault(detector_identity(item), item)

                    current_data = getattr(self._main, "_currentDataObj", None)
                    current_detector = (
                        detector_identity(current_data)
                        if current_data is not None
                        else None
                    )
                    current_payload = dict(first_params.get("auto_localization") or {})
                    current_first_item = first_by_detector.get(current_detector)
                    if (
                        current_payload
                        and current_detector
                        and self._same_data_source(current_data, current_first_item)
                    ):
                        shared_auto_localizations[current_detector] = current_payload
                    localizer = getattr(reconstructor, "localize", None)
                    for detector, first_item in first_by_detector.items():
                        if detector in shared_auto_localizations:
                            continue
                        if not callable(localizer):
                            continue
                        self._setPreviewStatus(
                            f"Localizing first {detector} data item for multi-data reuse..."
                        )
                        localize_params = dict(first_params)
                        localize_params["auto_localization"] = {}
                        try:
                            localized = localizer(first_item, localize_params)
                            localization, status = localized[:2]
                            details = localized[2] if len(localized) > 2 else {}
                        except Exception as exc:
                            self._logger.error(f"MoNaLISA localization failed: {exc}")
                            self._setPreviewStatus(f"Localization failed: {exc}")
                            return False
                        shared_auto_localizations[detector] = localization.to_dict()
                        if first_item is current_data:
                            widget = getattr(self, "_previewWidget", None)
                            setter = getattr(widget, "set_auto_localization", None)
                            if callable(setter):
                                setter(localization, status)
                            set_spot = getattr(widget, "set_auto_spot_sigma_px", None)
                            if callable(set_spot) and isinstance(details, dict):
                                set_spot(details.get("spot_sigma_px"))

        jobs = []
        for data_obj in data_objs:
            params = params_for(data_obj)
            if reconstructor.id == "monalisa" and len(data_objs) > 1:
                if relocalize_each:
                    params = dict(params)
                    params["auto_localization"] = {}
                elif shared_auto_localizations:
                    from imswitch.improcess.reconstructors.monalisa.session_registry import (
                        detector_identity,
                    )
                    shared = shared_auto_localizations.get(detector_identity(data_obj))
                    if shared is not None:
                        params = dict(params)
                        params["auto_localization"] = dict(shared)
            try:
                validator = getattr(reconstructor, "validate_source", None)
                if callable(validator):
                    validator(data_obj)
                estimate = reconstructor.estimate_resources(data_obj, params)
            except Exception as exc:
                self._logger.error(f"Reconstruction preflight failed: {exc}")
                self._set_reconstruction_job_state(
                    False, status=f"Preflight failed: {exc}"
                )
                return False
            confirmed = False
            if estimate is not None and estimate.required_bytes > budget:
                confirmer = getattr(
                    self._widget, 'confirmReconstructionMemory', None
                )
                confirmed = bool(
                    callable(confirmer) and confirmer(estimate, budget)
                )
                if not confirmed:
                    self._logger.info(
                        "Reconstruction not started because its memory estimate "
                        "was not confirmed"
                    )
                    self._set_reconstruction_job_state(
                        False,
                        status="Reconstruction not started: memory estimate declined.",
                    )
                    return False
            jobs.append(ReconstructionWorkerJob(
                data_obj=data_obj,
                params=dict(params),
                memory_budget_bytes=budget,
                confirmed_over_budget=confirmed,
            ))

        # Consolidated Multi Data belongs on Dataset, not T, unless a future
        # explicit common-lapse identity says otherwise. Progressive assembly
        # is independent of true acquisition-time streaming.
        progressive = bool(
            consolidate and len(jobs) > 1
            and getattr(reconstructor, "supports_progressive_finite", False)
        )
        self._multiPublication = None
        self._multiPublicationError = None
        self._multiPublicationCount = 0
        self._publishWorkerItemsSeparately = not consolidate
        self._latestWorkerResult = None
        if progressive:
            ids = tuple(
                f"{getattr(job.data_obj, 'dataPath', '')}::"
                f"{getattr(job.data_obj, 'datasetName', '')}#{index}"
                for index, job in enumerate(jobs)
            )
            plan = ResultAssemblyPlan.for_datasets(
                uuid.uuid4().hex, "multidata", dataset_ids=ids,
            )
            channel = self._commChannel
            self._multiPublication = ResultPublication(plan, PublicationHooks(
                preview=channel.sigLiveResultUpdated.emit,
                finalized=channel.sigResultFinalized.emit,
                current_changed=channel.sigCurrentResultChanged.emit,
            ))

        thread = QtCore.QThread()
        worker = ReconstructionWorker(
            reconstructor, jobs,
            consolidate=consolidate and not progressive,
            retain_results=consolidate and not progressive,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.itemProgress.connect(self._on_reconstruction_item_progress)
        worker.resultReady.connect(self._on_reconstruction_result_ready)
        worker.finished.connect(self._on_reconstruction_finished)
        worker.failed.connect(self._on_reconstruction_failed)
        worker.cancelled.connect(self._on_reconstruction_cancelled)
        for signal in (worker.finished, worker.failed, worker.cancelled):
            signal.connect(worker.deleteLater)
            signal.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(
            lambda: self._clear_reconstruction_worker_refs(thread)
        )

        self._reconstructionThread = thread
        self._reconstructionWorker = worker
        self._reconstructionWorkerReconstructor = reconstructor
        self._reconstructionJobLabels = [
            str(getattr(job.data_obj, "name", "") or getattr(job.data_obj, "datasetName", "") or "Data")
            for job in jobs
        ]
        inspection = (
            str(getattr(reconstructor, "result_role", "derived") or "derived")
            == "inspection"
        )
        self._set_reconstruction_job_state(
            True,
            progress=0.0,
            status=(
                "Opening in viewer..."
                if inspection
                else f"Starting {reconstructor.name}..."
            ),
            item_index=1,
            item_total=max(1, len(jobs)),
            item_label=(self._reconstructionJobLabels[0] if self._reconstructionJobLabels else ""),
        )
        thread.start()
        return True

    def cancelReconstruction(self):
        worker = self._reconstructionWorker
        if worker is None:
            return
        worker.request_cancel()
        thread = self._reconstructionThread
        if thread is not None:
            thread.requestInterruption()
        self._set_reconstruction_job_state(
            True, status="Cancelling after the current read or processing step..."
        )

    @QtCore.Slot(int, int, object)
    def _on_reconstruction_item_progress(self, item_index, item_total, progress):
        status = progress.message or progress.phase.replace("_", " ").capitalize()
        label = ""
        index = int(item_index) - 1
        if 0 <= index < len(self._reconstructionJobLabels):
            label = self._reconstructionJobLabels[index]
        self._set_reconstruction_job_state(
            True,
            progress=progress.fraction,
            status=status,
            item_index=int(item_index),
            item_total=int(item_total),
            item_label=label,
        )

    @QtCore.Slot(object, object)
    def _on_reconstruction_result_ready(self, result, run) -> None:
        if self._multiPublicationError is not None:
            return
        publisher = self._multiPublication
        if publisher is not None:
            try:
                publisher.append_completed(result, self._multiPublicationCount, run=run)
                self._multiPublicationCount += 1
            except Exception as exc:
                self._multiPublicationError = str(exc)
                self._logger.exception("Multi Data progressive assembly failed")
                if self._reconstructionWorker is not None:
                    self._reconstructionWorker.request_cancel()
        elif self._publishWorkerItemsSeparately:
            self._latestWorkerResult = result
            # Independent data entries can be published immediately; this is
            # not a consolidated result and never silently shares a T axis.
            self._publishPluginResult(
                result, getattr(result, "name", "result"),
                reconstructor=self._reconstructionWorkerReconstructor,
            )

    @QtCore.Slot(object)
    def _on_reconstruction_finished(self, outcome: ReconstructionWorkerOutcome):
        reconstructor = self._reconstructionWorkerReconstructor
        if getattr(self, "_multiPublication", None) is not None:
            publisher = self._multiPublication
            self._multiPublication = None
            if self._multiPublicationError:
                publisher.finish("failed")
                self._set_reconstruction_job_state(
                    False, status=f"Multi-data assembly failed: {self._multiPublicationError}"
                )
                self._multiPublicationError = None
                return
            try:
                if publisher.published is None:
                    raise RuntimeError("Multi Data produced no completed output")
                publisher.finish("complete")
            except Exception as exc:
                self._logger.exception("Multi Data finalization failed")
                try:
                    publisher.finish("failed")
                except Exception:
                    self._logger.exception("Could not finalize retained Multi Data partial")
                self._set_reconstruction_job_state(False, status=f"Multi-data finalization failed: {exc}")
                return
            # Preserve existing MoNaLISA result/widget synchronization.
            widget = getattr(self, "_previewWidget", None)
            sync = getattr(widget, "sync_reconstruction_results", None)
            if callable(sync) and reconstructor is not None and reconstructor.id == "monalisa":
                sync((publisher.published,))
            sync_session = getattr(widget, "sync_current_transient", None)
            if callable(sync_session) and reconstructor is not None and reconstructor.id == "monalisa":
                try:
                    sync_session()
                except Exception as exc:
                    self._logger.debug("Could not refresh MoNaLISA session calibration: %s", exc)
            self._set_reconstruction_job_state(False, progress=1.0, status="Reconstruction complete.")
            return
        if getattr(self, "_publishWorkerItemsSeparately", False):
            self._publishWorkerItemsSeparately = False
            last = self._latestWorkerResult
            self._latestWorkerResult = None
            widget = getattr(self, "_previewWidget", None)
            if reconstructor is not None and reconstructor.id == "monalisa":
                sync_spot = getattr(widget, "sync_reconstruction_results", None)
                if callable(sync_spot) and last is not None:
                    try:
                        sync_spot((last,))
                    except Exception as exc:
                        self._logger.debug("Could not refresh MoNaLISA spot width: %s", exc)
                sync = getattr(widget, "sync_current_transient", None)
                if callable(sync):
                    try:
                        sync()
                    except Exception as exc:
                        self._logger.debug("Could not refresh MoNaLISA calibration: %s", exc)
            self._set_reconstruction_job_state(False, progress=1.0, status="Reconstruction complete.")
            return
        if outcome.merged is not None:
            self._publishPluginResult(
                outcome.merged,
                f'{outcome.merged.name}_multi',
                reconstructor=reconstructor,
            )
        else:
            if outcome.consolidation_error:
                self._logger.error(
                    "Consolidation failed; publishing individual results: "
                    f"{outcome.consolidation_error}"
                )
            for result in outcome.results:
                self._publishPluginResult(
                    result, result.name, reconstructor=reconstructor
                )
        widget = getattr(self, "_previewWidget", None)
        sync = getattr(widget, "sync_current_transient", None)
        if reconstructor is not None and reconstructor.id == "monalisa":
            sync_spot = getattr(widget, "sync_reconstruction_results", None)
            if callable(sync_spot):
                try:
                    sync_spot(outcome.results)
                except Exception as exc:
                    self._logger.debug(f"Could not refresh MoNaLISA spot width: {exc}")
            if callable(sync):
                try:
                    sync()
                except Exception as exc:
                    self._logger.debug(f"Could not refresh MoNaLISA session calibration: {exc}")
        inspection = (
            str(getattr(reconstructor, "result_role", "derived") or "derived")
            == "inspection"
        )
        # Inspection is a transient open/view operation rather than a
        # reconstruction result. Clear the shared DATA progress row as soon as
        # the viewer is ready instead of leaving a completed "Viewer ready"
        # bar visible until another tab/state refresh happens.
        if inspection:
            self._set_reconstruction_job_state(False)
        else:
            self._set_reconstruction_job_state(
                False, progress=1.0, status="Reconstruction complete."
            )

    @QtCore.Slot(object)
    def _on_reconstruction_failed(self, failure: ReconstructionWorkerFailure):
        if self._multiPublication is not None:
            self._multiPublication.finish("failed")
            self._multiPublication = None
        self._multiPublicationError = None
        self._publishWorkerItemsSeparately = False
        self._latestWorkerResult = None
        self._logger.error(
            f"Reconstruction failed: {failure.message}\n{failure.traceback}"
        )
        self._set_reconstruction_job_state(
            False, status=f"Reconstruction failed: {failure.message}"
        )

    @QtCore.Slot(str)
    def _on_reconstruction_cancelled(self, message):
        if self._multiPublication is not None:
            self._multiPublication.finish("cancelled" if not self._multiPublicationError else "failed")
            self._multiPublication = None
        assembly_error = self._multiPublicationError
        self._multiPublicationError = None
        self._publishWorkerItemsSeparately = False
        self._latestWorkerResult = None
        if assembly_error:
            self._logger.error("Multi-data assembly failed: %s", assembly_error)
            self._set_reconstruction_job_state(
                False, status=f"Multi-data assembly failed: {assembly_error}"
            )
            return
        self._logger.info(message or "Reconstruction cancelled")
        self._set_reconstruction_job_state(
            False, status="Reconstruction cancelled."
        )

    def _clear_reconstruction_worker_refs(self, thread):
        if self._reconstructionThread is thread:
            self._reconstructionThread = None
            self._reconstructionWorker = None
            self._reconstructionWorkerReconstructor = None
            self._reconstructionJobLabels = []

    def _set_reconstruction_job_state(
        self, running, *, progress=None, status="", item_index=None, item_total=None,
        item_label=""
    ):
        setter = getattr(self._widget, 'setReconstructionJobState', None)
        if callable(setter):
            setter(
                bool(running), progress=progress, status=str(status or ""),
                item_index=item_index, item_total=item_total, item_label=str(item_label or ""),
            )
        self.refreshUpdateReconstructionAvailability()

    def closeEvent(self):
        self.cancelReconstruction()
        self._invalidate_localization_requests()
        return self.shutdownComplete()

    def shutdownComplete(self):
        return (
            self._reconstructionThread is None
            and self._localizationThread is None
        )

    def _publishPluginResult(self, result, displayName, reconstructor=None):
        producer = reconstructor or self._main._activeReconstructor
        self._commChannel.sigResultProduced.emit(result, displayName)
        if str(getattr(producer, "result_role", "derived") or "derived") != "inspection":
            finalized = getattr(self._commChannel, "sigResultFinalized", None)
            if finalized is not None:
                finalized.emit(result, displayName)
        self._commChannel.sigCurrentResultChanged.emit(result)
        # NOTE: Special-case by ID retained because widefield-starss batch result
        # collection is plugin-specific and not part of the generic plugin API.
        if producer is not None and producer.id == "widefield-starss":
            self._main.wfsBatchController.appendSingleResult(result)
        # Push reconstruction-derived metadata (e.g. MoNaLISA's computed
        # output pixel size) back into the active parameter widget so
        # the user sees up-to-date numbers without flipping to napari's
        # scale bar.  Best-effort: silently no-ops on plugins / widgets
        # that don't expose setOutputPixelSize.
        output_pixel_size_nm = getattr(result, 'output_pixel_size_nm', None)
        par_tree = getattr(self._widget, 'parTree', None)
        setter = getattr(par_tree, 'setOutputPixelSize', None)
        if callable(setter):
            try:
                setter(output_pixel_size_nm)
            except Exception:
                pass

    def _handlePreviewToggled(self, enabled):
        """Handle the parameter widget's preview checkbox."""
        self._commChannel.sigDetectionPreviewVisibilityChanged.emit(enabled)
        if enabled:
            self._updatePreview()
        else:
            import numpy as np
            self._commChannel.sigDetectionPreviewUpdated.emit(
                np.array([]), np.array([])
            )
            self._setPreviewStatus("")

    _LOCALIZATION_SIGNATURE_KEYS = (
        "localization_mode",
        "localization_refinement",
        "localization_settings",
        "spot_width_mode",
        "spot_fwhm",
        "spot_fwhm_unit",
        "pixel_size_nm",
        "scan_steps_fast",
        "scan_steps_slow",
        "scan_step_nm",
    )

    @classmethod
    def _localization_signature(cls, params: dict) -> str:
        payload = {
            key: params.get(key)
            for key in cls._LOCALIZATION_SIGNATURE_KEYS
        }
        return json.dumps(
            payload, sort_keys=True, default=str, separators=(",", ":")
        )

    def _defer_auto_localization(self, data_obj) -> None:
        if data_obj is None:
            return
        QtCore.QTimer.singleShot(
            0, lambda source=data_obj: self._auto_localize_loaded_data(source)
        )

    def _auto_localize_loaded_data(self, data_obj) -> None:
        """Start Auto localization only after source loading/metadata UI settles."""
        if getattr(self._main, "_currentDataObj", None) is not data_obj:
            return
        reconstructor = self._main._activeReconstructor
        widget = getattr(self, "_previewWidget", None)
        if (
            reconstructor is None
            or reconstructor.id != "monalisa"
            or widget is None
        ):
            return
        try:
            params = widget.get_values()
        except Exception:
            return
        if str(params.get("localization_mode", "Auto")) != "Auto":
            return
        has_current = getattr(
            widget, "has_auto_localization_for_current_data", None
        )
        if callable(has_current) and has_current():
            return
        self._queue_localization(data_obj, params=params, automatic=True)

    def _localizeCurrent(self):
        """Queue explicit localization without blocking the Qt/UI thread."""
        reconstructor = self._main._activeReconstructor
        data_obj = getattr(self._main, "_currentDataObj", None)
        widget = getattr(self, "_previewWidget", None)
        if reconstructor is None or data_obj is None or widget is None:
            return
        if reconstructor.id != "monalisa":
            return
        try:
            params = widget.get_values()
        except Exception as exc:
            self._setPreviewStatus(f"Localization failed: {exc}")
            return
        if str(params.get("localization_mode", "Auto")) != "Auto":
            return
        self._queue_localization(data_obj, params=params, automatic=False)

    def _queue_localization(self, data_obj, *, params: dict, automatic: bool) -> None:
        reconstructor = self._main._activeReconstructor
        if (
            reconstructor is None
            or reconstructor.id != "monalisa"
            or not callable(getattr(reconstructor, "localize", None))
        ):
            return
        params = dict(params)
        # Never let a previously resolved Auto value turn the localization
        # request into implicit reuse. The worker is explicitly computing one.
        params["auto_localization"] = {}
        signature = self._localization_signature(params)

        for existing in (self._localizationActiveJob, self._localizationPendingJob):
            if (
                existing is not None
                and existing.data_obj is data_obj
                and existing.signature == signature
            ):
                return

        self._localizationRequestCounter += 1
        job = LocalizationWorkerJob(
            request_id=self._localizationRequestCounter,
            data_obj=data_obj,
            params=params,
            signature=signature,
            automatic=bool(automatic),
        )
        self._localizationExpectedRequestId = job.request_id

        widget = getattr(self, "_previewWidget", None)
        state = getattr(widget, "set_localization_job_state", None)
        if self._localizationThread is not None:
            # One expensive localization lane only: browsing through files
            # replaces the pending request instead of spawning work for every
            # intermediate dataset.
            self._localizationPendingJob = job
            if callable(state):
                state(True, "Auto localization queued…" if automatic else "Localization queued…")
            return
        self._start_localization_job(job)

    def _start_localization_job(self, job: LocalizationWorkerJob) -> None:
        if not self._localization_job_is_current(job):
            return
        reconstructor = self._main._activeReconstructor
        widget = getattr(self, "_previewWidget", None)
        state = getattr(widget, "set_localization_job_state", None)
        if callable(state):
            state(True, "Auto: localizing…" if job.automatic else "Localizing…")

        thread = QtCore.QThread()
        worker = LocalizationWorker(reconstructor, job)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_localization_finished)
        worker.failed.connect(self._on_localization_failed)
        for signal in (worker.finished, worker.failed):
            signal.connect(worker.deleteLater)
            signal.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(
            lambda: self._clear_localization_worker_refs(thread)
        )

        self._localizationThread = thread
        self._localizationWorker = worker
        self._localizationActiveJob = job
        thread.start()

    def _localization_job_is_current(self, job: LocalizationWorkerJob) -> bool:
        if self._localizationExpectedRequestId != job.request_id:
            return False
        if getattr(self._main, "_currentDataObj", None) is not job.data_obj:
            return False
        reconstructor = self._main._activeReconstructor
        widget = getattr(self, "_previewWidget", None)
        if reconstructor is None or reconstructor.id != "monalisa" or widget is None:
            return False
        try:
            current = widget.get_values()
        except Exception:
            return False
        if str(current.get("localization_mode", "Auto")) != "Auto":
            return False
        current = dict(current)
        current["auto_localization"] = {}
        return self._localization_signature(current) == job.signature

    @QtCore.Slot(object)
    def _on_localization_finished(self, outcome: LocalizationWorkerOutcome):
        job = outcome.job
        if not self._localization_job_is_current(job):
            return
        widget = getattr(self, "_previewWidget", None)
        setter = getattr(widget, "set_auto_localization", None)
        if not callable(setter):
            return
        setter(outcome.localization, outcome.status)
        set_spot = getattr(widget, "set_auto_spot_sigma_px", None)
        if callable(set_spot):
            set_spot(outcome.details.get("spot_sigma_px"))
        state = getattr(widget, "set_localization_job_state", None)
        if callable(state):
            state(False)
        self._localizationExpectedRequestId = None
        self._updatePreview()

    @QtCore.Slot(object)
    def _on_localization_failed(self, failure: LocalizationWorkerFailure):
        if not self._localization_job_is_current(failure.job):
            return
        self._logger.error(
            f"Localization failed: {failure.message}\n{failure.traceback}"
        )
        widget = getattr(self, "_previewWidget", None)
        state = getattr(widget, "set_localization_job_state", None)
        if callable(state):
            state(False, f"Localization failed: {failure.message}")
        else:
            self._setPreviewStatus(f"Localization failed: {failure.message}")
        self._localizationExpectedRequestId = None

    def _clear_localization_worker_refs(self, thread) -> None:
        if self._localizationThread is not thread:
            return
        self._localizationThread = None
        self._localizationWorker = None
        self._localizationActiveJob = None
        pending = self._localizationPendingJob
        self._localizationPendingJob = None
        if pending is not None and self._localization_job_is_current(pending):
            self._start_localization_job(pending)

    def _invalidate_localization_requests(self) -> None:
        self._localizationExpectedRequestId = None
        self._localizationPendingJob = None
        widget = getattr(self, "_previewWidget", None)
        state = getattr(widget, "set_localization_job_state", None)
        if callable(state):
            state(False)

    def _setPreviewStatus(self, text):
        widget = getattr(self, '_previewWidget', None)
        if widget is not None and hasattr(widget, 'setPreviewStatus'):
            try:
                widget.setPreviewStatus(text)
            except Exception:
                pass

    def _updatePreview(self):
        """Ask the active plugin for its preview of the displayed frame and draw it."""
        try:
            reconstructor = self._main._activeReconstructor
            compute = getattr(reconstructor, 'detection_preview', None)
            if reconstructor is None or not callable(compute):
                return

            widget = getattr(self, '_previewWidget', None)
            if widget is None or not hasattr(widget, 'previewCheckbox'):
                return
            if not widget.previewCheckbox.isChecked():
                return

            import numpy as np
            dataframe_ctrl = self._main.dataFrameController
            image = dataframe_ctrl.getDisplayedImage2D()
            if image is None:
                self._commChannel.sigDetectionPreviewUpdated.emit(
                    np.array([]), np.array([])
                )
                self._setPreviewStatus("Preview: no data loaded.")
                return

            data_obj = getattr(self._main, '_currentDataObj', None)
            try:
                preview = compute(data_obj, np.asarray(image), widget.get_values())
            except Exception as exc:
                self._commChannel.sigDetectionPreviewUpdated.emit(
                    np.array([]), np.array([])
                )
                self._setPreviewStatus(f"Preview: {exc}")
                return
            if preview is None:
                self._commChannel.sigDetectionPreviewUpdated.emit(
                    np.array([]), np.array([])
                )
                self._setPreviewStatus("")
                return
            self._commChannel.sigDetectionPreviewStyleChanged.emit(
                str(getattr(preview, 'marker', 'circle'))
            )
            self._commChannel.sigDetectionPreviewUpdated.emit(
                np.asarray(preview.x), np.asarray(preview.y)
            )
            self._setPreviewStatus(preview.status)
        except Exception as e:
            self._logger.debug(f"Preview computation failed: {e}")


# Copyright (C) 2020-2021 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
