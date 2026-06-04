import os
import configparser
import traceback
from ast import literal_eval

from imswitch.imcommon.model import dirtools
from imswitch.imcontrol.view import guitools
from ..basecontrollers import ImConWidgetController


_attrCategoryScan = 'MS-RESOLFT_Scan'
_attrCategoryDevices = 'MS-RESOLFT_Dev'


class _ScanModeAdapter:
    """Per-mode parameter adapter.

    Each adapter owns its own parameter dicts and ``settingParameters`` guard so
    the widget<->dict synchronisation of one mode never interferes with the
    other. Subclasses mirror the parameter-building logic of the controller they
    replace, so the unified controller stays a thin lifecycle shell.
    """

    #: scan_type string handed to ScanManagerTriggerScope.runScan()
    scanType = None

    def __init__(self, widget, positioners, lasers):
        self._widget = widget
        self.positioners = positioners
        self._lasers = lasers
        self.scanParameterDict = {}
        self.deviceParameterDict = {}
        self.settingParameters = False

    # -- to be implemented by subclasses --
    def populateCombos(self, ttlKeys, posKeys):
        raise NotImplementedError

    def getParameters(self):
        raise NotImplementedError

    def setParameters(self):
        raise NotImplementedError

    def getTriggerScopeParameters(self):
        raise NotImplementedError

    def getScanLaserDevices(self):
        raise NotImplementedError


