"""The SimplePointScan panel (docs/simple-point-scan-plan.md §5.3).

A view: it shows the controller's state and turns every edit into a
:class:`~imswitch.imcontrol.model.simple_scan.SimpleScanPlan` it emits. The
rules -- shortest dwell, snapping, overview planning, estimates -- live in the
controller and model, where they are tested without a GUI.
"""

from __future__ import annotations

import dataclasses

import numpy as np
from qtpy import QtCore, QtWidgets

from imswitch.imcommon.view.guitools import colorutils
from imswitch.imcommon.view.guitools.naparitools import worldEdgeWidth
from imswitch.imcontrol.model.simple_scan import (
    AxisRegion,
    SimpleScanPlan,
    log_position,
    log_value,
    snap_length_um,
)
from .ScanWidgetBase import SuperScanWidget

_SLIDER_TICKS = 1000
_MAX_DIMS = 3


def _formatLength(um: float) -> str:
    return f'{um * 1000:.0f} nm' if um < 1.0 else f'{um:.3g} µm'


def _formatTime(seconds) -> str:
    if seconds is None:
        return '–'
    if seconds < 1e-3:
        return f'{seconds * 1e6:.0f} µs'
    if seconds < 1.0:
        return f'{seconds * 1e3:.0f} ms'
    if seconds < 60.0:
        return f'{seconds:.1f} s'
    if seconds < 3600.0:
        return f'{seconds / 60:.1f} min'
    return f'{seconds / 3600:.1f} h'


class ScanRegionOverlay:
    """The panel's rectangle in the napari viewer (plan F6, §5.3).

    Its own Shapes layer: the shared "Viewer Tools" layer is cleared on every
    tool click and read by other panels. The layer sits at scale 1, in world
    coordinates, which for a live point-scan layer (no translate) are the
    micrometres :mod:`~imswitch.imcontrol.controller.scan_region_mapping`
    converts. One rectangle at a time; what it means is the controller's.
    """

    LAYER_NAME = 'Scan region'
    _EDGE = '#00e5ff'
    _FACE = (0.0, 0.9, 1.0, 0.06)
    _SCREEN_PIXELS = 2

    def __init__(self, viewer, onDrawn):
        self._viewer = viewer
        self._onDrawn = onDrawn
        self._layer = None
        self._corners = None
        self._updating = False
        self._pending = False
        try:
            viewer.camera.events.zoom.connect(self._onZoom)
        except Exception:
            pass

    @property
    def layer(self):
        return self._layer

    def _edgeWidth(self):
        return worldEdgeWidth(self._viewer, self._SCREEN_PIXELS)

    def _ensureLayer(self):
        layers = self._viewer.layers
        if self._layer is not None and self._layer in layers:
            return self._layer
        active = layers.selection.active
        self._layer = self._viewer.add_shapes(
            name=self.LAYER_NAME, edge_color=self._EDGE, face_color=list(self._FACE),
            edge_width=self._edgeWidth(),
        )
        self._layer.events.data.connect(self._onData)
        if active is not None and active in layers:
            layers.selection.active = active   # showing it must not take the selection
        return self._layer

    def show(self, corners):
        """Show the rectangle with these world corners [row, col], or none."""
        corners = None if corners is None else np.asarray(corners, dtype=float)
        if corners is None and self._layer is None:
            return
        layer = self._ensureLayer()
        if (corners is not None and self._corners is not None and len(layer.data) == 1
                and corners.shape == self._corners.shape
                and np.allclose(corners, self._corners)):
            return
        self._corners = corners
        self._updating = True
        try:
            layer.data = []
            if corners is not None:
                layer.add_rectangles(
                    [corners], edge_color=self._EDGE, face_color=list(self._FACE),
                    edge_width=self._edgeWidth(),
                )
                layers = self._viewer.layers
                index = layers.index(layer)
                if index != len(layers) - 1:
                    layers.move(index, len(layers))    # above the live layers
        finally:
            self._updating = False

    def startDrawing(self):
        layer = self._ensureLayer()
        self._viewer.layers.selection.active = layer
        layer.mode = 'add_rectangle'

    def _onData(self, event):
        if self._updating:
            return
        action = getattr(event, 'action', None)
        if getattr(action, 'value', action) not in ('added', 'changed'):
            return
        # After napari has finished its own handling of the mouse release.
        if not self._pending:
            self._pending = True
            QtCore.QTimer.singleShot(0, self._emitDrawn)

    def _emitDrawn(self):
        self._pending = False
        layer = self._layer
        if layer is None or layer not in self._viewer.layers or not len(layer.data):
            return
        vertices = np.asarray(layer.data[-1], dtype=float)[:, -2:]
        if str(layer.mode).startswith('add'):
            layer.mode = 'select'          # move or resize it next, not draw another
        self._corners = None               # whatever the controller answers is new
        self._onDrawn(vertices.tolist())

    def _onZoom(self, _event=None):
        layer = self._layer
        if layer is None or layer not in self._viewer.layers or not len(layer.data):
            return
        self._updating = True
        try:
            layer.edge_width = [self._edgeWidth()] * len(layer.data)
        finally:
            self._updating = False


