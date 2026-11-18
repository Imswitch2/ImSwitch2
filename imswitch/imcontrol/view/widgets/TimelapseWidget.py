from typing import Dict, List, Optional, Tuple

from qtpy import QtCore, QtWidgets

from imswitch.imcontrol.model.workflows.timelapse import IntervalMode, TimelapseKind
from .basewidgets import Widget

KIND_CAMERA = TimelapseKind.CAMERA.value
KIND_SCAN = TimelapseKind.SCAN.value
KIND_TILING = TimelapseKind.TILING.value
MODE_START_TO_START = IntervalMode.START_TO_START.value
MODE_END_TO_START = IntervalMode.END_TO_START.value

_KIND_LABELS = {
    KIND_CAMERA: 'Camera frames (no scan)',
    KIND_SCAN: 'Scan',
    KIND_TILING: 'Tiling',
}
_MODE_LABELS = {
    MODE_START_TO_START: 'Start to start',
    MODE_END_TO_START: 'Delay after each point',
}

# Editor bounds only -- what a spin box needs in order to exist, not a policy
# about what an experiment may ask for.
#: QSpinBox holds a signed 32-bit int; this is its largest value.
_QSPINBOX_INT_MAX = 2_147_483_647
#: About 31 years in seconds: far beyond any experiment, so the editor never
#: refuses a real interval.
_INTERVAL_EDITOR_MAX_S = 1e9
_SETTLE_EDITOR_MAX_MS = 1e9

#: The colour the other panels use for an error message (ScanCloakPanel,
#: the Simple MoNaLISA panel), so a failed run reads the same everywhere.
_ERROR_STYLE = 'color: #d9534f;'


