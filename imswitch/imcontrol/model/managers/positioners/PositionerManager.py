from abc import ABC, abstractmethod
import json
import os
import threading

from typing import Dict, List

from imswitch.imcommon.model import dirtools, initLogger
from imswitch.imcontrol.model.devices.status import DeviceManagerStatusMixin


_POSITION_PERSISTENCE_FILENAME = 'positioner_positions.json'
_POSITION_PERSISTENCE_SCHEMA_VERSION = 1
_POSITION_PERSISTENCE_LOCK = threading.RLock()



class PositionerManager(DeviceManagerStatusMixin, ABC):
    """ Abstract base class for managers that control positioners. Each type of
    positioner corresponds to a manager derived from this class. """

    requiresReference: bool = False
    referenceWaitAfterS: float = 0.3
    persistsLastPosition: bool = False

    @abstractmethod
    def __init__(self, positionerInfo, name: str, initialPosition: Dict[str, float]):
        """
        Args:
            positionerInfo: See setup file documentation.
            name: The unique name that the device is identified with in the
              setup file.
            initialPosition: The initial position for each axis. This is a dict
              in the format ``{ axis: position }``.
        """

        self._positionerInfo = positionerInfo
        self._position = initialPosition

        self.__logger = initLogger(self, instanceName=name)

        self.__name = name

        self.__axes = positionerInfo.axes
        self.__referencedAxes = {axis: not self.requiresReference for axis in self.__axes}
        self.__restoredPositionAxes = set()
        self.__forPositioning = positionerInfo.forPositioning
        self.__forScanning = positionerInfo.forScanning
        self.__resetOnClose = positionerInfo.resetOnClose
        self.__joystick = positionerInfo.joystick
        self.__liveUpdate = positionerInfo.liveUpdate
        self.__hide = getattr(positionerInfo, 'hide', False)
        self.__shortcutModifier = getattr(positionerInfo, 'shortcutModifier', None)
        if not positionerInfo.forPositioning and not positionerInfo.forScanning:
            raise ValueError('At least one of forPositioning and forScanning must be set in'
                             ' PositionerInfo.')

        if self.persistsLastPosition:
            self._restorePersistedPositions()

    @property
    def name(self) -> str:
        """ Unique positioner name, defined in the positioner's setup info. """
        return self.__name

    @property
    def position(self) -> Dict[str, float]:
        """ The position of each axis. This is a dict in the format
        ``{ axis: position }``. """
        return self._position

    @property
    def defaultReferencePosition(self):
        """Default software position used by reference(), or None if unspecified."""
        return None

    @property
    def defaultReferenceVoltage(self):
        """Default hardware voltage used by reference(), or None if unspecified."""
        return None

    def positionToVoltage(self, position):
        """Return the hardware voltage for a software position, if applicable."""
        return None

    def isPositionRestored(self, axis: str) -> bool:
        """Whether this axis' displayed position came from session persistence.

        A restored value is only the last command remembered by ImSwitch. It is
        deliberately not treated as a hardware-verified position and does not
        imply that the axis is referenced.
        """
        if axis not in self.axes:
            raise ValueError(
                f'Axis {axis} not available. Available axes: {self.axes}'
            )
        return axis in self.__restoredPositionAxes

    def updateTrackedPosition(self, positions: Dict[str, float]) -> None:
        """ Sync the cached position from an external hardware read.

        Public API for device-specific controllers that read the live position
        straight from hardware and need the manager's tracked ``position`` to
        match — *without* commanding a move. Only known axes are updated. Use
        this instead of writing ``manager._position`` directly. """
        for axis, value in positions.items():
            if axis in self._position:
                self._position[axis] = float(value)
                self.__restoredPositionAxes.discard(axis)

    @property
    def axes(self) -> List[str]:
        """ The list of axes that are controlled by this positioner. """
        return self.__axes

    @property
    def forPositioning(self) -> bool:
        """ Whether the positioner is used for manual positioning. """
        return self.__forPositioning

    @property
    def isReferenceActionable(self) -> bool:
        """Whether this positioner can be referenced through the UI workflow."""
        return bool(
            self.requiresReference
            and self.forPositioning
            and not self.hide
        )

    @property
    def forScanning(self) -> bool:
        """ Whether the positioner is used for scanning. """
        return self.__forScanning

    @property
    def resetOnClose(self) -> bool:
        """ Whether the positioner should be reset to 0-position upon closing. """
        return self.__resetOnClose
    @property
    def joystick(self) -> bool:
        """ Whether the positioner is connected to a joystick. """
        return self.__joystick
    @property
    def liveUpdate(self) -> bool:
        """ Whether the positioner position should be updated live. """
        return self.__liveUpdate

    @property
    def hide(self) -> bool:
        """Whether this positioner is hidden from the manual Positioner widget."""
        return self.__hide

    @property
    def isAvailable(self) -> bool:
        """Whether this positioner should be exposed to UI/controllers."""
        return getattr(self, 'device', True) is not None

    @property
    def shortcutModifier(self):
        """ Keyboard-shortcut group used to jog this positioner from the
        Positioner widget: ``"ctrl"``, ``"ctrl-shift"``, or ``None``. """
        return self.__shortcutModifier
    
    @property
    def isReferenced(self)-> bool:
        """Wether all axes currently have a valid software reference."""
        return all(self.__referencedAxes.values())

    def isAxisReferenced(self, axis:str)->bool:
        """Wether specific axis has a valid software reference."""
        if axis not in self.__referencedAxes:
            raise ValueError(
                f'Axis {axis} not available. Available axes: {self.axes}'
            )
        return self.__referencedAxes[axis]

    @abstractmethod
    def move(self, dist: float, axis: str):
        """ Moves the positioner by the specified distance and returns the new
        position. Derived classes will update the position field manually. If
        the positioner controls multiple axes, the axis must be specified. """
        pass

    @abstractmethod
    def setPosition(self, position: float, axis: str):
        """ Adjusts the positioner to the specified position and returns the
        new position. Derived classes will update the position field manually.
        If the positioner controls multiple axes, the axis must be specified.
        """
        pass

    def finalize(self) -> None:
        """ Close/cleanup positioner. """
        pass

    def markReferenced(self,axis=None) -> None:
        self._setReferenceState(axis,True)
    
    def markUnreferenced(self,axis=None) -> None:
        self._setReferenceState(axis,False)

    def reference(self, axis: str = None, position=None) -> None:
        """Establish a known position reference for the requested axis.

        Positioners that require explicit referencing must override this method.
        
        arg:``position`` optionally specifies the position at which the axis should
        be referenced. If omitted, the manager chooses its default reference
        position.
        """
        if self.requiresReference:
            raise NotImplementedError(
                f'{self.__class__.__name__} requires referencing but does not '
                'implement reference().'
            )

    def _recordCommandedPosition(self, axis: str, position: float) -> None:
        """Update tracked state after a command known to have reached hardware.

        Persistent managers call this instead of assigning ``_position``
        directly. The current-session command supersedes any startup-restored
        provenance, then the value is written to the shared persistence store.
        """
        if axis not in self._position:
            raise ValueError(
                f'Axis {axis} not available. Available axes: {self.axes}'
            )
        self._position[axis] = float(position)
        self.__restoredPositionAxes.discard(axis)
        if self.persistsLastPosition:
            self._persistPosition(axis)

    def _normalizePersistedPosition(self, axis: str, position: float) -> float:
        """Adapt a stored software position to the current setup configuration.

        Subclasses may override this to clamp or otherwise validate the value
        before it is adopted. This hook must never command hardware.
        """
        return float(position)

    def _restorePersistedPositions(self) -> None:
        """Restore opted-in tracked positions without commanding hardware."""
        data = self._readPositionPersistenceData()
        positioners = data.get('positioners', {})
        managerTypeData = positioners.get(self.__class__.__name__, {})
        if not isinstance(managerTypeData, dict):
            return
        managerData = managerTypeData.get(self.name, {})
        if not isinstance(managerData, dict):
            return

        for axis in self.axes:
            stored = managerData.get(axis)
            if stored is None:
                continue
            if isinstance(stored, dict):
                stored = stored.get('position')
            if stored is None:
                continue
            try:
                position = self._normalizePersistedPosition(axis, stored)
            except (TypeError, ValueError) as error:
                self.__logger.warning(
                    'Ignoring invalid persisted position for "%s" axis %s: %s',
                    self.name, axis, error,
                )
                continue
            self._adoptPersistedPosition(axis, position)

    def _adoptPersistedPosition(self, axis: str, position: float, *, persist=False) -> None:
        """Adopt a persisted value without motion and mark its provenance."""
        if axis not in self._position:
            raise ValueError(
                f'Axis {axis} not available. Available axes: {self.axes}'
            )
        self._position[axis] = float(position)
        self.__restoredPositionAxes.add(axis)
        if persist and self.persistsLastPosition:
            self._persistPosition(axis)

    def _persistPosition(self, axis: str) -> None:
        """Persist one tracked axis atomically. Failures remain non-fatal."""
        path = self._positionPersistenceFile()
        try:
            with _POSITION_PERSISTENCE_LOCK:
                data = self._readPositionPersistenceData()
                positioners = data.setdefault('positioners', {})
                managers = positioners.get(self.__class__.__name__)
                if not isinstance(managers, dict):
                    managers = {}
                    positioners[self.__class__.__name__] = managers
                axes = managers.get(self.name)
                if not isinstance(axes, dict):
                    axes = {}
                    managers[self.name] = axes
                axes[axis] = {'position': float(self._position[axis])}

                os.makedirs(os.path.dirname(path), exist_ok=True)
                tempPath = f'{path}.tmp'
                with open(tempPath, 'w') as file:
                    json.dump(data, file, indent=2)
                os.replace(tempPath, path)
        except (OSError, TypeError, ValueError):
            self.__logger.warning(
                'Could not persist position for "%s" axis %s to %s; the '
                'position will not survive a restart.',
                self.name, axis, path, exc_info=True,
            )

    def _readPositionPersistenceData(self):
        path = self._positionPersistenceFile()
        with _POSITION_PERSISTENCE_LOCK:
            try:
                with open(path, 'r') as file:
                    data = json.load(file)
            except (FileNotFoundError, OSError, ValueError):
                data = {}

        if not isinstance(data, dict):
            data = {}
        if data.get('schema_version') != _POSITION_PERSISTENCE_SCHEMA_VERSION:
            # There is currently only one schema. Unknown/legacy content in
            # this generic file is ignored rather than guessed.
            data = {
                'schema_version': _POSITION_PERSISTENCE_SCHEMA_VERSION,
                'positioners': {},
            }
        elif not isinstance(data.get('positioners'), dict):
            data['positioners'] = {}
        return data

    def _positionPersistenceFile(self):
        return os.path.join(
            dirtools.UserFileDirs.Config,
            _POSITION_PERSISTENCE_FILENAME,
        )

    def _setReferenceState(self, axis, referenced:bool) -> None:
        """
        Mark axis reference state. If arg:``axis`` is None, all axes 
        are set at the same time, so that single axis positioner 
        does not have to specify the axis.
        Note that a non-referenced axis will raise an error.
        """
        axes = self.axes if axis is None else [axis]

        for ax in axes:
            if ax not in self.__referencedAxes:
                raise ValueError(
                    f'Axis {ax} not available for reference. Available axis:{self.axes}'
                )
            self.__referencedAxes[ax] = bool(referenced)


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
