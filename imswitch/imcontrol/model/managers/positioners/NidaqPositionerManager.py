from .PositionerManager import PositionerManager
from imswitch.imcontrol.model.devices.status import DeviceConnectionState


class NidaqPositionerManager(PositionerManager):
    """ PositionerManager for analog-value NI-DAQ-controlled positioners.

    Manager properties:

    - ``conversionFactor`` -- float value
    - ``minVolt`` -- minimum voltage
    - ``maxVolt`` -- maximum voltage
    """

    requiresReference: bool = True
    persistsLastPosition: bool = True

    def __init__(self, positionerInfo, name, **lowLevelManagers):
        if len(positionerInfo.axes) != 1:
            raise RuntimeError(f'{self.__class__.__name__} only supports one axis,'
                               f' {len(positionerInfo.axes)} provided.')

        self._nidaqManager = lowLevelManagers['nidaqManager']
        self._conversionFactor = positionerInfo.managerProperties['conversionFactor']
        self._minVolt = positionerInfo.managerProperties['minVolt']
        self._maxVolt = positionerInfo.managerProperties['maxVolt']
        self._defaultReferenceVoltage = positionerInfo.managerProperties.get(
            'defaultReferenceVoltage'
        )
        if self._defaultReferenceVoltage is not None:
            if not self._minVolt <= self._defaultReferenceVoltage <= self._maxVolt:
                raise ValueError(
                    f'defaultReferenceVoltage {self._defaultReferenceVoltage} V for '
                    f'"{name}" is outside [{self._minVolt}, {self._maxVolt}] V.'
                )

        super().__init__(positionerInfo, name, initialPosition={
            axis: 0 for axis in positionerInfo.axes
        })
        if getattr(self._nidaqManager, 'isSimulated', False):
            self._setMockActive("Simulated NI-DAQ positioner backend")
        else:
            # An analog NI-DAQ output can prove that the DAQ board is
            # available, but it cannot verify that the physical positioner
            # connected to that output exists or responds. Connection status
            # is therefore intentionally not applicable for this device.
            self._setConnectionState(
                DeviceConnectionState.NOT_APPLICABLE,
                summary="Physical connection cannot be verified through NI-DAQ output",
            )

        # Simulation has no unknown physical stage state to synchronize.
        # Treat its software coordinates as referenced so headless/API scans
        # are not blocked by an interactive hardware-safety workflow.
        if getattr(self._nidaqManager, 'isSimulating', False):
            self.markReferenced()

    @property
    def defaultReferenceVoltage(self):
        return self._defaultReferenceVoltage

    @property
    def defaultReferencePosition(self):
        if self._defaultReferenceVoltage is None:
            return None
        return self._defaultReferenceVoltage * self._conversionFactor

    def positionToVoltage(self, position):
        return position / self._conversionFactor

    def move(self, dist, axis):
        self.setPosition(self._position[axis] + dist, axis)

    def _setPosition(self, position, axis, *, raise_on_error=False):
        succeeded = self._nidaqManager.setAnalog(
            target=self.name,
            voltage=self.positionToVoltage(position),
            min_val=self._minVolt,
            max_val=self._maxVolt,
            raise_on_error=raise_on_error,
        )
        # Older/fake low-level managers return None on success, so only an
        # explicit False means the write was rejected. In that case neither the
        # tracked position nor its persisted last-command value may advance.
        if succeeded is False:
            return False

        self._recordCommandedPosition(axis, position)
        return True

    def setPosition(self, position, axis):
        self._setPosition(position, axis, raise_on_error=False)

    def resetToCurrent(self):
        self.setPosition(self._position[self.axes[0]], self.axes[0])

    def get_abs(self, axis):
        if axis not in self._position:
            raise ValueError(f'Axis {axis} not available. Available axes: {list(self._position.keys())}')
        return self._position[axis]

    def _normalizePersistedPosition(self, axis, position):
        """Clamp a remembered software position to the current voltage range."""
        voltage = float(position) / self._conversionFactor
        clampedVoltage = min(max(voltage, self._minVolt), self._maxVolt)
        return clampedVoltage * self._conversionFactor

    def reference(self, axis=None, position=None):
        if axis is None:
            axis = self.axes[0]

        if axis not in self.axes:
            raise ValueError(
                f'Axis {axis} not available. Available axes: {self.axes}'
            )

        if position is None:
            if self._defaultReferenceVoltage is None:
                raise ValueError(
                    f'No defaultReferenceVoltage is configured for "{self.name}".'
                )
            position = self.defaultReferencePosition

        voltage = self.positionToVoltage(position)

        if not self._minVolt <= voltage <= self._maxVolt:
            raise ValueError(
                f'Reference position {position} maps to {voltage:.3f} V, '
                f'outside [{self._minVolt}, {self._maxVolt}] V.'
            )

        self._setPosition(position, axis, raise_on_error=True)
        self.markReferenced(axis)


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