class TimelapseWidget(Widget):
    """Settings and progress of a timelapse; a view only.

    What each kind of timelapse allows (which lasers may be chosen, which
    detectors, whether frames per timepoint or a grouped file make sense) is
    decided by the controller and pushed in through the setters below. The
    widget remembers those decisions so that ending a run restores them,
    rather than enabling every control it disabled while running.
    """

    sigStartTimelapse = QtCore.Signal()
    sigStopTimelapse = QtCore.Signal()
    sigKindChanged = QtCore.Signal(str)
    sigParamsChanged = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._running = False
        # The controller's last word on what is editable, re-applied whenever
        # a run ends.
        self._framesPerPointEnabled = True
        self._lasersEditable = True
        self._detectorsEnabled = True
        self._singleFileEnabled = True
        self._detectorEnabled: Dict[str, bool] = {}
        self._laserRows: Dict[str, Tuple[QtWidgets.QCheckBox,
                                         Optional[QtWidgets.QDoubleSpinBox]]] = {}
        self._laserOrder: List[str] = []
        self._detectorBoxes: Dict[str, QtWidgets.QCheckBox] = {}
        self._detectorOrder: List[str] = []

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        self.setLayout(layout)

        layout.addWidget(self._buildAcquisitionGroup())
        layout.addWidget(self._buildTimingGroup())
        layout.addWidget(self._buildIlluminationGroup())
        layout.addWidget(self._buildDetectorsGroup())
        layout.addWidget(self._buildSavingGroup())
        layout.addLayout(self._buildRunRow())
        layout.addWidget(self._buildStatusBlock())
        layout.addStretch(1)

        self.startButton.clicked.connect(self.sigStartTimelapse)
        self.stopButton.clicked.connect(self.sigStopTimelapse)
        self.kindCombo.currentIndexChanged.connect(self._onKindChanged)
        for spin in (self.timepointsSpinbox, self.framesPerPointSpinbox):
            spin.valueChanged.connect(self.sigParamsChanged)
        for spin in (self.intervalSpinbox, self.settleSpinbox):
            spin.valueChanged.connect(self.sigParamsChanged)
        self.intervalModeCombo.currentIndexChanged.connect(self.sigParamsChanged)
        self.scanSourceCombo.currentIndexChanged.connect(self.sigParamsChanged)
        self.singleFileCheck.toggled.connect(self.sigParamsChanged)
        self.settleCheck.toggled.connect(self._onSettleToggled)

        self.setScanSourceVisible(False)
        self._applyEnabled()

    # ------------------------------------------------------------------
    # Layout blocks
    # ------------------------------------------------------------------

    @staticmethod
    def _group(title: str, inner: QtWidgets.QLayout) -> QtWidgets.QGroupBox:
        box = QtWidgets.QGroupBox(title)
        inner.setContentsMargins(8, 4, 8, 6)
        inner.setSpacing(6)
        box.setLayout(inner)
        return box

    @staticmethod
    def _noteLabel() -> QtWidgets.QLabel:
        label = QtWidgets.QLabel()
        label.setWordWrap(True)
        label.setVisible(False)
        return label

    def _buildAcquisitionGroup(self) -> QtWidgets.QGroupBox:
        """What one timepoint is."""
        grid = QtWidgets.QGridLayout()
        grid.addWidget(QtWidgets.QLabel('Each timepoint:'), 0, 0)
        self.kindCombo = QtWidgets.QComboBox()
        grid.addWidget(self.kindCombo, 0, 1)

        self.scanSourceLabel = QtWidgets.QLabel('Scan source:')
        grid.addWidget(self.scanSourceLabel, 1, 0)
        self.scanSourceCombo = QtWidgets.QComboBox()
        grid.addWidget(self.scanSourceCombo, 1, 1)

        grid.addWidget(QtWidgets.QLabel('Frames per timepoint:'), 2, 0)
        self.framesPerPointSpinbox = QtWidgets.QSpinBox()
        self.framesPerPointSpinbox.setRange(1, _QSPINBOX_INT_MAX)
        grid.addWidget(self.framesPerPointSpinbox, 2, 1)
        grid.setColumnStretch(1, 1)
        return self._group('Acquisition', grid)

    def _buildTimingGroup(self) -> QtWidgets.QGroupBox:
        """How many timepoints, and how far apart."""
        grid = QtWidgets.QGridLayout()
        grid.addWidget(QtWidgets.QLabel('Timepoints:'), 0, 0)
        self.timepointsSpinbox = QtWidgets.QSpinBox()
        self.timepointsSpinbox.setRange(1, _QSPINBOX_INT_MAX)
        grid.addWidget(self.timepointsSpinbox, 0, 1)

        grid.addWidget(QtWidgets.QLabel('Interval (s):'), 1, 0)
        self.intervalSpinbox = QtWidgets.QDoubleSpinBox()
        self.intervalSpinbox.setRange(0.0, _INTERVAL_EDITOR_MAX_S)
        self.intervalSpinbox.setDecimals(3)
        grid.addWidget(self.intervalSpinbox, 1, 1)

        grid.addWidget(QtWidgets.QLabel('Measured:'), 2, 0)
        self.intervalModeCombo = QtWidgets.QComboBox()
        for mode, label in _MODE_LABELS.items():
            self.intervalModeCombo.addItem(label, mode)
        self.intervalModeCombo.setToolTip(
            'Start to start: a fixed cadence; a timepoint that runs over\n'
            'starts the next one at once.\n'
            'Delay after each point: the interval is the pause between the end\n'
            'of one timepoint and the start of the next.'
        )
        grid.addWidget(self.intervalModeCombo, 2, 1)
        grid.setColumnStretch(1, 1)
        return self._group('Timing', grid)

    def _buildIlluminationGroup(self) -> QtWidgets.QGroupBox:
        """Which lasers light each timepoint -- and only it."""
        box = QtWidgets.QVBoxLayout()
        self._laserGrid = QtWidgets.QGridLayout()
        self._laserGrid.setColumnStretch(0, 1)
        box.addLayout(self._laserGrid)
        self.laserNoteLabel = self._noteLabel()
        box.addWidget(self.laserNoteLabel)

        settleRow = QtWidgets.QHBoxLayout()
        self.settleCheck = QtWidgets.QCheckBox('Extra settle after switching on')
        self.settleSpinbox = QtWidgets.QDoubleSpinBox()
        self.settleSpinbox.setRange(0.0, _SETTLE_EDITOR_MAX_MS)
        self.settleSpinbox.setDecimals(1)
        self.settleSpinbox.setSuffix(' ms')
        tip = (
            'For light sources slower than one camera frame (shutters, AOTF\n'
            'rise). Unchecked means none declared, and the data says so.'
        )
        self.settleCheck.setToolTip(tip)
        self.settleSpinbox.setToolTip(tip)
        settleRow.addWidget(self.settleCheck)
        settleRow.addWidget(self.settleSpinbox)
        settleRow.addStretch(1)
        box.addLayout(settleRow)
        return self._group('Illumination', box)

    def _buildDetectorsGroup(self) -> QtWidgets.QGroupBox:
        box = QtWidgets.QVBoxLayout()
        self._detectorLayout = QtWidgets.QVBoxLayout()
        box.addLayout(self._detectorLayout)
        self.detectorNoteLabel = self._noteLabel()
        box.addWidget(self.detectorNoteLabel)
        return self._group('Detectors', box)

    def _buildSavingGroup(self) -> QtWidgets.QGroupBox:
        box = QtWidgets.QVBoxLayout()
        self.singleFileCheck = QtWidgets.QCheckBox(
            'One file for all timepoints (HDF5/Zarr)'
        )
        self.singleFileCheck.setChecked(True)
        box.addWidget(self.singleFileCheck)
        self.singleFileNoteLabel = self._noteLabel()
        box.addWidget(self.singleFileNoteLabel)
        self.saveSummaryLabel = QtWidgets.QLabel()
        self.saveSummaryLabel.setWordWrap(True)
        self.saveSummaryLabel.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse
        )
        box.addWidget(self.saveSummaryLabel)
        return self._group('Saving', box)

    def _buildRunRow(self) -> QtWidgets.QHBoxLayout:
        row = QtWidgets.QHBoxLayout()
        self.startButton = QtWidgets.QPushButton('Start timelapse')
        self.stopButton = QtWidgets.QPushButton('Stop')
        self.stopButton.setToolTip(
            'Let the current timepoint finish, then stop.'
        )
        row.addWidget(self.startButton)
        row.addWidget(self.stopButton)
        return row

    def _buildStatusBlock(self) -> QtWidgets.QWidget:
        block = QtWidgets.QWidget()
        inner = QtWidgets.QVBoxLayout()
        inner.setContentsMargins(0, 0, 0, 0)
        block.setLayout(inner)
        self.progressBar = QtWidgets.QProgressBar()
        self.progressBar.setRange(0, 1)
        self.progressBar.setValue(0)
        self.progressBar.setFormat('0/0 timepoints')
        inner.addWidget(self.progressBar)
        self.statusLabel = QtWidgets.QLabel()
        self.statusLabel.setWordWrap(True)
        self.statusLabel.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        inner.addWidget(self.statusLabel)
        return block

    # ------------------------------------------------------------------
    # Kind and scan source
    # ------------------------------------------------------------------

    def setAvailableKinds(self, kinds: List[str]) -> None:
        current = self.getKind()
        self.kindCombo.blockSignals(True)
        try:
            self.kindCombo.clear()
            for kind in kinds:
                self.kindCombo.addItem(_KIND_LABELS.get(kind, kind), kind)
            index = self.kindCombo.findData(current)
            self.kindCombo.setCurrentIndex(index if index >= 0 else 0)
        finally:
            self.kindCombo.blockSignals(False)
        if self.getKind() != current:
            self._onKindChanged()

    def getKind(self) -> str:
        return self.kindCombo.currentData() or ''

    def setKind(self, kind: str) -> None:
        index = self.kindCombo.findData(kind)
        if index >= 0:
            self.kindCombo.setCurrentIndex(index)

    def _onKindChanged(self, *_args) -> None:
        self.sigKindChanged.emit(self.getKind())
        self.sigParamsChanged.emit()

    def setScanSources(self, names: List[str]) -> None:
        current = self.getScanSource()
        self.scanSourceCombo.blockSignals(True)
        try:
            self.scanSourceCombo.clear()
            for name in names:
                self.scanSourceCombo.addItem(name, name)
            index = self.scanSourceCombo.findData(current)
            if index >= 0:
                self.scanSourceCombo.setCurrentIndex(index)
        finally:
            self.scanSourceCombo.blockSignals(False)

    def getScanSource(self) -> str:
        return self.scanSourceCombo.currentData() or ''

    def setScanSource(self, name: str) -> None:
        index = self.scanSourceCombo.findData(name)
        if index >= 0:
            self.scanSourceCombo.setCurrentIndex(index)

    def setScanSourceVisible(self, visible: bool) -> None:
        self.scanSourceLabel.setVisible(bool(visible))
        self.scanSourceCombo.setVisible(bool(visible))

    # ------------------------------------------------------------------
    # Timing
    # ------------------------------------------------------------------

    def getTimepoints(self) -> int:
        return int(self.timepointsSpinbox.value())

    def setTimepoints(self, n: int) -> None:
        self.timepointsSpinbox.setValue(int(n))

    def getIntervalS(self) -> float:
        return float(self.intervalSpinbox.value())

    def setIntervalS(self, seconds: float) -> None:
        self.intervalSpinbox.setValue(float(seconds))

    def getIntervalMode(self) -> str:
        return self.intervalModeCombo.currentData()

    def setIntervalMode(self, mode: str) -> None:
        index = self.intervalModeCombo.findData(mode)
        if index >= 0:
            self.intervalModeCombo.setCurrentIndex(index)

    def getFramesPerPoint(self) -> int:
        return int(self.framesPerPointSpinbox.value())

    def setFramesPerPoint(self, n: int) -> None:
        self.framesPerPointSpinbox.setValue(int(n))

    def setFramesPerPointEnabled(self, enabled: bool) -> None:
        self._framesPerPointEnabled = bool(enabled)
        self._applyEnabled()

    # ------------------------------------------------------------------
    # Illumination
    # ------------------------------------------------------------------

    def setLasers(self, lasers: List[dict]) -> None:
        """One row per laser: a checkbox, and a power box unless binary."""
        while self._laserGrid.count():
            item = self._laserGrid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._laserRows = {}
        self._laserOrder = []
        for row, laser in enumerate(lasers):
            name = str(laser['name'])
            check = QtWidgets.QCheckBox(str(laser.get('label') or name))
            self._laserGrid.addWidget(check, row, 0)
            spin = None
            if not laser.get('binary', False):
                spin = QtWidgets.QDoubleSpinBox()
                minimum = laser.get('minimum')
                maximum = laser.get('maximum')
                if minimum is not None and maximum is not None:
                    spin.setRange(float(minimum), float(maximum))
                if laser.get('step') is not None:
                    spin.setSingleStep(float(laser['step']))
                if laser.get('decimals') is not None:
                    spin.setDecimals(int(laser['decimals']))
                units = str(laser.get('units') or '')
                if units:
                    spin.setSuffix(f' {units}')
                if laser.get('value') is not None:
                    spin.setValue(float(laser['value']))
                spin.valueChanged.connect(self.sigParamsChanged)
                self._laserGrid.addWidget(spin, row, 1)
            check.toggled.connect(self._onLaserToggled)
            self._laserRows[name] = (check, spin)
            self._laserOrder.append(name)
        self._applyEnabled()

    def _onLaserToggled(self, *_args) -> None:
        self._applyEnabled()
        self.sigParamsChanged.emit()

    def getLasers(self) -> List[Tuple[str, Optional[float]]]:
        """The ticked lasers in row order; power None for a binary laser."""
        chosen = []
        for name in self._laserOrder:
            check, spin = self._laserRows[name]
            if check.isChecked():
                chosen.append((name, float(spin.value()) if spin else None))
        return chosen

    def setLaserChecked(self, name: str, checked: bool) -> None:
        row = self._laserRows.get(name)
        if row is not None:
            row[0].setChecked(bool(checked))

    def setLaserPower(self, name: str, value: float) -> None:
        row = self._laserRows.get(name)
        if row is not None and row[1] is not None:
            row[1].setValue(float(value))

    def setLasersEditable(self, editable: bool, note: str = '') -> None:
        self._lasersEditable = bool(editable)
        self._setNote(self.laserNoteLabel, note)
        self._applyEnabled()

    def showLaserStates(self, states: Dict[str, bool]) -> None:
        """Show, read-only, which lasers something else will fire."""
        for name, active in states.items():
            row = self._laserRows.get(name)
            if row is None:
                continue
            check = row[0]
            # The checkbox's own signals only: this is a display, not an edit.
            check.blockSignals(True)
            try:
                check.setChecked(bool(active))
            finally:
                check.blockSignals(False)
        self._applyEnabled()

    def getIlluminationSettleMs(self) -> Optional[float]:
        if not self.settleCheck.isChecked():
            return None
        return float(self.settleSpinbox.value())

    def setIlluminationSettleMs(self, value: Optional[float]) -> None:
        if value is None:
            self.settleCheck.setChecked(False)
        else:
            self.settleSpinbox.setValue(float(value))
            self.settleCheck.setChecked(True)
        self._applyEnabled()

    def _onSettleToggled(self, *_args) -> None:
        self._applyEnabled()
        self.sigParamsChanged.emit()

    # ------------------------------------------------------------------
    # Detectors
    # ------------------------------------------------------------------

    def setDetectors(self, names: List[str], checked: List[str]) -> None:
        while self._detectorLayout.count():
            item = self._detectorLayout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._detectorBoxes = {}
        self._detectorOrder = []
        self._detectorEnabled = {}
        wanted = set(checked or ())
        for name in names:
            box = QtWidgets.QCheckBox(str(name))
            box.setChecked(name in wanted)
            box.toggled.connect(self.sigParamsChanged)
            self._detectorLayout.addWidget(box)
            self._detectorBoxes[name] = box
            self._detectorOrder.append(name)
            self._detectorEnabled[name] = True
        self._applyEnabled()

    def getDetectors(self) -> List[str]:
        return [
            name for name in self._detectorOrder
            if self._detectorBoxes[name].isChecked()
        ]

    def setDetectorEnabled(self, name: str, enabled: bool,
                           tooltip: str = '') -> None:
        box = self._detectorBoxes.get(name)
        if box is None:
            return
        self._detectorEnabled[name] = bool(enabled)
        box.setToolTip(tooltip)
        self._applyEnabled()

    def setDetectorsEnabled(self, enabled: bool, note: str = '') -> None:
        self._detectorsEnabled = bool(enabled)
        self._setNote(self.detectorNoteLabel, note)
        self._applyEnabled()

    # ------------------------------------------------------------------
    # Saving
    # ------------------------------------------------------------------

    def getSingleFile(self) -> bool:
        return bool(self.singleFileCheck.isChecked())

    def setSingleFile(self, single: bool) -> None:
        self.singleFileCheck.setChecked(bool(single))

    def setSingleFileEnabled(self, enabled: bool, note: str = '') -> None:
        self._singleFileEnabled = bool(enabled)
        self._setNote(self.singleFileNoteLabel, note)
        self._applyEnabled()

    def setSaveSummary(self, text: str) -> None:
        self.saveSummaryLabel.setText(str(text or ''))

    # ------------------------------------------------------------------
    # Run state
    # ------------------------------------------------------------------

    def setRunning(self, running: bool) -> None:
        self._running = bool(running)
        self._applyEnabled()

    def setProgress(self, done: int, total: int) -> None:
        total = max(0, int(total))
        done = max(0, min(int(done), total)) if total else 0
        self.progressBar.setRange(0, max(1, total))
        self.progressBar.setValue(done)
        self.progressBar.setFormat(f'{done}/{total} timepoints')

    def setStatus(self, text: str) -> None:
        self.statusLabel.setText(str(text or ''))

    def setStatusIsError(self, isError: bool) -> None:
        self.statusLabel.setStyleSheet(_ERROR_STYLE if isError else '')

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _setNote(label: QtWidgets.QLabel, note: str) -> None:
        label.setText(str(note or ''))
        label.setVisible(bool(note))

    def _applyEnabled(self) -> None:
        """Every control's enabled state, from the run state and the
        controller's flags -- the one place both are combined."""
        idle = not self._running
        self.startButton.setEnabled(idle)
        self.stopButton.setEnabled(self._running)
        for control in (self.kindCombo, self.scanSourceCombo,
                        self.timepointsSpinbox, self.intervalSpinbox,
                        self.intervalModeCombo):
            control.setEnabled(idle)
        self.framesPerPointSpinbox.setEnabled(
            idle and self._framesPerPointEnabled)

        lasers = idle and self._lasersEditable
        for check, spin in self._laserRows.values():
            check.setEnabled(lasers)
            if spin is not None:
                spin.setEnabled(lasers and check.isChecked())
        self.settleCheck.setEnabled(lasers)
        self.settleSpinbox.setEnabled(lasers and self.settleCheck.isChecked())

        detectors = idle and self._detectorsEnabled
        for name, box in self._detectorBoxes.items():
            box.setEnabled(detectors and self._detectorEnabled.get(name, True))

        self.singleFileCheck.setEnabled(idle and self._singleFileEnabled)
