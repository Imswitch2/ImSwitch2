import copy
import functools
import os
import traceback

from abc import abstractmethod
from enum import Enum

from imswitch.imcommon.controller.basecontrollers import (
    WidgetController,
    WidgetControllerFactory,
)
from imswitch.imcontrol.model import InvalidChildClassError
from imswitch.imcommon.model import APIExport, dirtools, initLogger


class ImConWidgetControllerFactory(WidgetControllerFactory):
    """ Factory class for creating a ImConWidgetController object. """

    def __init__(self, setupInfo, master, commChannel, moduleCommChannel):
        super().__init__(setupInfo=setupInfo, master=master, commChannel=commChannel,
                         moduleCommChannel=moduleCommChannel)


class ImConWidgetController(WidgetController):
    """ Superclass for all ImConWidgetController.
    All WidgetControllers should have access to the setup information,
    MasterController, CommunicationChannel and the linked Widget. """

    def __init__(self, setupInfo, commChannel, master, *args, **kwargs):
        # Protected attributes, which should only be accessed from controller and its subclasses
        self._setupInfo = setupInfo
        self._commChannel = commChannel
        self._master = master

        # Init superclass
        super().__init__(*args, **kwargs)


class ComponentStateApplyMode(Enum):
    """Distinguishes passive UI restore from active hardware application."""
    STARTUP_RESTORE = "startup_restore"
    SETUP_MODE_APPLY = "setup_mode_apply"


class StatefulComponentMixin:
    """Mixin for controllers that provide unified component state snapshots.
    
    Supersedes both the legacy getWidgetState/setWidgetState interface and
    the SetupModeMixin interface. A single component state payload serves
    both startup persistence and named setup modes, with applyMode controlling
    which activations are allowed.
    """
    
    # Class attributes
    stateSchemaVersion: int = 1
    componentName: str | None = None
    legacyStateNames: tuple = ()
    
    def getComponentState(self) -> dict:
        """Snapshot the current component state.
        
        Returns:
            A JSON-serializable dict. The schema is component-specific but
            must remain compatible within a stateSchemaVersion.
        
        Raises:
            May raise for unrecoverable failures (e.g., required hardware
            unavailable). The registry logs and skips the component.
        """
        raise NotImplementedError
    
    def applyComponentState(
        self,
        state: dict,
        *,
        applyMode: 'ComponentStateApplyMode',
    ) -> list[str]:
        """Restore component state from a snapshot.
        
        Args:
            state: A dict previously returned by getComponentState().
            applyMode: Controls which actions are permitted (see spec Section 2).
        
        Returns:
            A list of warning strings for recoverable issues (e.g., missing
            devices, value clamps, skipped unavailable presets). An empty list
            indicates full success.
        
        Raises:
            MUST NOT raise for recoverable schema mismatches, missing optional
            keys, or unavailable devices. Return a warning instead.
            MAY raise for catastrophic errors (e.g., corrupted state that
            cannot be parsed at all), but this should be rare.
        
        Safety:
            The component MUST enforce the apply-mode safety policy (spec Section 2).
            The consumer provides the mode; the component owns enforcement.
        """
        raise NotImplementedError
    
    def describeComponentState(self, state: dict) -> list[str]:
        """Generate a human-readable summary of a saved state.
        
        Args:
            state: A dict previously returned by getComponentState().
        
        Returns:
            A list of formatted strings suitable for display in a setup-mode
            inspector or update-preview dialog. May be multi-line; each string
            is one logical block.
        
        Examples:
            ["Laser 488nm: ON, 50.0 mW", "Laser 561nm: OFF"]
            ["Detector Camera1: ROI=(0,0,512,512), binning=2x2"]
        
        Notes:
            This replaces the component-specific raw-payload parsing currently
            in SetupModesController._summarizeSaved* methods. The consumer
            calls this instead of parsing the state dict directly.
        """
        raise NotImplementedError
    
    def getComponentStateHazards(
        self,
        state: dict,
        *,
        applyMode: 'ComponentStateApplyMode',
        context: dict | None = None,
    ) -> list[dict]:
        """Identify potential hazards in a saved state before applying it.
        
        Args:
            state: A dict previously returned by getComponentState().
            applyMode: The mode in which the state would be applied.
            context: Optional consumer-provided context (e.g., UI thresholds,
                suppressed-warning lists). See spec Section 5.2 for schema.
        
        Returns:
            A list of hazard records (see spec Section 5.3 for schema). An empty
            list means no hazards detected.
        
        Examples:
            High-power laser: severity="warning", kind="high_laser_power"
            Missing device: severity="info", kind="device_unavailable"
        
        Notes:
            The component identifies hazards; the consumer owns the policy
            (thresholds, confirmation dialogs, suppression). This replaces
            the current SetupModesController._getHighPowerLaserEntries logic.
        """
        raise NotImplementedError


