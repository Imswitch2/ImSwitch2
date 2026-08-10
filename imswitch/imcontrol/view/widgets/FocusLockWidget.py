import numpy as np
import pyqtgraph as pg
from qtpy import QtWidgets

from imswitch.imcontrol.view import guitools as guitools
from .basewidgets import Widget


class FocusLockWidget(Widget):
    """ Widget containing focus lock interface. """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Focus lock
        self.kpEdit = QtWidgets.QLineEdit('1')
        self.kpLabel = QtWidgets.QLabel('kp')
        self.kiEdit = QtWidgets.QLineEdit('0')
        self.kiLabel = QtWidgets.QLabel('ki')

        self.lockButton = guitools.BetterPushButton('Lock')
        self.lockButton.setCheckable(True)
        self.lockButton.setSizePolicy(QtWidgets.QSizePolicy.Preferred,
                                      QtWidgets.QSizePolicy.Expanding)

        # Checked by default: this is an opt-*out*. It used to be an unchecked,
        # unpersisted opt-in, so every fresh session silently allowed an active
        # focus lock to fight a hardware Z scan on the same piezo.
        self.ScanBlock = QtWidgets.QCheckBox('Pause during scans')
        self.ScanBlock.setChecked(True)
        self.ScanBlock.setToolTip(
            'Suspend focus correction while a scan drives the focus axis, and '
            'wait for the signal to come back before resuming.\n'
            'Unchecking this lets the lock oppose the scan waveform when both '
            'reach the same actuator.'
        )
        self.twoFociBox = QtWidgets.QCheckBox('Two foci')

        self.lockStateLabel = QtWidgets.QLabel('')
        self.lockStateLabel.setToolTip(
            'Focus-lock state. "Suspended" means a scan currently owns the '
            'focus axis; "Reacquiring" means it is waiting for the focus '
            'signal to return before correcting again.'
        )

        self.camDialogButton = guitools.BetterPushButton('Camera Dialog')

        # Focus lock calibration
        self.calibFromLabel = QtWidgets.QLabel('From (µm)')
        self.calibFromEdit = QtWidgets.QLineEdit('-1')
        self.calibToLabel = QtWidgets.QLabel('To (µm)')
        self.calibToEdit = QtWidgets.QLineEdit('1')
        self.focusCalibButton = guitools.BetterPushButton('Calib')
        self.focusCalibButton.setSizePolicy(QtWidgets.QSizePolicy.Preferred,
                                            QtWidgets.QSizePolicy.Expanding)
        self.calibCurveButton = guitools.BetterPushButton('See calib')
        self.calibrationDisplay = QtWidgets.QLineEdit('No calibration')
        self.calibrationDisplay.setReadOnly(True)
        self.focusCalibrationWindow = FocusCalibrationWindow()

        # Focus lock graph
        self.focusLockGraph = pg.GraphicsLayoutWidget()
        self.focusLockGraph.setAntialiasing(True)
        self.focusPlot = self.focusLockGraph.addPlot(row=1, col=0)
        self.focusPlot.setLabels(bottom=('Time', 's'), left=('Laser position', 'px'))
        self.focusPlot.showGrid(x=True, y=True)
        self.focusPlotCurve = self.focusPlot.plot(pen='y') # update from FocusLockController

        # Webcam graph
        self.webcamGraph = pg.GraphicsLayoutWidget()
        self.camImg = pg.ImageItem(border='w')
        self.camImg.setImage(np.zeros((100, 100)))
        self.vb = self.webcamGraph.addViewBox(invertY=True, invertX=False)
        self.vb.setAspectLocked(True)
        self.vb.addItem(self.camImg)
        self.center = pg.InfiniteLine()
        self.vb.addItem(self.center)
        self.center.setVisible(True)

        # GUI layout below
        grid = QtWidgets.QGridLayout()
        self.setLayout(grid)
        grid.addWidget(self.focusLockGraph, 0, 0, 1, 9)
        grid.addWidget(self.webcamGraph, 0, 9, 4, 1)
        grid.addWidget(self.focusCalibButton, 1, 2, 2, 1)
        grid.addWidget(self.calibrationDisplay, 3, 0, 1, 2)
        grid.addWidget(self.kpLabel, 1, 3)
        grid.addWidget(self.kpEdit, 1, 4)
        grid.addWidget(self.kiLabel, 2, 3)
        grid.addWidget(self.kiEdit, 2, 4)
        grid.addWidget(self.lockButton, 1, 5, 2, 1)
        grid.addWidget(self.ScanBlock, 3, 6)
        grid.addWidget(self.twoFociBox, 2, 6)
        grid.addWidget(self.calibFromLabel, 1, 0)
        grid.addWidget(self.calibFromEdit, 1, 1)
        grid.addWidget(self.calibToLabel, 2, 0)
        grid.addWidget(self.calibToEdit, 2, 1)
        grid.addWidget(self.calibCurveButton, 3, 2)
        grid.addWidget(self.camDialogButton, 1, 6, 1, 2)
        grid.addWidget(self.lockStateLabel, 2, 7)

    def setKp(self, kp):
        self.kpEdit.setText(str(kp))

    def setKi(self, ki):
        self.kiEdit.setText(str(ki))

    def setLockState(self, state):
        """Show the lifecycle state, so the button never stands alone.

        The lock button says "Unlock" whenever a lock is *wanted*, which is not
        the same as one being active: a scan can have suspended it, or
        reacquisition can have failed. Without this the operator has no way to
        tell those apart from the button.
        """
        text, colour = self._LOCK_STATE_DISPLAY.get(state, (state, 'gray'))
        self.lockStateLabel.setText(text)
        self.lockStateLabel.setStyleSheet(f'color: {colour};')

    _LOCK_STATE_DISPLAY = {
        'unlocked': ('Unlocked', 'gray'),
        'locked': ('Locked', 'limegreen'),
        'suspended': ('Suspended (scan)', 'orange'),
        'reacquiring': ('Reacquiring…', 'orange'),
        'reacquire-failed': ('Lock lost after scan', 'red'),
    }

    def showCalibrationCurve(self, data):
        self.focusCalibrationWindow.run(data)
        self.focusCalibrationWindow.show()


class FocusCalibrationWindow(QtWidgets.QFrame):
    def __init__(self):
        super().__init__()
        self.__focusCalibGraph = FocusCalibrationGraph()
        grid = QtWidgets.QGridLayout()
        self.setLayout(grid)
        grid.addWidget(self.__focusCalibGraph, 0, 0)

    def run(self, data):
        self.__focusCalibGraph.draw(data)


class FocusCalibrationGraph(pg.GraphicsLayoutWidget):
    def __init__(self):
        super().__init__()
        self.plot = self.addPlot(row=1, col=0)
        self.plot.setLabels(bottom=('Set z position', 'µm'),
                            left=('Laser spot position', 'px'))
        self.plot.showGrid(x=True, y=True)

    def draw(self, data):
        self.plot.clear()
        self.positionData = data['positionData']
        self.signalData = data['signalData']
        self.poly = data['poly']
        self.plot.plot(self.positionData,
                       self.signalData, pen=None, symbol='o')
        self.plot.plot(self.positionData,
                       np.polyval(self.poly, self.positionData), pen='r')


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
