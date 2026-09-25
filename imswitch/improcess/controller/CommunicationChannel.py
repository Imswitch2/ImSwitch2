from imswitch.imcommon.framework import Signal, SignalInterface


class CommunicationChannel(SignalInterface):
    """
    Communication Channel is a class that handles the communication between Master Controller
    and Widgets, or between Widgets.

    Every signal below carries an attribute docstring ending in ``Emitters:``
    and ``Listeners:`` lines naming the classes that ``emit`` and ``connect``
    to it in production code (tests excluded). Those lists are not maintained
    by hand alone -- ``_test/test_communication_channel_docs.py`` re-derives
    them from the AST of ``improcess`` and fails if a docstring drifts, if a
    signal is undocumented, or if a signal loses all emitters or listeners.
    """

    sigDataFolderChanged = Signal(object)
    """The data (input) folder selection changed. Payload: ``(dataFolderPath,)``.

    Emitted when the user picks a new folder to browse raw recordings from.
    ``MultiDataFrameController`` uses it as the default location for its
    add-data dialog; ``ImProcessMainViewController`` forwards it back to
    ``FileIOController.dataFolderChanged`` to persist the choice.

    Emitters: FileIOController
    Listeners: ImProcessMainViewController, MultiDataFrameController
    """

    sigSaveFolderChanged = Signal(object)
    """The save (output) folder selection changed. Payload: ``(saveFolderPath,)``.

    Counterpart to :attr:`sigDataFolderChanged` for the destination folder.
    ``ImProcessMainViewController`` forwards it to
    ``FileIOController.saveFolderChanged``.

    Emitters: FileIOController
    Listeners: ImProcessMainViewController
    """

    sigCurrentDataChanged = Signal(object)
    """The active raw dataset changed. Payload: ``(dataObj,)``.

    Emitted by ``FileIOController`` after a load completes, and by
    ``MultiDataFrameController`` when the user promotes a different entry to
    "current". Listeners refresh their view of the raw data:
    ``DataFrameController`` redraws the frame view, ``MetadataController``
    repopulates the metadata panel, and ``MultiDataFrameController`` updates
    which row is highlighted.

    Emitters: FileIOController, MultiDataFrameController
    Listeners: DataFrameController, ImProcessMainViewController,
        MetadataController, MultiDataFrameController
    """

    sigScanParamsUpdated = Signal(object, bool)
    """Scan geometry changed. Payload: ``(scanParDict, applyOnCurrentRecon)``.

    Emitted by ``ScanParamsController`` when the scan-params dialog is applied
    and by ``MoNaLISAController`` when its own grid changes; both deep-copy the
    dict before emitting, so listeners may retain it.
    ``ReconstructionViewController`` re-scales the displayed result, and
    ``ImProcessMainViewController`` forwards to
    ``MoNaLISAController.scanParamsUpdated``. ``applyOnCurrentRecon`` asks
    listeners to re-apply the geometry to the *current* reconstruction rather
    than only to subsequent ones.

    Emitters: MoNaLISAController, ScanParamsController
    Listeners: ImProcessMainViewController, ReconstructionViewController,
        ScanParamsController
    """

    sigPatternUpdated = Signal(object)
    """The illumination-pattern overlay changed. Payload: ``(pattern,)``.

    ``DataFrameController`` redraws the pattern overlay on the raw frame view.

    Emitters: MoNaLISAController
    Listeners: DataFrameController
    """

    sigStatusMessage = Signal(str)  # a transient line for the status bar (see ImProcessMainView.showStatusMessage)

    sigAddToMultiData = Signal(str, str)  # (path, datasetName)
    sigPatternVisibilityChanged = Signal(bool)
    """The pattern overlay was shown or hidden. Payload: ``(visible,)``.

    Emitters: MoNaLISAController
    Listeners: DataFrameController
    """

    sigDetectionPreviewUpdated = Signal(object, object)
    """New focus-detection scatter points. Payload: ``(x, y)`` arrays.

    ``ReconstructorManagerController`` emits the detected focus centres so
    ``DataFrameController`` can scatter them over the raw frame as a preview
    of what the reconstruction will use.

    Emitters: ReconstructorManagerController
    Listeners: DataFrameController
    """

    sigDetectionPreviewVisibilityChanged = Signal(bool)
    """The detection-preview scatter was shown or hidden. Payload: ``(visible,)``.

    Emitters: ReconstructorManagerController
    Listeners: DataFrameController
    """

    sigDisplayedFrameChanged = Signal()
    """The raw frame currently on screen changed. No payload.

    Emitted by ``DataFrameController`` when the user scrubs to a different
    frame, so ``ReconstructorManagerController`` can recompute its detection
    preview against the frame now being displayed.

    Emitters: DataFrameController
    Listeners: ReconstructorManagerController
    """

    sigCurrentResultChanged = Signal(object)
    """Which result is selected in the viewer. Payload: ``(result or None,)``.

    ``ReconstructionViewController`` is the source of truth and emits on
    selection changes (``None`` when the selection is cleared); the producing
    controllers emit it to auto-select a result they just made, and
    ``ImProcessMainController`` re-broadcasts it on behalf of result-producing
    panels. Listeners re-target whatever they compute from the selection:
    ``GraphController`` re-plots, ``ResultProcessorController`` and
    ``ImageToolbarController`` retarget their controls, and
    ``ImProcessMainController`` forwards to any dock exposing
    ``setCurrentResult``.

    Distinct from :attr:`sigResultProduced`: that announces a *new* result
    exists, this announces which one the user is looking at.

    Emitters: ImProcessMainController, ImageToolbarController,
        ReconstructionViewController, ReconstructorManagerController,
        ResultProcessorController
    Listeners: GraphController, ImProcessMainController, ImageToolbarController,
        ResultProcessorController, SmlmRenderController
    """

    sigResultProduced = Signal(object, str)
    """Fires when a new processing result is ready for the viewer.

    Producers — the legacy MoNaLISA path, the plugin reconstruction path, and
    any future processor-chain runner — emit ``(result, displayName)`` and
    forget. ``ReconstructionViewController`` is the canonical listener and
    folds the result into the reconstruction list, so producers no longer
    need to know which widget owns the napari layer list.

    Result-producing *panels* (e.g. ``MulticolorWidget``) do not touch this
    channel directly: they publish on a widget-local signal of the same name,
    which ``ImProcessMainController._wire_producing_panel`` bridges onto this
    one. That is why ``ImProcessMainController`` appears on both lists below.

    Emitters: ImProcessMainController, ImageToolbarController,
        LiveReconstructionController, MemoryLiveController, MoNaLISAController,
        ReconstructorManagerController, ResultProcessorController
    Listeners: ImProcessMainController, ReconstructionViewController
    """

    sigLiveResultUpdated = Signal(object)
    """Fires when a live reconstruction session has a new partial result.

    Emitted by LiveReconstructionController during streaming reconstruction
    to update the viewer with intermediate results. The signal carries a
    ProcessingResult snapshot.

    Emission is throttled to the configured viewer update interval
    (``liveViewerUpdateIntervalS``), which in the GPU path also governs the
    device-to-host transfer cadence.

    Emitters: LiveReconstructionController
    Listeners: ImProcessMainController, ReconstructionViewController
    """

    sigSaveLiveResult = Signal(str, str)
    """Write a finished live run's data object to disk. Payload: ``(name, path)``.

    Emitted when a timelapse finishes or is skipped and the watcher panel's save
    toggle is on. ``name`` identifies which data object to write, so a run whose
    entry has since been replaced or deleted is skipped rather than saving the
    wrong one; ``path`` is the fully-resolved destination, directories already
    created.

    The viewer owns the accumulated buffer (the session hands over copies), so
    it -- not the reconstruction controller -- is what can actually write it.

    Emitters: LiveModeController
    Listeners: ReconstructionViewController
    """

    sigLiveTimepointUpdated = Signal(int, object)
    """One reconstructed timepoint plane. Payload: ``(timepoint_index, plane)``.

    The incremental counterpart to :attr:`sigLiveResultUpdated`. The first
    update of a run arrives as a whole result on that signal, giving the viewer
    a buffer of its own; every refresh after it arrives here as a single
    timepoint slice to write into that buffer. The plane is a copy, so the
    process thread never shares memory with the viewer -- but it is one plane
    rather than the whole volume, so the per-refresh cost does not grow as the
    timelapse lengthens.

    ``plane`` keeps all of the result's axes with the timepoint axis at length
    1, so it assigns straight into the matching slice.

    Emitters: LiveReconstructionController
    Listeners: ReconstructionViewController
    """

    sigLiveTimepointDone = Signal(int)
    """0-based index of a scan stack (timepoint) that just finished streaming.

    Emitted by LiveReconstructionController so the viewer can advance its
    timepoint slider to the newest reconstructed stack.

    Emitters: LiveReconstructionController
    Listeners: ReconstructionViewController
    """

    sigResultsChanged = Signal()
    """Fires when the set of loaded results, or which of them are selected,
    changes.

    Companion to the pull accessors below: ``sigCurrentResultChanged`` only
    ever describes *one* result, so a panel offering an operation over several
    reconstruction objects has nothing to refresh on when a result is added,
    removed, or ctrl-clicked into the selection.

    Emitters: ReconstructionViewController
    Listeners: ImProcessMainController, ResultProcessorController
    """

    sigSmlmRenderSettingsChanged = Signal(object, object, object)
    """(result, gaussianOverrides, renderRange) — how to draw a point cloud.

    Emitted by the render-controls panel and applied by the reconstruction
    viewer's controller, which owns the renderer. Routed through the channel so
    the panel needs no reference to the viewer, matching how results are
    published in the other direction.

    Emitters: SmlmRenderController
    Listeners: ReconstructionViewController
    """

    sigSmlmRenderAppearanceChanged = Signal(object, object)
    """(result, appearance) — colormap and opacity for a point cloud.

    Separate from the settings signal because appearance rebuilds no geometry
    on napari-storm's side, so dragging an opacity slider must not replan.

    Emitters: SmlmRenderController
    Listeners: ReconstructionViewController
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