class SetupModeMixin(StatefulComponentMixin):
    """DEPRECATED: Legacy mixin for setup-mode state.
    
    Kept as a thin compatibility shim for FlipMirror and SuperScanController
    until Phase 2 migration. New controllers should implement
    StatefulComponentMixin directly.
    
    The setup-mode backend discovers controllers implementing this mixin and
    delegates component-specific serialization to them. Returned state must be
    JSON-serializable. Applying state should return warning strings for
    recoverable mismatches, such as unavailable devices, instead of raising.
    """

    def getSetupModeState(self):
        raise NotImplementedError

    def applySetupModeState(self, state):
        raise NotImplementedError
    
    # Bridge legacy methods to new interface (overridden by registry fallback)
    def getComponentState(self) -> dict:
        """Bridge to legacy getSetupModeState for compatibility."""
        return self.getSetupModeState()
    
    def applyComponentState(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode,
    ) -> list[str]:
        """Bridge to legacy applySetupModeState for compatibility."""
        result = self.applySetupModeState(state)
        if isinstance(result, list):
            return result
        return []
    
    def describeComponentState(self, state: dict) -> list[str]:
        """Stub for legacy controllers - registry will handle summarization."""
        return [f"Component state (legacy): {len(state)} keys"]
    
    def getComponentStateHazards(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode,
        context: dict | None = None,
    ) -> list[dict]:
        """Stub for legacy controllers - no hazard detection yet."""
        return []


class LiveUpdatedController(ImConWidgetController):
    """ Superclass for those controllers that will update the widgets with an
    upcoming frame from the camera.  Should be either active or not, and have
    an update function. """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.active = False

    def update(self, detectorName, im, init, isCurrentDetector):
        raise NotImplementedError


class ScanLifecycleMixin:
    """ Announces this controller as the CommunicationChannel's active scan
    source whenever its isRunning flag flips.

    Every scan controller signals its scan lifecycle by setting
    ``self.isRunning`` (True in runScanAdvanced, False in scanDone /
    scanFailed / error handlers). This mixin turns that flag into a property
    so each assignment also announces or withdraws the controller as the
    channel's active scan source — the single authority consumers such as
    BeadRec use to resolve "which controller is running this scan". Using the
    existing flag as the chokepoint means no call site, present or future,
    can be forgotten.

    Requirements on the inheriting controller: it must be an
    ImConWidgetController (so ``self._commChannel`` is set before the first
    isRunning assignment) and must keep maintaining ``isRunning`` around its
    scan lifecycle. The adoption-audit unit test in
    imswitch/imcontrol/_test/unit/test_scan_lifecycle.py enforces that every
    scan controller inherits this mixin. See docs/scan_lifecycle.md.
    """

    # Class-level default so the getter works before __init__ assigns it
    _isRunningFlag = False

    @property
    def isRunning(self) -> bool:
        return self._isRunningFlag

    @isRunning.setter
    def isRunning(self, value: bool) -> None:
        self._isRunningFlag = bool(value)
        if self._isRunningFlag:
            self._commChannel.setActiveScanSource(self)
        else:
            self._commChannel.clearActiveScanSource(self)


