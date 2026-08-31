from typing import Dict, Any

from imswitch.imcommon.model import APIExport, initLogger
from imswitch.imcontrol.model import getWidgetStatePersistence
from ..basecontrollers import ImConWidgetController, StatefulComponentMixin, ComponentStateApplyMode
from PyQt5.QtCore import QTimer 

class RotatorController(ImConWidgetController, StatefulComponentMixin):
    """ Linked to RotatorWidget."""

    componentName = 'Rotator'
    stateSchemaVersion = 1
    legacyStateNames = ()

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

        # Lifecycle callbacks may run on the Hardware Status reconnect worker.
        # Refresh only the affected rotator positions on this controller's UI
        # thread; a shared Elliptec bus reconnect can affect several rows.
        self._deviceLifecycleListener = None
        lifecycleService = getattr(self._master, 'deviceLifecycleService', None)
        if lifecycleService is not None:
            self._deviceLifecycleListener = self._deviceLifecycleChanged
            lifecycleService.addListener(self._deviceLifecycleListener)

        for name, _ in self._master.rotatorsManager:
            self.updatePosition(name)
        
        # Register for unified state persistence (canonical name)
        getWidgetStatePersistence().register('Rotator', self)
 
    def closeEvent(self):
        lifecycleService = getattr(self._master, 'deviceLifecycleService', None)
        listener = getattr(self, '_deviceLifecycleListener', None)
        if lifecycleService is not None and listener is not None:
            lifecycleService.removeListener(listener)
            self._deviceLifecycleListener = None

    def _deviceLifecycleChanged(self, result):
        affected = tuple(
            device_id
            for device_id in getattr(result, 'affected_device_ids', ())
            if getattr(device_id, 'kind', None) == 'rotator'
        )
        if not affected:
            return
        self._invokeOnControllerThreadIfNeeded(
            lambda: self._refreshLifecycleAffectedRotators(affected)
        )

    def _refreshLifecycleAffectedRotators(self, device_ids):
        known = {name for name, _ in self._master.rotatorsManager}
        for device_id in device_ids:
            if device_id.name in known:
                self.updatePosition(device_id.name)

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
    
    # Unified State Persistence Interface (StatefulComponentMixin)
    
    def getComponentState(self) -> dict:
        """Snapshot current rotator speed and step size settings.
        
        Returns speed and step size per rotator.
        Does NOT include continuous rotation state or current position — those
        are hardware state, not UI settings.
        
        Returns:
            {
                'rotators': {
                    name: {
                        'speed': int,
                        'step': float
                    },
                    ...
                }
            }
        """
        state = {'rotators': {}}
        
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
    
    def applyComponentState(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode
    ) -> list[str]:
        """Restore rotator speed and step size settings from a snapshot.
        
        IDENTICAL behavior in both STARTUP_RESTORE and SETUP_MODE_APPLY:
        - Restore speed setting per rotator
        - Restore step size per rotator
        
        NEVER (in either mode):
        - Rotate to a saved position
        - Start continuous rotation
        - Activate hardware
        
        Per spec Section 0 D2: settings only, never activation.
        
        Args:
            state: Dict returned by getComponentState()
            applyMode: STARTUP_RESTORE or SETUP_MODE_APPLY (no behavioral difference)
        
        Returns:
            List of warning strings (empty if fully successful)
        """
        warnings = []
        rotators_state = state.get('rotators', {})
        known_rotators = {name for name, _ in self._master.rotatorsManager}
        
        for name, rotator_state in rotators_state.items():
            if name not in known_rotators:
                warnings.append(f'Rotator "{name}" not present in current setup; skipped.')
                continue
            
            if 'speed' in rotator_state:
                try:
                    self._widget.setSpeed(name, rotator_state['speed'])
                except Exception as e:
                    warnings.append(f'Failed to restore speed for rotator "{name}": {e}')
            
            if 'step' in rotator_state:
                try:
                    self._widget.setRelStepSize(name, str(rotator_state['step']))
                except Exception as e:
                    warnings.append(f'Failed to restore step size for rotator "{name}": {e}')
        
        return warnings
    
    def describeComponentState(self, state: dict) -> list[str]:
        """Generate human-readable summary of saved rotator settings.
        
        Args:
            state: Dict returned by getComponentState()
        
        Returns:
            List of formatted strings suitable for setup-mode inspector
        """
        rotators_state = state.get('rotators', {})
        if not rotators_state:
            return ['  no rotator settings saved']
        
        summaries = ['  settings:']
        for name in sorted(rotators_state.keys()):
            rotator_state = rotators_state[name]
            speed = rotator_state.get('speed', 'N/A')
            step = rotator_state.get('step', 'N/A')
            summaries.append(f'    {name}: speed={speed}, step={step}°')
        
        return summaries
    
    def getComponentStateHazards(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode,
        context: dict | None = None
    ) -> list[dict]:
        """Identify potential hazards in saved rotator state.
        
        Rotator speed and step settings have no hazards (they do not rotate).
        
        Args:
            state: Dict returned by getComponentState()
            applyMode: STARTUP_RESTORE or SETUP_MODE_APPLY
            context: Optional consumer-provided context (unused)
        
        Returns:
            Empty list (no hazards)
        """
        return []


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
