from imswitch.imcommon.model import initLogger
from .PositionerManager import PositionerManager
from imswitch.imcontrol.model.devices.graph import rs232BackedPrimarySpec


class MHXYStageManager(PositionerManager):
    """ PositionerManager for control of a Marzhauser XY-stage through RS232
    communication.

    The manager's tracked position is kept in the *controller's* absolute
    coordinate frame, seeded from the hardware at startup via ``?pos``. This
    matters because ``move()`` issues a relative ``mor`` while
    ``setPosition()`` issues an absolute ``moa``: if the tracked frame were
    seeded at zero (as it used to be) those two would disagree by however far
    the stage happened to be from hardware zero when ImSwitch started, and any
    absolute move would fly off to the wrong part of the travel range.

    Manager properties:

    - ``rs232device`` -- name of the defined rs232 communication channel
      through which the communication should take place
    """

    def __init__(self, positionerInfo, name, *args, **lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)

        if (len(positionerInfo.axes) != 2
                or 'X' not in positionerInfo.axes or 'Y' not in positionerInfo.axes):
            raise RuntimeError(f'{self.__class__.__name__} requires two axes named X and Y'
                               f' respectively, {positionerInfo.axes} provided.')

        # The RS232 channel must exist before the initial position query below.
        self._usingMockFallback = False
        try:
            self._rs232Manager = lowLevelManagers['rs232sManager'][
                positionerInfo.managerProperties['rs232device']
            ]
        except Exception as e:
            self.__logger.warning(
                f'Failed to initialize rs232sManager, falling back to mock mode: {e}'
            )
            from imswitch.imcontrol.model.interfaces.RS232Driver_mock import MockRS232Driver
            self._rs232Manager = MockRS232Driver(name='mock', settings={'port': 'Mock'})
            self._usingMockFallback = True
            self._setConnectionError(
                e,
                summary="Marzhauser RS232 backend unavailable; mock fallback active",
                mock_active=True,
            )

        super().__init__(positionerInfo, name, initialPosition=self._readHardwarePosition(
            list(positionerInfo.axes)
        ))

        try:
            self.__logger.info(f"MHXYStage serial no: {self._rs232Manager.query('?readsn')}")
        except Exception as e:
            self.__logger.warning(f"Failed to read stage serial number: {e}")

    def _readHardwarePosition(self, axes):
        """ Return ``{axis: position}`` read from the controller via ``?pos``.

        Falls back to zeros for any axis that cannot be read, so a mock port or
        an unresponsive controller degrades to the previous behaviour instead
        of preventing startup. Callers that rely on absolute moves should check
        :attr:`positionSynced` to know whether the frame is trustworthy.
        """
        fallback = {axis: 0.0 for axis in axes}
        try:
            reply = self._rs232Manager.query('?pos')
        except Exception as e:
            self.__logger.warning(
                f'Could not read stage position (?pos): {e}. Tracked position '
                f'starts at zero, so absolute moves will be offset from the '
                f'controller frame until a successful sync.'
            )
            self.positionSynced = False
            return fallback

        parsed = self._parsePositionReply(reply, axes)
        if parsed is None:
            self.__logger.warning(
                f'Could not parse stage position reply {reply!r}. Tracked '
                f'position starts at zero, so absolute moves will be offset '
                f'from the controller frame until a successful sync.'
            )
            self.positionSynced = False
            return fallback

        self.positionSynced = True
        if not self._usingMockFallback:
            self._setConnected("Marzhauser stage responding")
        self.__logger.info(
            f'MHXYStage position synced from hardware: '
            f'{ {axis: round(value, 3) for axis, value in parsed.items()} }'
        )
        return parsed

    @staticmethod
    def _parsePositionReply(reply, axes):
        """ Parse a Tango ``?pos`` reply into ``{axis: float}``, or None.

        The controller answers with one whitespace- (or comma-) separated value
        per configured axis, in axis order. Replies with fewer values than we
        have axes are rejected rather than partially applied.
        """
        if reply is None:
            return None

        tokens = str(reply).replace(',', ' ').split()
        values = []
        for token in tokens:
            try:
                values.append(float(token))
            except ValueError:
                continue

        if len(values) < len(axes):
            return None

        return {axis: values[index] for index, axis in enumerate(axes)}

    def syncPositionFromHardware(self):
        """ Re-read the controller position into the tracked position.

        Use before any absolute move that must land accurately after the stage
        may have been moved outside ImSwitch — most notably by the joystick,
        which the manager cannot observe.

        Returns True when the tracked position now reflects hardware.
        """
        positions = self._readHardwarePosition(list(self.axes))
        if not self.positionSynced:
            return False
        self.updateTrackedPosition(positions)
        return True


    def getDeviceDescriptorSpec(self):
        rs232_name = (self._positionerInfo.managerProperties or {}).get('rs232device')
        return rs232BackedPrimarySpec(
            category='positioner',
            rs232_name=str(rs232_name),
        )

    def move(self, value, axis):
        if axis == 'X':
            cmd = 'mor x ' + str(float(value))
        elif axis == 'Y':
            cmd = 'mor y ' + str(float(value))
        else:
            self.__logger.error('Wrong axis, has to be "X" or "Y".')
            return
        self._rs232Manager.query(cmd)
        self._position[axis] = self._position[axis] + value

    def setPosition(self, value, axis):
        if axis == 'X':
            cmd = 'moa x ' + str(float(value))
        elif axis == 'Y':
            cmd = 'moa y ' + str(float(value))
        else:
            self.__logger.error('Wrong axis, has to be "X" or "Y".')
            return
        self._rs232Manager.query(cmd)
        self._position[axis] = value


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