class _LightSheetAdapter(_ScanModeAdapter):
    """Mirrors LightSheetMulticolorController's parameter logic."""

    scanType = 'MulticolorScan'

    def populateCombos(self, ttlKeys, posKeys):
        w = self._widget
        w.Laser1Edit.addItems(ttlKeys)
        w.Laser2Edit.addItems(ttlKeys)
        w.Laser3Edit.addItems(ttlKeys)
        w.Laser4Edit.addItems(ttlKeys)
        w.Laser5Edit.addItems(ttlKeys)
        w.CameraTTLEdit.addItems(ttlKeys)
        w.roScanDeviceEdit.addItems(posKeys)
        w.MulticolorScanDeviceEdit.addItems(posKeys)
        w.cycleScanDeviceEdit.addItems(posKeys)

    def getParameters(self):
        if self.settingParameters:
            return
        w = self._widget
        sp = self.scanParameterDict
        dp = self.deviceParameterDict
        sp['timeLapsePoints'] = w.getTimeLapsePoints()
        sp['timeLapseDelayS'] = w.getTimeLapseDelayS()
        sp['Laser1OnMs'] = w.getLaser1OnMs()
        sp['DelayAfterLaser1Ms'] = w.getDelayAfterLaser1Ms()
        sp['Laser2OnMs'] = w.getLaser2OnMs()
        sp['DelayAfterLaser2Ms'] = w.getDelayAfterLaser2Ms()
        sp['Laser3OnMs'] = w.getLaser3OnMs()
        sp['DelayAfterLaser3Ms'] = w.getDelayAfterLaser3Ms()
        sp['Laser4OnMs'] = w.getLaser4OnMs()
        sp['DelayAfterLaser4Ms'] = w.getDelayAfterLaser4Ms()
        sp['Laser5OnMs'] = w.getLaser5OnMs()
        sp['DelayAfterLaser5Ms'] = w.getDelayAfterLaser5Ms()
        sp['roRestingPosUm'] = w.getRoRestingPosUm()
        sp['roStartPosUm'] = w.getRoStartPosUm()
        sp['roStepSizeUm'] = w.getRoStepSizeUm()
        sp['MulticolorScanFirstUm'] = w.getMulticolorScanFirstUm()
        sp['MulticolorScanSecondUm'] = w.getMulticolorScanSecondUm()
        sp['roSteps'] = w.getRoSteps()
        sp['cycleStartPosUm'] = w.getCycleStartPosUm()
        sp['cycleStepSizeUm'] = w.getCycleStepSizeUm()
        sp['cycleSteps'] = w.getCycleSteps()
        dp['Laser1'] = w.getLaser1()
        dp['Laser2'] = w.getLaser2()
        dp['Laser3'] = w.getLaser3()
        dp['Laser4'] = w.getLaser4()
        dp['Laser5'] = w.getLaser5()
        dp['CameraTTL'] = w.getCameraTTL()
        dp['roScanDevice'] = w.getRoScanDevice()
        dp['MulticolorScanDevice'] = w.getMulticolorScanDevice()
        dp['cycleScanDevice'] = w.getCycleScanDevice()

    def setParameters(self):
        self.settingParameters = True
        try:
            w = self._widget
            sp = self.scanParameterDict
            dp = self.deviceParameterDict
            w.setTimeLapsePoints(sp['timeLapsePoints'])
            w.setTimeLapseDelayS(sp['timeLapseDelayS'])
            w.setLaser1OnMs(sp['Laser1OnMs'])
            w.setDelayAfterLaser1Ms(sp['DelayAfterLaser1Ms'])
            w.setLaser2OnMs(sp['Laser2OnMs'])
            w.setDelayAfterLaser2Ms(sp['DelayAfterLaser2Ms'])
            w.setLaser3OnMs(sp['Laser3OnMs'])
            w.setDelayAfterLaser3Ms(sp['DelayAfterLaser3Ms'])
            w.setLaser4OnMs(sp['Laser4OnMs'])
            w.setDelayAfterLaser4Ms(sp['DelayAfterLaser4Ms'])
            w.setLaser5OnMs(sp['Laser5OnMs'])
            w.setDelayAfterLaser5Ms(sp['DelayAfterLaser5Ms'])
            w.setRoRestingPosUm(sp['roRestingPosUm'])
            w.setRoStartPosUm(sp['roStartPosUm'])
            w.setRoStepSizeUm(sp['roStepSizeUm'])
            w.setRoSteps(sp['roSteps'])
            w.setCycleStartPosUm(sp['cycleStartPosUm'])
            w.setCycleStepSizeUm(sp['cycleStepSizeUm'])
            w.setCycleSteps(sp['cycleSteps'])
            w.setMulticolorScanFirstUm(sp['MulticolorScanFirstUm'])
            w.setMulticolorScanSecondUm(sp['MulticolorScanSecondUm'])
            w.setLaser1(dp['Laser1'])
            w.setLaser2(dp['Laser2'])
            w.setLaser3(dp['Laser3'])
            w.setLaser4(dp['Laser4'])
            w.setLaser5(dp['Laser5'])
            w.setCameraTTL(dp['CameraTTL'])
            w.setRoScanDevice(dp['roScanDevice'])
            w.setMulticolorScanDevice(dp['MulticolorScanDevice'])
            w.setCycleScanDevice(dp['cycleScanDevice'])
        finally:
            self.settingParameters = False

    def getTriggerScopeParameters(self):
        self.getParameters()
        dp = self.deviceParameterDict
        roConvFactor = self.positioners[dp['roScanDevice']].managerProperties['conversionFactor']
        sp = {}
        sp['Laser1OnUs']            = int(self.scanParameterDict['Laser1OnMs'] * 1000)
        sp['Laser2OnUs']            = int(self.scanParameterDict['Laser2OnMs'] * 1000)
        sp['Laser3OnUs']            = int(self.scanParameterDict['Laser3OnMs'] * 1000)
        sp['Laser4OnUs']            = int(self.scanParameterDict['Laser4OnMs'] * 1000)
        sp['Laser5OnUs']            = int(self.scanParameterDict['Laser5OnMs'] * 1000)
        sp['timeLapsePoints']       = int(self.scanParameterDict['timeLapsePoints'])
        sp['timeLapseDelayUs']      = int(self.scanParameterDict['timeLapseDelayS'] * 1_000_000)
        sp['DelayAfterLaser1Us']    = int(self.scanParameterDict['DelayAfterLaser1Ms'] * 1000)
        sp['DelayAfterLaser2Us']    = int(self.scanParameterDict['DelayAfterLaser2Ms'] * 1000)
        sp['roRestingV']            = self.scanParameterDict['roRestingPosUm'] / roConvFactor
        sp['roStartV']              = self.scanParameterDict['roStartPosUm'] / roConvFactor
        sp['roStepSizeV']           = self.scanParameterDict['roStepSizeUm'] / roConvFactor
        sp['MulticolorScanFirstV']  = self.scanParameterDict['MulticolorScanFirstUm'] / roConvFactor
        sp['MulticolorScanSecondV'] = self.scanParameterDict['MulticolorScanSecondUm'] / roConvFactor
        sp['roSteps']               = int(self.scanParameterDict['roSteps'])
        sp['cycleStartV']           = self.scanParameterDict['cycleStartPosUm'] / roConvFactor
        sp['cycleStepSizeV']        = self.scanParameterDict['cycleStepSizeUm'] / roConvFactor
        sp['cycleSteps']            = int(self.scanParameterDict['cycleSteps'])
        return {'deviceParameters': dp, 'scanParameters': sp}

    def getScanLaserDevices(self):
        self.getParameters()
        devices = []
        for device in self.deviceParameterDict.values():
            if device and device in self._lasers and device not in devices:
                devices.append(device)
        return devices


