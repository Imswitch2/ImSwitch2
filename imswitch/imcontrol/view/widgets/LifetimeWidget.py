"""The Lifetime widget: FLIM, tau-STED and the card's signals in one panel.

Layout (see the Lifetime 2.0 plan, section 5): a detector picker and a mode
switch on top; the aggregated decay of the last ready frame, always
visible; a settings group shared by the FLIM and Tau STED modes; a mode
panel; a status strip with the card's live rates; and a footer with Run
once / Live / Stop, the software accumulate count, a name and Save.

The widget holds no device logic: every control emits a signal the
``LifetimeController`` acts on, and every display is fed by it. ``FLIMHist``
in a setup file is an alias for this widget opened on its FLIM view.
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from qtpy import QtCore, QtGui, QtWidgets

from .basewidgets import Widget

MODES = ('FLIM', 'Tau STED', 'Signals')
FIT_METHODS = ('moment', 'phasor', 'exp1')
DISPLAYS = (('lifetime', 'Lifetime image'), ('intensity', 'Intensity image'),
            ('overlay', 'Intensity-weighted lifetime overlay'), ('none', 'No layer'))
#: The fields of the settings group, in the order they are laid out, with
#: their detector parameter (None: widget-only) and spin-box setup.
SETTING_FIELDS = (
    # key, label, detector parameter, (min, max, step, decimals, suffix)
    ('fit_method', 'Fit', 'fit_method', None),
    ('min_counts', 'Min counts / px', 'min_counts_per_pixel', (1, 1_000_000, 1, 0, '')),
    ('rep_rate_mhz', 'Rep rate', 'laser_rep_rate_mhz', (0.001, 10_000.0, 0.1, 4, ' MHz')),
    ('binwidth_ps', 'Bin width', 'binwidth_ps', (1, 1_000_000, 1, 0, ' ps')),
    ('n_bins', 'Bins', 'n_bins', (1, 1_000_000, 1, 0, '')),
    ('t0_ps', 't0', 't0_ps', (-10_000_000, 10_000_000, 10, 0, ' ps')),
    ('background_hz', 'Background', 'background_rate_hz', (0.0, 1e9, 100.0, 0, ' Hz')),
    ('tau_min_ns', 'Colour range min', None, (0.0, 1000.0, 0.1, 2, ' ns')),
    ('tau_max_ns', 'Colour range max', None, (0.0, 1000.0, 0.1, 2, ' ns')),
)


class LifetimeWidget(Widget):
    """See the module docstring."""

    #: The mode the panel opens on; the ``FLIMHist`` alias keeps 'FLIM'.
    initialMode = 'FLIM'

    sigDetectorChanged = QtCore.Signal(str)
    sigModeChanged = QtCore.Signal(str)
    sigSettingChanged = QtCore.Signal(str, object)  # (key, value)
    sigDisplayChanged = QtCore.Signal(str)
    sigMeasureRepRate = QtCore.Signal()
    sigFindT0 = QtCore.Signal()
    sigRunOnce = QtCore.Signal()
    sigLiveToggled = QtCore.Signal(bool)
    sigStop = QtCore.Signal()
    sigSave = QtCore.Signal()
    sigPileupMapToggled = QtCore.Signal(bool)
    sigTriggerLevelEdited = QtCore.Signal(str, float)  # (role, volts)
    sigTriggerSweep = QtCore.Signal(str)  # (role)
    sigScopeSnapshot = QtCore.Signal()
    sigPreflight = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._roleRows = {}
        self._buildTop()
        self._buildDecay()
        self._buildSettings()
        self._buildModePanels()
        self._buildStatusAndFooter()

        vbox = QtWidgets.QVBoxLayout()
        self.setLayout(vbox)
        vbox.addLayout(self._topRow)
        vbox.addWidget(self._decayPlot, stretch=2)
        vbox.addWidget(self._decayInfo)
        vbox.addWidget(self._settingsGroup)
        vbox.addWidget(self._modeStack, stretch=2)
        vbox.addWidget(self._statusStrip)
        vbox.addLayout(self._footer)

        self.setMode(self.initialMode)
        self.setRunning(False)

    # ------------------------------------------------------------------ #
    # Building                                                             #
    # ------------------------------------------------------------------ #

    def _buildTop(self):
        self._topRow = QtWidgets.QHBoxLayout()
        self._topRow.addWidget(QtWidgets.QLabel('Detector'))
        self.detectorCombo = QtWidgets.QComboBox()
        self.detectorCombo.currentTextChanged.connect(self.sigDetectorChanged)
        self._topRow.addWidget(self.detectorCombo)
        self._topRow.addSpacing(12)
        self._topRow.addWidget(QtWidgets.QLabel('Mode:'))
        self.modeButtons = {}
        self._modeGroup = QtWidgets.QButtonGroup(self)
        for mode in MODES:
            button = QtWidgets.QRadioButton(mode)
            self.modeButtons[mode] = button
            self._modeGroup.addButton(button)
            self._topRow.addWidget(button)
            button.toggled.connect(lambda checked, m=mode: checked and self._onModeToggled(m))
        self._topRow.addStretch()

    def _buildDecay(self):
        self._decayPlot = pg.PlotWidget()
        self._decayPlot.setLabel('bottom', 'Arrival time (ns)')
        self._decayPlot.setLabel('left', 'Photons')
        self._decayPlot.showGrid(x=True, y=True, alpha=0.3)
        self._decayCurve = self._decayPlot.plot([], [], pen=pg.mkPen((70, 130, 180), width=1.5),
                                                stepMode=False)
        self._peakLine = pg.InfiniteLine(angle=90, movable=False,
                                         pen=pg.mkPen('r', width=1.2, style=QtCore.Qt.DashLine))
        self._backgroundLine = pg.InfiniteLine(angle=0, movable=False,
                                               pen=pg.mkPen((200, 200, 60), width=1.0,
                                                            style=QtCore.Qt.DotLine))
        self._stedLine = pg.InfiniteLine(angle=90, movable=False,
                                         pen=pg.mkPen((255, 140, 0), width=1.2,
                                                      style=QtCore.Qt.DashDotLine))
        for line in (self._peakLine, self._backgroundLine, self._stedLine):
            line.setVisible(False)
            self._decayPlot.addItem(line)
        self._decayInfo = QtWidgets.QLabel('decay: no frame yet')
        self._decayInfo.setStyleSheet('color: grey;')
        self.logCheck = QtWidgets.QCheckBox('log')
        self.logCheck.toggled.connect(self._onLogToggled)
        self._topRow.addWidget(self.logCheck)

    def _buildSettings(self):
        self._settingsGroup = QtWidgets.QGroupBox('Fit and display')
        grid = QtWidgets.QGridLayout()
        self._settingsGroup.setLayout(grid)
        self.settingFields = {}
        row = col = 0
        for key, label, _param, spec in SETTING_FIELDS:
            grid.addWidget(QtWidgets.QLabel(label), row, col)
            if spec is None:
                field = QtWidgets.QComboBox()
                field.addItems(FIT_METHODS)
                field.currentTextChanged.connect(lambda v, k=key: self.sigSettingChanged.emit(k, v))
            else:
                lo, hi, step, decimals, suffix = spec
                real = decimals or any(isinstance(v, float) for v in (lo, hi, step))
                field = QtWidgets.QDoubleSpinBox() if real else QtWidgets.QSpinBox()
                field.setRange(lo, hi)
                field.setSingleStep(step)
                if decimals:
                    field.setDecimals(decimals)
                if suffix:
                    field.setSuffix(suffix)
                field.setKeyboardTracking(False)
                field.valueChanged.connect(lambda v, k=key: self.sigSettingChanged.emit(k, v))
            self.settingFields[key] = field
            grid.addWidget(field, row, col + 1)
            col += 2
            if col >= 6:
                row, col = row + 1, 0
        self.measureRepRateButton = QtWidgets.QPushButton('Measure rep rate')
        self.measureRepRateButton.setToolTip('The laser sync, divided and unfiltered, inside a '
                                             'calibration transaction (tutorial 04)')
        self.measureRepRateButton.clicked.connect(self.sigMeasureRepRate)
        self.findT0Button = QtWidgets.QPushButton('Find t0 from decay')
        self.findT0Button.setToolTip('Move t0 by where the last decay peaks')
        self.findT0Button.clicked.connect(self.sigFindT0)
        if col >= 6:
            row, col = row + 1, 0
        grid.addWidget(self.measureRepRateButton, row + 1, 0, 1, 2)
        grid.addWidget(self.findT0Button, row + 1, 2, 1, 2)
        grid.addWidget(QtWidgets.QLabel('Show'), row + 1, 4)
        self.displayCombo = QtWidgets.QComboBox()
        for key, label in DISPLAYS:
            self.displayCombo.addItem(label, userData=key)
        self.displayCombo.currentIndexChanged.connect(
            lambda _i: self.sigDisplayChanged.emit(self.getDisplay()))
        grid.addWidget(self.displayCombo, row + 1, 5)

    def _buildModePanels(self):
        self._modeStack = QtWidgets.QStackedWidget()
        # FLIM: the per-pixel lifetime distribution and the phasor readout.
        flim = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(flim)
        self._histPlot = pg.PlotWidget()
        self._histPlot.setLabel('bottom', 'Lifetime (ns)')
        self._histPlot.setLabel('left', 'Pixels')
        self._histBars = pg.BarGraphItem(x=[], height=[], width=0.1,
                                         brush=pg.mkBrush(70, 130, 180, 200), pen=pg.mkPen(None))
        self._histPlot.addItem(self._histBars)
        self._histMean = pg.InfiniteLine(angle=90, movable=False,
                                         pen=pg.mkPen('r', width=1.5, style=QtCore.Qt.DashLine))
        self._histMean.setVisible(False)
        self._histPlot.addItem(self._histMean)
        layout.addWidget(self._histPlot, stretch=1)
        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(QtWidgets.QLabel('Bins'))
        self.histBinsSpin = QtWidgets.QSpinBox()
        self.histBinsSpin.setRange(2, 1000)
        self.histBinsSpin.setValue(50)
        controls.addWidget(self.histBinsSpin)
        self.accumulateHistCheck = QtWidgets.QCheckBox('Pool scans')
        self.accumulateHistCheck.setToolTip('Pool the final lifetimes of every scan since ticked')
        controls.addWidget(self.accumulateHistCheck)
        controls.addStretch()
        self.phasorLabel = QtWidgets.QLabel('phasor (g, s): —')
        controls.addWidget(self.phasorLabel)
        layout.addLayout(controls)
        self.histStatLabel = QtWidgets.QLabel('Valid pixels: —   Mean: — ns')
        self.histStatLabel.setStyleSheet('color: grey;')
        layout.addWidget(self.histStatLabel)
        self._modeStack.addWidget(flim)

        # Tau STED: lifetime against intensity per pixel, the pile-up map.
        tau = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tau)
        self._scatterPlot = pg.PlotWidget()
        self._scatterPlot.setLabel('bottom', 'Intensity (photons)')
        self._scatterPlot.setLabel('left', 'Lifetime (ns)')
        self._scatter = pg.ScatterPlotItem(size=3, pen=pg.mkPen(None),
                                           brush=pg.mkBrush(70, 130, 180, 120))
        self._scatterPlot.addItem(self._scatter)
        layout.addWidget(self._scatterPlot, stretch=1)
        controls = QtWidgets.QHBoxLayout()
        self.pileupMapCheck = QtWidgets.QCheckBox('Pile-up map layer')
        self.pileupMapCheck.setToolTip('Photons per excitation pulse, per pixel; red above 10 %')
        self.pileupMapCheck.toggled.connect(self.sigPileupMapToggled)
        controls.addWidget(self.pileupMapCheck)
        controls.addStretch()
        self.tauStatLabel = QtWidgets.QLabel('τ: —')
        controls.addWidget(self.tauStatLabel)
        layout.addLayout(controls)
        self._modeStack.addWidget(tau)

        # Signals: the card, live.
        signals = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(signals)
        self.ratesTable = QtWidgets.QTableWidget(0, 3)
        self.ratesTable.setHorizontalHeaderLabels(['role', 'rate', 'trigger (V)'])
        self.ratesTable.horizontalHeader().setStretchLastSection(True)
        self.ratesTable.verticalHeader().setVisible(False)
        self.ratesTable.setMaximumHeight(170)
        layout.addWidget(self.ratesTable)
        self.signalsInfo = QtWidgets.QLabel('direction: —   filter: —   overflows: —')
        layout.addWidget(self.signalsInfo)
        buttons = QtWidgets.QHBoxLayout()
        self.sweepRoleCombo = QtWidgets.QComboBox()
        buttons.addWidget(self.sweepRoleCombo)
        self.sweepButton = QtWidgets.QPushButton('Trigger sweep')
        self.sweepButton.clicked.connect(lambda: self.sigTriggerSweep.emit(self.sweepRoleCombo.currentText()))
        buttons.addWidget(self.sweepButton)
        self.scopeButton = QtWidgets.QPushButton('Scope snapshot')
        self.scopeButton.clicked.connect(self.sigScopeSnapshot)
        buttons.addWidget(self.scopeButton)
        self.preflightButton = QtWidgets.QPushButton('Pre-flight')
        self.preflightButton.clicked.connect(self.sigPreflight)
        buttons.addWidget(self.preflightButton)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.signalsText = QtWidgets.QPlainTextEdit()
        self.signalsText.setReadOnly(True)
        self.signalsText.setPlaceholderText('Sweep, scope and pre-flight results appear here.')
        layout.addWidget(self.signalsText, stretch=1)
        self._modeStack.addWidget(signals)

    def _buildStatusAndFooter(self):
        self._statusStrip = QtWidgets.QLabel('● no card')
        self._statusStrip.setStyleSheet('color: grey;')
        self._footer = QtWidgets.QHBoxLayout()
        self.runButton = QtWidgets.QPushButton('Run once')
        self.runButton.clicked.connect(self.sigRunOnce)
        self.liveButton = QtWidgets.QPushButton('Live ▶')
        self.liveButton.setCheckable(True)
        self.liveButton.toggled.connect(self.sigLiveToggled)
        self.stopButton = QtWidgets.QPushButton('Stop')
        self.stopButton.clicked.connect(self.sigStop)
        for button in (self.runButton, self.liveButton, self.stopButton):
            self._footer.addWidget(button)
        self._footer.addSpacing(8)
        self._footer.addWidget(QtWidgets.QLabel('accumulate'))
        self.accumulateSpin = QtWidgets.QSpinBox()
        self.accumulateSpin.setRange(1, 1000)
        self.accumulateSpin.setValue(1)
        self.accumulateSpin.setToolTip('Scans summed per Run once / per Live frame')
        self._footer.addWidget(self.accumulateSpin)
        self._footer.addWidget(QtWidgets.QLabel('scans   name'))
        self.nameEdit = QtWidgets.QLineEdit('lifetime')
        self.nameEdit.setMaximumWidth(160)
        self._footer.addWidget(self.nameEdit)
        self.saveButton = QtWidgets.QPushButton('Save')
        self.saveButton.setToolTip("Save the last products into the Recording widget's folder")
        self.saveButton.clicked.connect(self.sigSave)
        self._footer.addWidget(self.saveButton)
        self._footer.addStretch()
        self.footerStatus = QtWidgets.QLabel('')
        self.footerStatus.setStyleSheet('color: grey;')
        self._footer.addWidget(self.footerStatus)

    # ------------------------------------------------------------------ #
    # Mode, detectors, settings                                            #
    # ------------------------------------------------------------------ #

    def _onModeToggled(self, mode):
        self._modeStack.setCurrentIndex(MODES.index(mode))
        self._settingsGroup.setVisible(mode != 'Signals')
        self.sigModeChanged.emit(mode)

    def setMode(self, mode: str):
        if mode not in MODES:
            mode = 'FLIM'
        self.modeButtons[mode].setChecked(True)
        self._modeStack.setCurrentIndex(MODES.index(mode))
        self._settingsGroup.setVisible(mode != 'Signals')

    def getMode(self) -> str:
        return next((m for m, b in self.modeButtons.items() if b.isChecked()), 'FLIM')

    def setDetectors(self, names, current=None):
        self.detectorCombo.blockSignals(True)
        self.detectorCombo.clear()
        self.detectorCombo.addItems(list(names))
        if current in names:
            self.detectorCombo.setCurrentText(current)
        self.detectorCombo.blockSignals(False)
        self.detectorCombo.setEnabled(len(names) > 1)

    def getDetector(self) -> str:
        return self.detectorCombo.currentText()

    def setSetting(self, key: str, value):
        field = self.settingFields[key]
        field.blockSignals(True)
        try:
            if isinstance(field, QtWidgets.QComboBox):
                field.setCurrentText(str(value))
            else:
                field.setValue(type(field.value())(value))
        finally:
            field.blockSignals(False)

    def getSetting(self, key: str):
        field = self.settingFields[key]
        return field.currentText() if isinstance(field, QtWidgets.QComboBox) else field.value()

    def getSettings(self) -> dict:
        return {key: self.getSetting(key) for key in self.settingFields}

    def getDisplay(self) -> str:
        return str(self.displayCombo.currentData() or 'lifetime')

    def setDisplay(self, key: str):
        index = self.displayCombo.findData(key)
        if index >= 0:
            self.displayCombo.setCurrentIndex(index)

    def getColourRange(self):
        lo, hi = self.getSetting('tau_min_ns'), self.getSetting('tau_max_ns')
        return (lo, hi) if hi > lo else (0.0, max(1.0, hi))

    def _onLogToggled(self, enabled):
        self._decayPlot.setLogMode(x=False, y=bool(enabled))

    def setRunning(self, running: bool, live: bool = False):
        self.runButton.setEnabled(not running)
        self.stopButton.setEnabled(running)
        self.saveButton.setEnabled(not running)
        self.accumulateSpin.setEnabled(not running)
        if not running and self.liveButton.isChecked():
            self.liveButton.blockSignals(True)
            self.liveButton.setChecked(False)
            self.liveButton.blockSignals(False)
        self.liveButton.setEnabled(not running or live)

    def setFooterStatus(self, text: str, error: bool = False):
        self.footerStatus.setText(text)
        self.footerStatus.setStyleSheet('color: #c33;' if error else 'color: grey;')

    def setStatusStrip(self, text: str, warn: bool = False):
        self._statusStrip.setText(text)
        self._statusStrip.setStyleSheet('color: #c60;' if warn else 'color: grey;')

    # ------------------------------------------------------------------ #
    # Displays                                                             #
    # ------------------------------------------------------------------ #

    def updateDecay(self, t_axis_ns, counts, *, peak_ns=None, background_per_bin=0.0,
                    direction='forward', tau_ns=0.0, sted_ns=None, binwidth_ps=None):
        t = np.asarray(t_axis_ns, dtype=float)
        c = np.asarray(counts, dtype=float)
        if t.size == 0 or c.size != t.size:
            self._decayCurve.setData([], [])
            self._decayInfo.setText('decay: no frame yet')
            return
        shown = np.maximum(c, 0.5) if self.logCheck.isChecked() else c
        self._decayCurve.setData(t, shown)
        if peak_ns is not None:
            self._peakLine.setValue(float(peak_ns))
        self._peakLine.setVisible(peak_ns is not None)
        if background_per_bin > 0:
            self._backgroundLine.setValue(float(background_per_bin))
        self._backgroundLine.setVisible(background_per_bin > 0)
        if sted_ns is not None:
            self._stedLine.setValue(float(sted_ns))
        self._stedLine.setVisible(sted_ns is not None)
        bins = f'{t.size}×{binwidth_ps:g} ps' if binwidth_ps else f'{t.size} bins'
        self._decayInfo.setText(
            f'{direction} · peak {peak_ns:.2f} ns · τ {tau_ns:.2f} ns · {int(c.sum()):,} photons '
            f'· {bins} · bg {background_per_bin:.3g}/bin'
            if peak_ns is not None else f'{direction} · {int(c.sum()):,} photons · {bins}')
        self._decayInfo.setStyleSheet('')

    def updateLifetimeHistogram(self, lifetimes_ns):
        values = np.asarray(lifetimes_ns, dtype=float).ravel()
        values = values[np.isfinite(values) & (values > 0)]
        lo, hi = self.getColourRange()
        if values.size == 0:
            self._histBars.setOpts(x=[], height=[], width=0.1)
            self._histMean.setVisible(False)
            self.histStatLabel.setText('Valid pixels: 0   Mean: — ns')
            return
        counts, edges = np.histogram(values, bins=int(self.histBinsSpin.value()), range=(lo, hi))
        centres = 0.5 * (edges[:-1] + edges[1:])
        self._histBars.setOpts(x=centres, height=counts.astype(float), width=(edges[1] - edges[0]) * 0.85)
        self._histPlot.setXRange(lo, hi, padding=0.02)
        mean = float(values.mean())
        self._histMean.setValue(mean)
        self._histMean.setVisible(True)
        self.histStatLabel.setText(f'Valid pixels: {values.size}   Mean: {mean:.2f} ns')
        self.histStatLabel.setStyleSheet('')

    def updatePhasor(self, g, s):
        if g is None:
            self.phasorLabel.setText('phasor (g, s): —')
        else:
            self.phasorLabel.setText(f'phasor (g, s): ({g:.3f}, {s:.3f})')

    def updateScatter(self, intensity, lifetime_ns, max_points=5000):
        x = np.asarray(intensity, dtype=float).ravel()
        y = np.asarray(lifetime_ns, dtype=float).ravel()
        good = np.isfinite(y) & (y > 0)
        x, y = x[good], y[good]
        if x.size > max_points:
            pick = np.linspace(0, x.size - 1, max_points).astype(int)
            x, y = x[pick], y[pick]
        self._scatter.setData(x, y)
        if y.size:
            self.tauStatLabel.setText(f'τ: median {np.median(y):.2f} ns over {y.size} px')
        else:
            self.tauStatLabel.setText('τ: —')

    def setRoles(self, roles):
        self.sweepRoleCombo.clear()
        self.sweepRoleCombo.addItems(list(roles))
        self.ratesTable.setRowCount(len(roles))
        self._roleRows = {}
        for row, role in enumerate(roles):
            self.ratesTable.setItem(row, 0, QtWidgets.QTableWidgetItem(role))
            self.ratesTable.setItem(row, 1, QtWidgets.QTableWidgetItem('—'))
            spin = QtWidgets.QDoubleSpinBox()
            spin.setRange(-2.5, 2.5)
            spin.setDecimals(3)
            spin.setSingleStep(0.01)
            spin.setKeyboardTracking(False)
            spin.valueChanged.connect(lambda v, r=role: self.sigTriggerLevelEdited.emit(r, float(v)))
            self.ratesTable.setCellWidget(row, 2, spin)
            self._roleRows[role] = row

    def setTriggerLevel(self, role, volts):
        row = self._roleRows.get(role)
        if row is None:
            return
        spin = self.ratesTable.cellWidget(row, 2)
        spin.blockSignals(True)
        spin.setValue(float(volts))
        spin.blockSignals(False)

    def updateRates(self, rates_hz: dict, *, direction='forward', filter_on=False, overflows=0):
        for role, row in self._roleRows.items():
            rate = rates_hz.get(role)
            text = '—' if rate is None else (f'{rate / 1e6:.3f} MHz' if rate >= 1e6 else f'{rate:,.0f} Hz')
            self.ratesTable.item(row, 1).setText(text)
        self.signalsInfo.setText(
            f'direction: {direction}   filter: {"on" if filter_on else "off"}   overflows: {overflows}')

    def setSignalsText(self, text: str):
        self.signalsText.setPlainText(text)

    def appendSignalsText(self, text: str):
        self.signalsText.appendPlainText(text)

    def setPreflight(self, items):
        """``items``: ``(status, line)`` pairs; status ok/warn/fail/skipped."""
        colours = {'ok': '#2a2', 'warn': '#c60', 'fail': '#c33', 'skipped': 'grey'}
        self.signalsText.clear()
        cursor = self.signalsText.textCursor()
        for status, line in items:
            fmt = QtGui.QTextCharFormat()
            fmt.setForeground(QtGui.QColor(colours.get(status, 'grey')))
            cursor.insertText(line + '\n', fmt)
        self.signalsText.setTextCursor(cursor)


class FLIMHistWidget(LifetimeWidget):
    """``FLIMHist`` in a setup file: the Lifetime widget on its FLIM view."""

    initialMode = 'FLIM'
