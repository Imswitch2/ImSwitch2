import threading

from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.devices.graph import HardwareDeviceId
from imswitch.imcontrol.model.devices.lifecycle import (
    DeviceLifecycleAction,
    DeviceLifecycleCapabilities,
    DeviceLifecycleNotSupportedError,
    DeviceLifecycleResult,
)
from imswitch.imcontrol.model.devices.status import DeviceId
from imswitch.imcontrol.model.interfaces.lantzlasers import (
    LinkedLantzLaser,
    openLantzLaser,
    openMockLantzLaser,
)
from .LantzLaserManager import LantzLaserManager


class _LegacyCobolt0601Lifecycle:
    """Runtime probe/reconnect lifecycle for one legacy logical Cobolt laser."""

    capabilities = DeviceLifecycleCapabilities(probe=True, reconnect=True)

    def __init__(self, manager):
        self._manager = manager
        self._hardware_id = HardwareDeviceId(
            category='laser', key=f'laser:{manager.name}'
        )

    @property
    def hardware_id(self):
        return self._hardware_id

    def _unsupported(self, action):
        raise DeviceLifecycleNotSupportedError(
            f'Legacy Cobolt lifecycle does not support {action.value}.'
        )

    def connect(self):
        return self._unsupported(DeviceLifecycleAction.CONNECT)

    def disconnect(self):
        return self._unsupported(DeviceLifecycleAction.DISCONNECT)

    def shutdown(self):
        return self._unsupported(DeviceLifecycleAction.SHUTDOWN)

    def probe(self):
        return self._manager._probeLifecycle()

    def reconnect(self):
        return self._manager._reconnectLifecycle()


