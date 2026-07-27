from typing import List, Union, Dict, Any

from imswitch.imcommon.model import APIExport
from imswitch.imcontrol.model import configfiletools, getWidgetStatePersistence
from imswitch.imcontrol.view import guitools
from ..basecontrollers import (
    ComponentStateApplyMode,
    ImConWidgetController,
    SetupModeApplyPriority,
    StatefulComponentMixin,
)


class LaserController(ImConWidgetController, StatefulComponentMixin):
    """ Linked to LaserWidget."""

    # StatefulComponentMixin attributes
    componentName = 'Laser'
    stateSchemaVersion = 1
    legacyStateNames = ()
    setupModeCategory = 'excitation'
    setupModeApplyPriority = SetupModeApplyPriority.EXCITATION
    setupModeHardwareCritical = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.settingAttr = False
        self.is_scanning = False
        # Scan membership is published once per iteration, including every
        # repeat frame; the arming/force-off only needs to run once per scan
        # sequence. This guards against re-issuing blocking serial laser
        # commands on every frame. Cleared in scanChanged(False).
        self._scanBuiltApplied = False

        # Set up lasers
        for lName, lManager in self._master.lasersManager:
            if lManager.usesCalibrationLookup():
                valueRangeMin = 0
                valueRangeMax = 100
                valueUnits = "%"
                valueDecimals = 1
                valueRangeStep = 0.5 if lManager.valueRangeStep is not None else None
            else:
                valueRangeMin = lManager.valueRangeMin
                valueRangeMax = lManager.valueRangeMax
                valueUnits = lManager.valueUnits
                valueDecimals = lManager.valueDecimals
                valueRangeStep = lManager.valueRangeStep

            self._widget.addLaser(
                lName, valueUnits, valueDecimals, lManager.wavelength,
                (valueRangeMin, valueRangeMax) if not lManager.isBinary else None,
                valueRangeStep if valueRangeStep is not None else None,
                (lManager.freqRangeMin, lManager.freqRangeMax, lManager.freqRangeInit) if lManager.isModulated else (0, 0, 0),
            )
            # Ensure laser/LED is off and at zero power on startup, regardless of
            # hardware state left over from a previous session.
            self._master.lasersManager[lName].setEnabled(False)
            if not lManager.isBinary:
                self.valueChanged(lName, valueRangeMin)

            self.setSharedAttr(lName, _enabledAttr, self._widget.isLaserActive(lName))
            self.setSharedAttr(lName, _valueAttr, self._widget.getValue(lName))

        # Load presets
        for laserPresetName in self._setupInfo.laserPresets:
            self._widget.addPreset(laserPresetName)

        self._widget.setCurrentPreset(None)  # Unselect

        # Connect CommunicationChannel signals
        self._commChannel.sharedAttrs.sigAttributeSet.connect(self.attrChanged)
        self._commChannel.sigScanStarting.connect(lambda: self.scanChanged(True))
        # Hardware arming runs on scanDevicesResolved (DAQ still free);
        # sigScanBuilt arrives after the manager is busy and is UI-only.
        self._commChannel.sigScanDevicesResolved.connect(self.scanDevicesResolved)
        self._commChannel.sigScanBuilt.connect(self.scanBuilt)
        self._commChannel.sigScanEnded.connect(lambda: self.scanChanged(False))

        # Connect LaserWidget signals
        self._widget.sigEnableChanged.connect(self.toggleLaser)
        self._widget.sigValueChanged.connect(self.valueChanged)

        self._widget.sigModEnabledChanged.connect(self.toggleModulation)
        self._widget.sigFreqChanged.connect(self.frequencyChanged)
        self._widget.sigDutyCycleChanged.connect(self.dutyCycleChanged)

        self._widget.sigPresetSelected.connect(self.presetSelected)
        self._widget.sigLoadPresetClicked.connect(self.loadPreset)
        self._widget.sigSavePresetClicked.connect(self.savePreset)
        self._widget.sigSavePresetAsClicked.connect(self.savePresetAs)
        self._widget.sigDeletePresetClicked.connect(self.deletePreset)
        
        # Register for unified state persistence (canonical name)
        getWidgetStatePersistence().register('Laser', self)

    def closeEvent(self):
        self._master.lasersManager.execOnAll(lambda l: l.setScanModeActive(False))
        self._master.lasersManager.execOnAll(lambda l: l.setValue(0))
        self._master.lasersManager.execOnAll(lambda l: l.setEnabled(False))

    def toggleLaser(self, laserName, enabled):
        """ Enable or disable laser (on/off)."""
        self._master.lasersManager[laserName].setEnabled(enabled)
        self.setSharedAttr(laserName, _enabledAttr, enabled)

    def valueChanged(self, laserName, magnitude):
        """ Change magnitude. """
        self._setLaserValue(laserName, magnitude)
    
    def toggleModulation(self, laserName, enabled):
        """ Enable or disable laser modulation (on/off). """
        self._master.lasersManager[laserName].setModulationEnabled(enabled)
        self.setSharedAttr(laserName, _freqEnAttr, enabled)

    def frequencyChanged(self, laserName, frequency):
        """ Change modulation frequency. """
        self._master.lasersManager[laserName].setModulationFrequency(frequency)
        self._widget.setModulationFrequency(laserName, frequency)
        self.setSharedAttr(laserName, _freqAttr, frequency)
    
    def dutyCycleChanged(self, laserName, dutyCycle):
        """ Change modulation duty cycle. """
        self._master.lasersManager[laserName].setModulationDutyCycle(dutyCycle)
        self._widget.setModulationDutyCycle(laserName, dutyCycle)
        self.setSharedAttr(laserName, _dcAttr, dutyCycle)

    def presetSelected(self, presetName):
        """ Handles what happens when a preset is selected in the preset list.
        """
        if presetName:
            self._widget.setCurrentPreset(presetName)

    def loadPreset(self):
        """ Handles what happens when the user requests the selected preset to
        be loaded. """
        presetToLoad = self._widget.getCurrentPreset()
        if not presetToLoad:
            return

        if presetToLoad not in self._setupInfo.laserPresets:
            return

        # Load values
        self.applyPreset(self._setupInfo.laserPresets[presetToLoad])

    def savePreset(self, name=None):
        """ Saves current values to a preset. If the name parameter is None,
        the values will be saved to the currently selected preset. """

        if not name:
            name = self._widget.getCurrentPreset()
            if not name:
                return

        # Add in GUI
        if name not in self._setupInfo.laserPresets:
            self._widget.addPreset(name)

        # Set in setup info
        self._setupInfo.setLaserPreset(name, self.makePreset())
        configfiletools.saveSetupInfo(configfiletools.loadOptions()[0], self._setupInfo)

        # Update selected preset in GUI
        self._widget.setCurrentPreset(name)

    def savePresetAs(self):
        """ Handles what happens when the user requests the current laser
        values to be saved as a new preset. """

        name = guitools.askForTextInput(self._widget, 'Add laser preset',
                                        'Enter a name for this preset:')

        if not name:  # No name provided
            return

        add = True
        if name in self._setupInfo.laserPresets:
            add = guitools.askYesNoQuestion(
                self._widget,
                'Laser preset already exists',
                f'A preset with the name "{name}" already exists. Do you want to overwrite it"?'
            )

        if add:
            self.savePreset(name)

    def deletePreset(self):
        """ Handles what happens when the user requests the selected preset to
        be deleted. """

        presetToDelete = self._widget.getCurrentPreset()
        if not presetToDelete:
            return

        confirmationResult = guitools.askYesNoQuestion(
            self._widget,
            'Delete laser preset?',
            f'Are you sure you want to delete the preset "{presetToDelete}"?'
        )

        if confirmationResult:
            # Remove in GUI
            self._widget.removePreset(presetToDelete)

            # Remove from setup info
            self._setupInfo.removeLaserPreset(presetToDelete)
            configfiletools.saveSetupInfo(configfiletools.loadOptions()[0], self._setupInfo)

    def makePreset(self):
        """ Returns a preset object corresponding to the current laser values.
        """
        return {lName: guitools.LaserPresetInfo(value=self._widget.getValue(lName))
                for lName, lManager in self._master.lasersManager if not lManager.isBinary}

    def applyPreset(self, laserPreset):
        """ Loads a preset object into the current values. """
        knownLasers = {lName for lName, _ in self._master.lasersManager}
        for laserName, laserPresetInfo in laserPreset.items():
            if laserName not in knownLasers:
                self._logger.warning(
                    f'Laser preset references unavailable laser "{laserName}"; skipped.'
                )
                continue
            if self._master.lasersManager[laserName].isBinary:
                self._logger.warning(
                    f'Laser preset references binary laser "{laserName}"; skipped.'
                )
                continue
            if not hasattr(laserPresetInfo, 'value'):
                self._logger.warning(
                    f'Laser preset entry for "{laserName}" has no value; skipped.'
                )
                continue
            self.setLaserValue(laserName, laserPresetInfo.value)

    def scanChanged(self, isScanning):
        """ Handles what happens when a scan is started/stopped.

        Arming individual lasers is the job of :meth:`scanBuilt`, which
        receives the authoritative list of lasers participating in the scan.
        scanChanged only handles UI editability and the safe teardown
        (disarming every laser) when the scan ends. This makes the scan
        module's device list the single source of truth for which lasers emit;
        scan powers are the current widget setpoints unless a scanner-specific
        workflow changes them explicitly.
        """

        self.is_scanning = isScanning
        if not isScanning:
            # Scan sequence finished — re-arm the one-shot scanBuilt logic.
            self._scanBuiltApplied = False

        for lName, _ in self._master.lasersManager:
            self._widget.setLaserEditable(lName, not isScanning)
            if not isScanning:
                # Teardown: disarm every laser so none is left in scan
                # (digital-modulation / external-control) mode after the scan.
                # Arming is deferred to scanBuilt; scanChanged only ever
                # disarms, never arms.
                self._master.lasersManager[lName].setScanModeActive(False)

    def scanBuilt(self, deviceList):
        """ Refresh laser UI editability for the built scan.

        Hardware arming deliberately does NOT happen here. sigScanBuilt is
        emitted from inside runScan, after the NI-DAQ manager has marked itself
        busy, so every one-shot digital/analog write this used to perform was
        refused — silently before the DAQ manager was hardened, and as a
        logged error afterwards. The consequence was real: a laser outside the
        scan's device list was never actually forced off and stayed on for the
        whole scan. Arming moved to scanDevicesResolved, which fires while the
        DAQ is still free.
        """
        for lName, _ in self._master.lasersManager:
            self._widget.setLaserEditable(lName, lName not in deviceList)

    def scanDevicesResolved(self, deviceList):
        """ Arm exactly the lasers participating in the imminent scan.

        Runs before the scan claims the DAQ, so the one-shot writes below
        actually reach the hardware.

        The scan module's device list is the sole authority for which lasers
        emit:

        - Lasers in the list are armed for TTL-gated emission at their current
          widget power (``setScanModeActive(True)``).
        - Lasers not in the list that the scan can drive via the DAQ /
          TriggerScope (they have an analog channel or a digital line) are
          force-disabled.
        - Pure RS232 lasers with no scan line are left in the state the user
          set, since the scan cannot gate them.

        The blocking hardware commands run once per scan sequence (guarded by
        ``_scanBuiltApplied``) so serial commands are not re-issued on every
        repeated scan frame. """
        if self._scanBuiltApplied:
            return
        for lName, _ in self._master.lasersManager:
            inScan = lName in deviceList
            if inScan:
                # Arm: scan/digital-modulation mode at the laser's current
                # widget power. Emission stays gated by the scanner's TTL line.
                self._master.lasersManager[lName].setScanModeActive(True)
                continue
            # Not participating. Only force off lasers the scan can actually
            # drive via the DAQ/TriggerScope (analog channel or digital line).
            # Pure RS232-controlled lasers with no scan line cannot be gated,
            # so we leave them in the state the user set.
            info = self._setupInfo.lasers.get(lName)
            hasScanChannel = info is not None and (
                info.getAnalogChannel() is not None
                or info.getDigitalLine() is not None
            )
            if not hasScanChannel:
                continue
            # Disarm and force off lasers not participating in this scan.
            self._master.lasersManager[lName].setScanModeActive(False)
            self._master.lasersManager[lName].setEnabled(False)
            # Sync the UI toggle silently so it reflects the forced-off
            # hardware state without re-triggering toggleLaser.
            self._widget.setLaserActive(lName, False, emitSignal=False)
        self._scanBuiltApplied = True

    def attrChanged(self, key, value):
        if self.settingAttr or len(key) != 3 or key[0] != _attrCategory:
            return

        laserName = key[1]
        if key[2] == _enabledAttr:
            self.setLaserActive(laserName, value)
        elif key[2] == _valueAttr:
            self.setLaserValue(laserName, value)

    def setSharedAttr(self, laserName, attr, value):
        self.settingAttr = True
        try:
            self._commChannel.sharedAttrs[(_attrCategory, laserName, attr)] = value
        finally:
            self.settingAttr = False

    def _setLaserValue(self, laserName, value):
        self._master.lasersManager[laserName].setValue(value)
        self._widget.setValue(laserName, value, emitSignal=False)
        self.setSharedAttr(laserName, _valueAttr, value)
        try:
            numericValue = float(value)
        except (TypeError, ValueError):
            numericValue = None
        if (
            numericValue is not None
            and numericValue <= 0
            and not self._master.lasersManager[laserName].isBinary
        ):
            self._master.lasersManager[laserName].setEnabled(False)
            self._widget.setLaserActive(laserName, False, emitSignal=False)
            self.setSharedAttr(laserName, _enabledAttr, False)

    @APIExport()
    def getLaserNames(self) -> List[str]:
        """ Returns the device names of all lasers. These device names can be
        passed to other laser-related functions. """
        return self._master.lasersManager.getAllDeviceNames()

    @APIExport(runOnUIThread=True)
    def setLaserActive(self, laserName: str, active: bool) -> None:
        """ Sets whether the specified laser is powered on. """
        self._widget.setLaserActive(laserName, active)

    @APIExport(runOnUIThread=True)
    def setLaserValue(self, laserName: str, value: Union[int, float]) -> None:
        """ Sets the value of the specified laser, in the units that the laser
        uses. """
        self._setLaserValue(laserName, value)

    @APIExport()
    def changeScanPower(self, laserName, laserValue):
        self.setLaserValue(laserName, laserValue)

    @APIExport(runOnUIThread=True)
    def sendTrigger(self, triggerId: int):
        """ Sends a trigger puls through external device """
        #TODo: Very special case, try to move in seperate manager 
        self._master.rs232sManager["ESP32"].sendTrigger(triggerId)

    @APIExport(runOnUIThread=True)
    def post_json(self, path: str, payload: dict) -> str:
        """ Sends the specified command to the RS232 device and returns a
        string encoded from the received bytes. """
        return self._master.rs232sManager["ESP32"].post_json(path, payload=payload, headers=None, timeout=1)

    @APIExport(runOnUIThread=True)
    def send_serial(self, payload: str) -> str:
        """ Sends the specified command to the RS232 device and returns a
        string encoded from the received bytes. """
        self._master.rs232sManager["ESP32"].writeSerial(payload)
        #self.__logger.debug(payload)
        returnmessage = self._master.rs232sManager["ESP32"].readSerial(is_blocking=True, timeout=1)

        return returnmessage
    
    # Unified State Persistence Interface (StatefulComponentMixin)
    
    def getComponentState(self) -> dict:
        """Snapshot current laser state for both startup and setup modes.
        
        Returns a JSON-serializable dict with structure expected by the
        existing SetupModesController summarizer (lifted in describeComponentState).
        
        Returns:
            {
                'lasers': {
                    laserName: {
                        'enabled': bool,
                        'value': float | None,
                        'isBinary': bool,
                        'valueUnits': str
                    }
                },
                'laserOrder': [laserName, ...],
                'currentPreset': str | None,
                'modulation': {
                    laserName: {
                        'frequency': int,
                        'dutyCycle': int
                    }
                }
            }
        
        """
        state = {
            'lasers': {},
            'laserOrder': [],
            'currentPreset': self._widget.getCurrentPreset(),
            'modulation': {}
        }
        
        for lName, lManager in self._master.lasersManager:
            state['laserOrder'].append(lName)
            state['lasers'][lName] = {
                'enabled': self._widget.isLaserActive(lName),
                'value': self._widget.getValue(lName) if not lManager.isBinary else None,
                'isBinary': lManager.isBinary,
                'valueUnits': lManager.valueUnits
            }
            
            module = self._widget.laserModules.get(lName)
            if lManager.isModulated and module is not None:
                modState = {}
                if hasattr(module, 'getFrequency'):
                    modState['frequency'] = module.getFrequency()
                if hasattr(module, 'getDutyCycle'):
                    modState['dutyCycle'] = module.getDutyCycle()
                if modState:
                    state['modulation'][lName] = modState
        
        return state
    
    def applyComponentState(self, state: dict, *, applyMode: ComponentStateApplyMode) -> list[str]:
        """Restore laser state from a snapshot.
        
        ALWAYS (both modes):
        - Set non-binary laser power values
        - Set modulation frequency/duty cycle
        - Restore selected-preset label in UI (does not apply it)
        
        ONLY in SETUP_MODE_APPLY:
        - Enable/disable lasers per saved 'enabled' state
        
        NEVER in STARTUP_RESTORE:
        - Enable lasers or emit light (append warning if saved state has enabled=True)
        
        Args:
            state: Dict returned by getComponentState()
            applyMode: ComponentStateApplyMode.STARTUP_RESTORE or SETUP_MODE_APPLY
        
        Returns:
            List of warning strings (empty if fully successful)
        """
        warnings = []
        known_lasers = {name for name, _ in self._master.lasersManager}
        lasers = state.get('lasers', {})
        
        # Always restore power values and modulation for non-binary lasers
        for lName, laserState in lasers.items():
            if lName not in known_lasers:
                warnings.append(f'Laser "{lName}" not present in current setup; skipped.')
                continue
            
            lManager = self._master.lasersManager[lName]
            
            # Set power value for non-binary lasers
            if not laserState.get('isBinary', False):
                value = laserState.get('value')
                if value is not None:
                    try:
                        self.setLaserValue(lName, value)
                    except Exception as e:
                        warnings.append(f'Failed to restore value for laser "{lName}": {e}')
            
            # Restore modulation settings
            modulation = state.get('modulation', {}).get(lName, {})
            if modulation:
                freq = modulation.get('frequency')
                if freq is not None:
                    try:
                        self.frequencyChanged(lName, freq)
                    except Exception as e:
                        warnings.append(f'Failed to restore modulation frequency for laser "{lName}": {e}')
                
                dc = modulation.get('dutyCycle')
                if dc is not None:
                    try:
                        self.dutyCycleChanged(lName, dc)
                    except Exception as e:
                        warnings.append(f'Failed to restore modulation duty cycle for laser "{lName}": {e}')
        
        # Enable/disable lasers ONLY in SETUP_MODE_APPLY
        if applyMode == ComponentStateApplyMode.SETUP_MODE_APPLY:
            for lName, laserState in lasers.items():
                if lName not in known_lasers:
                    continue
                enabled = laserState.get('enabled', False)
                if enabled and not laserState.get('isBinary', False):
                    value = self._asFloat(laserState.get('value'))
                    if value is not None and value <= 0:
                        enabled = False
                try:
                    self.setLaserActive(lName, enabled)
                except Exception as e:
                    warnings.append(f'Failed to set enable state for laser "{lName}": {e}')
        elif any(laserState.get('enabled', False) for laserState in lasers.values()):
            warnings.append('Laser enable states not restored in startup mode.')
        
        # Restore selected preset (UI label only — does NOT apply it to hardware)
        selected_preset = state.get('currentPreset')
        if selected_preset and selected_preset in self._setupInfo.laserPresets:
            self._widget.setCurrentPreset(selected_preset)
        
        return warnings
    
    def describeComponentState(self, state: dict) -> list[str]:
        """Generate human-readable summary of a saved laser state.
        
        Lifted from SetupModesController._summarizeSavedLaserState,
        _savedLaserItemsInDisplayOrder, _currentLaserDisplayOrder.
        
        Args:
            state: Dict returned by getComponentState()
        
        Returns:
            List of formatted strings suitable for setup-mode inspector
        """
        state = state or {}
        summaries = []
        
        if state.get("currentPreset") is not None:
            summaries.append(f"  preset: {self._fmt(state.get('currentPreset'))}")

        lasers = state.get("lasers") or {}
        if lasers:
            summaries.append("  states:")
        
        for laserName, laserState in self._savedLaserItemsInDisplayOrder(lasers, state):
            enabled = self._onOff(laserState.get("enabled"))
            if laserState.get("isBinary"):
                summaries.append(f"    {laserName}: {enabled}")
            else:
                units = laserState.get("valueUnits") or ""
                unitText = f" {units}" if units else ""
                summaries.append(
                    f"    {laserName}: {enabled}, {self._fmt(laserState.get('value'))}{unitText}"
                )
        
        return summaries or ["  no laser state"]
    
    def getComponentStateHazards(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode,
        context: dict | None = None
    ) -> list[dict]:
        """Identify high-power laser hazards in a saved state.
        
        Lifted from SetupModesController._getHighPowerLaserEntries,
        _isMilliwattUnit, _laserPowerThresholdMw.
        
        Only reports hazards relevant to SETUP_MODE_APPLY (enabling lasers).
        For STARTUP_RESTORE, returns empty list (no enable happens).
        
        Args:
            state: Dict returned by getComponentState()
            applyMode: The mode in which the state would be applied
            context: Optional dict with 'laserPowerThresholdMw' (default 50.0)
        
        Returns:
            List of hazard records per spec §5.3
        """
        # No hazards for STARTUP_RESTORE since we don't enable lasers
        if applyMode != ComponentStateApplyMode.SETUP_MODE_APPLY:
            return []
        
        context = context or {}
        thresholdMw = context.get('laserPowerThresholdMw', 50.0)
        
        lasers = state.get('lasers', {})
        if not isinstance(lasers, dict):
            return []
        
        hazards = []
        for laserName, savedState in lasers.items():
            if not isinstance(savedState, dict):
                continue
            if not savedState.get('enabled', False):
                continue
            if savedState.get('isBinary', False):
                continue
            
            value = self._asFloat(savedState.get('value'))
            if value is None or value <= thresholdMw:
                continue
            
            units = savedState.get('valueUnits')
            if not self._isMilliwattUnit(units):
                continue
            
            hazards.append({
                'kind': 'high_laser_power',
                'severity': 'warning',
                'message': f'Laser {laserName}: {value} mW exceeds {thresholdMw} mW threshold',
                'details': {
                    'laserName': laserName,
                    'value': value,
                    'units': units or 'mW',
                    'threshold': thresholdMw
                }
            })
        
        return hazards
    
    # Helper methods (lifted from SetupModesController)
    
    def _savedLaserItemsInDisplayOrder(self, lasers, state):
        """Order saved laser entries by saved order, current order, then keys."""
        names = []
        savedOrder = state.get("laserOrder")
        if isinstance(savedOrder, list):
            names.extend(savedOrder)
        
        names.extend(self._currentLaserDisplayOrder())
        names.extend(lasers.keys())
        
        orderedItems = []
        seen = set()
        for name in names:
            if name in seen or name not in lasers:
                continue
            orderedItems.append((name, lasers[name]))
            seen.add(name)
        
        return orderedItems
    
    def _currentLaserDisplayOrder(self):
        """Get current laser display order from lasersManager or widget."""
        if hasattr(self._master, 'lasersManager'):
            try:
                return [laserName for laserName, _ in self._master.lasersManager]
            except Exception:
                pass
        
        if hasattr(self._widget, 'laserModules') and isinstance(self._widget.laserModules, dict):
            return list(self._widget.laserModules.keys())
        
        return []
    
    def _fmt(self, value):
        """Format a value for human-readable display."""
        if value is None:
            return "None"
        if isinstance(value, bool):
            return self._onOff(value)
        if isinstance(value, float):
            return f"{value:.4g}"
        if isinstance(value, (list, tuple)):
            return "[" + ", ".join(self._fmt(item) for item in value) + "]"
        return str(value)
    
    def _onOff(self, value):
        """Convert boolean to ON/OFF string."""
        return "ON" if bool(value) else "OFF"
    
    def _asFloat(self, value):
        """Safely convert value to float, returning None on failure."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    
    def _isMilliwattUnit(self, units):
        """Check if units represent milliwatts."""
        if units is None or str(units).strip() == "":
            return True
        normalized = str(units).strip().lower()
        return normalized in {"mw", "milliwatt", "milliwatts"}




_attrCategory = 'Laser'
_enabledAttr = 'Enabled'
_valueAttr = 'Value'
_freqEnAttr = "ModulationEnabled"
_freqAttr = "Frequency"
_dcAttr = "DutyCycle"


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
