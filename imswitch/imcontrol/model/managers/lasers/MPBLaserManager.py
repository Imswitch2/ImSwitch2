import math
import threading
import time

import numpy as np

from imswitch.imcommon.model import initLogger
from .LaserManager import LaserManager


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(value)


class MPBLaserManager(LaserManager):
    """MPB Communications laser controlled over an ImSwitch RS232 manager.

    MPB emission has two distinct off paths:

    * normal ``setEnabled(False)`` / ``setValue(0)`` / ``finalize()`` calls
      ramp the live APC output down to the device-reported minimum before
      opening the laser-diode enable;
    * :meth:`emergencyDisable` opens the enable immediately and is intended for
      interlocks and failed ramp recovery.

    The ramp profile is deliberately configured per setup because the correct
    timing is device/firmware dependent. Existing setups use a conservative
    two-second, twenty-step profile until vendor-specific values are supplied.
    """

    def __init__(self, laserInfo, name, **kwargs):
        self.__logger = initLogger(self, instanceName=name)
        self._isMock = False
        self._rs232manager = None
        self._enabled = None
        self._desired_power = None
        self._command_lock = threading.RLock()
        self.__min_max_powers = [0, 1000]  # fallback for a mock/unavailable UI

        properties = laserInfo.managerProperties or {}
        self._ramp_down_enabled = _as_bool(
            properties.get('rampDownEnabled', True)
        )
        self._ramp_down_duration_s = max(
            0.0, float(properties.get('rampDownDurationS', 2.0))
        )
        self._ramp_down_steps = max(
            1, int(properties.get('rampDownSteps', 20))
        )
        self._ramp_down_dwell_s = max(
            0.0, float(properties.get('rampDownDwellS', 0.0))
        )
        self._use_mock_on_failure = _as_bool(
            properties.get('useMockOnFailure', True)
        )

        try:
            self._rs232manager = kwargs['rs232sManager']._subManagers[
                properties['rs232device']
            ]
            # Recovery comes before identity/diagnostic queries so a laser left
            # live by a crashed process spends the least possible time emitting.
            mode_reply = self._queryRequired('GETPOWERENABLE')

            if self._isApcModeReply(mode_reply):
                # Response format: 'F >99 3050'. APC power limits are required
                # to build a valid equipment-friendly recovery ramp.
                raw_limits = self._queryRequired('GETPOWERSETPTLIM 0')
                self.__min_max_powers = self._parsePowerLimits(raw_limits)
                # State is unknown after opening the port. If the previous
                # process crashed while emitting, recover with the same gentle
                # ramp used for a normal operator OFF.
                self.safeDisable(reason='startup recovery')
            else:
                # SETPOWER is not effective in ACC mode. Personnel safety wins:
                # darken immediately, change mode while dark, and never perform
                # the old OFF -> APC -> ON sequence.
                self.emergencyDisable(reason='startup mode correction')
                self.setMode(1)
                confirmed_mode = self._queryRequired('GETPOWERENABLE')
                if not self._isApcModeReply(confirmed_mode):
                    raise RuntimeError(
                        f'MPB laser did not enter APC mode: {confirmed_mode!r}'
                    )
                raw_limits = self._queryRequired('GETPOWERSETPTLIM 0')
                self.__min_max_powers = self._parsePowerLimits(raw_limits)

            self.__logger.debug(
                f'Power limits: {self.__min_max_powers[0]}–'
                f'{self.__min_max_powers[1]} mW'
            )
            serial_number = self._queryRequired('GETSN')
            self.__logger.debug(f'MPB laser {name}, SN: {serial_number}')

            self.setTriggerSource(0)  # internal; not implemented by this driver

        except Exception as exc:
            # Initialization is the recovery path after a crashed ImSwitch
            # process. Never enter mock fallback without first making an
            # independent best-effort attempt to darken a real connected unit.
            off_acknowledged = self._bestEffortImmediateOff()
            message = (
                f'MPB laser {name!r} initialization failed: {exc}. '
                f'Best-effort immediate OFF '
                f'{"was acknowledged" if off_acknowledged else "was not acknowledged"}.'
            )
            if not off_acknowledged:
                self.__logger.critical(message)
            elif self._use_mock_on_failure:
                self.__logger.warning(f'{message} Entering mock mode.')

            if not self._use_mock_on_failure:
                raise RuntimeError(message) from exc
            self._isMock = True

        super().__init__(
            laserInfo,
            name,
            isBinary=False,
            valueUnits='mW',
            valueDecimals=0,
        )

    @staticmethod
    def _parsePowerLimits(raw):
        """Parse ``GETPOWERSETPTLIM`` into whole-mW ``[minimum, maximum]``.

        Some units report fractional limits (``'199.9 3050.0'``). Setpoints
        are sent as whole mW, the form in field use, so the limits are
        rounded inward -- the minimum up, the maximum down -- and no setpoint
        or ramp step derived from them can fall outside what the unit
        reported.
        """
        try:
            low, high = (
                float(value) for value in str(raw).split('>')[-1].split()
            )
            values = [math.ceil(low), math.floor(high)]
        except (ValueError, OverflowError):
            raise ValueError(
                f'Unexpected MPB power-limit reply: {raw!r}'
            ) from None
        if values[0] < 0 or values[1] <= values[0]:
            raise ValueError(f'Unexpected MPB power-limit reply: {raw!r}')
        return values

    @staticmethod
    def _isApcModeReply(raw):
        return str(raw).split('>')[-1].strip() == '1'

    def _bestEffortImmediateOff(self):
        if self._rs232manager is None:
            return False
        try:
            self._queryRequired('SETLDENABLE 0')
        except Exception:
            return False
        self._enabled = False
        return True

    def _queryRequired(self, command):
        reply = self._rs232manager.query(command)
        if reply is None or not str(reply).strip():
            raise RuntimeError(f'MPB command {command!r} returned no acknowledgement')
        return reply

    def _setHardwarePower(self, power):
        value = int(np.clip(
            power,
            self.__min_max_powers[0],
            self.__min_max_powers[1],
        ))
        self._queryRequired(f'SETPOWER 0 {value}')
        return value

    def _readOutputPower(self):
        raw = self._queryRequired('POWER 0')
        return float(str(raw).split('>')[-1].strip())

    def _rampDownFrom(self, starting_power):
        minimum = self.__min_max_powers[0]
        if starting_power <= minimum:
            return

        raw_targets = np.linspace(
            float(starting_power),
            float(minimum),
            self._ramp_down_steps + 1,
        )[1:]
        targets = []
        for target in raw_targets:
            value = int(round(target))
            if not targets or value != targets[-1]:
                targets.append(value)

        interval_s = (
            self._ramp_down_duration_s / max(1, len(targets) - 1)
            if len(targets) > 1 else 0.0
        )
        for index, target in enumerate(targets):
            self._setHardwarePower(target)
            if interval_s > 0 and index < len(targets) - 1:
                time.sleep(interval_s)

        if self._ramp_down_dwell_s > 0:
            time.sleep(self._ramp_down_dwell_s)

    def safeDisable(self, *, reason='normal off'):
        """Ramp a live APC output down, then open the diode enable.

        The desired GUI/script setpoint is intentionally retained. The ramp
        changes only the temporary hardware setpoint; a later enable flushes
        the retained value before allowing emission.

        If output monitoring or any ramp step fails, the manager logs the loss
        of graceful shutdown and falls back to immediate OFF. An immediate-OFF
        failure is raised because the beam state is then unknown.
        """
        if self._isMock:
            return True

        with self._command_lock:
            ramp_error = None
            if self._enabled is not False and self._ramp_down_enabled:
                try:
                    starting_power = self._readOutputPower()
                    self._rampDownFrom(starting_power)
                except Exception as exc:
                    ramp_error = exc
                    self.__logger.error(
                        f'MPB graceful ramp failed during {reason}: {exc}. '
                        f'Falling back to immediate OFF.'
                    )

            try:
                self._queryRequired('SETLDENABLE 0')
            except Exception:
                self._enabled = None
                self.__logger.critical(
                    f'MPB immediate OFF failed during {reason}; laser state is '
                    f'unknown.',
                    exc_info=True,
                )
                raise

            self._enabled = False
            if ramp_error is None:
                self.__logger.debug(f'MPB laser safely disabled ({reason}).')
            return True

    def emergencyDisable(self, *, reason='emergency off'):
        """Immediately disable emission without a ramp.

        This path is for personnel-safety interlocks and for recovery when the
        equipment-friendly ramp cannot be completed.
        """
        if self._isMock:
            return True
        with self._command_lock:
            try:
                self._queryRequired('SETLDENABLE 0')
            except Exception:
                self._enabled = None
                self.__logger.critical(
                    f'MPB immediate OFF failed during {reason}; laser state is '
                    f'unknown.',
                    exc_info=True,
                )
                raise
            self._enabled = False
            return True

    def setEnabled(self, enabled):
        if self._isMock:
            return
        if not enabled:
            return self.safeDisable(reason='setEnabled(False)')

        with self._command_lock:
            if self._desired_power is None or self._desired_power <= 0:
                self.__logger.warning(
                    'Ignoring MPB enable request because no positive power '
                    'setpoint is selected.'
                )
                self.emergencyDisable(reason='invalid enable request')
                return False

            # The preceding OFF ramp left the physical setpoint at minimum.
            # Restore the user-requested setpoint before closing the enable.
            try:
                self._setHardwarePower(self._desired_power)
                self._queryRequired('SETLDENABLE 1')
            except Exception:
                self.__logger.error(
                    'MPB enable sequence failed; reverting to immediate OFF.',
                    exc_info=True,
                )
                try:
                    self.emergencyDisable(reason='failed enable recovery')
                except Exception:
                    self._enabled = None
                raise
            self._enabled = True
            return True

    def setValue(self, power):
        if self._isMock:
            return
        try:
            numeric_power = float(power)
        except (TypeError, ValueError):
            self.__logger.warning(f'Ignoring non-numeric MPB power {power!r}')
            return

        if numeric_power <= 0:
            with self._command_lock:
                self._desired_power = 0
                self.safeDisable(reason='zero power request')
            return

        clipped_power = int(np.clip(
            numeric_power,
            self.__min_max_powers[0],
            self.__min_max_powers[1],
        ))
        with self._command_lock:
            if self._enabled:
                self._setHardwarePower(clipped_power)
            self._desired_power = clipped_power

    def getValue(self):
        if self._isMock:
            return 0
        with self._command_lock:
            return self._readOutputPower()

    def setMode(self, mode):
        if self._isMock:
            return
        self._queryRequired(f'POWERENABLE {int(mode)}')

    def setTriggerSource(self, source):
        pass  # TODO: no verified MPB command profile available yet

    def setDigitalMod(self, digital, initialValue):
        pass

    def check_that_laser_is_in_APC_mode(self):
        """Ensure APC mode without ever re-enabling emission."""
        if self._isMock:
            return
        with self._command_lock:
            answer = self._queryRequired('GETPOWERENABLE')
            if not self._isApcModeReply(answer):
                self.emergencyDisable(reason='APC mode correction')
                self.setMode(1)
            confirmed = self._queryRequired('GETPOWERENABLE')
            if not self._isApcModeReply(confirmed):
                raise RuntimeError(
                    f'MPB laser did not enter APC mode: {confirmed!r}'
                )
            self.__logger.debug('APC mode confirmed.')

    def finalize(self):
        """Make a final serialized safe-off attempt before RS232 is closed."""
        if self._isMock:
            return True
        try:
            self.safeDisable(reason='manager finalization')
        except Exception:
            return False
        return True
