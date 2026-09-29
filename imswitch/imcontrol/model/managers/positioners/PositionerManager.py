from abc import ABC, abstractmethod

from typing import Dict, List


class PositionerManager(ABC):
    """ Abstract base class for managers that control positioners. Each type of
    positioner corresponds to a manager derived from this class. """

    requiresReference: bool = False
    referenceWaitAfterS: float = 0.3

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

        self.__name = name

        self.__axes = positionerInfo.axes
        self.__referencedAxes = {axis: not self.requiresReference for axis in self.__axes}
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

    def updateTrackedPosition(self, positions: Dict[str, float]) -> None:
        """ Sync the cached position from an external hardware read.

        Public API for device-specific controllers that read the live position
        straight from hardware and need the manager's tracked ``position`` to
        match — *without* commanding a move. Only known axes are updated. Use
        this instead of writing ``manager._position`` directly. """
        for axis, value in positions.items():
            if axis in self._position:
                self._position[axis] = float(value)

    @property
    def axes(self) -> List[str]:
        """ The list of axes that are controlled by this positioner. """
        return self.__axes

    @property
    def forPositioning(self) -> bool:
        """ Whether the positioner is used for manual positioning. """
        return self.__forPositioning

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
