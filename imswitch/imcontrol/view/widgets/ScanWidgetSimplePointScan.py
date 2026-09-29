"""The SimplePointScan panel (docs/simple-point-scan-plan.md §5.3, §10).

A cloak over the Advanced scan panel: :class:`ScanWidgetSimplePointScan` is
the Scan dock, holding the simple page (:class:`PointScanView`) and the real
``ScanWidgetAdvanced``, one switch apart.

The simple page is a view: it shows the controller's state and turns every
edit into a :class:`~imswitch.imcontrol.model.simple_scan.SimpleScanPlan` it
emits. The rules -- shortest dwell, snapping, overview planning, estimates --
live in the controller and model, where they are tested without a GUI.
"""

from __future__ import annotations

import dataclasses
import json

import numpy as np
from qtpy import QtCore, QtGui, QtWidgets

from imswitch.imcommon.view.guitools import colorutils
from imswitch.imcommon.view.guitools.naparitools import worldEdgeWidth
from imswitch.imcontrol.model.simple_scan import (
    AxisRegion,
    SimpleScanPlan,
    log_position,
    log_value,
    snap_length_um,
)
from .ScanCloakPanel import ScanCloakPanel, ScanCloakView
from .ScanWidgetAdvanced import ScanWidgetAdvanced

_SLIDER_TICKS = 1000
_MAX_DIMS = 3
_LASER_MIME = 'application/x-imswitch-laser'
_ACQUISITION_ONLY_TIP = 'Used by the acquisition. Switch to Acquisition to change it.'


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


def _laserMime(laser, lane):
    mime = QtCore.QMimeData()
    mime.setData(_LASER_MIME, QtCore.QByteArray(
        json.dumps({'laser': laser, 'lane': lane}).encode()))
    return mime


def _laserFromMime(mime):
    """(laser, lane it came from or None) carried by a drag, else None."""
    if mime is None or not mime.hasFormat(_LASER_MIME):
        return None
    try:
        data = json.loads(bytes(mime.data(_LASER_MIME)).decode())
        return str(data['laser']), data.get('lane')
    except (ValueError, KeyError, TypeError):
        return None


class _LaserChip(QtWidgets.QFrame):
    """A laser's label, dragged onto a channel.

    From the palette it adds the laser to where it is dropped; from a channel
    it moves it there. A channel's chip has a × that removes it.
    """

    def __init__(self, name, colour, *, lane=None, onRemove=None, onClick=None,
                 onDragging=None, tooltip=''):
        super().__init__()
        self.laser = name
        self.lane = lane
        self._onClick = onClick
        self._onDragging = onDragging
        self._pressPos = None
        self.setObjectName('laserChip')
        self.setStyleSheet(
            f'QFrame#laserChip {{ background-color: {colour}; border-radius: 4px; }}'
            ' QFrame#laserChip QLabel, QFrame#laserChip QToolButton'
            ' { color: #111; background: transparent; border: none; }')
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 2 if onRemove else 8, 2)
        layout.setSpacing(2)
        layout.addWidget(QtWidgets.QLabel(name))
        self.removeButton = None
        if onRemove is not None:
            self.removeButton = QtWidgets.QToolButton()
            self.removeButton.setText('×')
            self.removeButton.setAutoRaise(True)
            self.removeButton.setToolTip('Remove from this channel.')
            self.removeButton.clicked.connect(onRemove)
            layout.addWidget(self.removeButton)
        self.setToolTip(tooltip)
        self.setCursor(QtCore.Qt.OpenHandCursor)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self._pressPos = event.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pressPos is None or not event.buttons() & QtCore.Qt.LeftButton:
            return
        if ((event.pos() - self._pressPos).manhattanLength()
                < QtWidgets.QApplication.startDragDistance()):
            return
        self._pressPos = None
        drag = QtGui.QDrag(self)
        drag.setMimeData(_laserMime(self.laser, self.lane))
        drag.setPixmap(self.grab())
        drag.setHotSpot(event.pos())
        if self._onDragging is not None:
            self._onDragging(True)
        try:
            drag.exec_(QtCore.Qt.CopyAction | QtCore.Qt.MoveAction)
        finally:
            # A drop lands only now, and rebuilds the channels -- this chip
            # included -- so nothing here may touch the chip afterwards.
            if self._onDragging is not None:
                self._onDragging(False)

    def mouseReleaseEvent(self, event):
        clicked = self._pressPos is not None and event.button() == QtCore.Qt.LeftButton
        self._pressPos = None
        if clicked and self._onClick is not None:
            self._onClick()
            return
        super().mouseReleaseEvent(event)


