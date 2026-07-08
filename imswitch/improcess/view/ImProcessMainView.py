import numpy as np
import pyqtgraph as pg
from pyqtgraph.dockarea import Dock, DockArea
from pyqtgraph.parametertree import Parameter, ParameterTree
from qtpy import QtCore, QtWidgets

from imswitch.imcommon.model import initLogger
from imswitch.imcommon.view import PickDatasetsDialog
from imswitch.improcess.model.runtime_tools import (
    RuntimeAnalysisToolSpec,
    runtime_analysis_panel_shortcuts,
)
from imswitch.improcess.model.luts import IMAGE_LUTS
from imswitch.improcess.reconstructors.monalisa.gauss_processor import (
    DEFAULT_FOOTPRINT_NUM_RECTS,
    DEFAULT_GAUSSIAN_SIGMA_PX,
    DEFAULT_PINHOLE_RADIUS_SIGMA,
)
from .DataFrame import DataFrame
from .ColocalizationWidget import ColocalizationWidget
from .MultiDataFrame import MultiDataFrame
from .MulticolorWidget import MulticolorWidget
from .WatcherFrame import WatcherFrame
from .ReconstructionView import ReconstructionView
from .GraphWidget import GraphWidget
from .ProfileWidget import ProfileWidget
from .PSFResolutionWidget import PSFResolutionWidget
from .ROIManagerWidget import ROIManagerWidget
from .ROIStatsWidget import ROIStatsWidget
from .ResultProcessorWidget import ResultProcessorWidget
from .ResultsTableWidget import ResultsTableWidget
from .ScanParamsDialog import ScanParamsDialog
from .SegmentationWidget import SegmentationWidget
from .guitools import BetterPushButton
from .icons import improcessIcon


