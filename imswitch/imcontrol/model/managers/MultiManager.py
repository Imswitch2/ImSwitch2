import importlib
import logging
from abc import ABC, abstractmethod
from imswitch.imcommon.model import initLogger

from imswitch.imcommon.model import pythontools
from imswitch.imcontrol.model.plugins.registry import (
    get_default_registry,
    UnknownDeviceManagerError,
)

# Maps a MultiManager ``subManagersPackage`` to a device plugin registry kind.
# Only these device groups are loaded through MultiManager. StandManager uses
# its own registry-backed resolver because microscopeStand is a single setup
# object rather than a named device map. The pulse generator still uses a
# bespoke loader.
SUBMANAGERS_PACKAGE_TO_KIND = {
    'detectors': 'detector',
    'lasers': 'laser',
    'positioners': 'positioner',
    'rotators': 'rotator',
    'rs232': 'rs232',
    'flipMirrors': 'flip_mirror',
    'instruments': 'instrument',
    'slms': 'slm',
}


class MultiManager(ABC):
    """ Abstract class for a manager used to control a group of sub-managers.
    Intended to be extended for each type of manager. """

    @abstractmethod
    def __init__(self, managedDeviceInfos, subManagersPackage, **lowLevelManagers):
        #self.__logger = initLogger(self, instanceName='MultiManager')
        self._subManagers = {}
        # Devices whose manager could not be built: name -> FailedDeviceManager.
        # They are not in ``_subManagers``, so every loop over the working
        # devices simply does not see them; the device supervisor reports them.
        self._failedSubManagers = {}
        # Keep successful sub-manager objects alive for the remainder of the
        # shutdown sequence. Identity-scoped bookkeeping lets a failed device
        # retry without finalizing already-closed peers again, while a device
        # object replaced under the same name is still finalized.
        self._shutdownFinalizedSubManagerObjects = []
        currentPackage = '.'.join(__name__.split('.')[:-1])
        kind = SUBMANAGERS_PACKAGE_TO_KIND.get(subManagersPackage)
        if managedDeviceInfos:
            for managedDeviceName, managedDeviceInfo in managedDeviceInfos.items():
                # Create sub-manager. One device that cannot be built must not
                # take the rest of the setup down with it.
                try:
                    managerClass = self._resolveManagerClass(
                        currentPackage, subManagersPackage, kind,
                        managedDeviceInfo.managerName,
                    )
                    self._subManagers[managedDeviceName] = managerClass(
                        managedDeviceInfo, managedDeviceName, **lowLevelManagers)
                except (KeyboardInterrupt, SystemExit, GeneratorExit):
                    raise
                except BaseException as error:
                    # BaseException: some vendor SDKs raise outside Exception.
                    self._recordSubManagerFailure(
                        subManagersPackage, managedDeviceName,
                        getattr(managedDeviceInfo, 'managerName', None), error)

    def _recordSubManagerFailure(self, subManagersPackage, managedDeviceName,
                                 managerName, error):
        from imswitch.imcontrol.model.devices.status import FailedDeviceManager

        logging.getLogger('imswitch.imcontrol.MultiManager').error(
            'Device "%s" (%s, manager "%s") failed to start and is left out: '
            '%s: %s', managedDeviceName, subManagersPackage, managerName,
            type(error).__name__, error, exc_info=error)
        self._failedSubManagers[managedDeviceName] = FailedDeviceManager(
            managedDeviceName, error, managerName=managerName)

    def getFailedDevices(self):
        """ Devices that failed to start, as ``{name: FailedDeviceManager}``. """
        return dict(self.__dict__.get('_failedSubManagers', {}))

    @staticmethod
    def _resolveManagerClass(currentPackage, subManagersPackage, kind, managerName):
        """ Resolve a setup ``managerName`` to a manager class.

        Device plugin registry first (for MultiManager-backed kinds), then the
        legacy internal import path so existing setup files keep working. If
        both miss for a registry-backed kind, raise the actionable registry
        diagnostic instead of a raw ImportError.
        
        Precedence rule (deterministic, registry-first):
        1. Device plugin registry (built-ins + installed plugins)
        2. Legacy internal import path (fallback for un-registered managers)
        
        When both paths would resolve, registry wins and a WARNING is logged.
        """
        logger = logging.getLogger('imswitch.imcontrol.MultiManager')
        
        # 1. Device plugin registry (built-ins + installed plugins).
        registry_contribution = None
        if kind is not None:
            registry_contribution = get_default_registry().resolve(kind, managerName)
            if registry_contribution is not None:
                managerClass = get_default_registry().load_manager_class(
                    kind, managerName)
                
                # Check if legacy path would also resolve (shadowing detection).
                if MultiManager._legacyClassOrNone(
                    currentPackage, subManagersPackage, managerName
                ) is not None:
                    # Warn about shadowing: registry is hiding an in-tree manager.
                    logger.warning(
                        f"Registry-backed {kind} manager '{managerName}' from "
                        f"plugin '{registry_contribution.plugin_name}' is shadowing "
                        f"an in-tree manager at "
                        f"{currentPackage}.{subManagersPackage}.{managerName}. "
                        f"Registry resolution takes precedence (deterministic). "
                        f"Consider removing the in-tree manager or renaming the "
                        f"plugin contribution to avoid confusion."
                    )

                return managerClass

        # 2. Legacy internal import path (imswitch.imcontrol.model.managers.<pkg>).
        legacyClass = MultiManager._importLegacyManagerClass(
            currentPackage, subManagersPackage, managerName
        )
        if legacyClass is not None:
            return legacyClass

        if kind is not None:
            raise UnknownDeviceManagerError(
                get_default_registry().format_resolution_error(kind, managerName)
            )
        raise ImportError(
            f'No manager named "{managerName}" in '
            f'{currentPackage}.{subManagersPackage}'
        )

    @staticmethod
    def _legacyClassOrNone(currentPackage, subManagersPackage, managerName):
        """ Like :meth:`_importLegacyManagerClass`, but an in-tree manager
        that fails to import counts as absent (it cannot shadow anything). """
        try:
            return MultiManager._importLegacyManagerClass(
                currentPackage, subManagersPackage, managerName)
        except ManagerImportError:
            return None

    @staticmethod
    def _importLegacyManagerClass(currentPackage, subManagersPackage, managerName):
        """Return the in-tree manager class of that name, or None.

        Raises :class:`ManagerImportError` when the module exists but fails to
        import, so that reads differently from "no such manager".

        None covers every way there can fail to be one, including a name that
        could never be a module in the first place. Plugin contribution ids are
        namespaced — ``vendor.device-name`` — and ``joinModulePath`` rejects the
        hyphen with ValueError. That is a definitive "no in-tree manager by this
        name", so it must not escape:

        - escaping from the shadowing check made *every* installed plugin
          unusable by its own id, leaving only legacy class-name aliases working;
        - escaping from the fallback replaced the actionable "install this
          package" message with an opaque ValueError, for exactly the namespaced
          ids the external-plugin hints in ``external.py`` are keyed by.
        """
        try:
            modulePath = pythontools.joinModulePath(
                f'{currentPackage}.{subManagersPackage}', managerName)
        except ValueError:
            return None
        try:
            package = importlib.import_module(modulePath)
        except ModuleNotFoundError as error:
            missing = error.name or ''
            if modulePath == missing or modulePath.startswith(missing + '.'):
                return None  # there is no such manager module
            raise ManagerImportError(managerName, modulePath, error) from error
        except Exception as error:
            # The module exists, but loading it failed: a vendor library
            # missing (ImportError) or failing to load (OSError for a DLL).
            raise ManagerImportError(managerName, modulePath, error) from error
        return getattr(package, managerName, None)

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

    def execOnAll(self, func, *, condition=None):
        """ Executes a function on all sub-managers and returns the
        results. """
        if condition is None:
            def condition(_): return True

        return {managedDeviceName: func(subManager)
                for managedDeviceName, subManager in self._subManagers.items()
                if condition(subManager)}

    def finalize(self):
        """Close every pending sub-manager and aggregate explicit failures."""
        success = True
        logger = logging.getLogger(
            f'{__name__}.{self.__class__.__name__}'
        )
        completed = self.__dict__.setdefault(
            '_shutdownFinalizedSubManagerObjects', []
        )
        attempted = []
        for managedDeviceName, subManager in self._subManagers.items():
            if any(candidate is subManager for candidate in completed):
                continue
            # A malformed/out-of-tree group can alias one concrete manager
            # under multiple names. It still gets at most one attempt per
            # finalize call; a failure remains pending for the next retry.
            if any(candidate is subManager for candidate in attempted):
                continue
            attempted.append(subManager)
            finalize = getattr(subManager, 'finalize', None)
            if not callable(finalize):
                continue
            try:
                result = finalize()
            except Exception:
                success = False
                logger.exception(
                    'Failed to finalize managed device %r',
                    managedDeviceName,
                )
            else:
                if result is False:
                    success = False
                    logger.error(
                        'Managed device %r reported incomplete finalization',
                        managedDeviceName,
                    )
                elif not any(
                    candidate is subManager for candidate in completed
                ):
                    completed.append(subManager)
        return success

    def _validateManagedDeviceName(self, managedDeviceName):
        """ Raises an error if the specified device is not managed by this
        manager. """
        if managedDeviceName not in self._subManagers:
            failed = self.__dict__.get('_failedSubManagers', {}).get(managedDeviceName)
            if failed is not None:
                raise NoSuchSubManagerError(
                    f'Device "{managedDeviceName}" failed to start in this '
                    f'{self.__class__.__name__}: {failed.connectionStatusDetails}')
            raise NoSuchSubManagerError(f'Device "{managedDeviceName}" does not exist or is not'
                                        f' managed by this {self.__class__.__name__}.')

    def __getitem__(self, key):
        return self._subManagers[key]

    def __iter__(self):
        yield from self._subManagers.items()


class ManagerImportError(ImportError):
    """ A device manager module exists but could not be imported (a vendor
    library missing, or failing to load). Not the same as an unknown
    manager name. """

    def __init__(self, managerName, modulePath, cause):
        super().__init__(
            f'Device manager "{managerName}" was found ({modulePath}) but '
            f'failed to import: {type(cause).__name__}: {cause}')


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
