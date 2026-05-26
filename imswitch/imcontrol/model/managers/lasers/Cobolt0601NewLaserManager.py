"""LaserManager for Cobolt 06-01 / 06-MLD lasers.

This implementation uses the **universal old-style Cobolt command set**
(``l0``/``l1``/``em``/``sdmes``/``slp``/``slmp``) directly via
``laser.send_cmd(...)`` rather than the SCPI commands that newer firmware
introduced. The 06-01 and 06-MLD units in the lab reject the SCPI variants
(``las:paus``, ``laser:runmode?``) with "illegal command", so the old
command set is the safe lowest common denominator across firmware revisions.

Safety design (matches the WFS reference at
``/Users/lenny/PycharmProjects/WidefieldStarss/src/WFS/cobolt_laser.py``):

- **Off state = digital modulation mode + ``l0``.** With ``l0`` the master
  switch is off; even if the mode were wrong, the beam cannot emit. The
  laser stays warm so re-enabling is instant. This is the only setting in
  which power changes are *guaranteed* not to leak through to the beam.
- **On state = constant-power mode + ``l1`` at the cached setpoint.**
- **Power changes never apply when the laser is "off".** They update the
  cached setpoint and are flushed on the next enable.
- **Autostart disabled (``@cobas 0``)** so the laser does not power on by
  itself when the controller boots.
"""

from imswitch.imcommon.model import initLogger, pythontools
from .LaserManager import LaserManager, normalise_ports
import importlib
import traceback