class ImProcessMainView(QtWidgets.QMainWindow):
    sigSaveReconstruction = QtCore.Signal()
    sigSaveReconstructionAll = QtCore.Signal()
    sigSaveCoeffs = QtCore.Signal()
    sigSaveCoeffsAll = QtCore.Signal()
    sigSetDataFolder = QtCore.Signal()
    sigSetSaveFolder = QtCore.Signal()

    sigReconstuctCurrent = QtCore.Signal()
    sigReconstructMultiConsolidated = QtCore.Signal()
    sigReconstructMultiIndividual = QtCore.Signal()
    sigQuickLoadData = QtCore.Signal()
    sigQuickLoadVirtualData = QtCore.Signal()
    sigUpdate = QtCore.Signal()

    sigShowPatternChanged = QtCore.Signal(bool)
    sigFindPattern = QtCore.Signal()
    sigShowScanParamsClicked = QtCore.Signal()
    sigPatternParamsChanged = QtCore.Signal()

    sigFilesDropped = QtCore.Signal(list)  # List of pathlib.Path objects

    # Emitted when the user picks a different reconstructor in the Parameters
    # dock header. Carries the plugin id (e.g. "view-only", "monalisa").
    sigActiveReconstructorChanged = QtCore.Signal(str)
    sigLoadProcessorRequested = QtCore.Signal(str)

    sigImageAutoContrastRequested = QtCore.Signal()
    sigImageResetContrastRequested = QtCore.Signal()
    sigImageContrastDialogRequested = QtCore.Signal()
    sigImageChannelControlsRequested = QtCore.Signal()
    sigImageLutChanged = QtCore.Signal(str)
    sigImageResetViewRequested = QtCore.Signal()
    sigImageDuplicateRequested = QtCore.Signal()
    sigImageCropSubstackRequested = QtCore.Signal()
    sigImageMaxProjectionRequested = QtCore.Signal()
    sigImageSplitStackRequested = QtCore.Signal()
    sigImageSplitChannelsRequested = QtCore.Signal()
    sigImageMergeChannelsRequested = QtCore.Signal()
    sigImageMakeCompositeRequested = QtCore.Signal()
    sigImageMakeRgbRequested = QtCore.Signal()

    sigClosing = QtCore.Signal()

    def __init__(
        self,
        showParameterPanel: bool = True,
        showNapariLayerControls: bool = True,
        showReconstructionPanel: bool = True,
        showActionsPanel: bool = True,
        showFileWatcherPanel: bool = True,
        showMultiDataPanel: bool = True,
        showCurrentDataPanel: bool = True,
        showResultsPanel: bool = True,
        showGraphPanel: bool = True,
        showProfilePanel: bool = True,
        showFRCPanel: bool = False,
        showROIStatsPanel: bool = False,
        showProjectionPanel: bool = False,
        showROIManagerPanel: bool = False,
        showSegmentationPanel: bool = False,
        showPSFResolutionPanel: bool = False,
        showColocalizationPanel: bool = False,
        showMulticolorPanel: bool = False,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.setWindowTitle('Image Processing')
        self.setAcceptDrops(True)
        self._logger = initLogger(self, tryInheritParent=False)

        # self parameters
        self.r_l_text = 'Right/Left'
        self.u_d_text = 'Up/Down'
        self.b_f_text = 'Back/Forth'
        self.timepoints_text = 'Timepoints'
        self.p_text = 'pos'
        self.n_text = 'neg'

        # Actions in menubar
        menuBar = self.menuBar()
        file = menuBar.addMenu('&File')

        self._fileActions: dict[str, QtWidgets.QAction] = {}
        self._fileToolbar = self.addToolBar('File tools')
        self._fileToolbar.setObjectName('ImProcessFileToolsToolbar')
        self._fileToolbar.setToolButtonStyle(QtCore.Qt.ToolButtonIconOnly)

        quickLoadAction = QtWidgets.QAction(
            improcessIcon('quick-load-data', self),
            'Quick load data…',
            self,
        )
        quickLoadAction.setShortcut('Ctrl+T')
        quickLoadAction.triggered.connect(self.sigQuickLoadData)
        quickLoadAction.setToolTip('Load data as current data')
        quickLoadAction.setStatusTip('Load data as current data')
        file.addAction(quickLoadAction)
        self._addFileToolAction('quick-load-data', quickLoadAction)

        quickLoadVirtualAction = QtWidgets.QAction(
            improcessIcon('quick-load-virtual-data', self),
            'Virtual load data…',
            self,
        )
        quickLoadVirtualAction.setShortcut('Ctrl+Shift+T')
        quickLoadVirtualAction.setToolTip('Open data as a lazy virtual stack')
        quickLoadVirtualAction.setStatusTip('Open data as a lazy virtual stack')
        quickLoadVirtualAction.triggered.connect(self.sigQuickLoadVirtualData)
        file.addAction(quickLoadVirtualAction)
        self._addFileToolAction('quick-load-virtual-data', quickLoadVirtualAction)

        file.addSeparator()

        saveReconAction = QtWidgets.QAction(
            improcessIcon('save-reconstruction', self),
            'Save reconstruction…',
            self,
        )
        saveReconAction.setShortcut('Ctrl+D')
        saveReconAction.triggered.connect(self.sigSaveReconstruction)
        saveReconAction.setToolTip('Save the active reconstruction')
        saveReconAction.setStatusTip('Save the active reconstruction')
        file.addAction(saveReconAction)
        self._addFileToolAction('save-reconstruction', saveReconAction)
        saveReconAllAction = QtWidgets.QAction('Save all reconstructions…', self)
        saveReconAllAction.setShortcut('Ctrl+Shift+D')
        saveReconAllAction.triggered.connect(self.sigSaveReconstructionAll)
        file.addAction(saveReconAllAction)
        saveCoeffsAction = QtWidgets.QAction('Save coefficients of reconstruction…', self)
        saveCoeffsAction.setShortcut('Ctrl+A')
        saveCoeffsAction.triggered.connect(self.sigSaveCoeffs)
        file.addAction(saveCoeffsAction)
        saveCoeffsAllAction = QtWidgets.QAction('Save all coefficients…', self)
        saveCoeffsAllAction.setShortcut('Ctrl+Shift+A')
        saveCoeffsAllAction.triggered.connect(self.sigSaveCoeffsAll)
        file.addAction(saveCoeffsAllAction)

        file.addSeparator()

        setDataFolder = QtWidgets.QAction('Set default data folder…', self)
        setDataFolder.triggered.connect(self.sigSetDataFolder)
        file.addAction(setDataFolder)

        setSaveFolder = QtWidgets.QAction('Set default save folder…', self)
        setSaveFolder.triggered.connect(self.sigSetSaveFolder)
        file.addAction(setSaveFolder)

        self._imageActions: dict[str, QtWidgets.QAction] = {}
        self._imageLutActions: dict[str, QtWidgets.QAction] = {}
        self._imageMenu = menuBar.addMenu('&Image')
        self._analysisMenu = menuBar.addMenu('&Analyze')
        self._imageToolbar = self.addToolBar('Image tools')
        self._imageToolbar.setObjectName('ImProcessImageToolsToolbar')
        self._imageToolbar.setToolButtonStyle(QtCore.Qt.ToolButtonIconOnly)
        self._buildImageToolbar()
        self.setImageActionsEnabled(False)

        self._analysisToolActions: dict[str, QtWidgets.QAction] = {}
        self._processorToolbar = self.addToolBar('Analysis tools')
        self._processorToolbar.setObjectName('ImProcessAnalysisToolsToolbar')
        self._processorToolbar.setToolButtonStyle(QtCore.Qt.ToolButtonIconOnly)
        self._loadProcessorCombo = QtWidgets.QComboBox()
        self._loadProcessorCombo.setMinimumContentsLength(18)
        self._loadProcessorCombo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)
        self._loadProcessorCombo.setToolTip('Load an available built-in analysis tool at runtime')
        self._processorToolbar.addWidget(QtWidgets.QLabel('Load tool: '))
        self._processorToolbar.addWidget(self._loadProcessorCombo)
        self._loadProcessorCombo.activated.connect(self._on_load_processor_combo_activated)
        self._loadedProcessorCombo = QtWidgets.QComboBox()
        self._loadedProcessorCombo.setMinimumContentsLength(18)
        self._loadedProcessorCombo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)
        self._loadedProcessorCombo.setToolTip('Processors currently registered in ImProcess')
        self._processorToolbar.addSeparator()
        self._processorToolbar.addWidget(QtWidgets.QLabel('Loaded processors: '))
        self._processorToolbar.addWidget(self._loadedProcessorCombo)
        self._processorToolbar.addSeparator()
        self._processorToolbar.addWidget(QtWidgets.QLabel('Panels: '))
        self._buildAnalysisToolShortcuts()

        self.dataFrame = DataFrame()
        self.multiDataFrame = MultiDataFrame()
        self.watcherFrame = WatcherFrame()

        btnFrame = BtnFrame()
        self._btnFrame = btnFrame
        btnFrame.sigReconstuctCurrent.connect(self.sigReconstuctCurrent)
        btnFrame.sigReconstructMultiConsolidated.connect(self.sigReconstructMultiConsolidated)
        btnFrame.sigReconstructMultiIndividual.connect(self.sigReconstructMultiIndividual)
        btnFrame.sigQuickLoadData.connect(self.sigQuickLoadData)
        btnFrame.sigUpdate.connect(self.sigUpdate)

        self.reconstructionWidget = ReconstructionView(
            showLayerControls=showNapariLayerControls
        )
        self.graphWidget = GraphWidget() if showGraphPanel else None
        self.profileWidget = (
            ProfileWidget(self.reconstructionWidget.napariViewer)
            if showProfilePanel
            else None
        )
        self.frcWidget = (
            self._makeResultProcessorWidget('frc')
            if showFRCPanel
            else None
        )
        self.roiStatsWidget = (
            ROIStatsWidget(self.reconstructionWidget.napariViewer)
            if showROIStatsPanel
            else None
        )
        self.roiManagerWidget = (
            ROIManagerWidget(self.reconstructionWidget.napariViewer)
            if showROIManagerPanel
            else None
        )
        self.projectionWidget = (
            self._makeResultProcessorWidget('projection')
            if showProjectionPanel
            else None
        )
        self.segmentationWidget = (
            SegmentationWidget(
                self.reconstructionWidget.napariViewer,
                roiManagerWidget=self.roiManagerWidget,
            )
            if showSegmentationPanel
            else None
        )
        self.psfResolutionWidget = (
            PSFResolutionWidget(
                self.reconstructionWidget.napariViewer,
                roiManagerWidget=self.roiManagerWidget,
            )
            if showPSFResolutionPanel
            else None
        )
        self.colocalizationWidget = (
            ColocalizationWidget(
                self.reconstructionWidget.napariViewer,
                roiManagerWidget=self.roiManagerWidget,
            )
            if showColocalizationPanel
            else None
        )
        self.multicolorWidget = (
            MulticolorWidget(self.reconstructionWidget.napariViewer)
            if showMulticolorPanel
            else None
        )
        self.resultsTableWidget = ResultsTableWidget(
            show_filter=True, show_csv=True, show_plot=self.graphWidget is not None
        )
        if self.graphWidget is not None:
            self.resultsTableWidget.sigPlotRequested.connect(self._onTablePlotRequested)

        self.parTree = ReconParTree()
        self.showPatBool = self.parTree.p.param('Show pattern')
        self.showPatBool.sigValueChanged.connect(lambda _, v: self.sigShowPatternChanged.emit(v))
        self.bleachBool = self.parTree.p.param('Bleaching correction')
        self.extension = self.parTree.p.param('File extension')
        self.findPatBtn = self.parTree.p.param('Pattern').param('Find pattern')
        self.findPatBtn.sigActivated.connect(self.sigFindPattern)
        self.scanParWinBtn = self.parTree.p.param('Scanning parameters')
        self.scanParWinBtn.sigActivated.connect(self.sigShowScanParamsClicked)
        self.parTree.p.param('Pattern').sigTreeStateChanged.connect(self.sigPatternParamsChanged)
        self.scanParWinBtn = self.parTree.p.param('Scanning parameters')
        self.scanParWinBtn.sigActivated.connect(self.sigShowScanParamsClicked)

        self.scanParamsDialog = ScanParamsDialog(
            self, self.r_l_text, self.u_d_text, self.b_f_text,
            self.timepoints_text, self.p_text, self.n_text
        )

        self.pickDatasetsDialog = PickDatasetsDialog(self, allowMultiSelect=True)

        # Parameter tree lives inside a host frame so setParameterWidget can
        # swap the active reconstructor's parameter widget without disturbing
        # the surrounding dock. A small picker above the tree both shows
        # which reconstructor's parameters are currently displayed and lets
        # the user switch between registered reconstructors.
        parameterFrame = QtWidgets.QFrame()
        parameterGrid = QtWidgets.QGridLayout()
        parameterGrid.setContentsMargins(0, 0, 0, 0)
        parameterGrid.setVerticalSpacing(2)
        parameterFrame.setLayout(parameterGrid)
        self._activeReconstructorCombo = QtWidgets.QComboBox()
        self._activeReconstructorCombo.setToolTip(
            'Active reconstructor — pick another to swap the parameter tree'
        )
        self._activeReconstructorCombo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.AdjustToContents
        )
        # When the user (not setActiveReconstructorName) changes the combo,
        # emit the public signal so the controller can install the matching
        # parameter widget. We use activated/currentIndexChanged with a
        # blockSignals guard rather than the textActivated convenience.
        self._activeReconstructorCombo.currentIndexChanged.connect(
            self._on_active_reconstructor_combo_changed
        )
        parameterGrid.addWidget(self._activeReconstructorCombo, 0, 0)
        parameterGrid.addWidget(self.parTree, 1, 0)
        self.parameterGrid = parameterGrid

        # Single DockArea backs the central widget so every panel is a
        # free-floating, dockable, tab-able Dock — mirrors ImControl's layout.
        self.dockArea = DockArea()
        self.setCentralWidget(self.dockArea)
        self.docks: dict[str, Dock] = {}
        self._runtimeAnalysisDockAnchor = None
        # IDs of analysis tools whose docks were created via the runtime
        # 'Load processor' path (not the startup config block).  Persistence
        # only snapshots this set so config-driven panels can't drag stale
        # state into a session whose setup JSON no longer asks for them.
        self._runtimeAnalysisToolIds: set[str] = set()

        # --- Left column: parameters + buttons + data tabs ---
        parametersDock = Dock('Parameters', size=(2, 5))
        parametersDock.addWidget(parameterFrame)
        self.dockArea.addDock(parametersDock, 'left')
        self.docks['Parameters'] = parametersDock
        self.parameterDock = parametersDock
        if not showParameterPanel:
            parametersDock.hide()

        actionsDock = Dock('Actions', size=(2, 1))
        actionsDock.addWidget(btnFrame)
        self.dockArea.addDock(actionsDock, 'bottom', parametersDock)
        self.docks['Actions'] = actionsDock
        if not showActionsPanel:
            actionsDock.hide()

        self.watcherDock = Dock('File watcher', size=(2, 3))
        self.watcherDock.addWidget(self.watcherFrame)
        self.dockArea.addDock(self.watcherDock, 'bottom', actionsDock)
        self.docks['File watcher'] = self.watcherDock
        if not showFileWatcherPanel:
            self.watcherDock.hide()

        self.multiDataDock = Dock('Multidata management', size=(2, 3))
        self.multiDataDock.addWidget(self.multiDataFrame)
        self.dockArea.addDock(self.multiDataDock, 'above', self.watcherDock)
        self.docks['Multidata management'] = self.multiDataDock
        if not showMultiDataPanel:
            self.multiDataDock.hide()

        self.currentDataDock = Dock('Current data', size=(2, 3))
        self.currentDataDock.addWidget(self.dataFrame)
        self.dockArea.addDock(self.currentDataDock, 'above', self.multiDataDock)
        self.docks['Current data'] = self.currentDataDock
        if not showCurrentDataPanel:
            self.currentDataDock.hide()

        # --- Center: reconstruction view ---
        reconstructionDock = Dock('Reconstruction', size=(6, 9))
        reconstructionDock.addWidget(self.reconstructionWidget)
        self.dockArea.addDock(reconstructionDock, 'right')
        self.docks['Reconstruction'] = reconstructionDock
        self._reconstructionDock = reconstructionDock
        self._autoRevealReconstructionDock = not showReconstructionPanel
        if not showReconstructionPanel:
            reconstructionDock.hide()
        self.reconstructionWidget.sigItemSelected.connect(
            self._maybeAutoRevealReconstructionDock
        )

        # --- Right column: optional analysis panels ---
        analysisPanels = [
            ('Graph', self.graphWidget),
            ('Profile', self.profileWidget),
            ('Results', self.resultsTableWidget),
            ('Projection', self.projectionWidget),
            ('Segmentation', self.segmentationWidget),
            ('PSF resolution', self.psfResolutionWidget),
            ('Colocalization', self.colocalizationWidget),
            ('Multicolor', self.multicolorWidget),
            ('FRC', self.frcWidget),
            ('ROI manager', self.roiManagerWidget),
            ('ROI stats', self.roiStatsWidget),
        ]
        prevAnalysisDock = None
        for title, widget in analysisPanels:
            if widget is None:
                continue
            dock = Dock(title, size=(3, 3))
            dock.addWidget(widget)
            if prevAnalysisDock is None:
                self.dockArea.addDock(dock, 'right', reconstructionDock)
            else:
                self.dockArea.addDock(dock, 'bottom', prevAnalysisDock)
            self.docks[title] = dock
            if title == 'Results':
                self.resultsDock = dock
                if not showResultsPanel:
                    dock.hide()
            prevAnalysisDock = dock
            self._runtimeAnalysisDockAnchor = dock

        # Snapshot the default layout for the View > Reset layout action and
        # for callers that drop a corrupt persisted state.
        self._defaultDockState = self.dockArea.saveState()
        self._defaultDockVisibility = {
            title: not dock.isHidden()
            for title, dock in self.docks.items()
        }

        # --- View menu: toggle each dock + reset layout ---
        viewMenu = menuBar.addMenu('&View')
        self._viewMenu = viewMenu
        self._dockVisibilityActions: dict[str, QtWidgets.QAction] = {}
        for title, dock in self.docks.items():
            self._addDockVisibilityAction(title, dock)
        viewMenu.addSeparator()
        # Reconstruction-list pane lives inside the Reconstruction widget, not
        # in its own Dock — wire a dedicated toggle so users have a menu
        # affordance alongside the splitter drag handle.
        if hasattr(self.reconstructionWidget, 'toggleReconListPane'):
            reconListToggle = QtWidgets.QAction('Reconstructions list', self, checkable=True)
            reconListToggle.setChecked(True)
            reconListToggle.toggled.connect(
                lambda checked: self._setReconListPaneVisible(checked)
            )
            viewMenu.addAction(reconListToggle)
            self._reconListToggleAction = reconListToggle
        viewMenu.addSeparator()
        resetLayoutAction = QtWidgets.QAction('Reset layout', self)
        resetLayoutAction.triggered.connect(self.resetLayout)
        viewMenu.addAction(resetLayoutAction)

        pg.setConfigOption('imageAxisOrder', 'row-major')

        self._connectResultPusher(self.profileWidget)
        self._connectResultPusher(self.roiStatsWidget)

    def requestFilePathFromUser(self, caption=None, defaultFolder=None, nameFilter=None,
                                isSaving=False):
        func = (QtWidgets.QFileDialog().getOpenFileName if not isSaving
                else QtWidgets.QFileDialog().getSaveFileName)

        return func(self, caption=caption, directory=defaultFolder, filter=nameFilter)[0]

    def requestFolderPathFromUser(self, caption=None, defaultFolder=None):
        return QtWidgets.QFileDialog.getExistingDirectory(caption=caption, directory=defaultFolder)

    def setAvailableRuntimeProcessors(
        self,
        choices: list[tuple[str, str]],
        placeholder: str = 'All tools loaded',
    ) -> None:
        combo = self._loadProcessorCombo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(placeholder, userData=None)
        for processor_id, processor_name in choices:
            combo.addItem(str(processor_name or processor_id), userData=processor_id)
        combo.setEnabled(bool(choices))
        combo.setCurrentIndex(0)
        combo.blockSignals(False)

    def setLoadedRuntimeProcessors(self, choices: list[tuple[str, str]]) -> None:
        combo = self._loadedProcessorCombo
        combo.blockSignals(True)
        combo.clear()
        if not choices:
            combo.addItem('No processors loaded', userData=None)
        else:
            for processor_id, processor_name in choices:
                combo.addItem(str(processor_name or processor_id), userData=processor_id)
        combo.setCurrentIndex(0)
        combo.blockSignals(False)

    def _addFileToolAction(
        self,
        action_id: str,
        action: QtWidgets.QAction,
    ) -> QtWidgets.QAction:
        self._fileToolbar.addAction(action)
        self._fileActions[str(action_id)] = action
        return action

    def _buildAnalysisToolShortcuts(self) -> None:
        for shortcut in runtime_analysis_panel_shortcuts():
            self._addAnalysisToolAction(
                shortcut.id,
                shortcut.title,
                shortcut.tooltip,
                improcessIcon(shortcut.id, self),
            )
        self._addAnalysisDockAction(
            'results-table',
            'Results',
            'Open the results table panel',
            'Results',
        )

    def _addAnalysisToolAction(
        self,
        tool_id: str,
        text: str,
        tooltip: str,
        icon,
    ) -> QtWidgets.QAction:
        action = QtWidgets.QAction(icon, text, self)
        action.setToolTip(tooltip)
        action.setStatusTip(tooltip)
        action.triggered.connect(
            lambda _checked=False, current_id=str(tool_id): (
                self.sigLoadProcessorRequested.emit(current_id)
            )
        )
        self._processorToolbar.addAction(action)
        self._analysisMenu.addAction(action)
        self._analysisToolActions[str(tool_id)] = action
        return action

    def _addAnalysisDockAction(
        self,
        action_id: str,
        text: str,
        tooltip: str,
        dock_title: str,
    ) -> QtWidgets.QAction:
        action = QtWidgets.QAction(improcessIcon(action_id, self), text, self)
        action.setToolTip(tooltip)
        action.setStatusTip(tooltip)
        action.triggered.connect(
            lambda _checked=False, title=str(dock_title): self.raiseDockByTitle(title)
        )
        self._processorToolbar.addAction(action)
        self._analysisMenu.addAction(action)
        self._analysisToolActions[str(action_id)] = action
        return action

    def _on_load_processor_combo_activated(self, index: int) -> None:
        combo = self._loadProcessorCombo
        processor_id = combo.itemData(index) if 0 <= index < combo.count() else None
        combo.setCurrentIndex(0)
        if processor_id:
            self.sigLoadProcessorRequested.emit(str(processor_id))

    def ensureRuntimeAnalysisWidget(self, processor_id: str) -> str | None:
        """Create/show the matching analysis dock for a runtime-loaded processor.

        Returns the dock title on success, ``None`` when the request cannot be
        satisfied (unknown processor id, widget construction crashed, dock
        assembly failed).  All construction work is wrapped in try/except so
        a single misbehaving widget never leaves the dock area half-built or
        crashes ImProcess.
        """
        specs = self._runtimeAnalysisToolSpecs()
        spec = specs.get(processor_id)
        if spec is None:
            self._logger.warning(
                f'No runtime analysis widget registered for id {processor_id!r}'
            )
            return None
        title = spec.title
        dock = self.docks.get(title)
        if dock is not None:
            dock.show()
            self._safeRaiseDock(dock)
            if spec.widget_kind == 'roi-manager':
                self._wireROIManagerToDependentWidgets()
            self._syncDockVisibilityActions()
            return title

        try:
            factory = self._runtimeAnalysisToolFactory(spec)
            widget = factory()
        except Exception:
            self._logger.exception(
                f'Failed to construct runtime analysis widget for {processor_id!r}'
            )
            self._showStatusMessage(
                f'Could not open {title}; see log for traceback.', 6000
            )
            return None

        try:
            dock = Dock(title, size=(3, 3))
            dock.addWidget(widget)
            anchor = self._runtimeAnalysisDockAnchor
            if anchor is None:
                self.dockArea.addDock(dock, 'right', self._reconstructionDock)
            else:
                self.dockArea.addDock(dock, 'bottom', anchor)
            self.docks[title] = dock
            self._runtimeAnalysisDockAnchor = dock
            # Mark this tool as runtime-loaded so getLayoutState only persists
            # *user-added* panels.  Config-driven panels added in __init__
            # never go through this path and therefore stay out of the
            # persisted set.
            self._runtimeAnalysisToolIds.add(processor_id)
            setattr(self, spec.attribute, widget)
            if spec.widget_kind == 'roi-manager':
                self._wireROIManagerToDependentWidgets()
            if spec.widget_kind in ('profile', 'roi-stats'):
                self._connectResultPusher(widget)
            if spec.widget_kind == 'graph':
                self._wireGraphToDependentWidgets()
            self._addDockVisibilityAction(title, dock)
            dock.show()
            self._safeRaiseDock(dock)
            self._syncDockVisibilityActions()
        except Exception:
            # Roll back partial dock state so subsequent attempts can retry
            # without colliding with an orphaned dock entry.
            self._logger.exception(
                f'Failed to install runtime analysis dock for {processor_id!r}'
            )
            self.docks.pop(title, None)
            # Also drop the runtime-set entry if we got that far before the
            # exception, so the next save doesn't persist a tool we couldn't
            # actually install.
            self._runtimeAnalysisToolIds.discard(processor_id)
            try:
                widget.setParent(None)
            except Exception:
                pass
            self._showStatusMessage(
                f'Could not dock {title}; see log for traceback.', 6000
            )
            return None
        return title

    def _showStatusMessage(self, message: str, timeout_ms: int = 4000) -> None:
        """Best-effort status-bar message for runtime-loader failures."""
        try:
            self.statusBar().showMessage(message, timeout_ms)
        except Exception:
            pass

    def _buildImageToolbar(self) -> None:
        self._addImageAction(
            'auto-contrast',
            'Auto contrast',
            'Automatically stretch display levels for the active image',
            improcessIcon('auto-contrast', self),
            self.sigImageAutoContrastRequested,
        )
        self._addImageAction(
            'brightness-contrast',
            'Brightness/Contrast...',
            'Open the brightness and contrast min/max dialog',
            improcessIcon('brightness-contrast', self),
            self.sigImageContrastDialogRequested,
            shortcut='Ctrl+Shift+C',
        )
        self._addImageAction(
            'reset-contrast',
            'Reset contrast',
            'Reset display levels to the finite data range',
            improcessIcon('reset-contrast', self),
            self.sigImageResetContrastRequested,
        )
        self._addImageLutSelector()
        self._addImageAction(
            'channels',
            'Channels...',
            'Show per-channel visibility and LUT controls',
            improcessIcon('channels', self),
            self.sigImageChannelControlsRequested,
        )
        self._imageToolbar.addSeparator()
        self._imageMenu.addSeparator()
        self._addImageAction(
            'duplicate',
            'Duplicate',
            'Duplicate the active result',
            improcessIcon('duplicate', self),
            self.sigImageDuplicateRequested,
        )
        self._addImageAction(
            'crop-substack',
            'Crop/Substack...',
            'Create a cropped or ranged substack from the active result',
            improcessIcon('crop-substack', self),
            self.sigImageCropSubstackRequested,
        )
        self._addImageAction(
            'max-projection',
            'Max projection',
            'Create a max projection of the active result along the default stack axis',
            improcessIcon('max-projection', self),
            self.sigImageMaxProjectionRequested,
        )
        self._addImageAction(
            'split-stack',
            'Split stack',
            'Split the active stack into one result per plane',
            improcessIcon('split-stack', self),
            self.sigImageSplitStackRequested,
        )
        self._addImageAction(
            'split-channels',
            'Split channels',
            'Split a C, Channel or Base axis into one result per channel',
            improcessIcon('split-channels', self),
            self.sigImageSplitChannelsRequested,
        )
        self._addImageAction(
            'merge-channels',
            'Merge channels',
            'Merge selected compatible results into a C-axis channel stack',
            improcessIcon('merge-channels', self),
            self.sigImageMergeChannelsRequested,
        )
        self._addImageAction(
            'make-composite',
            'Make composite',
            'Render a C, Channel or Base axis as colored display layers',
            improcessIcon('make-composite', self),
            self.sigImageMakeCompositeRequested,
        )
        self._addImageAction(
            'make-rgb',
            'Make RGB',
            'Bake a C, Channel or Base axis into an RGB visualization result',
            improcessIcon('make-rgb', self),
            self.sigImageMakeRgbRequested,
        )
        self._imageToolbar.addSeparator()
        self._imageMenu.addSeparator()
        self._addImageAction(
            'reset-view',
            'Reset view',
            'Reset the reconstruction viewer camera',
            improcessIcon('reset-view', self),
            self.sigImageResetViewRequested,
        )

    def _addImageAction(
        self,
        action_id: str,
        text: str,
        tooltip: str,
        icon,
        signal,
        *,
        shortcut: str | None = None,
    ) -> QtWidgets.QAction:
        action = QtWidgets.QAction(icon, text, self)
        action.setToolTip(tooltip)
        action.setStatusTip(tooltip)
        if shortcut:
            action.setShortcut(shortcut)
        action.triggered.connect(lambda _checked=False, sig=signal: sig.emit())
        self._imageToolbar.addAction(action)
        self._imageMenu.addAction(action)
        self._imageActions[action_id] = action
        return action

    def _addImageLutSelector(self) -> None:
        self._imageToolbar.addSeparator()
        self._imageToolbar.addWidget(QtWidgets.QLabel('LUT: '))
        self._imageLutCombo = QtWidgets.QComboBox()
        self._imageLutCombo.setToolTip('Colormap for the active image layer')
        for lut_id, label in IMAGE_LUTS:
            self._imageLutCombo.addItem(label, lut_id)
        self._imageLutCombo.activated.connect(self._onImageLutActivated)
        self._imageToolbar.addWidget(self._imageLutCombo)

        menu = self._imageMenu.addMenu('LUT')
        group = QtWidgets.QActionGroup(self)
        group.setExclusive(True)
        for lut_id, label in IMAGE_LUTS:
            action = QtWidgets.QAction(label, self)
            action.setCheckable(True)
            action.setData(lut_id)
            action.triggered.connect(
                lambda _checked=False, value=lut_id: self._setImageLutFromUser(value)
            )
            group.addAction(action)
            menu.addAction(action)
            self._imageLutActions[lut_id] = action
        self.setImageLutValue("grayclip")

    def _onImageLutActivated(self, _index: int) -> None:
        self._setImageLutFromUser(str(self._imageLutCombo.currentData()))

    def _setImageLutFromUser(self, lut_id: str) -> None:
        self.setImageLutValue(lut_id)
        self.sigImageLutChanged.emit(str(lut_id))

    def setImageActionsEnabled(self, enabled: bool) -> None:
        for action in self._imageActions.values():
            action.setEnabled(bool(enabled))
        self.setImageLutEnabled(enabled)

    def setImageActionEnabled(self, action_id: str, enabled: bool) -> None:
        action = self._imageActions.get(action_id)
        if action is not None:
            action.setEnabled(bool(enabled))

    def imageAction(self, action_id: str) -> QtWidgets.QAction | None:
        return self._imageActions.get(action_id)

    def fileAction(self, action_id: str) -> QtWidgets.QAction | None:
        return self._fileActions.get(action_id)

    def analysisToolAction(self, tool_id: str) -> QtWidgets.QAction | None:
        return self._analysisToolActions.get(tool_id)

    def setImageLutEnabled(self, enabled: bool) -> None:
        if hasattr(self, "_imageLutCombo"):
            self._imageLutCombo.setEnabled(bool(enabled))
        for action in getattr(self, "_imageLutActions", {}).values():
            action.setEnabled(bool(enabled))

    def setImageLutValue(self, lut_id: str) -> None:
        lut_id = str(lut_id or "grayclip")
        if hasattr(self, "_imageLutCombo"):
            index = self._imageLutCombo.findData(lut_id)
            if index < 0:
                index = self._imageLutCombo.findData("grayclip")
            blocked = self._imageLutCombo.blockSignals(True)
            try:
                self._imageLutCombo.setCurrentIndex(max(0, index))
            finally:
                self._imageLutCombo.blockSignals(blocked)
        for action_lut, action in getattr(self, "_imageLutActions", {}).items():
            blocked = action.blockSignals(True)
            try:
                action.setChecked(action_lut == lut_id)
            finally:
                action.blockSignals(blocked)

    def _connectResultPusher(self, widget):
        if widget is None:
            return
        sig = getattr(widget, "sigResultPushed", None)
        if sig is None:
            return
        sig.connect(self._onResultPushed)

    def _onResultPushed(self, columns, records):
        self.resultsTableWidget.append_records(list(columns), list(records))
        dock = getattr(self, "resultsDock", None)
        if dock is not None:
            self._safeRaiseDock(dock)

    def _onTablePlotRequested(self, spec):
        """Render a results-table plot request into the shared Graph panel."""
        if self.graphWidget is None:
            return
        try:
            from imswitch.improcess.model.table_plots import build_plot_payloads

            payloads = build_plot_payloads(
                self.resultsTableWidget.get_columns(),
                self.resultsTableWidget.get_records(),
                dict(spec),
            )
        except Exception as exc:
            self._showStatusMessage(f"Could not plot: {exc}")
            return
        self.graphWidget.setPlotPayloads(list(payloads))
        dock = self.docks.get("Graph")
        if dock is not None:
            self._safeRaiseDock(dock)

    def raiseDockByTitle(self, title: str) -> bool:
        dock = self.docks.get(str(title))
        if dock is None:
            return False
        dock.show()
        self._safeRaiseDock(dock)
        self._syncDockVisibilityActions()
        return True

    def _safeRaiseDock(self, dock) -> None:
        """Bring a dock to the front without crashing on non-tab containers.

        pyqtgraph's Dock.raiseDock() delegates to container().raiseDock(self),
        which is only implemented on TContainer (tab groups). When a dock
        lives in a VContainer or HContainer (vertical / horizontal stack —
        which is exactly what addDock('bottom', anchor) produces for runtime
        analysis panels) the call raises AttributeError. There is nothing to
        raise in that case, so swallowing the error is the right behavior.
        """
        try:
            dock.raiseDock()
        except AttributeError:
            pass
        except Exception:
            self._logger.debug(
                f'Unexpected error raising dock {getattr(dock, "name", None)!r}',
                exc_info=True,
            )

    def isRuntimeAnalysisToolLoaded(self, tool_id: str) -> bool:
        spec = self._runtimeAnalysisToolSpecs().get(tool_id)
        if spec is None:
            return True
        return spec.title in self.docks

    def _runtimeAnalysisToolSpecs(self):
        from imswitch.improcess.model.runtime_tools import runtime_analysis_tool_specs

        return runtime_analysis_tool_specs()

    def _runtimeAnalysisToolAttributes(self):
        return {
            tool_id: spec.attribute
            for tool_id, spec in self._runtimeAnalysisToolSpecs().items()
        }

    def _runtimeAnalysisToolFactory(self, spec: RuntimeAnalysisToolSpec):
        viewer = self.reconstructionWidget.napariViewer
        factories = {
            'result-processor': lambda: self._makeResultProcessorWidget(
                spec.processor_id or spec.id
            ),
            'graph': lambda: GraphWidget(),
            'profile': lambda: ProfileWidget(viewer),
            'segmentation': lambda: SegmentationWidget(
                viewer,
                roiManagerWidget=self.roiManagerWidget,
            ),
            'psf-resolution': lambda: PSFResolutionWidget(
                viewer,
                roiManagerWidget=self.roiManagerWidget,
            ),
            'colocalization': lambda: ColocalizationWidget(
                viewer,
                roiManagerWidget=self.roiManagerWidget,
            ),
            'multicolor': lambda: MulticolorWidget(viewer),
            'roi-manager': lambda: ROIManagerWidget(viewer),
            'roi-stats': lambda: ROIStatsWidget(viewer),
        }
        try:
            return factories[spec.widget_kind]
        except KeyError as exc:
            raise KeyError(
                f"Unknown runtime analysis widget kind: {spec.widget_kind!r}"
            ) from exc

    def _makeResultProcessorWidget(self, processor_id: str):
        from imswitch.improcess.reconstructors.registry import get_registry

        # get_processor now raises KeyError with helpful diagnostic if not found
        processor = get_registry().get_processor(processor_id)
        return ResultProcessorWidget(processor)

    def getRuntimeAnalysisWidget(self, tool_id: str):
        attr_name = self._runtimeAnalysisToolAttributes().get(tool_id)
        return getattr(self, attr_name, None) if attr_name else None

    def _wireROIManagerToDependentWidgets(self) -> None:
        """Late-binding: hand the ROI Manager to widgets that already exist
        but were constructed before it. Uses the public setRoiManagerWidget
        contract on each consumer rather than poking the private attribute,
        so consumers can refresh internal state on the swap if they need to."""
        roi_manager = getattr(self, 'roiManagerWidget', None)
        if roi_manager is None:
            return
        for attr in ('segmentationWidget', 'psfResolutionWidget', 'colocalizationWidget'):
            widget = getattr(self, attr, None)
            if widget is None:
                continue
            setter = getattr(widget, 'setRoiManagerWidget', None)
            if callable(setter):
                try:
                    setter(roi_manager)
                except Exception:
                    self._logger.exception(
                        f'Could not wire ROI Manager into {attr}'
                    )
            elif hasattr(widget, '_roiManagerWidget'):
                widget._roiManagerWidget = roi_manager

    def _wireGraphToDependentWidgets(self) -> None:
        """Late-binding: connect table plot requests after Graph runtime load."""
        if getattr(self, 'graphWidget', None) is None:
            return
        table = getattr(self, 'resultsTableWidget', None)
        if table is None:
            return
        try:
            table.sigPlotRequested.disconnect(self._onTablePlotRequested)
        except Exception:
            pass
        try:
            table.sigPlotRequested.connect(self._onTablePlotRequested)
        except Exception:
            self._logger.exception('Could not wire results table plot action to Graph')

    def _maybeAutoRevealReconstructionDock(self, *args) -> None:
        """Show the reconstruction dock when hidden-at-startup data arrives."""
        if not getattr(self, '_autoRevealReconstructionDock', False):
            return
        self._autoRevealReconstructionDock = False
        self.raiseDockByTitle('Reconstruction')

    def _setReconListPaneVisible(self, visible: bool) -> None:
        """Drive the reconstruction-list pane state from the View menu.

        The pane lives behind a QSplitter inside ``ReconstructionView``; we
        only need to flip it when the user's request disagrees with the
        current state so dragging the splitter doesn't get reverted by a
        stale menu check.
        """
        recon = getattr(self, 'reconstructionWidget', None)
        if recon is None or not hasattr(recon, 'toggleReconListPane'):
            return
        collapsed = recon.isReconListPaneCollapsed()
        if visible and collapsed:
            recon.toggleReconListPane()
        elif not visible and not collapsed:
            recon.toggleReconListPane()

    def setReconstructionActionsVisible(
        self,
        reconstruct_current: bool = True,
        update_reconstruction: bool = True,
        reconstruct_multidata: bool = True,
    ) -> None:
        """Show or hide the modality-specific Actions buttons.

        - reconstruct_current: hide for pass-through plugins (process() is a
          no-op wrap, so the data has already auto-routed to the viewer).
        - update_reconstruction: hide for non-MoNaLISA plugins. The 'Update
          reconstruction' button re-applies MoNaLISA scan parameters and is
          meaningless elsewhere.
        - reconstruct_multidata: hide for pass-through plugins too — there is
          no real reconstruction to batch.  Hiding it also collapses the
          row 1 grid so the remaining buttons don't render with an empty
          left cell.
        """
        btnFrame = getattr(self, '_btnFrame', None)
        if btnFrame is None:
            return
        if hasattr(btnFrame, 'reconCurrBtn'):
            btnFrame.reconCurrBtn.setVisible(bool(reconstruct_current))
        if hasattr(btnFrame, 'updateBtn'):
            btnFrame.updateBtn.setVisible(bool(update_reconstruction))
        if hasattr(btnFrame, 'reconMultiBtn'):
            btnFrame.reconMultiBtn.setVisible(bool(reconstruct_multidata))

    def setReconstructorChoices(
        self,
        choices: list[tuple[str, str]],
        current_id: str | None = None,
    ) -> None:
        """Fill the Parameters-dock picker with the registered reconstructors.

        ``choices`` is a list of ``(plugin_id, display_name)`` pairs. The
        plugin id is stored as the item's userData so user picks resolve back
        to the registry without name-collision risk. When ``current_id`` is
        provided and matches an entry, that entry is preselected.
        """
        combo = self._activeReconstructorCombo
        combo.blockSignals(True)
        combo.clear()
        for plugin_id, plugin_name in choices:
            combo.addItem(str(plugin_name or plugin_id or '—'), userData=plugin_id)
        if current_id is not None:
            for i in range(combo.count()):
                if combo.itemData(i) == current_id:
                    combo.setCurrentIndex(i)
                    break
        combo.blockSignals(False)
        self._updateParametersDockTitle()

    def setActiveReconstructorName(self, name: str) -> None:
        """Sync the Parameters-dock picker to the named reconstructor.

        If the name matches an existing combo entry, select it; otherwise
        insert a transient ``userData=None`` placeholder so a controller-
        driven name that isn't yet in the choice list still shows up. The
        dock title is also updated in both cases.
        """
        display = name.strip() if name else ''
        combo = self._activeReconstructorCombo
        combo.blockSignals(True)
        matched = False
        for i in range(combo.count()):
            if combo.itemText(i) == display:
                combo.setCurrentIndex(i)
                matched = True
                break
        if not matched and display:
            combo.addItem(display, userData=None)
            combo.setCurrentIndex(combo.count() - 1)
        combo.blockSignals(False)
        self._updateParametersDockTitle()

    def _updateParametersDockTitle(self) -> None:
        display = self._activeReconstructorCombo.currentText().strip()
        dock = self.docks.get('Parameters') if hasattr(self, 'docks') else None
        if dock is None:
            return
        title = f'Parameters — {display}' if display else 'Parameters'
        try:
            dock.setTitle(title)
        except Exception:
            pass

    def _on_active_reconstructor_combo_changed(self, index: int) -> None:
        """User picked a different reconstructor — relay the plugin id to
        the controller via ``sigActiveReconstructorChanged``."""
        combo = self._activeReconstructorCombo
        if index < 0 or index >= combo.count():
            return
        plugin_id = combo.itemData(index)
        if plugin_id is None:
            # Transient placeholder inserted by setActiveReconstructorName for
            # an unknown name — nothing actionable for the controller.
            self._updateParametersDockTitle()
            return
        self._updateParametersDockTitle()
        self.sigActiveReconstructorChanged.emit(str(plugin_id))

    def raiseCurrentDataDock(self):
        self.currentDataDock.raiseDock()

    def raiseMultiDataDock(self):
        self.multiDataDock.raiseDock()

    def getLayoutState(self) -> dict:
        """Return passive GUI layout state for persistence.

        Also serializes the list of runtime-loaded analysis tool ids so
        :meth:`setLayoutState` can recreate their docks before applying
        ``dockArea.restoreState`` — otherwise napari-side docks added at
        runtime would be silently dropped by ``missing='ignore'``.
        """
        # Snapshot just the tool ids whose docks currently exist.
        loaded_ids = self.runtimeAnalysisToolIdsLoaded()
        return {
            'dock_area': self.dockArea.saveState(),
            'runtime_analysis_tool_ids': loaded_ids,
        }

    def setLayoutState(self, state: dict) -> None:
        """Restore passive GUI layout state if it is compatible with this setup."""
        if not isinstance(state, dict):
            return

        # Recreate runtime-loaded docks BEFORE restoreState so the dock area
        # has the matching titles to restore positions for.
        for tool_id in state.get('runtime_analysis_tool_ids', []) or []:
            try:
                self.ensureRuntimeAnalysisWidget(tool_id)
            except Exception:
                self._logger.exception(
                    f'Failed to recreate runtime analysis tool {tool_id!r} during '
                    f'layout restore'
                )

        dockAreaState = state.get('dock_area')
        if dockAreaState is None:
            self._applyStartupHiddenDocks()
            self._syncDockVisibilityActions()
            return
        try:
            self.dockArea.restoreState(
                dockAreaState, missing='ignore', extra='bottom'
            )
        except Exception:
            # A corrupt or incompatible saved state should never block startup;
            # fall back to the default placement silently.
            pass
        self._applyStartupHiddenDocks()
        self._syncDockVisibilityActions()

    def runtimeAnalysisToolIdsLoaded(self) -> list[str]:
        """Return ids of analysis tools loaded via the *runtime* path.

        Excludes config-driven panels (e.g. ``segmentationPanel: true`` in
        the setup JSON) so the persisted state stays in sync with each
        session's setup config — otherwise a panel that the user removed
        from their setup file would come back via persistence as if it
        were runtime-loaded.

        Defensive: a dock the user closed since loading at runtime drops
        out automatically (its title is no longer in ``self.docks``).
        """
        specs = self._runtimeAnalysisToolSpecs()
        return sorted(
            tool_id
            for tool_id in self._runtimeAnalysisToolIds
            if tool_id in specs and specs[tool_id].title in self.docks
        )

    def resetLayout(self) -> None:
        """Restore the default dock arrangement captured at construction."""
        default_state = getattr(self, '_defaultDockState', None)
        if default_state is None:
            return
        try:
            self.dockArea.restoreState(
                default_state, missing='ignore', extra='bottom'
            )
        except Exception:
            return
        default_visibility = getattr(self, '_defaultDockVisibility', {})
        for title, dock in self.docks.items():
            if default_visibility.get(title, True):
                dock.show()
            else:
                dock.hide()
        self._syncDockVisibilityActions()

    def _applyStartupHiddenDocks(self) -> None:
        """Keep config-hidden startup docks hidden after layout restore."""
        for title, visible in getattr(self, '_defaultDockVisibility', {}).items():
            if visible:
                continue
            dock = self.docks.get(title)
            if dock is not None:
                dock.hide()

    def _syncDockVisibilityActions(self) -> None:
        """Keep the View-menu checkboxes in sync with the current dock visibility."""
        actions = getattr(self, '_dockVisibilityActions', None)
        if not actions:
            return
        for title, action in actions.items():
            dock = self.docks.get(title)
            if dock is None:
                continue
            action.blockSignals(True)
            action.setChecked(not dock.isHidden())
            action.blockSignals(False)

    def _addDockVisibilityAction(self, title: str, dock: Dock) -> None:
        actions = getattr(self, '_dockVisibilityActions', None)
        if actions is None or title in actions:
            return
        action = QtWidgets.QAction(title, self, checkable=True)
        action.setChecked(not dock.isHidden())
        action.toggled.connect(
            lambda checked, d=dock: (d.show() if checked else d.hide())
        )
        view_menu = getattr(self, '_viewMenu', None)
        if view_menu is not None:
            view_menu.addAction(action)
        actions[title] = action

    def addNewData(self, reconObj, name):
        self.reconstructionWidget.addNewData(reconObj, name)

    def setParameterWidget(self, widget):
        """Replace the legacy parameter tree with the active reconstructor UI."""
        old = self.parameterGrid.itemAtPosition(1, 0)
        if old is not None and old.widget() is not None:
            old.widget().setParent(None)
        self.parTree = widget
        self.parameterGrid.addWidget(widget, 1, 0)

        self.showPatBool = None
        self.bleachBool = None
        self.extension = None
        self.findPatBtn = None
        self.scanParWinBtn = None

        p = getattr(widget, "p", None)
        if p is None:
            return
        try:
            self.showPatBool = p.param('Show pattern')
            self.showPatBool.sigValueChanged.connect(
                lambda _, v: self.sigShowPatternChanged.emit(v)
            )
        except Exception:
            pass
        try:
            self.bleachBool = p.param('Bleaching correction')
        except Exception:
            pass
        try:
            self.extension = p.param('File extension')
        except Exception:
            pass
        try:
            self.findPatBtn = p.param('Pattern').param('Find pattern')
            self.findPatBtn.sigActivated.connect(self.sigFindPattern)
            p.param('Pattern').sigTreeStateChanged.connect(self.sigPatternParamsChanged)
        except Exception:
            pass
        try:
            self.scanParWinBtn = p.param('Scanning parameters')
            self.scanParWinBtn.sigActivated.connect(self.sigShowScanParamsClicked)
        except Exception:
            pass

    def setLegacyMonalisaParameterWidget(self):
        """Restore the legacy MoNaLISA parameter tree in the Parameters dock."""
        if isinstance(getattr(self, "parTree", None), ReconParTree):
            return
        self.setParameterWidget(ReconParTree())

    def getReconstructionParams(self):
        if hasattr(self.parTree, "get_values"):
            return self.parTree.get_values()
        return {}

    def getMultiDatas(self):
        dataList = self.multiDataFrame.dataList
        for i in range(dataList.count()):
            yield dataList.item(i).data(1)

    def showScanParamsDialog(self, blocking=False):
        if blocking:
            result = self.scanParamsDialog.exec_()
            return result == QtWidgets.QDialog.Accepted
        else:
            self.scanParamsDialog.show()

    def showPickDatasetsDialog(self, blocking=False):
        if blocking:
            result = self.pickDatasetsDialog.exec_()
            return result == QtWidgets.QDialog.Accepted
        else:
            self.pickDatasetsDialog.show()

    def getPatternParams(self):
        if getattr(self, "findPatBtn", None) is None:
            return (0, 0, 1, 1)
        patternPars = self.parTree.p.param('Pattern')
        return (np.mod(patternPars.param('Row-offset').value(),
                       patternPars.param('Row-period').value()),
                np.mod(patternPars.param('Col-offset').value(),
                       patternPars.param('Col-period').value()),
                patternPars.param('Row-period').value(),
                patternPars.param('Col-period').value())

    def setPatternParams(self, rowOffset, colOffset, rowPeriod, colPeriod):
        if getattr(self, "findPatBtn", None) is None:
            return
        patternPars = self.parTree.p.param('Pattern')
        patternPars.param('Row-offset').setValue(rowOffset)
        patternPars.param('Col-offset').setValue(colOffset)
        patternPars.param('Row-period').setValue(rowPeriod)
        patternPars.param('Col-period').setValue(colPeriod)

    def getComputeDevice(self):
        if getattr(self, "parTree", None) is None:
            return "CPU"
        return self.parTree.p.param('CPU/GPU').value()

    def getPixelSizeNm(self):
        if getattr(self, "parTree", None) is None:
            return 1
        return self.parTree.p.param('Pixel size').value()

    def getFwhmNm(self):
        if getattr(self, "parTree", None) is None:
            return 1
        return self.parTree.p.param('Reconstruction options').param('PSF FWHM').value()

    def getBgModelling(self):
        if getattr(self, "parTree", None) is None:
            return "Constant"
        return self.parTree.p.param('Reconstruction options').param('BG modelling').value()

    def getBgGaussianSize(self):
        if getattr(self, "parTree", None) is None:
            return 1
        return self.parTree.p.param('Reconstruction options').param('BG modelling') \
            .param('BG Gaussian size').value()

    def closeEvent(self, event):
        self.sigClosing.emit()
        event.accept()

    def dragEnterEvent(self, event):
        """Accept drag events containing file URLs."""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()
    
    def dropEvent(self, event):
        """Process dropped files and emit sigFilesDropped signal."""
        from pathlib import Path
        from imswitch.improcess.model.dataset_sources import has_zarr_ancestor
        
        urls = event.mimeData().urls()
        paths = []
        rejected = []
        
        # Supported extensions
        supported_exts = {'.hdf5', '.hdf', '.h5', '.tiff', '.tif', '.zarr'}
        
        for url in urls:
            path = Path(url.toLocalFile())
            
            if not path.exists():
                continue
            
            # Check file extension
            if path.suffix.lower() in supported_exts or has_zarr_ancestor(path):
                paths.append(path)
            else:
                rejected.append(path.name)
        
        # Show rejection message if any files were rejected
        if rejected:
            self.statusBar().showMessage(
                f"Rejected unsupported files: {', '.join(rejected)} "
                f"(supported: HDF5, Zarr, TIFF)",
                5000
            )
        
        # Emit signal with accepted paths
        if paths:
            self.sigFilesDropped.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()

