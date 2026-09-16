"""Controller for the always-present ImProcess image toolbar."""

from __future__ import annotations

import numpy as np

from imswitch.imcommon.model import initLogger
from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.contrast import auto_levels, finite_range, histogram
from imswitch.improcess.model.result import result_kind
from imswitch.improcess.processors.base import normalize_processor_output
from imswitch.improcess.processors.channel_merge import ChannelMergeProcessor
from imswitch.improcess.processors.channel_split import ChannelSplitProcessor
from imswitch.improcess.processors.combine import StackCombineProcessor
from imswitch.improcess.processors.image_calculator import ImageCalculatorProcessor
from imswitch.improcess.processors.make_composite import MakeCompositeProcessor
from imswitch.improcess.processors.make_rgb import MakeRGBProcessor
from imswitch.improcess.processors.projection.processor import ProjectionProcessor
from imswitch.improcess.processors.stack_split import StackSplitProcessor
from imswitch.improcess.processors.stack_subset import StackSubsetProcessor
from imswitch.improcess.processors._axis_split import resolve_axis, shape_for_result
from imswitch.improcess.view.ContrastBrightnessDialog import ContrastBrightnessDialog
from imswitch.improcess.view.ChannelControlsDialog import ChannelControlsDialog
from imswitch.improcess.view.ChannelPickerDialog import ChannelPickerDialog
from imswitch.improcess.view.bulk_confirm import confirm_bulk_publish
from imswitch.improcess.view.ImageCalculatorDialog import ImageCalculatorDialog
from imswitch.improcess.view.MergeChannelsDialog import MergeChannelsDialog
from imswitch.improcess.view.StackCombineDialog import StackCombineDialog
from imswitch.improcess.view.StackSubsetDialog import StackSubsetDialog


_RGB_CHANNEL_LABELS = ("C", "Channel", "Channels", "Base")


def _projection_applies(result) -> bool:
    # A 2-D image passes the projection processor's own ndim >= 2 gate, but
    # projecting it yields a line — not what the toolbar button means.
    return (
        getattr(getattr(result, "data", None), "ndim", 0) > 2
        and ProjectionProcessor().accepts(result)
    )


#: Actions that run one processor per result, and the gate deciding whether a
#: given result is a valid input. One table so the enablement rule for the
#: active result and the filter applied to a multi-result selection can never
#: drift apart.
_SINGLE_RESULT_ACTIONS = {
    "duplicate": lambda result: True,
    "max-projection": _projection_applies,
    "crop-substack": lambda result: StackSubsetProcessor().accepts(result),
    "split-stack": lambda result: StackSplitProcessor().accepts(result),
    "split-channels": lambda result: ChannelSplitProcessor().accepts(result),
    "make-composite": lambda result: MakeCompositeProcessor().accepts(result),
    "make-rgb": lambda result: MakeRGBProcessor().accepts(result),
}


