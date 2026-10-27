import json
import os

from imswitch.imcommon.model import initLogger, dirtools
from .PositionerManager import PositionerManager

# Pre-generalization TriggerScope persistence. Kept as a read-only migration
# source so existing users retain their last commanded position after upgrade.
_LEGACY_PERSISTENCE_FILENAME = 'triggerscope_positions.json'


class TriggerScopePositionerManager(PositionerManager):
    """ PositionerManager for positioners wired to a TriggerScope DAC channel.

    Converts position values to DAC voltages using ``conversionFactor``
    (position = voltage × conversionFactor) and delegates to the board-level
    ``TriggerScopeManager`` via ``lowLevelManagers['triggerScopeManager']``.

    Supports exactly one axis per instance.

    The board is open-loop with no position readback, but it is *stateful*: the
    DAC holds its last commanded voltage for as long as the board stays powered.
    An ImSwitch restart or crash is only a serial reconnect — the hardware keeps
    its voltage; only the software forgets it. Two things keep the tracked
    position honest across that gap:

    - The tracked position is always derived from the voltage that was actually
      commanded (clamped to ``[minVolt, maxVolt]``), never from a requested
      value that the board would have refused. A request that maps outside the
      range is clamped to the reachable boundary, so e.g. a negative move on a
      ``minVolt=0`` axis parks at 0 instead of leaving the display showing a
      position the hardware never reached.
    - The last commanded position is persisted to disk and, on startup, the
      tracked position is restored from it **without commanding the DAC**. No
      motion is issued, so a manually centred stage keeps its focus across
      restarts and crashes — we re-adopt the last known truth instead of homing
      to 0 (which would throw the focus away and only allow motion in one
      direction). The only case this cannot cover is the board itself losing
      power (the DAC then resets and there is no readback to detect it); the
      operator's normal re-centre through the GUI re-commands the DAC and
      re-syncs software and hardware.

    Manager properties:

    - ``conversionFactor`` -- position units per volt
    - ``minVolt`` -- minimum allowed DAC voltage
    - ``maxVolt`` -- maximum allowed DAC voltage
    """

    requiresReference: bool = True
    persistsLastPosition: bool = True

    def __init__(self, positionerInfo, name, **lowLevelManagers):
        if len(positionerInfo.axes) != 1:
            raise RuntimeError(
                f'{self.__class__.__name__} only supports one axis,'
                f' {len(positionerInfo.axes)} provided.'
            )

        self.__logger = initLogger(self, instanceName=name)
        self._triggerScopeManager = lowLevelManagers['triggerScopeManager']
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

        # Migrate the old TriggerScope-only store once if the new generic store
        # has no value for this axis. No DAC command is issued.
        if not self.isPositionRestored(self.axes[0]):
            self._restoreLegacyPersistedPosition()

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
        self.setPosition(self._position[self.axes[0]] + dist, axis)

    def setPosition(self, position, axis):
        # Clamp to the reachable voltage range and store the position that was
        # actually commanded (not the requested one). The board has no readback,
        # so this is the only way to keep the tracked position consistent with
        # the hardware instead of drifting outside the valid range.
        voltage = self.positionToVoltage(position)
        clampedVoltage = min(max(voltage, self._minVolt), self._maxVolt)
        if clampedVoltage != voltage:
            self.__logger.warning(
                f'Requested position {position} maps to {voltage:.3f} V, '
                f'outside [{self._minVolt}, {self._maxVolt}] V for "{self.name}".'
                f' Clamping to {clampedVoltage:.3f} V '
                f'(position {clampedVoltage * self._conversionFactor}).'
            )
        self._triggerScopeManager.setAnalog(
            target=self.name,
            voltage=clampedVoltage
        )
        self._recordCommandedPosition(
            self.axes[0], clampedVoltage * self._conversionFactor
        )

    def closeEvent(self):
        pass

    def _normalizePersistedPosition(self, axis, position):
        """Clamp a remembered software position to the current voltage range."""
        voltage = float(position) / self._conversionFactor
        clampedVoltage = min(max(voltage, self._minVolt), self._maxVolt)
        return clampedVoltage * self._conversionFactor

    def _restoreLegacyPersistedPosition(self):
        """Migrate the old TriggerScope-only persistence file without motion."""
        path = os.path.join(
            dirtools.UserFileDirs.Config, _LEGACY_PERSISTENCE_FILENAME
        )
        try:
            with open(path, 'r') as f:
                data = json.load(f)
            stored = data[self.name][self.axes[0]]
        except (FileNotFoundError, KeyError, ValueError, OSError):
            return

        position = self._normalizePersistedPosition(self.axes[0], stored)
        self._adoptPersistedPosition(self.axes[0], position, persist=True)
        self.__logger.info(
            'Migrated persisted position %s for "%s" from %s without moving '
            'the DAC.', position, self.name, path,
        )

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

        self.setPosition(position, axis)
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