class _LaserDropZone(QtWidgets.QFrame):
    """Where a dragged laser lands: a channel (its index), the new-channel
    zone (``'new'``), or the palette (``'palette'``: out of its channel)."""

    def __init__(self, target, onDrop, *, dashed=False):
        super().__init__()
        self.target = target
        self._onDrop = onDrop
        self._dashed = dashed
        self.setObjectName('laserDropZone')
        self.setAcceptDrops(True)
        self._highlight(False)

    def _highlight(self, on):
        style = 'dashed' if self._dashed else 'solid'
        colour = '#00b7eb' if on else '#808080'
        self.setStyleSheet(
            f'QFrame#laserDropZone {{ border: 1px {style} {colour}; border-radius: 4px; }}')

    def dragEnterEvent(self, event):
        if _laserFromMime(event.mimeData()) is None:
            event.ignore()
            return
        event.acceptProposedAction()
        self._highlight(True)

    def dragMoveEvent(self, event):
        if _laserFromMime(event.mimeData()) is None:
            event.ignore()
        else:
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self._highlight(False)

    def dropEvent(self, event):
        self._highlight(False)
        payload = _laserFromMime(event.mimeData())
        if payload is None:
            event.ignore()
            return
        event.acceptProposedAction()
        self._onDrop(payload[0], payload[1], self.target)


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


