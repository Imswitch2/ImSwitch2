from imswitch.imcommon.model import initLogger
from imswitch.imcommon.model import pythontools
from abc import ABC
import importlib

from imswitch.imcontrol.model.plugins.registry import get_default_registry


class StandManager(ABC):
    """ StandManager interface for dealing with microscope stand managers. """
    def __init__(self, deviceInfo, **lowLevelManagers):
        self.__logger = initLogger(self)
        self.mocker = True
        self._subManager = None
        currentPackage = '.'.join(__name__.split('.')[:-1])
        if deviceInfo:
            manager, is_mock = self._resolveStandManagerClass(
                currentPackage,
                deviceInfo.managerName,
                self.__logger,
            )
            self._subManager = manager(deviceInfo, **lowLevelManagers)
            self.mocker = is_mock

    @classmethod
    def _resolveStandManagerClass(cls, currentPackage, managerName, logger):
        registry = get_default_registry()

        manager = registry.load_manager_class("stand", managerName)
        if manager is not None:
            return manager, cls._managerNameLooksMock(managerName, manager)

        try:
            return cls._resolveLegacyManagerClass(
                currentPackage, managerName
            ), False
        # ValueError: a namespaced plugin id such as "vendor.stand-x" is not a
        # legal module path, so joinModulePath rejects it. That means "there is
        # no in-tree stand manager by this name" — it must not escape, or an
        # uninstalled plugin surfaces "invalid characters" instead of the
        # actionable diagnostic below, which is what carries the external-plugin
        # install hint. Mirrors MultiManager._importLegacyManagerClass.
        except (ImportError, AttributeError, ValueError) as legacy_error:
            mockName = f'{managerName}_mock'
            manager = registry.load_manager_class("stand", mockName)
            if manager is not None:
                logger.warning(
                    f"Stand manager '{managerName}' is unavailable "
                    f"({legacy_error}). Loading mock manager '{mockName}'."
                )
                return manager, True

            try:
                return cls._resolveLegacyMockManagerClass(
                    currentPackage, managerName
                ), True
            except (ImportError, AttributeError, ValueError) as mock_error:
                raise ImportError(
                    registry.format_resolution_error("stand", managerName)
                ) from mock_error

    @staticmethod
    def _resolveLegacyManagerClass(currentPackage, managerName):
        package = importlib.import_module(
            pythontools.joinModulePath(f'{currentPackage}.{"stands"}', managerName)
        )
        return getattr(package, managerName)

    @staticmethod
    def _resolveLegacyMockManagerClass(currentPackage, managerName):
        package = importlib.import_module(
            pythontools.joinModulePath(
                f'{currentPackage}.{"stands"}', f'{managerName}_mock'
            )
        )
        return getattr(package, f'Mock{managerName}')

    @staticmethod
    def _managerNameLooksMock(managerName, manager):
        return (
            "mock" in managerName.lower()
            or manager.__name__.startswith("Mock")
        )

    def motCorrPos(self, position):
        self._subManager.motCorrPos(position)

    # Public capability passthroughs — controllers should call these instead of
    # reaching into ``standManager._subManager`` (driver-specific, may be a mock).
    # Each is a no-op when no sub-manager is loaded.

    def setFLUO(self) -> None:
        """ Switch the stand to fluorescence (widefield) mode. """
        if self._subManager is not None:
            self._subManager.setFLUO()

    def setCS(self) -> None:
        """ Switch the stand to confocal-scanning mode. """
        if self._subManager is not None:
            self._subManager.setCS()

    def setILshutter(self, value) -> None:
        """ Set the incident/illumination-light shutter state. """
        if self._subManager is not None:
            self._subManager.setILshutter(value)


# Copyright (C) 2020-2023 ImSwitch developers
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