class Cobolt0601LaserManager(LantzLaserManager):
    """LaserManager for legacy Cobolt 06-01 lasers.

    The historical Lantz-facing API is retained for setup compatibility, but
    the included Cobolt driver is a vendored serial driver. Linked multi-port
    configurations are treated as one logical laser and reconnect atomically.

    Manager properties:

    - ``digitalPorts`` -- a string array containing the COM ports to connect
      to, e.g. ``["COM4"]``
    """

    def __init__(self, laserInfo, name, **_lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)
        self._ioLock = threading.RLock()

        super().__init__(laserInfo, name, isBinary=False, valueUnits='mW', valueDecimals=0,
                         driver='cobolt.cobolt0601.Cobolt0601_f2', **_lowLevelManagers)

        self._digitalMod = False
        # GUI/API setpoint in mW. Do not infer scan power from the hardware APC
        # setpoint: setting the GUI to 0 switches the laser to ACC/current-zero
        # mode and can otherwise leave a stale APC setpoint behind.
        self._setpoint_mw = 0.0
        # Tracks the GUI on/off state so we can restore it when a scan ends.
        self._enabled = False
        self._knownSerials = tuple(None for _ in self._ports)

        with self._ioLock:
            try:
                self._initializeSafeState(self._laser)
                if not self._backendIsMock:
                    self._knownSerials = self._readSerials(self._laser)
                    self._setConnected('Cobolt initialized; emission OFF')
            except Exception as exc:
                if self._backendIsMock:
                    # The shipped mock must satisfy the legacy manager API. If
                    # it does not, startup cannot provide a usable manager.
                    raise
                self.__logger.warning(
                    'Cobolt initialization failed after opening hardware; '
                    'falling back to mock: %s', exc
                )
                self._installMockFallback(
                    exc,
                    summary='Cobolt initialization failed; mock fallback active',
                    close_current=True,
                )

        self._lifecycle = _LegacyCobolt0601Lifecycle(self)

    # ------------------------------------------------------------------
    # Backend/lifecycle helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _isUnsupportedReply(reply):
        text = '' if reply is None else str(reply).lower()
        return 'syntax error' in text or 'illegal command' in text

    @staticmethod
    def _physicalLasers(laser):
        if isinstance(laser, LinkedLantzLaser):
            return tuple(laser.lasers)
        return (laser,)

    @staticmethod
    def _closeBackend(laser):
        if laser is None:
            return
        try:
            laser.finalize()
        except Exception:
            pass

    def _checkedQuery(self, laser, command):
        reply = laser.query(command)
        replies = reply if isinstance(reply, list) else [reply]
        for candidate in replies:
            if self._isUnsupportedReply(candidate):
                raise RuntimeError(
                    f'Cobolt rejected command {command!r}: {candidate}'
                )
        return reply

    def _initializeSafeState(self, laser):
        """Put every physical Cobolt in a known dark non-modulated state.

        Each port is handled independently. If one l0 fails, do not proceed to
        disable its TTL gate (which could expose emission with master still on),
        but continue trying to darken and initialize the remaining siblings.
        """
        first_error = None
        for physical in self._physicalLasers(laser):
            try:
                physical.enabled = False       # l0 first — remove master emission
                physical.digital_mod = False   # only after l0 succeeded
                self._checkedQuery(physical, 'cp')
                physical.mode = 'APC'
                physical.autostart = False
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def _readSerials(self, laser):
        serials = []
        for physical in self._physicalLasers(laser):
            serial = getattr(physical, 'serial_number', None)
            if callable(serial):
                serial = serial()
            if serial is None:
                serials.append(None)
                continue
            text = str(serial).strip()
            if not text or self._isUnsupportedReply(text):
                serials.append(None)
            else:
                serials.append(text)
        return tuple(serials)

    def _checkIdentity(self, candidate_serials):
        for port, expected, actual in zip(
            self._ports, self._knownSerials, candidate_serials
        ):
            # Old firmware may not support sn?. Only enforce identity when both
            # observations are available.
            if expected is not None and actual is not None and expected != actual:
                raise RuntimeError(
                    f'Cobolt identity mismatch on {port}: expected serial '
                    f'{expected!r}, found {actual!r}'
                )

    def _installMockFallback(self, error, *, summary, close_current):
        if close_current:
            self._closeBackend(getattr(self, '_laser', None))

        mock = openMockLantzLaser(self._driver, self._ports)
        self._laser = mock
        self._backendIsMock = True
        self._backendOpenError = error
        self._digitalMod = False
        self._enabled = False
        self._knownSerials = tuple(None for _ in self._ports)
        self._initializeSafeState(mock)
        self._setConnectionError(
            error,
            summary=summary,
            mock_active=True,
        )

    def _bestEffortDarken(self):
        """Attempt l0 independently on every physical laser.

        LinkedLantzLaser stops forwarding when one member raises, which is not
        sufficient for a fail-closed recovery path. Try every port separately
        so one dead transport does not prevent darkening its siblings.
        """
        for physical in self._physicalLasers(self._laser):
            try:
                physical.enabled = False
            except Exception as exc:
                self.__logger.warning(
                    'Could not verify Cobolt emission OFF during recovery: %s', exc
                )
        # These are desired logical states after a failed command. Physical
        # state remains unknown when transport I/O failed and is reflected by
        # the ERROR connection status.
        self._enabled = False
        self._digitalMod = False

    def _recordCommandFailure(self, exc):
        self._setConnectionError(
            exc,
            summary='Cobolt command/communication failed',
            mock_active=bool(self._backendIsMock),
        )

    def _probeLifecycle(self):
        device_id = DeviceId('laser', self.name)
        with self._ioLock:
            if self._backendIsMock:
                details = self.connectionStatusDetails or 'Mock fallback is active.'
                return DeviceLifecycleResult(
                    hardware_id=self._lifecycle.hardware_id,
                    action=DeviceLifecycleAction.PROBE,
                    success=False,
                    summary='Cobolt hardware is not active',
                    details=details,
                    affected_device_ids=(device_id,),
                )

            try:
                for port, physical in zip(self._ports, self._physicalLasers(self._laser)):
                    reply = physical.query('gfv?')
                    if (
                        reply is None
                        or not str(reply).strip()
                        or self._isUnsupportedReply(reply)
                    ):
                        raise RuntimeError(
                            f'Cobolt {port} returned no valid firmware response'
                        )
                self._setConnected('Cobolt probe succeeded')
                return DeviceLifecycleResult(
                    hardware_id=self._lifecycle.hardware_id,
                    action=DeviceLifecycleAction.PROBE,
                    success=True,
                    summary='Cobolt responded on all configured ports',
                    affected_device_ids=(device_id,),
                )
            except Exception as exc:
                self._setConnectionError(
                    exc,
                    summary='Cobolt probe failed',
                    mock_active=False,
                )
                return DeviceLifecycleResult(
                    hardware_id=self._lifecycle.hardware_id,
                    action=DeviceLifecycleAction.PROBE,
                    success=False,
                    summary='Cobolt probe failed',
                    details=str(exc),
                    affected_device_ids=(device_id,),
                )

    def _reconnectLifecycle(self):
        device_id = DeviceId('laser', self.name)
        with self._ioLock:
            preserved_setpoint = self._setpoint_mw

            # Best effort only: if transport was already lost we cannot prove
            # the old physical device received l0. Try every linked port.
            self._bestEffortDarken()

            old_backend = self._laser
            self._closeBackend(old_backend)

            candidate = None
            try:
                opened = openLantzLaser(
                    self._driver,
                    self._ports,
                    allowMockFallback=False,
                )
                candidate = opened.laser

                # Identity is read before any mutating initialization commands.
                candidate_serials = self._readSerials(candidate)
                self._checkIdentity(candidate_serials)

                self._initializeSafeState(candidate)

                self._laser = candidate
                candidate = None
                self._backendIsMock = False
                self._backendOpenError = None
                self._digitalMod = False
                self._enabled = False
                self._setpoint_mw = preserved_setpoint
                self._knownSerials = candidate_serials
                self._setConnected('Cobolt reconnected; emission forced OFF')

                return DeviceLifecycleResult(
                    hardware_id=self._lifecycle.hardware_id,
                    action=DeviceLifecycleAction.RECONNECT,
                    success=True,
                    summary='Cobolt reconnected; emission forced OFF',
                    affected_device_ids=(device_id,),
                    deactivated_device_ids=(device_id,),
                )
            except Exception as exc:
                self._closeBackend(candidate)
                self._setpoint_mw = preserved_setpoint
                failure = RuntimeError(
                    f'{exc}. Physical emission state could not be verified.'
                )
                result_summary = 'Cobolt reconnect failed; mock fallback active'
                try:
                    self._installMockFallback(
                        failure,
                        summary=result_summary,
                        close_current=False,
                    )
                except Exception as mock_exc:
                    # Preserve the reconnect failure as primary context but do
                    # not claim a mock backend exists if fallback itself broke.
                    failure = RuntimeError(
                        f'{failure} Mock fallback also failed: {mock_exc}'
                    )
                    result_summary = 'Cobolt reconnect failed; no usable backend'
                    self._backendIsMock = False
                    self._digitalMod = False
                    self._enabled = False
                    self._setConnected()  # reset runtime mode to REAL before ERROR
                    self._setConnectionError(
                        failure,
                        summary=result_summary,
                        mock_active=False,
                    )

                return DeviceLifecycleResult(
                    hardware_id=self._lifecycle.hardware_id,
                    action=DeviceLifecycleAction.RECONNECT,
                    success=False,
                    summary=result_summary,
                    details=str(failure),
                    affected_device_ids=(device_id,),
                    deactivated_device_ids=(device_id,),
                )

    def getDeviceLifecycle(self):
        return self._lifecycle

    # ------------------------------------------------------------------
    # Normal laser commands
    # ------------------------------------------------------------------

    def setEnabled(self, enabled):
        with self._ioLock:
            self.__logger.debug(f'Laser turning {enabled}')
            try:
                self._laser.enabled = enabled
            except Exception as exc:
                self._bestEffortDarken()
                self._recordCommandFailure(exc)
                raise
            self._enabled = bool(enabled)

    def setValue(self, power, enabled=True, for_scanning=False):
        power = float(power)
        with self._ioLock:
            self._setpoint_mw = power
            try:
                if self._digitalMod:
                    self._setModPower(power)
                else:
                    self._setBasicPower(power)
            except Exception as exc:
                self._bestEffortDarken()
                self._recordCommandFailure(exc)
                raise

    def setScanModeActive(self, active):
        with self._ioLock:
            try:
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
                    # gate or changing modes. See original manager comments.
                    self._laser.enabled = False
                    self._laser.digital_mod = False
                    if self._laser.mode == 'ACC':
                        self._checkedQuery(self._laser, 'ci')
                    else:
                        self._checkedQuery(self._laser, 'cp')
                    self._laser.enabled = self._enabled
                    self.__logger.debug(
                        'scan mode OFF: master forced off before mode change, '
                        'CP/CC restored, master=%s', self._enabled
                    )
            except Exception as exc:
                self._bestEffortDarken()
                self._recordCommandFailure(exc)
                raise

            self._digitalMod = active

    def _setBasicPower(self, power):
        if power <= 0:
            self._laser.power_sp = 0
            self._laser.mode = 'ACC'
            self._checkedQuery(self._laser, 'ci')
            self._checkedQuery(self._laser, 'slc {:.1f}'.format(0))
        else:
            if self._laser.mode != 'APC':
                self._laser.power_sp = 0
                self._checkedQuery(self._laser, 'cp')
                self._laser.mode = 'APC'
            self._laser.power_sp = power / self._numLasers

    def _setModPower(self, power):
        self._laser.power_mod = power / self._numLasers

    def finalize(self):
        with self._ioLock:
            try:
                # Best effort darkening before transport close.
                self._laser.enabled = False
            except Exception:
                pass
            self._closeBackend(self._laser)
            self._enabled = False
            self._digitalMod = False
            self._setFinalizedStatus()


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
