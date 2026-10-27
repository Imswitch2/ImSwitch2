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

from qtpy import QtCore, QtWidgets

from imswitch.imcontrol.view import guitools
from .basewidgets import Widget


class ViewerToolsWidget(Widget):
    """Widget for switching napari viewer interaction modes."""

    #: 'pan', 'rectangle', 'line', 'timetrace', 'crosshair' or 'grid'
    sigToolSelected = QtCore.Signal(str)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # All tools are mutually exclusive
        self.toolButtonGroup = QtWidgets.QButtonGroup()
        self.toolButtonGroup.setExclusive(True)

        self.panButton = guitools.BetterPushButton('Pan')
        self.panButton.setCheckable(True)
        self.panButton.setChecked(True)
        self.panButton.setToolTip('Pan and zoom (no drawing)')

        self.rectangleButton = guitools.BetterPushButton('Rectangle ROI')
        self.rectangleButton.setCheckable(True)
        self.rectangleButton.setToolTip('Draw a rectangle ROI')

        self.lineButton = guitools.BetterPushButton('Line')
        self.lineButton.setCheckable(True)
        self.lineButton.setToolTip('Draw a line')

        self.crosshairButton = guitools.BetterPushButton('Crosshair')
        self.crosshairButton.setCheckable(True)
        self.crosshairButton.setToolTip('Click in the viewer to place a crosshair')

        self.gridButton = guitools.BetterPushButton('Grid')
        self.gridButton.setCheckable(True)
        self.gridButton.setToolTip('Show static grid overlay')

        self.intensityTraceButton = guitools.BetterPushButton('ROI Intensity vs T')
        self.intensityTraceButton.setCheckable(True)
        self.intensityTraceButton.setToolTip(
            'Draw a rectangle ROI and plot its mean intensity over time in the'
            ' Line Profile panel (the whole frame until one is drawn)'
        )

        for i, btn in enumerate([self.panButton, self.rectangleButton,
                                  self.lineButton, self.crosshairButton,
                                  self.gridButton, self.intensityTraceButton]):
            self.toolButtonGroup.addButton(btn, i)

        # Layout
        self._layout = layout = QtWidgets.QGridLayout()
        self.setLayout(layout)
        layout.addWidget(QtWidgets.QLabel('Viewer Tools:'), 0, 0, 1, 2)
        layout.addWidget(self.panButton, 1, 0)
        layout.addWidget(self.rectangleButton, 1, 1)
        layout.addWidget(self.lineButton, 2, 0)
        layout.addWidget(self.intensityTraceButton, 2, 1)
        layout.addWidget(self.crosshairButton, 3, 0)
        layout.addWidget(self.gridButton, 3, 1)
        # Plotted by the Line Profile panel; offered once it is known to exist.
        self.setIntensityTraceAvailable(False)

        # Signals
        self.panButton.clicked.connect(lambda: self.sigToolSelected.emit('pan'))
        self.rectangleButton.clicked.connect(lambda: self.sigToolSelected.emit('rectangle'))
        self.lineButton.clicked.connect(lambda: self.sigToolSelected.emit('line'))
        self.crosshairButton.clicked.connect(lambda: self.sigToolSelected.emit('crosshair'))
        self.gridButton.clicked.connect(lambda: self.sigToolSelected.emit('grid'))
        self.intensityTraceButton.clicked.connect(
            lambda: self.sigToolSelected.emit('timetrace'))

    def _toolButtons(self):
        return {
            'pan': self.panButton,
            'rectangle': self.rectangleButton,
            'line': self.lineButton,
            'timetrace': self.intensityTraceButton,
            'crosshair': self.crosshairButton,
            'grid': self.gridButton,
        }

    def setIntensityTraceAvailable(self, available):
        """Show the ROI Intensity vs T button, which needs the Line Profile panel."""
        self.intensityTraceButton.setVisible(bool(available))
        # Without it the Line button would sit beside a gap.
        self._layout.removeWidget(self.lineButton)
        self._layout.addWidget(self.lineButton, 2, 0, 1, 1 if available else 2)

    def setActiveTool(self, mode):
        btn = self._toolButtons().get(mode)
        if btn:
            btn.setChecked(True)

    def getActiveTool(self):
        for name, btn in self._toolButtons().items():
            if btn.isChecked():
                return name
        return 'pan'