class _PLSRMulticolorAdapter(_ScanModeAdapter):
    """Mirrors TriggerScopePLSRMulticolorController's parameter logic."""

    scanType = 'pLS-RESOLFT_multicolor_Scan'

    def populateCombos(self, ttlKeys, posKeys):
        w = self._widget
        w.onLaserEdit.addItems(ttlKeys)
        w.offLaserEdit.addItems(ttlKeys)
        w.roLaserEdit.addItems(ttlKeys)
        w.CameraTTLEdit.addItems(ttlKeys)
        w.Laser2Edit.addItems(ttlKeys)
        w.Laser3Edit.addItems(ttlKeys)
        w.roScanDeviceEdit.addItems(posKeys)
        w.cycleScanDeviceEdit.addItems(posKeys)
        w.MulticolorScanDeviceEdit.addItems(posKeys)

    def getParameters(self):
        if self.settingParameters:
            return
        w = self._widget
        sp = self.scanParameterDict
        dp = self.deviceParameterDict
        sp['timeLapsePoints'] = w.getTimeLapsePoints()
        sp['timeLapseDelayS'] = w.getTimeLapseDelayS()
        sp['delayBeforeOnTimeMs'] = w.getDelayBeforeOnTimeMs()
        sp['onTimeMs'] = w.getOnTimeMs()
        sp['delayAfterOnTimeMs'] = w.getDelayAfterOnTimeMs()
        sp['offTimeMs'] = w.getOffTimeMs()
        sp['delayAfterOffTimeMs'] = w.getDelayAfterOffTimeMs()
        sp['delayAfterDACStepMs'] = w.getDelayAfterDACStepMs()
        sp['roTimeMs'] = w.getRoTimeMs()
        sp['delayAfterRoMs'] = w.getDelayAfterRoMs()
        sp['roRestingPosUm'] = w.getRoRestingPosUm()
        sp['roStartPosUm'] = w.getRoStartPosUm()
        sp['roStepSizeUm'] = w.getRoStepSizeUm()
        sp['roSteps'] = w.getRoSteps()
        sp['cycleStartPosUm'] = w.getCycleStartPosUm()
        sp['cycleStepSizeUm'] = w.getCycleStepSizeUm()
        sp['cycleSteps'] = w.getCycleSteps()
        sp['Laser2OnMs'] = w.getLaser2OnMs()
        sp['DelayAfterLaser2Ms'] = w.getDelayAfterLaser2Ms()
        sp['MulticolorScanFirstUm'] = w.getMulticolorScanFirstUm()
        sp['MulticolorScanSecondUm'] = w.getMulticolorScanSecondUm()
        sp['Laser3OnMs'] = w.getLaser3OnMs()
        sp['DelayAfterLaser3Ms'] = w.getDelayAfterLaser3Ms()
        sp['MulticolorScanThirdUm'] = w.getMulticolorScanThirdUm()
        dp['onLaser'] = w.getOnLaser()
        dp['offLaser'] = w.getOffLaser()
        dp['roLaser'] = w.getRoLaser()
        dp['roScanDevice'] = w.getRoScanDevice()
        dp['cycleScanDevice'] = w.getCycleScanDevice()
        dp['Laser2'] = w.getLaser2()
        dp['Laser3'] = w.getLaser3()
        dp['MulticolorScanDevice'] = w.getMulticolorScanDevice()
        dp['CameraTTL'] = w.getCameraTTL()

    def setParameters(self):
        self.settingParameters = True
        try:
            w = self._widget
            sp = self.scanParameterDict
            dp = self.deviceParameterDict
            w.setTimeLapsePoints(sp['timeLapsePoints'])
            w.setTimeLapseDelayS(sp['timeLapseDelayS'])
            w.setDelayBeforeOnTimeMs(sp['delayBeforeOnTimeMs'])
            w.setOnTimeMs(sp['onTimeMs'])
            w.setDelayAfterOnTimeMs(sp['delayAfterOnTimeMs'])
            w.setOffTimeMs(sp['offTimeMs'])
            w.setDelayAfterOffTimeMs(sp['delayAfterOffTimeMs'])
            w.setDelayAfterDACStepMs(sp['delayAfterDACStepMs'])
            w.setRoTimeMs(sp['roTimeMs'])
            w.setDelayAfterRoMs(sp['delayAfterRoMs'])
            w.setRoRestingPosUm(sp['roRestingPosUm'])
            w.setRoStartPosUm(sp['roStartPosUm'])
            w.setRoStepSizeUm(sp['roStepSizeUm'])
            w.setRoSteps(sp['roSteps'])
            w.setCycleStartPosUm(sp['cycleStartPosUm'])
            w.setCycleStepSizeUm(sp['cycleStepSizeUm'])
            w.setCycleSteps(sp['cycleSteps'])
            w.setLaser2OnMs(sp['Laser2OnMs'])
            w.setDelayAfterLaser2Ms(sp['DelayAfterLaser2Ms'])
            w.setMulticolorScanFirstUm(sp['MulticolorScanFirstUm'])
            w.setMulticolorScanSecondUm(sp['MulticolorScanSecondUm'])
            w.setMulticolorScanThirdUm(sp['MulticolorScanThirdUm'])
            w.setLaser3OnMs(sp['Laser3OnMs'])
            w.setDelayAfterLaser3Ms(sp['DelayAfterLaser3Ms'])
            w.setOnLaser(dp['onLaser'])
            w.setOffLaser(dp['offLaser'])
            w.setRoLaser(dp['roLaser'])
            w.setRoScanDevice(dp['roScanDevice'])
            w.setCycleScanDevice(dp['cycleScanDevice'])
            w.setCameraTTL(dp['CameraTTL'])
            w.setLaser2(dp['Laser2'])
            w.setLaser3(dp['Laser3'])
            w.setMulticolorScanDevice(dp['MulticolorScanDevice'])
        finally:
            self.settingParameters = False

    def getTriggerScopeParameters(self):
        self.getParameters()
        deviceParameterDict = self.deviceParameterDict
        scanParameterDict = {}
        roConvFactor = self.positioners[deviceParameterDict['roScanDevice']].managerProperties['conversionFactor']
        scanParameterDict['onPulseTimeUs'] = int(self.scanParameterDict['onTimeMs'] * 1000)
        scanParameterDict['offPulseTimeUs'] = int(self.scanParameterDict['offTimeMs'] * 1000)
        scanParameterDict['roPulseTimeUs'] = int(self.scanParameterDict['roTimeMs'] * 1000)
        scanParameterDict['timeLapsePoints'] = int(self.scanParameterDict['timeLapsePoints'])
        scanParameterDict['timeLapseDelayUs'] = int(self.scanParameterDict['timeLapseDelayS'] * 1000000)
        scanParameterDict['delayBeforeOnUs'] = int(self.scanParameterDict['delayBeforeOnTimeMs'] * 1000)
        scanParameterDict['delayAfterOnUs'] = int(self.scanParameterDict['delayAfterOnTimeMs'] * 1000)
        scanParameterDict['delayAfterOffUs'] = int(self.scanParameterDict['delayAfterOffTimeMs'] * 1000)
        scanParameterDict['delayAfterDACStepUs'] = int(self.scanParameterDict['delayAfterDACStepMs'] * 1000)
        scanParameterDict['delayAfterRoUs'] = int(self.scanParameterDict['delayAfterRoMs'] * 1000)
        scanParameterDict['roRestingV'] = self.scanParameterDict['roRestingPosUm'] / roConvFactor
        scanParameterDict['roStartV'] = self.scanParameterDict['roStartPosUm'] / roConvFactor
        scanParameterDict['roStepSizeV'] = self.scanParameterDict['roStepSizeUm'] / roConvFactor
        scanParameterDict['roSteps'] = int(self.scanParameterDict['roSteps'])
        scanParameterDict['cycleStartV'] = self.scanParameterDict['cycleStartPosUm'] / roConvFactor
        scanParameterDict['cycleStepSizeV'] = self.scanParameterDict['cycleStepSizeUm'] / roConvFactor
        scanParameterDict['cycleSteps'] = int(self.scanParameterDict['cycleSteps'])
        scanParameterDict['Laser2OnUs'] = int(self.scanParameterDict['Laser2OnMs'] * 1000)
        scanParameterDict['DelayAfterLaser2Us'] = int(self.scanParameterDict['DelayAfterLaser2Ms'] * 1000)
        scanParameterDict['MulticolorScanFirstV'] = self.scanParameterDict['MulticolorScanFirstUm'] / roConvFactor
        scanParameterDict['MulticolorScanSecondV'] = self.scanParameterDict['MulticolorScanSecondUm'] / roConvFactor
        scanParameterDict['Laser3OnUs'] = int(self.scanParameterDict['Laser3OnMs'] * 1000)
        scanParameterDict['DelayAfterLaser3Us'] = int(self.scanParameterDict['DelayAfterLaser3Ms'] * 1000)
        scanParameterDict['MulticolorScanThirdV'] = self.scanParameterDict['MulticolorScanThirdUm'] / roConvFactor
        return {'deviceParameters': deviceParameterDict, 'scanParameters': scanParameterDict}

    def getScanLaserDevices(self):
        self.getParameters()
        laserRoles = ('onLaser', 'offLaser', 'roLaser', 'Laser2', 'Laser3')
        devices = []
        for role in laserRoles:
            device = self.deviceParameterDict.get(role)
            if device and device in self._lasers and device not in devices:
                devices.append(device)
        return devices


