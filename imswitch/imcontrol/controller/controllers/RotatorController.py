from typing import Dict, Any

from imswitch.imcommon.model import APIExport, initLogger
from imswitch.imcontrol.model import getWidgetStatePersistence
from ..basecontrollers import ImConWidgetController
from PyQt5.QtCore import QTimer 

class RotatorController(ImConWidgetController):
    """ Linked to RotatorWidget."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.__logger = initLogger(self, tryInheritParent=True)

        # Set up rotator in widget
        for name, _ in self._master.rotatorsManager:
            self._widget.addRotator(name)

        # Connect PositionerWidget signals
        self._widget.sigMoveRelClicked.connect(lambda name, dir: self.moveRel(name, dir))
        self._widget.sigMoveAbsClicked.connect(lambda name: self.moveAbs(name, self._widget.getAbsPos(name))) # Simone: to get scriptable, make sure that when the button is clicked, the method still fetches the absolute postion from the GUI widget
        self._widget.sigSetZeroClicked.connect(lambda name: self.setZeroPos(name))
        self._widget.sigSetSpeedClicked.connect(lambda name: self.setSpeed(name))
        self._widget.sigStartContMovClicked.connect(lambda name: self.startContMov(name))
        self._widget.sigStopContMovClicked.connect(lambda name: self.stopContMov(name))

        # Connect commChannel signals
        self._commChannel.sigUpdateRotatorPosition.connect(lambda name: self.updatePosition(name))
        self._commChannel.sigSetSyncInMovementSettings.connect(lambda name, pos, rel_shift, enabled: self.setSyncInMovement(name, pos, rel_shift, enabled))

        for name, _ in self._master.rotatorsManager:
            self.updatePosition(name)
        
        # Register for widget state persistence
        getWidgetStatePersistence().register('RotatorController', self)
 
    def closeEvent(self):
        pass

    def moveRel(self, name, dir=1):
        dist = dir * self._widget.getRelStepSize(name)
        self._master.rotatorsManager[name].move_rel(dist)
        self.updatePosition(name)

    @APIExport(runOnUIThread=True)
    def moveAbs(self, name, pos): #change by Simone: for scriptable, add pos position
        #pos = self._widget.getAbsPos(name) #commented by Simone because we want it scriptable. Now connected to the widget through line 20
        self._master.rotatorsManager[name].move_abs(pos)
        self.updatePosition(name)

    def setZeroPos(self, name):
        self._master.rotatorsManager[name].set_zero_pos()
        self.updatePosition(name)

    def setSpeed(self, name):
        speed = self._widget.getSpeed(name)
        self._master.rotatorsManager[name].set_rot_speed(speed)

    def startContMov(self, name):
        self._master.rotatorsManager[name].start_cont_rot()

    def stopContMov(self, name):
        self._master.rotatorsManager[name].stop_cont_rot()

    def updatePosition(self, name):
        pos = self._master.rotatorsManager[name].position
        #self.__logger.info(f'{name} pos is {pos}') #debugging simone
        self._widget.updatePosition(name, pos)

    def setSyncInMovement(self, name, pos, rel_shift, enabled):
        self._master.rotatorsManager[name].set_sync_in_set(pos, rel_shift, enabled)
    
    # Widget State Persistence Interface
    
    def getWidgetState(self) -> Dict[str, Any]:
        """
        Get current widget state for persistence.
        
        Returns speed and step size per rotator.
        Does NOT include continuous rotation state or current position.
        
        Returns:
            Dict with structure:
            {
                'version': 1,
                'rotators': {
                    name: {
                        'speed': int,
                        'step': float
                    },
                    ...
                }
            }
        """
        state = {
            'version': 1,
            'rotators': {}
        }
        
        for name, _ in self._master.rotatorsManager:
            state['rotators'][name] = {}
            try:
                state['rotators'][name]['speed'] = self._widget.getSpeed(name)
            except Exception as e:
                self.__logger.debug(f'Could not save speed for rotator {name}: {e}')
            
            try:
                state['rotators'][name]['step'] = self._widget.getRelStepSize(name)
            except Exception as e:
                self.__logger.debug(f'Could not save step size for rotator {name}: {e}')
        
        return state
    
    def setWidgetState(self, state: Dict[str, Any]) -> None:
        """
        Restore widget state from persistence.
        
        SAFETY: Does NOT restore continuous rotation state or position. Only restores:
        - Speed setting per rotator
        - Step size per rotator
        
        Args:
            state: Dict returned by getWidgetState()
        """
        try:
            rotators_state = state.get('rotators', {})
            known_rotators = {name for name, _ in self._master.rotatorsManager}
            
            for name, rotator_state in rotators_state.items():
                if name not in known_rotators:
                    self.__logger.debug(f'Skipping state for non-existent rotator: {name}')
                    continue
                
                # Restore speed
                if 'speed' in rotator_state:
                    try:
                        self._widget.setSpeed(name, rotator_state['speed'])
                    except Exception as e:
                        self.__logger.warning(f'Failed to restore speed for rotator {name}: {e}')
                
                # Restore step size
                if 'step' in rotator_state:
                    try:
                        self._widget.setRelStepSize(name, str(rotator_state['step']))
                    except Exception as e:
                        self.__logger.warning(f'Failed to restore step size for rotator {name}: {e}')
            
            self.__logger.debug('Widget state restored successfully')
        
        except Exception as e:
            self.__logger.error(f'Failed to restore widget state: {e}')
    
    def getStateSchemaVersion(self) -> int:
        """Return schema version for state compatibility checking."""
        return 1


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
