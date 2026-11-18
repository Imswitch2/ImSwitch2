"""Manager for the Thorlabs BPC303 benchtop piezo controller."""

from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.devices.status import backend_attribute
from .PositionerManager import PositionerManager


class BPC303PiezoManager(PositionerManager):
    """PositionerManager for a piezo stage on a Thorlabs BPC303 (three
    channels), through the Kinesis .NET libraries (pythonnet). It drives the
    controller as the polarization rig's Microscope_GUI did: open loop, each
    channel's output voltage, ``µm = V × travelUm / Vmax``, the position read
    back from the output voltage. A target outside the travel is clamped to
    it, with a warning.

    The setup's ``axes`` take the controller's channels in order (the first
    axis is channel 1) unless ``channels`` maps them.

    Manager properties:
        - serialNumber (str, required): The controller's serial number. One
          that starts with ``MOCK`` is a mock controller.
        - travelUm (float, default 20.0): The stage's travel in µm at the
          channel's largest output voltage (the old GUI's 20).
        - channels (dict, optional): Axis name to controller channel (1-3),
          e.g. ``{"X": 1, "Y": 2, "Z": 3}``.
        - kinesisPath (str, default ``C:\\Program Files\\Thorlabs\\Kinesis``):
          The folder that holds the Kinesis DLLs.
        - useMockOnFailure (bool, default False): A mock when the controller
          cannot be opened, instead of a positioner that is not connected.
    """

    def __init__(self, positionerInfo, name: str, **lowLevelManagers):
        super().__init__(positionerInfo, name, initialPosition={
            axis: 0.0 for axis in positionerInfo.axes
        })
        self.__logger = initLogger(self, instanceName=name)
        properties = positionerInfo.managerProperties
        self._serial = str(properties['serialNumber'])
        self._travel_um = float(properties.get('travelUm', 20.0))
        if not self._travel_um > 0:
            raise ValueError(f'{name}: travelUm must be positive, not {self._travel_um}.')
        self._channel_of = self._channels(properties.get('channels'), positionerInfo.axes, name)
        kinesis = properties.get('kinesisPath')
        is_mock = self._serial.upper().startswith('MOCK')
        count = max(self._channel_of.values())

        def open_real():
            if is_mock:
                return open_mock()
            from imswitch.imcontrol.model.interfaces.bpc303 import BPC303
            options = {'channels': count}
            if kinesis:
                options['kinesis_dir'] = str(kinesis)
            return BPC303(self._serial, **options)

        def open_mock():
            from imswitch.imcontrol.model.interfaces.bpc303 import MockBPC303
            return MockBPC303(self._serial, channels=count)

        self._installBackend(
            open_real, open_mock,
            label=f'BPC303 {self._serial}',
            configured_mock=is_mock,
            use_mock_on_failure=bool(properties.get('useMockOnFailure', False)),
            transient=bool(getattr(positionerInfo, 'transient', False)),
            connect_on_startup=bool(getattr(positionerInfo, 'connectOnStartup', False)),
        )
        if self.backendHolder.backend:
            self._update_position()

    #: The driver, read from the backend holder: a reconnect replaces it.
    _controller = backend_attribute()

    @staticmethod
    def _channels(mapping, axes, name):
        if not mapping:
            if len(axes) > 3:
                raise ValueError(f'{name}: the BPC303 has three channels, not {len(axes)} axes.')
            return {axis: index for index, axis in enumerate(axes, start=1)}
        channels = {str(axis): int(channel) for axis, channel in dict(mapping).items()}
        missing = [axis for axis in axes if axis not in channels]
        if missing:
            raise ValueError(f'{name}: channels has no channel for {", ".join(missing)}.')
        wrong = sorted({c for c in channels.values() if c not in (1, 2, 3)})
        if wrong:
            raise ValueError(f'{name}: the BPC303 has channels 1-3, not {wrong}.')
        return {axis: channels[axis] for axis in axes}

    def _volts_per_um(self, channel: int) -> float:
        return self._controller.max_voltage(channel) / self._travel_um

    def setPosition(self, position: float, axis: str) -> None:
        """Move ``axis`` to ``position`` µm (clamped to the travel)."""
        target = float(position)
        if not 0.0 <= target <= self._travel_um:
            clamped = min(max(target, 0.0), self._travel_um)
            self.__logger.warning(f'{axis}: {target:g} µm is outside 0-{self._travel_um:g} µm; '
                                  f'moving to {clamped:g} µm.')
            target = clamped
        channel = self._channel_of[axis]
        self._controller.set_output_voltage(channel, target * self._volts_per_um(channel))
        self._update_position()

    def move(self, dist: float, axis: str) -> None:
        """Move ``axis`` by ``dist`` µm."""
        self.setPosition(self._position[axis] + float(dist), axis)

    def _update_position(self) -> None:
        for axis, channel in self._channel_of.items():
            self._position[axis] = (self._controller.output_voltage(channel)
                                    / self._volts_per_um(channel))

    def updatePosition(self) -> None:
        """Read every axis back from its output voltage."""
        self._update_position()

    def _lifecycleReinitialise(self) -> None:
        self._update_position()

    def _lifecycleProbe(self):
        """Check: a fresh read of every axis (Hardware status -> Check)."""
        self._update_position()
        position = ', '.join(f'{axis} {self._position[axis]:.3f} µm' for axis in self.axes)
        return f'BPC303 {self._serial} answers: {position}'

    def finalize(self) -> None:
        """Close the controller."""
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
