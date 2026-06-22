from imswitch.imcommon.model import shortcut
from .PositionerManager import PositionerManager
import imswitch.imcontrol.model.interfaces.grbldriver as grbldriver

PHYS_TO_GRBL_FAC = .1
PHYS_TO_GRBL_FAC_Z = .1

DIR_X = -1
DIR_Y = 1
DIR_Z = 1


class GRBLStageManager(PositionerManager):
    """ PositionerManager for GRBL-based XYZ stages.

    Manager properties:

    - ``rs232device`` -- name of the defined RS232 communication channel
    - ``PHYS_TO_GRBL_FAC`` -- optional XY conversion factor (physical µm to GRBL units)
    - ``PHYS_TO_GRBL_FAC_Z`` -- optional Z conversion factor
    """

    def __init__(self, positionerInfo, name, **lowLevelManagers):
        self._rs232manager = lowLevelManagers['rs232sManager'][
            positionerInfo.managerProperties['rs232device']
        ]
        global PHYS_TO_GRBL_FAC, PHYS_TO_GRBL_FAC_Z
        try:
            PHYS_TO_GRBL_FAC = positionerInfo.managerProperties['PHYS_TO_GRBL_FAC']
            PHYS_TO_GRBL_FAC_Z = positionerInfo.managerProperties['PHYS_TO_GRBL_FAC_Z']
        except (KeyError, TypeError):
            pass

        self._backlash = None
        self.settle_time = 0.5

        super().__init__(positionerInfo, name, initialPosition={
            axis: 0 for axis in positionerInfo.axes
        })
        self.board = self._rs232manager._board

    def move(self, value, axis):
        if axis == 'X':
            self.board.move_rel((value * PHYS_TO_GRBL_FAC * DIR_X, 0, 0), blocking=False)
        elif axis == 'Y':
            self.board.move_rel((0, value * PHYS_TO_GRBL_FAC * DIR_Y, 0), blocking=False)
        elif axis == 'Z':
            self.board.move_rel((0, 0, value * PHYS_TO_GRBL_FAC_Z * DIR_Z), blocking=False)
        else:
            return
        self._position[axis] = self._position[axis] + value

    def setPosition(self, value, axis):
        self._position[axis] = value

    def closeEvent(self):
        self.board.close()

    try:
        from PyQt5 import QtCore as _QtCore

        @shortcut(actionId="grbl.jog.X.plus", defaultKey="Up",
                  displayName="Move up", initiallyBound=False)
        def key_moveXup(self):
            self.move(value=100, axis='X')

        @shortcut(actionId="grbl.jog.X.minus", defaultKey="Down",
                  displayName="Move down", initiallyBound=False)
        def key_moveXdown(self):
            self.move(value=-100, axis='X')

        @shortcut(actionId="grbl.jog.Y.minus", defaultKey="Left",
                  displayName="Move left", initiallyBound=False)
        def key_moveYleft(self):
            self.move(value=-100, axis='Y')

        @shortcut(actionId="grbl.jog.Y.plus", defaultKey="Right",
                  displayName="Move right", initiallyBound=False)
        def key_moveYright(self):
            self.move(value=100, axis='Y')

        @shortcut(actionId="grbl.jog.Z.plus", defaultKey="-",
                  displayName="Move Z up", initiallyBound=False)
        def key_moveZup(self):
            self.move(value=100, axis='Z')

        @shortcut(actionId="grbl.jog.Z.minus", defaultKey="+",
                  displayName="Move Z down", initiallyBound=False)
        def key_moveZdown(self):
            self.move(value=-100, axis='Z')

    except ImportError:
        pass


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
