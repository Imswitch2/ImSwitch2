from imswitch.imcommon.model import initLogger
from .PositionerManager import PositionerManager


class StepBoardPositionerManager(PositionerManager):
    """ PositionerManager for a positioner on a step board's DAC: a board that
    speaks the step-engine protocol (``StepProtocolScanEngine``,
    docs/devices/step-engine-protocol.rst). Its ``analogChannel`` is the
    board's ``<engine>/dac<i>``; in a scan the engine drives it, by hand this
    manager sets it (the protocol's ``SET``), between runs.

    Supports exactly one axis. The DAC holds what it was last set to and has
    no readback, so the tracked position is the one commanded: clamped to
    ``[minVolt, maxVolt]`` when they are given, unchanged when the board
    refuses (the error is logged).

    Manager properties:

    - ``conversionFactor`` -- µm per volt (the engine converts a scan's
      positions with it too)
    - ``minVolt`` -- the lowest voltage a move may command (optional)
    - ``maxVolt`` -- the highest voltage a move may command (optional)
    """

    def __init__(self, positionerInfo, name, **lowLevelManagers):
        if len(positionerInfo.axes) != 1:
            raise RuntimeError(f'{self.__class__.__name__} only supports one axis,'
                               f' {len(positionerInfo.axes)} provided.')
        self.__logger = initLogger(self, instanceName=name)
        props = positionerInfo.managerProperties
        self._conversionFactor = float(props['conversionFactor'])
        self._minVolt = props.get('minVolt')
        self._maxVolt = props.get('maxVolt')
        self._channel = str(positionerInfo.analogChannel)
        self._engineNamed = lowLevelManagers.get('scanEngineNamed')
        super().__init__(positionerInfo, name, initialPosition={
            axis: 0 for axis in positionerInfo.axes
        })

    def move(self, dist, axis):
        self.setPosition(self._position[axis] + dist, axis)

    def setPosition(self, position, axis):
        volts = float(position) / self._conversionFactor
        if self._minVolt is not None:
            volts = max(volts, float(self._minVolt))
        if self._maxVolt is not None:
            volts = min(volts, float(self._maxVolt))
        engineName = self._channel.split('/', 1)[0]
        engine = self._engineNamed(engineName) if callable(self._engineNamed) else None
        if engine is None:
            self.__logger.error(f'{self._channel}: no scan engine {engineName!r} sets it.')
            return
        try:
            engine.set(self._channel, volts)
        except Exception as error:
            self.__logger.error(f'{self._channel} not set to {volts:g} V: {error}')
            return
        self._position[axis] = volts * self._conversionFactor


# Copyright (C) 2020-2026 ImSwitch developers
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
