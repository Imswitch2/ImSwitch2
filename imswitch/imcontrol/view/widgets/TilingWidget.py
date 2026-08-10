import numpy as np
import pyqtgraph as pg
from qtpy import QtCore, QtWidgets

from imswitch.imcontrol.model.workflows.spiral import SERPENTINE, SPIRAL

from imswitch.imcontrol.view import guitools
from .basewidgets import Widget

#: Acquisition timing models offered by the widget. The mechanism behind
#: 'triggered' depends on the detector, which the controller works out.
MODE_FREE_RUNNING = 'free-running'
MODE_TRIGGERED = 'triggered'


class TilingWidget(Widget):
    """Widget for controlling spiral tiling scans."""

    sigStartTiling = QtCore.Signal()
    sigStopTiling = QtCore.Signal()
    sigParamsChanged = QtCore.Signal()
    sigClickOnOverview = QtCore.Signal(int, int)  # row, col in stitched canvas
    sigTuneSegmentation = QtCore.Signal()
    sigRunCellTargeting = QtCore.Signal()
    sigModeChanged = QtCore.Signal(str)
    sigDetectorChanged = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # One column of labelled blocks over a full-width overview. The
        # controls are grouped by the question they answer — how far to scan,
        # how to acquire, how to lay the tiles down, how to draw them — so a
        # setting can be found by what it does rather than by where it landed.
        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        self.setLayout(layout)

        layout.addWidget(self._buildScanGroup())
        layout.addWidget(self._buildAcquisitionGroup())
        layout.addWidget(self._buildAlignmentGroup())
        layout.addWidget(self._buildDisplayGroup())
        layout.addLayout(self._buildRunRow())
        layout.addWidget(self._buildStatusBlock())
        # The overview is the point of the widget, so it takes every pixel the
        # controls above it do not need, and all of the width.
        layout.addWidget(self._buildOverview(), 1)
        layout.addLayout(self._buildTargetingRow())

        # Wire signals
        self.startButton.clicked.connect(self.sigStartTiling)
        self.stopButton.clicked.connect(self.sigStopTiling)
        self.tuneSegmentationButton.clicked.connect(self.sigTuneSegmentation)
        self.runCellTargetingButton.clicked.connect(self.sigRunCellTargeting)
        self.nTilesXSpinbox.valueChanged.connect(self.sigParamsChanged)
        self.nTilesYSpinbox.valueChanged.connect(self.sigParamsChanged)
        self.patternCombo.currentIndexChanged.connect(self.sigParamsChanged)
        self.tileStepSpinbox.valueChanged.connect(self.sigParamsChanged)
        self.blendOverlapsCheck.stateChanged.connect(self.sigParamsChanged)
        self.intensityCorrectionCheck.stateChanged.connect(self.sigParamsChanged)
        self.settleTimeSpinbox.valueChanged.connect(self.sigParamsChanged)
        self.registerTilesCheck.stateChanged.connect(self.sigParamsChanged)
        self.saveTilesCheck.stateChanged.connect(self.sigParamsChanged)
        self.modeCombo.currentIndexChanged.connect(self._onModeChanged)
        self.scanSourceCombo.currentIndexChanged.connect(self.sigParamsChanged)
        self.detectorCombo.currentIndexChanged.connect(self.sigParamsChanged)
        self.detectorCombo.currentIndexChanged.connect(self.sigDetectorChanged)
        self.flipXCheck.stateChanged.connect(self.sigParamsChanged)
        self.flipYCheck.stateChanged.connect(self.sigParamsChanged)
        self.swapAxesCheck.stateChanged.connect(self.sigParamsChanged)
        self.overviewItem.scene().sigMouseClicked.connect(self._onSceneClicked)

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

    def _buildScanGroup(self) -> QtWidgets.QGroupBox:
        """How much ground to cover, and how settled the stage must be."""
        grid = QtWidgets.QGridLayout()

        grid.addWidget(QtWidgets.QLabel('Tiles X x Y:'), 0, 0)
        tilesRow = QtWidgets.QHBoxLayout()
        self.nTilesXSpinbox = QtWidgets.QSpinBox()
        self.nTilesXSpinbox.setMinimum(1)
        self.nTilesXSpinbox.setMaximum(1000)
        self.nTilesXSpinbox.setValue(3)
        tilesRow.addWidget(self.nTilesXSpinbox)
        self.nTilesYSpinbox = QtWidgets.QSpinBox()
        self.nTilesYSpinbox.setMinimum(1)
        self.nTilesYSpinbox.setMaximum(1000)
        self.nTilesYSpinbox.setValue(3)
        tilesRow.addWidget(self.nTilesYSpinbox)
        self.squareLockCheck = QtWidgets.QCheckBox('Square')
        self.squareLockCheck.setChecked(True)
        self.squareLockCheck.setToolTip(
            'Keep the two counts equal. Unlock for a rectangular area.'
        )
        tilesRow.addWidget(self.squareLockCheck)
        grid.addLayout(tilesRow, 0, 1)
        # The lock ties the two spinboxes rather than hiding one, so the grid
        # stays readable as N x N and unlocking never changes the area.
        self.nTilesXSpinbox.valueChanged.connect(self._matchTileCounts)
        self.nTilesYSpinbox.valueChanged.connect(self._matchTileCounts)
        self.squareLockCheck.toggled.connect(self._onSquareLockToggled)

        grid.addWidget(QtWidgets.QLabel('Step (µm):'), 0, 2)
        self.tileStepSpinbox = QtWidgets.QDoubleSpinBox()
        self.tileStepSpinbox.setMinimum(1.0)
        self.tileStepSpinbox.setMaximum(50000.0)
        self.tileStepSpinbox.setSingleStep(10.0)
        self.tileStepSpinbox.setDecimals(1)
        self.tileStepSpinbox.setValue(100.0)
        grid.addWidget(self.tileStepSpinbox, 0, 3)

        # The knob to reach for when the mosaic does not line up: too short and
        # tiles are grabbed while the stage is still ringing.
        grid.addWidget(QtWidgets.QLabel('Settle (ms):'), 1, 0)
        self.settleTimeSpinbox = QtWidgets.QDoubleSpinBox()
        self.settleTimeSpinbox.setMinimum(0.0)
        self.settleTimeSpinbox.setMaximum(10000.0)
        self.settleTimeSpinbox.setSingleStep(25.0)
        self.settleTimeSpinbox.setDecimals(0)
        self.settleTimeSpinbox.setValue(150.0)
        self.settleTimeSpinbox.setToolTip(
            'Wait after each stage move before capturing a tile.\n'
            'Increase this first if tiles do not overlap cleanly.'
        )
        grid.addWidget(self.settleTimeSpinbox, 1, 1)

        grid.addWidget(QtWidgets.QLabel('Pattern:'), 1, 2)
        self.patternCombo = QtWidgets.QComboBox()
        self.patternCombo.addItem('Spiral (centred)', SPIRAL)
        self.patternCombo.addItem('Serpentine (from here)', SERPENTINE)
        self.patternCombo.setToolTip(
            'The order tiles are visited in, and where the grid sits.\n\n'
            'Spiral grows outward from the current position, so the mosaic is\n'
            'centred here and a run stopped early still leaves a filled,\n'
            'centred area. Choose it when the extent is open-ended.\n\n'
            'Serpentine rasters from the current position in +X and +Y, one\n'
            'tile step at a time with no jumps, so every settle follows a\n'
            'single step. Choose it when you know the area and will finish it:\n'
            'frame the stage at one corner of what you want.'
        )
        grid.addWidget(self.patternCombo, 1, 3)
        grid.setColumnStretch(4, 1)
        return self._group('Scan', grid)

    def _buildAcquisitionGroup(self) -> QtWidgets.QGroupBox:
        """Where each tile's signal comes from, and on whose clock."""
        grid = QtWidgets.QGridLayout()

        # The operator chooses the timing model; which mechanism implements it
        # follows from the detector, which the controller works out.
        grid.addWidget(QtWidgets.QLabel('Mode:'), 0, 0)
        self.modeCombo = QtWidgets.QComboBox()
        self.modeCombo.addItem('Free-running', MODE_FREE_RUNNING)
        self.modeCombo.addItem('Triggered', MODE_TRIGGERED)
        self.modeCombo.setToolTip(
            'Free-running: grab a frame from a continuously running camera.\n'
            'Triggered: run one scan per tile — this drives a scanned\n'
            'detector (APD/PMT) directly, and clocks a camera wired to the\n'
            'scan trigger.'
        )
        grid.addWidget(self.modeCombo, 0, 1)

        self.scanSourceLabel = QtWidgets.QLabel('Scan:')
        self.scanSourceCombo = QtWidgets.QComboBox()
        self.scanSourceCombo.setToolTip(
            'Which scan controller to trigger. Only shown when the setup has\n'
            'more than one; the scan itself is configured in its own widget.'
        )
        grid.addWidget(self.scanSourceLabel, 0, 2)
        grid.addWidget(self.scanSourceCombo, 0, 3)
        self.scanSourceLabel.setVisible(False)
        self.scanSourceCombo.setVisible(False)

        self.detectorLabel = QtWidgets.QLabel('Align on:')
        self.detectorCombo = QtWidgets.QComboBox()
        self.detectorCombo.setToolTip(
            'Which detector the mosaic is built and aligned from. The setup\n'
            'file chooses the default; this overrides it for the session.\n'
            'Only shown when the setup has more than one to pick from.\n'
            '\n'
            'This is not the only detector saved: a run also saves whatever\n'
            'the Recording widget is set to capture, at every tile position,\n'
            'so long as each one declares how its pixels relate to this one.\n'
            '\n'
            'Changing it discards the current overview: a different detector\n'
            'means a different pixel size, so the existing mosaic no longer\n'
            'maps to stage coordinates.'
        )
        grid.addWidget(self.detectorLabel, 1, 0)
        grid.addWidget(self.detectorCombo, 1, 1)
        self.detectorLabel.setVisible(False)
        self.detectorCombo.setVisible(False)

        self.saveTilesCheck = QtWidgets.QCheckBox('Save tiles')
        self.saveTilesCheck.setToolTip(
            'Save each tile as an OME image as it is acquired, plus the\n'
            'stitched mosaic and a TileConfiguration.txt for Fiji/BigStitcher.\n'
            'Every tile carries its stage position in OME metadata.'
        )
        grid.addWidget(self.saveTilesCheck, 1, 2, 1, 2)
        grid.setColumnStretch(4, 1)
        return self._group('Acquisition', grid)

    def _buildAlignmentGroup(self) -> QtWidgets.QGroupBox:
        """Where each tile is laid down, and which way the mosaic grows."""
        outer = QtWidgets.QVBoxLayout()

        row = QtWidgets.QHBoxLayout()
        self.registerTilesCheck = QtWidgets.QCheckBox('Align tiles')
        self.registerTilesCheck.setToolTip(
            'Refine each tile by cross-correlating it with its already-placed\n'
            'neighbours instead of trusting the stage position alone.\n'
            'Also reports how far off the commanded positions were.'
        )
        row.addWidget(self.registerTilesCheck)

        row.addStretch(1)
        outer.addLayout(row)

        # Which way the overview grows depends on the camera mounting and the
        # stage sign convention, so it has to be settable per rig.
        orientation = QtWidgets.QHBoxLayout()
        orientation.addWidget(QtWidgets.QLabel('Orientation:'))
        self.flipXCheck = QtWidgets.QCheckBox('Flip X')
        self.flipXCheck.setToolTip(
            'Mirror the mosaic left/right.\n'
            'Use when tiles build in the opposite direction to the stage.'
        )
        self.flipYCheck = QtWidgets.QCheckBox('Flip Y')
        self.flipYCheck.setToolTip('Mirror the mosaic up/down.')
        self.swapAxesCheck = QtWidgets.QCheckBox('Swap X/Y')
        self.swapAxesCheck.setToolTip(
            'Exchange the mosaic axes, for a camera mounted at 90° to the stage.'
        )
        for check in (self.flipXCheck, self.flipYCheck, self.swapAxesCheck):
            orientation.addWidget(check)
        orientation.addStretch(1)
        outer.addLayout(orientation)
        return self._group('Alignment', outer)

    def _buildDisplayGroup(self) -> QtWidgets.QGroupBox:
        """How the placed tiles are drawn into one image."""
        row = QtWidgets.QHBoxLayout()
        self.blendOverlapsCheck = QtWidgets.QCheckBox('Mean overlaps')
        self.blendOverlapsCheck.setChecked(True)
        self.blendOverlapsCheck.setToolTip(
            'Average overlapping pixels instead of letting the newer tile win.'
        )
        self.intensityCorrectionCheck = QtWidgets.QCheckBox('Intensity correction')
        self.intensityCorrectionCheck.setToolTip(
            'Match each tile to its neighbours\' brightness where they overlap.'
        )
        row.addWidget(self.blendOverlapsCheck)
        row.addWidget(self.intensityCorrectionCheck)
        row.addStretch(1)
        return self._group('Stitching', row)

    def _buildRunRow(self) -> QtWidgets.QHBoxLayout:
        row = QtWidgets.QHBoxLayout()
        self.startButton = guitools.BetterPushButton('Start Tiling')
        self.stopButton = guitools.BetterPushButton('Stop')
        self.stopButton.setEnabled(False)
        self.navigateToggle = QtWidgets.QCheckBox('Navigate on click')
        self.navigateToggle.setToolTip(
            'Click anywhere on the overview to drive the stage there.'
        )
        row.addWidget(self.startButton, 2)
        row.addWidget(self.stopButton, 1)
        row.addWidget(self.navigateToggle)
        return row

    def _buildStatusBlock(self) -> QtWidgets.QWidget:
        holder = QtWidgets.QWidget()
        column = QtWidgets.QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)

        self.progressLabel = QtWidgets.QLabel('')
        self.progressLabel.setAlignment(QtCore.Qt.AlignCenter)
        column.addWidget(self.progressLabel)

        # Filled after a run: how far the commanded positions were off, and
        # whether the layout held together.
        self.registrationLabel = QtWidgets.QLabel('')
        self.registrationLabel.setWordWrap(True)
        self.registrationLabel.setAlignment(QtCore.Qt.AlignLeft)
        column.addWidget(self.registrationLabel)
        return holder

    def _buildOverview(self) -> QtWidgets.QWidget:
        self.overviewView = pg.GraphicsLayoutWidget()
        self.overviewView.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding
        )
        self.overviewView.setMinimumHeight(240)
        self.overviewItem = pg.ImageItem()
        self.overviewItem.setImage(np.zeros((64, 64), dtype=np.float32))
        self._overviewVB = self.overviewView.addViewBox()
        self._overviewVB.setAspectLocked(True)
        self._overviewVB.invertY(True)
        self._overviewVB.addItem(self.overviewItem)

        # Cell marker overlays
        self.cellMarkers = pg.ScatterPlotItem(
            symbol='+', size=14, pen=pg.mkPen('y', width=2), brush=None
        )
        self.currentCellMarker = pg.ScatterPlotItem(
            symbol='o', size=24, pen=pg.mkPen('r', width=3), brush=None
        )
        self._overviewVB.addItem(self.cellMarkers)
        self._overviewVB.addItem(self.currentCellMarker)
        self._cellPositions = None  # Store positions for highlightCurrentCell
        return self.overviewView

    def _buildTargetingRow(self) -> QtWidgets.QHBoxLayout:
        """Acts on the finished overview, so it sits under it."""
        row = QtWidgets.QHBoxLayout()
        self.tuneSegmentationButton = guitools.BetterPushButton(
            'Tune segmentation...'
        )
        self.tuneSegmentationButton.setEnabled(False)
        self.runCellTargetingButton = guitools.BetterPushButton('Detect cells')
        self.runCellTargetingButton.setEnabled(False)
        row.addWidget(self.tuneSegmentationButton)
        row.addWidget(self.runCellTargetingButton)
        return row

    def _onSceneClicked(self, event):
        if not self.navigateToggle.isChecked():
            return
        pos = self._overviewVB.mapSceneToView(event.scenePos())
        col = int(pos.x())
        row = int(pos.y())
        if col >= 0 and row >= 0:
            self.sigClickOnOverview.emit(row, col)

    def updateOverview(self, image: np.ndarray) -> None:
        """Display a new stitched overview (float32 H×W array)."""
        self.overviewItem.setImage(image.T)

    def setProgress(self, current: int, total: int) -> None:
        self.progressLabel.setText(f'{current} / {total}')

    def setRunning(self, running: bool) -> None:
        self.startButton.setEnabled(not running)
        self.stopButton.setEnabled(running)

    def _matchTileCounts(self, value: int) -> None:
        """Hold the counts equal while the lock is on, without recursing."""
        if not self.squareLockCheck.isChecked():
            return
        for spinbox in (self.nTilesXSpinbox, self.nTilesYSpinbox):
            if spinbox.value() == value:
                continue
            spinbox.blockSignals(True)
            spinbox.setValue(value)
            spinbox.blockSignals(False)

    def _onSquareLockToggled(self, locked: bool) -> None:
        if locked:
            self._matchTileCounts(self.nTilesXSpinbox.value())

    def getNTilesX(self) -> int:
        return self.nTilesXSpinbox.value()

    def getNTilesY(self) -> int:
        return self.nTilesYSpinbox.value()

    def getNTiles(self) -> int:
        """Total positions in the grid — what the progress bar counts."""
        return self.nTilesXSpinbox.value() * self.nTilesYSpinbox.value()

    def getPattern(self) -> str:
        return self.patternCombo.currentData()

    def setDefaultTileCounts(self, nx: int, ny: int) -> None:
        self.squareLockCheck.setChecked(nx == ny)
        self.nTilesXSpinbox.setValue(int(nx))
        self.nTilesYSpinbox.setValue(int(ny))

    def setDefaultPattern(self, pattern: str) -> None:
        index = self.patternCombo.findData(pattern)
        if index >= 0:
            self.patternCombo.setCurrentIndex(index)

    def getTileStepUm(self) -> float:
        return self.tileStepSpinbox.value()

    def getBlendOverlaps(self) -> bool:
        return self.blendOverlapsCheck.isChecked()

    def getIntensityCorrection(self) -> bool:
        return self.intensityCorrectionCheck.isChecked()

    def getSettleTimeMs(self) -> float:
        return self.settleTimeSpinbox.value()

    def getRegisterTiles(self) -> bool:
        return self.registerTilesCheck.isChecked()

    def getSaveTiles(self) -> bool:
        return self.saveTilesCheck.isChecked()

    def _onModeChanged(self) -> None:
        self.sigParamsChanged.emit()
        self.sigModeChanged.emit(self.getMode())

    def getMode(self) -> str:
        return self.modeCombo.currentData() or MODE_FREE_RUNNING

    def setMode(self, mode: str) -> None:
        index = self.modeCombo.findData(mode)
        if index >= 0:
            self.modeCombo.setCurrentIndex(index)

    def getScanSource(self):
        """Selected scan-source widget key, or None to let the app resolve it."""
        if not self.scanSourceCombo.isVisible():
            return None
        return self.scanSourceCombo.currentData()

    def setScanSource(self, key: str) -> None:
        index = self.scanSourceCombo.findData(key)
        if index >= 0:
            self.scanSourceCombo.setCurrentIndex(index)

    def setScanSources(self, keys) -> None:
        """Offer a scan-source choice, but only when there is one to make.

        A rig with a single scanner is never asked to pick it.
        """
        keys = list(keys or [])
        self.scanSourceCombo.blockSignals(True)
        self.scanSourceCombo.clear()
        for key in keys:
            self.scanSourceCombo.addItem(key, key)
        self.scanSourceCombo.blockSignals(False)

        showChoice = len(keys) > 1
        self.scanSourceLabel.setVisible(showChoice)
        self.scanSourceCombo.setVisible(showChoice)

    def getDetector(self):
        """Selected detector name, or None to let the controller resolve it.

        None when the setup offers no choice, so the setup-file value (and its
        fallback) stays the single authority on rigs with one camera.
        """
        if not self.detectorCombo.isVisible():
            return None
        return self.detectorCombo.currentData()

    def setDetector(self, name: str) -> None:
        index = self.detectorCombo.findData(name)
        if index >= 0:
            self.detectorCombo.setCurrentIndex(index)

    def setDetectors(self, names) -> None:
        """Offer a detector choice, but only when there is one to make."""
        names = list(names or [])
        self.detectorCombo.blockSignals(True)
        self.detectorCombo.clear()
        for name in names:
            self.detectorCombo.addItem(name, name)
        self.detectorCombo.blockSignals(False)

        showChoice = len(names) > 1
        self.detectorLabel.setVisible(showChoice)
        self.detectorCombo.setVisible(showChoice)

    def getTileOrientation(self) -> tuple:
        """Return ``(flip_x, flip_y, swap_axes)`` for mosaic assembly."""
        return (
            self.flipXCheck.isChecked(),
            self.flipYCheck.isChecked(),
            self.swapAxesCheck.isChecked(),
        )

    def setDefaultStep(self, step_um: float) -> None:
        self.tileStepSpinbox.setValue(step_um)

    def setDefaultSettleTimeMs(self, settle_ms: float) -> None:
        self.settleTimeSpinbox.setValue(settle_ms)

    def setDefaultRegisterTiles(self, enabled: bool) -> None:
        self.registerTilesCheck.setChecked(bool(enabled))

    def setDefaultSaveTiles(self, enabled: bool) -> None:
        self.saveTilesCheck.setChecked(bool(enabled))

    def setDefaultTileOrientation(
        self, flip_x: bool, flip_y: bool, swap_axes: bool
    ) -> None:
        self.flipXCheck.setChecked(bool(flip_x))
        self.flipYCheck.setChecked(bool(flip_y))
        self.swapAxesCheck.setChecked(bool(swap_axes))

    def setRegistrationSummary(self, summary: str) -> None:
        """Show the post-run registration diagnostics, or clear them."""
        self.registrationLabel.setText(summary or '')

    def setLabel(self, label: str) -> None:
        self.progressLabel.setText(label)

    def showCellMarkers(self, positions: np.ndarray) -> None:
        """Display cell position markers on the overview.
        
        Args:
            positions: (N, 2) array of (row, col) pixel coordinates.
        """
        if positions is None or len(positions) == 0:
            self.cellMarkers.clear()
            self._cellPositions = None
            return
        
        self._cellPositions = positions
        # ScatterPlotItem expects x, y where x=col, y=row (with invertY enabled)
        x = positions[:, 1]  # col
        y = positions[:, 0]  # row
        self.cellMarkers.setData(x=x, y=y)

    def highlightCurrentCell(self, idx: int) -> None:
        """Highlight a specific cell with a red circle.
        
        Args:
            idx: Index into the positions array from showCellMarkers.
                 Use -1 to clear the highlight.
        """
        if idx < 0 or self._cellPositions is None or idx >= len(self._cellPositions):
            self.currentCellMarker.clear()
            return
        
        row, col = self._cellPositions[idx]
        self.currentCellMarker.setData(x=[col], y=[row])

    def clearCellMarkers(self) -> None:
        """Clear all cell markers from the overview."""
        self.cellMarkers.clear()
        self.currentCellMarker.clear()
        self._cellPositions = None

    def setCellTargetingEnabled(self, enabled: bool) -> None:
        """Enable or disable cell-target detection controls.
        
        Args:
            enabled: True to enable the segmentation and targeting buttons.
        """
        self.tuneSegmentationButton.setEnabled(enabled)
        self.runCellTargetingButton.setEnabled(enabled)


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
