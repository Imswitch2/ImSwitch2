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

        # Row 1: Start, Stop buttons and navigate toggle
        self.startButton = guitools.BetterPushButton('Start Tiling')
        self.stopButton = guitools.BetterPushButton('Stop')
        self.stopButton.setEnabled(False)
        self.navigateToggle = QtWidgets.QCheckBox('Navigate on click')
        layout.addWidget(self.startButton, 1, 0, 1, 2)
        layout.addWidget(self.stopButton, 1, 2)
        layout.addWidget(self.navigateToggle, 1, 3)

        # Row 2: Progress label
        self.progressLabel = QtWidgets.QLabel('')
        self.progressLabel.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(self.progressLabel, 2, 0, 1, 4)

        # Rows 3+: Stitched overview display
        self.overviewView = pg.GraphicsLayoutWidget()
        self.overviewItem = pg.ImageItem()
        self.overviewItem.setImage(np.zeros((64, 64), dtype=np.float32))
        self._overviewVB = self.overviewView.addViewBox()
        self._overviewVB.setAspectLocked(True)
        self._overviewVB.invertY(True)
        self._overviewVB.addItem(self.overviewItem)
        layout.addWidget(self.overviewView, 3, 0, 4, 4)

        # Wire signals
        self.startButton.clicked.connect(self.sigStartTiling)
        self.stopButton.clicked.connect(self.sigStopTiling)
        self.nTilesSpinbox.valueChanged.connect(self.sigParamsChanged)
        self.tileStepSpinbox.valueChanged.connect(self.sigParamsChanged)
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

    def setDefaultStep(self, step_um: float) -> None:
        self.tileStepSpinbox.setValue(step_um)

    def setLabel(self, label: str) -> None:
        self.progressLabel.setText(label)


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
