import os
import json
import configparser
from ast import literal_eval
from ..basecontrollers import (
    ImConWidgetController,
    StatefulComponentMixin,
    ComponentStateApplyMode,
    SetupModeApplyPriority
)
import traceback
from imswitch.imcommon.model import APIExport, dirtools, initLogger
from imswitch.imcontrol.model import getWidgetStatePersistence
from imswitch.imcontrol.view import guitools
from ._triggerscope_scan_geometry import TriggerScopeScanGeometryMixin, check_firmware_scan_dac_ranges
from ._triggerscope_scan_lifecycle import TriggerScopeScanLifecycleMixin


class TriggerScopePLSRMulticolorController(
    StatefulComponentMixin,
    TriggerScopeScanGeometryMixin,
    TriggerScopeScanLifecycleMixin,
    ImConWidgetController,
):
    """Linked to TriggerScopePLSRMulticolorWidget."""

    componentName = 'TriggerScopePLSRMulticolor'
    stateSchemaVersion = 1
    legacyStateNames = ()
    setupModeCategory = 'scan'
    setupModeApplyPriority = SetupModeApplyPriority.SCAN

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._logger = initLogger(self)

        self.settingAttr = False
        self.settingParameters = False

        self._scanParameterDict = {}
        self._deviceParameterDict = {}

        self.isRunning = False
        self.doingNonFinalPartOfSequence = False

        self.scanDir = os.path.join(dirtools.UserFileDirs.Root, 'imcontrol_scans')
        if not os.path.exists(self.scanDir):
            os.makedirs(self.scanDir)

        self.updateScanParDict()

        self.positioners = {
            pName: pManager for pName, pManager in self._setupInfo.positioners.items()
            if pManager.forScanning
        }
        self.TTLDevices = self._setupInfo.getTTLDevices()

        self._widget.onLaserEdit.addItems(self.TTLDevices.keys())
        self._widget.offLaserEdit.addItems(self.TTLDevices.keys())
        self._widget.roLaserEdit.addItems(self.TTLDevices.keys())
        # This mode is the exception among the RESOLFT panels: it *programs*
        # the camera line into the firmware (``CameraTTLChan``), so the chosen
        # detector has to have a TriggerScope line for the scan to run at all.
        # Offering one without a line would arm the recording and then fail
        # part-way through uploading parameters.
        self._widget.CameraTTLEdit.addItems(
            name for name in self._setupInfo.detectors
            if name in self.TTLDevices
        )
        self._widget.Laser2Edit.addItems(self.TTLDevices.keys())
        self._widget.Laser3Edit.addItems(self.TTLDevices.keys())
        self._widget.roScanDeviceEdit.addItems(self.positioners.keys())
        self._widget.cycleScanDeviceEdit.addItems(self.positioners.keys())
        self._widget.MulticolorScanDeviceEdit.addItems(self.positioners.keys())

        self._initTriggerScopeScanLifecycle()

        self._commChannel.sigRunScan.connect(self.runScanExternal)
        self._commChannel.sigAbortScan.connect(self.abortScan)

        self._widget.sigSaveScanClicked.connect(self.saveScan)
        self._widget.sigLoadScanClicked.connect(self.loadScan)
        self._widget.sigRunScanClicked.connect(self.runScan)
        self._widget.sigParameterChanged.connect(self.updateScanParDict)

        self._commChannel.sigRequestScanParameters.connect(self.sendScanParameters)
        self._commChannel.sigRunScanTriggerScopePLSRMulticolor.connect(self.runScan)

        getWidgetStatePersistence().register('TriggerScopePLSRMulticolor', self)

    def sendScanParameters(self):
        triggerscopeParameters = self.getTriggerscopeParameters()
        self._commChannel.sigSendScanParameters.emit(triggerscopeParameters)

    def saveScan(self):
        fileName = guitools.askForFilePath(self._widget, 'Save scan', self.scanDir, isSaving=True)
        if not fileName:
            return
        self.saveScanParamsToFile(fileName)

    def saveScanParamsToFile(self, filePath: str) -> None:
        """Saves the set scanning parameters to the specified file."""
        self.getParameters()
        config = configparser.ConfigParser()
        config.optionxform = str
        config['scanParameterDict'] = self._scanParameterDict
        config['deviceParameterDict'] = self._deviceParameterDict
        with open(filePath, 'w') as configfile:
            config.write(configfile)

    def loadScan(self):
        fileName = guitools.askForFilePath(self._widget, 'Load scan', self.scanDir)
        if not fileName:
            return
        self.loadScanParamsFromFile(fileName)

    def loadScanParamsFromFile(self, filePath: str) -> None:
        """Loads scanning parameters from the specified file."""
        config = configparser.ConfigParser()
        config.optionxform = str
        config.read(filePath)
        for key in self._scanParameterDict:
            self._scanParameterDict[key] = literal_eval(
                config._sections['scanParameterDict'][key]
            )
        for key in self._deviceParameterDict:
            self._deviceParameterDict[key] = config._sections['deviceParameterDict'][key]
        self.setParameters()
        self.setAllSharedAttr()

    def setParameters(self):
        """Set parameter fields in widget according to parameter values in parameter dictionaries."""
        self.settingParameters = True
        try:
            self._widget.setTimeLapsePoints(self._scanParameterDict['timeLapsePoints'])
            self._widget.setTimeLapseDelayS(self._scanParameterDict['timeLapseDelayS'])
            self._widget.setDelayBeforeOnTimeMs(self._scanParameterDict['delayBeforeOnTimeMs'])
            self._widget.setOnTimeMs(self._scanParameterDict['onTimeMs'])
            self._widget.setDelayAfterOnTimeMs(self._scanParameterDict['delayAfterOnTimeMs'])
            self._widget.setOffTimeMs(self._scanParameterDict['offTimeMs'])
            self._widget.setDelayAfterOffTimeMs(self._scanParameterDict['delayAfterOffTimeMs'])
            self._widget.setDelayAfterDACStepMs(self._scanParameterDict['delayAfterDACStepMs'])
            self._widget.setRoTimeMs(self._scanParameterDict['roTimeMs'])
            self._widget.setDelayAfterRoMs(self._scanParameterDict['delayAfterRoMs'])
            self._widget.setRoRestingPosUm(self._scanParameterDict['roRestingPosUm'])
            self._widget.setRoStartPosUm(self._scanParameterDict['roStartPosUm'])
            self._widget.setRoStepSizeUm(self._scanParameterDict['roStepSizeUm'])
            self._widget.setRoSteps(self._scanParameterDict['roSteps'])
            self._widget.setCycleStartPosUm(self._scanParameterDict['cycleStartPosUm'])
            self._widget.setCycleStepSizeUm(self._scanParameterDict['cycleStepSizeUm'])
            self._widget.setCycleSteps(self._scanParameterDict['cycleSteps'])
            self._widget.setLaser2OnMs(self._scanParameterDict['Laser2OnMs'])
            self._widget.setDelayAfterLaser2Ms(self._scanParameterDict['DelayAfterLaser2Ms'])
            self._widget.setMulticolorScanFirstUm(self._scanParameterDict['MulticolorScanFirstUm'])
            self._widget.setMulticolorScanSecondUm(self._scanParameterDict['MulticolorScanSecondUm'])
            self._widget.setMulticolorScanThirdUm(self._scanParameterDict['MulticolorScanThirdUm'])
            self._widget.setLaser3OnMs(self._scanParameterDict['Laser3OnMs'])
            self._widget.setDelayAfterLaser3Ms(self._scanParameterDict['DelayAfterLaser3Ms'])
            self._widget.setOnLaser(self._deviceParameterDict['onLaser'])
            self._widget.setOffLaser(self._deviceParameterDict['offLaser'])
            self._widget.setRoLaser(self._deviceParameterDict['roLaser'])
            self._widget.setRoScanDevice(self._deviceParameterDict['roScanDevice'])
            self._widget.setCycleScanDevice(self._deviceParameterDict['cycleScanDevice'])
            self._widget.setCameraTTL(self._deviceParameterDict['CameraTTL'])
            self._widget.setLaser2(self._deviceParameterDict['Laser2'])
            self._widget.setLaser3(self._deviceParameterDict['Laser3'])
            self._widget.setMulticolorScanDevice(self._deviceParameterDict['MulticolorScanDevice'])
        finally:
            self.settingParameters = False

    def getTriggerscopeParameters(self):
        self.getParameters()
        deviceParameterDict = self._deviceParameterDict
        scanParameterDict = {}
        roConvFactor = self.positioners[deviceParameterDict['roScanDevice']].managerProperties['conversionFactor']
        # The multicolor scan device is a separate, independently-selected
        # positioner; its µm->V conversion must use its OWN conversionFactor, not
        # the RO device's (cf. LSXYR raster axes). Using roConvFactor here scaled
        # the multicolor positions wrongly whenever the two devices differ.
        multicolorConvFactor = self.positioners[deviceParameterDict['MulticolorScanDevice']].managerProperties['conversionFactor']
        scanParameterDict['onPulseTimeUs'] = int(self._scanParameterDict['onTimeMs'] * 1000)
        scanParameterDict['offPulseTimeUs'] = int(self._scanParameterDict['offTimeMs'] * 1000)
        scanParameterDict['roPulseTimeUs'] = int(self._scanParameterDict['roTimeMs'] * 1000)
        scanParameterDict['timeLapsePoints'] = int(self._scanParameterDict['timeLapsePoints'])
        scanParameterDict['timeLapseDelayUs'] = int(self._scanParameterDict['timeLapseDelayS'] * 1000000)
        scanParameterDict['delayBeforeOnUs'] = int(self._scanParameterDict['delayBeforeOnTimeMs'] * 1000)
        scanParameterDict['delayAfterOnUs'] = int(self._scanParameterDict['delayAfterOnTimeMs'] * 1000)
        scanParameterDict['delayAfterOffUs'] = int(self._scanParameterDict['delayAfterOffTimeMs'] * 1000)
        scanParameterDict['delayAfterDACStepUs'] = int(self._scanParameterDict['delayAfterDACStepMs'] * 1000)
        scanParameterDict['delayAfterRoUs'] = int(self._scanParameterDict['delayAfterRoMs'] * 1000)
        scanParameterDict['roRestingV'] = self._scanParameterDict['roRestingPosUm'] / roConvFactor
        scanParameterDict['roStartV'] = self._scanParameterDict['roStartPosUm'] / roConvFactor
        scanParameterDict['roStepSizeV'] = self._scanParameterDict['roStepSizeUm'] / roConvFactor
        scanParameterDict['roSteps'] = int(self._scanParameterDict['roSteps'])
        scanParameterDict['cycleStartV'] = self._scanParameterDict['cycleStartPosUm'] / roConvFactor
        scanParameterDict['cycleStepSizeV'] = self._scanParameterDict['cycleStepSizeUm'] / roConvFactor
        scanParameterDict['cycleSteps'] = int(self._scanParameterDict['cycleSteps'])
        scanParameterDict['Laser2OnUs'] = int(self._scanParameterDict['Laser2OnMs'] * 1000)
        scanParameterDict['DelayAfterLaser2Us'] = int(self._scanParameterDict['DelayAfterLaser2Ms'] * 1000)
        scanParameterDict['MulticolorScanFirstV'] = self._scanParameterDict['MulticolorScanFirstUm'] / multicolorConvFactor
        scanParameterDict['MulticolorScanSecondV'] = self._scanParameterDict['MulticolorScanSecondUm'] / multicolorConvFactor
        scanParameterDict['Laser3OnUs'] = int(self._scanParameterDict['Laser3OnMs'] * 1000)
        scanParameterDict['DelayAfterLaser3Us'] = int(self._scanParameterDict['DelayAfterLaser3Ms'] * 1000)
        scanParameterDict['MulticolorScanThirdV'] = self._scanParameterDict['MulticolorScanThirdUm'] / multicolorConvFactor
        check_firmware_scan_dac_ranges(
            self.positioners, deviceParameterDict, scanParameterDict, what='multicolor pLS-RESOLFT scan',
        )
        return {'deviceParameters': deviceParameterDict, 'scanParameters': scanParameterDict}

    def runScanExternal(self, recalculateSignals, isNonFinalPartOfSequence):
        return self._runTriggerScopeScanExternal(
            recalculateSignals, isNonFinalPartOfSequence
        )

    def runScanAdvanced(self, *, recalculateSignals=True, isNonFinalPartOfSequence=False,
                        sigScanStartingEmitted):
        """Runs a scan with the set scanning parameters."""
        try:
            self._widget.setScanButtonChecked(True)
            triggerscopeParameters = self.getTriggerscopeParameters()
            self._startTriggerScopeScan(
                parameters=triggerscopeParameters,
                scanType='pLS-RESOLFT_multicolor_Scan',
                laserDevices=self._getScanLaserDevices(),
                sigScanStartingEmitted=sigScanStartingEmitted,
                isNonFinalPartOfSequence=isNonFinalPartOfSequence,
            )
        except Exception:
            self._logger.error(traceback.format_exc())
            self.scanFailed()

    def abortScan(self):
        self._requestTriggerScopeStop()

    def scanDone(self):
        self._onTriggerScopeScanDone()

    def scanFailed(self):
        self._logger.error('Scan failed')
        self._failTriggerScopeScan()

    def getParameters(self):
        """Get parameters from widget field to controller dict."""
        if self.settingParameters:
            return
        self._scanParameterDict['timeLapsePoints'] = self._widget.getTimeLapsePoints()
        self._scanParameterDict['timeLapseDelayS'] = self._widget.getTimeLapseDelayS()
        self._scanParameterDict['delayBeforeOnTimeMs'] = self._widget.getDelayBeforeOnTimeMs()
        self._scanParameterDict['onTimeMs'] = self._widget.getOnTimeMs()
        self._scanParameterDict['delayAfterOnTimeMs'] = self._widget.getDelayAfterOnTimeMs()
        self._scanParameterDict['offTimeMs'] = self._widget.getOffTimeMs()
        self._scanParameterDict['delayAfterOffTimeMs'] = self._widget.getDelayAfterOffTimeMs()
        self._scanParameterDict['delayAfterDACStepMs'] = self._widget.getDelayAfterDACStepMs()
        self._scanParameterDict['roTimeMs'] = self._widget.getRoTimeMs()
        self._scanParameterDict['delayAfterRoMs'] = self._widget.getDelayAfterRoMs()
        self._scanParameterDict['roRestingPosUm'] = self._widget.getRoRestingPosUm()
        self._scanParameterDict['roStartPosUm'] = self._widget.getRoStartPosUm()
        self._scanParameterDict['roStepSizeUm'] = self._widget.getRoStepSizeUm()
        self._scanParameterDict['roSteps'] = self._widget.getRoSteps()
        self._scanParameterDict['cycleStartPosUm'] = self._widget.getCycleStartPosUm()
        self._scanParameterDict['cycleStepSizeUm'] = self._widget.getCycleStepSizeUm()
        self._scanParameterDict['cycleSteps'] = self._widget.getCycleSteps()
        self._scanParameterDict['Laser2OnMs'] = self._widget.getLaser2OnMs()
        self._scanParameterDict['DelayAfterLaser2Ms'] = self._widget.getDelayAfterLaser2Ms()
        self._scanParameterDict['MulticolorScanFirstUm'] = self._widget.getMulticolorScanFirstUm()
        self._scanParameterDict['MulticolorScanSecondUm'] = self._widget.getMulticolorScanSecondUm()
        self._scanParameterDict['Laser3OnMs'] = self._widget.getLaser3OnMs()
        self._scanParameterDict['DelayAfterLaser3Ms'] = self._widget.getDelayAfterLaser3Ms()
        self._scanParameterDict['MulticolorScanThirdUm'] = self._widget.getMulticolorScanThirdUm()
        self._deviceParameterDict['onLaser'] = self._widget.getOnLaser()
        self._deviceParameterDict['offLaser'] = self._widget.getOffLaser()
        self._deviceParameterDict['roLaser'] = self._widget.getRoLaser()
        self._deviceParameterDict['roScanDevice'] = self._widget.getRoScanDevice()
        # Cycle scan reuses the RO device (widget selector is disabled and
        # labelled "hard coded same as RO-device"); force it to the RO selection
        # rather than the disabled combo, which defaulted to positioner 0 and
        # diverged from RO whenever RO wasn't positioner 0.
        self._deviceParameterDict['cycleScanDevice'] = self._widget.getRoScanDevice()
        self._deviceParameterDict['Laser2'] = self._widget.getLaser2()
        self._deviceParameterDict['Laser3'] = self._widget.getLaser3()
        self._deviceParameterDict['MulticolorScanDevice'] = self._widget.getMulticolorScanDevice()
        self._deviceParameterDict['CameraTTL'] = self._widget.getCameraTTL()

    def emitScanSignal(self, signal, *args):
        signal.emit(*args)

    def _getScanLaserDevices(self):
        """Return the unique laser names used by this scan.

        The multicolor scan assigns lasers to roles (on/off/readout plus the
        extra Laser2/Laser3 channels). We collect the device chosen for each
        role, drop anything that isn't a registered laser (e.g. the CameraTTL
        role or an unset/empty field), and de-duplicate. The LaserController
        uses this list (via sigScanDevicesResolved) to arm exactly these lasers.
        """
        self.getParameters()
        laserRoles = ('onLaser', 'offLaser', 'roLaser', 'Laser2', 'Laser3')
        devices = []
        for role in laserRoles:
            device = self._deviceParameterDict.get(role)
            if device and device in self._setupInfo.lasers and device not in devices:
                devices.append(device)
        return devices

    def runScan(self) -> None:
        """Runs a scan with the set scanning parameters."""
        self.runScanAdvanced(sigScanStartingEmitted=False)

    def setSharedAttr(self, category, attr, value):
        self.settingAttr = True
        try:
            self._commChannel.sharedAttrs[(category, attr)] = value
        finally:
            self.settingAttr = False

    def updateScanParDict(self):
        self.getParameters()
        self.setAllSharedAttr()

    def setAllSharedAttr(self):
        for key, value in self._scanParameterDict.items():
            self.setSharedAttr(_attrCategoryScan, key, value)

    def closeEvent(self):
        pass

    def getComponentState(self) -> dict:
        """Snapshot the current TriggerScope PLSR Multicolor scan parameters."""
        self.getParameters()

        scanInfo = getattr(self._setupInfo, 'scan', None)

        return {
            'controller': type(self).__name__,
            'scanWidgetType': getattr(scanInfo, 'scanWidgetType', None),
            'scanParameterDict': dict(self._scanParameterDict),
            'deviceParameterDict': dict(self._deviceParameterDict),
        }

    def applyComponentState(self, state: dict, *, applyMode: ComponentStateApplyMode) -> list[str]:
        """Restore TriggerScope PLSR Multicolor scan parameters from a snapshot.

        CRITICAL SAFETY INVARIANT: This method MUST NEVER start a scan in either mode.
        It only restores scan parameters (scanParameterDict, deviceParameterDict).
        Starting a scan requires explicit user action (runScan/runScanAdvanced).

        Args:
            state: Component state dict from getComponentState().
            applyMode: STARTUP_RESTORE or SETUP_MODE_APPLY (behavior is identical).

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

        scanParameterDict = state.get('scanParameterDict', {})
        deviceParameterDict = state.get('deviceParameterDict', {})

        if not isinstance(scanParameterDict, dict):
            return warnings + ['Saved scan parameters are not a dictionary.']
        if not isinstance(deviceParameterDict, dict):
            return warnings + ['Saved device parameters are not a dictionary.']

        roScanDevice = deviceParameterDict.get('roScanDevice')
        cycleScanDevice = deviceParameterDict.get('cycleScanDevice')
        multicolorScanDevice = deviceParameterDict.get('MulticolorScanDevice')

        missingPositioners = []
        for device in [roScanDevice, cycleScanDevice, multicolorScanDevice]:
            if device and device not in self.positioners:
                missingPositioners.append(device)

        if missingPositioners:
            warnings.append(
                f'Missing scan positioner(s): {", ".join(set(missingPositioners))}. '
                'Scan state was not applied.'
            )
            return warnings

        onLaser = deviceParameterDict.get('onLaser')
        offLaser = deviceParameterDict.get('offLaser')
        roLaser = deviceParameterDict.get('roLaser')
        laser2 = deviceParameterDict.get('Laser2')
        laser3 = deviceParameterDict.get('Laser3')

        cameraTTL = deviceParameterDict.get('CameraTTL')

        missingTTLDevices = []
        # The camera belongs in this list for the multicolor mode alone: it is
        # the one that programs the camera's line into the firmware.
        for device in [onLaser, offLaser, roLaser, laser2, laser3, cameraTTL]:
            if device and device not in self.TTLDevices:
                missingTTLDevices.append(device)

        if missingTTLDevices:
            warnings.append(
                f'Missing TTL device(s): {", ".join(set(missingTTLDevices))}. '
                'Scan state was not applied.'
            )
            return warnings

        self._scanParameterDict = dict(scanParameterDict)
        self._deviceParameterDict = dict(deviceParameterDict)

        try:
            self.setParameters()
            self.setAllSharedAttr()
        except Exception as e:
            self._logger.error('Failed to apply TriggerScope PLSR Multicolor component state')
            self._logger.error(traceback.format_exc())
            warnings.append(f'Failed to apply scan state: {e}')

        return warnings

    def describeComponentState(self, state: dict) -> list[str]:
        """Generate a human-readable summary of a saved TriggerScope PLSR Multicolor scan state."""
        if not isinstance(state, dict) or not state:
            return ["  no scan state"]

        scan = state.get("scanParameterDict") or {}
        device = state.get("deviceParameterDict") or {}
        summaries = []

        if state.get("controller"):
            summaries.append(f"  controller: {state.get('controller')}")
        if state.get("scanWidgetType"):
            summaries.append(f"  widget type: {state.get('scanWidgetType')}")

        if device:
            summaries.append("  devices:")
            for key in ['onLaser', 'offLaser', 'roLaser', 'Laser2', 'Laser3',
                        'roScanDevice', 'cycleScanDevice', 'MulticolorScanDevice']:
                value = device.get(key)
                if value:
                    summaries.append(f"    {key}: {value}")

        if scan:
            summaries.append("  scan parameters:")
            timingKeys = ['timeLapsePoints', 'timeLapseDelayS', 'onTimeMs', 'offTimeMs', 'roTimeMs',
                          'Laser2OnMs', 'DelayAfterLaser2Ms', 'Laser3OnMs', 'DelayAfterLaser3Ms']
            for key in timingKeys:
                value = scan.get(key)
                if value is not None:
                    summaries.append(f"    {key}: {value}")

            roKeys = ['roSteps', 'roStepSizeUm', 'roStartPosUm', 'roRestingPosUm']
            cycleKeys = ['cycleSteps', 'cycleStepSizeUm', 'cycleStartPosUm']
            multicolorKeys = ['MulticolorScanFirstUm', 'MulticolorScanSecondUm', 'MulticolorScanThirdUm']

            for keyList, label in [(roKeys, 'readout'), (cycleKeys, 'cycle'), (multicolorKeys, 'multicolor')]:
                vals = {k: scan.get(k) for k in keyList if scan.get(k) is not None}
                if vals:
                    summaries.append(f"    {label}: {vals}")

        return summaries or ["  no scan state"]

    def getComponentStateHazards(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode,
        context: dict | None = None,
    ) -> list[dict]:
        """Identify potential hazards in a saved TriggerScope PLSR Multicolor scan state.

        Scan parameters carry no laser-power-like hazards; laser hazards belong
        to the Laser component. Returns an empty list.
        """
        return []


_attrCategoryScan = 'MS-RESOLFT_Scan'
_attrCategoryDevices = 'MS-RESOLFT_Dev'
