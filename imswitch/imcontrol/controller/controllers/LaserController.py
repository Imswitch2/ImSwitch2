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
    # value (``manual`` / ``scan``) so non-Qt consumers and future workflow
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
        # fall back dynamically to the current Manual setpoint, preserving the
        # behaviour of existing setups until Scan power is explicitly edited or
        # loaded from a Scan preset.
        self._manualPowerValues = {}
        self._scanPowerValues = {}
        self._currentPowerContext = LaserPowerContext.MANUAL
        self._scanLaserState = {}
        self._laserDisplayMeta = {}
        self._selectedPresetByContext = {
            LaserPowerContext.MANUAL: None,
            LaserPowerContext.SCAN: None,
        }
        # Lasers this scan armed, so teardown touches exactly those and no
        # others (see scanDevicesResolved / _disarmScanLasers).
        self._scanArmedLasers = []
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

        # The widget starts in Manual view. Preset namespaces are populated
        # contextually; switching view must never itself load a preset.
        if hasattr(self._widget, 'setPowerContext'):
            self._widget.setPowerContext(LaserPowerContext.MANUAL.value, emitSignal=False)
        self._refreshPresetControls()
        self._refreshAllLaserRows()

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

    def toggleLaser(self, laserName, enabled):
        """ Enable or disable laser (on/off)."""
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
        self.setLaserPower(laserName, magnitude, self._currentPowerContext)
    
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

    def _presetStoreForContext(self, context):
        context = normalizeLaserPowerContext(context)
        if context is LaserPowerContext.MANUAL:
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
        if context is getattr(self, '_currentPowerContext', LaserPowerContext.MANUAL):
            setCurrent = getattr(self._widget, 'setCurrentPreset', None)
            if callable(setCurrent):
                try:
                    setCurrent(name, emitSignal=False)
                except TypeError:
                    setCurrent(name)

    def _refreshPresetControls(self):
        context = getattr(self, '_currentPowerContext', LaserPowerContext.MANUAL)
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
        if not name:
            return
        self.saveLaserPreset(name, self._currentPowerContext, select=False)

    def clearPresetSelection(self):
        self._setSelectedPresetName(self._currentPowerContext, None)
        self._refreshPresetControls()

    def renamePreset(self):
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
    def getLaserPresetNames(self, context=LaserPowerContext.MANUAL.value) -> List[str]:
        """Return preset names in the requested Manual/Scan namespace."""
        return list(self._presetStoreForContext(context).keys())

    @APIExport(runOnUIThread=True)
    def getLaserPreset(
        self, name: str, context=LaserPowerContext.MANUAL.value
    ) -> Union[Dict[str, Any], None]:
        """Return ``name`` from one preset namespace without applying it.

        A shallow mapping copy is returned so callers can inspect or replace
        entries without mutating the setup configuration. ``LaserPresetInfo``
        entries are frozen dataclasses, so their values are immutable.
        """
        preset = self._presetStoreForContext(context).get(name)
        return dict(preset) if preset is not None else None

    @APIExport(runOnUIThread=True)
    def loadLaserPreset(self, name: str, context=LaserPowerContext.MANUAL.value) -> bool:
        """Load ``name`` into one power context.

        Loading a Scan preset only updates software state; it never writes to a
        laser manager or changes physical enable state.
        """
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
        context=LaserPowerContext.MANUAL.value,
        select: bool = False,
    ) -> None:
        """Save current powers to ``name`` in one preset namespace.

        Saving and selecting are separate backend operations. This allows a
        workflow to update another preset without loading or selecting it.
        The legacy Manual UI wrappers pass ``select=True`` to preserve their
        historical Save/Save As behaviour.
        """
        context = normalizeLaserPowerContext(context)
        presets = self._presetStoreForContext(context)
        isNew = name not in presets
        preset = self.makePreset(context=context)

        if context is LaserPowerContext.MANUAL:
            self._setupInfo.setLaserPreset(name, preset)
        else:
            self._setupInfo.setLaserScanPreset(name, preset)

        configfiletools.saveSetupInfo(configfiletools.loadOptions()[0], self._setupInfo)
        if select:
            # A selected Scan preset should represent an independent Scan
            # state, not continue following Manual through fallback after the
            # user just saved it as an acquisition preset.
            if context is LaserPowerContext.SCAN:
                for laserName, info in preset.items():
                    self._setScanPowerValue(laserName, info.value)
            self._setSelectedPresetName(context, name)
        if isNew:
            self._emitLaserPresetListChanged(context)
        if context is getattr(self, '_currentPowerContext', LaserPowerContext.MANUAL):
            self._refreshPresetControls()

    @APIExport(runOnUIThread=True)
    def deleteLaserPreset(self, name: str, context=LaserPowerContext.MANUAL.value) -> bool:
        """Delete ``name`` from one preset namespace."""
        context = normalizeLaserPowerContext(context)
        presets = self._presetStoreForContext(context)
        if name not in presets:
            return False

        if context is LaserPowerContext.MANUAL:
            self._setupInfo.removeLaserPreset(name)
        else:
            self._setupInfo.removeLaserScanPreset(name)

        if self._selectedPresetName(context) == name:
            self._setSelectedPresetName(context, None)
        configfiletools.saveSetupInfo(configfiletools.loadOptions()[0], self._setupInfo)
        self._emitLaserPresetListChanged(context)
        if context is getattr(self, '_currentPowerContext', LaserPowerContext.MANUAL):
            self._refreshPresetControls()
        return True

    @APIExport(runOnUIThread=True)
    def renameLaserPreset(
        self, oldName: str, newName: str,
        context=LaserPowerContext.MANUAL.value,
    ) -> bool:
        """Rename a preset inside one context without loading it."""
        context = normalizeLaserPowerContext(context)
        presets = self._presetStoreForContext(context)
        if oldName not in presets or not newName:
            return False

        preset = dict(presets[oldName])
        if context is LaserPowerContext.MANUAL:
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
        if context is getattr(self, '_currentPowerContext', LaserPowerContext.MANUAL):
            self._refreshPresetControls()
        return True

    def makePreset(self, context=LaserPowerContext.MANUAL):
        """Return a preset object for the effective values in ``context``."""
        context = normalizeLaserPowerContext(context)
        return {
            lName: guitools.LaserPresetInfo(
                value=self.getLaserPower(lName, context)
            )
            for lName, lManager in self._master.lasersManager
            if not lManager.isBinary
        }

    def applyPreset(self, laserPreset, context=LaserPowerContext.MANUAL):
        """Apply a preset to Manual hardware state or software-only Scan state."""
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

    def _manualPowerValue(self, laserName):
        values = getattr(self, '_manualPowerValues', {})
        if laserName in values:
            return values[laserName]
        return self._widget.getValue(laserName)

    def _refreshAllLaserRows(self):
        if not hasattr(self, '_master') or not hasattr(self, '_widget'):
            return
        for laserName, _ in self._master.lasersManager:
            self._refreshLaserRow(laserName)

    def _refreshLaserRow(self, laserName, *, updatePrimary=True):
        """Render backend Manual/Scan state into one row if the widget supports it."""
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
        context = getattr(self, '_currentPowerContext', LaserPowerContext.MANUAL)
        scanState = self._scanStateFor(laserName)
        activeForScan = bool(scanState.active_for_scan)
        aoOwned = scanState.power_control is LaserScanPowerControl.AO

        if not manager.isBinary:
            manualPower = self._manualPowerValue(laserName)
            scanPower = self.getLaserPower(laserName, LaserPowerContext.SCAN)
            if updatePrimary:
                value = manualPower if context is LaserPowerContext.MANUAL else scanPower
                setValue = getattr(self._widget, 'setValue', None)
                if callable(setValue):
                    try:
                        setValue(laserName, value, emitSignal=False)
                    except TypeError:
                        setValue(laserName, value)
        else:
            manualPower = scanPower = None

        if not contextualWidget:
            return

        setAO = getattr(self._widget, 'setLaserAOOwned', None)
        setPowerEditable = getattr(self._widget, 'setLaserPowerEditable', None)
        setScanActive = getattr(self._widget, 'setLaserScanActive', None)
        setSecondary = getattr(self._widget, 'setLaserSecondaryStatus', None)

        if callable(setScanActive):
            setScanActive(laserName, activeForScan)

        if context is LaserPowerContext.MANUAL:
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

        # Scan view: Scan power is primary and Manual state is compact/read-only.
        if callable(setAO):
            setAO(laserName, aoOwned, tooltip=self._AO_TOOLTIP)
        if callable(setPowerEditable) and not manager.isBinary:
            setPowerEditable(laserName, not aoOwned)
        if callable(setSecondary):
            manualOn = bool(self._widget.isLaserActive(laserName))
            if manager.isBinary:
                text = f'Manual {"ON" if manualOn else "OFF"}'
            else:
                text = (
                    f'Manual {"ON" if manualOn else "OFF"} · '
                    f'{self._formatLaserPower(laserName, manualPower)}'
                )
            setSecondary(
                laserName, text,
                emphasized=False,
                muted=not activeForScan,
            )

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

        Runs before the execution backend starts, so these writes reach the
        hardware. On NI-DAQ, ``sigScanBuilt`` is already inside the busy
        window; on TriggerScope it is only a compatibility boundary.

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
        so re-arming each frame would stall a fast repeat scan. When the
        republish belongs to a later part of a multi-part run (a timelapse
        timepoint), it instead switches back on exactly the power devices
        ``scanPartDone`` paused during the preceding wait — this fires before
        the execution backend claims the hardware, so the writes get through.
        """
        if self._scanArmedLasers:
            self._resumeScanLasersForNextPart()
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
            if powerDevice is not None and powerDevice not in enabled:
                enabled.append(powerDevice)

        self._scanArmedLasers = list(armed)
        self._scanEnabledLasers = list(enabled)
        self._scanLasersPaused = False
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

    def scanPartDone(self):
        """ One part of a multi-part scan run finished (a timelapse timepoint).

        The run is not over — ``sigScanEnded`` is deliberately withheld for
        non-final parts so the run-level reservation survives — which means
        the armed lasers stay armed straight through the wait until the next
        timepoint. Hardware whose TTL input really gates emission is dark
        anyway (the gate lines park low between parts), but a power device
        switched on over serial would keep emitting into the sample for the
        whole wait.

        So pause exactly the emission the arming switched on, and nothing
        else: a laser the user enabled by hand keeps its manual state here,
        just as it does at arming. ``scanDevicesResolved`` switches the paused
        lasers back on when the next part republishes membership.

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
                self._widget.setLaserActive(lName, True, emitSignal=False)
            except Exception as e:
                self._logger.error(
                    f'Failed to switch laser "{lName}" back on for the next '
                    f'scan part: {e}',
                    exc_info=True,
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
                # silently discards the user's Manual setpoint: the laser
                # backend still owns that value even when LaserWidget is
                # currently displaying Scan power. Put the Manual setpoint
                # back so device and backend agree again.
                #
                # Never write a zero back. Zero carries no information — the
                # laser is already off via setEnabled — and a widget that reads
                # 0 for a laser the user did set (as the AOTFs do) would
                # otherwise have its real amplitude destroyed here instead.
                setpoint = self._manualPowerValue(lName)
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
        self._master.lasersManager[laserName].setValue(value)
        if not hasattr(self, '_manualPowerValues'):
            self._manualPowerValues = {}
        self._manualPowerValues[laserName] = value
        if getattr(self, '_currentPowerContext', LaserPowerContext.MANUAL) is LaserPowerContext.MANUAL:
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
        self._emitLaserPowerChanged(laserName, LaserPowerContext.MANUAL, value)
        # In Scan view an unset Scan value follows Manual dynamically. Refresh
        # the primary editor only while that fallback is active; once Scan has
        # an explicit override, Manual changes must not replace the Scan value.
        scanUsesManualFallback = laserName not in getattr(self, '_scanPowerValues', {})
        updatePrimary = (
            getattr(self, '_currentPowerContext', LaserPowerContext.MANUAL)
            is LaserPowerContext.SCAN
            and scanUsesManualFallback
        )
        self._refreshLaserRow(laserName, updatePrimary=updatePrimary)

    def _setScanPowerValue(self, laserName, value):
        """Store an explicit Scan power without touching hardware or the UI."""
        manager = self._master.lasersManager[laserName]
        if manager.isBinary:
            raise ValueError(
                f'Binary laser "{laserName}" does not have a power setpoint.'
            )
        if not hasattr(self, '_scanPowerValues'):
            self._scanPowerValues = {}
        self._scanPowerValues[laserName] = value
        if getattr(self, '_currentPowerContext', LaserPowerContext.MANUAL) is LaserPowerContext.SCAN:
            self._widget.setValue(laserName, value, emitSignal=False)
        self._emitLaserPowerChanged(laserName, LaserPowerContext.SCAN, value)
        self._refreshLaserRow(laserName, updatePrimary=False)

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
        """Set the live Manual value of a laser (legacy API)."""
        self._setLaserValue(laserName, value)

    @APIExport(runOnUIThread=True)
    def getLaserPower(
        self, laserName: str, context=LaserPowerContext.MANUAL.value
    ) -> Union[int, float]:
        """Return the effective power for ``laserName`` in one context.

        An unset Scan value follows the current Manual value dynamically.
        """
        context = normalizeLaserPowerContext(context)
        manager = self._master.lasersManager[laserName]
        if manager.isBinary:
            raise ValueError(
                f'Binary laser "{laserName}" does not have a power setpoint.'
            )
        if context is LaserPowerContext.MANUAL:
            return self._manualPowerValue(laserName)
        scanValues = getattr(self, '_scanPowerValues', {})
        if laserName in scanValues:
            return scanValues[laserName]
        return self._manualPowerValue(laserName)

    @APIExport(runOnUIThread=True)
    def setLaserPower(
        self,
        laserName: str,
        value: Union[int, float],
        context=LaserPowerContext.MANUAL.value,
    ) -> None:
        """Set Manual hardware power or software-only Scan power."""
        context = normalizeLaserPowerContext(context)
        if context is LaserPowerContext.MANUAL:
            self._setLaserValue(laserName, value)
        else:
            self._setScanPowerValue(laserName, value)

    @APIExport(runOnUIThread=True)
    def getLaserPowers(self, context=LaserPowerContext.MANUAL.value) -> Dict[str, Any]:
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
        context=LaserPowerContext.MANUAL.value,
    ) -> None:
        """Set multiple laser powers in one context."""
        context = normalizeLaserPowerContext(context)
        for laserName, value in values.items():
            self.setLaserPower(laserName, value, context)

    @APIExport(runOnUIThread=True)
    def changeScanPower(self, laserName: str, laserValue: Union[int, float]) -> None:
        """Legacy alias of :meth:`setLaserValue` with Manual semantics.

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
        
        ``currentPreset`` remains as the legacy alias for the Manual preset.
        New state also records ``manualPreset``, ``scanPreset`` and only the
        explicit ``scanPowers`` overrides. Missing Scan values continue to
        follow Manual dynamically.
        """
        manualPreset = self._selectedPresetName(LaserPowerContext.MANUAL)
        state = {
            'lasers': {},
            'laserOrder': [],
            'currentPreset': manualPreset,
            'manualPreset': manualPreset,
            'scanPreset': self._selectedPresetName(LaserPowerContext.SCAN),
            'scanPowers': dict(getattr(self, '_scanPowerValues', {})),
            'modulation': {}
        }
        
        for lName, lManager in self._master.lasersManager:
            state['laserOrder'].append(lName)
            state['lasers'][lName] = {
                'enabled': self._widget.isLaserActive(lName),
                'value': self.getLaserPower(lName, LaserPowerContext.MANUAL) if not lManager.isBinary else None,
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

        # Scan state is software-only. Treat a missing key from an old saved
        # state as "no explicit Scan overrides", i.e. fallback to Manual.
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

        # Always restore Manual power values and modulation for non-binary lasers
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
        manualPreset = state.get('manualPreset', state.get('currentPreset'))
        if manualPreset and manualPreset in self._setupInfo.laserPresets:
            self._setSelectedPresetName(LaserPowerContext.MANUAL, manualPreset)
        elif not manualPreset:
            self._setSelectedPresetName(LaserPowerContext.MANUAL, None)

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
