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

    sigToolSelected = QtCore.Signal(str)  # 'pan', 'rectangle', 'line', 'crosshair', 'grid'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # All five tools are mutually exclusive
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

        for i, btn in enumerate([self.panButton, self.rectangleButton,
                                  self.lineButton, self.crosshairButton,
                                  self.gridButton]):
            self.toolButtonGroup.addButton(btn, i)

        # Layout
        layout = QtWidgets.QGridLayout()
        self.setLayout(layout)
        layout.addWidget(QtWidgets.QLabel('Viewer Tools:'), 0, 0, 1, 2)
        layout.addWidget(self.panButton, 1, 0)
        layout.addWidget(self.rectangleButton, 1, 1)
        layout.addWidget(self.lineButton, 2, 0)
        layout.addWidget(self.crosshairButton, 2, 1)
        layout.addWidget(self.gridButton, 3, 0, 1, 2)

        # Signals
        self.panButton.clicked.connect(lambda: self.sigToolSelected.emit('pan'))
        self.rectangleButton.clicked.connect(lambda: self.sigToolSelected.emit('rectangle'))
        self.lineButton.clicked.connect(lambda: self.sigToolSelected.emit('line'))
        self.crosshairButton.clicked.connect(lambda: self.sigToolSelected.emit('crosshair'))
        self.gridButton.clicked.connect(lambda: self.sigToolSelected.emit('grid'))

    def setActiveTool(self, mode):
        mapping = {
            'pan': self.panButton,
            'rectangle': self.rectangleButton,
            'line': self.lineButton,
            'crosshair': self.crosshairButton,
            'grid': self.gridButton,
        }
        btn = mapping.get(mode)
        if btn:
            btn.setChecked(True)

    def getActiveTool(self):
        for name, btn in [('pan', self.panButton), ('rectangle', self.rectangleButton),
                          ('line', self.lineButton), ('crosshair', self.crosshairButton),
                          ('grid', self.gridButton)]:
            if btn.isChecked():
                return name
        return 'pan'
