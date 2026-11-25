import importlib
from .LaserManager import LaserManager
from imswitch.imcommon.model import pythontools, initLogger
from imswitch.imcontrol.model.devices.status import backend_attribute


class _NullLaser:
    """The reported mock (``useMockOnFailure``): accepts and drops commands."""
    power = 0.0

    def enable(self):
        pass

    def disable(self):
        pass

    def shutdown(self):
        pass


class PyMicroscopeLaserManager(LaserManager):
    """ Generic LaserManager for laser handlers supported by Python Microscope.

    Manager properties:

    - ``pyMicroscopeDriver`` -- string describing the Python Microscope
        object to initialize; requires to specify the module
        and the class name, e.g. ``toptica.TopticaiBeam``
    - ``digitalPorts`` -- string describing the COM port
        to connect to, e.g. ``["COM4"]``
    - ``useMockOnFailure`` -- default ``false``: a laser that cannot be
        opened is left not connected (reported, commands refused,
        reconnectable from Hardware status; it comes back OFF). ``true``
        runs a reported mock instead.
    """
    def __init__(self, laserInfo, name, **_lowLevelManager) -> None:
        super().__init__(laserInfo, name, isBinary=False, valueUnits='mW', valueDecimals=1)
        self.__logger = initLogger(self, instanceName=name)
        self.__port = laserInfo.managerProperties.get("digitalPorts", "Unknown")
        self.__driver = str(laserInfo.managerProperties.get("pyMicroscopeDriver", "Unknown"))
        self.__maxPower = float(laserInfo.valueRangeMax)
        useMock = laserInfo.managerProperties.get('useMockOnFailure', False)
        if isinstance(useMock, str):
            useMock = useMock.strip().lower() in ('1', 'true', 'yes', 'on')

        def open_real():
            driver = self.__driver.split(".")
            if len(driver) != 2:
                raise ValueError(
                    f"pyMicroscopeDriver must be in format 'module.class', got: '{self.__driver}'"
                )
            package = importlib.import_module(
                pythontools.joinModulePath("microscope.lights", driver[0])
            )
            laser = getattr(package, driver[1])(self.__port)
            self.__logger.info(f"[{self.__port}] {self.__driver} initialized. ")
            return laser

        self._installBackend(
            open_real, _NullLaser,
            label=f'Python Microscope laser {name} ({self.__driver} on {self.__port})',
            use_mock_on_failure=bool(useMock),
            close=lambda laser: laser.shutdown(),
        )

    #: The driver, read from the backend holder: a reconnect replaces it.
    _laser = backend_attribute()

    def _lifecycleSafeState(self, *, verified: bool):
        """Emission off. Before a reconnect: best effort. After: errors count."""
        try:
            self._laser.disable()
        except Exception as exc:
            return [f'{self.name}: could not confirm OFF: {exc}'] if verified else []
        return []

    def setEnabled(self, enabled: bool) -> None:
        # A laser that is not there raises DeviceNotConnectedError.
        (self._laser.enable() if enabled else self._laser.disable())

    def setValue(self, value) -> None:
        # laser power is handled as percentage
        # so we divide for the max power to obtain
        # the actual percentage to which we set
        # the output power
        self._requireConnected('setting power')
        if self.__maxPower == 0:
            self.__logger.error(f"Cannot set power: maxPower is zero")
            return
        self._laser.power = float(value) / self.__maxPower

    def finalize(self) -> None:
        self.__logger.info(f"[{self.__port}] {self.__driver} closed.")
        self.backendHolder.close(suppress_errors=True)
        self._setFinalizedStatus()


# Copyright (C) 2021 ImSwitch developers
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
