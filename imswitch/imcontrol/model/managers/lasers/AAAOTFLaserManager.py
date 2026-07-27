import numpy as np
from scipy.interpolate import interp1d

from imswitch.imcommon.model import initLogger
from .LaserManager import LaserManager
from .aa_aotf_protocols import DEFAULT_PROFILE_ID, build_profiles
from ._protocol import DeviceInitializationError, ProtocolError


class AAAOTFLaserManager(LaserManager):
    """ LaserManager for controlling one channel of an AA Opto-Electronic
    acousto-optic modulator/tunable filter through RS232 communication.

    Vendor command strings live in ``aa_aotf_protocols``. This manager owns
    configuration, the calibration lookup, the ImSwitch value conversion, and
    the internal/external control policy, and drives the channel through named
    profile operations rather than command strings.

    Manager properties:

    - ``rs232device`` -- name of the defined rs232 communication channel
      through which the communication should take place
    - ``channel`` -- index of the channel in the acousto-optic device that
      should be controlled (indexing starts at 1)
    - ``protocolProfile`` -- command profile to use. Omitted means
      ``"aa.compatibility"``, the field-proven behavior; the AA controller has
      no safe read-only dialect query, so nothing is auto-discovered
    - ``toggleTrueExternal`` -- bool describing if the channel setting
      should use internal (False) or external (True) setting to be able
      to modify the laser power through ImSwitch. Default: False/null
    - ``ttlToggling`` -- bool describing if the channel should default to
      an extrenal control after setting a power value, to allow fast ttl
      toggling from another source. 
    """

    def __init__(self, laserInfo, name, **lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)
        self._channel = self._parse_channel(
            laserInfo.managerProperties['channel'], name
        )
        self._rs232manager = lowLevelManagers['rs232sManager'][
            laserInfo.managerProperties['rs232device']
        ]

        self._profiles = build_profiles()
        requested = laserInfo.managerProperties.get('protocolProfile')
        requested = str(requested).strip() if requested else DEFAULT_PROFILE_ID
        self._protocol_profile = requested
        self._profile = self._profiles.get(requested)
        if self._profile is None:
            raise DeviceInitializationError(
                f'AA laser {name!r}: unknown protocolProfile {requested!r}. '
                f'Available profiles: {", ".join(sorted(self._profiles))}.'
            )
        if 'toggleTrueExternal' in laserInfo.managerProperties:
            self._toggleTrueExternal = laserInfo.managerProperties['toggleTrueExternal']
        else:
            self._toggleTrueExternal = False
        if 'ttlToggling' in laserInfo.managerProperties:
            self._ttlToggling = laserInfo.managerProperties['ttlToggling']
        else:
            self._ttlToggling = False

        if self._toggleTrueExternal:
            if self._ttlToggling:
                #self.blankingOnInternal()
                self.internalControl()
            else:
                #self.blankingOnInternal()
                self.externalControl()
        else:
            if self._ttlToggling:
                #self.blankingOnExternal()
                self.externalControl()
            else:
                #self.blankingOnInternal()
                self.internalControl()

        self._lut = None
        self._value_units = 'arb'
        try:
            calib_csv_path = laserInfo.managerProperties["calibCsvPath"]
            self.create_lut_from_calib(calib_csv_path)
            self._value_units = '%'
        except AttributeError:
            pass  # Calib file not specified, managerProperties doesnt exist
        except KeyError:
            pass  # Calib file not specified, managerProperties does exist but calib is missing
        except Exception as e:
            self.__logger.error(f"Creating LUT for {name} from calib failed due to: {e}")

        super().__init__(laserInfo, name, isBinary=False, valueUnits=self._value_units, valueDecimals=0)

    @staticmethod
    def _parse_channel(value, name):
        """Validate the configured channel index at construction time."""
        try:
            channel = int(value)
        except (TypeError, ValueError):
            raise DeviceInitializationError(
                f'AA laser {name!r}: channel must be an integer, got '
                f'{value!r}.'
            ) from None
        if channel < 1:
            raise DeviceInitializationError(
                f'AA laser {name!r}: channel indexing starts at 1, got '
                f'{channel}.'
            )
        return channel

    def _run(self, operation_name: str, *args) -> bool:
        """Invoke a profile operation and report success as a bool.

        Transport failures are reported rather than raised, matching how this
        manager has always behaved towards the GUI, but they are no longer
        invisible: previously every reply and every failure mode was discarded.
        """
        operation = getattr(self._profile, operation_name, None)
        if operation is None:
            self.__logger.error(
                f'AA profile {self._profile.profile_id} does not implement '
                f'{operation_name}.'
            )
            return False
        try:
            operation(self._rs232manager, self._channel, *args)
        except ProtocolError as exc:
            self.__logger.error(
                f'AA channel {self._channel}: {operation_name} failed '
                f'({type(exc).__name__}): {exc.message}'
            )
            return False
        except ValueError as exc:
            self.__logger.error(
                f'AA channel {self._channel}: refusing {operation_name} with '
                f'an invalid value: {exc}'
            )
            return False
        return True

    def _apply_ttl_control_mode(self, *, before_command: bool) -> None:
        """Switch control mode around a write, when ``ttlToggling`` is set.

        The channel is put into the mode ImSwitch needs to issue the command,
        then returned to the opposite mode so an external TTL source can drive
        it. A no-op when ``ttlToggling`` is off.
        """
        if not self._ttlToggling:
            return
        use_external = (self._toggleTrueExternal if before_command
                        else not self._toggleTrueExternal)
        if use_external:
            self.externalControl()
        else:
            self.internalControl()

    def setEnabled(self, enabled):
        """Turn on (1) or off (0) laser emission"""
        self._apply_ttl_control_mode(before_command=True)
        self._run('set_channel_enabled', bool(enabled))
        self._apply_ttl_control_mode(before_command=False)

    def setValue(self, power):
        """Handles output power.
        Sends a RS232 command to the laser specifying the new intensity.
        """
        amplitude = self._amplitude_for(power)
        if amplitude is None:
            return
        self._apply_ttl_control_mode(before_command=True)
        self._run('set_channel_amplitude', amplitude)
        self._apply_ttl_control_mode(before_command=False)

    def _amplitude_for(self, power):
        """Convert an ImSwitch value to a raw AOTF amplitude.

        Returns None when the value cannot be converted, in which case nothing
        is sent. The calibration lookup clamps out-of-range requests to the
        measured endpoints rather than producing NaN.
        """
        try:
            if self._lut is not None:
                converted = float(self._lut(power))
                if np.isnan(converted):
                    self.__logger.error(
                        f'AA channel {self._channel}: calibration lookup '
                        f'produced no value for {power!r}; not sending an '
                        f'amplitude.'
                    )
                    return None
                return int(converted)
            return int(round(float(power)))
        except (TypeError, ValueError) as exc:
            self.__logger.error(
                f'AA channel {self._channel}: cannot convert value '
                f'{power!r} to an amplitude: {exc}'
            )
            return None

    #def blankingOnInternal(self):
    #    """Switch on the blanking of the channel, internal"""
    #    cmd = 'L' + str(self._channel) + 'O0'
    #    self._rs232manager.write(cmd)

    #def blankingOnExternal(self):
    #    """Switch on the blanking of the channel, external"""
    #    cmd = 'L' + str(self._channel) + 'O0'
    #    self._rs232manager.write(cmd)

    def setScanModeActive(self, active):
        """Arm/disarm the channel for TTL-gated triggering during a scan.

        When a scan starts the channel is switched to external control so
        the scanner's TTL line gates the diffracted beam on/off. When the
        scan ends it returns to internal control so ImSwitch governs the
        output again.

        Note: light is only produced while active if (1) the channel
        amplitude was set to a non-zero value via setValue, and (2) the
        scanner actually drives this channel's TTL line HIGH.
        """
        if active:
            self.externalControl()
        else:
            self.internalControl()

    def internalControl(self):
        """Switch the channel to internal control"""
        self._run('select_internal_control')

    def externalControl(self):
        """Switch the channel to external control"""
        self._run('select_external_control')

    def create_lut_from_calib(self, calib_csv_path):
        """Build the percentage-to-amplitude lookup from a calibration file.

        Out-of-range requests clamp to the measured endpoints. Without an
        explicit ``fill_value`` SciPy fills with NaN, and the caller's
        ``int(...)`` then raised ``ValueError`` for any value outside the
        calibrated span — so a laser calibrated over 5-95 % crashed at 100 %.
        """
        data = np.loadtxt(calib_csv_path)
        data[:, 1] -= data[:, 1].min()
        data[:, 1] /= data[:, 1].max() * 0.01 # convert to %
        percentages = data[:, 1]
        amplitudes = data[:, 0]
        self._lut = interp1d(
            percentages,
            amplitudes,
            bounds_error=False,
            fill_value=(
                float(amplitudes[np.argmin(percentages)]),
                float(amplitudes[np.argmax(percentages)]),
            ),
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
