import json
import os

from imswitch.imcommon.model import initLogger, dirtools
from .PositionerManager import PositionerManager

# Where the last commanded position of each TriggerScope axis is remembered
# between ImSwitch sessions. One JSON object keyed by positioner name -> axis.
_PERSISTENCE_FILENAME = 'triggerscope_positions.json'


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
        self._persistenceFile = os.path.join(
            dirtools.UserFileDirs.Config, _PERSISTENCE_FILENAME
        )
        super().__init__(positionerInfo, name, initialPosition={
            axis: 0 for axis in positionerInfo.axes
        })

        # Re-adopt the last known position WITHOUT moving the DAC. The board is
        # still holding that voltage from the previous session, so this restores
        # the software's view of the hardware rather than guessing or homing.
        self._restorePersistedPosition()

    def move(self, dist, axis):
        self.setPosition(self._position[self.axes[0]] + dist, axis)

    def setPosition(self, position, axis):
        # Clamp to the reachable voltage range and store the position that was
        # actually commanded (not the requested one). The board has no readback,
        # so this is the only way to keep the tracked position consistent with
        # the hardware instead of drifting outside the valid range.
        voltage = position / self._conversionFactor
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
        self._position[self.axes[0]] = clampedVoltage * self._conversionFactor
        self._persistPosition()

    def closeEvent(self):
        pass

    # ------------------------------------------------------------------
    # Position persistence (no motion is ever issued from these)
    # ------------------------------------------------------------------

    def _restorePersistedPosition(self):
        """ Restore the tracked position from disk without commanding the DAC.

        The stored value is clamped to the axis' current ``[minVolt, maxVolt]``
        range in case the setup changed since it was written. No ``setAnalog``
        call is made — the board is assumed to still hold this voltage.
        """
        stored = self._loadPersistedPosition()
        if stored is None:
            return
        voltage = stored / self._conversionFactor
        clampedVoltage = min(max(voltage, self._minVolt), self._maxVolt)
        self._position[self.axes[0]] = clampedVoltage * self._conversionFactor
        self.__logger.info(
            'Restored last known position %s for "%s" without moving the DAC '
            '(the board is assumed to still hold this voltage from the previous '
            'session).', self._position[self.axes[0]], self.name
        )

    def _loadPersistedPosition(self):
        """ Return the persisted position for this axis, or ``None`` if absent
        or unreadable. """
        try:
            with open(self._persistenceFile, 'r') as f:
                data = json.load(f)
            return data[self.name][self.axes[0]]
        except (FileNotFoundError, KeyError, ValueError, OSError):
            return None

    def _persistPosition(self):
        """ Write the current tracked position to disk, merging with any other
        positioners already recorded in the file. Failures are non-fatal. """
        try:
            os.makedirs(os.path.dirname(self._persistenceFile), exist_ok=True)
            try:
                with open(self._persistenceFile, 'r') as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    data = {}
            except (FileNotFoundError, ValueError, OSError):
                data = {}
            data.setdefault(self.name, {})[self.axes[0]] = \
                self._position[self.axes[0]]
            with open(self._persistenceFile, 'w') as f:
                json.dump(data, f, indent=2)
        except OSError:
            self.__logger.warning(
                'Could not persist position for "%s" to %s; the position will '
                'not survive a restart.', self.name, self._persistenceFile,
                exc_info=True
            )


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
