import importlib
from dataclasses import dataclass

from imswitch.imcommon.model import pythontools, initLogger


@dataclass(frozen=True)
class LantzLaserOpenResult:
    """Result of opening one logical Lantz-style laser backend.

    ``is_mock`` applies to the whole logical laser. Linked multi-port lasers
    are opened atomically: either every port is real or every port is mock.
    ``error`` retains the real-open failure when mock fallback was used.
    """

    laser: object
    is_mock: bool
    error: Exception | None = None


class LantzLaser:
    def __new__(cls, iName, ports):
        # Backwards-compatible convenience path. Managers that need to know
        # whether fallback occurred should call openLantzLaser() directly.
        return openLantzLaser(iName, ports).laser


class LinkedLantzLaser:
    def __init__(self, lasers):
        if len(lasers) < 1:
            raise ValueError('LinkedLantzLaser requires at least one laser, none passed')

        self.lasers = lasers

    @property
    def idn(self):
        return 'Linked Lasers ' + ' '.join([str(laser.idn) for laser in self.lasers])

    def finalize(self):
        for laser in self.lasers:
            laser.finalize()

    def __getattr__(self, item):
        if item == 'lasers':
            return super().__getattribute__(item)  # Prevent infinite recursion on lasers object

        value = getattr(self.lasers[0], item)
        if callable(value):
            return lambda *args, **kwargs: [getattr(laser, item)(*args, **kwargs)
                                            for laser in self.lasers]
        else:
            for laser in self.lasers:
                valueInLaser = getattr(laser, item)
                if valueInLaser != value:
                    raise ValueError(f'Laser {laser.idn} value {item} is {valueInLaser} while laser'
                                     f' {self.lasers[0]} value {item} is {value}')

            return value

    def __setattr__(self, key, value):
        if key == 'lasers':
            super().__setattr__(key, value)  # Prevent infinite recursion on lasers object
            return

        for laser in self.lasers:
            setattr(laser, key, value)


def _load_driver(iName, *, mock=False):
    pName, driverName = iName.rsplit('.', 1)
    root = (
        'imswitch.imcontrol.model.lantzdrivers_mock'
        if mock else
        'imswitch.imcontrol.model.lantzdrivers'
    )
    package = importlib.import_module(pythontools.joinModulePath(root, pName))
    return getattr(package, driverName)


def _finalize_all(lasers):
    for laser in reversed(lasers):
        try:
            laser.finalize()
        except Exception:
            pass


def _build_group(driver, ports):
    lasers = []
    try:
        for port in ports:
            laser = driver(port)
            # Track the instance before initialize(): initialization can open a
            # transport and then fail during capability probing. It must still
            # be finalized on rollback.
            lasers.append(laser)
            laser.initialize()
    except Exception:
        _finalize_all(lasers)
        raise
    return lasers[0] if len(lasers) == 1 else LinkedLantzLaser(lasers)


def openMockLantzLaser(iName, ports):
    """Open an all-mock logical laser for ``ports``.

    This never attempts the real driver and is used after a runtime reconnect
    failure so the configured manager remains available without another
    hardware-open attempt.
    """
    ports = list(ports)
    if len(ports) < 1:
        raise ValueError('LantzLaser requires at least one port, none passed')

    logger = initLogger('getLaser', tryInheritParent=True)
    try:
        mock_driver = _load_driver(iName, mock=True)
        return _build_group(mock_driver, ports)
    except Exception as exc:
        if isinstance(exc, (ModuleNotFoundError, AttributeError)):
            logger.error(f'No mocker found matching "{iName}"')
        else:
            logger.error(f'Failed to initialize mocker for "{iName}": {exc}')
        raise DriverLoadError(f'Failed to initialize mocker for "{iName}"') from exc


def openLantzLaser(iName, ports, *, allowMockFallback=True):
    """Open one logical laser, atomically across all configured ports.

    When ``allowMockFallback`` is true, *any* real-port failure closes all
    real ports already opened and replaces the entire logical laser with
    mocks. This prevents a linked laser from becoming a real/mock mixture.
    """
    ports = list(ports)
    if len(ports) < 1:
        raise ValueError('LantzLaser requires at least one port, none passed')

    logger = initLogger('getLaser', tryInheritParent=True)

    try:
        driver = _load_driver(iName, mock=False)
    except Exception as exc:
        if not isinstance(exc, (ModuleNotFoundError, AttributeError)):
            raise
        if not allowMockFallback:
            raise NoSuchDriverError(f'No driver found matching "{iName}"') from exc
        logger.warning(f'No driver found matching "{iName}" for laser, loading mocker')
        mock = openMockLantzLaser(iName, ports)
        return LantzLaserOpenResult(mock, True, exc)

    try:
        laser = _build_group(driver, ports)
        return LantzLaserOpenResult(laser, False, None)
    except Exception as exc:
        if not allowMockFallback:
            raise DriverLoadError(
                f'Failed to initialize driver "{iName}": {exc}'
            ) from exc

        logger.warning(
            f'Failed to initialize driver "{iName}" for laser, loading mocker'
            f' (error details: {exc})'
        )
        mock = openMockLantzLaser(iName, ports)
        return LantzLaserOpenResult(mock, True, exc)


def getLaser(iName, port):
    """Backwards-compatible single-port opener with mock fallback."""
    return openLantzLaser(iName, [port]).laser


class NoSuchDriverError(Exception):
    """ Exception raised when the specified driver is not found. """

    def __init__(self, message):
        self.message = message
        super().__init__(message)


class DriverLoadError(Exception):
    """ Exception raised when the specified driver fails to be initialized. """

    def __init__(self, message):
        self.message = message
        super().__init__(message)


# Copyright (C) 2017 Federico Barabas
# This file is part of Tormenta.
#
# Tormenta is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Tormenta is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
