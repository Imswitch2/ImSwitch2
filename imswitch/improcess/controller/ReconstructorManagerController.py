import copy

from qtpy import QtCore

from .reconstruction_worker import (
    ReconstructionWorker,
    ReconstructionWorkerFailure,
    ReconstructionWorkerJob,
    ReconstructionWorkerOutcome,
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
        if not selected or selected == getattr(data_obj, 'dataPath', None):
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
        return (
            selected
            if source_kind_for(resolved.format_id) in accepted
            else None
        )

    def currentDataChanged(self, data_obj) -> None:
        """Select a compatible plugin and pass it a metadata-only inspection."""
        from imswitch.improcess.reconstructors.registry import get_registry

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
        except Exception as exc:
            self._logger.warning(
                f"Could not inspect {getattr(data_obj, 'name', 'source')} "
                f"for {reconstructor.id}: {exc}"
            )

    def _on_user_changed_reconstructor(self, plugin_id: str):
        """Slot for view-side picker: swap the active reconstructor and
        re-install its parameter widget. If the new reconstructor is
        pass-through and a current DataObj is already loaded, also kick the
        auto-route so the viewer reflects the change immediately."""
        if not plugin_id:
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
        # Always reflect the active reconstructor in the Parameters dock so
        # the user can tell at a glance which plugin's parameters they are
        # editing — even for plugins that keep the legacy parameter tree.
        try:
            self._widget.setActiveReconstructorName(reconstructor.name)
        except Exception:
            pass

        # Switching away from the SMLM localizer must not leave a stale
        # detection-preview overlay on the raw-data viewer.
        if (getattr(self, '_smlmPreviewWidget', None) is not None
                and reconstructor.id != 'smlm-localizer'):
            self._smlmPreviewWidget = None
            try:
                import numpy as np
                self._commChannel.sigDetectionPreviewUpdated.emit(
                    np.array([]), np.array([])
                )
                self._commChannel.sigDetectionPreviewVisibilityChanged.emit(False)
            except Exception:
                pass

        # Gate the modality-specific Actions buttons:
        # - 'Reconstruct current' is ceremonial for pass-through plugins
        #   (process() is a no-op wrap), so hide it; currentDataChanged
        #   auto-routes the data to the viewer in that case.
        # - 'Update reconstruction' re-applies MoNaLISA scan parameters and
        #   only makes sense for the MoNaLISA plugin.
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
                update_reconstruction=(reconstructor.id == 'monalisa'),
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

        # Push the active reconstructor's preferred output folder name to
        # the file watcher so 'Watch and run' writes outputs under the
        # plugin's default_save_subdir instead of a hardcoded 'rec/'.
        watcher = self._main.watcherFrameController
        if watcher is not None:
            try:
                watcher.setSaveSubdir(getattr(reconstructor, 'default_save_subdir', 'rec'))
            except Exception:
                pass

        # NOTE: Special-case by ID retained because MoNaLISA uses a legacy parameter
        # tree that differs fundamentally from the standard plugin widget API.
        # This will remain until the scan-params/find-pattern path is migrated.
        if reconstructor.id == "monalisa":
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

        # SMLM live detection preview wiring. The installed widget replaces
        # self._widget.parTree, so keep our own reference for the preview
        # computation; the frame-changed connection is made once and stays —
        # _updateSmlmPreview no-ops when the SMLM widget is not installed.
        if reconstructor.id == "smlm-localizer" and hasattr(widget, "sigPreviewToggled"):
            try:
                widget.sigPreviewToggled.connect(self._handleSmlmPreviewToggled)
                widget.sigDetectionParamsChanged.connect(self._updateSmlmPreview)
                if not getattr(self, '_smlmFrameSignalConnected', False):
                    self._commChannel.sigDisplayedFrameChanged.connect(
                        self._updateSmlmPreview
                    )
                    self._smlmFrameSignalConnected = True
                self._smlmPreviewWidget = widget
            except Exception:
                pass

    def reconstructCurrent(self):
        if self._main._currentDataObj is None:
            return

        self.reconstruct([self._main._currentDataObj], consolidate=False)

    def reconstructMulti(self, consolidate):
        self.reconstruct(self._widget.getMultiDatas(), consolidate)

    def reconstruct(self, dataObjs, consolidate):
        # NOTE: Special-case by ID retained because MoNaLISA uses a separate legacy
        # reconstruction path (MoNaLISAController) that differs from the generic
        # plugin process() API.
        if self._main._activeReconstructor is None:
            return
        if self._main._activeReconstructor.id != "monalisa":
            self._reconstruct_with_plugin(dataObjs, consolidate)
            return
        params = self._widget.getReconstructionParams()
        if params.get('reconstruction_method') == 'Fast Gauss MoNaLISA':
            self._reconstruct_with_plugin(dataObjs, consolidate)
            return

        # Legacy MoNaLISA reconstruction path — delegate to MoNaLISAController.
        self._main.monalisaController.runLegacyReconstruct(dataObjs, consolidate)

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

    def _params_for_data_obj(self, reconstructor):
        params = self._widget.getReconstructionParams()
        if reconstructor.id == "monalisa":
            params = dict(params)
            params['scan_params'] = copy.deepcopy(
                self._main.monalisaController._scanParDict
            )
        return params

    @staticmethod
    def _default_memory_budget_bytes() -> int:
        """Reserve headroom for Qt, readers and one materialized input tile."""
        try:
            import psutil

            available = int(psutil.virtual_memory().available)
        except Exception:
            available = 2 * 1024 ** 3
        return max(256 * 1024 ** 2, int(available * 0.6))

    def _start_worker_reconstruction(self, reconstructor, data_objs, consolidate):
        if self._reconstructionThread is not None:
            self._logger.warning("A reconstruction job is already running")
            self._set_reconstruction_job_state(
                True, status="A reconstruction is already running."
            )
            return
        if not data_objs:
            return

        budget = self._default_memory_budget_bytes()
        jobs = []
        for data_obj in data_objs:
            params = self._params_for_data_obj(reconstructor)
            try:
                estimate = reconstructor.estimate_resources(data_obj, params)
            except Exception as exc:
                self._logger.error(f"Reconstruction preflight failed: {exc}")
                self._set_reconstruction_job_state(
                    False, status=f"Preflight failed: {exc}"
                )
                return
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
                    return
            jobs.append(ReconstructionWorkerJob(
                data_obj=data_obj,
                params=dict(params),
                memory_budget_bytes=budget,
                confirmed_over_budget=confirmed,
            ))

        thread = QtCore.QThread()
        worker = ReconstructionWorker(
            reconstructor, jobs, consolidate=consolidate
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_reconstruction_progress)
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
        self._set_reconstruction_job_state(
            True, progress=0.0, status=f"Starting {reconstructor.name}..."
        )
        thread.start()

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

    @QtCore.Slot(object)
    def _on_reconstruction_progress(self, progress):
        status = progress.message or progress.phase.capitalize()
        self._set_reconstruction_job_state(
            True, progress=progress.fraction, status=status
        )

    @QtCore.Slot(object)
    def _on_reconstruction_finished(self, outcome: ReconstructionWorkerOutcome):
        reconstructor = self._reconstructionWorkerReconstructor
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
        self._set_reconstruction_job_state(
            False, progress=1.0, status="Reconstruction complete."
        )

    @QtCore.Slot(object)
    def _on_reconstruction_failed(self, failure: ReconstructionWorkerFailure):
        self._logger.error(
            f"Reconstruction failed: {failure.message}\n{failure.traceback}"
        )
        self._set_reconstruction_job_state(
            False, status=f"Reconstruction failed: {failure.message}"
        )

    @QtCore.Slot(str)
    def _on_reconstruction_cancelled(self, message):
        self._logger.info(message or "Reconstruction cancelled")
        self._set_reconstruction_job_state(
            False, status="Reconstruction cancelled."
        )

    def _clear_reconstruction_worker_refs(self, thread):
        if self._reconstructionThread is thread:
            self._reconstructionThread = None
            self._reconstructionWorker = None
            self._reconstructionWorkerReconstructor = None

    def _set_reconstruction_job_state(
        self, running, *, progress=None, status=""
    ):
        setter = getattr(self._widget, 'setReconstructionJobState', None)
        if callable(setter):
            setter(
                bool(running), progress=progress, status=str(status or "")
            )

    def closeEvent(self):
        self.cancelReconstruction()
        return self.shutdownComplete()

    def shutdownComplete(self):
        return self._reconstructionThread is None

    def _publishPluginResult(self, result, displayName, reconstructor=None):
        self._commChannel.sigResultProduced.emit(result, displayName)
        self._commChannel.sigCurrentResultChanged.emit(result)
        # NOTE: Special-case by ID retained because widefield-starss batch result
        # collection is plugin-specific and not part of the generic plugin API.
        producer = reconstructor or self._main._activeReconstructor
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

    def _handleSmlmPreviewToggled(self, enabled):
        """Handle SMLM preview checkbox toggle."""
        self._commChannel.sigDetectionPreviewVisibilityChanged.emit(enabled)
        if enabled:
            self._updateSmlmPreview()
        else:
            import numpy as np
            self._commChannel.sigDetectionPreviewUpdated.emit(
                np.array([]), np.array([])
            )
            self._setSmlmPreviewStatus("")

    def _setSmlmPreviewStatus(self, text):
        widget = getattr(self, '_smlmPreviewWidget', None)
        if widget is not None and hasattr(widget, 'setPreviewStatus'):
            try:
                widget.setPreviewStatus(text)
            except Exception:
                pass

    def _updateSmlmPreview(self):
        """Compute and emit SMLM detection preview for the currently displayed frame."""
        try:
            if (self._main._activeReconstructor is None
                    or self._main._activeReconstructor.id != "smlm-localizer"):
                return

            widget = getattr(self, '_smlmPreviewWidget', None)
            if widget is None or not hasattr(widget, 'previewCheckbox'):
                return
            if not widget.previewCheckbox.isChecked():
                return

            dataframe_ctrl = self._main.dataFrameController
            image = dataframe_ctrl.getDisplayedImage2D()
            if image is None:
                import numpy as np
                self._commChannel.sigDetectionPreviewUpdated.emit(
                    np.array([]), np.array([])
                )
                self._setSmlmPreviewStatus("Preview: no data loaded.")
                return

            params = widget.get_detection_values()
            import numpy as np
            image = np.asarray(image)

            from imswitch.improcess.reconstructors.smlm.preview import compute_detection_preview
            x, y = compute_detection_preview(
                image,
                threshold=params['threshold'],
                roi=params['roi'],
                sigma=params['sigma'],
            )
            self._commChannel.sigDetectionPreviewUpdated.emit(x, y)
            if len(x):
                self._setSmlmPreviewStatus(
                    f"Preview: {len(x)} candidate spot(s) on the displayed frame."
                )
            else:
                self._setSmlmPreviewStatus(
                    "Preview: 0 candidates — lower the net-gradient threshold "
                    f"(now {params['threshold']:g})."
                )
        except Exception as e:
            self._logger.debug(f"SMLM preview computation failed: {e}")


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