class ScanWidgetSimplePointScan(SuperScanWidget):
    sigModeRequested = QtCore.Signal(str)
    sigAcquisitionEdited = QtCore.Signal(object)       # SimpleScanPlan
    sigOverviewChannelChanged = QtCore.Signal(int)
    sigStopClicked = QtCore.Signal()
    sigDrawRegionClicked = QtCore.Signal()
    sigRegionDrawn = QtCore.Signal(object)             # world vertices [[row, col], ...]
    sigReferenceDetectorChanged = QtCore.Signal(str)

    def __init__(self, *args, napariViewer=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._regionOverlay = (
            ScanRegionOverlay(napariViewer, self.sigRegionDrawn.emit)
            if napariViewer is not None else None
        )
        self._axes = []            # (name, label, smooth)
        self._gates = []           # (name, wavelength_nm, power_capable)
        self._overviewAxes = ()
        self._plan = None          # the acquisition plan last shown
        self._lanes = []           # [[laser, ...], ...]
        self._selectedLane = 0
        self._pixelRange = (1.0, 0.1)
        self._dwellRange = (2e-5, 1e-2)
        self._running = False
        self._updating = False
        self._buildUi()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _group(self, title):
        box = QtWidgets.QGroupBox(title)
        layout = QtWidgets.QGridLayout(box)
        layout.setContentsMargins(6, 6, 6, 6)
        return box, layout

    def _buildUi(self):
        row = 0
        # --- mode and start/stop ---
        self.overviewButton = QtWidgets.QPushButton('Overview')
        self.acquisitionButton = QtWidgets.QPushButton('Acquisition')
        for button in (self.overviewButton, self.acquisitionButton):
            button.setCheckable(True)
        self.overviewButton.setToolTip(
            'Look around: the whole field, live, big pixels, fastest safe dwell.')
        self.acquisitionButton.setToolTip(
            'Take the image: the chosen region, pixel size and dwell.')
        self.overviewButton.clicked.connect(lambda: self.sigModeRequested.emit('overview'))
        self.acquisitionButton.clicked.connect(lambda: self.sigModeRequested.emit('acquisition'))
        self.repeatBox.setText('Live')
        self.repeatBox.setToolTip('Repeat the scan until Stop.')
        self.scanButton.setText('Start')
        self.stopButton = QtWidgets.QPushButton('Stop')
        self.stopButton.setToolTip(
            'No further frame. A frame already running completes.')
        self.stopButton.clicked.connect(self.sigStopClicked)
        self.stopButton.setEnabled(False)
        modeRow = QtWidgets.QHBoxLayout()
        for widget in (self.overviewButton, self.acquisitionButton):
            modeRow.addWidget(widget)
        modeRow.addStretch(1)
        for widget in (self.repeatBox, self.scanButton, self.stopButton):
            modeRow.addWidget(widget)
        self.grid.addLayout(modeRow, row, 0)
        row += 1

        # --- overview ---
        box, layout = self._group('Overview')
        self.overviewLabel = QtWidgets.QLabel('–')
        self.overviewLabel.setWordWrap(True)
        self.overviewChannelCombo = QtWidgets.QComboBox()
        self.overviewChannelCombo.setToolTip('Which channel the overview fires.')
        self.overviewChannelCombo.currentIndexChanged.connect(self._onOverviewChannel)
        self.measuredLabel = QtWidgets.QLabel('')
        layout.addWidget(self.overviewLabel, 0, 0, 1, 3)
        layout.addWidget(QtWidgets.QLabel('Overview uses'), 1, 0)
        layout.addWidget(self.overviewChannelCombo, 1, 1)
        layout.addWidget(self.measuredLabel, 1, 2)
        self.grid.addWidget(box, row, 0)
        row += 1

        # --- region and dimensions ---
        box, layout = self._group('Region')
        self.drawButton = QtWidgets.QPushButton('Draw in viewer')
        self.drawButton.setToolTip('Draw the acquisition region on the live image.')
        self.drawButton.setEnabled(False)
        self.drawButton.clicked.connect(self._startDrawing)
        self.referenceLabel = QtWidgets.QLabel('on')
        self.referenceCombo = QtWidgets.QComboBox()
        self.referenceCombo.setToolTip(
            'The detector whose live image the rectangle is drawn on.')
        self.referenceCombo.currentIndexChanged.connect(self._onReferenceChosen)
        self.dimsSpin = QtWidgets.QSpinBox()
        self.dimsSpin.setRange(1, _MAX_DIMS)
        self.dimsSpin.setToolTip('How many axes to scan, fast axis first.')
        self.dimsSpin.valueChanged.connect(self._onDimsCount)
        layout.addWidget(QtWidgets.QLabel('Dimensions'), 0, 0)
        layout.addWidget(self.dimsSpin, 0, 1)
        layout.addWidget(self.drawButton, 0, 2)
        layout.addWidget(self.referenceLabel, 0, 3, QtCore.Qt.AlignRight)
        layout.addWidget(self.referenceCombo, 0, 4)
        for column, title in enumerate(('Axis', 'Centre (µm)', 'Size (µm)', 'Step (µm)', 'Pixels')):
            layout.addWidget(QtWidgets.QLabel(title), 1, column)
        self._dimRows = []
        for index in range(_MAX_DIMS):
            combo = QtWidgets.QComboBox()
            centre, size, step = (QtWidgets.QDoubleSpinBox() for _ in range(3))
            for spin in (centre, size, step):
                spin.setDecimals(3)
                spin.setRange(-100000.0, 100000.0)
                spin.setKeyboardTracking(False)
            size.setMinimum(0.001)
            step.setMinimum(0.001)
            pixels = QtWidgets.QLabel('')
            combo.currentIndexChanged.connect(lambda _=None, i=index: self._onAxisChosen(i))
            for spin in (centre, size, step):
                spin.valueChanged.connect(self._emitEdited)
            for column, widget in enumerate((combo, centre, size, step, pixels)):
                layout.addWidget(widget, 2 + index, column)
            self._dimRows.append((combo, centre, size, step, pixels))
        self.grid.addWidget(box, row, 0)
        row += 1

        # --- sampling ---
        box, layout = self._group('Sampling')
        self.pixelSlider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.dwellSlider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        for slider in (self.pixelSlider, self.dwellSlider):
            slider.setRange(0, _SLIDER_TICKS)
            slider.valueChanged.connect(self._onSlider)
        self.pixelSlider.setToolTip('Left: overview pixel size. Right: Nyquist.')
        self.dwellSlider.setToolTip('Left: the shortest safe dwell. Right: the longest.')
        self.pixelLabel = QtWidgets.QLabel('')
        self.dwellLabel = QtWidgets.QLabel('')
        layout.addWidget(QtWidgets.QLabel('Pixel size'), 0, 0)
        layout.addWidget(QtWidgets.QLabel('overview'), 0, 1)
        layout.addWidget(self.pixelSlider, 0, 2)
        layout.addWidget(QtWidgets.QLabel('Nyquist'), 0, 3)
        layout.addWidget(self.pixelLabel, 0, 4)
        layout.addWidget(QtWidgets.QLabel('Dwell time'), 1, 0)
        layout.addWidget(QtWidgets.QLabel('fastest'), 1, 1)
        layout.addWidget(self.dwellSlider, 1, 2)
        self.dwellMaxLabel = QtWidgets.QLabel('')
        layout.addWidget(self.dwellMaxLabel, 1, 3)
        layout.addWidget(self.dwellLabel, 1, 4)
        self.frameTimeLabel = QtWidgets.QLabel('–')
        font = self.frameTimeLabel.font()
        font.setPointSizeF(font.pointSizeF() * 1.4)
        font.setBold(True)
        self.frameTimeLabel.setFont(font)
        self.estimateNote = QtWidgets.QLabel('')
        self.estimateNote.setWordWrap(True)
        layout.addWidget(QtWidgets.QLabel('Scan time'), 2, 0)
        layout.addWidget(self.frameTimeLabel, 2, 1, 1, 2)
        layout.addWidget(self.estimateNote, 3, 0, 1, 5)
        self.grid.addWidget(box, row, 0)
        row += 1

        # --- channels ---
        box, layout = self._group('Lasers and channels')
        self.paletteLayout = QtWidgets.QHBoxLayout()
        layout.addLayout(self.paletteLayout, 0, 0, 1, 3)
        self.lanesLayout = QtWidgets.QVBoxLayout()
        layout.addLayout(self.lanesLayout, 1, 0, 1, 3)
        self.addLaneButton = QtWidgets.QPushButton('Add channel')
        self.togetherButton = QtWidgets.QPushButton('All together')
        self.separateButton = QtWidgets.QPushButton('One per laser')
        self.addLaneButton.setToolTip('A new line pass; click a laser to put it in the selected channel.')
        self.togetherButton.setToolTip('Every laser in one line pass (fired together).')
        self.separateButton.setToolTip('Each laser in its own line pass (recorded separately).')
        self.addLaneButton.clicked.connect(self._addLane)
        self.togetherButton.clicked.connect(self._allTogether)
        self.separateButton.clicked.connect(self._onePerLaser)
        buttons = QtWidgets.QHBoxLayout()
        for button in (self.addLaneButton, self.togetherButton, self.separateButton):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons, 2, 0, 1, 3)
        self.detectorNote = QtWidgets.QLabel('')
        self.detectorNote.setWordWrap(True)
        layout.addWidget(self.detectorNote, 3, 0, 1, 3)
        self.grid.addWidget(box, row, 0)
        row += 1

        # --- expert ---
        self.expertToggle = QtWidgets.QToolButton()
        self.expertToggle.setText('Expert')
        self.expertToggle.setCheckable(True)
        self.expertToggle.setArrowType(QtCore.Qt.RightArrow)
        self.expertToggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.expertBox = QtWidgets.QWidget()
        expert = QtWidgets.QGridLayout(self.expertBox)
        self.phaseDelaySpin = QtWidgets.QDoubleSpinBox()
        self.sliceDelaySpin = QtWidgets.QDoubleSpinBox()
        for spin in (self.phaseDelaySpin, self.sliceDelaySpin):
            spin.setRange(0.0, 1e7)
            spin.setDecimals(1)
            spin.setSuffix(' µs')
            spin.setKeyboardTracking(False)
            spin.valueChanged.connect(self._emitEdited)
        expert.addWidget(QtWidgets.QLabel('Phase delay'), 0, 0)
        expert.addWidget(self.phaseDelaySpin, 0, 1)
        expert.addWidget(QtWidgets.QLabel('Slice delay'), 1, 0)
        expert.addWidget(self.sliceDelaySpin, 1, 1)
        expert.addWidget(self.saveScanBtn, 2, 0)
        expert.addWidget(self.loadScanBtn, 2, 1)
        self.expertBox.setVisible(False)
        self.expertToggle.toggled.connect(self._onExpertToggled)
        self.grid.addWidget(self.expertToggle, row, 0)
        row += 1
        self.grid.addWidget(self.expertBox, row, 0)
        row += 1

        self.messageLabel = QtWidgets.QLabel('')
        self.messageLabel.setWordWrap(True)
        self.grid.addWidget(self.messageLabel, row, 0)
        row += 1
        self.grid.setRowStretch(row, 1)

    def _onExpertToggled(self, shown):
        self.expertBox.setVisible(shown)
        self.expertToggle.setArrowType(QtCore.Qt.DownArrow if shown else QtCore.Qt.RightArrow)

    # ------------------------------------------------------------------
    # SuperScanWidget contract
    # ------------------------------------------------------------------

    def initControls(self, positionerNames, TTLDeviceNames, TTLTimeUnits=None):
        """Names only; the controller configures the panel with more."""
        self._positionerNames = list(positionerNames)

    def getTTLIncluded(self, deviceName):
        return bool(self._plan) and deviceName in self._plan.lasers()

    def setScanMode(self):
        pass

    def isScanMode(self):
        return True

    def isContLaserMode(self):
        return False

    def setContLaserMode(self):
        pass

    def getSeqTimePar(self):
        return self._plan.dwell_s if self._plan else self._dwellRange[0]

    def setSeqTimePar(self, seqTimePar):
        pass

    def unsetTTL(self, deviceName):
        pass

    def setScanButtonChecked(self, checked):
        super().setScanButtonChecked(checked)
        self._running = bool(checked)
        self.stopButton.setEnabled(self._running)
        for button in (self.overviewButton, self.acquisitionButton):
            button.setEnabled(not self._running)
        if not self._running:
            self.measuredLabel.setText('')

    def getScanCenterPos(self, positionerName):
        region = (self._plan.regions.get(positionerName) if self._plan else None)
        return region.center_um if region else 0.0

    def setScanCenterPos(self, positionerName, centerPos):
        self._editRegion(positionerName, center_um=float(centerPos))

    def setScanSize(self, positionerName, size):
        self._editRegion(positionerName, length_um=float(size))

    def _editRegion(self, positionerName, **changes):
        if not self._plan or positionerName not in self._plan.regions:
            return
        regions = dict(self._plan.regions)
        regions[positionerName] = dataclasses.replace(regions[positionerName], **changes)
        self.sigAcquisitionEdited.emit(dataclasses.replace(self._plan, regions=regions))

    # ------------------------------------------------------------------
    # From the controller
    # ------------------------------------------------------------------

    def configureSimpleScan(self, *, axes, gates, overviewAxes, detectorNote,
                            detectors=()):
        self._axes = list(axes)
        self._gates = list(gates)
        self._overviewAxes = tuple(overviewAxes)
        self._updating = True
        try:
            for combo, *_ in self._dimRows:
                combo.clear()
                for name, label, _smooth in self._axes:
                    combo.addItem(label if label == name else f'{label} ({name})', name)
            self._buildPalette()
            self.referenceCombo.clear()
            for name in detectors:
                self.referenceCombo.addItem(name, name)
        finally:
            self._updating = False
        # A choice only when there is one to make.
        self.referenceLabel.setVisible(len(detectors) > 1)
        self.referenceCombo.setVisible(len(detectors) > 1)
        self.detectorNote.setText(detectorNote)

    # ------------------------------------------------------------------
    # The rectangle in the viewer
    # ------------------------------------------------------------------

    def hasViewer(self) -> bool:
        return self._regionOverlay is not None

    def setReferenceDetector(self, name):
        self._updating = True
        try:
            self.referenceCombo.setCurrentIndex(max(0, self.referenceCombo.findData(name)))
        finally:
            self._updating = False

    def setDrawing(self, available: bool, reason: str = ''):
        """Whether a rectangle can be drawn now; ``reason`` says why not."""
        available = bool(available) and self.hasViewer()
        self.drawButton.setEnabled(available)
        self.drawButton.setToolTip(
            'Draw the acquisition region on the live image.' if available else reason)

    def showRegion(self, corners):
        """The rectangle at these world corners [row, col], or none."""
        if self._regionOverlay is not None:
            self._regionOverlay.show(corners)

    def _startDrawing(self):
        if self._regionOverlay is None:
            return
        self._regionOverlay.startDrawing()
        self.showMessage('Drag a rectangle on the live image.')
        self.sigDrawRegionClicked.emit()

    def _onReferenceChosen(self, index):
        if not self._updating and index >= 0:
            self.sigReferenceDetectorChanged.emit(self.referenceCombo.itemData(index))

    def setSimpleScanState(self, *, mode, overview, overviewChannel, acquisition,
                           pixelRange, dwellRange):
        self._updating = True
        try:
            self._plan = acquisition
            self._pixelRange = tuple(pixelRange)
            self._dwellRange = tuple(dwellRange)
            self.overviewButton.setChecked(mode == 'overview')
            self.acquisitionButton.setChecked(mode == 'acquisition')
            self.repeatBox.setEnabled(mode == 'acquisition' and not self._running)
            self._showOverview(overview)
            self.overviewChannelCombo.clear()
            for index in range(max(1, len(acquisition.channels))):
                self.overviewChannelCombo.addItem(f'Channel {index + 1}')
            self.overviewChannelCombo.setCurrentIndex(
                min(overviewChannel, self.overviewChannelCombo.count() - 1))
            self._showAcquisition(acquisition)
        finally:
            self._updating = False

    def setEstimate(self, frameS, totalS, note, refusal):
        if refusal:
            self.frameTimeLabel.setText('–')
            self.estimateNote.setText(refusal)
            self.estimateNote.setStyleSheet('color: #d9534f;')
            return
        mode = 'overview' if self.overviewButton.isChecked() else 'acquisition'
        text = f'≈ {_formatTime(frameS)}'
        if mode == 'overview' and frameS:
            text += f'  ({1.0 / frameS:.2g} frames/s)'
        self.frameTimeLabel.setText(text)
        self.estimateNote.setText(' '.join(filter(None, [
            note, 'Turnarounds included; the re-arm between frames is not.',
        ])))
        self.estimateNote.setStyleSheet('')

    def setMeasuredRate(self, framesPerSecond):
        self.measuredLabel.setText(
            f'measured {framesPerSecond:.2g} frames/s' if framesPerSecond else '')

    def showMessage(self, text, error=False):
        self.messageLabel.setText(text)
        self.messageLabel.setStyleSheet('color: #d9534f;' if error else '')

    # ------------------------------------------------------------------
    # Showing state
    # ------------------------------------------------------------------

    def _showOverview(self, overview):
        if overview is None:
            self.overviewLabel.setText('–')
            return
        text = (f'Full field {overview.field_um:.3g} × {overview.field_um:.3g} µm · '
                f'{overview.pixels} × {overview.pixels} px of '
                f'{_formatLength(overview.step_um)} · dwell {_formatTime(overview.dwell_s)} · '
                f'≈ {_formatTime(overview.estimate_s)} per frame')
        if overview.note:
            text += f'\n{overview.note}'
        self.overviewLabel.setText(text)
        self.overviewLabel.setStyleSheet('' if overview.met else 'color: #f0ad4e;')

    def _showAcquisition(self, plan):
        dims = list(plan.dims)
        self.dimsSpin.setValue(max(1, len(dims)))
        for index, (combo, centre, size, step, pixels) in enumerate(self._dimRows):
            visible = index < len(dims)
            for widget in (combo, centre, size, step, pixels):
                widget.setVisible(visible)
            if not visible:
                continue
            name = dims[index]
            region = plan.regions[name]
            combo.setCurrentIndex(max(0, combo.findData(name)))
            centre.setValue(region.center_um)
            size.setValue(region.length_um)
            step.setValue(region.step_um)
            step.setEnabled(name not in self._overviewAxes)
            pixels.setText(str(region.pixels))
        xy = [plan.regions[d].step_um for d in dims if d in self._overviewAxes]
        coarse, fine = self._pixelRange
        if xy:
            self.pixelSlider.setValue(round(
                log_position(min(max(xy[0], fine), coarse), coarse, fine) * _SLIDER_TICKS))
        low, high = self._dwellRange
        self.dwellSlider.setValue(round(
            log_position(min(max(plan.dwell_s, low), high), low, high) * _SLIDER_TICKS))
        self.dwellMaxLabel.setText(_formatTime(high))
        self._showSamplingReadouts(plan)
        self.phaseDelaySpin.setValue(plan.phase_delay_us)
        self.sliceDelaySpin.setValue(plan.d3step_delay_us)
        lanes = [list(lane) for lane in plan.channels]
        if lanes != self.__dict__.get('_shownLanes'):
            self._lanes = lanes
            self._selectedLane = min(self._selectedLane, max(0, len(self._lanes) - 1))
            self._buildLanes()

    def _showSamplingReadouts(self, plan):
        xy = [d for d in plan.dims if d in self._overviewAxes]
        if xy:
            step = plan.regions[xy[0]].step_um
            counts = ' × '.join(str(plan.regions[d].pixels) for d in plan.dims)
            self.pixelLabel.setText(f'{_formatLength(step)} · {counts} px')
        self.dwellLabel.setText(_formatTime(plan.dwell_s))

    # ------------------------------------------------------------------
    # Channels
    # ------------------------------------------------------------------

    def _gateColour(self, wavelength):
        try:
            return colorutils.wavelengthToHex(float(wavelength), gamma=6.0)
        except Exception:
            return '#dddddd'

    def _chip(self, name, wavelength, onClick, tooltip):
        chip = QtWidgets.QPushButton(name)
        colour = self._gateColour(wavelength) if wavelength else '#dddddd'
        chip.setStyleSheet(
            f'QPushButton {{ background-color: {colour}; color: #111; '
            'border-radius: 4px; padding: 2px 8px; }')
        chip.setToolTip(tooltip)
        chip.clicked.connect(onClick)
        return chip

    def _buildPalette(self):
        while self.paletteLayout.count():
            item = self.paletteLayout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.paletteLayout.addWidget(QtWidgets.QLabel('Lasers'))
        for name, wavelength, _power in self._gates:
            self.paletteLayout.addWidget(self._chip(
                name, wavelength, lambda _=None, n=name: self._putInSelectedLane(n),
                'Put this laser in the selected channel.'))
        self.paletteLayout.addStretch(1)

    def _wavelength(self, name):
        for gate, wavelength, _power in self._gates:
            if gate == name:
                return wavelength
        return None

    def _buildLanes(self):
        self._shownLanes = [list(lane) for lane in self._lanes]
        while self.lanesLayout.count():
            item = self.lanesLayout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for index, lane in enumerate(self._lanes):
            row = QtWidgets.QFrame()
            row.setFrameShape(QtWidgets.QFrame.StyledPanel)
            layout = QtWidgets.QHBoxLayout(row)
            layout.setContentsMargins(4, 2, 4, 2)
            select = QtWidgets.QRadioButton(f'Channel {index + 1}')
            select.setChecked(index == self._selectedLane)
            select.toggled.connect(
                lambda checked, i=index: checked and self._selectLane(i))
            layout.addWidget(select)
            for laser in lane:
                layout.addWidget(self._chip(
                    laser, self._wavelength(laser),
                    lambda _=None, i=index, n=laser: self._removeFromLane(i, n),
                    'Remove from this channel.'))
            layout.addStretch(1)
            layout.addWidget(QtWidgets.QLabel(
                'fired together' if len(lane) > 1 else ('one line pass' if lane else 'empty')))
            self.lanesLayout.addWidget(row)

    def _selectLane(self, index):
        self._selectedLane = index

    def _putInSelectedLane(self, laser):
        if not self._lanes:
            self._lanes = [[]]
            self._selectedLane = 0
        for lane in self._lanes:
            if laser in lane:
                lane.remove(laser)
        self._lanes[self._selectedLane].append(laser)
        self._lanes = [lane for lane in self._lanes if lane] or [[]]
        self._selectedLane = min(self._selectedLane, len(self._lanes) - 1)
        self._emitEdited()

    def _removeFromLane(self, index, laser):
        if laser in self._lanes[index]:
            self._lanes[index].remove(laser)
        if len(self._lanes) > 1:
            self._lanes = [lane for lane in self._lanes if lane]
        self._selectedLane = min(self._selectedLane, max(0, len(self._lanes) - 1))
        self._emitEdited()

    def _addLane(self):
        self._lanes.append([])
        self._selectedLane = len(self._lanes) - 1
        self._buildLanes()

    def _allTogether(self):
        lasers = [laser for lane in self._lanes for laser in lane]
        self._lanes = [lasers]
        self._selectedLane = 0
        self._emitEdited()

    def _onePerLaser(self):
        lasers = [laser for lane in self._lanes for laser in lane]
        self._lanes = [[laser] for laser in lasers] or [[]]
        self._selectedLane = 0
        self._emitEdited()

    # ------------------------------------------------------------------
    # Edits -> plan
    # ------------------------------------------------------------------

    def _onOverviewChannel(self, index):
        if not self._updating and index >= 0:
            self.sigOverviewChannelChanged.emit(index)

    def _onDimsCount(self, count):
        if self._updating or not self._plan:
            return
        dims = list(self._plan.dims)[:count]
        for name, _label, _smooth in self._axes:
            if len(dims) >= count:
                break
            if name not in dims:
                dims.append(name)
        self._emitPlan(self._planWithDims(tuple(dims)))

    def _onAxisChosen(self, index):
        if self._updating or not self._plan:
            return
        dims = list(self._plan.dims)
        chosen = self._dimRows[index][0].currentData()
        if chosen is None or index >= len(dims):
            return
        if chosen in dims:
            other = dims.index(chosen)
            dims[other] = dims[index]
        dims[index] = chosen
        self._emitPlan(self._planWithDims(tuple(dims)))

    def _planWithDims(self, dims):
        regions = dict(self._plan.regions)
        for name in dims:
            if name not in regions:
                step = 0.5
                regions[name] = AxisRegion(0.0, snap_length_um(5.0, step), step)
        return dataclasses.replace(
            self._plan, dims=dims,
            regions={name: regions[name] for name in dims},
            park={k: v for k, v in self._plan.park.items() if k not in dims},
        )

    def _onSlider(self, _value):
        if self._updating or not self._plan:
            return
        self._emitEdited()

    def _emitEdited(self, *_):
        if self._updating or not self._plan:
            return
        self._emitPlan(self._planFromControls())

    def _emitPlan(self, plan):
        self._plan = plan
        self._showSamplingReadouts(plan)
        self.sigAcquisitionEdited.emit(plan)

    def _planFromControls(self) -> SimpleScanPlan:
        base = self._plan
        coarse, fine = self._pixelRange
        xyStep = log_value(self.pixelSlider.value() / _SLIDER_TICKS, coarse, fine)
        low, high = self._dwellRange
        dwell = log_value(self.dwellSlider.value() / _SLIDER_TICKS, low, high)
        regions = {}
        for index, name in enumerate(base.dims):
            _combo, centre, size, stepSpin, _pixels = self._dimRows[index]
            step = xyStep if name in self._overviewAxes else stepSpin.value()
            previous = base.regions.get(name)
            if (previous is not None
                    and abs(size.value() - round(previous.length_um, 3)) < 1e-9
                    and abs(step - previous.step_um) < 1e-12):
                length = previous.length_um      # an imported length stays verbatim
            else:
                length = snap_length_um(size.value(), step)
            regions[name] = AxisRegion(centre.value(), length, step)
        return dataclasses.replace(
            base,
            regions=regions,
            dwell_s=dwell,
            channels=tuple(tuple(lane) for lane in self._lanes if lane),
            phase_delay_us=self.phaseDelaySpin.value(),
            d3step_delay_us=self.sliceDelaySpin.value(),
        )


# Copyright (C) 2020-2026 ImSwitch developers
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
