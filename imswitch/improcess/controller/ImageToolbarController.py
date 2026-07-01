"""Controller for the always-present ImProcess image toolbar."""

from __future__ import annotations

import numpy as np

from imswitch.imcommon.model import initLogger
from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.contrast import auto_levels, finite_range, histogram
from imswitch.improcess.processors.base import normalize_processor_output
from imswitch.improcess.processors.channel_split import ChannelSplitProcessor
from imswitch.improcess.processors.projection.processor import ProjectionProcessor
from imswitch.improcess.processors.stack_split import StackSplitProcessor
from imswitch.improcess.processors.stack_subset import StackSubsetProcessor
from imswitch.improcess.view.ContrastBrightnessDialog import ContrastBrightnessDialog
from imswitch.improcess.view.StackSubsetDialog import StackSubsetDialog


class ImageToolbarController:
    """Wire image toolbar actions to the active reconstruction viewer state."""

    def __init__(self, commChannel, mainView, reconstructionController):
        self._commChannel = commChannel
        self._view = mainView
        self._reconstructionController = reconstructionController
        self._logger = initLogger(self, tryInheritParent=False)
        self._contrastDialog = None

        mainView.sigImageAutoContrastRequested.connect(self.autoContrast)
        mainView.sigImageResetContrastRequested.connect(self.resetContrast)
        mainView.sigImageContrastDialogRequested.connect(self.openContrastDialog)
        mainView.sigImageResetViewRequested.connect(self.resetView)
        mainView.sigImageDuplicateRequested.connect(self.duplicateResult)
        mainView.sigImageCropSubstackRequested.connect(self.cropSubstack)
        mainView.sigImageMaxProjectionRequested.connect(self.maxProjection)
        mainView.sigImageSplitStackRequested.connect(self.splitStack)
        mainView.sigImageSplitChannelsRequested.connect(self.splitChannels)
        commChannel.sigCurrentResultChanged.connect(self.currentResultChanged)
        self.currentResultChanged(reconstructionController.getActiveResult())

    def currentResultChanged(self, result) -> None:
        has_image = self._resultHasImage(result)
        self._view.setImageActionsEnabled(has_image)
        if hasattr(self._view, "setImageActionEnabled"):
            self._view.setImageActionEnabled(
                "max-projection",
                has_image and getattr(getattr(result, "data", None), "ndim", 0) > 2,
            )
            self._view.setImageActionEnabled(
                "crop-substack",
                has_image and StackSubsetProcessor().applies_to(result),
            )
            self._view.setImageActionEnabled(
                "split-stack",
                has_image and StackSplitProcessor().applies_to(result),
            )
            self._view.setImageActionEnabled(
                "split-channels",
                has_image and ChannelSplitProcessor().applies_to(result),
            )

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

    def resetView(self) -> None:
        try:
            self._view.reconstructionWidget.resetView()
        except Exception:
            self._logger.exception("Could not reset reconstruction view")

    def duplicateResult(self) -> None:
        result = self._reconstructionController.getActiveResult()
        if not self._resultHasImage(result):
            return
        try:
            duplicate = ArrayProcessingResult.duplicate(result)
        except Exception:
            self._logger.exception("Could not duplicate active result")
            return
        self._publishResult(duplicate)

    def cropSubstack(self) -> None:
        result = self._reconstructionController.getActiveResult()
        if not self._resultHasImage(result):
            return
        try:
            params = StackSubsetDialog.get_params(result, parent=self._view)
        except Exception:
            self._logger.exception("Could not collect crop/substack parameters")
            return
        if params is None:
            return
        self._runProcessor(StackSubsetProcessor(), result, params)

    def maxProjection(self) -> None:
        result = self._reconstructionController.getActiveResult()
        if not self._resultHasImage(result):
            return
        self._runProcessor(ProjectionProcessor(), result, {"axis": "Auto", "mode": "max"})

    def splitStack(self) -> None:
        result = self._reconstructionController.getActiveResult()
        if not self._resultHasImage(result):
            return
        self._runProcessor(StackSplitProcessor(), result, {"axis": "Auto"})

    def splitChannels(self) -> None:
        result = self._reconstructionController.getActiveResult()
        if not self._resultHasImage(result):
            return
        self._runProcessor(ChannelSplitProcessor(), result, {"axis": "Auto"})

    def _runProcessor(self, processor, result, params: dict) -> None:
        try:
            output = processor.apply(result, params)
            results = normalize_processor_output(output)
        except Exception:
            self._logger.exception(
                "Could not run image toolbar processor %s",
                getattr(processor, "id", type(processor).__name__),
            )
            return
        self._publishResults(results)

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

    def _publishResult(self, result) -> None:
        self._publishResults((result,))

    def _publishResults(self, results) -> None:
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
        return data is not None and getattr(data, "ndim", 0) >= 2


__all__ = ["ImageToolbarController"]
