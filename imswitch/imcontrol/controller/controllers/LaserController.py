from typing import List, Union, Dict, Any

from imswitch.imcommon.model import APIExport
from imswitch.imcommon.framework import Signal
from imswitch.imcontrol.model import (
    LaserPowerContext,
    LaserScanPowerControl,
    LaserScanState,
    configfiletools,
    getWidgetStatePersistence,
    normalizeLaserPowerContext,
    normalizeLaserScanState,
)
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
        self._currentPowerContext = LaserPowerContext.LIVE
        self._scanLaserState = {}
        self._laserDisplayMeta = {}
        self._selectedPresetByContext = {
            LaserPowerContext.LIVE: None,
            LaserPowerContext.SCAN: None,
        }
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

        # The widget starts in Live view. Preset namespaces are populated
        # contextually; switching view must never itself load a preset.
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

        # Register for unified state persistence (canonical name)
        getWidgetStatePersistence().register('Laser', self)

    def closeEvent(self):
        self._master.lasersManager.execOnAll(lambda l: l.setScanModeActive(False))
        self._master.lasersManager.execOnAll(lambda l: l.setValue(0))
        self._master.lasersManager.execOnAll(lambda l: l.setEnabled(False))

    def _requireLaserEditsAllowed(self):
        """Reject user/API laser mutations while acquisition owns the widget."""
        if getattr(self, 'is_scanning', False):
            raise RuntimeError(
                'Laser controls are read-only while a scan is active.'
            )

    def _refreshResumeLiveAvailability(self):
        """Reflect whether an explicit post-scan Live resume is available."""
        snapshot = getattr(self, '_resumableLiveEnabledSnapshot', None) or {}
        setter = getattr(self._widget, 'setResumeLiveAvailable', None)
        if callable(setter):
            setter(any(bool(enabled) for enabled in snapshot.values()))

    def _setResumableLiveEnabledSnapshot(self, snapshot):
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
        self.setLaserPower(laserName, magnitude, self._currentPowerContext)
    
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

    def _presetStoreForContext(self, context):
        context = normalizeLaserPowerContext(context)
        if context is LaserPowerContext.LIVE:
            return self._setupInfo.laserPresets
        return self._setupInfo.laserScanPresets

    def _selectedPresetName(self, context):
        context = normalizeLaserPowerContext(context)
        return getattr(self, '_selectedPresetByContext', {}).get(context)

    def _setSelectedPresetName(self, context, name):
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
        names = list(self._presetStoreForContext(context).keys())
        selected = self._selectedPresetName(context)
        setNames = getattr(self._widget, 'setPresetNames', None)
        if callable(setNames):
            setNames(names, selected=selected)
            return

        # Compatibility path for lightweight/legacy test widgets.
        setCurrent = getattr(self._widget, 'setCurrentPreset', None)
        if callable(setCurrent):
            try:
                setCurrent(selected)
            except TypeError:
                pass

    def powerContextChanged(self, context):
        """Switch which laser power context is displayed; never load a preset."""
        self._requireLaserEditsAllowed()
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
        """Combo selection immediately loads the preset in the visible context."""
        if not presetName:
            return
        self.loadLaserPreset(presetName, self._currentPowerContext)

    # Compatibility wrapper retained for scripts/tests that used the old
    # explicit Load button path. The final UI no longer exposes that button.
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
        """Save current values in the visible context and select the target."""
        if not name:
            name = self._selectedPresetName(self._currentPowerContext)
        if not name:
            getCurrent = getattr(self._widget, 'getCurrentPreset', None)
            name = getCurrent() if callable(getCurrent) else None
        if not name:
            return
        self.saveLaserPreset(name, self._currentPowerContext, select=True)

    def savePresetAs(self):
        self._requireLaserEditsAllowed()
        context = self._currentPowerContext
        name = guitools.askForTextInput(
            self._widget, 'Add laser preset', 'Enter a name for this preset:'
        )
        if not name:
            return

        presets = self._presetStoreForContext(context)
        add = True
        if name in presets:
            add = guitools.askYesNoQuestion(
                self._widget,
                'Laser preset already exists',
                f'A preset with the name "{name}" already exists. Do you want to overwrite it?'
            )
        if add:
            self.saveLaserPreset(name, context, select=True)

    def saveCurrentValuesTo(self, name):
        """Overwrite another preset without loading or selecting it first."""
        self._requireLaserEditsAllowed()
        if not name:
            return
        self.saveLaserPreset(name, self._currentPowerContext, select=False)

    def clearPresetSelection(self):
        self._requireLaserEditsAllowed()
        self._setSelectedPresetName(self._currentPowerContext, None)
        self._refreshPresetControls()

    def renamePreset(self):
        self._requireLaserEditsAllowed()
        context = self._currentPowerContext
        oldName = self._selectedPresetName(context)
        if not oldName:
            return
        newName = guitools.askForTextInput(
            self._widget, 'Rename laser preset', f'Enter a new name for "{oldName}":'
        )
        if not newName or newName == oldName:
            return
        presets = self._presetStoreForContext(context)
        if newName in presets:
            overwrite = guitools.askYesNoQuestion(
                self._widget,
                'Laser preset already exists',
                f'A preset with the name "{newName}" already exists. Do you want to overwrite it?'
            )
            if not overwrite:
                return
        self.renameLaserPreset(oldName, newName, context)

    def deletePreset(self):
        self._requireLaserEditsAllowed()
        context = self._currentPowerContext
        presetToDelete = self._selectedPresetName(context)
        if not presetToDelete:
            return
        confirmationResult = guitools.askYesNoQuestion(
            self._widget,
            'Delete laser preset?',
            f'Are you sure you want to delete the preset "{presetToDelete}"?'
        )
        if confirmationResult:
            self.deleteLaserPreset(presetToDelete, context)

    @APIExport(runOnUIThread=True)
    def getLaserPresetNames(self, context=LaserPowerContext.LIVE.value) -> List[str]:
        """Return preset names in the requested Live/Scan namespace."""
        return list(self._presetStoreForContext(context).keys())

    @APIExport(runOnUIThread=True)
    def getLaserPreset(
        self, name: str, context=LaserPowerContext.LIVE.value
    ) -> Union[Dict[str, Any], None]:
        """Return ``name`` from one preset namespace without applying it.

        A shallow mapping copy is returned so callers can inspect or replace
        entries without mutating the setup configuration. ``LaserPresetInfo``
        entries are frozen dataclasses, so their values are immutable.
        """
        preset = self._presetStoreForContext(context).get(name)
        return dict(preset) if preset is not None else None

    @APIExport(runOnUIThread=True)
    def loadLaserPreset(self, name: str, context=LaserPowerContext.LIVE.value) -> bool:
        """Load ``name`` into one power context.

        Loading a Scan preset only updates software state; it never writes to a
        laser manager or changes physical enable state.
        """
        self._requireLaserEditsAllowed()
        context = normalizeLaserPowerContext(context)
        presets = self._presetStoreForContext(context)
        if name not in presets:
            return False
        self.applyPreset(presets[name], context=context)
        self._setSelectedPresetName(context, name)
        self._emitLaserPresetLoaded(context, name)
        return True

    @APIExport(runOnUIThread=True)
    def saveLaserPreset(
        self,
        name: str,
        context=LaserPowerContext.LIVE.value,
        select: bool = False,
    ) -> None:
        """Save current powers to ``name`` in one preset namespace.

        Saving and selecting are separate backend operations. This allows a
        workflow to update another preset without loading or selecting it.
        The legacy Live UI wrappers pass ``select=True`` to preserve their
        historical Save/Save As behaviour.
        """
        self._requireLaserEditsAllowed()
        context = normalizeLaserPowerContext(context)
        presets = self._presetStoreForContext(context)
        isNew = name not in presets
        preset = self.makePreset(context=context)

        if context is LaserPowerContext.LIVE:
            self._setupInfo.setLaserPreset(name, preset)
        else:
            self._setupInfo.setLaserScanPreset(name, preset)

        configfiletools.saveSetupInfo(configfiletools.loadOptions()[0], self._setupInfo)
        if select:
            # A selected Scan preset should represent an independent Scan
            # state, not continue following Live through fallback after the
            # user just saved it as an acquisition preset.
            if context is LaserPowerContext.SCAN:
                for laserName, info in preset.items():
                    self._setScanPowerValue(laserName, info.value)
            self._setSelectedPresetName(context, name)
        if isNew:
            self._emitLaserPresetListChanged(context)
        if context is getattr(self, '_currentPowerContext', LaserPowerContext.LIVE):
            self._refreshPresetControls()

    @APIExport(runOnUIThread=True)
    def deleteLaserPreset(self, name: str, context=LaserPowerContext.LIVE.value) -> bool:
        """Delete ``name`` from one preset namespace."""
        self._requireLaserEditsAllowed()
        context = normalizeLaserPowerContext(context)
        presets = self._presetStoreForContext(context)
        if name not in presets:
            return False

        if context is LaserPowerContext.LIVE:
            self._setupInfo.removeLaserPreset(name)
        else:
            self._setupInfo.removeLaserScanPreset(name)

        if self._selectedPresetName(context) == name:
            self._setSelectedPresetName(context, None)
        configfiletools.saveSetupInfo(configfiletools.loadOptions()[0], self._setupInfo)
        self._emitLaserPresetListChanged(context)
        if context is getattr(self, '_currentPowerContext', LaserPowerContext.LIVE):
            self._refreshPresetControls()
        return True

    @APIExport(runOnUIThread=True)
    def renameLaserPreset(
        self, oldName: str, newName: str,
        context=LaserPowerContext.LIVE.value,
    ) -> bool:
        """Rename a preset inside one context without loading it."""
        self._requireLaserEditsAllowed()
        context = normalizeLaserPowerContext(context)
        presets = self._presetStoreForContext(context)
        if oldName not in presets or not newName:
            return False

        preset = dict(presets[oldName])
        if context is LaserPowerContext.LIVE:
            self._setupInfo.setLaserPreset(newName, preset)
            if newName != oldName:
                self._setupInfo.removeLaserPreset(oldName)
        else:
            self._setupInfo.setLaserScanPreset(newName, preset)
            if newName != oldName:
                self._setupInfo.removeLaserScanPreset(oldName)

        if self._selectedPresetName(context) == oldName:
            self._setSelectedPresetName(context, newName)
        configfiletools.saveSetupInfo(configfiletools.loadOptions()[0], self._setupInfo)
        self._emitLaserPresetListChanged(context)
        if context is getattr(self, '_currentPowerContext', LaserPowerContext.LIVE):
            self._refreshPresetControls()
        return True

    def makePreset(self, context=LaserPowerContext.LIVE):
        """Return a preset object for the effective values in ``context``."""
        context = normalizeLaserPowerContext(context)
        return {
            lName: guitools.LaserPresetInfo(
                value=self.getLaserPower(lName, context)
            )
            for lName, lManager in self._master.lasersManager
            if not lManager.isBinary
        }

    def applyPreset(self, laserPreset, context=LaserPowerContext.LIVE):
        """Apply a preset to Live hardware state or software-only Scan state."""
        context = normalizeLaserPowerContext(context)
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
            self.setLaserPower(laserName, laserPresetInfo.value, context)

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
        """Return semantic scan participation/power ownership for all lasers."""
        return {
            name: {
                'active_for_scan': self._scanStateFor(name).active_for_scan,
                'power_control': self._scanStateFor(name).power_control.value,
            }
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
        context = getattr(self, '_currentPowerContext', LaserPowerContext.LIVE)
        scanState = self._scanStateFor(laserName)
        activeForScan = bool(scanState.active_for_scan)
        aoOwned = scanState.power_control is LaserScanPowerControl.AO

        if not manager.isBinary:
            livePower = self._livePowerValue(laserName)
            scanPower = self.getLaserPower(laserName, LaserPowerContext.SCAN)
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
        """Track scan ownership and make the LaserWidget read-only in-run."""
        if isScanning:
            self.is_scanning = True
            setEditable = getattr(self._widget, 'setEditable', None)
            if callable(setEditable):
                setEditable(False)
            self._prepareScanRun()
            return

        try:
            self._disarmScanLasers()
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
                value = self.getLaserPower(target, LaserPowerContext.SCAN)
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

    def scanDevicesResolved(self, deviceList):
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

    def scanPartDone(self):
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

    def _resumeScanLasersForNextPart(self):
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

    def _disarmScanLasers(self):
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
    def getLaserPower(
        self, laserName: str, context=LaserPowerContext.LIVE.value
    ) -> Union[int, float]:
        """Return the effective power for ``laserName`` in one context.

        An unset Scan value follows the current Live value dynamically.
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
    def setLaserPower(
        self,
        laserName: str,
        value: Union[int, float],
        context=LaserPowerContext.LIVE.value,
    ) -> None:
        """Set Live hardware power or software-only Scan power."""
        context = normalizeLaserPowerContext(context)
        if context is LaserPowerContext.LIVE:
            self._setLaserValue(laserName, value)
        else:
            self._setScanPowerValue(laserName, value)

    @APIExport(runOnUIThread=True)
    def getLaserPowers(self, context=LaserPowerContext.LIVE.value) -> Dict[str, Any]:
        """Return effective non-binary laser powers for one context."""
        context = normalizeLaserPowerContext(context)
        return {
            laserName: self.getLaserPower(laserName, context)
            for laserName, manager in self._master.lasersManager
            if not manager.isBinary
        }

    @APIExport(runOnUIThread=True)
    def setLaserPowers(
        self, values: Dict[str, Union[int, float]],
        context=LaserPowerContext.LIVE.value,
    ) -> None:
        """Set multiple laser powers in one context."""
        context = normalizeLaserPowerContext(context)
        for laserName, value in values.items():
            self.setLaserPower(laserName, value, context)

    @APIExport(runOnUIThread=True)
    def changeScanPower(self, laserName: str, laserValue: Union[int, float]) -> None:
        """Legacy alias of :meth:`setLaserValue` with Live semantics.

        Despite the historical name, changing this method to software-only Scan
        state would silently break existing scripts, so the permanent Scan API
        is :meth:`setLaserPower` with ``context='scan'``.
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
        livePreset = self._selectedPresetName(LaserPowerContext.LIVE)
        state = {
            'lasers': {},
            'laserOrder': [],
            'currentPreset': livePreset,
            'livePreset': livePreset,
            'scanPreset': self._selectedPresetName(LaserPowerContext.SCAN),
            'scanPowers': dict(getattr(self, '_scanPowerValues', {})),
            'modulation': {}
        }
        
        for lName, lManager in self._master.lasersManager:
            state['laserOrder'].append(lName)
            state['lasers'][lName] = {
                'enabled': self._widget.isLaserActive(lName),
                'value': self.getLaserPower(lName, LaserPowerContext.LIVE) if not lManager.isBinary else None,
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
        self._requireLaserEditsAllowed()
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
            restoredScanPowers[lName] = value
        self._scanPowerValues = restoredScanPowers

        # Always restore Live power values and modulation for non-binary lasers
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
        
        # Restore selected preset names only; do NOT apply either preset.
        livePreset = state.get('livePreset', state.get('currentPreset'))
        if livePreset and livePreset in self._setupInfo.laserPresets:
            self._setSelectedPresetName(LaserPowerContext.LIVE, livePreset)
        elif not livePreset:
            self._setSelectedPresetName(LaserPowerContext.LIVE, None)

        scanPresets = getattr(self._setupInfo, 'laserScanPresets', {})
        scanPreset = state.get('scanPreset')
        if scanPreset and scanPreset in scanPresets:
            self._setSelectedPresetName(LaserPowerContext.SCAN, scanPreset)
        else:
            self._setSelectedPresetName(LaserPowerContext.SCAN, None)

        self._refreshPresetControls()
        self._refreshAllLaserRows()
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
