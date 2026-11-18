"""The Thorlabs BPC303 benchtop piezo controller (three channels), through the
Kinesis .NET libraries over pythonnet -- as the polarization rig's
Microscope_GUI drove it (``miao/modules/module_piezo.py``): each channel
enabled, polled, and set by its output voltage, open loop.

A real and a mock driver with the same calls, so the manager runs headless.
"""

import os
import time

from imswitch.imcommon.model import initLogger

#: Where the Kinesis installer puts its DLLs.
KINESIS_DIR = r'C:\Program Files\Thorlabs\Kinesis'
_DLLS = ('Thorlabs.MotionControl.DeviceManagerCLI.dll',
         'Thorlabs.MotionControl.GenericPiezoCLI.dll',
         'ThorLabs.MotionControl.Benchtop.PiezoCLI.dll')


def _number(value) -> float:
    """A .NET Decimal as a float. Its string follows the computer's locale,
    which writes a decimal comma in Sweden: read either."""
    return float(str(value).replace(',', '.'))


class BPC303:
    """The controller's channels 1..``channels``, by their output voltage."""

    def __init__(self, serial: str, *, channels: int = 3, kinesis_dir: str = KINESIS_DIR,
                 poll_ms: int = 250):
        self.__logger = initLogger(self)
        import clr                                   # pythonnet
        for dll in _DLLS:
            clr.AddReference(os.path.join(kinesis_dir, dll))
        from Thorlabs.MotionControl.Benchtop.PiezoCLI import BenchtopPiezo
        from Thorlabs.MotionControl.DeviceManagerCLI import DeviceManagerCLI
        from System import Decimal
        self._decimal = Decimal
        self._serial = str(serial)
        DeviceManagerCLI.BuildDeviceList()
        self._device = BenchtopPiezo.CreateBenchtopPiezo(self._serial)
        self._device.Connect(self._serial)
        self._channels, self._max_volts = [], []
        try:
            for index in range(1, int(channels) + 1):
                channel = self._device.GetChannel(index)
                if not channel.IsSettingsInitialized():
                    channel.WaitForSettingsInitialized(10000)
                    if not channel.IsSettingsInitialized():
                        raise RuntimeError(f'BPC303 {serial}: channel {index} did not '
                                           'initialise its settings in 10 s')
                channel.StartPolling(int(poll_ms))
                time.sleep(0.25)
                channel.EnableDevice()
                time.sleep(0.25)
                channel.GetPiezoConfiguration(channel.DeviceID)
                self._max_volts.append(_number(channel.GetMaxOutputVoltage()))
                self._channels.append(channel)
        except Exception:
            self.close()
            raise
        self.__logger.info(f'BPC303 {serial}: {len(self._channels)} channels, '
                           f'max {self._max_volts} V')

    def max_voltage(self, channel: int) -> float:
        """The channel's (1-based) largest output voltage."""
        return self._max_volts[channel - 1]

    def output_voltage(self, channel: int) -> float:
        return _number(self._channels[channel - 1].GetOutputVoltage())

    def set_output_voltage(self, channel: int, volts: float) -> None:
        self._channels[channel - 1].SetOutputVoltage(self._decimal(float(volts)))

    def close(self) -> None:
        for channel in self._channels:
            try:
                channel.StopPolling()
            except Exception:
                pass
        self._channels = []
        try:
            self._device.Disconnect()
        except Exception:
            pass


class MockBPC303:
    """The same calls, no hardware: each channel holds what it was set to."""

    def __init__(self, serial: str, *, channels: int = 3, max_volts: float = 75.0, **_ignored):
        self._serial = str(serial)
        self._max_volts = [float(max_volts)] * int(channels)
        self._volts = [0.0] * int(channels)

    def max_voltage(self, channel: int) -> float:
        return self._max_volts[channel - 1]

    def output_voltage(self, channel: int) -> float:
        return self._volts[channel - 1]

    def set_output_voltage(self, channel: int, volts: float) -> None:
        self._volts[channel - 1] = min(max(float(volts), 0.0), self._max_volts[channel - 1])

    def close(self) -> None:
        pass


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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