class ImageToolbarController:
    """Wire image toolbar actions to the active reconstruction viewer state."""

    def __init__(self, commChannel, mainView, reconstructionController):
        self._commChannel = commChannel
        self._view = mainView
        self._reconstructionController = reconstructionController
        self._logger = initLogger(self, tryInheritParent=False)
        self._contrastDialog = None
        self._channelDialog = None

        mainView.sigImageAutoContrastRequested.connect(self.autoContrast)
        mainView.sigImageResetContrastRequested.connect(self.resetContrast)
        mainView.sigImageContrastDialogRequested.connect(self.openContrastDialog)
        mainView.sigImageChannelControlsRequested.connect(self.openChannelControls)
        mainView.sigImageLutChanged.connect(self.setLut)
        mainView.sigImageResetViewRequested.connect(self.resetView)
        mainView.sigImageDuplicateRequested.connect(self.duplicateResult)
        mainView.sigImageCropSubstackRequested.connect(self.cropSubstack)
        mainView.sigImageMaxProjectionRequested.connect(self.maxProjection)
        mainView.sigImageSplitStackRequested.connect(self.splitStack)
        mainView.sigImageSplitChannelsRequested.connect(self.splitChannels)
        mainView.sigImageMergeChannelsRequested.connect(self.mergeChannels)
        mainView.sigImageStackCombineRequested.connect(self.stackCombine)
        mainView.sigImageCalculatorRequested.connect(self.imageCalculator)
        mainView.sigImageMakeCompositeRequested.connect(self.makeComposite)
        mainView.sigImageMakeRgbRequested.connect(self.makeRgb)
        commChannel.sigCurrentResultChanged.connect(self.currentResultChanged)
        # Multi-input actions gate on the reconstruction-list selection, which
        # can change without the current item moving (Ctrl+A, Ctrl-click
        # deselect) — so track it separately from sigCurrentResultChanged.
        reconWidget = getattr(mainView, "reconstructionWidget", None)
        if reconWidget is not None and hasattr(reconWidget, "sigSelectionChanged"):
            reconWidget.sigSelectionChanged.connect(self.selectionChanged)
        self.currentResultChanged(reconstructionController.getActiveResult())

    def currentResultChanged(self, result) -> None:
        has_image = self._resultHasImage(result)
        self._view.setImageActionsEnabled(has_image)
        if hasattr(self._view, "setImageActionEnabled"):
            self._view.setImageActionEnabled(
                "channels",
                has_image and bool(self._displayLayerStates()),
            )
            for action_id in _SINGLE_RESULT_ACTIONS:
                self._view.setImageActionEnabled(
                    action_id, self._appliesTo(action_id, result)
                )
            self._updateMultiInputActions()
            self._view.setImageActionEnabled(
                "image-calculator",
                has_image,
            )
        if hasattr(self._view, "setImageLutEnabled"):
            self._view.setImageLutEnabled(has_image)
        if has_image and hasattr(self._view, "setImageLutValue"):
            try:
                self._view.setImageLutValue(
                    self._reconstructionController.getActiveImageColormap()
                )
            except Exception:
                self._logger.debug("Could not sync image LUT selector", exc_info=True)
        self._refreshChannelDialog()

    def selectionChanged(self) -> None:
        self._updateMultiInputActions()

    def _updateMultiInputActions(self) -> None:
        """Enable merge-channels / stack-combine from what is *loaded*.

        Deliberately independent of both the current item and the selection:
        these actions open a picker over every loaded result, and gating them
        on a compatible multi-selection left the buttons dead by default with
        nothing on screen explaining why. The dialogs report incompatibility
        with a reason instead.
        """
        if not hasattr(self._view, "setImageActionEnabled"):
            return
        loaded = self._loadedImageResults()
        self._view.setImageActionEnabled("merge-channels", len(loaded) >= 2)
        self._view.setImageActionEnabled("stack-combine", len(loaded) >= 2)

    def autoContrast(self, saturated_percent: float = 0.35) -> None:
        data = self._activeImageForScope(self._dialogScope())
        if data is None:
            return
        levels = auto_levels(data, saturated_percent=saturated_percent)
        self._setLevels(levels)

    def resetContrast(self) -> None:
        data = self._activeImageForScope(self._dialogScope())
        if data is None:
            return
        levels = finite_range(data)
        self._reconstructionController.setActiveImageDisplayLevelsRange(*levels)
        self._setLevels(levels)

    def openContrastDialog(self) -> None:
        if not self._resultHasImage(self._reconstructionController.getActiveResult()):
            return

        dialog = self._contrastDialog
        if dialog is None:
            dialog = ContrastBrightnessDialog(self._view)
            dialog.sigLevelsChanged.connect(
                lambda minimum, maximum: self._setLevels(
                    (minimum, maximum), update_dialog=False
                )
            )
            dialog.sigAutoRequested.connect(self._autoFromDialog)
            dialog.sigResetRequested.connect(self.resetContrast)
            dialog.scopeCombo.currentTextChanged.connect(
                lambda _text: self._refreshContrastDialog(update_levels=False)
            )
            dialog.finished.connect(lambda _result: self._clearContrastDialog())
            self._contrastDialog = dialog

        self._refreshContrastDialog(update_levels=True)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def openChannelControls(self) -> None:
        states = self._displayLayerStates()
        if not states:
            return

        dialog = self._channelDialog
        if dialog is None:
            dialog = ChannelControlsDialog(self._view)
            dialog.sigLayerVisibilityChanged.connect(self.setChannelLayerVisible)
            dialog.sigLayerLutChanged.connect(self.setChannelLayerLut)
            dialog.finished.connect(lambda _result: self._clearChannelDialog())
            self._channelDialog = dialog

        dialog.setLayerStates(states)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def setChannelLayerVisible(self, layer_id: str, visible: bool) -> None:
        try:
            self._reconstructionController.setDisplayLayerVisible(layer_id, visible)
        except Exception:
            self._logger.exception("Could not set channel layer visibility")

    def setChannelLayerLut(self, layer_id: str, colormap: str) -> None:
        try:
            self._reconstructionController.setDisplayLayerColormap(layer_id, colormap)
        except Exception:
            self._logger.exception("Could not set channel layer LUT")

    def resetView(self) -> None:
        try:
            self._view.reconstructionWidget.resetView()
        except Exception:
            self._logger.exception("Could not reset reconstruction view")

    def setLut(self, colormap: str) -> None:
        if not self._resultHasImage(self._reconstructionController.getActiveResult()):
            return
        try:
            self._reconstructionController.setActiveImageColormap(str(colormap))
        except Exception:
            self._logger.exception("Could not set active image LUT")

    def duplicateResult(self) -> None:
        duplicates = []
        for result in self._selectedProcessingResults():
            try:
                duplicates.append(ArrayProcessingResult.duplicate(result))
            except Exception as exc:
                self._logger.exception("Could not duplicate result")
                self._showMessage(f"Could not duplicate: {exc}")
        if duplicates:
            self._publishResults(duplicates)

    def cropSubstack(self) -> None:
        result = self._reconstructionController.getActiveResult()
        if not self._resultHasImage(result):
            return
        try:
            napari_viewer = getattr(
                getattr(self._view, "reconstructionWidget", None), "napariViewer", None
            )
            params = StackSubsetDialog.get_params(
                result,
                parent=self._view,
                napari_viewer=napari_viewer,
                rois=self._croppableROIs(),
            )
        except Exception:
            self._logger.exception("Could not collect crop/substack parameters")
            return
        if params is None:
            return
        self._runProcessor(StackSubsetProcessor(), result, params)

    def _croppableROIs(self) -> list:
        """Visible ROIs from the ROI manager, if it is open.

        Only the ones that have a rectangle to offer: a crop is rectangular,
        and a line or a point has no extent to crop to. Empty when the panel
        is not open, which leaves the dialog exactly as it was.
        """
        panel = getattr(self._view, "roiManagerWidget", None)
        if panel is None:
            return []
        try:
            from imswitch.imcommon.algorithms.roi_geometry import roi_capabilities

            return [
                roi
                for roi in panel.rois(visible_only=True)
                if roi_capabilities(roi.roi_type).is_area
            ]
        except Exception:
            self._logger.debug("Could not read ROIs for cropping", exc_info=True)
            return []

    def maxProjection(self) -> None:
        self._runProcessorOverTargets(
            ProjectionProcessor(), {"axis": "Auto", "mode": "max"}, "max-projection"
        )

    def splitStack(self) -> None:
        self._runProcessorOverTargets(StackSplitProcessor(), {"axis": "Auto"}, "split-stack")

    def splitChannels(self) -> None:
        self._runProcessorOverTargets(ChannelSplitProcessor(), {"axis": "Auto"}, "split-channels")

    def mergeChannels(self) -> None:
        loaded = self._loadedImageResults()
        if len(loaded) < 2:
            return
        params = MergeChannelsDialog.get_params(
            loaded, parent=self._view, preselected=self._multiSelection()
        )
        if params is None:
            return
        try:
            processor = ChannelMergeProcessor()
            output = processor.apply(params["results"][0], params)
            results = normalize_processor_output(
                output, params["results"][0], processor, params, params["results"]
            )
        except Exception as exc:
            self._logger.exception("Could not merge the chosen channel results")
            self._showMessage(f"Could not merge channels: {exc}")
            return
        if params.get("composite"):
            results = self._asComposites(results)
        self._publishResults(results)

    def _asComposites(self, results):
        """Render merged channel stacks as coloured layers.

        The composite wraps the same data array, so it replaces the plain
        stack rather than being published beside it — one merge, one entry in
        the reconstruction list. A result that cannot become a composite is
        kept as it is.
        """
        composites = []
        for result in results:
            try:
                composite = MakeCompositeProcessor().apply(result, {"axis": "Auto"})
            except Exception as exc:
                self._logger.exception("Could not make a composite of the merge")
                self._showMessage(f"Merged, but could not make a composite: {exc}")
                composites.append(result)
                continue
            composites.append(composite)
        return composites

    def stackCombine(self) -> None:
        loaded = self._loadedImageResults()
        if len(loaded) < 2:
            return
        params = StackCombineDialog.get_params(
            loaded, parent=self._view, preselected=self._multiSelection()
        )
        if params is None:
            return
        try:
            processor = StackCombineProcessor()
            output = processor.apply(params["results"][0], params)
            results = normalize_processor_output(
                output, params["results"][0], processor, params, params["results"]
            )
        except Exception as exc:
            self._logger.exception("Could not stack/combine the chosen results")
            self._showMessage(f"Could not stack/combine: {exc}")
            return
        self._publishResults(results)

    def imageCalculator(self) -> None:
        # ImageJ-style: pick any two loaded results, not just the selection.
        loaded = self._loadedImageResults()
        if not loaded:
            return
        params = ImageCalculatorDialog.get_params(
            loaded,
            parent=self._view,
            active_result=self._reconstructionController.getActiveResult(),
        )
        if params is None:
            return
        try:
            processor = ImageCalculatorProcessor()
            output = processor.apply(params["results"][0], params)
            results = normalize_processor_output(
                output, params["results"][0], processor, params, params["results"]
            )
        except Exception as exc:
            self._logger.exception("Could not run the image calculator")
            self._showMessage(f"Could not run the image calculator: {exc}")
            return
        self._publishResults(results)

    def makeComposite(self) -> None:
        self._runProcessorOverTargets(
            MakeCompositeProcessor(), {"axis": "Auto"}, "make-composite"
        )

    def makeRgb(self) -> None:
        result = self._reconstructionController.getActiveResult()
        if not self._resultHasImage(result):
            return
        try:
            axis = resolve_axis(
                result,
                "Auto",
                preferred_labels=_RGB_CHANNEL_LABELS,
                require_label_match=True,
            )
        except Exception:
            self._logger.exception("Could not resolve RGB channel axis")
            return

        channel_count = int(shape_for_result(result)[axis])
        params = {"axis": "Auto"}
        if channel_count > 3:
            channels = ChannelPickerDialog.get_channels(result, axis, parent=self._view)
            if channels is None:
                return
            params["channels"] = channels
        else:
            channels = list(range(channel_count))

        channel_levels = self._channelLevelsForAxis(result, axis, channels)
        if channel_levels is not None:
            params["channel_levels"] = channel_levels

        self._runProcessor(MakeRGBProcessor(), result, params)

    def _channelLevelsForAxis(self, result, axis: int, channels: list) -> list | None:
        display_layers = result.display_layers() if hasattr(result, "display_layers") else []
        if not display_layers:
            return None
        if hasattr(result, "applyDisplayLayerSettings"):
            display_layers = result.applyDisplayLayerSettings(display_layers)

        levels_by_channel = {}
        for layer in display_layers:
            metadata = layer.metadata or {}
            if metadata.get("channel_axis") != axis:
                continue
            channel_index = metadata.get("channel_index")
            if channel_index is not None and layer.display_levels is not None:
                levels_by_channel[int(channel_index)] = layer.display_levels

        if not levels_by_channel:
            return None
        return [levels_by_channel.get(index) for index in channels]

    def _runProcessor(self, processor, result, params: dict) -> None:
        try:
            output = processor.apply(result, params)
            results = normalize_processor_output(output, result, processor, params)
        except Exception as exc:
            self._logger.exception(
                "Could not run image toolbar processor %s",
                getattr(processor, "id", type(processor).__name__),
            )
            self._showMessage(f"Could not run {processor.name}: {exc}")
            return
        self._publishResults(results)

    def _runProcessorOverTargets(self, processor, params: dict, action_id: str) -> None:
        """Run a parameterless op on every selected result, then publish once.

        Inputs the action does not apply to are skipped rather than
        cancelling the sweep — a selection routinely mixes results of
        different rank, and dropping the whole run because one of them has no
        stack axis would make the selection unusable.
        """
        targets = [
            result
            for result in self._selectedProcessingResults()
            if self._appliesTo(action_id, result)
        ]
        if not targets:
            return
        results = []
        failures = []
        for target in targets:
            try:
                results.extend(
                    normalize_processor_output(
                        processor.apply(target, params), target, processor, params
                    )
                )
            except Exception as exc:
                self._logger.exception(
                    "Could not run image toolbar processor %s on %s",
                    getattr(processor, "id", type(processor).__name__),
                    getattr(target, "name", "result"),
                )
                failures.append((target, str(exc)))
        if failures:
            name = getattr(failures[0][0], "name", "one result")
            self._showMessage(
                f"{processor.name} failed on {len(failures)} of {len(targets)} "
                f"results — '{name}': {failures[0][1]}"
            )
        if results:
            self._publishResults(results)

    def _appliesTo(self, action_id: str, result) -> bool:
        """Whether one image-toolbar action can run on ``result``."""
        if not self._resultHasImage(result):
            return False
        gate = _SINGLE_RESULT_ACTIONS.get(action_id)
        if gate is None:
            return True
        try:
            return bool(gate(result))
        except Exception:
            self._logger.debug(
                "Compatibility check failed for %s", action_id, exc_info=True
            )
            return False

    def _showMessage(self, message: str) -> None:
        """Surface an operation failure where the user is actually looking."""
        show = getattr(self._view, "showStatusMessage", None)
        if callable(show):
            show(message)

    def _autoFromDialog(self, saturated_percent: float) -> None:
        data = self._activeImageForScope(self._dialogScope())
        if data is None:
            return
        levels = auto_levels(data, saturated_percent=saturated_percent)
        self._setLevels(levels)

    def _setLevels(self, levels: tuple[float, float], *, update_dialog: bool = True) -> None:
        minimum, maximum = float(levels[0]), float(levels[1])
        self._reconstructionController.setActiveImageDisplayLevels(minimum, maximum)
        if update_dialog:
            dialog = self._contrastDialog
            if dialog is not None:
                dialog.setLevels(minimum, maximum)

    def _refreshContrastDialog(self, *, update_levels: bool) -> None:
        dialog = self._contrastDialog
        if dialog is None:
            return
        data = self._activeImageForScope(dialog.histogramScope())
        if data is None:
            return
        data_range = finite_range(data)
        counts, edges = histogram(data, value_range=data_range)
        dialog.setDataRange(*data_range)
        dialog.setHistogram(counts, edges)
        if update_levels:
            levels = self._currentLevels(data_range)
            dialog.setLevels(*levels)

    def _currentLevels(self, fallback: tuple[float, float]) -> tuple[float, float]:
        try:
            levels = self._reconstructionController.getActiveImageDisplayLevels()
        except Exception:
            return fallback
        if levels is None or len(levels) != 2:
            return fallback
        if not np.isfinite(levels[0]) or not np.isfinite(levels[1]):
            return fallback
        return float(levels[0]), float(levels[1])

    def _dialogScope(self) -> str:
        dialog = self._contrastDialog
        if dialog is None:
            return "stack"
        return dialog.histogramScope()

    def _activeImageForScope(self, scope: str):
        try:
            if scope == "view":
                return self._reconstructionController.getActiveImageCurrentView()
            return self._reconstructionController.getActiveImage()
        except Exception:
            self._logger.exception("Could not read active image for toolbar action")
            return None

    def _clearContrastDialog(self) -> None:
        self._contrastDialog = None

    def _clearChannelDialog(self) -> None:
        self._channelDialog = None

    def _refreshChannelDialog(self) -> None:
        dialog = self._channelDialog
        if dialog is not None:
            dialog.setLayerStates(self._displayLayerStates())

    def _displayLayerStates(self) -> list[dict]:
        if hasattr(self._reconstructionController, "getDisplayLayerStates"):
            return list(self._reconstructionController.getDisplayLayerStates())
        return []

    def _publishResult(self, result) -> None:
        self._publishResults((result,))

    def _publishResults(self, results) -> None:
        results = list(results)
        # Safety valve: Split stack / Make composite on the wrong axis can
        # mean hundreds of napari layers from one click — ask first.
        if not confirm_bulk_publish(self._view, results):
            return
        last_result = None
        for result in results:
            display_name = getattr(result, "name", "") or "Image result"
            self._commChannel.sigResultProduced.emit(result, display_name)
            last_result = result
        if last_result is not None:
            self._commChannel.sigCurrentResultChanged.emit(last_result)

    @staticmethod
    def _resultHasImage(result) -> bool:
        data = getattr(result, "data", None)
        if data is None or getattr(data, "ndim", 0) < 2:
            return False
        # Metric tables and curves carry 2D data arrays but are not images;
        # duplicate/contrast/LUT/stack actions must not be offered on them.
        return result_kind(result) not in ("table", "curve")

    def _selectedProcessingResults(self):
        """Selected image results, falling back to the active one.

        This is what a single-result operation runs on: selecting nothing is
        the ordinary case and must still act on what is on screen.
        """
        if hasattr(self._reconstructionController, "getSelectedResults"):
            selected = [
                result for _name, result in self._reconstructionController.getSelectedResults()
                if self._resultHasImage(result)
            ]
            if selected:
                return selected
        active = self._reconstructionController.getActiveResult()
        return [active] if self._resultHasImage(active) else []

    def _multiSelection(self):
        """A deliberate multi-selection, or ``None`` to pre-check everything.

        The reconstruction list always has its current item selected, so
        seeding a two-input picker from a one-item selection would open every
        dialog with one input ticked and OK already disabled — an operation
        that plainly should work on the two results in front of you.
        """
        selected = self._selectedProcessingResults()
        return selected if len(selected) >= 2 else None

    def _loadedImageResults(self):
        """Every loaded image result — the pool the multi-input pickers offer."""
        try:
            loaded = list(self._reconstructionController.getAllResults())
        except Exception:
            self._logger.exception("Could not enumerate the loaded results")
            return []
        return [result for _name, result in loaded if self._resultHasImage(result)]


__all__ = ["ImageToolbarController"]
