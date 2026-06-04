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
from .GraphWidget import GraphWidget
from .ProfileWidget import ProfileWidget
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
    sigDenoiseCurrent = QtCore.Signal()

    sigShowPatternChanged = QtCore.Signal(bool)
    sigFindPattern = QtCore.Signal()
    sigShowScanParamsClicked = QtCore.Signal()
    sigPatternParamsChanged = QtCore.Signal()

    sigFilesDropped = QtCore.Signal(list)  # List of pathlib.Path objects
    
    sigClosing = QtCore.Signal()

    def __init__(
        self,
        showGraphPanel: bool = True,
        showProfilePanel: bool = True,
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
        btnFrame.sigDenoiseCurrent.connect(self.sigDenoiseCurrent)

        self.reconstructionWidget = ReconstructionView()
        self.graphWidget = GraphWidget() if showGraphPanel else None
        self.profileWidget = (
            ProfileWidget(self.reconstructionWidget.napariViewer)
            if showProfilePanel
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

        parameterFrame = QtWidgets.QFrame()
        parameterGrid = QtWidgets.QGridLayout()
        parameterFrame.setLayout(parameterGrid)
        parameterGrid.addWidget(self.parTree, 0, 0)
        self.parameterGrid = parameterGrid

        DataDock = DockArea()

        self.watcherDock = Dock('File watcher')
        self.watcherDock.addWidget(self.watcherFrame)
        DataDock.addDock(self.watcherDock)

        self.multiDataDock = Dock('Multidata management')
        self.multiDataDock.addWidget(self.multiDataFrame)
        DataDock.addDock(self.multiDataDock, 'above', self.watcherDock)

        self.currentDataDock = Dock('Current data')
        self.currentDataDock.addWidget(self.dataFrame)
        DataDock.addDock(self.currentDataDock, 'above', self.multiDataDock)

        layout = QtWidgets.QHBoxLayout()
        self.cwidget = QtWidgets.QWidget()
        self.setCentralWidget(self.cwidget)
        self.cwidget.setLayout(layout)

        leftContainer = QtWidgets.QVBoxLayout()
        leftContainer.setContentsMargins(0, 0, 0, 0)

        rightContainer = QtWidgets.QVBoxLayout()
        rightContainer.setContentsMargins(0, 0, 0, 0)

        leftContainer.addWidget(parameterFrame, 1)
        leftContainer.addWidget(btnFrame, 0)
        leftContainer.addWidget(DataDock, 1)
        rightSplitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        rightSplitter.addWidget(self.reconstructionWidget)
        if self.graphWidget is not None:
            rightSplitter.addWidget(self.graphWidget)
        if self.profileWidget is not None:
            rightSplitter.addWidget(self.profileWidget)
        rightSplitter.setStretchFactor(0, 5)
        if self.graphWidget is not None and self.profileWidget is not None:
            rightSplitter.setStretchFactor(1, 1)
            rightSplitter.setStretchFactor(2, 1)
        elif self.graphWidget is not None or self.profileWidget is not None:
            rightSplitter.setStretchFactor(1, 1)
        else:
            rightSplitter.setStretchFactor(0, 1)
        rightContainer.addWidget(rightSplitter)

        layout.addLayout(leftContainer, 1)
        layout.addLayout(rightContainer, 3)

        pg.setConfigOption('imageAxisOrder', 'row-major')

    def requestFilePathFromUser(self, caption=None, defaultFolder=None, nameFilter=None,
                                isSaving=False):
        func = (QtWidgets.QFileDialog().getOpenFileName if not isSaving
                else QtWidgets.QFileDialog().getSaveFileName)

        return func(self, caption=caption, directory=defaultFolder, filter=nameFilter)[0]

    def requestFolderPathFromUser(self, caption=None, defaultFolder=None):
        return QtWidgets.QFileDialog.getExistingDirectory(caption=caption, directory=defaultFolder)

    def raiseCurrentDataDock(self):
        self.currentDataDock.raiseDock()

    def raiseMultiDataDock(self):
        self.multiDataDock.raiseDock()

    def addNewData(self, reconObj, name):
        self.reconstructionWidget.addNewData(reconObj, name)

    def setParameterWidget(self, widget):
        """Replace the legacy parameter tree with the active reconstructor UI."""
        old = self.parameterGrid.itemAtPosition(0, 0)
        if old is not None and old.widget() is not None:
            old.widget().setParent(None)
        self.parTree = widget
        self.parameterGrid.addWidget(widget, 0, 0)

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

    def getDenoiseCropSize(self):
        if not hasattr(self.parTree, "p") or self.parTree.p.param('Denoising options') is None:
            return 800
        return self.parTree.p.param('Denoising options').param('Crop size (px)').value()
    
    def getDenoiseBoolPad(self):
        if not hasattr(self.parTree, "p") or self.parTree.p.param('Denoising options') is None:
            return False
        return self.parTree.p.param('Denoising options').param('Padding').value()

    def getDenoiseModelName(self):
        if not hasattr(self.parTree, "p") or self.parTree.p.param('Denoising options') is None:
            return ""
        return self.parTree.p.param('Denoising options').param('Model name').value()


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
            {'name': 'Denoising options', 'type': 'group', 'children':[
                {'name': 'Crop size (px)', 'type': 'str', 'value': 800},
                {'name': 'Padding', 'type': 'bool'},
                {'name': 'Model name', 'type': 'str','value':'Vimentin_UNet_RCAN_lowSNR'}]}
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
    sigDenoiseCurrent = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.reconCurrBtn = BetterPushButton('Reconstruct current')
        self.reconCurrBtn.clicked.connect(self.sigReconstuctCurrent)
        self.quickLoadDataBtn = BetterPushButton('Quick load data')
        self.quickLoadDataBtn.clicked.connect(self.sigQuickLoadData)
        self.updateBtn = BetterPushButton('Update reconstruction')
        self.updateBtn.clicked.connect(self.sigUpdate)
        self.denoiseBtn = BetterPushButton("Denoise current")
        self.denoiseBtn.clicked.connect(self.sigDenoiseCurrent)

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
        layout.addWidget(self.denoiseBtn,3, 0, 1, 2)


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
