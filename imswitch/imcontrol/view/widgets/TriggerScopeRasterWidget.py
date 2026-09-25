import pyqtgraph as pg
from qtpy import QtCore, QtWidgets

from imswitch.imcontrol.view import guitools as guitools
from .basewidgets import Widget


class TriggerScopeRasterWidget(Widget):
    """Widget for the TriggerScope raster scan."""

    sigSaveScanClicked = QtCore.Signal()
    sigLoadScanClicked = QtCore.Signal()
    sigRunScanClicked = QtCore.Signal()
    sigAbortScanClicked = QtCore.Signal()
    sigForceStopScanClicked = QtCore.Signal()
    sigSeqTimeParChanged = QtCore.Signal()
    sigStageParChanged = QtCore.Signal()
    sigSignalParChanged = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # No minimum height of its own: docks stack vertically and a
        # splitter's minimum is the sum of its children's, so a panel that
        # insists on 200 px makes the window that much taller to open --
        # and a few of them together make it taller than the screen, at
        # which point Qt keeps the window at its minimum and the bottom is
        # cut off. The parameter form below scrolls instead.
        self.setMinimumSize(0, 0)

        self.scannerLabel = QtWidgets.QLabel('Raster scanner')
        self.scannerLabel.setStyleSheet('font-size: 14pt; font-weight: bold')

        self.saveScanBtn = guitools.BetterPushButton('Save Scan')
        self.loadScanBtn = guitools.BetterPushButton('Load Scan')

        self.seqTimePar = QtWidgets.QLineEdit('10')  # ms

        self.scanDims = []
        self.scanPar = {'seqTime': self.seqTimePar}
        self.pxParameters = {}
        self.pxParValues = {}

        self.scanButton = guitools.BetterPushButton('Run Scan')
        self.abortScanBtn = guitools.BetterPushButton('Stop after scan')
        self.abortScanBtn.setEnabled(False)
        self.abortScanBtn.setToolTip(
            'Stop repeating after the TriggerScope finishes the current scan.'
        )
        self._abortPending = False
        self.repeatBox = QtWidgets.QCheckBox('Repeat')

        self.graph = GraphFrame()
        self.graph.setEnabled(False)
        self.graph.setFixedHeight(128)

        self.scrollContainer = QtWidgets.QGridLayout()
        self.scrollContainer.setContentsMargins(0, 0, 0, 0)
        self.setLayout(self.scrollContainer)

        self.grid = QtWidgets.QGridLayout()
        self.gridContainer = QtWidgets.QWidget()
        self.gridContainer.setLayout(self.grid)

        self.scrollArea = QtWidgets.QScrollArea()
        self.scrollArea.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.scrollArea.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.scrollArea.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.scrollArea.setWidget(self.gridContainer)
        self.scrollArea.setWidgetResizable(True)
        self.scrollContainer.addWidget(self.scrollArea)
        self.gridContainer.installEventFilter(self)

        self.saveScanBtn.clicked.connect(self.sigSaveScanClicked)
        self.loadScanBtn.clicked.connect(self.sigLoadScanClicked)
        self.scanButton.clicked.connect(self.sigRunScanClicked)
        self.abortScanBtn.clicked.connect(self._onAbortScanClicked)
        self.seqTimePar.textChanged.connect(self.sigSeqTimeParChanged)

    def initControls(self, positionerNames, TTLDeviceNames, TTLTimeUnits):
        self.scanDims = positionerNames
        currentRow = 0

        currentRow += 1

        self.grid.addItem(
            QtWidgets.QSpacerItem(20, 40,
                                  QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Expanding),
            currentRow, 0, 1, -1
        )
        currentRow += 1

        self.grid.addWidget(self.scannerLabel, currentRow, 0)
        currentRow += 1

        self.grid.addWidget(self.loadScanBtn, currentRow, 0)
        self.grid.addWidget(self.saveScanBtn, currentRow, 1)
        self.grid.addItem(
            QtWidgets.QSpacerItem(40, 20,
                                  QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Minimum),
            currentRow, 3
        )
        self.grid.addWidget(self.repeatBox, currentRow, 4)
        self.grid.addWidget(self.abortScanBtn, currentRow, 5)
        self.grid.addWidget(self.scanButton, currentRow, 6)
        currentRow += 1

        sizeLabel = QtWidgets.QLabel('Size (µm)')
        stepLabel = QtWidgets.QLabel('Step size (µm)')
        stepsLabel = QtWidgets.QLabel('Steps (#)')
        scandimLabel = QtWidgets.QLabel('Scan dim')
        sizeLabel.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignBottom)
        stepLabel.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignBottom)
        stepsLabel.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignBottom)
        scandimLabel.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignBottom)
        self.grid.addWidget(sizeLabel, currentRow, 1)
        self.grid.addWidget(stepLabel, currentRow, 2)
        self.grid.addWidget(stepsLabel, currentRow, 3)
        self.grid.addWidget(scandimLabel, currentRow, 6)
        currentRow += 1

        for index, positionerName in enumerate(positionerNames):
            sizePar = QtWidgets.QLineEdit('2')
            self.scanPar['size' + positionerName] = sizePar
            stepSizePar = QtWidgets.QLineEdit('0.1')
            self.scanPar['stepSize' + positionerName] = stepSizePar
            numStepsPar = QtWidgets.QLineEdit('20')
            numStepsPar.setEnabled(False)
            self.scanPar['steps' + positionerName] = numStepsPar
            self.grid.addWidget(QtWidgets.QLabel(positionerName), currentRow, 0)
            self.grid.addWidget(sizePar, currentRow, 1)
            self.grid.addWidget(stepSizePar, currentRow, 2)
            self.grid.addWidget(numStepsPar, currentRow, 3)

            dimlabel = QtWidgets.QLabel(
                f'{index + 1}{guitools.ordinalSuffix(index + 1)} dimension:'
            )
            dimlabel.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            self.grid.addWidget(dimlabel, currentRow, 5)
            scanDimPar = QtWidgets.QComboBox()
            scanDimPar.addItems(self.scanDims)
            scanDimPar.setCurrentIndex(index)
            self.scanPar['scanDim' + str(index)] = scanDimPar
            self.grid.addWidget(scanDimPar, currentRow, 6)

            currentRow += 1

            self.scanPar['size' + positionerName].textChanged.connect(self.sigStageParChanged)
            self.scanPar['stepSize' + positionerName].textChanged.connect(self.sigStageParChanged)
            self.scanPar['steps' + positionerName].textChanged.connect(self.sigStageParChanged)
            self.scanPar['scanDim' + str(index)].currentIndexChanged.connect(self.sigStageParChanged)

        self.grid.addWidget(QtWidgets.QLabel('Dwell (ms):'), currentRow, 5)
        self.grid.addWidget(self.seqTimePar, currentRow, 6)

        self.grid.addItem(
            QtWidgets.QSpacerItem(20, 40,
                                  QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Expanding),
            currentRow, 0, 1, -1
        )
        currentRow += 1
        graphRow = currentRow

        startLabel = QtWidgets.QLabel(f'Start ({TTLTimeUnits})')
        endLabel = QtWidgets.QLabel(f'End ({TTLTimeUnits})')
        startLabel.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignBottom)
        endLabel.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignBottom)
        self.grid.addWidget(startLabel, currentRow, 1)
        self.grid.addWidget(endLabel, currentRow, 2)
        currentRow += 1

        for deviceName in TTLDeviceNames:
            self.grid.addWidget(QtWidgets.QLabel(deviceName), currentRow, 0)
            self.pxParameters['sta' + deviceName] = QtWidgets.QLineEdit('')
            self.pxParameters['end' + deviceName] = QtWidgets.QLineEdit('')
            self.grid.addWidget(self.pxParameters['sta' + deviceName], currentRow, 1)
            self.grid.addWidget(self.pxParameters['end' + deviceName], currentRow, 2)
            currentRow += 1

            self.pxParameters['sta' + deviceName].textChanged.connect(self.sigSignalParChanged)
            self.pxParameters['end' + deviceName].textChanged.connect(self.sigSignalParChanged)

        self.grid.addWidget(self.graph, graphRow, 3, currentRow - graphRow, 5)

    def repeatEnabled(self):
        return self.repeatBox.isChecked()

    def getScanDim(self, index):
        return self.scanPar['scanDim' + str(index)].currentText()

    def getScanSize(self, positionerName):
        return float(self.scanPar['size' + positionerName].text())

    def getScanStepSize(self, positionerName):
        return float(self.scanPar['stepSize' + positionerName].text())

    def getTTLIncluded(self, deviceName):
        return (self.pxParameters['sta' + deviceName].text() != '' and
                self.pxParameters['end' + deviceName].text() != '')

    def getTTLStarts(self, deviceName):
        return list(map(lambda s: float(s) / 1000 if s else None,
                        self.pxParameters['sta' + deviceName].text().split(',')))

    def getTTLEnds(self, deviceName):
        return list(map(lambda e: float(e) / 1000 if e else None,
                        self.pxParameters['end' + deviceName].text().split(',')))

    def getSeqTimePar(self):
        return float(self.seqTimePar.text()) / 1000

    def setRepeatEnabled(self, enabled):
        self.repeatBox.setChecked(enabled)

    def _onAbortScanClicked(self):
        if self._abortPending:
            self.sigForceStopScanClicked.emit()
        else:
            self.sigAbortScanClicked.emit()

    def setAbortPending(self, pending):
        self._abortPending = pending
        if pending:
            self.abortScanBtn.setText('Force stop / disarm')
            self.abortScanBtn.setToolTip(
                'Force ImSwitch to end the scan state and disarm the lasers. '
                'The TriggerScope firmware may still be scanning.'
            )
        else:
            self.abortScanBtn.setText('Stop after scan')
            self.abortScanBtn.setToolTip(
                'Stop repeating after the TriggerScope finishes the current scan.'
            )

    def setScanButtonChecked(self, checked):
        self.scanButton.setEnabled(not checked)
        self.scanButton.setCheckable(checked)
        self.scanButton.setChecked(checked)
        self.abortScanBtn.setEnabled(checked)
        if not checked:
            self.setAbortPending(False)

    def setScanDim(self, index, positionerName):
        scanDimPar = self.scanPar['scanDim' + str(index)]
        scanDimPar.setCurrentIndex(scanDimPar.findText(positionerName))

    def setScanSize(self, positionerName, size):
        self.scanPar['size' + positionerName].setText(str(round(size, 3)))

    def setScanStepSize(self, positionerName, stepSize):
        self.scanPar['stepSize' + positionerName].setText(str(round(stepSize, 3)))

    def setScanSteps(self, positionerName, steps):
        self.scanPar['steps' + positionerName].setText(str(steps))

    def setTTLStarts(self, deviceName, starts):
        self.pxParameters['sta' + deviceName].setText(str(round(1000 * starts, 3)))

    def setTTLEnds(self, deviceName, ends):
        self.pxParameters['end' + deviceName].setText(str(round(1000 * ends, 3)))

    def unsetTTL(self, deviceName):
        self.pxParameters['sta' + deviceName].setText('')
        self.pxParameters['end' + deviceName].setText('')

    def setSeqTimePar(self, seqTimePar):
        self.seqTimePar.setText(str(round(float(1000 * seqTimePar), 3)))

    def plotSignalGraph(self, areas, signals, colors):
        if len(areas) != len(signals) or len(signals) != len(colors):
            raise ValueError('Arguments "areas", "signals" and "colors" must be of equal length')

        self.graph.plot.clear()
        for i in range(len(areas)):
            self.graph.plot.plot(areas[i], signals[i], pen=pg.mkPen(colors[i]))

        self.graph.plot.setYRange(-0.1, 1.1)

    def eventFilter(self, source, event):
        if source is self.gridContainer and event.type() == QtCore.QEvent.Resize:
            width = self.gridContainer.minimumSizeHint().width() \
                    + self.scrollArea.verticalScrollBar().width()
            self.scrollArea.setMinimumWidth(width)
            self.setMinimumWidth(width)

        return False


class GraphFrame(pg.GraphicsLayoutWidget):
    """Creates the plot that plots the preview of the pulses."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.plot = self.addPlot(row=1, col=0)