class Cobolt0601NewLaserManager(LaserManager):
    """LaserManager for Cobolt 06-01 lasers.

    Manager properties:

    - ``digitalPorts`` -- a string array containing the COM ports to connect
      to, e.g. ``["COM4"]``.
    - ``modulationPowerMw`` -- power (mW) preloaded into the modulation
      setpoint at startup, so that if external TTL is wired the laser
      will emit at this level when toggled on with TTL HIGH. Default 5 mW.
    """

    def __init__(self, laserInfo, name, **_lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)

        # Lazy import of hardware library
        try:
            from .PyCoboltManager import Cobolt06
            self._Cobolt06 = Cobolt06
        except ImportError as e:
            self.__logger.error(f'Failed to import PyCoboltManager library: {e}')
            raise

        self._port = normalise_ports(laserInfo.managerProperties['digitalPorts'])[0]
        self._modulation_power_mw = float(
            laserInfo.managerProperties.get('modulationPowerMw', 5.0)
        )

        # Cached state — power setpoint chosen by GUI/script; only flushed
        # to the laser when _enabled is True.
        self._setpoint_mw = 0
        self._enabled = False
        self._real_hw = False

        self.__logger.debug(f'Initializing Cobolt laser {name} on {self._port}')
        try:
            self._laser = self._Cobolt06(port=self._port)
            self._real_hw = True
            self._init_safe_state()
            super().__init__(laserInfo, name, isBinary=False,
                             valueUnits='mW', valueDecimals=0)
        except Exception:
            err = traceback.format_exc()
            self.__logger.error(
                f'Failed to open real Cobolt {name} on {self._port}; loading mock.\n{err}'
            )
            package = importlib.import_module(
                pythontools.joinModulePath(
                    'imswitch.imcontrol.model.lantzdrivers_mock.cobolt.', 'cobolt0601'
                )
            )
            driver = getattr(package, 'MockCobolt06')
            self._laser = driver(self._port)
            self._laser.initialize()
            super().__init__(laserInfo, name, isBinary=False,
                             valueUnits='mW', valueDecimals=0)

    # ------------------------------------------------------------------
    # Raw command shim — bypasses the SCPI helpers in PyCoboltManager.
    # ------------------------------------------------------------------

    def _cmd(self, command: str) -> str:
        """Send a raw Cobolt command and return the reply.

        Wraps ``send_cmd`` so we can centrally log rejections without
        crashing the manager. Returns the controller reply string, or an
        empty string on transport failure.
        """
        try:
            reply = self._laser.send_cmd(command)
        except Exception as e:
            self.__logger.warning(f'Command {command!r} failed: {e}')
            return ''
        if reply and ('illegal command' in reply.lower()
                      or 'syntax error' in reply.lower()):
            self.__logger.warning(
                f'Cobolt firmware rejected {command!r}: {reply!r}'
            )
        return reply or ''

    # ------------------------------------------------------------------
    # Safe-state setup + ImSwitch API
    # ------------------------------------------------------------------

    def _init_safe_state(self) -> None:
        """Put the laser into the safe state at startup.

        - autostart off (``@cobas 0``)
        - modulation mode armed at the configured idle power
        - digital modulation enabled (so external TTL gates emission)
        - master ``l0`` so even without TTL the beam is dark
        """
        self._cmd('@cobas 0')
        # Pre-load the modulation power setpoint. Uses ``slmp <mW>``.
        self._cmd(f'slmp {float(self._modulation_power_mw)}')
        # Enter modulation mode (``em``) and enable digital gating (``sdmes 1``).
        self._cmd('em')
        self._cmd('sdmes 1')
        # Master off.
        self._cmd('l0')
        self._enabled = False
        self.__logger.info(
            f'Cobolt {self._port} initialised in safe state '
            f'(modulation mode, master off, modulation power = '
            f'{self._modulation_power_mw} mW). Key may need cycling once.'
        )

    def setEnabled(self, enabled: bool) -> None:
        """ImSwitch on/off toggle.

        - ``False`` → modulation mode + ``l0`` (beam cannot emit).
        - ``True``  → constant-power mode at the cached setpoint + ``l1``.
        """
        if enabled:
            # Constant-power mode at the cached setpoint, then master on.
            if self._setpoint_mw > 0:
                self._cmd(f'slp {float(self._setpoint_mw) / 1000.0}')
            self._cmd('cp')
            self._cmd('l1')
            self._enabled = True
            self.__logger.debug(
                f'Cobolt {self._port} ON  (CP mode, {self._setpoint_mw} mW)'
            )
        else:
            # Master off first, then return to modulation mode as safe state.
            self._cmd('l0')
            self._cmd(f'slmp {float(self._modulation_power_mw)}')
            self._cmd('em')
            self._cmd('sdmes 1')
            self._enabled = False
            self.__logger.debug(f'Cobolt {self._port} OFF (modulation safe state)')

    def setValue(self, power) -> None:
        """Update the constant-power setpoint.

        Only flushed to the laser when ``_enabled`` is True. When the
        laser is off, the new value is cached and will take effect on
        the next ``setEnabled(True)``. This prevents the well-known
        "I dialled the knob and the off laser fired" hazard.
        """
        try:
            value = int(power)
        except (TypeError, ValueError):
            self.__logger.warning(f'Ignoring non-numeric setValue({power!r})')
            return
        self._setpoint_mw = value

        if not self._enabled:
            self.__logger.debug(
                f'Cobolt {self._port} setpoint cached at {value} mW (laser is off)'
            )
            return

        # Laser is on → flush immediately.
        self._cmd(f'slp {float(value) / 1000.0}')
        self.__logger.debug(f'Cobolt {self._port} power -> {value} mW')

    def setScanModeActive(self, active: bool) -> None:
        """Switch between continuous (off) and scan/digital-modulation (on).

        In scan mode the laser is in digital-modulation mode at the cached
        power setpoint and emission is gated by external TTL. Outside scan
        mode the laser returns to whatever ``_enabled`` says.
        """
        if active:
            # Use the GUI setpoint as the modulation power if non-zero,
            # otherwise fall back to the idle modulation power.
            mod_power = self._setpoint_mw if self._setpoint_mw > 0 \
                else self._modulation_power_mw
            self._cmd(f'slmp {float(mod_power)}')
            self._cmd('em')
            self._cmd('sdmes 1')
            self._cmd('l1')   # master on; TTL gates the actual emission
            self.__logger.debug(
                f'Cobolt {self._port} scan mode ON (digital modulation, '
                f'mod power = {mod_power} mW)'
            )
        else:
            # Leave scan mode — back to the saved enable state.
            self.setEnabled(self._enabled)

    def setModulationEnabled(self, enabled: bool) -> None:
        self._cmd(f'sdmes {1 if enabled else 0}')

    def setModulationPower(self, power) -> None:
        try:
            value = float(power)
        except (TypeError, ValueError):
            return
        self._cmd(f'slmp {value}')

    def getModulationPower(self):
        try:
            return float(self._cmd('glmp?'))
        except (ValueError, TypeError):
            return self._modulation_power_mw

    def finalize(self) -> None:
        """Drive the laser to a hard-off state on shutdown."""
        try:
            self._cmd('l0')
        except Exception:
            self.__logger.error(
                f'Could not turn off Cobolt {self._port} during finalize:\n'
                f'{traceback.format_exc()}'
            )

    def getAllDeviceNames(self):  # legacy hook
        try:
            from .PyCoboltManager import list_lasers
            return list_lasers()
        except Exception:
            return []


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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
