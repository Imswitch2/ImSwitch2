"""Positioner manager for Thorlabs KDC101 single-axis motor controllers."""

from imswitch.imcommon.model import initLogger
from .PositionerManager import PositionerManager


class KDC101PositionerManager(PositionerManager):
    """PositionerManager for a Thorlabs KDC101-backed single-axis actuator.

    Manager properties:
        - port (str, required): Serial port, e.g. "COM15".
        - posConvFac (float, required): Encoder counts per ImSwitch position unit.
        - velConvFac (float, required): Encoder velocity conversion factor.
        - accConvFac (float, required): Encoder acceleration conversion factor.
        - positionUnit (str, default "um"): Unit label shown in the positioner UI.
        - homeOnInit (bool, default False): If true, home during construction.
    """

    def __init__(self, positionerInfo, name: str, **lowLevelManagers):
        if len(positionerInfo.axes) != 1:
            raise RuntimeError(
                f'{self.__class__.__name__} only supports one axis, '
                f'{len(positionerInfo.axes)} provided.'
            )

        axis = positionerInfo.axes[0]
        super().__init__(
            positionerInfo,
            name,
            initialPosition={axis: 0.0},
        )
        self.__logger = initLogger(self, instanceName=name)

        self._axis = axis
        properties = positionerInfo.managerProperties
        self.positionUnit = properties.get('positionUnit', properties.get('unit', 'um'))
        self._port = properties['port']
        self._posConvFac = float(properties['posConvFac'])
        self._velConvFac = float(properties['velConvFac'])
        self._accConvFac = float(properties['accConvFac'])

        if self._posConvFac == 0:
            raise ValueError('posConvFac must be non-zero.')

        self._device = self._getDeviceObj(self._port)
        self.device = self._device

        if self.device is None:
            self.__logger.error(
                'KDC101 positioner "%s" disabled; hardware driver is unavailable.',
                name,
            )
            return

        if properties.get('homeOnInit', False):
            self.__logger.info('Homing KDC101 positioner %s on %s', name, self._port)
            self.device.home()

        self.updatePosition()

    def move(self, dist: float, axis: str) -> None:
        """Move by a relative displacement in ImSwitch position units."""
        self._validate_axis(axis)
        target = self._position[axis] + dist
        self._move_to_units(target)
        self.updatePosition()

    def setPosition(self, position: float, axis: str) -> None:
        """Move to an absolute position in ImSwitch position units."""
        self._validate_axis(axis)
        self._move_to_units(position)
        self.updatePosition()

    def updatePosition(self) -> None:
        """Refresh cached position from the KDC101 status packet."""
        if self.device is None:
            return
        self._position[self._axis] = self._toPositionInUnits(
            self.device.status['position']
        )

    def jog_start(self, axis: str, sign: int) -> None:
        """Start continuous jogging when a consumer supports continuous motion."""
        self._validate_axis(axis)
        if self.device is not None:
            self.device.move_velocity(sign >= 0)

    def jog_stop(self, axis: str) -> None:
        """Stop continuous jogging and refresh the cached position."""
        self._validate_axis(axis)
        if self.device is not None:
            self.device.stop()
            self.updatePosition()

    def setJogDistanceInUnits(self, distInUnits: float) -> None:
        """Compatibility helper matching the legacy RS232 KDC101 manager."""
        if self.device is None:
            return
        self.device.set_jog_params(
            self._toEncPosition(distInUnits),
            self.device.jogparams['acceleration'],
            self.device.jogparams['max_velocity'],
        )

    def setJogAccelerationInUnits(self, accInUnits: float) -> None:
        if self.device is None:
            return
        self.device.set_jog_params(
            self.device.jogparams['step_size'],
            self._toEncAcceleration(accInUnits),
            self.device.jogparams['max_velocity'],
        )

    def setJogVelocityInUnits(self, velInUnits: float) -> None:
        if self.device is None:
            return
        self.device.set_jog_params(
            self.device.jogparams['step_size'],
            self.device.jogparams['acceleration'],
            self._toEncVelocity(velInUnits),
        )

    def setMoveVelocityInUnits(self, velInUnits: float) -> None:
        if self.device is None:
            return
        self.device.set_velocity_params(
            self.device.velparams['acceleration'],
            self._toEncVelocity(velInUnits),
        )

    def setMoveAccelerationInUnits(self, accInUnits: float) -> None:
        if self.device is None:
            return
        self.device.set_velocity_params(
            self._toEncAcceleration(accInUnits),
            self.device.velparams['max_velocity'],
        )

    def finalize(self) -> None:
        if self.device is not None and hasattr(self.device, 'close'):
            self.device.close()

    def _move_to_units(self, position: float) -> None:
        if self.device is None:
            return
        self.device.move_absolute(self._toEncPosition(position))

    def _validate_axis(self, axis: str) -> None:
        if axis != self._axis:
            raise ValueError(f'Unknown axis "{axis}" for KDC101 positioner "{self.name}".')

    def _toEncPosition(self, units: float) -> int:
        return int(units * self._posConvFac)

    def _toPositionInUnits(self, encPosition: float) -> float:
        return encPosition / self._posConvFac

    def _toEncVelocity(self, unitsPerS: float) -> int:
        return int(unitsPerS * self._velConvFac)

    def _toVelocityInUnits(self, encVelocity: float) -> float:
        return encVelocity / self._velConvFac

    def _toEncAcceleration(self, unitsPerSS: float) -> int:
        return int(unitsPerSS * self._accConvFac)

    def _toAccelerationInUnits(self, encAcceleration: float) -> float:
        return encAcceleration / self._accConvFac

    def _getDeviceObj(self, port: str):
        try:
            from thorlabs_apt_device.devices.kdc101 import KDC101
        except ImportError:
            self.__logger.error(
                'thorlabs_apt_device is not installed. Install it with: '
                'pip install thorlabs-apt-device'
            )
            return None

        try:
            device = KDC101(serial_port=port)
            self.__logger.info('Initialized KDC101 positioner on %s', port)
            return device
        except Exception:
            self.__logger.error('Failed to initialize KDC101 positioner on %s', port)
            return None


# Copyright (C) 2026 ImSwitch developers
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
