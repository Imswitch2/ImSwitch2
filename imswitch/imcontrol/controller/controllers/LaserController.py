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
        # Lasers this scan armed, so teardown touches exactly those and no
        # others (see scanDevicesResolved / _disarmScanLasers).
        self._scanArmedLasers = []

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
        # The scan's TTL device list, published before the DAQ is claimed.
        # This is where lasers are armed — sigScanBuilt arrives after the
        # manager is busy, when one-shot writes are refused.
        self._commChannel.sigScanDevicesResolved.connect(self.scanDevicesResolved)
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

        Arming and its UI locking belong to :meth:`scanDevicesResolved`, which
        knows exactly which lasers the scan owns. This method only tracks the
        scanning flag and tears down at the end.

        It deliberately no longer disables every laser for the duration: the
        scan owns only the lasers it gates, and locking the rest took controls
        away from the user for no reason. It also no longer disarms every
        laser on the way out — ``setScanModeActive(False)`` zeroes the output
        on managers with an analog channel, so that silently destroyed the
        setpoint of an AOM the scan had never touched.
        """

        self.is_scanning = isScanning

        if not isScanning:
            self._disarmScanLasers()

    def _powerDeviceFor(self, laserName):
        """The device that sets ``laserName``'s emission power, if separate.

        Some beam paths split one physical laser in two: a bare digital-line
        entry the scan gates, plus an attenuator (an AOTF channel) that holds
        the power over serial. Only the gate carries a TTL line, so only the
        gate ever appears in the scan's device list — the power device has to
        be reached through this declared link or it is never switched on, and
        the laser emits only when the user enables it by hand.
        """
        info = self._setupInfo.lasers.get(laserName)
        powerDevice = getattr(info, 'powerDevice', None) if info else None
        if not powerDevice:
            return None
        if powerDevice not in self._setupInfo.lasers:
            self._logger.error(
                f'Laser "{laserName}" declares powerDevice "{powerDevice}", '
                f'which is not a configured laser; its power will not be '
                f'switched on for scans.'
            )
            return None
        return powerDevice

    def scanDevicesResolved(self, deviceList):
        """ Arm exactly the lasers the scan programs a TTL sequence for.

        ``deviceList`` is the scan's TTL device list — the rows the user ticks
        in the scan widget — and is the sole authority for participation. It is
        deliberately NOT the NI-DAQ AO/DO device list, which only contains
        devices owning an analog channel or digital line.

        Runs before the scan claims the DAQ, so these writes reach the
        hardware; ``sigScanBuilt`` is already inside the busy window, where
        every one-shot output is refused.

        For each gated laser:

        1. Hand the gate over to the scan (``setScanModeActive(True)``), which
           releases its static line so the scan's DO task owns it.
        2. Switch on its declared ``powerDevice``, if any. This is what makes
           ticking the box sufficient: the attenuator that actually sets the
           power is a separate entry with no TTL line of its own, so without
           this step the scan gates a beam that nothing has turned on.
        3. Lock the on/off buttons of both — the scan owns emission now — but
           leave the power setpoints editable so they can still be tuned.

        The power is never written here. The widget already pushed the user's
        setpoint to the device when they set it, and re-writing it risks
        overwriting a good value with a bad read.

        Arming happens once per scan RUN. Membership is republished for every
        repeat frame, and several managers issue blocking RS232 commands here,
        so re-arming each frame would stall a fast repeat scan.
        """
        if self._scanArmedLasers:
            return

        armed = []
        for lName, _ in self._master.lasersManager:
            if lName not in deviceList:
                continue
            powerDevice = self._powerDeviceFor(lName)
            try:
                self._master.lasersManager[lName].setScanModeActive(True)
                if powerDevice is not None:
                    self._master.lasersManager[powerDevice].setEnabled(True)
                    # Sync the toggle to the hardware we just switched on.
                    # Without this the button reads OFF while the laser emits,
                    # which looks exactly like "I still have to press ON".
                    self._widget.setLaserActive(powerDevice, True,
                                                emitSignal=False)
            except Exception as e:
                self._logger.error(
                    f'Failed to arm laser "{lName}" for the scan: {e}',
                    exc_info=True,
                )
                continue
            armed.append(lName)
            if powerDevice is not None and powerDevice not in armed:
                armed.append(powerDevice)

        self._scanArmedLasers = list(armed)
        for lName in armed:
            self._widget.setLaserEnableEditable(lName, False)

        requested = [name for name in deviceList
                     if name in dict(self._master.lasersManager)]
        failed = [name for name in requested if name not in armed]
        # Name the pairing explicitly. A gate reported as "no powerDevice
        # declared" when one is expected means the setup link is not being
        # read, which shows up on the rig as "I still have to press ON".
        detail = ', '.join(
            f'{gate} -> {self._powerDeviceFor(gate) or "no powerDevice declared"}'
            for gate in requested if gate in armed
        ) or 'none'
        self._logger.debug(
            f'Scan lasers armed: {detail}. Gates handed to the scan TTL; '
            f'declared power devices switched on at their current setpoint.'
        )
        if failed:
            self._logger.warning(
                f'Scan requested these lasers but they could not be armed and '
                f'will not emit: {", ".join(failed)}'
            )

    def _disarmScanLasers(self):
        """ Return every laser this scan armed to a known-off idle state.

        A participating laser ends the scan OFF, and the UI is synced to match
        so the toggle cannot claim the laser is on while the hardware is dark.
        Lasers the scan never armed are untouched — the scan did not own them,
        so it does not get to switch them off.
        """
        for lName in getattr(self, '_scanArmedLasers', ()):
            try:
                manager = self._master.lasersManager[lName]
                manager.setScanModeActive(False)
                manager.setEnabled(False)
                # Leaving scan mode zeroes the output on managers that drive an
                # analog channel (NidaqLaserManager does setValue(0)), which
                # silently discards the user's setpoint: the widget still shows
                # the power, the device is at zero, and every later scan runs
                # dark. Put the setpoint back so device and UI agree again.
                #
                # Never write a zero back. Zero carries no information — the
                # laser is already off via setEnabled — and a widget that reads
                # 0 for a laser the user did set (as the AOTFs do) would
                # otherwise have its real amplitude destroyed here instead.
                setpoint = self._widget.getValue(lName)
                if setpoint:
                    manager.setValue(setpoint)
            except Exception as e:
                self._logger.error(
                    f'Failed to disarm laser "{lName}" after the scan: {e}',
                    exc_info=True,
                )
            self._widget.setLaserActive(lName, False, emitSignal=False)
            self._widget.setLaserEnableEditable(lName, True)
        if self._scanArmedLasers:
            self._logger.debug(
                f'Scan lasers returned to idle and switched off: '
                f'{", ".join(self._scanArmedLasers)}'
            )
        self._scanArmedLasers = []

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
