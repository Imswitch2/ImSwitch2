"""Backend-agnostic laser manager driven by a ``PulseGeneratorManager``.

This is the v4-era replacement for
:class:`PulseStreamerLaserManager`.  Where the old class talked
exclusively to PulseStreamer-shaped APIs, this one consumes whatever
:class:`PulseGeneratorManager` backend the setup builds -- the Teensy of
its ``teensyPulse`` section, or any ``pulseGenerators`` entry (a
PulseStreamer, a plugin's generator).

Set ``managerName: 'PulseGeneratorLaserManager'`` in the setup JSON. The
laser's ``digitalLine`` is the generator's channel index (HIGH = laser on)
and its ``analogChannel`` the analog output that sets its power, when the
generator has one.

managerProperties
-----------------
- ``pulseGenerator`` *(str, optional)* — the ``pulseGenerators`` entry it
  is wired to.  Omitted: the ``teensyPulse`` section's generator.
"""

from __future__ import annotations

from typing import Optional

from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.managers.pulsegen.loader import pulse_generator_named

from .LaserManager import LaserManager


class PulseGeneratorLaserManager(LaserManager):
    """Laser manager backed by a generic :class:`PulseGeneratorManager`.

    Behaviour:

    * :meth:`setEnabled` drives ``digitalChannel`` HIGH/LOW via the
      backend's ``setDigital``.
    * :meth:`setValue` sets ``analogChannel`` voltage via the backend's
      ``setAnalog``.  No-op if no analog channel is configured.

    Graceful degradation: if it names no generator and the setup has no
    ``teensyPulse`` section, the manager enters mock mode and logs warnings
    on every operation.  This matches the behaviour of
    :class:`PulseStreamerLaserManager`.  A generator it names and the setup
    does not have is a setup error.
    """

    def __init__(self, laserInfo, name: str, **lowLevelManagers):
        self._logger = initLogger(self, instanceName=name)

        props = getattr(laserInfo, 'managerProperties', None) or {}
        named = props.get('pulseGenerator')
        self._pulseGen, available = pulse_generator_named(lowLevelManagers, named)
        if named is not None and self._pulseGen is None:
            # A generator named and absent is a wrong setup, not a missing
            # Teensy: no mock stands in for it.
            raise ValueError(f'{name}: the setup has no pulse generator {named!r}; it has '
                             f'{", ".join(available) or "none"}.')
        self._isMock = self._pulseGen is None
        self._level = (laserInfo.ttlLevel if getattr(laserInfo, 'ttlActiveLevel', None)
                       else (lambda on: on))

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
        # On and off follow ttlActiveLevel, as a scan drives the line.
        self._pulseGen.setDigital(int(self._digitalChannel), self._level(bool(enabled)))

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
