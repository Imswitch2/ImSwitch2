import numpy as np
import pyqtgraph as pg
from pyqtgraph.dockarea import Dock, DockArea
from pyqtgraph.parametertree import Parameter, ParameterTree
from qtpy import QtCore, QtWidgets

from imswitch.imcommon.view import PickDatasetsDialog
from .DataFrame import DataFrame
from .MultiDataFrame import MultiDataFrame
from .WatcherFrame import WatcherFrame
from .ReconstructionView import ReconstructionView
from .FRCWidget import FRCWidget
from .GraphWidget import GraphWidget
from .ProfileWidget import ProfileWidget
from .ROIStatsWidget import ROIStatsWidget
from .ScanParamsDialog import ScanParamsDialog
from .guitools import BetterPushButton


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
    sigUpdate = QtCore.Signal()

    sigShowPatternChanged = QtCore.Signal(bool)
    sigFindPattern = QtCore.Signal()
    sigShowScanParamsClicked = QtCore.Signal()
    sigPatternParamsChanged = QtCore.Signal()

    sigFilesDropped = QtCore.Signal(list)  # List of pathlib.Path objects

    # Emitted when the user picks a different reconstructor in the Parameters
    # dock header. Carries the plugin id (e.g. "view-only", "monalisa").
    sigActiveReconstructorChanged = QtCore.Signal(str)

    sigClosing = QtCore.Signal()

    def __init__(
        self,
        showGraphPanel: bool = True,
        showProfilePanel: bool = True,
        showFRCPanel: bool = False,
        showROIStatsPanel: bool = False,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.setWindowTitle('Image Processing')
        self.setAcceptDrops(True)

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

        quickLoadAction = QtWidgets.QAction('Quick load data…', self)
        quickLoadAction.setShortcut('Ctrl+T')
        quickLoadAction.triggered.connect(self.sigQuickLoadData)
        file.addAction(quickLoadAction)

        file.addSeparator()

        saveReconAction = QtWidgets.QAction('Save reconstruction…', self)
        saveReconAction.setShortcut('Ctrl+D')
        saveReconAction.triggered.connect(self.sigSaveReconstruction)
        file.addAction(saveReconAction)
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

        self.dataFrame = DataFrame()
        self.multiDataFrame = MultiDataFrame()
        self.watcherFrame = WatcherFrame()

        btnFrame = BtnFrame()
        btnFrame.sigReconstuctCurrent.connect(self.sigReconstuctCurrent)
        btnFrame.sigReconstructMultiConsolidated.connect(self.sigReconstructMultiConsolidated)
        btnFrame.sigReconstructMultiIndividual.connect(self.sigReconstructMultiIndividual)
        btnFrame.sigQuickLoadData.connect(self.sigQuickLoadData)
        btnFrame.sigUpdate.connect(self.sigUpdate)

        self.reconstructionWidget = ReconstructionView()
        self.graphWidget = GraphWidget() if showGraphPanel else None
        self.profileWidget = (
            ProfileWidget(self.reconstructionWidget.napariViewer)
            if showProfilePanel
            else None
        )
        self.frcWidget = (
            FRCWidget(self.reconstructionWidget.napariViewer)
            if showFRCPanel
            else None
        )
        self.roiStatsWidget = (
            ROIStatsWidget(self.reconstructionWidget.napariViewer)
            if showROIStatsPanel
            else None
        )

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

        # --- Left column: parameters + buttons + data tabs ---
        parametersDock = Dock('Parameters', size=(2, 5))
        parametersDock.addWidget(parameterFrame)
        self.dockArea.addDock(parametersDock, 'left')
        self.docks['Parameters'] = parametersDock

        actionsDock = Dock('Actions', size=(2, 1))
        actionsDock.addWidget(btnFrame)
        self.dockArea.addDock(actionsDock, 'bottom', parametersDock)
        self.docks['Actions'] = actionsDock

        self.watcherDock = Dock('File watcher', size=(2, 3))
        self.watcherDock.addWidget(self.watcherFrame)
        self.dockArea.addDock(self.watcherDock, 'bottom', actionsDock)
        self.docks['File watcher'] = self.watcherDock

        self.multiDataDock = Dock('Multidata management', size=(2, 3))
        self.multiDataDock.addWidget(self.multiDataFrame)
        self.dockArea.addDock(self.multiDataDock, 'above', self.watcherDock)
        self.docks['Multidata management'] = self.multiDataDock

        self.currentDataDock = Dock('Current data', size=(2, 3))
        self.currentDataDock.addWidget(self.dataFrame)
        self.dockArea.addDock(self.currentDataDock, 'above', self.multiDataDock)
        self.docks['Current data'] = self.currentDataDock

        # --- Center: reconstruction view ---
        reconstructionDock = Dock('Reconstruction', size=(6, 9))
        reconstructionDock.addWidget(self.reconstructionWidget)
        self.dockArea.addDock(reconstructionDock, 'right')
        self.docks['Reconstruction'] = reconstructionDock

        # --- Right column: optional analysis panels ---
        analysisPanels = [
            ('Graph', self.graphWidget),
            ('Profile', self.profileWidget),
            ('FRC', self.frcWidget),
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
            prevAnalysisDock = dock

        # Snapshot the default layout for the View > Reset layout action and
        # for callers that drop a corrupt persisted state.
        self._defaultDockState = self.dockArea.saveState()

        # --- View menu: toggle each dock + reset layout ---
        viewMenu = menuBar.addMenu('&View')
        self._dockVisibilityActions: dict[str, QtWidgets.QAction] = {}
        for title, dock in self.docks.items():
            action = QtWidgets.QAction(title, self, checkable=True)
            action.setChecked(True)
            action.toggled.connect(
                lambda checked, d=dock: (d.show() if checked else d.hide())
            )
            viewMenu.addAction(action)
            self._dockVisibilityActions[title] = action
        viewMenu.addSeparator()
        resetLayoutAction = QtWidgets.QAction('Reset layout', self)
        resetLayoutAction.triggered.connect(self.resetLayout)
        viewMenu.addAction(resetLayoutAction)

        pg.setConfigOption('imageAxisOrder', 'row-major')

    def requestFilePathFromUser(self, caption=None, defaultFolder=None, nameFilter=None,
                                isSaving=False):
        func = (QtWidgets.QFileDialog().getOpenFileName if not isSaving
                else QtWidgets.QFileDialog().getSaveFileName)

        return func(self, caption=caption, directory=defaultFolder, filter=nameFilter)[0]

    def requestFolderPathFromUser(self, caption=None, defaultFolder=None):
        return QtWidgets.QFileDialog.getExistingDirectory(caption=caption, directory=defaultFolder)

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
        """Return passive GUI layout state for persistence."""
        return {'dock_area': self.dockArea.saveState()}

    def setLayoutState(self, state: dict) -> None:
        """Restore passive GUI layout state if it is compatible with this setup."""
        if not isinstance(state, dict):
            return
        dockAreaState = state.get('dock_area')
        if dockAreaState is None:
            return
        try:
            self.dockArea.restoreState(
                dockAreaState, missing='ignore', extra='bottom'
            )
        except Exception:
            # A corrupt or incompatible saved state should never block startup;
            # fall back to the default placement silently.
            pass
        self._syncDockVisibilityActions()

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
        for dock in self.docks.values():
            dock.show()
        self._syncDockVisibilityActions()

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
            action.setChecked(dock.isVisible())
            action.blockSignals(False)

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
            if path.suffix.lower() in supported_exts:
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
            {'name': 'Scanning parameters', 'type': 'action'},
            {'name': 'Show pattern', 'type': 'bool'},
            {'name': 'Bleaching correction', 'type': 'bool'},
            {'name': 'File extension', 'type': 'list', 'values': ['hdf5', 'zarr']},
        ]

        self.p = Parameter.create(name='params', type='group', children=params)
        self.setParameters(self.p, showTop=False)
        self._writable = True


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