class PointScanView(ScanCloakView):
    """The point-scan panel's simple page."""

    sigModeRequested = QtCore.Signal(str)
    sigAcquisitionEdited = QtCore.Signal(object)       # SimpleScanPlan
    sigOverviewChannelChanged = QtCore.Signal(int)
    sigDrawRegionClicked = QtCore.Signal()
    sigRegionDrawn = QtCore.Signal(object)             # world vertices [[row, col], ...]
    sigReferenceDetectorChanged = QtCore.Signal(str)

    def __init__(self, parent=None, *, napariViewer=None):
        super().__init__(parent)
        self._regionOverlay = (
            ScanRegionOverlay(napariViewer, self.sigRegionDrawn.emit)
            if napariViewer is not None else None
        )
        self._axes = []            # (name, label, smooth)
        self._gates = []           # (name, wavelength_nm, power_capable)
        self._overviewAxes = ()
        self._plan = None          # the acquisition plan last shown
        self._lanes = []           # [[laser, ...], ...]: a laser may be in several
        self._dragging = False
        self._pendingDrop = None
        self._pixelRange = (1.0, 0.1)
        self._dwellRange = (2e-5, 1e-2)
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
        # --- mode, in front of the base's Live / Start / Stop ---
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
        self.runRow.insertWidget(0, self.overviewButton)
        self.runRow.insertWidget(1, self.acquisitionButton)

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
        self.content.addWidget(box)

        # --- region and dimensions ---
        box, outer = self._group('Region')
        self.regionBox = box
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
        drawRow = QtWidgets.QHBoxLayout()
        drawRow.addWidget(self.drawButton)
        drawRow.addStretch(1)
        drawRow.addWidget(self.referenceLabel)
        drawRow.addWidget(self.referenceCombo)
        outer.addLayout(drawRow, 0, 0)
        # The numbers: greyed in Overview, which scans its own field.
        self.regionControls = QtWidgets.QWidget()
        layout = QtWidgets.QGridLayout(self.regionControls)
        layout.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.regionControls, 1, 0)
        layout.addWidget(QtWidgets.QLabel('Dimensions'), 0, 0)
        layout.addWidget(self.dimsSpin, 0, 1)
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
        self.content.addWidget(box)

        # --- sampling ---
        box, layout = self._group('Sampling')
        self.samplingBox = box
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
        self.content.addWidget(box)

        # --- channels ---
        box, layout = self._group('Lasers and channels')
        # Dropping a channel's laser back here takes it out of that channel.
        self.paletteZone = _LaserDropZone('palette', self._onLaserDropped)
        self.paletteZone.setToolTip(
            'Drag a laser onto a channel. Drag one out of a channel back here to remove it.')
        self.paletteLayout = QtWidgets.QHBoxLayout(self.paletteZone)
        self.paletteLayout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.paletteZone, 0, 0, 1, 3)
        self.lanesLayout = QtWidgets.QVBoxLayout()
        layout.addLayout(self.lanesLayout, 1, 0, 1, 3)
        self.togetherButton = QtWidgets.QPushButton('All together')
        self.separateButton = QtWidgets.QPushButton('One per laser')
        self.togetherButton.setToolTip('Every laser in one line pass (fired together).')
        self.separateButton.setToolTip('Each laser in its own line pass (recorded separately).')
        self.togetherButton.clicked.connect(self._allTogether)
        self.separateButton.clicked.connect(self._onePerLaser)
        buttons = QtWidgets.QHBoxLayout()
        for button in (self.togetherButton, self.separateButton):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons, 2, 0, 1, 3)
        self.detectorNote = QtWidgets.QLabel('')
        self.detectorNote.setWordWrap(True)
        layout.addWidget(self.detectorNote, 3, 0, 1, 3)
        self.content.addWidget(box)

    def _runningChanged(self, running):
        for button in (self.overviewButton, self.acquisitionButton):
            button.setEnabled(not running)
        if not running:
            self.measuredLabel.setText('')

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
            self.liveBox.setEnabled(mode == 'acquisition' and not self.isRunning())
            self._showModeControls(mode)
            self._showOverview(overview)
            self.overviewChannelCombo.clear()
            for index in range(max(1, len(acquisition.channels))):
                self.overviewChannelCombo.addItem(f'Channel {index + 1}')
            self.overviewChannelCombo.setCurrentIndex(
                min(overviewChannel, self.overviewChannelCombo.count() - 1))
            self._showAcquisition(acquisition)
        finally:
            self._updating = False

    def _showModeControls(self, mode):
        """Region numbers and sampling only drive the acquisition: in
        Overview they are greyed (drawing the region still works)."""
        acquisition = mode == 'acquisition'
        self.regionControls.setEnabled(acquisition)
        self.samplingBox.setEnabled(acquisition)
        tip = '' if acquisition else _ACQUISITION_ONLY_TIP
        self.regionControls.setToolTip(tip)
        self.samplingBox.setToolTip(tip)
        self.regionBox.setTitle('Region' if acquisition else 'Region (for the acquisition)')
        self.samplingBox.setTitle('Sampling' if acquisition else 'Sampling (for the acquisition)')
        self.estimateTitle.setText('Scan time' if acquisition else 'Frame time')

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
        lanes = [list(lane) for lane in plan.channels]
        if lanes != self.__dict__.get('_shownLanes'):
            self._lanes = lanes
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

    def _buildPalette(self):
        while self.paletteLayout.count():
            item = self.paletteLayout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.paletteLayout.addWidget(QtWidgets.QLabel('Lasers'))
        for name, wavelength, _power in self._gates:
            self.paletteLayout.addWidget(_LaserChip(
                name, self._gateColour(wavelength) if wavelength else '#dddddd',
                onClick=lambda n=name: self._dropLaser(n, None, 'last'),
                onDragging=self._setDragging,
                tooltip='Drag onto a channel, or onto "new channel". '
                        'A click adds it to the last channel.'))
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
        self.laneZones = []
        for index, lane in enumerate(self._lanes):
            zone = _LaserDropZone(index, self._onLaserDropped)
            layout = QtWidgets.QHBoxLayout(zone)
            layout.setContentsMargins(6, 3, 6, 3)
            layout.addWidget(QtWidgets.QLabel(f'Channel {index + 1}'))
            for laser in lane:
                wavelength = self._wavelength(laser)
                layout.addWidget(_LaserChip(
                    laser, self._gateColour(wavelength) if wavelength else '#dddddd',
                    lane=index,
                    onRemove=lambda _=None, i=index, n=laser: self._dropLaser(n, i, 'palette'),
                    onDragging=self._setDragging,
                    tooltip='Drag to another channel to move it.'))
            layout.addStretch(1)
            layout.addWidget(QtWidgets.QLabel(
                'fired together' if len(lane) > 1 else 'one line pass'))
            self.lanesLayout.addWidget(zone)
            self.laneZones.append(zone)
        self.newChannelZone = _LaserDropZone('new', self._onLaserDropped, dashed=True)
        layout = QtWidgets.QHBoxLayout(self.newChannelZone)
        layout.setContentsMargins(6, 6, 6, 6)
        hint = QtWidgets.QLabel(
            'Drop a laser here for a new channel' if self._lanes
            else 'Drop a laser here: lasers in one channel fire together')
        hint.setAlignment(QtCore.Qt.AlignCenter)
        hint.setStyleSheet('color: #808080;')
        layout.addWidget(hint)
        self.lanesLayout.addWidget(self.newChannelZone)

    def _setDragging(self, dragging):
        # A drop rebuilds the channels, and the chip being dragged is one of
        # them: the change waits until its drag has returned.
        self._dragging = dragging
        if not dragging and self._pendingDrop is not None:
            pending, self._pendingDrop = self._pendingDrop, None
            self._dropLaser(*pending)

    def _onLaserDropped(self, laser, fromLane, target):
        if self._dragging:
            self._pendingDrop = (laser, fromLane, target)
        else:
            self._dropLaser(laser, fromLane, target)

    def _dropLaser(self, laser, fromLane, target):
        """``laser`` into channel ``target`` (an index, ``'new'`` or
        ``'last'``), or with ``'palette'`` out of channel ``fromLane``.

        From the palette (``fromLane`` None) it is added; from a channel it
        moves. A channel holds a laser once; a laser may be in several
        channels (405 with 488 in one, with 561 in the next).
        """
        lanes = [list(lane) for lane in self._lanes]
        known = fromLane is not None and 0 <= fromLane < len(lanes)
        if target == 'palette':
            if not known or laser not in lanes[fromLane]:
                return
            lanes[fromLane].remove(laser)
        else:
            if target == 'last':
                target = len(lanes) - 1 if lanes else 'new'
            if target == 'new':
                lanes.append([])
                target = len(lanes) - 1
            if not 0 <= target < len(lanes) or target == fromLane:
                return
            if laser not in lanes[target]:
                lanes[target].append(laser)
            if known and laser in lanes[fromLane]:
                lanes[fromLane].remove(laser)
        self._setLanes([lane for lane in lanes if lane])

    def _setLanes(self, lanes):
        self._lanes = lanes
        self._emitEdited()
        if self._lanes != self.__dict__.get('_shownLanes'):
            self._buildLanes()

    def _uniqueLasers(self):
        return list(dict.fromkeys(laser for lane in self._lanes for laser in lane))

    def _allTogether(self):
        lasers = self._uniqueLasers()
        self._setLanes([lasers] if lasers else [])

    def _onePerLaser(self):
        self._setLanes([[laser] for laser in self._uniqueLasers()])

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
        )



class ScanWidgetSimplePointScan(ScanCloakPanel):
    """The Scan dock: the point-scan page and the Advanced scan panel."""

    backendWidgetClass = ScanWidgetAdvanced
    viewClass = PointScanView
    title = 'Point scan'


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
