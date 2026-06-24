from imswitch.imcommon.model import initLogger
from .LantzLaserManager import LantzLaserManager


class Cobolt0601LaserManager(LantzLaserManager):
    """ LaserManager for Cobolt 06-01 lasers. Uses digital modulation mode when
    scanning. Does currently not support DPL type lasers.

    Manager properties:

    - ``digitalPorts`` -- a string array containing the COM ports to connect
      to, e.g. ``["COM4"]``
    """

    def __init__(self, laserInfo, name, **_lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)

        super().__init__(laserInfo, name, isBinary=False, valueUnits='mW', valueDecimals=0,
                         driver='cobolt.cobolt0601.Cobolt0601_f2', **_lowLevelManagers)

        self._digitalMod = False
        # GUI/API setpoint in mW. Do not infer scan power from the hardware APC
        # setpoint: setting the GUI to 0 switches the laser to ACC/current-zero
        # mode and can otherwise leave a stale APC setpoint behind.
        self._setpoint_mw = 0.0
        # Tracks the GUI on/off state so we can restore it when a scan ends.
        # During a scan the master switch (l1) is forced on so the TTL gate
        # can pulse light; on scan exit we must return the master switch to
        # whatever the user had selected, otherwise the laser would emit
        # continuously in constant-power mode.
        self._enabled = False
        self._laser.enabled = False      # l0 first — ensure laser is off before any mode changes
        self._laser.digital_mod = False  # sdmes 0 — disable TTL gate
        self._laser.query('cp')          # enter constant-power mode (known clean state)
        self._laser.mode = 'APC'
        self._laser.autostart = False

    def setEnabled(self, enabled):
        self.__logger.debug(f'Laser turning {enabled}')
        self._enabled = enabled
        self._laser.enabled = enabled

    def setValue(self, power, enabled=True, for_scanning=False):
        power = float(power)
        self._setpoint_mw = power
        if self._digitalMod:
            self._setModPower(power)
        else:
            self._setBasicPower(power)

    def setScanModeActive(self, active):
        if active:
            self._laser.enter_mod_mode()   # em — enter modulation mode
            self._laser.digital_mod = True # sdmes 1 — enable TTL gate
            self._setModPower(self._setpoint_mw)  # slmp X — power when TTL is HIGH
            if self._setpoint_mw <= 0:
                # A zero GUI setpoint is an explicit off command. Keep the
                # master switch off even though the scan includes this laser.
                self._laser.enabled = False
                self.__logger.debug(
                    'scan mode ON requested at 0 mW: digital-mod set to 0, '
                    'master kept OFF')
                self._digitalMod = active
                return
            # Master switch ON so the TTL gate can produce light. This is
            # safe in digital-modulation mode: with sdmes 1 the beam stays
            # dark until the scanner drives this laser's TTL line HIGH.
            self._laser.enabled = True
            self.__logger.debug(
                'scan mode ON: digital-mod armed, master ON, '
                'P=%.1f mW (TTL-gated)', self._setpoint_mw)
        else:
            # SAFETY ORDER: drop the master switch BEFORE disabling the TTL
            # gate or changing modes. The master was forced ON during arming
            # (sdmes 1 keeps the beam dark until the scanner's TTL goes HIGH).
            # If we cleared the gate (sdmes 0) while the master was still ON,
            # the beam would emit at the modulation power for the serial-command
            # latency window before 'cp'/l0 land — the end-of-scan flash. Going
            # master-off first closes that window completely.
            self._laser.enabled = False
            self._laser.digital_mod = False
            # we go back to the mode before the scan
            if self._laser.mode == 'ACC':
                self._laser.query('ci')
            else:
                self._laser.query('cp')
            # Restore the master switch to the user's pre-scan selection.
            # Without this the laser would emit continuously in constant
            # power mode once the TTL gate is gone.
            self._laser.enabled = self._enabled
            self.__logger.debug('scan mode OFF: master forced off before mode '
                                'change, CP/CC restored, master=%s',
                                self._enabled)

        self._digitalMod = active

    def _setBasicPower(self, power):
        if power <= 0:
            self._laser.power_sp = 0
            self._laser.mode = 'ACC'
            self._laser.query('ci')
            self._laser.query('slc {:.1f}'.format(0))
        else:
            if self._laser.mode != 'APC':
                self._laser.power_sp = 0
                self._laser.query('cp')
                self._laser.mode = 'APC'
            self._laser.power_sp = power / self._numLasers


    def _setModPower(self, power):
        self._laser.power_mod = power / self._numLasers
        #self.__logger.debug(f'Set digital modulation mode power to: {power}')


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