class TriggerScopeScanController(ImConWidgetController):
    """Unified controller for the two RESOLFT-family TriggerScope scans.

    Hosts a multicolor light-sheet panel and a pLS-RESOLFT multicolor panel
    behind one widget; the visible panel selects which ``_ScanModeAdapter``
    builds the scan. The board-autonomous lifecycle (start/built/done) is shared
    and written once. The external pLS-RESOLFT-multicolor trigger API (used by
    EtSnouty) is always served by the PLSR adapter, independent of the visible
    mode.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.settingAttr = False
        self.isRunning = False
        self.doingNonFinalPartOfSequence = False

        self.scanDir = os.path.join(dirtools.UserFileDirs.Root, 'imcontrol_scans')
        if not os.path.exists(self.scanDir):
            os.makedirs(self.scanDir)

        self.positioners = {
            pName: pManager
            for pName, pManager in self._setupInfo.positioners.items()
            if pManager.forScanning
        }
        self.TTLDevices = self._setupInfo.getTTLDevices()
        lasers = self._setupInfo.lasers

        self._adapters = {
            self._widget.MODE_LIGHTSHEET: _LightSheetAdapter(
                self._widget.lsPage, self.positioners, lasers),
            self._widget.MODE_PLSR: _PLSRMulticolorAdapter(
                self._widget.plsrPage, self.positioners, lasers),
        }
        ttlKeys = list(self.TTLDevices.keys())
        posKeys = list(self.positioners.keys())
        for adapter in self._adapters.values():
            adapter.populateCombos(ttlKeys, posKeys)

        self.updateScanParDict()

        # ScanManagerTriggerScope signals
        self._master.scanManager.sigScanStarted.connect(
            lambda: self.emitScanSignal(self._commChannel.sigScanStarted)
        )
        self._master.scanManager.sigScanDone.connect(self.scanDone)

        self._commChannel.sigRunScan.connect(self.runScanExternal)
        self._commChannel.sigAbortScan.connect(self.abortScan)

        self._widget.sigSaveScanClicked.connect(self.saveScan)
        self._widget.sigLoadScanClicked.connect(self.loadScan)
        self._widget.sigRunScanClicked.connect(self.runScan)
        self._widget.sigParameterChanged.connect(self.updateScanParDict)
        self._widget.sigModeChanged.connect(self.updateScanParDict)

        # External pLS-RESOLFT multicolor API (e.g. EtSnouty event-triggered
        # acquisition). Always answered by the PLSR adapter, regardless of which
        # mode is currently visible.
        self._commChannel.sigRequestScanParameters.connect(self.sendScanParameters)
        self._commChannel.sigRunScanTriggerScopePLSRMulticolor.connect(
            self.runScanPLSRMulticolorExternal)

    # ------------------------------------------------------------------
    # Mode helpers
    # ------------------------------------------------------------------

    def _activeAdapter(self):
        return self._adapters[self._widget.currentMode()]

    # ------------------------------------------------------------------
    # Save / load (per visible mode)
    # ------------------------------------------------------------------

    def saveScan(self):
        fileName = guitools.askForFilePath(self._widget, 'Save scan', self.scanDir, isSaving=True)
        if not fileName:
            return
        self.saveScanParamsToFile(fileName)

    def saveScanParamsToFile(self, filePath: str) -> None:
        """Saves the set scanning parameters of the visible mode to a file."""
        adapter = self._activeAdapter()
        adapter.getParameters()
        config = configparser.ConfigParser()
        config.optionxform = str
        config['scanParameterDict'] = adapter.scanParameterDict
        config['deviceParameterDict'] = adapter.deviceParameterDict
        with open(filePath, 'w') as configfile:
            config.write(configfile)

    def loadScan(self):
        fileName = guitools.askForFilePath(self._widget, 'Load scan', self.scanDir)
        if not fileName:
            return
        self.loadScanParamsFromFile(fileName)

    def loadScanParamsFromFile(self, filePath: str) -> None:
        """Loads scanning parameters into the visible mode from a file."""
        adapter = self._activeAdapter()
        config = configparser.ConfigParser()
        config.optionxform = str
        config.read(filePath)
        for key in adapter.scanParameterDict:
            adapter.scanParameterDict[key] = literal_eval(
                config._sections['scanParameterDict'][key]
            )
        for key in adapter.deviceParameterDict:
            adapter.deviceParameterDict[key] = config._sections['deviceParameterDict'][key]
        adapter.setParameters()
        self.setAllSharedAttr()

    # ------------------------------------------------------------------
    # Scan execution
    # ------------------------------------------------------------------

    def runScanExternal(self, recalculateSignals, isNonFinalPartOfSequence):
        self._widget.setRepeatEnabled(False)
        self.runScanAdvanced(recalculateSignals=recalculateSignals,
                             isNonFinalPartOfSequence=isNonFinalPartOfSequence,
                             sigScanStartingEmitted=True)

    def runScanAdvanced(self, *, recalculateSignals=True, isNonFinalPartOfSequence=False,
                        sigScanStartingEmitted):
        """Runs a scan with the set scanning parameters of the visible mode."""
        if self._widget.autoStartRec.isChecked():
            self._commChannel.sigStartRecording.emit()
        try:
            adapter = self._activeAdapter()
            self._widget.setScanButtonChecked(True)
            self.isRunning = True
            self.doingNonFinalPartOfSequence = isNonFinalPartOfSequence
            if not sigScanStartingEmitted:
                self.emitScanSignal(self._commChannel.sigScanStarting)
            # Declare which lasers participate so the LaserController can arm
            # them (digital-modulation / external-control) and leave the rest
            # off. TriggerScope runs the scan autonomously, so this signal is
            # emitted here rather than from the DAQ manager.
            self.emitScanSignal(self._commChannel.sigScanBuilt,
                                adapter.getScanLaserDevices())
            params = adapter.getTriggerScopeParameters()
            self._master.scanManager.runScan(params, scan_type=adapter.scanType)
        except Exception:
            self._logger.error(traceback.format_exc())
            self.isRunning = False

    def runScan(self) -> None:
        """Runs a scan with the set scanning parameters of the visible mode."""
        self.runScanAdvanced(sigScanStartingEmitted=False)

    def runScanPLSRMulticolorExternal(self) -> None:
        """External entry point (EtSnouty) for a pLS-RESOLFT multicolor scan.

        Forces the visible mode to pLS-RESOLFT multicolor so the active adapter
        and the shared scan attributes (read by Snouty reconstruction) reflect
        what actually runs, then starts the scan.
        """
        self._widget.setMode(self._widget.MODE_PLSR)
        self.runScan()

    def abortScan(self):
        self.doingNonFinalPartOfSequence = False
        if not self.isRunning:
            self.scanFailed()

    def scanDone(self):
        # All TriggerScope scan controllers share the board-level sigScanDone, so
        # every one of them receives this when the firmware reports end-of-scan.
        # Only the controller that actually started the scan should tear down;
        # the rest must ignore it (their isRunning is False) to avoid redundant
        # laser disarm rounds and duplicate sigScanEnded emissions.
        if not self.isRunning:
            return
        self._logger.debug('Scan done')
        self.isRunning = False
        self.emitScanSignal(self._commChannel.sigScanDone)
        if not self.doingNonFinalPartOfSequence:
            self._widget.setScanButtonChecked(False)
            self.emitScanSignal(self._commChannel.sigScanEnded)
        if self._widget.autoStopRec.isChecked():
            self._commChannel.sigStopRecording.emit()

    def scanFailed(self):
        self._logger.error('Scan failed')
        self.isRunning = False
        self.doingNonFinalPartOfSequence = False
        self._widget.setScanButtonChecked(False)
        self.emitScanSignal(self._commChannel.sigScanEnded)

    def emitScanSignal(self, signal, *args):
        signal.emit(*args)

    # ------------------------------------------------------------------
    # External pLS-RESOLFT multicolor parameter API
    # ------------------------------------------------------------------

    def sendScanParameters(self):
        # Always report the pLS-RESOLFT multicolor parameters, independent of the
        # visible mode, so EtSnouty event-triggered acquisition is unaffected by
        # the GUI selection.
        triggerscopeParameters = self._adapters[
            self._widget.MODE_PLSR].getTriggerScopeParameters()
        self._commChannel.sigSendScanParameters.emit(triggerscopeParameters)

    # ------------------------------------------------------------------
    # Shared attributes
    # ------------------------------------------------------------------

    def setSharedAttr(self, category, attr, value):
        self.settingAttr = True
        try:
            self._commChannel.sharedAttrs[(category, attr)] = value
        finally:
            self.settingAttr = False

    def updateScanParDict(self):
        self._activeAdapter().getParameters()
        self.setAllSharedAttr()

    def setAllSharedAttr(self):
        for key, value in self._activeAdapter().scanParameterDict.items():
            self.setSharedAttr(_attrCategoryScan, key, value)

    def closeEvent(self):
        pass


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
