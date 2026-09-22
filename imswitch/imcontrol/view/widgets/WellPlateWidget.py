from functools import partial

from PyQt5.QtCore import Qt
from qtpy import QtCore, QtWidgets

from imswitch.imcommon.model import initLogger
from .basewidgets import Widget


class WellPlateWidget(Widget):
    """ Widget displaying a 96-well plate grid.  Clicking a well is currently
    a stub (logs the well name); full stage-movement wiring is pending. """

    sigStepUpClicked = QtCore.Signal(str, str)    # (positionerName, axis)
    sigStepDownClicked = QtCore.Signal(str, str)  # (positionerName, axis)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__logger = initLogger(self, instanceName='WellPlateWidget')
        self.Wells = {}
        self.pars = {}
        self.grid = QtWidgets.QGridLayout()
        self.setLayout(self.grid)

    def add_plate_view(self, positionerName):
        """ Build the 96-well plate button grid. """
        wellLayout = self.grid
        buttons = {
            ' ':   (0, 0),
            '1':   (0, 1),  '2':   (0, 2),  '3':   (0, 3),  '4':   (0, 4),
            '5':   (0, 5),  '6':   (0, 6),  '7':   (0, 7),  '8':   (0, 8),
            '9':   (0, 9),  '10':  (0, 10), '11':  (0, 11), '12':  (0, 12),
            'A':   (1, 0),  'A1':  (1, 1),  'A2':  (1, 2),  'A3':  (1, 3),
            'A4':  (1, 4),  'A5':  (1, 5),  'A6':  (1, 6),  'A7':  (1, 7),
            'A8':  (1, 8),  'A9':  (1, 9),  'A10': (1, 10), 'A11': (1, 11), 'A12': (1, 12),
            'B':   (2, 0),  'B1':  (2, 1),  'B2':  (2, 2),  'B3':  (2, 3),
            'B4':  (2, 4),  'B5':  (2, 5),  'B6':  (2, 6),  'B7':  (2, 7),
            'B8':  (2, 8),  'B9':  (2, 9),  'B10': (2, 10), 'B11': (2, 11), 'B12': (2, 12),
            'C':   (3, 0),  'C1':  (3, 1),  'C2':  (3, 2),  'C3':  (3, 3),
            'C4':  (3, 4),  'C5':  (3, 5),  'C6':  (3, 6),  'C7':  (3, 7),
            'C8':  (3, 8),  'C9':  (3, 9),  'C10': (3, 10), 'C11': (3, 11), 'C12': (3, 12),
            'D':   (4, 0),  'D1':  (4, 1),  'D2':  (4, 2),  'D3':  (4, 3),
            'D4':  (4, 4),  'D5':  (4, 5),  'D6':  (4, 6),  'D7':  (4, 7),
            'D8':  (4, 8),  'D9':  (4, 9),  'D10': (4, 10), 'D11': (4, 11), 'D12': (4, 12),
            'E':   (5, 0),  'E1':  (5, 1),  'E2':  (5, 2),  'E3':  (5, 3),
            'E4':  (5, 4),  'E5':  (5, 5),  'E6':  (5, 6),  'E7':  (5, 7),
            'E8':  (5, 8),  'E9':  (5, 9),  'E10': (5, 10), 'E11': (5, 11), 'E12': (5, 12),
            'F':   (6, 0),  'F1':  (6, 1),  'F2':  (6, 2),  'F3':  (6, 3),
            'F4':  (6, 4),  'F5':  (6, 5),  'F6':  (6, 6),  'F7':  (6, 7),
            'F8':  (6, 8),  'F9':  (6, 9),  'F10': (6, 10), 'F11': (6, 11), 'F12': (6, 12),
            'G':   (7, 0),  'G1':  (7, 1),  'G2':  (7, 2),  'G3':  (7, 3),
            'G4':  (7, 4),  'G5':  (7, 5),  'G6':  (7, 6),  'G7':  (7, 7),
            'G8':  (7, 8),  'G9':  (7, 9),  'G10': (7, 10), 'G11': (7, 11), 'G12': (7, 12),
            'H':   (8, 0),  'H1':  (8, 1),  'H2':  (8, 2),  'H3':  (8, 3),
            'H4':  (8, 4),  'H5':  (8, 5),  'H6':  (8, 6),  'H7':  (8, 7),
            'H8':  (8, 8),  'H9':  (8, 9),  'H10': (8, 10), 'H11': (8, 11), 'H12': (8, 12),
        }
        for txt, pos in buttons.items():
            if 0 in pos:
                lbl = QtWidgets.QLabel(txt)
                lbl.setAlignment(Qt.AlignCenter)
                lbl.setFixedSize(50, 30)
                lbl.setStyleSheet('background-color: white; font-weight: bold; font-size: 20px')
                self.Wells[txt] = lbl
            else:
                btn = QtWidgets.QPushButton(txt)
                btn.setFixedSize(50, 30)
                btn.setStyleSheet('background-color: grey; font-size: 14px')
                self.Wells[txt] = btn
            self.Wells[' '].setStyleSheet('background-color: none')
            wellLayout.addWidget(self.Wells[txt], pos[0], pos[1])

    def connect_wells(self):
        """ Connect well buttons to the stub handler. """
        for txt, btn in self.Wells.items():
            if isinstance(btn, QtWidgets.QPushButton):
                btn.clicked.connect(partial(self._onWellClicked, txt))

    def _onWellClicked(self, txt):
        self.__logger.debug(f'Well clicked: {txt}')

    def getStepSize(self, positionerName, axis):
        parNameSuffix = self._getParNameSuffix(positionerName, axis)
        return float(self.pars['StepEdit' + parNameSuffix].text())

    def setStepSize(self, positionerName, axis, stepSize):
        parNameSuffix = self._getParNameSuffix(positionerName, axis)
        self.pars['StepEdit' + parNameSuffix].setText(str(stepSize))

    def updatePosition(self, positionerName, axis, position):
        parNameSuffix = self._getParNameSuffix(positionerName, axis)
        if 'Position' + parNameSuffix in self.pars:
            self.pars['Position' + parNameSuffix].setText(
                f'<strong>{position:.2f} µm</strong>'
            )

    def _getParNameSuffix(self, positionerName, axis):
        return f'{positionerName}--{axis}'


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
