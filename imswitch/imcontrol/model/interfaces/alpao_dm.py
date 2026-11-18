"""ALPAO deformable mirrors through the ALPAO SDK's Python bindings
(``Lib64/asdk``), as the polarization rig's Microscope_GUI drove its DM69
(``miao/modules/module_deformablemirror.py``): open by serial number, read
the actuator count, send a command vector, reset.

A real and a mock driver with the same calls, so the manager runs headless.
"""

import os
import struct
import sys

import numpy as np

#: Where the ALPAO SDK installer puts its Python bindings.
ALPAO_SDK_DIR = r'C:\Program Files\Alpao\SDK\Samples\Python3'


class AlpaoDM:
    """One ALPAO mirror by its serial number (e.g. ``BAX123``)."""

    def __init__(self, serial: str, *, sdk_dir: str = ALPAO_SDK_DIR):
        sdk_dir = str(sdk_dir)
        if not os.path.isdir(sdk_dir):
            raise ImportError(f'ALPAO SDK not found in {sdk_dir} (install the ALPAO SDK '
                              'or set sdkPath)')
        if sdk_dir not in sys.path:
            sys.path.append(sdk_dir)
        if 8 * struct.calcsize('P') == 32:
            from Lib.asdk import DM
        else:
            from Lib64.asdk import DM
        self.serial = str(serial)
        self._dm = DM(self.serial)
        self._count = int(self._dm.Get('NBOfActuator'))
        if self._count <= 0:
            raise RuntimeError(f'ALPAO {self.serial}: the SDK reports {self._count} actuators')

    @property
    def actuator_count(self) -> int:
        return self._count

    def send(self, values: np.ndarray) -> None:
        self._dm.Send([float(v) for v in values])

    def reset(self) -> None:
        """All actuators to zero (the SDK's Reset)."""
        self._dm.Reset()

    def close(self) -> None:
        try:
            self._dm.Reset()
        finally:
            stop = getattr(self._dm, 'Stop', None)
            if callable(stop):
                stop()
            self._dm = None


class MockAlpaoDM:
    """A mirror that keeps the last command it was sent."""

    def __init__(self, serial: str, *, actuators: int = 69):
        self.serial = str(serial)
        self._count = int(actuators)
        self.last_command = np.zeros(self._count)
        self.sent = 0

    @property
    def actuator_count(self) -> int:
        return self._count

    def send(self, values: np.ndarray) -> None:
        self.last_command = np.array(values, dtype=float)
        self.sent += 1

    def reset(self) -> None:
        self.last_command = np.zeros(self._count)

    def close(self) -> None:
        self.reset()


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
