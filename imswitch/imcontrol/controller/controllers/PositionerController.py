from typing import Dict, List, Any

from imswitch.imcommon.model import APIExport
from imswitch.imcontrol.model import getWidgetStatePersistence
from ..basecontrollers import ImConWidgetController, StatefulComponentMixin, ComponentStateApplyMode
from imswitch.imcommon.model import initLogger
from qtpy.QtCore import QTimer

class PositionerController(ImConWidgetController, StatefulComponentMixin):
    """ Linked to PositionerWidget."""

    componentName = 'Positioner'
    stateSchemaVersion = 1
    legacyStateNames = ()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._liveUpdateIntervalMs = 300
        self._liveUpdateTimer = QTimer()
        self._liveUpdateTimer.setInterval(self._liveUpdateIntervalMs)
        self._liveUpdateTimer.timeout.connect(self._refreshLiveUpdatedPositioners)

        self.settingAttr = False
        self._previousJoystickState = None

        self.__logger = initLogger(self, tryInheritParent=True)

        # Set up positioners
        for pName, pManager in self._master.positionersManager:
            if not pManager.forPositioning:
                continue

            if getattr(pManager, 'device', True) is None:
                continue

            if pManager.joystick:
                self._widget.addJoystick(pName)

            speed = hasattr(pManager, 'speed')
            self._widget.addPositioner(
                pName,
                pManager.axes,
                speed,
                pManager.joystick,
                shortcutModifier=pManager.shortcutModifier,
                unit=getattr(pManager, 'positionUnit', 'µm')
            )
            for axis in pManager.axes:
                self.setSharedAttr(pName, axis, _positionAttr, pManager.position[axis])
                if speed:
                    self.setSharedAttr(pName, axis, _positionAttr, pManager.speed)
                # Push the manager's current position into the widget so the
                # displayed value reflects the actual hardware state at
                # startup — not just the initialPosition=0 placeholder.
                self.updatePosition(pName, axis)

            if pManager.joystick:
                # Set joystick checkbox status for first start
                self.setJoystickCheckStatus(self._master.positionersManager[pName].joystickStatus)
                # Connect channels
                self._commChannel.sigRecordingStarted.connect(
                    lambda pName=pName: self.setJoystickStatusForRec(False, pName)
                )
                self._commChannel.sigRecordingEnded.connect(
                    lambda pName=pName: self.setJoystickStatusAfterRec(pName)
                )
                # self._commChannel.sigInitiateEtMonalisa.connect(lambda state, pName=pName: self.setJoystickStatus(not state, pName))

                if hasattr(pManager, "sigJoystickStatusChanged"):
                    pManager.sigJoystickStatusChanged.connect(
                        lambda enabled, pName=pName: self._onManagerJoystickStatusChanged(pName, enabled)
                    )
        
        self._widget.sigJoystickToggled.connect(self.requestJoystickStatus)
        self._updateLiveTimerState()
        self._refreshLiveUpdatedPositioners()

        # Connect CommunicationChannel signals
        self._commChannel.sharedAttrs.sigAttributeSet.connect(self.attrChanged)
        self._commChannel.sigSetSpeed.connect(lambda speed: self.setSpeedGUI(speed))


        # Connect PositionerWidget signals
        self._widget.sigStepUpClicked.connect(self.stepUp)
        self._widget.sigStepDownClicked.connect(self.stepDown)
        self._widget.sigsetSpeedClicked.connect(self.setSpeedGUI)
        
        # Register for unified state persistence (canonical name)
        getWidgetStatePersistence().register('Positioner', self)
    

    def _onManagerJoystickStatusChanged(self, pName, enabled):
        self.setJoystickCheckStatus(enabled)

    def _hasLiveUpdatePositioner(self):
        for _, pManager in self._master.positionersManager:
            if not pManager.forPositioning:
                continue
            if getattr(pManager, 'device', True) is None:
                continue
            if getattr(pManager, 'liveUpdate', False):
                return True
        return False


    def _updateLiveTimerState(self):
        if self._hasLiveUpdatePositioner():
            if not self._liveUpdateTimer.isActive():
                self._liveUpdateTimer.start()
        else:
            if self._liveUpdateTimer.isActive():
                self._liveUpdateTimer.stop()


    def _refreshLiveUpdatedPositioners(self):
        for pName, pManager in self._master.positionersManager:
            if not pManager.forPositioning:
                continue
            if getattr(pManager, 'device', True) is None:
                continue
            if not getattr(pManager, 'liveUpdate', False):
                continue

            self.updatePosition(pName, 'all')

    def setJoystickStatusAfterRec(self, pName):
        if self._previousJoystickState:
            # if the joystick was enabled before the scan, enable it again after rec
            self.requestJoystickStatus(True, pName)
        self._previousJoystickState = None

    def setJoystickStatusForRec(self, enabled, pName):
        if not enabled and self._previousJoystickState is None:
            pManager = self._master.positionersManager[pName]
            self._previousJoystickState = getattr(pManager, "joystickStatus", False)
        self.requestJoystickStatus(enabled, pName)


    def requestJoystickStatus(self, enabled, pName):
        pManager = self._master.positionersManager[pName]

        if not hasattr(pManager, "setJoystickEnabled"):
            return

        pManager.setJoystickEnabled(enabled)

    def setJoystickCheckStatus(self, state:bool):
        if not state and self._widget.joystickCheck.isChecked():
            self._widget.joystickCheck.setChecked(False)
        if state and not self._widget.joystickCheck.isChecked():
            self._widget.joystickCheck.setChecked(True)

    def closeEvent(self):
        if hasattr(self, '_liveUpdateTimer') and self._liveUpdateTimer.isActive():
            self._liveUpdateTimer.stop()
        self._master.positionersManager.execOnAll(
            lambda p: [p.setPosition(0, axis) for axis in p.axes],
            condition = lambda p: p.resetOnClose
        )

    def getPos(self):
        return self._master.positionersManager.execOnAll(lambda p: p.position)

    def getSpeed(self):
        return self._master.positionersManager.execOnAll(lambda p: p.speed)

    def move(self, positionerName, axis, dist):
        """ Moves positioner by dist micrometers in the specified axis. """
        self._master.positionersManager[positionerName].move(dist, axis)
        self.updatePosition(positionerName, axis)

    def setPos(self, positionerName, axis, position):
        """ Moves the positioner to the specified position in the specified axis. """
        self._master.positionersManager[positionerName].setPosition(position, axis)
        self.updatePosition(positionerName, axis)

    def stepUp(self, positionerName, axis):
        self.move(positionerName, axis, self._widget.getStepSize(positionerName, axis))

    def stepDown(self, positionerName, axis):
        self.move(positionerName, axis, -self._widget.getStepSize(positionerName, axis))

    def setSpeedGUI(self):
        positionerName = self.getPositionerNames()[0]
        speed = self._widget.getSpeed()
        self.setSpeed(positionerName=positionerName, speed=speed)

    def setSpeed(self, positionerName, speed=(1000,1000,1000)):
        self._master.positionersManager[positionerName].setSpeed(speed)
        
    def updatePosition(self, positionerName, axis):
        pManager = self._master.positionersManager[positionerName]

        if hasattr(pManager, 'updatePosition'):
            pManager.updatePosition()

        if axis == 'all':
            for axisName in self._master.positionersManager[positionerName].axes:
                newPos = self._master.positionersManager[positionerName].position[axisName]
                self._widget.updatePosition(positionerName, axisName, newPos)
                self.setSharedAttr(positionerName, axisName, _positionAttr, newPos)
        else:
            newPos = self._master.positionersManager[positionerName].position[axis]
            self._widget.updatePosition(positionerName, axis, newPos)
            self.setSharedAttr(positionerName, axis, _positionAttr, newPos)



    def attrChanged(self, key, value):
        if self.settingAttr or len(key) != 4 or key[0] != _attrCategory:
            return

        positionerName = key[1]
        axis = key[2]
        if key[3] == _positionAttr:
            self.setPositioner(positionerName, axis, value)

    def setSharedAttr(self, positionerName, axis, attr, value):
        self.settingAttr = True
        try:
            self._commChannel.sharedAttrs[(_attrCategory, positionerName, axis, attr)] = value
        finally:
            self.settingAttr = False

    def setXYPosition(self, x, y):
        positionerX = self.getPositionerNames()[0]
        positionerY = self.getPositionerNames()[1]
        self.__logger.debug(f"Move {positionerX}, axis X, dist {str(x)}")
        self.__logger.debug(f"Move {positionerY}, axis Y, dist {str(y)}")
        #self.move(positionerX, 'X', x)
        #self.move(positionerY, 'Y', y)

    def setZPosition(self, z):
        positionerZ = self.getPositionerNames()[2]
        self.__logger.debug(f"Move {positionerZ}, axis Z, dist {str(z)}")
        #self.move(self.getPositionerNames[2], 'Z', z)

    @APIExport()
    def getPositionerNames(self) -> List[str]:
        """ Returns the device names of all positioners. These device names can
        be passed to other positioner-related functions. """
        return self._master.positionersManager.getAllDeviceNames()

    @APIExport()
    def getPositionerPositions(self) -> Dict[str, Dict[str, float]]:
        """ Returns the positions of all positioners. """
        return self.getPos()

    @APIExport(runOnUIThread=True)
    def setPositionerStepSize(self, positionerName: str, stepSize: float) -> None:
        """ Sets the step size of the specified positioner to the specified
        number of micrometers. """
        self._widget.setStepSize(positionerName, stepSize)

    @APIExport(runOnUIThread=True)
    def movePositioner(self, positionerName: str, axis: str, dist: float) -> None:
        """ Moves the specified positioner axis by the specified number of
        micrometers. """
        self.move(positionerName, axis, dist)

    @APIExport(runOnUIThread=True)
    def setPositioner(self, positionerName: str, axis: str, position: float) -> None:
        """ Moves the specified positioner axis to the specified position. """
        self.setPos(positionerName, axis, position)

    @APIExport(runOnUIThread=True)
    def setPositionerSpeed(self, positionerName: str, speed: float) -> None:
        """ Moves the specified positioner axis to the specified position. """
        self.setSpeed(positionerName, speed)

    @APIExport(runOnUIThread=True)
    def setMotorsEnabled(self, positionerName: str, is_enabled: int) -> None:
        """ Moves the specified positioner axis to the specified position. """
        self._master.positionersManager[positionerName].setEnabled(is_enabled)

    @APIExport(runOnUIThread=True)
    def stepPositionerUp(self, positionerName: str, axis: str) -> None:
        """ Moves the specified positioner axis in positive direction by its
        set step size. """
        self.stepUp(positionerName, axis)

    @APIExport(runOnUIThread=True)
    def stepPositionerDown(self, positionerName: str, axis: str) -> None:
        """ Moves the specified positioner axis in negative direction by its
        set step size. """
        self.stepDown(positionerName, axis)
    
    # Unified State Persistence Interface (StatefulComponentMixin)
    
    def getComponentState(self) -> dict:
        """Snapshot current positioner step sizes for both startup and setup modes.
        
        Returns step sizes per (positionerName, axis) pair.
        Does NOT include position values or speed — those are hardware state,
        not UI settings.
        
        Returns:
            {
                'step_sizes': {
                    positionerName: {
                        axis: float,
                        ...
                    },
                    ...
                }
            }
        """
        state = {'step_sizes': {}}
        
        for pName, pManager in self._master.positionersManager:
            if not pManager.forPositioning:
                continue
            
            state['step_sizes'][pName] = {}
            for axis in pManager.axes:
                try:
                    state['step_sizes'][pName][axis] = self._widget.getStepSize(pName, axis)
                except Exception as e:
                    self._logger.debug(
                        f'Could not save step size for positioner {pName}, axis {axis}: {e}'
                    )
        
        return state
    
    def applyComponentState(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode
    ) -> list[str]:
        """Restore positioner step sizes from a snapshot.
        
        IDENTICAL behavior in both STARTUP_RESTORE and SETUP_MODE_APPLY:
        - Restore step sizes per (positionerName, axis) pair
        
        NEVER (in either mode):
        - Move any stage
        - Change speed settings
        - Activate hardware
        
        Per spec Section 0 D2: settings only, never activation.
        
        Args:
            state: Dict returned by getComponentState()
            applyMode: STARTUP_RESTORE or SETUP_MODE_APPLY (no behavioral difference)
        
        Returns:
            List of warning strings (empty if fully successful)
        """
        warnings = []
        step_sizes = state.get('step_sizes', {})
        known_positioners = {name for name, _ in self._master.positionersManager}
        
        for pName, axes_state in step_sizes.items():
            if pName not in known_positioners:
                warnings.append(f'Positioner "{pName}" not present in current setup; skipped.')
                continue
            
            pManager = self._master.positionersManager[pName]
            if not pManager.forPositioning:
                continue
            
            for axis, step_size in axes_state.items():
                if axis not in pManager.axes:
                    warnings.append(f'Axis "{axis}" not present in positioner "{pName}"; skipped.')
                    continue
                
                try:
                    self._widget.setStepSize(pName, axis, str(step_size))
                except Exception as e:
                    warnings.append(f'Failed to restore step size for {pName}.{axis}: {e}')
        
        return warnings
    
    def describeComponentState(self, state: dict) -> list[str]:
        """Generate human-readable summary of saved positioner step sizes.
        
        Args:
            state: Dict returned by getComponentState()
        
        Returns:
            List of formatted strings suitable for setup-mode inspector
        """
        step_sizes = state.get('step_sizes', {})
        if not step_sizes:
            return ['  no positioner step sizes saved']
        
        summaries = ['  step sizes:']
        for pName in sorted(step_sizes.keys()):
            axes_state = step_sizes[pName]
            for axis in sorted(axes_state.keys()):
                step_size = axes_state[axis]
                summaries.append(f'    {pName}.{axis}: {step_size:.2f} µm')
        
        return summaries
    
    def getComponentStateHazards(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode,
        context: dict | None = None
    ) -> list[dict]:
        """Identify potential hazards in saved positioner state.
        
        Positioner step sizes have no hazards (they do not move the stage).
        
        Args:
            state: Dict returned by getComponentState()
            applyMode: STARTUP_RESTORE or SETUP_MODE_APPLY
            context: Optional consumer-provided context (unused)
        
        Returns:
            Empty list (no hazards)
        """
        return []




_attrCategory = 'Positioner'
_positionAttr = 'Position'


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
