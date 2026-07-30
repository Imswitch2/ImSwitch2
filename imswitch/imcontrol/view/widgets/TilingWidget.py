import numpy as np
import pyqtgraph as pg
from qtpy import QtCore, QtWidgets

from imswitch.imcontrol.view import guitools
from .basewidgets import Widget


class TilingWidget(Widget):
    """Widget for controlling spiral tiling scans."""

    sigStartTiling = QtCore.Signal()
    sigStopTiling = QtCore.Signal()
    sigParamsChanged = QtCore.Signal()
    sigClickOnOverview = QtCore.Signal(int, int)  # row, col in stitched canvas
    sigTuneSegmentation = QtCore.Signal()
    sigRunCellTargeting = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        layout = QtWidgets.QGridLayout()
        self.setLayout(layout)

        # Row 0: N tiles and step size
        layout.addWidget(QtWidgets.QLabel('N tiles:'), 0, 0)
        self.nTilesSpinbox = QtWidgets.QSpinBox()
        self.nTilesSpinbox.setMinimum(1)
        self.nTilesSpinbox.setMaximum(10000)
        self.nTilesSpinbox.setValue(9)
        layout.addWidget(self.nTilesSpinbox, 0, 1)

        layout.addWidget(QtWidgets.QLabel('Step (µm):'), 0, 2)
        self.tileStepSpinbox = QtWidgets.QDoubleSpinBox()
        self.tileStepSpinbox.setMinimum(1.0)
        self.tileStepSpinbox.setMaximum(50000.0)
        self.tileStepSpinbox.setSingleStep(10.0)
        self.tileStepSpinbox.setDecimals(1)
        self.tileStepSpinbox.setValue(100.0)
        layout.addWidget(self.tileStepSpinbox, 0, 3)

        # Row 1: Settle time — the knob to reach for when the mosaic does not
        # line up: too short and tiles are grabbed while the stage still rings.
        layout.addWidget(QtWidgets.QLabel('Settle (ms):'), 1, 0)
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
        layout.addWidget(self.settleTimeSpinbox, 1, 1)

        self.registerTilesCheck = QtWidgets.QCheckBox('Align tiles')
        self.registerTilesCheck.setToolTip(
            'Refine each tile by cross-correlating it with its already-placed\n'
            'neighbours instead of trusting the stage position alone.\n'
            'Also reports how far off the commanded positions were.'
        )
        layout.addWidget(self.registerTilesCheck, 1, 2, 1, 2)

        # Row 2: Start, Stop buttons and navigate toggle
        self.startButton = guitools.BetterPushButton('Start Tiling')
        self.stopButton = guitools.BetterPushButton('Stop')
        self.stopButton.setEnabled(False)
        self.navigateToggle = QtWidgets.QCheckBox('Navigate on click')
        layout.addWidget(self.startButton, 2, 0, 1, 2)
        layout.addWidget(self.stopButton, 2, 2)
        layout.addWidget(self.navigateToggle, 2, 3)

        # Row 3: Stitching options
        self.blendOverlapsCheck = QtWidgets.QCheckBox('Mean overlaps')
        self.blendOverlapsCheck.setChecked(True)
        self.intensityCorrectionCheck = QtWidgets.QCheckBox('Intensity correction')
        layout.addWidget(self.blendOverlapsCheck, 3, 0, 1, 2)
        layout.addWidget(self.intensityCorrectionCheck, 3, 2, 1, 2)

        # Row 4: Progress label
        self.progressLabel = QtWidgets.QLabel('')
        self.progressLabel.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(self.progressLabel, 4, 0, 1, 4)

        # Row 5: registration diagnostics, populated after a run
        self.registrationLabel = QtWidgets.QLabel('')
        self.registrationLabel.setWordWrap(True)
        self.registrationLabel.setAlignment(QtCore.Qt.AlignLeft)
        layout.addWidget(self.registrationLabel, 5, 0, 1, 4)

        # Row 6: Cell targeting controls
        self.tuneSegmentationButton = guitools.BetterPushButton('Tune segmentation...')
        self.tuneSegmentationButton.setEnabled(False)
        self.runCellTargetingButton = guitools.BetterPushButton('Detect cells')
        self.runCellTargetingButton.setEnabled(False)
        layout.addWidget(self.tuneSegmentationButton, 6, 0, 1, 2)
        layout.addWidget(self.runCellTargetingButton, 6, 2, 1, 2)

        # Rows 7+: Stitched overview display
        self.overviewView = pg.GraphicsLayoutWidget()
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
        
        layout.addWidget(self.overviewView, 7, 0, 4, 4)

        # Wire signals
        self.startButton.clicked.connect(self.sigStartTiling)
        self.stopButton.clicked.connect(self.sigStopTiling)
        self.tuneSegmentationButton.clicked.connect(self.sigTuneSegmentation)
        self.runCellTargetingButton.clicked.connect(self.sigRunCellTargeting)
        self.nTilesSpinbox.valueChanged.connect(self.sigParamsChanged)
        self.tileStepSpinbox.valueChanged.connect(self.sigParamsChanged)
        self.blendOverlapsCheck.stateChanged.connect(self.sigParamsChanged)
        self.intensityCorrectionCheck.stateChanged.connect(self.sigParamsChanged)
        self.settleTimeSpinbox.valueChanged.connect(self.sigParamsChanged)
        self.registerTilesCheck.stateChanged.connect(self.sigParamsChanged)
        self.overviewItem.scene().sigMouseClicked.connect(self._onSceneClicked)

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

    def getNTiles(self) -> int:
        return self.nTilesSpinbox.value()

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

    def setDefaultStep(self, step_um: float) -> None:
        self.tileStepSpinbox.setValue(step_um)

    def setDefaultSettleTimeMs(self, settle_ms: float) -> None:
        self.settleTimeSpinbox.setValue(settle_ms)

    def setDefaultRegisterTiles(self, enabled: bool) -> None:
        self.registerTilesCheck.setChecked(bool(enabled))

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
