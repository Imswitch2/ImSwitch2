from typing import Dict, Optional, Tuple

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

#: What a click on the overview does. The overview maps pixels back to stage
#: positions, so a click can either go there or remember the place.
CLICK_NONE = 'none'
CLICK_NAVIGATE = 'navigate'
CLICK_ADD_TARGET = 'add-target'

_TARGET_FILE_FILTER = 'Target list (*.json);;All files (*)'


class TilingWidget(Widget):
    """Widget for controlling spiral tiling scans."""

    sigStartTiling = QtCore.Signal()
    sigStopTiling = QtCore.Signal()
    sigParamsChanged = QtCore.Signal()
    sigClickOnOverview = QtCore.Signal(int, int)  # row, col in stitched canvas
    sigModeChanged = QtCore.Signal(str)
    sigDetectorChanged = QtCore.Signal()
    # Target list actions. Each carries a target id, never a row index: the
    # controller owns the list and the table is only a view of it.
    sigGoToTarget = QtCore.Signal(int)
    sigRemoveTarget = QtCore.Signal(int)
    sigClearTargets = QtCore.Signal()
    sigTargetEnabledChanged = QtCore.Signal(int, bool)
    sigSaveTargets = QtCore.Signal(str)  # path
    sigLoadTargets = QtCore.Signal(str)  # path

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
        layout.addWidget(self._buildTargetsGroup())

        # Wire signals
        self.startButton.clicked.connect(self.sigStartTiling)
        self.stopButton.clicked.connect(self.sigStopTiling)
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

        self.goToTargetButton.clicked.connect(self._onGoToTarget)
        self.removeTargetButton.clicked.connect(self._onRemoveTarget)
        self.clearTargetsButton.clicked.connect(self.sigClearTargets)
        self.saveTargetsButton.clicked.connect(self._onSaveTargets)
        self.loadTargetsButton.clicked.connect(self._onLoadTargets)
        self.targetsTable.itemSelectionChanged.connect(
            self._onTargetSelectionChanged
        )
        self.targetsTable.itemChanged.connect(self._onTargetItemChanged)

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
        # One chooser rather than two checkboxes: a click cannot both drive
        # the stage and mark a target, so the choices are exclusive.
        self.clickModeCombo = QtWidgets.QComboBox()
        self.clickModeCombo.addItem('does nothing', CLICK_NONE)
        self.clickModeCombo.addItem('moves the stage', CLICK_NAVIGATE)
        self.clickModeCombo.addItem('adds a target', CLICK_ADD_TARGET)
        self.clickModeCombo.setToolTip(
            'What a click on the overview does.\n'
            'Moves the stage: drive the XY positioner to the clicked spot.\n'
            'Adds a target: remember the clicked spot in the target list\n'
            'below, without moving anything.'
        )
        row.addWidget(self.startButton, 2)
        row.addWidget(self.stopButton, 1)
        row.addWidget(QtWidgets.QLabel('Click:'))
        row.addWidget(self.clickModeCombo)
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

        # Target markers: one cross per target that lies on the overview, and
        # a ring around the one selected in the table.
        self._targetPenOn = pg.mkPen('y', width=2)
        self._targetPenOff = pg.mkPen((160, 160, 160), width=1)
        self.targetMarkers = pg.ScatterPlotItem(symbol='+', size=14, brush=None)
        self.selectedTargetMarker = pg.ScatterPlotItem(
            symbol='o', size=24, pen=pg.mkPen('r', width=3), brush=None
        )
        self._overviewVB.addItem(self.targetMarkers)
        self._overviewVB.addItem(self.selectedTargetMarker)
        self._targetMarkers: Dict[int, Tuple[float, float]] = {}
        self._targetEnabled: Dict[int, bool] = {}
        return self.overviewView

    def _buildTargetsGroup(self) -> QtWidgets.QGroupBox:
        """Stage positions to come back to, listed under the overview.

        The list is the controller's; the table shows it and sends back what
        the operator asked for. It is sized to stay a strip under the
        overview rather than compete with it for height.
        """
        outer = QtWidgets.QVBoxLayout()

        self.targetsTable = QtWidgets.QTableWidget(0, 5)
        self.targetsTable.setHorizontalHeaderLabels(
            ['On', '#', 'X (µm)', 'Y (µm)', 'Source']
        )
        self.targetsTable.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectRows
        )
        self.targetsTable.setSelectionMode(
            QtWidgets.QAbstractItemView.SingleSelection
        )
        self.targetsTable.setEditTriggers(
            QtWidgets.QAbstractItemView.NoEditTriggers
        )
        self.targetsTable.verticalHeader().setVisible(False)
        header = self.targetsTable.horizontalHeader()
        header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)
        self.targetsTable.setMaximumHeight(160)
        self.targetsTable.setToolTip(
            'Stage positions to return to. Tick a target off to keep it in\n'
            'the list but skip it; select one to see where it is on the\n'
            'overview. Scripts read the same list through\n'
            'api.imcontrol.getTargets().'
        )
        outer.addWidget(self.targetsTable)

        buttons = QtWidgets.QHBoxLayout()
        self.goToTargetButton = guitools.BetterPushButton('Go to')
        self.goToTargetButton.setToolTip(
            'Drive the stage to the selected target.'
        )
        self.removeTargetButton = guitools.BetterPushButton('Remove')
        self.clearTargetsButton = guitools.BetterPushButton('Clear')
        self.saveTargetsButton = guitools.BetterPushButton('Save…')
        self.loadTargetsButton = guitools.BetterPushButton('Load…')
        self.saveTargetsButton.setToolTip(
            'Write the list to a JSON file a script or a later session can\n'
            'load again.'
        )
        self.goToTargetButton.setEnabled(False)
        self.removeTargetButton.setEnabled(False)
        for button in (
            self.goToTargetButton, self.removeTargetButton,
            self.clearTargetsButton, self.saveTargetsButton,
            self.loadTargetsButton,
        ):
            buttons.addWidget(button)
        self.targetsCountLabel = QtWidgets.QLabel('')
        buttons.addStretch(1)
        buttons.addWidget(self.targetsCountLabel)
        outer.addLayout(buttons)
        self._updatingTargets = False
        return self._group('Targets', outer)

    def _onSceneClicked(self, event):
        if self.getClickMode() == CLICK_NONE:
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

    def getClickMode(self) -> str:
        """What a click on the overview does: one of the ``CLICK_*`` values."""
        return self.clickModeCombo.currentData() or CLICK_NONE

    def setClickMode(self, mode: str) -> None:
        index = self.clickModeCombo.findData(mode)
        if index >= 0:
            self.clickModeCombo.setCurrentIndex(index)

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

    # ------------------------------------------------------------------
    # Targets
    # ------------------------------------------------------------------

    def setTargets(self, records) -> None:
        """Show the target list.

        ``records`` are the controller's target dicts (``id``, ``stage_xy``,
        ``enabled``, ``source``). The selection survives a rebuild when its
        target is still there, so ticking one target does not drop the
        selection on another.
        """
        records = list(records or [])
        selected = self.selectedTargetId()
        self._updatingTargets = True
        try:
            self.targetsTable.setRowCount(0)
            self.targetsTable.setRowCount(len(records))
            self._targetEnabled = {}
            for rowIndex, record in enumerate(records):
                target_id = int(record['id'])
                x_um, y_um = record['stage_xy']
                enabled = bool(record.get('enabled', True))
                self._targetEnabled[target_id] = enabled

                onItem = QtWidgets.QTableWidgetItem()
                onItem.setFlags(
                    QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsEnabled
                    | QtCore.Qt.ItemIsSelectable
                )
                onItem.setCheckState(
                    QtCore.Qt.Checked if enabled else QtCore.Qt.Unchecked
                )
                onItem.setData(QtCore.Qt.UserRole, target_id)
                self.targetsTable.setItem(rowIndex, 0, onItem)
                for column, text in enumerate(
                    (
                        str(target_id),
                        f'{float(x_um):.1f}',
                        f'{float(y_um):.1f}',
                        str(record.get('source', '')),
                    ),
                    start=1,
                ):
                    item = QtWidgets.QTableWidgetItem(text)
                    item.setData(QtCore.Qt.UserRole, target_id)
                    self.targetsTable.setItem(rowIndex, column, item)
                if target_id == selected:
                    self.targetsTable.selectRow(rowIndex)
        finally:
            self._updatingTargets = False

        enabledCount = sum(1 for on in self._targetEnabled.values() if on)
        if not records:
            self.targetsCountLabel.setText('')
        elif enabledCount == len(records):
            self.targetsCountLabel.setText(f'{len(records)} targets')
        else:
            self.targetsCountLabel.setText(
                f'{enabledCount} of {len(records)} targets on'
            )
        self._redrawTargetMarkers()
        self._onTargetSelectionChanged()

    def setTargetMarkers(self, markers) -> None:
        """Place targets on the overview.

        ``markers`` maps a target id to its ``(row, col)`` overview pixel.
        Targets that are not in it — because there is no overview, or they
        lie outside it — are listed but not drawn.
        """
        self._targetMarkers = {
            int(target_id): (float(rc[0]), float(rc[1]))
            for target_id, rc in dict(markers or {}).items()
        }
        self._redrawTargetMarkers()

    def selectedTargetId(self) -> Optional[int]:
        items = self.targetsTable.selectedItems()
        if not items:
            return None
        value = items[0].data(QtCore.Qt.UserRole)
        return None if value is None else int(value)

    def _redrawTargetMarkers(self) -> None:
        spots = []
        for target_id, (row, col) in self._targetMarkers.items():
            on = self._targetEnabled.get(target_id, True)
            spots.append({
                'pos': (col, row),
                'pen': self._targetPenOn if on else self._targetPenOff,
                'data': target_id,
            })
        self.targetMarkers.setData(spots=spots)
        self._highlightSelectedTarget()

    def _highlightSelectedTarget(self) -> None:
        marker = self._targetMarkers.get(self.selectedTargetId())
        if marker is None:
            self.selectedTargetMarker.clear()
            return
        row, col = marker
        self.selectedTargetMarker.setData(x=[col], y=[row])

    def _onTargetSelectionChanged(self) -> None:
        hasSelection = self.selectedTargetId() is not None
        self.goToTargetButton.setEnabled(hasSelection)
        self.removeTargetButton.setEnabled(hasSelection)
        self._highlightSelectedTarget()

    def _onTargetItemChanged(self, item) -> None:
        if self._updatingTargets or item.column() != 0:
            return
        target_id = item.data(QtCore.Qt.UserRole)
        if target_id is None:
            return
        self.sigTargetEnabledChanged.emit(
            int(target_id), item.checkState() == QtCore.Qt.Checked
        )

    def _onGoToTarget(self) -> None:
        target_id = self.selectedTargetId()
        if target_id is not None:
            self.sigGoToTarget.emit(target_id)

    def _onRemoveTarget(self) -> None:
        target_id = self.selectedTargetId()
        if target_id is not None:
            self.sigRemoveTarget.emit(target_id)

    def _onSaveTargets(self) -> None:
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(
            self, 'Save targets', '', _TARGET_FILE_FILTER
        )
        if path:
            self.sigSaveTargets.emit(path)

    def _onLoadTargets(self) -> None:
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self, 'Load targets', '', _TARGET_FILE_FILTER
        )
        if path:
            self.sigLoadTargets.emit(path)


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
