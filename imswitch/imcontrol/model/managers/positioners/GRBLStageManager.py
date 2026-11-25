from imswitch.imcommon.model import shortcut
from imswitch.imcontrol.model.devices.status import DeviceNotConnectedError
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

    @property
    def board(self):
        """The board driver, always the transport's current one (a reconnect
        replaces it)."""
        return self._rs232manager._board

    # ---- position after a reconnect --------------------------------------
    # A reopened board has lost its position. The stage then refuses to move
    # until the user references it (the Positioner panel's Reference action
    # homes the board); nothing homes by itself.
    @property
    def requiresReference(self) -> bool:
        return self.__dict__.get('_positionLost', False)

    @property
    def defaultReferencePosition(self) -> float:
        return 0.0

    def reference(self, axis=None, position=None):
        """Home the board (explicit user action); every axis is then known."""
        self.board.home()
        for known in self.axes:
            self._position[known] = 0
        self._positionLost = False
        self.markReferenced()
        self._setConnected('GRBL stage referenced')

    def _lifecycleSafeState(self, *, verified: bool):
        return []                       # a stage does not move by itself

    def _onTransportReconnected(self, real: bool):
        if not real:
            self._setConnectionError('serial port could not be reopened',
                                     summary='GRBL board did not reconnect')
            return ['serial port could not be reopened']
        self._positionLost = True
        self.markUnreferenced()
        self._setConnected('GRBL stage reconnected; position unknown, reference it before moving')
        return []

    def _requirePosition(self):
        self._requireConnected('move')
        if self.__dict__.get('_positionLost', False):
            raise DeviceNotConnectedError(
                f'{self.name}: move refused, the board was reconnected and its position '
                f'is unknown; reference (home) the stage first')

    def move(self, value, axis):
        self._requirePosition()
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
        self._requirePosition()
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