class ReconParTree(ParameterTree):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Parameter tree for the reconstruction
        params = [
            {'name': 'Pixel size', 'type': 'float', 'value': 77, 'suffix': 'nm'},
            {'name': 'Reconstruction method', 'type': 'list',
             'value': 'MoNaLISA',
             'values': ['MoNaLISA', 'Fast Gauss MoNaLISA'],
             'tip': (
                 'MoNaLISA runs the full post-acquisition SignalExtractor path. '
                 'Fast Gauss MoNaLISA uses the low-latency Gaussian reassignment '
                 'path that live reconstruction always uses.'
             )},
            {'name': 'CPU/GPU', 'type': 'list', 'values': ['GPU', 'CPU']},
            {'name': 'Pattern', 'type': 'group', 'children': [
                {'name': 'Row-offset', 'type': 'float', 'value': 9.89, 'limits': (0, 9999)},
                {'name': 'Col-offset', 'type': 'float', 'value': 10.4, 'limits': (0, 9999)},
                {'name': 'Row-period', 'type': 'float', 'value': 11.05, 'limits': (0, 9999)},
                {'name': 'Col-period', 'type': 'float', 'value': 11.05, 'limits': (0, 9999)},
                {'name': 'Find pattern', 'type': 'action'}]},
            {'name': 'Reconstruction options', 'type': 'group', 'children': [
                {'name': 'PSF FWHM', 'type': 'float', 'value': 220, 'limits': (0, 9999),
                 'suffix': 'nm'},
                {'name': 'BG modelling', 'type': 'list',
                 'values': ['Constant', 'Gaussian', 'No background'], 'children': [
                    {'name': 'BG Gaussian size', 'type': 'float', 'value': 500, 'suffix': 'nm'}]}]},
            {'name': 'Fast Gauss options', 'type': 'group', 'children': [
                {'name': 'Footprint mode', 'type': 'list',
                 'value': 'Rectangular shells',
                 'values': ['Rectangular shells', 'Circular pinhole'],
                 'tip': ('Rectangular shells keeps the Mini_Recon footprint. '
                         'Circular pinhole uses the Pinhole radius value.')},
                {'name': 'Footprint rectangles', 'type': 'int',
                 'value': DEFAULT_FOOTPRINT_NUM_RECTS, 'limits': (1, 99),
                 'tip': ('Concentric rectangular shells sampled around each focus '
                         '(used in Rectangular shells mode).')},
                {'name': 'Gaussian sigma', 'type': 'float',
                 'value': DEFAULT_GAUSSIAN_SIGMA_PX, 'limits': (0.01, 9999),
                 'suffix': 'px',
                 'tip': 'Gaussian sigma for the footprint fit, in pixels.'},
                {'name': 'Pinhole radius', 'type': 'float',
                 'value': DEFAULT_PINHOLE_RADIUS_SIGMA, 'limits': (0.01, 99),
                 'suffix': '×σ',
                 'tip': ('Circular detection pinhole radius as a multiple of the '
                         'Gaussian sigma. Used only in Circular pinhole mode.')}]},
            {'name': 'Scanning parameters', 'type': 'action'},
            {'name': 'Show pattern', 'type': 'bool'},
            {'name': 'Bleaching correction', 'type': 'bool'},
            {'name': 'File extension', 'type': 'list', 'values': ['hdf5', 'zarr']},
        ]

        self.p = Parameter.create(name='params', type='group', children=params)
        self.setParameters(self.p, showTop=False)
        self._writable = True

    def get_values(self) -> dict:
        """Return MoNaLISA reconstruction parameters from the legacy tree."""
        pattern_pars = self.p.param('Pattern')
        recon_opts = self.p.param('Reconstruction options')
        bg_modelling = recon_opts.param('BG modelling')
        fast_gauss_opts = self.p.param('Fast Gauss options')

        return {
            'pixel_size_nm': self.p.param('Pixel size').value(),
            'reconstruction_method': self.p.param('Reconstruction method').value(),
            'device': self.p.param('CPU/GPU').value(),
            'row_offset': pattern_pars.param('Row-offset').value(),
            'col_offset': pattern_pars.param('Col-offset').value(),
            'row_period': pattern_pars.param('Row-period').value(),
            'col_period': pattern_pars.param('Col-period').value(),
            'psf_fwhm_nm': recon_opts.param('PSF FWHM').value(),
            'bg_modelling': bg_modelling.value(),
            'bg_gaussian_size_nm': bg_modelling.param('BG Gaussian size').value(),
            'fast_gauss_footprint_mode': fast_gauss_opts.param(
                'Footprint mode').value(),
            'fast_gauss_footprint_num_rects': fast_gauss_opts.param(
                'Footprint rectangles').value(),
            'fast_gauss_gaussian_sigma_px': fast_gauss_opts.param(
                'Gaussian sigma').value(),
            'fast_gauss_pinhole_radius_sigma': fast_gauss_opts.param(
                'Pinhole radius').value(),
            'bleaching_correction': self.p.param('Bleaching correction').value(),
        }


