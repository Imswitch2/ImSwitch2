"""Manager for ALPAO deformable mirrors."""

import numpy as np

from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.devices.status import backend_attribute
from .DeformableMirrorManager import DeformableMirrorManager


class AlpaoDMManager(DeformableMirrorManager):
    """ DeformableMirrorManager for an ALPAO mirror (DM69, DM97, ...) through
    the ALPAO SDK's Python bindings, as the polarization rig's
    Microscope_GUI drove it: open by serial number, the actuator count from
    the SDK, commands in -1..1, Reset on close.

    Manager properties:
        - serialNumber (str, required): The mirror's serial number. One that
          starts with ``MOCK`` is a mock mirror.
        - actuators (int, default 69): The mock mirror's actuator count (a
          real mirror reports its own).
        - sdkPath (str, default ``C:\\Program Files\\Alpao\\SDK\\Samples\\Python3``):
          The folder that holds the SDK's ``Lib64`` bindings.
        - useMockOnFailure (bool, default False): A mock when the mirror
          cannot be opened, instead of a mirror that is not connected.
        - flatFile, modesFile, firstMode, maxCommand: see
          :class:`DeformableMirrorManager`.
    """

    def __init__(self, mirrorInfo, name: str, **lowLevelManagers):
        super().__init__(mirrorInfo, name, **lowLevelManagers)
        self.__logger = initLogger(self, instanceName=name)
        properties = mirrorInfo.managerProperties
        self._serial = str(properties['serialNumber'])
        mockActuators = int(properties.get('actuators', 69))
        if mockActuators <= 0:
            raise ValueError(f'{name}: actuators must be positive, not {mockActuators}.')
        sdk = properties.get('sdkPath')
        is_mock = self._serial.upper().startswith('MOCK')

        def open_real():
            if is_mock:
                return open_mock()
            from imswitch.imcontrol.model.interfaces.alpao_dm import AlpaoDM
            return AlpaoDM(self._serial, **({'sdk_dir': str(sdk)} if sdk else {}))

        def open_mock():
            from imswitch.imcontrol.model.interfaces.alpao_dm import MockAlpaoDM
            return MockAlpaoDM(self._serial, actuators=mockActuators)

        self._installBackend(
            open_real, open_mock,
            label=f'ALPAO {self._serial}',
            configured_mock=is_mock,
            use_mock_on_failure=bool(properties.get('useMockOnFailure', False)),
            transient=bool(getattr(mirrorInfo, 'transient', False)),
            connect_on_startup=bool(getattr(mirrorInfo, 'connectOnStartup', False)),
        )
        # The actuator count comes from the mirror; an absent one has none to
        # report, so the mock count stands in until it is connected.
        count = mockActuators
        if self.backendHolder.backend:
            try:
                count = int(self._mirror.actuator_count)
            except Exception:
                count = mockActuators
        self._loadFiles(count)

    #: The driver, read from the backend holder: a reconnect replaces it.
    _mirror = backend_attribute()

    @property
    def serialNumber(self) -> str:
        return self._serial

    def _send(self, vector: np.ndarray) -> None:
        self._mirror.send(vector)

    def _reset(self) -> None:
        self._requireConnected('mirror reset')
        self._mirror.reset()

    def _lifecycleReinitialise(self) -> None:
        count = int(self._mirror.actuator_count)
        if count != self._actuatorCount:
            self.__logger.warning(f'The reconnected mirror has {count} actuators, '
                                  f'not {self._actuatorCount}; reloading its files.')
            self._loadFiles(count)
        super()._lifecycleReinitialise()

    def _lifecycleProbe(self):
        """Check: the actuator count, read from the SDK."""
        return f'ALPAO {self._serial} answers: {int(self._mirror.actuator_count)} actuators'

    def finalize(self) -> None:
        """Reset and close the mirror."""
        self.backendHolder.close(suppress_errors=False)
        self._setFinalizedStatus()


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
