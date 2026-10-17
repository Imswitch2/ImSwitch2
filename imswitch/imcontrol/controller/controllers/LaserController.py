from typing import List, Union, Dict, Any

from imswitch.imcommon.model import APIExport
from imswitch.imcommon.framework import Signal
from imswitch.imcontrol.model import (
    LaserAcquisitionState,
    LaserPowerContext,
    LaserPowerMode,
    LaserScanPowerControl,
    LaserScanState,
    configfiletools,
    getWidgetStatePersistence,
    normalizeLaserPowerContext,
    normalizeLaserPowerMode,
    laserPowerModeFromSetup,
    normalizeLaserScanState,
)
from copy import deepcopy
from imswitch.imcontrol.view import guitools
from ..basecontrollers import (
    ComponentStateApplyMode,
    ImConWidgetController,
    SetupModeApplyPriority,
    StatefulComponentMixin,
)


class LaserController(ImConWidgetController, StatefulComponentMixin):
    """ Linked to LaserWidget."""

    # Semantic laser-state events. Context is emitted as the stable string
    # value (``live`` / ``scan``) so non-Qt consumers and future workflow
    # services do not depend on widget state or enum serialization.
    sigLaserPowerChanged = Signal(str, str, object)
    sigLaserPresetListChanged = Signal(str)
    sigLaserPresetLoaded = Signal(str, str)

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

        # Scan power remains software-only outside acquisition. Missing values
        # fall back dynamically to the current Live setpoint, preserving the
        # behaviour of existing setups until Scan power is explicitly edited or
        # loaded from a Scan preset.
        self._livePowerValues = {}
        self._scanPowerValues = {}
        self._powerMode = laserPowerModeFromSetup(self._setupInfo)
        self._currentPowerContext = LaserPowerContext.LIVE
        self._scanLaserState = {}
        self._laserDisplayMeta = {}
        self._selectedPresetByContext = {
            LaserPowerContext.LIVE: None,
            LaserPowerContext.SCAN: None,
        }
        self._selectedCoupledPreset = None
        self._setupModeController = None
        # Lasers this scan armed, so teardown touches exactly those and no
        # others (see scanDevicesResolved / _disarmScanLasers).
        self._scanArmedLasers = []
        # Run-level Live/Scan handoff state. Only lasers actually owned by
        # the resolved scan (selected gate plus any declared powerDevice) are
        # snapshotted/suppressed. Non-participating Live lasers deliberately
        # retain the immediate pre-work ImSwitch2 behaviour and are untouched.
        self._scanLiveEnabledSnapshot = None
        self._scanLiveSuppressed = False
        self._resumableLiveEnabledSnapshot = None
        self._scanPreparationFailed = None
        self._scanPowerWrittenLasers = []
        # The subset of _scanArmedLasers whose emission the arming itself
        # switched on (the declared power devices). These are what
        # scanPartDone pauses during the waits of a multi-part run.
        self._scanEnabledLasers = []
        self._scanLasersPaused = False

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

            self._laserDisplayMeta[lName] = (valueUnits, valueDecimals)
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

        # Configure setup-level power policy before populating contextual UI.
        # Coupled keeps the modern sticky preset header but presents one Live-like
        # hardware power view; separated exposes the full Live/Scan UI.
        setPowerMode = getattr(self._widget, 'setPowerMode', None)
        if callable(setPowerMode):
            setPowerMode(self._powerMode.value)
        if hasattr(self._widget, 'setPowerContext'):
            self._widget.setPowerContext(LaserPowerContext.LIVE.value, emitSignal=False)
        self._refreshPresetControls()
        self._refreshAllLaserRows()
        self._refreshResumeLiveAvailability()

        # Connect CommunicationChannel signals
        self._commChannel.sharedAttrs.sigAttributeSet.connect(self.attrChanged)
        self._commChannel.sigScanStarting.connect(lambda: self.scanChanged(True))
        # The scan's TTL device list, published before its execution backend
        # starts. This is where lasers are armed; sigScanBuilt is too late for
        # NI-DAQ writes and only a compatibility event for TriggerScope.
        self._commChannel.sigScanDevicesResolved.connect(self.scanDevicesResolved)
        # One part of a multi-part run finished (a timelapse timepoint; never
        # a repeat frame -- those re-arm without publishing). The run keeps
        # its lasers armed across the wait, so darkness between timepoints is
        # produced here by pausing the emission the arming switched on.
        self._commChannel.sigScanDone.connect(self.scanPartDone)
        self._commChannel.sigScanEnded.connect(lambda: self.scanChanged(False))

        # Connect LaserWidget signals
        self._widget.sigEnableChanged.connect(self.toggleLaser)
        self._widget.sigValueChanged.connect(self.valueChanged)

        self._widget.sigModEnabledChanged.connect(self.toggleModulation)
        self._widget.sigFreqChanged.connect(self.frequencyChanged)
        self._widget.sigDutyCycleChanged.connect(self.dutyCycleChanged)

        self._widget.sigPowerContextChanged.connect(self.powerContextChanged)
        self._widget.sigPresetSelected.connect(self.presetSelected)
        self._widget.sigUpdatePresetClicked.connect(self.updateSelectedPreset)
        self._widget.sigSavePresetAsClicked.connect(self.savePresetAs)
        self._widget.sigSavePresetToClicked.connect(self.saveCurrentValuesTo)
        self._widget.sigRenamePresetClicked.connect(self.renamePreset)
        self._widget.sigDeletePresetClicked.connect(self.deletePreset)
        self._widget.sigClearPresetClicked.connect(self.clearPresetSelection)
        resumeSignal = getattr(self._widget, 'sigResumeLiveClicked', None)
        if resumeSignal is not None:
            resumeSignal.connect(self.resumeLiveIllumination)

        # Scan participation/AO ownership is semantic workflow state, not
        # something LaserWidget infers by inspecting Advanced Scan widgets.
        scanWorkflow = getattr(self._commChannel, 'scanWorkflow', None)
        if scanWorkflow is not None:
            signal = getattr(scanWorkflow, 'sigLaserScanStateChanged', None)
            if signal is not None:
                signal.connect(self.scanLaserStateChanged)
            currentState = getattr(scanWorkflow, 'laser_scan_state', None)
            if callable(currentState):
                self.scanLaserStateChanged(currentState())


        # Lifecycle changes are published by the scoped device lifecycle
        # service, not the global CommunicationChannel. The callback may run on
        # a worker thread, so the handler marshals UI synchronization back onto
        # this controller's affinity thread.
        self._deviceLifecycleListener = None
        lifecycleService = getattr(self._master, 'deviceLifecycleService', None)
        if lifecycleService is not None:
            self._deviceLifecycleListener = self._deviceLifecycleChanged
            lifecycleService.addListener(self._deviceLifecycleListener)
        
        # Register for unified state persistence (canonical name)
        getWidgetStatePersistence().register('Laser', self)

    def setSetupModeController(self, setupModeController):
        """Inject setup-mode storage services without coupling Laser to files."""
        self._setupModeController = setupModeController

    def closeEvent(self):
        lifecycleService = getattr(self._master, 'deviceLifecycleService', None)
        listener = getattr(self, '_deviceLifecycleListener', None)
        if lifecycleService is not None and listener is not None:
            lifecycleService.removeListener(listener)
            self._deviceLifecycleListener = None
        self._master.lasersManager.execOnAll(lambda l: l.setScanModeActive(False))
        self._master.lasersManager.execOnAll(lambda l: l.setValue(0))
        self._master.lasersManager.execOnAll(lambda l: l.setEnabled(False))

    def _deviceLifecycleChanged(self, result):
        deactivated = tuple(
            device_id
            for device_id in getattr(result, 'deactivated_device_ids', ())
            if getattr(device_id, 'kind', None) == 'laser'
        )
        if not deactivated:
            return

        self._invokeOnControllerThreadIfNeeded(
            lambda: self._applyLifecycleDeactivation(deactivated)
        )

    def _applyLifecycleDeactivation(self, device_ids):
        knownLasers = {name for name, _ in self._master.lasersManager}
        deactivatedAny = False
        for device_id in device_ids:
            laserName = device_id.name
            if laserName not in knownLasers:
                continue
            deactivatedAny = True
            # The lifecycle adapter already forced the physical output OFF.
            # Synchronize desired/UI state without issuing another hardware
            # command from the controller.
            self._widget.setLaserActive(laserName, False, emitSignal=False)
            self.setSharedAttr(laserName, _enabledAttr, False)

        if deactivatedAny:
            # A reconnect/deactivation establishes a fresh safe-OFF baseline.
            # Do not let an old post-scan Resume snapshot re-enable a device
            # after lifecycle handling deliberately forced it dark.
            self._setResumableLiveEnabledSnapshot(None)

    def _isSeparatedPowerMode(self):
        return getattr(self, '_powerMode', LaserPowerMode.COUPLED) is LaserPowerMode.SEPARATED

    def _requireLaserEditsAllowed(self):
        """Reject mutations only for separated acquisition ownership.

        Coupled intentionally preserves the pre-work contract: power, modulation
        and non-participating controls remain editable while a scan is active.
        """
        if self._isSeparatedPowerMode() and getattr(self, 'is_scanning', False):
            raise RuntimeError(
                'Laser controls are read-only while a scan is active.'
            )

    def _refreshResumeLiveAvailability(self):
        """Reflect whether separated mode has an explicit post-scan Resume."""
        snapshot = getattr(self, '_resumableLiveEnabledSnapshot', None) or {}
        setter = getattr(self._widget, 'setResumeLiveAvailable', None)
        if callable(setter):
            setter(
                self._isSeparatedPowerMode()
                and any(bool(enabled) for enabled in snapshot.values())
            )

    def _setResumableLiveEnabledSnapshot(self, snapshot):
        if not self._isSeparatedPowerMode():
            snapshot = None
        self._resumableLiveEnabledSnapshot = (
            dict(snapshot) if snapshot and any(snapshot.values()) else None
        )
        self._refreshResumeLiveAvailability()

    def _invalidateResumeLive(self):
        if getattr(self, '_resumableLiveEnabledSnapshot', None) is None:
            return
        self._setResumableLiveEnabledSnapshot(None)

    def toggleLaser(self, laserName, enabled):
        """ Enable or disable laser (on/off)."""
        self._requireLaserEditsAllowed()
        # Any explicit Live ON/OFF action supersedes a pending post-scan
        # restoration. Power/preset/context edits deliberately do not.
        self._invalidateResumeLive()
        applied = self._master.lasersManager[laserName].setEnabled(enabled)
        # Managers may explicitly reject an unsafe enable (for example an MPB
        # laser with no positive setpoint). Keep the toggle and shared state
        # fail-closed instead of displaying ON while the hardware remains dark.
        if applied is False:
            enabled = False
            self._widget.setLaserActive(
                laserName, False, emitSignal=False
            )
        self.setSharedAttr(laserName, _enabledAttr, enabled)
        self._refreshLaserRow(laserName, updatePrimary=False)

    def valueChanged(self, laserName, magnitude):
        """Change the primary power value in the currently displayed context."""
        self._requireLaserEditsAllowed()
        self.setLaserPowerSetting(laserName, magnitude, self._currentPowerContext)
    
    def toggleModulation(self, laserName, enabled):
        """ Enable or disable laser modulation (on/off). """
        self._requireLaserEditsAllowed()
        self._master.lasersManager[laserName].setModulationEnabled(enabled)
        self.setSharedAttr(laserName, _freqEnAttr, enabled)

    def frequencyChanged(self, laserName, frequency):
        """ Change modulation frequency. """
        self._requireLaserEditsAllowed()
        self._master.lasersManager[laserName].setModulationFrequency(frequency)
        self._widget.setModulationFrequency(laserName, frequency)
        self.setSharedAttr(laserName, _freqAttr, frequency)
    
    def dutyCycleChanged(self, laserName, dutyCycle):
        """ Change modulation duty cycle. """
        self._requireLaserEditsAllowed()
        self._master.lasersManager[laserName].setModulationDutyCycle(dutyCycle)
        self._widget.setModulationDutyCycle(laserName, dutyCycle)
        self.setSharedAttr(laserName, _dcAttr, dutyCycle)

    def _rawPresetDefinition(self, name):
        return getattr(self._setupInfo, 'laserPresets', {}).get(name)

    def _normalizePresetPowers(self, rawPowers):
        """Return a ``laser -> LaserPresetInfo`` mapping from JSON or objects."""
        if not isinstance(rawPowers, dict):
            return {}
        normalized = {}
        for laserName, info in rawPowers.items():
            if hasattr(info, 'value'):
                normalized[str(laserName)] = info
                continue
            if isinstance(info, dict) and 'value' in info:
                normalized[str(laserName)] = guitools.LaserPresetInfo(value=info['value'])
        return normalized

    def _presetDefinition(self, name):
        """Return ``(context|None, powers)`` for one global preset name."""
        raw = self._rawPresetDefinition(name)
        if raw is None:
            return None, None
        if isinstance(raw, dict) and 'context' in raw and 'powers' in raw:
            try:
                context = normalizeLaserPowerContext(raw.get('context'))
            except (TypeError, ValueError):
                self._logger.warning(
                    f'Laser preset "{name}" has invalid context {raw.get("context")!r}; '
                    'treating it as unspecified.'
                )
                context = None
            return context, self._normalizePresetPowers(raw.get('powers'))
        # Historical/raw shape: context unspecified and payload unchanged.
        return None, self._normalizePresetPowers(raw)

    @staticmethod
    def _serializePresetPowers(powers):
        result = {}
        for laserName, info in dict(powers or {}).items():
            value = info.value if hasattr(info, 'value') else info.get('value')
            result[str(laserName)] = {'value': value}
        return result

    def _writePresetDefinition(self, name, powers, context):
        serialized = self._serializePresetPowers(powers)
        if context is None:
            payload = serialized
        else:
            context = normalizeLaserPowerContext(context)
            payload = {'context': context.value, 'powers': serialized}
        self._setupInfo.setLaserPreset(name, payload)

    def _presetContext(self, name):
        context, powers = self._presetDefinition(name)
        return context if powers is not None else None

    def _visiblePresetNames(self, context=None):
        names = list(getattr(self._setupInfo, 'laserPresets', {}).keys())
        if not self._isSeparatedPowerMode():
            return names
        context = normalizeLaserPowerContext(
            context if context is not None else self._currentPowerContext
        )
        visible = []
        for name in names:
            presetContext, powers = self._presetDefinition(name)
            if powers is None:
                continue
            if context is LaserPowerContext.LIVE:
                if presetContext is LaserPowerContext.LIVE:
                    visible.append(name)
            elif presetContext in (None, LaserPowerContext.SCAN):
                visible.append(name)
        return visible

    def _selectedPresetName(self, context=None):
        if not self._isSeparatedPowerMode():
            return getattr(self, '_selectedCoupledPreset', None)
        context = normalizeLaserPowerContext(
            context if context is not None else self._currentPowerContext
        )
        return getattr(self, '_selectedPresetByContext', {}).get(context)

    def _setSelectedPresetName(self, context, name):
        if not self._isSeparatedPowerMode():
            self._selectedCoupledPreset = name
            setCurrent = getattr(self._widget, 'setCurrentPreset', None)
            if callable(setCurrent):
                try:
                    setCurrent(name, emitSignal=False)
                except TypeError:
                    setCurrent(name)
            return
        context = normalizeLaserPowerContext(context)
        if not hasattr(self, '_selectedPresetByContext'):
            self._selectedPresetByContext = {}
        self._selectedPresetByContext[context] = name
        if context is getattr(self, '_currentPowerContext', LaserPowerContext.LIVE):
            setCurrent = getattr(self._widget, 'setCurrentPreset', None)
            if callable(setCurrent):
                try:
                    setCurrent(name, emitSignal=False)
                except TypeError:
                    setCurrent(name)

    def _refreshPresetControls(self):
        context = getattr(self, '_currentPowerContext', LaserPowerContext.LIVE)
        names = self._visiblePresetNames(context)
        selected = self._selectedPresetName(context)
        if selected not in names:
            selected = None
        setNames = getattr(self._widget, 'setPresetNames', None)
        if callable(setNames):
            setNames(names, selected=selected)
            return
        setCurrent = getattr(self._widget, 'setCurrentPreset', None)
        if callable(setCurrent):
            try:
                setCurrent(selected)
            except TypeError:
                pass

    def powerContextChanged(self, context):
        """Switch Live/Scan editing context in separated mode only."""
        self._requireLaserEditsAllowed()
        if not self._isSeparatedPowerMode():
            self._currentPowerContext = LaserPowerContext.LIVE
            setContext = getattr(self._widget, 'setPowerContext', None)
            if callable(setContext):
                setContext(LaserPowerContext.LIVE.value, emitSignal=False)
            return
        context = normalizeLaserPowerContext(context)
        self._currentPowerContext = context
        setContext = getattr(self._widget, 'setPowerContext', None)
        if callable(setContext):
            try:
                setContext(context.value, emitSignal=False)
            except TypeError:
                setContext(context.value)
        self._refreshPresetControls()
        self._refreshAllLaserRows()

    def presetSelected(self, presetName):
        """Combo selection immediately loads the selected global preset."""
        if presetName:
            self.loadLaserPreset(presetName, self._currentPowerContext)

    def loadPreset(self):
        presetToLoad = self._selectedPresetName(self._currentPowerContext)
        if not presetToLoad:
            getCurrent = getattr(self._widget, 'getCurrentPreset', None)
            presetToLoad = getCurrent() if callable(getCurrent) else None
        if presetToLoad:
            self.loadLaserPreset(presetToLoad, self._currentPowerContext)

    def updateSelectedPreset(self):
        name = self._selectedPresetName(self._currentPowerContext)
        if name:
            self.saveLaserPreset(name, self._currentPowerContext, select=True)

    def savePreset(self, name=None):
        if not name:
            name = self._selectedPresetName(self._currentPowerContext)
        if not name:
            getCurrent = getattr(self._widget, 'getCurrentPreset', None)
            name = getCurrent() if callable(getCurrent) else None
        if name:
            self.saveLaserPreset(name, self._currentPowerContext, select=True)

    def savePresetAs(self):
        self._requireLaserEditsAllowed()
        name = guitools.askForTextInput(
            self._widget, 'Add laser preset', 'Enter a name for this preset:'
        )
        if not name:
            return
        presets = getattr(self._setupInfo, 'laserPresets', {})
        add = True
        if name in presets:
            add = guitools.askYesNoQuestion(
                self._widget,
                'Laser preset already exists',
                f'A preset with the name "{name}" already exists. Do you want to overwrite it?'
            )
        if add:
            self.saveLaserPreset(name, self._currentPowerContext, select=True)

    def saveCurrentValuesTo(self, name):
        self._requireLaserEditsAllowed()
        if name:
            self.saveLaserPreset(name, self._currentPowerContext, select=False)

    def clearPresetSelection(self):
        self._requireLaserEditsAllowed()
        self._setSelectedPresetName(self._currentPowerContext, None)
        self._refreshPresetControls()

    def renamePreset(self):
        self._requireLaserEditsAllowed()
        oldName = self._selectedPresetName(self._currentPowerContext)
        if not oldName:
            return
        references = self._setupModePresetReferences(oldName)
        dialog = getattr(self._widget, 'showPresetRenameDialog', None)
        if callable(dialog):
            result = dialog(oldName, references)
            if result is None:
                return
            newName = result.get('name')
            updateReferences = bool(result.get('updateSetupModeReferences', True))
        else:
            newName = guitools.askForTextInput(
                self._widget, 'Rename laser preset', f'Enter a new name for "{oldName}":'
            )
            updateReferences = True
        if not newName or newName == oldName:
            return
        if newName in getattr(self._setupInfo, 'laserPresets', {}):
            overwrite = guitools.askYesNoQuestion(
                self._widget,
                'Laser preset already exists',
                f'A preset with the name "{newName}" already exists. Do you want to overwrite it?'
            )
            if not overwrite:
                return
        self.renameLaserPreset(
            oldName, newName, self._currentPowerContext,
            updateSetupModeReferences=updateReferences,
        )

    def deletePreset(self):
        self._requireLaserEditsAllowed()
        presetToDelete = self._selectedPresetName(self._currentPowerContext)
        if not presetToDelete:
            return
        references = self._setupModePresetReferences(presetToDelete)
        confirm = getattr(self._widget, 'confirmPresetDelete', None)
        if callable(confirm):
            confirmationResult = confirm(presetToDelete, references)
        else:
            confirmationResult = guitools.askYesNoQuestion(
                self._widget,
                'Delete laser preset?',
                f'Are you sure you want to delete the preset "{presetToDelete}"?'
            )
        if confirmationResult:
            self.deleteLaserPreset(presetToDelete, self._currentPowerContext)

    @APIExport(runOnUIThread=True)
    def getLaserPresetNames(self, context=LaserPowerContext.LIVE.value) -> List[str]:
        """Return preset names visible in the requested current policy/context."""
        return list(self._visiblePresetNames(context))

    @APIExport(runOnUIThread=True)
    def getLaserPreset(self, name: str, context=LaserPowerContext.LIVE.value) -> Union[Dict[str, Any], None]:
        """Return one global preset's normalized power mapping without applying it."""
        _storedContext, preset = self._presetDefinition(name)
        return dict(preset) if preset is not None else None

    @APIExport(runOnUIThread=True)
    def loadLaserPreset(self, name: str, context=LaserPowerContext.LIVE.value) -> bool:
        self._requireLaserEditsAllowed()
        storedContext, preset = self._presetDefinition(name)
        if preset is None:
            return False
        if self._isSeparatedPowerMode():
            context = normalizeLaserPowerContext(context)
            if name not in self._visiblePresetNames(context):
                return False
            targetContext = context
        else:
            targetContext = LaserPowerContext.LIVE
        self.applyPreset(preset, context=targetContext)
        self._setSelectedPresetName(targetContext, name)
        self._emitLaserPresetLoaded(targetContext, name)
        return True

    @APIExport(runOnUIThread=True)
    def saveLaserPreset(self, name: str, context=LaserPowerContext.LIVE.value, select: bool = False) -> None:
        """Save current values into the single global preset namespace.

        New coupled presets are unspecified; new separated presets inherit the
        active Live/Scan context. Updating an existing preset preserves its
        stored context even when coupled mode can see every preset.
        """
        self._requireLaserEditsAllowed()
        requestedContext = normalizeLaserPowerContext(context)
        existingContext, existingPowers = self._presetDefinition(name)
        isNew = existingPowers is None
        sourceContext = requestedContext if self._isSeparatedPowerMode() else LaserPowerContext.LIVE
        targetContext = (
            (requestedContext if self._isSeparatedPowerMode() else None)
            if isNew else existingContext
        )
        preset = self.makePreset(context=sourceContext)
        self._writePresetDefinition(name, preset, targetContext)
        configfiletools.saveSetupInfo(configfiletools.loadOptions()[0], self._setupInfo)
        if select:
            if self._isSeparatedPowerMode() and sourceContext is LaserPowerContext.SCAN:
                for laserName, info in preset.items():
                    self._setScanPowerValue(laserName, info.value)
            self._setSelectedPresetName(sourceContext, name)
        if isNew:
            self._emitLaserPresetListChanged(sourceContext)
        self._refreshPresetControls()

    @APIExport(runOnUIThread=True)
    def deleteLaserPreset(self, name: str, context=LaserPowerContext.LIVE.value) -> bool:
        self._requireLaserEditsAllowed()
        if name not in getattr(self._setupInfo, 'laserPresets', {}):
            return False
        self._setupInfo.removeLaserPreset(name)
        if getattr(self, '_selectedCoupledPreset', None) == name:
            self._selectedCoupledPreset = None
        for presetContext in (LaserPowerContext.LIVE, LaserPowerContext.SCAN):
            if self._selectedPresetByContext.get(presetContext) == name:
                self._selectedPresetByContext[presetContext] = None
        configfiletools.saveSetupInfo(configfiletools.loadOptions()[0], self._setupInfo)
        self._emitLaserPresetListChanged(context)
        self._refreshPresetControls()
        return True

    @APIExport(runOnUIThread=True)
    def renameLaserPreset(
        self, oldName: str, newName: str, context=LaserPowerContext.LIVE.value,
        updateSetupModeReferences: bool = True,
    ) -> bool:
        self._requireLaserEditsAllowed()
        presets = getattr(self._setupInfo, 'laserPresets', {})
        if oldName not in presets or not newName:
            return False
        raw = presets[oldName]
        self._setupInfo.setLaserPreset(newName, raw)
        if newName != oldName:
            self._setupInfo.removeLaserPreset(oldName)
        if getattr(self, '_selectedCoupledPreset', None) == oldName:
            self._selectedCoupledPreset = newName
        for presetContext in (LaserPowerContext.LIVE, LaserPowerContext.SCAN):
            if self._selectedPresetByContext.get(presetContext) == oldName:
                self._selectedPresetByContext[presetContext] = newName
        if updateSetupModeReferences and newName != oldName:
            self._rewriteSetupModePresetReferences(oldName, newName)
        configfiletools.saveSetupInfo(configfiletools.loadOptions()[0], self._setupInfo)
        self._emitLaserPresetListChanged(context)
        self._refreshPresetControls()
        return True

    def makePreset(self, context=LaserPowerContext.LIVE):
        context = normalizeLaserPowerContext(context)
        return {
            lName: guitools.LaserPresetInfo(value=self.getLaserPowerSetting(lName, context))
            for lName, lManager in self._master.lasersManager if not lManager.isBinary
        }

    def applyPreset(self, laserPreset, context=LaserPowerContext.LIVE):
        context = normalizeLaserPowerContext(context)
        if not self._isSeparatedPowerMode():
            context = LaserPowerContext.LIVE
        knownLasers = {lName for lName, _ in self._master.lasersManager}
        for laserName, laserPresetInfo in dict(laserPreset or {}).items():
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
            self.setLaserPowerSetting(laserName, laserPresetInfo.value, context)

    _AO_TOOLTIP = (
        'Power controlled through analog output by the scan configuration. '
        'Edit it in Advanced Scan → Power Level (%).'
    )

    def scanLaserStateChanged(self, stateByLaser):
        """Consume semantic scan participation/AO state from the workflow service."""
        normalized = {}
        for name, state in dict(stateByLaser or {}).items():
            try:
                normalized[str(name)] = normalizeLaserScanState(state)
            except (TypeError, ValueError):
                self._logger.warning(
                    f'Ignoring invalid scan-laser state for "{name}": {state!r}'
                )
        self._scanLaserState = normalized
        self._refreshAllLaserRows()

    def _scanStateFor(self, laserName):
        return getattr(self, '_scanLaserState', {}).get(laserName, LaserScanState())

    @APIExport(runOnUIThread=True)
    def getLaserScanStatus(self) -> Dict[str, Dict[str, Any]]:
        """Return the legacy scan-configuration status view for all lasers.

        This compatibility API intentionally reports the raw scan-published
        participation/AO state only. New code that needs the power actually
        used under the current laser power mode should call
        :meth:`getLaserAcquisitionState` instead.
        """
        return {
            name: {
                'active_for_scan': self._scanStateFor(name).active_for_scan,
                'power_control': self._scanStateFor(name).power_control.value,
            }
            for name, _ in self._master.lasersManager
        }

    def _laserAcquisitionState(self, laserName: str) -> LaserAcquisitionState:
        """Resolve the acquisition-power behavior for one laser path."""
        # Validate against the live manager collection first: public callers
        # should get the same KeyError they would get from the other laser APIs
        # for an unknown runtime device.
        self._master.lasersManager[laserName]

        scanState = self._scanStateFor(laserName)
        activeForScan = bool(scanState.active_for_scan)
        powerDevice = self._scanPowerTargetFor(laserName)
        powerManager = self._master.lasersManager[powerDevice]
        powerControl = self._scanPowerControlFor(powerDevice)
        powerMode = normalizeLaserPowerMode(
            getattr(self, '_powerMode', LaserPowerMode.COUPLED)
        )

        value = None
        valueContext = None
        usesFallback = False

        # Inactive paths are deliberately reported without an effective
        # scalar: the current acquisition will not own/apply their power.
        # AO-owned paths likewise have no single controller-side scalar.
        if (
            activeForScan
            and powerControl is LaserScanPowerControl.DIRECT
            and not powerManager.isBinary
        ):
            if powerMode is LaserPowerMode.COUPLED:
                value = self.getLaserPowerSetting(
                    powerDevice, LaserPowerContext.LIVE
                )
                valueContext = LaserPowerContext.LIVE
            else:
                scanValues = getattr(self, '_scanPowerValues', {})
                usesFallback = powerDevice not in scanValues
                valueContext = (
                    LaserPowerContext.LIVE
                    if usesFallback else LaserPowerContext.SCAN
                )
                value = self.getLaserPowerSetting(
                    powerDevice, LaserPowerContext.SCAN
                )

        return LaserAcquisitionState(
            active_for_scan=activeForScan,
            power_mode=powerMode,
            power_control=powerControl,
            power_device=powerDevice,
            value=value,
            value_context=valueContext,
            uses_fallback=usesFallback,
        )

    @staticmethod
    def _serializeLaserAcquisitionState(state: LaserAcquisitionState) -> Dict[str, Any]:
        return {
            'active_for_scan': state.active_for_scan,
            'power_mode': state.power_mode.value,
            'power_control': state.power_control.value,
            'power_device': state.power_device,
            'value': state.value,
            'value_context': (
                state.value_context.value
                if state.value_context is not None else None
            ),
            'uses_fallback': state.uses_fallback,
        }

    @APIExport(runOnUIThread=True)
    def getLaserAcquisitionState(self, laserName: str) -> Dict[str, Any]:
        """Return the power behavior the current acquisition would use.

        This resolves setup laser-category ``powerMode``, scan participation, AO
        ownership, gate -> ``powerDevice`` routing and separated-mode Scan
        fallback into one RPC-friendly state object. ``value`` is ``None``
        when the path is inactive or AO-owned because no controller-side
        scalar power is authoritative in those cases.
        """
        return self._serializeLaserAcquisitionState(
            self._laserAcquisitionState(laserName)
        )

    @APIExport(runOnUIThread=True)
    def getLaserAcquisitionStates(self) -> Dict[str, Dict[str, Any]]:
        """Return resolved acquisition-power state for every runtime laser."""
        return {
            name: self._serializeLaserAcquisitionState(
                self._laserAcquisitionState(name)
            )
            for name, _ in self._master.lasersManager
        }

    def _formatLaserPower(self, laserName, value):
        manager = self._master.lasersManager[laserName]
        displayMeta = getattr(self, '_laserDisplayMeta', {}).get(laserName)
        if displayMeta is not None:
            units, decimals = displayMeta
        else:
            units = getattr(manager, 'valueUnits', '')
            decimals = getattr(manager, 'valueDecimals', None)
        try:
            numeric = float(value)
            text = (
                f'{numeric:.{int(decimals)}f}'
                if decimals is not None else f'{numeric:g}'
            )
        except (TypeError, ValueError):
            text = str(value)
        units = str(units or '').strip()
        return f'{text} {units}'.strip()

    def _livePowerValue(self, laserName):
        values = getattr(self, '_livePowerValues', {})
        if laserName in values:
            return values[laserName]
        return self._widget.getValue(laserName)

    def _refreshAllLaserRows(self):
        if not hasattr(self, '_master') or not hasattr(self, '_widget'):
            return
        for laserName, _ in self._master.lasersManager:
            self._refreshLaserRow(laserName)

    def _refreshLaserRow(self, laserName, *, updatePrimary=True):
        """Render backend Live/Scan state into one row if the widget supports it."""
        manager = self._master.lasersManager[laserName]
        widgetType = type(self._widget)
        contextualWidget = any(
            callable(getattr(widgetType, methodName, None))
            for methodName in (
                'setLaserSecondaryStatus', 'setLaserAOOwned', 'setLaserScanActive'
            )
        )
        if not contextualWidget and not updatePrimary:
            return

        if not self._isSeparatedPowerMode():
            if not manager.isBinary and updatePrimary:
                setValue = getattr(self._widget, 'setValue', None)
                if callable(setValue):
                    value = self._livePowerValue(laserName)
                    try:
                        setValue(laserName, value, emitSignal=False)
                    except TypeError:
                        setValue(laserName, value)
            if contextualWidget:
                setAO = getattr(self._widget, 'setLaserAOOwned', None)
                setPowerEditable = getattr(self._widget, 'setLaserPowerEditable', None)
                setScanActive = getattr(self._widget, 'setLaserScanActive', None)
                setSecondary = getattr(self._widget, 'setLaserSecondaryStatus', None)
                if callable(setAO):
                    setAO(laserName, False)
                if callable(setPowerEditable) and not manager.isBinary:
                    setPowerEditable(laserName, True)
                if callable(setScanActive):
                    setScanActive(laserName, False)
                if callable(setSecondary):
                    setSecondary(laserName, '')
            return

        context = getattr(self, '_currentPowerContext', LaserPowerContext.LIVE)
        scanState = self._scanStateFor(laserName)
        activeForScan = bool(scanState.active_for_scan)
        aoOwned = scanState.power_control is LaserScanPowerControl.AO

        if not manager.isBinary:
            livePower = self._livePowerValue(laserName)
            scanPower = self.getLaserPowerSetting(laserName, LaserPowerContext.SCAN)
            if updatePrimary:
                value = livePower if context is LaserPowerContext.LIVE else scanPower
                setValue = getattr(self._widget, 'setValue', None)
                if callable(setValue):
                    try:
                        setValue(laserName, value, emitSignal=False)
                    except TypeError:
                        setValue(laserName, value)
        else:
            livePower = scanPower = None

        if not contextualWidget:
            return

        setAO = getattr(self._widget, 'setLaserAOOwned', None)
        setPowerEditable = getattr(self._widget, 'setLaserPowerEditable', None)
        setScanActive = getattr(self._widget, 'setLaserScanActive', None)
        setSecondary = getattr(self._widget, 'setLaserSecondaryStatus', None)

        if callable(setScanActive):
            setScanActive(laserName, activeForScan)

        if context is LaserPowerContext.LIVE:
            if callable(setAO):
                setAO(laserName, False)
            if callable(setPowerEditable) and not manager.isBinary:
                setPowerEditable(laserName, True)

            if callable(setSecondary):
                if manager.isBinary:
                    text = 'Scan active' if activeForScan else 'Scan inactive'
                elif aoOwned:
                    text = 'AO'
                else:
                    text = f'Scan: {self._formatLaserPower(laserName, scanPower)}'
                setSecondary(
                    laserName, text,
                    emphasized=activeForScan,
                    muted=not activeForScan,
                    tooltip=self._AO_TOOLTIP if aoOwned else '',
                    badge=aoOwned,
                )
            return

        # Scan view: Scan power is primary and Live state is compact/read-only.
        if callable(setAO):
            setAO(laserName, aoOwned, tooltip=self._AO_TOOLTIP)
        if callable(setPowerEditable) and not manager.isBinary:
            setPowerEditable(laserName, not aoOwned)
        if callable(setSecondary):
            liveOn = bool(self._widget.isLaserActive(laserName))
            if manager.isBinary:
                text = f'Live {"ON" if liveOn else "OFF"}'
            else:
                text = (
                    f'Live {"ON" if liveOn else "OFF"} · '
                    f'{self._formatLaserPower(laserName, livePower)}'
                )
            setSecondary(
                laserName, text,
                emphasized=False,
                muted=not activeForScan,
            )

    def _scanWorkflowPreparationError(self, message=None, *, clear=False):
        workflow = getattr(self._commChannel, 'scanWorkflow', None)
        if workflow is None:
            return
        if clear:
            method = getattr(workflow, 'clear_laser_scan_preparation_error', None)
            if callable(method):
                method()
            return
        method = getattr(workflow, 'set_laser_scan_preparation_error', None)
        if callable(method):
            method(message)

    def _prepareScanRun(self):
        """Reset run-level laser handoff state without touching hardware.

        Scan membership is not known at ``sigScanStarting``. To preserve the
        immediate pre-work ImSwitch2 behaviour, non-participating Live lasers
        are left completely untouched; suppression therefore happens later at
        ``sigScanDevicesResolved`` for scan-owned devices only.
        """
        if self._scanLiveEnabledSnapshot is not None:
            return

        self._scanWorkflowPreparationError(clear=True)
        self._scanPreparationFailed = None
        self._scanPowerWrittenLasers = []
        self._setResumableLiveEnabledSnapshot(None)
        self._scanLiveEnabledSnapshot = {}
        self._scanLiveSuppressed = False

    def _scanOwnedDevices(self, deviceList):
        """Return the unique gate/power devices whose hardware this scan owns."""
        owned = []
        for gateName in deviceList:
            if gateName not in self._setupInfo.lasers:
                continue
            for name in (gateName, self._powerDeviceFor(gateName)):
                if name is not None and name not in owned:
                    owned.append(name)
        return owned

    def _suppressParticipatingLiveForScan(self, deviceList):
        """Dark only scan-owned devices that were Live-ON before handoff."""
        if self._scanLiveSuppressed:
            return

        owned = self._scanOwnedDevices(deviceList)
        snapshot = {
            name: bool(self._widget.isLaserActive(name))
            for name in owned
        }
        self._scanLiveEnabledSnapshot = dict(snapshot)
        failures = []

        for name in owned:
            if not snapshot.get(name, False):
                continue
            manager = self._master.lasersManager[name]
            try:
                applied = manager.setEnabled(False)
                if applied is False:
                    raise RuntimeError('manager rejected disable request')
            except Exception as e:
                failures.append(f'{name}: {e}')
                self._logger.error(
                    f'Failed to suppress Live illumination for scan-owned '
                    f'laser "{name}" before the scan: {e}',
                    exc_info=True,
                )
                continue
            self._widget.setLaserActive(name, False, emitSignal=False)
            self.setSharedAttr(name, _enabledAttr, False)

        self._logger.debug(
            '[LaserScan] scan-owned Live snapshot: %s; suppressed: %s',
            snapshot,
            [name for name, enabled in snapshot.items() if enabled],
        )

        if failures:
            message = (
                'Could not reliably suppress participating Live illumination '
                'before scan: ' + '; '.join(failures)
            )
            self._scanPreparationFailed = message
            self._scanWorkflowPreparationError(message)
            self._logger.error('[LaserScan] %s', message)
            raise RuntimeError(message)

        self._scanLiveSuppressed = True

    def scanChanged(self, isScanning):
        if self._isSeparatedPowerMode():
            return self._scanChangedSeparated(isScanning)
        return self._scanChangedCoupled(isScanning)

    def _scanChangedCoupled(self, isScanning):
        """Pre-work coupled run state: no Live/Scan handoff or global lock."""
        self.is_scanning = isScanning
        if isScanning:
            # Coupled never participates in the separated pre-arm veto/Resume
            # protocol. Clear stale service/UI state defensively without
            # touching laser hardware.
            self._scanWorkflowPreparationError(clear=True)
            self._setResumableLiveEnabledSnapshot(None)
            return
        self._disarmScanLasersCoupled()

    def scanDevicesResolved(self, deviceList):
        if self._isSeparatedPowerMode():
            return self._scanDevicesResolvedSeparated(deviceList)
        return self._scanDevicesResolvedCoupled(deviceList)

    def _scanDevicesResolvedCoupled(self, deviceList):
        """Arm scan TTL lasers with the exact pre-work coupled ownership policy."""
        if self._scanArmedLasers:
            self._resumeScanLasersForNextPartCoupled()
            return

        armed = []
        enabled = []
        for lName, _ in self._master.lasersManager:
            if lName not in deviceList:
                continue
            powerDevice = self._powerDeviceFor(lName)
            try:
                self._master.lasersManager[lName].setScanModeActive(True)
                if powerDevice is not None:
                    self._master.lasersManager[powerDevice].setEnabled(True)
                    self._widget.setLaserActive(
                        powerDevice, True, emitSignal=False
                    )
            except Exception as e:
                self._logger.error(
                    f'Failed to arm laser "{lName}" for the scan: {e}',
                    exc_info=True,
                )
                continue
            armed.append(lName)
            if powerDevice is not None and powerDevice not in armed:
                armed.append(powerDevice)
            if powerDevice is not None and powerDevice not in enabled:
                enabled.append(powerDevice)

        self._scanArmedLasers = list(armed)
        self._scanEnabledLasers = list(enabled)
        self._scanLasersPaused = False
        for lName in armed:
            self._widget.setLaserEnableEditable(lName, False)

        requested = [
            name for name in deviceList if name in dict(self._master.lasersManager)
        ]
        failed = [name for name in requested if name not in armed]
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

    def scanPartDone(self):
        if self._isSeparatedPowerMode():
            return self._scanPartDoneSeparated()
        return self._scanPartDoneCoupled()

    def _scanPartDoneCoupled(self):
        if self._scanLasersPaused or not self._scanEnabledLasers:
            return
        self._scanLasersPaused = True
        for lName in self._scanEnabledLasers:
            try:
                self._master.lasersManager[lName].setEnabled(False)
                self._widget.setLaserActive(lName, False, emitSignal=False)
            except Exception as e:
                self._logger.error(
                    f'Failed to pause laser "{lName}" between scan parts: {e}',
                    exc_info=True,
                )

    def _resumeScanLasersForNextPart(self):
        if self._isSeparatedPowerMode():
            return self._resumeScanLasersForNextPartSeparated()
        return self._resumeScanLasersForNextPartCoupled()

    def _resumeScanLasersForNextPartCoupled(self):
        if not self._scanLasersPaused:
            return
        self._scanLasersPaused = False
        for lName in self._scanEnabledLasers:
            try:
                self._master.lasersManager[lName].setEnabled(True)
                self._widget.setLaserActive(lName, True, emitSignal=False)
            except Exception as e:
                self._logger.error(
                    f'Failed to switch laser "{lName}" back on for the next '
                    f'scan part: {e}', exc_info=True,
                )

    def _disarmScanLasers(self):
        if self._isSeparatedPowerMode():
            return self._disarmScanLasersSeparated()
        return self._disarmScanLasersCoupled()

    def _disarmScanLasersCoupled(self):
        """Return exactly the pre-work armed devices to their coupled idle state."""
        for lName in getattr(self, '_scanArmedLasers', ()):
            try:
                manager = self._master.lasersManager[lName]
                manager.setScanModeActive(False)
                manager.setEnabled(False)
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
        self._scanEnabledLasers = []
        self._scanLasersPaused = False

    def _scanChangedSeparated(self, isScanning):
        """Track scan ownership and make the LaserWidget read-only in-run."""
        if isScanning:
            self.is_scanning = True
            setEditable = getattr(self._widget, 'setEditable', None)
            if callable(setEditable):
                setEditable(False)
            self._prepareScanRun()
            return

        try:
            self._disarmScanLasersSeparated()
        finally:
            self.is_scanning = False
            setEditable = getattr(self._widget, 'setEditable', None)
            if callable(setEditable):
                setEditable(True)

    def _scanPowerControlFor(self, laserName):
        return self._scanStateFor(laserName).power_control

    def _scanPowerTargetFor(self, gateName):
        return self._powerDeviceFor(gateName) or gateName

    def _applyDirectScanPowers(self, deviceList):
        """Apply direct Scan setpoints once after owned Live paths are dark."""
        targets = []
        for gateName in deviceList:
            if gateName not in self._setupInfo.lasers:
                continue
            target = self._scanPowerTargetFor(gateName)
            if target not in targets:
                targets.append(target)

        direct = {}
        ao = []
        failures = []
        for target in targets:
            manager = self._master.lasersManager[target]
            if manager.isBinary:
                continue
            if self._scanPowerControlFor(target) is LaserScanPowerControl.AO:
                ao.append(target)
                continue
            try:
                value = self.getLaserPowerSetting(target, LaserPowerContext.SCAN)
                # Zero is a real acquisition setpoint and must be written.
                if value is not None:
                    applied = manager.setValue(value)
                    if applied is False:
                        raise RuntimeError('manager rejected Scan power write')
                    direct[target] = value
                    if target not in self._scanPowerWrittenLasers:
                        self._scanPowerWrittenLasers.append(target)
            except Exception as e:
                failures.append(f'{target}: {e}')
                self._logger.error(
                    f'Failed to apply Scan power for laser "{target}": {e}',
                    exc_info=True,
                )

        self._logger.debug('[LaserScan] direct Scan powers: %s', direct)
        if ao:
            self._logger.debug('[LaserScan] AO-controlled powers: %s', ao)

        if failures:
            message = (
                'Could not apply all direct Scan powers before arming: '
                + '; '.join(failures)
            )
            self._scanPreparationFailed = message
            self._scanWorkflowPreparationError(message)
            raise RuntimeError(message)

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

    def _scanDevicesResolvedSeparated(self, deviceList):
        """ Arm exactly the lasers the scan programs a TTL sequence for.

        ``deviceList`` is the scan's TTL device list — the rows the user ticks
        in the scan widget — and is the sole authority for participation. It is
        deliberately NOT the NI-DAQ AO/DO device list, which only contains
        devices owning an analog channel or digital line.

        Runs before the execution backend starts, so these writes reach the
        hardware. On NI-DAQ, ``sigScanBuilt`` is already inside the busy
        window; on TriggerScope it is only a compatibility boundary.

        For each new run:

        1. Snapshot and suppress Live emission only for scan-owned devices
           (selected gates plus declared ``powerDevice`` entries).
        2. Apply each DIRECT Scan setpoint to the actual power-bearing device.
           AO-owned lasers are deliberately skipped.
        3. Hand each gate over to its existing manager-specific scan mode.
        4. Switch on a declared ``powerDevice`` when the existing pairing
           contract requires serial enabling.

        Non-participating Live lasers are intentionally untouched, matching the
        immediate pre-work ImSwitch2 baseline. The LaserWidget is globally
        read-only during the run, so that preserved Live state cannot be edited
        while acquisition owns its participating devices.

        Direct Scan power is applied once per run, before manager arming, after
        participating Live emission has been suppressed. Repeat frames and later
        parts reuse that prepared state rather than issuing per-frame power writes.

        Arming happens once per scan RUN. Membership is republished for every
        repeat frame, and several managers issue blocking RS232 commands here,
        so re-arming each frame would stall a fast repeat scan. When the
        republish belongs to a later part of a multi-part run (a timelapse
        timepoint), it instead switches back on exactly the power devices
        ``scanPartDone`` paused during the preceding wait — this fires before
        the execution backend claims the hardware, so the writes get through.
        """
        # Defensive fallback for third-party scan sources that publish
        # membership without sigScanStarting. Normal ImSwitch runs reset the
        # handoff state at the run-level start signal.
        if self._scanLiveEnabledSnapshot is None:
            self.scanChanged(True)
        if self._scanPreparationFailed:
            raise RuntimeError(self._scanPreparationFailed)

        if self._scanArmedLasers:
            self._resumeScanLasersForNextPart()
            return

        self._suppressParticipatingLiveForScan(deviceList)
        self._applyDirectScanPowers(deviceList)

        armed = []
        enabled = []
        for lName, _ in self._master.lasersManager:
            if lName not in deviceList:
                continue
            powerDevice = self._powerDeviceFor(lName)
            try:
                self._master.lasersManager[lName].setScanModeActive(True)
                if powerDevice is not None:
                    self._master.lasersManager[powerDevice].setEnabled(True)
            except Exception as e:
                self._logger.error(
                    f'Failed to arm laser "{lName}" for the scan: {e}',
                    exc_info=True,
                )
                continue
            armed.append(lName)
            if powerDevice is not None and powerDevice not in armed:
                armed.append(powerDevice)
            if powerDevice is not None and powerDevice not in enabled:
                enabled.append(powerDevice)

        self._scanArmedLasers = list(armed)
        self._scanEnabledLasers = list(enabled)
        self._scanLasersPaused = False
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
            f'declared power devices switched on at prepared Scan setpoints.'
        )
        if failed:
            self._logger.warning(
                f'Scan requested these lasers but they could not be armed and '
                f'will not emit: {", ".join(failed)}'
            )

    def _scanPartDoneSeparated(self):
        """ One part of a multi-part scan run finished (a timelapse timepoint).

        The run is not over — ``sigScanEnded`` is deliberately withheld for
        non-final parts so the run-level reservation survives — which means
        the armed lasers stay armed straight through the wait until the next
        timepoint. Hardware whose TTL input really gates emission is dark
        anyway (the gate lines park low between parts), but a power device
        switched on over serial would keep emitting into the sample for the
        whole wait.

        So pause exactly the serial emission the arming switched on, and
        nothing else. Pre-scan Live state for scan-owned devices remains
        suppressed for the whole run; non-participating Live lasers remain
        untouched. ``scanDevicesResolved`` switches the paused power devices
        back on when the next part republishes membership.

        This also fires once when a single scan completes; the pause is then
        immediately followed by the full teardown, which switches the same
        lasers off anyway.
        """
        if self._scanLasersPaused or not self._scanEnabledLasers:
            return
        self._scanLasersPaused = True
        for lName in self._scanEnabledLasers:
            try:
                self._master.lasersManager[lName].setEnabled(False)
                self._widget.setLaserActive(lName, False, emitSignal=False)
            except Exception as e:
                self._logger.error(
                    f'Failed to pause laser "{lName}" between scan parts: '
                    f'{e}',
                    exc_info=True,
                )

    def _resumeScanLasersForNextPartSeparated(self):
        """ Switch the lasers ``scanPartDone`` paused back on. """
        if not self._scanLasersPaused:
            return
        self._scanLasersPaused = False
        for lName in self._scanEnabledLasers:
            try:
                self._master.lasersManager[lName].setEnabled(True)
            except Exception as e:
                self._logger.error(
                    f'Failed to switch laser "{lName}" back on for the next '
                    f'scan part: {e}',
                    exc_info=True,
                )

    def _disarmScanLasersSeparated(self):
        """End scan ownership, restore Live setpoints, and keep owned paths dark.

        Only devices actually owned/prepared by this scan are touched.
        Non-participating Live lasers preserve their pre-scan hardware state.
        Manager-specific ``setScanModeActive(False)`` sequencing remains
        authoritative for every armed laser. No path here restores Live ON.
        """
        armed = list(getattr(self, '_scanArmedLasers', ()))
        snapshot = dict(getattr(self, '_scanLiveEnabledSnapshot', None) or {})
        written = list(getattr(self, '_scanPowerWrittenLasers', ()))
        if not armed and not snapshot and not written:
            self._scanLiveEnabledSnapshot = None
            self._scanLiveSuppressed = False
            self._scanPreparationFailed = None
            self._scanWorkflowPreparationError(clear=True)
            return

        restoreTargets = []
        for name in armed + written + list(snapshot):
            if name not in restoreTargets:
                restoreTargets.append(name)

        # First preserve the existing manager-specific scan exit semantics.
        for name in armed:
            try:
                self._master.lasersManager[name].setScanModeActive(False)
            except Exception as e:
                self._logger.error(
                    f'Failed to leave scan mode for laser "{name}": {e}',
                    exc_info=True,
                )

        # Explicitly dark only scan-owned/prepared devices. Non-participating
        # Live lasers are deliberately not called here.
        darkFailures = []
        for name in restoreTargets:
            manager = self._master.lasersManager[name]
            try:
                applied = manager.setEnabled(False)
                if applied is False:
                    raise RuntimeError('manager rejected disable request')
            except Exception as e:
                darkFailures.append(f'{name}: {e}')
                self._logger.error(
                    f'CRITICAL: failed to ensure scan-owned laser "{name}" '
                    f'is OFF after the scan: {e}',
                    exc_info=True,
                )
                continue
            self._widget.setLaserActive(name, False, emitSignal=False)
            self.setSharedAttr(name, _enabledAttr, False)

        # Restore the current Live setpoints only after scan-owned emission has
        # been forced OFF. Explicit zero is meaningful and must not be skipped.
        restored = {}
        restoreFailures = []
        for name in restoreTargets:
            manager = self._master.lasersManager[name]
            if manager.isBinary:
                continue
            try:
                setpoint = self._livePowerValue(name)
                if setpoint is not None:
                    applied = manager.setValue(setpoint)
                    if applied is False:
                        raise RuntimeError('manager rejected Live setpoint restore')
                    restored[name] = setpoint
            except Exception as e:
                restoreFailures.append(f'{name}: {e}')
                self._logger.error(
                    f'CRITICAL: failed to restore Live setpoint for laser '
                    f'"{name}": {e}',
                    exc_info=True,
                )

        # Resume is safe only if every scan-owned path is known dark AND every
        # non-binary Live setpoint was restored. Otherwise re-enabling could
        # expose the sample at a stale Scan setpoint.
        resumeSafe = not darkFailures and not restoreFailures
        self._setResumableLiveEnabledSnapshot(
            snapshot if resumeSafe else None
        )

        if darkFailures or restoreFailures:
            reasons = []
            if darkFailures:
                reasons.append('dark confirmation failed: ' + '; '.join(darkFailures))
            if restoreFailures:
                reasons.append('Live setpoint restore failed: ' + '; '.join(restoreFailures))
            self._logger.error(
                '[LaserScan] Resume state discarded because teardown was not '
                'fully safe: %s',
                ' | '.join(reasons),
            )
        elif armed or snapshot or written:
            self._logger.debug(
                '[LaserScan] teardown complete; Live setpoints restored: %s; '
                'scan-owned lasers remain OFF; non-participating Live lasers '
                'were untouched',
                restored,
            )

        self._scanArmedLasers = []
        self._scanEnabledLasers = []
        self._scanLasersPaused = False
        self._scanPowerWrittenLasers = []
        self._scanLiveEnabledSnapshot = None
        self._scanLiveSuppressed = False
        self._scanPreparationFailed = None
        self._scanWorkflowPreparationError(clear=True)

    def resumeLiveIllumination(self):
        """Explicitly restore the scan-owned Live ON state from the last run.

        The pending snapshot is consumed before any enable attempt so Resume is
        a one-shot action, not an undo/history stack. Re-enabling goes through
        the existing public Live path; manager rejection therefore keeps that
        laser OFF exactly as for a deliberate user ON action.
        """
        if not self._isSeparatedPowerMode():
            return False
        self._requireLaserEditsAllowed()
        snapshot = dict(getattr(self, '_resumableLiveEnabledSnapshot', None) or {})
        if not snapshot or not any(snapshot.values()):
            self._setResumableLiveEnabledSnapshot(None)
            return False

        # Consume first. ``setLaserActive`` eventually reaches ``toggleLaser``,
        # whose ordinary Live action invalidates any pending snapshot.
        self._setResumableLiveEnabledSnapshot(None)
        knownLasers = {name for name, _ in self._master.lasersManager}
        resumed = []
        failures = []

        for name, wasEnabled in snapshot.items():
            if not wasEnabled:
                continue
            if name not in knownLasers:
                self._logger.warning(
                    f'Cannot Resume Live laser "{name}": device is no longer configured.'
                )
                continue
            try:
                self.setLaserActive(name, True)
                if not self._widget.isLaserActive(name):
                    raise RuntimeError('manager rejected Live enable request')
                resumed.append(name)
            except Exception as e:
                failures.append(f'{name}: {e}')
                try:
                    self._widget.setLaserActive(name, False, emitSignal=False)
                    self.setSharedAttr(name, _enabledAttr, False)
                except Exception:
                    pass
                self._logger.error(
                    f'Failed to Resume Live illumination for laser "{name}": {e}',
                    exc_info=True,
                )

        # Resume restores Live illumination, so leave the user looking at the
        # state that now physically matters even if the button was clicked
        # while the Scan context was displayed.
        self.powerContextChanged(LaserPowerContext.LIVE)

        if failures:
            self._logger.error(
                '[LaserScan] Resume Live completed with failures: %s',
                '; '.join(failures),
            )
        elif resumed:
            self._logger.debug('[LaserScan] resumed Live illumination: %s', resumed)
        return bool(resumed) and not failures

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

    def _emitLaserPowerChanged(self, laserName, context, value):
        try:
            self.sigLaserPowerChanged.emit(
                laserName, normalizeLaserPowerContext(context).value, value
            )
        except (AttributeError, RuntimeError):
            # Some lightweight tests construct controllers without initializing
            # the QObject base. State updates must still work there.
            pass

    def _emitLaserPresetListChanged(self, context):
        try:
            self.sigLaserPresetListChanged.emit(
                normalizeLaserPowerContext(context).value
            )
        except (AttributeError, RuntimeError):
            pass

    def _emitLaserPresetLoaded(self, context, name):
        try:
            self.sigLaserPresetLoaded.emit(
                normalizeLaserPowerContext(context).value, name
            )
        except (AttributeError, RuntimeError):
            pass

    def _setLaserValue(self, laserName, value):
        self._requireLaserEditsAllowed()
        self._master.lasersManager[laserName].setValue(value)
        if not hasattr(self, '_livePowerValues'):
            self._livePowerValues = {}
        self._livePowerValues[laserName] = value
        if getattr(self, '_currentPowerContext', LaserPowerContext.LIVE) is LaserPowerContext.LIVE:
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
        self._emitLaserPowerChanged(laserName, LaserPowerContext.LIVE, value)
        # In Scan view an unset Scan value follows Live dynamically. Refresh
        # the primary editor only while that fallback is active; once Scan has
        # an explicit override, Live changes must not replace the Scan value.
        scanUsesLiveFallback = laserName not in getattr(self, '_scanPowerValues', {})
        updatePrimary = (
            getattr(self, '_currentPowerContext', LaserPowerContext.LIVE)
            is LaserPowerContext.SCAN
            and scanUsesLiveFallback
        )
        self._refreshLaserRow(laserName, updatePrimary=updatePrimary)

    def _setScanPowerValue(self, laserName, value):
        """Store an explicit Scan power without touching hardware or the UI."""
        self._requireLaserEditsAllowed()
        manager = self._master.lasersManager[laserName]
        if manager.isBinary:
            raise ValueError(
                f'Binary laser "{laserName}" does not have a power setpoint.'
            )
        if not hasattr(self, '_scanPowerValues'):
            self._scanPowerValues = {}
        self._scanPowerValues[laserName] = value
        if getattr(self, '_currentPowerContext', LaserPowerContext.LIVE) is LaserPowerContext.SCAN:
            self._widget.setValue(laserName, value, emitSignal=False)
        self._emitLaserPowerChanged(laserName, LaserPowerContext.SCAN, value)
        self._refreshLaserRow(laserName, updatePrimary=False)

    @APIExport()
    def getLaserNames(self) -> List[str]:
        """ Returns the device names of all lasers. These device names can be
        passed to other laser-related functions. """
        return self._master.lasersManager.getAllDeviceNames()

    @APIExport(runOnUIThread=True)
    def getLaserActive(self, laserName: str) -> bool:
        """Return the current Live ON/OFF state for ``laserName``."""
        return bool(self._widget.isLaserActive(laserName))

    @APIExport(runOnUIThread=True)
    def setLaserActive(self, laserName: str, active: bool) -> None:
        """ Sets whether the specified laser is powered on. """
        self._requireLaserEditsAllowed()
        self._widget.setLaserActive(laserName, active)

    @APIExport(runOnUIThread=True)
    def setLaserValue(self, laserName: str, value: Union[int, float]) -> None:
        """Set the live value of a laser (legacy API)."""
        self._setLaserValue(laserName, value)

    @APIExport(runOnUIThread=True)
    def getLaserPowerSetting(
        self, laserName: str, context=LaserPowerContext.LIVE.value
    ) -> Union[int, float]:
        """Return the configured power setting for one Live/Scan context.

        This API deliberately describes configuration rather than acquisition
        behavior. An unset Scan setting follows the current Live setting
        dynamically, even while coupled laser power mode keeps that Scan
        setting dormant. Use :meth:`getLaserAcquisitionState` to ask what the
        current acquisition would actually use.
        """
        context = normalizeLaserPowerContext(context)
        manager = self._master.lasersManager[laserName]
        if manager.isBinary:
            raise ValueError(
                f'Binary laser "{laserName}" does not have a power setpoint.'
            )
        if context is LaserPowerContext.LIVE:
            return self._livePowerValue(laserName)
        scanValues = getattr(self, '_scanPowerValues', {})
        if laserName in scanValues:
            return scanValues[laserName]
        return self._livePowerValue(laserName)

    @APIExport(runOnUIThread=True)
    def setLaserPowerSetting(
        self,
        laserName: str,
        value: Union[int, float],
        context=LaserPowerContext.LIVE.value,
    ) -> None:
        """Set one configured Live/Scan power setting.

        Live remains hardware-backed. Scan remains software-only until a
        separated acquisition applies it; in coupled mode it can therefore be
        configured as dormant future separated-mode state without affecting
        current hardware.
        """
        context = normalizeLaserPowerContext(context)
        if context is LaserPowerContext.LIVE:
            self._setLaserValue(laserName, value)
        else:
            self._setScanPowerValue(laserName, value)

    @APIExport(runOnUIThread=True)
    def getLaserPowerSettings(self, context=LaserPowerContext.LIVE.value) -> Dict[str, Any]:
        """Return configured non-binary laser power settings for one context."""
        context = normalizeLaserPowerContext(context)
        return {
            laserName: self.getLaserPowerSetting(laserName, context)
            for laserName, manager in self._master.lasersManager
            if not manager.isBinary
        }

    @APIExport(runOnUIThread=True)
    def setLaserPowerSettings(
        self, values: Dict[str, Union[int, float]],
        context=LaserPowerContext.LIVE.value,
    ) -> None:
        """Set multiple configured laser power settings in one context."""
        context = normalizeLaserPowerContext(context)
        for laserName, value in values.items():
            self.setLaserPowerSetting(laserName, value, context)

    @APIExport(runOnUIThread=True)
    def changeScanPower(self, laserName: str, laserValue: Union[int, float]) -> None:
        """Legacy alias of :meth:`setLaserValue` with Live semantics.

        Despite the historical name, changing this method to software-only Scan
        state would silently break existing scripts, so the permanent Scan
        configuration API is :meth:`setLaserPowerSetting` with
        ``context='scan'``.
        """
        self._setLaserValue(laserName, laserValue)

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
    
    # Setup-mode-specific Laser capture/apply contract (v2)

    laserSetupModeStateVersion = 2

    def getSetupModeCaptureOptions(self, previousState: dict | None = None):
        """Describe Laser-specific capture choices for the Save dialog."""
        return self._buildSetupModeCaptureOptions(previousState, forUpdate=False)

    def getSetupModeUpdateCaptureOptions(self, previousState: dict | None = None):
        """Describe Laser capture choices for Update using current power mode."""
        return self._buildSetupModeCaptureOptions(previousState, forUpdate=True)

    def _buildSetupModeCaptureOptions(self, previousState, *, forUpdate):
        previous = (
            self._normalizeSetupModeState(previousState, warnAmbiguous=False)[0]
            if previousState else None
        )
        currentMode = normalizeLaserPowerMode(
            getattr(self, '_powerMode', LaserPowerMode.COUPLED)
        )
        previousMode = (
            normalizeLaserPowerMode(previous.get('capturedPowerMode'))
            if previous else None
        )

        def binding_defaults(section, context, *, allPresets=False):
            binding = (section or {}).get('powerBinding') or {}
            kind = (
                binding.get('kind')
                if binding.get('kind') in ('snapshot', 'preset')
                else 'snapshot'
            )
            presetName = binding.get('presetName') if kind == 'preset' else None
            fallbackPowers = (
                deepcopy(binding.get('fallbackPowers') or {})
                if kind == 'preset' else {}
            )
            if allPresets:
                names = list(getattr(self._setupInfo, 'laserPresets', {}).keys())
            else:
                names = list(self._visiblePresetNames(context))
            if presetName and presetName not in names:
                names.append(presetName)
            return kind, presetName, names, fallbackPowers

        if currentMode is LaserPowerMode.COUPLED:
            sourceSection = None
            if previousMode is LaserPowerMode.COUPLED:
                sourceSection = (previous or {}).get('coupled')
            elif previousMode is LaserPowerMode.SEPARATED:
                scan = (previous or {}).get('scan') or {}
                if scan.get('included'):
                    sourceSection = scan
            kind, presetName, names, fallbackPowers = binding_defaults(
                sourceSection, LaserPowerContext.LIVE, allPresets=True
            )
            options = {
                'kind': 'power_bindings',
                'powerMode': 'coupled',
                'sections': [{
                    'id': 'coupled',
                    'label': None,
                    'checkable': False,
                    'included': True,
                    'bindingKind': kind,
                    'presetName': presetName,
                    'presetNames': names,
                    'fallbackPowers': fallbackPowers,
                }],
            }
            if forUpdate and previousMode is LaserPowerMode.SEPARATED:
                options['notice'] = (
                    'Laser was saved in separated mode; current setup is coupled. '
                    'Updating Laser will replace the saved Live/Scan state with '
                    'current coupled semantics.'
                )
            return options

        liveSource = None
        scanSource = None
        liveSelected = True
        scanSelected = True
        if previousMode is LaserPowerMode.SEPARATED:
            liveSource = (previous or {}).get('live') or {}
            scanSource = (previous or {}).get('scan') or {}
            liveSelected = bool(liveSource.get('included'))
            scanSelected = bool(scanSource.get('included'))
        elif previousMode is LaserPowerMode.COUPLED:
            # Cross-mode recapture is Scan-biased: the old coupled binding is
            # the closest semantic match for the new Scan subsection.
            scanSource = (previous or {}).get('coupled') or {}

        liveKind, livePreset, liveNames, liveFallback = binding_defaults(
            liveSource, LaserPowerContext.LIVE
        )
        scanKind, scanPreset, scanNames, scanFallback = binding_defaults(
            scanSource, LaserPowerContext.SCAN
        )
        options = {
            'kind': 'power_bindings',
            'powerMode': 'separated',
            'sections': [
                {
                    'id': 'live',
                    'label': 'Live',
                    'checkable': True,
                    'included': True if forUpdate else liveSelected,
                    'updateSelected': liveSelected if previousMode is LaserPowerMode.SEPARATED else True,
                    'bindingKind': liveKind,
                    'presetName': livePreset,
                    'presetNames': liveNames,
                    'fallbackPowers': liveFallback,
                },
                {
                    'id': 'scan',
                    'label': 'Scan',
                    'checkable': True,
                    'included': True if forUpdate else scanSelected,
                    'updateSelected': scanSelected if previousMode is LaserPowerMode.SEPARATED else True,
                    'bindingKind': scanKind,
                    'presetName': scanPreset,
                    'presetNames': scanNames,
                    'fallbackPowers': scanFallback,
                },
            ],
        }
        if forUpdate and previousMode is LaserPowerMode.COUPLED:
            options['notice'] = (
                'Laser was saved in coupled mode; current setup is separated. '
                'Updating Laser will replace it with separated Live/Scan state.'
            )
        return options

    def prepareSetupModeState(
        self, fullState: dict, *, captureOptions: dict | None = None,
        previousState: dict | None = None,
    ) -> dict:
        """Convert one canonical Laser snapshot into the setup-mode v2 payload."""
        if captureOptions is None:
            captureOptions = self.getSetupModeCaptureOptions(previousState)
        captureOptions = captureOptions or {}
        powerMode = normalizeLaserPowerMode(
            captureOptions.get(
                'powerMode', getattr(self, '_powerMode', LaserPowerMode.COUPLED)
            )
        )
        sectionOptions = {
            section.get('id'): section
            for section in captureOptions.get('sections', [])
            if isinstance(section, dict) and section.get('id')
        }

        lasers = deepcopy(fullState.get('lasers') or {})
        modulation = deepcopy(fullState.get('modulation') or {})
        laserOrder = list(fullState.get('laserOrder') or lasers.keys())
        livePowers = {
            name: state.get('value')
            for name, state in lasers.items()
            if isinstance(state, dict)
            and not state.get('isBinary')
            and state.get('value') is not None
        }
        scanPowers = self._capturedEffectiveScanPowers(fullState, livePowers)

        payload = {
            'laserSetupModeStateVersion': self.laserSetupModeStateVersion,
            'capturedPowerMode': powerMode.value,
            'laserOrder': laserOrder,
        }

        if powerMode is LaserPowerMode.COUPLED:
            options = sectionOptions.get('coupled') or {}
            payload['coupled'] = {
                'powerBinding': self._makeSetupModePowerBinding(
                    options, livePowers, LaserPowerContext.LIVE
                ),
                'lasers': lasers,
                'modulation': modulation,
            }
            return payload

        liveOptions = sectionOptions.get('live') or {
            'included': True, 'bindingKind': 'snapshot'
        }
        scanOptions = sectionOptions.get('scan') or {
            'included': True, 'bindingKind': 'snapshot'
        }

        payload['live'] = {'included': bool(liveOptions.get('included', True))}
        if payload['live']['included']:
            payload['live'].update({
                'powerBinding': self._makeSetupModePowerBinding(
                    liveOptions, livePowers, LaserPowerContext.LIVE
                ),
                'lasers': lasers,
                'modulation': modulation,
            })

        payload['scan'] = {'included': bool(scanOptions.get('included', True))}
        if payload['scan']['included']:
            payload['scan']['powerBinding'] = self._makeSetupModePowerBinding(
                scanOptions, scanPowers, LaserPowerContext.SCAN
            )
        return payload

    @staticmethod
    def _capturedEffectiveScanPowers(fullState, livePowers):
        """Resolve effective Scan powers using only the captured runtime state."""
        overrides = dict((fullState or {}).get('scanPowers') or {})
        powers = {}
        for laserName, liveValue in dict(livePowers or {}).items():
            if laserName in overrides and overrides[laserName] is not None:
                powers[laserName] = overrides[laserName]
            else:
                powers[laserName] = liveValue
        return powers

    def _makeSetupModePowerBinding(self, options, currentPowers, context):
        kind = str((options or {}).get('bindingKind') or 'snapshot').lower()
        currentPowers = dict(currentPowers or {})
        if kind != 'preset':
            return {'kind': 'snapshot', 'powers': currentPowers}

        presetName = (options or {}).get('presetName')
        if not presetName:
            # UI validation should prevent this, but stay deterministic for API users.
            return {'kind': 'snapshot', 'powers': currentPowers}

        presetContext, preset = self._presetDefinition(presetName)
        if preset is None and (options or {}).get('fallbackPowers'):
            # A missing followed preset keeps its already-saved fallback during
            # Update instead of silently replacing it with unrelated current
            # hardware values.
            fallback = dict((options or {}).get('fallbackPowers') or {})
        else:
            fallback = dict(currentPowers)
            if preset is not None:
                for laserName, info in preset.items():
                    value = info.value if hasattr(info, 'value') else None
                    if value is not None:
                        fallback[laserName] = value
        return {
            'kind': 'preset',
            'presetName': str(presetName),
            'presetContextAtCapture': (
                presetContext.value if isinstance(presetContext, LaserPowerContext)
                else normalizeLaserPowerContext(context).value
            ),
            'fallbackPowers': fallback,
        }

    def _resolveSetupModePowerBinding(self, binding):
        binding = binding or {}
        kind = binding.get('kind', 'snapshot')
        if kind != 'preset':
            return dict(binding.get('powers') or {}), []

        presetName = binding.get('presetName')
        fallback = dict(binding.get('fallbackPowers') or {})
        _presetContext, preset = self._presetDefinition(presetName) if presetName else (None, None)
        if preset is None:
            return fallback, [
                f'Laser preset "{presetName}" is unavailable; using saved fallback powers.'
            ]

        resolved = dict(fallback)
        missing = []
        presetValues = {}
        for laserName, info in preset.items():
            value = info.value if hasattr(info, 'value') else None
            if value is not None:
                presetValues[laserName] = value
                resolved[laserName] = value
        for laserName in fallback:
            if laserName not in presetValues:
                missing.append(laserName)
        warnings = []
        if missing:
            warnings.append(
                f'Laser preset "{presetName}" has no value for {", ".join(missing)}; '
                'using saved fallback powers for those lasers.'
            )
        return resolved, warnings

    def _normalizeSetupModeState(self, state, *, warnAmbiguous=True):
        """Normalize v2 and historical Laser setup-mode payloads."""
        state = state or {}
        if state.get('laserSetupModeStateVersion') == self.laserSetupModeStateVersion:
            return deepcopy(state), []

        warnings = []
        lasers = deepcopy(state.get('lasers') or {})
        modulation = deepcopy(state.get('modulation') or {})
        livePowers = {
            name: item.get('value')
            for name, item in lasers.items()
            if isinstance(item, dict) and not item.get('isBinary') and item.get('value') is not None
        }
        scanPowers = dict(state.get('scanPowers') or {})
        scanPreset = state.get('scanDefaultPreset') or state.get('scanPreset')
        meaningfulScan = bool(scanPowers) or bool(scanPreset)

        if meaningfulScan:
            normalized = {
                'laserSetupModeStateVersion': self.laserSetupModeStateVersion,
                'capturedPowerMode': 'separated',
                'laserOrder': list(state.get('laserOrder') or lasers.keys()),
                'live': {
                    'included': True,
                    'powerBinding': {'kind': 'snapshot', 'powers': livePowers},
                    'lasers': lasers,
                    'modulation': modulation,
                },
                'scan': {'included': True},
            }
            if scanPreset:
                fallback = dict(livePowers)
                fallback.update(scanPowers)
                normalized['scan']['powerBinding'] = {
                    'kind': 'preset',
                    'presetName': scanPreset,
                    'presetContextAtCapture': 'scan',
                    'fallbackPowers': fallback,
                }
            else:
                effective = dict(livePowers)
                effective.update(scanPowers)
                normalized['scan']['powerBinding'] = {
                    'kind': 'snapshot', 'powers': effective
                }
            return normalized, warnings

        if warnAmbiguous and any(
            key in state for key in ('livePreset', 'scanPreset', 'scanPowers')
        ):
            warnings.append(
                'Historical Laser setup-mode state did not record its power policy; '
                'it was interpreted as coupled because no explicit Scan state was saved.'
            )
        normalized = {
            'laserSetupModeStateVersion': self.laserSetupModeStateVersion,
            'capturedPowerMode': 'coupled',
            'laserOrder': list(state.get('laserOrder') or lasers.keys()),
            'coupled': {
                'powerBinding': {'kind': 'snapshot', 'powers': livePowers},
                'lasers': lasers,
                'modulation': modulation,
            },
        }
        return normalized, warnings

    def _setupModeApplyPlan(self, state):
        """Compute cross-mode semantics before any hardware is touched."""
        normalized, warnings = self._normalizeSetupModeState(state)
        captured = normalizeLaserPowerMode(normalized.get('capturedPowerMode'))
        current = normalizeLaserPowerMode(getattr(self, '_powerMode', LaserPowerMode.COUPLED))
        plan = {
            'state': normalized,
            'captured': captured,
            'current': current,
            'applyLive': False,
            'applyScan': False,
            'applyCoupled': False,
            'coupledMustRemainOff': False,
            'warnings': list(warnings),
        }

        if captured is current:
            if current is LaserPowerMode.COUPLED:
                plan['applyCoupled'] = True
            else:
                plan['applyLive'] = bool((normalized.get('live') or {}).get('included'))
                plan['applyScan'] = bool((normalized.get('scan') or {}).get('included'))
            return plan

        if captured is LaserPowerMode.COUPLED and current is LaserPowerMode.SEPARATED:
            plan['applyScan'] = True
            plan['warnings'].append(
                'Laser setup mode was saved with coupled power; its power was applied '
                'to Scan only and current Live illumination was left unchanged.'
            )
            return plan

        # Separated saved -> coupled current: Scan wins; Live is never promoted.
        scanIncluded = bool((normalized.get('scan') or {}).get('included'))
        liveIncluded = bool((normalized.get('live') or {}).get('included'))
        if scanIncluded:
            plan['applyCoupled'] = True
            plan['coupledMustRemainOff'] = True
            if liveIncluded:
                plan['warnings'].append(
                    'Laser setup mode was saved with separated power. Scan power was '
                    'applied to the coupled setpoint while lasers were kept OFF; '
                    'saved Live state was not applied.'
                )
            else:
                plan['warnings'].append(
                    'Separated Scan power was applied to the coupled setpoint while '
                    'lasers were kept OFF.'
                )
        else:
            plan['warnings'].append(
                'This setup mode contains only separated Live laser state, but the '
                'current setup uses coupled laser power. Laser state was not applied.'
            )
        return plan

    def _applySetupModeLiveSection(self, section, powers, *, forceOff=False):
        warnings = []
        knownLasers = {name for name, _ in self._master.lasersManager}
        lasers = section.get('lasers') or {}
        modulationByLaser = section.get('modulation') or {}

        # Include power-only entries (for example Scan -> coupled conversion)
        # even when the saved section intentionally has no Live metadata.
        names = list(lasers.keys())
        for name in powers:
            if name not in names:
                names.append(name)

        for laserName in names:
            if laserName not in knownLasers:
                warnings.append(f'Laser "{laserName}" not present in current setup; skipped.')
                continue
            manager = self._master.lasersManager[laserName]
            laserState = lasers.get(laserName) or {}
            savedBinary = bool(laserState.get('isBinary', manager.isBinary))
            targetEnabled = bool(laserState.get('enabled', False)) and not forceOff
            value = powers.get(laserName)
            if targetEnabled and not savedBinary:
                numericValue = self._asFloat(value)
                if numericValue is not None and numericValue <= 0:
                    targetEnabled = False

            restoreFailed = False
            try:
                self.setLaserActive(laserName, False)
            except Exception as e:
                warnings.append(f'Failed to disable laser "{laserName}" before restore: {e}')
                continue

            if not savedBinary and value is not None:
                try:
                    self.setLaserValue(laserName, value)
                except Exception as e:
                    restoreFailed = True
                    warnings.append(f'Failed to restore value for laser "{laserName}": {e}')

            modulation = modulationByLaser.get(laserName) or {}
            if modulation and not forceOff:
                module = self._widget.laserModules.get(laserName)
                if not self._laserModuleSupportsModulation(module):
                    restoreFailed = True
                    warnings.append(
                        f'Laser "{laserName}" has no modulation controls; modulation state was not applied.'
                    )
                else:
                    freq = modulation.get('frequency')
                    if freq is not None:
                        try:
                            self.frequencyChanged(laserName, freq)
                        except Exception as e:
                            restoreFailed = True
                            warnings.append(
                                f'Failed to restore modulation frequency for laser "{laserName}": {e}'
                            )
                    duty = modulation.get('dutyCycle')
                    if duty is not None:
                        try:
                            self.dutyCycleChanged(laserName, duty)
                        except Exception as e:
                            restoreFailed = True
                            warnings.append(
                                f'Failed to restore modulation duty cycle for laser "{laserName}": {e}'
                            )
                    if 'enabled' in modulation:
                        try:
                            enabled = bool(modulation.get('enabled'))
                            self.toggleModulation(laserName, enabled)
                            self._widget.setModulationEnabled(
                                laserName, enabled, emitSignal=False
                            )
                        except Exception as e:
                            restoreFailed = True
                            warnings.append(
                                f'Failed to restore modulation enable state for laser "{laserName}": {e}'
                            )

            if targetEnabled and not restoreFailed:
                try:
                    self.setLaserActive(laserName, True)
                except Exception as e:
                    warnings.append(f'Failed to enable laser "{laserName}" after restore: {e}')
        return warnings

    def _applySetupModeScanPowers(self, powers):
        warnings = []
        knownLasers = {name for name, _ in self._master.lasersManager}
        for laserName, value in dict(powers or {}).items():
            if laserName not in knownLasers:
                warnings.append(
                    f'Scan power references laser "{laserName}" not present in current setup; skipped.'
                )
                continue
            if self._master.lasersManager[laserName].isBinary:
                warnings.append(f'Scan power references binary laser "{laserName}"; skipped.')
                continue
            try:
                self._setScanPowerValue(laserName, value)
            except Exception as e:
                warnings.append(f'Failed to restore Scan power for laser "{laserName}": {e}')
        return warnings

    def _applySetupModeStateV2(self, state):
        plan = self._setupModeApplyPlan(state)
        normalized = plan['state']
        warnings = list(plan['warnings'])
        captured = plan['captured']

        if plan['applyLive']:
            section = normalized.get('live') or {}
            powers, bindingWarnings = self._resolveSetupModePowerBinding(
                section.get('powerBinding')
            )
            warnings.extend(bindingWarnings)
            warnings.extend(self._applySetupModeLiveSection(section, powers))
            self._setSetupModePresetSelection(LaserPowerContext.LIVE, section.get('powerBinding'))

        if plan['applyScan']:
            section = (
                normalized.get('scan') or {}
                if captured is LaserPowerMode.SEPARATED
                else normalized.get('coupled') or {}
            )
            powers, bindingWarnings = self._resolveSetupModePowerBinding(
                section.get('powerBinding')
            )
            warnings.extend(bindingWarnings)
            warnings.extend(self._applySetupModeScanPowers(powers))
            self._setSetupModePresetSelection(LaserPowerContext.SCAN, section.get('powerBinding'))

        if plan['applyCoupled']:
            if captured is LaserPowerMode.COUPLED:
                section = normalized.get('coupled') or {}
            else:
                section = normalized.get('scan') or {}
            powers, bindingWarnings = self._resolveSetupModePowerBinding(
                section.get('powerBinding')
            )
            warnings.extend(bindingWarnings)
            if plan['coupledMustRemainOff']:
                warnings.extend(self._applySetupModeLiveSection({}, powers, forceOff=True))
            else:
                warnings.extend(self._applySetupModeLiveSection(section, powers))
            self._setSetupModePresetSelection(LaserPowerContext.LIVE, section.get('powerBinding'))

        self._refreshPresetControls()
        self._refreshAllLaserRows()
        return warnings

    def _setSetupModePresetSelection(self, context, binding):
        binding = binding or {}
        presetName = binding.get('presetName') if binding.get('kind') == 'preset' else None
        if self._isSeparatedPowerMode():
            self._setSelectedPresetName(context, presetName)
        else:
            self._selectedCoupledPreset = presetName

    def _setupModeHazardState(self, state):
        """Return (laser metadata, resolved powers) only when apply can emit."""
        plan = self._setupModeApplyPlan(state)
        normalized = plan['state']
        if plan['current'] is LaserPowerMode.COUPLED:
            if not plan['applyCoupled'] or plan['coupledMustRemainOff']:
                return {}, {}, []
            section = normalized.get('coupled') or {}
        else:
            if not plan['applyLive']:
                return {}, {}, []
            section = normalized.get('live') or {}
        powers, warnings = self._resolveSetupModePowerBinding(section.get('powerBinding'))
        return section.get('lasers') or {}, powers, warnings

    def _setupModePresetReferences(self, presetName):
        backend = getattr(self, '_setupModeController', None)
        if backend is None:
            return []
        references = []
        for record in backend.getSetupModeComponentStates(self.componentName):
            if self._stateReferencesPreset(record.get('state'), presetName):
                references.append(record.get('modeName'))
        return references

    def _stateReferencesPreset(self, state, presetName):
        state = state or {}
        if state.get('laserSetupModeStateVersion') == self.laserSetupModeStateVersion:
            for sectionName in ('coupled', 'live', 'scan'):
                binding = (state.get(sectionName) or {}).get('powerBinding') or {}
                if binding.get('kind') == 'preset' and binding.get('presetName') == presetName:
                    return True
            return False
        return any(
            state.get(key) == presetName
            for key in ('scanDefaultPreset', 'currentPreset', 'livePreset', 'scanPreset')
        )

    def _rewriteSetupModePresetReferences(self, oldName, newName):
        backend = getattr(self, '_setupModeController', None)
        if backend is None:
            return []
        updated = []
        for record in backend.getSetupModeComponentStates(self.componentName):
            modeName = record.get('modeName')
            state = deepcopy(record.get('state') or {})
            changed = False
            if state.get('laserSetupModeStateVersion') == self.laserSetupModeStateVersion:
                for sectionName in ('coupled', 'live', 'scan'):
                    binding = (state.get(sectionName) or {}).get('powerBinding') or {}
                    if binding.get('kind') == 'preset' and binding.get('presetName') == oldName:
                        binding['presetName'] = newName
                        changed = True
            else:
                for key in ('scanDefaultPreset', 'currentPreset', 'livePreset', 'scanPreset'):
                    if state.get(key) == oldName:
                        state[key] = newName
                        changed = True
            if changed:
                backend.replaceSetupModeComponentState(modeName, self.componentName, state)
                updated.append(modeName)
        return updated

    # Unified State Persistence Interface (StatefulComponentMixin)
    
    def getComponentState(self) -> dict:
        """Snapshot current laser state for both startup and setup modes.
        
        Returns a JSON-serializable dict with structure expected by the
        existing SetupModesController summarizer (lifted in describeComponentState).
        
        ``currentPreset`` remains as the legacy alias for the Live preset.
        New state also records ``livePreset``, ``scanPreset`` and only the
        explicit ``scanPowers`` overrides. Missing Scan values continue to
        follow Live dynamically.
        """
        livePreset = getattr(self, '_selectedPresetByContext', {}).get(
            LaserPowerContext.LIVE
        )
        scanPreset = getattr(self, '_selectedPresetByContext', {}).get(
            LaserPowerContext.SCAN
        )
        coupledPreset = getattr(self, '_selectedCoupledPreset', None)
        state = {
            'lasers': {},
            'laserOrder': [],
            # Historical currentPreset is retained as the coupled/single-view
            # selection. Live and Scan keep independent selections so changing
            # the configured laser power mode does not overwrite either side.
            'currentPreset': coupledPreset,
            'livePreset': livePreset,
            'scanPreset': scanPreset,
            'scanPowers': dict(getattr(self, '_scanPowerValues', {})),
            'modulation': {}
        }
        
        for lName, lManager in self._master.lasersManager:
            state['laserOrder'].append(lName)
            state['lasers'][lName] = {
                'enabled': self._widget.isLaserActive(lName),
                'value': self.getLaserPowerSetting(lName, LaserPowerContext.LIVE) if not lManager.isBinary else None,
                'isBinary': lManager.isBinary,
                'valueUnits': lManager.valueUnits
            }
            
            module = self._widget.laserModules.get(lName)
            if self._laserModuleSupportsModulation(module):
                state['modulation'][lName] = {
                    'enabled': self._laserModuleModulationEnabled(module),
                    'frequency': module.getFrequency(),
                    'dutyCycle': module.getDutyCycle(),
                }
        
        return state
    
    def applyComponentState(self, state: dict, *, applyMode: ComponentStateApplyMode) -> list[str]:
        """Restore laser state from a snapshot.

        Power and modulation values are restored in both persistence modes.
        Setup-mode application additionally restores ON/OFF state, with OFF
        targets made dark before any power write and ON targets enabled only
        after their power/modulation state has been restored. Startup restore
        never enables lasers.
        """
        self._requireLaserEditsAllowed()
        if applyMode == ComponentStateApplyMode.SETUP_MODE_APPLY:
            return self._applySetupModeStateV2(state)

        warnings = []
        known_lasers = {name for name, _ in self._master.lasersManager}
        lasers = state.get('lasers', {})

        # Scan state is software-only. Treat a missing key from an old saved
        # state as "no explicit Scan overrides", i.e. fallback to Live.
        restoredScanPowers = {}
        for lName, value in (state.get('scanPowers') or {}).items():
            if lName not in known_lasers:
                warnings.append(
                    f'Scan power references laser "{lName}" not present in current setup; skipped.'
                )
                continue
            if self._master.lasersManager[lName].isBinary:
                warnings.append(
                    f'Scan power references binary laser "{lName}"; skipped.'
                )
                continue
            # Zero is an explicit Scan override and must not be treated as absent.
            if value is not None:
                restoredScanPowers[lName] = value
        self._scanPowerValues = restoredScanPowers

        for lName, laserState in lasers.items():
            if lName not in known_lasers:
                warnings.append(f'Laser "{lName}" not present in current setup; skipped.')
                continue

            lManager = self._master.lasersManager[lName]
            savedBinary = bool(laserState.get('isBinary', lManager.isBinary))
            targetEnabled = bool(laserState.get('enabled', False))
            value = laserState.get('value')

            # A non-binary zero-power target is effectively OFF even if an old
            # snapshot says enabled=True. Keep zero as a real value to restore.
            if targetEnabled and not savedBinary:
                numericValue = self._asFloat(value)
                if numericValue is not None and numericValue <= 0:
                    targetEnabled = False

            restoreFailed = False

            # Setup-mode application always makes the affected laser dark
            # before changing its Live setpoint or modulation state. This makes
            # OFF targets safe and gives ON targets a real "enable last"
            # ordering rather than assuming the laser happened to be off.
            if applyMode == ComponentStateApplyMode.SETUP_MODE_APPLY:
                try:
                    self.setLaserActive(lName, False)
                except Exception as e:
                    warnings.append(f'Failed to disable laser "{lName}" before restore: {e}')
                    # Do not write a new setpoint if darkness could not be
                    # established first.
                    continue

            if not savedBinary and value is not None:
                try:
                    self.setLaserValue(lName, value)
                except Exception as e:
                    restoreFailed = True
                    warnings.append(f'Failed to restore value for laser "{lName}": {e}')

            modulation = state.get('modulation', {}).get(lName, {})
            if modulation:
                module = self._widget.laserModules.get(lName)
                if not self._laserModuleSupportsModulation(module):
                    restoreFailed = True
                    warnings.append(
                        f'Laser "{lName}" has no modulation controls; modulation state was not applied.'
                    )
                else:
                    freq = modulation.get('frequency')
                    if freq is not None:
                        try:
                            self.frequencyChanged(lName, freq)
                        except Exception as e:
                            restoreFailed = True
                            warnings.append(
                                f'Failed to restore modulation frequency for laser "{lName}": {e}'
                            )

                    dc = modulation.get('dutyCycle')
                    if dc is not None:
                        try:
                            self.dutyCycleChanged(lName, dc)
                        except Exception as e:
                            restoreFailed = True
                            warnings.append(
                                f'Failed to restore modulation duty cycle for laser "{lName}": {e}'
                            )

                    if 'enabled' in modulation:
                        try:
                            modEnabled = bool(modulation.get('enabled'))
                            self.toggleModulation(lName, modEnabled)
                            self._widget.setModulationEnabled(
                                lName, modEnabled, emitSignal=False
                            )
                        except Exception as e:
                            restoreFailed = True
                            warnings.append(
                                f'Failed to restore modulation enable state for laser "{lName}": {e}'
                            )

            # ON is the final action and only happens after a complete restore.
            # If a value/modulation step failed, the laser stays fail-closed.
            if (
                applyMode == ComponentStateApplyMode.SETUP_MODE_APPLY
                and targetEnabled
                and not restoreFailed
            ):
                try:
                    self.setLaserActive(lName, True)
                except Exception as e:
                    warnings.append(f'Failed to enable laser "{lName}" after restore: {e}')

        if (
            applyMode == ComponentStateApplyMode.STARTUP_RESTORE
            and any(laserState.get('enabled', False) for laserState in lasers.values())
        ):
            warnings.append('Laser enable states not restored in startup mode.')

        # Restore preset selections without applying their powers. Selection
        # identities are independent so changing the configured laser power mode
        # does not lose the dormant separated selections.
        allPresets = getattr(self._setupInfo, 'laserPresets', {})
        currentPreset = state.get('currentPreset')
        livePreset = state.get('livePreset')
        scanPreset = state.get('scanPreset')

        self._selectedCoupledPreset = (
            currentPreset if currentPreset in allPresets else None
        )
        self._selectedPresetByContext[LaserPowerContext.LIVE] = (
            livePreset
            if livePreset in allPresets
            and self._presetContext(livePreset) is LaserPowerContext.LIVE
            else None
        )
        self._selectedPresetByContext[LaserPowerContext.SCAN] = (
            scanPreset
            if scanPreset in allPresets
            and self._presetContext(scanPreset) in (None, LaserPowerContext.SCAN)
            else None
        )

        # Compatibility with state saved before contextual selections existed.
        # An unspecified historical currentPreset belongs to Scan when the
        # setup now runs separated, matching unspecified-preset visibility.
        if livePreset is None and scanPreset is None and currentPreset in allPresets:
            presetContext = self._presetContext(currentPreset)
            if presetContext is LaserPowerContext.LIVE:
                self._selectedPresetByContext[LaserPowerContext.LIVE] = currentPreset
            elif presetContext in (None, LaserPowerContext.SCAN):
                self._selectedPresetByContext[LaserPowerContext.SCAN] = currentPreset

        self._refreshPresetControls()
        self._refreshAllLaserRows()
        return warnings

    def _describeSetupModeStateV2(self, state):
        summaries = []
        mode = state.get('capturedPowerMode', 'coupled')

        def binding_text(binding):
            binding = binding or {}
            if binding.get('kind') == 'preset':
                return f'Follow preset "{binding.get("presetName")}"'
            return 'Snapshot current powers'

        def append_powers(prefix, binding, laserMeta=None):
            binding = binding or {}
            powers = (
                binding.get('fallbackPowers')
                if binding.get('kind') == 'preset'
                else binding.get('powers')
            ) or {}
            summaries.append(f'{prefix} source: {binding_text(binding)}')
            for laserName in state.get('laserOrder') or powers.keys():
                if laserName not in powers:
                    continue
                units = ''
                if laserMeta and isinstance(laserMeta.get(laserName), dict):
                    units = laserMeta[laserName].get('valueUnits') or ''
                unitText = f' {units}' if units else ''
                summaries.append(
                    f'  {laserName}: {self._fmt(powers[laserName])}{unitText}'
                )

        if mode == 'coupled':
            section = state.get('coupled') or {}
            append_powers('Power', section.get('powerBinding'), section.get('lasers') or {})
            for laserName, laserState in self._savedLaserItemsInDisplayOrder(
                section.get('lasers') or {}, state
            ):
                summaries.append(
                    f'  {laserName}: {self._onOff(laserState.get("enabled"))}'
                )
            return summaries or ['no laser state']

        live = state.get('live') or {}
        scan = state.get('scan') or {}
        if live.get('included'):
            summaries.append('Live:')
            append_powers('  Power', live.get('powerBinding'), live.get('lasers') or {})
            for laserName, laserState in self._savedLaserItemsInDisplayOrder(
                live.get('lasers') or {}, state
            ):
                summaries.append(
                    f'  {laserName}: {self._onOff(laserState.get("enabled"))}'
                )
        else:
            summaries.append('Live: not saved')

        if scan.get('included'):
            summaries.append('Scan:')
            append_powers('  Power', scan.get('powerBinding'))
        else:
            summaries.append('Scan: not saved')
        return summaries or ['no laser state']

    @staticmethod
    def _orderedSetupModeLineDiff(oldLines, newLines):
        oldLines = list(oldLines or [])
        newLines = list(newLines or [])
        removed = [line for line in oldLines if line not in newLines]
        added = [line for line in newLines if line not in oldLines]
        return [*(f'- {line}' for line in removed), *(f'+ {line}' for line in added)]

    def _describeSetupModeSection(self, state, sectionId):
        state = state or {}
        section = state.get(sectionId) or {}
        if not section.get('included'):
            return ['not saved']

        binding = section.get('powerBinding') or {}
        if binding.get('kind') == 'preset':
            lines = [f'Power source: Follow preset "{binding.get("presetName")}"']
            powers = binding.get('fallbackPowers') or {}
        else:
            lines = ['Power source: Snapshot current powers']
            powers = binding.get('powers') or {}

        laserMeta = section.get('lasers') or {}
        order = state.get('laserOrder') or list(powers.keys())
        for laserName in order:
            if laserName not in powers:
                continue
            units = ''
            if isinstance(laserMeta.get(laserName), dict):
                units = laserMeta[laserName].get('valueUnits') or ''
            unitText = f' {units}' if units else ''
            lines.append(
                f'{laserName}: {self._fmt(powers[laserName])}{unitText}'
            )

        if sectionId == 'live':
            for laserName, laserState in self._savedLaserItemsInDisplayOrder(
                laserMeta, state
            ):
                lines.append(
                    f'{laserName}: {self._onOff(laserState.get("enabled"))}'
                )
        return lines

    def describeSetupModeUpdate(self, oldState, candidateState):
        """Expose Live/Scan as Update subsections only for separated mode."""
        oldNormalized = (
            self._normalizeSetupModeState(oldState, warnAmbiguous=False)[0]
            if oldState else None
        )
        candidateNormalized = (
            self._normalizeSetupModeState(candidateState, warnAmbiguous=False)[0]
            if candidateState else None
        )
        candidateMode = (candidateNormalized or {}).get('capturedPowerMode')
        oldMode = (oldNormalized or {}).get('capturedPowerMode')
        notice = None
        if oldMode and candidateMode and oldMode != candidateMode:
            notice = (
                f'Laser was saved in {oldMode} mode; current setup is '
                f'{candidateMode}. Updating Laser will recapture it using '
                f'{candidateMode} semantics.'
            )

        if candidateMode == 'separated':
            sections = []
            for sectionId, label in (('live', 'Live'), ('scan', 'Scan')):
                oldLines = (
                    self._describeSetupModeSection(oldNormalized, sectionId)
                    if oldMode == 'separated' else []
                )
                newLines = self._describeSetupModeSection(
                    candidateNormalized, sectionId
                )
                sections.append({
                    'id': sectionId,
                    'label': label,
                    'changed': oldLines != newLines,
                    'lines': self._orderedSetupModeLineDiff(oldLines, newLines),
                })
            return {
                'changed': oldState != candidateState,
                'lines': [],
                'sections': sections,
                'notice': notice,
            }

        oldLines = self.describeComponentState(oldNormalized or {}) if oldNormalized else []
        newLines = (
            self.describeComponentState(candidateNormalized or {})
            if candidateNormalized else []
        )
        return {
            'changed': oldState != candidateState,
            'lines': self._orderedSetupModeLineDiff(oldLines, newLines),
            'sections': [],
            'notice': notice,
        }

    def mergeSetupModeState(
        self, oldState, candidateState, *, selectedParts=None
    ):
        """Merge separated Live/Scan updates while preserving unchecked JSON."""
        candidate = deepcopy(candidateState)
        if candidate.get('capturedPowerMode') != 'separated':
            return candidate

        selected = set(selectedParts or ())
        if not selected:
            return candidate

        oldIsSeparatedV2 = (
            isinstance(oldState, dict)
            and oldState.get('laserSetupModeStateVersion') == self.laserSetupModeStateVersion
            and oldState.get('capturedPowerMode') == 'separated'
        )
        for sectionId in ('live', 'scan'):
            if sectionId in selected:
                continue
            if oldIsSeparatedV2 and sectionId in oldState:
                candidate[sectionId] = deepcopy(oldState[sectionId])
            else:
                candidate[sectionId] = {'included': False}
        return candidate

    def describeComponentState(self, state: dict) -> list[str]:
        """Generate a human-readable Live/Scan summary of saved laser state."""
        state = state or {}
        if state.get('laserSetupModeStateVersion') == self.laserSetupModeStateVersion:
            return self._describeSetupModeStateV2(state)
        summaries = []

        if state.get('currentPreset') is not None:
            summaries.append(f"  preset: {self._fmt(state.get('currentPreset'))}")
        if state.get('livePreset') is not None:
            summaries.append(f"  Live preset: {self._fmt(state.get('livePreset'))}")
        if state.get('scanPreset') is not None:
            summaries.append(f"  Scan preset: {self._fmt(state.get('scanPreset'))}")

        lasers = state.get('lasers') or {}
        if lasers:
            summaries.append('  Live:')

        orderedLasers = self._savedLaserItemsInDisplayOrder(lasers, state)
        for laserName, laserState in orderedLasers:
            enabled = self._onOff(laserState.get('enabled'))
            if laserState.get('isBinary'):
                summaries.append(f"    {laserName}: {enabled}")
            else:
                units = laserState.get('valueUnits') or ''
                unitText = f' {units}' if units else ''
                summaries.append(
                    f"    {laserName}: {enabled}, {self._fmt(laserState.get('value'))}{unitText}"
                )

        # Current-format Scan values are explicit overrides only. Show both
        # explicit values (including zero) and the absence of an override so
        # setup-mode inspection does not hide that a laser follows Live.
        if 'scanPowers' in state:
            scanPowers = state.get('scanPowers') or {}
            summaries.append('  Scan:')
            for laserName, laserState in orderedLasers:
                if laserState.get('isBinary'):
                    continue
                if laserName in scanPowers and scanPowers[laserName] is not None:
                    units = laserState.get('valueUnits') or ''
                    unitText = f' {units}' if units else ''
                    summaries.append(
                        f"    {laserName}: {self._fmt(scanPowers[laserName])}{unitText} (override)"
                    )
                else:
                    summaries.append(f'    {laserName}: follows Live')

        return summaries or ['  no laser state']

    def getComponentStateHazards(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode,
        context: dict | None = None
    ) -> list[dict]:
        """Identify high-power hazards for the state that can actually emit."""
        if applyMode != ComponentStateApplyMode.SETUP_MODE_APPLY:
            return []

        context = context or {}
        thresholdMw = context.get('laserPowerThresholdMw', 50.0)
        lasers, powers, _warnings = self._setupModeHazardState(state)
        hazards = []

        for laserName, savedState in lasers.items():
            if not isinstance(savedState, dict):
                continue
            if not savedState.get('enabled', False):
                continue
            if savedState.get('isBinary', False):
                continue

            value = self._asFloat(powers.get(laserName))
            if value is None or value <= thresholdMw:
                continue
            units = savedState.get('valueUnits')
            if not self._isMilliwattUnit(units):
                continue

            hazards.append({
                'kind': 'high_laser_power',
                'severity': 'warning',
                'message': (
                    f'Laser {laserName}: {value} mW exceeds {thresholdMw} mW threshold'
                ),
                'details': {
                    'laserName': laserName,
                    'value': value,
                    'units': units or 'mW',
                    'threshold': thresholdMw,
                },
            })
        return hazards

    # Helper methods (lifted from SetupModesController)

    def _laserModuleSupportsModulation(self, module) -> bool:
        """Return whether a laser row has usable frequency-modulation controls."""
        if module is None:
            return False

        supports = getattr(module, 'supportsModulation', None)
        if isinstance(supports, bool):
            return supports

        # Compatibility/defensive fallback for older widgets and lightweight
        # test doubles: require the actual controls used by persistence.
        return all(
            hasattr(module, attr)
            for attr in (
                'modulationEnable',
                'modulationFrequencyEdit',
                'modulationDutyCycleEdit',
            )
        )

    def _laserModuleModulationEnabled(self, module) -> bool:
        getter = getattr(module, 'isModulationEnabled', None)
        if callable(getter):
            return bool(getter())
        return bool(module.modulationEnable.isChecked())
    
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
