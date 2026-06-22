import importlib
from abc import ABC, abstractmethod
from imswitch.imcommon.model import initLogger

from imswitch.imcommon.model import pythontools
from imswitch.imcontrol.model.plugins.registry import (
    get_default_registry,
    UnknownDeviceManagerError,
)

# Maps a MultiManager ``subManagersPackage`` to a device plugin registry kind.
# Only these device groups are loaded through MultiManager, so only they get
# plugin resolution. StandManager and the pulse generator use bespoke loaders
# (see docs/design/DEVICE_PLUGINS.md, "MultiManager-backed vs bespoke kinds").
SUBMANAGERS_PACKAGE_TO_KIND = {
    'detectors': 'detector',
    'lasers': 'laser',
    'positioners': 'positioner',
    'rotators': 'rotator',
    'rs232': 'rs232',
    'flipMirrors': 'flip_mirror',
    'slms': 'slm',
}


class MultiManager(ABC):
    """ Abstract class for a manager used to control a group of sub-managers.
    Intended to be extended for each type of manager. """

    @abstractmethod
    def __init__(self, managedDeviceInfos, subManagersPackage, **lowLevelManagers):
        #self.__logger = initLogger(self, instanceName='MultiManager')
        self._subManagers = {}
        currentPackage = '.'.join(__name__.split('.')[:-1])
        kind = SUBMANAGERS_PACKAGE_TO_KIND.get(subManagersPackage)
        if managedDeviceInfos:
            for managedDeviceName, managedDeviceInfo in managedDeviceInfos.items():
                # Create sub-manager
                managerClass = self._resolveManagerClass(
                    currentPackage, subManagersPackage, kind,
                    managedDeviceInfo.managerName,
                )
                self._subManagers[managedDeviceName] = managerClass(
                    managedDeviceInfo, managedDeviceName, **lowLevelManagers)

    @staticmethod
    def _resolveManagerClass(currentPackage, subManagersPackage, kind, managerName):
        """ Resolve a setup ``managerName`` to a manager class.

        Device plugin registry first (for MultiManager-backed kinds), then the
        legacy internal import path so existing setup files keep working. If
        both miss for a registry-backed kind, raise the actionable registry
        diagnostic instead of a raw ImportError. """
        # 1. Device plugin registry (built-ins + installed plugins).
        if kind is not None:
            managerClass = get_default_registry().load_manager_class(
                kind, managerName)
            if managerClass is not None:
                return managerClass

        # 2. Legacy internal import path (imswitch.imcontrol.model.managers.<pkg>).
        try:
            package = importlib.import_module(
                pythontools.joinModulePath(
                    f'{currentPackage}.{subManagersPackage}', managerName)
            )
            return getattr(package, managerName)
        except (ImportError, AttributeError) as exc:
            if kind is not None:
                raise UnknownDeviceManagerError(
                    get_default_registry().format_resolution_error(
                        kind, managerName)
                ) from exc
            raise

    def hasDevices(self):
        """ Returns whether this manager manages any devices. """
        return len(self._subManagers) > 0

    def getAllDeviceNames(self, condition=None):
        """ Returns the names of all managed devices. """
        if condition is None:
            def condition(_): return True

        return list(managedDeviceName
                    for managedDeviceName, subManager in self._subManagers.items()
                    if condition(subManager))

    def execOn(self, managedDeviceName, func):
        """ Executes a function on a specific sub-manager and returns the
        result. """
        self._validateManagedDeviceName(managedDeviceName)
        return func(self._subManagers[managedDeviceName])

    def getDevice(self, managedDeviceName):
        """ Public access to a named sub-manager device. Use this instead of
        reaching into ``manager._subManagers[name]`` from controllers. Raises a
        clear error for an unknown device name. """
        self._validateManagedDeviceName(managedDeviceName)
        return self._subManagers[managedDeviceName]

    def __getitem__(self, managedDeviceName):
        return self.getDevice(managedDeviceName)

    def execOnAll(self, func, *, condition=None):
        """ Executes a function on all sub-managers and returns the
        results. """
        if condition is None:
            def condition(_): return True

        return {managedDeviceName: func(subManager)
                for managedDeviceName, subManager in self._subManagers.items()
                if condition(subManager)}

    def finalize(self):
        """ Close/cleanup sub-managers. """
        for subManager in self._subManagers.values():
            if hasattr(subManager, 'finalize') and callable(subManager.finalize):
                subManager.finalize()

    def _validateManagedDeviceName(self, managedDeviceName):
        """ Raises an error if the specified device is not managed by this
        manager. """
        if managedDeviceName not in self._subManagers:
            raise NoSuchSubManagerError(f'Device "{managedDeviceName}" does not exist or is not'
                                        f' managed by this {self.__class__.__name__}.')

    def __getitem__(self, key):
        return self._subManagers[key]

    def __iter__(self):
        yield from self._subManagers.items()


class NoSuchSubManagerError(RuntimeError):
    """ Error raised when a function related to a sub-manager is called if the
    sub-manager is not managed by the MultiManager. """
    pass


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