class BtnFrame(QtWidgets.QFrame):
    sigReconstuctCurrent = QtCore.Signal()
    sigReconstructMultiConsolidated = QtCore.Signal()
    sigReconstructMultiIndividual = QtCore.Signal()
    sigQuickLoadData = QtCore.Signal()
    sigUpdate = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.reconCurrBtn = BetterPushButton('Reconstruct current')
        self.reconCurrBtn.clicked.connect(self.sigReconstuctCurrent)
        self.quickLoadDataBtn = BetterPushButton('Quick load data')
        self.quickLoadDataBtn.clicked.connect(self.sigQuickLoadData)
        self.updateBtn = BetterPushButton('Update reconstruction')
        self.updateBtn.clicked.connect(self.sigUpdate)

        self.reconMultiBtn = QtWidgets.QToolButton()
        self.reconMultiBtn.setSizePolicy(
            QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Preferred)
        )
        self.reconMultiBtn.setText('Reconstruct multidata')
        self.reconMultiBtn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self.reconMultiConsolidated = QtWidgets.QAction('Consolidate into a single reconstruction')
        self.reconMultiConsolidated.triggered.connect(self.sigReconstructMultiConsolidated)
        self.reconMultiBtn.addAction(self.reconMultiConsolidated)
        self.reconMultiIndividual = QtWidgets.QAction('Reconstruct data items individually')
        self.reconMultiIndividual.triggered.connect(self.sigReconstructMultiIndividual)
        self.reconMultiBtn.addAction(self.reconMultiIndividual)

        layout = QtWidgets.QGridLayout()
        self.setLayout(layout)

        layout.addWidget(self.quickLoadDataBtn, 0, 0, 1, 2)
        layout.addWidget(self.reconCurrBtn, 1, 0)
        layout.addWidget(self.reconMultiBtn, 1, 1)
        layout.addWidget(self.updateBtn, 2, 0, 1, 2)


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
