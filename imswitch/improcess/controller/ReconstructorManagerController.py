import copy

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

    def initActiveReconstructor(self):
        """Select, install, and publish the default reconstructor. Called once
        by the coordinator after all subsidiary controllers exist."""
        self._main._activeReconstructor = self._select_reconstructor()
        if self._main._activeReconstructor is not None:
            self._install_reconstructor_params(self._main._activeReconstructor)
        self._publishReconstructorChoices()

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

            choices = [(r.id, r.name) for r in get_registry().reconstructors()]
            current = self._main._activeReconstructor.id if self._main._activeReconstructor else None
            self._widget.setReconstructorChoices(choices, current)
        except AttributeError:
            pass
        except Exception as exc:
            self._logger.debug(
                f"Could not publish reconstructor choices to view: {exc}"
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
                if self._main._activeReconstructor is candidate:
                    return
                self._main._activeReconstructor = candidate
                self._install_reconstructor_params(candidate)
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
        # NOTE: Special-case by ID retained because update_reconstruction is a
        # MoNaLISA-specific UI action that cannot be expressed through the
        # plugin registry's current API.
        is_pass_through = bool(getattr(reconstructor, 'is_pass_through', False))
        try:
            self._widget.setReconstructionActionsVisible(
                reconstruct_current=not is_pass_through,
                update_reconstruction=(reconstructor.id == 'monalisa'),
                reconstruct_multidata=not is_pass_through,
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
        if self._main._activeReconstructor is None:
            return
        if consolidate:
            self._logger.warning(
                f"{self._main._activeReconstructor.name} does not support consolidated "
                "multi-data reconstruction yet; processing items individually."
            )
        for dataObj in dataObjs:
            params = self._widget.getReconstructionParams()
            if self._main._activeReconstructor.id == "monalisa":
                params = dict(params)
                params['scan_params'] = copy.deepcopy(
                    self._main.monalisaController._scanParDict
                )
            self._logger.info(
                f"Running {self._main._activeReconstructor.id} reconstruction for {dataObj.name}"
            )
            result = self._main._activeReconstructor.process(dataObj, params)
            self._commChannel.sigResultProduced.emit(result, result.name)
            self._commChannel.sigCurrentResultChanged.emit(result)
            # NOTE: Special-case by ID retained because widefield-starss batch result
            # collection is plugin-specific and not part of the generic plugin API.
            if self._main._activeReconstructor.id == "widefield-starss":
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
