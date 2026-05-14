from qtpy import QtCore, QtWidgets

from imswitch.imcontrol.view import guitools
from .basewidgets import Widget


class TilingWidget(Widget):
    """ Widget for controlling tiling scans with configurable parameters. """

    sigStartTiling = QtCore.Signal()
    sigStopTiling = QtCore.Signal()
    sigParamsChanged = QtCore.Signal()
    sigSaveFocus = QtCore.Signal(bool)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        layout = QtWidgets.QGridLayout()
        self.setLayout(layout)

        # Row 0: N tiles and step size
        nTilesLabel = QtWidgets.QLabel('N tiles:')
        self.nTilesSpinbox = QtWidgets.QSpinBox()
        self.nTilesSpinbox.setMinimum(1)
        self.nTilesSpinbox.setMaximum(10000)
        self.nTilesSpinbox.setValue(9)

        stepLabel = QtWidgets.QLabel('Step (µm):')
        self.tileStepSpinbox = QtWidgets.QDoubleSpinBox()
        self.tileStepSpinbox.setMinimum(1.0)
        self.tileStepSpinbox.setMaximum(50000.0)
        self.tileStepSpinbox.setSingleStep(10.0)
        self.tileStepSpinbox.setDecimals(1)
        self.tileStepSpinbox.setValue(100.0)

        layout.addWidget(nTilesLabel, 0, 0)
        layout.addWidget(self.nTilesSpinbox, 0, 1)
        layout.addWidget(stepLabel, 0, 2)
        layout.addWidget(self.tileStepSpinbox, 0, 3)

        # Row 1: Start and Stop buttons
        self.startButton = guitools.BetterPushButton('Start Tiling')
        self.stopButton = guitools.BetterPushButton('Stop')
        self.stopButton.setEnabled(False)

        layout.addWidget(self.startButton, 1, 0, 1, 2)
        layout.addWidget(self.stopButton, 1, 2, 1, 2)

        # Row 2: Progress label
        self.progressLabel = QtWidgets.QLabel('')
        self.progressLabel.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(self.progressLabel, 2, 0, 1, 4)

        # Wire up signals
        self.startButton.clicked.connect(self.sigStartTiling)
        self.stopButton.clicked.connect(self.sigStopTiling)
        self.nTilesSpinbox.valueChanged.connect(self.sigParamsChanged)
        self.tileStepSpinbox.valueChanged.connect(self.sigParamsChanged)

    def setProgress(self, current, total):
        self.progressLabel.setText(f'{current} / {total}')

    def setRunning(self, running):
        self.startButton.setEnabled(not running)
        self.stopButton.setEnabled(running)

    def getNTiles(self):
        return self.nTilesSpinbox.value()

    def getTileStepUm(self):
        return self.tileStepSpinbox.value()

    def setDefaultStep(self, step_um):
        self.tileStepSpinbox.setValue(step_um)

    def setLabel(self, label):
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
