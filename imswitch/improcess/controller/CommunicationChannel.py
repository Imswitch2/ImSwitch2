from imswitch.imcommon.framework import Signal, SignalInterface


class CommunicationChannel(SignalInterface):
    """
    Communication Channel is a class that handles the communication between Master Controller
    and Widgets, or between Widgets.
    """

    sigDataFolderChanged = Signal(object)  # (dataFolderPath)

    sigSaveFolderChanged = Signal(object)  # (saveFolderPath)

    sigCurrentDataChanged = Signal(object)  # (dataObj)

    sigScanParamsUpdated = Signal(object, bool)  # (scanParDict, applyOnCurrentRecon)

    sigPatternUpdated = Signal(object)  # (pattern)

    sigPatternVisibilityChanged = Signal(bool)  # (visible)

    sigDetectionPreviewUpdated = Signal(object, object)  # (x, y) scatter arrays

    sigDetectionPreviewVisibilityChanged = Signal(bool)  # (visible)

    sigDisplayedFrameChanged = Signal()  # emitted when the displayed frame changes

    sigStatusMessage = Signal(str)  # a transient line for the status bar (see ImProcessMainView.showStatusMessage)

    sigAddToMultiData = Signal(str, str)  # (path, datasetName)

    sigReconstruct = Signal(object, bool)

    sigExecutionFinished = Signal(object)

    sigCurrentResultChanged = Signal(object)  # (processingResult/reconObj or None)

    sigResultProduced = Signal(object, str)
    """Fires when a new processing result is ready for the viewer.

    Producers — the legacy MoNaLISA path, the plugin reconstruction path, and
    any future processor-chain runner — emit ``(result, displayName)`` and
    forget. ``ReconstructionViewController`` is the canonical listener and
    folds the result into the reconstruction list, so producers no longer
    need to know which widget owns the napari layer list.
    """

    sigLiveResultUpdated = Signal(object)
    """Fires when a live reconstruction session has a new partial result.

    Emitted by LiveReconstructionController during streaming reconstruction
    to update the viewer with intermediate results. The signal carries a
    ProcessingResult snapshot.
    """

    sigResultsChanged = Signal()
    """Fires when the set of loaded results, or which of them are selected,
    changes.

    Companion to the pull accessors below: ``sigCurrentResultChanged`` only
    ever describes *one* result, so a panel offering an operation over several
    reconstruction objects has nothing to refresh on when a result is added,
    removed, or ctrl-clicked into the selection.
    """

    sigSmlmRenderSettingsChanged = Signal(object, object, object)
    """(result, gaussianOverrides, renderRange) — how to draw a point cloud.

    Emitted by the render-controls panel and applied by the reconstruction
    viewer's controller, which owns the renderer. Routed through the channel so
    the panel needs no reference to the viewer, matching how results are
    published in the other direction.
    """

    sigSmlmRenderAppearanceChanged = Signal(object, object)
    """(result, appearance) — colormap and opacity for a point cloud.

    Separate from the settings signal because appearance rebuilds no geometry
    on napari-storm's side, so dragging an opacity slider must not replan.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__resultProvider = None

    def setResultProvider(self, provider) -> None:
        """Register who can enumerate the loaded/selected results.

        The reconstruction list widget stays the single source of truth —
        this only lets any widget reach it through the channel instead of
        needing a direct reference to ``ReconstructionViewController``, which
        is what previously limited multi-result operations to one controller.
        A provider implements ``getAllResults()`` and ``getSelectedResults()``,
        each returning ``(displayName, result)`` pairs.
        """
        self.__resultProvider = provider

    def getAllResults(self) -> list:
        """Every loaded result as ``(displayName, result)``; empty if unknown."""
        return self.__queryProvider("getAllResults")

    def getSelectedResults(self) -> list:
        """The selected results as ``(displayName, result)``; empty if unknown."""
        return self.__queryProvider("getSelectedResults")

    def __queryProvider(self, method: str) -> list:
        provider = self.__resultProvider
        getter = getattr(provider, method, None) if provider is not None else None
        if not callable(getter):
            return []
        return list(getter())


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