class SuperScanController(StatefulComponentMixin, ScanLifecycleMixin, ImConWidgetController):
    componentName = 'Scan'
    stateSchemaVersion = 1
    legacyStateNames = ('ScanController', 'ScanControllerAdvanced', 'ScanControllerMoNaLISA', 'ScanControllerPointScan')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Make non-overwritable functions
        self.isValidScanController = self.__isValidScanController
        self.isValidChild = self.isValidScanController

        self._logger = initLogger(self)

        self.settingAttr = False
        self.settingParameters = False

        self._analogParameterDict = {}
        self._digitalParameterDict = {}
        self._positionersScan = []
        self.signalDict = None
        self.scanInfoDict = None
        self.isRunning = False
        self.doingNonFinalPartOfSequence = False

        self.positioners = {
            pName: pManager for pName, pManager in self._setupInfo.positioners.items()
            if pManager.forScanning
        }
        self.TTLDevices = self._setupInfo.getTTLDevices()

        self.scanDir = os.path.join(dirtools.UserFileDirs.Root, 'imcontrol_scans')
        if not os.path.exists(self.scanDir):
            os.makedirs(self.scanDir)

        # Connect NidaqManager signals
        self._master.nidaqManager.sigScanBuilt.connect(
            lambda _, __, deviceList: self.emitScanSignal(self._commChannel.sigScanBuilt, deviceList)
        )
        self._master.nidaqManager.sigScanStarted.connect(
            lambda: self.emitScanSignal(self._commChannel.sigScanStarted)
        )
        self._master.nidaqManager.sigScanDone.connect(self.scanDone)
        self._master.nidaqManager.sigScanBuildFailed.connect(self.scanFailed)

        # Connect CommunicationChannel signals
        self._commChannel.sigRunScan.connect(self.runScanExternal)
        self._commChannel.sigAbortScan.connect(self.abortScan)
        self._commChannel.sharedAttrs.sigAttributeSet.connect(self.attrChanged)
        self._commChannel.sigToggleBlockScanWidget.connect(lambda block: self.toggleBlockWidget(block))
        self._commChannel.sigRequestScanParameters.connect(self.sendScanParameters)
        self._commChannel.sigSetAxisCenters.connect(lambda devices, centers: self.setCenterParameters(devices, centers))

        # Connect ScanWidget signals
        self._widget.sigSaveScanClicked.connect(self.saveScan)
        self._widget.sigLoadScanClicked.connect(self.loadScan)
        self._widget.sigRunScanClicked.connect(self.runScan)
        self._widget.sigSeqTimeParChanged.connect(self.updateScanTTLAttrs)
        self._widget.sigStageParChanged.connect(self.updatePixels)
        self._widget.sigStageParChanged.connect(self.updateScanStageAttrs)
        self._widget.sigSignalParChanged.connect(self.updateScanTTLAttrs)

    @property
    def parameterDict(self):
        return None

    def __isValidScanController(self):
        if self.parameterDict is None:
            raise InvalidChildClassError('ScanController needs to return a valid parameterDict')
        else:
            return True

    def runScanExternal(self, recalculateSignals, isNonFinalPartOfSequence):
        """ Run scan from external non-scan-widget trigger. """
        self._widget.setScanMode()
        self._widget.setRepeatEnabled(False)
        self.runScanAdvanced(recalculateSignals=recalculateSignals,
                             isNonFinalPartOfSequence=isNonFinalPartOfSequence,
                             sigScanStartingEmitted=True)
    
    @abstractmethod
    def setParameters(self):
        """ Set scan parameters from analog and digital parameter dictionaries. """
        pass

    @abstractmethod
    def getParameters(self):
        """ Get scan parameters from widget GUI fields. """
        pass

    @abstractmethod
    def updatePixels(self):
        """ Update number of pixels field in GUI. """
        pass

    @abstractmethod
    def emitScanSignal(self, signal, *args):
        """ Emit general scan signal. """
        pass

    @abstractmethod
    def runScanAdvanced(self, *, recalculateSignals=True, isNonFinalPartOfSequence=False,
                        sigScanStartingEmitted):
        """ Run a scan with the set scanning parameters. """
        pass

    @abstractmethod
    def scanDone(self):
        """ Called when scan is done, clean up and toggle GUI. """
        pass

    @APIExport(runOnUIThread=True)
    @abstractmethod
    def saveScanParamsToFile(self, filePath: str) -> None:
        """ Saves the set scanning parameters to the specified file. """
        pass

    @APIExport(runOnUIThread=True)
    @abstractmethod
    def loadScanParamsFromFile(self, filePath: str) -> None:
        """ Loads scanning parameters from the specified file. """
        pass

    def getNextAxial(self):
        return None

    def getNumCamTTL(self):
        numCamTTL = {}
        for detector in self._setupInfo.detectors.keys():
            if self._master.scanManager.getTTLCycleSignalsDict(self._digitalParameterDict).get(detector,None) is not None:
                ttl=self._master.scanManager.getTTLCycleSignalsDict(self._digitalParameterDict).get(detector).tolist()
                numCamTTL[detector] = len([i for i in range(len(ttl)-1) if ttl[i+1]-ttl[i] == 1])
        return numCamTTL

    def getNumScanPositions(self):
        """ Returns the number of scan positions for the configured scan. """
        _, positions, _ = self._master.scanManager.getScanSignalsDict(self._analogParameterDict)
        numPositions = functools.reduce(lambda x, y: x * y, positions)
        return numPositions

    def saveScan(self):
        """ Save scan parameters template. """
        from imswitch.imcontrol.view import guitools

        fileName = guitools.askForFilePath(
            self._widget, 'Save scan', self.scanDir,
            nameFilter='Scan parameters (*.json)', isSaving=True
        )
        if not fileName:
            return
        self.saveScanParamsToFile(fileName)

    def loadScan(self):
        """ Load scan parameters template. """
        from imswitch.imcontrol.view import guitools

        fileName = guitools.askForFilePath(
            self._widget, 'Load scan', self.scanDir,
            nameFilter='Scan parameters (*.json)'
        )
        if not fileName:
            return
        self.loadScanParamsFromFile(fileName)

    def abortScan(self):
        """ Abort scan. """
        self.doingNonFinalPartOfSequence = False  # So that sigScanEnded is emitted
        if not self.isRunning:
            self.scanFailed()

    def scanFailed(self):
        """ Called when scan failed. """
        self._logger.error('Scan failed')
        self.isRunning = False
        self.doingNonFinalPartOfSequence = False
        self._widget.setScanButtonChecked(False)
        self.emitScanSignal(self._commChannel.sigScanEnded)

    def setCenterParameters(self, devices, centers):
        """ Set center parameter for all axes. """
        for centerpos, scannerSet in zip(centers, devices):
            # for every incoming device, listed in order of scanAxes
            for scannerAxis in self.positioners:
                # for every device, listed in order as device list
                if scannerSet == scannerAxis:
                    self._widget.setScanCenterPos(scannerSet, centerpos)

    def attrChanged(self, key, value):
        if self.settingAttr or len(key) != 2:
            return

        if key[0] == _attrCategoryStage:
            self._analogParameterDict[key[1]] = value
            self.setParameters()
        elif key[0] == _attrCategoryTTL:
            self._digitalParameterDict[key[1]] = value
            self.setParameters()
            
    def toggleBlockWidget(self, block):
        """ Blocks/unblocks scan widget if scans are run from elsewhere. """
        self._widget.setEnabled(block)

    def setSharedAttr(self, category, attr, value):
        self.settingAttr = True
        try:
            self._commChannel.sharedAttrs[(category, attr)] = value
        finally:
            self.settingAttr = False

    def updateScanStageAttrs(self):
        self.getParameters()

        for key, value in self._analogParameterDict.items():
            self.setSharedAttr(_attrCategoryStage, key, value)

        positiveDirections = []
        for i in range(len(self.positioners)):
            positionerName = self._analogParameterDict['target_device'][i]
            if positionerName != 'None':
                positiveDirection = self._setupInfo.positioners[positionerName].isPositiveDirection
                positiveDirections.append(positiveDirection)

        self.setSharedAttr(_attrCategoryStage, 'positive_direction', positiveDirections)

    def updateScanTTLAttrs(self):
        self.getParameters()

        for key, value in self._digitalParameterDict.items():
            self.setSharedAttr(_attrCategoryTTL, key, value)


    @APIExport(runOnUIThread=True)
    def runScan(self) -> None:
        """ Runs a scan with the set scanning parameters. """
        self.runScanAdvanced(sigScanStartingEmitted=False)
        
    def sendScanParameters(self):
        self.getParameters()
        self._commChannel.sigSendScanParameters.emit(self._analogParameterDict, self._digitalParameterDict, self._positionersScan)

    def getComponentState(self) -> dict:
        """Snapshot the current scan parameter dictionaries for component state persistence."""
        self.getParameters()

        mode = {
            'repeatEnabled': (
                self._widget.repeatEnabled() if hasattr(self._widget, 'repeatEnabled') else None
            )
        }

        if hasattr(self._widget, 'isScanMode'):
            mode['scanMode'] = self._widget.isScanMode()
        if hasattr(self._widget, 'isContLaserMode'):
            mode['contLaserMode'] = self._widget.isContLaserMode()

        scanInfo = getattr(self._setupInfo, 'scan', None)

        return {
            'controller': type(self).__name__,
            'scanWidgetType': getattr(scanInfo, 'scanWidgetType', None),
            'analogParameterDict': copy.deepcopy(self._analogParameterDict),
            'digitalParameterDict': copy.deepcopy(self._digitalParameterDict),
            'positionersScan': list(self._positionersScan),
            'mode': mode,
        }

    def applyComponentState(self, state: dict, *, applyMode: ComponentStateApplyMode) -> list[str]:
        """Apply scan parameter dictionaries from component state.
        
        CRITICAL SAFETY INVARIANT: This method MUST NEVER start a scan in either mode.
        It only restores scan parameters (analogParameterDict, digitalParameterDict, mode flags).
        Starting a scan requires explicit user action (runScan/runScanAdvanced).
        
        Args:
            state: Component state dict from getComponentState().
            applyMode: STARTUP_RESTORE or SETUP_MODE_APPLY (behavior is identical for scan params).
        
        Returns:
            List of warning strings for recoverable issues.
        """
        warnings = []

        if self.isRunning:
            return ['Scan is currently running; scan parameters were not changed.']

        if not isinstance(state, dict):
            return ['Saved scan state is not a dictionary.']

        savedWidgetType = state.get('scanWidgetType')
        currentWidgetType = getattr(getattr(self._setupInfo, 'scan', None), 'scanWidgetType', None)
        if savedWidgetType and currentWidgetType and savedWidgetType != currentWidgetType:
            warnings.append(
                f'Saved scan widget type "{savedWidgetType}" differs from current '
                f'"{currentWidgetType}".'
            )

        analogParameterDict = copy.deepcopy(state.get('analogParameterDict', {}))
        digitalParameterDict = copy.deepcopy(state.get('digitalParameterDict', {}))

        if not isinstance(analogParameterDict, dict):
            return warnings + ['Saved analog scan parameters are not a dictionary.']
        if not isinstance(digitalParameterDict, dict):
            return warnings + ['Saved digital scan parameters are not a dictionary.']

        missingPositioners = self._getMissingScanDevices(
            analogParameterDict.get('target_device', []),
            set(self.positioners.keys())
        )
        if missingPositioners:
            warnings.append(
                f'Missing scan positioner(s): {", ".join(missingPositioners)}. '
                'Scan state was not applied.'
            )
            return warnings

        scanDimDevices = analogParameterDict.get('scan_dim_target_device', [])
        missingScanDims = self._getMissingScanDevices(scanDimDevices, set(self.positioners.keys()))
        if missingScanDims:
            warnings.append(
                f'Missing scan dimension positioner(s): {", ".join(missingScanDims)}. '
                'Scan state was not applied.'
            )
            return warnings

        missingTTLDevices = self._getMissingScanDevices(
            digitalParameterDict.get('target_device', []),
            set(self.TTLDevices.keys())
        )
        if missingTTLDevices:
            warnings.append(
                f'Missing TTL device(s): {", ".join(missingTTLDevices)}. '
                'Scan state was not applied.'
            )
            return warnings

        self._analogParameterDict = analogParameterDict
        self._digitalParameterDict = digitalParameterDict

        positionersScan = state.get(
            'positionersScan',
            analogParameterDict.get('scan_dim_target_device', self._positionersScan)
        )
        if isinstance(positionersScan, (list, tuple)):
            self._positionersScan = list(positionersScan)
        elif positionersScan is not None:
            warnings.append(
                'Saved scan dimension selection is invalid; keeping the current selection.'
            )

        mode = state.get('mode', {}) or {}
        try:
            if mode.get('scanMode') is True and hasattr(self._widget, 'setScanMode'):
                self._widget.setScanMode()
            elif mode.get('contLaserMode') is True and hasattr(self._widget, 'setContLaserMode'):
                self._widget.setContLaserMode()

            if mode.get('repeatEnabled') is not None and hasattr(self._widget, 'setRepeatEnabled'):
                self._widget.setRepeatEnabled(bool(mode['repeatEnabled']))

            self.setParameters()
            self.signalDict = None
            self.scanInfoDict = None

            try:
                self.updateScanStageAttrs()
                self.updateScanTTLAttrs()
            except Exception:
                self._logger.error('Failed to update shared scan attributes after component state apply')
                self._logger.error(traceback.format_exc())
                warnings.append(
                    'Scan parameters were applied, but shared scan attributes could not be updated.'
                )
        except Exception as e:
            self._logger.error('Failed to apply scan component state')
            self._logger.error(traceback.format_exc())
            warnings.append(f'Failed to apply scan state: {e}')

        return warnings

    def describeComponentState(self, state: dict) -> list[str]:
        """Generate a human-readable summary of a saved scan state.
        
        Lifted from SetupModesController._summarizeSavedScanState and helpers.
        """
        if not isinstance(state, dict) or not state:
            return ["  no scan state"]

        analog = state.get("analogParameterDict") or {}
        digital = state.get("digitalParameterDict") or {}
        mode = state.get("mode") or {}
        summaries = []

        if state.get("controller"):
            summaries.append(f"  controller: {self._fmt(state.get('controller'))}")
        if state.get("scanWidgetType"):
            summaries.append(f"  widget type: {self._fmt(state.get('scanWidgetType'))}")

        modeLines = self._summarizeSavedScanMode(mode)
        if modeLines:
            summaries.append("  mode:")
            summaries.extend(modeLines)

        sequenceTime = analog.get("sequence_time", digital.get("sequence_time"))
        if sequenceTime is not None:
            summaries.append(f"  sequence time: {self._fmt(sequenceTime)}")

        scanDimensions = state.get("positionersScan") or analog.get("scan_dim_target_device")
        if scanDimensions:
            summaries.append(f"  scan dimensions: {self._fmt(scanDimensions)}")

        axisLines = self._summarizeSavedScanAxes(analog)
        if axisLines:
            summaries.append("  axes:")
            summaries.extend(axisLines)

        digitalOverviewLines = self._summarizeSavedScanDigitalOverview(digital)
        if digitalOverviewLines:
            summaries.append("  digital:")
            summaries.extend(digitalOverviewLines)

        ttlLines = self._summarizeSavedScanTTL(digital)
        if ttlLines:
            summaries.append("  TTL:")
            summaries.extend(ttlLines)

        analogExtraLines = self._summarizeSavedScanExtras(
            analog,
            {
                "target_device", "axis_length", "axis_step_size",
                "axis_centerpos", "axis_startpos", "scan_dim_target_device",
                "sequence_time",
            }
        )
        if analogExtraLines:
            summaries.append("  analog extras:")
            summaries.extend(analogExtraLines)

        digitalExtraLines = self._summarizeSavedScanExtras(
            digital,
            {
                "target_device", "TTL_start", "TTL_end", "TTL_sequence",
                "TTL_sequence_axis", "sequence_time", "n_linesteps", "Nx", "Ny",
                "advanced_mode", "linestep_enable", "pulse_starts_s",
                "pulse_ends_s", "linestep_power_percent",
            }
        )
        if digitalExtraLines:
            summaries.append("  digital extras:")
            summaries.extend(digitalExtraLines)

        return summaries or ["  no scan state"]

    def getComponentStateHazards(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode,
        context: dict | None = None,
    ) -> list[dict]:
        """Identify potential hazards in a saved scan state.
        
        Scan parameters carry no laser-power-like hazards; laser hazards belong
        to the Laser component. Returns an empty list.
        """
        return []

    def _summarizeSavedScanMode(self, mode):
        """Lifted from SetupModesController."""
        if not isinstance(mode, dict):
            return []

        labels = {
            "repeatEnabled": "repeat",
            "scanMode": "scan mode",
            "contLaserMode": "continuous laser mode",
        }
        summaries = []
        for key in ("repeatEnabled", "scanMode", "contLaserMode"):
            value = mode.get(key)
            if value is not None:
                summaries.append(f"    {labels[key]}: {self._fmt(value)}")
        return summaries

    def _summarizeSavedScanAxes(self, analog):
        """Lifted from SetupModesController."""
        if not isinstance(analog, dict):
            return []

        devices = analog.get("target_device") or []
        if not isinstance(devices, list):
            devices = [devices]

        axisKeys = [
            ("length", "axis_length"),
            ("step", "axis_step_size"),
            ("center", "axis_centerpos"),
            ("start", "axis_startpos"),
        ]
        maxAxisCount = max(
            [len(devices)]
            + [
                len(analog.get(key) or [])
                for _, key in axisKeys
                if isinstance(analog.get(key), list)
            ]
        )

        summaries = []
        for index in range(maxAxisCount):
            device = self._scanListValue(devices, index, f"axis {index + 1}")
            parts = []
            for label, key in axisKeys:
                value = self._scanListValue(analog.get(key), index)
                if value is not None:
                    parts.append(f"{label} {self._fmt(value)}")

            if parts:
                summaries.append(f"    {device}: " + ", ".join(parts))

        return summaries

    def _summarizeSavedScanDigitalOverview(self, digital):
        """Lifted from SetupModesController."""
        if not isinstance(digital, dict):
            return []

        labels = {
            "Nx": "Nx",
            "Ny": "Ny",
            "n_linesteps": "line steps",
            "advanced_mode": "advanced mode",
        }
        summaries = []
        for key in ("Nx", "Ny", "n_linesteps", "advanced_mode"):
            if key in digital:
                summaries.append(f"    {labels[key]}: {self._fmt(digital.get(key))}")
        return summaries

    def _summarizeSavedScanTTL(self, digital):
        """Lifted from SetupModesController."""
        if not isinstance(digital, dict):
            return []

        deviceNames = self._scanTTLDeviceNames(digital)
        if not deviceNames:
            return []

        summaries = []
        for index, deviceName in enumerate(deviceNames):
            parts = []

            ttlStart = self._scanListValue(digital.get("TTL_start"), index)
            ttlEnd = self._scanListValue(digital.get("TTL_end"), index)
            if ttlStart is not None:
                parts.append(f"start {self._fmtMilliseconds(ttlStart)}")
            if ttlEnd is not None:
                parts.append(f"end {self._fmtMilliseconds(ttlEnd)}")

            ttlSequence = self._scanListValue(digital.get("TTL_sequence"), index)
            ttlAxis = self._scanListValue(digital.get("TTL_sequence_axis"), index)
            if ttlSequence is not None:
                parts.append(f"sequence {self._fmtShort(ttlSequence)}")
            if ttlAxis is not None:
                parts.append(f"axis {self._fmtShort(ttlAxis)}")

            self._appendDictTTLPart(parts, digital.get("linestep_enable"), deviceName, "enabled")
            self._appendDictTTLPart(
                parts, digital.get("pulse_starts_s"), deviceName, "starts",
                formatter=self._fmtMilliseconds
            )
            self._appendDictTTLPart(
                parts, digital.get("pulse_ends_s"), deviceName, "ends",
                formatter=self._fmtMilliseconds
            )
            self._appendDictTTLPart(
                parts, digital.get("linestep_power_percent"), deviceName, "power"
            )

            summaries.append(f"    {deviceName}: " + (", ".join(parts) if parts else "saved"))

        return summaries

    def _scanTTLDeviceNames(self, digital):
        """Lifted from SetupModesController."""
        names = []

        targetDevices = digital.get("target_device") or []
        if not isinstance(targetDevices, list):
            targetDevices = [targetDevices]
        names.extend([name for name in targetDevices if name is not None])

        for key in ("linestep_enable", "pulse_starts_s", "pulse_ends_s", "linestep_power_percent"):
            value = digital.get(key)
            if isinstance(value, dict):
                names.extend(value.keys())

        uniqueNames = []
        seen = set()
        for name in names:
            key = str(name)
            if key in seen:
                continue
            uniqueNames.append(name)
            seen.add(key)

        return uniqueNames

    def _appendDictTTLPart(self, parts, valuesByDevice, deviceName, label, formatter=None):
        """Lifted from SetupModesController."""
        if not isinstance(valuesByDevice, dict) or deviceName not in valuesByDevice:
            return
        formatter = formatter or self._fmtShort
        parts.append(f"{label} {formatter(valuesByDevice.get(deviceName))}")

    def _summarizeSavedScanExtras(self, values, excludedKeys):
        """Lifted from SetupModesController."""
        if not isinstance(values, dict):
            return []

        summaries = []
        for key in sorted(set(values.keys()) - set(excludedKeys), key=str):
            summaries.append(f"    {key}: {self._fmtShort(values.get(key))}")
        return summaries

    def _scanListValue(self, value, index, default=None):
        """Lifted from SetupModesController."""
        if isinstance(value, list):
            if 0 <= index < len(value):
                return value[index]
            return default
        return value if value is not None else default

    def _fmt(self, value):
        """Lifted from SetupModesController."""
        if value is None:
            return "None"
        if isinstance(value, bool):
            return self._onOff(value)
        if isinstance(value, float):
            return f"{value:.4g}"
        if isinstance(value, (list, tuple)):
            return "[" + ", ".join(self._fmt(item) for item in value) + "]"
        return str(value)

    def _fmtShort(self, value, maxLength=140):
        """Lifted from SetupModesController."""
        text = self._fmt(value)
        if len(text) <= maxLength:
            return text
        return text[:maxLength - 3] + "..."

    def _fmtMilliseconds(self, value):
        """Lifted from SetupModesController."""
        if isinstance(value, (list, tuple)):
            return "[" + ", ".join(self._fmtMilliseconds(item) for item in value) + "]"

        seconds = self._asFloat(value)
        if seconds is None:
            return self._fmtShort(value)

        return f"{self._fmtDecimal(seconds * 1000)} ms"

    def _fmtDecimal(self, value):
        """Lifted from SetupModesController."""
        text = f"{value:.6f}".rstrip("0").rstrip(".")
        return "0" if text in ("", "-0") else text

    def _onOff(self, value):
        """Lifted from SetupModesController."""
        return "ON" if bool(value) else "OFF"

    def _asFloat(self, value):
        """Lifted from SetupModesController."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _getMissingScanDevices(self, deviceNames, availableNames):
        missing = []
        for deviceName in deviceNames or []:
            if deviceName in (None, 'None'):
                continue
            if deviceName not in availableNames:
                missing.append(str(deviceName))
        return sorted(set(missing))

_attrCategoryStage = 'ScanStage'
_attrCategoryTTL = 'ScanTTL'


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
