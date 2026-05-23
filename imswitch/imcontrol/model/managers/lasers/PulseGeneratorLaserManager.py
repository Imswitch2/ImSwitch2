"""Backend-agnostic laser manager driven by a ``PulseGeneratorManager``.

This is the v4-era replacement for
:class:`PulseStreamerLaserManager`.  Where the old class talked
exclusively to PulseStreamer-shaped APIs, this one consumes whatever
:class:`PulseGeneratorManager` backend is wired into
``lowLevelManagers['pulseGeneratorManager']`` — Teensy today,
PulseStreamer once its manager is also migrated, an NI digital-out card
someday.

Set ``managerName: 'PulseGeneratorLaserManager'`` in the setup JSON.

managerProperties
-----------------
- ``digitalChannel`` *(int, required)* — the pulse-generator channel
  index this laser is wired to.  HIGH = laser on.
- ``analogChannel`` *(int, optional)* — analog output channel used to
  set laser power, if the backend supports it.  Leave unset (or
  ``None``) for binary on/off lasers.
"""

from __future__ import annotations

from typing import Optional

from imswitch.imcommon.model import initLogger

from .LaserManager import LaserManager


class PulseGeneratorLaserManager(LaserManager):
    """Laser manager backed by a generic :class:`PulseGeneratorManager`.

    Behaviour:

    * :meth:`setEnabled` drives ``digitalChannel`` HIGH/LOW via the
      backend's ``setDigital``.
    * :meth:`setValue` sets ``analogChannel`` voltage via the backend's
      ``setAnalog``.  No-op if no analog channel is configured.

    Graceful degradation: if no pulse generator is wired at startup
    (``lowLevelManagers['pulseGeneratorManager'] is None``) — e.g.
    ``setupInfo.teensyPulse`` was left unset — the manager enters mock
    mode and logs warnings on every operation.  This matches the
    behaviour of :class:`PulseStreamerLaserManager`.
    """

    def __init__(self, laserInfo, name: str, **lowLevelManagers):
        self._logger = initLogger(self, instanceName=name)

        self._pulseGen = lowLevelManagers.get('pulseGeneratorManager')
        self._isMock = self._pulseGen is None

        # Read laser-info fields with defaults so a partial setup still
        # boots (matches the PulseStreamerLaserManager fall-through).
        self._digitalChannel: Optional[int] = getattr(laserInfo, 'digitalLine', None)
        self._analogChannel: Optional[int] = getattr(laserInfo, 'analogChannel', None)

        if self._isMock:
            self._logger.warning(
                'No pulseGeneratorManager wired (setupInfo.teensyPulse is '
                'None?).  PulseGeneratorLaserManager will accept calls '
                'but no hardware will be driven.'
            )

        # If the backend reports it doesn't support analog, treat this
        # laser as binary even if the user configured an analogChannel.
        # Lets a v3 firmware (no analog) consume the same setup file as
        # v4 + future analog backends.
        backend_supports_analog = (
            self._pulseGen is not None and self._pulseGen.supports_analog
        )
        effective_analog = (
            self._analogChannel if backend_supports_analog else None
        )
        if self._analogChannel is not None and not backend_supports_analog:
            self._logger.warning(
                f'laser {name}: analogChannel={self._analogChannel} configured '
                f'but backend does not support analog; treating as binary'
            )

        super().__init__(
            laserInfo, name,
            isBinary=effective_analog is None,
            valueUnits='V',
            valueDecimals=1,
        )

    def setEnabled(self, enabled: bool) -> None:
        if self._isMock:
            self._logger.debug(f'mock: setEnabled({enabled}) ignored')
            return
        if self._digitalChannel is None:
            self._logger.warning(
                'setEnabled called but no digitalChannel configured'
            )
            return
        self._pulseGen.setDigital(int(self._digitalChannel), bool(enabled))

    def setValue(self, voltage: float) -> None:
        if self._isMock:
            self._logger.debug(f'mock: setValue({voltage}) ignored')
            return
        if self._analogChannel is None:
            # Binary laser — ignore set-value calls.  The base class may
            # still call us with the on/off voltages depending on the
            # widget; that's the same shape PulseStreamerLaserManager had.
            return
        if not self._pulseGen.supports_analog:
            return
        self._pulseGen.setAnalog(int(self._analogChannel), float(voltage))


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
